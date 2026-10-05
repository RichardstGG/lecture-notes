"""擷取路徑的迴歸測試：ffmpeg 丟音訊時結束代碼仍是 0，所以要自己量出來。

背景：一份 macOS 實機錄音宣告 87.7 分鐘，但只錄到 70.0 分鐘的音訊
（5,196 個空隙、平均每 1.01 秒固定掉約 0.2 秒），而當時完全沒有任何警告。
見 docs/platform-macos.md「擷取掉音訊」。
"""
import queue
import threading
import unittest
from unittest import mock

from . import _pathfix  # noqa: F401
from core import transcribe as T


def packets(duration=0.02, count=10, start=0.0, gaps=()):
    """產生 (pts, duration)；gaps 是 {封包索引: 這個索引之前要插入多少秒空隙}。"""
    gaps = dict(gaps)
    pts = start
    out = []
    for i in range(count):
        pts += gaps.get(i, 0.0)
        out.append((round(pts, 6), duration))
        pts += duration
    return out


class CaptureGapReportTests(unittest.TestCase):
    def test_clean_stream_reports_no_loss(self):
        r = T.capture_gap_report(packets(count=100))
        self.assertEqual(r["gap_count"], 0)
        self.assertEqual(r["lost_seconds"], 0.0)
        self.assertEqual(r["lost_ratio"], 0.0)
        self.assertAlmostEqual(r["audio_seconds"], 2.0, places=3)
        self.assertAlmostEqual(r["span_seconds"], 2.0, places=3)
        self.assertIsNone(r["mean_gap_spacing_seconds"])

    def test_single_gap_is_measured(self):
        r = T.capture_gap_report(packets(count=10, gaps={5: 0.2}))
        self.assertEqual(r["gap_count"], 1)
        self.assertAlmostEqual(r["lost_seconds"], 0.2, places=3)
        self.assertAlmostEqual(r["max_gap_seconds"], 0.2, places=3)
        self.assertAlmostEqual(r["audio_seconds"], 0.2, places=3)
        self.assertAlmostEqual(r["span_seconds"], 0.4, places=3)
        self.assertAlmostEqual(r["lost_ratio"], 0.5, places=3)

    def test_the_macos_pattern_is_recognisable(self):
        # 每 50 個封包（1.0 秒）掉 0.2 秒，重現實機量到的樣態
        gaps = {i: 0.2 for i in range(50, 500, 50)}
        r = T.capture_gap_report(packets(count=500, gaps=gaps))
        self.assertEqual(r["gap_count"], 9)
        self.assertAlmostEqual(r["lost_seconds"], 1.8, places=3)
        self.assertAlmostEqual(r["audio_seconds"], 10.0, places=3)
        # 規律間隔是「系統性問題」的指紋，不是偶發負載
        self.assertAlmostEqual(r["mean_gap_spacing_seconds"], 1.2, places=2)
        self.assertGreater(r["lost_ratio"], T.CAPTURE_LOSS_ERROR)

    def test_sub_millisecond_jitter_is_not_counted_as_a_gap(self):
        r = T.capture_gap_report(packets(count=10, gaps={5: 0.0005}))
        self.assertEqual(r["gap_count"], 0)

    def test_overlapping_packets_do_not_produce_negative_loss(self):
        pk = [(0.0, 0.02), (0.01, 0.02), (0.05, 0.02)]
        r = T.capture_gap_report(pk)
        self.assertGreaterEqual(r["lost_seconds"], 0.0)
        self.assertEqual(r["gap_count"], 1)

    def test_empty_stream_is_safe(self):
        r = T.capture_gap_report([])
        self.assertEqual(r["span_seconds"], 0.0)
        self.assertEqual(r["lost_ratio"], 0.0)

    def test_accepts_a_generator_without_materialising_it(self):
        # 三小時的錄音有五十幾萬個封包，不可以整份讀進記憶體
        r = T.capture_gap_report(p for p in packets(count=100))
        self.assertAlmostEqual(r["audio_seconds"], 2.0, places=3)

    def test_nonzero_start_time_does_not_inflate_the_span(self):
        # 實機那份錄音的第一個封包 pts 是 0.1067，不是 0
        r = T.capture_gap_report(packets(count=100, start=0.1067))
        self.assertAlmostEqual(r["span_seconds"], 2.0, places=3)
        self.assertEqual(r["gap_count"], 0)


