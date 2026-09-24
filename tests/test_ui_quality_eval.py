"""Tests for the public-sample quality snapshot."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests import _pathfix  # noqa: F401
from ui.quality_eval import ROOT, SAMPLE, evaluate, main, measure, normalized, read_entries


class QualityEvalTests(unittest.TestCase):
    def test_normalized_handles_case_spacing_and_width_without_rewriting_words(self):
        self.assertEqual(normalized("ＦＯＲＫ、檔案 描述子"), "fork檔案描述子")
        self.assertNotEqual(normalized("MUTIX"), normalized("Multics"))

    def test_redone_summary_uses_latest_entry_for_same_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in (
                {"label": "00:00:00", "status": "failed"},
                {"label": "00:00:00", "status": "ok"},
            )), encoding="utf-8")
            self.assertEqual(read_entries(path), [{"label": "00:00:00", "status": "ok"}])

    def test_measure_separates_asr_terms_note_terms_and_script_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "transcript.md").write_text(
                "## 00:00:00\n老師提到 MUTIX 和 fork。期中考一定會考。\n",
                encoding="utf-8",
            )
            (root / "notes.jsonl").write_text(json.dumps({
                "label": "00:00:00", "status": "ok", "elapsed": 2.5,
                "topic": "Multics", "points": [],
                "terms": [
                    {"term": "Multics", "asr_original": "MUTIX", "verified": True},
                    {"term": "MUTIX", "asr_original": "", "verified": True},
                ],
                "emphasis": [],
                "verify": {"emphasis_dropped": [], "terms_unverified": []},
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            result = measure(root, "今天講 Multics 和 fork。期中考一定會考。")

        self.assertEqual(result["sections"], ["00:00:00"])
        self.assertEqual(result["summary_status"]["ok"], 1)
        self.assertFalse(result["terms"]["Multics"]["transcript"])
        self.assertEqual(result["terms"]["Multics"]["script_count"], 1)
        self.assertTrue(result["terms"]["Multics"]["note_term"])
        self.assertEqual(result["terms"]["Multics"]["note_term_count"], 1)
        self.assertIn("MUTIX", result["verified_note_terms_absent_from_script"])
        self.assertTrue(result["emphasis"]["期中考一定會考"]["transcript"])

    def test_public_reference_is_readable_without_models(self):
        script = (SAMPLE / "test8min.txt").read_text(encoding="utf-8")
        result = measure(SAMPLE / "expected", script)
        self.assertEqual(len(result["sections"]), 2)
        self.assertEqual(result["summary_status"]["ok"], 2)
        self.assertGreater(result["srt_cues"], 0)
        self.assertIn("MUTIX", result["verified_note_terms_absent_from_script"])

    def test_report_schema_and_config_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "config.used.toml").write_bytes(
                (ROOT / "config" / "default.toml").read_bytes()
            )
            (session / "transcript.md").write_text("## 00:00:00\nUNIX。\n", encoding="utf-8")
            report = evaluate(session)

        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["settings"]["whisper_model"], "large-v3-turbo")
        self.assertEqual(len(report["config_sha256"]), 64)
        self.assertEqual(report["current"]["sections"], ["00:00:00"])
        self.assertEqual(report["current"]["summary_status"]["ok"], 0)
        self.assertIsNone(report["audio_matches_sample"])
        self.assertNotIn("baseline", report)

    def test_baseline_option_reports_only_changed_settings_and_term_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            candidate = root / "candidate"
            baseline.mkdir()
            candidate.mkdir()
            default = (ROOT / "config" / "default.toml").read_text(encoding="utf-8")
            (baseline / "config.used.toml").write_text(default, encoding="utf-8")
            (candidate / "config.used.toml").write_text(
                default.replace("terms = []", 'terms = ["PID"]', 1), encoding="utf-8",
            )
            (baseline / "transcript.md").write_text(
                "## 00:00:00\n老師說 BID、PID 和負行程。\n", encoding="utf-8",
            )
            (candidate / "transcript.md").write_text(
                "## 00:00:00\n老師說 PID、PID 和父行程。\n", encoding="utf-8",
            )
            other_audio = root / "other.ogg"
            other_audio.write_bytes(b"different")
            (baseline / "status.json").write_text(json.dumps({
                "input_file": str(SAMPLE / "test8min.ogg"),
            }), encoding="utf-8")
            (candidate / "status.json").write_text(json.dumps({
                "input_file": str(other_audio),
            }), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                main([str(candidate), "--baseline", str(baseline)])
            report = json.loads(output.getvalue())

        self.assertEqual(list(report["settings_delta"]), ["whisper_terms"])
        self.assertEqual(report["settings_delta"]["whisper_terms"]["current"], ["PID"])
        self.assertEqual(report["term_changes"]["PID"]["baseline_transcript"], True)
        self.assertEqual(report["term_changes"]["PID"]["current_transcript"], True)
        self.assertEqual(report["term_changes"]["PID"]["baseline_transcript_count"], 1)
        self.assertEqual(report["term_changes"]["PID"]["current_transcript_count"], 2)
        self.assertEqual(report["term_changes"]["父行程"]["current_transcript"], True)
        self.assertEqual(len(report["baseline_config_sha256"]), 64)
        self.assertTrue(report["baseline_audio_matches_sample"])
        self.assertFalse(report["audio_matches_sample"])


if __name__ == "__main__":
    unittest.main()
