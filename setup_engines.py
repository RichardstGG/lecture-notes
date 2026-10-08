#!/usr/bin/env python3
"""在專案目錄下取得並編譯 whisper.cpp / llama.cpp，版本鎖定在 engines.lock。

用法：
  python3 setup_engines.py                    依 engines.lock 的版本編譯（已編好同版本就略過）
  python3 setup_engines.py whisper|llama      只處理其中一個
  python3 setup_engines.py diarize            只取得會議發言者辨識用的兩個模型（約 35MB，驗證 SHA-256）
  python3 setup_engines.py --update           升級到最新版，編譯成功後寫回 engines.lock
  python3 setup_engines.py --rebuild          版本沒變也強制重新編譯
  python3 setup_engines.py --lock             不編譯，只把目前 checkout 的版本記進 engines.lock
  python3 setup_engines.py --backend vulkan   後端：auto（預設）/ vulkan / cuda / metal / cpu
  python3 setup_engines.py --import-models DIR  從其他位置搬入已下載的模型
  python3 setup_engines.py --generator Ninja  指定 cmake generator（Windows 預設依已安裝的 Visual Studio 自動選）
  python3 setup_engines.py --prebuilt        只適用 Windows：下載官方預編譯檔，不編譯

後端預設：Linux 與 Windows 用 Vulkan，macOS 用 Metal。
以靜態連結編譯（BUILD_SHARED_LIBS=OFF），整個專案資料夾搬到哪裡都能執行。
--prebuilt 不編譯，版本見 WIN_PREBUILT，跟 engines.lock 的原始碼 commit 不一定相同。
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
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

# 會議發言者辨識（sherpa-onnx）的兩個模型。檔名對應 docs/meeting-workbench-contract.md
# 的 [diarization] 預設路徑。GitHub release 的資產 tag 是可變的，所以雜湊一律驗證，
# 不信任「當下抓到的是什麼」。授權在 docs/diarize-install.md。
_SHERPA_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
DIARIZE_MODELS = [
    {"name": "segmentation", "license": "MIT", "size_mb": 7,
     "file": "sherpa-onnx-pyannote-segmentation-3-0.onnx",
     "url": f"{_SHERPA_RELEASES}/speaker-segmentation-models/"
            "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
     "archive_sha256": "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
     "member": "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
     "sha256": "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079",
     "license_member": "sherpa-onnx-pyannote-segmentation-3-0/LICENSE",
     "license_file": "sherpa-onnx-pyannote-segmentation-3-0.LICENSE.txt"},
    {"name": "embedding", "license": "Apache-2.0", "size_mb": 28,
     "file": "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
     "url": f"{_SHERPA_RELEASES}/speaker-recongition-models/"
            "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
     "sha256": "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2"},
]

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
            "  不想安裝編譯環境的話，執行 python setup_engines.py --prebuilt。")

# 官方 Windows 預編譯檔。這不是 engines.lock 裡的原始碼 commit：
# whisper.cpp 從 v1.9.0 之後就沒有再附 Windows 執行檔；llama.cpp 用 b11067 的
# Vulkan 版，跟本專案傳給 llama-server 的參數相容，而且執行時只要系統有 Vulkan 驅動。
# 雜湊不符就拒絕安裝，避免下載中斷或檔案被換掉還繼續用。
_WHISPER_PREBUILT = {
    "id": "whisper-bin-x64-v1.9.0",
    "backend": "cpu",
    "url": "https://github.com/ggml-org/whisper.cpp/releases/download/v1.9.0/whisper-bin-x64.zip",
    "sha256": "00c4304b6be363a224a4b69829df49009f74131df8c3ce6a5878b89a11cd26ef",
    "server": "whisper-server.exe",
    "note": "官方 Windows 版沒有 Vulkan 建置，whisper 用 CPU",
}
_LLAMA_VULKAN_PREBUILT = {
    "id": "llama-b11067-bin-win-vulkan-x64",
    "backend": "vulkan",
    "url": ("https://github.com/ggml-org/llama.cpp/releases/download/b11067/"
            "llama-b11067-bin-win-vulkan-x64.zip"),
    "sha256": "fb0761e218675372bc59eae5ef802936a2aa7edabda4a17df4f8a732adcad20d",
    "server": "llama-server.exe",
    "note": "Vulkan 版；執行時用系統的 Vulkan 驅動，不需要 Vulkan SDK",
}
WIN_PREBUILT = {"whisper": _WHISPER_PREBUILT, "llama": {"vulkan": _LLAMA_VULKAN_PREBUILT}}


def step(msg):
    print(f"\n==== {msg} ====", flush=True)


def die(msg):
    print(f"✖ {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd, hint=None, **kw):
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], **kw)
    if r.returncode != 0:
        if hint:
            print(hint, file=sys.stderr, flush=True)
        die(f"指令失敗（{r.returncode}）：{' '.join(str(c) for c in cmd)}")
    return r


def out(cmd):
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8",
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


# ---------------------------------------------------------------- Windows：Visual Studio
# MSVC 偵測（find_msvc）與「缺哪些編譯工具」的判斷都在 core/platform.py，lec doctor 共用同一份。
VS_GENERATORS = {16: "Visual Studio 16 2019", 17: "Visual Studio 17 2022",
                 18: "Visual Studio 18 2026"}
WIN_CONFIGURE_HINT = (
    "\nWindows 編譯設定失敗的常見原因：\n"
    "  1. Visual Studio 沒有安裝「使用 C++ 的桌面開發」工作負載\n"
    "  2. CMake 太舊、不認得已安裝的 Visual Studio：winget upgrade Kitware.CMake\n"
    "  3. CUDA 版：CUDA Toolkit 沒有整合進 Visual Studio（重裝 CUDA 並勾選 Visual Studio Integration）；\n"
    "     或改在「x64 Native Tools Command Prompt for VS」裡執行，並加上 --generator Ninja\n")

_UNSET = object()


def pick_generator(requested, msvc):
    """決定要傳給 cmake -G 的 generator（None = 交給 cmake）。

    - 使用者用 --generator 指定：照用。
    - 非 Windows、或 cl 已在 PATH（Developer Command Prompt）：交給 cmake。
    - 一般 PowerShell：依 vswhere 找到的 VS 版本明確指定 Visual Studio generator，
      並先確認這版 cmake 認得它；不認得就提早說清楚，而不是讓 cmake 默默退回 NMake。
    """
    if requested or P.NAME != "windows" or shutil.which("cl") or not msvc:
        return requested or None
    try:
        major = int(str(msvc["version"]).split(".")[0])
    except ValueError:
        return None
    gen = VS_GENERATORS.get(major)
    if not gen:   # 比這份對照表更新的 VS：交給 cmake 自己判斷，失敗時 WIN_CONFIGURE_HINT 會說明
        print(f"⚠ 不認得 Visual Studio {msvc['version']}，交給 cmake 自動選擇 generator")
        return None
    if gen not in out(["cmake", "--help"]):
        ver = out(["cmake", "--version"]).splitlines()[:1]
        die(f"找到 {gen}，但目前的 {ver[0] if ver else 'cmake'} 不支援它，請升級 CMake"
            "（winget upgrade Kitware.CMake），或在「x64 Native Tools Command Prompt for VS」裡執行並加 --generator Ninja")
    print(f"✔ Visual Studio {msvc['version']}（{gen}）")
    return gen


# ---------------------------------------------------------------- 前置檢查
def check_tools(backend, msvc=_UNSET):
    missing = ["git"] if not shutil.which("git") else []
    if not shutil.which("cmake"):
        missing.append("cmake")
    missing += P.missing_build_tools(backend, msvc="probe" if msvc is _UNSET else msvc)
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


def build(directory, backend, args, targets, jobs, rebuild, generator=None, cmake_generator=None):
    """generator：使用者 --generator 指定的值（記進 build stamp）；
    cmake_generator：實際傳給 cmake -G 的值（Windows 可能是自動選的 VS generator），
    沒給就用 generator。自動選的不記進 stamp，所以 --lock 與實際編譯的 stamp 一致。"""
    directory = Path(directory)
    cmake_generator = cmake_generator or generator
    stamp_file = P.build_stamp_path(directory)
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
    if cmake_generator:
        cmake_cmd += ["-G", cmake_generator]
    cmake_cmd += [*BACKEND_FLAGS[backend], *args]
    run(cmake_cmd, hint=WIN_CONFIGURE_HINT if P.NAME == "windows" else None)
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


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _verified_or_die(path, expected, what):
    got = sha256_file(path)
    if got != expected:
        try:
            Path(path).unlink()
        except OSError:
            pass
        die(f"{what} 的 SHA-256 不符（預期 {expected[:12]}…，實際 {got[:12]}…），"
            "已刪除下載的檔案。可能是下載中斷或來源被更換；請重跑一次，"
            "仍然失敗就不要使用這個來源。")


def _extract_member(archive, member, dest):
    """從 tar 取出「指定的一個成員」寫到 dest。成員用完整名稱精確比對，不信任任何路徑。"""
    with tarfile.open(archive, "r:*") as tar:
        try:
            info = tar.getmember(member)
        except KeyError:
            die(f"壓縮檔裡找不到 {member}，來源的檔案結構可能變了")
        if not info.isfile():
            die(f"壓縮檔裡的 {member} 不是一般檔案")
        src = tar.extractfile(info)
        with open(dest, "wb") as f:
            shutil.copyfileobj(src, f)


def fetch_diarization_models(models_dir=None, downloader=None):
    """取得會議發言者辨識的兩個模型到 models/，全部驗證 SHA-256。

    - 已存在且雜湊相符：略過。
    - 已存在但雜湊不符：**拒絕覆蓋**，要使用者自己決定（可能是自行替換的模型）。
    - 不存在：下載 → 驗證 → 原子改名。下載或驗證失敗不會留下半成品。
    """
    models_dir = Path(models_dir or ROOT / "models")
    downloader = downloader or download
    models_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for m in DIARIZE_MODELS:
        dest = models_dir / m["file"]
        if dest.is_file():
            if sha256_file(dest) != m["sha256"]:
                die(f"{dest} 已存在但 SHA-256 與預期不符，不覆蓋。"
                    f"若是自行替換的模型就忽略這個訊息；否則請刪除後重跑。")
            print(f"✔ 已有 {m['name']} 模型 {dest.name}")
            paths.append(dest)
            continue
        print(f"▶ 下載發言者辨識的 {m['name']} 模型（約 {m['size_mb']}MB，{m['license']}）")
        part = models_dir / f".{m['file']}.part"
        try:
            downloader(m["url"], part)
            if "archive_sha256" in m:
                _verified_or_die(part, m["archive_sha256"], f"{m['name']} 壓縮檔")
                tmp = models_dir / f".{m['file']}.tmp"
                _extract_member(part, m["member"], tmp)
                _verified_or_die(tmp, m["sha256"], f"{m['name']} 模型")
                if m.get("license_member"):
                    _extract_member(part, m["license_member"],
                                    models_dir / m["license_file"])
                os.replace(tmp, dest)
            else:
                _verified_or_die(part, m["sha256"], f"{m['name']} 模型")
                os.replace(part, dest)
        finally:
            for leftover in (part, models_dir / f".{m['file']}.tmp"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
        print(f"✔ {m['name']} 模型 {dest.stat().st_size / 1e6:.1f} MB {dest}")
        paths.append(dest)
    return paths


def ensure_whisper_model(directory, import_dir):
    model = Path(directory) / "models" / f"ggml-{WHISPER_MODEL}.bin"
    import_model(model, model.name, import_dir)
    if not model.is_file():
        print(f"▶ 下載 whisper 模型 {WHISPER_MODEL}（約 1.6GB）")
        download(WHISPER_MODEL_URL, model)
    print(f"✔ 模型 {model.stat().st_size / 1e9:.1f} GB {model}")


def report_llama_runtime(binary, import_dir):
    devs = [line.strip() for line in out([binary, "--list-devices"]).splitlines()
            if line.strip().lower().startswith(("vulkan", "cuda", "metal", "rocm"))]
    print("▶ 可用裝置：" + ("；".join(devs) if devs else "（沒偵測到 GPU，會用 CPU）"))
    models = ROOT / "models"
    models.mkdir(exist_ok=True)
    for name in ("Qwen3-8B-Q4_K_M.gguf", "Qwen3-4B-Q4_K_M.gguf"):
        import_model(models / name, name, import_dir)
    found = sorted(models.glob("*.gguf"))
    if found:
        for path in found:
            print(f"✔ {path.stat().st_size / 1e9:.1f} GB  {path}")
    else:
        print(f"⚠ {models} 裡沒有 .gguf，請依 docs/<平台>_setup.md 的步驟下載 Qwen3-8B-Q4_K_M.gguf")


# ---------------------------------------------------------------- Windows 預編譯檔
def prebuilt_spec(engine, backend):
    """這個引擎在 --prebuilt 時要下載的那一包。whisper 一律是官方 CPU zip。"""
    if engine == "whisper":
        return WIN_PREBUILT["whisper"]
    if engine != "llama":
        die(f"沒有 {engine} 的 Windows 預編譯檔")
    spec = WIN_PREBUILT["llama"].get(backend)
    if not spec:
        die(f"Windows 預編譯檔沒有 {backend} 版的 llama.cpp（目前只有 vulkan）。"
            "CUDA 或 CPU 版請看 docs/windows_setup.md「預編譯檔」自己放進 llama.cpp\\build\\bin，"
            "或安裝編譯環境後不要加 --prebuilt。")
    return spec


def prebuilt_stamp(spec):
    return f"PREBUILT={spec['id']} BACKEND={spec['backend']}"


def _zip_member_name(filename):
    """只取 zip 成員的檔名。含 ..、絕對路徑或磁碟機代號就拒絕。"""
    raw = filename.replace("\\", "/")
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        die(f"壓縮檔含有不安全的路徑：{filename}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        die(f"壓縮檔含有不安全的路徑：{filename}")
    return parts[-1]


def extract_windows_binaries(archive, dest):
    """把 zip 裡的 .exe / .dll 平放到 dest。其他檔不取，路徑也不保留。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    placed = {}
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            name = _zip_member_name(info.filename)
            if Path(name).suffix.lower() not in {".exe", ".dll"}:
                continue
            if name in placed:
                die(f"壓縮檔裡有兩個 {name}，無法決定要放哪一個")
            target = dest / name
            with bundle.open(info, "r") as src, target.open("wb") as out_file:
                shutil.copyfileobj(src, out_file)
            placed[name] = target
    if not placed:
        die(f"{archive} 裡沒有 .exe 或 .dll")
    return placed


