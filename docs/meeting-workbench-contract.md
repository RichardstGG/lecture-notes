# 雙工作台正式契約（規格 v1，分階段實作）

現況核對：2026-10-15。本次會議寫入 API 與正式 UI 控制以 main `5054bfd` 為基底。Linux 單一來源為 v1 範圍；本次 UI 真機整合驗收仍待完成，macOS／Windows 維持 beta。

2026-10-08 鎖與狀態層：status／run writer 已升 schema 2，events 維持 schema 1；所有工作共用持有至結束的原子 OS 鎖，舊檔不回寫。會議錄音與辨識編排已於本次 CLI 里程碑接線。詳見 [單工作鎖與狀態相容性](meeting-workbench-lock-status.md)。

2026-10-08 設定與資料層：已加入獨立會議設定載入／驗證、`lec meetings --json`、`lec config --work-type meeting`、work type 快照及排他輸出目錄函式。此設定里程碑未接錄音、辨識編排與 API 控制。詳見 [已實作範圍、CLI schema 與相容性](meeting-workbench-config.md)。

2026-10-06 雙音源增量：已實作設定驗證、能力查詢與安全阻擋，錄音仍未開放；引擎介面、來源 schema 2 與硬體驗收皆明標提案／未測試。詳見 [雙音源契約與 CROSS_AGENT_REQUEST](meeting-workbench-dual-audio.md)。

本文件記錄已實作的基礎契約與完整控制流程。設定查詢、目錄分配、鎖／狀態、引擎及唯讀 UI 已分階段提供；`lec meeting run`、`lec meeting diarize` 已接持久來源與本機引擎，會議寫入 API 已提供。各項現況見文末。**maintainer 2026-10-15 決定**：v1 為 Linux 單一來源的錄音／匯入／會後辨識，CLI 與 Web UI 控制皆可運作；辨識品質優化列 v2.x，不作接線前提；雙來源錄音不在 v1。決策依據為 2026-10-04 Claude Code 的 `CROSS_AGENT_REQUEST` 及使用者回覆「以此請求作為正式依據」。選定 sherpa-onnx 1.13.8、pyannote segmentation 3.0 ONNX、3D-Speaker CAMPPlus 中文英文 ONNX；辨識品質尚待乾淨會議錄音驗證。前端已移除 mock，透過正式 API 控制 CLI 子行程，歷史與雙稿讀取沿用 session API／SSE。

## 範圍與既有行為

- `lecture` 保留現有課堂錄音、匯入、轉錄、摘要、筆記、設定及歷史；`meeting` 只提供錄音、原逐字稿、會後帶代號逐字稿。沒有摘要、決議、待辦、跨會議身分配對。代號僅在單一 session 內有效。
- 目前 `lec run` 以 `course` 啟動，`mode=live|file`；`status.json`、`run.json` writer 為 schema 2，`events.jsonl` 維持 schema 1，reader 相容 schema 1。現有課堂流程的 phase 為 `starting/loading/recording/transcribing/summarizing/finishing/done/failed/aborted`；狀態容器與 UI 已能承載 `diarizing`，會議 CLI 已啟用辨識編排；正式 UI 控制已接入。`transcript.md`、`transcript.srt` 由轉錄器產生；即時錄音只在 `audio.keep_recording=true` 時保存 `recording_HHMMSS.ogg`。UI 的 `Phase` 型別已有 `string` fallback。
- 本契約的 `work_type` 是工作類型；現有 `mode` 保留輸入或操作模式，不改義。讀取舊 `run.json`、`status.json`、session 清單或 `config.used.toml` 缺少 `work_type` 時，正規化為 `lecture`；不回寫舊檔。未知非空 `work_type` 不可猜測為會議，UI 顯示「未知類型」並禁止專屬操作。新檔明寫 `work_type`。

## 設定、路徑與來源保存

設定順序：`config/default.toml` → `config/local.toml` → 工作設定檔 → CLI。課堂仍用 `courses/<id>.toml`；會議改用獨立 `meetings/<id>.toml`，絕不讀同名課程檔。CLI 最後覆寫；`--source` 只覆寫 `audio.source`，`--set` 覆寫指定鍵。會議設定允許 `meeting.name`、`whisper`、`audio`、`vad`、`transcript`、`diarization`；執行時強制 `summary.enabled=false`，即使其他層設定為 true。不得載入 llama-server。`lec config --work-type meeting <id>` 印生效設定。會議設定不得由課堂 UI 編輯器寫入。

