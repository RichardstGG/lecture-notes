# 會議雙音源契約與引擎交接（2026-10-06）

2026-10-08 現況補充：下方「沒有雙路引擎」描述 PR #62 時點；PR #63 已合併引擎，仍未接會議 CLI／API。新設定資料層不開放雙來源，schema 與介面差異見 [最新里程碑說明](meeting-workbench-config.md)。

## 交付狀態與範圍

以重新 fetch 的 `origin/main`（3a5c364）及程式為基準：會議工作台仍為預覽；沒有 `lec meeting run`、meeting-runs API、會議來源寫入器或雙路錄音引擎。`core/diarize.py` 已存在，但未接會議生命週期。這次交付是可獨立審查的**設定驗證＋能力查詢＋啟動阻擋＋UI 預覽**。以下明標「提案」的介面和檔案格式尚未實作或經 Claude 驗證，不表示功能已開放。

目標第一版：Linux PulseAudio 相容服務（開發機 PipeWire）、耳機、指定輸出 sink 的 monitor 加指定麥克風；同 session、同鎖、共同時間軸、分軌保存，混音給既有即時轉錄。來源角色不代表姓名或人物身分；不承諾重疊語音完美轉錄。指定輸出可能收進其他程式音訊；會議 app 的 mute 不等於本程式 mic mute。本版沒有程式內獨立 mute 控制，不能以音量低推斷故障。

## 已實作：設定與安全邊界

```toml
[audio]
capture_mode = "single"  # single | dual；舊設定缺省 single
source = "default"       # 保留既有 --source 及 audio.sources 別名語義
system_source = ""       # dual 必填完整 Pulse monitor source name
microphone_source = ""   # dual 必填完整 Pulse microphone source name
```

- 不重用 `audio.sources`；仍是原本的別名表。設定合併及 `--set` 順序不變。
- `Config.capture_plan()` 回傳 `{"mode":"single","tracks":[]}` 或 `{"mode":"dual","tracks":[{"id":"system","role":"system","device_id":"..."},{"id":"microphone","role":"microphone","device_id":"..."}]}`。single 的解析繼續由 `audio_source()` 完成。
- dual 必須 backend=auto|pulse，來源為非空完整名稱（允許英數、`_ . : -`），拒絕 default/auto、純數字、空白、別名、相同來源。`system`、`microphone` 為 session 內固定 track ID；`device_id` 為 Pulse 名稱，非變動的數字索引；不是跨硬體的永久 UUID。
- 設定驗證只檢查形式，**不宣稱裝置存在或 role 正確**。必須由 Claude 的能力／裝置探測判斷 monitor_of_sink，不以 `.monitor` 字尾猜 role。
- `--source` 仍只改 `audio.source`，不改兩路設定。不新增可錄音的 meeting 命令。`lec run` 的 dual（包括搭配 --file）在鎖、輸出與引擎之前回既有 exit 1 與 `capture_unavailable` 訊息；`audio_source()` 另行防止引擎意外以 dual 設定呼叫單路。非法設定維持既有 ConfigError／CLI exit 1。
- 不改既有 status/run/events schema 1、lock 或 stop 語義。本 milestone 無兩路作業，因此不能聲稱已解決現有 check+write 鎖競態。開放會議錄音前必須完成原子鎖及並發測試。

## 已實作：能力查詢與 UI

`lec capture-capabilities [--json]`，成功 exit 0，不讀私有設定、不探測／測錄裝置。JSON schema 1：

```json
{"schema_version":1,"single":{"available":true,"reason_code":null,"message":"既有單來源流程；裝置可用性須另行檢查"},"dual":{"available":false,"reason_code":"engine_not_integrated","message":"雙音源引擎與會議流程尚未整合"}}
```

非 Linux 的 dual reason 為 `unsupported_platform`。`available` 表示產品流程已整合，不代表裝置、權限、模型就緒。single true 僅指既有 lec run；不是會議 single 已可用。未來需同時滿足平台探測成功與 session 整合才可將 dual 設 true，不能只檢查 Linux 或 import 成功。

