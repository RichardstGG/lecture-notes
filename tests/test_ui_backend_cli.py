"""Tests for the subprocess-only UI/CLI boundary."""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401
from ui.backend.cli_client import LecClient, LecCommandError


class LecClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "fake_lec.py"

    def tearDown(self):
        self.tmp.cleanup()

    def write_script(self, body):
        self.script.write_text(textwrap.dedent(body), encoding="utf-8")
        return LecClient((sys.executable, self.script), self.root, timeout=1)

    async def test_status_runs_json_command_and_preserves_unicode(self):
        client = self.write_script("""
            import json, sys
            assert sys.argv[1:] == ["status", "--json"]
            print(json.dumps({"schema_version": 1, "running": True, "course": "測試課"}))
        """)
        data = await client.status()
        self.assertEqual(data["course"], "測試課")

    async def test_courses_rejects_wrong_json_shape(self):
        client = self.write_script("""
            print('{"not": "a list"}')
        """)
        with self.assertRaisesRegex(LecCommandError, "invalid response") as ctx:
            await client.courses()
        self.assertEqual(ctx.exception.code, "cli_invalid_response")

    async def test_nonzero_exit_is_structured(self):
        client = self.write_script("""
            import sys
            print("broken", file=sys.stderr)
            raise SystemExit(7)
        """)
        with self.assertRaises(LecCommandError) as ctx:
            await client.run_json("status", "--json")
        self.assertEqual(ctx.exception.code, "cli_failed")
        self.assertEqual(ctx.exception.exit_code, 7)
        self.assertEqual(ctx.exception.stderr, "broken")

    async def test_invalid_json_is_structured(self):
        client = self.write_script("""
            print("not json")
        """)
        with self.assertRaises(LecCommandError) as ctx:
            await client.run_json("status", "--json")
        self.assertEqual(ctx.exception.code, "cli_invalid_json")

    async def test_timeout_kills_process(self):
        client = self.write_script("""
            import time
            time.sleep(2)
        """)
        client.timeout = 0.01
        with self.assertRaises(LecCommandError) as ctx:
            await client.run_json("status", "--json")
        self.assertEqual(ctx.exception.code, "cli_timeout")

    async def test_stop_uses_text_command(self):
        client = self.write_script("""
            import sys
            assert sys.argv[1:] == ["stop", "--force"]
            print("停止要求已送出")
        """)
        self.assertEqual(await client.stop(force=True), "停止要求已送出")
