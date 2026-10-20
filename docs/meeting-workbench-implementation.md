# 雙工作台開發規格與交付順序

2026-10-08 鎖與狀態層：status／run writer 已升 schema 2，events 維持 schema 1；所有工作共用持有至結束的原子 OS 鎖，舊檔不回寫。會議錄音與辨識編排已於本次 CLI 里程碑接線。詳見 [單工作鎖與狀態相容性](meeting-workbench-lock-status.md)。

2026-10-08 設定與資料層：已加入獨立會議設定載入／驗證、`lec meetings --json`、`lec config --work-type meeting`、work type 快照及排他輸出目錄函式。此設定里程碑未接錄音、辨識編排與 API 控制。詳見 [已實作範圍、CLI schema 與相容性](meeting-workbench-config.md)。

2026-10-06 雙音源增量：已實作設定驗證、能力查詢與安全阻擋，錄音仍未開放；引擎介面、來源 schema 2 與硬體驗收皆明標提案／未測試。詳見 [雙音源契約與 CROSS_AGENT_REQUEST](meeting-workbench-dual-audio.md)。

以 [正式契約](meeting-workbench-contract.md) 為準。本文件列實作切點；目前已完成契約、CLI 編排／持久來源／引擎接線、正式寫入 API／UI 控制，以及以 session API／SSE 讀取會議歷史與兩份逐字稿。mock 已移除；Linux UI 真機整合驗收待 Claude 完成。

目前前端採分組導航：會議工作台使用淡藍色主題，歷史紀錄與內網共享子選單捲動到既有區塊，會議設定指向「開始一場會議」的正式設定與控制區塊；本機設定為獨立頁面。切換頁面保留表單與停止請求狀態，不啟停背景工作或關閉分享。會議設定由新增 API 持久化；會議沒有摘要選單。課堂總結方式沿用 PR #53 的上游選取契約；詳見 [工作台 UI 契約](../ui/WORKBENCH.md)。

## 現有 source 對照

| 現有位置 | 已有行為 | 必要變動及驗證 |
| --- | --- | --- |
| `core/config.py`、`config/default.toml` | 已提供 work type、獨立 meetings 設定與 diarization 驗證；課堂 `load()` 行為保留 | 執行編排沿用已驗證的設定；保持同名課堂／會議隔離 |
| `core/cli.py` | 課堂與會議工作同步執行；會議列表、設定查詢與 JSON/exit contract 已提供 | 正式 UI 控制已接線，品質優化列 v2.x；沒有引擎時回明確失敗 |
| `core/session.py` | 會議跳過 LLM、保存來源、編排會後辨識；沿用 stop 檔 | 正常停／強停已有契約測試，真實錄音硬體驗收待完成 |
| `core/status.py` | status/run schema 2、event schema 1；持有整段工作的 OS 鎖，四行程競跑測試 | meeting run/diarize 已共用；實際硬體取消仍待驗收 |
| `core/transcribe.py` | ffmpeg 16 kHz mono、VAD、whisper、原稿/SRT；`keep_recording` 可關 | CLI 編排持久來源；引擎按音訊時間戳重新轉錄，禁止字數或 OpenCC 字元位置對齊 |
| `core/diarize.py`（新）、`core/diarize_worker.py`（新） | 已實作 `diarize_session`；詳見下方「引擎層現況」 | CLI 執行／設定映射／狀態檔已由 Codex 接線；品質門檻值待乾淨樣本 |
| `core/servers.py` | whisper/llama server | CLI 已沿用 WhisperServer 管理辨識期生命週期，課堂流程不變 |
| `ui/backend/{app,schemas,session_store,process_control,upload_store}.py` | 課堂 API、upload staging、schema 1 reader 相容、會議雙稿唯讀與 SSE | 已加 MeetingStore 與執行 API，CLI 負責來源複製與完整性驗證 |
| `ui/frontend/src/{App.tsx,types.ts}` | 雙工作台導航、跨頁提示、會議歷史／雙稿與進度讀取；控制接正式 API | 已移除 mock，保留真資料進度、雙稿、取消與重跑；Phase 保留 string 降級 |