已提供的會議設定鍵：

```toml
[meeting]
name = ""                         # 空白時用 meeting id

[paths]
meeting_output_root = "meetings"    # 相對 paths.output_root
meeting_session_name = "{meeting}_{date:%Y%m%d_%H%M%S}"

[diarization]
segmentation_model = "models/sherpa-onnx-pyannote-segmentation-3-0.onnx"
embedding_model = "models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
num_speakers = 0                  # 0 = 未指定；開始會後辨識時必須提供 1..30
cluster_threshold = 0.5
segmentation_window_shift = 0.1  # 秒，須 > 0
```

`num_speakers` 是預計參與人數，不保證同數量的實質發言者。API/CLI 啟動辨識時其有效值必須為 1..30；未提供或 0 回輸入錯誤，不啟用純自動分群。threshold 須在 (0,1]。兩個模型路徑按現有 `Config.path` 規則解析，實際使用的設定寫入 session 的 `config.used.toml`；每次辨識另存 `diarization.request.json`（規格版本、參數、模型路徑及雜湊、來源雜湊），不覆蓋錄音時的設定快照。

會議輸出目錄位於 `paths.output_root/paths.meeting_output_root/<safe meeting name>_<local YYYYMMDD_HHMMSS>`；`meeting_output_root` 必須是無 `..` 的相對路徑，`safe_name` 與現有課堂相同。建立須採原子排他建立，碰撞時追加 `-2`, `-3`，不得重用空目錄或覆寫任何檔案。這使會議仍在 session store 可列舉的 output root 之下。會議 id 是相對 output root 的完整路徑，例如 `meetings/設計會議_20261004_143000`；課堂舊 id 不變。

CLI 已實作以下來源保存契約，UI 匯入沿用相同流程。每個會議 session 必須有可重跑的來源音檔：現場錄音強制保留 `source_audio.ogg`，匯入則在開始處理前複製到同一 session 的 `source_audio.<原副檔名>`，以暫存檔加原子 rename 完成後才開始轉錄；不得只保留 upload staging 或外部絕對路徑。`source.json` schema 1 記錄 `kind=recording|import`、原檔名（僅顯示）、session 相對檔名、byte size、SHA-256、音長秒數與建立時間。匯入檔原名不得成為磁碟路徑。複製失敗時作業失敗且不可開始轉錄；來源不完整或雜湊不符時禁止重跑。清理 UI upload staging 不影響會議重跑。課堂原本的錄音保存行為不變。

## 辨識模組與產物

Claude 引擎層提供同步可呼叫介面 `diarize_session(request, progress, cancel) -> DiarizationResult`（模組所有權見 [CLAUDE.md](../CLAUDE.md)／[CODEX.md](../CODEX.md)）。`request` 含 session 內來源音檔的已驗證絕對路徑、預計人數、兩模型路徑、threshold、window shift、轉錄設定及輸出 staging 目錄；不得以舊 Markdown 的字元位置作對齊。`progress` 收到 `{stage: "segmentation"|"retranscription", processed_seconds, total_seconds, speakers_found}`，秒數單調不減且在 `[0,total]`；`cancel` 在每個可中斷區間被輪詢，正常取消保留原逐字稿。引擎完成 generation 發布後，回傳實際代號數、各代號有聲秒數與最終產物及 manifest 的 session 相對路徑。輸入、模型、解碼、推論、ASR、寫檔錯誤以穩定代碼分類；不吞例外、不產生看似成功的空稿。

`transcript.md` / `transcript.srt` 繼續是錄音或匯入期間的原稿，格式不變。新 `diarization/<generation>/transcript.speakers.md` 為 UTF-8 Obsidian Markdown：首行 `# <meeting name> · 發言者逐字稿`，其後每一發言行為 `[HH:MM:SS.mmm–HH:MM:SS.mmm] S01: 文字`。代號依首次出現順序編為 S01、S02；無法可靠指派時使用 `S00`，不虛構身分。時間來自音訊區間及重新轉錄的實際時間戳，不按字數估算，也不以 OpenCC 後的字元索引貼回原稿。允許繁體中文及英文術語原文。

