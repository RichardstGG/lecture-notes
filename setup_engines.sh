#!/usr/bin/env bash
# 在專案目錄下取得並編譯 whisper.cpp / llama.cpp（Vulkan），版本鎖定在 engines.lock
#
# 用法：
#   ./setup_engines.sh                  # 依 engines.lock 的版本取得並編譯（已編好同版本就略過）
#   ./setup_engines.sh whisper|llama    # 只處理其中一個
#   ./setup_engines.sh --update         # 升級到最新版，編譯成功後寫回 engines.lock
#   ./setup_engines.sh --rebuild        # 版本沒變也強制重新編譯
#   ./setup_engines.sh --lock           # 不編譯，只把目前 checkout 的版本記進 engines.lock
#   ./setup_engines.sh --import-models ~/舊資料夾   # 從其他位置搬入已下載的模型
#   VULKAN=OFF ./setup_engines.sh       # 編純 CPU 版
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
LOCK_FILE="$ROOT/engines.lock"
VULKAN="${VULKAN:-ON}"
JOBS="${JOBS:-$(nproc)}"
WHISPER_ARGS=(-DWHISPER_BUILD_TESTS=OFF -DWHISPER_SDL2=OFF)
WHISPER_TARGETS=(whisper-server whisper-cli)
LLAMA_ARGS=(-DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF)
LLAMA_TARGETS=(llama-server llama-bench)

DO_WHISPER=1; DO_LLAMA=1; UPDATE=0; REBUILD=0; LOCK_ONLY=0; IMPORT_DIR=""
while (( $# )); do
  case "$1" in
    whisper) DO_LLAMA=0 ;;
    llama)   DO_WHISPER=0 ;;
    --update)  UPDATE=1 ;;
    --rebuild) REBUILD=1 ;;
    --lock)    LOCK_ONLY=1 ;;
    --import-models) IMPORT_DIR="${2:?--import-models 需要資料夾}"; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "未知參數：$1（見 --help）" >&2; exit 1 ;;
  esac
  shift
done

step() { echo; echo "==== $* ===="; }
die()  { echo "✖ $*" >&2; exit 1; }

# ---------------------------------------------------------------- engines.lock
lock_get() {   # $1 = KEY
  [[ -f "$LOCK_FILE" ]] || return 0
  sed -n "s/^$1=\([0-9a-f]\{7,40\}\).*/\1/p" "$LOCK_FILE" | head -1
}
lock_set() {   # $1 = KEY，$2 = 目錄
  local key="$1" dir="$2" sha desc
  sha="$(git -C "$dir" rev-parse HEAD)"
  desc="$(git -C "$dir" log -1 --format='%cs %s' | cut -c1-70)"
  [[ -f "$LOCK_FILE" ]] || printf '%s\n' \
    "# 已測試的 whisper.cpp / llama.cpp 版本；./setup_engines.sh 會 checkout 這些 commit" \
    "# 升級：./setup_engines.sh --update（編譯成功後自動更新本檔）" > "$LOCK_FILE"
  local tmp; tmp="$(mktemp)"
  grep -v "^$key=" "$LOCK_FILE" | grep -v "^# $key: " > "$tmp" || true
  printf '# %s: %s\n%s=%s\n' "$key" "$desc" "$key" "$sha" >> "$tmp"
  mv "$tmp" "$LOCK_FILE"
  echo "▶ engines.lock：$key=${sha:0:9}（$desc）"
}

build_stamp() {   # $1 = 目錄，其餘 = cmake 參數
  local dir="$1"; shift
  echo "$(git -C "$dir" rev-parse HEAD) VULKAN=$VULKAN $*"
}

if (( LOCK_ONLY )); then
  # 把「目前 checkout 且已編好」的版本記為已測試，之後不會因為缺少編譯紀錄而重編
  for pair in "whisper.cpp WHISPER_REF $DO_WHISPER" "llama.cpp LLAMA_REF $DO_LLAMA"; do
    read -r dir key on <<< "$pair"
    (( on )) || continue
    if [[ ! -d "$dir/.git" ]]; then echo "⚠ 沒有 $dir"; continue; fi
    lock_set "$key" "$ROOT/$dir"
    if [[ -d "$dir/build/bin" ]]; then
      if [[ "$dir" == whisper.cpp ]]; then build_stamp "$ROOT/$dir" "${WHISPER_ARGS[@]}"
      else build_stamp "$ROOT/$dir" "${LLAMA_ARGS[@]}"; fi > "$dir/build/.lec-build"
    fi
  done
  exit 0
fi

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
  echo "Debian / Ubuntu 請執行："
  echo "  sudo apt install git cmake build-essential pkg-config curl libvulkan-dev glslc vulkan-tools"
  exit 1
fi
echo "✔ 工具齊全（Vulkan=$VULKAN，平行 $JOBS 工作）"

# 取得原始碼並切到指定版本：$1 = 目錄，$2 = repo，$3 = commit（空 = 最新）
fetch_repo() {
  local dir="$1" repo="$2" ref="$3"
  if [[ -e "$dir" && ! -d "$dir/.git" ]]; then
    die "$dir 存在但不是 git repo，請先改名或移走後再執行"
  fi
  if [[ ! -d "$dir/.git" ]]; then
    echo "▶ 下載 $repo"
    git init -q "$dir"
    git -C "$dir" remote add origin "$repo"
  fi
  local head; head="$(git -C "$dir" rev-parse -q --verify HEAD 2>/dev/null || true)"
  if [[ -n "$ref" && "$head" == "$ref"* ]]; then
    echo "▶ 已是鎖定版本 ${ref:0:9}"
  elif [[ -n "$ref" ]]; then
    echo "▶ 切換到鎖定版本 ${ref:0:9}"
    git -C "$dir" fetch -q --depth 1 origin "$ref"
    git -C "$dir" -c advice.detachedHead=false checkout -q FETCH_HEAD
  else
    echo "▶ 取得最新版"
    git -C "$dir" fetch -q --depth 1 origin HEAD
    git -C "$dir" -c advice.detachedHead=false checkout -q FETCH_HEAD
  fi
  echo "  版本：$(git -C "$dir" log -1 --format='%h %cs %s' | cut -c1-80)"
}

