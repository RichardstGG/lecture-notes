# macOS 平台備忘

狀態：**正式支援**（maintainer 2026-10-20 決定，依據會議 v1 真機驗收）。下面標了日期的各節是當時的調查與驗證紀錄，其中「實驗中」是當時的狀態。

2026-10-14 已在 Apple Silicon 真機重現擷取遺失，
並完成原生 callback、ffmpeg 輪詢與新擷取路徑的對照；適用範圍與長時間驗證見下方「擷取掉音訊」。
以下內容大多是靜態驗證（讀 ffmpeg / AVFoundation 官方行為推導）與 mock test，
不是「已測過沒問題」。第一次在真機上跑之前請先看完「還需要哪些實機測試」一節；
要實際驗的時候照「實機驗證流程」那一節的步驟跑。

## 安裝時的 macOS 差異（實機回報，2026-09）

在 MacBook 上照 README 安裝時遇到的三個問題，都已經寫進 [mac_setup.md](mac_setup.md)：

- **zsh 把 `#` 當參數**：zsh 預設沒開 `interactivecomments`，貼上
  `python3 setup_engines.py whisper    # Linux/macOS；…` 時 `#` 和後面的字都被當成
  參數，`setup_engines.py` 回報「未知的引擎：#」。解法是只複製指令本身，或
  `setopt interactivecomments`。
- **`~/.local/bin` 不存在、也不在 PATH**：這是 Linux（XDG）的慣例，macOS 沒有。
  `ln -s … ~/.local/bin/lec` 會回報 `No such file or directory`。要先 `mkdir -p`，
  再把它加進 `~/.zshrc` 的 PATH。
- **內建 `python3` 太舊**：Command Line Tools 附的 `python3` 通常是 3.9，`lec` 需要
  3.11+（`tomllib`）。[mac_setup.md](mac_setup.md) 的 brew 指令已加上 `python`。

## 錄音（AVFoundation）

- 後端固定用 `avfoundation`。裝置列表靠解析
  `ffmpeg -f avfoundation -list_devices true -i ""` 的輸出，
  只抓「AVFoundation audio devices:」區段裡的 `[編號] 名稱` 這種格式
  （`core/platform.py::_avfoundation_sources`）。
- 錄音時傳給 ffmpeg 的是**編號**（`-i :<index>`），不是名稱字串，所以裝置名稱
  裡有中文、空白、括號都不影響錄音，只影響「怎麼在畫面上認出這台裝置」。
- `resolve_source()` / `list_sources()` 用名稱或編號比對；同名裝置會拿到第一個
  符合的（AVFoundation 本身沒有給更穩定的 id，只有 index，且 index 可能因為
  裝置插拔而改變順序——這點沒辦法在程式面完全解決，建議固定用同一顆麥克風）。
- 相關解析邏輯的 mock 測試：`tests/test_platform_parsers.py::AvfoundationSourcesTests`。

## 擷取掉音訊（實機量測，2026-10-05／06／14）

**狀態：本台內建麥克風的課堂單來源修復已通過 ≥ 30 分鐘 `lec` 真機驗證；macOS 仍為實驗中。**
直接用 ffmpeg 錄原生 WAV 仍遺失約 11–12%，退出碼為 0；原生 callback 收到的音訊完整，
把 ffmpeg 的 10 ms 輪詢睡眠暫改為 1 ms 後，兩輪 60 秒皆無遺失。這不支持把 #52 的
Python 讀取執行緒稱為根因修復。其他 Mac／輸入來源尚未驗證；**在對應環境通過長測前，在 Mac 上不要錄需要事後處理的內容。**

### 量測（兩份 Mac 錄音帶回 Linux 分析）

| | 錄音 A（#52 之前） | 錄音 B（錄於 #52 之後；是否跑到 #52 未確認） | Linux 對照 |
|---|---|---|---|
| 時間戳跨距／實際音訊 | 87.7 分／70.0 分 | 76.3 分／64.0 分 | 吻合 |
| **遺失** | **20.2%** | **16.1%** | ≈0%（10 份正常錄音 0.000–0.057%） |
| 空隙數 | 5,196 | 4,390 | — |
| 空隙長度中位數 | 213 ms | **128 ms** | — |
| 空隙間距 | 1011.98 ms | 1013.9 ms | — |
| 間距變異係數 | 0.01 | 0.007 | — |
| ogg 每頁封包數（滿載 50） | 40 | 45 | 50 |
| 每頁多出來的時間（正常 0） | 213 ms | 128 ms | 0 ms |
| encoder tag | `Lavc63.1.101`（ffmpeg 8.x） | 同 | 本機 ffmpeg 7.1.5 |

- 封包全是標準 20 ms Opus frame，所以不是 VBR 假象。ogg 時間戳空隙呈現 **幾乎規律的 1 秒間隔**，
  但這與封裝頁間隔相近，不能直接當作實際擷取缺口的週期；本次直接 demux 量到的是約 93 ms 一次的 10.667 ms 缺口。
- **兩份錄音的差異**：缺口長度 213 ms → 128 ms，遺失 20.2% → 16.1%，週期完全沒變。兩份是不同場次、不同條件的單次錄音，
  **尚不能把差異歸因於 #52**；在確認錄音 B 有跑到 #52 之前，這個差異也無法說明 #52 的緩解有沒有效果。
- **錄音 B 的 Mac 客戶端是否真的跑到 #52 未確認。**確認方式：`git merge-base --is-ancestor f6d3182 HEAD`，
  以及該 session 的 `events.jsonl` 有沒有 `capture_integrity` 事件（修正版錄完會寫、並在終端機印
  「✖ 錄音存檔遺失 …」）。這決定後面的推論能走多遠。
- 錄音 B 另外在第 73.6–75.8 分有 12 個連續的 4–30 秒大空隙（合計 135 秒），不像週期性問題，像機器整個停住；
  原因未知（闔蓋？睡眠？切換視窗？）。

### 已確認

- **缺的是真的音訊**，不是時間戳假象。每個 ogg 頁（1 秒）內部都有異常的波形跳變：每頁「最大跳變／區塊中位跳變」
  Mac 兩份的中位數是 28–35，Linux 是 6.2；Mac 有 97–100% 的頁超過 Linux 的 p90。
  錄音 B 這項只取樣到 32 頁（過濾掉太小聲的頁），錄音 A 是 112 頁；方向很明確，但不要拿它當精確數字。
