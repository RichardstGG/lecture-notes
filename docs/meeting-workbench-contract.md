# 雙工作台正式契約（規格 v1，分階段實作）

2026-10-09 單來源錄音與匯入：`lec meeting run` 已接上原稿與持久來源，強制保留錄音、停用總結，並處理 stop／force、來源雜湊與擷取遺失。辨識編排與 meeting API／UI 控制仍待後續里程碑。詳見 [CLI、source.json 與驗證限制](meeting-workbench-run.md)。

2026-10-08 鎖與狀態層：status／run writer 已升 schema 2，events 維持 schema 1；所有工作共用持有至結束的原子 OS 鎖，舊檔不回寫。會議錄音與辨識編排尚未接線。詳見 [單工作鎖與狀態相容性](meeting-workbench-lock-status.md)。

2026-10-08 設定與資料層：已加入獨立會議設定載入／驗證、`lec meetings --json`、`lec config --work-type meeting`、work type 快照及排他輸出目錄函式。此設定里程碑未接錄音、辨識編排與 API 控制。詳見 [已實作範圍、CLI schema 與相容性](meeting-workbench-config.md)。

2026-10-06 雙音源增量：已實作設定驗證、能力查詢與安全阻擋，錄音仍未開放；引擎介面、來源 schema 2 與硬體驗收皆明標提案／未測試。詳見 [雙音源契約與 CROSS_AGENT_REQUEST](meeting-workbench-dual-audio.md)。

本文件是後續實作的目標契約，不表示會議 CLI、錄音 API 或引擎目前已提供這些功能。決策依據為 2026-10-04 Claude Code 的 `CROSS_AGENT_REQUEST` 及使用者回覆「以此請求作為正式依據」。選定 sherpa-onnx 1.13.8、pyannote segmentation 3.0 ONNX、3D-Speaker CAMPPlus 中文英文 ONNX；辨識品質尚待乾淨會議錄音驗證。目前前端保留 mock 錄音／辨識預覽，真實會議歷史與雙稿讀取使用既有 session API，不接真引擎。

## 範圍與既有行為

- `lecture` 保留現有課堂錄音、匯入、轉錄、摘要、筆記、設定及歷史；`meeting` 只提供錄音、原逐字稿、會後帶代號逐字稿。沒有摘要、決議、待辦、跨會議身分配對。代號僅在單一 session 內有效。
- 目前 `lec run` 以 `course` 啟動，`mode=live|file`；`status.json`、`run.json` writer 為 schema 2，`events.jsonl` 維持 schema 1，reader 相容 schema 1。現有 phase 為 `starting/loading/recording/transcribing/summarizing/finishing/done/failed/aborted`。`transcript.md`、`transcript.srt` 由轉錄器產生；即時錄音只在 `audio.keep_recording=true` 時保存 `recording_HHMMSS.ogg`。UI 的 `Phase` 型別已有 `string` fallback。
- 本契約的 `work_type` 是工作類型；現有 `mode` 保留輸入或操作模式，不改義。讀取舊 `run.json`、`status.json`、session 清單或 `config.used.toml` 缺少 `work_type` 時，正規化為 `lecture`；不回寫舊檔。未知非空 `work_type` 不可猜測為會議，UI 顯示「未知類型」並禁止專屬操作。新檔明寫 `work_type`。

## 設定、路徑與來源保存

設定順序：`config/default.toml` → `config/local.toml` → 工作設定檔 → CLI。課堂仍用 `courses/<id>.toml`；會議改用獨立 `meetings/<id>.toml`，絕不讀同名課程檔。CLI 最後覆寫；`--source` 只覆寫 `audio.source`，`--set` 覆寫指定鍵。會議設定允許 `meeting.name`、`whisper`、`audio`、`vad`、`transcript`、`diarization`；執行時強制 `summary.enabled=false`，即使其他層設定為 true。不得載入 llama-server。`lec config --work-type meeting <id>` 印生效設定。會議設定不得由課堂 UI 編輯器寫入。

目標新增鍵：

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

