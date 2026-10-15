"""平台差異集中在這裡：Linux（PulseAudio）、macOS（avfoundation）、Windows（dshow）。

其他模組一律透過本檔取得錄音參數、裝置清單、防休眠、狀態資料夾、行程操作與編譯工具鏈偵測，
不要自己判斷作業系統。
"""
import ctypes
import hashlib
import io
import json
import math
import os
import platform as _pf
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
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
        # 行程名稱（comm）可以是任意位元組，不是合法 UTF-8 時不能讓 pid_alive 崩潰
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
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


# 編譯引擎會用到的工具；key 是程式用的識別，label 是印給人看的名稱。
# setup.py 用 key 對照這個平台的套件名稱，doctor / setup_engines.py 只印 label。
BUILD_TOOLS = {
    "msvc": "Visual Studio 的「使用 C++ 的桌面開發」工作負載（MSVC 編譯器）",
    "vulkan_sdk": "Vulkan SDK",
    "cxx": "C++ 編譯器",
    "glslc": "glslc",
    "libvulkan": "libvulkan-dev",
    "nvcc": "CUDA Toolkit（nvcc）",
}


def missing_build_tool_keys(backend=None, msvc="probe"):
    """編譯引擎需要、但這台電腦缺少的工具（BUILD_TOOLS 的 key；空 list = 齊全）。

    只看編譯器與後端 SDK，git / cmake 由呼叫端各自回報（setup_engines.py 直接中止，
    lec doctor 列成獨立項目）。所有呼叫端共用這裡，判斷才不會不一致。
    msvc："probe"（預設）= Windows 上自己呼叫 find_msvc()；已經查過的話傳進來可省一次 vswhere。
    """
    backend = engine_backend(backend)
    missing = []
    if NAME == "windows":
        if msvc == "probe":
            msvc = find_msvc()
        if not (shutil.which("cl") or msvc):
            missing.append("msvc")
        if backend == "vulkan" and not (os.environ.get("VULKAN_SDK") or shutil.which("glslc")):
            missing.append("vulkan_sdk")
    else:
        if not cxx_compiler():
            missing.append("cxx")
        if backend == "vulkan":
            if not shutil.which("glslc"):
                missing.append("glslc")
            if not _has_vulkan_lib():
                missing.append("libvulkan")
    if backend == "cuda" and not shutil.which("nvcc"):
        missing.append("nvcc")
    return missing


def missing_build_tools(backend=None, msvc="probe"):
    """同 missing_build_tool_keys()，但回傳印給人看的名稱。"""
    return [BUILD_TOOLS[k] for k in missing_build_tool_keys(backend, msvc)]


# ------------------------------------------------- 環境偵測（setup.py 的互動問題靠這些）
def nvidia_gpu():
    """有 NVIDIA 驅動就回傳第一張卡的名稱，否則 None。

    用 nvidia-smi 而不是 lspci：驅動裝好就一定有這支程式（Windows 在 System32），
    而 lspci 在精簡安裝的 Linux 上可能不存在。有卡不代表能編 CUDA——那要另外看 nvcc。
    """
    if not shutil.which("nvidia-smi"):
        return None
    out = _stdout(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=20)
    name = out.splitlines()[0].strip() if out else ""
    return name or "NVIDIA GPU（nvidia-smi 沒回報型號）"


def cuda_toolkit():
    """CUDA Toolkit 的版本字串（例如 "12.4"）；沒裝回傳 None。"""
    if not shutil.which("nvcc"):
        return None
    for line in _stdout(["nvcc", "--version"], timeout=20).splitlines():
        if "release" in line:
            part = line.split("release", 1)[1].strip()
            return part.split(",")[0].strip() or None
    return "（版本不明）"


def gpu_names():
    """盡量列出這台機器的 GPU 名稱（可能是空 list）。只用來顯示，不用來做決定。"""
    nvidia = nvidia_gpu()
    names = [nvidia] if nvidia else []
    if NAME == "macos":
        return names or ["Apple GPU（Metal）"]
    if shutil.which("vulkaninfo"):
        for line in _stdout(["vulkaninfo", "--summary"], timeout=30).splitlines():
            if "deviceName" in line and "=" in line:
                name = line.split("=", 1)[1].strip()
                if name and "llvmpipe" not in name and name not in names:
                    names.append(name)
    return names


