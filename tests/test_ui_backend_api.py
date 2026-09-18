"""HTTP contract tests for the local UI backend."""
import importlib.util
import json
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401

HAS_UI_DEPS = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx", "pydantic"))
if HAS_UI_DEPS:
    import httpx
    from ui.backend.app import _sse, _status_events, create_app
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


class DisconnectAfterOne:
    def __init__(self):
        self.calls = 0

    async def is_disconnected(self):
        self.calls += 1
        return self.calls > 1


@unittest.skipUnless(HAS_UI_DEPS, "UI backend dependencies are not installed")
class BackendApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = StubClient()
        settings = BackendSettings(Path.cwd(), status_poll_interval=0.001)
        self.app = create_app(settings=settings, client=self.client)

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
        stream = _status_events(DisconnectAfterOne(), self.client, 0.001)
        event = await anext(stream)
        await stream.aclose()
        self.assertEqual(event, _sse("status", self.client.status_result))
        data = json.loads(event.split("data: ", 1)[1])
        self.assertFalse(data["running"])


if __name__ == "__main__":
    unittest.main()
