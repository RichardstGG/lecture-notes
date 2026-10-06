# 雙來源錄音（會議）— Linux 引擎層

狀態：**實驗中**。引擎層與測試已完成，**尚未接上 `lec` CLI／設定／UI**（那些是 Codex 的範圍，見
[meeting-workbench-implementation.md](meeting-workbench-implementation.md)「雙來源錄音引擎層現況」）；
**硬體驗收尚未做**（見最後一節的步驟）。macOS／Windows **不支援**：沒有可以直接錄輸出裝置的方式
（要另外裝 BlackHole／VB-Cable 之類的虛擬音訊裝置），也沒有實機驗證，引擎會明確拒絕並說明原因。

## 目標

同一場線上會議，**同一個 session** 同時收：

1. 指定輸出裝置的聲音（遠端說話者：該輸出裝置的 monitor 來源）
2. 指定的使用者麥克風（本機說話者）

兩路分軌保存（日後會後轉錄與發言者辨識用），另外產生一份混音餵給既有的 VAD／Whisper 即時流程。

## 架構

```
ffmpeg(system monitor) ─┐                            ┌→ tracks/system.ogg
                        ├→ 對齊到共同時間軸 ────────→├→ tracks/mic.ogg
ffmpeg(mic)            ─┘   （每路各自一個執行緒）   └→ 混音 → 即時 VAD／Whisper ＋ recording_*.ogg
```

- 每一路是**獨立的 ffmpeg 行程**，不是一個 ffmpeg 開兩個輸入。原因：單一 ffmpeg 只要有一個輸入出錯就
  整個結束，做不到「一路故障、另一路繼續」。
- 程式碼：[core/capture.py](../core/capture.py)（引擎）、[core/transcribe.py](../core/transcribe.py)
  （`Transcriber(capture_sources=…)` 接進既有流程）、[core/devices.py](../core/devices.py)
  （`plan_dual()` 裝置解析與預檢）、[core/platform.py](../core/platform.py)（pactl 查詢）、
  [core/doctor.py](../core/doctor.py)（「雙來源錄音」那一行）。
- 沒有傳 `capture_sources` 時，`Transcriber` 的單來源與檔案模式**完全不變**
  （`tests/test_transcribe_dual.py::SingleSourceRegressionTests`）。

## 時間基準與同步（不假定同時啟動就是同步）

共同時間基準是這台機器的單調時鐘（`time.monotonic`）。每個擷取行程送來的資料塊都記下到達時間 `a`；
設 `n` 為累計樣本數，`phi = a − n/16000` 就是「這一路的起點時間」。理想情況 `phi` 是常數：

| 現象 | `phi` 的樣子 | 處理 |
|---|---|---|
| 兩個 ffmpeg 起跑時間不同 | 兩路的 `phi` 常數不同 | 時間軸原點 T0 = 較早的那一路；較晚的那一路開頭補零，偏移記為 `start_offset_ms` |
| 管線暫時卡住，資料之後一次到齊 | 暫時變大，之後回來 | 不是掉音，不補零 |
| 資料真的遺失 | 變大**而且不回來**（持續 0.6 秒以上、超過 0.15 秒） | 補靜音，記為缺口（`at_seconds`、`seconds`） |
| 裝置時脈與電腦時鐘有差（數十 ppm） | 緩慢上升／下降 | 每 100 ms 最多補／丟 4 個樣本修正，不產生可聽見的瑕疵 |
| 裝置取樣率不同（44.1／48 kHz） | — | 由 ffmpeg 轉成 16 kHz 單聲道 |

基準線取最近 30 秒 `phi` 的最小值（延遲只會讓 `phi` 變大，所以最小值最接近真實起點）。這個設計的取捨：
漂移修正落後約一個視窗，穩態誤差 = 漂移量 × 30 秒，**100 ppm 約 3 ms**。

**結果**：每個分軌檔與混音的 `t=0` 都是同一個原點，兩軌樣本數相等、可以直接用時間戳對照；
`capture.json` 記下補了什麼（起點偏移、缺口、補／丟的樣本數、時脈誤差 ppm），要稽核時有紀錄。

已量到的對齊精度見下方「驗證」。

## 可靠性行為