def total_memory_gb():
    """實體記憶體（GB，十進位）；問不到回傳 None。"""
    if IS_WINDOWS:
        class _MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / 1e9
        except OSError:
            return None
        return None
    try:                                   # Linux 與 macOS 都支援這兩個 sysconf
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
    except (OSError, ValueError):
        return None


def free_disk_gb(path="."):
    """指定路徑所在磁碟的可用空間（GB，十進位）；問不到回傳 None。"""
    try:
        return shutil.disk_usage(str(path)).free / 1e9
    except OSError:
        return None


PACKAGE_MANAGERS = {"linux": ("apt", "dnf", "pacman", "zypper"), "macos": ("brew",),
                    "windows": ("winget",)}


def package_manager():
    """這台機器上找得到的套件管理器名稱；找不到回傳 None。

    只用來決定「要印哪一種安裝指令」。lec 自己不裝任何系統套件。
    """
    for name in PACKAGE_MANAGERS.get(NAME, ()):
        if shutil.which(name):
            return name
    return None


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


# ----------------------------------- 輸出裝置與串流綁定（雙來源錄音用；目前只有 pulse 後端）
def _pactl_json(*args):
    """執行 `pactl -f json <args>` 並解析；沒有 pactl、輸出不是 JSON 時回傳 None。"""
    import json
    if not shutil.which("pactl"):
        return None
    out = _run(["pactl", "-f", "json", *args])
    try:
        return json.loads(out[out.index("["):out.rindex("]") + 1])
    except ValueError:
        return None


def is_monitor_source(name):
    """pulse 的 monitor 來源（某個輸出裝置播出去的聲音），不是真的麥克風。"""
    return str(name).endswith(".monitor")


def monitor_of(sink):
    """輸出裝置 → 它的 monitor 來源名稱（固定指向這個裝置，不會跟著預設輸出變）。"""
    return f"{sink}.monitor"


def list_sinks(backend=None):
    """回傳 [{id, name, description, state, port}]；無法列出（非 pulse、沒有 pactl）時回傳 None。
    id 是設定要存的值（sink 名稱）；port 是目前使用的連接埠（耳機／喇叭／HDMI…），只供提示用。"""
    if audio_backend(backend) != "pulse":
        return None
    data = _pactl_json("list", "sinks")
    if data is None:
        return None
    return [{"id": s.get("name", ""), "name": s.get("name", ""),
             "description": s.get("description", ""),
             "state": str(s.get("state", "")).lower(),
             "port": str(s.get("active_port") or "")} for s in data]


def default_sink(backend=None):
    if audio_backend(backend) != "pulse" or not shutil.which("pactl"):
        return None
    out = _stdout(["pactl", "get-default-sink"], timeout=15)
    return out or None


def pulse_server():
    """音訊伺服器資訊 {name, version, flavor}，flavor 為 pipewire / pulseaudio；問不到回傳 None。

    PipeWire 透過 pipewire-pulse 相容層回報「PulseAudio (on PipeWire x.y)」，
    兩者共用同一份 pactl 介面，這裡只用來讓 doctor 與錯誤訊息講得清楚。
    """
    if not shutil.which("pactl"):
        return None
    info = {}
    for line in _stdout(["pactl", "info"], timeout=15).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            info[key.strip()] = value.strip()
    name = info.get("Server Name", "")
    if not name:
        return None
    return {"name": name, "version": info.get("Server Version", ""),
            "flavor": "pipewire" if "pipewire" in name.lower() else "pulseaudio"}