每個會議 session 必須有可重跑的來源音檔：現場錄音強制保留 `source_audio.ogg`，匯入則在開始處理前複製到同一 session 的 `source_audio.<原副檔名>`，以暫存檔加原子 rename 完成後才開始轉錄；不得只保留 upload staging 或外部絕對路徑。`source.json` schema 1 記錄 `kind=recording|import`、原檔名（僅顯示）、session 相對檔名、byte size、SHA-256、音長秒數與建立時間。匯入檔原名不得成為磁碟路徑。複製失敗時作業失敗且不可開始轉錄；來源不完整或雜湊不符時禁止重跑。清理 UI upload staging 不影響會議重跑。課堂原本的錄音保存行為不變。

## 辨識模組與產物

Claude 引擎層提供同步可呼叫介面 `diarize_session(request, progress, cancel) -> DiarizationResult`（模組所有權見 [CLAUDE.md](../CLAUDE.md)／[CODEX.md](../CODEX.md)）。`request` 含 session 內來源音檔的已驗證絕對路徑、預計人數、兩模型路徑、threshold、window shift、轉錄設定及輸出 staging 目錄；不得以舊 Markdown 的字元位置作對齊。`progress` 收到 `{stage: "segmentation"|"retranscription", processed_seconds, total_seconds, speakers_found}`，秒數單調不減且在 `[0,total]`；`cancel` 在每個可中斷區間被輪詢，正常取消保留原逐字稿。模組回傳實際代號數、各代號有聲秒數與暫存產物路徑。輸入、模型、解碼、推論、ASR、寫檔錯誤以穩定代碼分類；不吞例外、不產生看似成功的空稿。

`transcript.md` / `transcript.srt` 繼續是錄音或匯入期間的原稿，格式不變。新 `diarization/<generation>/transcript.speakers.md` 為 UTF-8 Obsidian Markdown：首行 `# <meeting name> · 發言者逐字稿`，其後每一發言行為 `[HH:MM:SS.mmm–HH:MM:SS.mmm] S01: 文字`。代號依首次出現順序編為 S01、S02；無法可靠指派時使用 `S00`，不虛構身分。時間來自音訊區間及重新轉錄的實際時間戳，不按字數估算，也不以 OpenCC 後的字元索引貼回原稿。允許繁體中文及英文術語原文。

同一 generation 的 `speakers.json` schema 1 為機器資料：`schema_version`, `source_sha256`, `requested_speakers`, `actual_speakers`, `duration_seconds`, `speakers`（`id`, `speech_seconds`, `segment_count`）, `segments`（`start_ms`, `end_ms`, `speaker_id`, `text`）, `engine`（名稱、版本、模型雜湊、參數）。`start_ms < end_ms`，按開始時間遞增；重疊發言可有交疊區間。`actual_speakers` 計非 S00 且至少一段非空文字的代號數，不強制等於 requested。完整 staging 結果檢查成功後移到新的 generation 目錄，最後以原子 rename 更新 session 根的 `diarization.current.json` schema 1（`generation`, `speaker_transcript`, `speakers` 三個 session 相對路徑）。UI/CLI 只讀 manifest 指定的那一版。取消或失敗保留上一版成功結果，並記錄本次失敗；重跑不可改原稿或來源。

## 生命週期、停止與單一工作鎖

課堂 phase 序列與 stop 檔機制維持現狀。會議錄音／匯入：`starting → loading → recording|transcribing → finishing → done|failed|aborted`；獨立會後辨識：`starting → loading → diarizing → finishing → done|failed|aborted`。`diarizing` 是唯一新 phase；細分 `segmentation|retranscription` 放在狀態欄位 `diarization.stage`，避免擴張 phase 清單。新增 phase 需讓舊 UI 以未知 phase 安全降級；新版 UI 要顯示階段與數值進度。

所有課堂 run、summarize、會議 run、會後辨識共用現有 `<state_dir>/run.json` 的單一鎖。同時間只允許一項；會後辨識中開始錄音回 busy/HTTP 409，不以 `mode` 分鎖。這是使用者明確要求，優先於 Claude 請求中的併行建議。`mode` 繼續為 `live|file|summarize`，辨識用 `diarize`；新增 `work_type`。UI 換頁或斷線不得停止 CLI 子行程。背景工作以 lock/session status 為準，不以頁面 component 狀態為準。

