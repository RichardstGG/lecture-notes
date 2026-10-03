# 雙工作台開發規格與交付順序

以 [正式契約](meeting-workbench-contract.md) 為準。本文件列實作切點；目前僅完成契約與前端 mock 預覽。不得把 mock 預覽當成可錄會議或可執行辨識的功能。

## 現有 source 對照

| 現有位置 | 已有行為 | 必要變動及驗證 |
| --- | --- | --- |
| `core/config.py`、`config/default.toml` | default → local → course → CLI；`--model` 只改 summary | 加 work type 選擇、獨立 meetings 設定檔、diarization 驗證；舊 `load()` 行為不變，測相同 id 不串資料 |
| `core/cli.py` | `lec run` 同步執行；`status --json` 查單一鎖 | 加 meeting 子命令與 JSON/exit contract；沒有引擎時不可宣稱辨識成功 |
| `core/session.py` | 建課堂 session、轉錄後可總結；停用 stop 檔輪詢 | meeting 跳過 LLM、來源保存、會後辨識工作；正常停與強停按契約區分 |
| `core/status.py` | status/event/run schema 1；`RunLock` 目前 check+write 有競態 | schema 2 追加欄位與原子單工作鎖；雙行程競跑測試 |
| `core/transcribe.py` | ffmpeg 16 kHz mono、VAD、whisper、原稿/SRT；`keep_recording` 可關 | 由待指派 owner 製作持久來源與帶音訊時間戳之重新轉錄介面；禁止字數或 OpenCC 字元位置對齊 |
| `core/servers.py` | whisper/llama server | 待指派 owner 確認辨識期 server 生命週期，不能影響課堂 |
| `ui/backend/{app,schemas,session_store,process_control,upload_store}.py` | CLI wrapper、課堂 API、單 output root、upload staging | 增量 API、meeting 設定 store、來源保存前置處理、schema 1 reader 相容及 SSE target |
| `ui/frontend/src/{App.tsx,types.ts}` | 課堂單頁，Phase 以 string 降級；live status polling | 雙工作台導航與跨頁工作提示；meeting preview mock 後再換真 API 資料 |

## 切分為可 review 的里程碑

1. **此 PR：契約與 mock 前端。** 只新增兩份規格與獨立 meeting 預覽元件。真實課堂流程、CLI、API、設定、status 與檔案輸出均不改。示範狀態控制只改 React 本地狀態；錄音按鈕 disabled。前端測 10/4 短發言折疊與進度展示。
2. **Codex 契約底層。** 加 work type、會議設定、來源 metadata、目錄命名、status schema 2、鎖與 CLI 入參。先以假辨識器測 error/cancel/retry 與原子輸出，不導入模型。涉及未指派檔案前先取得 maintainer 指派；文件和 contract tests 與程式同 PR。
3. **Claude 引擎品質門檻。** 乾淨的約十人中文加英文術語錄音驗證 diarization，再依 `diarize_session` 契約做封裝與時間戳產物。原始受損錄音的 20.2% 遺失不能作準確度通過標準。模型缺失、錯誤、取消均要可測；不得依賴 torch、numpy、網路服務。
4. **Codex API 與真資料 UI。** API 從 CLI 取得真狀態，session store 讀雙稿，前端改用 meeting 真設定與作業。移除 mock 前須有錄音／匯入／辨識／重跑/取消的 API contract tests；UI 不 import `core`。
5. **整合與硬體驗收。** 兩平台 beta 仍用實驗中措辭；Linux 實機錄音與匯入皆測、跨頁不中止、暫存清理後重跑、長任務進度、10/4 顯示、兩行程搶鎖、Ctrl+C/stop/stop_force。完成前不可宣稱會議功能可用。

## 契約檢查點

- **現有課堂兼容**：同一個 `lec run` 命令及 `RunPanel`；schema 1 session 缺 `work_type` 顯示課堂。課堂 phase、摘要檔及 course 設定合併結果不變。
- **獨立資料**：meeting 設定不能覆蓋同名課程；meeting session 以 `meetings/` 區隔。同一秒同名並發建立不同路徑。
- **來源完整**：錄音與匯入都保存 session 內檔案及 SHA-256；驗證 `source.json` 才能重跑。舊課堂不要求新 metadata。
- **進度可靠**：`diarization.stage` 的秒數來自音訊處理位置；重轉錄階段可從零重新計，UI 顯示階段標籤，不把兩階段秒數相加成虛假百分比。總長未知時不顯示百分比。
- **結果原子性**：先完成 `speakers.json` schema/區間驗證及 Markdown，再同一 generation 指標切換為目前成功版本；避免兩個最終檔在崩潰時一新一舊。可用 generation 子目錄 + 原子 manifest 指標；讀取器只讀 manifest 指定版本。
- **人數語義**：requested 是使用者提供的預計數，found 是當前分群代號數，actual 是有非空文字的最終代號數。UI 的「主要」僅為顯示門檻，不回寫模型輸出。
- **互斥**：會後處理佔用唯一工作鎖；全域 status 只有一個 active work。切換 UI 與 SSE 重連不更動行程。

## 尚待 maintainer 指派及決策

- `core/transcribe.py`、`core/servers.py`、新 `core/diarize.py`、`core/summarize.py`、`core/util.py`、`config/default.toml`、`config/template.toml`、`meetings/` 範本及跨層整合測試未在 `CODEX.md` / `CLAUDE.md` 指定 owner；指派前不跨界實作。
- 模型檔的取得、授權標示、版本鎖及各平台安裝驗證要由 Claude 與 maintainer 確定。採用方案的品質尚待乾淨錄音驗證。
- 會議辨識產物的 generation manifest 具體檔名及舊檔讀取策略須在底層實作 PR 定稿並補測試；正式契約要求「舊成功結果在重跑失敗時仍可讀」，不能只依賴逐一 rename 兩個檔。
