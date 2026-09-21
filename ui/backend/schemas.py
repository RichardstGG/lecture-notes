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


class GlossaryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str
    means: str = ""
    aka: list[str] = Field(default_factory=list)


class CourseVocabulary(BaseModel):
    terms: list[str] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)


class CourseDetail(BaseModel):
    api_version: int = API_VERSION
    id: str
    file: str
    content: str
    vocabulary: CourseVocabulary | None = None


class CourseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=100)


class CourseUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str


class CourseVocabularyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terms: list[str] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)


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


class RunStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    course: str = Field(min_length=1)
    input_file: str | None = None
    model: str | None = None
    source: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False


class SummarizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    redo: str | None = Field(default=None, pattern=r"^(all|\d{2}:\d{2}:\d{2})$")
    model: str | None = None
    course: str | None = None


class ProcessActionResponse(BaseModel):
    accepted: bool
    operation: str
    pid: int | None = None
    completed: bool | None = None
    exit_code: int | None = None
    force: bool | None = None
    message: str


class ApiErrorDetail(BaseModel):
    code: str
    message: str
    exit_code: int | None = None
    stderr: str | None = None


class ApiErrorResponse(BaseModel):
    error: ApiErrorDetail
