"""會後發言者辨識引擎層：用假 diarizer／假 ASR 驗證契約，不載入模型、不開 server。

對應 docs/meeting-workbench-contract.md 驗收項目中屬於引擎層的部分：進度單調、
取消安全點、錯誤分類、區間不變式、原子輸出、不覆蓋舊產物、requested != actual、
S00、OpenCC 不影響代號。

唯一的外部依賴是 ffmpeg（用真實的小 wav 檔餵它）；沒有 ffmpeg 時整檔跳過。
"""
import hashlib
import json
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from . import _pathfix  # noqa: F401
from core import diarize as D
from core.diarize import (DiarizationCancelled, DiarizationError, DiarizationRequest,
                          DiarizerOutput, UNASSIGNED, diarize_session)

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
WAV_SECONDS = 30.0


def make_wav(path, seconds=WAV_SECONDS, sr=16000):
    """聽起來是什麼不重要，ffmpeg 要能解碼就行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for i in range(int(seconds * sr)):
        frames += struct.pack("<h", 2000 if (i // 800) % 2 else -2000)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return path


def fake_diarizer(turns, duration=WAV_SECONDS):
    """turns: [(start, end, cluster, conf)]"""
    def inner(req, progress, cancel):
        progress(duration * 0.5, duration, 0)
        progress(duration, duration, 0)
        return DiarizerOutput(list(turns), duration, "test-engine")
    return inner


def fake_asr(texts_per_call=None, fail_on=None):
    """每次呼叫回傳一組 (start, end, text)，時間相對於送進去的那段音訊。"""
    calls = {"n": 0}

    def inner(req, pcm, prompt):
        i = calls["n"]
        calls["n"] += 1
        if fail_on is not None and i == fail_on:
            raise DiarizationError("asr_failed", "injected failure")
        dur = len(pcm) / 2 / 16000
        if texts_per_call and i < len(texts_per_call):
            return texts_per_call[i]
        return [(0.0, max(dur, 0.01), f"第{i}段內容")]
    inner.calls = calls
    return inner


@unittest.skipUnless(HAVE_FFMPEG, "需要 ffmpeg")
class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        self.audio = make_wav(self.session / "source_audio.wav")
        self.m1 = self.root / "seg.onnx"
        self.m2 = self.root / "emb.onnx"
        self.m1.write_bytes(b"not a real model")
        self.m2.write_bytes(b"not a real model either")

    def req(self, **kw):
        base = dict(session_dir=self.session, source_audio=self.audio,
                    source_sha256="", meeting_name="設計會議", requested_speakers=3,
                    segmentation_model=self.m1, embedding_model=self.m2, opencc=False)
        base.update(kw)
        return DiarizationRequest(**base)

    def staging_dirs(self):
        return list(self.session.glob(".diarize-staging-*"))


# ---------------------------------------------------------------- 純函式
class TestPure(unittest.TestCase):
    def test_relabel_uses_first_appearance_order(self):
        turns = [(0.0, 1.0, 7, 1.0), (1.0, 2.0, 2, 1.0), (2.0, 3.0, 7, 1.0)]
        self.assertEqual([t[2] for t in D._relabel(turns, None)], ["S01", "S02", "S01"])

    def test_relabel_confidence_floor_gives_s00(self):
        out = D._relabel([(0.0, 1.0, 1, 0.9), (1.0, 2.0, 2, 0.1)], 0.5)
        self.assertEqual([t[2] for t in out], ["S01", UNASSIGNED])

    def test_relabel_floor_off_by_default_never_emits_s00(self):
        out = D._relabel([(0.0, 1.0, 1, 0.0), (1.0, 2.0, 2, 0.0)], None)
        self.assertNotIn(UNASSIGNED, [t[2] for t in out])

    def test_merge_only_joins_same_speaker_within_gap(self):
        turns = [(0.0, 1.0, "S01"), (1.5, 2.0, "S01"), (2.1, 3.0, "S02"), (9.0, 10.0, "S01")]
        self.assertEqual(D._merge(turns, 1.0),
                         [[0.0, 2.0, "S01"], [2.1, 3.0, "S02"], [9.0, 10.0, "S01"]])

    def test_merge_sorts_by_start(self):
        merged = D._merge([(5.0, 6.0, "S01"), (0.0, 1.0, "S02")], 1.0)
        self.assertEqual([m[0] for m in merged], [0.0, 5.0])

    def test_timestamp_format(self):
        self.assertEqual(D._ts(0), "00:00:00.000")
        self.assertEqual(D._ts(3661.234), "01:01:01.234")

    def test_usable_filters_hallucination_and_prompt_echo_but_not_real_text(self):
        self.assertFalse(D._usable("   "))
        self.assertFalse(D._usable("感謝觀看"))
        self.assertFalse(D._usable("以下是繁體中文的會議內容"))
        self.assertTrue(D._usable("我們來討論一下會議內容的部分"))

    def test_every_error_code_the_worker_can_emit_is_a_known_code(self):
        for code in ("input_invalid", "engine_unavailable", "decode_failed",
                     "segmentation_failed"):
            self.assertIn(code, D.CODES)


# ---------------------------------------------------------------- 輸入驗證
class TestValidation(Base):
    def _expect(self, code, **kw):
        with self.assertRaises(DiarizationError) as cm:
            diarize_session(self.req(**kw), diarizer=fake_diarizer([]), asr=fake_asr())
        self.assertEqual(cm.exception.code, code)

    def test_speakers_zero_rejected(self):
        self._expect("input_invalid", requested_speakers=0)

    def test_speakers_above_max_rejected(self):
        self._expect("input_invalid", requested_speakers=31)

    def test_speakers_non_int_rejected(self):
        self._expect("input_invalid", requested_speakers=3.5)

    def test_speakers_bool_rejected(self):
        self._expect("input_invalid", requested_speakers=True)

    def test_threshold_out_of_range_rejected(self):
        self._expect("input_invalid", cluster_threshold=0.0)
        self._expect("input_invalid", cluster_threshold=1.5)

    def test_window_shift_must_be_positive(self):
        self._expect("input_invalid", segmentation_window_shift=0.0)

    def test_missing_source(self):
        self._expect("source_missing", source_audio=self.session / "nope.wav")

    def test_missing_model(self):
        self._expect("model_missing", segmentation_model=self.root / "nope.onnx")

    def test_source_hash_mismatch_blocks_rerun(self):
        self._expect("source_hash_mismatch", source_sha256="0" * 64)

    def test_matching_hash_allows_run(self):
        h = hashlib.sha256(self.audio.read_bytes()).hexdigest()
        r = diarize_session(self.req(source_sha256=h),
                            diarizer=fake_diarizer([(0.0, 5.0, 1, 1.0)]), asr=fake_asr())
        self.assertEqual(r.actual_speakers, 1)

    def test_validation_failure_leaves_nothing_behind(self):
        self._expect("input_invalid", requested_speakers=0)
        self.assertEqual(self.staging_dirs(), [])
        self.assertFalse((self.session / "diarization").exists())


# ---------------------------------------------------------------- 進度
class TestProgress(Base):
    def test_progress_is_monotonic_and_bounded_per_stage(self):
        seen = []

        def naughty(req, progress, cancel):       # 故意回報亂序與超界的秒數
            progress(WAV_SECONDS * 0.8, WAV_SECONDS, 0)
            progress(WAV_SECONDS * 0.2, WAV_SECONDS, 0)      # 退回去
            progress(WAV_SECONDS * 99, WAV_SECONDS, 0)       # 超界
            return DiarizerOutput([(0.0, 5.0, 1, 1.0), (6.0, 9.0, 2, 1.0)], WAV_SECONDS, "t")

        diarize_session(self.req(), progress=seen.append, diarizer=naughty, asr=fake_asr())
        self.assertEqual(seen[0]["stage"], "segmentation")
        self.assertIn("retranscription", [e["stage"] for e in seen])
        for stage in ("segmentation", "retranscription"):
            vals = [e["processed_seconds"] for e in seen if e["stage"] == stage]
            self.assertEqual(vals, sorted(vals), f"{stage} 秒數必須單調不減")
            for e in (x for x in seen if x["stage"] == stage):
                self.assertGreaterEqual(e["processed_seconds"], 0.0)
                self.assertLessEqual(e["processed_seconds"], e["total_seconds"],
                                     f"{stage} 秒數超出 total")

    def test_retranscription_restarts_from_zero(self):
        seen = []
        diarize_session(self.req(), progress=seen.append,
                        diarizer=fake_diarizer([(0.0, 5.0, 1, 1.0)]), asr=fake_asr())
        rt = [e["processed_seconds"] for e in seen if e["stage"] == "retranscription"]
        self.assertEqual(rt[0], 0.0, "重轉錄階段要從零重新計，不能延續上一階段")

    def test_speakers_found_reported_after_clustering(self):
        seen = []
        diarize_session(self.req(), progress=seen.append,
                        diarizer=fake_diarizer([(0.0, 5.0, 1, 1.0), (6.0, 9.0, 2, 1.0)]),
                        asr=fake_asr())
        self.assertEqual(max(e["speakers_found"] for e in seen), 2)

    def test_no_progress_callback_is_fine(self):
        diarize_session(self.req(), progress=None,
                        diarizer=fake_diarizer([(0.0, 5.0, 1, 1.0)]), asr=fake_asr())


# ---------------------------------------------------------------- 取消
class TestCancel(Base):
    def _good_run(self):
        return diarize_session(self.req(), diarizer=fake_diarizer([(0.0, 5.0, 1, 1.0)]),
                               asr=fake_asr())

    def test_cancel_before_work_raises_cancelled(self):
        with self.assertRaises(DiarizationCancelled):
            diarize_session(self.req(), cancel=lambda: True,
                            diarizer=fake_diarizer([(0.0, 5.0, 1, 1.0)]), asr=fake_asr())

    def test_cancel_during_retranscription(self):
        state = {"n": 0}

        def asr(req, pcm, prompt):
            state["n"] += 1
            return [(0.0, 1.0, "內容")]

        with self.assertRaises(DiarizationCancelled):
            diarize_session(self.req(), cancel=lambda: state["n"] >= 1,
                            diarizer=fake_diarizer([(0.0, 3.0, 1, 1.0), (5.0, 8.0, 2, 1.0),
                                                    (10.0, 13.0, 1, 1.0)]),
                            asr=asr)
        self.assertEqual(state["n"], 1, "取消後不該再多送任何一段")

    def test_cancel_preserves_previous_successful_generation(self):
        first = self._good_run()
        before = (self.session / "diarization.current.json").read_text(encoding="utf-8")
        with self.assertRaises(DiarizationCancelled):
            diarize_session(self.req(), cancel=lambda: True,
                            diarizer=fake_diarizer([(0.0, 5.0, 1, 1.0)]), asr=fake_asr())
        self.assertEqual((self.session / "diarization.current.json").read_text(encoding="utf-8"), before)
        self.assertTrue((self.session / first.speaker_transcript).is_file())

    def test_cancel_leaves_no_staging_dir(self):
        state = {"n": 0}

        def asr(req, pcm, prompt):
            state["n"] += 1
            return [(0.0, 1.0, "內容")]

        with self.assertRaises(DiarizationCancelled):
            diarize_session(self.req(), cancel=lambda: state["n"] >= 1,
                            diarizer=fake_diarizer([(0.0, 3.0, 1, 1.0), (5.0, 8.0, 2, 1.0)]),
                            asr=asr)
        self.assertEqual(self.staging_dirs(), [])


# ---------------------------------------------------------------- 失敗
class TestFailure(Base):
    def test_asr_failure_propagates_and_does_not_write(self):
        with self.assertRaises(DiarizationError) as cm:
            diarize_session(self.req(),
                            diarizer=fake_diarizer([(0.0, 3.0, 1, 1.0), (5.0, 8.0, 2, 1.0)]),
                            asr=fake_asr(fail_on=1))
        self.assertEqual(cm.exception.code, "asr_failed")
        self.assertFalse((self.session / "diarization.current.json").exists())
        self.assertEqual(self.staging_dirs(), [])

    def test_failure_preserves_previous_generation(self):
        first = diarize_session(self.req(), diarizer=fake_diarizer([(0.0, 5.0, 1, 1.0)]),
                                asr=fake_asr())
        before = (self.session / "diarization.current.json").read_text(encoding="utf-8")
        with self.assertRaises(DiarizationError):
            diarize_session(self.req(), diarizer=fake_diarizer([(0.0, 3.0, 1, 1.0)]),
                            asr=fake_asr(fail_on=0))
        self.assertEqual((self.session / "diarization.current.json").read_text(encoding="utf-8"), before)
        self.assertEqual(json.loads(before)["generation"], first.generation)

    def test_all_text_filtered_is_empty_result_not_success(self):
        with self.assertRaises(DiarizationError) as cm:
            diarize_session(self.req(), diarizer=fake_diarizer([(0.0, 3.0, 1, 1.0)]),
                            asr=fake_asr(texts_per_call=[[(0.0, 1.0, "感謝觀看")]]))
        self.assertEqual(cm.exception.code, "empty_result")
        self.assertFalse((self.session / "diarization.current.json").exists())

    def test_no_turns_is_empty_result(self):
        with self.assertRaises(DiarizationError) as cm:
            diarize_session(self.req(), diarizer=fake_diarizer([]), asr=fake_asr())
        self.assertEqual(cm.exception.code, "empty_result")

    def test_unexpected_exception_is_classified_not_leaked(self):
        def boom(req, progress, cancel):
            raise RuntimeError("something odd")
        with self.assertRaises(DiarizationError) as cm:
            diarize_session(self.req(), diarizer=boom, asr=fake_asr())
        self.assertEqual(cm.exception.code, "segmentation_failed")
        self.assertEqual(self.staging_dirs(), [])


# ---------------------------------------------------------------- 產物
class TestArtifacts(Base):
    def run_ok(self, turns=None, texts=None, **kw):
        turns = turns or [(0.0, 4.0, 1, 1.0), (5.0, 9.0, 2, 1.0), (10.0, 14.0, 1, 1.0)]
        return diarize_session(self.req(**kw), diarizer=fake_diarizer(turns),
                               asr=fake_asr(texts_per_call=texts))

    def test_speakers_json_schema_and_invariants(self):
        r = self.run_ok()
        js = json.loads((self.session / r.speakers_json).read_text(encoding="utf-8"))
        for key in ("schema_version", "source_sha256", "requested_speakers",
                    "actual_speakers", "duration_seconds", "speakers", "segments", "engine"):
            self.assertIn(key, js)
        self.assertEqual(js["schema_version"], 1)
        prev = -1
        for s in js["segments"]:
            self.assertLess(s["start_ms"], s["end_ms"], "start_ms 必須 < end_ms")
            self.assertGreaterEqual(s["start_ms"], prev, "須按開始時間遞增")
            prev = s["start_ms"]
        for key in ("name", "version", "segmentation_model_sha256",
                    "embedding_model_sha256", "params"):
            self.assertIn(key, js["engine"])
        self.assertEqual(js["engine"]["version"], "test-engine")

    def test_actual_may_differ_from_requested(self):
        # 要求 10 人，實際只有 2 個代號 —— 契約明文允許，不可強制相等
        r = self.run_ok(requested_speakers=10)
        self.assertEqual(r.requested_speakers, 10)
        self.assertEqual(r.actual_speakers, 2)

    def test_markdown_shape(self):
        r = self.run_ok()
        lines = (self.session / r.speaker_transcript).read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "# 設計會議 · 發言者逐字稿")
        body = [l for l in lines if l.startswith("[")]
        self.assertTrue(body)
        for l in body:
            self.assertRegex(
                l, r"^\[\d{2}:\d{2}:\d{2}\.\d{3}–\d{2}:\d{2}:\d{2}\.\d{3}\] S\d{2}: .+$")

    def test_manifest_points_at_generation(self):
        r = self.run_ok()
        man = json.loads((self.session / "diarization.current.json").read_text(encoding="utf-8"))
        self.assertEqual(man["schema_version"], 1)
        self.assertEqual(man["generation"], r.generation)
        self.assertEqual(man["speaker_transcript"], r.speaker_transcript)
        self.assertEqual(man["speakers"], r.speakers_json)
        for rel in (man["speaker_transcript"], man["speakers"]):
            self.assertFalse(Path(rel).is_absolute(), "manifest 必須是 session 相對路徑")
            self.assertTrue((self.session / rel).is_file())

    def test_rerun_creates_new_generation_without_touching_old(self):
        first = self.run_ok()
        old_md = (self.session / first.speaker_transcript).read_text(encoding="utf-8")
        second = self.run_ok(requested_speakers=4)
        self.assertNotEqual(first.generation, second.generation)
        self.assertEqual((self.session / first.speaker_transcript).read_text(encoding="utf-8"), old_md)
        man = json.loads((self.session / "diarization.current.json").read_text(encoding="utf-8"))
        self.assertEqual(man["generation"], second.generation)
        gens = sorted(p.name for p in (self.session / "diarization").iterdir() if p.is_dir())
        self.assertEqual(gens, ["0001", "0002"])

    def test_success_leaves_no_staging_dir_and_no_worker_log(self):
        r = self.run_ok()
        self.assertEqual(self.staging_dirs(), [])
        self.assertEqual(list((self.session / "diarization" / r.generation).glob("worker.log")), [])

    def test_original_transcript_untouched(self):
        orig = self.session / "transcript.md"
        orig.write_text("# 原稿\n不可被改動\n", encoding="utf-8")
        before = orig.read_text(encoding="utf-8")
        self.run_ok()
        self.assertEqual(orig.read_text(encoding="utf-8"), before)

    def test_short_turns_are_skipped(self):
        asr = fake_asr()
        diarize_session(self.req(min_turn_seconds=1.0),
                        diarizer=fake_diarizer([(0.0, 0.2, 1, 1.0), (2.0, 6.0, 2, 1.0)]),
                        asr=asr)
        self.assertEqual(asr.calls["n"], 1, "短於 min_turn_seconds 的區間不該送 ASR")

    def test_times_come_from_asr_offsets_not_char_counts(self):
        # 同一段音訊回兩句，長度差很多但時間由 ASR 指定；若有人改成按字數比例分配，
        # 第二句的 start 會跟著字數變，這個測試就會掛。
        texts = [[(0.5, 1.0, "短"), (1.0, 3.0, "這一句明顯長很多很多很多")]]
        r = self.run_ok(turns=[(2.0, 8.0, 1, 1.0)], texts=texts)
        segs = json.loads((self.session / r.speakers_json).read_text(encoding="utf-8"))["segments"]
        self.assertEqual(len(segs), 2)
        # turn start 2.0 - pad 0.15 = 1.85；加上 ASR 的 0.5 / 1.0
        self.assertAlmostEqual(segs[0]["start_ms"] / 1000, 1.85 + 0.5, places=2)
        self.assertAlmostEqual(segs[1]["start_ms"] / 1000, 1.85 + 1.0, places=2)

    def test_segment_end_clamped_into_turn_window(self):
        # ASR 回報超出該段音訊長度的 end，不能原封不動寫出去
        r = self.run_ok(turns=[(1.0, 3.0, 1, 1.0)], texts=[[(0.0, 999.0, "超長")]])
        segs = json.loads((self.session / r.speakers_json).read_text(encoding="utf-8"))["segments"]
        self.assertLessEqual(segs[0]["end_ms"] / 1000, 3.0 + 0.15 + 1e-6)

    def test_turn_past_audio_end_is_clamped_to_duration(self):
        # 分群回報的區間超出實際音長，不能去切不存在的音訊
        r = self.run_ok(turns=[(25.0, 40.0, 1, 1.0)], texts=[[(0.0, 999.0, "尾巴")]])
        segs = json.loads((self.session / r.speakers_json).read_text(encoding="utf-8"))["segments"]
        self.assertLessEqual(segs[0]["end_ms"] / 1000, WAV_SECONDS + 1e-6)

    def test_speakers_sorted_by_speech_time(self):
        texts = [[(0.0, 1.0, "甲一")], [(0.0, 3.0, "乙一句比較長")], [(0.0, 1.0, "甲二")]]
        r = self.run_ok(texts=texts)
        secs = [s["speech_seconds"] for s in r.speakers]
        self.assertEqual(secs, sorted(secs, reverse=True))

    def test_hallucination_lines_dropped_but_run_succeeds(self):
        texts = [[(0.0, 1.0, "感謝觀看")], [(0.0, 2.0, "這句是真的內容")],
                 [(0.0, 1.0, "以下是繁體中文的會議內容")]]
        r = self.run_ok(texts=texts)
        md = (self.session / r.speaker_transcript).read_text(encoding="utf-8")
        self.assertIn("這句是真的內容", md)
        self.assertNotIn("感謝觀看", md)
        self.assertNotIn("以下是繁體中文的會議內容", md)

    def test_request_is_not_mutated(self):
        # 以前用 object.__setattr__ 去改 frozen dataclass；現在必須是 replace
        req = self.req()
        diarize_session(req, diarizer=fake_diarizer([(0.0, 5.0, 1, 1.0)]), asr=fake_asr())
        self.assertIsNone(req.staging_dir)


class TestOpenCC(Base):
    def test_opencc_does_not_shift_speaker_labels(self):
        # OpenCC 不保長度（内存与网络 5 字 -> 記憶體與網路 6 字）。轉換在分群之後才做，
        # 而且逐段一一對應，所以代號不能跟著跑掉。
        from core.util import opencc_available
        if not opencc_available():
            self.skipTest("opencc 未安裝")
        texts = [[(0.0, 1.0, "内存与网络")], [(0.0, 1.0, "简体转换")]]
        r = diarize_session(self.req(opencc=True),
                            diarizer=fake_diarizer([(0.0, 3.0, 1, 1.0), (4.0, 7.0, 2, 1.0)]),
                            asr=fake_asr(texts_per_call=texts))
        segs = json.loads((self.session / r.speakers_json).read_text(encoding="utf-8"))["segments"]
        self.assertEqual([s["speaker_id"] for s in segs], ["S01", "S02"])
        self.assertNotEqual(segs[0]["text"], "内存与网络", "應已轉成繁體")


if __name__ == "__main__":
    unittest.main()
