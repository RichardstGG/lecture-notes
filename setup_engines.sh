#!/usr/bin/env bash
# 在專案目錄下取得並編譯 whisper.cpp / llama.cpp（Vulkan），並把模型集中到專案目錄
#
# 用法：
#   ./setup_engines.sh              # whisper + llama 都處理
#   ./setup_engines.sh whisper      # 只處理 whisper.cpp
#   ./setup_engines.sh llama        # 只處理 llama.cpp
#   ./setup_engines.sh --update     # 先 git pull 到最新版再重新編譯
#
# 結果：
#   ./whisper.cpp/build/bin/whisper-server、./whisper.cpp/models/ggml-large-v3-turbo.bin
#   ./llama.cpp/build/bin/llama-server、llama-bench
#   ./models/*.gguf
# 以靜態連結編譯（BUILD_SHARED_LIBS=OFF），整個專案資料夾搬到哪裡都能執行。
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$ROOT"

WHISPER_REPO="https://github.com/ggml-org/whisper.cpp.git"
LLAMA_REPO="https://github.com/ggml-org/llama.cpp.git"
WHISPER_MODEL="large-v3-turbo"
VULKAN="${VULKAN:-ON}"                 # VULKAN=OFF ./setup_engines.sh 可編純 CPU 版
JOBS="${JOBS:-$(nproc)}"
OLD_HOME_DIRS=("$HOME/whisper.cpp" "$HOME/llama.cpp" "$HOME/models")   # 舊安裝位置（只搬模型，不刪）

DO_WHISPER=1; DO_LLAMA=1; UPDATE=0
for a in "$@"; do
  case "$a" in
    whisper) DO_LLAMA=0 ;;
    llama)   DO_WHISPER=0 ;;
    --update) UPDATE=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "未知參數：$a" >&2; exit 1 ;;
  esac
done

step() { echo; echo "==== $* ===="; }
die()  { echo "✖ $*" >&2; exit 1; }

# ---------------------------------------------------------------- 前置檢查
step "檢查編譯工具"
missing=()
for c in git cmake make gcc g++ pkg-config curl; do command -v "$c" >/dev/null || missing+=("$c"); done
if [[ "$VULKAN" == "ON" ]]; then
  command -v glslc >/dev/null || missing+=("glslc")
  pkg-config --exists vulkan 2>/dev/null || missing+=("libvulkan-dev")
