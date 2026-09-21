# Windows 平台備忘（實驗中，尚未實機驗證）

狀態：**程式已支援，尚未在真正的 Windows 上跑過**。這次修改在程式碼審查中
發現並修正了一個會讓 Windows 麥克風清單永遠是空的解析 bug（見下方），但因為
沒有 Windows 實機，只能用假造的 ffmpeg 輸出做 mock 測試，**沒有**在真正的
Windows 環境驗證過。

## 這次修好的 bug：DirectShow 麥克風列表永遠是空的

`core/platform.py::_dshow_sources()` 原本的 regex 假設 ffmpeg 列出裝置時，
名稱後面會標註 `"裝置名稱" (audio)`：

```python
_DSHOW_NAME = re.compile(r'"([^"]+)"\s*\((audio|video)\)')
```

但 ffmpeg 實際的 `-f dshow -list_devices true` 輸出是先印一行**區段標題**
（`DirectShow video devices` / `DirectShow audio devices`），裝置本身只印
`"名稱"`，並不會在名稱後面加註 `(audio)`／`(video)`：

```
[dshow @ 0x...] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0x...]  "Integrated Webcam"
[dshow @ 0x...]     Alternative name "@device_pnp_...#{...}"
[dshow @ 0x...] DirectShow audio devices
[dshow @ 0x...]  "麥克風陣列 (Realtek(R) Audio)"
[dshow @ 0x...]     Alternative name "@device_cm_{...}\wave_{...}"
```

舊的 regex 在這種真實輸出上永遠 match 不到（`tests/test_platform_parsers.py`
裡的 `DshowSourcesTests` 用同樣的字串證實了這點），也就是說**在真正的
Windows 上，`lec devices` 應該會回報找不到任何麥克風**，即使 ffmpeg 有正確
列出裝置。這應該是 Windows 目前還沒實測出問題、但邏輯上一定會炸的地方。

修好後的解析方式改成：先追蹤目前在哪個區段（`video` / `audio`），只在
`audio` 區段內把 `"名稱"` 當成一個裝置，下一行如果有 `Alternative name`
就把裝置路徑存成 `id`（比顯示名稱穩定，不受中文、逗號、重複名稱影響）。

## 錄音（DirectShow）

- 後端固定用 `dshow`。
- 存進設定檔的 `id` 優先用 `Alternative name`（例如
  `@device_cm_{33D9A762-...}\wave_{...}`），這個字串不含中文或逗號，交給
  ffmpeg 當 `-i audio=<id>` 時不需要額外處理跳脫字元；只有在裝置沒有
  Alternative name 時才會退回用顯示名稱本身當 `id`。
- 重複裝置名稱（兩個麥克風顯示同一個名字）沒問題：因為 `id` 用的是
  Alternative name／裝置路徑，兩個裝置的路徑一定不同，`lec devices --save`
  存的是 `id` 不是顯示名稱，所以不會選錯。
- ffmpeg 的參數是用 Python list（`subprocess` 的 argv），不是 shell 字串，
  所以裝置名稱裡的空白、逗號、中文都不需要額外加引號或跳脫。

## UTF-8 主控台輸出

- `core/platform.py::force_utf8()` 會把 `stdout`/`stderr` 重新設定成 UTF-8，
  這是避免 Windows 預設主控台編碼（多半是 cp950 / cp936 之類）把中文字和
  `✔`/`✖`/`⚠` 印成亂碼的關鍵。
- **這次順便修的另一個 bug**：`force_utf8()` 原本只有 `setup_engines.py` 會呼叫，
  真正每天在用的 `lec` 指令本身完全沒呼叫過，等於編譯階段的中文正常、
  但 `lec run` / `lec doctor` 等日常指令在 Windows 上很可能還是亂碼。
  已經在 `lec`（CLI 入口）裡補上 `P.force_utf8()`，在 import `core.cli` 之後、
  執行任何指令之前就先重設好編碼。

## 子行程的文字編碼（實機回報，2026-09）

`lec run 測試課 --file samples/test8min.ogg` 在 Windows 上轉錄到 00:03:13 那段時印出
`UnicodeEncodeError: 'cp950' codec can't encode character '\u6269'`，那一段轉錄花了
20.7 秒（其他段不到 1 秒），而且沒轉成繁體。

原因：`subprocess.run(..., text=True)` 沒指定 `encoding` 時會用系統 locale 編碼，
繁體中文 Windows 是 cp950（Big5）。whisper 偶爾輸出簡體字（「扩」U+6269），
這個字不在 cp950 裡，送進 opencc 的寫入執行緒就丟出例外、stdin 沒被關閉，
opencc 一直等到 20 秒逾時，`opencc_convert()` 只好回傳沒轉換的原文。

修正：`core/` 與 `setup_engines.py` 裡所有 `text=True` 的 subprocess 呼叫都明確指定
`encoding="utf-8"`（opencc、ffmpeg、whisper/llama-server、git 的輸出都是 UTF-8）。
`tests/test_platform_subprocess_encoding.py` 有一個靜態檢查，之後新增的
`text=True` 呼叫如果忘了指定 encoding，測試會失敗。

## 行程與停止

- Windows 沒辦法對別的行程送 `SIGINT`（`interrupt()` 直接回傳 `False`），
  所以 `lec stop` 與 UI 一律走 `stop` / `stop_force` 檔案（跟 macOS / Linux
  介面一致，見主 README「停止方式」），不依賴任何 Windows 專屬的訊號機制。
- `kill_tree()` / `kill_now()` 用 `taskkill /PID <pid> /T /F` 連同子行程一起關閉。
- `spawn_kwargs()` 用 `CREATE_NEW_PROCESS_GROUP`，讓 whisper-server /
  llama-server 不會被终端機的 Ctrl+C 直接波及。

