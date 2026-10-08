# Windows 安裝

狀態：**實驗中（beta）**。已在 Windows 實機跑過 CUDA 編譯、處理音檔與列出錄音裝置；
其餘行為大多只有 mock 與靜態驗證，請不要當成「已支援」。已知限制與實機驗證步驟見 [platform-windows.md](platform-windows.md)。

錄音用 DirectShow，GPU 預設 Vulkan（可改 CUDA）。

## 1. 取得程式

需要先有 Git（沒有的話：`winget install Git.Git`，裝完開新的視窗）。

cmd：

```bat
cd /d %USERPROFILE%
git clone https://github.com/RichardstGG/lecture-notes.git lecture-notes
cd lecture-notes
```

PowerShell：

```powershell
cd $HOME
git clone https://github.com/RichardstGG/lecture-notes.git lecture-notes
cd lecture-notes
```

> **不要把 `~/lecture-notes` 交給 `git clone`。** cmd 完全不認得 `~`；
> PowerShell 只在自己的指令（例如 `cd ~`）裡展開 `~`，傳給 `git` 這類外部程式時
> （Windows 內建的 PowerShell 5.1）會原樣傳過去。結果都是建立一個名字就叫 `~` 的
> 資料夾，變成 `C:\Users\<你>\~\lecture-notes`。已經這樣 clone 的話，在 cmd 搬回正確位置：
>
> ```bat
> cd /d %USERPROFILE%
> move "%USERPROFILE%\~\lecture-notes" "%USERPROFILE%\lecture-notes"
> rmdir "%USERPROFILE%\~"
> ```
>
> `rmdir` 不加 `/s` 只會刪空資料夾。**不要**在 PowerShell 用 `Remove-Item ~ -Recurse`
> 或 `rm -r ~` 刪它：PowerShell 會把 `~` 當成整個家目錄。

其他文件裡的 `cd ~/lecture-notes`：PowerShell 可以照打；cmd 請改成 `cd /d %USERPROFILE%\lecture-notes`。

## 2. 一鍵安裝（建議）

在 repo 資料夾雙擊 `windows_setup.bat`（或在 cmd 裡執行它）。它會：

1. 尋找 Python 3.11+，沒有就用 winget 安裝 Python 3.13。
2. 補上缺少的 ffmpeg、Git、Node.js 與 VC++ 執行庫。
3. 下載或編譯語音引擎，並下載語音模型與總結模型。沒有 Visual Studio 時會改用官方預編譯檔（見下方「預編譯檔」）。
4. 建好 Web UI，最後詢問要不要立刻打開 <http://127.0.0.1:8765>。

有 NVIDIA 顯示卡時會問要不要用 CUDA。先看它會做什麼而不安裝：`windows_setup.bat --dry-run`。
外部摘要 API 不在這裡設定，用 Web UI 的「本機設定 → 摘要 API 上游」。

裝完直接跳到步驟 4。

## 3. 手動安裝（不用一鍵安裝時）

**系統套件**（PowerShell；另需 Visual Studio Build Tools 的「C++ 桌面開發」與 Vulkan SDK）：

```powershell
winget install Git.Git Kitware.CMake Gyan.FFmpeg Python.Python.3.13
winget install LunarG.VulkanSDK
```

**編譯引擎並取得語音模型：**

```bat
python setup.py
```

它會先檢查 Python、ffmpeg、GPU、CUDA Toolkit 與磁碟空間，再問幾個問題，然後編譯引擎。
偵測到缺的是編譯工具時，會改走預編譯檔。

- 先看它會做什麼而不安裝：`python setup.py --dry-run`
- 只需要逐字稿、不做總結：`python setup.py --whisper-only`，之後可跳過下載總結模型，並用 `python lec run <課名> --transcribe-only`。

**下載總結模型**（Qwen 官方 GGUF，約 5GB；在 repo 資料夾裡執行，cmd 或 PowerShell，整條寫成一行）：

```bat
curl.exe -L -C - --create-dirs -o models\Qwen3-8B-Q4_K_M.gguf https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf
```