class ProbeCaptureTests(unittest.TestCase):
    def test_missing_file_returns_none(self):
        self.assertIsNone(T.probe_capture("/nonexistent/recording.ogg"))

    def test_unreadable_stream_returns_none(self):
        with mock.patch.object(T, "_ffprobe_packets", return_value=iter([])), \
             mock.patch.object(T.Path, "is_file", return_value=True):
            self.assertIsNone(T.probe_capture("whatever.ogg"))


class _FakeStatus:
    def __init__(self):
        self.errors = []
        self.events = []

    def error(self, msg):
        self.errors.append(msg)

    def event(self, kind, **kv):
        self.events.append((kind, kv))

    def update(self, **kv):
        pass


class _Target(T.Transcriber):
    """只為了呼叫 _check_capture，不跑 __init__。"""

    def __init__(self, status, recording_path=None):
        self.status = status
        if recording_path is not None:
            self.recording_path = recording_path


class CheckCaptureTests(unittest.TestCase):
    def test_no_recording_means_nothing_to_check(self):
        st = _FakeStatus()
        self.assertIsNone(_Target(st)._check_capture())
        self.assertEqual(st.events, [])

    def test_recording_path_defaults_to_none_on_subclasses(self):
        # 繞過 __init__ 的子類別也要能安全取用（測試 harness 就是這樣用的）
        self.assertIsNone(T.Transcriber.recording_path)

    def test_heavy_loss_is_reported_as_an_error(self):
        st = _FakeStatus()
        t = _Target(st, recording_path="rec.ogg")
        report = {"audio_seconds": 4200.0, "span_seconds": 5259.0,
                  "lost_seconds": 1059.0, "lost_ratio": 0.2014,
                  "gap_count": 5196, "max_gap_seconds": 0.288,
                  "mean_gap_spacing_seconds": 1.012}
        lines = []
        with mock.patch.object(T, "probe_capture", return_value=report), \
             mock.patch.object(T, "log", side_effect=lines.append):
            out = t._check_capture()
        self.assertEqual(out, report)
        self.assertEqual(len(st.errors), 1)
        self.assertIn("20.1%", st.errors[0])
        self.assertTrue(any("會後處理唯一的來源" in l for l in lines),
                        "要說清楚這份錄音不能用來做發言者辨識")
        self.assertTrue(any("規律間隔" in l for l in lines),
                        "規律間隔是系統性問題的指紋，要講出來")
        self.assertEqual(st.events[-1][0], "capture_integrity")

    def _run_check(self, report):
        st = _FakeStatus()
        t = _Target(st, recording_path="rec.ogg")
        lines = []
        with mock.patch.object(T, "probe_capture", return_value=report), \
             mock.patch.object(T, "log", side_effect=lines.append):
            t._check_capture()
        return st, lines

    def test_middling_loss_warns_but_is_not_an_error(self):
        st, lines = self._run_check(
            {"audio_seconds": 990.0, "span_seconds": 1000.0, "lost_seconds": 10.0,
             "lost_ratio": 0.01, "gap_count": 30, "max_gap_seconds": 0.5,
             "mean_gap_spacing_seconds": None})
        self.assertEqual(st.errors, [], "1% 還不到錯誤門檻")
        self.assertTrue(any("個空隙" in l for l in lines))

    def test_granule_rounding_noise_stays_silent(self):
        # 開發機上 10 份正常錄音量到 0.000–0.057%，最多 835 個空隙。
        # 這些是 ogg/opus granule 取整的產物，不能每次錄音都嚇使用者一次。
        st, lines = self._run_check(
            {"audio_seconds": 7805.7, "span_seconds": 7810.1, "lost_seconds": 4.4,
             "lost_ratio": 0.00057, "gap_count": 701, "max_gap_seconds": 0.03,
             "mean_gap_spacing_seconds": 11.1})
        self.assertEqual(st.errors, [])
        self.assertEqual(lines, [], "正常錄音要完全安靜")
        self.assertEqual(st.events[-1][0], "capture_integrity",
                         "乾淨也要留下紀錄，才能看出是哪一次開始壞的")

    def test_clean_capture_is_silent_but_still_recorded(self):
        st, lines = self._run_check(
            {"audio_seconds": 1000.0, "span_seconds": 1000.0, "lost_seconds": 0.0,
             "lost_ratio": 0.0, "gap_count": 0, "max_gap_seconds": 0.0,
             "mean_gap_spacing_seconds": None})
        self.assertEqual(st.errors, [])
        self.assertEqual(lines, [])
        self.assertEqual(st.events[-1][0], "capture_integrity")

    def test_thresholds_are_ordered_with_headroom_over_observed_noise(self):
        self.assertLess(T.CAPTURE_LOSS_WARN, T.CAPTURE_LOSS_ERROR)
        self.assertGreater(T.CAPTURE_LOSS_WARN, 0.00057 * 5,
                           "warn 門檻要高於實測的無害雜訊，留足夠餘裕")


