"""core/transcribe.py 的純邏輯：時間格式、SRT 解析與修復、標點正規化、VAD 切段、
逐字稿排版。

這些是全 repo 最細緻的邏輯，原本只有失敗路徑（`tests/test_transcribe_failures.py`）
有測試。其中幾個行為不直覺但是刻意的，下面用註解標出來，免得之後被「順手改掉」：

- `decode_srt()` 要修復 whisper.cpp 把一個中文字的 UTF-8 位元組拆在相鄰兩句字幕之間
  的情況（`a88f462` 修的 bug：直接解碼會整段遺失）。
- `VadChunker` 不可以丟掉任何音訊：所有切出來的位元組加上 buffer 必須等於推進去的量。
- `normalize_punct()` 保留數字與英文內部的 `,` `.` `:`，只把中文語境的半形標點改全形。

全部純函式，不需要 whisper-server、ffmpeg 或網路。
"""
import struct
import tempfile
import unittest
from pathlib import Path

from . import _pathfix  # noqa: F401
from core import transcribe as T


def _frame(amplitude):
    """一個 30ms、單一振幅的 PCM frame。"""
    return struct.pack("<%dh" % T.FRAME_SAMPLES, *([amplitude] * T.FRAME_SAMPLES))


class TimestampTests(unittest.TestCase):
    def test_srt_ts_formats_hours_minutes_millis(self):
        self.assertEqual(T.srt_ts(0), "00:00:00,000")
        self.assertEqual(T.srt_ts(1.5), "00:00:01,500")
        self.assertEqual(T.srt_ts(3661.123), "01:01:01,123")

    def test_srt_ts_clamps_negative_to_zero(self):
        self.assertEqual(T.srt_ts(-5), "00:00:00,000")

    def test_parse_ts_round_trips_srt_ts(self):
        for seconds in (0, 1.5, 59.999, 3600, 3661.123):
            with self.subTest(seconds=seconds):
                self.assertAlmostEqual(T.parse_ts(T.srt_ts(seconds)), seconds, places=3)

    def test_parse_ts_accepts_a_dot_as_the_millisecond_separator(self):
        self.assertAlmostEqual(T.parse_ts("00:00:01.500"), 1.5)


class ParseSrtTests(unittest.TestCase):
    def test_parses_cues_and_joins_wrapped_body_lines(self):
        text = ("1\n00:00:00,000 --> 00:00:02,000\n第一句\n續行\n\n"
                "2\n00:00:02,000 --> 00:00:04,000\n第二句\n")
        self.assertEqual(T.parse_srt(text),
                         [(0.0, 2.0, "第一句續行"), (2.0, 4.0, "第二句")])

    def test_blocks_without_a_timestamp_or_body_are_skipped(self):
        text = ("just noise\n\n"
                "2\n00:00:02,000 --> 00:00:04,000\n\n\n"
                "3\n00:00:04,000 --> 00:00:05,000\n有內容\n")
        self.assertEqual(T.parse_srt(text), [(4.0, 5.0, "有內容")])

    def test_unparsable_timestamps_are_dropped_not_raised(self):
        self.assertEqual(T.parse_srt("1\nxx --> yy\n內容\n"), [])

    def test_empty_input_gives_no_cues(self):
        for text in ("", "\n\n", "   "):
            with self.subTest(text=text):
                self.assertEqual(T.parse_srt(text), [])


class IncompleteUtf8TailTests(unittest.TestCase):
    """回傳結尾「不完整 UTF-8 字元」的位元組數。"""

    def test_complete_text_has_no_dangling_bytes(self):
        self.assertEqual(T._incomplete_utf8_tail(b"abc"), 0)
        self.assertEqual(T._incomplete_utf8_tail("你好".encode()), 0)

    def test_counts_a_truncated_three_byte_character(self):
        full = "世".encode()
        self.assertEqual(len(full), 3)
        self.assertEqual(T._incomplete_utf8_tail(full[:1]), 1)
        self.assertEqual(T._incomplete_utf8_tail(full[:2]), 2)

    def test_counts_a_truncated_four_byte_character(self):
        self.assertEqual(T._incomplete_utf8_tail("😀".encode()[:3]), 3)

    def test_empty_and_orphan_continuation_bytes_are_zero(self):
        self.assertEqual(T._incomplete_utf8_tail(b""), 0)
        self.assertEqual(T._incomplete_utf8_tail(b"\x80\x80\x80\x80"), 0)


