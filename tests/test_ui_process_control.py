"""Tests for background CLI launching and structured configuration overrides."""
import asyncio
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from ui.backend.cli_client import LecCommandError
from ui.backend.process_control import (ControlError, LecProcessLauncher,
                                        _is_descendant, _override_args, _process_parents, _toml_value)


class OverrideTests(unittest.TestCase):
    def test_toml_values_cover_supported_json_types(self):
        self.assertEqual(_toml_value(True), "true")
        self.assertEqual(_toml_value(3), "3")
        self.assertEqual(_toml_value(0.25), "0.25")
        self.assertEqual(_toml_value("中文"), '"中文"')
        self.assertEqual(_toml_value(["a", 2, False]), '["a", 2, false]')

    def test_override_arguments_are_sorted_and_dotted(self):
        self.assertEqual(_override_args({"z.value": 1, "a.value": "x"}), [
            "--set", 'a.value="x"', "--set", "z.value=1",
        ])
        with self.assertRaises(ControlError) as ctx:
            _override_args({"invalid": 1})
        self.assertEqual(ctx.exception.code, "invalid_override")

    def test_nested_objects_and_nonfinite_numbers_are_rejected(self):
        for value in ({"nested": True}, float("nan")):
            with self.subTest(value=value), self.assertRaises(ControlError):
                _override_args({"summary.value": value})


class ProcessAncestryTests(unittest.TestCase):
    def test_posix_snapshot_parses_pid_and_parent_columns(self):
        with patch('ui.backend.process_control.os.name', 'posix'), \
                patch('ui.backend.process_control.subprocess.run',
                      return_value=subprocess.CompletedProcess([], 0, ' 10 1\n20 10\n')) as run:
            self.assertEqual(_process_parents(), {10: 1, 20: 10})
        run.assert_called_once_with(
            ['ps', '-A', '-o', 'pid=,ppid='], check=True,
            capture_output=True, text=True, timeout=2)

    def test_only_descendants_are_accepted_and_cycles_terminate(self):
        with patch('ui.backend.process_control._process_parents',
                   return_value={20: 10, 30: 20, 40: 50, 50: 40}):
            self.assertTrue(_is_descendant(30, 10))
            for pid in (40, 999, -1, None, '30', True):
                with self.subTest(pid=pid):
                    self.assertFalse(_is_descendant(pid, 10))

    def test_snapshot_failure_does_not_confirm_startup(self):
        with patch('ui.backend.process_control._process_parents', side_effect=OSError):
            self.assertFalse(_is_descendant(30, 10))


class LauncherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "fake_lec.py"
        self.log = self.root / "process.log"
        self.launchers = []

    async def asyncTearDown(self):
        for launcher in self.launchers:
            for process in tuple(launcher._processes):
                process.wait(timeout=2)
        await asyncio.sleep(0)

    def tearDown(self):
        self.tmp.cleanup()

    def launcher(self, body, settle=0.02):
        self.script.write_text(textwrap.dedent(body), encoding="utf-8")
        launcher = LecProcessLauncher(
            (sys.executable, self.script), self.root, self.log,
            settle_seconds=settle,
        )
        self.launchers.append(launcher)
        return launcher

    async def test_start_returns_before_background_process_finishes(self):
        launcher = self.launcher("""
            import time
            print("background started", flush=True)
            time.sleep(0.2)
            print("background finished", flush=True)
        """)
        started = time.monotonic()
        result = await launcher.start("run", "course")
        elapsed = time.monotonic() - started

        self.assertGreater(result.pid, 0)
        self.assertFalse(result.completed)
        self.assertLess(elapsed, 0.15)
        await asyncio.sleep(0.3)
        self.assertIn("background finished", self.log.read_text(encoding="utf-8"))

    async def test_immediate_failure_returns_log_diagnostics(self):
        launcher = self.launcher("""
            import sys
            print("startup failed", file=sys.stderr, flush=True)
            raise SystemExit(7)
        """, settle=0.15)
        with self.assertRaises(LecCommandError) as ctx:
            await launcher.start("run", "course")
        self.assertEqual(ctx.exception.code, "cli_start_failed")
        self.assertEqual(ctx.exception.exit_code, 7)
        self.assertIn("startup failed", ctx.exception.stderr)

    async def test_immediate_success_is_completed_not_failed(self):
        launcher = self.launcher("""
            print("nothing to do", flush=True)
        """, settle=0.15)
        result = await launcher.start("summarize", "session")
        self.assertGreater(result.pid, 0)
        self.assertTrue(result.completed)
        self.assertEqual(result.exit_code, 0)

    async def test_meeting_fast_completion_uses_the_single_envelope(self):
        launcher = self.launcher("""
            import json
            print(json.dumps(dict(schema_version=1, accepted=True, session="/output/meeting", work_type="meeting")))
        """)
        async def idle():
            return {"running": False}
        result = await launcher.start("meeting", "run", "team", "--json", wait_for_session=idle)
        self.assertTrue(result.completed)
        self.assertEqual(result.session, "/output/meeting")

    async def test_meeting_never_accepts_another_pid_and_timeout_keeps_child_alive(self):
        from ui.backend.process_control import ControlError
        marker = self.root / "finished"
        launcher = self.launcher("""
            import time
            from pathlib import Path
            time.sleep(.2)
            Path("finished").touch()
        """)
        async def another_job():
            return {"running": True, "pid": -1, "session": "/unrelated/session"}
        with self.assertRaises(ControlError) as result:
            await launcher.start("meeting", wait_for_session=another_job, startup_timeout=.01)
        self.assertEqual((result.exception.code, result.exception.status_code), ("startup_pending", 503))
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        self.assertTrue(marker.exists())

    async def test_cancelling_startup_wait_does_not_stop_child(self):
        launcher = self.launcher("""
            import time
            from pathlib import Path
            time.sleep(.2)
            Path("finished").touch()
        """)
        entered = asyncio.Event()
        async def status():
            entered.set()
            await asyncio.Future()
        task = asyncio.create_task(launcher.start("meeting", wait_for_session=status))
        await entered.wait()
        process = next(iter(launcher._processes))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        deadline = time.monotonic() + 5
        while not (self.root / "finished").exists() and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        self.assertTrue((self.root / "finished").exists())
        self.assertEqual(await asyncio.to_thread(process.wait, timeout=2), 0)


class DetachedLauncherTests(unittest.TestCase):
    def test_background_process_survives_event_loop_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "fake_lec.py"
            marker = root / "finished"
            script.write_text(textwrap.dedent("""
                import sys
                import time
                from pathlib import Path

                time.sleep(0.2)
                Path(sys.argv[1]).write_text("finished", encoding="utf-8")
            """), encoding="utf-8")
            launcher = LecProcessLauncher(
                (sys.executable, script), root, root / "process.log",
                settle_seconds=0.02,
            )

            result = asyncio.run(launcher.start(marker))
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)

            self.assertGreater(result.pid, 0)
            self.assertTrue(marker.is_file())
            for process in tuple(launcher._processes):
                self.assertEqual(process.wait(timeout=1), 0)


if __name__ == "__main__":
    unittest.main()
