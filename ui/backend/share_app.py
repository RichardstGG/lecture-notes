"""Independent LAN HTTP application; deliberately imports no control application."""
import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .sharing import ShareError

COOKIE = 'lecture_share_guest'
STATIC = Path(__file__).with_name('share_static')


class JoinRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    invitation: str = Field(min_length=1, max_length=128)
    nickname: str = Field(min_length=1, max_length=40)


def create_share_app(room, authority):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ShareError)
    async def share_error(_request, exc):
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message}},
                            status_code=exc.status_code)

    @app.middleware('http')
    async def boundary(request, call_next):
        origin = request.headers.get('origin')
        if request.headers.get('host') != authority or (origin and origin != f'http://{authority}'):
            return JSONResponse({'error': {'code': 'invalid_origin', 'message': '不允許此來源。'}}, status_code=403)
        if not room.active:
            return JSONResponse({'error': {'code': 'share_closed', 'message': '分享已關閉。'}}, status_code=410)
        if request.method == 'POST' and request.headers.get('content-type', '').split(';')[0] != 'application/json':
            return Response(status_code=415)
        response = await call_next(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        })
        return response

    @app.get('/')
    async def index():
        return FileResponse(STATIC / 'index.html')

    @app.get('/reader.js')
    async def script():
        return FileResponse(STATIC / 'reader.js', media_type='text/javascript')

    @app.get('/reader.css')
    async def style():
        return FileResponse(STATIC / 'reader.css', media_type='text/css')

    @app.post('/share/v1/join')
    async def join(payload: JoinRequest, request: Request):
        token = room.join(payload.invitation, payload.nickname, request.cookies.get(COOKIE))
        response = JSONResponse({'joined': True})
        response.set_cookie(COOKIE, token, httponly=True, samesite='strict', path='/')
        return response

    @app.get('/share/v1/snapshot')
    async def snapshot(request: Request):
        room.authorize(request.cookies.get(COOKIE))
        data = room.snapshot()
        marker = json.dumps([data['version'], data['course'], data['phase']], ensure_ascii=False)
        etag = '"' + hashlib.sha256(marker.encode()).hexdigest() + '"'
        if request.headers.get('if-none-match') == etag:
            return Response(status_code=304, headers={'ETag': etag})
        return JSONResponse(data, headers={'ETag': etag})

    @app.post('/share/v1/leave')
    async def leave(request: Request):
        room.leave(request.cookies.get(COOKIE))
        response = JSONResponse({'left': True})
        response.delete_cookie(COOKIE, path='/')
        return response

    @app.get('/share/v1/download/{target}')
    async def download(target: str, request: Request):
        room.authorize(request.cookies.get(COOKIE))
        if target not in {'transcript', 'notes'}:
            return Response(status_code=404)
        data = room.snapshot()
        label = '逐字稿' if target == 'transcript' else '筆記'
        content = (f'# {label} — 目前版本\n\n'
                   f'擷取時間：{data["captured_at"]}\n\n'
                   f'版本：{data["version"]}（內容仍可能更新）\n\n---\n\n'
                   + data[target]['content'])
        return Response(content, media_type='text/markdown; charset=utf-8', headers={
            'Content-Disposition': f'attachment; filename="{target}-current-version-{data["version"]}.md"',
        })

    return app
