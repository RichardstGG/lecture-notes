"""Tests for the explicit lec run transcribe-only flag."""
import unittest
from unittest import mock

from tests import _pathfix  # noqa: F401
from core import cli
from core import config as C


class TranscribeOnlyTests(unittest.TestCase):
    def test_run_flag_disables_summary_in_effective_config(self):
        with mock.patch("core.session.LectureRun") as lecture_run:
            lecture_run.return_value.run.return_value = 0
            code = cli.main(["run", str(C.DEFAULT_FILE), "--transcribe-only"])

        self.assertEqual(code, 0)
        effective = lecture_run.call_args.args[0]
        self.assertFalse(effective["summary"]["enabled"])

    def test_run_help_documents_flag(self):
        with mock.patch("sys.stdout") as stdout:
            with self.assertRaises(SystemExit) as exit_info:
                cli.main(["run", "--help"])
        self.assertEqual(exit_info.exception.code, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("--transcribe-only", output)
        self.assertIn("summary.enabled=false", output)


if __name__ == "__main__":
    unittest.main()
