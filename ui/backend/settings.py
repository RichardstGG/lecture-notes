"""Configuration owned by the local UI service."""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BackendSettings:
    repo_root: Path
    cli_timeout: float = 30.0
    status_poll_interval: float = 2.0

    @classmethod
    def from_env(cls):
        default_root = Path(__file__).resolve().parents[2]
        root = Path(os.environ.get("LECTURE_NOTES_ROOT", default_root)).expanduser().resolve()
        timeout = float(os.environ.get("LECTURE_NOTES_UI_CLI_TIMEOUT", "30"))
        poll = float(os.environ.get("LECTURE_NOTES_UI_POLL_INTERVAL", "2"))
        if timeout <= 0:
            raise ValueError("LECTURE_NOTES_UI_CLI_TIMEOUT must be greater than zero")
        if poll <= 0:
            raise ValueError("LECTURE_NOTES_UI_POLL_INTERVAL must be greater than zero")
        return cls(repo_root=root, cli_timeout=timeout, status_poll_interval=poll)
