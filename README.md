# 課堂筆記系統（lec）

課堂錄音 → whisper.cpp 即時逐字稿 → llama.cpp 每 5 分鐘總結 → Obsidian Markdown，全部在本機執行。

Repo：<https://github.com/RichardstGG/lecture-notes>

## 平台支援

| 平台 | 錄音 | GPU 後端 | 狀態 |
|---|---|---|---|
| Linux（PulseAudio / PipeWire） | pulse | Vulkan | 已實測（Debian 13、Intel Arc 140V） |
| macOS | avfoundation | Metal | 程式已支援，實測中 |
| Windows | dshow | Vulkan（可改 CUDA） | 程式已支援，實測中 |

共同需求：Python 3.11+、ffmpeg、git、cmake，以及 C++ 編譯環境。

## 安裝

```bash
git clone https://github.com/RichardstGG/lecture-notes.git ~/lecture-notes
cd ~/lecture-notes
```

**1. 系統套件**

```bash
# Linux（Debian / Ubuntu）
sudo apt install ffmpeg curl pulseaudio-utils opencc \
                 git cmake build-essential pkg-config libvulkan-dev glslc vulkan-tools

# macOS
xcode-select --install && brew install cmake ffmpeg opencc

# Windows（PowerShell；另需 Visual Studio Build Tools 的「C++ 桌面開發」與 Vulkan SDK）
winget install Git.Git Kitware.CMake Gyan.FFmpeg Python.Python.3.13
winget install LunarG.VulkanSDK
```

**2. 編譯引擎並取得 whisper 模型**

```bash
python3 setup_engines.py            # Linux/macOS；Windows 用 python setup_engines.py
```

後端預設 Linux/Windows 為 Vulkan、macOS 為 Metal；要用 NVIDIA CUDA 加 `--backend cuda`（需 CUDA Toolkit）。

**3. 下載 LLM 模型（Qwen 官方 GGUF，約 5GB）**

```bash
curl -L -C - -o models/Qwen3-8B-Q4_K_M.gguf \
  https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf
```

**4. 設定麥克風並檢查環境**

```bash
ln -s ~/lecture-notes/lec ~/.local/bin/lec   # Linux/macOS；Windows 用 python lec …
lec devices                     # 列出麥克風
lec devices --test <編號>        # 錄 3 秒看音量
lec devices --save <編號>        # 寫入 config/local.toml
lec doctor --mic                # 全部 ✔ 就可以上課了
```

**5. 用內附的範例音檔驗證整條流程**

```bash
lec run 測試課 --file samples/test8min.ogg   # 轉錄 → 總結，跑完看 outputs/
```

結果可以跟 `samples/expected/` 對照（是結構對照，不是逐字比對，說明見 `samples/README.md`）。
確認沒問題之後就可以建立自己的課程設定：

```bash
lec new 計算機概論 --from example
```

更新程式：`git pull`，若 `engines.lock` 有變動再執行一次 `setup_engines.py`。

### Windows 預編譯檔（不想裝編譯環境時）

llama.cpp 官方有 Windows Vulkan / CUDA 版，whisper.cpp 官方只有 CPU 與 cuBLAS 版（且只有部分 release 附執行檔，例如 v1.9.0）。
手動下載後把執行檔與 DLL 放進 `llama.cpp/build/bin/` 與 `whisper.cpp/build/bin/`，`lec doctor` 就能找到：

- <https://github.com/ggml-org/llama.cpp/releases>：`llama-<版本>-bin-win-vulkan-x64.zip`
- <https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.0>：`whisper-bin-x64.zip`（CPU）或 `whisper-cublas-12.4.0-bin-x64.zip`（NVIDIA）

### 目錄結構

```
~/lecture-notes/
├─ lec, core/, prompts/          程式
├─ setup_engines.py              取得與編譯引擎
├─ samples/                      8 分鐘範例音檔、講稿與參考輸出
├─ tools/make_sample.py          重新產生範例音檔（一般使用者用不到）
├─ config/default.toml           全域預設（進 git）
├─ config/local.toml             這台電腦專屬：麥克風、路徑…（不進 git，範例 local.example.toml）
├─ courses/<課名>.toml            你的課程設定（不進 git）
├─ courses/examples/             課程設定範例（進 git）
├─ engines.lock                  已測試的 whisper.cpp / llama.cpp 版本（進 git）
├─ whisper.cpp/  llama.cpp/      由 setup_engines.py 取得與編譯（不進 git）
├─ models/                       LLM 模型 .gguf（不進 git）
└─ outputs/<課名>_<YYYYMMDD>/     每堂課的輸出（不進 git）
```

