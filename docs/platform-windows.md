# Windows 平台備忘（實測中）

狀態：**實測中**。已在 Windows 實機跑過 `setup_engines.py --backend cuda`、
`lec run --file`（CUDA）與 `lec devices`，下面標註「實機」的段落是依實機結果修正的；
其餘仍是依文件推導與 mock test。

## DirectShow 裝置列表的兩種格式（實機更正，2026-09）

ffmpeg `-f dshow -list_devices true` 的輸出有兩種格式，`parse_dshow_devices()` 兩種都支援：

新版（Windows 實機 gyan.dev ffmpeg 9.0.1 的原文）：類型直接寫在名稱後面，沒有區段標題。

```
[in#0 @ …] "Integrated Camera" (video)
[in#0 @ …]   Alternative name "@device_pnp_\\?\usb#vid_174f&…\global"
[in#0 @ …] "OBS Virtual Camera" (none)
[in#0 @ …]   Alternative name "@device_sw_{…}\{…}"
[in#0 @ …] "麥克風排列 (Realtek(R) Audio)" (audio)
[in#0 @ …]   Alternative name "@device_cm_{33D9A762-…}\wave_{…}"
```

類型可能是 `audio`、`video`、`none`，或同時有兩種的 `audio, video`。

舊版：先印 `DirectShow video devices` / `DirectShow audio devices` 區段標題，裝置只印
`"名稱"`，要看所在區段判斷類型。

**更正**：第一個跨平台 PR（`f324e10`）在沒有 Windows 實機的情況下，以為 ffmpeg 只用
舊版的區段標題格式，把原本會認 `"名稱" (audio)` 的解析器改成只認區段標題。結果在新版
ffmpeg 上 `lec devices` 一個裝置都列不出來，`default` 解析成 `None`，
`lec devices --test default` 把 `audio=None` 交給 ffmpeg 而得到 `I/O error`；當時的權限
提示又把 `I/O error` 當成權限問題，錯誤地叫使用者去檢查隱私權設定。修正內容：

- 兩種格式都解析，只收類型含 `audio` 的裝置；Alternative name 只接在剛解析出來的
  音訊裝置後面。`DshowModernFormatTests` 用的是上面的實機原文。
- 找不到任何裝置時，`test_volume()` 直接回報「找不到任何錄音裝置」，不再執行
  `ffmpeg -i audio=None`。
- dshow 的權限提示只認 `Access is denied`，不再把 `I/O error` 當成權限問題。

## 錄音（DirectShow）

- 後端固定用 `dshow`。
- 存進設定檔的 `id` 優先用 `Alternative name`（例如
  `@device_cm_{33D9A762-...}\wave_{...}`），這個字串不含中文或逗號，交給
  ffmpeg 當 `-i audio=<id>` 時不需要額外處理跳脫字元；只有在裝置沒有
  Alternative name 時才會退回用顯示名稱本身當 `id`。
- **待實機確認**：用 Alternative name 開裝置（`-i "audio=@device_cm_{…}\wave_{…}"`）
  還沒在實機上成功驗證過；已確認的是用顯示名稱 `-i "audio=麥克風排列 (Realtek(R) Audio)"`
  可以錄音。如果 Alternative name 開不起來，要改成用顯示名稱開裝置。
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

- `lec doctor` 會顯示一行「編譯工具」：後端、vswhere 找到的 Visual Studio 版本，
  以及缺少的工具。後端優先讀 `<引擎>/build/.lec-build`（`setup_engines.py` 寫的編譯紀錄）
  裡的 `BACKEND=`，沒有紀錄才用平台預設（Windows = Vulkan）——所以用
  `--backend cuda` 編過的機器會檢查 `nvcc`，不會被提醒缺 Vulkan 的 `glslc`。判斷用的是
  `core/platform.py::missing_build_tools()` / `find_msvc()`，跟 `setup_engines.py`
  編譯前的前置檢查同一份，所以「doctor 說齊全但 setup_engines 說缺」這種不一致
  不會發生。缺工具只算**警告**：已經編好引擎、或改用官方預編譯檔的人不需要編譯環境。
  這一行目前只有 mock 測試（`tests/test_doctor_platform.py`），**還沒有在 Windows
  實機上核對過 vswhere 的版本字串長相**。
