"""Run the local-only UI backend with `python -m ui.backend`."""
import argparse

import uvicorn


GRACEFUL_SHUTDOWN_SECONDS = 1


class LectureNotesServer(uvicorn.Server):
    """Tell long-lived SSE responses to close before Uvicorn drains requests."""

    def __init__(self, config, shutdown_event):
        super().__init__(config)
        self.shutdown_event = shutdown_event

    def handle_exit(self, sig, frame):
        self.shutdown_event.set()
        super().handle_exit(sig, frame)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lecture Notes local UI backend")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    from .app import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=args.port,
        # SSE connections intentionally remain open while a browser tab exists.
        # Keep a final bound in case a non-SSE request fails to drain.
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = LectureNotesServer(config, app.state.shutdown_event)
    try:
        server.run()
    except KeyboardInterrupt:
        # Uvicorn re-raises the captured signal after a graceful shutdown.
        # Its public `uvicorn.run()` helper suppresses this in the same way.
        pass


if __name__ == "__main__":
    main()