fi
if (( ${#missing[@]} )); then
  echo "缺少：${missing[*]}"
  echo "Debian 13 請執行："
  echo "  sudo apt install git cmake build-essential pkg-config curl libvulkan-dev glslc vulkan-tools"
  exit 1
fi
echo "✔ 工具齊全（Vulkan=$VULKAN，平行 $JOBS 工作）"

# 取得或更新原始碼：$1 = 目錄，$2 = repo
fetch_repo() {
  local dir="$1" repo="$2"
  if [[ -d "$dir/.git" ]]; then
    echo "▶ $dir 已存在"
    if (( UPDATE )); then
      git -C "$dir" pull --ff-only
    fi
    echo "  版本：$(git -C "$dir" log -1 --format='%h %cs %s' | cut -c1-80)"
  elif [[ -e "$dir" ]]; then
    die "$dir 存在但不是 git repo，請先改名或移走後再執行"
  else
    echo "▶ 下載 $repo"
    git clone --depth 1 "$repo" "$dir"
  fi
}

# 編譯：$1 = 目錄，其餘 = cmake 參數，最後用 -- 分隔 target
build_repo() {
  local dir="$1"; shift
  local args=() targets=()
  while (( $# )); do [[ "$1" == "--" ]] && { shift; targets=("$@"); break; }; args+=("$1"); shift; done
  echo "▶ 清除舊的 build（避免沿用搬家前的 CMake 快取）"
  rm -rf "$dir/build"
  cmake -S "$dir" -B "$dir/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    -DGGML_VULKAN="$VULKAN" "${args[@]}"
  local t
  for t in "${targets[@]}"; do
    echo "▶ 編譯 $t"
    cmake --build "$dir/build" --config Release -j "$JOBS" --target "$t"
  done
}

# 確認執行檔沒有依賴找不到的函式庫，並顯示有沒有連到 Vulkan
check_bin() {
  local bin="$1"
  [[ -x "$bin" ]] || die "編譯後找不到 $bin"
  if ldd "$bin" | grep -q "not found"; then
    ldd "$bin" | grep "not found"
    die "$bin 有找不到的函式庫"
  fi
  if ldd "$bin" | grep -q libvulkan; then
    echo "✔ $(basename "$bin")（已連結 Vulkan）"
  else
    echo "✔ $(basename "$bin")（純 CPU）"
  fi
}

# 把舊位置的檔案搬進來（同一個檔案系統上 mv 是瞬間完成）
adopt_file() {
  local dest="$1"; shift
  [[ -f "$dest" ]] && return 0
  local src
  for src in "$@"; do
    if [[ -f "$src" ]]; then
      mkdir -p "$(dirname "$dest")"
      echo "▶ 搬移 $src → $dest"
      mv -n "$src" "$dest"
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------- whisper.cpp
if (( DO_WHISPER )); then
  step "whisper.cpp"
  fetch_repo "$ROOT/whisper.cpp" "$WHISPER_REPO"
  build_repo "$ROOT/whisper.cpp" -DWHISPER_BUILD_TESTS=OFF -DWHISPER_SDL2=OFF -- whisper-server whisper-cli
  check_bin "$ROOT/whisper.cpp/build/bin/whisper-server"

  model="$ROOT/whisper.cpp/models/ggml-$WHISPER_MODEL.bin"
  if ! adopt_file "$model" "$HOME/whisper.cpp/models/ggml-$WHISPER_MODEL.bin"; then
    echo "▶ 下載 whisper 模型 $WHISPER_MODEL（約 1.6GB）"
    bash "$ROOT/whisper.cpp/models/download-ggml-model.sh" "$WHISPER_MODEL" "$ROOT/whisper.cpp/models"
  fi
  [[ -f "$model" ]] && echo "✔ 模型 $(du -h "$model" | cut -f1) $model"
fi

# ---------------------------------------------------------------- llama.cpp
if (( DO_LLAMA )); then
  step "llama.cpp"
  fetch_repo "$ROOT/llama.cpp" "$LLAMA_REPO"
  build_repo "$ROOT/llama.cpp" -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
    -- llama-server llama-bench
  check_bin "$ROOT/llama.cpp/build/bin/llama-server"
  if [[ "$VULKAN" == "ON" ]]; then
    echo "▶ 可用裝置："
    "$ROOT/llama.cpp/build/bin/llama-server" --list-devices 2>&1 | grep -iE "vulkan|device|intel" | head -5 || true
  fi

  step "LLM 模型"
  mkdir -p "$ROOT/models"
  shopt -s nullglob
  for f in "$HOME"/models/*.gguf; do
    adopt_file "$ROOT/models/$(basename "$f")" "$f" || true
  done
  shopt -u nullglob
  ls -lh "$ROOT"/models/*.gguf 2>/dev/null | awk '{print "✔ " $5 "  " $NF}' \
    || echo "⚠ $ROOT/models 裡沒有 .gguf，請把 Qwen3-8B-Q4_K_M.gguf / Qwen3-4B-Q4_K_M.gguf 放進去"
fi

# ---------------------------------------------------------------- 收尾
step "完成"
for d in "${OLD_HOME_DIRS[@]}"; do
  [[ -d "$d" ]] && echo "舊資料夾 $d 仍在；確認 lec 正常後可自行刪除"
done
echo "下一步：./lec config UNIXops | grep -A6 '\[paths\]'  然後  ./lec run UNIXops --file <錄音>"
