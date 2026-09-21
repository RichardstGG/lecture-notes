"""Session directory naming and nesting contract tests."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core.config import ConfigError
from core.session import make_session_dir


FIXED_NOW = datetime(2026, 9, 21, 14, 30)


class FakeConfig:
    def __init__(self, output_root, session_name, course_name="UNIXops"):
        self.data = {"paths": {
            "output_root": str(output_root),
            "session_name": session_name,
        }}
        self.course_name = course_name

    def __getitem__(self, key):
        return self.data[key]

    @staticmethod
    def path(value):
        return Path(value)


class SessionPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "outputs"

    def tearDown(self):
        self.tmp.cleanup()

    def make_dir(self, template, course="UNIXops"):
        cfg = FakeConfig(self.output, template, course)
        with patch("core.session.datetime") as clock:
            clock.now.return_value = FIXED_NOW
            return make_session_dir(cfg)

    def test_default_template_keeps_flat_layout(self):
        path = self.make_dir("{course}_{date:%Y%m%d}")

        self.assertEqual(path, self.output / "UNIXops_20260921")
        self.assertTrue(path.is_dir())

    def test_forward_slash_creates_nested_layout(self):
        path = self.make_dir("{course}/{date:%Y%m%d}")

        self.assertEqual(path, self.output / "UNIXops" / "20260921")

    def test_nonempty_collision_suffixes_only_the_last_component(self):
        first = self.make_dir("{course}/{date:%Y%m%d}")
        (first / "transcript.md").write_text("started", encoding="utf-8")

        second = self.make_dir("{course}/{date:%Y%m%d}")

        self.assertEqual(second, self.output / "UNIXops" / "20260921_1430")

    def test_empty_existing_directory_is_reused(self):
        existing = self.output / "UNIXops" / "20260921"
        existing.mkdir(parents=True)

        path = self.make_dir("{course}/{date:%Y%m%d}")

        self.assertEqual(path, existing)

    def test_course_separator_is_sanitized_before_formatting(self):
        path = self.make_dir("{course}/{date:%Y%m%d}", course="A/B")

        self.assertEqual(path, self.output / "A_B" / "20260921")

    def test_unsafe_and_empty_components_cannot_escape_output_root(self):
        for template, expected in (
            ("/../nested/./{date:%Y%m%d}", self.output / "nested" / "20260921"),
            ("/.././", self.output / "session"),
        ):
            with self.subTest(template=template):
                path = self.make_dir(template)
                self.assertEqual(path, expected)
                self.assertTrue(path.resolve().is_relative_to(self.output.resolve()))

    def test_backslash_creates_nested_layout(self):
        path = self.make_dir(r"{course}\{date:%Y%m%d}")

        self.assertEqual(path, self.output / "UNIXops" / "20260921")

    def test_unknown_format_field_raises_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            self.make_dir("{foo}/{date:%Y%m%d}")

        self.assertIn("paths.session_name", str(ctx.exception))
        self.assertIn("可用 / 分層", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
