"""macOS 單來源 helper 的生命週期與帶時間戳 PCM transport。

Mock tests 不代表麥克風驗證；Swift/ffmpeg interoperability 使用合成 PCM。
"""
import contextlib
import concurrent.futures
import io
import os
import json
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from . import _pathfix  # noqa: F401
from core import platform as P
from core import transcribe as T


class CaptureProcessTests(unittest.TestCase):
    def make_process(self, helper_code=0, ffmpeg_code=0):
        ffmpeg = mock.Mock(stdout=io.BytesIO(), pid=123)
        ffmpeg.wait.return_value = ffmpeg_code
        helper = mock.Mock()
        helper.poll.return_value = helper_code
        helper.wait.return_value = helper_code
        return P.MacOSCaptureProcess(ffmpeg, helper, diagnostics=io.BytesIO(b"native error"))

    def test_graceful_stop_signals_source_and_allows_ffmpeg_to_finish_on_eof(self):
        proc = self.make_process()
        proc.helper.poll.side_effect = [None, 0]
        proc.send_signal(signal.SIGINT)
        proc.helper.send_signal.assert_called_once_with(signal.SIGINT)
        proc.proc.send_signal.assert_not_called()
        self.assertEqual(proc.wait(), 0)
        self.assertIsNone(P.native_capture_problem(proc))
        self.assertEqual(proc.wait(), 0)  # cleanup/wait 可重複

    def test_helper_failure_does_not_replace_successful_ffmpeg_exit_code(self):
        proc = self.make_process(helper_code=1)
        self.assertEqual(proc.wait(), 0)
        self.assertIn("helper 結束代碼 1", P.native_capture_problem(proc))
        self.assertIn("native error", P.native_capture_problem(proc))

    def test_stop_before_native_signal_handlers_are_ready_is_not_a_capture_error(self):
        proc = self.make_process(helper_code=-signal.SIGINT, ffmpeg_code=1)
        proc.helper.poll.side_effect = [None, -signal.SIGINT]
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(), 1)
        self.assertIsNone(P.native_capture_problem(proc))

    def test_unresponsive_helper_is_killed_and_reaped_after_ffmpeg_exits(self):
        proc = self.make_process(helper_code=-9)
        proc.helper.poll.return_value = None
        proc.helper.wait.side_effect = [subprocess.TimeoutExpired("capture", 5), -9]
        self.assertEqual(proc.wait(), 0)
        proc.helper.terminate.assert_called_once()
        proc.helper.kill.assert_called_once()
        self.assertIn("-9", P.native_capture_problem(proc))

    def test_wait_timeout_keeps_resources_for_a_later_wait(self):
        proc = self.make_process()
        proc.proc.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 1), 0]
        with self.assertRaises(subprocess.TimeoutExpired):
            proc.wait(timeout=1)
        self.assertFalse(proc.diagnostics.closed)
        self.assertEqual(proc.wait(), 0)
        self.assertTrue(proc.diagnostics.closed)

    def test_success_report_keeps_the_last_native_json_line_after_wait(self):
        report = {"audio_seconds": 99.0, "pts_span_seconds": 100.0, "callbacks": 100,
                  "gap_count": 1, "lost_seconds": 1.0, "wall_seconds": 101.0, "error": ""}
        proc = self.make_process()
        proc.diagnostics = io.BytesIO(("unrelated stderr\nnative capture: {}\n"
                                      + "native capture: " + json.dumps(report) + "\n").encode())
        self.assertEqual(proc.wait(), 0)
        self.assertEqual(P.native_capture_report(proc), report)
        self.assertIsNone(P.native_capture_problem(proc))
        self.assertTrue(proc.diagnostics.closed)
        self.assertEqual(proc.wait(), 0)
        self.assertEqual(proc.capture_report, report)

    def test_absent_truncated_or_invalid_report_does_not_change_wait_result(self):
        for diagnostics in (b"", b"native capture: {", b"native capture: []",
                            b'native capture: {"gap_count": "oops"}', b"native capture: null"):
            with self.subTest(diagnostics=diagnostics):
                proc = self.make_process()
                proc.diagnostics = io.BytesIO(diagnostics)
                self.assertEqual(proc.wait(), 0)
                self.assertIsNone(proc.capture_report)
                self.assertIsNone(proc.capture_problem)
        proc = self.make_process(helper_code=-9)
        proc.diagnostics = None
        self.assertEqual(proc.wait(), 0)
        self.assertIsNone(proc.capture_report)
        self.assertIn("沒有診斷資訊", proc.capture_problem)

    def test_non_native_process_has_no_native_error(self):
        self.assertIsNone(P.native_capture_problem(mock.Mock()))
        self.assertIsNone(P.native_capture_report(mock.Mock()))


