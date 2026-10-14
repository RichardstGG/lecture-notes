# 雙工作台開發規格與交付順序

2026-10-08 鎖與狀態層：status／run writer 已升 schema 2，events 維持 schema 1；所有工作共用持有至結束的原子 OS 鎖，舊檔不回寫。會議錄音與辨識編排已於本次 CLI 里程碑接線。詳見 [單工作鎖與狀態相容性](meeting-workbench-lock-status.md)。

2026-10-08 設定與資料層：已加入獨立會議設定載入／驗證、`lec meetings --json`、`lec config --work-type meeting`、work type 快照及排他輸出目錄函式。此設定里程碑未接錄音、辨識編排與 API 控制。詳見 [已實作範圍、CLI schema 與相容性](meeting-workbench-config.md)。

2026-10-06 雙音源增量：已實作設定驗證、能力查詢與安全阻擋，錄音仍未開放；引擎介面、來源 schema 2 與硬體驗收皆明標提案／未測試。詳見 [雙音源契約與 CROSS_AGENT_REQUEST](meeting-workbench-dual-audio.md)。

以 [正式契約](meeting-workbench-contract.md) 為準。本文件列實作切點；目前已完成契約、CLI 編排／持久來源／引擎接線、前端 mock 預覽，以及以現有 session API 讀取真實會議歷史與兩份逐字稿。不得把 mock 預覽當成正式 UI 錄音或辨識控制。CLI 會議功能維持實驗中。

目前前端採分組導航：會議工作台使用淡藍色主題，歷史紀錄與內網共享子選單捲動到既有區塊，會議設定指向「開始一場會議」預覽區塊；本機設定為獨立頁面。切換頁面保留預覽表單與狀態，不啟停背景工作或關閉分享。本次導航調整未新增會議設定持久化、會議摘要選單或引擎。課堂總結方式沿用 PR #53 的上游選取契約；詳見 [工作台 UI 契約](../ui/WORKBENCH.md)。

## 現有 source 對照

| 現有位置 | 已有行為 | 必要變動及驗證 |
| --- | --- | --- |
| `core/config.py`、`config/default.toml` | 已提供 work type、獨立 meetings 設定與 diarization 驗證；課堂 `load()` 行為保留 | 執行編排沿用已驗證的設定；保持同名課堂／會議隔離 |
| `core/cli.py` | 課堂與會議工作同步執行；會議列表、設定查詢與 JSON/exit contract 已提供 | 正式 UI 控制待品質驗收；沒有引擎時回明確失敗 |
| `core/session.py` | 會議跳過 LLM、保存來源、編排會後辨識；沿用 stop 檔 | 正常停／強停已有契約測試，真實錄音硬體驗收待完成 |
| `core/status.py` | status/run schema 2、event schema 1；持有整段工作的 OS 鎖，四行程競跑測試 | meeting run/diarize 已共用；實際硬體取消仍待驗收 |
| `core/transcribe.py` | ffmpeg 16 kHz mono、VAD、whisper、原稿/SRT；`keep_recording` 可關 | CLI 編排持久來源；引擎按音訊時間戳重新轉錄，禁止字數或 OpenCC 字元位置對齊 |
| `core/diarize.py`（新）、`core/diarize_worker.py`（新） | 已實作 `diarize_session`；詳見下方「引擎層現況」 | CLI 執行／設定映射／狀態檔已由 Codex 接線；品質門檻值待乾淨樣本 |
| `core/servers.py` | whisper/llama server | CLI 已沿用 WhisperServer 管理辨識期生命週期，課堂流程不變 |
| `ui/backend/{app,schemas,session_store,process_control,upload_store}.py` | 課堂 API、upload staging、schema 1 reader 相容、會議雙稿唯讀與 SSE | 待加會議設定 store、執行 API 與來源保存前置處理 |
| `ui/frontend/src/{App.tsx,types.ts}` | 雙工作台導航、跨頁提示、會議歷史／雙稿與進度讀取；控制仍為預覽 | 待以正式執行 API 取代錄音／辨識 mock；Phase 保留 string 降級 |

## 切分為可 review 的里程碑

1. **已完成：契約、mock 前端及唯讀分享銜接。** 新增規格與 meeting 預覽元件；示範狀態控制只改 React 本地狀態，錄音按鈕 disabled。既有內網分享可選擇真實 `work_type=meeting` session，訪客讀取原逐字稿及目前 generation 的帶代號稿。課堂分享、CLI、設定、錄音與辨識流程不改。測 10/4 折疊、進度、兩類分享文件及下載隔離。
2. **Codex 契約底層：CLI 編排完成。** work type、會議設定查詢、目錄命名、status schema 2、原子鎖、來源 metadata 與會議執行 CLI 已提供。使用假辨識器驗證 error/cancel/retry 與原子輸出；真實品質仍待驗收。檔案所有權與跨界變更遵循 [CLAUDE.md](../CLAUDE.md)／[CODEX.md](../CODEX.md)；文件和 contract tests 與程式同 PR。
3. **Claude 引擎已封裝，品質驗收待完成。** 已提供 `diarize_session` 封裝與時間戳產物；仍須以乾淨的約十人中文加英文術語錄音驗證 diarization。原始受損錄音的 20.2% 遺失不能作準確度通過標準。模型缺失、錯誤、取消均要可測；不得依賴 torch、numpy、網路服務。
4. **Codex API 與真資料 UI：唯讀完成，控制待接線。** 現有 session store 已能讀雙稿；前端歷史與結果現讀真實 meeting session，session SSE 在帶代號稿新增或切換 generation 時發 `speaker_transcript` content event，status schema 2 的會後階段進度可顯示在會議頁與跨頁提示。底層執行 CLI 與來源保存已落地；會議設定、錄音／匯入、辨識、重跑與取消 API 仍待品質驗收後接線。移除其 mock 前須有對應 API contract tests；UI 不 import `core`。
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
**已由 CLI 編排執行，正式 UI 控制尚未接入**；CLI 啟動／收尾 WhisperServer、保存來源與請求紀錄，並映射進度、錯誤及取消。**辨識準確度尚未驗證**，
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
故延後到品質確認之後。

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
- 正式 UI 寫入 API 與控制沒有啟用。品質門檻、真實辨識準確度、Linux／Mac／Windows 錄音硬體驗收仍待 Claude 與 maintainer 驗證。