同一 generation 的 `speakers.json` schema 1 為機器資料：`schema_version`, `source_sha256`, `requested_speakers`, `actual_speakers`, `duration_seconds`, `speakers`（`id`, `speech_seconds`, `segment_count`）, `segments`（`start_ms`, `end_ms`, `speaker_id`, `text`）, `engine`（名稱、版本、模型雜湊、參數）。`start_ms < end_ms`，按開始時間遞增；重疊發言可有交疊區間。`actual_speakers` 計非 S00 且至少一段非空文字的代號數，不強制等於 requested。完整 staging 結果檢查成功後移到新的 generation 目錄，最後以原子 rename 更新 session 根的 `diarization.current.json` schema 1（`generation`, `speaker_transcript`, `speakers` 三個 session 相對路徑）。UI/CLI 只讀 manifest 指定的那一版。取消或失敗保留上一版成功結果，並記錄本次失敗；重跑不可改原稿或來源。

## 生命週期、停止與單一工作鎖

課堂 phase 序列與 stop 檔機制維持現狀。會議錄音／匯入：`starting → loading → recording|transcribing → finishing → done|failed|aborted`；獨立會後辨識：`starting → loading → diarizing → finishing → done|failed|aborted`。`diarizing` 是唯一新 phase；細分 `segmentation|retranscription` 放在狀態欄位 `diarization.stage`，避免擴張 phase 清單。新增 phase 需讓舊 UI 以未知 phase 安全降級；新版 UI 要顯示階段與數值進度。

已實作的課堂 run、summarize 使用 `<state_dir>/run.lock` 的單一原子 OS 鎖，持有 handle 至工作結束；`<state_dir>/run.json` 僅為公開工作 metadata。會議 run、會後辨識已共用同一把鎖。同時間只允許一項；會後辨識中開始錄音回 busy/HTTP 409，不以 `mode` 分鎖。這是使用者明確要求，優先於 Claude 請求中的併行建議。`mode` 繼續為 `live|file|summarize`，辨識用 `diarize`；新增 `work_type`。UI 換頁或斷線不得停止 CLI 子行程。背景工作以 lock/session status 為準，不以頁面 component 狀態為準。

`lec stop` 與 UI stop 仍在鎖所指 session 寫 `stop` / `stop_force`，由工作行程輪詢；不得改 OS signal。正常停止錄音：完成已收集片段、寫原稿、來源與狀態，結束為 `done`（明記 `stop_reason=user`），不自動啟動會後辨識。強制停止：立即終止，狀態 `aborted`，已完成來源片段可保留但 `source.json` 只有檔案完整校驗後才可用於重試。辨識正常取消：在安全點停下，`aborted`、`stop_reason=user`，舊成功產物保留；強制停止：立即 `aborted`，清理 staging 可延後。重試須重新取得同一把鎖、驗證來源與設定，再從頭跑；不宣稱可續跑。重試時若正在執行，回 busy，不刪原有成果。CLI Ctrl+C 須與同一語義對齊。原本僅靠 `run.json` PID 檢查的競態已由 sidecar OS 鎖修正，並有跨行程競跑測試；CLI 停止與取消已有 mock／跨行程契約測試，既有 CLI Linux 真機驗收由 maintainer 確認；本次 UI 真機整合驗收仍待完成。鎖的限制及舊 writer 相容性見 [單工作鎖與狀態相容性](meeting-workbench-lock-status.md)。

## CLI、JSON、API 與相容性

已提供 `lec meetings --json` 及 `lec config --work-type meeting` 唯讀查詢。以下執行命令與其 JSON／退出碼已由 CLI 實作；API 寫入已接線。

