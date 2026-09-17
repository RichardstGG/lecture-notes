#!/usr/bin/env bash
# 課堂即時轉錄：錄音分段 → whisper-server → 帶時間戳逐字稿
#
# 用法：
#   ./lecture.sh 作業系統                     # 麥克風即時錄音，Ctrl+C 結束
#   ./lecture.sh 作業系統 --file 錄音.mp3     # 用既有音檔測試整條流程
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

for cmd in ffmpeg ffprobe curl python3; do
  command -v "$cmd" >/dev/null || { echo "缺少指令: $cmd" >&2; exit 1; }
done
SERVER_BIN="$WHISPER_DIR/build/bin/whisper-server"
[[ -x "$SERVER_BIN" ]] || { echo "找不到 $SERVER_BIN，請確認 whisper.cpp 已編譯" >&2; exit 1; }
[[ -f "$WHISPER_MODEL" ]] || { echo "找不到模型 $WHISPER_MODEL" >&2; exit 1; }

DATE="$(date +%F)"
SESSION_DIR="$LECTURE_ROOT/${DATE}_${COURSE}"
if [[ -d "$SESSION_DIR/segments" ]] && ls "$SESSION_DIR/segments"/seg_*.wav >/dev/null 2>&1; then
  SESSION_DIR="${SESSION_DIR}_$(date +%H%M)"   # 同一天同課名第二次錄，避免覆蓋
fi
mkdir -p "$SESSION_DIR/segments"
LOG="$SESSION_DIR/session.log"
echo "▶ 輸出資料夾: $SESSION_DIR"

SERVER_URL="http://$WHISPER_HOST:$WHISPER_PORT"
SERVER_PID=""
WORKER_PID=""
STARTED_SERVER=0

cleanup() {
  if [[ -n "$WORKER_PID" ]] && kill -0 "$WORKER_PID" 2>/dev/null; then
    kill "$WORKER_PID" 2>/dev/null || true
  fi
  if [[ "$STARTED_SERVER" == 1 && -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# ---- 1. 啟動 whisper-server（已在跑就沿用）----
if curl -s -o /dev/null "$SERVER_URL/"; then
  echo "▶ whisper-server 已在執行，沿用"
else
  echo "▶ 啟動 whisper-server（載入模型中）..."
  setsid "$SERVER_BIN" -m "$WHISPER_MODEL" -l "$WHISPER_LANG" -t "$WHISPER_THREADS" \
    --host "$WHISPER_HOST" --port "$WHISPER_PORT" \
    >>"$SESSION_DIR/whisper-server.log" 2>&1 &
  SERVER_PID=$!
  STARTED_SERVER=1
  for _ in $(seq 1 120); do
    curl -s -o /dev/null "$SERVER_URL/" && break
    kill -0 "$SERVER_PID" 2>/dev/null || { echo "whisper-server 啟動失敗，見 whisper-server.log" >&2; exit 1; }
    sleep 1
  done
  curl -s -o /dev/null "$SERVER_URL/" || { echo "whisper-server 120 秒內沒有回應" >&2; exit 1; }
  grep -i "vulkan" "$SESSION_DIR/whisper-server.log" | head -2 || echo "⚠ log 中沒看到 Vulkan，可能在跑純 CPU"
fi

# ---- 2. 啟動轉錄 worker（獨立 session，不吃 Ctrl+C）----
setsid python3 "$SCRIPT_DIR/transcribe_worker.py" \
  --session "$SESSION_DIR" --course "$COURSE" --server "$SERVER_URL" \
  --prompt "$BASE_PROMPT" --overlap "$OVERLAP_SECONDS" --keep "$KEEP_SEGMENTS" \
  >>"$LOG" 2>&1 &
WORKER_PID=$!

# ---- 3. 錄音並切段 ----
SEG_ARGS=(-ac 1 -ar 16000 -c:a pcm_s16le -f segment -segment_time "$SEGMENT_SECONDS" -reset_timestamps 1
          "$SESSION_DIR/segments/seg_%03d.wav")
set +e
if [[ -n "$INPUT_FILE" ]]; then
  echo "▶ 測試模式：切分 $INPUT_FILE"
  ffmpeg -hide_banner -loglevel error -i "$INPUT_FILE" "${SEG_ARGS[@]}"
else
  echo "▶ 開始錄音（來源: $AUDIO_SOURCE）。按 Ctrl+C 結束錄音。"
  echo "  即時逐字稿: $SESSION_DIR/transcript.md"
  trap 'echo; echo "▶ 停止錄音..."' INT
  ffmpeg -hide_banner -loglevel warning -stats -f pulse -i "$AUDIO_SOURCE" "${SEG_ARGS[@]}"
  trap - INT
fi
set -e
touch "$SESSION_DIR/segments/DONE"

# ---- 4. 等剩下的分段轉完 ----
echo "▶ 錄音結束，等待剩餘分段轉錄完成（進度見 $LOG）..."
tail -n 0 -f "$LOG" --pid="$WORKER_PID" 2>/dev/null || wait "$WORKER_PID"
wait "$WORKER_PID" 2>/dev/null || true
echo "✔ 完成"
echo "  $SESSION_DIR/transcript.md"
echo "  $SESSION_DIR/transcript.srt"
