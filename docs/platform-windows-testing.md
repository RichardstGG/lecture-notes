# Windows 測試的可攜性守則、Linux 上的模擬方法與診斷紀錄

測試在 Linux 開發、在 Windows 測試站（見 [windows-test-station.md](windows-test-station.md)）驗證。2026-10 一輪診斷發現：
**很多測試在 Linux 上完全無害，在 Windows 上卻會失敗、卡住，甚至殺掉整個測試 runner 而不留任何結果。**
這份文件記下規則、能在 Linux 上提前抓到這些問題的方法，以及那一輪診斷的結論。

範圍：Claude 擁有的平台與引擎測試。Codex 的 CLI／狀態／UI 測試適用同樣的規則，但那些檔案由 Codex 維護。

## 1. 寫測試時的守則

| # | 守則 | 違反時的症狀 |
|---|---|---|
| 1 | **永遠不要讓測試真的呼叫 `os.kill` 打到自己或同一個主控台。**強制 `IS_WINDOWS=False` 去測 POSIX 分支時，一定要 mock `P.os.kill`（並斷言參數）。 | Windows 的 `os.kill(pid, sig)` 不是 POSIX 語意：sig `0`／`1` 是 `CTRL_C_EVENT`／`CTRL_BREAK_EVENT`（對整個主控台的行程送事件，**包含 runner 自己**），其他 sig 是 `TerminateProcess`。runner 以 `0xC000013A` 結束，`tests_run=0`。 |
| 2 | **模擬 POSIX 時，Windows 沒有的屬性要用 `create=True` patch，而且正式碼的 POSIX 分支會讀到的也要一併補。**例如 `os.killpg`、`os.sysconf`、`signal.SIGKILL`。 | `AttributeError`。只補 `killpg` 不夠：`kill_tree` 會讀 `signal.SIGKILL`，測試斷言本身也可能引用它。 |
| 3 | **每個文字檔讀寫都明確指定 `encoding="utf-8"`**；`subprocess` 用 `text=True` 時也要 `encoding="utf-8", errors="replace"`。 | Windows 預設 cp950，讀 UTF-8 的中文會 `UnicodeDecodeError`，或讀成亂碼卻沒報錯。 |
| 4 | **寫給管線的 JSON 用 `ensure_ascii=True`。** | stdout 接到管線時用系統預設編碼；編不出來的字元讓寫入端崩潰，Big5 的第二個位元組可能剛好是 `0x5C`（反斜線），會破壞 JSON 跳脫。 |
| 5 | **暫存目錄一律 `Path(tmp.name).resolve()`**，只要正式碼會 `resolve()`。 | Windows 的 `TEMP` 常是 8.3 短路徑（`USERNA~1`），`resolve()` 之後變長路徑，期望值對不上。 |
| 6 | **存進檔案的相對路徑用 `as_posix()`**，不要 `str(path)`。 | Windows 寫出 `tracks\mic.ogg`，在別的平台讀就是找不到的檔名。 |
| 7 | **venv 裡的 python 用 `upgrade.venv_python()` 取得**，不要寫死 `.venv/bin/python`。 | Windows 是 `.venv/Scripts/python.exe`，fixture 建在錯的位置，呼叫序列斷言失敗。 |
| 8 | **假行程（fake）收尾的動作要放進 `finally`**，不要依賴只在 POSIX 存在的常數。 | `FakeCapture.kill()` 在 `_halt.set()` 之前就讀 `signal.SIGKILL`，Windows 上 `AttributeError`，背景執行緒永遠不停，測試卡到逾時，直譯器結束時 daemon 執行緒對已關閉的 stderr 寫入而崩潰（`0xC0000409`）。 |

守則 1 有靜態檢查：`tests/test_posix_simulation_hygiene.py` 掃描所有測試，找出「強制 `IS_WINDOWS=False` ＋ 呼叫
`pid_alive`／`kill_tree`／`kill_now`／`interrupt` ＋ 沒有 mock `kill`／`killpg`」的組合。這是找文字模式，**不是證明**，抓不到所有
可能讓測試對自己送訊號的寫法。

## 2. 在 Linux 上提前抓到這些問題

