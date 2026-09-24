# macOS 平台備忘（實驗中，尚未實機驗證）

狀態：**程式已支援，尚未在真正的 Mac 上跑過**。以下內容大多是靜態驗證
（讀 ffmpeg / AVFoundation 官方行為推導）與 mock test，不是「已測過沒問題」。
第一次在真機上跑之前請先看完「還需要哪些實機測試」一節。

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

在沒有實機驗證之前，README 與 `lec doctor` 都刻意維持「實驗中」而不是
「已支援」的措辭，請不要在這些項目確認之前把 macOS 改標成正式支援。