def pulse_record_bindings():
    """目前每條錄音串流實際綁定的來源：{行程 pid: 來源名稱}；問不到回傳 None。

    來源消失（拔除）時，錄音串流可能被系統「救援」轉接到別的來源（PulseAudio 的
    module-rescue-streams 設計上轉到預設來源）；ffmpeg 完全不會知道。開發機實測
    PipeWire 1.4.2：卸載來源後 ffmpeg 不報錯、也沒有結束，資料持續流入。
    雙來源錄音靠這個確認串流沒有被偷偷換掉；單一來源的舊流程不受影響，也不呼叫它。
    """
    outputs = _pactl_json("list", "source-outputs")
    sources = _pactl_json("list", "sources")
    if outputs is None or sources is None:
        return None
    names = {s.get("index"): s.get("name", "") for s in sources}
    bound = {}
    for o in outputs:
        pid = (o.get("properties") or {}).get("application.process.id")
        try:
            bound[int(pid)] = names.get(o.get("source"), "")
        except (TypeError, ValueError):
            continue
    return bound


def sink_activity(backend=None):
    """目前有幾條播放串流接在各輸出裝置上：{sink 名稱: 數量}；問不到回傳 None。
    用來在開錄前提示「會議聲音似乎不是從你選的輸出裝置播出」。"""
    if audio_backend(backend) != "pulse":
        return None
    inputs = _pactl_json("list", "sink-inputs")
    sinks = _pactl_json("list", "sinks")
    if inputs is None or sinks is None:
        return None
    names = {s.get("index"): s.get("name", "") for s in sinks}
    counts = {name: 0 for name in names.values()}
    for i in inputs:
        name = names.get(i.get("sink"))
        if name is not None and not i.get("corked"):
            counts[name] += 1
    return counts


def _macos_default_source(rows):
    """優先實體輸入；ffmpeg 清單沒有 transport 時排除已知 Background Music。"""
    for row in rows:
        transport = str(row.get("transport") or "").strip().casefold()
        if transport in {"virtual", "aggregate", "autoaggregate"}:
            continue
        if "background music" in row["name"].casefold():
            continue
        return row
    return None


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
    if backend == "avfoundation":
        row = _macos_default_source(rows)
        return row["id"] if row else None
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
        # 這是 demux 後的佇列，不能防止 avfoundation 單格暫存覆寫音訊。
        # 課堂單來源錄音透過 start_live_capture 使用原生 callback；其他呼叫者
        # 保留既有參數。實機證據與適用範圍見 docs/platform-macos.md。
        return ["-f", "avfoundation", "-thread_queue_size", "4096", "-i", f":{idx}"]
    if backend == "dshow":
        return ["-f", "dshow", "-thread_queue_size", "4096", "-i", f"audio={source}"]
    raise ValueError(f"未知的錄音後端：{backend}")