| 情況 | 行為 |
|---|---|
| **開始** | 兩路都要在 15 秒內開始送資料、而且串流確實綁在指定裝置上，否則整個不開始（`CaptureStartError`，已啟動的行程全部收掉、**不留任何輸出檔**）。開始前要先收 2 秒音訊估起點，所以 `start()` 約需 2–3 秒 |
| **不接受 `default`** | `plan_dual()` 在開始當下把 default 解析成實際裝置並固定。預設裝置之後改變，錄音不會跟著換 |
| **一路中途故障** | 事件 `capture_source_failed`（來源、類別、時間軸秒數、發現時間、原因）＋ `status.error`；該路分軌在故障處正常封裝；**另一路繼續**；報告 `degraded=true`。不自動重連、不自動換裝置 |
| **靜音 ≠ 故障** | 故障只看資料流：行程結束（`stream_ended`）、活著卻 5 秒沒資料（`stalled`）。數位靜音（全 0）是正常資料，記在 `levels.digital_silence_seconds`，不會觸發任何故障 |
| **串流被系統轉接** | 來源消失（拔除）時，系統可能把錄音串流**默默轉接到別的來源**（PulseAudio 的 `module-rescue-streams` 設計上轉到預設來源）。開發機（PipeWire 1.4.2）實測：卸載一個虛擬來源後，ffmpeg 不報錯、不結束，資料持續流入；**串流被轉去哪裡沒有檢查**。引擎每秒用 `pactl` 查串流實際綁定的來源；不符就判 `source_moved`，**最後一次確認正確之後到達的資料全部丟棄**，不會讓別的裝置（可能是你的麥克風）的聲音混進 system 軌。USB 實際拔除時系統的轉接行為屬 Not tested |
| **預設輸出改變** | 只發警告 `default_output_changed`，不跟隨（會議 app 的聲音可能移到新裝置，這時 system 軌會是靜音，要使用者知道） |
| **正常停止** | `request_stop()`：兩路擷取行程結束 → 已收到的資料處理完 → 分軌與混音封裝 → 算 SHA-256、驗音長 → `capture.json` 寫 `status: complete`。可以在 signal handler 裡呼叫（不取鎖、不等待）。上層的 `stop` 檔／Ctrl+C 契約不變 |
| **強制停止／崩潰** | `capture.json` 停在 `recording`（每 30 秒存檔）或 `aborted`，`verify_capture()` 一律判 `incomplete`，`usable` 全為 false（不可當作重跑來源）；檔案多半還能播（編碼行程在輸入結束時自行封裝），`playable` 標出哪些讀得出來，供人工搶救 |
| **單來源與既有錄音** | 不受影響 |

`verify_capture(session_dir)` 的 `state`：`complete`／`degraded`（完整但有故障或缺口）／`incomplete`／`invalid`
（檔案與紀錄不符：大小、SHA-256、音長）／`missing`（沒有 `capture.json`，例如舊的單來源錄音）。

## 產物與 `capture.json`（**提案**，待與 Codex 的多音軌 metadata 契約對齊）

```
<session>/
├─ tracks/system.ogg        ogg/opus 32k，16 kHz 單聲道，t=0 = 時間軸原點
├─ tracks/mic.ogg
├─ recording_HHMMSS.ogg     混音（audio.keep_recording=true 時；沿用既有檔名，下游不用改）
└─ capture.json
```

`audio.keep_recording=false` 只關混音檔，**分軌一律保留**（分軌就是雙來源錄音的目的）。
混音是**截頂相加**（不縮放：單路說話時音量不變），沒有回音消除。

`capture.json` 重點欄位（`schema_version: 1`；欄位名稱是提案，Codex 若有既定的多音軌 schema 以它為準）：
`status`（recording／complete／aborted／failed）、`timebase.origin_wall`、`sources[]`
（`role`、`device`、`track`、`start_offset_ms`、`samples_in/out`、`duration_seconds`、`valid_until_seconds`、
`gaps[]`、`drift{clock_error_ppm,slip_inserted,slip_dropped}`、`levels{peak_dbfs,digital_silence_seconds,…}`、
`fault`、`complete`、`bytes`、`sha256`）、`mix`、`degraded`、`faults[]`、`warnings[]`。

## 引擎介面（Codex 接線用；也是提案）

```python
from core import devices, capture

plan = devices.plan_dual(system_output="default", mic="default")   # 只查詢，不錄音
# plan["ok"], plan["sources"] = [{role, device, label, kind}, …], plan["errors"/"warnings"] = [{code, message}]
specs = [capture.SourceSpec(**s) for s in plan["sources"]]

t = Transcriber(cfg, session_dir, whisper_url, status=status, capture_sources=specs)
result = t.run()          # 既有流程；多了 result["capture_session"]（= capture.json 內容）與 result["degraded"]
t.request_stop()          # 既有的停止方式，不改成 OS signal
capture.verify_capture(session_dir)
```

