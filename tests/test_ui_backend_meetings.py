"""Meeting HTTP contracts using real CLI processes/locks and mock audio engines."""
import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

import httpx

from tests import _pathfix  # noqa: F401
from ui.backend.app import create_app, _session_events
from ui.backend.cli_client import LecClient
from ui.backend.process_control import LecProcessLauncher
from ui.backend.settings import BackendSettings


# No hardware/model execution. Everything above the engine boundary is real,
# including configuration validation, source persistence, OS locks and stop files.
SCRIPT = r'''
import sys, time, uuid, json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from core import cli, config as C
from core.diarize import DiarizationCancelled
root = Path(sys.argv[2])
C.LOCAL_FILE = root / 'config/local.toml'
C.MEETINGS_DIR = root / 'meetings'
args = sys.argv[3:]
if args[:1] == ['meeting'] and (root / 'reject').exists():
    time.sleep(.3)  # Longer than the legacy launcher's 150ms settle period.
    code = (root / 'reject').read_text()
    print(json.dumps(dict(schema_version=1, accepted=False, error=dict(code=code, message='startup rejected'))))
    sys.exit(3 if code == 'busy' else 2)
class Whisper:
    url = 'http://127.0.0.1:8178'
    def __init__(self, cfg): pass
    def ensure(self, path): pass
    def stop(self): pass
    def kill_now(self): time.sleep(.05)  # Exercise transcriber return during force cleanup.
class Transcriber:
    def __init__(self, cfg, session, url, source, status):
        self.session, self.status, self.source = session, status, source
        self.stopping = False
        self.abort = False
        self.proc = self
    def kill(self):
        if not self.stopping: self.status.error('ffmpeg 結束代碼 -9')
    def request_stop(self): self.stopping = True
    def run(self):
        if self.source is None:
            self.recording_path = self.session / 'recording_120000.ogg'
            self.recording_path.write_bytes(b'complete recording')
        (self.session / 'transcript.md').write_text('original transcript', encoding='utf-8')
        (root / 'entered-run').touch()
        while not self.stopping and not (root / 'finish').exists(): time.sleep(.02)
        self.status.event('capture_integrity', lost_ratio=0)
        return dict(duration=12.5, ffmpeg_failed=False, capture=dict(lost_ratio=0),
                    aborted=self.source is not None and self.stopping)
def diarize(request, progress, cancel):
    progress(dict(stage='segmentation', processed_seconds=1, total_seconds=12.5, speakers_found=1))
    (root / 'entered-diarize').touch()
    while not (root / 'finish').exists():
        if cancel(): raise DiarizationCancelled()
        time.sleep(.02)
    progress(dict(stage='retranscription', processed_seconds=12.5, total_seconds=12.5, speakers_found=1))
    generation = uuid.uuid4().hex
    target = request.session_dir / 'diarization' / generation
    target.mkdir(parents=True)
    (target / 'transcript.speakers.md').write_text('S01: result ' + generation, encoding='utf-8')
    (target / 'speakers.json').write_text(json.dumps(dict(schema_version=1, actual_speakers=1,
        speakers=[dict(id='S01', speech_seconds=12.5, segment_count=1)])), encoding='utf-8')
    (request.session_dir / 'diarization.current.json').write_text(json.dumps(dict(schema_version=1,
        generation=generation, speaker_transcript=f'diarization/{generation}/transcript.speakers.md',
        speakers=f'diarization/{generation}/speakers.json')), encoding='utf-8')
    return SimpleNamespace(actual_speakers=1, duration_seconds=12.5)
with patch('core.session.WhisperServer', Whisper), patch('core.session.Transcriber', Transcriber), \
     patch('core.session.source_duration', return_value=12.5), patch('core.diarize.diarize_session', diarize):
    sys.exit(cli.main(args))
'''


class MeetingApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'config').mkdir()
        (self.root / 'meetings').mkdir()
        (self.root / 'meetings/team.toml').write_text('[meeting]\nname="Team"', encoding='utf-8')
        (self.root / 'config/local.toml').write_text(
            f'[paths]\noutput_root="{(self.root / "outputs").as_posix()}"\nstate_dir="{(self.root / "state").as_posix()}"\n'
            f'[diarization]\nsegmentation_model="{(self.root / "seg").as_posix()}"\nembedding_model="{(self.root / "emb").as_posix()}"\n'
            '[system]\ninhibit_sleep=false\nstatus_interval=0.02', encoding='utf-8')
        for name in ('seg', 'emb'):
            (self.root / name).write_bytes(b'model')
        repo = Path(__file__).resolve().parents[1]
        self.client = LecClient((sys.executable, '-c', SCRIPT, repo, self.root), self.root)
        self.launcher = LecProcessLauncher.for_client(self.client, self.root / 'process.log')
        settings = BackendSettings(self.root, upload_root=self.root / 'uploads', status_poll_interval=.01)
        self.app = create_app(settings, client=self.client, launcher=self.launcher)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        (self.root / 'finish').touch()
        for process in tuple(self.launcher._processes):
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except Exception:
                process.kill()
                await asyncio.to_thread(process.wait)
        await asyncio.gather(*self.launcher._tasks, return_exceptions=True)
        await self.http.aclose()
        self.tmp.cleanup()

    async def wait_path(self, path):
        for _ in range(250):
            if path.exists():
                return
            await asyncio.sleep(.02)
        self.fail(f'Timed out waiting for {path}')

    async def idle(self):
        for _ in range(250):
            if not (await self.client.status())['running']:
                return
            await asyncio.sleep(.02)
        self.fail('Job did not stop')

    async def start(self, imported=False):
        data = dict(meeting_id='team', speakers=3)
        if imported:
            uploaded = await self.http.post('/api/v1/audio-uploads?filename=test8min.ogg', content=b'audio')
            self.assertEqual(uploaded.status_code, 201)
            data['input_file'] = uploaded.json()['path']
            data['original_name'] = uploaded.json()['name']
        response = await self.http.post('/api/v1/meeting-runs', json=data)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['work_type'], 'meeting')
        session = Path(response.json()['session'])
        await self.wait_path(self.root / 'entered-run')
        return session, data.get('input_file')

    def url(self, session):
        return '/api/v1/sessions/' + quote(session.relative_to(self.root / 'outputs').as_posix(), safe='') + '/diarize'

    async def test_settings_crud_validation_and_no_overwrite(self):
        response = await self.http.get('/api/v1/meetings')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['id'], 'team')
        payload = dict(id='設計', content='[meeting]\nname="設計討論"\n[diarization]\nnum_speakers=10')
        response = await self.http.post('/api/v1/meetings', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['api_version'], 1)
        self.assertEqual((await self.http.post('/api/v1/meetings', json=payload)).status_code, 409)
        url = '/api/v1/meetings/' + quote('設計')
        self.assertEqual((await self.http.get(url)).json()['content'], payload['content'])
        changed = '[meeting]\nname="更新"'
        self.assertEqual((await self.http.put(url, json=dict(content=changed))).status_code, 200)
        for content in ('invalid=[', '[summary]\nenabled=true', '[diarization]\nnum_speakers=31', '[whisper]\nmodel=[]'):
            rejected = await self.http.put(url, json=dict(content=content))
            self.assertEqual(rejected.status_code, 400, rejected.text)
            self.assertIn('error', rejected.json())
        self.assertEqual((await self.http.get(url)).json()['content'], changed)
        for method in ('get', 'put'):
            response = await getattr(self.http, method)('/api/v1/meetings/missing', **({'json': {'content': changed}} if method == 'put' else {}))
            self.assertEqual(response.status_code, 404)
        for identifier in ('../bad', 'CON', '-switch', 'a\x7fb', ' leading'):
            self.assertEqual((await self.http.post('/api/v1/meetings', json=dict(id=identifier))).status_code, 400)
        self.assertFalse(list((self.root / 'meetings').glob('.meeting-*')))
        response = await self.http.get('/api/v1/meetings/' + quote('a/b', safe=''))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'invalid_meeting_id')

    async def test_run_errors_have_envelope_and_exact_http_codes(self):
        for payload in ({}, dict(meeting_id='team', speakers=True), dict(meeting_id='team', speakers=31),
                        dict(meeting_id='team', speakers=2, input_file=''),
                        dict(meeting_id='team', speakers=2, input_file='/tmp/audio'),
                        dict(meeting_id='team', speakers=2, input_file='bad\x00.ogg')):
            response = await self.http.post('/api/v1/meeting-runs', json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertIn('error', response.json())
        self.assertEqual((await self.http.post('/api/v1/meeting-runs', json=dict(meeting_id='missing', speakers=2))).status_code, 404)
        self.assertEqual((await self.http.post('/api/v1/meeting-runs', json=dict(meeting_id='team', speakers=2, input_file='/missing/audio.ogg'))).status_code, 422)
        for code, http in [('busy', 409), ('input_invalid', 400), ('source_missing', 422)]:
            (self.root / 'reject').write_text(code)
            response = await self.http.post('/api/v1/meeting-runs', json=dict(meeting_id='team', speakers=2))
            self.assertEqual(response.status_code, http, response.text)
            self.assertEqual(response.json()['error']['code'], code)
        self.assertFalse((self.root / 'outputs').exists())

    async def test_original_name_is_sanitized_before_subprocess_and_remains_display_metadata(self):
        uploaded = await self.http.post('/api/v1/audio-uploads?filename=test8min.ogg', content=b'audio')
        path = uploaded.json()['path']
        (self.root / 'finish').touch()
        response = await self.http.post('/api/v1/meeting-runs', json=dict(
            meeting_id='team', speakers=2, input_file=path,
            original_name='../test\x00\n\u202e8min.ogg' + '長' * 300))
        self.assertEqual(response.status_code, 202, response.text)
        await self.idle()
        session = Path(response.json()['session'])
        metadata = json.loads((session / 'source.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['original_name'], ('../test8min.ogg' + '長' * 300)[:255])
        self.assertEqual(metadata['path'], 'source_audio.ogg')
        self.assertEqual((session / metadata['path']).read_bytes(), b'audio')
        for data in [dict(meeting_id='team', speakers=2, original_name='label.ogg'),
                     dict(meeting_id='team', speakers=2, input_file=path, original_name='\x00\n'),
                     dict(meeting_id='team', speakers=2, input_file=path, original_name='x' * 4097)]:
            rejected = await self.http.post('/api/v1/meeting-runs', json=data)
            self.assertEqual(rejected.status_code, 400, rejected.text)
            self.assertEqual(rejected.json()['error']['code'], 'input_invalid')

    async def test_simultaneous_work_only_one_succeeds_and_force_is_clean(self):
        # Independent control instances bypass the in-process asyncio lock;
        # the real CLI OS lock must still admit exactly one job.
        other_app = create_app(self.app.state.settings, client=self.client, launcher=self.launcher)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url='http://127.0.0.1') as other:
            responses = await asyncio.gather(*(http.post('/api/v1/meeting-runs', json=dict(meeting_id='team', speakers=2)) for http in (self.http, other)))
        self.assertEqual(sorted(response.status_code for response in responses), [202, 409])
        winner = next(response for response in responses if response.status_code == 202)
        await self.wait_path(self.root / 'entered-run')
        response = await self.http.post('/api/v1/runs/stop', json=dict(force=True))
        self.assertEqual(response.status_code, 202)
        await self.idle()
        session = Path(winner.json()['session'])
        self.assertFalse((session / 'source.json').exists())
        status = json.loads((session / 'status.json').read_text())
        self.assertEqual((status['phase'], status['stop_reason'], status['last_error']), ('aborted', 'user', None))

    async def test_import_cancel_retry_keeps_source_and_old_generation_and_sse(self):
        session, staging = await self.start(imported=True)
        response = await self.http.post('/api/v1/runs/stop', json=dict(force=False))
        self.assertEqual(response.status_code, 202)
        await self.idle()
        Path(staging).unlink()  # Retry must use the session copy.
        self.assertEqual((session / 'source_audio.ogg').read_bytes(), b'audio')
        metadata = json.loads((session / 'source.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['original_name'], 'test8min.ogg')
        self.assertNotEqual(metadata['original_name'], Path(staging).name)
        raw = (session / 'transcript.md').read_bytes()
        for payload in ({}, {'speakers': 0}, {'speakers': '3'}):
            response = await self.http.post(self.url(session), json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertIn('error', response.json())
        (self.root / 'finish').touch()
        self.assertEqual((await self.http.post(self.url(session), json=dict(speakers=4))).status_code, 202)
        await self.idle()
        manifest = (session / 'diarization.current.json').read_bytes()
        old_file = session / json.loads(manifest)['speaker_transcript']
        previous = old_file.read_bytes()
        sid = session.relative_to(self.root / 'outputs').as_posix()
        initial = self.app.state.sessions.get(sid)
        self.assertEqual(initial['source']['kind'], 'import')
        self.assertEqual(initial['source']['original_name'], 'test8min.ogg')
        self.assertEqual(initial['speakers']['actual_speakers'], 1)
        (self.root / 'finish').unlink()
        for force in (False, True):
            (self.root / 'entered-diarize').unlink(missing_ok=True)
            self.assertEqual((await self.http.post(self.url(session), json=dict(speakers=5))).status_code, 202)
            await self.wait_path(self.root / 'entered-diarize')
            self.assertEqual((await self.http.post(self.url(session), json=dict(speakers=5))).status_code, 409)
            await self.http.post('/api/v1/runs/stop', json=dict(force=force))
            await self.idle()
            self.assertEqual((session / 'diarization.current.json').read_bytes(), manifest)
            self.assertEqual(old_file.read_bytes(), previous)
            self.assertEqual((session / 'transcript.md').read_bytes(), raw)
        class Connected:
            async def is_disconnected(self): return False
        stream = _session_events(Connected(), self.app.state.sessions, sid, .001, initial=initial)
        await anext(stream)
        (self.root / 'finish').touch()
        self.assertEqual((await self.http.post(self.url(session), json=dict(speakers=3))).status_code, 202)
        await self.idle()
        content = await anext(stream)
        self.assertIn('"target":"speaker_transcript"', content)
        await stream.aclose()
        self.assertEqual(old_file.read_bytes(), previous)
        self.assertEqual((session / 'transcript.md').read_bytes(), raw)
        self.assertEqual((await self.http.post('/api/v1/sessions/missing/diarize', json=dict(speakers=2))).status_code, 404)
        (session / 'source_audio.ogg').unlink()
        self.assertEqual((await self.http.post(self.url(session), json=dict(speakers=2))).status_code, 422)

    async def test_local_write_boundary_rejects_foreign_origin(self):
        for path, data in [('/api/v1/meetings', {'id': 'new'}),
                           ('/api/v1/meeting-runs', {'meeting_id': 'team', 'speakers': 2}),
                           ('/api/v1/sessions/missing/diarize', {'speakers': 2})]:
            response = await self.http.post(path, json=data, headers={'origin': 'https://foreign.example'})
            self.assertEqual(response.status_code, 403)
        # The sharing service is independently constructed; no control routes.
        from ui.backend.share_app import create_share_app
        routes = {route.path for route in create_share_app(None, '127.0.0.1').routes}
        self.assertFalse(any('/api/v1/' in path for path in routes))