每一項都先**量修正前的失敗、再確認修正後通過**，這樣才知道修的是對的原因。

| 想模擬的 Windows 現象 | 做法 | 抓得到的 |
|---|---|---|
| 缺少 API（`signal.SIGKILL`、`os.killpg`、`os.sysconf`） | 在跑測試前 `del signal.SIGKILL`、`delattr(os, "killpg")`…（見下方腳本）。**要先 import `unittest.mock`、`asyncio`、`subprocess`**，否則它們在 import 時就會用到被刪掉的 API。 | 守則 2、8 |
| 預設編碼是 cp950 | `LC_ALL=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 python3 -m unittest …`（預設編碼變 ASCII）。**指令列與環境變數裡不能有中文**，否則 Python 連指令都解不開。 | 守則 3、4 |
| 8.3 短路徑的 `TEMP` | `ln -s <真實目錄> <連結>`，再 `TMPDIR=<連結> python3 -m unittest …` | 守則 5 |
| venv 版面不同 | 把 `upgrade.venv_python` patch 成回傳 `Scripts/python.exe`；或測試自己用 `subTest` 兩種版面都跑 | 守則 7 |
| 真實 `os.kill` 呼叫 | 用間諜函式包住 `os.kill`／`os.killpg`，記錄每一次真實呼叫與所屬測試（見下方腳本） | 守則 1 |

**這些模擬各有偏差，不是 Windows 的替代品：**

- ASCII 比 cp950 嚴格（任何非 ASCII 位元組都失敗，cp950 只在遇到不合法序列時才失敗），所以它回報的是「隱含編碼依賴的
  清單」，**不是** Windows 失敗數的預測；也有反方向的風險：cp950 能解出卻是亂碼的情況，ASCII 一樣會抓到。
- 刪掉 `os.killpg`／`signal.SIGKILL` 會讓「真的在 Linux 上走 POSIX 分支殺真行程」的測試也失敗（`test_diarize_worker` 的取消測試、
  `PidAliveProcessNameTests` 等；連標準函式庫的 `Popen.kill()` 在 POSIX 上都會讀 `signal.SIGKILL`），那是模擬過度的假象：
  真的 Windows 上 `IS_WINDOWS` 為真、走的是 `taskkill`，Linux 專用的測試也會被 `skipUnless` 跳過。判讀時要區分
  「patch 不存在的屬性」與「真的走 POSIX 路徑」，並把模擬的範圍限縮在相關的測試模組。
- Linux 上 `os.kill(os.getpid(), 0)` 的 11 次真實呼叫（`test_cli_stop`、`test_status_contract`、`test_status_lock`、
  `test_platform_process`）**只有 1 個**在 Windows 上是危險的：只有那個測試強制走 POSIX 分支。其餘的在 Windows 走 `ctypes`
  分支，不會呼叫 `os.kill`。看到間諜記錄時不要全部當成問題。

### 模擬腳本（存成 `winsim.py`，不要 commit）

```python
#!/usr/bin/env python3
"""用法：winsim.py [--no-sigkill] [--no-posix-os] discover -s tests -t ."""
import asyncio, os, signal, subprocess, sys, tempfile, shutil, threading, unittest, unittest.mock  # 先載入
sys.path.insert(0, os.getcwd())

def main():
    args = sys.argv[1:]
    if "--no-sigkill" in args:
        args.remove("--no-sigkill"); del signal.SIGKILL
    if "--no-posix-os" in args:
        args.remove("--no-posix-os")
        for name in ("killpg", "sysconf", "getpgid"):
            if hasattr(os, name): delattr(os, name)
    unittest.main(module=None, argv=["winsim", *args])

if __name__ == "__main__":      # 必須有：multiprocessing 的 spawn 會重新 import 主程式，沒有這行會遞迴地重跑整個套件
    main()
```

### 真實 `os.kill` 間諜（概念）

```python
real_kill = os.kill
def spy(pid, sig):
    log.append((pid, int(sig), pid == os.getpid(), where_in_tests()))   # where_in_tests：從 traceback 找 tests/ 內的 frame
    return real_kill(pid, sig)
os.kill = spy
```