- 寫成一行是因為 cmd 不認得 bash 的 `\` 換行，會把第一行當成少了網址的指令而出現 `curl: (3) URL rejected: Bad hostname`。
- `--create-dirs` 會在 `models` 資料夾不存在時自動建立。少了它、又不在 repo 資料夾裡執行，curl 會回報 `Failed to open the file` 與 `(23)`。
- 用 `curl.exe` 而不是 `curl`：Windows 內建的 PowerShell 5.1 裡 `curl` 是 `Invoke-WebRequest` 的別名，不認得 `-L`、`-C` 這些參數；`curl.exe` 在 cmd 和 PowerShell 都會叫到真正的 curl（Windows 10 以後內建）。
- `-C -` 可以續傳，中途斷掉就重跑同一行。

想自己控制編譯細節，見 README 的「[手動編譯](../README.md#手動編譯)」。

### 預編譯檔（不想裝編譯環境時）

沒有 Visual Studio 或 Vulkan SDK 時，在 repo 資料夾執行：

```bat
python setup_engines.py --prebuilt
```

`python setup.py` 若偵測到缺的是編譯工具，也會改走這條路。它會下載並核對 SHA-256：

- whisper.cpp v1.9.0 的 `whisper-bin-x64.zip`（CPU。官方沒有 Windows Vulkan 版，而且 v1.9.1 之後沒有再發布 Windows 執行檔）
- llama.cpp b11067 的 `llama-b11067-bin-win-vulkan-x64.zip`

執行檔會放進 `whisper.cpp/build/bin/` 與 `llama.cpp/build/bin/`。這兩個版本跟 `engines.lock` 的原始碼 commit 不同；
要完全同一版請改裝編譯環境，不要加 `--prebuilt`。CUDA 與純 CPU 的 llama 預編譯檔還沒有自動下載。

也可以自己從下面的位置下載，把執行檔與 DLL 放進上面兩個 `build/bin/`，`lec doctor` 就能找到：

- <https://github.com/ggml-org/llama.cpp/releases>：`llama-<版本>-bin-win-vulkan-x64.zip`（或 CUDA 版）
- <https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.0>：`whisper-bin-x64.zip`（CPU）或 `whisper-cublas-12.4.0-bin-x64.zip`（NVIDIA）

## 4. 執行 `lec`

Windows 不需要設定 PATH：在 repo 資料夾裡把各文件中的 `lec` 換成 `python lec` 即可。確認：`python lec --help` 能顯示說明。

## 5. 設定麥克風並檢查環境

```bat
python lec devices
python lec devices --test <編號>
python lec devices --save <編號>
python lec doctor --mic
```

依序是：列出麥克風、錄 3 秒看音量、把選好的麥克風寫入 `config/local.toml`、檢查整個環境（全部 ✔ 就可以上課了）。

## 6. 用內附的範例音檔驗證整條流程

```bat
python lec run 測試課 --file samples/test8min.ogg
```

跑完看 `outputs/`。結果可以跟 `samples/expected/` 對照（是結構對照，不是逐字比對，說明見 `samples/README.md`）。
確認沒問題之後就可以建立自己的課程設定：

```bat
python lec new 計算機概論 --from example
```

## 7. Web UI

一鍵安裝已經建好 UI。手動安裝的話需要 Node.js 22.22+，第一次使用時：

```bat
python upgrade.py --skip-engines
```

啟動：

```bat
python lec --start-ui
```

然後開啟 <http://127.0.0.1:8765>。UI 的操作說明見 README 的「[Web UI](../README.md#web-ui)」。

## 升級

舊版安裝先取得升級腳本一次：

```bat
cd /d %USERPROFILE%\lecture-notes
git pull --ff-only
```

之後只要：

```bat
python upgrade.py
```

它會更新 Git checkout、依 `engines.lock` 編譯引擎、更新 `.venv` 與 Web UI，預設也會裝會議發言者辨識的依賴與模型（約 50MB）。

- 只用逐字稿、不裝 llama.cpp：`python upgrade.py --whisper-only`
- 不需要會議功能：加 `--skip-diarization`（說明見 [diarize-install.md](diarize-install.md)）
- 其他選項：`python upgrade.py --help`

只接受 fast-forward 更新；tracked 檔案有未提交修改、或還有課程在執行時會先停止。
不會覆寫 `.gitignore` 保護的課程設定、本機設定、模型、錄音或輸出。

## 遇到問題

- 先跑 `python lec doctor`，回報問題時附上 `python lec doctor --json` 與該堂課的 `session.log`。
- Windows 特有的行為與已知限制：[platform-windows.md](platform-windows.md)。
