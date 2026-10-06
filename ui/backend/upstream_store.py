"""Write-only connection settings for private summary API upstreams."""
import json
import os
import re
import stat
import tempfile
import threading
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


UPSTREAM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class UpstreamStoreError(Exception):
    def __init__(self, code, message, status_code=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_detail(self):
        return {"code": self.code, "message": self.message}


def _string(value, field, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise UpstreamStoreError("invalid_upstream", f"Invalid {field}")
    return value.strip()


def _validate(upstream_id, payload):
    if not isinstance(upstream_id, str) or not UPSTREAM_ID.fullmatch(upstream_id) or upstream_id == "local":
        raise UpstreamStoreError("invalid_upstream_id", "Invalid or reserved upstream id")
    if not isinstance(payload, dict):
        raise UpstreamStoreError("invalid_upstream", "Invalid upstream settings")
    name = _string(payload.get("name"), "name", 200)
    base_url = _string(payload.get("base_url"), "base_url", 2048).rstrip("/")
    model = _string(payload.get("model"), "model", 500)
    try:
        url = urlsplit(base_url)
        _ = url.port
    except ValueError:
        raise UpstreamStoreError("invalid_upstream", "Invalid base_url") from None
    if (any(char.isspace() for char in base_url)
            or url.scheme not in {"http", "https"} or not url.hostname
            or url.username or url.password or url.query or url.fragment):
        raise UpstreamStoreError("invalid_upstream", "Invalid base_url")

    auth_mode = payload.get("auth_mode", "none")
    if auth_mode not in {"none", "api_key", "environment"}:
        raise UpstreamStoreError("invalid_upstream", "Invalid auth_mode")
    result = {"name": name, "base_url": base_url, "model": model}
    if auth_mode == "api_key":
        result["api_key"] = _string(payload.get("api_key"), "api_key", 8192)
    elif auth_mode == "environment":
        env_name = _string(payload.get("api_key_env"), "api_key_env", 128)
        if not ENV_NAME.fullmatch(env_name):
            raise UpstreamStoreError("invalid_upstream", "Invalid api_key_env")
        result["api_key_env"] = env_name
    return result


def _quote(value):
    return json.dumps(value, ensure_ascii=False)


def _render(data):
    lines = ["# 私有摘要 API 上游；由本機 UI 管理，不進 Git。", "", "[upstreams]", ""]
    for upstream_id, spec in data.items():
        lines.append(f"[upstreams.{upstream_id}]")
        for key in ("name", "base_url", "model", "api_key", "api_key_env"):
            if key in spec:
                lines.append(f"{key} = {_quote(spec[key])}")
        lines.append("")
    return "\n".join(lines)


class UpstreamStore:
    """Manage the private registry while returning only a safe public projection."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    @classmethod
    def from_settings(cls, settings):
        return cls(settings.repo_root / "config" / "upstreams.toml")

    def _read(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or not self.path.is_file():
            raise UpstreamStoreError("upstream_store_unavailable", "Unable to read upstream settings", 500)
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise UpstreamStoreError("upstream_store_too_large", "Upstream settings are too large", 413)
            with self.path.open("rb") as source:
                raw = tomllib.load(source)
            upstreams = raw.get("upstreams", {})
            if set(raw) != {"upstreams"} or not isinstance(upstreams, dict):
                raise ValueError
            result = {}
            for key, spec in upstreams.items():
                if (not isinstance(spec, dict)
                        or set(spec) - {"name", "base_url", "model", "api_key", "api_key_env"}
                        or "api_key" in spec and "api_key_env" in spec):
                    raise ValueError
                result[key] = _validate(key, {
                    "name": spec.get("name"), "base_url": spec.get("base_url"),
                    "model": spec.get("model"),
                    "auth_mode": ("api_key" if "api_key" in spec else
                                  "environment" if "api_key_env" in spec else "none"),
                    "api_key": spec.get("api_key"), "api_key_env": spec.get("api_key_env"),
                })
            return result
        except UpstreamStoreError:
            raise
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
            raise UpstreamStoreError(
                "invalid_upstream_store",
                "config/upstreams.toml cannot be read or contains invalid settings",
                400,
            ) from None

    @staticmethod
    def _public(upstream_id, spec):
        auth_mode = ("api_key" if "api_key" in spec else
                     "environment" if "api_key_env" in spec else "none")
        return {"id": upstream_id, "name": spec["name"], "kind": "api",
                "auth_mode": auth_mode}

    def list(self):
        with self._lock:
            data = self._read()
            return {"api_version": 1, "upstreams": [
                self._public(key, spec) for key, spec in data.items()
            ]}

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.is_symlink():
            raise UpstreamStoreError("upstream_store_unavailable", "Unable to write upstream settings", 500)
        fd, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent,
        )
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
                target.write(_render(data))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise UpstreamStoreError("upstream_store_unavailable", "Unable to write upstream settings", 500) from None

    def create(self, upstream_id, payload):
        spec = _validate(upstream_id, payload)
        with self._lock:
            data = self._read()
            if upstream_id in data:
                raise UpstreamStoreError("upstream_exists", "Upstream already exists", 409)
            data[upstream_id] = spec
            self._write(data)
            return self._public(upstream_id, spec)

    def update(self, upstream_id, payload):
        spec = _validate(upstream_id, payload)
        with self._lock:
            data = self._read()
            if upstream_id not in data:
                raise UpstreamStoreError("upstream_not_found", "Upstream not found", 404)
            data[upstream_id] = spec
            self._write(data)
            return self._public(upstream_id, spec)

    def delete(self, upstream_id):
        if not isinstance(upstream_id, str) or not UPSTREAM_ID.fullmatch(upstream_id) or upstream_id == "local":
            raise UpstreamStoreError("invalid_upstream_id", "Invalid or reserved upstream id")
        with self._lock:
            data = self._read()
            if upstream_id not in data:
                raise UpstreamStoreError("upstream_not_found", "Upstream not found", 404)
            del data[upstream_id]
            self._write(data)
            return {"api_version": 1, "deleted": upstream_id}