`lec stop` 與 UI stop 仍在鎖所指 session 寫 `stop` / `stop_force`，由工作行程輪詢；不得改 OS signal。正常停止錄音：完成已收集片段、寫原稿、來源與狀態，結束為 `done`（明記 `stop_reason=user`），不自動啟動會後辨識。強制停止：立即終止，狀態 `aborted`，已完成來源片段可保留但 `source.json` 只有檔案完整校驗後才可用於重試。辨識正常取消：在安全點停下，`aborted`、`stop_reason=user`，舊成功產物保留；強制停止：立即 `aborted`，清理 staging 可延後。重試須重新取得同一把鎖、驗證來源與設定，再從頭跑；不宣稱可續跑。重試時若正在執行，回 busy，不刪原有成果。CLI Ctrl+C 須與同一語義對齊。鎖已由持續存在的 `run.lock` OS 鎖保護，`run.json` 是公開 metadata；跨行程競跑只允許一個 owner。

## CLI、JSON、API 與相容性

目標 CLI：`lec meeting run <id> --speakers N [--file PATH] [--source ID] [--set section.key=value] [--json]`；`lec meeting diarize <session-path> --speakers N [--set diarization.key=value] [--json]`；`lec meetings --json` 列設定。`--speakers` 為本次預計人數，錄音時寫入來源 metadata，重跑可更改。既有 `lec run`、`lec summarize`、`lec config`、`lec status --json`、`lec stop` 語法與既有退出碼不變。新增命令的 `--json` 在工作結束時只輸出一個 `{schema_version:1, accepted:true, work_type:"meeting", operation:"run"|"diarize", session, pid, outcome:"done"|"aborted"}`；參數錯誤 exit 2、忙碌 exit 3、作業錯誤 exit 1、使用者取消 exit 130，錯誤 JSON 用 `{schema_version:1, accepted:false, error:{code,message}}`，stderr 不含私有音訊內容。長工作進度以另一個 `lec status --json` / status 檔查詢；CLI 工作命令同步執行，不持續輸出 JSONL。既有 `lec status --json` 頂層 shape 保留，新增可選 `work_type`；`running:false` 時仍可只有 schema/running。

新 `status.json` schema 2 只增加 `work_type`、`stop_reason`、`diarization`。`diarization` 為 null 或 `{stage, processed_seconds, total_seconds, requested_speakers, speakers_found, actual_speakers}`；完成前 `actual_speakers=null`。既有欄位與意義保留。`run.json` schema 2 只增加 `work_type`；`events.jsonl` schema 1 保留既有事件，新增 `diarization_progress` / `diarization_cancelled` 類型與相同數值欄位。讀 schema 1 時缺值依上方 lecture 規則處理；schema 2 讀取器需容忍未知附加欄位。新 reader 不要求所有舊 session 重寫。`config.used.toml` 新增 `[work] type="meeting"` 或 `"lecture"`；舊缺值為 lecture，TOML 既有鍵不改。`meetings/*.toml` 為會議專用，不沿用課程 namespace。

目標 API v1 保持既有端點和 shape，新增 `GET /api/v1/meetings`、`POST /api/v1/meetings`、`GET /api/v1/meetings/{id}`、`PUT /api/v1/meetings/{id}`（設定列表、建立、讀取、修改）、`POST /api/v1/meeting-runs`（`meeting_id,speakers,input_file?,source?`）、`POST /api/v1/sessions/{id}/diarize`（`speakers` 必填）、既有 `POST /api/v1/runs/stop` 停目前唯一工作。既有 `/api/v1/status`、sessions 列表／細節新增可選 `work_type` 與 `diarization`；meeting 詳細資料再加可選 `speaker_transcript` 與 `speakers` 檔案內容，不刪 `transcript` / `notes`。所有新寫入端點驗證 id、來源檔、參數和鎖；錯誤 envelope 沿用 `{error:{code,message}}`，忙碌 409、錯參數 400、無來源 422、找不到 404。SSE status 繼續傳全快照；session stream 新 target `speaker_transcript` 用 replace/append，舊 target 不變。API version 保持 1，新增可選欄位而非改現有欄位型別。

## UI 契約

