"""HTTP contract tests for the local UI backend."""
import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401

HAS_UI_DEPS = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx", "pydantic"))
if HAS_UI_DEPS:
    import httpx
    from ui.backend.app import _session_events, _sse, _status_events, create_app
    from ui.backend.cli_client import LecCommandError
    from ui.backend.course_store import CourseStore
    from ui.backend.process_control import LaunchResult
    from ui.backend.settings import BackendSettings


class StubClient:
    def __init__(self):
        self.status_result = {"schema_version": 1, "running": False}
        self.courses_result = []
        self.error = None
        self.stop_calls = []
        self.course_root = None
        self.course_create_calls = []
        self.course_validate_calls = []

    async def status(self):
        if self.error:
            raise self.error
        return self.status_result

    async def courses(self):
        if self.error:
            raise self.error
        return self.courses_result

    async def stop(self, force=False):
        if self.error:
            raise self.error
        self.stop_calls.append(force)
        return "force stop" if force else "stop"

    async def create_course(self, course_id):
        self.course_create_calls.append(course_id)
        path = self.course_root / f"{course_id}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'[course]\nname = "{course_id}"\n', encoding="utf-8")
        return f"created {path}"

    async def validate_course(self, path):
        self.course_validate_calls.append(Path(path).read_text(encoding="utf-8"))
        if "invalid-model" in self.course_validate_calls[-1]:
            raise LecCommandError("cli_failed", "summary.model is not defined", exit_code=1)
        return "valid"


class StubLauncher:
    def __init__(self):
        self.calls = []
        self.pid = 4321

    async def start(self, *args):
        self.calls.append(args)
        return LaunchResult(self.pid)


class DisconnectAfter:
    def __init__(self, allowed_calls=1):
        self.calls = 0
        self.allowed_calls = allowed_calls

    async def is_disconnected(self):
        self.calls += 1
        return self.calls > self.allowed_calls