# 原生 callback 保存每個 PCM buffer；Matroska 保留 capture PTS，避免用 raw
# PCM 的合成時間戳掩蓋真正的擷取遺失。只由課堂單來源即時錄音使用。
_MACOS_CAPTURE_SWIFT = r'''
import AVFoundation
import Foundation
import CoreMedia
import Darwin

func bytes(_ value: UInt64, count: Int? = nil) -> Data {
    let n = count ?? max(1, (64 - value.leadingZeroBitCount + 7) / 8)
    return Data((0..<n).reversed().map { UInt8(truncatingIfNeeded: value >> ($0 * 8)) })
}
func size(_ value: Int) -> Data {
    for n in 1...8 where UInt64(value) < (UInt64(1) << (7*n)) - 1 {
        return bytes(UInt64(value) | (UInt64(1) << (7*n)), count: n)
    }
    fatalError("element too large")
}
func element(_ id: UInt64, _ payload: Data) -> Data { bytes(id) + size(payload.count) + payload }
func uint(_ id: UInt64, _ value: UInt64) -> Data { element(id, bytes(value)) }
func text(_ id: UInt64, _ value: String) -> Data { element(id, Data(value.utf8)) }
func interleave(_ data: Data, frames: Int, channels: Int, width: Int) -> Data {
    var packed = Data(count: data.count)
    data.withUnsafeBytes { src in
        packed.withUnsafeMutableBytes { dst in
            for s in 0..<frames { for c in 0..<channels {
                dst.baseAddress!.advanced(by: (s*channels+c)*width).copyMemory(
                    from: src.baseAddress!.advanced(by: (c*frames+s)*width), byteCount: width)
            } }
        }
    }
    return packed
}
func header(rate: Double, channels: UInt64, bits: UInt64, codec: String) -> Data {
    let ebml = uint(0x4286, 1) + uint(0x42F7, 1) + uint(0x42F2, 4) + uint(0x42F3, 8)
        + text(0x4282, "matroska") + uint(0x4287, 4) + uint(0x4285, 2)
    let info = uint(0x2AD7B1, 1000) + text(0x4D80, "lec-native") + text(0x5741, "lec-native")
    let audio = element(0xB5, bytes(rate.bitPattern, count: 8)) + uint(0x9F, channels) + uint(0x6264, bits)
    let track = uint(0xD7, 1) + uint(0x73C5, 1) + uint(0x83, 2) + text(0x86, codec) + element(0xE1, audio)
    return element(0x1A45DFA3, ebml) + bytes(0x18538067) + Data([1,255,255,255,255,255,255,255])
        + element(0x1549A966, info) + element(0x1654AE6B, element(0xAE, track))
}

final class Receiver: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    let writer = DispatchQueue(label: "lec-native-writer")
    let lock = NSLock()
    var pending = 0
    var problem: String? = nil
    var firstPTS: Double? = nil
    var lastEnd: Double = 0
    var audioSeconds = 0.0
    var gaps = 0
    var lostSeconds = 0.0
    var callbacks = 0
    var received = false
    var formatKey: String? = nil
    func failure(_ text: String) { lock.lock(); if problem == nil { problem = text }; lock.unlock() }
    func error() -> String? { lock.lock(); defer { lock.unlock() }; return problem }
    func hasAudio() -> Bool { lock.lock(); defer { lock.unlock() }; return received }
    func finishWriting() {
        // 錯誤時不等待已塞住的 writer；程序退出會關閉 pipe，讓上游失敗可被觀察。
        if error() == nil { writer.sync {} }
    }
    func enqueue(_ data: Data) {
        lock.lock()
        if pending + data.count > 32*1024*1024 { problem = "PCM writer backlog exceeds 32 MiB"; lock.unlock(); return }
        pending += data.count
        lock.unlock()
        writer.async {
            do { try FileHandle.standardOutput.write(contentsOf: data) }
            catch { self.failure("PCM pipe write failed") }
            self.lock.lock(); self.pending -= data.count; self.lock.unlock()
        }
    }
    func captureOutput(_ output: AVCaptureOutput, didOutput sample: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard error() == nil else { return }
        guard let desc = CMSampleBufferGetFormatDescription(sample),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(desc),
              let block = CMSampleBufferGetDataBuffer(sample) else { failure("missing PCM format or data"); return }
        let f = asbd.pointee
        guard f.mFormatID == kAudioFormatLinearPCM,
              f.mFormatFlags & kAudioFormatFlagIsBigEndian == 0 else { failure("unsupported PCM layout"); return }
        let float = f.mFormatFlags & kAudioFormatFlagIsFloat != 0
        guard (float && f.mBitsPerChannel == 32) || (!float && f.mFormatFlags & kAudioFormatFlagIsSignedInteger != 0 && [16,24,32].contains(f.mBitsPerChannel)) else {
            failure("unsupported PCM sample representation"); return
        }
        let pts = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sample))
        let count = CMSampleBufferGetNumSamples(sample)
        guard pts.isFinite, f.mSampleRate > 0, count > 0 else { failure("invalid PCM timestamp"); return }
        let duration = Double(count) / f.mSampleRate
        let key = "\(f.mSampleRate)/\(f.mChannelsPerFrame)/\(f.mBitsPerChannel)/\(float)"
        if let old = formatKey, old != key { failure("PCM format changed during recording"); return }
        formatKey = key
        if firstPTS == nil {
            firstPTS = pts
            enqueue(header(rate: f.mSampleRate, channels: UInt64(f.mChannelsPerFrame),
                           bits: UInt64(f.mBitsPerChannel), codec: float ? "A_PCM/FLOAT/IEEE" : "A_PCM/INT/LIT"))
        } else if pts < lastEnd - 0.001 { failure("PCM timestamp moved backwards"); return }
        else if pts - lastEnd > 0.001 { gaps += 1; lostSeconds += pts - lastEnd }
        let tick = (pts - firstPTS!) * 1000000
        guard tick >= 0 else { failure("PCM timestamp moved backwards"); return }
        let length = CMBlockBufferGetDataLength(block)
        var data = Data(count: length)
        let status = data.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!) }
        guard status == noErr else { failure("PCM copy failed"); return }
        let width = Int(f.mBitsPerChannel / 8)
        let channels = Int(f.mChannelsPerFrame)
        guard length == count * channels * width else { failure("unexpected PCM byte count"); return }
        if channels > 1 && f.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0 {
            data = interleave(data, frames: count, channels: channels, width: width)
        }
        let blockPayload = Data([0x81,0,0,0x80]) + data
        enqueue(element(0x1F43B675, uint(0xE7, UInt64(tick.rounded())) + element(0xA3, blockPayload)))
        lastEnd = pts + duration; audioSeconds += duration; callbacks += 1
        lock.lock(); received = true; lock.unlock()
    }
}

let target = CommandLine.arguments[1]
let devices: [AVCaptureDevice]
if #available(macOS 14.0, *) {
    devices = AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone], mediaType: .audio, position: .unspecified).devices
} else { devices = AVCaptureDevice.devices(for: .audio) }
guard let device = devices.first(where: { $0.localizedName == target }) else { fputs("native capture: input device unavailable\n", stderr); exit(1) }
if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
    let permission = DispatchSemaphore(value: 0)
    AVCaptureDevice.requestAccess(for: .audio) { _ in permission.signal() }
    _ = permission.wait(timeout: .now() + 30)
}
guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
    fputs("native capture: microphone permission unavailable; allow the calling app in System Settings > Privacy & Security > Microphone\n", stderr)
    exit(1)
}
let session = AVCaptureSession()
let input = try AVCaptureDeviceInput(device: device)
guard session.canAddInput(input) else { fatalError("input unavailable") }
session.addInput(input)
let output = AVCaptureAudioDataOutput()
let receiver = Receiver()
let queue = DispatchQueue(label: "lec-native-callback")
output.setSampleBufferDelegate(receiver, queue: queue)
guard session.canAddOutput(output) else { fatalError("output unavailable") }
session.addOutput(output)
signal(SIGPIPE, SIG_IGN)
signal(SIGINT, SIG_IGN); signal(SIGTERM, SIG_IGN)
var stopping = false
let signals = [SIGINT, SIGTERM].map { sig -> DispatchSourceSignal in
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { stopping = true }; source.resume(); return source
}
let parent = getppid()
let parentWatch = DispatchSource.makeTimerSource(queue: .global())
parentWatch.schedule(deadline: .now(), repeating: 0.25)
parentWatch.setEventHandler { if getppid() != parent { exit(1) } }
parentWatch.resume()
session.startRunning()
let start = ProcessInfo.processInfo.systemUptime
let limit = Double.infinity
while !stopping && receiver.error() == nil && ProcessInfo.processInfo.systemUptime - start < limit {
    if ProcessInfo.processInfo.systemUptime - start > 30 && !receiver.hasAudio() {
        receiver.failure("no PCM received within 30 seconds")
    }
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.01))
}
session.stopRunning()
queue.sync {}
receiver.finishWriting()
let report: [String: Any] = ["audio_seconds": receiver.audioSeconds,
    "pts_span_seconds": receiver.lastEnd - (receiver.firstPTS ?? receiver.lastEnd),
    "callbacks": receiver.callbacks, "gap_count": receiver.gaps, "lost_seconds": receiver.lostSeconds,
    "wall_seconds": ProcessInfo.processInfo.systemUptime - start, "error": receiver.error() ?? ""]
let json = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
FileHandle.standardError.write(Data("native capture: ".utf8) + json + Data("\n".utf8))
withExtendedLifetime(signals) {}
withExtendedLifetime(parentWatch) {}
exit(receiver.error() == nil && receiver.callbacks > 0 ? 0 : 1)

'''


