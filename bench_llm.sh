#!/usr/bin/env bash
# 測試 LLM（預設 Qwen3 8B）總結課堂逐字稿的速度與品質（所有路徑都在專案目錄下）
#
# 用法：
#   ./bench_llm.sh                         # 自動找 outputs/ 中最近一份逐字稿，取第 2 個 5 分鐘段落
#   ./bench_llm.sh 逐字稿.md               # 指定逐字稿
#   ./bench_llm.sh 逐字稿.md 3             # 指定逐字稿與第幾個段落（## 小標題）
#   MODELS="Qwen3-4B-Q4_K_M.gguf Qwen3-8B-Q4_K_M.gguf" ./bench_llm.sh   # 預設只測 8B；可指定多個（空白分隔）
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LLAMA_DIR="$ROOT/llama.cpp"
MODEL_DIR="$ROOT/models"
OUTPUTS="$ROOT/outputs"
PORT="${PORT:-8179}"
read -r -a MODELS <<< "${MODELS:-Qwen3-8B-Q4_K_M.gguf}"
OUT_DIR="$OUTPUTS/_llm-bench/$(date +%Y%m%d_%H%M%S)"

SERVER_BIN="$LLAMA_DIR/build/bin/llama-server"
[[ -x "$SERVER_BIN" ]] || { echo "找不到 $SERVER_BIN，請先執行 ./setup_engines.sh llama" >&2; exit 1; }

# ---- 找逐字稿 ----
TRANSCRIPT="${1:-}"
if [[ -z "$TRANSCRIPT" ]]; then
  TRANSCRIPT=$(ls -t "$OUTPUTS"/*/transcript.md 2>/dev/null | while read -r f; do
    [[ $(grep -c '^## ' "$f") -ge 2 ]] && { echo "$f"; break; }; done || true)
  [[ -n "$TRANSCRIPT" ]] || TRANSCRIPT=$(ls -t "$OUTPUTS"/*/transcript.md 2>/dev/null | head -1 || true)
fi
[[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" ]] || { echo "找不到逐字稿（$OUTPUTS/*/transcript.md），請指定檔案" >&2; exit 1; }
SECTION="${2:-2}"
[[ $(grep -c '^## ' "$TRANSCRIPT") -ge "$SECTION" ]] || SECTION=1

mkdir -p "$OUT_DIR"
awk -v s="$SECTION" '/^## /{n++} n==s' "$TRANSCRIPT" > "$OUT_DIR/input.md"
echo "▶ 逐字稿：$TRANSCRIPT（第 $SECTION 段，$(wc -m < "$OUT_DIR/input.md") 字）"
echo "▶ 結果資料夾：$OUT_DIR"

SERVER_PID=""
cleanup() { [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT

# 已有程式佔用 port 就先提醒（例如 lec run 正在執行）
if curl -s -o /dev/null "http://127.0.0.1:$PORT/health"; then
  echo "⚠ port $PORT 已有程式在執行（lec run 或之前開的 llama-server），請先關掉再跑，或用 PORT=8180 ./bench_llm.sh" >&2
  exit 1
fi

for MODEL in "${MODELS[@]}"; do
  NAME="${MODEL%.gguf}"
  if [[ ! -f "$MODEL_DIR/$MODEL" ]]; then
    echo; echo "⚠ 略過 $NAME：找不到 $MODEL_DIR/$MODEL"
    continue
  fi

  echo; echo "================ $NAME ================"
  echo "▶ 載入模型..."
  "$SERVER_BIN" -m "$MODEL_DIR/$MODEL" -ngl 99 -c 8192 -np 1 --jinja \
    --host 127.0.0.1 --port "$PORT" > "$OUT_DIR/$NAME.server.log" 2>&1 &
  SERVER_PID=$!
  ready=0
  for _ in $(seq 1 180); do
    [[ "$(curl -s "http://127.0.0.1:$PORT/health")" == *ok* ]] && { ready=1; break; }
    kill -0 "$SERVER_PID" 2>/dev/null || { echo "llama-server 啟動失敗，見 $OUT_DIR/$NAME.server.log" >&2; exit 1; }
    sleep 1
  done
  (( ready )) || { echo "llama-server 180 秒內沒有就緒" >&2; exit 1; }

  python3 - "$OUT_DIR/input.md" "$OUT_DIR/$NAME.md" "$PORT" <<'EOF'
import json, re, shutil, subprocess, sys, time, urllib.request

inp, outp, port = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(inp, encoding="utf-8").read()

SYSTEM = (
    "你是課堂筆記助理。只根據逐字稿內容，用繁體中文（台灣用語）輸出 Markdown 條列重點，格式：\n"
    "### 主題\n一句話說明這段在講什麼\n"
    "### 重點\n- 條列關鍵內容\n"
    "### 術語\n- **術語**：簡短解釋（只列逐字稿出現的）\n"
    "### 老師強調\n- 若無則寫「無」\n\n"
    "規則：修正明顯的語音辨識錯字（例如同音字、英文術語拼錯），省略口語贅詞，"
    "不要編造逐字稿沒有的內容，不要輸出思考過程。"
)
body = {
    "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "以下是課堂逐字稿片段：\n\n" + text + "\n\n/no_think"},
    ],
    "chat_template_kwargs": {"enable_thinking": False},
    "temperature": 0.3,
    "max_tokens": 800,
}
req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                             data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})

# 暖機一次（第一次呼叫含 shader 編譯，不計時）
warm = dict(body, messages=[{"role": "user", "content": "你好 /no_think"}], max_tokens=5)
urllib.request.urlopen(urllib.request.Request(req.full_url, data=json.dumps(warm).encode(),
                       headers={"Content-Type": "application/json"})).read()

t0 = time.time()
r = json.load(urllib.request.urlopen(req))
elapsed = time.time() - t0

content = r["choices"][0]["message"]["content"]
thought = "<think>" in content or "</think>" in content
content = re.sub(r"(?s)^.*?</think>\s*", "", content).strip()
if shutil.which("opencc"):
    content = subprocess.run(["opencc", "-c", "s2twp.json"], input=content,
                             capture_output=True, text=True).stdout.replace("臺", "台")

u = r.get("usage", {})
tm = r.get("timings", {})
stats = (f"耗時 {elapsed:.1f}s｜輸入 {u.get('prompt_tokens')} tokens"
         f"（{tm.get('prompt_per_second', 0):.0f} t/s）｜輸出 {u.get('completion_tokens')} tokens"
         f"（{tm.get('predicted_per_second', 0):.1f} t/s）"
         f"｜結束原因 {r['choices'][0].get('finish_reason')}")
if thought:
    stats += "｜⚠ 仍有思考輸出"

open(outp, "w", encoding="utf-8").write(content + "\n\n---\n" + stats + "\n")
print(content)
print("\n" + stats)
EOF

  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  SERVER_PID=""
  sleep 2
done

echo; echo "✔ 完成，結果存於 $OUT_DIR"
ls -1 "$OUT_DIR"/*.md
