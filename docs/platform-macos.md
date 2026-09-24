# macOS 平台備忘（實驗中，尚未實機驗證）

狀態：**程式已支援，尚未在真正的 Mac 上跑過**（只有 README 的安裝步驟是實機回報）。
以下內容大多是靜態驗證（讀 ffmpeg / AVFoundation 官方行為推導）與 mock test，
不是「已測過沒問題」。第一次在真機上跑之前請先看完「還需要哪些實機測試」一節；
要實際驗的時候照「實機驗證流程」那一節的步驟跑。

## 安裝時的 macOS 差異（實機回報，2026-09）

在 MacBook 上照 README 安裝時遇到的三個問題，都已經寫進 README 的安裝步驟：

- **zsh 把 `#` 當參數**：zsh 預設沒開 `interactivecomments`，貼上
  `python3 setup_engines.py whisper    # Linux/macOS；…` 時 `#` 和後面的字都被當成
  參數，`setup_engines.py` 回報「未知的引擎：#」。解法是只複製指令本身，或
  `setopt interactivecomments`。
- **`~/.local/bin` 不存在、也不在 PATH**：這是 Linux（XDG）的慣例，macOS 沒有。
  `ln -s … ~/.local/bin/lec` 會回報 `No such file or directory`。要先 `mkdir -p`，
  再把它加進 `~/.zshrc` 的 PATH。
- **內建 `python3` 太舊**：Command Line Tools 附的 `python3` 通常是 3.9，`lec` 需要
  3.11+（`tomllib`）。README 的 brew 指令已加上 `python`。

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

依 README 的「安裝」章節裝好 Homebrew 套件與 `lec` 之後：

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

- 「作業系統」應該是 `⚠ … 實驗中`（在這輪驗完之前刻意保持這個措辭）。
- 「錄音後端」應該是 `avfoundation`。
- 「編譯工具」應該是 `metal 後端（…）：齊全`；顯示「已編譯的後端」表示讀到了
  `whisper.cpp/build/.lec-build`。
- 「GPU」那一行要等 llama.cpp 也編好才會出現，應該列出 Metal 裝置（風險項目 3）。
- `python3 -m unittest` 在 Mac 上也應該全綠——它會走到 `default_state_dir()` 的
  `~/Library/Application Support` 分支等 macOS 專屬路徑。

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

依 README 下載 `models/Qwen3-8B-Q4_K_M.gguf` 並 `python3 setup_engines.py llama` 之後：

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

在沒有實機驗證之前，README 與 `lec doctor` 都刻意維持「實驗中」而不是
「已支援」的措辭，請不要在這些項目確認之前把 macOS 改標成正式支援。