## 切分為可 review 的里程碑

1. **已完成：契約、mock 前端及唯讀分享銜接。** 新增規格與 meeting 預覽元件；示範狀態控制只改 React 本地狀態，錄音按鈕 disabled。既有內網分享可選擇真實 `work_type=meeting` session，訪客讀取原逐字稿及目前 generation 的帶代號稿。課堂分享、CLI、設定、錄音與辨識流程不改。測 10/4 折疊、進度、兩類分享文件及下載隔離。
2. **Codex 契約底層：CLI 編排完成。** work type、會議設定查詢、目錄命名、status schema 2、原子鎖、來源 metadata 與會議執行 CLI 已提供。使用假辨識器驗證 error/cancel/retry 與原子輸出；真實品質仍待驗收。檔案所有權與跨界變更遵循 [CLAUDE.md](../CLAUDE.md)／[CODEX.md](../CODEX.md)；文件和 contract tests 與程式同 PR。
3. **Claude 引擎已封裝，品質驗收待完成。** 已提供 `diarize_session` 封裝與時間戳產物；仍須以乾淨的約十人中文加英文術語錄音驗證 diarization。原始受損錄音的 20.2% 遺失不能作準確度通過標準。模型缺失、錯誤、取消均要可測；不得依賴 torch、numpy、網路服務。
4. **Codex API 與真資料 UI：正式控制已接線。** 現有 session store 已能讀雙稿；前端歷史與結果現讀真實 meeting session，session SSE 在帶代號稿新增或切換 generation 時發 `speaker_transcript` content event，status schema 2 的會後階段進度可顯示在會議頁與跨頁提示。底層執行 CLI 與來源保存已落地；會議設定、錄音／匯入、辨識、重跑與取消 API 已接線，先通過對應 API contract tests 才移除 mock；品質優化列 v2.x；UI 不 import `core`。
5. **整合與硬體驗收。** 兩平台 beta 仍用實驗中措辭；Linux 實機錄音與匯入皆測、跨頁不中止、暫存清理後重跑、長任務進度、10/4 顯示、兩行程搶鎖、Ctrl+C/stop/stop_force。完成前不可宣稱會議功能可用。
   - **2026-10-15 Linux 真機 CLI 驗收（Hardware test，Debian 13、內建數位麥克風、PipeWire pulse、main `5054bfd`）**：
     `lec meeting run` 現場錄音三次——`lec stop` 約 60 秒 → `done`／exit 0、`stop_reason=user`、發布 `kind=recording` 的 `source.json`，
     capture `lost_ratio` 0.006%；`lec stop --force` 約 30 秒 → `aborted`／exit 130、不發布 `source.json`、鎖已釋放、無殘留 ffmpeg／whisper-server；
     SIGINT 約 30 秒 → `done`／exit 0 並發布來源。對第一段 `lec meeting diarize` → `done`、generation `0001`，原稿與來源雜湊不變；
     辨識進行中另啟 `lec meeting run` → `busy`／exit 3。
   - **2026-10-15 Linux API 端到端（Manual test，PR #104 審查時，匯入 `samples/test8min.ogg`，不經瀏覽器）**：
     建立會議設定 200、重複 409、TOML 錯誤 400、`speakers=0` 400；`POST /meeting-runs` 202（0.17 秒回應、回傳 session、pid 相符）、
     執行中再送 409；`POST /sessions/{id}/diarize` 202 → 辨識中 `POST /runs/stop` 正常取消 → 歷史原稿仍 `done`、`diarization_status`
     為 `aborted`／`user`；再次辨識 → `done` 並經 API 回傳帶代號稿。
   - **2026-10-15 Linux UI 整合驗收（Hardware test＋Manual test，main `e9cf85e`，Claude 桌面 app 內建瀏覽器操作 Web UI，內建數位麥克風）**：
     - UI 現場錄音約 1 分鐘 → 「正常停止」→ 原稿完成、來源 `kind=recording`；錄音中切到課堂工作台約 20 秒再回來，工作不中斷，
       跨頁提示列持續顯示「錄音中」；執行中「開始錄音」「開始辨識」皆停用。
     - UI 會後辨識 → 顯示 `segmentation`／`retranscription` 階段、秒數與「要求 N／已找到 M」→「正常取消」→ 上一版 generation
       `0001` 保留、`diarization_status=aborted`、無殘留 staging →「重跑辨識」→ 發布 `0002`。歷史清單約數秒內隨輪詢更新。
     - UI 現場錄音約 20 秒 →「強制停止」→ `aborted`、不發布 `source.json`、`last_error` 為空（#104 的修正確認有效）、無殘留 ffmpeg／whisper-server。
     - UI 匯入（maintainer 以檔案選擇器選 `samples/test8min.ogg`）→ `done`，`source_audio` 雜湊與原檔相同；刪除 upload staging 後
       從 UI 重跑辨識 → `done`。
     - UI 現場錄音 15 分 31 秒 →「正常停止」→ `done`；capture `lost_ratio` 0.008%；每分鐘取樣 RSS 穩定（lec 約 25 MB、
       whisper-server 約 99 MB 不含 GPU 記憶體、ffmpeg 約 18 MB），轉錄佇列維持 0。會後辨識約 75 秒完成。
   - **UI 驗收發現的兩個問題已由 #106 修正**（Linux 於 PR 審查時再以真實 UI 確認）：
     1. 會議工作台水平裁切：`.meeting-workbench` 的 grid 欄寬為 `auto`，歷史卡片列的 min-content 隨場次增加把欄撐寬，
        卡片右側（含「開始辨識」按鈕）被切掉。現在只有歷史列自己水平捲動。
     2. UI 匯入時 `source.json` 的 `original_name` 記成上傳暫存的隨機檔名，不是使用者的原始檔名。
   - **2026-10-15、10-18 macOS 真機驗收（Hardware test＋Manual test，Apple M5 Pro、macOS 27.0.1、ffmpeg 9.0.1，各一輪）**：
     - 第一輪（main `a28bcc3`）：匯入與會後辨識（約 30 秒）、取消保留舊 generation、重跑、搶鎖 `busy`；CLI 與 UI 的錄音正常停止／強停／
       Ctrl+C；UI 檔案選擇器匯入（顯示原始檔名）、刪除上傳暫存後辨識、跨頁、800px 版面；UI 啟動通知約 0.47 秒，沒有 503。
       原生擷取（avfoundation helper）`lost_ratio` 與原生 PTS 遺失皆為 0；helper 第一次編譯、之後使用快取（目錄 `0700`）。
       15 分鐘 UI 錄音 `lost_ratio` 0、86,171 次 callback 0 個空隙；每分鐘取樣 lag ≤ 1.3 秒、queue ≤ 1，lec 約 35–42 MiB、
       whisper-server 約 1.8 GiB（RSS，不含全部 GPU 記憶體）、helper 約 26 MiB 沒有持續增長；會後辨識 89 秒。
       Metal 的 SRT 與 `samples/expected` 位元組相同。自動測試 1121 項，唯一錯誤是 `127.0.0.2` 逾時（已由 #107 修正）。
     - 第二輪（main `fb3c039`，#107／#108／#110 重測）：完整測試 1131 項 0 失敗 0 錯誤（略過 11）、`npm audit` 0 個漏洞；
       強停錄音檔 CLI 142,814 bytes／29.0 秒、UI 80,993 bytes／16.0 秒，皆非空、可完整解碼、無 `source.json`；正常停止
       60.73 秒、`lost_ratio` 0；UI 啟動通知約 0.27 秒、pid 與 `lec status` 相等、重複送出回 409 `run_active`。
   - **2026-10-16、10-20 Windows 真機驗收（Hardware test＋Manual test，Windows 11 Pro 10.0.26200、Ryzen 9 5900X、
     RX 6600 XT／Vulkan、ffmpeg 9.0.2，各一輪）**：
     - 10-16（main `fb3c039`，非錄音）：匯入、會後辨識約 209 秒、取消保留舊 generation、重跑、`busy`；UI 檔案選擇器匯入
       「會議 測試 test8min.ogg」顯示原始檔名、刪除暫存後辨識、跨頁、800px 版面；三個 session 的 `PermissionError`／`WinError 5/32/33` 為 0。
       #110 的檔案匯入：API 回傳的 launcher pid（venv `python.exe` 啟動器）與 `lec status`／`run.json` 的實際 pid 不同，
       仍在約 0.7 秒回 202、沒有 503。Windows 的 SRT 與 Linux／macOS 基準不同（100 行對 104 行；AMD Vulkan），不算失敗。
     - 10-20（main `ff88453`，錄音）：CLI 正常停止／強停／真正的 `CTRL_C_EVENT` 為 `done`／`aborted`／`done`，來源發布規則正確；
       #108 強停錄音檔 CLI 52,337 bytes／29.178 秒、UI 59,457 bytes／32.098 秒，皆非空、可解碼；#110 錄音按鈕啟動 0.740 秒／202
       （launcher pid 66116 → 實際 pid 71396 = `status` = `run.json`），重複送出回 409 `run_active`；跨頁、強停、7 個 session 的
       `session.log` 權限錯誤為 0。15 分鐘錄音來源 917.677 秒、`lost_ratio` 0.0089%（74 個最長 2 毫秒的空隙）、記憶體持平
       （lec 約 33 MiB、whisper-server 約 1.6 GiB、ffmpeg 約 34 MiB）。#112 在沒有 cmake 的 process 中：`setup_engines.py whisper`
       與 `upgrade.py --skip-pull --whisper-only --skip-ui` exit 0；`setup_engines.py llama` exit 1 並附說明；doctor 的新訊息正確。
     - **限制**：這台 Windows 的麥克風輸入近乎靜音（`mean` 約 −91 dB），VAD 略過全部 153 段，所以**有人聲時 Windows 的即時轉錄延遲與積壓沒有測**；
       對該錄音的會後辨識回 `empty_result`、不發布 generation、原稿與來源保留，這是契約規定的行為（沒有可用發言不當成功），不是失敗。
       Windows 上對「有人聲的匯入檔」的會後辨識已於 10-16 驗證成功。
   - **強停錄音的尾端損失（#108 修正後，三個平台一致）**：Linux 1.13 秒、macOS 1.3／1.1 秒、Windows 1.12／1.20 秒
     （最終 `status` 的 `elapsed` 減去檔案長度）。原因是 ffmpeg 的 Ogg 封裝預設 `page_duration` 為 1 秒，被強制結束時最後一個未寫出的
     page 會遺失，另有約 0.1–0.3 秒的編碼與管線緩衝，所以預期是約 1–1.5 秒；超過 2 秒或檔案為空才算異常。
   - **仍然只有一台機器的證據**：三個平台各一台，macOS 為 Apple Silicon（Intel Mac 未測），Windows 為 AMD GPU。maintainer 2026-10-20 決定 macOS／Windows 改標正式支援。
   - 觀察（v2.x 品質範圍，不阻擋 v1）：
     - 強停後 `last_error` 的 `ffmpeg 結束代碼 -9` 與 `lec stop` 對會議仍寫「補做總結」兩項已由 #104 修正。
     - 真實環境錄音的帶代號稿比原稿稀疏許多（58 秒只留 6 段）。
     - 15 分鐘錄音裡 VAD 略過 119 段、只送 4 段；後 12 分鐘確實無人說話（會後辨識同樣只在前 2 分 40 秒找到語音），
       但前 1 分鐘幾句很短、很小聲的應答（「對，對」「嗯嗯」）被 VAD 判為「幾乎無人聲」略過，會後辨識則有抓到。
   - **其他觀察（小問題，不阻擋 v1；三項屬 Codex 範圍，已整理給 maintainer）**：
     - `empty_result` 對使用者的訊息是「會後辨識失敗，請依錯誤代碼檢查本機環境」，對近乎靜音的來源有誤導；
       較貼切的說法是「沒有偵測到可用的發言（來源可能幾乎無聲）」（`core/session.py` 的錯誤映射）。
     - UI 強制停止後，status 的 `servers.whisper` 仍是 `ok`，實際引擎行程已被終止。
     - 會後辨識進行中切到課堂工作台，主卡片把 `mode=diarize` 顯示成「載入模型 · 現場錄音」；上方提示列正確。
     - 數個 UI 測試用 1 秒的子行程逾時（例如 `test_courses_rejects_wrong_json_shape`），在辨識負載下的 Windows 上會波動；
       空載的 Windows basic 全數通過。

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
**已由 CLI 編排執行，正式 UI 控制已接入**；CLI 啟動／收尾 WhisperServer、保存來源與請求紀錄，並映射進度、錯誤及取消。**辨識準確度尚未驗證**，
唯一可用的真實會議錄音有 20.2% 音訊遺失（見 [platform-macos.md](platform-macos.md)「擷取掉音訊」），不能當通過依據。

