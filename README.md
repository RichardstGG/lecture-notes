# 課堂筆記系統（lec）

課堂錄音 → whisper.cpp 即時逐字稿 → 每 5 分鐘分段總結 → Obsidian Markdown。預設全部在本機執行，也可選擇已儲存的相容 API 上游進行摘要。

Repo：<https://github.com/RichardstGG/lecture-notes>

## 摘要上游：本地 GPU 或已儲存 API

預設 `[summary] upstream = "local"` 保留既有 llama-server／本機模型流程。
UI「本機設定 → 摘要 API 上游」可新增、完整覆寫或刪除私有上游；課堂與歷史紀錄的「摘要上游」選單可選本地 GPU 或已儲存的 API。錄音、音檔轉錄後摘要、補做／重做皆使用所選上游。
API 模式不檢查本機 GGUF、不啟停本機或遠端 LLM 行程；語音轉錄仍使用本機 whisper。
目前只提供課程摘要，會議工作台仍為預覽、沒有會議摘要流水線；未來會議可共用此連線層。

建立 **`config/upstreams.toml`**（已加入 `.gitignore`；不要把此內容放進課程檔或 `local.toml`）：

```toml
[upstreams.lab]
name = "內網 30B GPU"
base_url = "http://192.0.2.10:8000/v1"  # 文件示例位址，請換成實際伺服器
model = "your-deployed-model-id"
api_key_env = "LEC_LAB_API_KEY"         # 無認證時省略

[upstreams.backup]
name = "備用 GPU"
base_url = "https://gpu.example.invalid/api/v1"
model = "another-model-id"
# api_key = "REPLACE_ME"               # 或在此私有檔儲存 Bearer key
```

- 表格名稱是穩定 ID，允許英數開頭、後接英數／底線／連字號，最多 64 字元；`local` 保留。
- `name` 是可公開的選單名稱。`base_url` 必須含 API 路徑（通常 `/v1`），程式只追加 `/chat/completions`；不允許 URL 帳密、query 或 fragment。
- `model` 是該服務部署的模型 ID；`api_key` 與 `api_key_env` 二擇一或皆省略。環境變數須存在於啟動 UI／CLI 的行程環境。
- 選單只顯示 ID、名稱、類型與認證模式。URL、模型 ID、認證值及環境變數名稱不會進入 UI 清單、session 設定快照或請求錯誤紀錄。名稱與 ID 請勿放密鑰。
- 設定頁的連線欄位為只寫：重新開啟或覆寫既有上游時，不會從後端讀回 URL、模型 ID、API key 或環境變數名稱，必須重新輸入完整連線設定。私有檔以原子替換寫入，支援的平台會限制為目前使用者可讀寫。
- UI 重新整理後會重讀設定；選單不代表已驗證連線可達。更換或刪除 ID 後，舊 session 補做時請選擇有效 ID。
- 請求使用標準 Chat Completions 的 `model/messages/temperature/top_p/max_tokens`，不傳本機引擎的 `cache_prompt`、`chat_template_kwargs`，也不做遠端暖機。上游明確以 HTTP 400 拒絕 JSON schema 時，改用一般輸出後解析 JSON。
- API 上游會收到逐字稿、課程 prompt、術語與前段主題。外部連線不跟隨重新導向，也不使用環境 proxy；HTTPS 使用系統信任的憑證。

可在 `config/local.toml` 或課程設定的 `[summary]` 儲存 `upstream = "lab"`，只保存 ID。
也可單次覆寫（`--upstream` 優先於 `--set summary.upstream=...`）：

```bash
./lec run 課名 --upstream lab
./lec run 課名 --file lecture.ogg --upstream lab
./lec summarize outputs/課程資料夾 --upstream lab
./lec summarize outputs/課程資料夾 --redo 00:05:00 --upstream backup
./lec summarize outputs/課程資料夾 --redo all --upstream local
```

