"""Async subprocess boundary between the UI service and the existing CLI."""
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class LecCommandError(Exception):
    code: str
    message: str
    exit_code: int | None = None
    stderr: str = ""

    def __str__(self):
        return self.message

    def as_detail(self):
        detail: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.exit_code is not None:
            detail["exit_code"] = self.exit_code
        if self.stderr:
            detail["stderr"] = self.stderr
        return detail


class LecClient:
    """Run only fixed, structured `lec` commands without importing `core`."""

    def __init__(self, command, cwd, timeout=30.0):
        self.command = tuple(str(part) for part in command)
        self.cwd = Path(cwd)
        self.timeout = float(timeout)

    @classmethod
    def for_repo(cls, repo_root, timeout=30.0):
        root = Path(repo_root).resolve()
        return cls((sys.executable, root / "lec"), cwd=root, timeout=timeout)

    async def run_text(self, *args):
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command, *map(str, args), cwd=self.cwd, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise LecCommandError(
                "cli_unavailable", f"Unable to start lec: {exc}", stderr=str(exc),
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
        except TimeoutError as exc:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise LecCommandError(
                "cli_timeout", f"lec did not finish within {self.timeout:g} seconds",
            ) from exc
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode:
            raise LecCommandError(
                "cli_failed", err or out.strip() or "lec command failed",
                exit_code=process.returncode, stderr=err[:2000],
            )
        return out

    async def run_json(self, *args):
        out = await self.run_text(*args)
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise LecCommandError(
                "cli_invalid_json", "lec returned an invalid JSON response",
            ) from exc

    async def status(self):
        result = await self.run_json("status", "--json")
        if not isinstance(result, dict) or not isinstance(result.get("running"), bool):
            raise LecCommandError("cli_invalid_response", "lec status returned an invalid response")
        return result

    async def courses(self):
        result = await self.run_json("courses", "--json")
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise LecCommandError("cli_invalid_response", "lec courses returned an invalid response")
        return result

    async def models(self):
        result = await self.run_json("models", "--json")
        if not isinstance(result, dict) or not isinstance(result.get("schema_version"), int):
            raise LecCommandError("cli_invalid_response", "lec models returned an invalid response")
        for key in ("summary", "whisper"):
            group = result.get(key)
            if (not isinstance(group, dict) or not isinstance(group.get("selected"), str)
                    or not isinstance(group.get("models"), list)
                    or not all(isinstance(item, dict) for item in group["models"])):
                raise LecCommandError(
                    "cli_invalid_response", "lec models returned an invalid response",
                )
        return result

    async def create_course(self, course_id):
        return (await self.run_text("new", course_id)).strip()

    async def validate_course(self, path):
        return await self.run_text("config", str(Path(path).resolve()))

    async def stop(self, force=False):
        args = ["stop"]
        if force:
            args.append("--force")
        return (await self.run_text(*args)).strip()