## 狀態資料夾與執行檔搜尋

- `default_state_dir()` 用 `%LOCALAPPDATA%\lecture-notes`；沒設定這個環境變數
  時退回 `~\AppData\Local\lecture-notes`。
- `find_engine_bin()` 會依序找 `build/bin/`、`build/bin/Release/`、
  `build/Release/`、`build/bin/Debug/`，涵蓋 Ninja（single-config，輸出在
  `build/bin/`）與 Visual Studio（multi-config，輸出可能在 `bin/Release`
  或 `Release`）兩種常見佈局。**沒有實機驗證過** llama.cpp / whisper.cpp
  目前版本在 Windows 上實際會落在哪一種——如果之後發現都找不到，请先用
  `dir /s whisper-server.exe` 確認實際路徑，再回來調整搜尋順序。
- `library_dirs()` 找 `*.dll` 加進 `PATH`（Windows 用 `PATH` 找 DLL，不是
  `LD_LIBRARY_PATH`），這是給「下載官方預編譯檔」這種情境用的（見主
  README「Windows 預編譯檔」一節），預設的靜態編譯流程通常用不到。

## CMake generator／編譯環境

**實機回報（2026-09，Windows + `--backend cuda`）**：`check_tools()` 顯示「工具齊全」，
但 cmake 印出 `Building for: NMake Makefiles`，接著 `nmake -?` 找不到、
`CMAKE_C_COMPILER not set` 而失敗。原因是舊版 `check_tools()` 只要
`Program Files (x86)\Microsoft Visual Studio` 資料夾存在就算有編譯器——只裝了
VS Installer、沒勾「使用 C++ 的桌面開發」時這個資料夾也存在；cmake 找不到可用的
Visual Studio 就退回 NMake，而一般 PowerShell 裡沒有 `nmake`／`cl`。

修正後的行為：

- `find_msvc()` 用 `vswhere.exe -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64`
  確認真的有裝 MSVC；`check_tools()` 要求 `cl` 在 PATH 上（Developer Command Prompt）
  或 vswhere 找得到，否則直接報「缺少：Visual Studio 的『使用 C++ 的桌面開發』工作負載」。
  只有 `ninja` 不算（ninja 不是編譯器）。
- `pick_generator()`：沒有 `--generator`、又不在 Developer Command Prompt 時，依 vswhere
  回報的 VS 主版本明確指定 `Visual Studio 16 2019`／`17 2022`／`18 2026`，並先用
  `cmake --help` 確認這版 cmake 認得它；不認得（例如 VS 2026 配舊版 CMake）就提早
  叫你升級 CMake，而不是讓 cmake 默默退回 NMake。對照表以外的更新版本交給 cmake 自己判斷。
- cmake 設定階段失敗時，Windows 會額外印出常見原因（缺 C++ 工作負載、CMake 太舊、
  CUDA 沒整合進 Visual Studio）與改用 `--generator Ninja` 的做法。
- `--generator <NAME>` 仍可手動指定，優先於自動選擇。build stamp 只記錄手動指定的
  generator，自動選的不記，讓 `--lock` 與實際編譯寫出的 stamp 一致。
- CUDA + Visual Studio generator 需要 CUDA Toolkit 的 Visual Studio Integration
  （安裝 CUDA 時若 VS 尚未安裝就不會裝上）。沒有的話可以在「x64 Native Tools Command
  Prompt for VS」裡執行 `python setup_engines.py --backend cuda --generator Ninja`
  （VS 內附 Ninja）。

## doctor 訊息

- `lec doctor` 對 Windows 的錄音來源錯誤，會透過 `devices.hint()` 提示
  「請確認已安裝 ffmpeg 並在 PATH 中」；`core/devices.py` 新增的
  `_permission_hint()` 對 dshow 也準備了「Access is denied」等常見拒絕字樣的
  啟發式判斷，但這部分**沒有 Windows 麥克風權限對話框的實機文字可以核對**，
  關鍵字可能需要之後根據真實錯誤訊息調整。

## 還需要哪些實機測試

1. `_dshow_sources()` 對真實 ffmpeg 版本輸出的解析（尤其是筆電內建麥克風、
   USB 麥克風、藍牙耳機等不同裝置類型的顯示格式）。
2. `lec run` 實際錄音、`stop` / `stop_force` 檔案觸發停止、`taskkill /T /F`
   是否真的把 whisper-server / llama-server 及其子行程都關乾淨。
3. `force_utf8()` 補上之後，`cmd.exe` 與 PowerShell 兩種主控台下中文與
   ✔/✖/⚠ 是否都正常顯示（不同主控台對 UTF-8 的支援程度不完全一樣）。
4. `setup_engines.py` 在乾淨 Windows 環境（只裝 Visual Studio Build Tools，
   或只裝 Ninja + Developer Command Prompt）分別編譯 whisper.cpp / llama.cpp
   是否成功，`find_engine_bin()` 找到的路徑是否正確。
5. Vulkan SDK / CUDA Toolkit 偵測邏輯（`VULKAN_SDK` 環境變數、`nvcc`）是否
   跟實際安裝後的環境變數狀態一致。
6. 官方預編譯檔（README「Windows 預編譯檔」）搭配 `library_dirs()` /
   `env_with_libs()` 的 DLL 搜尋路徑是否真的能讓 `lec doctor` 找到並成功執行。

在以上項目至少跑過一輪之前，README 與 `lec doctor` 都刻意維持「實驗中」而
不是「已支援」的措辭，請不要在這些項目確認之前把 Windows 改標成正式支援。
