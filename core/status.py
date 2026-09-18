"""給 UI 讀的狀態檔：
- <輸出資料夾>/status.json：目前狀態快照（每 system.status_interval 秒覆寫）
- <輸出資料夾>/events.jsonl：只追加的事件紀錄
- <state_dir>/run.json：目前是否有 lec run 在執行（同一時間只允許一個）
"""
import json
import os
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .util import pid_alive, read_json, write_json

STATUS_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
RUN_SCHEMA_VERSION = 1


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
    """<state_dir>/run.json：確保同一時間只有一個 lec run。"""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "run.json"
        self.held = False

    def current(self):
        info = read_json(self.path)
        if info and pid_alive(info.get("pid")):
            normalized = {
                "schema_version": info.get("schema_version", 0),
                "pid": info.get("pid"), "started_at": info.get("started_at"),
                "course": info.get("course"), "session": info.get("session"),
                "mode": info.get("mode") or "run",
            }
            normalized.update(info)
            normalized["mode"] = normalized.get("mode") or "run"
            return normalized
        return None

    def acquire(self, **info):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cur = self.current()
        if cur and cur.get("pid") != os.getpid():
            return cur
        record = {
            "schema_version": RUN_SCHEMA_VERSION,
            "pid": os.getpid(), "started_at": now_iso(),
            "course": None, "session": None, "mode": "run",
        }
        record.update(info)
        record["schema_version"] = RUN_SCHEMA_VERSION
        write_json(self.path, record)
        self.held = True
        return None

    def update(self, **info):
        if self.held:
            data = read_json(self.path, {})
            data.update(info)
            data["schema_version"] = RUN_SCHEMA_VERSION
            write_json(self.path, data)

    def release(self):
        if self.held:
            try:
                info = read_json(self.path)
                if info and info.get("pid") == os.getpid():
                    self.path.unlink()
            except OSError:
                pass
            self.held = False
