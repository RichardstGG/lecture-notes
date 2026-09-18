"""Tests for background CLI launching and structured configuration overrides."""
import asyncio
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401
from ui.backend.cli_client import LecCommandError
from ui.backend.process_control import (ControlError, LecProcessLauncher,
                                        _override_args, _toml_value)


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


class LauncherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "fake_lec.py"
        self.log = self.root / "process.log"

    def tearDown(self):
        self.tmp.cleanup()

    def launcher(self, body, settle=0.02):
        self.script.write_text(textwrap.dedent(body), encoding="utf-8")
        return LecProcessLauncher(
            (sys.executable, self.script), self.root, self.log,
            settle_seconds=settle,
        )

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


if __name__ == "__main__":
    unittest.main()
