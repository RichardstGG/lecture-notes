#!/usr/bin/env python3
"""第一次安裝用的互動式設定：依賴自檢 → 問幾個問題 → 呼叫 setup_engines.py。

用法：
  python3 setup.py                互動模式（Windows 用 python setup.py）
  python3 setup.py --yes          全部用偵測到的預設值，不問問題
  python3 setup.py --dry-run      只顯示會做什麼，不真的編譯
  python3 setup.py --backend cuda 跳過後端詢問，直接指定
  python3 setup.py --prebuilt     Windows：下載官方預編譯檔，不編譯

啟動時先檢查 Python、執行時工具、GPU 與 CUDA Toolkit。有顯卡且是互動模式時，
先問總結要用本機 GPU 還是外部 API；NVIDIA 再問要不要 CUDA。還沒有 CUDA Toolkit
就只印安裝說明，不編譯、也不下載。外部 API 只裝本機 whisper，位址與金鑰寫進
不進 git 的 config/upstreams.toml。--yes 不會詢問，也不會改成外部 API。

Linux／macOS 不會安裝系統套件，只列出缺少的項目與對應指令。
Windows 的 windows_setup.bat 會加上 --provision：用 winget 補 ffmpeg、Git、
Node.js 與 VC++ 執行庫，下載 LLM 模型，裝好 Web UI，並可立刻啟動。
不會用 winget 安裝 Visual Studio、Vulkan SDK 或 CUDA Toolkit。
已經裝好、要更新到新版請用 upgrade.py。
Windows 沒有 Visual Studio 時，--yes 會改走 --prebuilt，不下載編譯工具。
"""
import argparse
import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from core import config as C                                     # noqa: E402
from core import platform as P                                   # noqa: E402

P.force_utf8()

# 執行 lec 需要、但不是編譯工具的東西（key: (label, 必要嗎, 用途)）
RUNTIME_TOOLS = {
    "ffmpeg": ("ffmpeg", True, "錄音與解碼"),
    "git": ("git", True, "取得 whisper.cpp / llama.cpp 原始碼"),
    "cmake": ("cmake", True, "編譯引擎"),
    "opencc": ("opencc", False, "轉成台灣繁體用語"),
    "pactl": ("pactl", False, "列出麥克風"),
}

# key → 各套件管理器的套件名稱。只寫有實際依據的（apt = Debian/Ubuntu，見 README
# 的安裝章節；brew = macOS；winget = Windows）。沒有對應項目的就印 note 讓使用者自己處理，
# 不猜套件名稱——套件名稱在不同發行版並不一致。
# 「notes」是某個套件管理器專屬的說明，「note」才是不分平台都成立的說明——
# 否則偵測不到套件管理器時會把 Windows 的說明印在 Linux 上（實測踩到過）。
PACKAGES = {
    "ffmpeg": {"apt": ["ffmpeg"], "brew": ["ffmpeg"], "winget": ["Gyan.FFmpeg"]},
    "git": {"apt": ["git"], "brew": ["git"], "winget": ["Git.Git"]},
    "cmake": {"apt": ["cmake"], "brew": ["cmake"], "winget": ["Kitware.CMake"]},
    "opencc": {"apt": ["opencc"], "brew": ["opencc"],
               "notes": {"winget": "Windows 沒有現成套件，可略過（只影響繁體用語轉換）"}},
    "pactl": {"apt": ["pulseaudio-utils"]},
    "cxx": {"apt": ["build-essential", "pkg-config"],
            "notes": {"brew": "執行 xcode-select --install（Command Line Tools，不是 brew 套件）"}},
    "glslc": {"apt": ["glslc"]},
    "libvulkan": {"apt": ["libvulkan-dev", "vulkan-tools"]},
    "vulkan_sdk": {"winget": ["LunarG.VulkanSDK"],
                   "note": "或從 https://vulkan.lunarg.com 下載 Vulkan SDK"},
    "msvc": {"note": "安裝 Visual Studio Build Tools 並勾選「使用 C++ 的桌面開發」"
                     "工作負載（https://visualstudio.microsoft.com/downloads/）；"
                     "不想裝編譯環境可執行 python setup_engines.py --prebuilt"},
    "nvcc": {"note": "安裝 CUDA Toolkit（https://developer.nvidia.com/cuda-downloads）；"
                     "Windows 要一併勾選 Visual Studio Integration"},
}
INSTALL_PREFIX = {"apt": "sudo apt install", "brew": "brew install",
                  "winget": "winget install"}