## 3. 怎麼讀 Windows 測試站的摘要

`summary.json`（schema 1）的重點：`request_id`、`sha`（**兩者都要對得上你派送的**才能採信）、`status`、`tests_run`、
`tests_skipped`、`reason`、`steps[].exit_code`。原始日誌留在測試 PC 本機，只上傳摘要；用 `local_run_id` 去 PC 上找。

- **`tests_run=0`／`reason=no_tests_executed` 不代表沒有測試**，代表站台在日誌裡找不到最後的 `Ran N tests in …` 那一行，
  也就是測試行程**在跑完之前就結束了**。日誌裡仍有每個測試的標頭，最後一個標頭就是死亡當下在跑的測試。
- 結束代碼：

| 代碼 | 十六進位 | 意義 | 已知成因 |
|---|---|---|---|
| `1` | | 有測試失敗，正常跑完 | |
| `3221226505` | `0xC0000409` | fail-fast（stack buffer overrun 類） | 直譯器結束時 daemon 執行緒對已關閉的 stderr 寫入（`FakeCapture.kill` 沒設 `_halt`） |
| `3221225786` | `0xC000013A` | `STATUS_CONTROL_C_EXIT`：被主控台的 Ctrl+C 事件終結 | 測試真的呼叫了 `os.kill(os.getpid(), 0)`（守則 1） |

- 「中斷」會在**後面某個正在建立子行程的測試**才浮現（KeyboardInterrupt 出現在 `CreateProcess`），不在送訊號的那個測試裡。
  所以 traceback 指到的位置不是源頭，要往前找有沒有真實的 `os.kill`／主控台事件。
- 離線、被取消、被封鎖、缺少報告、逾時、被跳過都要明講，**絕不當成通過**。

## 4. 2026-10 診斷紀錄

起因：Codex 對 PR #76 的 Windows `basic` 做本機診斷，35 個失敗方法（42 筆 FAIL／ERROR）在 PR 與基線各兩輪重現，
基線是當時的 `main`（`974eb09`）。結論：**基線既有的 Windows 失敗，不是 PR #76 引入的。**

### 35 個方法的對應

| 類別 | 根因 | 方法數 | 負責 | 修正 |
|---|---|---|---|---|
| A | `FakeCapture.kill` 讀 Windows 沒有的 `signal.SIGKILL`，`_halt` 沒被設定 | 9 | Claude | #78 |
| B | 測試用預設編碼（cp950）讀 UTF-8 成品 | 6（diarize）＋3（CLI／status／summarize） | Claude／Codex | #78／#77 |
| C | POSIX 模擬 patch Windows 沒有的 `os.sysconf`／`os.killpg`（且漏掉 `signal.SIGKILL`） | 5 | Claude | #78 |
| D | 8.3 `TEMP` 與 `resolve()` 的長路徑不一致 | 5（platform_engine）＋2（session_meetings） | Claude／Codex | #78／#77 |
| E | fixture 寫死 `.venv/bin/python` | 3 | Claude | #78 |
| F | `capture.json` 以主機分隔符序列化相對路徑 | 1 | Claude | #79 |
| G | `atomic_write` 的 `os.replace` 在目標被開著時 `PermissionError(winerror=5)` | 1 | Claude | #80 |

另外在診斷之外找到、並修正的**正式碼**問題（診斷的 Windows `basic` 抓不到，因為 cp950 編得出這些中文）：
`diarize_worker` 以 `ensure_ascii=False` 寫 stdout、`pid_alive` 遇到非 UTF-8 行程名稱會崩潰（會讓 `lec` 啟動時崩潰）、
`_probe_duration` 的 `text=True` 沒指定編碼（#81）。

驗證每一類時都先在 Linux 重現（上一節的方法），修正前得到與診斷相同的失敗方法，修正後通過。Linux 重現時多抓到幾個診斷沒列的
同類失敗（A 多 1 個、B 多 2 個），以及：**C 類光補 `create=True` 不夠**。

### 中斷事件（`0xC000013A`）

