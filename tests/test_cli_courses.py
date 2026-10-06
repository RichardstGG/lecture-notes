"""Public course-list projection used by the summary-method selector."""
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


class CourseSummaryDefaultsTests(unittest.TestCase):
    def test_json_reports_effective_enabled_after_local_and_course_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            courses = root / "courses"
            courses.mkdir()
            default = root / "default.toml"
            default.write_text(C.DEFAULT_FILE.read_text(encoding="utf-8"), encoding="utf-8")
            local = root / "local.toml"
            local.write_text('[summary]\nenabled = false\n', encoding="utf-8")
            (courses / "inherit.toml").write_text('[course]\nname = "只轉錄課程"\n', encoding="utf-8")
            (courses / "enabled.toml").write_text('[summary]\nenabled = true\n', encoding="utf-8")
            (courses / "invalid.toml").write_text('invalid = [', encoding="utf-8")
            output = io.StringIO()
            with mock.patch.multiple(C, COURSES_DIR=courses, DEFAULT_FILE=default,
                                     LOCAL_FILE=local, UPSTREAMS_FILE=root / "upstreams.toml"), \
                    contextlib.redirect_stdout(output):
                code = main(["courses", "--json"])
            data = {item["id"]: item for item in json.loads(output.getvalue())}
            self.assertEqual(code, 0)
            self.assertIs(data["inherit"]["summary_enabled"], False)
            self.assertIs(data["enabled"]["summary_enabled"], True)
            self.assertEqual(data["inherit"]["name"], "只轉錄課程")
            self.assertIn("model", data["enabled"])
            self.assertIn("upstream", data["enabled"])
            self.assertIn("error", data["invalid"])
            self.assertNotIn("summary_enabled", data["invalid"])