CLI：`lec meeting run <id> --speakers N [--file PATH] [--original-name NAME] [--source ID] [--set section.key=value] [--json]`；`lec meeting diarize <session-path> --speakers N [--set diarization.key=value] [--json]`；`lec meetings --json` 列設定。`--speakers` 為本次預計人數，錄音時寫入來源 metadata，重跑可更改。既有 `lec run`、`lec summarize`、`lec config`、`lec status --json`、`lec stop` 語法與既有退出碼不變。新增命令的 `--json` 在工作結束時只輸出一個 `{schema_version:1, accepted:true, work_type:"meeting", operation:"run"|"diarize", session, pid, outcome:"done"|"aborted"}`；參數錯誤 exit 2、忙碌 exit 3、作業錯誤 exit 1、使用者取消 exit 130，錯誤 JSON 用 `{schema_version:1, accepted:false, error:{code,message}}`，stderr 不含私有音訊內容。長工作進度以另一個 `lec status --json` / status 檔查詢；CLI 工作命令同步執行，不持續輸出 JSONL。既有 `lec status --json` 頂層 shape 保留，新增可選 `work_type`；`running:false` 時仍可只有 schema/running。

已實作的 `status.json` schema 2 容器只增加 `work_type`、`stop_reason`、`diarization`。`diarization` 為 null 或 `{stage, processed_seconds, total_seconds, requested_speakers, speakers_found, actual_speakers}`；完成前 `actual_speakers=null`。既有欄位與意義保留。`run.json` schema 2 只增加 `work_type`；`events.jsonl` schema 1 保留既有事件，新增 `diarization_progress` / `diarization_cancelled` 類型與相同數值欄位。讀 schema 1 時缺值依上方 lecture 規則處理；schema 2 讀取器需容忍未知附加欄位。新 reader 不要求所有舊 session 重寫。`config.used.toml` 新增 `[work] type="meeting"` 或 `"lecture"`；舊缺值為 lecture，TOML 既有鍵不改。`meetings/*.toml` 為會議專用，不沿用課程 namespace。

session API／SSE 已提供會議雙稿與 `speaker_transcript` content event；會議設定與執行端點已接線。

API v1 保持既有端點和 shape，新增 `GET /api/v1/meetings`、`POST /api/v1/meetings`、`GET /api/v1/meetings/{id}`、`PUT /api/v1/meetings/{id}`（設定列表、建立、讀取、修改）、`POST /api/v1/meeting-runs`（`meeting_id,speakers,input_file?,source?,original_name?`）、`POST /api/v1/sessions/{id}/diarize`（`speakers` 必填）、既有 `POST /api/v1/runs/stop` 停目前唯一工作。既有 `/api/v1/status`、sessions 列表／細節新增可選 `work_type` 與 `diarization`；meeting 詳細資料再加可選 `speaker_transcript` 與 `speakers` 檔案內容，不刪 `transcript` / `notes`。所有新寫入端點驗證 id、來源檔、參數和鎖；錯誤 envelope 沿用 `{error:{code,message}}`，忙碌 409、錯參數 400、無來源 422、找不到 404。SSE status 繼續傳全快照；session stream 新 target `speaker_transcript` 用 replace/append，舊 target 不變。API version 保持 1，新增可選欄位而非改現有欄位型別。

### 會議寫入 API（2026-10-15）

| 端點 | 請求 | 成功回應 |
| --- | --- | --- |
| `GET /api/v1/meetings` | — | 200，`[{id,file,work_type,name?,num_speakers?,error?}]` |
| `POST /api/v1/meetings` | `{id,content?}`；content 預設空名稱的 meeting 表格 | 200，`{api_version:1,id,file,content}` |
| `GET /api/v1/meetings/{id}` | 可攜單一檔名 | 200，同上 |
| `PUT /api/v1/meetings/{id}` | `{content}`，完整取代該 TOML | 200，同上 |
| `POST /api/v1/meeting-runs` | `{meeting_id,speakers,input_file?,source?,original_name?}` | 202，既有 action envelope 加可選 `work_type`、`session` |
| `POST /api/v1/sessions/{id}/diarize` | `{speakers}` | 202，同上，`operation=diarize` |

`speakers` 必填且只接受整數 1..30，不接受 bool／字串／小數；無效 JSON、未知欄位與錯參數回 400。
找不到設定／session 回 404；已有工作或重複建立設定回 409；缺少來源回 422。
錯誤使用 `{error:{code,message}}`；啟動時來源完整性錯誤也映射 422。
CLI／作業啟動失敗回 502，CLI 不可用或啟動確認逾時回 503。
設定檔限制 1 MiB（超出回 413）。所有新寫入端點檢查本機 Host／Origin 與 JSON content type。
主控服務仍只綁 `127.0.0.1`；獨立內網唯讀分享 app 不掛載這些端點。