MODEL_8B, MODEL_4B = "qwen3-8b", "qwen3-4b"
MODEL_FILES = {MODEL_8B: "Qwen3-8B-Q4_K_M.gguf", MODEL_4B: "Qwen3-4B-Q4_K_M.gguf"}
MODEL_URL = "https://huggingface.co/{repo}-GGUF/resolve/main/{name}"
SMALL_MODEL_MEMORY_GB = 16      # 低於這個記憶體就建議 4B 而不是 8B
NEEDED_DISK_GB = 15             # 引擎 build + whisper 模型 1.6GB + LLM 約 5GB
# 缺了這些就不能從原始碼編譯，但 Windows 官方預編譯檔不需要它們。
COMPILE_ONLY_KEYS = {"git", "cmake", "msvc", "vulkan_sdk", "cxx", "glslc", "libvulkan", "nvcc"}


def title(msg):
    print(f"\n==== {msg} ====", flush=True)


def pad(text, width):
    """中文是全形字，用字元數對齊會歪（跟 lec doctor 的輸出同樣處理）。"""
    shown = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(1, width - shown)


def ask_yes_no(question, default, ask):
    """回傳 True/False；ask 是取得輸入的函式（測試時可注入）。"""
    hint = "[Y/n]" if default else "[y/N]"
    answer = (ask(f"{question} {hint} ") or "").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "是")


def ask_choice(question, options, default, ask):
    """印出代號選項。空答案用 default；對不上任何代號就回 None。"""
    print(question)
    for key, label in options:
        mark = "（預設）" if key == default else ""
        print(f"    {key}  {label}{mark}")
    answer = (ask("  請選：") or "").strip().lower()
    if not answer:
        return default
    for key, _label in options:
        if answer == key.lower():
            return key
    return None


def has_gpu(env):
    return bool(env.get("gpus") or env.get("nvidia"))


def _mark(ok, required):
    if ok:
        return "✔"
    return "✖" if required else "⚠"


def detect():
    """把 setup 需要知道的環境資訊收成一個 dict（純讀取，不動任何東西）。"""
    return {
        "platform": P.NAME,
        "describe": P.describe(),
        "arch": os.uname().machine if hasattr(os, "uname") else os.environ.get(
            "PROCESSOR_ARCHITECTURE", "?"),
        "python": ".".join(str(v) for v in sys.version_info[:3]),
        "memory_gb": P.total_memory_gb(),
        "free_disk_gb": P.free_disk_gb(ROOT),
        "gpus": P.gpu_names(),
        "nvidia": P.nvidia_gpu(),
        "cuda": P.cuda_toolkit(),
        "package_manager": P.package_manager(),
        "default_backend": P.engine_backend(),
    }


def show_environment(env):
    title("這台機器")
    rows = [("作業系統", f"{env['describe']}｜{env['arch']}"),
            ("Python", env["python"]),
            ("記憶體", f"{env['memory_gb']:.0f} GB" if env["memory_gb"] else "問不到"),
            ("可用磁碟", f"{env['free_disk_gb']:.0f} GB" if env["free_disk_gb"] else "問不到"),
            ("GPU", "；".join(env["gpus"]) if env["gpus"] else "沒偵測到獨立／內建 GPU"),
            ("CUDA Toolkit", env["cuda"] or "沒有"),
            ("套件管理器", env["package_manager"] or "沒偵測到")]
    for name, value in rows:
        print(f"  {pad(name, 14)}{value}")


def report_dependencies(env):
    """啟動時的依賴自檢。只印結果，不安裝、也不在這裡中止。"""
    title("依賴自檢")
    py_ok = sys.version_info >= (3, 11)
    py_note = env["python"] if py_ok else f"{env['python']}（需要 3.11 以上）"
    print(f"  {_mark(py_ok, True)} Python  {py_note}")
    for key, (label, required, why) in RUNTIME_TOOLS.items():
        if key == "pactl" and env["platform"] != "linux":
            continue
        found = shutil.which(key)
        compile_only = key in COMPILE_ONLY_KEYS
        note = found or f"找不到（{why}）"
        if not found and compile_only:
            note += "；只編譯原始碼時需要，Windows 預編譯檔可略過"
        # 編譯工具缺了仍可改走預編譯檔，自檢用警告；ffmpeg 這種執行期依賴才算失敗。
        print(f"  {_mark(bool(found), required and not compile_only)} {label}  {note}")
    if env.get("gpus"):
        gpu = "；".join(env["gpus"])
    else:
        gpu = "沒偵測到"
    print(f"  {_mark(has_gpu(env), False)} GPU  {gpu}")
    if env.get("nvidia"):
        cuda = env.get("cuda")
        print(f"  {_mark(bool(cuda), False)} CUDA Toolkit  "
              + (cuda or "沒有（可改用 Vulkan，或先安裝 CUDA Toolkit 再選 CUDA）"))
    elif has_gpu(env):
        print("  ⚠ CUDA  這張卡不是 NVIDIA，本機後端不會用 CUDA")
    else:
        print("  ⚠ CUDA  沒有 NVIDIA GPU")


