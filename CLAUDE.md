# CLAUDE.md

給 Claude Code 的專案說明。通用協作規則（PR workflow、安全限制、回報格式）見 @AGENTS.md，本檔只補充這個專案特有的部分。

## 這是什麼

課堂錄音 →（whisper.cpp）逐字稿 →（llama.cpp + Qwen3-8B）每 5 分鐘總結 → Obsidian Markdown，全部在本機跑，不上雲。
使用者是資訊系學生，一堂課最長 3 小時，一天最多 8 小時。

- CLI：`lec`，只用 Python 標準函式庫，主要邏輯在 `core/`
  （唯一例外：`lec --start-ui` 會延遲 import `ui.backend`，缺 fastapi 時給明確錯誤；其他子指令維持純 stdlib）
- Web UI：`ui/`（FastAPI backend + React/TS/Vite frontend），`lec --start-ui` 啟動
  - **主控服務只綁 127.0.0.1。**
  - 「內網唯讀分享」是另一個獨立的 HTTP 服務，預設綁 `0.0.0.0`，也可限定在 `10/8`、`172.16/12`、`192.168/16` 內網位址；它不掛載主控 API，只提供唯讀逐字稿。限制與 API 契約見 `ui/SHARING.md`。不要把「只綁 127.0.0.1」當成全域不變量。
- 平台：Linux 為 v1 正式支援（Debian 13 + Intel Arc 140V Vulkan 實測），macOS／Windows 為 beta

## 常用指令

```bash
python3 -m unittest                      # 全部測試（純 stdlib）
python3 -m unittest tests.test_platform_parsers -v
./lec doctor                             # 檢查環境，回報問題時附上輸出
./lec run 課名 --file samples/test8min.ogg   # 用內建 8 分鐘樣本跑完整流程
./lec run 課名 --transcribe-only         # 只轉錄，不載入 LLM
python3 setup_engines.py whisper         # 只編譯 whisper.cpp
python3 setup.py --dry-run --yes         # 互動式安裝：只印會做什麼，不編譯
```

UI 的測試需要 `ui/backend/requirements.txt` 的 fastapi；沒安裝時 `tests/test_ui_backend_*.py` 會失敗，這不是程式壞掉。

## 檔案所有權（開發者分工）

這個 repo 的開發工作由 Claude Code 與 Codex 分工，各自獨立分支、透過 PR 整合。開發者建立 PR 後，由 Antigravity（設定見 `.agents/agents/code-reviewer/agent.md`）進行獨立程式碼審查，最後由 human maintainer 進行最終審查與合併。**Claude Code 在這裡扮演 Claude 這一邊。**

分工的原則是**「機器面」對「契約面」**：Claude 負責跟這台機器、原生行程與引擎打交道的部分；Codex 負責對外的契約面（CLI 參數、設定 schema、生命週期編排、狀態檔、UI）。遇到新檔案先問它屬於哪一邊，而不是看誰先寫的。

| | Claude（你） | Codex |
|---|---|---|
| **`core/`** | `platform.py`、`devices.py`、`doctor.py`、`servers.py`、`transcribe.py`、`util.py`、`capture.py`、`diarize.py`、`diarize_worker.py` | `cli.py`、`config.py`、`session.py`、`status.py`、`summarize.py`、`terms.py` |
| **其他** | `setup.py`、`setup_engines.py`、`upgrade.py`、`linux_setup.sh`／`mac_setup.command`／`windows_setup.bat`、`docs/platform-*.md`、`engines.lock`、`docs/changelog-update.md`、`.agents/agents/code-reviewer/agent.md`、`README.old.md`（凍結，不再更新）、平台與引擎相關測試 | `ui/`（含 `ui/quality_eval.py`、`ui/SHARING.md`）、UI API/schema、UI contract tests、`prompts/summary.md`、`config/template.toml`、`meetings/` 範本 |
| **共用** | `README.md`、`AGENTS.md`、`config/default.toml`、`docs/meeting-workbench-*.md`、`docs/{windows,mac,linux}_setup.md` | ← 同左 |
| **與 Antigravity 共有** | `CHANGELOG.md` | — |
| **凍結** | `samples/*`、`tools/make_sample.py` | ← 同左（見下方「樣本檔案」） |
| **`tools/`** | `dual_capture_check.py`、`score_diarization.py` | `windows_runner/`（見 AGENTS.md 的 Windows test station） |

為什麼這樣切（依實際相依關係，不是依誰先寫的）：

- `servers.py` 是全 repo 第二高的原生耦合（10 處 `subprocess`／`P.spawn_kwargs`／`P.kill_tree`／`P.env_with_libs`／`P.find_engine_bin`，僅次於 `platform.py` 的 17 處），而且 `doctor.py`（Claude）直接 import 它。它是引擎行程層，不是契約層。
- `transcribe.py` 驅動 ffmpeg 並透過 `P.resolve_source`／`P.ffmpeg_input`／`P.spawn_kwargs` 取得音訊；最難的部分全在 Claude 的平台抽象上。
- `summarize.py` 原生耦合為 0，但讀 16 處設定鍵（全 repo 最高），是純設定／產品邏輯；glossary 功能與 `ui/quality_eval.py` 都是 Codex 做的。
- `terms.py` 原生耦合為 0，只相依 `summarize.py`，而且只被 `lec terms`（`cli.py`）與 UI 的 TermCandidates 消費，與 summarize 同一群。
- `util.py` 雙方都用，但 `pid_alive` 委派給 `platform.py`、`opencc_convert` 要 shell out，編碼與 subprocess 的跨平台問題一向由 Claude 處理（例如 `7abccf8` 的 Windows UTF-8 修正）。
- `config/default.toml` 設為共用，是因為 `core/config.py` 會拿它驗證未知鍵，**任何一方加功能都必須在這裡加鍵**；若歸單一所有者，每個功能都要走一次 CROSS_AGENT_REQUEST，成本不合理。

