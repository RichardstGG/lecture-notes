"""Versioned HTTP response models for the UI backend."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


API_VERSION = 1


class HealthResponse(BaseModel):
    service: str = "lecture-notes-ui"
    api_version: int = API_VERSION


class RuntimeStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 0
    running: bool
    pid: int | None = None
    started_at: str | None = None
    course: str | None = None
    session: str | None = None
    mode: str | None = None
    status: dict[str, Any] | None = None


class CourseSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    file: str
    id: str
    name: str | None = None
    model: str | None = None
    terms: int | None = Field(default=None, ge=0)
    error: str | None = None


class ApiErrorDetail(BaseModel):
    code: str
    message: str
    exit_code: int | None = None
    stderr: str | None = None


class ApiErrorResponse(BaseModel):
    error: ApiErrorDetail