`--model` 仍只選本機模型；API 的模型由私有上游設定決定。未指定上游時遵循既有設定合併順序。
補做預設優先讀目前課程設定，課程不存在才讀 `config.used.toml`；該快照僅保存上游 ID，連線資料一律讀目前私有檔。
「只轉錄」不讀取上游認證、不發摘要請求。

五分鐘是逐字稿分段；下一節出現、且達到最少字數才送摘要，並非每逢整五分鐘立刻請求。
`request_timeout=240`、`retries=2`、`final_wait=180` 的預設值維持不變：一次請求最多等 240 秒、最多重試兩次；即時錄音結束後整體收尾最多等 180 秒，可能先於請求逾時。
正常停止錄音先完成轉錄收尾；在摘要階段停止或超過 `final_wait`，API 模式會中止本地等待且不寫入遲到的回覆。
**這不會停止遠端伺服器的推論**；背景連線可能持續到讀取逾時或 CLI 結束。
音檔與離線摘要不套用 `final_wait`，每段遵循請求逾時及重試設定。`stop`／`stop_force` 檔案機制保持不變。

HTTP 401/403 不重試；連線、逾時或格式錯誤會記錄去敏感資訊的失敗原因。
已記錄的失敗段落沿用既有語意，需 `--redo <時間>` 或 UI 重做；沒有寫入的中止段落可直接補做。
轉錄成功但摘要失敗仍保留既有 `done`／退出碼 0 語意，請查看 `errors`、`last_error` 與筆記中的失敗標記。

API v1／schema 1 保留，新增可選欄位：

- `lec models --json`／`GET /api/v1/models`：`summary_upstreams = {selected, options: [{id, name, kind}]}`，`kind` 為 `local|api`。
- `lec courses --json` 增加 `upstream`；`POST /api/v1/runs` 及 `POST /api/v1/sessions/{id}/summarize` 可傳 `upstream` ID。既有 `redo=all|hh:mm:ss` 不變。
- `status.json` 增加 `summary_upstream`，遠端 `summary_connection` 為 `ready`（尚未請求）、`ok` 或 `failed`；本機 `servers.llama` 在 API 模式維持 `not_started`。`summary_model` 與筆記 `model` 在 API 模式記錄上游 ID。
- 補做／重做現在也更新 session 狀態和摘要進度，phase 名稱維持既有契約。

此連線層以 mock HTTP／mock 引擎測試驗證；部署上游的模型相容性、摘要品質與 GPU 效能需另做實測。

## 平台支援

| 平台 | 錄音 | GPU 後端 | 狀態 |
|---|---|---|---|
| Linux（PulseAudio / PipeWire） | pulse | Vulkan | 已實測（Debian 13、Intel Arc 140V） |
| macOS | avfoundation | Metal | 程式已支援，實測中 |
| Windows | dshow | Vulkan（可改 CUDA） | 程式已支援，實測中 |

共同需求：Python 3.11+、ffmpeg、git、cmake，以及 C++ 編譯環境。

## 安裝

Linux／macOS：

```bash
git clone https://github.com/RichardstGG/lecture-notes.git ~/lecture-notes
cd ~/lecture-notes
```

Windows（cmd）：

```bat
cd /d %USERPROFILE%
git clone https://github.com/RichardstGG/lecture-notes.git lecture-notes
cd lecture-notes
```

Windows（PowerShell）：

```powershell
cd $HOME
git clone https://github.com/RichardstGG/lecture-notes.git lecture-notes
cd lecture-notes
```

> **Windows 注意**：不要把 `~/lecture-notes` 交給 `git clone`。cmd 完全不認得 `~`；
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
>
> 本文件其他地方的 `cd ~/lecture-notes`：PowerShell 可以照打；cmd 請改成
> `cd /d %USERPROFILE%\lecture-notes`。

**1. 系統套件**

