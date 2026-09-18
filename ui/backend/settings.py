"""Configuration owned by the local UI service."""
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BackendSettings:
    repo_root: Path
    cli_timeout: float = 30.0
    status_poll_interval: float = 2.0
    output_root: Path | None = None
    max_content_bytes: int = 16 * 1024 * 1024
    process_log: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "lecture-notes-ui-process.log",
    )

    @classmethod
    def from_env(cls):
        default_root = Path(__file__).resolve().parents[2]
        root = Path(os.environ.get("LECTURE_NOTES_ROOT", default_root)).expanduser().resolve()
        output = os.environ.get("LECTURE_NOTES_OUTPUT_ROOT")
        output_root = Path(output).expanduser() if output else None
        if output_root is not None and not output_root.is_absolute():
            output_root = root / output_root
        timeout = float(os.environ.get("LECTURE_NOTES_UI_CLI_TIMEOUT", "30"))
        poll = float(os.environ.get("LECTURE_NOTES_UI_POLL_INTERVAL", "2"))
        max_mb = float(os.environ.get("LECTURE_NOTES_UI_MAX_CONTENT_MB", "16"))
        process_log_raw = os.environ.get("LECTURE_NOTES_UI_PROCESS_LOG")
        process_log = (Path(process_log_raw).expanduser() if process_log_raw else
                       Path(tempfile.gettempdir()) / "lecture-notes-ui-process.log")
        if not process_log.is_absolute():
            process_log = root / process_log
        if timeout <= 0:
            raise ValueError("LECTURE_NOTES_UI_CLI_TIMEOUT must be greater than zero")
        if poll <= 0:
            raise ValueError("LECTURE_NOTES_UI_POLL_INTERVAL must be greater than zero")
        if max_mb <= 0:
            raise ValueError("LECTURE_NOTES_UI_MAX_CONTENT_MB must be greater than zero")
        return cls(repo_root=root, output_root=output_root.resolve() if output_root else None,
                   cli_timeout=timeout, status_poll_interval=poll,
                   max_content_bytes=int(max_mb * 1024 * 1024),
                   process_log=process_log.resolve())