**呼叫端要知道的事**

- `request` 是 `DiarizationRequest`：必填 `session_dir`、`source_audio`（session 內的絕對路徑）、
  `source_sha256`（空字串＝不驗；非空且不符就丟 `source_hash_mismatch`）、`meeting_name`、
  `requested_speakers`（1..30 的 int，bool 與 0 都算 `input_invalid`）、兩個模型路徑。
  其餘欄位有預設值：`cluster_threshold=0.5`、`segmentation_window_shift=0.1`、`threads=8`、
  `whisper_server="http://127.0.0.1:8178"`、`opencc=True`、`max_asr_seconds=28.0`。
- **單次送 whisper 的音訊不超過 `max_asr_seconds`（含 pad，允許 1–30 秒）。** 同一人合併後的區間可以
  遠超過 30 秒；引擎層在每段後半找最安靜的 300ms，從中間切開再送，規則與課堂 VAD 沒有靜音時的退路相同。
  沒加上限之前，8 分鐘樣本裡一段 70.8 秒的區間被 whisper 解成 byte-level 錯字（「，」→「Ｂ」共 13 處）
  並吐出無效 UTF-8；只看單一 30ms frame 的切法又曾把「李奇」切成兩半。上限記在 `speakers.json` 的
  `engine.params.max_asr_seconds`，也會出現在 `diarization.request.json`。
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
  在 `speakers.json` 的 `engine` 欄位已有；CLI 編排層已依正式契約另存請求紀錄。

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
故延後到品質確認之後。長區間切片後請求數變多（8 分鐘樣本 16 → 26 次），總耗時實測沒有明顯變化（約 150 秒）。