# 編譯：$1 = 目錄，其餘 = cmake 參數，-- 之後是 target
build_repo() {
  local dir="$1"; shift
  local args=() targets=()
  while (( $# )); do [[ "$1" == "--" ]] && { shift; targets=("$@"); break; }; args+=("$1"); shift; done
  local stamp="$dir/build/.lec-build" want
  want="$(build_stamp "$dir" "${args[@]}")"
  local have_all=1 t
  for t in "${targets[@]}"; do [[ -x "$dir/build/bin/$t" ]] || have_all=0; done
  if (( ! REBUILD && have_all )) && [[ -f "$stamp" && "$(cat "$stamp")" == "$want" ]]; then
    echo "▶ 已編譯過相同版本，略過（要重編請加 --rebuild）"
    return
  fi
  echo "▶ 清除舊的 build"
  rm -rf "$dir/build"
  cmake -S "$dir" -B "$dir/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    -DGGML_VULKAN="$VULKAN" "${args[@]}"
  for t in "${targets[@]}"; do
    echo "▶ 編譯 $t"
    cmake --build "$dir/build" --config Release -j "$JOBS" --target "$t"
  done
  echo "$want" > "$stamp"
}

check_bin() {
  local bin="$1"
  [[ -x "$bin" ]] || die "編譯後找不到 $bin"
  if ldd "$bin" | grep -q "not found"; then
    ldd "$bin" | grep "not found"
    die "$bin 有找不到的函式庫"
  fi
  if ldd "$bin" | grep -q libvulkan; then echo "✔ $(basename "$bin")（已連結 Vulkan）"
  else echo "✔ $(basename "$bin")（純 CPU）"; fi
}

# 從 --import-models 指定的資料夾找模型搬進來：$1 = 目的地，$2 = 檔名
import_model() {
  local dest="$1" name="$2" src
  [[ -f "$dest" || -z "$IMPORT_DIR" ]] && return 0
  src="$(find "$IMPORT_DIR" -maxdepth 4 -type f -name "$name" 2>/dev/null | head -1)"
  [[ -n "$src" ]] || return 0
  mkdir -p "$(dirname "$dest")"
  echo "▶ 搬移 $src → $dest"
  mv -n "$src" "$dest"
}

pick_ref() {   # $1 = KEY
  (( UPDATE )) && return 0
  lock_get "$1"
}

# ---------------------------------------------------------------- whisper.cpp
if (( DO_WHISPER )); then
  step "whisper.cpp"
  fetch_repo "$ROOT/whisper.cpp" "$WHISPER_REPO" "$(pick_ref WHISPER_REF)"
  build_repo "$ROOT/whisper.cpp" "${WHISPER_ARGS[@]}" -- "${WHISPER_TARGETS[@]}"
  check_bin "$ROOT/whisper.cpp/build/bin/whisper-server"
  [[ -z "$(lock_get WHISPER_REF)" || $UPDATE -eq 1 ]] && lock_set WHISPER_REF "$ROOT/whisper.cpp"

  model="$ROOT/whisper.cpp/models/ggml-$WHISPER_MODEL.bin"
  import_model "$model" "ggml-$WHISPER_MODEL.bin"
  if [[ ! -f "$model" ]]; then
    echo "▶ 下載 whisper 模型 $WHISPER_MODEL（約 1.6GB）"
    bash "$ROOT/whisper.cpp/models/download-ggml-model.sh" "$WHISPER_MODEL" "$ROOT/whisper.cpp/models"
  fi
  [[ -f "$model" ]] && echo "✔ 模型 $(du -h "$model" | cut -f1) $model"
fi

# ---------------------------------------------------------------- llama.cpp
if (( DO_LLAMA )); then
  step "llama.cpp"
  fetch_repo "$ROOT/llama.cpp" "$LLAMA_REPO" "$(pick_ref LLAMA_REF)"
  build_repo "$ROOT/llama.cpp" "${LLAMA_ARGS[@]}" -- "${LLAMA_TARGETS[@]}"
  check_bin "$ROOT/llama.cpp/build/bin/llama-server"
  [[ -z "$(lock_get LLAMA_REF)" || $UPDATE -eq 1 ]] && lock_set LLAMA_REF "$ROOT/llama.cpp"
  if [[ "$VULKAN" == "ON" ]]; then
    echo "▶ 可用裝置："
    "$ROOT/llama.cpp/build/bin/llama-server" --list-devices 2>&1 | grep -iE "vulkan|device|intel|amd|nvidia" | head -5 || true
  fi

  step "LLM 模型"
  mkdir -p "$ROOT/models"
  for m in Qwen3-8B-Q4_K_M.gguf Qwen3-4B-Q4_K_M.gguf; do import_model "$ROOT/models/$m" "$m"; done
  if compgen -G "$ROOT/models/*.gguf" >/dev/null; then
    ls -lh "$ROOT"/models/*.gguf | awk '{print "✔ " $5 "  " $NF}'
  else
    echo "⚠ $ROOT/models 裡沒有 .gguf，請下載 Qwen3-8B-Q4_K_M.gguf 放進去"
  fi
fi

step "完成"
echo "下一步：./lec doctor"