def download_verified(url, dest, expected, what, downloader=None):
    """下載到 dest.part，雜湊相符才改名。失敗時留下 .part，方便 curl 續傳。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    (downloader or download)(url, part)
    _verified_or_die(part, expected, what)
    os.replace(part, dest)
    return dest


def _clear_imported_binaries(dest):
    dest = Path(dest)
    if not dest.is_dir():
        return
    for path in dest.iterdir():
        if path.is_file() and path.suffix.lower() in {".exe", ".dll"}:
            path.unlink()


def binary_starts(path):
    """執行檔能不能啟動。缺 DLL 時 Windows 常常沒有 stdout，而且結束碼不是 0。"""
    try:
        result = subprocess.run(
            [str(path), "--help"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, env=P.env_with_libs(path))
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode == 0:
        return True
    return bool(result.stdout or result.stderr)


def install_prebuilt_archive(spec, dest_dir, downloader=None):
    """下載、驗證、解出 spec 描述的那一包，回傳 server 執行檔路徑。"""
    dest_dir = Path(dest_dir)
    archive = dest_dir.parent / f"{spec['id']}.zip"
    print(f"▶ 下載 {spec['id']}")
    download_verified(spec["url"], archive, spec["sha256"], spec["id"], downloader)
    _clear_imported_binaries(dest_dir)
    placed = extract_windows_binaries(archive, dest_dir)
    try:
        archive.unlink()
    except OSError:
        pass
    server = dest_dir / spec["server"]
    if not server.is_file():
        found = ", ".join(sorted(name for name in placed if name.lower().endswith(".exe"))) or "（沒有）"
        die(f"預編譯檔裡沒有 {spec['server']}。找到的執行檔：{found}")
    return server


def install_prebuilt(names, backend, import_dir=None):
    """Windows：放置官方預編譯檔，不下載原始碼、也不呼叫 cmake。"""
    if P.NAME != "windows":
        die("--prebuilt 只適用於 Windows。這個平台請用原始碼編譯。")
    if backend != "vulkan":
        die("--prebuilt 目前只支援 --backend vulkan（Windows 預設）。"
            "whisper 會用官方 CPU 版；llama.cpp 用 Vulkan 版。"
            "要 CPU 或 CUDA 版請看 docs/windows_setup.md「預編譯檔」自己放檔，"
            "或安裝編譯環境後不要加 --prebuilt。")
    specs = [(name, prebuilt_spec(name, backend)) for name in names]
    for name, spec in specs:
        engine = ENGINES[name]
        step(f"{engine['dir'].name} 預編譯檔")
        print(f"  {spec['note']}")
        dest = engine["dir"] / "build" / "bin"
        stamp_path = P.build_stamp_path(engine["dir"])
        server = dest / spec["server"]
        want = prebuilt_stamp(spec)
        try:
            have = stamp_path.read_text(encoding="utf-8").strip()
        except OSError:
            have = ""
        if server.is_file() and have == want and binary_starts(server):
            print(f"▶ 已有相同的預編譯檔 {server.name}，略過"
                  f"（要重抓請刪除 {engine['dir'].name}\\build）")
        else:
            server = install_prebuilt_archive(spec, dest)
            if not binary_starts(server):
                die(f"{server.name} 無法啟動。檔案在 {dest}。"
                    "請確認已安裝 Visual C++ 可轉散發套件，Vulkan 版還需要顯示卡驅動。")
            stamp_path.parent.mkdir(parents=True, exist_ok=True)
            stamp_path.write_text(want + "\n", encoding="utf-8")
            print(f"✔ {server}（預編譯 {spec['backend']}）")
        if name == "whisper":
            ensure_whisper_model(engine["dir"], import_dir)
        else:
            report_llama_runtime(server, import_dir)
    step("完成")
    print("下一步：python lec doctor")
    return 0


# ---------------------------------------------------------------- 主程式
def main(argv=None):
    ap = argparse.ArgumentParser(description="取得並編譯 whisper.cpp / llama.cpp",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("engines", nargs="*", metavar="whisper|llama|diarize", default=[])
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "vulkan", "cuda", "metal", "cpu"])
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--lock", action="store_true")
    ap.add_argument("--import-models", metavar="DIR")
    ap.add_argument("--generator", metavar="NAME",
                    help="傳給 cmake -G（例如 Ninja）。預設：Windows 依 vswhere 找到的 Visual Studio 自動指定，其他平台交給 cmake")
    ap.add_argument("--prebuilt", action="store_true",
                    help="Windows：下載官方預編譯檔到 build/bin，不編譯（沒有 Visual Studio / Vulkan SDK 時用）")
    args = ap.parse_args(argv)

    want_diarize = "diarize" in args.engines
    engine_args = [n for n in args.engines if n != "diarize"]
    for name in engine_args:
        if name not in ENGINES:
            die(f"未知的目標：{name}（可用：whisper、llama、diarize）")
    if want_diarize and not engine_args:
        # 只要模型：不需要編譯工具鏈，也不必動引擎
        step("發言者辨識模型")
        fetch_diarization_models()
        step("完成")
        return 0
    names = engine_args or ["whisper", "llama"]
    backend = P.engine_backend(args.backend)
    if backend not in BACKEND_FLAGS:
        die(f"不支援的後端：{backend}")
    lock = lock_read()

    if args.prebuilt:
        if args.update or args.lock or args.rebuild:
            die("--prebuilt 不能跟 --update、--lock 或 --rebuild 一起用")
        if P.NAME != "windows":
            die("--prebuilt 只適用於 Windows。這個平台請用原始碼編譯。")
        if backend != "vulkan":
            die("--prebuilt 目前只支援 --backend vulkan（Windows 預設）。"
                "whisper 會用官方 CPU 版；llama.cpp 用 Vulkan 版。"
                "要 CPU 或 CUDA 版請看 docs/windows_setup.md「預編譯檔」自己放檔，"
                "或安裝編譯環境後不要加 --prebuilt。")
        code = install_prebuilt(names, backend, args.import_models)
        if want_diarize:
            step("發言者辨識模型")
            fetch_diarization_models()
        return code

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
    msvc = P.find_msvc() if P.NAME == "windows" else None
    jobs = check_tools(backend, msvc)
    cmake_generator = pick_generator(args.generator, msvc)

    for name in names:
        e = ENGINES[name]
        step(e["dir"].name)
        fetch_repo(e["dir"], e["repo"], None if args.update else lock.get(e["key"]))
        build(e["dir"], backend, e["args"], e["targets"], jobs, args.rebuild, args.generator,
              cmake_generator)
        check_bin(P.find_engine_bin(e["dir"], e["targets"][0]), backend)
        if args.update or not lock.get(e["key"]):
            lock_write(e["key"], e["dir"])

        if name == "whisper":
            ensure_whisper_model(e["dir"], args.import_models)
        else:
            report_llama_runtime(P.find_engine_bin(e["dir"], "llama-server"), args.import_models)

    if want_diarize:
        step("發言者辨識模型")
        fetch_diarization_models()

    step("完成")
    print("下一步：" + ("python3 lec doctor" if P.IS_WINDOWS else "./lec doctor"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