`GET /api/v1/capture-capabilities` 原樣回上述版本化欄位，backend 經 CLI 邊界，無 core import；CLI 失敗沿用 API error envelope，不能回可用。既有 API 無刪改。

UI 來源模式提供單一／系統＋麥克風預覽；兩個裝置選單仍停用，兩路均「未開始 · 音量未知」。依真能力回應顯示未開放原因，查詢失敗保持停用。既有開始按鈕仍停用，即使單路 available=true 也不推定會議可錄。預覽操作不取得鎖、不呼叫 start/stop、不寫入正式 session。

## 提案：Claude 可消費的引擎介面 v1

下列介面需由 Claude 回覆接受或修訂後才接線；本 PR 不建立動態猜測／自動啟用 adapter。

1. `core.devices.capture_inventory(backend="auto") -> dict`：無錄音副作用，回 `{schema_version:1, backend, dual_supported, reason_code, sources:[{device_id,name,role:"system"|"microphone",monitor_of_sink:null|sink_name,available}]}`。僅列能確定角色的來源，monitor 綁定指定 sink；不跟隨 default sink，數字 index 僅供內部使用。失去裝置或使用者切換預設輸出不能自動換選定裝置。
2. `core.transcribe.run_dual_capture(request, on_source_status, should_stop) -> result` 同步介面：request 含 `contract_version=1`、兩個 capture_plan tracks、session staging 絕對路徑、既有轉錄設定與 whisper endpoint。source status callbacks 不得包含音訊內容。`should_stop() -> "none"|"stop"|"force"` 由 session 統一映射 stop/stop_force；不另建工作鎖、不私自啟停 LLM。
3. 引擎在開始前重查來源存在、role、權限及兩路能開啟；兩路都成功後才回報 recording。任一路啟動失敗即關閉另一個 handle，丟具有穩定 `.code`／`.source_id` 的錯誤，不留成功 metadata。
4. 引擎採同一 monotonic epoch 與 sample clock 定義時間（輸出統一 16 kHz mono）。兩路原始音訊各保存，混音供現有 ASR；必要補零、重取樣／漂移補償須回報，不可只拼接兩個獨立錄音的 wall-clock 起點。明定混音 gain/limiter 並記錄於 metadata；不承諾不經真機驗證的同步精度。
5. 途中一路失效：保留另一個，回 `degraded` 與明確 error/gap；缺失處混音補零。禁用無聲降級、自動裝置替換和無記錄重連；v1 不自動重連，失效軌終止直到全場停止。兩路都失效即失敗。靜音但持續收到有效 sample 是 `silent`，不是裝置錯誤。
6. 正常 stop：一起停止擷取、排空混音轉錄、封存兩軌／雜湊／缺口，回 result。force：一起取消所有 handle/子行程，標 aborted，只允許重新驗證完整封存檔；不得因單一路先停而宣稱整場正常完成。
7. result 為 `{contract_version:1,outcome:"complete"|"incomplete"|"aborted",duration_samples,tracks,mix,errors}`。tracks / mix 使用下述 schema 2 記錄；session 在驗證 staging 後才原子發佈 source.json。完整性由所有 track 決定；不從 ASR 有文字推論錄音成功。
8. 穩定錯誤碼：`unsupported_platform`、`backend_unavailable`、`source_missing`、`source_role_mismatch`、`source_duplicate`、`permission_denied`、`source_open_failed`、`source_lost`、`capture_failed`、`write_failed`、`sync_failed`。錯誤不可被空成功結果吞掉，session/status/API 保留 code、source_id、message。

## 提案：source.json 版本遷移與重跑

原契約單檔 source.json schema 1 尚無正式 writer。本次不更名、不寫舊資料。schema 1 reader 仍依原格式驗證 kind、相對檔名、size、SHA-256、音長；不存在版本或未知版本不可猜為可重跑。新增 schema 2 專用多軌格式，**不可把 schema 1 的單檔欄位改成陣列而保留版本**。