```bash
# Linux（Debian / Ubuntu）
sudo apt install ffmpeg curl pulseaudio-utils opencc \
                 git cmake build-essential pkg-config libvulkan-dev glslc vulkan-tools

# macOS
brew install python cmake ffmpeg opencc

# Windows（PowerShell；另需 Visual Studio Build Tools 的「C++ 桌面開發」與 Vulkan SDK）
winget install Git.Git Kitware.CMake Gyan.FFmpeg Python.Python.3.13
winget install LunarG.VulkanSDK
```

macOS 內建的 `python3` 通常是 3.9，低於需求的 3.11；上面的 `brew install python`
會裝新版。裝完先開新的終端機，用 `python3 --version` 確認是 3.11 以上。

> **macOS（zsh）注意**：zsh 預設不把 `#` 當註解。本文件指令後面的 `# …` 說明
> 如果一起貼上，會被當成參數（例如 `setup_engines.py` 回報「未知的引擎：#」）。
> 只複製 `#` 前面的指令，或執行一次
> `echo 'setopt interactivecomments' >> ~/.zshrc && source ~/.zshrc`，
> 之後就能連同註解一起貼上。

**2. 編譯引擎並取得 whisper 模型**

不確定該選哪個 GPU 後端的話，用互動式安裝：它會偵測這台機器（GPU、CUDA Toolkit、
記憶體、磁碟、套件管理器）、問幾個問題，再呼叫 `setup_engines.py`。
**它不會安裝任何系統套件**，只會告訴你缺哪些、以及這台機器對應的安裝指令。

```bash
python3 setup.py                    # Linux/macOS；Windows 用 python setup.py
```

Linux 可以跑 `./linux_setup.sh`，macOS 在 Finder 雙擊 `mac_setup.command`，
Windows 雙擊 `windows_setup.bat`——三個都只是同一支 `setup.py` 的入口。
先看它會做什麼而不編譯：`python3 setup.py --dry-run`。

自己清楚要什麼的話也可以直接跑：

```bash
python3 setup_engines.py            # Linux/macOS；Windows 用 python setup_engines.py
```

如果這台電腦只需要逐字稿、不使用 LLM 總結，可只安裝 whisper.cpp：

```bash
python3 setup_engines.py whisper    # Linux/macOS；Windows 用 python setup_engines.py whisper
```

這個選項不會取得或編譯 llama.cpp；後續可略過步驟 3，並使用
`lec run <課名> --transcribe-only`。

後端預設 Linux/Windows 為 Vulkan、macOS 為 Metal；要用 NVIDIA CUDA 加 `--backend cuda`（需 CUDA Toolkit）。

**3. 下載 LLM 模型（Qwen 官方 GGUF，約 5GB）**

在 repo 資料夾裡執行（模型要放在 repo 裡的 `models/`，`lec` 才找得到）。

Linux／macOS：

```bash
curl -L -C - --create-dirs -o models/Qwen3-8B-Q4_K_M.gguf \
  https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf
```

Windows（cmd 或 PowerShell，整條寫成一行）：

```bat
curl.exe -L -C - --create-dirs -o models\Qwen3-8B-Q4_K_M.gguf https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf
```

- 行尾的 `\` 換行是 bash／zsh 的寫法，cmd 不認得，會把第一行當成少了網址的
  指令而出現 `curl: (3) URL rejected: Bad hostname`；所以 Windows 版寫成一行。
- `--create-dirs` 會在 `models` 資料夾不存在時自動建立。少了它、又不在 repo
  資料夾裡執行，curl 會回報 `Failed to open the file` 與 `(23)`。
- 用 `curl.exe` 而不是 `curl`：Windows 內建的 PowerShell 5.1 裡 `curl` 是
  `Invoke-WebRequest` 的別名，不認得 `-L`、`-C` 這些參數；`curl.exe` 在 cmd 和
  PowerShell 都會叫到真正的 curl（Windows 10 以後內建）。
- `-C -` 可以續傳，中途斷掉就重跑同一行。

**4. 讓 `lec` 可以直接執行**

在 repo 資料夾裡執行。`"$PWD/lec"` 會連到目前這份 repo，就算不是 clone 在
`~/lecture-notes` 也不會連錯。

Linux：

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/lec" ~/.local/bin/lec
```

