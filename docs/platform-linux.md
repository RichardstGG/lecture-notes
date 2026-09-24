# Linux 平台備忘（已實測）

狀態：**已實測**（Debian 13、Intel Arc 140V，見主 README「平台支援」表）。
本檔記錄 Linux 特有的行為與已知限制，方便日後跨平台修改時對照迴歸。

## 錄音（PulseAudio / PipeWire）

- 後端固定用 `pulse`（`core/platform.py` 的 `AUDIO_BACKENDS`）。PipeWire 透過
  `pipewire-pulse` 相容層提供同一份 `pactl` 介面，不需要另外分支處理。
- 裝置列表：優先用 `pactl -f json list sources`，失敗（舊版 pactl 不支援
  `-f json`）才退回 `pactl list short sources` 純文字格式。兩種格式都有對應的
  mock 測試，見 `tests/test_platform_parsers.py::PulseSourcesTests`。
- 預設會濾掉 `*.monitor`（喇叭回音）來源；`lec devices --all` 才會列出。
- `default_source()` 用 `pactl get-default-source`。
- 錄音輸入參數固定 `-f pulse -i <來源名稱或 default>`。

## GPU 後端

- 預設 Vulkan（`DEFAULT_BACKEND["linux"] = "vulkan"`）。
- 編譯工具的判斷在 `core/platform.py::missing_build_tools()`：Vulkan 需要 `glslc` 與
  `libvulkan-dev`（用 `pkg-config --exists vulkan` 或 `VULKAN_SDK` 環境變數判斷），
  CUDA 需要 `nvcc`（`--backend cuda`），其他平台同一份邏輯。
- `setup_engines.py`（編譯前）與 `lec doctor`（「編譯工具」那一行）共用這個函式，
  兩邊不會給出不同答案。doctor 要檢查哪個後端，優先讀 `<引擎>/build/.lec-build`
  （`setup_engines.py` 寫的編譯紀錄）裡的 `BACKEND=`，沒有紀錄才用平台預設。
  doctor 缺工具只算**警告**：已經編好引擎、或改用官方預編譯檔的
  人不需要編譯環境，不該因此讓 `lec doctor` 回傳 exit code 1。

## 防止休眠

- 用 `systemd-inhibit --what=sleep:idle:handle-lid-switch ... tail --pid=<pid> -f /dev/null`，
  跟著 lec 的行程存在，`lec` 結束時自動釋放。
- 沒有 `systemd-inhibit`（非 systemd 發行版）時只會印警告，不會中斷執行。

## 行程管理

- `spawn_kwargs()` 用 `start_new_session=True`，讓 whisper-server / llama-server /
  ffmpeg 不會被終端機的 Ctrl+C 直接波及。
- 停止／關閉優先用 `stop` / `stop_force` 檔（跟 macOS / Windows 一致，見主
  README「停止方式」），行程層級的 `kill_tree()` 用 `os.killpg`
  （SIGTERM 等 `timeout` 秒 → SIGKILL）。
- `pid_alive()` 用 `os.kill(pid, 0)`，另外排除殭屍程序（讀 `/proc/<pid>/stat`）。

## 動態函式庫

- `setup_engines.py` 預設 `BUILD_SHARED_LIBS=OFF`（靜態連結），一般不需要額外
  設定 `LD_LIBRARY_PATH`；`library_dirs()` / `env_with_libs()` 是為了「搬到別的
  機器」或未來改用共用函式庫編譯時預留的路徑，掃描 build 資料夾內的
  `lib*.so*` 並加進 `LD_LIBRARY_PATH`。

## 回歸測試涵蓋範圍

跨平台修改（`core/platform.py`）時，以下 Linux 行為都有 mock 測試把關，
執行 `python3 -m unittest`（於 repo 根目錄）即可：

- `_pulse_sources()` 的 JSON／short 格式解析、缺 `pactl` 時回傳 `None`
- `list_sources()` 濾掉 `.monitor` 來源
- `default_state_dir()` 讀 `XDG_STATE_HOME` 與其預設值
- `audio_backend()` / `engine_backend()` 在 `NAME == "linux"` 時的預設值
- `spawn_kwargs()`、`kill_tree()` / `kill_now()` 的 POSIX 分支
- `find_engine_bin()` 在一般（非 Visual Studio）build 佈局下的搜尋順序
- `missing_build_tools()` 在 Linux 上對編譯器、`glslc`、`libvulkan-dev`、`nvcc` 的判斷
  （`tests/test_platform_build_tools.py`）
- `lec doctor` 的平台分支與「編譯工具」項目（`tests/test_doctor_platform.py`）

## 已知限制

- 目前只在 Debian 13 + Intel Arc 140V（Vulkan）實測過；其他發行版（Arch、
  Fedora、非 systemd 的發行版等）理論上應該可行，但沒有實機驗證。
