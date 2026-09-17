"""給 UI 讀的狀態檔：
- <輸出資料夾>/status.json：目前狀態快照（每 system.status_interval 秒覆寫）
- <輸出資料夾>/events.jsonl：只追加的事件紀錄
- <state_dir>/run.json：目前是否有 lec run 在執行（同一時間只允許一個）
"""
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from .util import pid_alive, read_json, write_json


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Status:
    def __init__(self, session_dir, interval=2, **initial):
        self.dir = Path(session_dir)
        self.interval = interval
        self._lock = threading.RLock()
        self._data = {
            "phase": "starting", "pid": os.getpid(), "started_at": now_iso(),
            "elapsed": 0, "transcribe_lag": None, "queue": 0,
            "sections_total": 0, "sections_summarized": 0, "llm_busy": False,
            "servers": {}, "errors": 0, "last_error": None,
        }
        self._data.update(initial)
        self._dirty = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def update(self, **kv):
        with self._lock:
            self._data.update(kv)
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
        rec = {"time": now_iso(), "type": kind, **kv}
        with self._lock:
            with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def get(self, key):
        with self._lock:
            return self._data.get(key)

    def flush(self):
        with self._lock:
            data = dict(self._data, updated_at=now_iso())
            self._dirty = False
        try:
            write_json(self.dir / "status.json", data)
        except OSError:
            pass

    def _loop(self):
        while not self._stop.wait(self.interval):
            if self._dirty:
                self.flush()

    def close(self):
        self._stop.set()
        self.flush()


class RunLock:
    """<state_dir>/run.json：確保同一時間只有一個 lec run。"""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "run.json"
        self.held = False

    def current(self):
        info = read_json(self.path)
        if info and pid_alive(info.get("pid")):
            return info
        return None

    def acquire(self, **info):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cur = self.current()
        if cur and cur.get("pid") != os.getpid():
            return cur
        write_json(self.path, {"pid": os.getpid(), "started_at": now_iso(), **info})
        self.held = True
        return None

    def update(self, **info):
        if self.held:
            data = read_json(self.path, {})
            data.update(info)
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