設定寫入先產生同目錄隱藏 `.tmp`，以新增的唯讀命令
`lec config --work-type meeting --validate-file PATH` 套用與 `load_meeting` 相同的 sections／未知鍵／值驗證。
此選項不可與 id 同用、不可用於 lecture；成功印出生效 TOML 並回 0，錯誤回 2，不啟動引擎、不建立 session、不寫入 meetings。
通過後建立使用原子 no-replace link（重複建立不覆寫），更新使用原子 replace；失敗清理暫存並保留舊設定。
原有 CLI 語法、exit code 與設定合併規則不變。UI 不 import `core`。

**啟動確認**：API 為每次會議 CLI 分開收集唯一的 stdout envelope，stderr 留在本機 process log。
不以固定 150ms 存活判定成功；輪詢既有 `lec status --json`，只有 `running=true`、已發布 session，且 PID 等於本次子行程或經 OS 行程快照確認為其後代，才回 202。
Windows venv 的 `python.exe` 會另啟動直譯器；後代判斷涵蓋此包裝器，不接受無關 CLI 的 PID。
行程快照查詢失敗時不確認啟動，繼續輪詢至原有 timeout；不更改直譯器、phase、停止機制、CLI 參數／exit code 或 status／run／events schema。
若在 session 配置前退出（即使超過 150ms），先讀該行程的 error envelope：busy→409、input_invalid／exit 2→400、source_missing→422；不會一律 202。
很快結束的成功／取消工作亦回 202，並以 `completed=true,exit_code,session` 表達已完成。
配置 session 後發生的引擎／模型／解碼錯誤屬已接受工作的結果，由 status SSE 與 session 歷史呈現。
等待上限沿用 CLI timeout（預設 30 秒），尚未確認則回 503 `startup_pending`；行程繼續，應先查 status 再重試。
請求取消、換頁、SSE 斷線或 API event loop 關閉都不停止 detached CLI。停止仍只呼叫既有 stop API 寫 stop／stop_force。

匯入先走既有 `/api/v1/audio-uploads`，其 staging path 原樣傳 `--file`，upload 回應的 `name` 以選用 `original_name` 傳給 API，再由 `--original-name=NAME` 傳到 CLI。CLI 複製進 session 後才轉錄，重跑只讀 session 的來源。
`original_name`／`--original-name` 僅在匯入時使用，不搭配 input_file／--file 回 400／exit 2。
API 的原始欄位上限 4096 字元；API 與 CLI 都先移除不可列印字元（含控制字元與方向格式控制）、去除前後空白，再截斷至 255 個 Unicode 字元；清理後為空回 400／exit 2。
此名稱只寫 `source.json.original_name` 供顯示，不用於來源／session 路徑、副檔名判斷、複製或讀取；磁碟來源仍為 `source_audio.<輸入檔副檔名>`。
舊 API 呼叫與直接 CLI `--file PATH` 未提供此欄位／選項時仍沿用 PATH 的 basename。既有 exit code、JSON envelope、API v1、source/status/run/events schema 版本不變；不改寫舊 session 的 metadata。
Session API 新增可選 `source`（有效來源 metadata）與 `speakers`（manifest 指定的結構化結果）；不提供音檔下載。
Session SSE 保留原有 `speaker_transcript` append／replace，來源或發言者資料改變時另發既有 `snapshot`，更新人數／收折列表。
原稿與既有 generation 不被重跑或取消覆蓋，status/run/events 的 schema 版本不變。

## UI 契約

主導航有課堂、會議兩個工作台；歷史與設定依類型隔離顯示，跨頁頂部始終展示目前唯一工作的類型、session、phase、進度與回到工作按鈕。切換頁面只改瀏覽狀態；不呼叫 stop。meeting 錄音／匯入沿用 RunPanel 的互動概念，但表單選會議設定、必填預計參與人數、無摘要模型與筆記欄。錄音中顯示原逐字稿；完成後顯示來源、原稿及帶代號稿，未辨識時顯示明確空狀態與啟動按鈕。會後處理顯示音訊秒數與總秒數、目前 segmentation/retranscription、人數 `要求 N / 已找到 M`、正常取消、強制停止、重試；未知總長時用已處理秒數，不偽造百分比。

