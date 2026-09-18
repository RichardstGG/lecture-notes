"""Read-only access to completed and active lecture session files."""
import json
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


SESSION_MARKERS = ("transcript.md", "notes.md", "status.json", "config.used.toml")


@dataclass
class SessionStoreError(Exception):
    code: str
    message: str
    status_code: int = 500

    def __str__(self):
        return self.message

    def as_detail(self):
        return {"code": self.code, "message": self.message}


def _configured_output_root(repo_root, override=None):
    root = Path(repo_root).resolve()
    if override is not None:
        value = Path(override).expanduser()
        return value.resolve() if value.is_absolute() else (root / value).resolve()

    value = "outputs"
    for path in (root / "config" / "default.toml", root / "config" / "local.toml"):
        try:
            with open(path, "rb") as stream:
                configured = tomllib.load(stream).get("paths", {}).get("output_root")
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SessionStoreError(
                "output_config_invalid", f"Unable to read output root from {path.name}: {exc}",
            ) from exc
        if configured is not None:
            value = configured

    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _read_json(path):
    if path.is_symlink():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class SessionStore:
    def __init__(self, output_root, max_content_bytes=16 * 1024 * 1024):
        self.output_root = Path(output_root).resolve()
        self.max_content_bytes = int(max_content_bytes)

    @classmethod
    def from_settings(cls, settings):
        root = _configured_output_root(settings.repo_root, settings.output_root)
        return cls(root, settings.max_content_bytes)

    def _is_session(self, path):
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return (not path.is_symlink() and resolved.parent == self.output_root
                and resolved.is_dir()
                and any((resolved / name).is_file()
                        and not (resolved / name).is_symlink()
                        for name in SESSION_MARKERS))

    def _session_dir(self, session_id):
        if (not session_id or session_id in {".", ".."} or "/" in session_id
                or "\\" in session_id or "\x00" in session_id):
            raise SessionStoreError("invalid_session_id", "Invalid session identifier", 400)
        candidate = self.output_root / session_id
        if candidate.is_symlink():
            raise SessionStoreError("session_not_found", "Session not found", 404)
        path = candidate.resolve()
        if path.parent != self.output_root or not self._is_session(path):
            raise SessionStoreError("session_not_found", "Session not found", 404)
        return path

    def _course_name(self, path, status):
        if status.get("course"):
            return str(status["course"])
        used = path / "config.used.toml"
        try:
            if used.is_symlink():
                raise OSError("symbolic links are not session content")
            with open(used, "rb") as stream:
                name = tomllib.load(stream).get("course", {}).get("name")
            if name:
                return str(name)
        except (OSError, tomllib.TOMLDecodeError):
            pass
        try:
            transcript = path / "transcript.md"
            if transcript.is_symlink():
                raise OSError("symbolic links are not session content")
            text = transcript.read_text(encoding="utf-8", errors="replace")[:4096]
            match = re.search(r"(?m)^course:\s*(.+?)\s*$", text)
            if match:
                return match.group(1)
        except OSError:
            pass
        return None

    def _summary(self, path):
        status = _read_json(path / "status.json")
        files = [item for item in path.iterdir() if item.is_file() and not item.is_symlink()]
        timestamps = [item.stat().st_mtime for item in files]
        newest = max(timestamps, default=path.stat().st_mtime)
        oldest = min(timestamps, default=newest)
        recordings = list(path.glob("recording_*.ogg"))
        return {
            "id": path.name,
            "course": self._course_name(path, status),
            "started_at": status.get("started_at") or _iso(oldest),
            "updated_at": status.get("updated_at") or _iso(newest),
            "phase": status.get("phase"),
            "mode": status.get("mode"),
            "elapsed": status.get("elapsed"),
            "sections_total": status.get("sections_total"),
            "sections_summarized": status.get("sections_summarized"),
            "has_transcript": ((path / "transcript.md").is_file()
                               and not (path / "transcript.md").is_symlink()),
            "has_notes": ((path / "notes.md").is_file()
                          and not (path / "notes.md").is_symlink()),
            "has_recording": bool(recordings),
        }

    def list(self):
        try:
            if not self.output_root.exists():
                return []
            sessions = [self._summary(path) for path in self.output_root.iterdir()
                        if self._is_session(path)]
        except OSError as exc:
            raise SessionStoreError(
                "sessions_unavailable", f"Unable to read session directory: {exc}",
            ) from exc
        return sorted(sessions, key=lambda item: item["updated_at"], reverse=True)

    def _content_file(self, path):
        if not path.is_file() or path.is_symlink():
            return {"content": "", "updated_at": None, "size_bytes": 0}
        try:
            stat = path.stat()
            if stat.st_size > self.max_content_bytes:
                raise SessionStoreError(
                    "content_too_large",
                    f"{path.name} exceeds the {self.max_content_bytes}-byte UI limit",
                    413,
                )
            raw = path.read_bytes()
        except SessionStoreError:
            raise
        except OSError as exc:
            raise SessionStoreError(
                "content_unavailable", f"Unable to read {path.name}: {exc}",
            ) from exc
        if len(raw) > self.max_content_bytes:
            raise SessionStoreError(
                "content_too_large",
                f"{path.name} exceeds the {self.max_content_bytes}-byte UI limit",
                413,
            )
        return {
            "content": raw.decode("utf-8", errors="replace"),
            "updated_at": _iso(stat.st_mtime),
            "size_bytes": len(raw),
        }

    def get(self, session_id):
        path = self._session_dir(session_id)
        return {
            "api_version": 1,
            "session": self._summary(path),
            "transcript": self._content_file(path / "transcript.md"),
            "notes": self._content_file(path / "notes.md"),
        }
