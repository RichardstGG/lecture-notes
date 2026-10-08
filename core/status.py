"""給 UI 讀的狀態檔：
- <輸出資料夾>/status.json：目前狀態快照（每 system.status_interval 秒覆寫）
- <輸出資料夾>/events.jsonl：只追加的事件紀錄
- <state_dir>/run.json：目前是否有 lec run 在執行（同一時間只允許一個）
"""
import errno
import json
import os
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .config import normalize_work_type
from .util import pid_alive, read_json, write_json

STATUS_SCHEMA_VERSION = 2
EVENT_SCHEMA_VERSION = 1
RUN_SCHEMA_VERSION = 2


def normalize_status(value):
    """Read schema 1/2 without rewriting files or guessing unknown work types."""
    result = dict(value) if isinstance(value, dict) else {}
    result["work_type"] = normalize_work_type(result.get("work_type"))
    result.setdefault("stop_reason", None)
    result.setdefault("diarization", None)
    return result


def _try_lock(stream):
    """Nonblocking local-filesystem lock; the sidecar is never replaced/unlinked."""
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


def _unlock(stream):
    if os.name == "nt":
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _event_sequence(path):
    """Return the last event sequence, including legacy JSONL rows without seq."""
    valid_rows = 0
    highest_seq = 0
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            valid_rows += 1
            value = rec.get("seq")
            if isinstance(value, int) and not isinstance(value, bool):
                highest_seq = max(highest_seq, value)
    except OSError:
        pass
    return max(valid_rows, highest_seq)


class Status:
    def __init__(self, session_dir, interval=2, **initial):
        self.dir = Path(session_dir)
        self.interval = interval
        self._lock = threading.RLock()
        self._data = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "work_type": "lecture", "stop_reason": None, "diarization": None,
            "phase": "starting", "pid": os.getpid(), "started_at": now_iso(),
            "course": None, "session": str(self.dir), "mode": None, "input_file": "",
            "summary_model": None,
            "elapsed": 0, "transcribed": 0, "transcribe_lag": None, "queue": 0,
            "sections_total": 0, "sections_summarized": 0, "llm_busy": False,
            "llm_section": None,
            "servers": {"whisper": "not_started", "llama": "not_started"},
            "errors": 0, "last_error": None,
        }
        self._data.update(initial)
        self._data = normalize_status(self._data)
        self._data["schema_version"] = STATUS_SCHEMA_VERSION
        servers = initial.get("servers")
        if isinstance(servers, dict):
            self._data["servers"] = {
                "whisper": servers.get("whisper", "not_started"),
                "llama": servers.get("llama", "not_started"),
            }
        self._event_seq = _event_sequence(self.dir / "events.jsonl")
        self._dirty = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def update(self, **kv):
        with self._lock:
            self._data.update(kv)
            self._data["work_type"] = normalize_work_type(self._data.get("work_type"))
            self._data["schema_version"] = STATUS_SCHEMA_VERSION
            self._dirty = True

    def set_server(self, name, state):
        with self._lock:
            self._data["servers"][name] = state
            self._dirty = True

    def phase(self, phase, **extra):
        self.update(phase=phase, **extra)
        self.event("phase", phase=phase)
        self.flush()

    def error(self, msg):
        with self._lock:
            self._data["errors"] += 1
            self._data["last_error"] = msg
            self._dirty = True
        self.event("error", message=msg)

    def event(self, kind, **kv):
        with self._lock:
            self._event_seq += 1
            rec = {**kv, "schema_version": EVENT_SCHEMA_VERSION, "seq": self._event_seq,
                   "time": now_iso(), "type": kind}
            with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def get(self, key):
        with self._lock:
            return self._data.get(key)

    def flush(self):
        with self._lock:
            data = dict(deepcopy(self._data), updated_at=now_iso())
            self._dirty = False
            # Serialize publication too: heartbeat and phase updates share a temp file.
            try:
                write_json(self.dir / "status.json", data)
            except OSError:
                pass

    def _loop(self):
        while not self._stop.wait(self.interval):
            # updated_at is also a heartbeat for the UI, so flush even without changes.
            self.flush()

    def close(self):
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=max(float(self.interval), 0.1) + 0.5)
        self.flush()