class MacOSCaptureProcess:
    """單來源擷取的兩個子行程。SIGINT 先停止來源，PCM/Opus 由 EOF 正常收尾。

    ffmpeg 的 returncode 維持原值；helper 的失敗另由 capture_problem 回報。
    編譯或啟動失敗時 stdout 為 EOF、wait() 為 None（尚未啟動 ffmpeg）。
    """

    def __init__(self, proc=None, helper=None, cache=None, diagnostics=None, problem=None):
        self.proc, self.helper = proc, helper
        self.cache, self.diagnostics = cache, diagnostics
        self.capture_problem = problem
        self.capture_report = None
        self.stdout = proc.stdout if proc else io.BytesIO()
        self.pid = proc.pid if proc else None
        self.returncode = None
        self._closed = False
        self._stop_requested = False

    def poll(self):
        return self.proc.poll() if self.proc else 1

    def send_signal(self, sig):
        self._stop_requested = True
        if self.helper and self.helper.poll() is None:
            self.helper.send_signal(sig)

    def terminate(self):
        for child in (self.helper, self.proc):
            if child and child.poll() is None:
                child.terminate()

    def kill(self):
        for child in (self.helper, self.proc):
            if child and child.poll() is None:
                child.kill()

    def wait(self, timeout=None):
        if self._closed:
            return self.returncode
        self.returncode = self.proc.wait(timeout=timeout) if self.proc else None
        if self.helper:
            if self.helper.poll() is None:
                self.helper.terminate()
            try:
                code = self.helper.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.helper.kill()
                code = self.helper.wait()
            detail = ""
            if self.diagnostics:
                try:
                    self.diagnostics.seek(0, io.SEEK_END)
                    self.diagnostics.seek(max(0, self.diagnostics.tell() - 65536))
                    detail = self.diagnostics.read().decode("utf-8", "replace").strip()
                    lines = [line.removeprefix("native capture: ") for line in detail.splitlines()
                             if line.startswith("native capture: ")]
                    report = json.loads(lines[-1]) if lines else None
                    fields = ("audio_seconds", "pts_span_seconds", "callbacks", "gap_count",
                              "lost_seconds", "wall_seconds")
                    if (isinstance(report, dict) and isinstance(report.get("error"), str)
                            and all(type(report.get(k)) in (int, float)
                                    and math.isfinite(report[k]) and report[k] >= 0 for k in fields)):
                        self.capture_report = report
                except (OSError, ValueError, TypeError, OverflowError):
                    pass  # kill／截斷／缺少報告不能掩蓋真實的子行程退出碼
            if code != 0 and not (self._stop_requested and code in (-signal.SIGINT, -signal.SIGTERM)):
                self.capture_problem = f"helper 結束代碼 {code}：{detail[:2048] or '沒有診斷資訊'}"
        if self.diagnostics:
            self.diagnostics.close()
        if self.cache:
            self.cache.cleanup()
        self._closed = True
        return self.returncode


