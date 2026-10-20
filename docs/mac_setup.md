# macOS 安裝

狀態：**正式支援**。錄音用 AVFoundation 原生擷取、GPU 用 Metal。課堂與會議流程已在 Apple Silicon 實機驗收。已知限制與實機驗證步驟見 [platform-macos.md](platform-macos.md)。

需要：Homebrew、Python 3.11+、ffmpeg、git、cmake、Xcode Command Line Tools；用 Web UI 另需 Node.js 22.22+。

> **zsh 注意**：zsh 預設不把 `#` 當註解。下面指令後面的 `# …` 說明如果一起貼上，會被當成參數
> （例如 `setup_engines.py` 回報「未知的引擎：#」）。只複製 `#` 前面的指令，或執行一次
> `echo 'setopt interactivecomments' >> ~/.zshrc && source ~/.zshrc`，之後就能連同註解一起貼上。

## 1. 系統套件

先安裝 Xcode Command Line Tools（已經裝過會提示已安裝）：

```bash
xcode-select --install
```

它提供編譯引擎用的 C++ 編譯器，以及錄音需要的 `swiftc`：macOS 的即時錄音透過一支小型原生程式擷取麥克風，第一次錄音時用 `swiftc` 編譯並快取在 `~/Library/Application Support/lecture-notes/native-capture/`，之後只有原始碼或 `swiftc` 版本變了才重新編譯（細節見 [platform-macos.md](platform-macos.md#原生-helper-編譯快取issue-87)）。
缺少 `swiftc` 時即時錄音會直接失敗，`lec doctor` 也會顯示 FAIL。

接著安裝其他套件：

```bash
brew install python cmake ffmpeg opencc
```

macOS 內建的 `python3` 通常是 3.9，低於需求的 3.11；`brew install python` 會裝新版。
裝完先開新的終端機，用 `python3 --version` 確認是 3.11 以上。

## 2. 取得程式

```bash
git clone https://github.com/RichardstGG/lecture-notes.git ~/lecture-notes
cd ~/lecture-notes
```

## 3. 編譯引擎並取得語音模型

在 Finder 雙擊 `mac_setup.command`，或在終端機執行：

```bash
python3 setup.py
```

它會先檢查 Python、ffmpeg、GPU 與磁碟空間，再問幾個問題，然後編譯引擎（預設後端 Metal）。
**它不會安裝系統套件**，缺什麼只會列出對應的安裝指令。

- 先看它會做什麼而不安裝：`python3 setup.py --dry-run`
- 不問問題、全用偵測到的預設值：`python3 setup.py --yes`
- 只需要逐字稿、不做總結：`python3 setup.py --whisper-only`，之後可跳過步驟 4，並用 `lec run <課名> --transcribe-only`。
  **會議功能**（`lec meeting run`／`lec meeting diarize`）也只需要 whisper，不會用到 llama.cpp 與總結模型；此時 `lec doctor` 對 llama-server 與 Qwen3-8B 的錯誤可以忽略。
- 外部摘要 API 不在這裡設定，用 Web UI 的「本機設定 → 摘要 API 上游」。

想自己控制編譯細節，見 README 的「[手動編譯](../README.md#手動編譯)」。

## 4. 下載總結模型（Qwen 官方 GGUF，約 5GB）

在 repo 資料夾裡執行（模型要放在 repo 裡的 `models/`，`lec` 才找得到）：

```bash
curl -L -C - --create-dirs -o models/Qwen3-8B-Q4_K_M.gguf \
  https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf
```

`-C -` 可以續傳，中途斷掉就重跑同一行。

## 5. 讓 `lec` 可以直接執行

在 repo 資料夾裡執行。`"$PWD/lec"` 會連到目前這份 repo，就算不是 clone 在 `~/lecture-notes` 也不會連錯。

```zsh
mkdir -p ~/.local/bin
ln -sf "$PWD/lec" ~/.local/bin/lec
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

`~/.local/bin` 是 Linux 的慣例，macOS 預設既沒有這個資料夾，也不會把它放進 PATH，所以要自己建立並加進 `~/.zshrc`。
macOS 預設 PATH 裡的 `/usr/bin` 受系統保護不能寫入，`/usr/local/bin` 在 Apple Silicon 上需要 sudo；
放在 `~/.local/bin` 不用 sudo，也不會碰到系統或 Homebrew 的檔案。
不想改 PATH 的話，也可以改連到 Homebrew 的資料夾：`ln -sf "$PWD/lec" /opt/homebrew/bin/lec`
（Homebrew 管理的位置，`brew doctor` 可能會提醒有非 Homebrew 的檔案）。

確認：`lec --help` 能顯示說明就完成了。

## 6. 設定麥克風並檢查環境

```bash
lec devices
lec devices --test <編號>
lec devices --save <編號>
lec doctor --mic
```

依序是：列出麥克風、錄 3 秒看音量、把選好的麥克風寫入 `config/local.toml`、檢查整個環境（全部 ✔ 就可以上課了）。

第一次錄音時 macOS 會詢問是否允許終端機使用麥克風；按過拒絕的話，到「系統設定 > 隱私權與安全性 > 麥克風」打開。

## 7. 用內附的範例音檔驗證整條流程

```bash
lec run 測試課 --file samples/test8min.ogg   # 轉錄 → 總結，跑完看 outputs/
```

結果可以跟 `samples/expected/` 對照（是結構對照，不是逐字比對，說明見 `samples/README.md`）。
確認沒問題之後就可以建立自己的課程設定：

```bash
lec new 計算機概論 --from example
```

## 8. Web UI（選用）

需要 Node.js 22.22+（例如 `brew install node`）。第一次使用時建立 `.venv`、安裝後端套件並建好前端：

```bash
cd ~/lecture-notes
python3 upgrade.py --skip-engines
lec --start-ui
```

然後開啟 <http://127.0.0.1:8765>。`--skip-engines` 是因為前面已編好引擎；之後日常升級直接 `python3 upgrade.py`。
UI 的操作說明見 README 的「[Web UI](../README.md#web-ui)」。

## 升級

舊版安裝先取得升級腳本一次：

```bash
cd ~/lecture-notes
git pull --ff-only
```

之後只要：

```bash
python3 upgrade.py
```

它會更新 Git checkout、依 `engines.lock` 編譯引擎、更新 `.venv` 與 Web UI，預設也會裝會議發言者辨識的依賴與模型（約 50MB）。

- 只用逐字稿或會議功能、不裝 llama.cpp：`python3 upgrade.py --whisper-only`（只處理 whisper.cpp；它已編好時不需要 cmake 等編譯工具）
- 不需要會議功能：加 `--skip-diarization`（說明見 [diarize-install.md](diarize-install.md)）
- 其他選項：`python3 upgrade.py --help`

只接受 fast-forward 更新；tracked 檔案有未提交修改、或還有課程在執行時會先停止。
不會覆寫 `.gitignore` 保護的課程設定、本機設定、模型、錄音或輸出。

## 遇到問題

- 先跑 `lec doctor`，回報問題時附上 `lec doctor --json` 與該堂課的 `session.log`。
- macOS 的已知限制與實機驗證流程：[platform-macos.md](platform-macos.md)。