def choose_backend(env, ask, forced=None):
    """回傳 (後端, 為什麼)。Vulkan 在 NVIDIA 上也能跑，所以 CUDA 是「要不要更快」的選擇。"""
    if forced:
        return forced, "由 --backend 指定"
    default = env["default_backend"]
    if env["platform"] == "macos":
        return "metal", "macOS 一律用 Metal"
    if env["nvidia"] and env["cuda"]:
        question = (f"偵測到 {env['nvidia']} 與 CUDA Toolkit {env['cuda']}。"
                    "要用 CUDA 編譯嗎？（n = 用 Vulkan，NVIDIA 也支援）")
        if ask_yes_no(question, True, ask):
            return "cuda", "偵測到 NVIDIA GPU 與 CUDA Toolkit"
        return "vulkan", "有 CUDA 但你選擇 Vulkan"
    if env["nvidia"]:
        print(f"  偵測到 {env['nvidia']}，但沒有 CUDA Toolkit（nvcc）。")
        print("  選 CUDA 的話這次不會編譯，只會印出安裝說明；裝好 Toolkit 後再跑一次。")
        print("  選否就改用 Vulkan（NVIDIA 也支援）。")
        if ask_yes_no("仍要使用 CUDA 後端嗎？", False, ask):
            return "cuda", "你選擇 CUDA，但還沒有 CUDA Toolkit"
        return "vulkan", "有 NVIDIA GPU 但沒有 CUDA Toolkit，改用 Vulkan"
    if not env["gpus"]:
        print("  沒偵測到 GPU。只用 CPU 的話 whisper large-v3-turbo 會非常慢")
        print("  （Intel Arc 140V 是 6–7 倍即時速度，CPU 通常慢上好幾倍）。")
        if not ask_yes_no("仍要繼續（用 CPU）嗎？", False, ask):
            return None, "使用者取消"
        return "cpu", "沒偵測到 GPU"
    return default, f"{env['platform']} 的預設後端"


def choose_summary_route(env, ask):
    """有顯卡時問總結要走本機 GPU 還是外部 API。沒有顯卡維持本機。回傳 local、api 或 None。"""
    if not has_gpu(env):
        return "local"
    names = "；".join(env["gpus"]) if env["gpus"] else env["nvidia"]
    if env.get("nvidia"):
        local = "本機顯卡（下一步可選 CUDA 或 Vulkan；語音轉錄仍用本機 whisper）"
    elif env.get("platform") == "macos":
        local = "本機顯卡（Metal；語音轉錄仍用本機 whisper）"
    elif env.get("platform") == "windows":
        local = "本機顯卡（Vulkan。沒有 Visual Studio 時可下載官方預編譯檔；只有 NVIDIA 能用 CUDA）"
    else:
        local = "本機顯卡（Vulkan；語音轉錄仍用本機 whisper）"
    picked = ask_choice(
        f"\n  偵測到顯卡：{names}\n  總結要怎麼做？",
        [("1", local),
         ("2", "外部模型 API（不裝 llama.cpp、不下載 GGUF；轉錄仍用本機 whisper）")],
        "1", ask)
    if picked == "1":
        return "local"
    if picked == "2":
        return "api"
    print("  認不得這個選項。")
    return None


def _plain_text(value):
    return isinstance(value, str) and value.strip() and not any(ord(c) < 32 for c in value)