@unittest.skipUnless(HAS_UI_DEPS, "UI backend dependencies are not installed")
class BackendApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "outputs"
        self.course_root = Path(self.tmp.name) / "courses"
        self.course_root.mkdir()
        self.frontend = Path(self.tmp.name) / "frontend"
        self.frontend.mkdir()
        (self.frontend / "index.html").write_text(
            "<!doctype html><title>Lecture Notes</title>", encoding="utf-8",
        )
        self.client = StubClient()
        self.client.course_root = self.course_root
        self.launcher = StubLauncher()
        settings = BackendSettings(
            Path.cwd(), output_root=self.output, status_poll_interval=0.001,
            frontend_dist=self.frontend,
        )
        self.app = create_app(
            settings=settings, client=self.client, launcher=self.launcher,
            course_store=CourseStore(self.course_root),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def make_session(self, name="測試課_20260918"):
        session = self.output / name
        session.mkdir(parents=True)
        (session / "status.json").write_text(json.dumps({
            "schema_version": 1, "course": "測試課", "phase": "recording",
            "started_at": "2026-09-18T10:00:00+08:00",
            "updated_at": "2026-09-18T10:05:00+08:00",
        }), encoding="utf-8")
        (session / "transcript.md").write_text("first\n", encoding="utf-8")
        (session / "notes.md").write_text("note\n", encoding="utf-8")
        return session

    async def request(self, method, path, **kwargs):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def test_health_is_versioned(self):
        response = await self.request("GET", "/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"service": "lecture-notes-ui", "api_version": 1})

    async def test_built_frontend_is_served_at_root(self):
        response = await self.request("GET", "/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Lecture Notes", response.text)

    async def test_status_passes_through_cli_contract(self):
        self.client.status_result = {
            "schema_version": 1, "running": True, "course": "測試課",
            "future_field": "kept",
        }
        response = await self.request("GET", "/api/v1/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["course"], "測試課")
        self.assertEqual(response.json()["future_field"], "kept")

    async def test_courses_supports_dynamic_model_names_and_invalid_course(self):
        self.client.courses_result = [
            {"file": "/courses/a.toml", "id": "a", "name": "A",
             "model": "future-14b", "terms": 3},
            {"file": "/courses/b.toml", "id": "b", "error": "bad TOML"},
        ]
        response = await self.request("GET", "/api/v1/courses")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["model"], "future-14b")
        self.assertEqual(response.json()[1]["error"], "bad TOML")

    async def test_course_create_and_detail_use_versioned_contract(self):
        response = await self.request("POST", "/api/v1/courses", json={"id": "資料結構"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["api_version"], 1)
        self.assertEqual(response.json()["id"], "資料結構")
        self.assertEqual(self.client.course_create_calls, ["資料結構"])

        response = await self.request("GET", "/api/v1/courses/資料結構")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name = "資料結構"', response.json()["content"])

    async def test_course_create_rejects_unsafe_and_duplicate_ids(self):
        for course_id in ("../escape", "-option", "CON"):
            with self.subTest(course_id=course_id):
                response = await self.request(
                    "POST", "/api/v1/courses", json={"id": course_id},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_course_id")

        (self.course_root / "existing.toml").write_text("[course]\n", encoding="utf-8")
        response = await self.request("POST", "/api/v1/courses", json={"id": "existing"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "course_exists")

    async def test_course_update_validates_then_atomically_replaces_content(self):
        target = self.course_root / "測試課.toml"
        target.write_text('[course]\nname = "舊名稱"\n', encoding="utf-8")
        content = '[course]\nname = "新名稱"\n\n[summary]\nmodel = "qwen3-8b"\n'
        response = await self.request(
            "PUT", "/api/v1/courses/測試課", json={"content": content},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], content)
        self.assertEqual(target.read_text(encoding="utf-8"), content)
        self.assertEqual(self.client.course_validate_calls, [content])

    async def test_course_update_preserves_existing_file_when_validation_fails(self):
        target = self.course_root / "測試課.toml"
        original = '[course]\nname = "原始"\n'
        target.write_text(original, encoding="utf-8")

        response = await self.request(
            "PUT", "/api/v1/courses/測試課",
            json={"content": '[summary]\nmodel = "invalid-model"\n'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_course_config")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    async def test_cli_failure_uses_stable_error_envelope(self):
        self.client.error = LecCommandError(
            "cli_failed", "command failed", exit_code=3, stderr="details",
        )
        response = await self.request("GET", "/api/v1/status")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": {
            "code": "cli_failed", "message": "command failed",
            "exit_code": 3, "stderr": "details",
        }})

    async def test_timeout_is_service_unavailable(self):
        self.client.error = LecCommandError("cli_timeout", "timed out")
        response = await self.request("GET", "/api/v1/courses")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "cli_timeout")

    async def test_start_run_builds_fixed_cli_arguments(self):
        input_file = Path(self.tmp.name) / "lecture.ogg"
        input_file.write_bytes(b"audio")
        response = await self.request("POST", "/api/v1/runs", json={
            "course": "測試課", "input_file": str(input_file),
            "model": "future-14b", "source": "mic-1",
            "overrides": {"summary.temperature": 0.3, "summary.enabled": False,
                          "whisper.terms": ["核心", "kernel"]},
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["pid"], 4321)
        self.assertEqual(self.launcher.calls, [(
            "run", "測試課", "--file", str(input_file.resolve()),
            "--model", "future-14b", "--source", "mic-1",
            "--set", "summary.enabled=false",
            "--set", "summary.temperature=0.3",
            "--set", 'whisper.terms=["核心", "kernel"]',
        )])

    async def test_start_run_rejects_active_process(self):
        self.client.status_result = {
            "schema_version": 1, "running": True, "course": "SecOps",
        }
        response = await self.request("POST", "/api/v1/runs", json={"course": "測試課"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "run_active")
        self.assertEqual(self.launcher.calls, [])

    async def test_start_run_rejects_invalid_override(self):
        response = await self.request("POST", "/api/v1/runs", json={
            "course": "測試課", "overrides": {"not-dotted": True},
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_override")

    async def test_start_run_rejects_option_like_course(self):
        response = await self.request("POST", "/api/v1/runs", json={"course": "--help"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_argument")

    async def test_start_run_rejects_missing_input_file(self):
        response = await self.request("POST", "/api/v1/runs", json={
            "course": "測試課", "input_file": "/definitely/missing/audio.ogg",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "input_file_not_found")

    async def test_force_stop_invokes_cli(self):
        response = await self.request(
            "POST", "/api/v1/runs/stop", json={"force": True},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["operation"], "stop")
        self.assertTrue(response.json()["force"])
        self.assertEqual(self.client.stop_calls, [True])

    async def test_summarize_builds_fixed_cli_arguments(self):
        session = self.make_session()
        response = await self.request(
            "POST", f"/api/v1/sessions/{session.name}/summarize",
            json={"redo": "all", "model": "future-14b", "course": "測試課"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["operation"], "summarize")
        self.assertEqual(self.launcher.calls, [(
            "summarize", str(session.resolve()), "--redo", "all",
            "--model", "future-14b", "--course", "測試課",
        )])

    async def test_status_stream_emits_sse_status_event(self):
        stream = _status_events(DisconnectAfter(), self.client, 0.001)
        event = await anext(stream)
        await stream.aclose()
        self.assertEqual(event, _sse("status", self.client.status_result))
        data = json.loads(event.split("data: ", 1)[1])
        self.assertFalse(data["running"])

    async def test_status_stream_closes_when_server_shutdown_is_requested(self):
        shutdown = asyncio.Event()
        stream = _status_events(
            DisconnectAfter(allowed_calls=100), self.client, 60,
            shutdown_event=shutdown,
        )
        self.assertTrue((await anext(stream)).startswith("event: status\n"))
        shutdown.set()
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=0.1)

    async def test_session_stream_closes_when_server_shutdown_is_requested(self):
        session = self.make_session()
        shutdown = asyncio.Event()
        stream = _session_events(
            DisconnectAfter(allowed_calls=100), self.app.state.sessions,
            session.name, 60, shutdown_event=shutdown,
        )
        self.assertTrue((await anext(stream)).startswith("event: snapshot\n"))
        shutdown.set()
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=0.1)

    async def test_sessions_list_and_detail(self):
        session = self.make_session()
        response = await self.request("GET", "/api/v1/sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["id"], session.name)

        response = await self.request("GET", f"/api/v1/sessions/{session.name}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transcript"]["content"], "first\n")
        self.assertEqual(response.json()["notes"]["content"], "note\n")

    async def test_missing_session_uses_stable_error_envelope(self):
        response = await self.request("GET", "/api/v1/sessions/missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": {
            "code": "session_not_found", "message": "Session not found",
        }})

        response = await self.request("GET", "/api/v1/sessions/missing/stream")
        self.assertEqual(response.status_code, 404)

    async def test_session_stream_sends_snapshot_then_append_delta(self):
        session = self.make_session()
        stream = _session_events(
            DisconnectAfter(allowed_calls=2), self.app.state.sessions,
            session.name, 0.001,
        )
        snapshot = await anext(stream)
        self.assertTrue(snapshot.startswith("event: snapshot\n"))
        with open(session / "transcript.md", "a", encoding="utf-8") as output:
            output.write("second\n")

        event = await anext(stream)
        await stream.aclose()
        self.assertTrue(event.startswith("event: content\n"))
        data = json.loads(event.split("data: ", 1)[1])
        self.assertEqual(data["target"], "transcript")
        self.assertEqual(data["operation"], "append")
        self.assertEqual(data["content"], "second\n")

    async def test_session_stream_replaces_rebuilt_notes(self):
        session = self.make_session()
        stream = _session_events(
            DisconnectAfter(allowed_calls=2), self.app.state.sessions,
            session.name, 0.001,
        )
        await anext(stream)
        (session / "notes.md").write_text("rebuilt\n", encoding="utf-8")

        event = await anext(stream)
        await stream.aclose()
        data = json.loads(event.split("data: ", 1)[1])
        self.assertEqual(data["target"], "notes")
        self.assertEqual(data["operation"], "replace")
        self.assertEqual(data["content"], "rebuilt\n")


if __name__ == "__main__":
    unittest.main()