- `lec doctor` 對 Windows 的錄音來源錯誤，會透過 `devices.hint()` 提示
  「請確認已安裝 ffmpeg 並在 PATH 中」；`core/devices.py` 新增的
  `_permission_hint()` 對 dshow 也準備了「Access is denied」等常見拒絕字樣的
  啟發式判斷，但這部分**沒有 Windows 麥克風權限對話框的實機文字可以核對**，
  關鍵字可能需要之後根據真實錯誤訊息調整。

## 實機驗證流程（下一輪照這個順序跑）

「還需要哪些實機測試」那一節的可執行版本。上一輪實機已經確認的是
`setup_engines.py --backend cuda`、`lec run --file`（CUDA）與 `lec devices`，
所以這一輪的重點在**錄音、停止、主控台編碼、以及 doctor／setup.py 的新項目**。
每一步都標了它關掉哪一個風險項目，把**輸出原文**帶回來就能逐項換成實機結論。

Windows 有三種主控台，步驟裡會標明該用哪一個：

- **PowerShell**（開始選單搜尋 PowerShell）：一般操作用這個。
- **cmd.exe**（搜尋 cmd）：只在步驟 1 的編碼比對用。
- **x64 Native Tools Command Prompt for VS**（裝了 Visual Studio Build Tools 才有）：
  只在 `--generator Ninja` 那條岔路用。

`lec` 在 Windows 沒有 shebang，所以指令都是 `python lec …`（不是 `./lec`）。
如果 `python` 指到 Microsoft Store 的殼或 3.9，改用 `py -3 lec …` 並在回報時說明。

### 1. 兩種主控台的 UTF-8 輸出（對應風險項目 3）

PowerShell 與 cmd.exe 各跑一次，比對中文與符號：

```powershell
cd $HOME\lecture-notes
python lec doctor
```

```bat
cd /d %USERPROFILE%\lecture-notes
python lec doctor
```

要看的東西：`✔` `⚠` `✖` 有沒有變成問號或方框、「作業系統」「錄音後端」等中文欄名
有沒有亂碼、以及 `chcp` 回報的 code page（`chcp` 直接執行就會印）。兩種主控台的
輸出如果不一致，那正是 `force_utf8()` 要修的情境——把兩邊的螢幕內容都帶回來。

### 2. 環境偵測與缺少的套件（對應風險項目 5，以及新的 setup.py）

先只看偵測結果，不編譯：

```powershell
python setup.py --dry-run --yes
```

這一步會一次印出 `setup.py` 所有偵測結果，是這一輪最有效率的一份輸出：

- **GPU／CUDA Toolkit**：有 NVIDIA 的話應該列出型號與 CUDA 版本（`nvidia_gpu()` 用
  `nvidia-smi`、`cuda_toolkit()` 用 `nvcc`）。沒有 NVIDIA 的機器應該顯示
  `vulkaninfo` 回報的裝置。
- **記憶體**：Windows 是走 `ctypes` 的 `GlobalMemoryStatusEx`，**這條路徑只有 mock 測過**，
  數字明顯不對（例如 0 或 None）要回報。
- **套件管理器**：有裝 winget 的話應該顯示 `winget`。
- **缺少的套件**：缺 Vulkan SDK 會印 `winget install LunarG.VulkanSDK`；缺 MSVC 會印
  「安裝 Visual Studio Build Tools 並勾選『使用 C++ 的桌面開發』」的說明而**不是**指令。
  它不會幫你安裝任何東西。

接著確認雙擊入口也能用：在檔案總管雙擊 `windows_setup.bat`。要看的是視窗有沒有立刻
關掉（`pause` 應該讓它停住）、`python` 有沒有被找到、以及 `.bat` 自己印的英文訊息正常
（那個檔案刻意只用 ASCII，因為 cmd 用主控台 code page 顯示 `.bat`，中文會變亂碼）。

### 3. 編譯與 build 產物路徑（對應風險項目 4）

互動模式會偵測後端再問你（有 NVIDIA + CUDA 時會問要不要用 CUDA）：

```powershell
python setup.py
```

編完確認執行檔實際落在哪裡（`find_engine_bin()` 的搜尋順序假設 `build/bin/`、
`build/bin/Release/`、`build/Release/`、`build/bin/Debug/`）：