**2026-10-15 流程冒煙測試（Manual test，Linux／Vulkan，不是品質驗收）**：`samples/test8min.ogg`（單人課堂）
走 `lec meeting run --file` → `lec meeting diarize --speakers 2`。原稿 SRT 與 `samples/expected` 位元組相同；
辨識約 150 秒，`actual_speakers=1`（單人錄音沒有硬湊第二位）；兩次重跑輸出逐字相同，原稿與來源雜湊不變。
仍有 whisper 自身的零星錯字（例如英文「owner」轉成「ёнner」），以及 whisper 在同一片音訊內斷句時把句號
留到下一行開頭。乾淨多人會議的準確度仍未驗證。

## 雙來源錄音引擎層現況（Claude）

線上會議同時收「指定輸出裝置的聲音」與「使用者麥克風」。**引擎層已完成，設定驗證與能力查詢已提供，但公開錄音入口仍停用**。CLI／UI 執行編排及來源／狀態契約映射仍由 Codex 接線。第一版只支援 Linux（PulseAudio 相容介面，含 PipeWire），主要情境是戴耳機；
**硬體驗收尚未做**，macOS／Windows 引擎會明確拒絕。設計、可靠性行為、限制與驗收步驟見
[platform-dual-capture.md](platform-dual-capture.md)；以下 schema 與事件名稱都是**提案**，Codex 的契約若不同，
以契約為準、引擎配合調整。

