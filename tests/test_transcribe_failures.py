"""轉錄失敗的可見性：耗盡重試的段落與 ffmpeg 失敗都不該被默默吞掉。

對照修改前的行為：`transcribe_request()` 失敗後回傳空字串，呼叫端分不出
「這段真的沒人說話」與「這段轉錄失敗」，整段音訊會從 transcript.md 消失，
而且 status.json 的 errors 不會增加、最後還印「✔ 轉錄完成」。

全部是 mock-based，不需要 whisper-server、ffmpeg 或網路。
"""
import tempfile
import unittest
import urllib.error
import wave
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import summarize as SUM
from core import transcribe as T


def _wav(path, seconds=1.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(T.SR)
        w.writeframes(b"\x00\x01" * int(T.SR * seconds))
    return path


SRT_OK = b"1\n00:00:00,000 --> 00:00:02,000\n\xe4\xbd\xa0\xe5\xa5\xbd\n\n"


class TranscribeRequestTests(unittest.TestCase):
    """送件失敗時要丟 TranscribeError，不是回傳空字串。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wav = _wav(Path(self.tmp.name) / "chunk.wav")
        sleep = mock.patch.object(T.time, "sleep")        # 不要真的等 2 秒 x3
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_raises_after_exhausting_retries(self):
        boom = urllib.error.URLError("Connection refused")
        with mock.patch.object(T.urllib.request, "urlopen", side_effect=boom) as up:
            with self.assertRaises(T.TranscribeError) as caught:
                T.transcribe_request("http://127.0.0.1:9", self.wav, "p")
        self.assertEqual(up.call_count, 3)
        self.assertIn("Connection refused", str(caught.exception))

    def test_returns_decoded_srt_on_success(self):
        resp = mock.MagicMock()
        resp.read.return_value = SRT_OK
        resp.__enter__.return_value = resp
        with mock.patch.object(T.urllib.request, "urlopen", return_value=resp):
            out = T.transcribe_request("http://x", self.wav, "p")
        self.assertEqual(T.parse_srt(out), [(0.0, 2.0, "你好")])

    def test_retries_then_succeeds_without_raising(self):
        resp = mock.MagicMock()
        resp.read.return_value = SRT_OK
        resp.__enter__.return_value = resp
        with mock.patch.object(T.urllib.request, "urlopen",
                               side_effect=[OSError("flaky"), resp]) as up:
            out = T.transcribe_request("http://x", self.wav, "p")
        self.assertEqual(up.call_count, 2)
        self.assertEqual(T.parse_srt(out), [(0.0, 2.0, "你好")])

    def test_json_error_body_is_also_retried_and_raises(self):
        resp = mock.MagicMock()
        resp.read.return_value = b'{"error": "model not loaded"}'
        resp.__enter__.return_value = resp
        with mock.patch.object(T.urllib.request, "urlopen", return_value=resp):
            with self.assertRaises(T.TranscribeError) as caught:
                T.transcribe_request("http://x", self.wav, "p")
        self.assertIn("model not loaded", str(caught.exception))


class GapMarkerTests(unittest.TestCase):
    """transcript.md 的缺口標記：人看得見，但不能騙到下游的解析器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.w = T.TranscriptWriter(self.dir, "計概", 300, 2.5, 180, opencc=False)
        self.addCleanup(self._close_writer)

    def _close_writer(self):
        for fh in (self.w.md, self.w.srt):
            if not fh.closed:
                fh.close()

    def _text(self):
        return (self.dir / "transcript.md").read_text(encoding="utf-8")

    def test_marker_is_written_and_counted(self):
        self.w.add([(0.0, 2.0, "第一句")])
        self.w.add_gap(24.0, 48.0, "連線失敗")
        self.w.close()
        text = self._text()
        self.assertIn(T.GAP_MARK, text)
        self.assertIn("00:00:24–00:00:48", text)
        self.assertIn("24.0 秒", text)
        self.assertIn("連線失敗", text)
        self.assertEqual(self.w.gaps, 1)

    def test_marker_does_not_land_in_the_srt(self):
        self.w.add([(0.0, 2.0, "第一句")])
        self.w.add_gap(24.0, 48.0, "連線失敗")
        self.w.close()
        srt = (self.dir / "transcript.srt").read_text(encoding="utf-8")
        self.assertNotIn(T.GAP_MARK, srt)
        self.assertNotIn("沒有逐字稿", srt)

    def test_next_paragraph_is_not_swallowed_by_the_blockquote(self):
        self.w.add([(0.0, 2.0, "第一句")])
        self.w.add_gap(24.0, 48.0, "連線失敗")
        self.w.add([(48.0, 50.0, "後面這句")])
        self.w.close()
        lines = self._text().splitlines()
        i = next(n for n, l in enumerate(lines) if l.startswith(T.GAP_MARK))
        self.assertEqual(lines[i + 1].strip(), "",
                         "標記後面要空一行，否則 Markdown 會把下一段併進引用區塊")
        self.assertTrue(lines[i + 2].startswith("`00:00:48`"))

    def test_marker_is_not_parsed_as_a_section_or_a_timestamp(self):
        # summarize.py 用這兩個 regex 切小節與標時間。先記下只有正常內容時的結果，
        # 再加入缺口標記，兩者必須完全一樣——標記不可以變出多餘的小節或時間錨點。
        self.w.add([(0.0, 2.0, "第一句")])
        before = self._text()
        self.w.add_gap(24.0, 48.0, "連線失敗")
        self.w.close()
        after = self._text()

        self.assertEqual([m.group(1) for m in SUM.HEADER_RE.finditer(after)],
                         [m.group(1) for m in SUM.HEADER_RE.finditer(before)])
        self.assertEqual(SUM.STAMP_RE.findall(after), SUM.STAMP_RE.findall(before))
        self.assertIn(T.GAP_MARK, after)        # 標記確實寫出去了，不是沒寫才相等

    def test_paragraph_is_closed_before_the_marker(self):
        self.w.add([(0.0, 2.0, "沒有句號結尾的一句")])
        self.w.add_gap(24.0, 48.0, "逾時")
        self.w.close()
        text = self._text()
        self.assertIn("沒有句號結尾的一句。\n", text)
        self.assertEqual(self.w.para_len, 0)

    def test_gap_before_any_text_still_writes_cleanly(self):
        self.w.add_gap(0.0, 24.0, "一開始就失敗")
        self.w.add([(24.0, 26.0, "後面這句")])
        self.w.close()
        lines = self._text().splitlines()
        i = next(n for n, l in enumerate(lines) if l.startswith(T.GAP_MARK))
        self.assertEqual(lines[i + 1].strip(), "")


class _FakeStatus:
    def __init__(self):
        self.errors = []
        self.updates = []

    def error(self, msg):
        self.errors.append(msg)

    def update(self, **kv):
        self.updates.append(kv)


class _Harness(T.Transcriber):
    """繞過 Transcriber.__init__ 的設定載入，只裝上 _worker 需要的欄位。"""

    def __init__(self, session, status):
        self.session = session
        self.status = status
        self.live = False
        self.min_voiced = 0.08
        self.q = __import__("queue").Queue()
        self.stats = {"audio": 0.0, "work": 0.0, "lost": 0.0}
        self.gaps = []
        self.ffmpeg_returncode = None
        self.writer = T.TranscriptWriter(session, "計概", 300, 2.5, 180, opencc=False)

    def close_writer(self):
        for fh in (self.writer.md, self.writer.srt):
            if not fh.closed:
                fh.close()


class WorkerGapTests(unittest.TestCase):
    """_worker 遇到失敗的段落時，三個通道都要留下紀錄。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.status = _FakeStatus()
        self.h = _Harness(self.dir, self.status)
        self.addCleanup(self.h.close_writer)

    def _run_worker_with(self, side_effect, seconds=24.0):
        pcm = b"\x00\x01" * int(T.SR * seconds)
        self.h.q.put((12.0, pcm, 1.0, 0.0))
        self.h.q.put(None)
        with mock.patch.object(_Harness, "_process", side_effect=side_effect):
            self.h._worker()

    def test_failed_chunk_is_recorded_everywhere(self):
        self._run_worker_with(T.TranscribeError("連線失敗：timed out"))
        self.h.writer.close()

        self.assertEqual(len(self.h.gaps), 1)
        gap = self.h.gaps[0]
        self.assertEqual(gap["start"], 12.0)
        self.assertEqual(gap["seconds"], 24.0)
        self.assertIn("timed out", gap["reason"])
        self.assertEqual(self.h.stats["lost"], 24.0)

        self.assertEqual(len(self.status.errors), 1)
        self.assertIn("00:00:12", self.status.errors[0])
        self.assertIn("24 秒音訊未轉錄", self.status.errors[0])

        text = (self.dir / "transcript.md").read_text(encoding="utf-8")
        self.assertIn(T.GAP_MARK, text)
        self.assertIn("timed out", text)

    def test_failed_chunk_does_not_inflate_transcribed_audio(self):
        self._run_worker_with(T.TranscribeError("boom"))
        self.assertEqual(self.h.stats["audio"], 0.0,
                         "失敗的段落不可以算進『已轉錄音訊』，否則速度是灌水的")

    def test_worker_keeps_going_after_a_failed_chunk(self):
        pcm = b"\x00\x01" * int(T.SR * 10)
        for start in (0.0, 10.0, 20.0):
            self.h.q.put((start, pcm, 1.0, 0.0))
        self.h.q.put(None)
        with mock.patch.object(_Harness, "_process",
                               side_effect=[T.TranscribeError("1"), "", T.TranscribeError("3")]):
            self.h._worker()
        self.assertEqual([g["start"] for g in self.h.gaps], [0.0, 20.0])

    def test_non_transcribe_exceptions_are_recorded_too(self):
        self._run_worker_with(OSError("disk full"))
        self.assertEqual(len(self.h.gaps), 1)
        self.assertIn("disk full", self.h.gaps[0]["reason"])
        self.assertEqual(len(self.status.errors), 1)

    def test_almost_silent_chunks_are_skipped_without_becoming_gaps(self):
        pcm = b"\x00\x01" * int(T.SR * 24)
        self.h.q.put((0.0, pcm, 0.0, 0.0))        # voiced 低於 min_voiced
        self.h.q.put(None)
        with mock.patch.object(_Harness, "_process") as proc:
            self.h._worker()
        proc.assert_not_called()
        self.assertEqual(self.h.gaps, [])
        self.assertEqual(self.status.errors, [])


class _FakeProc:
    """假的 ffmpeg：吐完 chunks 就結束，wait() 回傳指定的 returncode。"""

    def __init__(self, chunks, returncode):
        self._chunks = list(chunks)
        self._rc = returncode
        self.stdout = self

    def read(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def wait(self):
        return self._rc

    def poll(self):
        return self._rc


class RunResultTests(unittest.TestCase):
    """run() 的回傳值與最後一行 log 要如實反映 ffmpeg 的結果。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _run(self, returncode, chunks=()):
        h = _Harness(self.dir, _FakeStatus())
        self.addCleanup(h.close_writer)
        h.file = str(self.dir / "in.ogg")
        h.cfg = {"audio": {}, "vad": {"silence_ms": 500}}
        h.min_chunk, h.max_chunk = 24, 30
        h.use_opencc = False
        h.abort = False
        h.stopping = False
        h.proc = None
        h.tmp_dir = self.dir / ".tmp"
        h.tmp_dir.mkdir(exist_ok=True)
        h.chunker = T.VadChunker(24, 30, 500, 2.5)
        lines = []
        with mock.patch.object(T.subprocess, "Popen",
                               return_value=_FakeProc(chunks, returncode)), \
             mock.patch.object(T, "log", side_effect=lines.append), \
             mock.patch.object(T.P, "spawn_kwargs", return_value={}):
            result = T.Transcriber.run(h)
        return result, lines

    def test_ffmpeg_failure_is_reported_in_the_result(self):
        result, lines = self._run(187)
        self.assertTrue(result["ffmpeg_failed"])
        self.assertEqual(result["ffmpeg_returncode"], 187)
        self.assertEqual(result["duration"], 0)
        self.assertTrue(any("沒有讀到任何音訊" in l for l in lines))
        self.assertFalse(any(l.startswith("✔ 轉錄完成") for l in lines),
                         "完全讀不到音訊時不可以回報 ✔ 轉錄完成")

    def test_clean_exit_still_reports_success(self):
        result, lines = self._run(0)
        self.assertFalse(result["ffmpeg_failed"])
        self.assertEqual(result["ffmpeg_returncode"], 0)
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["lost_seconds"], 0.0)
        self.assertTrue(any(l.startswith("✔ 轉錄完成") for l in lines))

    def test_interrupted_ffmpeg_is_not_treated_as_a_failure(self):
        for rc in (255, -2, -15):
            with self.subTest(rc=rc):
                result, _ = self._run(rc)
                self.assertFalse(result["ffmpeg_failed"])

    def test_result_keeps_the_keys_session_py_already_uses(self):
        result, _ = self._run(0)
        for key in ("duration", "speed", "aborted"):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