- 開始失敗：`result["ffmpeg_failed"]=True`、`result["duration"]==0`、`result["capture_session"]["start_error"]
  ={code,message,role,device}`；`code` 穩定：`unsupported_platform`／`invalid_sources`／`ffmpeg_missing`／
  `start_failed`／`start_timeout`／`wrong_source`／`output_exists`／`cancelled`（啟動中被要求停止，不算失敗）。
- 事件（`status.event(kind, **kv)`）：`capture_started`、`capture_gap`、`capture_source_failed`、
  `capture_warning`（`binding_unverifiable`／`default_output_changed`）、`capture_error`
  （`track_write_failed`／`mix_write_failed`／`internal_error`）、`capture_start_failed`。
  來源故障與 `capture_error` 同時呼叫 `status.error()`。
- `MultiCapture.snapshot()` 給即時狀態：每路 `recording`／`failed`／`ended`、多久沒收到資料、近期峰值、
  是否正處於數位靜音（讓 UI 區分「沒聲音」與「壞了」）。
- 不新增任何 `config/default.toml` 設定鍵；來源選擇怎麼進設定檔由 Codex 定義，引擎只吃 `SourceSpec`。

## 驗證（誠實分層）

| 層級 | 內容 | 結果 |
|---|---|---|
| **Automated** | `TrackAligner`（模擬時鐘）：起點偏移補零、抖動、管線卡住 2 秒不誤判、真實掉音補零、低於門檻的落差當漂移、±100 ppm 慢／快裝置 20–30 分鐘、兩軌互相對齊、起點估計誤差收斂、混音截頂（`tests/test_capture_aligner.py`） | 通過 |
| **Mock**（假擷取行程＋真的 ffmpeg 編碼） | 雙來源參數與輸出映射、起跑時間差、單路故障（結束／停滯）、靜音不是故障、串流被轉接、預設輸出改變、啟動失敗不留檔、啟動中停止、不覆寫既有錄音、強制停止與崩潰判定、混音含兩路、端到端時脈修正、`Transcriber` 整合與單來源回歸（`tests/test_capture_session.py`、`tests/test_transcribe_dual.py`） | 通過 |
| **Mock** | pactl 輸出解析（sink／串流綁定／播放活動／伺服器種類）、`plan_dual()`、doctor 項目（`tests/test_platform_pulse_dual.py`、`tests/test_devices_dual.py`） | 通過 |
| **Manual（虛擬裝置）** | 在開發機（PipeWire 1.4.2）用臨時虛擬 null sink／remap source 跑真的 ffmpeg 與 pactl：`LEC_TEST_PULSE=1 python3 -m unittest tests.test_capture_pulse_integration -v`（預設跳過；會建立並卸載 `lec_t_*` 虛擬裝置）。兩路不串音；**兩個 ffmpeg 起跑差約 36–44 ms，對齊後同一串點擊聲在兩軌的位置誤差 ≤ 1.3 ms**；串流被轉接約 1 秒內偵測到，轉接之後的音訊不進分軌 | 通過 |
| **Hardware** | 真實耳機麥克風、真實會議 app、遠端／本機／同時說話、靜音、實際拔除裝置、輸出裝置切換、60–120 分鐘漂移 | **Not tested**（步驟見下） |
| macOS／Windows | — | **Not tested／不支援** |

虛擬裝置驗證**證明不了**的事：真實裝置的時脈漂移量、藍牙延遲、會議 app 的行為、USB 實際拔除時系統怎麼轉接。

## 已知限制

- **沒有回音消除。** 不戴耳機時，麥克風會收到喇叭播出的遠端聲音，混音裡遠端出現兩次（略有延遲）。
  `plan_dual()` 與 `lec doctor` 偵測到輸出像是喇叭時會警告（啟發式，依 pactl 回報的連接埠名稱）。
- **monitor 收的是「整個輸出裝置」。** 通知音、音樂、別的程式的聲音都會進 system 軌；本程式不知道哪一段是
  會議 app。`plan_dual()` 會在「別的輸出裝置正在播放、你選的卻沒有」時警告。
- **會議 app 的 mute 與回音消除不會套用。** 本程式拿到的是作業系統層的音訊：你在會議 app 按靜音，
  本程式的 mic 軌照樣錄到你說的話。
- **來源角色不等於人物身分。** system 軌可能含多位遠端說話者，本機麥克風也可能收到多人。
- **不自動重連。** 一路故障後該路到結束都沒有聲音（另一路照常）。短暫的藍牙中斷也是如此。
- 停滯門檻 5 秒：短於此的資料中斷會被當成缺口補靜音，長於此視為故障。
- 漂移修正落後約 30 秒（見上）。時脈誤差只在起點收斂後至少 120 秒的資料才會報（`clock_error_ppm`，否則為 null）。
- 兩路都不是會議 app 的固定延遲補償：藍牙裝置若回報不準的延遲，兩軌之間可能殘留固定偏移（硬體驗收要量）。
- 整個流程多一份記憶體內的緩衝：Python 本身卡住超過約 2 秒，系統音訊伺服器會丟資料（跟單來源相同，
  只是現在會被偵測並記成缺口）。