發言者列表只呈現實際出現代號，按語音秒數排序。低於總語音 2% 或少於 30 秒的代號預設收折為「其他短發言」，可展開；此為顯示分組，不改 `speakers.json` 或 Markdown。要求 10、實際有實質內容 4 時顯示 `4 位主要發言者` 與其餘已辨識短發言，不畫十個空欄。S00 顯示「未指派」。正式會議頁只讀取真實 session 與產物，沒有 mock 狀態或示範逐字稿。

會議工作台沿用現有內網唯讀分享服務與同一個分享房間限制：固定選定的一場 session、邀請連結／QR code、暱稱與最多 20 位在線、關閉即撤銷。會議訪客只看及下載原逐字稿與 `diarization.current.json` 指向的帶發言者逐字稿；未辨識時第二份顯示等待內容，不顯示課堂筆記。分享中的重新辨識切換 generation 後，最多 2 秒更新快照與版本雜湊。分享不公開音檔、結構化發言區間或其他場次。會議工作台只列 `work_type=meeting` 的真實 session 供選擇；mock 示範資料不可分享。詳細 API 見 `ui/SHARING.md`。

## 驗收與實作分工

以下為完整功能的驗收要求，並非目前全數完成的驗證結果；已完成範圍見各里程碑文件。

自動 contract tests 須涵蓋：舊缺值判 lecture、設定合併順序與獨立 namespace、排他目錄、來源複製與雜湊、schema 1→2 reader、CLI JSON/exit code、API 400/404/409/422、phase 序列、兩工作並發只一個成功、stop/force/cancel/retry、不覆蓋舊產物、UI 10/4 收折與跨頁進度。UI lint/type/build 及後端/stdlib 測試須全過。Mock tests 模擬長時進度、取消安全點、解碼與模型錯誤。Manual tests 用乾淨音檔檢查原稿不變、暫存清理後可重跑、UI 切頁不中斷、Obsidian 顯示。Hardware tests 在 Linux 真機錄 10 人中文夾英文會議，檢查時間戳與代號、耗時、記憶體、正常/強制停止、重試；macOS/Windows 需各自真機驗證才可聲稱支援。辨識品質優化列為 v2.x，不是 v1 接線門檻；不能以受損的 87.7 分鐘錄音作準確度通過依據。

Codex 實作順序：契約與 UI mock → `core/config.py` / `core/cli.py` / `core/status.py` / `core/session.py` + contract tests → UI backend schema/API/store/control → 真資料 UI。Claude 實作順序：乾淨樣本品質驗證 → 引擎封裝及時間戳產物 → 與 Codex 定義的介面整合；品質優化列 v2.x，不阻擋正式 UI 接線。檔案所有權、共用檔案規則與測試分工以 [CLAUDE.md](../CLAUDE.md)／[CODEX.md](../CODEX.md) 為準；跨所有權變更走 CROSS_AGENT_REQUEST，跨層整合測試依被斷言的契約分工。

## 現況差距與待決事項

目前 main 已提供會議設定查詢／驗證、排他目錄分配、status／run schema 2、原子工作鎖，以及可直接呼叫的辨識引擎與 generation 輸出。本次提供 `lec meeting run`／`lec meeting diarize`、會議持久來源產生流程與 `diarization.request.json`；錄音／辨識寫入 API 與正式控制 UI 已接線，剩餘 Linux UI 真機整合驗收。session API、前端歷史與分享 reader 已能依真實會議 session 的 `work_type` 與 generation manifest 讀取兩份逐字稿；session SSE 能在帶代號稿新增或切換 generation 時傳 `speaker_transcript` content event。正式會議來源可由 CLI 生成，mock 示範資料不能分享。Claude 所述模型組合已被採用為實作候選，尚需乾淨樣本品質驗收；模型檔再散佈方式與各平台安裝驗證由 maintainer 決定。實作前若變更本契約須同步更新文件與 contract tests。


## CLI 落地格式與操作（實驗中）

先在 `meetings/<id>.toml` 建立會議設定，可參考 `meetings/examples/template.toml`：

