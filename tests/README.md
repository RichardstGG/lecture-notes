# 測試

跨平台修改的 mock-based tests，都只依賴 Python 標準函式庫的 `unittest`
（不需要另外 `pip install pytest`；如果本機已經有 pytest，這些測試檔也能直接
被 pytest 收集執行，寫法上沒有用到任何 pytest 專屬功能）。

在 repo 根目錄執行全部測試：

```bash
python3 -m unittest
```

只跑某一個檔案：

```bash
python3 -m unittest tests.test_platform_parsers -v
```

## 檔案對照

| 檔案 | 涵蓋範圍 |
|---|---|
| `test_platform_parsers.py` | 錄音來源解析：PulseAudio（Linux）、AVFoundation（macOS）、DirectShow（Windows，含 dshow 解析 bug 的迴歸測試） |
| `test_platform_paths.py` | 狀態資料夾預設路徑、錄音／引擎後端選擇 |
| `test_platform_process.py` | 行程存活判斷、spawn 參數、中斷／強制關閉、防休眠（Windows 分支用 mock 假造 `ctypes.windll`） |
| `test_platform_engine.py` | 引擎執行檔搜尋（Visual Studio Release/Debug 佈局等）、動態函式庫搜尋路徑 |
| `test_platform_build_tools.py` | 編譯工具鏈偵測：`find_msvc()`（vswhere）、`cxx_compiler()`、`missing_build_tools()` 在三個平台與各後端的判斷 |
| `test_setup_engines.py` | `setup_engines.py` 的後端／generator 選擇、`engines.lock` 讀寫、git／cmake 檢查與「編譯工具判斷委派給 `core/platform.py`」、`--generator` 參數傳遞 |
| `test_upgrade.py` | `upgrade.py` 的 fast-forward 更新保護、引擎參數、virtualenv 與 frontend build orchestration |
| `test_session_paths.py` | 輸出資料夾命名、分層、同名後綴與 traversal 防護 |
| `test_devices_permission_hint.py` | 麥克風權限錯誤訊息（啟發式，非窮舉） |
| `test_devices_sources.py` | 來源解析（`devices.resolve()` 與執行期 `resolve_source()` 的差別）、音量判斷門檻、列不出裝置時的提示 |
| `test_doctor_helpers.py` | `core/doctor.py` 的小工具函式，以及只轉錄模式（`summary.enabled = false`）下的檢查結果 |
| `test_doctor_platform.py` | `lec doctor` 的平台分支（實測／實驗中措辭、平台專屬指令、錄音後端）與「編譯工具」項目 |
| `test_cli_transcribe_only.py` | `lec run --transcribe-only` 的設定覆寫與 help contract |
| `test_cli_models.py` | `lec models` 的人類可讀輸出與 versioned JSON model discovery contract |
| `test_ui_course_store.py` | UI 課程設定的安全路徑、symlink 防護、驗證與原子寫入 |

macOS／Windows 相關的測試全部是靜態驗證＋mock，**不代表已經在真機上測過**，
細節與已知限制見 `docs/platform-macos.md`、`docs/platform-windows.md`。

檔名都用 `test_platform_*` / `test_setup_engines*` / `test_devices_*` /
`test_doctor_*` 前綴，跟 Codex 之後可能加入的 UI / CLI contract tests
（例如 `test_ui_*`、`test_cli_contract*`）區隔開，避免撞名或誤植。