## 硬體驗收步驟

**這些步驟會真的收錄你的麥克風與輸出裝置，請自己執行，不要在不知情的狀況下錄別人。**
工具：`tools/dual_capture_check.py`（獨立於 `lec`，不載入 whisper／LLM）。主要情境：**戴耳機**。

準備：

```bash
./lec doctor                                  # 看「雙來源錄音」那一行
python3 tools/dual_capture_check.py --plan    # 只列出會錄哪兩個來源，不錄音
```

把會議 app（或測試用的播放程式）的輸出設成你要測的耳機；若要指定裝置，加
`--system-output <sink 名稱或編號> --mic <來源名稱或編號>`（`pactl list short sinks`／`lec devices`）。
每個情境錄 2–3 分鐘，結束後工具會自動印出分析（也可以之後用 `--analyze <資料夾>`）。

```bash
python3 tools/dual_capture_check.py --minutes 3 --out /tmp/dual_check_1
```

| # | 情境 | 做法 | 預期 |
|---|---|---|---|
| 1 | 遠端說話 | 只讓對方說話（或在該輸出裝置播一段別人的語音） | system 軌有語音（峰值明顯高於 −40 dBFS）；mic 軌只有背景噪音；`faults` 空 |
| 2 | 本機說話 | 只有你說話 | 反過來：mic 軌有語音，system 軌近乎數位靜音；**不是故障** |
| 3 | 同時說話 | 兩邊同時說 | 兩軌各自只有自己的聲音（戴耳機）；`recording_*.ogg` 兩者都聽得到 |
| 4 | 靜音 | 在會議 app 按靜音；再把麥克風硬體靜音 | 會議 app 靜音**不影響** mic 軌（見限制）；硬體靜音使 mic 軌數位靜音增加，`faults` 空 |
| 5 | 裝置拔除 | 錄音中拔掉 USB 麥克風或耳機 | 約 1–5 秒內印出 `✖ 來源故障`（`source_moved`／`stream_ended`／`stalled`），另一軌繼續；結束後 `degraded`、完整性 `degraded`；故障軌在故障處封裝完整 |
| 6 | 輸出裝置切換 | 錄音中把系統輸出從耳機切到喇叭 | 約 5 秒內出現警告 `default_output_changed`；**不會跟著換**；system 軌之後變靜音（會議聲音已不在原裝置） |
| 7 | 強制停止 | 錄音中連按兩次 Ctrl+C | 完整性判 `incomplete`、`usable` 全 false、`playable` 多半為 true |
| 8 | 60–120 分鐘漂移 | 見下 | 兩軌點擊延遲的變化應在數十毫秒內；`clock_error_ppm` 合理（數十 ppm，超過 500 會標警告） |

**漂移（情境 8）。** 要有同時出現在兩軌的已知訊號：用喇叭（不戴耳機）播放每分鐘一次的點擊音，讓 system monitor
與麥克風都收到。產生 2 小時的點擊檔並播到你選的輸出裝置：

```bash
ffmpeg -f lavfi -i "aevalsrc='0.8*sin(2*PI*2000*t)*lt(mod(t,60),0.015)':s=48000:d=7200" -ac 2 -c:a flac /tmp/clicks_60s.flac
paplay --device=<輸出 sink 名稱> /tmp/clicks_60s.flac &
python3 tools/dual_capture_check.py --minutes 90 --out /tmp/dual_drift --system-output <輸出 sink 名稱>
python3 tools/dual_capture_check.py --analyze /tmp/dual_drift --clicks
```

判讀：「mic 相對 system 的延遲」的**變化**（開頭 → 結尾）就是殘餘漂移；延遲本身包含聲音傳播與裝置延遲，
固定值不用管。修正有效時，90 分鐘內變化應在數十毫秒以內；若變化接近
`|clock_error_ppm| × 時間`（例如 50 ppm × 90 分鐘 = 270 ms），代表修正沒有作用，請回報整份 `--analyze` 輸出。

## 回報與驗證用語

引擎層與虛擬裝置驗證屬於 Automated／Mock／Manual；**所有真實裝置情境在上表完成前一律標 Not tested**，
文件與 `lec doctor` 維持「實驗中」措辭，不寫成「已支援」。
