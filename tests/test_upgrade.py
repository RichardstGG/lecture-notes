"""Tests for the cross-platform one-command upgrade workflow."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _pathfix  # noqa: F401
import upgrade as U


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        (self.root / "ui/backend").mkdir(parents=True)
        (self.root / "ui/frontend").mkdir(parents=True)
        (self.root / "ui/backend/requirements.txt").write_text("", encoding="utf-8")
        self.root_patch = mock.patch.object(U, "ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def completed(self, stdout=""):
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    def test_update_checkout_uses_fast_forward_pull(self):
        with mock.patch.object(
            U, "git_output", side_effect=["main", "", "old", "new"],
        ), mock.patch.object(U, "run", return_value=self.completed()) as run:
            changed = U.update_checkout()

        self.assertTrue(changed)
        run.assert_called_once_with(["git", "-C", self.root, "pull", "--ff-only"])

    def test_update_checkout_refuses_tracked_changes(self):
        with mock.patch.object(
            U, "git_output", side_effect=["main", " M README.md"],
        ), self.assertRaises(U.UpgradeError) as error:
            U.update_checkout()
        self.assertIn("未提交修改", str(error.exception))

    def test_whisper_only_forwards_engine_options(self):
        args = U.parse_args([
            "--skip-pull", "--skip-ui", "--whisper-only",
            "--backend", "cpu", "--generator", "Ninja", "--rebuild",
        ])
        with mock.patch.object(U, "ensure_no_active_run"), \
                mock.patch.object(U, "run", return_value=self.completed()) as run:
            U.perform_upgrade(args)

        run.assert_called_once_with([
            sys.executable, self.root / "setup_engines.py",
            "--backend", "cpu", "--generator", "Ninja", "--rebuild", "whisper",
        ])

    def test_default_upgrade_includes_ui_install_and_build(self):
        args = U.parse_args(["--skip-pull"])
        with mock.patch.object(U, "ensure_no_active_run") as ensure_no_active_run, \
                mock.patch.object(U, "run", return_value=self.completed()) as run, \
                mock.patch.object(U, "install_ui") as install_ui:
            U.perform_upgrade(args)

        ensure_no_active_run.assert_called_once_with()
        run.assert_called_once_with(U.engine_command(args))
        install_ui.assert_called_once_with()

    def test_skip_engines_installs_ui_only(self):
        args = U.parse_args(["--skip-pull", "--skip-engines"])
        with mock.patch.object(U, "ensure_no_active_run") as ensure_no_active_run, \
                mock.patch.object(U, "run") as run, \
                mock.patch.object(U, "install_ui") as install_ui:
            U.perform_upgrade(args)

        ensure_no_active_run.assert_not_called()
        run.assert_not_called()
        install_ui.assert_called_once_with()

    def test_active_run_blocks_upgrade_without_stopping_it(self):
        status = self.completed('{"running": true, "course": "作業系統"}')
        with mock.patch.object(U, "run", return_value=status) as run, \
                self.assertRaises(U.UpgradeError) as error:
            U.ensure_no_active_run()

        self.assertIn("作業系統", str(error.exception))
        run.assert_called_once_with(
            [sys.executable, self.root / "lec", "status", "--json"], capture=True,
        )

    def test_ui_install_uses_venv_and_clean_frontend_install(self):
        python = self.root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        calls = []

        def record(command, **kwargs):
            calls.append((command, kwargs))
            return self.completed()

        with mock.patch.object(U, "run", side_effect=record), \
                mock.patch.object(U.shutil, "which", return_value="/usr/bin/npm"):
            U.install_ui()

        self.assertEqual(calls, [
            ([python, "-m", "pip", "install", "-r",
              self.root / "ui/backend/requirements.txt"], {}),
            (["/usr/bin/npm", "ci"], {"cwd": self.root / "ui/frontend"}),
            (["/usr/bin/npm", "run", "build"], {"cwd": self.root / "ui/frontend"}),
        ])

    def test_windows_venv_python_path(self):
        with mock.patch.object(U.os, "name", "nt"):
            self.assertEqual(U.venv_python(), self.root / ".venv/Scripts/python.exe")

    def test_main_restarts_updated_script_before_building(self):
        with mock.patch.object(U, "update_checkout", return_value=True), \
                mock.patch.object(U, "perform_upgrade") as perform, \
                mock.patch.object(U, "run", return_value=self.completed()) as run:
            code = U.main(["--whisper-only"])

        self.assertEqual(code, 0)
        perform.assert_not_called()
        run.assert_called_once_with([
            sys.executable, self.root / "upgrade.py", "--skip-pull", "--whisper-only",
        ])


if __name__ == "__main__":
    unittest.main()
