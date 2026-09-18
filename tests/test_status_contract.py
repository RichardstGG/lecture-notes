"""Contract tests for run.json, status.json, and events.jsonl."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import _pathfix  # noqa: F401
from core import cli
from core.status import (EVENT_SCHEMA_VERSION, RUN_SCHEMA_VERSION,
                         STATUS_SCHEMA_VERSION, RunLock, Status)


STATUS_KEYS = {
    "schema_version", "phase", "pid", "started_at", "updated_at",
    "course", "session", "mode", "input_file", "summary_model",
    "elapsed", "transcribed", "transcribe_lag", "queue",
    "sections_total", "sections_summarized", "llm_busy", "llm_section",
    "servers", "errors", "last_error",
}


class StatusContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Path(self.tmp.name)
        self.statuses = []

    def tearDown(self):
        for status in self.statuses:
            status.close()
        self.tmp.cleanup()

    def make_status(self, **initial):
        status = Status(self.session, interval=60, **initial)
        self.statuses.append(status)
        return status

    def test_status_snapshot_has_stable_versioned_shape(self):
        status = self.make_status(course="UNIXops", mode="live")
        status.update(schema_version=999)
        status.flush()

        data = json.loads((self.session / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data), STATUS_KEYS)
        self.assertEqual(data["schema_version"], STATUS_SCHEMA_VERSION)
        self.assertEqual(data["course"], "UNIXops")
        self.assertEqual(data["mode"], "live")
        self.assertEqual(data["session"], str(self.session))
        self.assertEqual(data["transcribed"], 0)
        self.assertIsNone(data["llm_section"])
        self.assertEqual(data["servers"], {
            "whisper": "not_started", "llama": "not_started",
        })

    def test_events_are_versioned_and_sequenced(self):
        status = self.make_status()
        status.event("custom", value=1, schema_version=999, seq=999,
                     time="caller-value", type="caller-value")
        status.phase("loading")

        rows = [json.loads(line) for line in
                (self.session / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["seq"] for row in rows], [1, 2])
        self.assertTrue(all(row["schema_version"] == EVENT_SCHEMA_VERSION for row in rows))
        self.assertEqual(rows[0]["type"], "custom")
        self.assertNotEqual(rows[0]["time"], "caller-value")
        self.assertEqual(rows[1], {
            "schema_version": EVENT_SCHEMA_VERSION,
            "seq": 2,
            "time": rows[1]["time"],
            "type": "phase",
            "phase": "loading",
        })

    def test_event_sequence_continues_after_legacy_rows(self):
        (self.session / "events.jsonl").write_text(
            '{"time":"old-1","type":"phase"}\n'
            '{"time":"old-2","type":"error"}\n'
            'not json\n', encoding="utf-8")
        status = self.make_status()
        status.event("custom")

        row = json.loads((self.session / "events.jsonl").read_text(
            encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["seq"], 3)

    def test_event_sequence_resumes_after_higher_existing_sequence(self):
        (self.session / "events.jsonl").write_text(
            '{"schema_version":1,"seq":7,"type":"phase"}\n', encoding="utf-8")
        status = self.make_status()
        status.event("custom")

        row = json.loads((self.session / "events.jsonl").read_text(
            encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["seq"], 8)


class RunContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_run_record_has_stable_versioned_shape(self):
        lock = RunLock(self.state)
        self.assertIsNone(lock.acquire(course="UNIXops", mode="file"))

        data = json.loads((self.state / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], RUN_SCHEMA_VERSION)
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["course"], "UNIXops")
        self.assertEqual(data["mode"], "file")
        self.assertIsNone(data["session"])
        self.assertIn("started_at", data)
        lock.release()

    def test_legacy_run_record_is_normalized_for_readers(self):
        (self.state / "run.json").write_text(
            '{"pid":123,"course":"legacy"}\n', encoding="utf-8")
        with mock.patch("core.status.pid_alive", return_value=True):
            data = RunLock(self.state).current()

        self.assertEqual(data["schema_version"], 0)
        self.assertEqual(data["pid"], 123)
        self.assertEqual(data["course"], "legacy")
        self.assertEqual(data["mode"], "run")
        self.assertIsNone(data["session"])

    def test_status_command_reports_schema_when_idle(self):
        args = SimpleNamespace(json=True)
        out = io.StringIO()
        with mock.patch("core.cli._state_dir", return_value=self.state), redirect_stdout(out):
            self.assertEqual(cli.cmd_status(args), 0)

        self.assertEqual(json.loads(out.getvalue()), {
            "schema_version": RUN_SCHEMA_VERSION,
            "running": False,
        })

    def test_status_command_combines_active_run_and_session_snapshot(self):
        session = self.state / "session"
        session.mkdir()
        (session / "status.json").write_text(
            '{"schema_version":1,"phase":"recording"}\n', encoding="utf-8")
        lock = RunLock(self.state)
        self.assertIsNone(lock.acquire(course="UNIXops", session=str(session), mode="live"))

        args = SimpleNamespace(json=True)
        out = io.StringIO()
        with mock.patch("core.cli._state_dir", return_value=self.state), redirect_stdout(out):
            self.assertEqual(cli.cmd_status(args), 0)

        data = json.loads(out.getvalue())
        self.assertTrue(data["running"])
        self.assertEqual(data["schema_version"], RUN_SCHEMA_VERSION)
        self.assertEqual(data["mode"], "live")
        self.assertEqual(data["status"], {
            "schema_version": STATUS_SCHEMA_VERSION,
            "phase": "recording",
        })
        lock.release()


if __name__ == "__main__":
    unittest.main()
