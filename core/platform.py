"""平台差異集中在這裡：Linux（PulseAudio）、macOS（avfoundation）、Windows（dshow）。

其他模組一律透過本檔取得錄音參數、裝置清單、防休眠、狀態資料夾、行程操作與編譯工具鏈偵測，
不要自己判斷作業系統。
"""
import ctypes
import os
import platform as _pf
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

if sys.platform.startswith("win"):
    NAME, EXE = "windows", ".exe"
elif sys.platform == "darwin":
    NAME, EXE = "macos", ""
else:
    NAME, EXE = "linux", ""

IS_WINDOWS = NAME == "windows"
AUDIO_BACKENDS = {"linux": "pulse", "macos": "avfoundation", "windows": "dshow"}
DEFAULT_BACKEND = {"linux": "vulkan", "macos": "metal", "windows": "vulkan"}


def describe():
    return f"{_pf.system()} {_pf.release()}（{NAME}）"


def force_utf8():
    """Windows 終端機預設不是 UTF-8，中文與 ✔ 會變亂碼。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def default_state_dir():
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "lecture-notes"
    if NAME == "macos":
        return Path.home() / "Library" / "Application Support" / "lecture-notes"
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "lecture-notes"


def audio_backend(configured="auto"):
    return AUDIO_BACKENDS[NAME] if configured in ("", "auto", None) else configured


def engine_backend(configured="auto"):
    return DEFAULT_BACKEND[NAME] if configured in ("", "auto", None) else configured


# ---------------------------------------------------------------- 行程
def spawn_kwargs(detach=True):
    """讓 server 與 ffmpeg 不會被終端機的 Ctrl+C 直接打斷。"""
    if not detach:
        return {}
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def pid_alive(pid):
    if not pid:
        return False
    pid = int(pid)
    if IS_WINDOWS:
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                   # 殭屍程序算已結束
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def interrupt(pid):
    """請對方優雅結束（等同 Ctrl+C）。Windows 無法對別的行程送 SIGINT，
    所以 lec 的停止一律走 stop 檔，這裡只用於 POSIX 上的即時停止。"""
    if IS_WINDOWS:
        return False
    try:
        os.kill(int(pid), signal.SIGINT)
        return True
    except OSError:
        return False


def kill_tree(pid, timeout=15):
    """關閉 server（連同它的子程序）。"""
    pid = int(pid)
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            os.kill(pid, sig)
        end = time.time() + (timeout if sig == signal.SIGTERM else 5)
        while time.time() < end:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            if not pid_alive(pid):
                return
            time.sleep(0.3)


def kill_now(pid):
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return
    try:
        os.killpg(int(pid), signal.SIGKILL)
    except OSError:
        pass


def find_engine_bin(engine_dir, name):
    """找 whisper-server / llama-server；Windows 的 Visual Studio 會多一層 Release/。"""
    base = Path(engine_dir) / "build"
    for rel in ("bin", "bin/Release", "Release", "bin/Debug"):
        p = base / rel / (name + EXE)
        if p.is_file():
            return p
    return base / "bin" / (name + EXE)          # 不存在時回傳預期路徑，方便顯示錯誤


def library_dirs(binary):
    """build 內含共用函式庫的資料夾（動態連結時要加進搜尋路徑）。"""
    build = Path(binary).resolve().parent.parent
    pattern = "*.dll" if IS_WINDOWS else "lib*.so*" if NAME == "linux" else "lib*.dylib"
    try:
        return sorted({str(p.parent) for p in build.rglob(pattern)})
    except OSError:
        return []


def env_with_libs(binary, env=None):
    env = dict(env or os.environ)
    dirs = library_dirs(binary)
    if not dirs:
        return env
    key = "PATH" if IS_WINDOWS else "DYLD_LIBRARY_PATH" if NAME == "macos" else "LD_LIBRARY_PATH"
    old = env.get(key)
    env[key] = os.pathsep.join(dirs + ([old] if old else []))
    return env


# --------------------------------------------- 編譯工具鏈（setup_engines.py 與 lec doctor 共用）
VC_TOOLS = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"
BUILD_STAMP = ".lec-build"      # setup_engines.py 寫在 <引擎資料夾>/build/ 下的編譯紀錄


def build_stamp_path(engine_dir):
    return Path(engine_dir) / "build" / BUILD_STAMP


def built_backend(engine_dir):
    """從 build stamp 讀出這個引擎當初「實際用哪個後端編的」；讀不到就回傳 None。

    lec doctor 用這個判斷該檢查哪些編譯工具：用 --backend cuda 編過的機器不該被提醒
    缺 Vulkan 的 glslc。
    """
    try:
        text = build_stamp_path(engine_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    for token in text.split():
        if token.startswith("BACKEND="):
            return token.split("=", 1)[1] or None
    return None


def _stdout(cmd, timeout=60):
    """只取 stdout（vswhere 的 JSON 不能混進 stderr）；失敗回傳空字串。"""
    try:
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, **kw)
        return (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def find_msvc():
    """用 vswhere 找「有裝 C++ 工具」的 Visual Studio；回傳 {version, path} 或 None。

    只看 Program Files 下有沒有 Microsoft Visual Studio 資料夾不夠：只裝 VS Installer、
    沒勾「使用 C++ 的桌面開發」時資料夾也存在，cmake 會退回 NMake 然後失敗。
    """
    import json
    base = os.environ.get("ProgramFiles(x86)") or "C:/Program Files (x86)"
    vswhere = Path(base) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.exists():
        return None
    raw = _stdout([vswhere, "-latest", "-products", "*", "-requires", VC_TOOLS,
                   "-format", "json", "-utf8"])
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return None
    if not items:
        return None
    return {"version": items[0].get("installationVersion", ""),
            "path": items[0].get("installationPath", "")}


def cxx_compiler():
    """POSIX 上第一個找得到的 C++ 編譯器路徑；找不到回傳 None。"""
    for name in ("c++", "clang++", "g++"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _has_vulkan_lib():
    """libvulkan 的開發檔案在不在（VULKAN_SDK 或 pkg-config）。"""
    if os.environ.get("VULKAN_SDK"):
        return True
    if not shutil.which("pkg-config"):
        return False
    try:
        return subprocess.run(["pkg-config", "--exists", "vulkan"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def missing_build_tools(backend=None, msvc="probe"):
    """編譯引擎需要、但這台電腦缺少的東西（中文名稱 list；空 list = 齊全）。

    只看編譯器與後端 SDK，git / cmake 由呼叫端各自回報（setup_engines.py 直接中止，
    lec doctor 列成獨立項目）。兩邊共用這裡，判斷才不會不一致。
    msvc："probe"（預設）= Windows 上自己呼叫 find_msvc()；已經查過的話傳進來可省一次 vswhere。
    """
    backend = engine_backend(backend)
    missing = []
    if NAME == "windows":
        if msvc == "probe":
            msvc = find_msvc()
        if not (shutil.which("cl") or msvc):
            missing.append("Visual Studio 的「使用 C++ 的桌面開發」工作負載（MSVC 編譯器）")
        if backend == "vulkan" and not (os.environ.get("VULKAN_SDK") or shutil.which("glslc")):
            missing.append("Vulkan SDK")
    else:
        if not cxx_compiler():
            missing.append("C++ 編譯器")
        if backend == "vulkan":
            if not shutil.which("glslc"):
                missing.append("glslc")
            if not _has_vulkan_lib():
                missing.append("libvulkan-dev")
    if backend == "cuda" and not shutil.which("nvcc"):
        missing.append("CUDA Toolkit（nvcc）")
    return missing


# ---------------------------------------------------------------- 防休眠
class Inhibitor:
    """執行期間阻止系統休眠；stop() 釋放。"""

    def __init__(self):
        self.proc = None
        self.windows_held = False

    def start(self, why, log=print):
        if IS_WINDOWS:
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED：持續阻止睡眠（螢幕仍可關）
            try:
                ok = ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
                self.windows_held = bool(ok)
                log("▶ 已阻止休眠" if ok else "⚠ 無法阻止休眠（SetThreadExecutionState 失敗）")
            except OSError as e:
                log(f"⚠ 無法阻止休眠：{e}")
            return
        if NAME == "macos":
            cmd = ["caffeinate", "-i", "-m", "-s", "-w", str(os.getpid())]
        else:
            cmd = ["systemd-inhibit", "--what=sleep:idle:handle-lid-switch", "--who=lec",
                   f"--why={why}", "--mode=block", "tail", f"--pid={os.getpid()}", "-f", "/dev/null"]
        if not shutil.which(cmd[0]):
            log(f"⚠ 找不到 {cmd[0]}，無法阻止休眠")
            return
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, **spawn_kwargs())
        time.sleep(0.5)
        if self.proc.poll() is not None:
            err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
            log(f"⚠ 無法阻止休眠：{err[:200]}")
            self.proc = None
        else:
            log("▶ 已阻止休眠 / 蓋螢幕休眠" if NAME == "linux" else "▶ 已阻止休眠")

    def stop(self):
        if self.windows_held:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)   # 只留 ES_CONTINUOUS = 解除
            except OSError:
                pass
            self.windows_held = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


# ---------------------------------------------------------------- 錄音來源
def _run(cmd, timeout=15):
    try:
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, **kw)
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _pulse_sources():
    import json
    if not shutil.which("pactl"):
        return None
    out = _run(["pactl", "-f", "json", "list", "sources"])
    try:
        data = json.loads(out[out.index("["):out.rindex("]") + 1])
        return [{"id": s.get("name", ""), "name": s.get("name", ""),
                 "description": s.get("description", ""),
                 "state": str(s.get("state", "")).lower()} for s in data]
    except (ValueError, KeyError):
        pass
    out = _run(["pactl", "list", "short", "sources"])
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append({"id": parts[1], "name": parts[1], "description": "",
                         "state": parts[-1].lower() if len(parts) >= 5 else ""})
    return rows


_AVF_HEADER = re.compile(r"AVFoundation audio devices", re.I)
_AVF_ITEM = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")


def _avfoundation_sources():
    out = _run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""])
    rows, in_audio = [], False
    for line in out.splitlines():
        if _AVF_HEADER.search(line):
            in_audio = True
            continue
        if in_audio and "video devices" in line.lower():
            in_audio = False
        if not in_audio:
            continue
        m = _AVF_ITEM.search(line)
        if m:
            rows.append({"id": m.group(2).strip(), "name": m.group(2).strip(),
                         "description": f"avfoundation index {m.group(1)}", "state": "",
                         "index": int(m.group(1))})
    return rows


_DSHOW_SECTION = re.compile(r"DirectShow\s+(audio|video)\s+devices", re.I)
_DSHOW_TYPED = re.compile(r'"([^"]+)"\s*\(([^()]*)\)\s*$')
_DSHOW_NAME = re.compile(r'"([^"]+)"')
_DSHOW_ALT = re.compile(r'Alternative name\s+"([^"]+)"')


def parse_dshow_devices(out):
    """解析 ffmpeg -f dshow -list_devices true 的輸出，只回傳音訊裝置。

    兩種格式都要支援：
    - 新版 ffmpeg（實測 9.0.1）：類型寫在名稱後面，沒有區段標題
        [in#0 @ …] "麥克風排列 (Realtek(R) Audio)" (audio)
        [in#0 @ …]   Alternative name "@device_cm_{…}\\wave_{…}"
      類型可能是 audio、video、none，或 "audio, video"。
    - 舊版 ffmpeg：先印「DirectShow video devices」/「DirectShow audio devices」
      區段標題，裝置只印 "名稱"，用所在區段判斷類型。
    """
    rows, pending, section = [], None, None
    for line in out.splitlines():
        sec = _DSHOW_SECTION.search(line)
        if sec:
            section = sec.group(1).lower()
            pending = None
            continue
        alt = _DSHOW_ALT.search(line)
        if alt:
            if pending is not None:
                # 裝置路徑比顯示名稱穩定（不受中文、逗號、重複名稱影響）
                pending["id"] = alt.group(1)
                pending["description"] = pending["name"]
            pending = None
            continue
        typed = _DSHOW_TYPED.search(line)
        if typed:
            kinds = {k.strip().lower() for k in typed.group(2).split(",")}
            name = typed.group(1)
        else:
            m = _DSHOW_NAME.search(line)
            if not m:
                continue
            kinds = {section} if section else set()
            name = m.group(1)
        if "audio" in kinds:
            pending = {"id": name, "name": name, "description": "", "state": "", "kind": "audio"}
            rows.append(pending)
        else:
            pending = None
    return rows


def _dshow_sources():
    out = _run(["ffmpeg", "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"])
    return parse_dshow_devices(out)


def list_sources(backend=None, include_monitors=False):
    """回傳 [{id, name, description, state}]；無法列出時回傳 None。
    id 是設定檔要存的值（pulse 來源名稱 / mac 裝置名稱 / Windows 裝置路徑）。"""
    backend = audio_backend(backend)
    if backend == "pulse":
        rows = _pulse_sources()
        if rows is None:
            return None
        if not include_monitors:
            rows = [r for r in rows if not r["name"].endswith(".monitor")]
        return rows
    if not shutil.which("ffmpeg"):
        return None
    rows = _avfoundation_sources() if backend == "avfoundation" else _dshow_sources()
    return rows


def default_source(backend=None):
    backend = audio_backend(backend)
    if backend == "pulse":
        if not shutil.which("pactl"):
            return None
        try:
            result = subprocess.run(
                ["pactl", "get-default-source"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    rows = list_sources(backend) or []
    return rows[0]["id"] if rows else None


def resolve_source(choice, backend=None):
    """設定值（default / 編號 / 名稱 / id）→ 實際要傳給 ffmpeg 的來源識別。

    找不到就照原樣交給 ffmpeg（見函式尾端）；要「找不到就報錯」的互動查詢請用
    `devices.resolve()`，兩者的差別在那邊的 docstring 有說明。
    """
    backend = audio_backend(backend)
    rows = list_sources(backend) or []
    if choice in ("", "default", None):
        return default_source(backend) or ("default" if backend == "pulse" else None)
    for i, r in enumerate(rows):
        if choice in (r["id"], r["name"]) or str(choice) == str(r.get("index", i)):
            return r["id"]
    return choice                                   # 找不到就照原樣交給 ffmpeg


def ffmpeg_input(source, backend=None):
    """回傳 ffmpeg 的輸入參數。"""
    backend = audio_backend(backend)
    if backend == "pulse":
        return ["-f", "pulse", "-i", source or "default"]
    if backend == "avfoundation":
        idx = None
        for r in _avfoundation_sources():
            if source in (r["id"], r["name"]):
                idx = r.get("index")
                break
        if idx is None:
            idx = source if str(source).isdigit() else 0
        return ["-f", "avfoundation", "-thread_queue_size", "512", "-i", f":{idx}"]
    if backend == "dshow":
        return ["-f", "dshow", "-thread_queue_size", "512", "-i", f"audio={source}"]
    raise ValueError(f"未知的錄音後端：{backend}")
