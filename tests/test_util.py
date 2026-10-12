"""core/util.py：時間格式、Logger、OpenCC、原子寫檔、檔名收斂。

這個檔被 core 幾乎每個模組使用，但原本沒有任何直接測試（只被
`test_platform_subprocess_encoding.py` 與 `test_ui_session_store.py` 間接碰到）。

三個迴歸重點：
- `safe_name()` 對一般課名的結果**必須完全不變**；只收斂 Windows 不接受的名稱。
- `atomic_write()` 失敗時不可以把 `.tmp` 留在使用者的輸出資料夾（Obsidian 會看到）。
- `Logger.attach()` 重複呼叫不可以洩漏前一個 file handle。

OpenCC 的測試把 subprocess 換掉，所以不需要真的安裝 opencc。
"""
import errno
import json
import os
import random
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import util as U


class HmsTests(unittest.TestCase):
    def test_formats_hours_minutes_seconds(self):
        self.assertEqual(U.hms(0), "00:00:00")
        self.assertEqual(U.hms(61), "00:01:01")
        self.assertEqual(U.hms(3661), "01:01:01")
        self.assertEqual(U.hms(360000), "100:00:00")

    def test_negative_and_fractional_are_clamped_and_truncated(self):
        self.assertEqual(U.hms(-5), "00:00:00")
        self.assertEqual(U.hms(1.9), "00:00:01")

    def test_parse_hms_round_trips_whole_seconds(self):
        for seconds in (0, 61, 3661, 86399):
            with self.subTest(seconds=seconds):
                self.assertEqual(U.parse_hms(U.hms(seconds)), seconds)

    def test_parse_hms_keeps_fractional_seconds(self):
        self.assertAlmostEqual(U.parse_hms("00:00:01.500"), 1.5)
        self.assertAlmostEqual(U.parse_hms("  01:01:01.25  "), 3661.25)

    def test_parse_hms_rejects_malformed_input(self):
        for bad in ("", "abc", "1:2", "00:00:00:00"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    U.parse_hms(bad)


class SafeNameTests(unittest.TestCase):
    """課名 → 資料夾名稱。被 core/session.py 的 make_session_dir() 使用。"""

    def test_ordinary_course_names_are_untouched(self):
        # 這組是行為契約：任何修改都不可以改到正常課名的輸出資料夾名稱。
        for name in ("計概", "計算機概論", "Operating Systems", "資料結構 2026",
                     "a_b", ".hidden", "UNIX-101", "網路通訊概論"):
            with self.subTest(name=name):
                self.assertEqual(U.safe_name(name), name)

    def test_path_separators_and_control_characters_become_underscores(self):
        self.assertEqual(U.safe_name("a/b"), "a_b")
        self.assertEqual(U.safe_name("a\\b"), "a_b")
        self.assertEqual(U.safe_name('a:b*?"<>|'), "a_b______")
        self.assertEqual(U.safe_name("\x00\x01壞"), "__壞")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(U.safe_name("  空白  "), "空白")

    def test_empty_or_dot_only_names_fall_back(self):
        for name in ("", "   ", ".", "..", "...."):
            with self.subTest(name=name):
                self.assertEqual(U.safe_name(name), "course")

    def test_trailing_dots_and_spaces_are_removed(self):
        # Windows 會默默吃掉結尾的點與空白，建出來的資料夾名字就跟算出來的不一樣，
        # 連帶讓 make_session_dir() 的同名偵測失效。
        self.assertEqual(U.safe_name("課程."), "課程")
        self.assertEqual(U.safe_name("課程 . "), "課程")
        self.assertEqual(U.safe_name("課程..."), "課程")

    def test_windows_reserved_device_names_get_a_suffix(self):
        # 課名叫 CON 會讓 mkdir() 在 Windows 丟出 OSError。
        for name in ("CON", "NUL", "PRN", "AUX", "COM1", "LPT9"):
            with self.subTest(name=name):
                self.assertEqual(U.safe_name(name), name + "_")

    def test_reserved_names_are_matched_case_insensitively_and_with_extensions(self):
        self.assertEqual(U.safe_name("aux"), "aux_")
        self.assertEqual(U.safe_name("Con"), "Con_")
        self.assertEqual(U.safe_name("con.txt"), "con.txt_")

    def test_names_merely_containing_a_reserved_word_are_left_alone(self):
        for name in ("CONCEPT", "計概CON論", "console", "COM10"):
            with self.subTest(name=name):
                self.assertEqual(U.safe_name(name), name)

    def test_result_is_always_usable_as_a_single_path_component(self):
        for name in ("a/b", "CON", "課程.", "..", "", "x" * 5):
            with self.subTest(name=name):
                result = U.safe_name(name)
                self.assertTrue(result)
                self.assertEqual(Path(result).name, result)
                self.assertEqual(result, result.rstrip(". "))


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _entries(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_writes_the_file_and_leaves_no_temporary_behind(self):
        U.atomic_write(self.dir / "status.json", "hello")
        self.assertEqual(self._entries(), ["status.json"])
        self.assertEqual((self.dir / "status.json").read_text(encoding="utf-8"), "hello")

    def test_overwrites_an_existing_file(self):
        target = self.dir / "f.txt"
        target.write_text("old", encoding="utf-8")
        U.atomic_write(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_writes_utf8_without_a_locale_dependent_codec(self):
        target = self.dir / "f.txt"
        U.atomic_write(target, "繁體中文 ✔")
        self.assertEqual(target.read_bytes().decode("utf-8"), "繁體中文 ✔")

    def test_temporary_is_removed_when_the_rename_fails(self):
        # Windows 上若 UI 正開著 status.json，os.replace 會是 sharing violation；
        # 舊版會把 .status.json.tmp 留在使用者的 Obsidian 資料夾裡。
        with mock.patch("core.util.os.replace", side_effect=OSError("sharing violation")):
            with self.assertRaises(OSError):
                U.atomic_write(self.dir / "status.json", "x")
        self.assertEqual(self._entries(), [])

    def test_temporary_is_removed_when_the_write_fails(self):
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                U.atomic_write(self.dir / "status.json", "x")
        self.assertEqual(self._entries(), [])

    def test_cleanup_also_happens_on_keyboard_interrupt(self):
        with mock.patch("core.util.os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                U.atomic_write(self.dir / "status.json", "x")
        self.assertEqual(self._entries(), [])

    def test_temporary_name_is_hidden_and_derived_from_the_target(self):
        seen = []
        real = Path.write_text

        def spy(self, *a, **kw):
            seen.append(self.name)
            return real(self, *a, **kw)

        with mock.patch.object(Path, "write_text", spy):
            U.atomic_write(self.dir / "status.json", "x")
        self.assertEqual(seen, [".status.json.tmp"])


def winerror(code):
    """Windows 的 PermissionError：目標被別的行程開著時 winerror 是 5／32／33。"""
    e = PermissionError(errno.EACCES, "Access is denied")
    e.winerror = code           # Linux 的 OSError 沒有這個屬性，手動補上來模擬
    return e


class FlakyReplace:
    """os.replace 的替身：前幾次丟指定的例外，之後交給真的 os.replace（所以原子性是真的）。"""

    def __init__(self, failures=(), always=None):
        self.real = os.replace
        self.failures = list(failures)
        self.always = always
        self.calls = 0
        self.on_call = None

    def __call__(self, src, dst):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.always is not None:
            raise self.always()
        if self.failures:
            raise self.failures.pop(0)
        return self.real(src, dst)


class AtomicWriteRetryTests(unittest.TestCase):
    """Windows 上目標檔被占用時 os.replace 會暫時失敗（診斷的 G 類）。

    在 Linux 用「注入帶 winerror 的 PermissionError」模擬，並讓 _on_windows() 回傳 True；
    真的 os.replace 仍然執行，所以原子性、暫存檔清理都是實際行為，不是 mock。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.target = self.dir / "status.json"
        self.target.write_text("old", encoding="utf-8")
        self.sleeps = []
        for patcher in (mock.patch.object(U, "_on_windows", return_value=True),
                        mock.patch.object(U.time, "sleep", side_effect=self.sleeps.append)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _entries(self):
        return sorted(p.name for p in self.dir.iterdir())

    def _write(self, replace):
        with mock.patch.object(U.os, "replace", replace):
            U.atomic_write(self.target, "new")

    def test_transient_denials_are_retried_until_the_replace_succeeds(self):
        replace = FlakyReplace([winerror(5), winerror(32), winerror(33)])
        self._write(replace)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "new")
        self.assertEqual(replace.calls, 4)
        self.assertEqual(self.sleeps, list(U._REPLACE_BACKOFF[:3]), "退避時間依序遞增")
        self.assertEqual(self._entries(), ["status.json"], "成功後不留暫存檔")

    def test_the_old_content_is_untouched_until_the_replace_actually_succeeds(self):
        seen = []
        replace = FlakyReplace([winerror(5), winerror(5)])
        replace.on_call = lambda: seen.append(self.target.read_text(encoding="utf-8"))
        self._write(replace)
        self.assertEqual(seen, ["old", "old", "old"], "重試期間讀者看到的還是完整的舊檔")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "new")

    def test_gives_up_after_the_bounded_attempts_and_raises_the_original_error(self):
        replace = FlakyReplace(always=lambda: winerror(5))
        with mock.patch.object(U.os, "replace", replace):
            with self.assertRaises(PermissionError) as cm:
                U.atomic_write(self.target, "new")
        self.assertEqual(cm.exception.winerror, 5, "丟出去的是原本的錯誤，不是包裝過的")
        self.assertEqual(cm.exception.errno, errno.EACCES)
        self.assertEqual(replace.calls, len(U._REPLACE_BACKOFF) + 1, "次數有上限")
        self.assertEqual(self.sleeps, list(U._REPLACE_BACKOFF))
        self.assertEqual(self.target.read_text(encoding="utf-8"), "old", "失敗時目標檔不動")
        self.assertEqual(self._entries(), ["status.json"], "失敗也不留暫存檔")

    def test_the_total_wait_is_bounded(self):
        self.assertLessEqual(sum(U._REPLACE_BACKOFF), 1.0)
        self.assertLessEqual(len(U._REPLACE_BACKOFF), 10)
        self.assertTrue(all(d > 0 for d in U._REPLACE_BACKOFF))

    def test_a_permission_error_on_posix_is_a_real_error_and_is_not_retried(self):
        U._on_windows.return_value = False
        replace = FlakyReplace(always=lambda: winerror(5))
        with mock.patch.object(U.os, "replace", replace):
            with self.assertRaises(PermissionError):
                U.atomic_write(self.target, "new")
        self.assertEqual(replace.calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_other_windows_errors_are_not_retried(self):
        for name, factory in (("沒有 winerror 的 PermissionError",
                               lambda: PermissionError(errno.EACCES, "denied")),
                              ("需要特權（winerror 1314）", lambda: winerror(1314)),
                              ("磁碟滿", lambda: OSError(errno.ENOSPC, "No space left")),
                              ("找不到路徑", lambda: FileNotFoundError(errno.ENOENT, "missing")),
                              ("帶 winerror 5 的非 PermissionError", lambda: self._oserror_with_winerror())):
            with self.subTest(name):
                self.sleeps.clear()
                replace = FlakyReplace(always=factory)
                with mock.patch.object(U.os, "replace", replace):
                    with self.assertRaises(OSError):
                        U.atomic_write(self.target, "new")
                self.assertEqual(replace.calls, 1)
                self.assertEqual(self.sleeps, [])
                self.assertEqual(self._entries(), ["status.json"])

    @staticmethod
    def _oserror_with_winerror():
        e = OSError(errno.EIO, "io error")
        e.winerror = 5
        return e

    def test_an_interrupt_during_the_backoff_still_cleans_up(self):
        U.time.sleep.side_effect = KeyboardInterrupt
        replace = FlakyReplace(always=lambda: winerror(32))
        with mock.patch.object(U.os, "replace", replace):
            with self.assertRaises(KeyboardInterrupt):
                U.atomic_write(self.target, "new")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "old")
        self.assertEqual(self._entries(), ["status.json"])

    def test_readers_never_see_a_partial_file_while_writers_hit_transient_denials(self):
        # 重現診斷的 test_get_survives_atomic_write_churn：寫入端不斷 replace，讀取端不斷讀，
        # 其中三成的 replace 先遇到暫時性占用。寫入端不可以失敗，讀取端不可以讀到半個檔。
        rng = random.Random(7)
        real = os.replace
        calls = {"n": 0, "denied": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if rng.random() < 0.3:
                calls["denied"] += 1
                raise winerror(rng.choice([5, 32, 33]))
            return real(src, dst)

        self.target.write_text(json.dumps({"elapsed": -1}), encoding="utf-8")
        failures, bad_reads, done = [], [], threading.Event()

        def writer():
            try:
                for i in range(200):
                    U.atomic_write(self.target, json.dumps({"phase": "recording", "elapsed": i}))
            except BaseException as e:           # noqa: BLE001 - 要把任何失敗帶回主執行緒
                failures.append(e)
            finally:
                done.set()

        def reader():
            while not done.is_set():
                try:
                    json.loads(self.target.read_text(encoding="utf-8"))
                except ValueError as e:
                    bad_reads.append(e)

        with mock.patch.object(U.os, "replace", flaky):
            threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
        self.assertEqual(failures, [])
        self.assertEqual(bad_reads, [], "原子 replace：讀者只會看到舊檔或新檔")
        self.assertGreater(calls["denied"], 20, "要真的注入了足夠多的暫時性失敗，這個測試才有意義")
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["elapsed"], 199)
        self.assertEqual(self._entries(), ["status.json"])


class JsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_round_trip_keeps_non_ascii_readable(self):
        path = self.dir / "x.json"
        U.write_json(path, {"課程": "計概", "n": 1})
        self.assertEqual(U.read_json(path), {"課程": "計概", "n": 1})
        self.assertIn("計概", path.read_text(encoding="utf-8"))

    def test_written_json_ends_with_a_newline(self):
        path = self.dir / "x.json"
        U.write_json(path, {"a": 1})
        self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))

    def test_read_json_returns_the_default_for_missing_or_broken_files(self):
        self.assertEqual(U.read_json(self.dir / "nope.json", "D"), "D")
        bad = self.dir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(U.read_json(bad, "D"), "D")

    def test_read_json_default_is_none(self):
        self.assertIsNone(U.read_json(self.dir / "nope.json"))


class OpenccTests(unittest.TestCase):
    """OpenCC 是選用的；沒有安裝或失敗時必須原樣回傳，不可以丟例外。"""

    def test_available_follows_which(self):
        with mock.patch.object(U.shutil, "which", return_value="/usr/bin/opencc"):
            self.assertTrue(U.opencc_available())
        with mock.patch.object(U.shutil, "which", return_value=None):
            self.assertFalse(U.opencc_available())

    def _convert(self, texts, stdout):
        with mock.patch.object(U.shutil, "which", return_value="/usr/bin/opencc"), \
             mock.patch.object(U.subprocess, "run",
                               return_value=mock.Mock(stdout=stdout)):
            return U.opencc_convert(texts)

    def test_converts_line_by_line(self):
        self.assertEqual(self._convert(["软件", "数据库"], "軟體\n資料庫\n"),
                         ["軟體", "資料庫"])

    def test_rewrites_the_formal_tai_character_to_the_everyday_one(self):
        self.assertEqual(self._convert(["台湾"], "臺灣\n"), ["台灣"])

    def test_line_count_mismatch_falls_back_to_the_originals(self):
        # 寧可不轉換，也不要把句子錯位對上
        self.assertEqual(self._convert(["软件", "数据库"], "軟體\n"), ["软件", "数据库"])

    def test_empty_input_short_circuits_without_running_opencc(self):
        with mock.patch.object(U.subprocess, "run") as run:
            self.assertEqual(U.opencc_convert([]), [])
        run.assert_not_called()

    def test_missing_opencc_returns_the_input_unchanged(self):
        with mock.patch.object(U.shutil, "which", return_value=None), \
             mock.patch.object(U.subprocess, "run") as run:
            self.assertEqual(U.opencc_convert(["软件"]), ["软件"])
        run.assert_not_called()

    def test_subprocess_failures_return_the_input_unchanged(self):
        for boom in (OSError("no exec"),
                     subprocess.TimeoutExpired("opencc", 20),
                     subprocess.CalledProcessError(1, "opencc")):
            with self.subTest(boom=type(boom).__name__):
                with mock.patch.object(U.shutil, "which", return_value="/usr/bin/opencc"), \
                     mock.patch.object(U.subprocess, "run", side_effect=boom):
                    self.assertEqual(U.opencc_convert(["软件"]), ["软件"])


class LoggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_writes_to_the_log_file_with_a_timestamp(self):
        log = U.Logger()
        log.attach(self.dir / "session.log")
        self.addCleanup(log.close)
        log("測試訊息")
        text = (self.dir / "session.log").read_text(encoding="utf-8")
        self.assertIn("測試訊息", text)
        self.assertRegex(text, r"^\[\d{2}:\d{2}:\d{2}\] ")

    def test_works_before_attach_without_raising(self):
        U.Logger()("還沒 attach")        # 只印到終端機

    def test_reattaching_closes_the_previous_handle(self):
        log = U.Logger()
        log.attach(self.dir / "a.log")
        first = log._fh
        log.attach(self.dir / "b.log")
        self.addCleanup(log.close)
        self.assertTrue(first.closed, "重複 attach 不可以洩漏前一個 file handle")
        log("只進 b")
        self.assertEqual((self.dir / "a.log").read_text(encoding="utf-8"), "")
        self.assertIn("只進 b", (self.dir / "b.log").read_text(encoding="utf-8"))

    def test_close_is_idempotent_and_stops_writing(self):
        log = U.Logger()
        log.attach(self.dir / "a.log")
        log("第一行")
        log.close()
        log.close()
        log("關掉之後")
        self.assertNotIn("關掉之後", (self.dir / "a.log").read_text(encoding="utf-8"))

    def test_concurrent_writers_do_not_interleave_within_a_line(self):
        log = U.Logger()
        log.attach(self.dir / "a.log")
        self.addCleanup(log.close)

        def worker(n):
            for i in range(40):
                log(f"thread{n}-{i}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = [l for l in (self.dir / "a.log").read_text(encoding="utf-8").splitlines() if l]
        self.assertEqual(len(lines), 160)
        for line in lines:
            self.assertRegex(line, r"^\[\d{2}:\d{2}:\d{2}\] thread\d-\d+$")


class MiscTests(unittest.TestCase):
    def test_pid_alive_delegates_to_the_platform_layer(self):
        with mock.patch("core.platform.pid_alive", return_value=True) as alive:
            self.assertTrue(U.pid_alive(1234))
        alive.assert_called_once_with(1234)

    def test_die_prints_to_stderr_and_exits_with_the_given_code(self):
        with mock.patch("sys.stderr") as err:
            with self.assertRaises(SystemExit) as caught:
                U.die("壞掉了", 3)
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("壞掉了", "".join(c.args[0] for c in err.write.call_args_list))

    def test_die_defaults_to_exit_code_one(self):
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                U.die("壞掉了")
        self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