schema 2 頂層：`schema_version=2, kind="recording", capture_mode="dual", created_at, sample_rate=16000, duration_samples, complete, tracks, mix`。範例（欄位名稱是提案，不是已生成檔案）：

```json
{"schema_version":2,"kind":"recording","capture_mode":"dual","created_at":"2026-10-06T12:00:00+08:00","sample_rate":16000,"duration_samples":160000,"complete":true,"tracks":[{"id":"system","role":"system","device_id":"out.monitor","path":"audio/system.wav","size_bytes":320044,"sha256":"<64 lowercase hex>","start_offset_samples":0,"sample_count":160000,"finalized":true,"gaps":[]},{"id":"microphone","role":"microphone","device_id":"mic","path":"audio/microphone.wav","size_bytes":320044,"sha256":"<64 lowercase hex>","start_offset_samples":0,"sample_count":160000,"finalized":true,"gaps":[]}],"mix":{"path":"audio/mix.wav","size_bytes":320044,"sha256":"<64 lowercase hex>","finalized":true,"sample_count":160000,"gains":{"system":0.5,"microphone":0.5},"limiter":false}}
```

- WAV PCM16 mono 為交接提案，容量約兩分軌＋混音每小時 346 MB；可改無損容器，但須先同步文件及 reader，不能拿有損壓縮 sample 數當精準時間軸。
- `start_offset_samples` 是第一 sample 相對共同 epoch 的非負整數；`sample_count` 包含實際寫入的補零 sample。缺口 `{start_sample,end_sample,reason}` 使用全 session 時間，半開區間、排序、不重疊且落在 `[0,duration_samples]`。起始延遲、斷流及直到結束的缺失均要列 gap；真實靜音不列 gap。完整 complete=true 需兩軌已封存且無遺失缺口。
- 重跑必須先取得唯一工作鎖，再檢查所有檔案位於 session（拒絕絕對路徑、`..` 與逃逸 symlink）、角色/ID 唯一且與固定兩角色相符、有限且非負的整數時間值（bool 不算整數）、interval 合法、size/SHA-256/解碼 sample 數一致，mix 與 timeline 對齊。
- 缺檔、雜湊變動、未封存、未知版本一律拒絕。v1 重跑策略對 complete=false（即使保留一路可聽）拒絕自動辨識，UI 可顯示不完整原因；將來若允許部分重跑要明確使用者選擇，不偷補成完整。
- schema 1 不原地重寫；reader 可在記憶體正規化為單軌。schema 2 不能直接丟給現有只接一檔的 diarize_session：第一輪只能在明確驗證後用 mix 做既有辨識，未來分軌轉錄／辨識合併須記錄各軌 hash 集合及合併版本，不用來源角色推論人物。

## 提案：狀態、停止、CLI/API 接線

沿用 meeting 正式契約的未實作 status schema 2，增加可選 `capture`：`{mode, state:"starting"|"recording"|"degraded"|"stopped", complete:null|bool, sources:[{id,role,device_id,state:"opening"|"active"|"silent"|"failed"|"stopped",level_dbfs:null|number,last_sample_at:null|ISO8601,error:null|{code,message},gaps:[]}]}`。音量範圍 [-120,0] dBFS，未知為 null，靜音為 -120；所有 timestamp/level 來自引擎，UI 不推測。來源失效事件仍用 events schema 1 的新增 type `capture_source_error`，含 source_id/code；不新增 phase 來取代既有 phase。

meeting run 未來追加 `--capture-mode single|dual --system-source ID --microphone-source ID`，API meeting-runs 對應新增可選 capture_mode/system_source/microphone_source；省略為 single，既有 source/file 不改義。dual+file 或 dual+source 輸入衝突回 exit 2 / HTTP 400，unsupported 回 exit 1 / HTTP 422，忙碌 exit 3 / HTTP 409。啟動前驗證能力與裝置，拿鎖後再次驗證，以免熱拔插競態。檢查失敗不啟動一路替代。

