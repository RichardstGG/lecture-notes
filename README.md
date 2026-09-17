# 課堂筆記系統（lec）

課堂錄音 → whisper.cpp 即時逐字稿 → llama.cpp 每 5 分鐘總結 → Obsidian Markdown。

## 安裝與目錄結構

所有東西都放在 app 根目錄（`~/lecture-notes`）底下，設定裡的相對路徑都以這裡為基準：

```
~/lecture-notes/
├─ lec, core/, config/, courses/, prompts/     程式與設定（進 git）
├─ whisper.cpp/   build/bin/whisper-server、models/ggml-large-v3-turbo.bin
├─ llama.cpp/     build/bin/llama-server
├─ models/        Qwen3-8B-Q4_K_M.gguf、Qwen3-4B-Q4_K_M.gguf
└─ outputs/       <課名>_<YYYYMMDD>/        每堂課的輸出
```

`whisper.cpp/`、`llama.cpp/`、`models/`、`outputs/` 已列在 `.gitignore`。

```bash
chmod +x ~/lecture-notes/lec
ln -s ~/lecture-notes/lec ~/.local/bin/lec     # 之後任何地方都能打 lec
sudo apt install opencc                        # 選用：台灣繁體用語轉換
```

需要：Python 3.11+、ffmpeg、curl。

**編譯 whisper.cpp / llama.cpp（在專案目錄下）**

```bash
sudo apt install git cmake build-essential pkg-config curl libvulkan-dev glslc vulkan-tools
./setup_engines.sh              # clone（已存在就沿用）→ 清掉 build → Vulkan 靜態編譯 → 集中模型
./setup_engines.sh --update     # 之後要更新版本：git pull 再重新編譯
./setup_engines.sh llama        # 只重編其中一個（whisper / llama）
```

- 以 `BUILD_SHARED_LIBS=OFF` 靜態連結，整個專案資料夾搬到哪裡都能執行。
- whisper 模型：優先從 `~/whisper.cpp/models/` 搬過來，沒有才下載；LLM：把 `~/models/*.gguf` 搬進 `./models/`。舊資料夾不會被刪。
- 基準測試：`./bench_llm.sh [逐字稿.md] [第幾段]`，結果在 `outputs/_llm-bench/`。
舊的 `lecture.sh`、`live_transcribe.py`、`config.env` 已被取代，可以刪除（`bench_llm.sh` 保留）。

`--file` 可用任何 ffmpeg 能解碼的檔案：mp3、m4a/aac、wav、flac、ogg/opus、wma、webm，以及 mp4/mkv/mov 等影片（自動取音軌）。

## 指令

| 指令 | 說明 |
|---|---|
| `lec run 課名` | 上課：即時轉錄＋總結，Ctrl+C 結束（自動阻止休眠） |
| `lec run 課名 --file 錄音.mp3` | 處理音檔：轉錄完再總結 |
| `lec run 課名 --model qwen3-4b --source mic2` | 臨時換模型、錄音來源 |
| `lec run 課名 --set vad.sensitivity=3` | 臨時覆寫任一設定（可重複） |
| `lec summarize <資料夾>` | 補做尚未完成的總結 |
| `lec summarize <資料夾> --redo 00:05:02` | 重做某一段（`--redo all` 全部重做，舊檔備份為 .bak） |
| `lec config 課名` | 印出合併後的設定 |
| `lec courses` / `lec new 課名` | 列出 / 建立課程設定檔 |
| `lec status` / `lec stop` | 查看 / 停止目前的執行（`--json`、`--force`） |

Ctrl+C 行為：
- 錄音中按一次：停止錄音，轉完剩餘段落，再補做最後一段總結（最多等 `summary.final_wait` 秒）。
- 補做總結時再按：放棄總結，正常收尾（之後可用 `lec summarize` 補）。
- 轉錄剩餘段落時再按：強制結束。

## 設定

合併順序：`config/default.toml` → `courses/<課名>.toml` → 指令列。
課程設定檔只要寫跟預設不同的項目，最常用的是：

```toml
[whisper]
terms = ["光纖", "冗餘", "TCP/IP"]     # 同時用在 whisper prompt 與總結的錯字參考

[summary]
model = "qwen3-4b"
extra_instructions = "本課程著重網路概念，重點請保留協定名稱與層級。"
# prompt_file = "prompts/計概.md"      # 整份替換預設 prompt（可用 {course} {terms} {extra_instructions}）
```

`lec summarize` 預設使用**目前**的課程設定檔（改完 prompt 可直接重做）；找不到才用資料夾內的 `config.used.toml`。

## 輸出（outputs/<課名>_<YYYYMMDD>/，同一天同課名再錄會加 _HHMM）

| 檔案 | 內容 |
|---|---|
| `transcript.md` / `transcript.srt` | 逐字稿（每 5 分鐘一個 `## hh:mm:ss`）、字幕 |
| `notes.md` | 筆記：每段 主題 / 重點 / 術語 / 老師強調 |
| `notes.jsonl` | 總結的原始結果（含驗證報告、耗時、token 數），notes.md 由此產生 |
| `recording_*.ogg` | 整堂錄音 |
| `config.used.toml` | 本次實際使用的設定 |
| `session.log`、`*-server.log` | 執行紀錄 |
| `status.json`、`events.jsonl` | 給 UI 讀的狀態 |

## 防止加入逐字稿以外的內容

1. prompt 規定只能使用本段逐字稿；術語沒解釋就留空。
2. LLM 以 JSON schema 回覆，程式驗證：
   - 「老師強調」的原句要在逐字稿中找得到（相似度 ≥ `summary.quote_match`），否則刪除，並換成逐字稿原文、標上時間。
   - 術語要出現在逐字稿中，否則依 `summary.unverified_terms` 標 ⚠ / 刪除。

## 給 UI 的介面（UI 不 import core）

- **設定**：讀寫 `courses/*.toml`；`lec courses --json` 列出課程；`lec config 課名` 看合併結果。
- **啟動 / 停止**：subprocess 執行 `lec run …`；停止送 SIGINT 給 `lec status --json` 的 `pid`（或執行 `lec stop`）。
- **狀態**：`~/.local/state/lecture-notes/run.json`（是否執行中、輸出資料夾）、`<資料夾>/status.json`：

```json
{"phase": "recording", "mode": "live", "course": "…", "pid": 123,
 "elapsed": 1834.0, "transcribed": 1820.5, "transcribe_lag": 6.2, "queue": 0,
 "sections_total": 7, "sections_summarized": 6, "llm_busy": true, "llm_section": "00:30:02",
 "servers": {"whisper": "ok", "llama": "ok"}, "errors": 0, "last_error": null}
```

  `phase`：starting → loading → recording / transcribing → summarizing → finishing → done（或 failed / aborted）。
- **事件**：`<資料夾>/events.jsonl`（phase、summary、error、stop_requested）。

## 模組

```
lec                 CLI 入口
core/cli.py         子指令
core/config.py      設定載入、合併、驗證、輸出
core/session.py     一次 run / summarize 的流程、訊號處理、收尾
core/servers.py     whisper-server / llama-server 啟動、沿用、關閉
core/transcribe.py  VAD 切段＋即時轉錄（原 live_transcribe.py）
core/summarize.py   分段總結、驗證、notes.md
core/status.py      status.json / events.jsonl / 執行鎖
```