多數發行版登入時會自動把已存在的 `~/.local/bin` 加進 PATH；如果剛建立這個
資料夾，要重新登入（或開新的登入 shell）之後 `lec` 才找得到。

macOS：

```zsh
mkdir -p ~/.local/bin
ln -sf "$PWD/lec" ~/.local/bin/lec
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

`~/.local/bin` 是 Linux 的慣例，macOS 預設既沒有這個資料夾，也不會把它放進
PATH，所以要自己建立並加進 `~/.zshrc`。macOS 預設 PATH 裡的 `/usr/bin` 受系統
保護不能寫入，`/usr/local/bin` 在 Apple Silicon 上需要 sudo；放在 `~/.local/bin`
不用 sudo，也不會碰到系統或 Homebrew 的檔案。不想改 PATH 的話，也可以改連到
Homebrew 的資料夾：`ln -sf "$PWD/lec" /opt/homebrew/bin/lec`（Homebrew 管理的
位置，`brew doctor` 可能會提醒有非 Homebrew 的檔案）。

Windows：不需要這一步，在 repo 資料夾裡把下面的 `lec` 換成 `python lec` 即可。

確認：`lec --help` 能顯示說明就完成了。

**5. 設定麥克風並檢查環境**

```bash
lec devices
lec devices --test <編號>
lec devices --save <編號>
lec doctor --mic
```

依序是：列出麥克風、錄 3 秒看音量、把選好的麥克風寫入 `config/local.toml`、
檢查整個環境（全部 ✔ 就可以上課了）。macOS 第一次錄音時會詢問是否允許
終端機使用麥克風；按過拒絕的話，到「系統設定 > 隱私權與安全性 > 麥克風」打開。

**6. 用內附的範例音檔驗證整條流程**

```bash
lec run 測試課 --file samples/test8min.ogg   # 轉錄 → 總結，跑完看 outputs/
```

結果可以跟 `samples/expected/` 對照（是結構對照，不是逐字比對，說明見 `samples/README.md`）。
確認沒問題之後就可以建立自己的課程設定：

```bash
lec new 計算機概論 --from example
```

## 升級舊版安裝

舊版使用者先取得升級腳本一次：

```bash
cd ~/lecture-notes
git pull --ff-only
```

之後只需一個指令，就會更新目前 Git checkout、依 `engines.lock` clone／編譯引擎、
建立或沿用 `.venv`、安裝 backend dependencies，並執行 `npm ci` 與 frontend build：

```bash
python3 upgrade.py                    # Windows 用 python upgrade.py
```

只使用逐字稿、不安裝 llama.cpp：

```bash
python3 upgrade.py --whisper-only
```

腳本只接受 fast-forward Git 更新；若 tracked 檔案有未提交修改或仍有課程正在執行
會先停止，不會覆寫受 `.gitignore` 保護的課程設定、local config、模型、錄音或
輸出。其他選項可用 `python3 upgrade.py --help` 查看。

## Web UI

UI 是本機 React 頁面，透過 HTTP / SSE 連到同一台電腦上的 FastAPI service；
service 再以 subprocess 呼叫 `lec`，不會把錄音、逐字稿或課程設定上傳到外部。
主控服務只綁定 `127.0.0.1`。

額外需求：Node.js 22.22+。第一次使用交給 `upgrade.py` 建立 `.venv`、
安裝 backend dependencies，並執行 `npm ci` 與 frontend build：

```bash
cd ~/lecture-notes
python3 upgrade.py --skip-engines        # Windows：python upgrade.py --skip-engines
```

`--skip-engines` 是因為前面已完成引擎編譯；後續日常升級直接執行
`python3 upgrade.py`，會同時更新引擎與 UI。

啟動 UI：

```bash
cd ~/lecture-notes
lec --start-ui                           # Windows：python lec --start-ui
```

這個指令會自動使用專案的 `.venv` 啟動 service，不需要先 activate。
然後開啟 <http://127.0.0.1:8765>。若要改 port：
`lec --start-ui --port 9876`。原本的
`python -m ui.backend [--port PORT]` 仍保留給開發與除錯使用。

### UI 操作

1. **建立／編輯課程**：到「課程設定」，輸入新課程名稱即可從
   `config/template.toml` 建立 `courses/<課名>.toml`。左側選擇課程後，可在
   「術語與錯字對照」分頁編輯 `whisper.terms` 與 `[summary.glossary]`，也可切到
   「原始 TOML」調整其他設定。儲存時會先解析 TOML，再用 `lec config` 驗證完整
   合併設定；驗證失敗不會覆寫舊檔。課程檔受 `.gitignore` 保護。
2. **開始處理**：在「課堂工作台」選擇課程與模式。「現場錄音」使用本機預設
   麥克風；「處理音檔」可拖放檔案或開啟系統檔案選擇器。選到的檔案只會複製到
   這台電腦的系統暫存區，再交給 `lec`；也可切換成手動輸入路徑，直接使用原檔。
   模型選單會顯示本機設定的 summary 模型與檔案大小；尚未安裝的模型會標示並
   停用。保留「使用課程預設」就會套用該課程的模型設定。若電腦無法執行 LLM，
   可勾選「只轉錄」，只產生逐字稿而不啟動總結模型。
3. **麥克風與本機設定**：現場錄音可臨時選擇麥克風。「本機裝置與診斷」
   可錄音 3 秒測試音量，並將選擇儲存為 `config/local.toml` 的本機預設；
   也可執行 `lec doctor`，選擇課程設定，或加做麥克風錄音測試。
4. **查看進度**：NOW PROCESSING 區塊顯示目前階段、階段計時、轉錄佇列與筆記
   進度；逐字稿與筆記會透過 SSE 自動更新。
5. **歷史紀錄／補做筆記**：右側選擇過去 session 即可查看逐字稿與筆記；只有
   逐字稿、尚無筆記的 session 可以按「補做課堂筆記」。
6. **停止工作**：「正常停止」會完成剩餘轉錄與最後一段總結；按下後會顯示轉圈與
   「正在停止」，直到工作實際結束，期間仍可在必要時改用「立即停止」。關閉或
   Ctrl+C 停止 UI service 本身不等於停止 `lec` 工作；背景工作
   會繼續，下次啟動 UI 時重新發現。即使瀏覽器 SSE 分頁仍開著，UI service 也能
   正常由 Ctrl+C 關閉，不必先關分頁。

### 內網唯讀分享

在主控頁選擇場次後，到「內網唯讀分享」確認連結 IP 與分享埠。預設監聽
所有 IPv4 介面（`0.0.0.0`），並以預設路由介面的 IP 產生連結及本機 QR code；
偵測失敗可手動填寫，也可取消勾選以限定單一內網 IP。手動開啟後讓參與者掃碼
或複製連結。參與者必填暱稱；可閱讀、下載目前版本的逐字稿
和筆記。主控端可看到暱稱與在線狀態，最多 20 位同時在線。停止錄音後內容仍可用，
直到手動關閉分享。重新開啟會產生新連結，舊連結失效。分享服務監聽獨立內網埠，
主控服務仍只在本機。詳細限制與 API 契約見 [內網唯讀分享](ui/SHARING.md)。

更新 frontend 程式後要重新執行 `npm run build`。開發模式可分兩個 terminal：

```bash
# terminal 1（repo root，已啟用 .venv）
python -m ui.backend