### whisper.cpp / llama.cpp 版本

| 指令 | 說明 |
|---|---|
| `setup_engines.py` | checkout `engines.lock` 的版本並編譯；同版本已編好就略過 |
| `setup_engines.py --update` | 升級到最新版，編譯成功後寫回 `engines.lock`（測試沒問題再 commit） |
| `setup_engines.py --rebuild` | 版本或後端不變，強制重新編譯 |
| `setup_engines.py --lock` | 不編譯，把目前的版本記進 `engines.lock` |
| `setup_engines.py --backend vulkan\|cuda\|metal\|cpu` | 指定後端（預設依平台） |
| `setup_engines.py --import-models <資料夾>` | 從其他位置搬入已下載的模型 |
| `setup_engines.py --generator Ninja` | 指定 cmake generator（Windows 想用 Ninja 而非預設判斷的 Visual Studio 時；不指定就維持原本行為） |

以 `BUILD_SHARED_LIBS=OFF` 靜態連結，整個專案資料夾搬到哪裡都能執行。
基準測試：`./bench_llm.sh [逐字稿.md] [第幾段]`（bash 腳本，Linux / macOS 可用），結果在 `outputs/_llm-bench/`。

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
| `lec courses` / `lec new 課名 [--from 範例]` | 列出 / 建立課程設定檔 |
| `lec devices [--test N] [--save N]` | 列出 / 測試 / 設定麥克風 |
| `lec doctor [課名] [--mic]` | 檢查環境（回報問題請附上輸出，`--json` 給 UI） |
| `lec status` / `lec stop` | 查看 / 停止目前的執行（`--json`、`--force`） |

停止方式（Ctrl+C 與 `lec stop` 等效，三個平台相同）：
- 錄音中一次：停止錄音，轉完剩餘段落，再補做最後一段總結（最多等 `summary.final_wait` 秒）。
- 補做總結時再一次：放棄總結，正常收尾（之後可用 `lec summarize` 補）。
- 轉錄剩餘段落時再一次：強制結束。

`lec stop` 是在輸出資料夾寫 `stop` 檔（`--force` 寫 `stop_force`），程式每秒檢查一次。
Windows 無法對別的行程送 Ctrl+C，所以 UI 與腳本一律用這個方式停止。

## 設定

合併順序：`config/default.toml` → `config/local.toml` → `courses/<課名>.toml` → 指令列。
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

- **設定**：讀寫 `courses/*.toml`、`config/local.toml`；`lec courses --json` 列出課程；`lec config 課名` 看合併結果；`lec devices --json` 列出麥克風；`lec doctor --json` 環境檢查。
- **啟動 / 停止**：subprocess 執行 `lec run …`；停止在輸出資料夾寫 `stop`（或 `stop_force`）檔，也可執行 `lec stop`。不需要送訊號，三個平台一致。
- **狀態**：`run.json`（是否執行中、輸出資料夾；位置見 `lec doctor` 的「狀態資料夾」，Linux 為 `~/.local/state/lecture-notes`）、`<資料夾>/status.json`：

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
core/devices.py     錄音來源列表與音量測試
core/doctor.py      環境檢查
core/platform.py    平台差異（錄音後端、防休眠、狀態資料夾、行程管理）
```

## 範例音檔

`samples/test8min.ogg` 是一段約 8 分鐘的模擬課堂錄音：
講稿（`samples/test8min.txt`）由本專案自行撰寫，再由作者本人朗讀錄製，
跟 lec 實際錄音一樣是單聲道 Opus 語音。
不是任何真實課程的錄音，著作權屬本專案，可自由散布。詳見 `samples/README.md`。

## 開發

- 分支：`main`。程式、設定範例、prompts、`samples/` 範例素材與 `engines.lock` 進 git；本機設定、個人課程、原始錄音與 outputs 已由 `.gitignore` 排除。
- 升級引擎：`python3 setup_engines.py --update` → `lec run 課名 --file <錄音>` 與 `./bench_llm.sh` 確認沒問題 → `git commit engines.lock`。
- 回報問題時附上 `lec doctor --json` 與該堂課的 `session.log`。
- 授權：MIT（見 LICENSE）。whisper.cpp、llama.cpp 為 MIT，Qwen3 模型為 Apache-2.0，皆在執行時自行取得。
- 錄音前請先取得老師或學校同意；錄音與逐字稿只留在本機。
