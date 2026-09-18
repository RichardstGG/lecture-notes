#!/usr/bin/env python3
"""在專案目錄下取得並編譯 whisper.cpp / llama.cpp，版本鎖定在 engines.lock。

用法：
  python3 setup_engines.py                    依 engines.lock 的版本編譯（已編好同版本就略過）
  python3 setup_engines.py whisper|llama      只處理其中一個
  python3 setup_engines.py --update           升級到最新版，編譯成功後寫回 engines.lock
  python3 setup_engines.py --rebuild          版本沒變也強制重新編譯
  python3 setup_engines.py --lock             不編譯，只把目前 checkout 的版本記進 engines.lock
  python3 setup_engines.py --backend vulkan   後端：auto（預設）/ vulkan / cuda / metal / cpu
  python3 setup_engines.py --import-models DIR  從其他位置搬入已下載的模型
  python3 setup_engines.py --generator Ninja  指定 cmake generator（Windows 想用 Ninja 而非 Visual Studio 時）

後端預設：Linux 與 Windows 用 Vulkan，macOS 用 Metal。
以靜態連結編譯（BUILD_SHARED_LIBS=OFF），整個專案資料夾搬到哪裡都能執行。
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from core import platform as P                                   # noqa: E402

P.force_utf8()

WHISPER_REPO = "https://github.com/ggml-org/whisper.cpp.git"
LLAMA_REPO = "https://github.com/ggml-org/llama.cpp.git"
WHISPER_MODEL = "large-v3-turbo"
WHISPER_MODEL_URL = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
                     f"ggml-{WHISPER_MODEL}.bin")
LOCK_FILE = ROOT / "engines.lock"

ENGINES = {
    "whisper": {"dir": ROOT / "whisper.cpp", "repo": WHISPER_REPO, "key": "WHISPER_REF",
                "args": ["-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_SDL2=OFF"],
                "targets": ["whisper-server", "whisper-cli"]},
    "llama": {"dir": ROOT / "llama.cpp", "repo": LLAMA_REPO, "key": "LLAMA_REF",
              "args": ["-DLLAMA_OPENSSL=OFF", "-DLLAMA_BUILD_TESTS=OFF",
                       "-DLLAMA_BUILD_EXAMPLES=OFF"],
              "targets": ["llama-server", "llama-bench"]},
}
BACKEND_FLAGS = {"vulkan": ["-DGGML_VULKAN=ON"], "cuda": ["-DGGML_CUDA=ON"],
                 "metal": ["-DGGML_METAL=ON"], "cpu": []}

APT_HINT = ("sudo apt install git cmake build-essential pkg-config "
            "libvulkan-dev glslc vulkan-tools ffmpeg opencc")
BREW_HINT = "xcode-select --install && brew install cmake ffmpeg opencc"
WIN_HINT = ("請安裝：Git for Windows、CMake、Visual Studio Build Tools（含 C++ 桌面開發），"
            "Vulkan 版另需 Vulkan SDK（https://vulkan.lunarg.com），CUDA 版另需 CUDA Toolkit。\n"
            "  不想安裝編譯環境的話，可改用官方預編譯檔，見 README「Windows 預編譯檔」。")


def step(msg):
    print(f"\n==== {msg} ====", flush=True)


def die(msg):
    print(f"✖ {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd, **kw):
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], **kw)
    if r.returncode != 0:
        die(f"指令失敗（{r.returncode}）：{' '.join(str(c) for c in cmd)}")
    return r


def out(cmd):
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                           errors="replace", timeout=60)
        return (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------- engines.lock
def lock_read():
    refs = {}
    if LOCK_FILE.exists():
        for line in LOCK_FILE.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                refs[k.strip()] = v.strip()
    return refs


def lock_write(key, directory):
    sha = out(["git", "-C", directory, "rev-parse", "HEAD"])
    desc = out(["git", "-C", directory, "log", "-1", "--format=%cs %s"])[:70]
    lines = []
    if LOCK_FILE.exists():
        lines = [l for l in LOCK_FILE.read_text(encoding="utf-8").splitlines()
                 if not l.startswith(f"{key}=") and not l.startswith(f"# {key}:")]
    else:
        lines = ["# 已測試的 whisper.cpp / llama.cpp 版本；setup_engines.py 會 checkout 這些 commit",
                 "# 升級：python3 setup_engines.py --update（編譯成功後自動更新本檔）"]
    lines += [f"# {key}: {desc}", f"{key}={sha}"]
    LOCK_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"▶ engines.lock：{key}={sha[:9]}（{desc}）")


def build_stamp(directory, backend, args, generator=None):
    sha = out(["git", "-C", directory, "rev-parse", "HEAD"])
    gen = f" GENERATOR={generator}" if generator else ""
    return f"{sha} BACKEND={backend}{gen} {' '.join(args)}"


# ---------------------------------------------------------------- 前置檢查
def check_tools(backend):
    missing = ["git"] if not shutil.which("git") else []
    if not shutil.which("cmake"):
        missing.append("cmake")
    if P.NAME == "windows":
        if not (shutil.which("cl") or shutil.which("ninja") or
                Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"),
                     "Microsoft Visual Studio").exists()):
            missing.append("Visual Studio Build Tools")
        if backend == "vulkan" and not (os.environ.get("VULKAN_SDK") or shutil.which("glslc")):
            missing.append("Vulkan SDK")
    else:
        if not shutil.which("c++") and not shutil.which("clang++") and not shutil.which("g++"):
            missing.append("C++ 編譯器")
        if backend == "vulkan":
            if not shutil.which("glslc"):
                missing.append("glslc")
            has_vulkan = bool(shutil.which("pkg-config")) and subprocess.run(
                ["pkg-config", "--exists", "vulkan"], capture_output=True).returncode == 0
            if not has_vulkan and not os.environ.get("VULKAN_SDK"):
                missing.append("libvulkan-dev")
    if backend == "cuda" and not shutil.which("nvcc"):
        missing.append("CUDA Toolkit（nvcc）")
    if missing:
        print("缺少：" + "、".join(missing))
        print({"linux": "  " + APT_HINT, "macos": "  " + BREW_HINT}.get(P.NAME, WIN_HINT))
        sys.exit(1)
    jobs = os.cpu_count() or 4
    print(f"✔ 工具齊全（後端 {backend}，平行 {jobs} 工作）")
    return jobs


# ---------------------------------------------------------------- 原始碼
def fetch_repo(directory, repo, ref):
    directory = Path(directory)
    if directory.exists() and not (directory / ".git").is_dir():
        die(f"{directory} 存在但不是 git repo，請先改名或移走")
    if not (directory / ".git").is_dir():
        print(f"▶ 下載 {repo}")
        run(["git", "init", "-q", directory])
        run(["git", "-C", directory, "remote", "add", "origin", repo])
    head = out(["git", "-C", directory, "rev-parse", "-q", "--verify", "HEAD"])
    if ref and head.startswith(ref):
        print(f"▶ 已是鎖定版本 {ref[:9]}")
    else:
        target = ref or "HEAD"
        print(f"▶ 取得 {target[:9] if ref else '最新版'}（llama.cpp 較大，需要一點時間）")
        run(["git", "-C", directory, "fetch", "--depth", "1", "--progress", "origin", target])
        run(["git", "-C", directory, "-c", "advice.detachedHead=false",
             "checkout", "-q", "FETCH_HEAD"])
    print("  版本：" + out(["git", "-C", directory, "log", "-1", "--format=%h %cs %s"])[:80])


def build(directory, backend, args, targets, jobs, rebuild, generator=None):
    directory = Path(directory)
    stamp_file = directory / "build" / ".lec-build"
    want = build_stamp(directory, backend, args, generator)
    have = all(P.find_engine_bin(directory, t).is_file() for t in targets)
    if have and not rebuild and stamp_file.exists() and \
            stamp_file.read_text(encoding="utf-8").strip() == want:
        print("▶ 已編譯過相同版本、後端與 generator，略過（要重編請加 --rebuild）")
        return
    print("▶ 清除舊的 build")
    shutil.rmtree(directory / "build", ignore_errors=True)
    cmake_cmd = ["cmake", "-S", directory, "-B", directory / "build",
                 "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF"]
    if generator:
        cmake_cmd += ["-G", generator]
    cmake_cmd += [*BACKEND_FLAGS[backend], *args]
    run(cmake_cmd)
    for t in targets:
        print(f"▶ 編譯 {t}")
        run(["cmake", "--build", directory / "build", "--config", "Release",
             "-j", str(jobs), "--target", t])
    stamp_file.write_text(want + "\n", encoding="utf-8")


def check_bin(path, backend):
    path = Path(path)
    if not path.is_file():
        die(f"編譯後找不到 {path}")
    if P.NAME == "linux":
        deps = out(["ldd", str(path)])
        if "not found" in deps:
            print(deps)
            die(f"{path} 有找不到的函式庫")
    print(f"✔ {path.name}（後端 {backend}）")


# ---------------------------------------------------------------- 模型
def import_model(dest, name, import_dir):
    dest = Path(dest)
    if dest.is_file() or not import_dir:
        return
    for src in Path(import_dir).rglob(name):
        if src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"▶ 搬移 {src} → {dest}")
            shutil.move(str(src), str(dest))
            return


def download(url, dest):
    """用 curl 或 python 下載（支援續傳）。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("curl"):
        run(["curl", "-L", "-C", "-", "--fail", "-o", dest, url])
        return
    import urllib.request
    print(f"  下載 {url}")
    urllib.request.urlretrieve(url, dest)                       # noqa: S310