# terminal 2
cd ui/frontend
npm run dev
```

開發頁面在 <http://127.0.0.1:5173>，Vite 會把 `/api` proxy 到 port `8765`。
完整 backend API 與環境變數見 `docs/ui-backend.md`。

### Windows 預編譯檔（不想裝編譯環境時）

llama.cpp 官方有 Windows Vulkan / CUDA 版，whisper.cpp 官方只有 CPU 與 cuBLAS 版（且只有部分 release 附執行檔，例如 v1.9.0）。
手動下載後把執行檔與 DLL 放進 `llama.cpp/build/bin/` 與 `whisper.cpp/build/bin/`，`lec doctor` 就能找到：

- <https://github.com/ggml-org/llama.cpp/releases>：`llama-<版本>-bin-win-vulkan-x64.zip`
- <https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.0>：`whisper-bin-x64.zip`（CPU）或 `whisper-cublas-12.4.0-bin-x64.zip`（NVIDIA）

### 目錄結構

```
~/lecture-notes/
├─ lec, core/, prompts/          程式
├─ ui/backend/, ui/frontend/     本機 FastAPI service 與 React UI
├─ setup.py                      互動式安裝（偵測環境、告知缺少的套件）
├─ linux_setup.sh / mac_setup.command / windows_setup.bat   setup.py 的平台入口
├─ setup_engines.py              取得與編譯引擎
├─ upgrade.py                    更新既有安裝並重新編譯
├─ samples/                      8 分鐘範例音檔、講稿與參考輸出
├─ tools/make_sample.py          重新產生範例音檔（一般使用者用不到）
├─ config/default.toml           全域預設（進 git）
├─ config/local.toml             這台電腦專屬：麥克風、路徑…（不進 git，範例 local.example.toml）
├─ courses/<課名>.toml            你的課程設定（不進 git）
├─ courses/examples/             課程設定範例（進 git）
├─ engines.lock                  已測試的 whisper.cpp / llama.cpp 版本（進 git）
├─ whisper.cpp/  llama.cpp/      由 setup_engines.py 取得與編譯（不進 git）
├─ models/                       LLM 模型 .gguf（不進 git）
└─ outputs/<課名>_<YYYYMMDD>/     每堂課的輸出（不進 git）
```

### whisper.cpp / llama.cpp 版本

| 指令 | 說明 |
|---|---|
| `setup_engines.py` | checkout `engines.lock` 的版本並編譯；同版本已編好就略過 |
| `setup_engines.py whisper` | 只取得／編譯 whisper.cpp 並下載 Whisper 模型，不處理 llama.cpp |
| `setup_engines.py --update` | 升級到最新版，編譯成功後寫回 `engines.lock`（測試沒問題再 commit） |
| `setup_engines.py --rebuild` | 版本或後端不變，強制重新編譯 |
| `setup_engines.py --lock` | 不編譯，把目前的版本記進 `engines.lock` |
| `setup_engines.py --backend vulkan\|cuda\|metal\|cpu` | 指定後端（預設依平台） |
| `setup_engines.py --import-models <資料夾>` | 從其他位置搬入已下載的模型 |
| `setup_engines.py --generator Ninja` | 指定 cmake generator（Windows 想用 Ninja 而非預設判斷的 Visual Studio 時；不指定就維持原本行為） |

以 `BUILD_SHARED_LIBS=OFF` 靜態連結，整個專案資料夾搬到哪裡都能執行。
基準測試：`./bench_llm.sh [逐字稿.md] [第幾段]`（bash 腳本，Linux / macOS 可用），結果在 `outputs/_llm-bench/`。

`--file` 可用任何 ffmpeg 能解碼的檔案：mp3、m4a/aac、wav、flac、ogg/opus、wma、webm，以及 mp4/mkv/mov 等影片（自動取音軌）。

## 指令

| 指令 | 說明 |
|---|---|
| `lec run 課名` | 上課：即時轉錄＋總結，Ctrl+C 結束（自動阻止休眠） |
| `lec run 課名 --file 錄音.mp3` | 處理音檔：轉錄完再總結 |
| `lec run 課名 --transcribe-only` | 只轉錄，不啟動總結模型（等同 `--set summary.enabled=false`） |
| `lec run 課名 --model qwen3-4b --source mic2` | 臨時換模型、錄音來源 |
| `lec run 課名 --set vad.sensitivity=3` | 臨時覆寫任一設定（可重複） |
| `lec summarize <資料夾>` | 補做尚未完成的總結 |
| `lec summarize <資料夾> --redo 00:05:02` | 重做某一段（`--redo all` 全部重做，舊檔備份為 .bak） |
| `lec config 課名` | 印出合併後的設定 |
| `lec courses` / `lec new 課名 [--from 範例]` | 列出 / 建立課程設定檔 |
| `lec models [課名] [--json]` | 列出 summary／Whisper 模型設定與本機檔案狀態 |
| `lec devices [--test N] [--save N]` | 列出 / 測試 / 設定麥克風 |
| `lec doctor [課名] [--mic]` | 檢查環境（回報問題請附上輸出，`--json` 給 UI） |
| `lec status` / `lec stop` | 查看 / 停止目前的執行（`--json`、`--force`） |

停止方式（Ctrl+C 與 `lec stop` 等效，三個平台相同）：
- 錄音中一次：停止錄音，轉完剩餘段落，再補做最後一段總結（最多等 `summary.final_wait` 秒）。
- 補做總結時再一次：放棄總結，正常收尾（之後可用 `lec summarize` 補）。
- 轉錄剩餘段落時再一次：強制結束。

`lec stop` 是在輸出資料夾寫 `stop` 檔（`--force` 寫 `stop_force`），程式每秒檢查一次。
Windows 無法對別的行程送 Ctrl+C，所以 UI 與腳本一律用這個方式停止。

## 設定

合併順序：`config/default.toml` → `config/local.toml` → `courses/<課名>.toml` → 指令列。
課程設定檔只要寫跟預設不同的項目，最常用的是：

```toml
[whisper]
terms = ["光纖", "冗餘", "TCP/IP"]     # 同時用在 whisper prompt 與總結的錯字參考

