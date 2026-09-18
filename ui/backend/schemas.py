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


class SessionSummary(BaseModel):
    id: str
    course: str | None = None
    started_at: str
    updated_at: str
    phase: str | None = None
    mode: str | None = None
    elapsed: float | None = None
    sections_total: int | None = Field(default=None, ge=0)
    sections_summarized: int | None = Field(default=None, ge=0)
    has_transcript: bool
    has_notes: bool
    has_recording: bool


class SessionContentFile(BaseModel):
    content: str
    updated_at: str | None = None
    size_bytes: int = Field(ge=0)


class SessionDetail(BaseModel):
    api_version: int = API_VERSION
    session: SessionSummary
    transcript: SessionContentFile
    notes: SessionContentFile


class ApiErrorDetail(BaseModel):
    code: str
    message: str
    exit_code: int | None = None
    stderr: str | None = None


class ApiErrorResponse(BaseModel):
    error: ApiErrorDetail
