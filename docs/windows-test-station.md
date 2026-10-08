# Windows 測試站（契約 v1）

Windows 11 / RX 6600 XT / 獨立麥克風，同時供日常使用。此工具由 Codex 維護，
只負責測試派送與執行；不更改既有平台與引擎實作的所有權。

## 架構與授權

開發 agent 判斷 diff → 推送完整 commit → `dispatch.py` → GitHub hosted validation →
Windows self-hosted runner → `summary.json` → 開發 agent 查結果並修正。
常駐 Codex 不是接單必要條件，可協助解讀 PC 本機日誌。

- 只有 repository owner 從 `main` 的 `workflow_dispatch` 派送；沒有 PR、fork、push 自動觸發。
- 限同一 repository 的 `main`、`codex/*`、`claude/*`，SHA 必須是派送驗證時的 branch head。
- Harness 取 workflow 的固定 SHA；待測程式另取完整 SHA。更新分支後需新 request。
- 公開 repo 的 self-hosted runner **不是安全沙箱**：只有已信任的程式才能派送。
  owner dispatch 與 branch 前綴無法把惡意程式變安全。不要把外部 PR 直接搬到允許分支執行。
  建議使用沒有私人憑證的專用 Windows 標準使用者帳號；不要以 Administrator 執行 runner。
  新增其他可使用此 runner 的 workflow 時也必須審查。
- Runner 與 Codex 必須共用此帳號的測試時段設定；不把密碼、PAT、registration token 放進 Git。

## 測試組合

| Profile | 實際執行 | 前置條件／證據範圍 |
|---|---|---|
| `basic` | UI Python 依賴檢查、全部 Python unittest、CLI help | 自動；包含 mock，沒有 GPU 或麥克風 smoke；不包含 frontend build |
| `gpu` | `lec run --file ... --transcribe-only` | 有效 GPU 時段、Vulkan whisper build、模型與本機固定音檔；只驗檔案轉錄，沒有 LLM 摘要 |
| `microphone` | `lec devices --test <明確設定來源>` | 有效 microphone 時段、ffmpeg 與已設定裝置；會錄 3 秒，驗證裝置可開啟，非聲音品質保證 |

GPU 報告的 `acceleration_verified=false` 是刻意的：Vulkan build stamp 和轉錄成功
不能證明沒有 CPU fallback。GPU 使用率、裝置選擇與 backend log 要另做 hardware validation。
正常停止、強制停止、UI 桌面操作、完整安裝／升級和 llama 摘要不在 v1 hardware smoke 範圍。
相關改動仍需依 `docs/platform-windows.md` 做額外測試，不能只用本 workflow 宣稱通過。

## 第一次安裝（Windows PowerShell）

先安裝原生 Windows Git 與 Python 3.11+，確認 `git --version`、`python --version`。
不使用 WSL。先由人審查及合併此 PR，workflow 必須在 main 才能派送。

以下路徑是範例，可改成你指定的專用測試目錄。腳本會顯示路徑要求輸入 YES，
不會自行修正磁碟或資料夾名稱。尚未 clone 時：

```powershell
$project = Join-Path $env:USERPROFILE 'project\lecture-notes'
git clone https://github.com/RichardstGG/lecture-notes.git $project
Set-Location $project
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\windows_runner\Initialize-Station.ps1 -ProjectRoot $project
```

若已有 checkout，直接進該目錄執行初始化，腳本不 pull/reset/覆寫現有檔案。
初始化建立 `%LOCALAPPDATA%\lecture-notes-test-runner\venv`，安裝 fastapi、uvicorn、httpx；
不會安裝 ffmpeg、引擎、模型、系統服務，也不會開啟硬體時段。
`ExecutionPolicy Bypass` 僅針對這一次 PowerShell process。

到 repository Settings → Actions → Runners → New self-hosted runner，選 Windows / x64，
按照 GitHub 當下顯示的下載、checksum 與註冊指令操作，目錄另放在例如
`%LOCALAPPDATA%\lecture-notes-actions-runner`，**不要放在 project 或 Actions 的 `_work` 內**。
註冊時加上 label `lecture-notes-win11`，其他預設 labels 保留。
registration token 只在 PC 輸入，不貼到聊天。

先用 `run.cmd` 在登入桌面互動執行，不安裝 service，以便麥克風測試沿用使用者工作階段。
初始化與 runner 必須用同一個 Windows 帳號。PC 保持上線、不要休眠；Codex 開著不代表 runner 在線。
初次先派 basic，確認 native Windows 測試結果後才 provision 硬體。

## 硬體環境與時段

RX 6600 XT 使用 Vulkan 路線，非 CUDA。依現有 Windows 平台文件安裝 ffmpeg、
MSVC C++ build tools、CMake、Vulkan SDK，再依現有安裝入口準備 whisper 引擎與模型。
先在 PC 確認引擎可以運作；v1 不自動改動 PC 的 GPU 驅動或編譯環境。
在 `%LOCALAPPDATA%\lecture-notes-test-runner\machine.json` 手動填入本機設定：

```json
{
  "schema_version": 1,
  "whisper_dir": "D:/test-assets/whisper.cpp",
  "whisper_model": "D:/test-assets/whisper.cpp/models/ggml-large-v3-turbo.bin",
  "input_audio": "D:/test-assets/test8min.ogg",
  "microphone": "明確的 DirectShow 裝置名稱或 Alternative name"
}
```

