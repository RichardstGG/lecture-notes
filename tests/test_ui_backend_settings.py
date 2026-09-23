"""Tests for local UI service environment settings."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from ui.backend.settings import BackendSettings


class BackendSettingsTests(unittest.TestCase):
    def test_upload_settings_accept_relative_root_and_gigabyte_limit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "LECTURE_NOTES_ROOT": tmp,
            "LECTURE_NOTES_UI_UPLOAD_ROOT": "private/uploads",
            "LECTURE_NOTES_UI_MAX_UPLOAD_GB": "1.5",
        }, clear=True):
            settings = BackendSettings.from_env()

        self.assertEqual(
            settings.upload_root, (Path(tmp) / "private" / "uploads").resolve(),
        )
        self.assertEqual(settings.max_upload_bytes, int(1.5 * 1024 ** 3))

    def test_upload_limit_must_be_positive(self):
        with patch.dict("os.environ", {
            "LECTURE_NOTES_UI_MAX_UPLOAD_GB": "0",
        }, clear=True), self.assertRaisesRegex(ValueError, "MAX_UPLOAD_GB"):
            BackendSettings.from_env()


if __name__ == "__main__":
    unittest.main()
