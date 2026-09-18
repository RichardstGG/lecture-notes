"""HTTP contract tests for the local UI backend."""
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
    from ui.backend.settings import BackendSettings


class StubClient:
    def __init__(self):
        self.status_result = {"schema_version": 1, "running": False}
        self.courses_result = []
        self.error = None

    async def status(self):
        if self.error:
            raise self.error
        return self.status_result

    async def courses(self):
        if self.error:
            raise self.error
        return self.courses_result


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
        self.client = StubClient()
        settings = BackendSettings(
            Path.cwd(), output_root=self.output, status_poll_interval=0.001,
        )
        self.app = create_app(settings=settings, client=self.client)

    def tearDown(self):
        self.tmp.cleanup()

    def make_session(self, name="UNIXops_20260918"):
        session = self.output / name
        session.mkdir(parents=True)
        (session / "status.json").write_text(json.dumps({
            "schema_version": 1, "course": "UNIXops", "phase": "recording",
            "started_at": "2026-09-18T10:00:00+08:00",
            "updated_at": "2026-09-18T10:05:00+08:00",
        }), encoding="utf-8")
        (session / "transcript.md").write_text("first\n", encoding="utf-8")
        (session / "notes.md").write_text("note\n", encoding="utf-8")
        return session

    async def request(self, method, path):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path)

    async def test_health_is_versioned(self):
        response = await self.request("GET", "/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"service": "lecture-notes-ui", "api_version": 1})

    async def test_status_passes_through_cli_contract(self):
        self.client.status_result = {
            "schema_version": 1, "running": True, "course": "UNIXops",
            "future_field": "kept",
        }
        response = await self.request("GET", "/api/v1/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["course"], "UNIXops")
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

    async def test_status_stream_emits_sse_status_event(self):
        stream = _status_events(DisconnectAfter(), self.client, 0.001)
        event = await anext(stream)
        await stream.aclose()
        self.assertEqual(event, _sse("status", self.client.status_result))
        data = json.loads(event.split("data: ", 1)[1])
        self.assertFalse(data["running"])

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