- 缺口在頁內的位置接近均勻分布（σ 約 28–29%，均勻分布約 29%），**沒有固定相位**，所以指不出是哪個週期性動作。
- **量測陷阱（曾因此得出錯誤結論，已收回）**：ogg 的時間只記在頁尾（granule position），ffprobe 只能把
  空隙畫在「頁的開頭」，真正的缺口可能在那 1 秒內任何位置。**不要在 ogg 頁界或 ffprobe 顯示的空隙位置量
  波形連續性**，量到的是頁界，不是缺口。要看整頁範圍內有沒有異常跳變。

### 排除項目（maintainer 提供的另一台 Mac 實驗）

環境：Apple Silicon、macOS 27.0.1（26A434）、Homebrew ffmpeg 9.0.1，內建 48 kHz
單聲道麥克風、插電；另裝有 Background Music 與 iPhone Continuity 麥克風。
下表為提供的量測，與下節本次實驗分開；不是此次重新執行的結果。

| 實驗 | 牆鐘／預期 | 實際音訊或遺失 | 證據範圍 |
|---|---|---|---|
| E1 `/tmp`，16 kHz Opus，150 秒 | 150／150 秒 | 12.0%；頁封包中位數 45，頁多出 117–128 ms | 不經過 `lec` 也掉 |
| E2 Desktop／iCloud，16 kHz Opus | 150／150 秒 | 12.2–12.3% | 輸出位置不是主因 |
| E3 `/tmp`，16 kHz PCM WAV，兩次 | 約 151／150 秒 | 131.8／131.7 秒；12.1–12.2% | Opus 與 ogg 不是主因 |
| A 原生 48 kHz 單聲道 WAV | 60／60 秒 | 52.87 秒；11.9% | 不需重取樣也掉 |
| B 指定 48 kHz WAV | 61／60 秒 | 52.82 秒；12.0% | 同上 |
| C 16 kHz 單聲道 WAV | 60／60 秒 | 52.55 秒；12.4% | 聲道與重取樣不是主因 |

因此先前的 iCloud 寫檔、Opus／ogg、Python 下游、藍牙與電池推測，不能解釋這組
直接錄內建麥克風 WAV 的結果。先前 D 實驗因 zsh 斷詞失效，不算佇列選項已驗證。

### 本次隔離實驗（Hardware test，2026-10-14）

本機：arm64、macOS 27.0.1（26A434）、ffmpeg 9.0.1、內建麥克風 48 kHz 單聲道、
插電且電量 100%。CoreAudio 列出內建輸入／輸出（Transport: Built-in）、Background Music
與 UI Sounds（Virtual）、iPhone 麥克風（Unknown）；HAL 另有 Background Music、DeskIn、
Parrot 驅動。只有驅動 host 在執行，未觀察到可退出的 Background Music App。
所有實驗用 bash 與低音量本機測試音，WAV 先比對實際樣本數與預期 60 秒。

| 實驗 | 牆鐘秒（兩輪） | WAV 秒（兩輪） | 相對 60 秒遺失 | 退出碼 |
|---|---|---|---|---|
| 原生格式 ffmpeg WAV | 60.324939／60.327274 | 53.141333／52.992000 | 11.4311%／11.6800% | 0／0 |
| queue 4096 + `nobuffer` + verbose/debug_ts | 60.325072／60.326899 | 50.858667／47.637333 | 15.2356%／20.6044% | 0／0 |
| 指定內建裝置名稱，verbose/debug_ts 控制 | 60.316914／60.324448 | 53.088000／53.130667 | 11.5200%／11.4489% | 0／0 |
| 同上，暫將 `av_usleep(10000)` 改為 1 ms | 60.695369／60.302751 | 60.000000／60.000000 | 0%／0% | 0／0 |
| 僅 queue 4096，指定內建名稱 | 60.329716／90.012283 | 53.226667／未完成 | 11.2889%／不可計算 | 0／逾時 |
| AVAudioRecorder 原生 WAV | 61.386138／60.464931 | 60.000000／60.000000 | 0%／0% | 0／0 |
| 原生 callback → Matroska → ffmpeg WAV/Opus | 60.635932／60.249607 | 60.000000／60.000000 | 0%／0% | helper、ffmpeg 皆 0 |

- 原生 AVCaptureSession callback 兩輪各收到 **5,625 × 512 samples = 60 秒**，PTS 跨距
  同為 60 秒，缺口數與遺失秒數皆 0。callback 牆鐘 60.039172／60.038232 秒；到達間隔
  中位數 10.670500／10.665500 ms，最大 11.271375／11.610583 ms；沒有 < 1 ms 的突發群。
- 原樣 ffmpeg 的直接 demux PTS 有 **648／645 個缺口**，各中位數 10.667 ms，總共
  6.912418／6.880415 秒，缺口平均間距 92.504895／93.068323 ms。不是每秒只掉一次。
  1 ms 對照的 demux 缺口皆 0；診斷 dylib 分別替換了 47,682／47,549 次 10 ms 睡眠。
  這只改該子行程，不改已安裝 ffmpeg 或全域環境；不是產品中的修復方式。
- 新路徑兩輪 Opus 的 `probe_capture` 都是 audio/span 60.007 秒、lost_ratio 0、gap_count 0。
- 作廢／未完成也保留：授權確認前兩輪基準各等了 90.009271／90.013604 秒，不能判斷遺失。
  第二輪留下的未收尾 WAV，ffprobe 顯示 19.113854 秒，但其 header 未完成，不能採用。
  後來兩輪編號 `:1` 的 verbose 實驗實際回傳 44.1 kHz 雙聲道（牆鐘 60.275121／60.278924、
  WAV 50.515011／50.479819 秒），不是內建麥克風，因此不列入內建對照；改用明確裝置名稱重做。
  第一版原生輸出原型也曾拒絕單聲道的 non-interleaved 格式，0 callbacks，沒有產生音檔；修正後才得到上表結果。
