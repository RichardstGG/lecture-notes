"""Contract tests for model discovery through the public CLI."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _pathfix  # noqa: F401
from core import config as C
from core.cli import main


class ModelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.summary_path = self.root / "summary.gguf"
        self.summary_path.write_bytes(b"model")
        self.whisper_path = self.root / "whisper.bin"
        self.cfg = C.Config({
            "summary": {"model": "installed-summary"},
            "models": {
                "installed-summary": {
                    "path": str(self.summary_path), "disable_thinking": True,
                },
                "missing-summary": {"path": str(self.root / "missing.gguf")},
            },
            "whisper": {
                "model": "installed-whisper",
                "models": {
                    "installed-whisper": {"path": str(self.whisper_path)},
                },
            },
        })

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        output = io.StringIO()
        with mock.patch.object(C, "load", return_value=(self.cfg, False)) as load, \
                contextlib.redirect_stdout(output):
            code = main(list(args))
        return code, output.getvalue(), load

    def test_json_contract_reports_defaults_availability_and_size(self):
        code, output, load = self.run_cli("models", "測試課", "--json")
        data = json.loads(output)

        self.assertEqual(code, 0)
        load.assert_called_once_with("測試課")
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["summary"]["selected"], "installed-summary")
        self.assertEqual(data["summary"]["models"][0], {
            "id": "installed-summary",
            "path": str(self.summary_path),
            "available": True,
            "size_bytes": 5,
            "disable_thinking": True,
        })
        self.assertFalse(data["summary"]["models"][1]["available"])
        self.assertNotIn("size_bytes", data["summary"]["models"][1])
        self.assertEqual(data["whisper"]["selected"], "installed-whisper")

    def test_text_output_marks_selected_and_missing_models(self):
        code, output, _load = self.run_cli("models")
        self.assertEqual(code, 0)
        self.assertIn("總結模型：", output)
        self.assertIn("* installed-summary", output)
        self.assertIn("✔ 已安裝", output)
        self.assertIn("missing-summary", output)
        self.assertIn("✖ 未安裝", output)
        self.assertIn("Whisper 模型：", output)


if __name__ == "__main__":
    unittest.main()