規則：

- Claude 的實作分支使用 `claude/<task-name>`，**base 一律明確指定 `origin/main`**（這個 checkout 可能同時被別的對話使用，不要相信當下的 HEAD；必要時用 `git worktree`）。
- 只改自己擁有的範圍。需要動到對方的檔案時，**停下來**，用 CROSS_AGENT_REQUEST 格式回報給使用者，不要自己改、也不要混進自己的 commit。
- CROSS_AGENT_REQUEST 內容：Requester／Target agent／類型（blocking or non-blocking）／目的／現有行為／問題／建議行為／涉及檔案／是否改變 public contract／相容性影響／建議測試／Requester 目前能否繼續其他工作。
- **共用檔案**：要改就獨立成一個 commit。`config/default.toml` 只加（或只改）自己程式會讀的鍵，並在同一個 PR 補上說明；`docs/meeting-workbench-*.md` 由改動行為的那一方在同一個 PR 內更新。新的平台文件放 `docs/platform-*.md`。安裝步驟改變時（例如改了 `setup.py`、`upgrade.py`、啟動檔），同一個 PR 內更新對應的 `docs/*_setup.md`。
- **`core/util.py` 的加法例外**：任何一方都可以在自己的 commit 裡新增「純 stdlib、無副作用」的小工具函式，不必先發 CROSS_AGENT_REQUEST；但修改或刪除既有函式的簽章與行為仍走正常流程。
- **測試歸屬**跟著被斷言的那個 contract：斷言 CLI JSON／API／狀態檔契約的歸 Codex，斷言平台與引擎行為的歸 Claude。檔名以被測模組命名。
- **`CHANGELOG.md`** 不跟著 PR 更新：maintainer 累積一批後交給 Antigravity 更新，Claude 負責復核。提示詞與復核清單在 `docs/changelog-update.md`。自己的 PR 不要順手改 `CHANGELOG.md`。
- 若某個檔案在這張表裡找不到，**不要自行假設是自己的**——停下來問 maintainer。
- 若之後改成單一 agent 開發，這一整節可以刪掉。

## 不可擅自改動的 public contract

- **停止機制**：UI 與 `lec stop` 靠在輸出資料夾寫 `stop` / `stop_force` 檔（由程式輪詢），不是送 OS signal。三個平台一致，不要改成 signal。
- **phase 名稱**：`starting → loading → recording/transcribing → summarizing → finishing → done/failed/aborted`
- **設定合併順序**：`config/default.toml` → `config/local.toml`（本機，不進 git）→ `courses/<課名>.toml`（不進 git）→ 指令列（`--model` / `--source` / `--set 區塊.鍵=值`）
- **UI 不得 import core**：只能讀寫設定檔、呼叫 `lec` 子指令（多半有 `--json`）、讀 `status.json` / `events.jsonl` / `run.json`
  - 反方向也要留意：平台偵測邏輯會因此在 `core/platform.py`（Claude）與 `ui/backend/share_network.py`（Codex，硬寫 Linux 的 `ip`）各有一份。改動其中一邊時確認另一邊。
- **總結模型固定 Qwen3-8B**（`[models.*]` 設定驅動，不要在程式裡寫死模型清單）
- CLI 指令與既有參數名稱、exit code、JSON/TOML schema 同樣算 contract，要改必須先說明現況、差異、相容性，並補 contract test。

目前實際出貨的狀態：`status.json` / `run.json` writer 為 **schema 2**，`events.jsonl` 維持 **schema 1**。status 的共同欄位新增 `work_type`、`stop_reason`、`diarization`，run 只新增 `work_type`；reader 相容 schema 1、不回寫舊檔。所有 mode 共用 `<state_dir>/run.lock` 的原子 OS 鎖與公開 `run.json`。課堂 phase 序列不變；`diarizing` 與進度事件已有狀態容器契約測試，但 meeting diarize 編排尚未接線。詳見 [鎖與狀態契約](docs/meeting-workbench-lock-status.md)。

**文件優先順序**：`docs/meeting-workbench-contract.md` 規劃了 schema 2、`diarizing` phase、`mode=diarize` 與 `lec meeting *` 指令。經 maintainer 核准的契約文件優先於本檔上面這份清單，但**實作該變更的同一個 PR 必須同時更新本檔**，不要讓兩邊各說一套。還沒核准、或程式還沒跟上的部分，一律以上面這份清單為準。

2026-10-09 增量：`lec meeting run <id> --speakers N [--file PATH|--source ID] [--set ...] [--json]` 已提供單來源保存與原稿，會議強制保留來源、不啟動 LLM。source.json schema 1 與 CLI 0/1/2/3/130、正常 stop／強停契約見 [單來源會議流程](docs/meeting-workbench-run.md)；status 2 的可選 capture 保留擷取報告。尚無 meeting diarize 編排或 API/UI 錄音控制，真實麥克風與辨識品質未驗收。

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
- 這個 checkout 可能同時被多個對話操作（也有 Codex 的 worktree 在 `~/.codex/worktrees/`）。開工前確認分支，建分支時明確指定 `origin/main`。
