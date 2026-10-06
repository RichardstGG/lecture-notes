# 雙工作台開發規格與交付順序

以 [正式契約](meeting-workbench-contract.md) 為準。本文件列實作切點；目前已完成契約、前端 mock 預覽，以及以現有 session API 讀取真實會議歷史與兩份逐字稿。不得把 mock 預覽當成可錄會議或可執行辨識的功能。

目前前端採分組導航：會議工作台使用淡藍色主題，歷史紀錄與內網共享子選單捲動到既有區塊，會議設定指向「開始一場會議」預覽區塊；本機設定為獨立頁面。切換頁面保留預覽表單與狀態，不啟停背景工作或關閉分享。本次導航調整未新增會議設定持久化、會議摘要選單或引擎。課堂總結方式沿用 PR #53 的上游選取契約；詳見 [工作台 UI 契約](../ui/WORKBENCH.md)。

## 現有 source 對照

| 現有位置 | 已有行為 | 必要變動及驗證 |
| --- | --- | --- |
| `core/config.py`、`config/default.toml` | default → local → course → CLI；`--model` 只改 summary | 加 work type 選擇、獨立 meetings 設定檔、diarization 驗證；舊 `load()` 行為不變，測相同 id 不串資料 |
| `core/cli.py` | `lec run` 同步執行；`status --json` 查單一鎖 | 加 meeting 子命令與 JSON/exit contract；沒有引擎時不可宣稱辨識成功 |
| `core/session.py` | 建課堂 session、轉錄後可總結；停用 stop 檔輪詢 | meeting 跳過 LLM、來源保存、會後辨識工作；正常停與強停按契約區分 |
| `core/status.py` | status/event/run schema 1；`RunLock` 目前 check+write 有競態 | schema 2 追加欄位與原子單工作鎖；雙行程競跑測試 |
| `core/transcribe.py` | ffmpeg 16 kHz mono、VAD、whisper、原稿/SRT；`keep_recording` 可關 | 由 Claude 製作持久來源與帶音訊時間戳之重新轉錄介面；禁止字數或 OpenCC 字元位置對齊 |
| `core/diarize.py`（新）、`core/diarize_worker.py`（新） | 已實作 `diarize_session`；詳見下方「引擎層現況」 | 接 CLI／設定／狀態檔屬 Codex；品質門檻值待乾淨樣本 |
| `core/servers.py` | whisper/llama server | 由 Claude 確認辨識期 server 生命週期，不能影響課堂 |
| `ui/backend/{app,schemas,session_store,process_control,upload_store}.py` | CLI wrapper、課堂 API、單 output root、upload staging | 增量 API、meeting 設定 store、來源保存前置處理、schema 1 reader 相容及 SSE target |
| `ui/frontend/src/{App.tsx,types.ts}` | 課堂單頁，Phase 以 string 降級；live status polling | 雙工作台導航與跨頁工作提示；meeting preview mock 後再換真 API 資料 |

## 切分為可 review 的里程碑

1. **此 PR：契約、mock 前端及分享銜接。** 新增規格與 meeting 預覽元件；示範狀態控制只改 React 本地狀態，錄音按鈕 disabled。既有內網分享可選擇真實 `work_type=meeting` session，訪客讀取原逐字稿及目前 generation 的帶代號稿。課堂分享、CLI、設定、錄音與辨識流程不改。測 10/4 折疊、進度、兩類分享文件及下載隔離。
2. **Codex 契約底層。** 加 work type、會議設定、來源 metadata、目錄命名、status schema 2、鎖與 CLI 入參。先以假辨識器測 error/cancel/retry 與原子輸出，不導入模型。檔案所有權與跨界變更遵循 [CLAUDE.md](../CLAUDE.md)／[CODEX.md](../CODEX.md)；文件和 contract tests 與程式同 PR。
3. **Claude 引擎品質門檻。** 乾淨的約十人中文加英文術語錄音驗證 diarization，再依 `diarize_session` 契約做封裝與時間戳產物。原始受損錄音的 20.2% 遺失不能作準確度通過標準。模型缺失、錯誤、取消均要可測；不得依賴 torch、numpy、網路服務。
4. **Codex API 與真資料 UI。** 現有 session store 已能讀雙稿；前端歷史與結果現讀真實 meeting session，session SSE 在帶代號稿新增或切換 generation 時發 `speaker_transcript` content event，status schema 2 的會後階段進度可顯示在會議頁與跨頁提示。會議設定、錄音／匯入、辨識、重跑與取消 API 仍待底層 CLI／狀態契約落地。移除其 mock 前須有對應 API contract tests；UI 不 import `core`。
5. **整合與硬體驗收。** 兩平台 beta 仍用實驗中措辭；Linux 實機錄音與匯入皆測、跨頁不中止、暫存清理後重跑、長任務進度、10/4 顯示、兩行程搶鎖、Ctrl+C/stop/stop_force。完成前不可宣稱會議功能可用。