[summary]
model = "qwen3-4b"
extra_instructions = "本課程著重網路概念，重點請保留協定名稱與層級。"
# prompt_file = "prompts/計概.md"      # 整份替換預設 prompt（可用 {course} {terms} {extra_instructions}）
```

若要依課程分層儲存，可在 `config/local.toml` 設定
`paths.session_name = "{course}/{date:%Y%m%d}"`，輸出會放在
`outputs/<課程名稱>/<YYYYMMDD>/`。預設值仍是單層的
`{course}_{date:%Y%m%d}`。

`lec summarize` 預設使用**目前**的課程設定檔（改完 prompt 可直接重做）；找不到才用資料夾內的 `config.used.toml`。

## 輸出（outputs/<課名>_<YYYYMMDD>/，同一天同課名再錄會加 _HHMM）

| 檔案 | 內容 |
|---|---|
| `transcript.md` / `transcript.srt` | 逐字稿（每 5 分鐘一個 `## hh:mm:ss`）、字幕 |
| `notes.md` | 筆記：每段 主題 / 重點 / 術語 / 老師強調 |
| `notes.jsonl` | 總結的原始結果（含驗證報告、耗時、token 數），notes.md 由此產生 |
| `recording_*.ogg` | 整堂錄音 |
| `config.used.toml` | 本次實際使用的設定 |
| `session.log`、`*-server.log` | 執行紀錄 |
| `status.json`、`events.jsonl` | 給 UI 讀的狀態 |

