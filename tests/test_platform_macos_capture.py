"""macOS 單來源 helper 的生命週期與帶時間戳 PCM transport。

Mock tests 不代表麥克風驗證；Swift/ffmpeg interoperability 使用合成 PCM。
"""
import io
import json
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
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
        self.assertEqual(popen.call_args_list[0].args[0][1], self.ROWS[0]["name"])
        expected = self.CMD[:5] + ["-f", "matroska", "-i", "pipe:0"] + self.CMD[11:]
        self.assertEqual(popen.call_args_list[1].args[0], expected)
        self.assertTrue(helper.stdout.closed)
        self.assertEqual(build.call_args.args[0][1:3], ["-module-cache-path", str(root / "modules")])
        self.assertEqual((root / "capture.swift").read_text(), P._MACOS_CAPTURE_SWIFT)
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
            log.assert_called_once_with(f"🎙 macOS 原生擷取來源：{self.ROWS[0]['name']}")

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