兩路共用同一個 `<state_dir>/run.json` 原子鎖及 session stop/stop_force；一個 stop 同時結束兩路。中途一路失效仍保留鎖到剩餘錄音及收尾完成；最終 incomplete 用 failed + 錯誤資訊，不回正常完成。以上均屬後續整合驗收，這次沒有虛構作業成功路徑。

## CROSS_AGENT_REQUEST

- Requester：Codex
- Target agent：Claude Code
- 類型：blocking（開放實際雙音源錄音）；non-blocking（本契約 milestone）
- 目的：實作指定 monitor＋mic 的共同時間軸分軌擷取、混音 ASR、能力/狀態回報。
- 現有行為：transcribe 只有單一 audio_source；devices 列表未提供上述可依賴的角色與 sink 關係契約；meeting 尚無 session 接線。
- 問題：不能讓 UI 的雙來源選擇實際只錄一路，也不能用 mock 或 OS 判斷作為可用證據。
- 建議行為：確認／修訂本文件介面 v1，再在所有權內實作探測、啟動原子性、共同 clock、兩軌封存、缺口與停止；提供可測的 fake engine adapter 或 fixture，不預設啟動麥克風。
- 涉及檔案：Claude 的 core/platform.py、devices.py、doctor.py、transcribe.py 與平台／引擎測試；Codex 後續接 config/CLI/session/status、UI API/前端及契約測試。
- 是否改變 public contract：是，新增能力 inventory、引擎 callback/result、source schema 2、可選 capture status；既有單路語義不變。
- 相容性影響：audio.sources 保持別名表；schema 1 來源只讀相容；Windows/macOS 明示 unavailable；不可讓 dual 降級。
- 建議測試：兩路啟動其一失敗需清理；途中失效保留另一軌；silence 不報故障；來源切換不跟隨；stop/force 都關閉兩路；缺口/hash/sample clock；單路回歸。
- Requester 目前能否繼續其他工作：本 PR 可獨立完成；實錄、狀態流及 source schema writer 待介面確認與引擎及會議 session 整合。未傳送訊息給其他聊天。

## 驗收矩陣

- Automated / Mock：設定單路回歸與 dual 合法／缺失／相同／別名／錯格式；CLI 平台能力、無裝置副作用；啟動拒絕先於 lock/output/server；API schema、錯誤 envelope、只讀；UI 雙路選單停用、未知音量、能力查詢失敗。
- 後續整合 Not tested：真實裝置存在/role、兩路單鎖並發、統一 stop/force、來源中途錯誤 callback、source schema 1/2 writer/reader 與重跑驗證；這些是開放入口的必要門檻。
- Hardware Not tested：耳機下遠端單講、本機單講、同時說話；預期靜音與 app mute；拔任一路／兩路、變更預設輸出、正常／強停、磁碟寫入錯誤；60–120 分鐘量測兩路起始偏差與累積漂移（先記錄實測數值，不能先宣稱可接受門檻）。未啟動真實麥克風。

本 milestone 驗證：`python3 -m unittest -q` 707 tests，705 通過、2 skip；`npm --prefix ui/frontend test` 55 通過；`npm --prefix ui/frontend run build`（TypeScript + Vite）通過。測試包含 mock，不代表引擎或硬體驗證。

兩項 skip 為 `tests.test_diarize_worker.TestRealSherpa` 的真引擎測試（worktree 未配置 sherpa/模型）。若要補驗證，在具備 sherpa 的 Python 環境設定 `LEC_TEST_DIARIZE_MODELS=/path/to/models python -m unittest tests.test_diarize_worker.TestRealSherpa -v`；這仍不取代雙音源硬體驗收。最後調整後 69 項相關 Python 契約測試全通過。手動 CLI 驗證 capture-capabilities JSON 與 dual 啟動回 exit 1 均符合預期，未開啟錄音；未做瀏覽器視覺或硬體手動驗證。
