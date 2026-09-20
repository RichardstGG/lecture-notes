"""Tests for the local UI service entry point."""
import unittest
from unittest.mock import Mock, patch

from tests import _pathfix  # noqa: F401
from ui.backend.__main__ import (GRACEFUL_SHUTDOWN_SECONDS,
                                 LectureNotesServer, main)


class BackendMainTests(unittest.TestCase):
    @patch("ui.backend.__main__.LectureNotesServer")
    @patch("ui.backend.__main__.uvicorn.Config")
    def test_server_bounds_graceful_shutdown_for_open_sse_connections(
        self, config, server,
    ):
        server.return_value.run.side_effect = KeyboardInterrupt
        main(["--port", "9876"])

        from ui.backend.app import app
        config.assert_called_once_with(
            app,
            host="127.0.0.1",
            port=9876,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
        server.assert_called_once_with(config.return_value, app.state.shutdown_event)
        server.return_value.run.assert_called_once_with()
        self.assertEqual(GRACEFUL_SHUTDOWN_SECONDS, 1)

    def test_server_notifies_sse_streams_before_shutdown(self):
        from asyncio import Event
        from signal import SIGINT

        event = Event()
        config = Mock()
        with patch("uvicorn.Server.__init__", return_value=None), \
                patch("uvicorn.Server.handle_exit") as parent_exit:
            server = LectureNotesServer(config, event)
            server.handle_exit(SIGINT, None)

        self.assertTrue(event.is_set())
        parent_exit.assert_called_once_with(SIGINT, None)


if __name__ == "__main__":
    unittest.main()
