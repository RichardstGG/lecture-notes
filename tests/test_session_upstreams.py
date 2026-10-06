"""All summary orchestration paths with mocked transcription/HTTP, real notes."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core.config import Config, DEFAULT_FILE, load_toml
from core.session import LectureRun, OfflineSummary
from core.summarize import Summarizer


class UpstreamSessionTests(unittest.TestCase):
    def test_live_file_resume_and_redo_never_manage_llama(self):
        for mode in ('live', 'file', 'resume', 'redo'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = Config(load_toml(DEFAULT_FILE))
                cfg.data['system'].update(inhibit_sleep=False, opencc=False)
                cfg.data['paths'].update(output_root=str(root / 'outputs'), state_dir=str(root / 'state'))
                cfg.data['summary'].update(upstream='lab', min_chars=1, empty_chars=1,
                                           poll_seconds=0.01, final_wait=2)
                transcript = '## 00:00:00\nFirst section transcript\n## 00:05:00\nSecond section transcript'
                payload = {'choices': [{'message': {'content': json.dumps({'topic': 'Topic', 'points': [], 'terms': [], 'emphasis': []})}, 'finish_reason': 'stop'}]}
                target = ('http://secret.invalid/v1', {'name': 'lab', 'remote': True,
                          'model_id': 'secret-model', 'api_key': 'secret-key', 'disable_thinking': False})
                root.joinpath('transcript.md').write_text(transcript)
                source = root / 'input.wav'
                source.touch()
                run = LectureRun(cfg, source if mode == 'file' else None) if mode in ('live', 'file') else OfflineSummary(cfg, root, 'all' if mode == 'redo' else None)
                first = threading.Event()
                calls = []
                def post(summary, body, timeout):
                    calls.append(body)
                    first.set()
                    return payload
                def transcribe():
                    (run.dir / 'transcript.md').write_text(transcript)
                    if mode == 'live':
                        self.assertTrue(first.wait(2), 'first section must summarize before capture finishes')
                        self.assertEqual(len(calls), 1, 'last section stays pending until transcription ends')
                    return dict(duration=310, aborted=False, gaps=[], ffmpeg_failed=False)
                with (patch.object(Config, 'remote_summary', return_value=target),
                      patch('core.session.LlamaServer') as llama,
                      patch('core.session.WhisperServer'),
                      patch('core.session.Transcriber') as transcriber,
                      patch('core.session.shutil.which', return_value='/mock/tool'),
                      patch.object(run, '_install_signals'),
                      patch.object(run, '_start_stop_watcher'),
                      patch.object(Summarizer, '_post', post)):
                    transcriber.return_value.run.side_effect = transcribe
                    self.assertEqual(run.run(), 0)
                    llama.assert_not_called()
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(body['model'] == 'secret-model' for body in calls))
                entries = [json.loads(line) for line in (run.dir / 'notes.jsonl').read_text().splitlines()]
                self.assertEqual([e['status'] for e in entries], ['ok', 'ok'])
                public = ''.join(p.read_text() for p in run.dir.iterdir() if p.suffix in ('.log', '.toml', '.json', '.jsonl', '.md'))
                for secret in ('secret.invalid', 'secret-key', 'secret-model'):
                    self.assertNotIn(secret, public)
                if mode in ('live', 'file', 'resume', 'redo'):
                    status = json.loads((run.dir / 'status.json').read_text())
                    self.assertEqual(status['summary_upstream'], 'lab')
                    self.assertEqual(status['summary_connection'], 'ok')
                    self.assertEqual(status['servers']['llama'], 'not_started')

    def test_live_final_wait_cancels_remote_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(load_toml(DEFAULT_FILE))
            cfg.data['system'].update(inhibit_sleep=False, opencc=False)
            cfg.data['paths'].update(output_root=str(root / 'outputs'), state_dir=str(root / 'state'))
            cfg.data['summary'].update(upstream='lab', min_chars=1, empty_chars=1,
                                       poll_seconds=0.01, final_wait=0.1)
            run = LectureRun(cfg)
            entered, release = threading.Event(), threading.Event()
            target = ('http://private.invalid/v1', {'name': 'lab', 'remote': True,
                      'model_id': '30b', 'api_key': '', 'disable_thinking': False})
            def request(*args):
                entered.set()
                release.wait(5)
                return {'choices': [{'message': {'content': '{}'}}]}
            def transcribe():
                (run.dir / 'transcript.md').write_text('## 00:00:00\nFirst section\n## 00:05:00\nLast section')
                self.assertTrue(entered.wait(2))
                return dict(duration=310, aborted=False, gaps=[], ffmpeg_failed=False)
            with (patch.object(Config, 'remote_summary', return_value=target),
                  patch('core.session.LlamaServer') as llama,
                  patch('core.session.WhisperServer'),
                  patch('core.session.Transcriber') as transcriber,
                  patch('core.session.shutil.which', return_value='/mock/tool'),
                  patch.object(run, '_install_signals'), patch.object(run, '_start_stop_watcher'),
                  patch.object(Summarizer, '_post_http', side_effect=request)):
                transcriber.return_value.run.side_effect = transcribe
                try:
                    self.assertEqual(run.run(), 0)
                    self.assertFalse(run.summary_thread.is_alive())
                    self.assertFalse((root / 'state' / 'run.json').exists())
                    self.assertFalse((run.dir / 'notes.jsonl').exists())
                    self.assertEqual(json.loads((run.dir / 'status.json').read_text())['phase'], 'done')
                    llama.assert_not_called()
                finally:
                    release.set()

    def test_transcribe_only_does_not_resolve_private_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(load_toml(DEFAULT_FILE))
            cfg.data['system']['inhibit_sleep'] = False
            cfg.data['paths'].update(output_root=str(root / 'outputs'), state_dir=str(root / 'state'))
            cfg.data['summary'].update(upstream='missing', enabled=False)
            run = LectureRun(cfg)
            with (patch.object(Config, 'remote_summary', side_effect=AssertionError('must not load registry')),
                  patch('core.session.LlamaServer') as llama,
                  patch('core.session.WhisperServer'), patch('core.session.Transcriber') as transcriber,
                  patch('core.session.shutil.which', return_value='/mock/tool'),
                  patch.object(run, '_install_signals'), patch.object(run, '_start_stop_watcher')):
                transcriber.return_value.run.return_value = dict(duration=1, aborted=False, gaps=[], ffmpeg_failed=False)
                self.assertEqual(run.run(), 0)
                llama.assert_not_called()