class DecodeSrtTests(unittest.TestCase):
    """whisper.cpp 會把一個中文字拆在兩句字幕之間；直接解碼會整段遺失。"""

    def test_character_split_across_two_cues_is_rejoined(self):
        middle = "世".encode()
        raw = (b"1\n00:00:00,000 --> 00:00:01,000\n" + "你好".encode() + middle[:1]
               + b"\n\n2\n00:00:01,000 --> 00:00:02,000\n" + middle[1:] + "界".encode()
               + b"\n\n")
        cues = T.parse_srt(T.decode_srt(raw))
        self.assertEqual(cues, [(0.0, 1.0, "你好"), (1.0, 2.0, "世界")],
                         "拆開的「世」要搬到下一句開頭，不可以變成 U+FFFD 或整段消失")

    def test_unrepairable_bytes_are_dropped_rather_than_shown_as_replacement(self):
        raw = b"1\n00:00:00,000 --> 00:00:01,000\n\xff\xfe" + "好".encode() + b"\n\n"
        text = T.decode_srt(raw)
        self.assertNotIn("�", text)
        self.assertEqual(T.parse_srt(text), [(0.0, 1.0, "好")])

    def test_plain_ascii_passes_through(self):
        raw = b"1\n00:00:00,000 --> 00:00:01,000\nhello\n\n"
        self.assertEqual(T.parse_srt(T.decode_srt(raw)), [(0.0, 1.0, "hello")])

    def test_junk_input_returns_something_parse_srt_can_handle(self):
        for raw in (b"", b"garbage", b"\n\n\n"):
            with self.subTest(raw=raw):
                self.assertEqual(T.parse_srt(T.decode_srt(raw)), [])


class NormalizePunctTests(unittest.TestCase):
    def test_half_width_punctuation_becomes_full_width_in_chinese(self):
        self.assertEqual(T.normalize_punct("他說:好,那就這樣"), "他說：好，那就這樣")
        self.assertEqual(T.normalize_punct("真的嗎?太好了!"), "真的嗎？太好了！")

    def test_numbers_and_english_keep_their_own_separators(self):
        # 千分位與時間不可以被改成全形，否則數字會變成亂碼
        self.assertEqual(T.normalize_punct("這是 1,000 元"), "這是 1,000 元")
        self.assertEqual(T.normalize_punct("a:b 12:30"), "a：b 12:30")

    def test_sentence_final_period_after_latin_becomes_full_width(self):
        self.assertEqual(T.normalize_punct("FOO.BAR 結束."), "FOO.BAR 結束。")

    def test_ellipsis_is_collapsed(self):
        self.assertEqual(T.normalize_punct("嗯...對"), "嗯…對")
        self.assertEqual(T.normalize_punct("嗯。。。對"), "嗯…對")

    def test_repeated_and_redundant_punctuation_is_collapsed(self):
        self.assertEqual(T.normalize_punct("好，，對"), "好，對")
        self.assertEqual(T.normalize_punct("好，。對"), "好。對")

    def test_whitespace_around_punctuation_is_removed(self):
        self.assertEqual(T.normalize_punct("好 ， 對"), "好，對")


class FrameRmsTests(unittest.TestCase):
    def test_silence_is_zero_and_louder_is_bigger(self):
        self.assertEqual(T.frame_rms(_frame(0)), 0.0)
        self.assertLess(T.frame_rms(_frame(100)), T.frame_rms(_frame(8000)))

    def test_constant_amplitude_rms_equals_that_amplitude(self):
        self.assertAlmostEqual(T.frame_rms(_frame(1000)), 1000.0, places=6)

    def test_empty_frame_is_zero_not_a_division_error(self):
        self.assertEqual(T.frame_rms(b""), 0.0)


