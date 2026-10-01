"""CLI JSON and empty-session behavior."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401
from core.cli import main


class CliTermsTests(unittest.TestCase):
    def test_json_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            (session / "notes.jsonl").write_text(json.dumps({
                "label": "00:00:00", "terms": [
                    {"term": "大眾電容", "explain": "可能指元件", "asr_original": "", "verified": True},
                ],
            }), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["terms", directory, "--json"]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(set(result), {
                "schema_version", "session", "course", "course_id", "course_file",
                "whisper_prompt_base", "defined", "candidates",
            })
            self.assertEqual(set(result["candidates"][0]), {
                "term", "count", "sections", "explain", "asr_original",
                "verified", "flags", "variants",
            })
            self.assertEqual(result["candidates"][0]["flags"], ["hedged"])
            (session / "notes.jsonl").unlink()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["terms", directory, "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["candidates"], [])

    def test_course_override_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session"
            session.mkdir()
            (session / "notes.jsonl").write_text(json.dumps({
                "label": "00:00:00", "terms": [{"term": "雜訊", "explain": ""}],
            }), encoding="utf-8")
            course = Path(directory) / "course.toml"
            course.write_text('[summary]\nignored_terms = ["雜訊"]\n', encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["terms", str(session), "--course", str(course), "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["candidates"], [])
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as invalid:
                    main(["terms", str(session), "--similarity", "1.1"])
                with self.assertRaises(SystemExit) as missing:
                    main(["terms", str(session / "missing")])
            self.assertEqual(invalid.exception.code, 1)
            self.assertEqual(missing.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
