#!/usr/bin/env bash
# 課堂即時轉錄（VAD 版）
#
# 用法：
#   ./lecture.sh 作業系統                     # 麥克風即時錄音，Ctrl+C 結束
#   ./lecture.sh 作業系統 --file 錄音.mp3     # 用既有音檔處理
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=config.env
source "$SCRIPT_DIR/config.env"

COURSE="${1:-}"
if [[ -z "$COURSE" ]]; then
  echo "用法: $0 <課名> [--file 音檔]" >&2
  exit 1
fi
shift
INPUT_FILE=""
if [[ "${1:-}" == "--file" ]]; then
  INPUT_FILE="${2:?--file 需要音檔路徑}"
  [[ -f "$INPUT_FILE" ]] || { echo "找不到檔案: $INPUT_FILE" >&2; exit 1; }
fi

for cmd in ffmpeg curl python3; do
  command -v "$cmd" >/dev/null || { echo "缺少指令: $cmd" >&2; exit 1; }
done
SERVER_BIN="$WHISPER_DIR/build/bin/whisper-server"
[[ -x "$SERVER_BIN" ]] || { echo "找不到 $SERVER_BIN，請確認 whisper.cpp 已編譯" >&2; exit 1; }
[[ -f "$WHISPER_MODEL" ]] || { echo "找不到模型 $WHISPER_MODEL" >&2; exit 1; }

SESSION_DIR="$LECTURE_ROOT/$(date +%F)_${COURSE}"
if [[ -s "$SESSION_DIR/transcript.md" ]]; then
  SESSION_DIR="${SESSION_DIR}_$(date +%H%M)"   # 同一天同課名再錄，避免混在一起
fi
mkdir -p "$SESSION_DIR"
echo "▶ 輸出資料夾: $SESSION_DIR"

SERVER_URL="http://$WHISPER_HOST:$WHISPER_PORT"
SERVER_PID=""
cleanup() {
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ---- 啟動 whisper-server（已在跑就沿用）----
if curl -s -o /dev/null "$SERVER_URL/"; then
  echo "▶ whisper-server 已在執行，沿用"
else
  echo "▶ 啟動 whisper-server（載入模型中）..."
  # setsid：讓 Ctrl+C 不會直接打斷 server，等最後一段轉完才關
  setsid "$SERVER_BIN" -m "$WHISPER_MODEL" -l "$WHISPER_LANG" -t "$WHISPER_THREADS" \
    --host "$WHISPER_HOST" --port "$WHISPER_PORT" \
    >>"$SESSION_DIR/whisper-server.log" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 120); do
    curl -s -o /dev/null "$SERVER_URL/" && break
    kill -0 "$SERVER_PID" 2>/dev/null || { echo "whisper-server 啟動失敗，見 whisper-server.log" >&2; exit 1; }
    sleep 1
  done
  curl -s -o /dev/null "$SERVER_URL/" || { echo "whisper-server 120 秒內沒有回應" >&2; exit 1; }
  grep -qi "vulkan" "$SESSION_DIR/whisper-server.log" && echo "▶ Vulkan 已啟用" \
    || echo "⚠ log 中沒看到 Vulkan，可能在跑純 CPU"
fi

[[ -z "$INPUT_FILE" ]] && echo "  即時逐字稿: $SESSION_DIR/transcript.md"

# ---- 錄音 + VAD 切段 + 轉錄（前景執行，Ctrl+C 由 Python 優雅處理）----
set +e
python3 "$SCRIPT_DIR/live_transcribe.py" \
  --session "$SESSION_DIR" --course "$COURSE" --server "$SERVER_URL" \
  --source "$AUDIO_SOURCE" ${INPUT_FILE:+--file "$INPUT_FILE"} \
  --prompt "$BASE_PROMPT" \
  --min-chunk "$VAD_MIN_CHUNK" --max-chunk "$VAD_MAX_CHUNK" \
  --silence-ms "$VAD_SILENCE_MS" --sensitivity "$VAD_SENSITIVITY" \
  --section-min "$SECTION_MINUTES" --para-gap "$PARA_GAP" --para-max "$PARA_MAX" \
  --keep-recording "$KEEP_RECORDING" --opencc "$USE_OPENCC" \
  2>&1 | tee -ia "$SESSION_DIR/session.log"
set -e

echo "  $SESSION_DIR/transcript.md"
echo "  $SESSION_DIR/transcript.srt"