```bat
cd /d %USERPROFILE%\lecture-notes
dir /s /b whisper-server.exe llama-server.exe
type whisper.cpp\build\.lec-build
```

沒裝 Visual Studio Build Tools、只有 Ninja 的機器，要在
**x64 Native Tools Command Prompt for VS** 裡另外試一次：

```bat
python setup_engines.py --generator Ninja
```

### 4. doctor 的「編譯工具」那一行（對應風險項目 5）

```powershell
python lec doctor
python lec doctor --json
```

要看的東西：

- 「編譯工具」那一行的後端應該跟步驟 3 編的一致，而且括號裡寫「已編譯的後端」
  （表示讀到了 `build\.lec-build`；用 CUDA 編的機器就不該提 Vulkan 的 `glslc`）。
- 有裝 Visual Studio 的話，同一行會顯示 vswhere 找到的版本字串。**這個字串的實際
  長相還沒在實機核對過**，請整行帶回來。
- 一般 PowerShell 與 Developer Command Prompt 下各跑一次：後者 `cl` 在 PATH 上，
  `missing_build_tools()` 走的是不同分支。

### 5. 裝置列表原文（對應風險項目 1）

```powershell
ffmpeg -hide_banner -f dshow -list_devices true -i dummy
python lec devices
python lec devices --json
```

第一個指令的原文請整段帶回來。上一輪已經拿到過新版 ffmpeg（gyan.dev 9.0.1）的格式並
據此修好 parser，這一輪要補的是**不同裝置類型**：筆電內建麥克風、USB 麥克風、藍牙耳機，
有幾種就各插一次、各跑一次。要注意 `lec devices --json` 的 `id` 是不是
`Alternative name`（`@device_cm_{…}\wave_{…}`）而不是顯示名稱。

### 6. 用 Alternative name 實際開裝置（對應風險項目 7）

這是目前 DirectShow 這條路上最後一個沒驗證的環節：`lec` 存進設定的 `id` 是裝置路徑，
但**還沒有在實機上用它成功錄過音**（已確認可行的是用顯示名稱）。

```powershell
python lec devices --save 0
python lec devices --test default
python lec doctor --mic
```

（`--save 0` 的 `0` 換成步驟 5 列出的編號。）成功的話會印出「平均 … dB、峰值 … dB」。
失敗的話把**錯誤訊息原文**帶回來，特別是 ffmpeg 有沒有抱怨 `Could not find audio only
device`——那就表示要改成用顯示名稱開裝置，`ffmpeg_input()` 會需要調整。

順便也驗一次權限提示：到「設定 > 隱私權與安全性 > 麥克風」把桌面應用程式的存取權關掉，
再跑一次 `python lec devices --test default`，把錯誤訊息原文帶回來——`_PERMISSION_HINTS`
的 dshow 關鍵字目前只認 `Access is denied`，那是猜的。測完記得開回來。

### 7. 即時錄音、停止機制與行程收尾（對應風險項目 2）

停止機制是三平台一致的 public contract（寫 `stop` 檔，不是送 signal），Windows 這條路
還沒實機驗證過。開一個 PowerShell 錄音：

```powershell
python lec run 測試課
```

另一個 PowerShell 視窗：

```powershell
python lec status
python lec stop
python lec status
Get-Process whisper-server,llama-server,ffmpeg -ErrorAction SilentlyContinue
```

要看的東西：`lec stop` 印出「已寫入停止要求」並指向輸出資料夾裡的 `stop` 檔、phase 走
`recording → finishing → done`、最後 `Get-Process` **什麼都不該剩**（`kill_tree()` 用的是
`taskkill /PID <pid> /T /F`，`/T` 才會連子行程一起關）。再重跑一次錄音並改用
`python lec stop --force`（寫 `stop_force`，應該立刻結束），同樣檢查殘留行程。

也請順手確認 Ctrl+C：在跑 `lec run` 的視窗按一次 Ctrl+C，應該收尾而不是留下孤兒行程
（`spawn_kwargs()` 用 `CREATE_NEW_PROCESS_GROUP` 就是為了這個）。

### 8. 官方預編譯檔（對應風險項目 6，選用）

