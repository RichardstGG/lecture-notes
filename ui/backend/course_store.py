"""Safe course configuration reads and atomic validated updates."""
import asyncio
import json
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
_TABLE_HEADER = re.compile(r'^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?(?:\r?\n)?$')


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


def _normalize_vocabulary(data):
    """Return the two UI-managed vocabulary fields, or None if malformed."""
    whisper = data.get("whisper", {})
    summary = data.get("summary", {})
    if not isinstance(whisper, dict) or not isinstance(summary, dict):
        return None

    terms = whisper.get("terms", [])
    glossary = summary.get("glossary", {})
    if (not isinstance(terms, list) or not all(isinstance(term, str) for term in terms)
            or not isinstance(glossary, dict)):
        return None

    entries = []
    for term, value in glossary.items():
        if not isinstance(term, str):
            return None
        if isinstance(value, str):
            means, aliases = value, []
        elif isinstance(value, dict):
            means = value.get("means", "")
            aliases = value.get("aka", [])
            if (not isinstance(means, str) or not isinstance(aliases, list)
                    or not all(isinstance(alias, str) for alias in aliases)):
                return None
        else:
            return None
        entries.append({"term": term, "means": means, "aka": aliases})
    return {"terms": terms, "glossary": entries}


def _table_name(line):
    match = _TABLE_HEADER.match(line)
    if not match:
        return None
    return re.sub(r"\s*\.\s*", ".", match.group(1))


def _table_span(content, table_name):
    """Return header/body/end offsets for a canonical bare-key TOML table."""
    offset = 0
    header_start = body_start = None
    for line in content.splitlines(keepends=True):
        name = _table_name(line)
        if header_start is not None and name is not None:
            return header_start, body_start, offset
        if name == table_name:
            header_start = offset
            body_start = offset + len(line)
        offset += len(line)
    if header_start is not None:
        return header_start, body_start, len(content)
    return None


def _assignment_end(content, start, section_end, table_name, key):
    """Find a complete TOML assignment by parsing one additional line at a time."""
    offset = start
    for line in content[start:section_end].splitlines(keepends=True):
        offset += len(line)
        snippet = content[start:offset]
        try:
            parsed = tomllib.loads(f"[{table_name}]\n{snippet}")
        except tomllib.TOMLDecodeError:
            continue
        if key in parsed.get(table_name, {}):
            return offset
    raise CourseStoreError(
        "invalid_course_config", f"Unable to locate {table_name}.{key} assignment", 400,
    )


def _replace_terms(content, terms):
    newline = "\r\n" if "\r\n" in content else "\n"
    assignment = "terms = " + json.dumps(terms, ensure_ascii=False) + newline
    span = _table_span(content, "whisper")
    if span is None:
        separator = "" if not content or content.endswith(("\n", "\r")) else newline
        return content + separator + newline + "[whisper]" + newline + assignment

    _header_start, body_start, section_end = span
    match = re.search(r"(?m)^[ \t]*terms[ \t]*=", content[body_start:section_end])
    if match:
        start = body_start + match.start()
        end = _assignment_end(content, start, section_end, "whisper", "terms")
        return content[:start] + assignment + content[end:]

    before = content[:section_end]
    separator = "" if before.endswith(("\n", "\r")) else newline
    return before + separator + assignment + content[section_end:]


def _render_glossary(glossary, newline):
    lines = []
    for entry in glossary:
        term = json.dumps(entry["term"], ensure_ascii=False)
        means = json.dumps(entry["means"], ensure_ascii=False)
        aliases = json.dumps(entry["aka"], ensure_ascii=False)
        lines.append(f"{term} = {{ means = {means}, aka = {aliases} }}{newline}")
    return "".join(lines)


def _replace_glossary(content, glossary):
    newline = "\r\n" if "\r\n" in content else "\n"
    rendered = _render_glossary(glossary, newline)
    span = _table_span(content, "summary.glossary")
    if span is None:
        separator = "" if not content or content.endswith(("\n", "\r")) else newline
        return content + separator + newline + "[summary.glossary]" + newline + rendered

    _header_start, body_start, section_end = span
    body = content[body_start:section_end]
    comments = "".join(
        line for line in body.splitlines(keepends=True)
        if not line.strip() or line.lstrip().startswith("#")
    )
    if comments and not comments.endswith(("\n", "\r")):
        comments += newline
    return content[:body_start] + comments + rendered + content[section_end:]


def _clean_vocabulary(terms, glossary):
    clean_terms = []
    for value in terms:
        if not isinstance(value, str) or not value.strip():
            raise CourseStoreError(
                "invalid_course_vocabulary", "Whisper terms must be non-empty strings", 400,
            )
        value = value.strip()
        if value not in clean_terms:
            clean_terms.append(value)

    clean_glossary = []
    seen = set()
    for entry in glossary:
        term = str(entry.get("term", "")).strip()
        means = str(entry.get("means", "")).strip()
        aliases = entry.get("aka", [])
        if not term or not isinstance(aliases, list):
            raise CourseStoreError(
                "invalid_course_vocabulary", "Glossary terms and aliases are invalid", 400,
            )
        if term in seen:
            raise CourseStoreError(
                "invalid_course_vocabulary", f"Duplicate glossary term: {term}", 400,
            )
        seen.add(term)
        clean_aliases = []
        for alias in aliases:
            if not isinstance(alias, str) or not alias.strip():
                raise CourseStoreError(
                    "invalid_course_vocabulary", "Glossary aliases must be non-empty strings", 400,
                )
            alias = alias.strip()
            if alias != term and alias not in clean_aliases:
                clean_aliases.append(alias)
        clean_glossary.append({"term": term, "means": means, "aka": clean_aliases})
    return clean_terms, clean_glossary


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
        try:
            vocabulary = _normalize_vocabulary(tomllib.loads(content))
        except tomllib.TOMLDecodeError:
            vocabulary = None
        return {
            "api_version": 1,
            "id": path.stem,
            "file": str(path),
            "content": content,
            "vocabulary": vocabulary,
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

    def _validate_content(self, content):
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise CourseStoreError("course_too_large", "Course configuration is too large", 413)
        try:
            tomllib.loads(content)
        except tomllib.TOMLDecodeError as exc:
            raise CourseStoreError(
                "invalid_course_config", f"Invalid TOML: {exc}", 400,
            ) from None

    async def _replace_locked(self, client, path, content, info):
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

    async def update(self, client, course_id, content):
        path = self.path_for(course_id)
        self._regular_file(path)
        self._validate_content(content)
        async with self._lock:
            info = self._regular_file(path)
            await self._replace_locked(client, path, content, info)
        return self.get(course_id)

    async def update_vocabulary(self, client, course_id, terms, glossary):
        path = self.path_for(course_id)
        clean_terms, clean_glossary = _clean_vocabulary(terms, glossary)
        self._regular_file(path)
        async with self._lock:
            info = self._regular_file(path)
            try:
                content = path.read_text(encoding="utf-8")
                tomllib.loads(content)
            except UnicodeDecodeError:
                raise CourseStoreError(
                    "invalid_course_encoding", "Course configuration must be UTF-8", 400,
                ) from None
            except tomllib.TOMLDecodeError as exc:
                raise CourseStoreError(
                    "invalid_course_config", f"Invalid TOML: {exc}", 400,
                ) from None
            updated = _replace_glossary(
                _replace_terms(content, clean_terms), clean_glossary,
            )
            self._validate_content(updated)
            await self._replace_locked(client, path, updated, info)
        return self.get(course_id)
