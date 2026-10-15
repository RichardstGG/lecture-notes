"""Meeting TOML editing; the CLI owns all configuration semantics."""
import asyncio
import os
import stat
import tempfile
from pathlib import Path

from .cli_client import LecCommandError
from .course_store import CourseStore, CourseStoreError, _validate_course_id


class MeetingStore(CourseStore):
    def path_for(self, meeting_id):
        # Same portable namespace as course ids, plus DEL (core meeting rule).
        if meeting_id != meeting_id.strip() or "\x7f" in meeting_id:
            raise CourseStoreError("invalid_meeting_id", "Invalid meeting id", 400)
        try:
            identifier = _validate_course_id(meeting_id)
        except CourseStoreError as exc:
            raise CourseStoreError("invalid_meeting_id", "Invalid portable meeting id", 400) from exc
        return self.root / f"{identifier}.toml"

    def _regular_file(self, path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise CourseStoreError("meeting_not_found", "Meeting not found", 404) from None
        if not stat.S_ISREG(info.st_mode):
            raise CourseStoreError("meeting_not_found", "Meeting not found", 404)
        if info.st_size > self.max_bytes:
            raise CourseStoreError("meeting_too_large", "Meeting configuration is too large", 413)
        return info

    def get(self, meeting_id):
        try:
            result = super().get(meeting_id)
        except OSError as exc:
            raise CourseStoreError("meeting_unavailable", "Unable to read meeting settings", 500) from exc
        return {key: result[key] for key in ("api_version", "id", "file", "content")}

    async def write(self, client, meeting_id, content, *, create=False):
        path = self.path_for(meeting_id)
        self._validate_content(content)
        try:
            async with self._lock:
                if create:
                    if path.exists() or path.is_symlink():
                        raise CourseStoreError("meeting_exists", "Meeting already exists", 409)
                    info = None
                else:
                    info = self._regular_file(path)
                self.root.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                                     dir=self.root, prefix=".meeting-",
                                                     suffix=".tmp", delete=False) as output:
                        temporary = Path(output.name)
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    try:
                        await client.validate_meeting(temporary)
                    except LecCommandError as exc:
                        if exc.code == "cli_failed":
                            raise CourseStoreError("invalid_meeting_config", exc.message, 400) from exc
                        raise
                    if create:
                        try:
                            # Atomic no-replace publication across API processes too.
                            os.link(temporary, path)
                        except FileExistsError:
                            raise CourseStoreError("meeting_exists", "Meeting already exists", 409) from None
                    else:
                        self._regular_file(path)
                        os.chmod(temporary, info.st_mode & 0o777)
                        os.replace(temporary, path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise CourseStoreError("meeting_write_failed", "Unable to write meeting settings", 500) from exc
        return await asyncio.to_thread(self.get, meeting_id)
