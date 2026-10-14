"""macOS 單來源 helper 的生命週期與帶時間戳 PCM transport。

Mock tests 不代表麥克風驗證；Swift/ffmpeg interoperability 使用合成 PCM。
"""
import io
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

    def test_non_native_process_has_no_native_error(self):
        self.assertIsNone(P.native_capture_problem(mock.Mock()))


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
