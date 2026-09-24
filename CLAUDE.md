# CLAUDE.md

給 Claude Code 的專案說明。通用協作規則（PR workflow、安全限制、回報格式）見 @AGENTS.md，本檔只補充這個專案特有的部分。

## 這是什麼

課堂錄音 →（whisper.cpp）逐字稿 →（llama.cpp + Qwen3-8B）每 5 分鐘總結 → Obsidian Markdown，全部在本機跑，不上雲。
使用者是資訊系學生，一堂課最長 3 小時，一天最多 8 小時。

- CLI：`lec`，只用 Python 標準函式庫，主要邏輯在 `core/`
- Web UI：`ui/`（FastAPI backend + React/TS/Vite frontend），`lec --start-ui` 啟動，只綁 127.0.0.1
- 平台：Linux 為 v1 正式支援（Debian 13 + Intel Arc 140V Vulkan 實測），macOS／Windows 為 beta

## 常用指令

```bash
python3 -m unittest                      # 全部測試（純 stdlib）
python3 -m unittest tests.test_platform_parsers -v
./lec doctor                             # 檢查環境，回報問題時附上輸出
./lec run 課名 --file samples/test8min.ogg   # 用內建 8 分鐘樣本跑完整流程
./lec run 課名 --transcribe-only         # 只轉錄，不載入 LLM
python3 setup_engines.py whisper         # 只編譯 whisper.cpp
```

UI 的測試需要 `ui/backend/requirements.txt` 的 fastapi；沒安裝時 `tests/test_ui_backend_*.py` 會失敗，這不是程式壞掉。

## 檔案所有權（開發者分工）

這個 repo 的開發工作由 Claude Code 與 Codex 分工，各自獨立分支、透過 PR 整合。開發者建立 PR 後，由 Antigravity（設定見 `.agents/agents/code-reviewer/agent.md`）進行獨立程式碼審查，最後由 human maintainer 進行最終審查與合併。**Claude Code 在這裡扮演 Claude 這一邊。**

| | Claude（你） | Codex |
|---|---|---|
| 擁有 | `core/platform.py`、`core/devices.py`、`core/doctor.py`、`setup_engines.py`、`docs/platform-*.md`、平台相關測試與修復 | `core/cli.py`、`core/config.py`、`core/session.py`、`core/status.py`、`ui/`、UI API/schema、UI contract tests |

- Claude 的實作分支使用 `claude/<task-name>`。
- 只改自己擁有的範圍。需要動到對方的檔案時，**停下來**，用 CROSS_AGENT_REQUEST 格式回報給使用者，不要自己改、也不要混進自己的 commit。
- CROSS_AGENT_REQUEST 內容：Requester／Target agent／類型（blocking or non-blocking）／目的／現有行為／問題／建議行為／涉及檔案／是否改變 public contract／相容性影響／建議測試／Requester 目前能否繼續其他工作。
- `README.md` 是共用檔案，容易衝突：要改就獨立成一個 commit。新的平台文件放 `docs/platform-*.md`。
- 若之後改成單一 agent 開發，這一整節可以刪掉。

## 不可擅自改動的 public contract

- **停止機制**：UI 與 `lec stop` 靠在輸出資料夾寫 `stop` / `stop_force` 檔（由程式輪詢），不是送 OS signal。三個平台一致，不要改成 signal。
- **phase 名稱**：`starting → loading → recording/transcribing → summarizing → finishing → done/failed/aborted`
- **設定合併順序**：`config/default.toml` → `config/local.toml`（本機，不進 git）→ `courses/<課名>.toml`（不進 git）→ 指令列（`--model` / `--source` / `--set 區塊.鍵=值`）
- **UI 不得 import core**：只能讀寫設定檔、呼叫 `lec` 子指令（多半有 `--json`）、讀 `status.json` / `events.jsonl` / `run.json`
- **總結模型固定 Qwen3-8B**（`[models.*]` 設定驅動，不要在程式裡寫死模型清單）
- CLI 指令與既有參數名稱、exit code、JSON/TOML schema 同樣算 contract，要改必須先說明現況、差異、相容性，並補 contract test。

## 樣本檔案

`samples/test8min.ogg`（使用者本人錄的 8 分鐘真實錄音）、`samples/test8min.txt`、`samples/expected/*`、`tools/make_sample.py` 不要修改或刪除。真的發現 bug 就先回報，不要自己動手。

## 回報與驗證用語

測試結論一定要標明層級：Automated test／Mock test／Static validation／Manual test／Hardware test／Not tested。
macOS 與 Windows 的大部分行為只有 mock 與靜態驗證，**在真機驗證前不要寫成「已支援」**，文件與 `lec doctor` 都維持「實驗中」的措辭。

## 環境備忘

- 開發機：Debian 13、Intel Core Ultra 7 258V（Arc 140V iGPU、32GB）、PipeWire，麥克風在 `config/local.toml` 設定
- 引擎版本鎖在 `engines.lock`，由 `setup_engines.py` checkout 與編譯（靜態連結）
- 效能參考：whisper large-v3-turbo 約 6–7x 即時；Qwen3-8B 每段總結約 77–95 秒
- 不進 git：`outputs/`、`models/`、`whisper.cpp/`、`llama.cpp/`、`config/local.toml`、`courses/*.toml`
- 歷史背景文件：`docs/handoff-2026-09-17.md`（已過時，與 repo 現況衝突時以 repo 為準）