Windows `basic` 曾兩次（Codex 的 `aef6694a…` 與 Claude 的 `d3c14c73…`，不同 SHA）以 `tests_run=0` 結束。Codex 讀兩份原始日誌：
兩次都停在 `test_session_meetings.MeetingDirectoryTests.test_independent_processes_allocate_distinct_directories` 的
`subprocess.Popen` → `CreateProcess` 被 `KeyboardInterrupt` 打斷，各有 486 個測試標頭。

源頭：`tests/test_platform_process.py::test_posix_current_process_is_alive` 把 `IS_WINDOWS` 設成 `False` 卻沒 mock `os.kill`，
實際執行 `os.kill(os.getpid(), 0)`。Windows 上 `0` 是 `CTRL_C_EVENT`。證據（Codex 在測試站上的隔離實驗，依序執行「PID 測試 →
會議子行程測試」）：保留真實 `os.kill` 時兩次都在後者被中斷（`0xC000013A`）；runtime mock `os.kill` 時兩次都完成（exit 0）。
**未找到 `CTRL_BREAK` 的證據。**

**未解：**PR #76 head 的第一次 `basic`（`74fc2933…`）同樣含這個測試、harness 也相同（`main@974eb09`，harness 的最後修改在三次
run 之前），卻能跑完 991 項。所以「這個測試每次都會造成中斷」不成立；它在上述順序下足以造成中斷，但為什麼那一次沒發生，目前**無法解釋**。

修正：mock `P.os.kill` 並保留 `assert_called_once_with(os.getpid(), 0)`（#78），並加上守則 1 的靜態檢查。

### 合併後的 Windows 驗證（2026-10-09）

把 Codex 的 #77 與 Claude 的 #78–#81 合併成一個**暫時的驗證分支**（`claude/windows-verify-combined`，SHA
`f30ef0cb03bb9b09f9112509045fe905dc7710a1`；#78 與 #81 在 `tests/test_platform_process.py`、`tests/test_capture_session.py` 有
「兩邊各自新增」的衝突，保留兩邊），派送 `basic`：request `8ffcd2bb67a9452aa6508f4fbc4256fd`、
run 37894227163、`local_run_id=066e04ad9f22484e9d5a85e8b74f98be`。

| 項目 | 結果 |
|---|---|
| 套件是否跑完 | **是**：`tests_run=1005`，與同一個樹在 Linux 上的 1005 項一致；不再被 `0xC000013A` 中斷 |
| 耗時 | 286 秒 |
| 跳過 | 10（Linux 是 9） |
| `unittest` 結束代碼 | **1：有測試失敗**。摘要只上傳計數，**失敗的測試名稱只在測試 PC 的本機日誌裡**，目前未取得 |

**已經確定的：**中斷源頭確實是 `test_posix_current_process_is_alive` 真的呼叫 `os.kill(os.getpid(), 0)`；mock 之後套件完整跑完。
**還不知道的：**是哪些測試失敗，所以 A–G 各類的修正在真 Windows 上是否生效，**尚未確認**。需要有人讀那份本機日誌，
例如在測試 PC 上：

```powershell
Select-String -Path <local_run 目錄>\unittest.log -Pattern '^(FAIL|ERROR):'
```

取得清單之後，與上面的 35 個方法逐一對照：仍然失敗的屬於「修正沒生效」；不在那 35 個之內的屬於「新發現」（可能是這一輪新增的
測試在 Windows 上的行為，也可能是先前被早死的 run 遮住的）。

## 5. 尚未驗證／待辦

- 完整 `basic` 已能跑完（1005 項），但**仍有失敗，名稱未知**（見「合併後的 Windows 驗證」）。在拿到失敗清單、逐一確認之前，不得宣稱 A–G 在 Windows 上已修好。
- `atomic_write` 的重試退避（約 0.6 秒）是否足夠只是推估，沒有實測讀取者佔用檔案的時間分布。
- 守則 3 的隱含編碼清單中，Codex 的測試檔仍有不少處（ASCII 環境下 25 個錯誤，集中在 UI／CLI 測試）；它們由 Codex 維護。
- `tools/windows_runner` 屬 Codex；若要讓站台在偵測到 `0xC000013A` 時直接標示「疑似主控台事件」而不是 `no_tests_executed`，是它的範圍。
