"""Contracts for test dispatch, local consent, and station reporting."""
import importlib.util
import json
import ctypes
from ctypes import wintypes
from pathlib import Path
import tempfile
import sys
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load(name):
    path = Path(__file__).resolve().parents[1] / "tools" / "windows_runner" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


station = load("station")
dispatch = load("dispatch")


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.control = Path(self.temp.name)

    def test_basic_needs_no_hardware_consent(self):
        station.require_window(self.control, "basic")

    def test_missing_corrupt_expired_and_wrong_profile_fail_closed(self):
        for value in (None, "broken", {}, {"schema_version": 1, "profiles": ["gpu"], "expires_at": 99},
                      {"schema_version": 1, "profiles": ["microphone"], "expires_at": 200},
                      {"schema_version": 1, "profiles": "not-gpu", "expires_at": 200},
                      {"schema_version": 1, "profiles": ["gpu"], "expires_at": float("inf")}):
            with self.subTest(value=value):
                if value is not None:
                    station.write_json(self.control / "window.json", value)
                with self.assertRaises(station.Blocked):
                    station.require_window(self.control, "gpu", now=100)

    def test_valid_window_can_be_revoked_and_expires(self):
        with patch.object(station.time, "time", return_value=100):
            station.set_window(self.control, ["gpu"], 1)
        station.require_window(self.control, "gpu", now=101)
        with self.assertRaises(station.Blocked):
            station.require_window(self.control, "gpu", now=160)
        (self.control / "window.json").unlink()
        with self.assertRaises(station.Blocked):
            station.require_window(self.control, "gpu", now=102)

    def test_no_unbounded_or_unrecognized_window(self):
        for minutes, profiles in ((0, ["gpu"]), (241, ["gpu"]), (1, ["basic"])):
            with self.assertRaises(ValueError):
                station.set_window(self.control, profiles, minutes)

    def test_revocation_terminates_only_launched_tree(self):
        with patch.object(station, "require_window", side_effect=[None, station.Blocked("test_window_closed")]), \
                patch.object(station.subprocess, "Popen") as popen, \
                patch.object(station.subprocess, "run") as kill:
            popen.return_value.poll.return_value = None
            popen.return_value.pid = 1234
            with self.assertRaises(station.Blocked):
                station.execute_step(["python", "lec"], self.control, self.control / "log",
                                     self.control, "microphone", 999)
            self.assertEqual(kill.call_args.args[0], ["taskkill", "/PID", "1234", "/T", "/F"])

    def test_execution_timeout_terminates_tree(self):
        with patch.object(station.subprocess, "Popen") as popen, \
                patch.object(station.subprocess, "run") as kill:
            popen.return_value.poll.return_value = None
            popen.return_value.pid = 42
            with self.assertRaises(TimeoutError):
                station.execute_step(["python"], self.control, self.control / "log",
                                     self.control, "basic", 0)
            self.assertEqual(kill.call_args.args[0][2], "42")