def upstream_error(spec):
    """回傳第一個不符合 config/upstreams.toml 契約的原因；合格就回 None。"""
    ident = spec.get("id", "")
    if ident == "local" or not C.UPSTREAM_ID.fullmatch(ident or ""):
        return "ID 必須是英數開頭，之後可接英數、底線或連字號，最多 64 字，而且不能叫 local"
    for field, label in (("name", "顯示名稱"), ("base_url", "base_url"), ("model", "模型 ID")):
        if not _plain_text(spec.get(field, "")):
            return f"{label} 不能空白，也不能含控制字元"
    url = urlsplit(spec["base_url"])
    if (any(c.isspace() for c in spec["base_url"]) or url.scheme not in {"http", "https"}
            or not url.hostname or url.username or url.password or url.query or url.fragment):
        return "base_url 必須是 http 或 https，且不能有帳密、query 或 fragment"
    try:
        _ = url.port
    except ValueError:
        return "base_url 的 port 不合法"
    if spec.get("api_key") and spec.get("api_key_env"):
        return "api_key 與 api_key_env 只能留一個"
    for field in ("api_key", "api_key_env"):
        if field in spec and not _plain_text(spec[field]):
            return f"{field} 不能空白，也不能含控制字元"
    return None


def prompt_upstream(ask):
    """問外部摘要 API。不合格回 None，不把金鑰印回畫面。"""
    print("  語音轉錄仍在本機。這裡只設定總結用的 OpenAI 相容 API。")
    print("  寫進 config/upstreams.toml 的內容不進 git。金鑰不會被印出來。")
    ident = (ask("  上游 ID（英數開頭，例如 lab）：") or "").strip()
    name = (ask("  顯示名稱：") or "").strip()
    base_url = (ask("  base_url（需含路徑，通常以 /v1 結尾）：") or "").strip()
    model = (ask("  模型 ID：") or "").strip()
    auth = (ask("  認證  1 不需要  2 環境變數名稱（預設）  3 把 key 寫進私有檔 [2] ") or "").strip() or "2"
    spec = {"id": ident, "name": name, "base_url": base_url, "model": model}
    if auth == "1":
        pass
    elif auth == "3":
        spec["api_key"] = (ask("  API key：") or "").strip()
    elif auth == "2":
        spec["api_key_env"] = (ask("  環境變數名稱（例如 LEC_LAB_API_KEY）：") or "").strip()
    else:
        print("  ✖ 認不得的認證選項。")
        return None
    error = upstream_error(spec)
    if error:
        print(f"  ✖ {error}")
        return None
    return spec


def save_api_upstream(spec):
    """寫入私有 upstreams.toml，並把 summary.upstream 記進 local.toml。"""
    error = upstream_error(spec)
    if error:
        raise C.ConfigError(error)
    existing = C.load_upstreams() if C.UPSTREAMS_FILE.exists() else {}
    entry = {"name": spec["name"], "base_url": spec["base_url"], "model": spec["model"]}
    if spec.get("api_key_env"):
        entry["api_key_env"] = spec["api_key_env"]
    elif spec.get("api_key"):
        entry["api_key"] = spec["api_key"]
    existing[spec["id"]] = entry
    text = C.dump_toml({"upstreams": existing}, "私有摘要上游（不進 git）；由 setup.py 寫入")
    C.UPSTREAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = C.UPSTREAMS_FILE.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(C.UPSTREAMS_FILE)
    C.load_upstreams()
    C.set_local("summary.upstream", spec["id"])


def choose_engines(env, ask):
    """回傳要處理的引擎 list（[] = 兩個都要，對應 setup_engines.py 的預設）。"""
    print("\n  總結（llama.cpp + Qwen3-8B）需要再多約 5GB 的模型；")
    print("  只要逐字稿的話可以只裝 whisper.cpp，之後用 lec run --transcribe-only。")
    if ask_yes_no("要連 llama.cpp 一起裝（做課堂總結）嗎？", True, ask):
        return []
    return ["whisper"]


def suggest_model(env):
    """依記憶體建議總結模型；回傳 (模型名稱, 說明)。"""
    memory = env["memory_gb"]
    if memory and memory < SMALL_MODEL_MEMORY_GB:
        return MODEL_4B, f"記憶體只有 {memory:.0f} GB，建議先用較小的 {MODEL_4B}"
    return MODEL_8B, f"建議預設的 {MODEL_8B}"


def missing_tools(backend):
    """回傳 (缺少的 key list, 其中哪些是「不補就不能繼續」的)。"""
    missing, blocking = [], []
    for key, (label, required, _why) in RUNTIME_TOOLS.items():
        if key == "pactl" and P.NAME != "linux":
            continue
        if not shutil.which(key):
            missing.append(key)
            if required:
                blocking.append(key)
    for key in P.missing_build_tool_keys(backend):
        missing.append(key)
        blocking.append(key)                 # 編譯器或後端 SDK 缺了就編不起來
    return missing, blocking


def _note_for(entry, manager):
    """這個工具在這台機器上該顯示的說明；沒有適用的就回 None。"""
    return (entry.get("notes") or {}).get(manager) or entry.get("note")


