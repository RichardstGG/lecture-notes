"""Tests for the cross-platform one-command upgrade workflow."""
import contextlib
import io
import os
import re
import shutil
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

    def test_detached_head_explains_both_ways_forward_without_pulling(self):
        with mock.patch.object(U, "git_output", side_effect=[""]), \
                mock.patch.object(U, "run") as run, \
                self.assertRaises(U.UpgradeError) as error:
            U.update_checkout()
        message = str(error.exception)
        self.assertIn("detached HEAD", message)
        self.assertIn("git switch main", message)
        self.assertIn("--skip-pull", message)
        run.assert_not_called()

    def test_skip_pull_is_documented_in_help(self):
        # 錯誤訊息會叫使用者用 --skip-pull，所以它不能是隱藏選項。
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            U.parse_args(["--help"])
        self.assertIn("--skip-pull", out.getvalue())

    def test_update_checkout_refuses_tracked_changes(self):
        with mock.patch.object(
            U, "git_output", side_effect=["main", " M README.md"],
        ), self.assertRaises(U.UpgradeError) as error:
            U.update_checkout()
        self.assertIn("未提交修改", str(error.exception))

    def test_whisper_only_forwards_engine_options(self):
        args = U.parse_args([
            "--skip-pull", "--skip-ui", "--skip-diarization", "--whisper-only",
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
                mock.patch.object(U, "install_diarization") as install_diarization, \
                mock.patch.object(U, "install_ui") as install_ui:
            U.perform_upgrade(args)

        ensure_no_active_run.assert_called_once_with()
        run.assert_called_once_with(U.engine_command(args))
        install_diarization.assert_called_once_with()
        install_ui.assert_called_once_with()

    def test_skip_engines_installs_ui_only(self):
        args = U.parse_args(["--skip-pull", "--skip-engines", "--skip-diarization"])
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
        # venv 的 python 在 POSIX 是 bin/python，在 Windows 是 Scripts/python.exe。fixture 要建在
        # venv_python() 回傳的位置，否則 ensure_venv 會多執行一次建立 venv。兩種版面都跑。
        for relative in (Path("bin/python"), Path("Scripts/python.exe")):
            with self.subTest(layout=str(relative)), \
                    mock.patch.object(U, "venv_python", lambda r=relative: self.root / ".venv" / r):
                python = U.venv_python()
                python.parent.mkdir(parents=True, exist_ok=True)
                python.write_text("", encoding="utf-8")
                calls = []

                def record(command, **kwargs):
                    calls.append((command, kwargs))
                    return self.completed()

                with mock.patch.object(U, "run", side_effect=record), \
                        mock.patch.object(U, "venv_ready", return_value=True), \
                        mock.patch.object(U.shutil, "which",
                                          side_effect=lambda n: "/usr/bin/npm" if n == "npm" else None):
                    U.install_ui()

                self.assertEqual(calls, [
                    ([python, "-m", "pip", "install", "-r",
                      self.root / "ui/backend/requirements.txt",
                      "-c", self.root / "requirements.lock"], {}),
                    (["/usr/bin/npm", "ci"], {"cwd": self.root / "ui/frontend"}),
                    (["/usr/bin/npm", "run", "build"], {"cwd": self.root / "ui/frontend"}),
                ])

    def ensure_venv(self, uv=None):
        with mock.patch.object(U, "run", return_value=self.completed()) as run, \
                mock.patch.object(U.shutil, "which", return_value=uv):
            U.ensure_venv()
        return [call.args[0] for call in run.call_args_list]

    def test_missing_venv_uses_uv_managed_python(self):
        self.assertEqual(self.ensure_venv("/bin/uv"), [[
            "/bin/uv", "venv", "--managed-python", "--python", U.UV_PYTHON,
            "--seed", "--clear", self.root / ".venv",
        ]])

    def test_missing_venv_falls_back_to_stdlib_venv_with_clear(self):
        self.assertEqual(self.ensure_venv(), [
            [sys.executable, "-m", "venv", "--clear", self.root / ".venv"],
        ])

    def test_broken_venv_is_rebuilt_after_checking_for_active_run(self):
        (self.root / ".venv").mkdir()
        with mock.patch.object(U, "ensure_no_active_run") as guard:
            calls = self.ensure_venv()
        guard.assert_called_once_with()
        self.assertEqual(calls, [[sys.executable, "-m", "venv", "--clear", self.root / ".venv"]])

    def test_active_run_blocks_venv_rebuild(self):
        (self.root / ".venv").mkdir()
        error = U.UpgradeError("仍在執行")
        with mock.patch.object(U, "ensure_no_active_run", side_effect=error), \
                self.assertRaises(U.UpgradeError):
            self.ensure_venv()

    def test_venv_ready_rejects_interpreter_that_cannot_start(self):
        self.assertFalse(U.venv_ready(self.root / ".venv/bin/python", None))

    def test_venv_ready_requires_pinned_uv_venv_only_when_uv_exists(self):
        config = self.root / ".venv/pyvenv.cfg"
        config.parent.mkdir()
        cases = [
            ("uv = 0.12.16\nversion_info = 3.13\n", "/bin/uv", True),
            ("version_info = 3.13.5\nuv = 0.12.16\n", "/bin/uv", True),
            ("uv = 0.12.16\nversion_info = 3.12\n", "/bin/uv", False),
            ("uv = 0.12.16\nversion_info = 3.130\n", "/bin/uv", False),
            ("home = /opt/homebrew/bin\nversion = 3.13.5\n", "/bin/uv", False),
            ("home = /opt/homebrew/bin\nversion = 3.13.5\n", None, True),
        ]
        for text, uv, expected in cases:
            with self.subTest(text=text, uv=uv), \
                    mock.patch.object(U.subprocess, "run", return_value=self.completed()):
                config.write_text(text, encoding="utf-8")
                self.assertEqual(U.venv_ready("python", uv), expected)

    def test_pip_install_uses_uv_with_the_lock_when_available(self):
        with mock.patch.object(U, "run") as run, \
                mock.patch.object(U.shutil, "which", return_value="/bin/uv"):
            U.pip_install("python", "reqs.txt")
        run.assert_called_once_with([
            "/bin/uv", "pip", "install", "--python", "python",
            "-r", "reqs.txt", "-c", self.root / "requirements.lock",
        ])

    def test_lock_covers_every_direct_requirement(self):
        repo = Path(U.__file__).resolve().parent
        lock = (repo / "requirements.lock").read_text(encoding="utf-8")
        for name in ("requirements-diarize.txt", "ui/backend/requirements.txt"):
            for line in (repo / name).read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("#"):
                    package = re.match(r"[A-Za-z0-9_.-]+", line)[0]
                    self.assertIn(line if "==" in line else f"{package}==", lock)

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


class LauncherTests(unittest.TestCase):
    def test_launcher_forwards_arguments_and_exit_code(self):
        name = "upgrade.cmd" if os.name == "nt" else "upgrade"
        with tempfile.TemporaryDirectory() as tmp:
            # 路徑含空白：Windows 使用者資料夾常見，入口要正確加引號。
            folder = Path(tmp) / "a dir"
            folder.mkdir()
            shutil.copy2(Path(U.__file__).with_name(name), folder)
            (folder / "upgrade.py").write_text(
                "import sys\nprint(sys.argv[1:])\nsys.exit(7)\n", encoding="utf-8",
            )
            result = subprocess.run(
                [str(folder / name), "--skip-ui", "a b"], capture_output=True, text=True,
            )

        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("['--skip-ui', 'a b']", result.stdout)


if __name__ == "__main__":
    unittest.main()