# ---------------------------------------------------------------- 主程式
def main():
    ap = argparse.ArgumentParser(description="取得並編譯 whisper.cpp / llama.cpp",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("engines", nargs="*", metavar="whisper|llama", default=[])
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "vulkan", "cuda", "metal", "cpu"])
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--lock", action="store_true")
    ap.add_argument("--import-models", metavar="DIR")
    ap.add_argument("--generator", metavar="NAME",
                    help="傳給 cmake -G（例如 Windows 上的 Ninja；預設交給 cmake 自動判斷）")
    args = ap.parse_args()

    for name in args.engines:
        if name not in ENGINES:
            die(f"未知的引擎：{name}（可用：whisper、llama）")
    names = args.engines or ["whisper", "llama"]
    backend = P.engine_backend(args.backend)
    if backend not in BACKEND_FLAGS:
        die(f"不支援的後端：{backend}")
    lock = lock_read()

    if args.lock:
        for name in names:
            e = ENGINES[name]
            if not (e["dir"] / ".git").is_dir():
                print(f"⚠ 沒有 {e['dir'].name}")
                continue
            lock_write(e["key"], e["dir"])
            if (e["dir"] / "build").is_dir():
                (e["dir"] / "build" / ".lec-build").write_text(
                    build_stamp(e["dir"], backend, e["args"], args.generator) + "\n", encoding="utf-8")
        return 0

    step("檢查編譯工具")
    jobs = check_tools(backend)

    for name in names:
        e = ENGINES[name]
        step(e["dir"].name)
        fetch_repo(e["dir"], e["repo"], None if args.update else lock.get(e["key"]))
        build(e["dir"], backend, e["args"], e["targets"], jobs, args.rebuild, args.generator)
        check_bin(P.find_engine_bin(e["dir"], e["targets"][0]), backend)
        if args.update or not lock.get(e["key"]):
            lock_write(e["key"], e["dir"])

        if name == "whisper":
            model = e["dir"] / "models" / f"ggml-{WHISPER_MODEL}.bin"
            import_model(model, model.name, args.import_models)
            if not model.is_file():
                print(f"▶ 下載 whisper 模型 {WHISPER_MODEL}（約 1.6GB）")
                download(WHISPER_MODEL_URL, model)
            print(f"✔ 模型 {model.stat().st_size / 1e9:.1f} GB {model}")
        else:
            bin_path = P.find_engine_bin(e["dir"], "llama-server")
            devs = [l.strip() for l in out([bin_path, "--list-devices"]).splitlines()
                    if l.strip().lower().startswith(("vulkan", "cuda", "metal", "rocm"))]
            print("▶ 可用裝置：" + ("；".join(devs) if devs else "（沒偵測到 GPU，會用 CPU）"))
            models = ROOT / "models"
            models.mkdir(exist_ok=True)
            for m in ("Qwen3-8B-Q4_K_M.gguf", "Qwen3-4B-Q4_K_M.gguf"):
                import_model(models / m, m, args.import_models)
            found = sorted(models.glob("*.gguf"))
            if found:
                for f in found:
                    print(f"✔ {f.stat().st_size / 1e9:.1f} GB  {f}")
            else:
                print(f"⚠ {models} 裡沒有 .gguf，請依 README 下載 Qwen3-8B-Q4_K_M.gguf")

    step("完成")
    print("下一步：" + ("python3 lec doctor" if P.IS_WINDOWS else "./lec doctor"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