## 契約檢查點

- **現有課堂兼容**：同一個 `lec run` 命令及 `RunPanel`；schema 1 session 缺 `work_type` 顯示課堂。課堂 phase、摘要檔及 course 設定合併結果不變。
- **獨立資料**：meeting 設定不能覆蓋同名課程；meeting session 以 `meetings/` 區隔。同一秒同名並發建立不同路徑。
- **來源完整**：錄音與匯入都保存 session 內檔案及 SHA-256；驗證 `source.json` 才能重跑。舊課堂不要求新 metadata。
- **進度可靠**：`diarization.stage` 的秒數來自音訊處理位置；重轉錄階段可從零重新計，UI 顯示階段標籤，不把兩階段秒數相加成虛假百分比。總長未知時不顯示百分比。
- **結果原子性**：先完成 `speakers.json` schema/區間驗證及 Markdown，再同一 generation 指標切換為目前成功版本；避免兩個最終檔在崩潰時一新一舊。可用 generation 子目錄 + 原子 manifest 指標；讀取器只讀 manifest 指定版本。
- **人數語義**：requested 是使用者提供的預計數，found 是當前分群代號數，actual 是有非空文字的最終代號數。UI 的「主要」僅為顯示門檻，不回寫模型輸出。
- **互斥**：會後處理佔用唯一工作鎖；全域 status 只有一個 active work。切換 UI 與 SSE 重連不更動行程。

## 引擎層現況（Claude）

`core/diarize.py` 已提供契約的 `diarize_session(request, progress=None, cancel=None) -> DiarizationResult`，
**尚未接任何 CLI／UI／設定**；那些由 Codex 依下列介面接線。**辨識準確度尚未驗證**，
唯一可用的真實會議錄音有 20.2% 音訊遺失（見 [platform-macos.md](platform-macos.md)「擷取掉音訊」），不能當通過依據。

**呼叫端要知道的事**

- `request` 是 `DiarizationRequest`：必填 `session_dir`、`source_audio`（session 內的絕對路徑）、
  `source_sha256`（空字串＝不驗；非空且不符就丟 `source_hash_mismatch`）、`meeting_name`、
  `requested_speakers`（1..30 的 int，bool 與 0 都算 `input_invalid`）、兩個模型路徑。
  其餘欄位有預設值：`cluster_threshold=0.5`、`segmentation_window_shift=0.1`、`threads=8`、
  `whisper_server="http://127.0.0.1:8178"`、`opencc=True`。
- **whisper-server 必須先在跑**，引擎層不啟動它（server 生命週期屬 session 編排）。
  沿用正式流程啟動它的參數即可，不需要 `-dtw`；引擎層用 `verbose_json` 取 whisper 自己的區段時間。
- **`progress(event)`** 收到 `{stage, processed_seconds, total_seconds, speakers_found}`。
  `stage` 依序為 `segmentation`、`retranscription`；各自從零計，秒數單調不減且落在 `[0, total]`
  （由引擎層強制，不信任底層回報）。`speakers_found` 在 segmentation 完成前是 0。
