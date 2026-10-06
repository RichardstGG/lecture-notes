"""Transcriber 的雙來源模式（capture_sources）與單來源回歸。

擷取端是假的（tests/_capture_fakes.py），編碼是真的 ffmpeg；whisper 以 mock 取代。
驗證層級：Mock test＋Automated。需要 ffmpeg（含 libopus），沒有就跳過。
"""
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from . import _capture_fakes as F
from core import capture as C
from core import config as CFG
from core import transcribe as T

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
SPECS = [C.SourceSpec("system", "out.monitor", "輸出", "monitor"),
         C.SourceSpec("mic", "mic0", "麥克風", "input")]
SRT = b"1\n00:00:00,000 --> 00:00:02,000\n\xe4\xbd\xa0\xe5\xa5\xbd\n\n"


class FakeStatus:
    def __init__(self):
        self.errors, self.events, self.updates = [], [], []
        self._lock = threading.Lock()

    def error(self, msg):
        with self._lock:
            self.errors.append(msg)

    def event(self, kind, **kv):
        with self._lock:
            self.events.append((kind, kv))

    def update(self, **kv):
        with self._lock:
            self.updates.append(kv)


def make_cfg(keep_recording=True):
    cfg, _ = CFG.load(None)
    cfg["vad"].update(min_chunk=1, max_chunk=3, silence_ms=200)
    cfg["audio"]["keep_recording"] = keep_recording
    cfg["audio"]["backend"] = "pulse"
    return cfg


