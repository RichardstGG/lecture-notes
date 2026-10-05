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
        """接上 session.log。重複呼叫時先關掉前一個 handle，不要洩漏。"""
        fh = open(path, "a", encoding="utf-8")
        with self._lock:                     # __call__ 會在別的執行緒讀 _fh
            old, self._fh = self._fh, fh
        if old:
            old.close()

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
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=True).stdout
        lines = out.rstrip("\n").split("\n")
        if len(lines) != len(texts):
            return texts
        return [l.replace("臺", "台") for l in lines]   # 台灣日常寫法用「台」
    except Exception:
        return texts


def atomic_write(path, text):
    """先寫暫存檔再 rename。失敗時不要把暫存檔留在輸出資料夾裡。

    os.replace() 本身會失敗：Windows 上若別的程式（例如 UI 正在輪詢
    status.json）開著目標檔，會是 sharing violation。原本的寫法會留下一個
    .status.json.tmp 在使用者的 Obsidian 資料夾裡。
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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

# Windows 的保留裝置名稱：不分大小寫、也不管後面接什麼副檔名，都不能當檔名或資料夾名。
# 課名叫 CON 就會讓 make_session_dir() 的 mkdir() 丟出 OSError。
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name):
    """課名 → 可以當資料夾名稱的字串。

    三個平台都套用同一套規則：輸出資料夾是 Obsidian vault 的一部分，
    在 Linux 錄的課可能被同步到 Windows 上開，所以不能只在 Windows 上收斂。
    一般的課名（中文、英數、空白）結果完全不變。
    """
    name = _SAFE.sub("_", name).strip()
    # Windows 會默默吃掉結尾的點與空白，造成「建出來的資料夾名字跟算出來的不一樣」，
    # 連帶讓 make_session_dir() 的同名偵測失效。
    name = name.rstrip(". ")
    if name.split(".", 1)[0].upper() in _WIN_RESERVED:
        name += "_"
    return name or "course"