轉錄某一段連續失敗到重試用盡時，`transcript.md` 會留下
`> ⚠ 轉錄失敗，hh:mm:ss–hh:mm:ss…` 標出哪一段沒有逐字稿（原因同時寫進 `session.log`，
並計入 `status.json` 的 `errors`）。看到這個標記就表示那段音訊沒有進筆記，
可以用 `recording_*.ogg` 自行回聽。

## 防止加入逐字稿以外的內容

1. prompt 規定只能使用本段逐字稿；術語沒解釋就留空。
2. LLM 以 JSON schema 回覆，程式驗證：
   - 「老師強調」的原句要在逐字稿中找得到（相似度 ≥ `summary.quote_match`），否則刪除，並換成逐字稿原文、標上時間。
   - 術語要出現在逐字稿中，否則依 `summary.unverified_terms` 標 ⚠ / 刪除。

## 給 UI 的介面（UI 不 import core）

- **設定**：讀寫 `courses/*.toml`、`config/local.toml`；`lec courses --json` 列出課程；`lec config 課名` 看合併結果；`lec devices --json` 列出麥克風；`lec doctor --json` 環境檢查。
- **啟動 / 停止**：subprocess 執行 `lec run …`；停止在輸出資料夾寫 `stop`（或 `stop_force`）檔，也可執行 `lec stop`。不需要送訊號，三個平台一致。
- **狀態**：`run.json`（是否執行中、輸出資料夾；位置見 `lec doctor` 的「狀態資料夾」，Linux 為 `~/.local/state/lecture-notes`）、`<資料夾>/status.json`：