- **`cancel()`** 會被輪詢；回傳 true 就丟 `DiarizationCancelled`。取消會真的結束分群子行程
  （實測從開始到取消完成約 8 秒，含模型載入；取消本身不等 sherpa），並清掉暫存目錄；上一版成功產物與 `diarization.current.json` 不變。
- **錯誤**丟 `DiarizationError`，用 `.code` 分類（不要 parse `.message`）：
  `input_invalid`、`source_missing`、`source_hash_mismatch`、`model_missing`、`engine_unavailable`、
  `decode_failed`、`segmentation_failed`、`asr_failed`、`empty_result`、`write_failed`。
  跑完卻沒有任何可用發言是 `empty_result`，不會寫出看似成功的空稿。
- **產物**：`diarization/<generation>/{transcript.speakers.md,speakers.json}`，最後原子更新 session 根的
  `diarization.current.json`。重跑建立新 generation，不改舊的、不改原稿與來源。
  `DiarizationResult` 回傳 generation、requested／actual、各代號有聲秒數與三個 session 相對路徑。
- `diarization.request.json`（契約要求每次辨識另存的請求紀錄）**不是引擎層寫的**：參數與模型雜湊
  在 `speakers.json` 的 `engine` 欄位已有，要不要另存一份由接線的一方決定。

**依賴與行程**

- 分群跑在子行程（`diarize_worker.py`），因為 sherpa-onnx 是第三方套件，只能放專案 `.venv`
  （`lec` 本身仍只用標準函式庫；Debian 13 的 PEP 668 不讓人裝進系統 Python）。
  直譯器依序找：`DiarizationRequest.python` → 環境變數 `LEC_DIARIZE_PYTHON` → 目前直譯器 → 專案 `.venv`。
  找不到就丟 `engine_unavailable`，訊息內含安裝指令。
- 已實測的版本鎖：`sherpa-onnx==1.13.8`（`core/diarize.py::SHERPA_ONNX_VERSION`）。
- `upgrade.py` 預設在專案 `.venv` 安裝依賴並下載兩個模型；也可依 [安裝說明](diarize-install.md) 單獨執行 `setup_engines.py diarize`。安裝流程已具備，仍不代表辨識品質通過驗收。

**門檻值刻意留空，等乾淨樣本**

- `min_turn_seconds=0.25`：短於此的區間不送 whisper。實測極短或近無聲的段落最容易吐出幻覺
  或回吐 prompt，但沒有資料能定下真正的門檻。
- `unassigned_confidence_floor=None`：不設就永遠不會產生 `S00`。硬塞一個數字只會製造假的「未指派」。
- prompt 回吐的過濾只擋明確的 prompt 字串，**沒有擋乾淨**：在一份真實會議上仍有「和會議內容，」這類
  變形片段混進去。「會議內容」本身是正常用詞，不能列入過濾；正確解法是區間長度與能量門檻，需要資料。

**已知成本**：重新轉錄逐區間送 whisper，每個請求不論多短都填滿 30 秒視窗，所以約為會議長度的 0.65 倍
（87.7 分鐘會議約 57 分鐘）。把同一人的相鄰區間打包到接近 28 秒可大幅降低，但要先確認不影響時間戳，
故延後到品質確認之後。

## 所有權依據與尚待決策

- 檔案所有權與共用檔案規則以 [CLAUDE.md](../CLAUDE.md)／[CODEX.md](../CODEX.md) 為準；跨所有權變更走 CROSS_AGENT_REQUEST，跨層整合測試依被斷言的契約分工。
- 模型取得、授權標示與版本鎖見 [安裝說明](diarize-install.md)；各平台安裝驗證與採用方案的品質仍待乾淨錄音確認。
- 會議辨識產物的 generation manifest 具體檔名及舊檔讀取策略須在底層實作 PR 定稿並補測試；正式契約要求「舊成功結果在重跑失敗時仍可讀」，不能只依賴逐一 rename 兩個檔。
