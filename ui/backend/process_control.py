"""Safe background process control for the fixed `lec` command surface."""
import asyncio
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .cli_client import LecCommandError


_OVERRIDE_KEY = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$")


@dataclass(frozen=True)
class LaunchResult:
    pid: int
    completed: bool = False
    exit_code: int | None = None
    session: str | None = None


class ControlError(Exception):
    def __init__(self, code, message, status_code=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_detail(self):
        return {"code": self.code, "message": self.message}


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not supported")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"unsupported value type: {type(value).__name__}")


def _override_args(overrides):
    args = []
    for key in sorted(overrides):
        if not _OVERRIDE_KEY.fullmatch(key):
            raise ControlError(
                "invalid_override", f"Invalid configuration override: {key}", 400,
            )
        try:
            value = _toml_value(overrides[key])
        except ValueError as exc:
            raise ControlError(
                "invalid_override", f"Invalid value for {key}: {exc}", 400,
            ) from exc
        args += ["--set", f"{key}={value}"]
    return args


def _cli_value(value, name):
    if "\x00" in value or value.startswith("-"):
        raise ControlError("invalid_argument", f"Invalid {name}: {value!r}", 400)
    return value


def _process_parents():
    """Read an OS process snapshot without importing core or adding dependencies."""
    if os.name != "nt":
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="], check=True,
            capture_output=True, text=True, timeout=2,
        )
        return dict(tuple(map(int, line.split())) for line in result.stdout.splitlines())

    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        parents = {}
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            parents[entry.pid] = entry.parent
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
        return parents
    finally:
        kernel.CloseHandle(snapshot)


def _is_descendant(pid, ancestor):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        parents = _process_parents()
    except (OSError, ValueError, subprocess.SubprocessError):
        return False  # Fail closed; a later poll may confirm startup.
    seen = set()
    while pid in parents and pid not in seen:
        seen.add(pid)
        pid = parents[pid]
        if pid == ancestor:
            return True
    return False


class LecProcessLauncher:
    """Spawn a detached CLI child and asynchronously reap it while the API lives."""

    def __init__(self, command, cwd, log_path, settle_seconds=0.15):
        self.command = tuple(str(part) for part in command)
        self.cwd = Path(cwd)
        self.log_path = Path(log_path)
        self.settle_seconds = float(settle_seconds)
        self._processes = set()
        self._tasks = set()

    @classmethod
    def for_client(cls, client, log_path):
        return cls(client.command, client.cwd, log_path)

    async def _reap(self, process):
        while process.poll() is None:
            await asyncio.sleep(0.1)
        process.wait()
        self._processes.discard(process)

    async def start(self, *args, wait_for_session=None, startup_timeout=30):
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        command = (*self.command, *map(str, args))
        marker = (f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                  f"start {json.dumps(command, ensure_ascii=False)}\n").encode("utf-8")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        envelope = tempfile.TemporaryFile() if wait_for_session else None
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "ab") as output:
                start_offset = output.tell()
                output.write(marker)
                output.flush()
                process = subprocess.Popen(
                    command, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                    stdout=envelope if envelope is not None else output,
                    stderr=output if envelope is not None else subprocess.STDOUT, **kwargs,
                )
        except OSError as exc:
            if envelope is not None:
                envelope.close()
            raise LecCommandError(
                "cli_unavailable", f"Unable to start lec: {exc}", stderr=str(exc),
            ) from exc

        self._processes.add(process)
        task = asyncio.create_task(self._reap(process))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if wait_for_session is not None:
            try:
                deadline = asyncio.get_running_loop().time() + startup_timeout
                while True:
                    if process.poll() is not None:
                        process.wait()
                        envelope.seek(0)
                        try:
                            payload = json.loads(envelope.read(1024 * 1024))
                        except (ValueError, UnicodeError):
                            raise ControlError("cli_invalid_json", "Invalid meeting startup response", 502) from None
                        if not isinstance(payload, dict) or not isinstance(payload.get("accepted"), bool):
                            raise ControlError("cli_invalid_response", "Invalid meeting startup response", 502)
                        if not payload["accepted"]:
                            error = payload.get("error") or {}
                            if (not isinstance(error, dict) or not isinstance(error.get("code"), str)
                                    or not isinstance(error.get("message"), str)):
                                raise ControlError("cli_invalid_response", "Invalid meeting startup error", 502)
                            code = error["code"]
                            http = (409 if code == "busy" else
                                    422 if code in {"source_missing", "source_incomplete", "source_hash_mismatch"} else
                                    400 if code == "input_invalid" or process.returncode == 2 else 502)
                            raise ControlError(code, error.get("message", "Meeting startup failed"), http)
                        if process.returncode not in (0, 130) or not payload.get("session"):
                            raise ControlError("cli_start_failed", "Meeting startup failed", 502)
                        return LaunchResult(process.pid, completed=True, exit_code=process.returncode,
                                            session=payload["session"])
                    status = await wait_for_session()
                    # A different CLI may have won the lock. Only acknowledge
                    # our child after it publishes its own allocated session.
                    if status.get("running") and status.get("session"):
                        pid = status.get("pid")
                        owned = pid == process.pid or await asyncio.to_thread(
                            _is_descendant, pid, process.pid,
                        )
                        # Windows venv python.exe forwards to a child interpreter.
                        # Recheck our launcher after the OS snapshot, too.
                        if owned and process.poll() is None:
                            return LaunchResult(process.pid, session=status["session"])
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ControlError("startup_pending",
                                           "Startup not confirmed; check status before retrying", 503)
                    await asyncio.sleep(0.05)
            finally:
                # The detached child keeps its own descriptor, even on request
                # cancellation. A browser disconnect must never stop the job.
                envelope.close()

        await asyncio.sleep(self.settle_seconds)
        if process.poll() is not None:
            process.wait()
            try:
                with open(self.log_path, "rb") as output:
                    output.seek(start_offset)
                    detail = output.read()[-4000:].decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            if process.returncode:
                raise LecCommandError(
                    "cli_start_failed", detail or "lec exited during startup",
                    exit_code=process.returncode, stderr=detail,
                )
            return LaunchResult(process.pid, completed=True, exit_code=0)
        return LaunchResult(process.pid)