**介面（可直接呼叫）**

- `devices.plan_dual(system_output="default", mic="default")` → `{ok, sources, errors, warnings, resolved}`。
  只查詢不錄音；`default` 在開始當下解析成實際裝置並固定（預設裝置之後改變，錄音不跟著換）。
  `errors[].code`：`unsupported_platform`／`no_audio_tools`／`output_not_found`／`mic_not_found`／
  `no_default_output`／`no_default_mic`／`mic_is_monitor`／`same_device`；
  `warnings[].code`：`output_may_echo`、`no_playback_on_output`。`devices.list_outputs()` 列輸出裝置。
- `Transcriber(cfg, dir, url, status=…, capture_sources=[SourceSpec(**s) for s in plan["sources"]])`：
  不傳就是原本的單來源流程（不變）。停止仍走既有的 `request_stop()`（由 stop／stop_force 檔觸發，不是 OS signal）。
  `run()` 的回傳多兩個鍵（只有雙來源模式才有）：`capture_session`、`degraded`。
- `capture.verify_capture(session_dir)` → `state ∈ complete｜degraded｜incomplete｜invalid｜missing`；
  重跑前的完整性閘門。強制停止或崩潰一律是 `incomplete`（不可重跑，`playable` 供人工搶救）。
- `MultiCapture.snapshot()`：每路 `recording｜failed｜ended`、多久沒資料、近期峰值、是否數位靜音，
  讓 UI 區分「沒聲音」與「壞了」。

