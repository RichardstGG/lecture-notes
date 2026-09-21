"""Safe course configuration reads and atomic validated updates."""
import asyncio
import os
import re
import stat
import tempfile
import tomllib
from pathlib import Path

from .cli_client import LecCommandError


MAX_COURSE_BYTES = 1024 * 1024
_INVALID_ID = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class CourseStoreError(Exception):
    def __init__(self, code, message, status_code=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_detail(self):
        return {"code": self.code, "message": self.message}


def _validate_course_id(course_id):
    course_id = str(course_id).strip()
    stem = course_id.split(".", 1)[0].upper()
    if (not course_id or len(course_id) > 100 or len(course_id.encode("utf-8")) > 200
            or course_id.startswith(("-", "."))
            or course_id.endswith((" ", ".")) or _INVALID_ID.search(course_id)
            or stem in _WINDOWS_RESERVED):
        raise CourseStoreError(
            "invalid_course_id",
            "Course id must be a portable filename without path separators or reserved characters",
            400,
        )
    return course_id


class CourseStore:
    def __init__(self, root, max_bytes=MAX_COURSE_BYTES):
        self.root = Path(root).resolve()
        self.max_bytes = int(max_bytes)
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings):
        return cls(settings.repo_root / "courses")

    def path_for(self, course_id):
        course_id = _validate_course_id(course_id)
        return self.root / f"{course_id}.toml"

    def _regular_file(self, path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise CourseStoreError("course_not_found", "Course not found", 404) from None
        if not stat.S_ISREG(info.st_mode):
            raise CourseStoreError("course_not_found", "Course not found", 404)
        if info.st_size > self.max_bytes:
            raise CourseStoreError("course_too_large", "Course configuration is too large", 413)
        return info

    def get(self, course_id):
        path = self.path_for(course_id)
        self._regular_file(path)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise CourseStoreError(
                "invalid_course_encoding", "Course configuration must be UTF-8", 400,
            ) from None
        return {
            "api_version": 1,
            "id": path.stem,
            "file": str(path),
            "content": content,
        }

    async def create(self, client, course_id):
        course_id = _validate_course_id(course_id)
        path = self.path_for(course_id)
        async with self._lock:
            if path.exists() or path.is_symlink():
                raise CourseStoreError("course_exists", "Course already exists", 409)
            try:
                await client.create_course(course_id)
            except LecCommandError as exc:
                if path.exists() or path.is_symlink():
                    raise CourseStoreError("course_exists", "Course already exists", 409) from exc
                raise
            return self.get(course_id)

    async def update(self, client, course_id, content):
        path = self.path_for(course_id)
        self._regular_file(path)
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise CourseStoreError("course_too_large", "Course configuration is too large", 413)
        try:
            tomllib.loads(content)
        except tomllib.TOMLDecodeError as exc:
            raise CourseStoreError(
                "invalid_course_config", f"Invalid TOML: {exc}", 400,
            ) from None

        async with self._lock:
            info = self._regular_file(path)
            validation_path = None
            write_path = path.with_suffix(".toml.tmp")
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".toml", delete=False,
                ) as validation:
                    validation.write(content)
                    validation_path = Path(validation.name)
                try:
                    await client.validate_course(validation_path)
                except LecCommandError as exc:
                    if exc.code == "cli_failed":
                        raise CourseStoreError(
                            "invalid_course_config", exc.message, 400,
                        ) from exc
                    raise

                self.root.mkdir(parents=True, exist_ok=True)
                with open(write_path, "w", encoding="utf-8", newline="") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(write_path, stat.S_IMODE(info.st_mode))
                write_path.replace(path)
            finally:
                if validation_path is not None:
                    validation_path.unlink(missing_ok=True)
                write_path.unlink(missing_ok=True)
        return self.get(course_id)
