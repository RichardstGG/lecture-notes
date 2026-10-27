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
- 平台：Linux、macOS、Windows 皆為正式支援（maintainer 2026-10-20 決定；Linux 為 Debian 13 + Intel Arc 140V Vulkan，macOS 為 Apple Silicon，Windows 為 Windows 11 + AMD Vulkan 實測）

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

UI 的測試需要 `python3 -m pip install -r ui/backend/requirements-dev.txt`（fastapi、uvicorn，加上測試用的 httpx）；沒安裝時 `tests/test_ui_backend_*.py` 與 `tests/test_ui_sharing.py` 會失敗，這不是程式壞掉。

## 開發流程與檔案規則

開發流程（GitHub issue → Codex 在 issue 專屬 worktree 實作 → Claude 在本機驗證、commit、開 draft PR → Antigravity 獨立審查 → maintainer review 與合併）見 @AGENTS.md 與 `.claude/skills/ai-run/SKILL.md`；Antigravity 的設定在 `.agents/agents/code-reviewer/agent.md`。

- 建分支時 **base 一律明確指定 `origin/main`**（這個 checkout 可能同時被別的對話使用，不要相信當下的 HEAD；必要時用 `git worktree`）。
- `config/default.toml`：`core/config.py` 會拿它驗證未知鍵，新增設定鍵一定要加在這裡，並在同一個 PR 補上說明。
- 改動會議行為的 PR 同時更新 `docs/meeting-workbench-*.md`。新的平台文件放 `docs/platform-*.md`。安裝步驟改變時（例如改了 `setup.py`、`upgrade.py`、啟動檔），同一個 PR 內更新對應的 `docs/*_setup.md`。
- 測試檔名以被測模組命名。
- **`CHANGELOG.md`** 不跟著 PR 更新：maintainer 累積一批後交給 Antigravity 更新，Claude 負責復核。提示詞與復核清單在 `docs/changelog-update.md`。
- `README.old.md` 已凍結，不再更新；`samples/*`、`tools/make_sample.py` 同樣凍結（見下方「樣本檔案」）。

## 不可擅自改動的 public contract

- **停止機制**：UI 與 `lec stop` 靠在輸出資料夾寫 `stop` / `stop_force` 檔（由程式輪詢），不是送 OS signal。三個平台一致，不要改成 signal。
- **phase 名稱**：課堂 `starting → loading → recording/transcribing → summarizing → finishing → done/failed/aborted`；會議錄音／匯入省略 summarizing，會後辨識為 `starting → loading → diarizing → finishing → done/failed/aborted`。
- **設定合併順序**：`config/default.toml` → `config/local.toml`（本機，不進 git）→ `courses/<課名>.toml`（不進 git）→ 指令列（`--model` / `--source` / `--set 區塊.鍵=值`）
- **UI 不得 import core**：只能讀寫設定檔、呼叫 `lec` 子指令（多半有 `--json`）、讀 `status.json` / `events.jsonl` / `run.json`
  - 反方向也要留意：平台偵測邏輯會因此在 `core/platform.py` 與 `ui/backend/share_network.py`（硬寫 Linux 的 `ip`）各有一份。改動其中一邊時確認另一邊。
- **總結模型固定 Qwen3-8B**（`[models.*]` 設定驅動，不要在程式裡寫死模型清單）
- CLI 指令與既有參數名稱、exit code、JSON/TOML schema 同樣算 contract，要改必須先說明現況、差異、相容性，並補 contract test。

目前實際出貨的狀態：`status.json` / `run.json` writer 為 **schema 2**，`events.jsonl` 維持 **schema 1**。status 只新增 `work_type`、`stop_reason`、`diarization`，run 只新增 `work_type`；reader 相容 schema 1、不回寫舊檔。所有 mode 共用 `<state_dir>/run.lock` 的原子 OS 鎖與公開 `run.json`。課堂 phase 序列不變；`lec meeting run`／`lec meeting diarize` 已接持久來源、請求紀錄、`diarizing` 與進度／取消事件，會議 CLI 維持實驗中。**v1 範圍（maintainer 2026-10-15 決定）**：Linux、單一來源的會議錄音／匯入／會後辨識，CLI 與 Web UI 控制都要可運作；辨識品質優化列 v2.x，不是接線前提，雙來源錄音不在 v1。正式寫入 API 與 UI 控制已接線（設定 CRUD、錄音／匯入、停止／強停、辨識／取消／重跑），先通過 API contract tests 才移除 mock。新增唯讀 `lec config --work-type meeting --validate-file PATH` 供 UI 驗證暫存 TOML；既有參數／exit code、stop 檔機制及 schema 版本不變。API 等本次 CLI PID 的鎖與 session 再回 202，配置前失敗按 envelope 映射 400／409／422。**真機驗收（Hardware test）**：Linux 的 CLI 與 Web UI 已完成（2026-10-15，含 15 分鐘錄音）；macOS（Apple Silicon）完成兩輪（2026-10-15、10-18）；Windows 11 完成匯入、會後辨識、取消與重跑、錄音控制、強停錄音檔、#110 啟動確認與 15 分鐘錄音（2026-10-16、10-20）。maintainer 2026-10-20 決定 macOS／Windows 改標正式支援，V2.0 完成。細節見 `docs/meeting-workbench-implementation.md` 里程碑 5。詳見 [鎖與狀態契約](docs/meeting-workbench-lock-status.md)。

**文件優先順序**：`docs/meeting-workbench-contract.md` 規劃了 schema 2、`diarizing` phase、`mode=diarize` 與 `lec meeting *` 指令。經 maintainer 核准的契約文件優先於本檔上面這份清單，但**實作該變更的同一個 PR 必須同時更新本檔**，不要讓兩邊各說一套。還沒核准、或程式還沒跟上的部分，一律以上面這份清單為準。

## 樣本檔案

`samples/test8min.ogg`（使用者本人錄的 8 分鐘真實錄音）、`samples/test8min.txt`、`samples/expected/*`、`tools/make_sample.py` 不要修改或刪除。真的發現 bug 就先回報，不要自己動手。

## 回報與驗證用語

測試結論一定要標明層級：Automated test／Mock test／Static validation／Manual test／Hardware test／Not tested。
三個平台都是正式支援，但每個平台都只有一台機器的真機證據；新的平台相關行為在該平台真機驗證前，仍要標明驗證層級，不要只憑 mock 寫成「已驗證」。

## 環境備忘

- 開發機：Debian 13、Intel Core Ultra 7 258V（Arc 140V iGPU、32GB）、PipeWire，麥克風在 `config/local.toml` 設定
- 引擎版本鎖在 `engines.lock`，由 `setup_engines.py` checkout 與編譯（靜態連結）
- 效能參考：whisper large-v3-turbo 約 6–7x 即時；Qwen3-8B 每段總結約 77–95 秒
- 不進 git：`outputs/`、`models/`、`whisper.cpp/`、`llama.cpp/`、`config/local.toml`、`courses/*.toml`
- 歷史背景文件：`docs/handoff-2026-09-17.md`（已過時，與 repo 現況衝突時以 repo 為準）
- 這個 checkout 可能同時被多個對話操作（Codex 的 issue worktree 在 `.tmp/wt-issue-N`）。開工前確認分支，建分支時明確指定 `origin/main`。
