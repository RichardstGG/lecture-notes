# 會議設定與資料層（2026-10-08）

此里程碑完成設定與目錄契約，**沒有啟用會議錄音、匯入、辨識或真資料控制 UI**。會議功能仍實驗中。
基底為 `origin/main` 的 `de63393`。Claude 的 `diarize_session`、`plan_dual`、多路 Transcriber 與 `verify_capture` 已存在；不重寫它們。

## 已實作的入口

- `lec meetings [--json]`：唯讀列出 `meetings/*.toml`，不列 `examples/`，不探測音訊、不建立設定或 session。
- `lec config --work-type meeting [id] [--source ID] [--set section.key=value]`：印出合併 TOML。缺少 id 時只查 default/local；指定不存在或不合法 id 回 exit 2，stdout 為空、stderr 為可讀錯誤。`--model` / `--upstream` 是課堂摘要選項，meeting 查詢回 exit 2。
- `lec config [id]` 預設仍是 lecture；新增 `--work-type lecture` 為顯式同義選項。既有課堂指令及退出碼不變。

`lec meetings --json` 成功 exit 0，只輸出一份版本化 JSON：

```json
{"schema_version":1,"meetings":[{"id":"design","file":"/repo/meetings/design.toml","work_type":"meeting","name":"設計會議","num_speakers":10}]}
```

每個損壞／不可讀檔案獨立列 `{id,file,work_type,error}`，不包含成功欄位 `name` / `num_speakers`，也不遮蔽其他正常設定。空目錄回 `meetings:[]`。此為新命令的 shape，未改既有 `lec courses --json` 的陣列。

## 設定與隔離

`core.config.load_meeting(id=None, *, sets=(), source=None) -> Config`：default → local → meetings/id.toml → CLI，再強制 `[work] type="meeting"` 及 `summary.enabled=false`。不讀同名課堂檔，不自行建立缺檔。摘要上游／模型的無效本機設定不阻擋會議設定查詢，也不讀私有上游認證。

會議檔只允許 `[meeting]` 的 name 與 whisper/audio/vad/transcript/diarization 表格；禁止 course、summary、paths、work 等其他表格，未知鍵回錯誤。paths 等全域設定由 default/local 或 CLI 提供；CLI 不接受 course/work 覆寫以免切換工作類型。`--source` 只覆寫 audio.source，雙音源能力閘門保持停用。

id 為不含副檔名的可攜單一檔名；拒絕路徑分隔符、絕對路徑、控制字元、Windows 保留字、開頭 `.` / `-`、尾端 `.`、前後空白及過長名稱。中文及中間空白可用。讀取會議設定拒絕符號連結。模型不存在不影響設定查詢；真正載入前仍須由引擎檢查。

建立個人設定的目前方法（不啟動錄音）：複製 `meetings/examples/template.toml` 成 `meetings/<id>.toml`，修改 name／num_speakers 後用上述 CLI 驗證。`meetings/.gitignore` 排除個人 `*.toml` 與 `*.toml.tmp`，不修改共享 checkout 原有 `.gitignore`。API 建立與編輯屬後續里程碑。

`Config.work_type` 正規化缺值／空值為 lecture，保留未知非空字串，非字串為 unknown。`Config.dump()` 在新快照寫入 `[work] type`，不回寫舊檔、不修改記憶體內原始設定、保留 work 下其他欄位。課堂錄音／補摘要入口拒絕 meeting 或未知類型，以免誤用課堂流程；不會因 metadata 自動啟動會議作業。

## 參數與目錄

- `[diarization] num_speakers=0` 依正式契約表示「尚未指定」，可儲存或查詢。顯式人數需整數 1..30；拒絕 bool、小數、負數及 >30。`Config.diarization_options(require_speakers=True)` 另拒絕 0，供後續執行入口強制驗證，不能直接把查詢預設交給引擎。
- cluster_threshold 須是有限數值且在 (0,1]；segmentation_window_shift 須是有限正數；bool、NaN、Infinity 都不接受。
- 兩模型路徑須非空字串，依既有 Config.path 解析。預設檔名與正式契約／已合併安裝程式相同。
- meeting.name 空字串以 id 代替；無 id 的全域查詢顯示「未命名會議」。
- `make_meeting_session_dir(cfg)` 在 output_root/meeting_output_root 下排他建立新目錄。default 為 `meetings/<safe name>_<local YYYYMMDD_HHMMSS>`；同名追加 `-2`、`-3`，即使原目錄為空也不重用。檔案／符號連結碰撞也不覆寫。
- meeting_output_root 允許巢狀相對路徑（兩種斜線均可），拒絕絕對路徑、Windows drive、`..`、空／不合法元件；meeting 子目錄不能是符號連結。meeting_session_name 只支援 `{meeting}`、`{date:...}`，產出單層檔名，使用既有 safe_name。課堂 make_session_dir 行為不變。

函式只分配目錄，尚未由 meeting run 呼叫；不是已啟動的錄音。狀態／鎖仍 schema 1，原子單工作鎖與 status schema 2 在另一個里程碑，不能把本次目錄並發測試當作工作鎖驗證。

## 文件與最新程式的差異

10/06 雙音源文件的「沒有雙路引擎」是 PR #62 時點的歷史紀錄；PR #63 已提供 core/capture.py 等引擎。它使用 `plan_dual`／`capture.json`／Opus 分軌與 `mic` ID，與舊提案中的 run_dual_capture／source.json schema 2／WAV／microphone ID 不完全相同。單來源優先，本次不採用或改寫雙音源 schema，不打開入口；雙來源里程碑需先依正式契約解決這些接點。

正式契約 num_speakers 的 0 sentinel 優先於簡述中的「設定一律 1..30」；本次區分可儲存設定與執行必填驗證。既有安裝已合併，文件中「安裝流程未提供」不得再作為阻擋理由。

## 驗證與未完成項目

契約測試：同名課堂／會議隔離、合併順序、強制關閉摘要、參數型別／邊界、未知類型保留、CLI JSON／exit、模型路徑、輸出安全、空目錄碰撞及四個獨立行程同時分配不同目錄。測試不使用麥克風、不安裝套件。

後續依序為鎖／狀態、單來源 session 與來源保存、辨識編排、API 與真資料 UI，最後獨立接雙音源。依 AGENTS 不疊依賴未合併的 PR。

辨識準確度 **Not tested**：沒有乾淨多人樣本及人工標註；唯一真實會議錄音有 20.2% 遺失，不能作驗收依據。雙音源硬體、macOS／Windows 引擎真機仍 **Not tested**。min_turn_seconds、S00 信賴度、prompt 回吐過濾等品質門檻待樣本，不宣稱「會議功能可用」。本 PR 的 Windows basic 只代表設定／CLI 等自動測試，與錄音或辨識硬體驗收不同。
