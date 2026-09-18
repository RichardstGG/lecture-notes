"""FastAPI application for the browser UI."""
import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .cli_client import LecClient, LecCommandError
from .schemas import (ApiErrorResponse, CourseSummary, HealthResponse,
                      RuntimeStatusResponse, SessionDetail, SessionSummary)
from .session_store import SessionStore, SessionStoreError
from .settings import BackendSettings


def _sse(event, data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


async def _status_events(request, client, interval):
    previous = None
    while not await request.is_disconnected():
        try:
            status = await client.status()
            marker = json.dumps(status, ensure_ascii=False, sort_keys=True)
            if marker != previous:
                previous = marker
                yield _sse("status", status)
            else:
                yield ": heartbeat\n\n"
        except LecCommandError as exc:
            yield _sse("error", exc.as_detail())
        await asyncio.sleep(interval)


async def _session_events(request, sessions, session_id, interval, initial=None):
    previous = None
    while not await request.is_disconnected():
        try:
            current = initial if initial is not None else sessions.get(session_id)
            initial = None
            if previous is None:
                yield _sse("snapshot", current)
            else:
                changed = False
                for target in ("transcript", "notes"):
                    before = previous[target]["content"]
                    after = current[target]["content"]
                    if before == after:
                        continue
                    changed = True
                    append = after.startswith(before)
                    yield _sse("content", {
                        "api_version": 1,
                        "target": target,
                        "operation": "append" if append else "replace",
                        "content": after[len(before):] if append else after,
                        "updated_at": current[target]["updated_at"],
                        "size_bytes": current[target]["size_bytes"],
                    })
                if not changed:
                    yield ": heartbeat\n\n"
            previous = current
        except SessionStoreError as exc:
            yield _sse("error", exc.as_detail())
        await asyncio.sleep(interval)


def create_app(settings=None, client=None, sessions=None):
    settings = settings or BackendSettings.from_env()
    client = client or LecClient.for_repo(settings.repo_root, settings.cli_timeout)
    sessions = sessions or SessionStore.from_settings(settings)
    app = FastAPI(
        title="Lecture Notes UI API",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.lec_client = client
    app.state.sessions = sessions
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.exception_handler(LecCommandError)
    async def lec_error_handler(_request, exc):
        status_code = 503 if exc.code in {"cli_unavailable", "cli_timeout"} else 502
        return JSONResponse(status_code=status_code, content={"error": exc.as_detail()})

    @app.exception_handler(SessionStoreError)
    async def session_error_handler(_request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.as_detail()})

    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health():
        return HealthResponse()

    @app.get(
        "/api/v1/status", response_model=RuntimeStatusResponse,
        response_model_exclude_none=True,
        responses={502: {"model": ApiErrorResponse}, 503: {"model": ApiErrorResponse}},
    )
    async def status(request: Request):
        return await request.app.state.lec_client.status()

    @app.get(
        "/api/v1/courses", response_model=list[CourseSummary],
        response_model_exclude_none=True,
        responses={502: {"model": ApiErrorResponse}, 503: {"model": ApiErrorResponse}},
    )
    async def courses(request: Request):
        return await request.app.state.lec_client.courses()

    @app.get(
        "/api/v1/sessions", response_model=list[SessionSummary],
        response_model_exclude_none=True,
        responses={500: {"model": ApiErrorResponse}},
    )
    async def session_list(request: Request):
        return request.app.state.sessions.list()

    @app.get(
        "/api/v1/sessions/{session_id}", response_model=SessionDetail,
        response_model_exclude_none=True,
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse},
                   413: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
    )
    async def session_detail(session_id: str, request: Request):
        return request.app.state.sessions.get(session_id)

    @app.get(
        "/api/v1/sessions/{session_id}/stream", response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
                   400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    )
    async def session_stream(session_id: str, request: Request):
        initial = request.app.state.sessions.get(session_id)
        stream = _session_events(
            request, request.app.state.sessions, session_id,
            request.app.state.settings.status_poll_interval, initial=initial,
        )
        return StreamingResponse(
            stream, media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/v1/status/stream", response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
    )
    async def status_stream(request: Request):
        stream = _status_events(
            request, request.app.state.lec_client,
            request.app.state.settings.status_poll_interval,
        )
        return StreamingResponse(
            stream, media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