只有在你想驗「不裝編譯環境」那條路時才需要：照 README「Windows 預編譯檔」把 exe 與 DLL
放進 `whisper.cpp\build\bin\`、`llama.cpp\build\bin\`，然後 `python lec doctor`。
要看的是 doctor 找不找得到執行檔、GPU 那一行有沒有列出裝置（`library_dirs()` /
`env_with_libs()` 把 DLL 目錄加進 `PATH` 是否真的生效）。

### 一次收集所有輸出

在 PowerShell 裡跑這段，會把環境與各指令輸出寫成一個檔案，回報時附上它。
每一段都包在 try/catch 裡，所以缺 ffmpeg／nvidia-smi 這種情況只會記一行「失敗」，
不會讓整段中斷：

```powershell
cd $HOME\lecture-notes
$R = "$HOME\lec-windows-report.txt"
$global:out = @()
function Add-Section($title, $block) {
  $global:out += "== $title =="
  try { $global:out += (& $block 2>&1 | Out-String) } catch { $global:out += "（失敗：$_）" }
}
Add-Section "OS" { Get-CimInstance Win32_OperatingSystem |
  Select-Object Caption, Version, OSArchitecture | Format-List }
Add-Section "chcp" { chcp }
Add-Section "python" { python -V }
Add-Section "ffmpeg" { ffmpeg -version | Select-Object -First 2 }
Add-Section "cmake" { cmake --version | Select-Object -First 1 }
Add-Section "nvidia-smi" { nvidia-smi | Select-Object -First 12 }
Add-Section "VULKAN_SDK" { "$env:VULKAN_SDK" }
Add-Section "setup.py 偵測" { python setup.py --dry-run --yes }
Add-Section "dshow 原文" { ffmpeg -hide_banner -f dshow -list_devices true -i dummy }
Add-Section "lec devices --json" { python lec devices --json }
Add-Section "lec doctor" { python lec doctor }
Add-Section "build 產物" { Get-ChildItem -Recurse -Filter *-server.exe |
  Select-Object -ExpandProperty FullName }
Add-Section "build stamp" { Get-Content whisper.cpp\build\.lec-build }
Add-Section "unittest" { python -m unittest 2>&1 | Select-Object -Last 8 }
$global:out | Out-File -FilePath $R -Encoding utf8
"寫到 $R"
```

`python -m unittest` 那一段如果只有 `tests/test_ui_backend_*.py` 失敗，那是因為沒裝
`ui/backend/requirements.txt` 的 fastapi，不是程式壞掉（見 CLAUDE.md）；其他測試檔失敗才要回報。

另外單獨帶回來的（這個檔案收不到的）：

- 步驟 1 在 cmd.exe 裡的螢幕內容（截圖或複製貼上都可以）
- 步驟 6 `lec devices --test default` 的結果，以及關掉麥克風權限後的錯誤訊息原文
- 步驟 7 `lec stop` / `lec stop --force` 之後 `Get-Process` 有沒有殘留
- 步驟 2 雙擊 `windows_setup.bat` 的行為（視窗有沒有留住、訊息正常嗎）

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
   跟實際安裝後的環境變數狀態一致，以及 `lec doctor` 的「編譯工具」那一行在
   一般 PowerShell 與 Developer Command Prompt 下是否都顯示正確
   （只有 CUDA 版實機跑過 `setup_engines.py`，doctor 這一行還沒）。
6. 官方預編譯檔（README「Windows 預編譯檔」）搭配 `library_dirs()` /
   `env_with_libs()` 的 DLL 搜尋路徑是否真的能讓 `lec doctor` 找到並成功執行。
7. 用 `Alternative name`（`@device_cm_{…}\wave_{…}`）當 `-i audio=<id>` 能不能實際開啟
   裝置錄音；目前只確認過用顯示名稱可以（見上面「錄音（DirectShow）」）。
8. `setup.py` 在 Windows 的偵測與互動：`GlobalMemoryStatusEx` 取記憶體、winget 的
   缺套件指令、雙擊 `windows_setup.bat` 的行為，以及 `lec doctor` 的「編譯工具」那一行
   顯示的 vswhere 版本字串（全部只有 mock 測試）。

要驗這些項目的話，照上面「實機驗證流程」那一節跑，不用自己想順序；
每一步都標了對應的風險項目編號。

在以上項目至少跑過一輪之前，README 與 `lec doctor` 都刻意維持「實驗中」而
不是「已支援」的措辭，請不要在這些項目確認之前把 Windows 改標成正式支援。
