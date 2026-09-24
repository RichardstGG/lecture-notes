#!/usr/bin/env python3
"""第一次安裝用的互動式設定：偵測這台機器 → 問幾個問題 → 呼叫 setup_engines.py 編譯。

用法：
  python3 setup.py                互動模式（Windows 用 python setup.py）
  python3 setup.py --yes          全部用偵測到的預設值，不問問題
  python3 setup.py --dry-run      只顯示會做什麼，不真的編譯
  python3 setup.py --backend cuda 跳過後端詢問，直接指定

**這支腳本不會安裝任何系統套件**，只會告訴你缺哪些、以及這台機器對應的安裝指令。
已經裝好、要更新到新版請用 upgrade.py。
"""
import argparse
import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
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
                     "不想裝編譯環境可改用官方預編譯檔，見 README「Windows 預編譯檔」"},
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
        print("  先用 Vulkan（NVIDIA 也支援）；之後裝好 CUDA Toolkit 可以再跑一次這支腳本。")
        return "vulkan", "有 NVIDIA GPU 但沒有 CUDA Toolkit"
    if not env["gpus"]:
        print("  沒偵測到 GPU。只用 CPU 的話 whisper large-v3-turbo 會非常慢")
        print("  （Intel Arc 140V 是 6–7 倍即時速度，CPU 通常慢上好幾倍）。")
        if not ask_yes_no("仍要繼續（用 CPU）嗎？", False, ask):
            return None, "使用者取消"
        return "cpu", "沒偵測到 GPU"
    return default, f"{env['platform']} 的預設後端"


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


def build_command(engines, backend):
    return [sys.executable, str(ROOT / "setup_engines.py"), *engines, "--backend", backend]


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

    if sys.version_info < (3, 11):
        print(f"\n✖ lec 需要 Python 3.11 以上（現在是 {env['python']}），請先升級再跑一次。")
        return 1

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
    print(f"  → {backend}（{reason}）")

    engines = ["whisper"] if args.whisper_only else ([] if args.yes else choose_engines(env, ask))

    missing, blocking = missing_tools(backend)
    report_missing(missing, blocking, env["package_manager"])
    if blocking:
        print("\n✖ 先補上標成 ✖ 的東西，再跑一次 python3 setup.py。")
        return 1

    free = env["free_disk_gb"]
    if free and free < NEEDED_DISK_GB:
        print(f"\n⚠ 可用磁碟只有 {free:.0f} GB，引擎與模型大約需要 {NEEDED_DISK_GB} GB。")
        if not args.yes and not ask_yes_no("仍要繼續嗎？", False, ask):
            print("\n已取消。")
            return 1

    command = build_command(engines, backend)
    title("接下來會做的事")
    print("  " + " ".join(command))
    print("  （依 engines.lock 取得原始碼並編譯，順便下載 whisper 模型約 1.6GB）")
    if not engines:
        model, why = suggest_model(env)
        print(f"\n  總結模型：{why}")
        for line in model_hint(model):
            print(line)
        print("  （setup_engines.py 不會自動下載 LLM 模型；用 --import-models 可以搬入既有檔案）")
    if args.dry_run:
        print("\n--dry-run：到這裡為止，沒有編譯。")
        return 0
    if not args.yes and not ask_yes_no("\n開始編譯嗎？", True, ask):
        print("\n已取消。")
        return 1

    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        print("\n✖ setup_engines.py 失敗，訊息在上面。修好後可以直接重跑這支腳本。")
        return result.returncode

    title("下一步")
    lec = "python lec" if P.IS_WINDOWS else "./lec"
    print(f"  {lec} devices            列出麥克風，再用 --save <編號> 存成預設")
    print(f"  {lec} doctor             檢查整個環境（全部 ✔ 就可以上課了）")
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
