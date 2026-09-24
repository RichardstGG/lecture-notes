"""Filesystem contract tests for UI session history and content."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core.util import atomic_write
from ui.backend.session_store import (SessionStore, SessionStoreError,
                                      _configured_output_root, _iso)


class OutputRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_output_root_overrides_default(self):
        (self.root / "config" / "default.toml").write_text(
            '[paths]\noutput_root = "default-output"\n', encoding="utf-8")
        (self.root / "config" / "local.toml").write_text(
            '[paths]\noutput_root = "local-output"\n', encoding="utf-8")
        self.assertEqual(
            _configured_output_root(self.root), (self.root / "local-output").resolve(),
        )

    def test_explicit_output_root_has_highest_priority(self):
        (self.root / "config" / "default.toml").write_text(
            '[paths]\noutput_root = "ignored"\n', encoding="utf-8")
        settings = SimpleNamespace(
            repo_root=self.root, output_root=Path("chosen"), max_content_bytes=123,
        )
        store = SessionStore.from_settings(settings)
        self.assertEqual(store.output_root, (self.root / "chosen").resolve())
        self.assertEqual(store.max_content_bytes, 123)

    def test_existing_positional_settings_arguments_remain_compatible(self):
        from ui.backend.settings import BackendSettings

        settings = BackendSettings(self.root, 12.5, 0.5)
        self.assertEqual(settings.cli_timeout, 12.5)
        self.assertEqual(settings.status_poll_interval, 0.5)
        self.assertIsNone(settings.output_root)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "outputs"
        self.output.mkdir()
        self.store = SessionStore(self.output, max_content_bytes=1024)

    def tearDown(self):
        self.tmp.cleanup()

    def make_session(self, name="測試課_20260918", updated="2026-09-18T12:00:00+08:00"):
        session = self.output / name
        session.mkdir(parents=True)
        (session / "status.json").write_text(json.dumps({
            "schema_version": 1, "course": "測試課", "phase": "done",
            "mode": "live", "elapsed": 123.4, "sections_total": 3,
            "sections_summarized": 2, "started_at": "2026-09-18T10:00:00+08:00",
            "updated_at": updated,
        }), encoding="utf-8")
        (session / "transcript.md").write_text("# Transcript\n", encoding="utf-8")
        (session / "notes.md").write_text("# Notes\n", encoding="utf-8")
        (session / "recording_100000.ogg").write_bytes(b"audio")
        return session

    def test_list_returns_newest_first_and_ignores_unrelated_directories(self):
        older = self.make_session("older", "2026-09-17T12:00:00+08:00")
        newer = self.make_session("newer", "2026-09-18T12:00:00+08:00")
        (self.output / ".obsidian").mkdir()
        (self.output / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")

        sessions = self.store.list()

        self.assertEqual([item["id"] for item in sessions], [newer.name, older.name])
        self.assertEqual(sessions[0]["course"], "測試課")
        self.assertEqual(sessions[0]["phase"], "done")
        self.assertTrue(sessions[0]["has_transcript"])
        self.assertTrue(sessions[0]["has_notes"])
        self.assertTrue(sessions[0]["has_recording"])

    def test_get_returns_transcript_and_notes(self):
        session = self.make_session()
        detail = self.store.get(session.name)
        self.assertEqual(detail["api_version"], 1)
        self.assertEqual(detail["transcript"]["content"], "# Transcript\n")
        self.assertEqual(detail["notes"]["content"], "# Notes\n")
        self.assertEqual(detail["session"]["elapsed"], 123.4)

    def test_get_survives_atomic_write_churn(self):
        session = self.make_session()
        finished = threading.Event()
        failures = []

        def write_status():
            try:
                for index in range(300):
                    atomic_write(session / "status.json", json.dumps({
                        "phase": "recording", "elapsed": index,
                    }))
            except OSError as exc:
                failures.append(exc)
            finally:
                finished.set()

        writer = threading.Thread(target=write_status)
        writer.start()
        try:
            while not finished.is_set():
                self.assertEqual(self.store.get(session.name)["session"]["id"], session.name)
        finally:
            writer.join()
        self.assertEqual(failures, [])

    def test_summary_skips_file_removed_between_listing_and_stat(self):
        session = self.make_session()
        disappearing = session / "stop"
        disappearing.write_text("stop", encoding="utf-8")
        real_stat = Path.stat
        calls = 0

        def stat(path, *args, **kwargs):
            nonlocal calls
            if path == disappearing:
                calls += 1
                if calls == 2:
                    raise FileNotFoundError(disappearing)
            return real_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", stat):
            summary = self.store._summary(session)
        self.assertEqual(summary["id"], session.name)
        self.assertGreaterEqual(calls, 2)

    def test_get_wraps_unexpected_filesystem_error(self):
        session = self.make_session()
        with patch.object(self.store, "_summary", side_effect=OSError("transient")):
            with self.assertRaises(SessionStoreError) as ctx:
                self.store.get(session.name)
        self.assertEqual(ctx.exception.code, "session_unavailable")

    def test_hidden_temporary_file_does_not_change_legacy_timestamps(self):
        session = self.output / "legacy"
        session.mkdir()
        marker = session / "config.used.toml"
        marker.write_text('[course]\nname = "legacy"\n', encoding="utf-8")
        temporary = session / ".status.json.tmp"
        temporary.write_text("{}", encoding="utf-8")
        os.utime(marker, (1_700_000_000, 1_700_000_000))
        os.utime(temporary, (1_800_000_000, 1_800_000_000))

        summary = self.store.get(session.name)["session"]
        self.assertEqual(summary["started_at"], _iso(1_700_000_000))
        self.assertEqual(summary["updated_at"], _iso(1_700_000_000))

    def test_nested_sessions_use_unique_relative_path_ids(self):
        first = self.make_session("UNIXops/20260921")
        second = self.make_session("Networks/20260921", "2026-09-19T12:00:00+08:00")

        sessions = self.store.list()

        self.assertEqual(
            [item["id"] for item in sessions],
            ["Networks/20260921", "UNIXops/20260921"],
        )
        detail = self.store.get("UNIXops/20260921")
        self.assertEqual(detail["session"]["id"], "UNIXops/20260921")
        self.assertEqual(self.store.path_for("UNIXops/20260921"), first.resolve())
        self.assertNotEqual(first, second)

    def test_course_falls_back_to_config_used(self):
        session = self.output / "course-fallback"
        session.mkdir()
        (session / "config.used.toml").write_text(
            '[course]\nname = "資料結構"\n', encoding="utf-8")
        self.assertEqual(self.store.list()[0]["course"], "資料結構")

    def test_rejects_path_traversal_and_missing_session(self):
        for session_id, code, status in (
            ("../outside", "invalid_session_id", 400),
            ("course/../outside", "invalid_session_id", 400),
            ("/absolute", "invalid_session_id", 400),
            ("course//date", "invalid_session_id", 400),
            ("..\\outside", "invalid_session_id", 400),
            ("missing", "session_not_found", 404),
        ):
            with self.subTest(session_id=session_id), self.assertRaises(SessionStoreError) as ctx:
                self.store.get(session_id)
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual(ctx.exception.status_code, status)

    def test_rejects_symlink_that_escapes_output_root(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "transcript.md").write_text("private", encoding="utf-8")
        link = self.output / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(SessionStoreError) as ctx:
            self.store.get("linked")
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(self.store.list(), [])

    def test_does_not_follow_symlinked_content(self):
        session = self.output / "safe-session"
        session.mkdir()
        (session / "status.json").write_text('{"course":"safe"}', encoding="utf-8")
        outside = Path(self.tmp.name) / "outside-transcript.md"
        outside.write_text("private", encoding="utf-8")
        try:
            (session / "transcript.md").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        detail = self.store.get(session.name)
        self.assertFalse(detail["session"]["has_transcript"])
        self.assertEqual(detail["transcript"]["content"], "")

    def test_content_limit_is_enforced(self):
        session = self.make_session()
        (session / "transcript.md").write_bytes(b"x" * 1025)
        with self.assertRaises(SessionStoreError) as ctx:
            self.store.get(session.name)
        self.assertEqual(ctx.exception.code, "content_too_large")
        self.assertEqual(ctx.exception.status_code, 413)

    def test_missing_output_root_is_an_empty_history(self):
        missing = SessionStore(self.output / "missing")
        self.assertEqual(missing.list(), [])


if __name__ == "__main__":
    unittest.main()