@unittest.skipUnless(HAVE_FFMPEG, "需要 ffmpeg 與 ffprobe")
class DualTranscriberTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.status = FakeStatus()
        patcher = mock.patch.object(T, "transcribe_request", return_value=SRT.decode())
        patcher.start()
        self.addCleanup(patcher.stop)

    def transcriber(self, devices, keep_recording=True, **opts):
        self.popen = F.FakePopen(devices)
        t = T.Transcriber(make_cfg(keep_recording), self.tmp, "http://x", status=self.status,
                          capture_sources=SPECS)
        t.capture_options = dict(popen=self.popen, binding=None, say=lambda m: None,
                                 start_window=0.5, start_timeout=3, stall_timeout=1.0, **opts)
        return t

    def run_for(self, t, seconds):
        out = {}
        th = threading.Thread(target=lambda: out.update(t.run()))
        th.start()
        time.sleep(seconds)
        t.request_stop()
        th.join(40)
        self.assertFalse(th.is_alive(), "Transcriber.run 沒有結束")
        return out

    def test_two_tracks_and_a_mix_feed_the_normal_pipeline(self):
        t = self.transcriber({"out.monitor": F.FakeCapture(freq=440),
                              "mic0": F.FakeCapture(freq=880)})
        result = self.run_for(t, 4)
        self.assertFalse(result["ffmpeg_failed"])
        self.assertFalse(result["degraded"])
        self.assertEqual(result["capture_session"]["status"], "complete")
        self.assertGreater(result["duration"], 3.0)
        self.assertTrue((self.tmp / "tracks" / "system.ogg").is_file())
        self.assertTrue((self.tmp / "tracks" / "mic.ogg").is_file())
        recs = list(self.tmp.glob("recording_*.ogg"))
        self.assertEqual(len(recs), 1, "混音存成既有的 recording_*.ogg，下游不用改")
        self.assertEqual(t.recording_path, recs[0])
        self.assertIsNotNone(result["capture"], "混音檔照舊做完整性檢查")
        self.assertTrue((self.tmp / "transcript.md").read_text(encoding="utf-8").strip())
        kinds = [k for k, _ in self.status.events]
        self.assertIn("capture_started", kinds)
        self.assertIn("capture_integrity", kinds)
        self.assertEqual(self.status.errors, [])

    def test_one_source_failing_is_degraded_and_reported_as_an_error(self):
        t = self.transcriber({"out.monitor": F.FakeCapture(freq=440),
                              "mic0": F.FakeCapture(freq=880, die_after=1.5,
                                                    stderr_text="device unplugged")})
        result = self.run_for(t, 4)
        self.assertFalse(result["ffmpeg_failed"], "還有一路在錄，不算整個失敗")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["capture_session"]["faults"][0]["role"], "mic")
        self.assertTrue(any("mic" in e and "device unplugged" in e for e in self.status.errors))
        self.assertIn("capture_source_failed", [k for k, _ in self.status.events])
        self.assertGreater(result["duration"], 3.0, "另一路的錄音與轉錄要繼續")

    def test_failing_to_open_one_source_means_nothing_is_recorded(self):
        t = self.transcriber({"out.monitor": F.FakeCapture(freq=440),
                              "mic0": F.FakeCapture(die_after=0, stderr_text="No such entity")})
        result = self.run_for(t, 1)
        self.assertTrue(result["ffmpeg_failed"])
        self.assertEqual(result["duration"], 0)
        self.assertEqual(result["capture_session"]["start_error"]["code"], "start_failed")
        self.assertEqual(result["capture_session"]["start_error"]["role"], "mic")
        self.assertTrue(any("No such entity" in e for e in self.status.errors))
        self.assertFalse((self.tmp / "tracks").exists())
        self.assertEqual(list(self.tmp.glob("recording_*.ogg")), [])
        self.assertIsNone(t.recording_path)

    def test_keep_recording_off_still_keeps_the_tracks(self):
        t = self.transcriber({"out.monitor": F.FakeCapture(freq=440),
                              "mic0": F.FakeCapture(freq=880)}, keep_recording=False)
        result = self.run_for(t, 2.5)
        self.assertEqual(list(self.tmp.glob("recording_*.ogg")), [])
        self.assertTrue((self.tmp / "tracks" / "mic.ogg").is_file())
        self.assertIsNone(result["capture"])

    def test_stop_goes_through_capture_not_an_os_signal_on_a_single_proc(self):
        t = self.transcriber({"out.monitor": F.FakeCapture(freq=440),
                              "mic0": F.FakeCapture(freq=880)})
        self.run_for(t, 2)
        self.assertIsNone(t.proc, "雙來源模式沒有單一 ffmpeg 行程")
        self.assertTrue(t.stopping)
        for cap in self.popen.captures.values():
            self.assertIsNotNone(cap.returncode)

    def test_unfinalised_artifacts_are_reported_as_errors(self):
        t = T.Transcriber.__new__(T.Transcriber)
        t.status = self.status
        T.Transcriber._report_capture_artifacts(t, {
            "sources": [{"track": "tracks/mic.ogg", "complete": True},
                        {"track": "tracks/system.ogg", "complete": False, "problem": "編碼行程中斷"}],
            "mix": {"path": "recording_1.ogg", "complete": False, "problem": "ffprobe 讀不出音長"}})
        self.assertEqual(len(self.status.errors), 2)
        self.assertIn("tracks/system.ogg", self.status.errors[0])
        self.assertIn("編碼行程中斷", self.status.errors[0])
        self.assertIn("recording_1.ogg", self.status.errors[1])

    def test_dual_capture_is_live_only(self):
        with self.assertRaises(ValueError):
            T.Transcriber(make_cfg(), self.tmp, "http://x", input_file="a.ogg",
                          capture_sources=SPECS)


class SingleSourceRegressionTests(unittest.TestCase):
    def test_without_capture_sources_nothing_changes(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        t = T.Transcriber(make_cfg(), tmp, "http://x")
        self.assertIsNone(t.capture_sources)
        self.assertIsNone(t.capture)
        self.assertTrue(t.live)
        t.writer.close()

    def test_legacy_live_command_is_still_a_single_ffmpeg_with_inline_recording(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        t = T.Transcriber(make_cfg(), tmp, "http://x")
        seen = {}

        class StopImmediately(Exception):
            pass

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            raise StopImmediately

        with mock.patch.object(T.subprocess, "Popen", fake_popen), \
                mock.patch.object(T.P, "resolve_source", return_value="mic0"):
            with self.assertRaises(StopImmediately):
                t.run()
        cmd = seen["cmd"]
        self.assertEqual(cmd[:2], ["ffmpeg", "-hide_banner"])
        self.assertIn("pipe:1", cmd)
        self.assertIn("libopus", cmd, "單來源仍由同一個 ffmpeg 順便存檔")
        self.assertEqual(cmd[cmd.index("-i") + 1], "mic0")
        t.q.put(None)
        t.writer.close()


if __name__ == "__main__":
    unittest.main()