class RequestTests(unittest.TestCase):
    def test_dispatch_uses_trusted_workflow_and_literal_arguments(self):
        command = dispatch.dispatch_args("codex/example", "a" * 40, "basic", "quotes ' and $()", "request-1")
        self.assertEqual(command[command.index("--ref") + 1], "main")
        self.assertIn("reason=quotes ' and $()", command)
        self.assertIn("target_ref=codex/example", command)

    def test_rejects_fork_refs_and_unpinned_requests(self):
        for ref, sha in (("refs/pull/1/head", "a" * 40), ("main", "HEAD"), ("other/repo", "a" * 40)):
            with self.assertRaises(ValueError):
                dispatch.dispatch_args(ref, sha, "basic", "test", "request")

    def test_checkout_mismatch_and_private_config_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with patch.object(station.subprocess, "check_output", return_value="b" * 40):
                with self.assertRaisesRegex(station.Blocked, "sha_mismatch"):
                    station.assert_checkout(repo, "a" * 40)
            (repo / "config").mkdir()
            (repo / "config/local.toml").touch()
            with patch.object(station.subprocess, "check_output", return_value="a" * 40):
                with self.assertRaisesRegex(station.Blocked, "private_config"):
                    station.assert_checkout(repo, "a" * 40)

    def test_basic_has_no_hardware_commands(self):
        plan = station.commands(Path("repo"), "basic", {}, Path("run"))
        self.assertEqual([item[0] for item in plan], ["dependencies", "unittest", "cli_help"])
        self.assertEqual(plan[1][1][-3:], ["-t", ".", "-v"])

    def test_gpu_paths_are_isolated_and_arguments_preserve_spaces(self):
        config = {"whisper_dir": "C:/Test engines/whisper", "whisper_model": "C:/models/test.bin",
                  "input_audio": "C:/audio/test file.ogg"}
        command = station.commands(Path("repo"), "gpu", config, Path("test-run"))[0][1]
        self.assertIn(config["input_audio"], command)
        self.assertIn("--transcribe-only", command)
        self.assertIn("paths.state_dir=" + json.dumps(str(Path("test-run") / "state")), command)

    def test_actual_build_stamp_format(self):
        with tempfile.TemporaryDirectory() as temp:
            stamp = Path(temp) / "build/.lec-build"
            stamp.parent.mkdir()
            stamp.write_text('abcdef BACKEND=vulkan -DGGML_VULKAN=ON\n')
            station.require_vulkan({"whisper_dir": temp})
            stamp.write_text('abcdef BACKEND=cpu -DGGML_VULKAN=OFF\n')
            with self.assertRaises(station.Blocked):
                station.require_vulkan({"whisper_dir": temp})

    def test_failed_command_summary_and_lock_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = SimpleNamespace(request_id="request", sha="a" * 40, profile="basic",
                                   repo=root, report_dir=root / "public", timeout_seconds=30)
            fake_job = SimpleNamespace(contain_process_tree=lambda: 123)
            with patch.object(station, "control_dir", return_value=root / "private"), \
                    patch.object(station.sys, "platform", "win32"), \
                    patch.dict(sys.modules, {"windows_job": fake_job}), \
                    patch.object(station, "assert_checkout"), \
                    patch.object(station, "execute_step", return_value=7), patch("builtins.print"):
                self.assertEqual(station.run(args), 1)
            report = json.loads((root / "public/summary.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["steps"], [{"name": "dependencies", "exit_code": 7}])
            self.assertFalse((root / "private/station.lock").exists())

    def test_missing_window_never_launches_hardware(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = SimpleNamespace(request_id="request", sha="a" * 40, profile="microphone",
                                   repo=root, report_dir=root / "public", timeout_seconds=30)
            fake_job = SimpleNamespace(contain_process_tree=lambda: 123)
            with patch.object(station, "control_dir", return_value=root / "private"), \
                    patch.object(station.sys, "platform", "win32"), \
                    patch.dict(sys.modules, {"windows_job": fake_job}), \
                    patch.object(station, "assert_checkout"), \
                    patch.object(station, "execute_step") as execute, patch("builtins.print"):
                self.assertEqual(station.run(args), 2)
                execute.assert_not_called()
            report = json.loads((root / "public/summary.json").read_text())
            self.assertEqual(report["reason"], "test_window_closed")

    def test_interrupted_step_never_reports_pass_and_releases_lock(self):
        for results in ([KeyboardInterrupt()], [0, KeyboardInterrupt()]):
            with self.subTest(results=results), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                args = SimpleNamespace(request_id="request", sha="a" * 40, profile="basic",
                                       repo=root, report_dir=root / "public", timeout_seconds=30)
                fake_job = SimpleNamespace(contain_process_tree=lambda: 123)
                with patch.object(station, "control_dir", return_value=root / "private"), \
                        patch.object(station.sys, "platform", "win32"), \
                        patch.dict(sys.modules, {"windows_job": fake_job}), \
                        patch.object(station, "assert_checkout"), \
                        patch.object(station, "execute_step", side_effect=results), patch("builtins.print"):
                    self.assertEqual(station.run(args), 4)
                report = json.loads((root / "public/summary.json").read_text())
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["reason"], "execution_interrupted")
                self.assertNotIn("tests_run", report)
                self.assertFalse((root / "private/station.lock").exists())

    def test_basic_pass_requires_all_steps_and_counts_completed_tests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = SimpleNamespace(request_id="request", sha="a" * 40, profile="basic",
                                   repo=root, report_dir=root / "public", timeout_seconds=30)
            fake_job = SimpleNamespace(contain_process_tree=lambda: 123)

            def complete_step(command, repo, log, control, profile, deadline):
                log.write_text("Ran 3 tests in 0.1s\nOK (skipped=1)\n", encoding="utf-8")
                return 0

            with patch.object(station, "control_dir", return_value=root / "private"), \
                    patch.object(station.sys, "platform", "win32"), \
                    patch.dict(sys.modules, {"windows_job": fake_job}), \
                    patch.object(station, "assert_checkout"), \
                    patch.object(station, "execute_step", side_effect=complete_step), patch("builtins.print"):
                self.assertEqual(station.run(args), 0)
            report = json.loads((root / "public/summary.json").read_text())
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["steps"], [{"name": name, "exit_code": 0}
                                             for name in ("dependencies", "unittest", "cli_help")])
            self.assertEqual((report["tests_run"], report["tests_skipped"]), (3, 1))

    def test_existing_lock_blocks_reentry_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private = root / "private"
            private.mkdir()
            (private / "station.lock").write_text("123")
            args = SimpleNamespace(request_id="request", sha="a" * 40, profile="basic",
                                   repo=root, report_dir=root / "public", timeout_seconds=30)
            fake_job = SimpleNamespace(contain_process_tree=lambda: 123)
            with patch.object(station, "control_dir", return_value=private), \
                    patch.object(station.sys, "platform", "win32"), \
                    patch.dict(sys.modules, {"windows_job": fake_job}), \
                    patch.object(station, "execute_step") as execute, patch("builtins.print"):
                self.assertEqual(station.run(args), 2)
                execute.assert_not_called()
            report = json.loads((root / "public/summary.json").read_text())
            self.assertEqual(report["reason"], "station_busy_or_stale_lock")
            self.assertEqual((private / "station.lock").read_text(), "123")

    def test_non_windows_report_is_blocked_and_contains_no_local_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = SimpleNamespace(request_id="request", sha="a" * 40, profile="gpu",
                                   repo=root, report_dir=root / "public", timeout_seconds=30)
            with patch.object(station, "control_dir", return_value=root / "private"), \
                    patch.object(station.sys, "platform", "linux"), patch("builtins.print"):
                code = station.run(args)
            self.assertEqual(code, 2)
            report = json.loads((root / "public/summary.json").read_text())
            self.assertEqual(report["reason"], "native_windows_required")
            self.assertNotIn(str(root), json.dumps(report))


@unittest.skipUnless(sys.platform == "win32", "Native Windows process APIs required")
class NativeProcessTests(unittest.TestCase):
    """Real Windows processes; synthetic window fixtures never authorize hardware."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def assert_pid_exited(self, pid):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            self.assertEqual(ctypes.get_last_error(), 87)  # PID no longer exists
            return
        try:
            result = kernel.WaitForSingleObject(handle, 5000)
            if result != 0:
                # The live handle keeps this test-owned process identity stable.
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            self.assertEqual(result, 0)
        finally:
            kernel.CloseHandle(handle)

    def tree_command(self):
        marker = self.root / "tree.json"
        marker.unlink(missing_ok=True)
        code = (
            "import json, os, subprocess, sys, time; from pathlib import Path; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "creationflags=subprocess.CREATE_NO_WINDOW); "
            "Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid])); time.sleep(60)"
        )
        return [sys.executable, "-c", code, str(marker)], marker

    def test_steps_do_not_share_listener_console(self):
        code = "import ctypes; assert ctypes.windll.kernel32.GetConsoleWindow() == 0"
        result = station.execute_step([sys.executable, "-c", code], self.root,
                                      self.root / "console.log", self.root, "basic",
                                      time.monotonic() + 10)
        self.assertEqual(result, 0)

    def test_timeout_terminates_real_child_and_grandchild(self):
        command, marker = self.tree_command()
        with self.assertRaises(TimeoutError):
            station.execute_step(command, self.root, self.root / "timeout.log",
                                 self.root, "basic", time.monotonic() + 3)
        self.assertTrue(marker.exists(), "Tree did not start before the deadline")
        for pid in json.loads(marker.read_text()):
            self.assert_pid_exited(pid)

    def test_fixture_revocation_and_expiry_terminate_real_tree(self):
        for revoke in (True, False):
            with self.subTest(revoke=revoke):
                command, marker = self.tree_command()
                station.write_json(self.root / "window.json", {
                    "schema_version": 1, "profiles": ["gpu"], "expires_at": time.time() + 3,
                })
                timer = threading.Timer(2, lambda: (self.root / "window.json").unlink(missing_ok=True))
                if revoke:
                    timer.start()
                try:
                    with self.assertRaisesRegex(station.Blocked, "test_window_closed"):
                        station.execute_step(command, self.root, self.root / "revocation.log",
                                             self.root, "gpu", time.monotonic() + 10)
                finally:
                    if revoke:
                        timer.cancel()
                        timer.join()
                self.assertTrue(marker.exists(), "Tree did not start before fixture revocation")
                for pid in json.loads(marker.read_text()):
                    self.assert_pid_exited(pid)

    def test_job_object_cleans_descendant_on_exit_and_termination(self):
        job_dir = Path(__file__).resolve().parents[1] / "tools/windows_runner"
        for terminate in (False, True):
            with self.subTest(terminate=terminate):
                marker = self.root / "job-child.txt"
                marker.unlink(missing_ok=True)
                code = (
                    "import subprocess, sys, time; from pathlib import Path; "
                    "sys.path.insert(0, sys.argv[1]); from windows_job import contain_process_tree; "
                    "handle = contain_process_tree(); "
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                    "creationflags=subprocess.CREATE_NO_WINDOW); "
                    "marker = Path(sys.argv[2]); temporary = marker.with_suffix('.tmp'); "
                    "temporary.write_text(str(child.pid)); temporary.replace(marker); "
                    "time.sleep(60 if sys.argv[3] == 'True' else 0)"
                )
                worker = subprocess.Popen([sys.executable, "-c", code, str(job_dir), str(marker), str(terminate)],
                                          creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    deadline = time.monotonic() + 10
                    while not marker.exists() and worker.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(marker.exists(), "Contained worker did not start")
                    if terminate:
                        worker.kill()  # Only the worker created by this test
                    worker.wait(timeout=10)
                    if not terminate:
                        self.assertEqual(worker.returncode, 0)
                    self.assert_pid_exited(int(marker.read_text()))
                finally:
                    if worker.poll() is None:
                        worker.kill()
                    worker.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