主導航有課堂、會議兩個工作台；歷史與設定依類型隔離顯示，跨頁頂部始終展示目前唯一工作的類型、session、phase、進度與回到工作按鈕。切換頁面只改瀏覽狀態；不呼叫 stop。meeting 錄音／匯入沿用 RunPanel 的互動概念，但表單選會議設定、必填預計參與人數、無摘要模型與筆記欄。錄音中顯示原逐字稿；完成後顯示來源、原稿及帶代號稿，未辨識時顯示明確空狀態與啟動按鈕。會後處理顯示音訊秒數與總秒數、目前 segmentation/retranscription、人數 `要求 N / 已找到 M`、正常取消、強制停止、重試；未知總長時用已處理秒數，不偽造百分比。

發言者列表只呈現實際出現代號，按語音秒數排序。低於總語音 2% 或少於 30 秒的代號預設收折為「其他短發言」，可展開；此為顯示分組，不改 `speakers.json` 或 Markdown。要求 10、實際有實質內容 4 時顯示 `4 位主要發言者` 與其餘已辨識短發言，不畫十個空欄。S00 顯示「未指派」。頁面清楚區分 mock 預覽與真實 session；stub 不呼叫未實作端點，不寫正式資料。

會議工作台沿用現有內網唯讀分享服務與同一個分享房間限制：固定選定的一場 session、邀請連結／QR code、暱稱與最多 20 位在線、關閉即撤銷。會議訪客只看及下載原逐字稿與 `diarization.current.json` 指向的帶發言者逐字稿；未辨識時第二份顯示等待內容，不顯示課堂筆記。分享中的重新辨識切換 generation 後，最多 2 秒更新快照與版本雜湊。分享不公開音檔、結構化發言區間或其他場次。會議工作台只列 `work_type=meeting` 的真實 session 供選擇；mock 示範資料不可分享。詳細 API 見 `ui/SHARING.md`。

## 驗收與實作分工

自動 contract tests：舊缺值判 lecture、設定合併順序與獨立 namespace、排他目錄、來源複製與雜湊、schema 1→2 reader、CLI JSON/exit code、API 400/404/409/422、phase 序列、兩工作並發只一個成功、stop/force/cancel/retry、不覆蓋舊產物、UI 10/4 收折與跨頁進度。UI lint/type/build 及後端/stdlib 測試全過。Mock tests 模擬長時進度、取消安全點、解碼與模型錯誤。Manual tests 用乾淨音檔檢查原稿不變、暫存清理後可重跑、UI 切頁不中斷、Obsidian 顯示。Hardware tests 在 Linux 真機錄 10 人中文夾英文會議，檢查時間戳與代號、耗時、記憶體、正常/強制停止、重試；macOS/Windows 需各自真機驗證才可聲稱支援。真實辨識準確度仍是未驗證門檻，不能以受損的 87.7 分鐘錄音作通過依據。

Codex 實作順序：契約與 UI mock → `core/config.py` / `core/cli.py` / `core/status.py` / `core/session.py` + contract tests → UI backend schema/API/store/control → 真資料 UI。Claude 實作順序：乾淨樣本品質驗證 → 引擎封裝及時間戳產物 → 與 Codex 定義的介面整合；不得在品質驗證前接入正式 UI。檔案所有權、共用檔案規則與測試分工以 [CLAUDE.md](../CLAUDE.md)／[CODEX.md](../CODEX.md) 為準；跨所有權變更走 CROSS_AGENT_REQUEST，跨層整合測試依被斷言的契約分工。

## 現況差距與待決事項

已實作 meeting run CLI、單來源保存、status/run schema 2 與原子鎖；尚無 meeting 錄音 API、辨識編排或真實錄音 UI。session API、前端歷史與分享 reader 已能依真實會議 session 的 `work_type` 與 generation manifest 讀取兩份逐字稿；session SSE 能在帶代號稿新增或切換 generation 時傳 `speaker_transcript` content event。mock 示範資料不能分享。Claude 所述模型組合已被採用為實作候選，尚需乾淨樣本品質驗收；模型檔再散佈方式與各平台安裝驗證由 maintainer 決定。實作前若變更本契約須同步更新文件與 contract tests。