class RunLock:
    """One lifetime lock for all work types/modes; run.json remains the public record.

    run.lock is an empty, persistent OS-lock sidecar. Never delete/replace it:
    unlinking a locked inode could admit a second owner. Process exit closes the
    handle and releases the lock; stale JSON alone cannot retain ownership.
    """

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "run.json"
        self.guard_path = Path(state_dir) / "run.lock"
        self.held = False
        self._stream = None
        self._owner_pid = None
        self._mutex = threading.RLock()

    def _live_record(self):
        info = read_json(self.path)
        if not isinstance(info, dict):
            return None
        pid = info.get("pid")
        if type(pid) is not int or pid <= 0 or not pid_alive(pid):
            return None
        result = {"schema_version": 0, "pid": pid, "started_at": None,
                  "course": None, "session": None, "mode": "run", **info}
        result["mode"] = result.get("mode") or "run"
        result["work_type"] = normalize_work_type(result.get("work_type"))
        return result

    @staticmethod
    def _pending():
        # The owner may not have published metadata yet. Busy must remain visible.
        return {"schema_version": RUN_SCHEMA_VERSION, "pid": None, "started_at": None,
                "course": None, "session": None, "mode": "run", "work_type": "unknown"}

    def current(self):
        with self._mutex:
            record = self._live_record()
            if record:
                return record
            if self.held and self._owner_pid == os.getpid():
                return self._pending()
            try:
                stream = self.guard_path.open("r+b")
            except FileNotFoundError:
                return None
            with stream:
                if not _try_lock(stream):
                    # Re-read after contention in case metadata was just published.
                    return self._live_record() or self._pending()
                try:
                    return self._live_record()
                finally:
                    _unlock(stream)

    def acquire(self, **info):
        with self._mutex:
            if self.held:
                return self.current() or self._pending()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.guard_path.open("a+b")
            try:
                if not _try_lock(stream):
                    stream.close()
                    return self._live_record() or self._pending()
            except BaseException:
                stream.close()
                raise
            try:
                # Respect a still-live legacy writer that does not hold run.lock.
                current = self._live_record()
                if current:
                    _unlock(stream)
                    stream.close()
                    return current
                record = {"started_at": now_iso(), "course": None, "session": None,
                          "mode": "run", **info, "schema_version": RUN_SCHEMA_VERSION,
                          "pid": os.getpid(),
                          "work_type": normalize_work_type(info.get("work_type"))}
                write_json(self.path, record)
            except BaseException:
                try:
                    _unlock(stream)
                finally:
                    stream.close()
                raise
            self._stream = stream
            self._owner_pid = os.getpid()
            self.held = True
            return None

    def update(self, **info):
        with self._mutex:
            if self.held and self._owner_pid == os.getpid():
                data = read_json(self.path, {})
                if not isinstance(data, dict) or data.get("pid") != self._owner_pid:
                    raise RuntimeError("run.json ownership changed while lock was held")
                data.update(info)
                data["schema_version"] = RUN_SCHEMA_VERSION
                data["pid"] = self._owner_pid
                data["work_type"] = normalize_work_type(data.get("work_type"))
                write_json(self.path, data)

    def release(self):
        with self._mutex:
            if not self.held:
                return
            try:
                if self._owner_pid == os.getpid():
                    info = read_json(self.path)
                    if isinstance(info, dict) and info.get("pid") == self._owner_pid:
                        self.path.unlink(missing_ok=True)
            finally:
                try:
                    if self._owner_pid == os.getpid():
                        _unlock(self._stream)
                finally:
                    self._stream.close()
                    self._stream = None
                    self._owner_pid = None
                    self.held = False