**產物**（session 內）：`tracks/system.ogg`、`tracks/mic.ogg`（t=0 都是同一個時間軸原點）、
混音沿用既有的 `recording_HHMMSS.ogg`、`capture.json`（起點偏移、缺口、補丟樣本、時脈誤差、故障、SHA-256）。
`audio.keep_recording=false` 只關混音，分軌一律保留。

**事件**（`status.event`）：`capture_started`、`capture_gap`、`capture_source_failed`、`capture_warning`、
`capture_error`、`capture_start_failed`；來源故障與 `capture_error` 同時呼叫 `status.error()`，
UI 不會漏看。開始時兩路都必須成功開啟，否則整個不開始（`result["ffmpeg_failed"]=True`、
`capture_session.start_error.code` 是穩定分類）。中途單路故障：另一路繼續，`degraded=true`，不自動換裝置、不自動重連。

**Codex 接線需求**（細節見 PR 的 CROSS_AGENT_REQUEST）

1. 執行設定映射：既有雙音源設定驗證與能力查詢已提供；公開入口啟用前須對齊引擎的輸出裝置／麥克風參數，呼叫 `plan_dual()`、拒絕 `ok=false`，不改既有 `audio.sources` 別名表的意義。
2. `source_audio.ogg`／`source.json` 該指向混音還是另行處理，以及分軌如何出現在 `source.json`（`kind=recording` 的延伸）。
3. 狀態契約：上述事件是否放進 `events.jsonl`（新類型）、`status.json` 是否顯示每路狀態與降級。
4. 重跑閘門：用 `verify_capture()`，`incomplete`／`invalid` 不可重跑。
5. 會議模式 `keep_recording` 的政策、UI 的來源選擇與「戴耳機、無回音消除」提示。

## 所有權依據與尚待決策

- 檔案所有權與共用檔案規則以 [CLAUDE.md](../CLAUDE.md)／[CODEX.md](../CODEX.md) 為準；跨所有權變更走 CROSS_AGENT_REQUEST，跨層整合測試依被斷言的契約分工。
- 模型取得、授權標示與版本鎖見 [安裝說明](diarize-install.md)；各平台安裝驗證與採用方案的品質仍待乾淨錄音確認。
- 引擎已使用 `diarization.current.json` 原子切換 generation，session／分享 reader 已讀取 manifest 指定的帶代號稿；取消或失敗保留上一版成功產物。後續編排須延續此契約，並補上執行入口層的失敗／重跑驗證，不能改為逐一 rename 兩個最終檔。


## CLI 編排里程碑（Codex）