class StartCaptureTests(unittest.TestCase):
    CMD = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
           "-thread_queue_size", "4096", "-i", ":1", "-ac", "1", "-ar", "16000",
           "-f", "s16le", "pipe:1", "-c:a", "libopus", "rec.ogg"]
    ROWS = [{"id": "1", "name": "內建 麥克風 (USB)", "index": 1}]

    def setUp(self):
        # 此組專注原有來源／停止契約；持久快取由 HelperCacheTests 另測。
        class TemporaryOnly:
            path = None
            def __init__(self, compiler):
                self.compiler = compiler
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                pass
            def binary(self, work):
                return P._compile_macos_capture(self.compiler, work)
        patch = mock.patch.object(P, "_MacOSHelperCache", TemporaryOnly)
        patch.start()
        self.addCleanup(patch.stop)

    def test_linux_and_dshow_keep_the_original_command_and_spawn_options(self):
        for platform, backend in (("linux", "pulse"), ("windows", "dshow"), ("macos", "pulse")):
            with self.subTest(platform=platform, backend=backend), \
                 mock.patch.object(P, "NAME", platform), \
                 mock.patch.object(P, "spawn_kwargs", return_value={"creationflags": 7}), \
                 mock.patch.object(P.subprocess, "Popen") as popen, \
                 mock.patch.object(P.shutil, "which") as which:
                result = P.start_live_capture(self.CMD, "1", backend)
                self.assertIs(result, popen.return_value)
                popen.assert_called_once_with(self.CMD, stdout=subprocess.PIPE, creationflags=7)
                which.assert_not_called()

    def test_missing_compiler_is_an_explicit_startup_failure_without_ffmpeg_fallback(self):
        with mock.patch.object(P, "NAME", "macos"), \
             mock.patch.object(P.shutil, "which", return_value=None), \
             mock.patch.object(P.subprocess, "Popen") as popen:
            result = P.start_live_capture(self.CMD, "1", "avfoundation")
        self.assertEqual(result.stdout.read(), b"")
        self.assertIsNone(result.wait())
        self.assertIn("swiftc", P.native_capture_problem(result))
        popen.assert_not_called()

    def test_only_input_is_replaced_and_pipe_and_cache_are_closed_after_wait(self):
        helper = mock.Mock(stdout=io.BytesIO())
        helper.poll.return_value = helper.wait.return_value = 0
        ffmpeg = mock.Mock(stdout=io.BytesIO(), pid=2)
        ffmpeg.wait.return_value = 0
        with mock.patch.object(P, "NAME", "macos"), \
             mock.patch.object(P.shutil, "which", return_value="/usr/bin/swiftc"), \
             mock.patch.object(P, "_avfoundation_sources", return_value=self.ROWS), \
             mock.patch.object(P.subprocess, "run", return_value=mock.Mock(returncode=0)) as build, \
             mock.patch.object(P.subprocess, "Popen", side_effect=[helper, ffmpeg]) as popen:
            result = P.start_live_capture(self.CMD, "1", "avfoundation")
        root = Path(result.cache.name)
        self.addCleanup(result.cache.cleanup)     # 中途斷言失敗也不留下暫存目錄
        self.assertEqual(popen.call_args_list[0].args[0][1], self.ROWS[0]["name"])
        expected = self.CMD[:5] + ["-f", "matroska", "-i", "pipe:0"] + self.CMD[11:]
        self.assertEqual(popen.call_args_list[1].args[0], expected)
        self.assertTrue(helper.stdout.closed)
        self.assertEqual(build.call_args.args[0][1:3], ["-module-cache-path", str(root / "modules")])
        self.assertEqual((root / "capture.swift").read_text(encoding="utf-8"), P._MACOS_CAPTURE_SWIFT)
        self.assertEqual(result.wait(), 0)
        self.assertFalse(root.exists())

    def test_unknown_source_fails_without_compiling_or_starting_helper(self):
        for source in ("不存在", "99", 99):
            with self.subTest(source=source), \
                 mock.patch.object(P, "NAME", "macos"), \
                 mock.patch.object(P.shutil, "which", return_value="swiftc"), \
                 mock.patch.object(P, "_avfoundation_sources", return_value=self.ROWS), \
                 mock.patch.object(P.subprocess, "run") as build, \
                 mock.patch.object(P.subprocess, "Popen") as popen:
                result = P.start_live_capture(self.CMD, source, "avfoundation")
            self.assertIn(self.ROWS[0]["name"], P.native_capture_problem(result))
            self.assertIn("可用裝置", P.native_capture_problem(result))
            self.assertIsNone(result.wait())
            build.assert_not_called()
            popen.assert_not_called()

    def test_default_skips_virtual_devices_and_logs_actual_source(self):
        rows = [{"id": "bgm", "name": "Background Music", "index": 0}, *self.ROWS]
        for source in (None, "", "default"):
            helper = mock.Mock(stdout=io.BytesIO())
            helper.poll.return_value = helper.wait.return_value = 0
            ffmpeg = mock.Mock(stdout=io.BytesIO())
            ffmpeg.wait.return_value = 0
            with self.subTest(source=source), \
                 mock.patch.object(P, "NAME", "macos"), \
                 mock.patch.object(P.shutil, "which", return_value="swiftc"), \
                 mock.patch.object(P, "_avfoundation_sources", return_value=rows), \
                 mock.patch.object(P.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                 mock.patch.object(P.subprocess, "Popen", side_effect=[helper, ffmpeg]) as popen, \
                 mock.patch("core.util.log") as log:
                result = P.start_live_capture(self.CMD, source, "avfoundation")
                result.wait()
            self.assertEqual(popen.call_args_list[0].args[0][1], self.ROWS[0]["name"])
            log.assert_any_call(f"🎙 macOS 原生擷取來源：{self.ROWS[0]['name']}")

    def test_default_source_filters_transport_and_known_names_only_on_avfoundation(self):
        rows = [{"id": "virtual", "name": "Loopback", "transport": "Virtual"},
                {"id": "bgm", "name": "Background Music (UI Sounds)"},
                {"id": "mic", "name": "Built-in microphone", "transport": "Built-in"}]
        with mock.patch.object(P, "list_sources", return_value=rows):
            self.assertEqual(P.default_source("avfoundation"), "mic")
            self.assertEqual(P.resolve_source("default", "avfoundation"), "mic")
            self.assertEqual(P.default_source("dshow"), "virtual")
        with mock.patch.object(P, "list_sources", return_value=rows[:2]):
            self.assertIsNone(P.default_source("avfoundation"))
        self.assertIsNone(P._macos_default_source([]))

    def test_explicit_virtual_device_and_numeric_index_remain_selectable(self):
        rows = [{"id": "Background Music", "name": "Background Music", "index": 0}]
        for source in ("Background Music", "0", 0):
            helper = mock.Mock(stdout=io.BytesIO())
            helper.poll.return_value = helper.wait.return_value = 0
            ffmpeg = mock.Mock(stdout=io.BytesIO())
            ffmpeg.wait.return_value = 0
            with self.subTest(source=source), \
                 mock.patch.object(P, "NAME", "macos"), \
                 mock.patch.object(P.shutil, "which", return_value="swiftc"), \
                 mock.patch.object(P, "_avfoundation_sources", return_value=rows), \
                 mock.patch.object(P.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                 mock.patch.object(P.subprocess, "Popen", side_effect=[helper, ffmpeg]) as popen:
                result = P.start_live_capture(self.CMD, source, "avfoundation")
                result.wait()
            self.assertEqual(popen.call_args_list[0].args[0][1], "Background Music")

    def test_compile_failure_is_reported_and_does_not_start_a_microphone(self):
        with mock.patch.object(P, "NAME", "macos"), \
             mock.patch.object(P.shutil, "which", return_value="swiftc"), \
             mock.patch.object(P, "_avfoundation_sources", return_value=self.ROWS), \
             mock.patch.object(P.subprocess, "run", return_value=mock.Mock(returncode=1, stderr=b"SDK missing")), \
             mock.patch.object(P.subprocess, "Popen") as popen:
            result = P.start_live_capture(self.CMD, "1", "avfoundation")
        self.assertIn("SDK missing", P.native_capture_problem(result))
        self.assertIsNone(result.wait())
        popen.assert_not_called()

    def test_ffmpeg_spawn_failure_reaps_helper_and_reports_startup_problem(self):
        helper = mock.Mock(stdout=io.BytesIO())
        helper.poll.return_value = None
        with mock.patch.object(P, "NAME", "macos"), \
             mock.patch.object(P.shutil, "which", return_value="swiftc"), \
             mock.patch.object(P, "_avfoundation_sources", return_value=self.ROWS), \
             mock.patch.object(P.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(P.subprocess, "Popen", side_effect=[helper, OSError("ffmpeg missing")]):
            result = P.start_live_capture(self.CMD, "1", "avfoundation")
        helper.kill.assert_called_once()
        helper.wait.assert_called_once()
        self.assertTrue(helper.stdout.closed)
        self.assertIn("ffmpeg missing", P.native_capture_problem(result))


@unittest.skipUnless(os.name == "posix", "cache locking uses POSIX; Linux/macOS mock coverage")
class HelperCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / "state"
        self.cache = self.state / "native-capture"
        self.version, self.builds, self.exec_failures = b"Swift test version 1", 0, 0
        self.executed, self.logs = [], []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for patch in (mock.patch.object(P, "NAME", "macos"),
                      mock.patch.object(P, "default_state_dir", return_value=self.state),
                      mock.patch.object(P.shutil, "which", return_value="swiftc"),
                      mock.patch.object(P, "_avfoundation_sources", return_value=StartCaptureTests.ROWS),
                      mock.patch.object(P.subprocess, "run", side_effect=self.compile),
                      mock.patch.object(P.subprocess, "Popen", side_effect=self.spawn),
                      mock.patch("core.util.log", side_effect=self.logs.append)):
            self.stack.enter_context(patch)

    def compile(self, cmd, **kwargs):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, self.version, b"")
        self.builds += 1
        binary = Path(cmd[-1])
        binary.write_bytes(b"compiled helper " + str(self.builds).encode())
        binary.chmod(0o700)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    def spawn(self, cmd, **kwargs):
        if cmd[0] != "ffmpeg":
            self.executed.append(Path(cmd[0]))
            if self.exec_failures:
                self.exec_failures -= 1
                raise OSError("invalid executable")
        child = mock.Mock(stdout=io.BytesIO(), pid=123)
        child.poll.return_value = child.wait.return_value = 0
        return child

    def start(self, expected_problem=None):
        proc = P.start_live_capture(StartCaptureTests.CMD, "1", "avfoundation")
        proc.wait()
        if expected_problem:
            self.assertIn(expected_problem, P.native_capture_problem(proc))
        else:
            self.assertIsNone(P.native_capture_problem(proc))
        return proc

    def cached(self):
        files = list(self.cache.glob("*.bin"))
        self.assertEqual(len(files), 1)
        return files[0]

    def test_second_start_reuses_binary_and_wait_does_not_remove_it(self):
        first = self.start()
        cached = self.cached()
        self.assertFalse(Path(first.cache.name).exists())
        self.start()
        self.assertEqual(self.builds, 1)
        self.assertEqual(self.executed, [cached, cached])
        self.assertTrue(cached.exists())
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o700)
        self.assertTrue(any("使用快取" in line for line in self.logs))
        self.assertEqual(sum("原生擷取來源" in line for line in self.logs), 2)

    def test_source_version_and_architecture_each_invalidate_cache(self):
        self.start()
        with mock.patch.object(P, "_MACOS_CAPTURE_SWIFT", P._MACOS_CAPTURE_SWIFT + "\n"):
            self.start()
        self.version = b"Swift test version 2"
        self.start()
        with mock.patch.object(P._pf, "machine", return_value="another-arch"):
            self.start()
        self.assertEqual(self.builds, 4)
        self.assertEqual(len(list(self.cache.glob("*.bin"))), 3)

    def test_unwritable_cache_falls_back_to_per_recording_temporary_directory(self):
        real_open = P.os.open
        def denied(path, *args, **kwargs):
            if str(path).endswith(".lock"):
                raise PermissionError("cache read-only")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(P.os, "open", side_effect=denied):
            self.start()
            self.start()
        self.assertEqual(self.builds, 2)
        self.assertFalse(list(self.cache.glob("*.bin")))
        self.assertTrue(all(not path.exists() for path in self.executed))
        self.assertTrue(any("cache read-only" in line for line in self.logs))

    def test_publish_permission_failure_still_uses_the_compiled_temporary_binary(self):
        with mock.patch.object(P.os, "replace", side_effect=PermissionError("write denied")):
            self.start()
        self.assertEqual(self.builds, 1)
        self.assertFalse(list(self.cache.glob(".publish-*")))
        self.assertFalse(self.executed[0].exists())  # temp 已依原語意清理

    def test_corrupt_or_nonexecutable_cache_is_removed_and_rebuilt_once(self):
        self.start()
        path = self.cached()
        for change in (lambda: path.write_bytes(b"damaged"), lambda: path.chmod(0o600),
                       lambda: path.with_suffix(".sha256").write_bytes(b"\xff")):
            previous = self.builds
            change()
            self.start()
            self.assertEqual(self.builds, previous + 1)
            self.start()
            self.assertEqual(self.builds, previous + 1)

    def test_exec_oserror_rebuilds_once_and_a_second_failure_is_reported(self):
        self.start()
        self.exec_failures = 1
        self.start()
        self.assertEqual(self.builds, 2)
        before = len(self.executed)
        self.exec_failures = 2
        self.start(expected_problem="invalid executable")
        self.assertEqual(self.builds, 3)
        self.assertEqual(len(self.executed) - before, 2)

    def test_unsafe_directory_or_symlink_is_not_used_or_modified(self):
        self.cache.mkdir(mode=0o700, parents=True)
        self.cache.chmod(0o770)
        self.start()
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o770)
        self.assertFalse(list(self.cache.glob("*.bin")))
        self.cache.rmdir()
        target = self.root / "elsewhere"
        target.mkdir(mode=0o700)
        self.cache.symlink_to(target, target_is_directory=True)
        self.start()
        self.assertTrue(self.cache.is_symlink())
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.builds, 2)

    def test_unsafe_binary_permission_owner_or_symlink_uses_temporary_build(self):
        self.start()
        cached = self.cached()
        cached.chmod(0o722)
        self.start()
        self.assertEqual(cached.stat().st_mode & 0o777, 0o722)
        cached.chmod(0o700)
        real_lstat = P.Path.lstat
        def different_owner(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path == cached:
                data = list(info); data[4] = os.getuid() + 1
                return os.stat_result(data)
            return info
        with mock.patch.object(P.Path, "lstat", different_owner):
            self.start()
        target = self.root / "external"
        target.write_bytes(b"do not change")
        cached.unlink(); cached.symlink_to(target)
        self.start()
        self.assertEqual(target.read_bytes(), b"do not change")
        self.assertTrue(cached.is_symlink())
        self.assertEqual(self.builds, 4)

    def test_foreign_directory_owner_is_rejected(self):
        self.cache.mkdir(mode=0o700, parents=True)
        real_lstat = P.Path.lstat
        def different_owner(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path == self.cache:
                data = list(info); data[4] = os.getuid() + 1
                return os.stat_result(data)
            return info
        with mock.patch.object(P.Path, "lstat", different_owner):
            self.start()
        self.assertFalse(list(self.cache.glob("*.bin")))
        self.assertEqual(self.builds, 1)

    def test_writable_parent_and_symlink_lock_are_rejected(self):
        self.state.mkdir(mode=0o777)
        self.state.chmod(0o777)
        self.start()
        self.assertFalse(self.cache.exists())
        self.state.chmod(0o700)
        self.cache.mkdir(mode=0o700)
        target = self.root / "lock-target"
        target.write_bytes(b"untouched")
        (self.cache / ".lock").symlink_to(target)
        self.start()
        self.assertEqual(target.read_bytes(), b"untouched")
        self.assertFalse(list(self.cache.glob("*.bin")))
        self.assertEqual(self.builds, 2)

    def test_unsafe_digest_is_not_trusted_or_overwritten(self):
        self.start()
        path = self.cached()
        sidecar = path.with_suffix(".sha256")
        sidecar.chmod(0o666)
        original = sidecar.read_bytes()
        self.start()
        self.assertEqual(self.builds, 2)
        self.assertEqual(sidecar.read_bytes(), original)
        self.assertNotEqual(self.executed[-1], path)

    def test_corruption_followed_by_compile_failure_reports_once(self):
        self.start()
        path = self.cached()
        path.write_bytes(b"corrupt")
        def failed(cmd, **kwargs):
            if "--version" in cmd:
                return subprocess.CompletedProcess(cmd, 0, self.version, b"")
            self.builds += 1
            return subprocess.CompletedProcess(cmd, 1, b"", b"compiler unavailable")
        with mock.patch.object(P.subprocess, "run", side_effect=failed):
            self.start(expected_problem="compiler unavailable")
        self.assertEqual(self.builds, 2)
        self.assertEqual(len(self.executed), 1)
        self.assertFalse(path.exists())

    def test_concurrent_starts_publish_complete_binary_once(self):
        barrier = threading.Barrier(2)
        def start():
            barrier.wait(timeout=5)
            proc = P.start_live_capture(StartCaptureTests.CMD, "1", "avfoundation")
            proc.wait()
            return P.native_capture_problem(proc)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual(results, [None, None])
        self.assertEqual(self.builds, 1)
        cached = self.cached()
        self.assertEqual(cached.read_bytes(), b"compiled helper 1")
        self.assertEqual(cached.with_suffix(".sha256").read_text().strip(), P._MacOSHelperCache._digest(cached))
        self.assertFalse(list(self.cache.glob(".publish-*")))

    def test_pruning_retains_recently_used_entries(self):
        original = self.version
        self.start(); recent = self.cached()
        self.version = b"v2"; self.start()
        self.version = b"v3"; self.start()
        self.version = original; self.start()  # refresh 使用時間
        self.version = b"v4"; self.start()
        self.assertTrue(recent.exists())
        self.assertEqual(self.builds, 4)
        self.assertEqual(len(list(self.cache.glob("*.bin"))), 3)
        self.assertEqual(len(list(self.cache.glob("*.sha256"))), 3)


@unittest.skipUnless(P.NAME == "macos" and shutil.which("swiftc") and shutil.which("ffmpeg")
                     and shutil.which("ffprobe"), "synthetic transport test needs macOS Swift and ffmpeg")
class SwiftTransportTests(unittest.TestCase):
    def test_planar_pcm_backlog_limit_and_capture_gap_survive_opus_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = P._MACOS_CAPTURE_SWIFT.split("let target = CommandLine.arguments[1]")[0]
            source += '''
if CommandLine.arguments.count > 1 {
    let blocked = Receiver()
    blocked.enqueue(Data(count: 32*1024*1024))
    blocked.enqueue(Data([1]))
    precondition(blocked.error() != nil)
    blocked.finishWriting()
    exit(1)
}
precondition(interleave(Data([1,2,3,4,10,11,12,13]), frames: 2, channels: 2, width: 2)
             == Data([1,2,10,11,3,4,12,13]))
let receiver = Receiver()
receiver.enqueue(Data(count: 32*1024*1024+1))
precondition(receiver.error() == "PCM writer backlog exceeds 32 MiB")
FileHandle.standardOutput.write(header(rate: 48000, channels: 1, bits: 32, codec: "A_PCM/FLOAT/IEEE"))
for i in 0..<200 {
    let tick = UInt64((Double(i)*512/48000 + (i >= 100 ? 0.128 : 0))*1000000)
    FileHandle.standardOutput.write(element(0x1F43B675, uint(0xE7, tick)
        + element(0xA3, Data([0x81,0,0,0x80]) + Data(count: 512*4))))
}
'''
            swift, binary, mkv, ogg = (root / n for n in ("test.swift", "test", "test.mkv", "test.ogg"))
            swift.write_text(source, encoding="utf-8")
            subprocess.run(["swiftc", "-module-cache-path", str(root / "modules"), str(swift),
                            "-o", str(binary)], capture_output=True, check=True, timeout=120)
            # 消費端不讀 pipe，32 MiB writer 必定塞住；錯誤收尾仍須在期限內退出。
            with subprocess.Popen([str(binary), "blocked"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) as blocked:
                try:
                    self.assertEqual(blocked.wait(timeout=5), 1)
                finally:
                    if blocked.poll() is None:
                        blocked.kill()
                        blocked.wait()
            with mkv.open("wb") as output:
                subprocess.run([str(binary)], stdout=output, stderr=subprocess.PIPE, check=True, timeout=10)
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(mkv),
                            "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k", str(ogg)],
                           capture_output=True, check=True, timeout=10)
            report = T.probe_capture(ogg)
            self.assertEqual(report["gap_count"], 1)
            self.assertAlmostEqual(report["lost_seconds"], 0.128, places=3)
            self.assertGreater(report["lost_ratio"], T.CAPTURE_LOSS_ERROR)