這些路徑只是範例，填入你的實際絕對路徑。可使用 repo 的凍結音檔，不要改樣本。
沒有硬體設定時 basic 仍可用；hardware profile 會回報 blocked。
引擎必須有 `build/.lec-build` 的 `BACKEND=vulkan`；不支援無 stamp 的預編譯引擎。
測試固定用 loopback port 28178，已占用就 blocked，避免使用日常引擎服務。

```powershell
$python = Join-Path $env:LOCALAPPDATA 'lecture-notes-test-runner\venv\Scripts\python.exe'
# 從 project 根目錄操作；最多 240 分鐘。
& $python tools/windows_runner/station.py open-window --profiles gpu --minutes 30
# 需要錄音時才開 microphone；也可一次指定 gpu microphone。
& $python tools/windows_runner/station.py open-window --profiles microphone --minutes 10
& $python tools/windows_runner/station.py close-window
```

每次開啟會取代原有時段，關閉會刪除時段檔。執行中每 0.5 秒檢查一次，關閉／到期
會終止當次測試樹。Windows Job Object 在 runner process 結束時清理剩餘子行程，
建立 containment 失敗便不啟動測試。逾時／被關閉不算測試失敗重試成功，也不能視為通過。
測試子行程使用 `CREATE_NO_WINDOW`，避免 Windows console event 波及 runner 的主控台。
收到可處理的 `KeyboardInterrupt` 時，摘要為 `error`／`execution_interrupted`、退出碼 4；
只有全部步驟完成才回報 `passed`。直接強制終止 harness 時可能沒有摘要，不能算通過。
這是測試資源限制，不是阻擋惡意待測程式的安全邊界。

## Agent 的派送與回報

修改路徑／編碼／CLI／UI 啟動等至少派 basic；行程、引擎、錄音改動再選相關硬體 profile。
純文件／提示詞通常免派送。先做本機適用檢查，推送可 review 的 commit，再用完整 SHA：

```bash
python3 tools/windows_runner/dispatch.py --ref codex/example --sha FULL_40_CHARACTER_SHA --profile basic --reason 'Windows path and CLI regression'
```

可先加 `--dry-run` 檢查，這不會送出 request。真正派送需 `gh` 登入 repository owner。
此派送屬使用者已授權的測試流程；不得藉此傳送私人資料或執行其他任務。
`gpu`／`microphone` 需先讓使用者在 PC 開時段；agent 不得代開或延長。

派送後 `gh run list --repo RichardstGG/lecture-notes --workflow windows-station.yml --json databaseId,displayTitle,status,conclusion,url`，
用 request ID 找到 run，再 `gh run watch RUN_ID --repo RichardstGG/lecture-notes`，
`gh run download RUN_ID --repo RichardstGG/lecture-notes` 取得 summary artifact。
報告對不上 SHA／request ID 就不能採信。成功派送不代表測試通過。

每台 PC 同時只跑一項測試；本機 exclusive lock 也阻擋手動重入。
GitHub concurrency 只保留最多一個 pending job，多個 request 可能取消較早的 pending job，
不是耐久 FIFO queue。發送前先查同 SHA/profile 是否已排隊或執行，避免重複派送；
取消、offline、無 summary、缺環境都要明確回報，不得算成功。硬體不自動重試。
強制終止造成 stale `station.lock` 時，先確認沒有 station.py／測試子行程，再由 PC 使用者移除。

## 報告契約與資料

`summary.json` schema_version=1：request_id、local_run_id、sha、profile、status、steps
（name/exit_code）、python、os、os_release、started_at（Unix 秒）、duration_seconds、evidence。
可選欄位：reason、tests_run、tests_skipped、configured_backend、acceleration_verified。
status／process exit：passed=0、failed=1、blocked=2、timed_out=3、error=4。
blocked reason 為穩定代碼，例如 test_window_closed、hardware_not_configured、sha_mismatch。

只上傳摘要（保留 14 天），不包含電腦名稱、絕對路徑、裝置名稱、完整 exception 或測試 stdout。
原始 log、轉錄輸出與 runner error 存在 PC 的
`%LOCALAPPDATA%\lecture-notes-test-runner\runs\<local_run_id>`；不提交 Git、不自動上傳。
可依 request 的 local_run_id 在 Windows Codex 查閱，回傳之前另行篩選私人內容。
PC 使用者定期清理已不需要的本機 runs；v1 不自動刪除證據。

## 驗收

1. basic 在真正 Windows 上有 tests_run > 0，結果／SHA／request ID 可對應。
2. 未開時段的 hardware request blocked，未開啟裝置／載入模型。
3. 開 GPU 時段不授權麥克風；短時段到期或手動 close 能停止執行，沒有殘留測試子行程。
4. 同時兩個 request 不重疊使用硬體；PC 日常設定與已有行程保持不變。
5. 本機 log 存在，GitHub artifact 只有 summary.json。
6. 實際跑 Vulkan 檔案轉錄與指定麥克風，分別標 hardware smoke；GPU 加速另查證。

Linux 上的規則／mock 測試不算上述 Windows 真機驗收。workflow 首次啟用與上述驗收
必須在 PR 經人審查合併、PC 註冊後完成。

`tests.test_windows_runner.NativeProcessTests` 在原生 Windows 啟動真實的 Python
子行程，驗證主控台隔離、逾時、Job Object 正常退出／強制終止後的清理。
其中撤銷／到期只使用暫存目錄內的合成時段與無硬體的睡眠行程，不會開啟本機硬體時段，
也不能當成 GPU／麥克風實測；其他平台因缺 Windows API 不執行這組測試。
