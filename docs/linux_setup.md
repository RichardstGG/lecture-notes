# Linux 安裝

狀態：**正式支援**。已在 Debian 13（Intel Core Ultra 7 258V、Arc 140V、PipeWire）實測。
其他發行版沒有實測過；下面的套件名稱是 Debian／Ubuntu 的。

需要：Python 3.11+、ffmpeg、git、cmake、C++ 編譯環境；用 Web UI 另需 Node.js 22.22+。

## 1. 系統套件

```bash
sudo apt install ffmpeg curl pulseaudio-utils opencc \
                 git cmake build-essential pkg-config libvulkan-dev glslc vulkan-tools
```

## 2. 取得程式

```bash
git clone https://github.com/RichardstGG/lecture-notes.git ~/lecture-notes
cd ~/lecture-notes
```

## 3. 編譯引擎並取得語音模型

```bash
./linux_setup.sh                    # 等同 python3 setup.py
```

它會先檢查 Python、ffmpeg、GPU、CUDA Toolkit 與磁碟空間，再問幾個問題，然後編譯引擎。
**它不會安裝系統套件**，缺什麼只會列出對應的安裝指令。

- 先看它會做什麼而不安裝：`python3 setup.py --dry-run`
- 不問問題、全用偵測到的預設值：`python3 setup.py --yes`
- 有 NVIDIA 顯示卡時會問要不要用 CUDA（需要 CUDA Toolkit；還沒裝時只會印出安裝說明，不會編譯）。預設後端是 Vulkan。
- 只需要逐字稿、不做總結：`python3 setup.py --whisper-only`，之後可跳過步驟 4，並用 `lec run <課名> --transcribe-only`。
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

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/lec" ~/.local/bin/lec
```

多數發行版登入時會自動把已存在的 `~/.local/bin` 加進 PATH；如果剛建立這個資料夾，
要重新登入（或開新的登入 shell）之後 `lec` 才找得到。確認：`lec --help` 能顯示說明就完成了。

## 6. 設定麥克風並檢查環境

```bash
lec devices
lec devices --test <編號>
lec devices --save <編號>
lec doctor --mic
```

依序是：列出麥克風、錄 3 秒看音量、把選好的麥克風寫入 `config/local.toml`、檢查整個環境（全部 ✔ 就可以上課了）。

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

需要 Node.js 22.22+。第一次使用時建立 `.venv`、安裝後端套件並建好前端：

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

- 只用逐字稿、不裝 llama.cpp：`python3 upgrade.py --whisper-only`
- 不需要會議功能：加 `--skip-diarization`（說明見 [diarize-install.md](diarize-install.md)）
- 其他選項：`python3 upgrade.py --help`

只接受 fast-forward 更新；tracked 檔案有未提交修改、或還有課程在執行時會先停止。
不會覆寫 `.gitignore` 保護的課程設定、本機設定、模型、錄音或輸出。

## 遇到問題

- 先跑 `lec doctor`，回報問題時附上 `lec doctor --json` 與該堂課的 `session.log`。
- Linux 特有的行為與已知限制：[platform-linux.md](platform-linux.md)。