```json
{"phase": "recording", "mode": "live", "course": "…", "pid": 123,
 "elapsed": 1834.0, "transcribed": 1820.5, "transcribe_lag": 6.2, "queue": 0,
 "sections_total": 7, "sections_summarized": 6, "llm_busy": true, "llm_section": "00:30:02",
 "servers": {"whisper": "ok", "llama": "ok"}, "errors": 0, "last_error": null}
```

  `phase`：starting → loading → recording / transcribing → summarizing → finishing → done（或 failed / aborted）。
- **事件**：`<資料夾>/events.jsonl`（phase、summary、error、stop_requested）。

## 模組

```
lec                 CLI 入口
core/cli.py         子指令
core/config.py      設定載入、合併、驗證、輸出
core/session.py     一次 run / summarize 的流程、訊號處理、收尾
core/servers.py     whisper-server / llama-server 啟動、沿用、關閉
core/transcribe.py  VAD 切段＋即時轉錄（原 live_transcribe.py）
core/summarize.py   分段總結、驗證、notes.md
core/status.py      status.json / events.jsonl / 執行鎖
core/devices.py     錄音來源列表與音量測試
core/doctor.py      環境檢查
core/platform.py    平台差異（錄音後端、防休眠、狀態資料夾、行程管理）
```

## 範例音檔

`samples/test8min.ogg` 是一段約 8 分鐘的模擬課堂錄音：
講稿（`samples/test8min.txt`）由本專案自行撰寫，再由作者本人朗讀錄製，
跟 lec 實際錄音一樣是單聲道 Opus 語音。
不是任何真實課程的錄音，著作權屬本專案，可自由散布。詳見 `samples/README.md`。

## 開發

- 分支：`main`。程式、設定範例、prompts、`samples/` 範例素材與 `engines.lock` 進 git；本機設定、個人課程、原始錄音與 outputs 已由 `.gitignore` 排除。
- 升級引擎：`python3 setup_engines.py --update` → `lec run 課名 --file <錄音>` 與 `./bench_llm.sh` 確認沒問題 → `git commit engines.lock`。
- 回報問題時附上 `lec doctor --json` 與該堂課的 `session.log`。
- 授權：MIT（見 LICENSE）。whisper.cpp、llama.cpp 為 MIT，Qwen3 模型為 Apache-2.0，皆在執行時自行取得。
- 錄音前請先取得老師或學校同意；錄音與逐字稿只留在本機。
