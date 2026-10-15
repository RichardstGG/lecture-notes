"""Read-only access to completed and active session files."""
import json
import math
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
        path = Path(path)
        try:
            resolved = path.resolve()
        except OSError:
            return False
        try:
            relative = path.relative_to(self.output_root)
        except ValueError:
            return False
        current = self.output_root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return False
        return (resolved != self.output_root and resolved.is_relative_to(self.output_root)
                and resolved.is_dir()
                and any((resolved / name).is_file()
                        and not (resolved / name).is_symlink()
                        for name in SESSION_MARKERS))

    def _session_dir(self, session_id):
        if (not session_id or "\\" in session_id or "\x00" in session_id):
            raise SessionStoreError("invalid_session_id", "Invalid session identifier", 400)
        parts = session_id.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise SessionStoreError("invalid_session_id", "Invalid session identifier", 400)
        path = self.output_root.joinpath(*parts)
        if not self._is_session(path):
            raise SessionStoreError("session_not_found", "Session not found", 404)
        return path.resolve()

    def _course_name(self, path, status, work_type="lecture"):
        if work_type == "meeting" and status.get("meeting"):
            return str(status["meeting"])
        if status.get("course"):
            return str(status["course"])
        used = path / "config.used.toml"
        try:
            if used.is_symlink():
                raise OSError("symbolic links are not session content")
            with open(used, "rb") as stream:
                data = tomllib.load(stream)
                section = "meeting" if work_type == "meeting" else "course"
                name = data.get(section, {}).get("name")
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

    def _meeting_source(self, path):
        """Display availability only; execution validates the actual audio hash."""
        metadata = _read_json(path / "source.json")
        name, size, digest = (metadata.get(key) for key in ("path", "size_bytes", "sha256"))
        duration = metadata.get("duration_seconds")
        if (type(metadata.get("schema_version")) is not int or metadata["schema_version"] != 1
                or metadata.get("kind") not in ("recording", "import")
                or not isinstance(name, str) or not re.fullmatch(r"source_audio\.[A-Za-z0-9]+", name)
                or type(size) is not int or size <= 0
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or type(duration) not in (float, int) or not math.isfinite(duration) or duration <= 0
                or not isinstance(metadata.get("original_name"), str)
                or not isinstance(metadata.get("created_at"), str)
                or type(metadata.get("requested_speakers")) is not int
                or not 1 <= metadata["requested_speakers"] <= 30):
            return None
        source = path / name
        if source.is_symlink() or not source.is_file():
            return None
        try:
            stat = source.stat()
            if stat.st_size != size:
                return None
            return metadata
        except OSError:
            return None

    @staticmethod
    def _original_meeting_phase(path):
        # The first run's terminal phase precedes all diarization attempts. Its
        # existing event carries the original outcome, including partial imports.
        events = path / "events.jsonl"
        if events.is_symlink():
            return None
        try:
            with events.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(event, dict) and event.get("type") == "phase"
                            and event.get("phase") in ("done", "failed", "aborted")):
                        return event["phase"]
        except (OSError, UnicodeError):
            pass
        return None

    def _summary(self, path):
        status = _read_json(path / "status.json")
        work_type = status.get("work_type") or "lecture"
        if work_type == "lecture":
            used = path / "config.used.toml"
            if used.is_file() and not used.is_symlink():
                try:
                    with open(used, "rb") as stream:
                        work_type = tomllib.load(stream).get("work", {}).get("type") or "lecture"
                except (OSError, tomllib.TOMLDecodeError):
                    pass
        timestamps = []
        for item in path.iterdir():
            if item.name.startswith("."):
                continue
            try:
                if item.is_file() and not item.is_symlink():
                    timestamps.append(item.stat().st_mtime)
            except FileNotFoundError:
                # Atomic writes and deleted session files can disappear during a scan.
                continue
        if timestamps:
            newest = max(timestamps)
            oldest = min(timestamps)
        else:
            newest = oldest = path.stat().st_mtime
        recordings = list(path.glob("recording_*.ogg"))
        source = self._meeting_source(path) if work_type == "meeting" else None
        summary = {
            "id": path.relative_to(self.output_root).as_posix(),
            "course": self._course_name(path, status, work_type),
            "work_type": work_type,
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
            "has_recording": bool(source) if work_type == "meeting" else bool(recordings),
            "has_speaker_transcript": (path / "diarization.current.json").is_file()
                                      and not (path / "diarization.current.json").is_symlink(),
        }
        if work_type == "meeting" and status.get("mode") == "diarize":
            # Raw status remains the current operation for lec status/SSE. The
            # library presents the capture outcome separately from that attempt.
            summary["phase"] = self._original_meeting_phase(path)
            kind = _read_json(path / "source.json").get("kind")
            summary["mode"] = {"import": "file", "recording": "live"}.get(kind)
            summary["diarization_status"] = {key: status.get(key) for key in (
                "phase", "stop_reason", "last_error", "diarization")}
        return summary

    def _speaker_transcript(self, path):
        manifest_path = path / "diarization.current.json"
        if not manifest_path.is_file():
            return {"content": "", "updated_at": None, "size_bytes": 0}
        manifest = _read_json(manifest_path)
        generation = manifest.get("generation")
        name = manifest.get("speaker_transcript")
        if (manifest.get("schema_version") != 1
                or not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", generation)
                or name != f"diarization/{generation}/transcript.speakers.md"):
            raise SessionStoreError("speaker_transcript_invalid", "Invalid speaker transcript manifest", 422)
        current = path
        for part in name.split("/"):
            current /= part
            if current.is_symlink():
                raise SessionStoreError("speaker_transcript_invalid", "Invalid speaker transcript path", 422)
        return self._content_file(current)

    def list(self):
        try:
            if not self.output_root.exists():
                return []
            sessions = []
            pending = [self.output_root]
            while pending:
                parent = pending.pop()
                for path in parent.iterdir():
                    if path.is_symlink() or not path.is_dir():
                        continue
                    pending.append(path)
                    if self._is_session(path):
                        sessions.append(self._summary(path))
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
        try:
            path = self._session_dir(session_id)
            summary = self._summary(path)
            detail = {
                "api_version": 1,
                "session": summary,
                "transcript": self._content_file(path / "transcript.md"),
                "notes": self._content_file(path / "notes.md"),
            }
            if summary["work_type"] == "meeting":
                detail["speaker_transcript"] = self._speaker_transcript(path)
                detail["source"] = self._meeting_source(path)
                manifest = _read_json(path / "diarization.current.json")
                generation = manifest.get("generation")
                if (isinstance(generation, str) and re.fullmatch(r"[A-Za-z0-9_-]+", generation)
                        and manifest.get("speakers") == f"diarization/{generation}/speakers.json"):
                    target = path / manifest["speakers"]
                    if not any(parent.is_symlink() for parent in (target.parent.parent, target.parent, target)):
                        content = self._content_file(target)["content"]
                        try:
                            speakers = json.loads(content)
                            if (isinstance(speakers, dict) and type(speakers.get("actual_speakers")) is int
                                    and speakers["actual_speakers"] >= 0 and isinstance(speakers.get("speakers"), list)
                                    and all(isinstance(row, dict) and isinstance(row.get("id"), str)
                                            and re.fullmatch(r"S[0-9]{2,}", row["id"])
                                            and type(row.get("speech_seconds")) in (int, float)
                                            and math.isfinite(row["speech_seconds"]) and row["speech_seconds"] >= 0
                                            and type(row.get("segment_count")) is int and row["segment_count"] >= 0
                                            for row in speakers["speakers"])):
                                detail["speakers"] = speakers
                        except ValueError:
                            pass
            return detail
        except OSError as exc:
            raise SessionStoreError(
                "session_unavailable", f"Unable to read session: {exc}",
            ) from exc

    def path_for(self, session_id):
        """Resolve an existing safe session for fixed CLI operations."""
        return self._session_dir(session_id)
