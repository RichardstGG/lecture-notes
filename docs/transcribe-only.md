# 只轉錄模式（不跑 llama.cpp 總結）

給記憶體較小或 GPU 較弱、跑不動 Qwen3-8B 的電腦用（例如 8GB 記憶體的
MacBook）：上課時只用 whisper.cpp 產生逐字稿，**完全不啟動 llama-server**，
之後再把輸出資料夾拿到較強的電腦上用 `lec summarize` 補做筆記。

這個模式靠既有設定 `summary.enabled = false` 達成，沒有新增 CLI 參數或設定鍵。

## 狀態

| 項目 | 驗證層級 |
|---|---|
| `lec run` 在 `summary.enabled = false` 時不建立 `LlamaServer`、不進 `summarizing` phase | 程式碼審查（`core/session.py`）＋ Linux manual test（CPU whisper，見 PR 說明） |
| `lec doctor` 在只轉錄模式下，缺 llama.cpp／LLM 模型只算警告、不檢查 llama port | Automated test（`tests/test_doctor_helpers.py`） |
| `setup_engines.py whisper` 只編譯 whisper.cpp、只下載 whisper 模型 | 既有行為；Linux manual test |
| 在 8GB 記憶體的 Apple Silicon Mac 上實際轉錄一整堂課 | **Not tested**（需要實機） |

## 安裝（只裝 whisper.cpp）

```bash
python3 setup_engines.py whisper      # 不編譯 llama.cpp，也不需要下載 Qwen3 模型
```

## 設定成「這台電腦永遠只轉錄」

在 `config/local.toml`（這台電腦專屬，不進 git）加上：

```toml
[summary]
enabled = false
```

之後 `lec run`、`lec doctor` 都會照這個設定。`lec doctor` 會多一行：

```
✔ 總結                關閉：只轉錄，不會啟動 llama.cpp（…）
⚠ llama-server        找不到 …（只轉錄模式不需要；要總結時執行 setup_engines.py llama）
⚠ LLM 模型（qwen3-8b） 找不到 …（只轉錄模式不需要）
```

這兩個 ⚠ 是預期的，`lec doctor` 的結束碼仍是 0。whisper-server／whisper 模型
缺少時一樣是 ✖。

## 只有這一次不總結

不想改 `local.toml` 時，用既有的 `--set`：

```bash
./lec run 計算機概論 --set summary.enabled=false
./lec run 計算機概論 --file 錄音.m4a --set summary.enabled=false
```

（`--file` 模式也可以用 `--set summary.file_mode=off`，效果相同。）

## 之後在別台電腦補做總結

把整個輸出資料夾（`outputs/<課名>_<日期>/`，裡面有 `transcript.md`）複製到
有 llama.cpp 與 Qwen3-8B 的電腦上，然後：

```bash
./lec summarize outputs/計算機概論_20260921
```

`lec summarize` 只會總結尚未完成的段落，產生 `notes.md`。

## 記憶體估算（參考值，未實測）

- whisper large-v3-turbo 模型檔約 1.6GB，執行時約 2GB 上下。
- Qwen3-8B Q4_K_M 約 5GB，加上 ctx 8192 的 KV cache，8GB 記憶體的機器同時跑
  whisper＋8B 很吃緊，這也是只轉錄模式存在的原因。