- `lec meeting run <id> --speakers N [--file PATH] [--source ID] [--set k=v] [--json]`：
  原子配置新 session，強制保留來源、停用摘要，匯入先複製／驗證再轉錄，錄音先驗完整性再保存 `source_audio.ogg`。
- `lec meeting diarize <session-path> --speakers N [--set diarization.k=v] [--json]`：
  驗來源 metadata／大小／SHA-256，保存請求與模型雜湊，管理 WhisperServer，呼叫 `diarize_session`。
- 兩工作與課堂共用 `run.lock`，忙碌 exit 3。status/run 保持 schema 2、events 保持 schema 1；進度新增
  `diarization_progress`，取消新增 `diarization_cancelled`。來源、JSON／exit、停止與欄位格式見正式契約的「CLI 落地格式與操作」。
- Automated／Mock contract tests：`python3 -m unittest tests.test_session_meeting -v`。
  包含 JSON 0／1／2／3／130、兩真實 CLI 行程搶鎖、stop／stop_force／Ctrl+C、匯入失敗、來源雜湊、
  capture integrity、所有引擎錯誤代碼、請求紀錄、進度及保留上一版結果。
- 此 CLI 里程碑當時未啟用正式 UI；2026-10-15 寫入 API 與控制現況見下方里程碑 4／5。品質優化列 v2.x；Linux UI 真機、Mac／Windows 錄音硬體驗收仍待完成。


### PR #95 審核修正

匯入取消與現場正常停止分開收尾；辨識保留原錄音的時間／長度／原稿指標。
歷史以原工作的 phase 事件呈現原稿結果，以可選的 `diarization_status` 顯示最近辨識結果；
已驗證 `source_audio.*` 納入 `has_recording`，不再依賴會議原本的 recording 檔名。
該輪修正未擴張 status schema；正式 UI 控制已於後續里程碑接線。收尾錯誤必釋放鎖並回作業錯誤；manifest 發布後晚到的強停不逆轉完成。
設定快照優先、長音檔解碼 timeout 與雙重雜湊檢查詳見正式契約「歷史、辨識結果與取消競態」。
新增 Automated／Mock tests 覆蓋匯入 stop／SIGINT、辨識成功／失敗／取消的歷史、收尾寫入失敗、
manifest 發布競態、來源 symlink／竄改、HTTP 唯讀 response 及前端原稿／辨識結果並存。


### PR #95 第二輪審核修正（A–D）

- A：UI `has_recording` 改為 metadata／路徑／檔案大小的顯示檢查，不開啟來源音檔、不驗內容雜湊；
  完整性由正式辨識入口及引擎把關。session API／SSE 的磁碟讀取移至執行緒。同大小竄改測試改為顯示 true，並保留大小錯誤／symlink／越界／缺檔的 false 測試。
- B：已有主錯誤時，收尾錯誤只附加 session.log，不取代主錯誤；成功後收尾失敗仍回 exit 1。
- C：收尾停用 stop 檔 watcher，但第二次 Ctrl+C 可快速退出並回收本次擁有的 server、鎖及 signal handler。
  process exit 130 不撤銷已決定的結果；完成仍呈現 done，不另發 aborted。語義詳見正式契約。
- D：選擇 close 失敗僅記 log，保留已發布 terminal phase／envelope／exit code。

Automated／Mock tests 使用真實子行程模擬 30 秒收尾阻塞，兩次 SIGINT 後須於 3 秒內退出、保留單一 terminal phase；
另驗證原錯誤優先、close 失敗一致性、UI 不讀取音檔及 API／SSE 讀取執行緒。未變更引擎介面或狀態檔 schema，會議仍為實驗中。


## 里程碑 4：會議寫入 API 與正式控制（2026-10-15）

