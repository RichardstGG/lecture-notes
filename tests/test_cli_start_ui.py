"""Tests for launching the local Web UI through the lec entry point."""
import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _pathfix  # noqa: F401
from core import cli


class StartUiTests(unittest.TestCase):
    def test_top_level_flag_dispatches_port(self):
        with mock.patch.object(cli, "cmd_start_ui", return_value=0) as start:
            code = cli.main(["--start-ui", "--port", "9876"])

        self.assertEqual(code, 0)
        args = start.call_args.args[0]
        self.assertTrue(args.start_ui)
        self.assertEqual(args.port, 9876)

    def test_ui_port_rejects_out_of_range_values(self):
        for value in ("0", "65536", "not-a-number"):
            with self.subTest(value=value), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit) as exit_info:
                    cli.main(["--start-ui", "--port", value])
            self.assertEqual(exit_info.exception.code, 2)

    def test_start_ui_cannot_be_combined_with_subcommand(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as exit_info:
            cli.main(["--start-ui", "status"])
        self.assertEqual(exit_info.exception.code, 2)

    def test_existing_project_venv_replaces_current_process(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            args = argparse.Namespace(port=9876)
            with mock.patch.object(cli, "_project_venv_python", return_value=python), \
                    mock.patch.object(cli.os, "execv") as execv:
                code = cli.cmd_start_ui(args)

        self.assertEqual(code, 0)
        execv.assert_called_once_with(
            str(python),
            [str(python), str(cli.C.APP_ROOT / "lec"), "--start-ui", "--port", "9876"],
        )

    def test_active_project_python_runs_backend_in_process(self):
        args = argparse.Namespace(port=None)
        python = Path(sys.prefix) / "bin/python"
        with mock.patch.object(
            cli, "_project_venv_python", return_value=python,
        ), mock.patch("ui.backend.__main__.main", return_value=None) as backend:
            code = cli.cmd_start_ui(args)

        self.assertEqual(code, 0)
        backend.assert_called_once_with(["--port", "8765"])

    def test_help_documents_ui_launcher(self):
        with mock.patch("sys.stdout") as stdout, self.assertRaises(SystemExit) as exit_info:
            cli.main(["--help"])
        self.assertEqual(exit_info.exception.code, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("--start-ui", output)
        self.assertIn("--port PORT", output)


if __name__ == "__main__":
    unittest.main()