class VadChunkerTests(unittest.TestCase):
    def _chunker(self, min_s=12, max_s=30):
        return T.VadChunker(min_s, max_s, silence_ms=500, sensitivity=2.5)

    def test_never_loses_audio(self):
        """切出來的位元組 + 還留在 buffer 的，必須剛好等於推進去的量。"""
        c = self._chunker()
        pushed = emitted = 0
        for i in range(4000):                       # 120 秒
            amplitude = 20 if (i // 40) % 5 == 0 else 3000
            pushed += T.FRAME_BYTES
            out = c.push(_frame(amplitude))
            if out:
                emitted += len(out[1])
        tail = c.flush()
        if tail:
            emitted += len(tail[1])
        self.assertEqual(emitted, pushed)

    def test_speech_with_pauses_cuts_at_or_after_min_chunk(self):
        c = self._chunker()
        cuts = []
        for i in range(4000):
            amplitude = 20 if (i // 40) % 5 == 0 else 3000
            out = c.push(_frame(amplitude))
            if out:
                cuts.append((out[0], len(out[1]) / 2 / T.SR))
        self.assertTrue(cuts)
        for start, duration in cuts:
            self.assertGreaterEqual(duration, 12 - 0.2)
            self.assertLessEqual(duration, 30 + 0.2)

    def test_chunk_starts_are_strictly_increasing(self):
        c = self._chunker()
        starts = [out[0] for i in range(4000)
                  if (out := c.push(_frame(20 if (i // 40) % 5 == 0 else 3000)))]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(starts), len(set(starts)))

    def test_voiced_ratio_stays_within_zero_and_one(self):
        c = self._chunker()
        for i in range(2000):
            out = c.push(_frame(20 if (i // 40) % 5 == 0 else 3000))
            if out:
                self.assertGreaterEqual(out[2], 0.0)
                self.assertLessEqual(out[2], 1.0)

    def test_constant_tone_without_any_pause_is_still_cut_at_max_chunk(self):
        # 沒有靜音可切時 _best_cut() 退而求其次找最安靜的 300ms，不可以無限累積
        c = self._chunker()
        cuts = [out for i in range(1200) if (out := c.push(_frame(8000)))]
        self.assertTrue(cuts, "一直有聲音也必須在 max_chunk 強制切段")
        for _, chunk, _ in cuts:
            self.assertLessEqual(len(chunk) / 2 / T.SR, 30 + 0.2)

    def test_pure_silence_is_cut_at_the_midpoint_of_the_pause(self):
        # 刻意的行為：整個 buffer 都是靜音時切在停頓中點（約一半），
        # 所以會短於 min_chunk。這些段落 voiced=0，_worker 會直接略過。
        c = self._chunker()
        cuts = [out for _ in range(1200) if (out := c.push(_frame(0)))]
        self.assertTrue(cuts)
        self.assertTrue(all(voiced == 0.0 for _, _, voiced in cuts))

    def test_flush_returns_the_remainder_once_then_nothing(self):
        c = self._chunker()
        for _ in range(10):
            c.push(_frame(3000))
        first = c.flush()
        self.assertIsNotNone(first)
        self.assertEqual(len(first[1]), 10 * T.FRAME_BYTES)
        self.assertIsNone(c.flush())

    def test_total_seconds_counts_cut_and_buffered_audio(self):
        c = self._chunker()
        for _ in range(100):
            c.push(_frame(3000))
        self.assertAlmostEqual(c.total_seconds, 100 * T.FRAME_MS / 1000, places=6)


class TranscriptWriterLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _writer(self, section_s=300, para_gap=2.5, para_max=180):
        w = T.TranscriptWriter(self.dir, "計概", section_s, para_gap, para_max, opencc=False)
        self.addCleanup(lambda: [f.close() for f in (w.md, w.srt) if not f.closed])
        return w

    def _md(self):
        return (self.dir / "transcript.md").read_text(encoding="utf-8")

    def _srt(self):
        return (self.dir / "transcript.srt").read_text(encoding="utf-8")

    def test_front_matter_is_written_once(self):
        w = self._writer()
        w.add([(0.0, 1.0, "第一句")])
        w.close()
        text = self._md()
        self.assertEqual(text.count("type: transcript"), 1)
        self.assertIn("tags: [逐字稿]", text)
        self.assertIn("# 計概 逐字稿", text)

    def test_section_heading_every_section_interval(self):
        w = self._writer(section_s=300)
        for start in (0.0, 100.0, 301.0, 700.0):
            w.add([(start, start + 1, f"句{int(start)}")])
        w.close()
        headings = [l for l in self._md().splitlines() if l.startswith("## ")]
        self.assertEqual(headings, ["## 00:00:00", "## 00:05:01", "## 00:11:40"])
        self.assertEqual(w.sections, 3)

    def test_srt_cue_numbers_are_sequential(self):
        w = self._writer()
        w.add([(0.0, 1.0, "一"), (1.0, 2.0, "二")])
        w.add([(2.0, 3.0, "三")])
        w.close()
        numbers = [l for l in self._srt().splitlines() if l.strip().isdigit()]
        self.assertEqual(numbers, ["1", "2", "3"])

    def test_hallucinated_boilerplate_is_dropped(self):
        w = self._writer()
        w.add([(0.0, 1.0, "請訂閱我的頻道"), (1.0, 2.0, "真正的課堂內容")])
        w.close()
        text = self._md()
        self.assertIn("真正的課堂內容", text)
        self.assertNotIn("訂閱", text)

    def test_whisper_repeat_loop_is_suppressed(self):
        w = self._writer()
        w.add([(float(i), i + 1.0, "一樣的句子") for i in range(5)])
        w.close()
        self.assertEqual(self._md().count("一樣的句子"), 2,
                         "連續重複的句子最多留兩次，第三次之後視為 whisper 迴圈")

    def test_a_long_pause_starts_a_new_paragraph(self):
        w = self._writer(para_gap=2.5)
        w.add([(0.0, 1.0, "這一句夠長所以 para_len 會超過四十個字" * 2)])
        w.add([(10.0, 11.0, "停頓很久之後的新段落")])
        w.close()
        self.assertIn("\n\n`00:00:10` 停頓很久之後的新段落", self._md())

    def test_continuing_text_is_joined_with_punctuation(self):
        w = self._writer()
        w.add([(0.0, 1.0, "前半"), (1.0, 2.0, "後半")])
        w.close()
        self.assertIn("前半，後半", self._md())

    def test_adjacent_ascii_is_joined_with_a_space(self):
        w = self._writer()
        w.add([(0.0, 1.0, "fork"), (1.0, 2.0, "exec")])
        w.close()
        self.assertIn("fork exec", self._md())

    def test_close_terminates_the_last_sentence(self):
        w = self._writer()
        w.add([(0.0, 1.0, "沒有句號的結尾")])
        w.close()
        self.assertTrue(self._md().rstrip().endswith("。"))

    def test_add_returns_the_tail_used_as_the_next_whisper_prompt(self):
        w = self._writer()
        tail = w.add([(0.0, 1.0, "第一句"), (1.0, 2.0, "第二句")])
        w.close()
        self.assertIn("第二句", tail)

    def test_empty_cue_list_writes_nothing_and_returns_empty_tail(self):
        w = self._writer()
        before = self._md()
        self.assertEqual(w.add([]), "")
        w.close()
        self.assertEqual(self._md().rstrip("\n"), before.rstrip("\n"))


class SplitSentencesTests(unittest.TestCase):
    def test_long_cue_is_split_and_time_shared_by_character_count(self):
        out = T.TranscriptWriter.split_sentences(0.0, 9.0, "一二三。四五六。七八九。")
        self.assertEqual([t for _, _, t in out], ["一二三。", "四五六。", "七八九。"])
        self.assertAlmostEqual(out[0][0], 0.0)
        self.assertAlmostEqual(out[-1][1], 9.0, places=6)
        for a, b, _ in out:
            self.assertLessEqual(a, b)

    def test_single_sentence_is_returned_untouched(self):
        self.assertEqual(T.TranscriptWriter.split_sentences(0.0, 3.0, "沒有標點"),
                         [(0.0, 3.0, "沒有標點")])


if __name__ == "__main__":
    unittest.main()
