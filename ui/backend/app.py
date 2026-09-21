"""FastAPI application for the browser UI."""
import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .cli_client import LecClient, LecCommandError
from .course_store import CourseStore, CourseStoreError
from .process_control import (ControlError, LecProcessLauncher,
                              ProcessController)
from .schemas import (ApiErrorResponse, CourseCreateRequest, CourseDetail,
                      CourseSummary, CourseUpdateRequest,
                      CourseVocabularyUpdateRequest, HealthResponse,
                      ProcessActionResponse, RunStartRequest,
                      RuntimeStatusResponse, SessionDetail, SessionSummary,
                      StopRequest, SummarizeRequest)
from .session_store import SessionStore, SessionStoreError
from .settings import BackendSettings


def _sse(event, data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


async def _wait_or_shutdown(interval, shutdown_event):
    if shutdown_event is None:
        await asyncio.sleep(interval)
        return False
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
        return True
    except TimeoutError:
        return False


async def _status_events(request, client, interval, shutdown_event=None):
    previous = None
    while (not (shutdown_event and shutdown_event.is_set())
           and not await request.is_disconnected()):
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
        if await _wait_or_shutdown(interval, shutdown_event):
            break


async def _session_events(
    request, sessions, session_id, interval, initial=None, shutdown_event=None,
):
    previous = None
    while (not (shutdown_event and shutdown_event.is_set())
           and not await request.is_disconnected()):
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
        if await _wait_or_shutdown(interval, shutdown_event):
            break


def create_app(
    settings=None, client=None, sessions=None, launcher=None, course_store=None,
):
    settings = settings or BackendSettings.from_env()
    client = client or LecClient.for_repo(settings.repo_root, settings.cli_timeout)
    sessions = sessions or SessionStore.from_settings(settings)
    course_store = course_store or CourseStore.from_settings(settings)
    launcher = launcher or LecProcessLauncher.for_client(client, settings.process_log)
    controller = ProcessController(client, launcher, sessions, settings.repo_root)
    app = FastAPI(
        title="Lecture Notes UI API",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.lec_client = client
    app.state.sessions = sessions
    app.state.course_store = course_store
    app.state.controller = controller
    app.state.shutdown_event = asyncio.Event()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["*"],
    )

    @app.exception_handler(LecCommandError)
    async def lec_error_handler(_request, exc):
        status_code = 503 if exc.code in {"cli_unavailable", "cli_timeout"} else 502
        return JSONResponse(status_code=status_code, content={"error": exc.as_detail()})

    @app.exception_handler(SessionStoreError)
    async def session_error_handler(_request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.as_detail()})

    @app.exception_handler(ControlError)
    async def control_error_handler(_request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.as_detail()})

    @app.exception_handler(CourseStoreError)
    async def course_store_error_handler(_request, exc):
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

    @app.post(
        "/api/v1/courses", response_model=CourseDetail, status_code=201,
        responses={400: {"model": ApiErrorResponse}, 409: {"model": ApiErrorResponse},
                   502: {"model": ApiErrorResponse}, 503: {"model": ApiErrorResponse}},
    )
    async def course_create(payload: CourseCreateRequest, request: Request):
        return await request.app.state.course_store.create(
            request.app.state.lec_client, payload.id,
        )

    @app.get(
        "/api/v1/courses/{course_id}", response_model=CourseDetail,
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse},
                   413: {"model": ApiErrorResponse}},
    )
    async def course_detail(course_id: str, request: Request):
        return request.app.state.course_store.get(course_id)

    @app.put(
        "/api/v1/courses/{course_id}", response_model=CourseDetail,
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse},
                   413: {"model": ApiErrorResponse}, 502: {"model": ApiErrorResponse},
                   503: {"model": ApiErrorResponse}},
    )
    async def course_update(
        course_id: str, payload: CourseUpdateRequest, request: Request,
    ):
        return await request.app.state.course_store.update(
            request.app.state.lec_client, course_id, payload.content,
        )

    @app.put(
        "/api/v1/courses/{course_id}/vocabulary", response_model=CourseDetail,
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse},
                   413: {"model": ApiErrorResponse}, 502: {"model": ApiErrorResponse},
                   503: {"model": ApiErrorResponse}},
    )
    async def course_vocabulary_update(
        course_id: str, payload: CourseVocabularyUpdateRequest, request: Request,
    ):
        return await request.app.state.course_store.update_vocabulary(
            request.app.state.lec_client, course_id, payload.terms,
            [entry.model_dump() for entry in payload.glossary],
        )

    @app.post(
        "/api/v1/runs", response_model=ProcessActionResponse,
        response_model_exclude_none=True, status_code=202,
        responses={400: {"model": ApiErrorResponse}, 409: {"model": ApiErrorResponse},
                   502: {"model": ApiErrorResponse}, 503: {"model": ApiErrorResponse}},
    )
    async def run_start(payload: RunStartRequest, request: Request):
        return await request.app.state.controller.start_run(
            payload.course, input_file=payload.input_file, model=payload.model,
            source=payload.source, overrides=payload.overrides,
        )

    @app.post(
        "/api/v1/runs/stop", response_model=ProcessActionResponse,
        response_model_exclude_none=True, status_code=202,
        responses={502: {"model": ApiErrorResponse}, 503: {"model": ApiErrorResponse}},
    )
    async def run_stop(payload: StopRequest, request: Request):
        return await request.app.state.controller.stop(force=payload.force)

    @app.get(
        "/api/v1/sessions", response_model=list[SessionSummary],
        response_model_exclude_none=True,
        responses={500: {"model": ApiErrorResponse}},
    )
    async def session_list(request: Request):
        return request.app.state.sessions.list()

    @app.post(
        "/api/v1/sessions/{session_id:path}/summarize",
        response_model=ProcessActionResponse, response_model_exclude_none=True,
        status_code=202,
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse},
                   409: {"model": ApiErrorResponse}, 502: {"model": ApiErrorResponse},
                   503: {"model": ApiErrorResponse}},
    )
    async def session_summarize(
        session_id: str, payload: SummarizeRequest, request: Request,
    ):
        return await request.app.state.controller.summarize(
            session_id, redo=payload.redo, model=payload.model, course=payload.course,
        )

    @app.get(
        "/api/v1/sessions/{session_id:path}/stream", response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
                   400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    )
    async def session_stream(session_id: str, request: Request):
        initial = request.app.state.sessions.get(session_id)
        stream = _session_events(
            request, request.app.state.sessions, session_id,
            request.app.state.settings.status_poll_interval, initial=initial,
            shutdown_event=request.app.state.shutdown_event,
        )
        return StreamingResponse(
            stream, media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/v1/sessions/{session_id:path}", response_model=SessionDetail,
        response_model_exclude_none=True,
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse},
                   413: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
    )
    async def session_detail(session_id: str, request: Request):
        return request.app.state.sessions.get(session_id)

    @app.get(
        "/api/v1/status/stream", response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
    )
    async def status_stream(request: Request):
        stream = _status_events(
            request, request.app.state.lec_client,
            request.app.state.settings.status_poll_interval,
            shutdown_event=request.app.state.shutdown_event,
        )
        return StreamingResponse(
            stream, media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    frontend_dist = settings.frontend_dist
    if frontend_dist is not None and (frontend_dist / "index.html").is_file():
        frontend_index_html = (frontend_dist / "index.html").read_text(encoding="utf-8")
        assets = frontend_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

        @app.get("/", include_in_schema=False, response_class=HTMLResponse)
        async def frontend_index():
            return HTMLResponse(frontend_index_html)

    return app


app = create_app()
