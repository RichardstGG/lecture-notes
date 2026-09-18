"""共用小工具：時間格式、日誌、OpenCC、原子寫檔、程序檢查。"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path


def hms(t):
    t = int(max(t, 0))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def parse_hms(s):
    h, m, sec = s.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


class Logger:
    """同時輸出到終端機與 session.log。"""

    def __init__(self):
        self._fh = None
        self._lock = threading.RLock()

    def attach(self, path):
        self._fh = open(path, "a", encoding="utf-8")

    def __call__(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        with self._lock:
            print(line, flush=True)
            if self._fh:
                self._fh.write(line + "\n")
                self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


log = Logger()


def opencc_available():
    return shutil.which("opencc") is not None


def opencc_convert(texts):
    """批次轉台灣繁體；失敗或行數不符時原樣回傳。texts 內不可含換行。"""
    if not texts or not opencc_available():
        return texts
    try:
        out = subprocess.run(["opencc", "-c", "s2twp.json"], input="\n".join(texts),
                             capture_output=True, text=True, timeout=20, check=True).stdout
        lines = out.rstrip("\n").split("\n")
        if len(lines) != len(texts):
            return texts
        return [l.replace("臺", "台") for l in lines]   # 台灣日常寫法用「台」
    except Exception:
        return texts


def atomic_write(path, text):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def pid_alive(pid):
    from .platform import pid_alive as _alive      # 平台差異在 core/platform.py
    return _alive(pid)


def die(msg, code=1):
    print(f"✖ {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


_SAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def safe_name(name):
    return _SAFE.sub("_", name).strip() or "course"