def install_lines(keys, manager):
    """把缺少的 key 變成「要印給使用者的行」。不執行任何安裝。"""
    lines, packages, notes = [], [], []
    for key in keys:
        entry = PACKAGES.get(key, {})
        names = entry.get(manager)
        note = _note_for(entry, manager)
        if names:
            packages += names
        elif note:
            notes.append(f"{label_of(key)}：{note}")
        else:
            notes.append(f"{label_of(key)}：請用你的套件管理器安裝"
                         "（不同發行版套件名稱不一致，這裡不猜）")
    if packages and manager in INSTALL_PREFIX:
        lines.append(f"  {INSTALL_PREFIX[manager]} " + " ".join(dict.fromkeys(packages)))
    lines += [f"  {note}" for note in notes]
    return lines


def label_of(key):
    if key in RUNTIME_TOOLS:
        return RUNTIME_TOOLS[key][0]
    return P.BUILD_TOOLS.get(key, key)


def report_missing(keys, blocking, manager):
    title("缺少的套件")
    if not keys:
        print("  工具齊全，不需要安裝任何東西。")
        return
    for key in keys:
        mark = "✖" if key in blocking else "⚠"
        why = RUNTIME_TOOLS[key][2] if key in RUNTIME_TOOLS else "編譯引擎"
        print(f"  {mark} {label_of(key)}（{why}）")
    print("\n  安裝指令（lec 不會幫你執行，請自己跑）：")
    for line in install_lines(keys, manager):
        print(line)
    if not manager:
        print("  （沒偵測到支援的套件管理器，上面只列出缺少的東西）")


# Windows 啟動檔才會安裝的執行期套件。編譯器、Vulkan SDK、CUDA 不在這裡。
WINDOWS_RUNTIME = (
    ("ffmpeg", "Gyan.FFmpeg", lambda: bool(shutil.which("ffmpeg"))),
    ("git", "Git.Git", lambda: bool(shutil.which("git"))),
    ("Node.js", "OpenJS.NodeJS.LTS", lambda: node_is_acceptable(node_version())),
    ("VC++ 2015+", "Microsoft.VCRedist.2015+.x64", lambda: vc_redist_x64_installed()),
)


def node_version():
    """回傳 node 的版本 tuple；找不到或問不到就回 None。"""
    node = shutil.which("node")
    if not node:
        return None
    try:
        result = subprocess.run(
            [node, "-p", "process.versions.node"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = []
    for piece in (result.stdout or "").strip().split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) or None


def node_is_acceptable(version):
    """對齊 ui/frontend 的 engines：22.22+、24.15+，或 26 以上。"""
    if not version:
        return False
    if version >= (26, 0):
        return True
    if version >= (24, 15):
        return True
    return (22, 22) <= version < (23, 0)


def vc_redist_x64_installed():
    """預編譯的 .exe 需要 VC++ 2015–2022 x64 執行庫。非 Windows 視為不需要。"""
    if os.name != "nt":
        return True
    import winreg
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X64"),
    )
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as key:
                installed, _typ = winreg.QueryValueEx(key, "Installed")
        except OSError:
            continue
        if installed:
            return True
    return False