- MeetingStore 以 CLI 唯讀 `config --work-type meeting --validate-file` 驗證後原子發布設定。GET／POST／PUT、run、diarize 端點與錯誤碼見正式契約。
- 啟動器等到本次 PID 已持鎖並發布 session；配置前失敗讀取獨立 JSON envelope，避免一律回 202。斷線／換頁不停止 CLI。
- UI 提供會議選取／建立／TOML 編輯、必填預計人數、單來源裝置或 staging 匯入、正常停止／強停、雙稿與來源、辨識／取消／重跑。
- 真資料發言者依秒數排序、短發言收折；未知總長只顯示已處理秒數；跨頁提示含類型、session、phase、進度。雙來源選項維持停用。
- `lec stop` 依工作類型說明收尾或取消；強停錄音在 kill 前標記 stopping／abort，避免預期的 ffmpeg -9 成為來源錯誤；即使 reader 同時回傳，也固定為 aborted，不發布可重跑來源。未改轉錄器。
- Automated test／Mock test：`tests.test_ui_backend_meetings` 以真實 CLI 子行程、檔案與 OS 鎖驗設定、400／404／409／422、延遲啟動失敗、同時啟動、stop／force／cancel／retry、staging 清理、保留原稿／舊 generation、speaker_transcript SSE。音訊與辨識引擎皆 mock。
- Static validation：前端 `npm run lint`（TypeScript unused checks）、`npm run typecheck`、`npm run build`；Mock test：前端 `npm test`，涵蓋控制、SSE、10/4 收折、跨頁與停止狀態。
- Manual test（mock 引擎）：瀏覽器啟動錄音、跨頁保持同一 session、正常停止、保存來源、啟動／取消辨識與重跑入口。

完整檢查命令：`python3 -m unittest`；10 項環境性 skip 涵蓋 PulseAudio 真實串流、sherpa 模型、macOS Swift 與原生 Windows process API，不能視為硬體測試通過。

## 里程碑 5：驗收狀態（2026-10-15）

Hardware test（既有證據，由 maintainer 本次任務提供）：CLI 已在 Linux 真機驗錄音、lec stop、stop --force、SIGINT、會後辨識及搶鎖。
本次沒有重做硬體驗收。Not tested：Claude 後續以正式 UI 驗錄音、匯入、跨頁、暫存清理後重跑、長時間錄音與搶鎖；Windows basic 由 maintainer 觸發，macOS／Windows 真機會議流程尚未驗證。
辨識品質優化列 v2.x，不是 v1 接線前提。獨立 review／human final approval／merge 仍依 AGENTS.md，不自動合併。


## v1 UI 整合驗收修正（2026-10-15）

Claude 已在 Linux 真機 main `e9cf85e` 驗過錄音、匯入、停止／強停、辨識／取消／重跑、跨頁及長時間錄音（證據由 maintainer 本次任務提供）。本次只修該次驗收發現的兩個問題，仍待 Claude 真機複驗：

- 會議工作台 grid 欄寬改為 `minmax(0, 1fr)`，避免歷史卡片列的 min-content 把整個工作台撐寬；歷史列沿用自己的水平捲動。
- 匯入將上傳回應的原始 `name` 隨 API 選用 `original_name`、CLI 選用 `--original-name` 傳到 source.json，清理與相容性見正式契約。名稱僅供顯示，不能改變磁碟路徑。
- Automated test／Mock test：CLI 預設 --file basename 不變；自訂名稱、控制字元、255 字元上限、類似路徑的名稱不影響 source_audio；API 上傳 test8min.ogg 後 metadata 與 session 回應保留該名稱，staging 清除後重跑仍保留。
- Static validation／Mock test：CSS 回歸檢查限制外層 grid、保留 history overflow-x:auto；前端測試確認送出 staged path 與原始名稱。
- Manual test（六場 fixture，沒有音訊引擎）：瀏覽器在 1024px／800px 寬度下，工作台寬度分別為 717.3px／511.2px，等於可用容器寬度；歷史列 scrollWidth=1360px、clientWidth=666px／460px，限制在自己的容器中。所有會議卡片與開始辨識按鈕均位於容器內，並可捲動選到第六張卡片。
- Hardware test：本次未執行；Not tested：上述兩項修正的 Linux 真實 UI 複驗由 Claude 接續，Windows basic 仍由 maintainer 觸發。
