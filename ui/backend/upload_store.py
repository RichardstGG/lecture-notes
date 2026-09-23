"""Stream browser-selected media into a local, process-safe staging directory."""
import os
import uuid
from pathlib import Path


SUPPORTED_MEDIA_SUFFIXES = {
    ".3gp", ".aac", ".aif", ".aiff", ".amr", ".caf", ".flac", ".m4a",
    ".mka", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg", ".mpg", ".mts",
    ".oga", ".ogg", ".opus", ".ts", ".wav", ".webm", ".wma",
}


class UploadStoreError(Exception):
    def __init__(self, code, message, status_code=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_detail(self):
        return {"code": self.code, "message": self.message}


class AudioUploadStore:
    def __init__(self, root, max_bytes):
        self.root = Path(root).resolve()
        self.max_bytes = int(max_bytes)

    def _validate_name(self, filename):
        if (not filename or filename in {".", ".."} or "\x00" in filename
                or "/" in filename or "\\" in filename
                or any(ord(char) < 32 for char in filename)):
            raise UploadStoreError("invalid_upload_name", "Invalid audio filename")
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_MEDIA_SUFFIXES:
            formats = ", ".join(sorted(SUPPORTED_MEDIA_SUFFIXES))
            raise UploadStoreError(
                "unsupported_media_type",
                f"Unsupported media extension {suffix or '(none)'}; supported: {formats}",
                415,
            )
        return suffix

    async def save(self, filename, chunks, content_length=None):
        suffix = self._validate_name(filename)
        if content_length is not None:
            try:
                content_length = int(content_length)
            except (TypeError, ValueError) as exc:
                raise UploadStoreError(
                    "invalid_content_length", "Invalid Content-Length header",
                ) from exc
            if content_length < 0:
                raise UploadStoreError(
                    "invalid_content_length", "Invalid Content-Length header",
                )
            if content_length > self.max_bytes:
                raise UploadStoreError(
                    "upload_too_large",
                    f"Audio file exceeds the {self.max_bytes}-byte upload limit",
                    413,
                )

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            target = self.root / f"{uuid.uuid4().hex}{suffix}"
            partial = target.with_suffix(f"{suffix}.part")
            size = 0
            with open(partial, "xb") as output:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise UploadStoreError(
                            "upload_too_large",
                            f"Audio file exceeds the {self.max_bytes}-byte upload limit",
                            413,
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise UploadStoreError("empty_upload", "Audio file is empty")
            os.replace(partial, target)
        except UploadStoreError:
            if "partial" in locals():
                partial.unlink(missing_ok=True)
            raise
        except OSError as exc:
            if "partial" in locals():
                partial.unlink(missing_ok=True)
            raise UploadStoreError(
                "upload_failed", f"Unable to store audio file: {exc}", 500,
            ) from exc

        return {
            "api_version": 1,
            "name": filename,
            "path": str(target),
            "size_bytes": size,
        }