class ProcessController:
    def __init__(self, client, launcher, sessions, repo_root):
        self.client = client
        self.launcher = launcher
        self.sessions = sessions
        self.repo_root = Path(repo_root).resolve()
        self._lock = asyncio.Lock()

    async def _ensure_idle(self):
        status = await self.client.status()
        if status.get("running"):
            course = status.get("course") or "unknown course"
            raise ControlError(
                "run_active", f"Another work item is already running: {course}", 409,
            )

    async def _start(self, *args):
        try:
            return await self.launcher.start(*args)
        except LecCommandError as exc:
            if exc.code == "cli_start_failed":
                # Another CLI/API process may have acquired the shared lock since
                # _ensure_idle. Preserve legacy CLI exit codes, return HTTP 409.
                await self._ensure_idle()
            raise

    async def start_run(self, course, input_file=None, model=None, source=None, overrides=None, upstream=None):
        args = ["run", _cli_value(course, "course")]
        if input_file:
            path = Path(input_file).expanduser()
            if not path.is_absolute():
                path = self.repo_root / path
            path = path.resolve()
            if not path.is_file():
                raise ControlError("input_file_not_found", f"Input file not found: {path}", 400)
            args += ["--file", str(path)]
        if model:
            args += ["--model", _cli_value(model, "model")]
        if source:
            args += ["--source", _cli_value(source, "source")]
        args += _override_args(overrides or {})
        if upstream:
            args += ["--upstream", _cli_value(upstream, "upstream")]
        async with self._lock:
            await self._ensure_idle()
            result = await self._start(*args)
        return {"accepted": True, "operation": "run", "pid": result.pid,
                "completed": result.completed, "exit_code": result.exit_code,
                "message": ("Lecture process completed" if result.completed
                            else "Lecture process started")}

    async def _meeting_start(self, operation, *args):
        async with self._lock:
            await self._ensure_idle()
            result = await self.launcher.start(
                "meeting", operation, *args, "--json", wait_for_session=self.client.status,
                startup_timeout=getattr(self.client, "timeout", 30),
            )
        return {"accepted": True, "operation": operation, "work_type": "meeting",
                "pid": result.pid, "session": result.session,
                "completed": result.completed, "exit_code": result.exit_code,
                "message": "Meeting completed" if result.completed else "Meeting started"}

    async def start_meeting(self, meeting_id, speakers, input_file=None, source=None, original_name=None):
        # Existence and portable-id validation stay in the settings store/API.
        args = [meeting_id, "--speakers", str(speakers)]
        if original_name is not None:
            # Display metadata only. Sanitize before argv too (NUL cannot be
            # passed to subprocess); the CLI repeats this for direct callers.
            original_name = "".join(char for char in original_name if char.isprintable()).strip()[:255]
            if not input_file or not original_name:
                raise ControlError("input_invalid", "Original name requires an import file and visible text", 400)
            args.append(f"--original-name={original_name}")
        if input_file is not None:
            if "\x00" in input_file or not re.fullmatch(r"[A-Za-z0-9]+", Path(input_file).suffix.lstrip(".")):
                raise ControlError("input_invalid", "Input audio path requires a valid file extension", 400)
            path = Path(input_file).expanduser()
            if not path.is_absolute():
                path = self.repo_root / path
            if not path.is_file():
                raise ControlError("source_missing", "Input audio file not found", 422)
            args += ["--file", str(path.resolve())]
        if source is not None:
            args += ["--source", _cli_value(source, "source")]
        return await self._meeting_start("run", *args)

    async def diarize(self, session_id, speakers):
        session = await asyncio.to_thread(self.sessions.path_for, session_id)
        # Only read metadata here. The CLI verifies the source hash under the
        # shared OS lock; no audio reads or core imports on the API side.
        detail = await asyncio.to_thread(self.sessions.get, session_id)
        if detail["session"].get("work_type") != "meeting":
            raise ControlError("input_invalid", "Session is not a meeting", 400)
        if not detail["session"]["has_recording"]:
            raise ControlError("source_missing", "No reusable meeting source", 422)
        return await self._meeting_start("diarize", str(session), "--speakers", str(speakers))

    async def stop(self, force=False):
        async with self._lock:
            message = await self.client.stop(force=force)
        return {"accepted": True, "operation": "stop", "force": force,
                "message": message or "Stop request sent"}

    async def summarize(self, session_id, redo=None, model=None, course=None, upstream=None):
        session = self.sessions.path_for(session_id)
        args = ["summarize", str(session)]
        if upstream:
            args += ["--upstream", _cli_value(upstream, "upstream")]
        if redo:
            args += ["--redo", redo]
        if model:
            args += ["--model", _cli_value(model, "model")]
        if course:
            args += ["--course", _cli_value(course, "course")]
        async with self._lock:
            await self._ensure_idle()
            result = await self._start(*args)
        return {"accepted": True, "operation": "summarize", "pid": result.pid,
                "completed": result.completed, "exit_code": result.exit_code,
                "message": ("Summary process completed" if result.completed
                            else "Summary process started")}