class _BlockingStdout:
    """read() 會一直供應資料，直到 stop 被設定。用來證明讀取不被下游卡住。"""

    def __init__(self, block, total_blocks):
        self.block = block
        self.left = total_blocks
        self.reads = 0
        self.closed = False

    def read(self, _n):
        if self.left <= 0:
            return b""
        self.left -= 1
        self.reads += 1
        return self.block

    def close(self):
        self.closed = True


class DrainTests(unittest.TestCase):
    """_drain 只負責把 pipe 讀乾；VAD 與 status 不在讀取路徑上。

    這是 macOS 掉音訊的根因修復：以前三件事在同一個迴圈，任何停頓都會
    讓 ffmpeg 阻塞，進而讓作業系統的擷取緩衝溢出。
    """

    def _drain_all(self, stdout):
        t = T.Transcriber.__new__(T.Transcriber)
        t.proc = mock.Mock(stdout=stdout)
        sink = queue.Queue()
        th = threading.Thread(target=T.Transcriber._drain, args=(t, sink))
        th.start()
        th.join(timeout=5)
        self.assertFalse(th.is_alive(), "_drain 應該在 pipe 結束後自己收尾")
        out = []
        while True:
            item = sink.get_nowait()
            if item is None:
                break
            out.append(item)
        return out

    def test_drain_reads_everything_and_terminates_with_none(self):
        out = self._drain_all(_BlockingStdout(b"x" * T.READ_BLOCK, 5))
        self.assertEqual(len(out), 5)
        self.assertEqual(b"".join(out), b"x" * (5 * T.READ_BLOCK))

    def test_drain_keeps_reading_while_consumer_does_nothing(self):
        # 消費端完全不動，_drain 還是要把全部讀完 —— 這正是以前做不到的事
        stdout = _BlockingStdout(b"y" * T.READ_BLOCK, 40)
        out = self._drain_all(stdout)
        self.assertEqual(stdout.reads, 40)
        self.assertEqual(len(out), 40)

    def test_drain_survives_a_closed_pipe(self):
        class Boom:
            def read(self, _n):
                raise ValueError("I/O operation on closed file")
        t = T.Transcriber.__new__(T.Transcriber)
        t.proc = mock.Mock(stdout=Boom())
        sink = queue.Queue()
        T.Transcriber._drain(t, sink)
        self.assertIsNone(sink.get_nowait(), "即使讀取爆掉也要放哨兵，不然主迴圈會卡死")

    def test_backlog_threshold_is_about_thirty_seconds(self):
        secs = T.BACKLOG_WARN_BLOCKS * T.READ_BLOCK / 2 / T.SR
        self.assertAlmostEqual(secs, 30.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
