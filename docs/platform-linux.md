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
- `setup_engines.py` 會檢查 `glslc`、`libvulkan-dev`（用 `pkg-config --exists vulkan`
  或 `VULKAN_SDK` 環境變數判斷）。
- CUDA 需要 `nvcc`（`--backend cuda`）。

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

## 已知限制

- 目前只在 Debian 13 + Intel Arc 140V（Vulkan）實測過；其他發行版（Arch、
  Fedora、非 systemd 的發行版等）理論上應該可行，但沒有實機驗證。