def refresh_windows_path():
    """winget 寫進登錄檔的 PATH 不會自動進這個行程。把使用者與系統 Path 拼回來。"""
    if os.name != "nt":
        return
    import winreg

    def read(root, sub):
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _typ = winreg.QueryValueEx(key, "Path")
        except OSError:
            return ""
        return os.path.expandvars(str(value))

    user = read(winreg.HKEY_CURRENT_USER, r"Environment")
    machine = read(winreg.HKEY_LOCAL_MACHINE,
                   r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
    current = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(part for part in (user, machine, current) if part)


def missing_runtime_packages():
    return [(label, package) for label, package, ready in WINDOWS_RUNTIME if not ready()]


def winget_install(package):
    command = ["winget", "install", "--id", package, "-e",
               "--accept-package-agreements", "--accept-source-agreements",
               "--disable-interactivity"]
    print("  $ " + " ".join(command), flush=True)
    result = subprocess.run(command)
    refresh_windows_path()
    return result.returncode


def install_missing_runtime(args, ask):
    """用 winget 補 Windows 執行環境。呼叫端負責限定平台。回傳有沒有補齊。"""
    missing = missing_runtime_packages()
    if not missing:
        print("✔ Windows 執行環境齊全（ffmpeg、Git、Node.js、VC++）。")
        return True
    print("\n  還缺：" + "、".join(f"{label}（{package}）" for label, package in missing))
    if not args.yes and not ask_yes_no("要用 winget 安裝這些執行環境嗎？", True, ask):
        print("  請先安裝上面的項目，再跑一次 windows_setup.bat。")
        return False
    if not shutil.which("winget"):
        print("✖ 找不到 winget，無法自動安裝。請先安裝 App Installer，或手動安裝上面的項目。")
        return False
    for _label, package in missing:
        winget_install(package)
    still = missing_runtime_packages()
    if still:
        print("✖ 安裝後仍然缺少：" + "、".join(label for label, _package in still))
        print("  新裝的程式可能還不在這個視窗的 PATH。關掉視窗再跑一次 windows_setup.bat。")
        return False
    print("✔ 執行環境已補齊")
    return True


def remember_smaller_model(model):
    """記憶體不夠、又還沒指定模型時，把 4B 記進 local.toml。預設 8B 不用寫。"""
    if model != MODEL_4B:
        return
    data = C.load_toml(C.LOCAL_FILE) if C.LOCAL_FILE.exists() else {}
    if (data.get("summary") or {}).get("model"):
        return
    C.set_local("summary.model", MODEL_4B)
    print(f"✔ 已把 summary.model 記成 {MODEL_4B}（config/local.toml，不進 git）")


def ensure_llm_model(model):
    """下載建議的 GGUF。已有成品就略過；中斷的部分留在 .part，重跑會續傳。"""
    dest = ROOT / "models" / MODEL_FILES[model]
    if dest.is_file() and dest.stat().st_size > 0:
        print(f"✔ 已有模型 {dest.name}（{dest.stat().st_size / 1e9:.1f} GB）")
        remember_smaller_model(model)
        return 0
    url = model_url(model)
    print(f"▶ 下載 {dest.name}")
    print(f"  {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        dest.unlink()
    part = dest.with_name(dest.name + ".part")
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        print("✖ 找不到 curl，無法下載 LLM 模型。")
        return 1
    result = subprocess.run([curl, "-L", "-C", "-", "--fail", "-o", str(part), url])
    if result.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
        print("✖ 模型下載失敗。再跑一次 windows_setup.bat 會從中斷的地方續傳。")
        return 1
    os.replace(part, dest)
    print(f"✔ {dest.name}（{dest.stat().st_size / 1e9:.1f} GB）")
    remember_smaller_model(model)
    return 0


def ensure_ui():
    """建立 .venv、安裝 backend，並編譯 frontend。已經可用就略過。"""
    import upgrade
    refresh_windows_path()
    dist = ROOT / "ui" / "frontend" / "dist" / "index.html"
    python = upgrade.venv_python()
    if python.is_file() and dist.is_file():
        probe = subprocess.run([str(python), "-c", "import fastapi"], capture_output=True)
        if probe.returncode == 0:
            print("✔ Web UI 已安裝")
            return 0
    try:
        upgrade.install_ui()
    except upgrade.UpgradeError as exc:
        print(f"✖ {exc}")
        return 1
    if not dist.is_file():
        print("✖ Web UI 編譯結束，但找不到 ui/frontend/dist/index.html")
        return 1
    print("✔ Web UI 已安裝")
    return 0


def maybe_launch_ui(args, ask):
    command = [sys.executable, str(ROOT / "lec"), "--start-ui"]
    shown = " ".join(command)
    if args.yes or args.dry_run or not sys.stdin.isatty():
        print(f"啟動 UI：{shown}")
        print("  瀏覽器開 http://127.0.0.1:8765")
        return 0
    if not ask_yes_no("要現在啟動 Web UI 嗎？瀏覽器開 http://127.0.0.1:8765", True, ask):
        print(f"  稍後執行：{shown}")
        return 0
    print("▶ 這個視窗要留著。關掉它或按 Ctrl+C 只會停 UI，不會刪已安裝的東西。")
    try:
        result = subprocess.run(command, cwd=str(ROOT))
    except KeyboardInterrupt:
        print("\n已關閉 UI。")
        return 0
    if result.returncode in (0, 130) or (result.returncode is not None and result.returncode < 0):
        print("\n已關閉 UI。")
        return 0
    return result.returncode


def wants_full_install(args):
    return bool(args.provision) and not args.dry_run


def finish_usable_install(args, env, engines, upstream, ask):
    """引擎就緒之後，補上本機模型與 Web UI，讓啟動檔跑完就能用。"""
    if upstream is None and engines != ["whisper"]:
        model, why = suggest_model(env)
        print(f"\n  總結模型：{why}")
        if ensure_llm_model(model) != 0:
            return 1
    elif engines == ["whisper"]:
        print("\n  這次不下載 LLM 模型。")
    if ensure_ui() != 0:
        return 1
    return maybe_launch_ui(args, ask)


def build_command(engines, backend, prebuilt=False):
    command = [sys.executable, str(ROOT / "setup_engines.py"), *engines, "--backend", backend]
    if prebuilt:
        command.append("--prebuilt")
    return command


def runtime_blockers(blocking):
    """編譯工具以外、缺了就連預編譯檔都不能用的項目（例如 ffmpeg）。"""
    return [key for key in blocking if key not in COMPILE_ONLY_KEYS]


def model_url(model):
    repo = "Qwen/Qwen3-8B" if model == MODEL_8B else "Qwen/Qwen3-4B"
    return MODEL_URL.format(repo=repo, name=MODEL_FILES[model])


def model_hint(model):
    """LLM 模型要自己下載（setup_engines.py 只會自動抓 whisper 模型）。
    這裡不印 shell 指令：curl 的換行寫法在 PowerShell 與 cmd 不一樣，README 步驟 3 兩種都有。"""
    return [f"  檔案：{MODEL_FILES[model]} → 放進 {ROOT / 'models'}",
            f"  來源：{model_url(model)}",
            "  指令見 README「下載 LLM 模型」（Linux/macOS 與 Windows 各一份）"]


def run_setup(args, ask):
    env = detect()
    show_environment(env)
    report_dependencies(env)

    if sys.version_info < (3, 11):
        print(f"\n✖ lec 需要 Python 3.11 以上（現在是 {env['python']}），請先升級再跑一次。")
        return 1

    upstream = None
    route = "local"
    if not args.yes and not args.whisper_only and not args.backend and has_gpu(env):
        route = choose_summary_route(env, ask)
        if not route:
            print("\n已取消。")
            return 1
    if route == "api":
        title("外部摘要 API")
        upstream = prompt_upstream(ask)
        if not upstream:
            print("\n已取消。")
            return 1
        backend = "metal" if env["platform"] == "macos" else env["default_backend"]
        reason = f"外部 API {upstream['id']}：只裝本機 whisper"
        engines = ["whisper"]
    else:
        title("後端")
        backend = args.backend
        if args.yes and not backend:
            backend = "metal" if env["platform"] == "macos" else env["default_backend"]
            reason = "--yes：用偵測到的預設值"
            if env["nvidia"] and env["cuda"]:
                backend, reason = "cuda", "--yes：偵測到 NVIDIA GPU 與 CUDA Toolkit"
        else:
            backend, reason = choose_backend(env, ask, forced=backend)
        if not backend:
            print("\n已取消。")
            return 1
        engines = ["whisper"] if args.whisper_only else ([] if args.yes else choose_engines(env, ask))
    print(f"  → {backend}（{reason}）")

    missing, blocking = missing_tools(backend)
    report_missing(missing, blocking, env["package_manager"])

    use_prebuilt = False
    if args.prebuilt:
        if env["platform"] != "windows":
            print("\n✖ --prebuilt 只適用於 Windows。")
            return 1
        if backend != "vulkan":
            print("\n✖ 內建的 Windows 預編譯檔是 Vulkan 版 llama.cpp（whisper 為官方 CPU 版）。")
            print("  CUDA 請看 README「Windows 預編譯檔」，或拿掉 --prebuilt 自己編譯。")
            return 1
        use_prebuilt = True
    if wants_full_install(args) and env["platform"] == "windows":
        if not install_missing_runtime(args, ask):
            return 1
        missing, blocking = missing_tools(backend)
    if runtime_blockers(blocking):
        print("\n✖ 先補上標成 ✖ 的東西，再跑一次 python3 setup.py。")
        return 1
    if backend == "cuda" and not env.get("cuda"):
        print("\n✖ 還沒有 CUDA Toolkit（nvcc）。這次不會編譯，也不會下載。")
        if "nvcc" not in missing:
            print("  安裝說明（不會自動安裝）：")
            for line in install_lines(["nvcc"], env["package_manager"]):
                print(line)
        print("  裝好 Toolkit 後再跑一次；或重新執行，改選 Vulkan。")
        return 1
    if blocking and not use_prebuilt:
        if env["platform"] == "windows" and backend == "vulkan":
            print("\n  沒有編譯環境時，可以改下載官方預編譯檔（不需要 Visual Studio 或 Vulkan SDK）。")
            if engines == ["whisper"]:
                print("  這次只裝 whisper.cpp。官方 Windows 版是 CPU。")
            else:
                print("  whisper.cpp 官方 Windows 版是 CPU；llama.cpp 這個包是 Vulkan，會用到顯示卡。")
            if args.yes:
                use_prebuilt = True
                print("  → --yes：改用官方預編譯檔")
            elif ask_yes_no("改用官方 Windows 預編譯檔嗎？", True, ask):
                use_prebuilt = True
            else:
                print("\n✖ 先補上標成 ✖ 的東西，再跑一次 python3 setup.py。")
                return 1
        else:
            print("\n✖ 先補上標成 ✖ 的東西，再跑一次 python3 setup.py。")
            return 1

    needed = 4 if engines == ["whisper"] else NEEDED_DISK_GB
    free = env["free_disk_gb"]
    if free and free < needed:
        print(f"\n⚠ 可用磁碟只有 {free:.0f} GB，這次大約需要 {needed} GB。")
        if not args.yes and not ask_yes_no("仍要繼續嗎？", False, ask):
            print("\n已取消。")
            return 1

    command = build_command(engines, backend, prebuilt=use_prebuilt)
    title("接下來會做的事")
    print("  " + " ".join(command))
    if use_prebuilt:
        print("  （下載官方 Windows 預編譯檔，不編譯；順便下載 whisper 模型約 1.6GB）")
    else:
        print("  （依 engines.lock 取得原始碼並編譯，順便下載 whisper 模型約 1.6GB）")
    if not engines:
        model, why = suggest_model(env)
        print(f"\n  總結模型：{why}")
        if wants_full_install(args):
            print("  引擎裝好後會下載這個 GGUF。")
        else:
            for line in model_hint(model):
                print(line)
            print("  （setup_engines.py 不會自動下載 LLM 模型；用 --import-models 可以搬入既有檔案）")
    if upstream:
        print(f"\n  摘要上游 {upstream['id']} 會寫進 config/upstreams.toml，"
              "並把 summary.upstream 記進 config/local.toml（兩個都不進 git）")
    if args.dry_run:
        if args.provision:
            print("  --provision：正式執行時還會用 winget 補執行環境、下載 LLM 模型、安裝 Web UI。")
        print("\n--dry-run：到這裡為止，沒有真的安裝。")
        return 0
    question = "\n開始下載預編譯檔嗎？" if use_prebuilt else "\n開始編譯嗎？"
    if not args.yes and not ask_yes_no(question, True, ask):
        print("\n已取消。")
        return 1
    if upstream:
        try:
            save_api_upstream(upstream)
        except C.ConfigError as exc:
            print(f"\n✖ 無法記下外部 API：{exc}")
            return 1
        print(f"✔ 已記下上游 {upstream['id']}")

    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        print("\n✖ setup_engines.py 失敗，訊息在上面。修好後可以直接重跑這支腳本。")
        return result.returncode

    if wants_full_install(args):
        code = finish_usable_install(args, env, engines, upstream, ask)
        if code != 0:
            return code

    title("下一步")
    lec = "python lec" if P.IS_WINDOWS else "./lec"
    print(f"  {lec} devices            列出麥克風，再用 --save <編號> 存成預設")
    print(f"  {lec} doctor             檢查整個環境（全部 ✔ 就可以上課了）")
    if upstream:
        if upstream.get("api_key_env"):
            print(f"  先在這個視窗設定環境變數 {upstream['api_key_env']}，再啟動 lec。")
        print(f"  {lec} run 課名 --upstream {upstream['id']}")
    else:
        print(f"  {lec} run 課名 --file samples/test8min.ogg    用內建樣本跑一次完整流程")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="互動式安裝：偵測環境、告知缺少的套件、編譯引擎",
        epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="不問問題，全部用偵測到的預設值")
    parser.add_argument("--dry-run", action="store_true", help="只顯示會做什麼，不編譯")
    parser.add_argument("--backend", choices=["vulkan", "cuda", "metal", "cpu"],
                        help="跳過後端詢問，直接指定")
    parser.add_argument("--whisper-only", action="store_true",
                        help="只裝 whisper.cpp（不做總結）")
    parser.add_argument("--prebuilt", action="store_true",
                        help="Windows：下載官方預編譯檔，不編譯")
    parser.add_argument("--provision", action="store_true",
                        help="補齊執行環境、LLM 模型與 Web UI；windows_setup.bat 會加上")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return run_setup(args, input)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