- X3 未退出／移除 HAL 驅動；X4 未實測另一個輸入；X7 未安裝 ffmpeg@7。這些不算通過或已排除
  所有裝置／版本。[ffmpeg 7.1.5 原始碼](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/libavdevice/avfoundation.m#L250-L263)亦有同樣的單格暫存，不足以假設降版可解決。

### 根因與原始碼依據

[ffmpeg 9.0.1 audio callback](https://github.com/FFmpeg/FFmpeg/blob/n9.0.1/libavdevice/avfoundation.m#L250-L263)
在鎖內把未讀的 `current_audio_frame` 釋放，再存新 buffer；不是一個會保存所有音訊的佇列，
也沒有在覆寫時記錄 drop 錯誤。[讀取端](https://github.com/FFmpeg/FFmpeg/blob/n9.0.1/libavdevice/avfoundation.m#L1084-L1163)
取走該格，沒有資料就回傳 EAGAIN。[CLI demux 執行緒](https://github.com/FFmpeg/FFmpeg/blob/n9.0.1/fftools/ffmpeg_demux.c#L711-L716)
遇到 EAGAIN 會睡 10 ms，再重試。本機 buffer 間隔是 10.667 ms，睡眠加排程延遲讓讀取來不及，
舊 buffer 就被新 callback 覆寫。原生收到全部 buffer、原樣 ffmpeg 缺整個 512-sample buffer、
只縮短這項睡眠就歸零，三組對照支持這個本機根因。沒有證據把它歸因於 `lec` 每秒更新或 iCloud。

`thread_queue_size` 是 demux 之後的佇列，無法保存尚未被 demux 讀走、已在 callback 覆寫的 buffer。
9.0.1 的 `-h demuxer=avfoundation` 沒有 `audio_buffer_size` 選項，不要用它調擷取緩衝。

### 修復與相容性

- `core/platform.py::start_live_capture` 只供 macOS 課堂單來源即時錄音使用。Swift AVFoundation
  serial callback 複製每個 buffer，專屬 writer FIFO 輸出 PCM；待寫資料超過 32 MiB 時明確失敗，
  不覆寫或靜悄悄丟資料。浮點／整數 PCM 與 planar→interleaved 處理以明確格式檢查防止誤解碼；
  中途格式變更、時間戳倒退、pipe 失敗與 30 秒收不到 PCM 都報錯。
- PCM 透過帶原始 capture PTS 的 Matroska pipe 交給既有 ffmpeg，輸出仍是 16 kHz PCM 與
  原有 Opus 存檔。**不使用無時間戳 raw PCM 作輸入**，以免把真正遺失掩蓋成連續的合成時間。
  合成測試刻意加入 128 ms 缺口，編成 Opus 後 `probe_capture` 仍量到 0.128 秒、一個缺口。
- 需要 Xcode Command Line Tools 的 `swiftc`；缺少時 `lec doctor` 顯示 FAIL，請先執行
  `xcode-select --install` 並完成安裝。helper 使用下節的安全 binary 快取；重新編譯時原始碼／
  module cache 留在每次錄音的獨立暫存目錄，正常收尾刪除。不加新設定鍵。
  編譯／啟動失敗明確回報，**不退回已知會掉音訊的 ffmpeg avfoundation 擷取**。
- `lec stop`／Ctrl+C 仍走原協定：停止檔或 signal handler 要求 helper 停止來源，等 FIFO
  寫完、EOF 讓 ffmpeg 封裝完成。強制停止時 helper 偵測父行程離開而結束，避免留下錄音子行程。
  強制停止仍是 aborted，不保證完整性事件或可用存檔。
- CLI 參數、設定鍵、status/run/events schema、`capture_integrity` 事件與 0.5%／2% 門檻皆不變。
  `ffmpeg_returncode` 保留 ffmpeg 真實退出碼；在 ffmpeg 啟動前失敗為 None。
  上游 native helper 失敗也令既有 `ffmpeg_failed` 為 true，即使 ffmpeg 封裝 EOF 後退出 0；另記錄原生錯誤。
- Linux pulse、Windows dshow、檔案轉錄、裝置音量測試及其他擷取呼叫保留既有路徑。
  本次沒有改動會議功能；本節的單來源驗證不能套用到其他路徑。

### 原生 helper 編譯快取（Issue #87）

沿用 `default_state_dir()`，macOS 位置為
`~/Library/Application Support/lecture-notes/native-capture/`，不受課程的 state_dir 設定影響。
鍵是 SHA-256（Swift 原始碼 UTF-8 + NUL + `swiftc --version` 的 stdout／stderr + NUL + 機器架構）。
任一輸入改變就重新編譯。binary 為 `<key>.bin`，另有 `<key>.sha256` 保存內容摘要；
命中時仍需 swiftc 來取得版本，缺 swiftc 的啟動失敗與 doctor FAIL 判定不變。

安全與復原：

- 快取目錄須為目前使用者擁有的真實目錄、權限 0700。父目錄亦不可由其他帳號替換：
  拒絕 symlink、其他擁有者與 group／world 可寫的父目錄（root 擁有的 sticky 暫存目錄例外）。
  不會自行修正既有不安全目錄的權限。
- binary、摘要及 `.lock` 須為目前使用者擁有的一般檔案，不可 group／world 可寫或為 symlink；
  binary 還須有使用者執行權限且摘要相符。寫入的新 binary 為 0700、摘要與 lock 為 0600。
- 跨行程 flock 保護查驗、編譯、發布與 helper 啟動，成功 exec 後釋放。binary／摘要各用私有
  暫存檔、fsync、`os.replace` 原子改名；讀者使用同一把鎖，不會讀到一半的 binary／摘要組。
- 一般毀損、缺少摘要或不可執行的安全檔案會刪除該筆並重編一次；命中後 helper Popen 發生
  OSError 也只刪除／重編／重試一次，再失敗明確回報，沒有無限重試或退回 ffmpeg AVFoundation。
  不安全的擁有者／權限／symlink 檔案則保持原樣、拒用，改在錄音專用暫存目錄編譯並記錄原因。
- 快取不可寫、版本查詢／鎖／發布失敗時使用原有暫存編譯；快取問題不令錄音失敗。
  真正的編譯／啟動錯誤仍明確報錯。每次記錄「使用快取」或「重新編譯」，並保留實際裝置名稱。
- 每次發布新版本，保留本次及另外兩筆最近使用的安全版本；命中更新使用時間。只清理本快取
  格式的安全檔案，不刪不明項目。`MacOSCaptureProcess.cache` 仍只持有錄音專用 TemporaryDirectory，
  `wait()` 不刪持久 binary。helper 原始碼、PCM／stderr 協定、來源選擇與報告、停止與回收均不變。
  Linux pulse／Windows dshow 不查詢版本、不讀寫此快取；CLI／設定／schema 不變。

#### 快取驗證（本台 arm64 Mac）

Hardware test：清空專用快取後，用實際 `./lec run ... --source default --transcribe-only`
與持續低音量測試音各錄超過 1 分鐘。量測外部觀察器只包裝每次 `MacOSCaptureProcess.stdout.read()`，
在第一筆非空 PCM（兩次均 9600 bytes）返回時記 monotonic 時間；起點是啟動 lec 程序前。
此觀察器只在本機測試目錄，不改 helper／產品檔案，數字包含 Python／server／helper／ffmpeg 的啟動時間。
最初測試音與停止參數有誤的 trial 已作廢，不混入下表。

| 量測 | 第一次冷啟動 | 第二次暖啟動 |
|---|---|---|
| 開始至收到第一筆 PCM 秒 | 5.363376833003713 | 2.8879577920015436 |
| 編譯／使用快取 | 是／否 | 否／是 |
| probe audio／span 秒 | 61.233／61.233 | 61.713／61.713 |
| probe lost_seconds／lost_ratio／gap_count | 0／0／0 | 0／0／0 |
| Ogg 完整頁／封包中位數／額外頁時間中位數 ms | 61／50／0 | 61／50／0 |
| native audio_seconds | 61.226666666662055 | 61.706666666662 |
| native pts_span_seconds | 61.22666666668374 | 61.706666666665114 |
| native callbacks | 5740 | 5785 |
| native gap_count／lost_seconds／error | 0／0／空字串 | 0／0／空字串 |
| native wall_seconds | 61.248346500011394 | 61.72502654168056 |
| 總程序牆鐘秒 | 67.05262758300523 | 65.00920866700471 |
| 退出碼／殘留自有子行程 | 0／無 | 0／無 |

兩次 session.log 皆選到內建麥克風，原生報告的 gap_count／lost_seconds 相同且為 0，
`capture_integrity.detail.native_capture` 亦保留，事件 schema_version 仍為 1。
快取目錄／binary 權限均 0700；`codesign -v <cache binary>` exit 0、stdout／stderr 空，
`codesign -dv --verbose=2` 回報 `Signature=adhoc` 與 `adhoc,linker-signed`，是 swiftc linker
產生的內嵌簽章，未另行修改 helper 或自行補簽。

Hardware test（並行）：另清空快取，兩個獨立 PTY 在同一 barrier 後啟動 lec，使用各自 run lock／
whisper port 以免被既有全域單工作鎖拒絕；兩者共享同一使用者 helper 快取。
A 重新編譯，B 使用快取；各錄 60.796 秒、lost_ratio／gap_count 皆 0、封包中位數 50，
native callbacks 各 5699、audio_seconds 各 60.78933333332877、gap_count／lost_seconds 皆 0。
第一筆 PCM 耗時分別為 5.816715083026793／5.816949208994629 秒（B 包含等待 A 編譯時間）。
兩者正常停止、退出 0、無自有子行程殘留。之後快取只有一份 binary、摘要相符、沒有發布暫存檔，
再次 codesign 驗證 exit 0；並行沒有毀損快取。

Automated／Mock test：119 項相關平台、轉錄與 doctor 測試通過；其中 14 項 POSIX 快取 mock
可在 Linux／macOS 執行，不需真正 swiftc 或麥克風。覆蓋二次命中、source／版本／架構改變、
不可寫降級、毀損與 exec OSError 重編一次、重編失敗、uid／權限／symlink（含父目錄、lock、摘要）、
並行完整發布及最近使用版本保留。Windows 只跳過 POSIX 快取 mock，既有 pulse／dshow spawn 契約仍驗證。
完整 `python3 -m unittest`：1059 項、12 skipped、1 個既有 UI loopback ConnectTimeout
（`test_wildcard_accepts_two_local_addresses_and_rejects_forged_host`／127.0.0.2）。
未修改的 base（1dcfc0a）單獨重跑同項仍失敗；沒有改 UI 或繞過測試。
Static validation：Swift 原始碼、A 的來源解析、MacOSCaptureProcess 停止／回收程式碼與 base 逐段比對相同；
Python 編譯與完整 diff 檢查通過。
Not tested：Intel Mac、真實 macOS 升級令編譯器版本改變的重編、沒有 Command Line Tools 的 Mac，
Linux／Windows 真機、NFS／網路家目錄。macOS／Windows 保持實驗中。

### swiftc 檢查與相容性（Issue #89）

只在 macOS 將缺少 swiftc 從 WARN 改為 FAIL；已有 swiftc 為 OK。
Linux／Windows 不新增此項，其他工具的必要／選用判斷保持原樣。
這與原生錄音缺 swiftc 時立即失敗、且不退回 ffmpeg AVFoundation 的行為一致。
README 與 mac_setup.command 提示安裝 Command Line Tools；不自動安裝，也不改 setup 流程。
macOS／Windows 仍標示「實驗中」。

`lec doctor` 的既有規則是「有任一 FAIL 就 exit 1，否則 exit 0」。因此 macOS
只有缺 swiftc、沒有其他 FAIL 時，文字與 `--json` 模式的退出碼均由 0 變成 1。
命令、參數與判定規則不變；JSON 仍是 `[{status, name, detail}, ...]`，不新增鍵或版本。
依賴 exit 0 的環境健康檢查會正確判定此環境尚不能原生錄音；只做檔案轉錄也會收到此診斷。

已檢查呼叫端：UI 的 doctor client 接受 exit 0／1 並讀取同一 JSON 清單；
UI 啟動沒有 doctor 的 FAIL 閘門。setup.py、setup_engines.py、upgrade.py 只印出
下一步執行 doctor 的提示，不依賴「沒有 FAIL」。mac_setup.command 仍傳回 setup.py 的退出碼。
未修改 CLI／UI／設定或狀態 schema。

Manual test（本台 arm64 Mac）：執行 `./lec doctor` 的相關原文如下。

```text
✔ Python              3.14.7
⚠ 作業系統            Darwin 27.0.0（macos）｜實驗中
✔ 錄音後端            avfoundation
✔ swiftc              /usr/bin/swiftc
```

本次隔離 worktree 未放引擎／模型，另有 4 個既存環境錯誤、3 個警告，文字與 JSON 模式
都 exit 1；swiftc 項本身為 OK。`--json` 共 23 項，全部仍只有 status／name／detail 三個鍵。
缺 swiftc 的 FAIL 與提示、只有這一項 FAIL 時的 exit 1／JSON 相容性由 Mock test 驗證；
沒有移除本機 Command Line Tools，也沒有把缺 swiftc 情境當成 Hardware test。

Automated／Mock test：54 項 doctor 目標測試與既有 UI doctor exit 1 相容性測試通過。
本 PR 完整 `python3 -m unittest` 執行 1045 項：12 skipped，只有既有 UI 測試
`test_wildcard_accepts_two_local_addresses_and_rejects_forged_host` 因 `127.0.0.2`
ConnectTimeout 失敗。未修改的 base（59a5d7a）單獨重跑同項仍失敗；沒有修改 UI 繞過它。
Static validation：Python 編譯、`bash -n mac_setup.command` 與完整 diff 檢查通過。
Not tested：Intel Mac、實際未安裝 Command Line Tools 的 Mac、Linux／Windows 真機；
Linux／Windows 無 swiftc 項及其餘診斷狀態不變已由 Mock test 驗證。

### 來源選擇（PR #86 審查 A）

單來源原生擷取不會把未知名稱／編號靜默轉成第一個裝置：在編譯／啟動 helper 前
回報可用裝置名稱。明確指定虛擬裝置仍可使用；完整性檢查不能證明音量或音源正確。
`default`／未設定略過 transport 為 Virtual／Aggregate／AutoAggregate 的項目，
也排除名稱含 Background Music 的項目；目前 ffmpeg 枚舉沒有 transport，使用名稱規則。
若沒有可選項目則明確失敗，不猜測其他虛擬裝置名稱。成功啟動會把實際裝置名稱寫入 session.log。

`default_source("avfoundation")` 同樣使用此規則，影響其既有呼叫者（裝置查詢、doctor、
預設來源解析）的 macOS 預設選擇／顯示；CLI、設定、事件與狀態 schema 不變。
pulse 的系統預設查詢與 dshow 的首項選擇保持原行為。

### 原生擷取報告（PR #86 審查 B）

`MacOSCaptureProcess.wait()` 在成功或失敗路徑都解析 stderr 最後一行
`native capture: {...}`，保留為 `capture_report`；缺少／截斷／不合法報告不讓 wait 拋例外，
也不改寫 ffmpeg 真實退出碼。收尾會將 audio_seconds、pts_span_seconds、callbacks、gap_count、
lost_seconds、wall_seconds、error 的原始值寫入 session.log，並放進既有
`capture_integrity` 事件的 `detail.native_capture`。頂層 probe 數字、run 回傳值、
status.json 與 events.jsonl schema 版本均不變。

原生 PTS 缺口不受 Ogg 頁界呈現影響：只要 gap_count > 0 就警告；遺失比例
lost_seconds / pts_span_seconds 達 0.5% 為警告，達 2% 為錯誤並記錄 status.error，
與 probe_capture 同門檻。未保留錄音／ffprobe 無報告時仍寫原生 log 與警告，但不偽造存檔事件。

### 實際 `lec` 驗證

短測（Hardware test）：錄音 61.937 秒，audio/span 同長、lost_ratio 0、gap_count 0，
完整 ogg 頁封包數中位數 50、多出時間中位數 0 ms。`lec stop` 退出 0、phase done、
擷取完整性事件已寫入，無本次子行程殘留。

強制停止（Hardware test）：發出 `lec stop --force` 時 elapsed 11.1 秒，退出 130、phase aborted，
無本次子行程殘留；**沒有**自動完整性事件。事後量到 12.156 秒、lost_ratio 0，這不代表強制停止保證存檔完整。

長測（Hardware test）：`lec` 實際錄到 **1862.865 秒（31 分 2.865 秒）**，audio/span
皆 1862.865 秒、lost_seconds 0、lost_ratio 0、gap_count 0。完整 ogg 頁 1,862 頁，
封包數中位數 50、頁多出時間中位數 0 ms；自動 `capture_integrity` 事件同樣回報 0 遺失。
發出 `lec stop` 時 elapsed 1860.3 秒、牆鐘 1866.852329 秒；包含載入與收尾／行程檢查的總牆鐘
1871.748083 秒。退出碼 0、phase done、errors 0，沒有本次子行程殘留。達成本台的修好門檻。

長測使用相同的正常 PCM／PTS 擷取路徑；錄音期間另外補了 writer 已失敗時不等待塞住 pipe 的
收尾保護。這項失敗路徑以合成堵塞 pipe 測試驗證，不當作麥克風硬體證據。
原始修復提交版本的額外短測（Hardware test）：音訊／跨距 62.247 秒、遺失與缺口皆 0，完整頁 62 頁，
封包中位數 50、多出時間中位數 0 ms；stop 時 elapsed 60.3 秒、牆鐘 66.672350 秒，總牆鐘
71.065830 秒。完整性事件有寫入、退出 0、done、errors 0，無本次子行程殘留。

Automated / Mock test（A／B 後）：102 項平台、擷取完整性、轉錄失敗與 doctor 目標測試通過，包含 Swift／ffmpeg
合成 PCM 時間戳互通、planar 交錯與堵塞 pipe 的失敗收尾。Static validation：Python 編譯與 diff 檢查通過。
Manual test：`lec doctor` 仍顯示 macOS 實驗中，能找到 swiftc；CLI help／停止參數不變。

完整 suite 使用隔離環境補齊 UI 依賴、設定 `TMPDIR=/private/tmp` 並允許 loopback 後，
A／B 後重新執行完整 `python3 -m unittest`：1042 項中有 1 個既有 UI 測試 `test_wildcard_accepts_two_local_addresses_and_rejects_forged_host`
因本機 `127.0.0.2` 連線逾時失敗，另 12 項 skipped。未修改的 origin/main（ce41e1f）相同測試
亦為 ConnectTimeout，與錄音 patch 無關；沒有修改 UI／會議碼去繞過它。原生失敗契約、
來源選擇與報告保留測試均包含在這次完整測試與 102 項通過的目標測試。完成全套驗證需要裝好 `ui/backend/requirements.txt` 與 httpx，
在支援這兩個 loopback 位址的 Linux／Windows 環境執行 `python3 -m unittest`。
macOS 另外需使用 canonical `TMPDIR=/private/tmp`，避免既有會議測試的 `/var` symlink 路徑斷言差異。

Windows basic／microphone：**Not tested**。固定 main workflow 僅接受 repository owner 派送，
WRITE contributor 的派送會 skipped，不代表 PC 已執行測試。需由 owner 以 primary repository 的
trusted branch 與完整 SHA 派送；microphone 另需 PC 使用者開啟有效測試時段。PR 記錄派送與摘要結果，
未取得對應 SHA／request ID 的通過摘要前不得視為通過。Linux 真機、其他 Mac、USB／iPhone、
ffmpeg@7、闔蓋／休眠與 TCC 拒絕情境都未在此修復驗證；不據此宣稱跨平台硬體全面支援。

### A／B 審查修正的實機驗證（2026-10-14）

Hardware test：arm64、macOS 27.0.1（26A434）、ffmpeg 9.0.1，實際使用
`lec run ... --source default --transcribe-only`，播放經使用者授權的低音量測試音。
裝置枚舉首項為 Background Music（index 0），另有 Background Music (UI Sounds)（index 2）；
`default_source()`、`resolve_source("default")` 與 session.log 均確認選到內建麥克風（index 1）。

| 量測 | 原始數字 |
|---|---|
| probe audio_seconds／span_seconds | 302.407／302.407 秒 |
| probe lost_seconds／lost_ratio／gap_count／max_gap_seconds | 0／0／0／0 |
| Ogg 完整頁數／每頁封包中位數／額外頁時間中位數 | 302／50／0 ms |
| native audio_seconds／pts_span_seconds | 302.40000000020115／302.3999999999942 秒 |
| native callbacks／gap_count／lost_seconds／error | 28350／0／0／空字串 |
| native wall_seconds | 302.41959916666383 秒 |
| 停止時 elapsed／程序牆鐘／總程序牆鐘 | 301.5／307.15267949999543／313.0282460839953 秒 |
| CLI 退出碼／最終 phase／errors／殘留自有子行程 | 0／done／0／無 |

原生報告在 session.log 與 `capture_integrity.detail.native_capture` 的數字一致；
事件 schema_version 仍為 1，頂層 probe 數字沒有被原生報告覆蓋。
Mock／Automated test 另驗證未知來源不啟動 helper、明確虛擬裝置可選、成功報告有 gap 時
保留並寫入事件與日誌、缺報告不影響結果、低比例原生 gap 警告與 0.5%／2% 門檻。

Not tested：Bluetooth／錄音中取樣率切換（#88）、擷取架構後續設計（#90）、人工驗收清單（#91），
沒有在這次補修實作。#87 helper 快取與 #89 doctor／README 等 #86 合併後另開 PR。

### 重做隔離實驗

只用 **bash** 執行；錄音期間要有持續聲音。每輪重新確認來源，避免跨行程裝置編號差異。
FFmpeg 支援以下以裝置名稱做隔離實驗；程式既有來源設定與裝置列表介面保留。
音訊編碼選項必須放在 `-i` **之後**，`thread_queue_size` 等輸入選項放在其前。

```bash
NAME='MacBook Pro的麥克風'
ffmpeg -nostdin -hide_banner -loglevel verbose -debug_ts -f avfoundation -i ":$NAME" -t 60 -c:a pcm_s16le /tmp/control.wav
ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/control.wav
ffmpeg -nostdin -hide_banner -loglevel verbose -debug_ts -f avfoundation -thread_queue_size 4096 -i ":$NAME" -t 60 -c:a pcm_s16le /tmp/queue.wav
```

系統／原生對照、另一個輸入與版本對照仍可用來追查其他 Mac 的不同結果；移除 HAL 驅動或
安裝 ffmpeg@7 前要先得到使用者同意。錄音與診斷日誌只留本機，不上傳錄音內容。

ogg 每頁封包數與多出來的時間（正常錄音每頁 50 個封包、多出來的時間 0 ms）：

```python
#!/usr/bin/env python3
import statistics, struct, sys
def pages(path):
    data, i, out = open(path, "rb").read(), 0, []
    while True:
        i = data.find(b"OggS", i)
        if i < 0 or i + 27 > len(data): break
        gp, nseg = struct.unpack_from("<q", data, i + 6)[0], data[i + 26]
        lac = data[i + 27:i + 27 + nseg]
        out.append((gp, sum(1 for v in lac if v < 255)))
        i += 27 + nseg + sum(lac)
    return [p for p in out if p[0] >= 0][2:]
for path in sys.argv[1:]:
    pg = pages(path)
    extra = [(g1 - g0) - n1 * 960 for (g0, _), (g1, n1) in zip(pg, pg[1:])]   # 960 = 20 ms @ 48 kHz
    npk = [n for _, n in pg[1:]]
    print(f"{path}: {len(pg)} 頁；每頁封包數 median={statistics.median(npk):.0f}（滿載 50）；"
          f"每頁多出來的時間 median={statistics.median(extra)/48:.1f} ms（正常 0）")
```

### 「已修好」的定義

在這台 Mac 上實際錄 **≥ 30 分鐘**，`probe_capture` 的 `lost_ratio` < 0.005（理想是接近 Linux 的 < 0.0006），
而且每頁封包數接近 50。少於這個不得宣稱修好。修好之前，**不要在 Mac 上錄任何需要事後處理的內容**；
驗證用的樣本請在 Linux 錄。

## 麥克風權限（TCC）

macOS 10.14+ 對麥克風有系統層級的權限管制：

- **列出**裝置名稱通常不需要權限（`AVCaptureDevice.devices()` 本身不觸發
  TCC 檢查），所以 `lec devices` 理論上即使沒給權限也能看到麥克風清單。
- **實際錄音**（`lec devices --test`、`lec doctor --mic`、`lec run`）第一次
  執行時，系統應該會跳出「是否允許『終端機』/`python3` 使用麥克風」的對話框。
  在沒有互動式視窗的情況下（例如透過 SSH 執行、或已經拒絕過一次），ffmpeg
  可能會卡住直到逾時，而不是立刻回報錯誤。
- 這次修改在 `core/devices.py` 加了 `_permission_hint()`：`test_volume()`
  在 avfoundation 逾時，或 ffmpeg 輸出常見的拒絕字樣（`not authorized`、
  `Operation not permitted`、`Input/output error`）時，會多附一句「請至
  系統設定 > 隱私權與安全性 > 麥克風」的提示。**這是啟發式判斷**，實際
  ffmpeg 版本印出的錯誤文字可能不完全一樣，需要在真機上核對並視情況調整
  `_PERMISSION_HINTS` 裡的關鍵字。

## 防止休眠

- 用 `caffeinate -i -m -s -w <lec 的 pid>`，`lec` 結束時 `caffeinate` 也會跟著結束
  （`-w` 是等待該 pid 結束），`Inhibitor.stop()` 另外主動 terminate 一次。
- 只防止系統睡眠，不擋螢幕關閉（沒有加 `-d`），跟目前的行為說明一致
  （`▶ 已阻止休眠`，不像 Linux 訊息會提到「蓋螢幕」）。
- 沒裝 Xcode Command Line Tools 時 `caffeinate` 應該仍然存在（系統內建），
  真正可能缺的是 `ffmpeg`／`cmake`／`opencc`（`brew install`）。

## GPU 後端（Metal）

- 預設 Metal（`DEFAULT_BACKEND["macos"] = "metal"`）。
- 編譯需要 Xcode Command Line Tools（`xcode-select --install`）。編譯工具的判斷在
  `core/platform.py::missing_build_tools()`（`setup_engines.py` 與 `lec doctor` 共用）：
  Metal 後端只要求「有 c++／clang++／g++ 其中之一」，沒有另外檢查 Metal 編譯器
  （`xcrun metal`）是否可用——**這點只在真機上才能確認**，如果日後發現 Metal 編譯失敗
  但事前檢查沒攔下來，就在這個函式裡加檢查。
- `lec doctor` 會多一行「編譯工具」顯示目前後端與缺少的工具；缺工具只算警告
  （已經編好引擎或用預編譯檔的人不需要編譯環境）。macOS 上這一行目前只有
  mock 測試（`tests/test_doctor_platform.py`），沒有實機輸出可以核對。
- Apple Silicon 與 Intel Mac 應該都能用同一套流程編譯（CMake 會自動判斷
  架構），差別主要在 Homebrew 安裝路徑不同（Apple Silicon 預設
  `/opt/homebrew`、Intel 預設 `/usr/local`）：如果 `brew install` 完後
  `shutil.which("ffmpeg")` 等找不到工具，通常是沒有把 `brew shellenv` 加進
  shell 設定檔，不是程式的 bug。

## 執行檔／動態函式庫搜尋

- `find_engine_bin()` 在 macOS 用跟 Linux 一樣的搜尋順序（`build/bin/`），因為
  用 Makefile／Ninja 產生器時輸出結構相同；不會用到 `bin/Release` 這種
  Visual Studio 專屬路徑。
- `library_dirs()` 找 `lib*.dylib`；因為 `setup_engines.py` 預設靜態編譯
  （`BUILD_SHARED_LIBS=OFF`），一般情況下這裡會是空的，`env_with_libs()`
  也就不會動到 `DYLD_LIBRARY_PATH`。
- **刻意不做**的檢查：沒有比照 Linux 的 `ldd` 加上 `otool -L` 相依函式庫檢查。
  macOS 11+ 的系統函式庫大多收在 dyld shared cache 裡，檔案系統上看不到實際
  檔案，`Path(...).exists()` 對這些系統函式庫會回傳 `False`，如果拿來當「找
  不到函式庫」的判斷依據，反而會對完全正常的編譯結果誤判為錯誤。如果之後
  真的要做，需要用 `otool -L` 的輸出加上「已知系統路徑一律視為存在」的白名單，
  而不是單純檢查檔案是否存在。

## Ctrl+C / 行程收尾

- 跟 Linux 走同一條 POSIX 路徑（`signal.SIGINT`／`os.killpg`），理論上行為
  應該一致，因為 macOS 也是 POSIX 系統。

## 實機驗證流程（第一次在 Mac 上跑時照這個順序）

下面這份是「還需要哪些實機測試」那一節的可執行版本：照順序跑，把**輸出原文**帶回來，
就能把上面那些「靜態驗證／mock」的項目一項一項換成實機結論。分兩輪：

- **第一輪（步驟 1–7）**只需要 whisper.cpp 與 1.6GB 的 whisper 模型，不用下載 5GB 的
  Qwen3-8B，也不需要能上課。平台層（裝置、權限、編譯、防休眠、停止）都在這一輪驗完。
- **第二輪（步驟 8）**才需要 LLM 模型，驗總結與效能。

> zsh 提醒：本節的指令區塊刻意不含 `#` 註解——zsh 預設沒開 `interactivecomments`，
> 貼上帶 `#` 的行會被當成指令而報錯（見上面「安裝時的 macOS 差異」）。

### 1. 安裝與編譯（對應風險項目 3）

依 [mac_setup.md](mac_setup.md) 裝好 Homebrew 套件與 `lec` 之後：

```bash
python3 setup_engines.py whisper
ls -l whisper.cpp/build/bin
```

要看的東西：

- `檢查編譯工具` 那一步有沒有誤報缺東西（只裝 Command Line Tools、沒裝完整 Xcode 的
  Mac 也應該通過；判斷在 `core/platform.py::missing_build_tools()`）。
- cmake 有沒有真的用 Metal 編（`-DGGML_METAL=ON`）、build 產物是不是落在 `build/bin/`
  （`find_engine_bin()` 的假設，風險項目 5）。
- 之後要驗總結才需要 `python3 setup_engines.py llama`，第一輪可以先跳過。

### 2. 環境檢查

```bash
./lec doctor
python3 -m unittest
```

`lec doctor` 要注意：

- 「作業系統」應該是 `✓ … 正式支援`。
- 「錄音後端」應該是 `avfoundation`。
- 「編譯工具」應該是 `metal 後端（…）：齊全`；顯示「已編譯的後端」表示讀到了
  `whisper.cpp/build/.lec-build`。
- 「GPU」那一行要等 llama.cpp 也編好才會出現，應該列出 Metal 裝置（風險項目 3）。
- `python3 -m unittest` 在 Mac 上也應該全綠——它會走到 `default_state_dir()` 的
  `~/Library/Application Support` 分支等 macOS 專屬路徑。只有
  `tests/test_ui_backend_*.py` 失敗的話是沒裝 fastapi（見 CLAUDE.md），不是程式壞掉。

### 3. 裝置列表與 parser（對應風險項目 1）

```bash
ffmpeg -hide_banner -f avfoundation -list_devices true -i ""
./lec devices
./lec devices --json
```

**第一個指令的輸出原文是這一輪最重要的素材**：`_avfoundation_sources()` 目前假設的格式
（`AVFoundation audio devices:` 區段標題 + `[編號] 名稱`）完全是從文件推導的。Windows 的
dshow 就是因為沒有實機原文而解析錯一整版（見 `docs/platform-windows.md` 的「更正」），
拿到原文之後才能把它寫成 `AvfoundationSourcesTests` 的迴歸素材。

失敗徵兆：`lec devices` 一個裝置都列不出來、名稱被截斷、中文變亂碼、或藍牙耳機
／iPhone 麥克風（Continuity）沒被列進來。有內建麥克風以外的裝置（USB 麥、藍牙耳機）
的話，插上去再跑一次，兩份輸出都帶回來。

### 4. 麥克風權限（對應風險項目 2）

```bash
./lec devices --test default
```

第一次執行會跳出系統的麥克風權限對話框（TCC）。兩種情況都要記錄：

1. **允許**之後應該印出 `平均 … dB、峰值 … dB`。如果是逾時而不是拿到 dB 值，把訊息
   原文帶回來。
2. 然後刻意到「系統設定 > 隱私權與安全性 > 麥克風」把權限關掉，再跑一次同一個指令，
   **把錯誤訊息原文帶回來**。`core/devices.py::_PERMISSION_HINTS` 猜的關鍵字
   （`not authoriz`、`Operation not permitted`、`Input/output error`）就是要靠這段原文
   校正——dshow 那邊就是因為關鍵字猜太寬，把「裝置不存在」誤報成權限問題。

測完記得把權限開回來。

### 5. 轉錄一輪（不需要 LLM）

```bash
./lec run mac測試 --file samples/test8min.ogg --transcribe-only
```

跑完比對 `outputs/mac測試_*/transcript.md` 與 `samples/expected/transcript.md`：不會逐字
相同（whisper 本身有隨機性），要看的是時間小標題的段數差不多、沒有整段空白或亂碼。
順便記下處理 8 分鐘音檔花多久（Linux 開發機的 whisper large-v3-turbo 約 6–7x 即時）。

### 6. 防休眠（對應風險項目 4）

開著 `./lec run mac測試2`（麥克風即時錄音）的時候，另一個終端機執行：

```bash
pmset -g assertions | grep -i -e PreventUserIdleSystemSleep -e PreventSystemSleep
./lec status
```

應該看到 `caffeinate` 持有的 assertion。接著蓋上螢幕等一兩分鐘再打開，用 `./lec status`
確認錄音沒有中斷（`Inhibitor` 用的是 `caffeinate -i -m -s -w <pid>`）。

### 7. 停止機制與行程收尾（三平台一致的 public contract）

錄音進行中，在另一個終端機：

```bash
./lec stop
./lec status
pgrep -fl "whisper-server|llama-server|ffmpeg"
```

`lec stop` 是在輸出資料夾寫 `stop` 檔（不是送 signal），phase 應該走
`recording → finishing → done`，而且 `pgrep` 最後不該再留下 whisper-server ／ ffmpeg。
再測一次 `./lec stop --force`（寫 `stop_force`，應該立刻結束）。

### 8. 總結與效能（第二輪，需要 Qwen3-8B）

依 [mac_setup.md](mac_setup.md) 下載 `models/Qwen3-8B-Q4_K_M.gguf` 並 `python3 setup_engines.py llama` 之後：

```bash
./lec run mac測試3 --file samples/test8min.ogg
```

要看的是 `lec doctor` 的 GPU 那行有沒有列出 Metal、summarizing phase 有沒有正常推進、
`notes.md` 的內容跟 `samples/expected/notes.md` 是否同一個量級，以及**每段總結耗時**
（Linux + Arc 140V 是 77–95 秒／段，Apple Silicon 的數字會寫進 README 的效能表）。

### 一次收集所有輸出

第一輪跑完之後，用這段把環境資訊與各指令輸出收進一個檔案，回報時附上它：

```bash
R=~/lec-macos-report.txt
{
  echo "== sw_vers =="; sw_vers
  echo "== uname -m =="; uname -m
  echo "== python3 =="; python3 -V
  echo "== ffmpeg =="; ffmpeg -version | head -2
  echo "== cmake =="; cmake --version | head -1
  echo "== avfoundation 原文 =="; ffmpeg -hide_banner -f avfoundation -list_devices true -i ""
  echo "== lec devices --json =="; ./lec devices --json
  echo "== lec doctor =="; ./lec doctor
  echo "== build/bin =="; ls -l whisper.cpp/build/bin llama.cpp/build/bin
  echo "== build stamp =="; cat whisper.cpp/build/.lec-build
  echo "== otool -L =="; otool -L whisper.cpp/build/bin/whisper-server
  echo "== unittest =="; python3 -m unittest 2>&1 | tail -5
} > "$R" 2>&1
echo "寫到 $R"
```

另外單獨帶回來的（上面那個檔案收不到的）：

- 步驟 4 第 2 種情況（關掉麥克風權限後）`./lec devices --test default` 的錯誤訊息原文
- 步驟 5 的 `transcript.md` 前 30 行左右，以及處理花了多久
- 步驟 6 的 `pmset -g assertions` 輸出、蓋螢幕前後 `./lec status` 的差異
- 步驟 7 `lec stop` 之後 `pgrep` 還有沒有殘留的行程

## 還需要哪些實機測試

以下项目目前**只有靜態驗證或 mock test**，沒有在真正的 Mac 上跑過，是這次
沒有 macOS 實機時最大的風險來源：

0. **擷取完整性（本台單來源長測通過；其他環境待驗）**（見「擷取掉音訊」一節）。本機已定位 ffmpeg 單格音訊暫存與
   10 ms 輪詢造成的 buffer 覆寫；直接 ffmpeg WAV 仍會遺失。原生 callback 的課堂單來源修復在本台錄了
   31 分 2.865 秒、lost_ratio 0、每頁封包中位數 50。其他 Mac／輸入來源未驗證；**在對應環境確認之前，在 Mac 上不要錄需要事後處理的內容。**
1. `ffmpeg -f avfoundation -list_devices true` 的實際輸出格式（尤其是中文
   裝置名稱、藍牙耳機等特殊裝置）是否跟這裡假設的一致。
2. 第一次執行時的麥克風權限對話框行為、以及 `_permission_hint()` 猜的錯誤
   字樣是否對得上真實 ffmpeg 版本印出的文字。
3. `setup_engines.py` 在 Apple Silicon 與 Intel Mac 上實際編譯
   whisper.cpp / llama.cpp（Metal 後端）是否成功、`--list-devices` 是否正確
   回報 Metal 裝置；以及 `lec doctor` 的「編譯工具」那一行在只裝了 Command Line
   Tools（沒裝完整 Xcode）的 Mac 上是否判斷正確。
4. `caffeinate` 阻止休眠期間，蓋上螢幕（筆電）是否真的不會中斷背景編譯／錄音。
5. `find_engine_bin()` / `library_dirs()` 假設的 build 產物佈局（`build/bin/`）
   是否跟 whisper.cpp / llama.cpp 目前版本在 macOS 上的實際輸出一致。

要驗這些項目的話，照上面「實機驗證流程」那一節跑，不用自己想順序；
每一步都標了對應的風險項目編號。

2026-10-20 起 README 與 `lec doctor` 將 macOS 標為正式支援；上面各項仍是發生問題時優先查的方向。
