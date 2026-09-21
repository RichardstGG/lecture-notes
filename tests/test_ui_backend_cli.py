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

    async def test_models_runs_versioned_json_command(self):
        client = self.write_script("""
            import json, sys
            assert sys.argv[1:] == ["models", "--json"]
            print(json.dumps({
                "schema_version": 1,
                "summary": {"selected": "qwen", "models": []},
                "whisper": {"selected": "large", "models": []},
            }))
        """)
        data = await client.models()
        self.assertEqual(data["summary"]["selected"], "qwen")

    async def test_models_rejects_incomplete_json_contract(self):
        client = self.write_script("""
            import json
            print(json.dumps({"schema_version": 1, "summary": {"models": []}}))
        """)
        with self.assertRaises(LecCommandError) as ctx:
            await client.models()
        self.assertEqual(ctx.exception.code, "cli_invalid_response")

    async def test_devices_uses_json_contract(self):
        client = self.write_script("""
            import json, sys
            assert sys.argv[1:] == ["devices", "--json"]
            print(json.dumps({
                "current": "mic-1", "default": "mic-2",
                "sources": [{"id": "mic-1", "name": "教室麥克風"}],
            }))
        """)
        result = await client.devices()
        self.assertEqual(result["sources"][0]["name"], "教室麥克風")

    async def test_devices_rejects_incomplete_source_contract(self):
        client = self.write_script("""
            import json
            print(json.dumps({"current": "default", "default": None,
                              "sources": [{"name": "missing id"}]}))
        """)
        with self.assertRaises(LecCommandError) as ctx:
            await client.devices()
        self.assertEqual(ctx.exception.code, "cli_invalid_response")

    async def test_device_actions_use_single_fixed_option_arguments(self):
        client = self.write_script("""
            import sys
            if sys.argv[2].startswith("--save="):
                assert sys.argv[1:] == ["devices", "--save=mic with spaces"]
                print("已儲存")
            else:
                assert sys.argv[1:] == ["devices", "--test=mic with spaces"]
                print("音量正常")
        """)
        self.assertEqual(await client.save_device("mic with spaces"), "已儲存")
        self.assertEqual(await client.test_device("mic with spaces"), "音量正常")

    async def test_device_actions_reject_null_bytes_before_spawning(self):
        client = self.write_script("raise AssertionError('must not run')")
        for action in (client.save_device, client.test_device):
            with self.subTest(action=action.__name__), self.assertRaises(LecCommandError) as ctx:
                await action("mic\x00bad")
            self.assertEqual(ctx.exception.code, "invalid_argument")

    async def test_doctor_accepts_json_diagnostics_with_exit_one(self):
        client = self.write_script("""
            import json, sys
            assert sys.argv[1:] == ["doctor", "測試課", "--mic", "--json"]
            print(json.dumps([
                {"status": "✔", "name": "Python", "detail": "3.13"},
                {"status": "✖", "name": "麥克風", "detail": "missing"},
            ]))
            raise SystemExit(1)
        """)
        result = await client.doctor(course="測試課", mic=True)
        self.assertEqual(result[1]["status"], "✖")

    async def test_doctor_rejects_option_like_course(self):
        client = self.write_script("raise AssertionError('must not run')")
        with self.assertRaises(LecCommandError) as ctx:
            await client.doctor(course="--help")
        self.assertEqual(ctx.exception.code, "invalid_argument")

    async def test_doctor_rejects_course_paths(self):
        client = self.write_script("raise AssertionError('must not run')")
        with self.assertRaises(LecCommandError) as ctx:
            await client.doctor(course="../private.toml")
        self.assertEqual(ctx.exception.code, "invalid_argument")

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

    async def test_course_mutations_use_fixed_text_commands(self):
        client = self.write_script("""
            import sys
            if sys.argv[1] == "new":
                assert sys.argv[2:] == ["資料結構"]
                print("created")
            else:
                assert sys.argv[1] == "config"
                assert sys.argv[2].endswith("course.toml")
                print("valid")
        """)
        self.assertEqual(await client.create_course("資料結構"), "created")
        self.assertEqual(
            (await client.validate_course(self.root / "course.toml")).strip(), "valid",
        )