```sh
lec meeting run team --speakers 3 --file samples/test8min.ogg --json
lec meeting run team --speakers 3 --source default --json
lec meeting diarize outputs/meetings/team_20261014_143000 --speakers 3 --json
```

`--speakers` 必填且為 1..30 的整數，優先於 `--set diarization.num_speakers`。
run 的 `--set` 沿用會議設定驗證；diarize 僅允許 `diarization.*`，沿用錄音時快照的會議、轉錄與辨識設定，
再套本次覆寫。錄音後修改 `local.toml` 的模型路徑或閾值不覆蓋 session 快照；要更換請用 `--set diarization.<key>=<value>`，`meeting diarize --help` 也列此規則。鎖的位置採目前 default/local 設定的 `paths.state_dir`，確保 `lec status`／`lec stop` 查到同一項工作。
`audio.capture_mode=dual` 的錄音入口仍拒絕，能力查詢不變。課堂指令與課堂來源保存行為不變。

`source.json` schema 1 的實際欄位：`kind`、`original_name`（只供顯示）、`path`（session 內的 `source_audio.<ext>`）、
`size_bytes`、`sha256`（小寫十六進位）、`duration_seconds`、`created_at`（含時區 ISO 8601）、`requested_speakers`。
匯入先以暫存檔和原子 rename 複製，完整解碼檢查後發布 metadata，再啟動轉錄。錄音先通過 capture integrity，
再 rename 為 `source_audio.ogg`，完整解碼與雜湊完成後才發布 metadata；未能驗證的強停錄音不發布 `source.json`。
完整性未知或掉音訊達引擎錯誤門檻會失敗（`source_incomplete`）。完整解碼的 timeout 為 `max(120, duration_seconds * 2)` 秒，隨音長增加，不以固定 120 秒限制長會議。匯入已保存完整來源時，即使轉錄失敗／取消，來源仍可重跑。

`diarization.request.json` 使用 `schema_version=1`、`spec_version=1`、`created_at`，以及 `DiarizationRequest` 的實際欄位：
`session_dir`、`source_audio`、`source_sha256`、`meeting_name`、`requested_speakers`、兩個模型絕對路徑、
`cluster_threshold`、`segmentation_window_shift`、`staging_dir`、`whisper_server`、`whisper_prompt`、`threads`、
`merge_gap`、`pad`、`min_turn_seconds`、`opencc`、`python`、`unassigned_confidence_floor`，另存
`segmentation_model_sha256`、`embedding_model_sha256`。路徑為 JSON 字串，未指定可選值為 null；不覆寫 `config.used.toml`。
本次沒有新增品質門檻設定鍵；`min_turn_seconds=0.25`、`unassigned_confidence_floor=null` 仍用引擎預設，待品質驗收決定。

JSON 工作開始且正常完成時 exit 0、`accepted:true,outcome:"done"`；接受後取消／強停 exit 130、
`accepted:true,outcome:"aborted"`（接受的是作業，outcome 表示結果）；尚未配置 session 就取消則 `accepted:false,error.code="cancelled"`。參數／設定錯誤 exit 2、忙碌 exit 3、
作業失敗 exit 1，均使用 `accepted:false,error:{code,message}`。引擎錯誤 code 保留在 error 和 status `last_error`。
JSON 模式只輸出一個 envelope，不把 ASR 文字寫入 stdout/stderr；完整逐字稿及本機 session log 留在 session。
Ctrl+C 與 stop 使用相同語義：正常停止現場錄音完成原稿與來源，exit 0；匯入轉錄丟棄剩餘內容時則為 aborted／exit 130，已驗證來源仍保留。辨識取消 exit 130 並保留上一個 generation。
辨識強停會立即發布 aborted 並關閉由本次作業擁有的 WhisperServer；分群階段由取消輪詢終止／回收 worker 後釋放鎖，重新轉錄階段直接終止 CLI。
不得只退出 CLI 而留下推論行程。staging 清理依引擎取消安全點完成，非續跑。


### 歷史、辨識結果與取消競態

