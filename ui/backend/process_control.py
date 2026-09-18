"""Safe background process control for the fixed `lec` command surface."""
import asyncio
import json
import math
import os
import re
import subprocess
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


class LecProcessLauncher:
    """Spawn a detached CLI child and asynchronously reap it while the API lives."""

    def __init__(self, command, cwd, log_path, settle_seconds=0.15):
        self.command = tuple(str(part) for part in command)
        self.cwd = Path(cwd)
        self.log_path = Path(log_path)
        self.settle_seconds = float(settle_seconds)
        self._tasks = set()

    @classmethod
    def for_client(cls, client, log_path):
        return cls(client.command, client.cwd, log_path)

    async def _reap(self, process):
        await process.wait()

    async def start(self, *args):
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

        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "ab") as output:
                start_offset = output.tell()
                output.write(marker)
                output.flush()
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, **kwargs,
                )
        except OSError as exc:
            raise LecCommandError(
                "cli_unavailable", f"Unable to start lec: {exc}", stderr=str(exc),
            ) from exc

        await asyncio.sleep(self.settle_seconds)
        if process.returncode is not None:
            await process.wait()
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

        task = asyncio.create_task(self._reap(process))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
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
                "run_active", f"Another lecture process is already running: {course}", 409,
            )

    async def start_run(self, course, input_file=None, model=None, source=None, overrides=None):
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
        async with self._lock:
            await self._ensure_idle()
            result = await self.launcher.start(*args)
        return {"accepted": True, "operation": "run", "pid": result.pid,
                "completed": result.completed, "exit_code": result.exit_code,
                "message": ("Lecture process completed" if result.completed
                            else "Lecture process started")}

    async def stop(self, force=False):
        async with self._lock:
            message = await self.client.stop(force=force)
        return {"accepted": True, "operation": "stop", "force": force,
                "message": message or "Stop request sent"}

    async def summarize(self, session_id, redo=None, model=None, course=None):
        session = self.sessions.path_for(session_id)
        args = ["summarize", str(session)]
        if redo:
            args += ["--redo", redo]
        if model:
            args += ["--model", _cli_value(model, "model")]
        if course:
            args += ["--course", _cli_value(course, "course")]
        async with self._lock:
            await self._ensure_idle()
            result = await self.launcher.start(*args)
        return {"accepted": True, "operation": "summarize", "pid": result.pid,
                "completed": result.completed, "exit_code": result.exit_code,
                "message": ("Summary process completed" if result.completed
                            else "Summary process started")}