def native_capture_problem(proc):
    """一般 Popen 沒有 native helper；不要改 Linux/dshow 的失敗判斷。"""
    return proc.capture_problem if isinstance(proc, MacOSCaptureProcess) else None


def native_capture_report(proc):
    """只有原生 wrapper 提供報告，普通 Popen／mock 不新增行為。"""
    return proc.capture_report if isinstance(proc, MacOSCaptureProcess) else None


def _compile_macos_capture(compiler, work):
    from .util import log
    log("▶ 重新編譯 macOS 原生擷取 helper")
    swift, binary = work / "capture.swift", work / "capture"
    swift.write_text(_MACOS_CAPTURE_SWIFT, encoding="utf-8")
    build = subprocess.run(
        [compiler, "-module-cache-path", str(work / "modules"), str(swift), "-o", str(binary)],
        capture_output=True, timeout=120,
    )
    if build.returncode:
        detail = build.stderr.decode("utf-8", "replace").strip()[-1000:]
        raise OSError(f"macOS 原生擷取 helper 編譯失敗：{detail}")
    return binary


class _MacOSHelperCache:
    """私有持久快取。lock 持有到 helper exec 成功，避免查驗／清理／啟動競爭。

    原始碼與 module cache 仍在每次錄音的 TemporaryDirectory；此物件沒有 cleanup，
    不交給 MacOSCaptureProcess。摘要 sidecar 與 binary 在同一把 lock 下原子發布。
    """

    def __init__(self, compiler):
        self.compiler = compiler
        self.root = default_state_dir() / "native-capture"
        self.lock = None
        self.path = None
        self.key = None

    def _log_fallback(self, reason):
        from .util import log
        log(f"⚠ 不使用 macOS helper 快取：{reason}；改用暫存目錄重新編譯")
        self.path = None

    @staticmethod
    def _safe_file(path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise PermissionError(f"{path.name} 不是目前使用者擁有的安全一般檔案（symlink／擁有者／權限）")
        return info

    def _check_parents(self):
        # 父目錄不能由其他帳號替換；只允許 root 擁有的 sticky 暫存目錄例外。
        for parent in self.root.parents:
            try:
                info = parent.lstat()
            except FileNotFoundError:
                continue
            sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or (info.st_mode & 0o022 and not sticky)):
                raise PermissionError("快取父目錄的擁有者／權限／symlink 不安全")

    def __enter__(self):
        try:
            if os.name != "posix":
                raise OSError("此快取需要 POSIX 檔案鎖")
            import fcntl
            version = subprocess.run([self.compiler, "--version"], capture_output=True, timeout=10)
            if version.returncode:
                raise OSError("無法取得 swiftc --version")
            self.key = hashlib.sha256(_MACOS_CAPTURE_SWIFT.encode("utf-8") + b"\0"
                                     + version.stdout + version.stderr + b"\0"
                                     + _pf.machine().encode("utf-8")).hexdigest()
            self._check_parents()
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_parents()  # umask 也可能令新建立的中間目錄不安全
            info = self.root.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise PermissionError("快取目錄必須由目前使用者擁有、權限 0700，且不是 symlink")
            self.lock = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(self.lock)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022):
                raise PermissionError("快取鎖檔的擁有者／權限／型別不安全")
            fcntl.flock(self.lock, fcntl.LOCK_EX)
            self.path = self.root / (self.key + ".bin")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._log_fallback(str(exc))
        return self

    def __exit__(self, *exc):
        if self.lock is not None:
            os.close(self.lock)  # 關閉 fd 一併釋放 flock；不刪 lock 檔

    @staticmethod
    def _digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _discard(self):
        for path in (self.path, self.path.with_suffix(".sha256")):
            try:
                self._safe_file(path)
                path.unlink()
            except FileNotFoundError:
                pass

    def _publish_file(self, path, data, mode):
        fd, name = tempfile.mkstemp(prefix=".publish-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _prune(self):
        entries = []
        for path in self.root.glob("*.bin"):
            if not re.fullmatch(r"[0-9a-f]{64}\.bin", path.name):
                continue
            try:
                info = self._safe_file(path)
                self._safe_file(path.with_suffix(".sha256"))
                entries.append((info.st_mtime_ns, path))
            except (OSError, ValueError):
                continue
        newest = [p for _, p in sorted(entries, reverse=True) if p != self.path]
        for path in newest[2:]:  # 本次使用的版本 + 另外兩筆最近使用版本
            path.unlink()
            path.with_suffix(".sha256").unlink(missing_ok=True)

    def binary(self, work, rebuild=False):
        from .util import log
        if self.path:
            try:
                if not rebuild:
                    try:
                        info = self._safe_file(self.path)
                        digest_path = self.path.with_suffix(".sha256")
                        self._safe_file(digest_path)
                        try:
                            digest = digest_path.read_text(encoding="ascii").strip()
                        except UnicodeError:
                            digest = ""  # 截斷／毀損的摘要視同 binary 不匹配
                        if (info.st_size and info.st_mode & 0o100 and os.access(self.path, os.X_OK)
                                and self._digest(self.path) == digest):
                            os.utime(self.path, None, follow_symlinks=False)
                            log("▶ 使用快取 macOS 原生擷取 helper")
                            return self.path
                        log("⚠ macOS helper 快取毀損或不可執行；刪除該筆並重新編譯一次")
                    except FileNotFoundError:
                        pass
                self._discard()
            except (OSError, UnicodeError) as exc:
                self._log_fallback(str(exc))
        binary = _compile_macos_capture(self.compiler, work)
        if self.path:
            try:
                # 發布的 binary 與 digest 各自原子改名；讀取端持有相同 lock。
                self._publish_file(self.path, binary.read_bytes(), 0o700)
                self._publish_file(self.path.with_suffix(".sha256"),
                                   (self._digest(binary) + "\n").encode("ascii"), 0o600)
                self._prune()
                return self.path
            except OSError as exc:
                self._log_fallback(str(exc))
        return binary


def start_live_capture(cmd, source, backend=None):
    """啟動課堂單來源錄音；macOS avfoundation 避開 ffmpeg 的單格暫存擷取。

    cmd 是既有 ffmpeg 輸入＋輸出 argv。僅替換輸入為帶原始 PTS 的 Matroska
    pipe；輸出、來源設定、停止檔協定、Linux pulse 與 Windows dshow 皆保留。
    每次的原始碼／module cache 暫存目錄正常收尾刪除；安全的持久 binary 快取不隨錄音刪除。
    """
    if NAME != "macos" or audio_backend(backend) != "avfoundation":
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, **spawn_kwargs())
    cache = diagnostics = helper = proc = None
    try:
        compiler = shutil.which("swiftc")
        if not compiler:
            raise OSError("macOS 原生擷取需要 swiftc；請安裝 Xcode Command Line Tools（xcode-select --install）")
        rows = _avfoundation_sources()
        if source in (None, "", "default"):
            row = _macos_default_source(rows)
        else:
            row = next((r for r in rows if str(source) in
                        (str(r["id"]), r["name"], str(r.get("index")))), None)
        if row is None:
            available = "、".join(r["name"] for r in rows) or "（無）"
            raise OSError(f"找不到指定的 macOS 錄音來源 {source!r}；可用裝置：{available}；"
                          "請重新執行 lec devices 或明確指定來源")
        cache = tempfile.TemporaryDirectory(prefix="lec-macos-capture-")
        root = Path(cache.name)
        diagnostics = tempfile.TemporaryFile()
        with _MacOSHelperCache(compiler) as persistent:
            binary = persistent.binary(root)
            try:
                helper = subprocess.Popen([str(binary), row["name"]], stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, stderr=diagnostics, **spawn_kwargs())
            except OSError:
                if not persistent.path:
                    raise
                from .util import log
                log("⚠ macOS helper 快取無法啟動；刪除該筆並重新編譯一次")
                binary = persistent.binary(root, rebuild=True)
                helper = subprocess.Popen([str(binary), row["name"]], stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, stderr=diagnostics, **spawn_kwargs())
        first_format = cmd.index("-f")
        input_end = cmd.index("-i", first_format) + 2
        native_cmd = [*cmd[:first_format], "-f", "matroska", "-i", "pipe:0", *cmd[input_end:]]
        try:
            proc = subprocess.Popen(native_cmd, stdin=helper.stdout, stdout=subprocess.PIPE,
                                    **spawn_kwargs())
        finally:
            helper.stdout.close()    # 不保留 pipe 的額外讀取端，ffmpeg 關閉時 helper 才能收到 EPIPE
        from .util import log
        log(f"🎙 macOS 原生擷取來源：{row['name']}")
        return MacOSCaptureProcess(proc, helper, cache, diagnostics)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        if helper:
            if helper.poll() is None:
                helper.kill()
            helper.wait()
        if diagnostics:
            diagnostics.close()
        if cache:
            cache.cleanup()
        return MacOSCaptureProcess(problem=str(exc))