辨識作業建立新的 status writer 時，沿用原 session 的 `started_at`、`elapsed`、`transcribed`、`transcribe_lag`、
`sections_total`、`sections_summarized`、`input_file`，保留錄音／匯入的時間和原稿指標。
`status.json` 的 `mode`、`phase`、`stop_reason`、`last_error`、`diarization` 仍描述最近一次工作，
所以 `lec status --json`／status SSE 在辨識期間仍回報 `mode=diarize`、`diarizing` 及進度。
本次未新增 status/run/events 欄位或事件種類，schema 2／2／1 不變；當次作業開始時間仍由 `run.json.started_at` 記錄。

會議的唯讀 session API 不再以辨識作業的 terminal phase 判定原稿是否完成：`mode=diarize` 時，
歷史摘要的 `phase` 取既有 `events.jsonl` 中原錄音／匯入第一個 terminal phase，`mode` 取
`source.json.kind` 對應的 live/file；原稿已取消仍顯示 aborted，不因來源與部分原稿存在就猜成 done。
缺少原工作 phase 事件時以 null 表示未知，不猜完成。會議的 `has_recording` 是顯示用可用性旗標：
只驗證 `source.json` schema（含 sha256 字串格式）、拒絕 metadata／音檔 symlink 與越界路徑、確認檔案存在及 `st_size` 相符。
UI 不讀取音檔內容、不計算雜湊；同大小內容竄改仍可能回 true，不保證來源內容完整。
來源完整性以 `meeting diarize` 的 `verify_meeting_source()` 與引擎 `_validate` 為準，兩者的 SHA-256 檢查維持不變。
課堂仍沿用 `recording_*.ogg` 規則。async session API 與 SSE 的檔案讀取使用執行緒，避免磁碟 I/O 阻塞 event loop。

API version 1 的會議摘要新增可選 `diarization_status={phase,stop_reason,last_error,diarization}`，
取自既有 status 欄位，讓歷史同時顯示原稿完成狀態與最近辨識完成／失敗／取消。課堂及尚未辨識的 session 不帶此欄位；
舊 reader 可忽略附加欄位。正式 UI 寫入 API／控制已啟用，歷史同時顯示原稿與最近辨識結果。

辨識 manifest 的原子切換是產物發布點：已切換 `diarization.current.json` 或引擎已成功返回後，
一般晚到的 Ctrl+C／stop_force 不再把完成結果改成 aborted，成功作業維持 done／exit 0；收尾期間二次 Ctrl+C 的 process exit 特例見下段。
呼叫端比較本次執行前後的 manifest，涵蓋原子切換至成功返回之間的短暫區間；發布前取消仍沿既有 aborted 語義。
編排層 `verify_meeting_source` 與引擎 `_validate` 各自計算 SHA-256 是刻意保留的兩道邊界檢查，
後者再次偵測驗證到推論之間的來源變更，未修改引擎介面。

收尾的 status 寫入與 cleanup 各自防護，原本成功而收尾失敗時仍回作業錯誤 exit 1；已有錯誤代碼時保留原錯誤，
後續收尾例外僅附加到本機 `session.log`，不覆寫 envelope 或 status 的主錯誤。
採用 close 規則 D(a)：terminal phase 已發布後，`status.close()` 失敗僅記 session.log，保持已發布 outcome 與 exit code。
鎖釋放與 signal 還原不依賴前面的 status 寫入成功。

收尾期間維持停止 stop 檔 watcher，不再處理 stop／stop_force 檔，避免與正常收尾競爭發布 terminal phase。
Ctrl+C 仍計數：第一次可讓正常收尾繼續，第二次立即 kill 本次擁有的 server、釋放鎖並還原 signal，以 130 結束 CLI。
這是強制結束「收尾行程」，不撤銷已決定的作業結果：原 outcome 為 done 或辨識 manifest 已發布時，
status／envelope 仍為 done（`accepted:true,outcome:"done"`），process exit 為 130，不新增 aborted 或取消事件。
已有 failed／aborted terminal phase 同樣保留；failed 仍回既有錯誤 envelope，另以 process exit 130 表示收尾受中斷。
此特例不改變第一輪的匯入取消、現場正常停止、辨識欄位保留與 manifest 發布規則。
CLI 僅在參數／設定載入階段把欄位型別錯誤歸類為 input_invalid，工作中的非預期錯誤不回 exit 2。
設定錯誤 message 只列出可辨識的鍵名，不回吐設定值、音訊文字或憑證。
