"""Meeting lifecycle/source contracts with mocked engines; no microphone access."""
import contextlib
import hashlib
import io
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
import wave
import struct
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core import config as C
from core.session import MeetingRun, MeetingRunError
from core.status import RunLock


CAPTURE = {'lost_ratio': 0.0, 'audio_seconds': 3, 'span_seconds': 3,
           'lost_seconds': 0, 'gap_count': 0}


def config(root):
    data = C.load_toml(C.DEFAULT_FILE)
    data['paths'].update(output_root=str(root / 'outputs'), state_dir=str(root / 'state'))
    data['work'] = {'type': 'meeting'}
    data['meeting']['name'] = '設計會議'
    data['diarization']['num_speakers'] = 10
    data['system'].update(inhibit_sleep=False, opencc=False, status_interval=0.05)
    data['audio']['keep_recording'] = False  # meeting must override this privately
    data['summary']['enabled'] = True
    return C.Config(data)


def audio_file(path):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * 48000)
    return path


class MeetingRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        tool_check = patch('core.session.shutil.which', return_value='/mock/tool')
        tool_check.start()
        self.addCleanup(tool_check.stop)
        self.cfg = config(self.root)
        self.source = audio_file(self.root / '私人 檔名.WAV')
        self.original = self.source.read_bytes()
        self.result = dict(duration=3, aborted=False, capture=dict(CAPTURE), gaps=[],
                           lost_seconds=0, ffmpeg_failed=False, ffmpeg_returncode=0)

    def execute(self, *, live=False, action=None, real_decode=False):
        run = MeetingRun(self.cfg, None if live else self.source)
        self.run_object = run
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(run, '_install_signals'))
            whisper = stack.enter_context(patch('core.session.WhisperServer'))
            llama = stack.enter_context(patch('core.session.LlamaServer'))
            summarize = stack.enter_context(patch('core.session.Summarizer'))
            transcriber = stack.enter_context(patch('core.session.Transcriber'))
            if not real_decode:
                stack.enter_context(patch.object(run, '_audio_command', return_value=json.dumps(
                    {'streams': [{'index': 0}], 'format': {'duration': '3'}}).encode()))
                stack.enter_context(patch('core.session.probe_capture', return_value=dict(CAPTURE)))
            def transcribe():
                engine_cfg, directory, _, source, _ = transcriber.call_args.args
                self.assertFalse(engine_cfg['summary']['enabled'])
                self.assertTrue(engine_cfg['audio']['keep_recording'])
                self.assertEqual(engine_cfg.course_name, self.cfg.meeting_name)
                self.assertEqual(RunLock(self.root / 'state').current()['work_type'], 'meeting')
                if not live:
                    self.assertEqual(Path(source).parent, directory)
                    self.assertEqual(Path(source).read_bytes(), self.original)
                    self.assertTrue((directory / 'source.json').is_file())
                else:
                    recording = directory / 'recording_120000.ogg'
                    recording.write_bytes(self.original)
                    transcriber.return_value.recording_path = recording
                (directory / 'transcript.md').write_text('原稿不能被辨識覆蓋', encoding='utf-8')
                (directory / 'transcript.srt').write_text('原時間戳', encoding='utf-8')
                if action:
                    action(run)
                return self.result
            transcriber.return_value.run.side_effect = transcribe
            try:
                return run.run()
            finally:
                llama.assert_not_called()
                summarize.assert_not_called()
                self.assertTrue(self.cfg['summary']['enabled'])
                self.assertFalse(self.cfg['audio']['keep_recording'])
                self.assertIsNone(RunLock(self.root / 'state').current())
                if whisper.called:
                    whisper.return_value.stop.assert_called()

    def status(self):
        return json.loads((self.run_object.dir / 'status.json').read_text(encoding='utf-8'))

    def test_import_saved_before_engine_and_survives_external_source_removal(self):
        result = self.execute(action=lambda run: self.source.unlink())
        directory = Path(result['session'])
        source = json.loads((directory / 'source.json').read_text(encoding='utf-8'))
        self.assertEqual(source['path'], 'source_audio.wav')
        self.assertEqual(source['original_name'], '私人 檔名.WAV')
        self.assertEqual(source['sha256'], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(source['size_bytes'], len(self.original))
        self.assertEqual((source['schema_version'], source['kind'], source['requested_speakers']), (1, 'import', 10))
        self.assertTrue(source['complete'])
        self.assertEqual((directory / source['path']).read_bytes(), self.original)
        self.assertEqual((directory / 'transcript.md').read_text(encoding='utf-8'), '原稿不能被辨識覆蓋')
        self.assertFalse((directory / 'notes.md').exists())
        self.assertFalse((directory / 'diarization.current.json').exists())
        state = self.status()
        self.assertEqual((state['work_type'], state['phase'], state['summary_model']), ('meeting', 'done', None))
        events = [json.loads(line) for line in (directory / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([e['phase'] for e in events if e['type'] == 'phase'],
                         ['starting', 'loading', 'transcribing', 'finishing', 'done'])

    def test_live_normal_stop_is_done_and_seals_recording_without_summary(self):
        result = self.execute(live=True, action=lambda run: run._on_signal(None, None))
        self.assertEqual(result['outcome'], 'done')
        self.assertEqual(self.status()['stop_reason'], 'user')
        self.assertTrue((self.run_object.dir / 'source_audio.ogg').exists())
        self.assertEqual(list(self.run_object.dir.glob('recording_*.ogg')), [])

    def test_stop_file_cancels_import_and_keeps_validated_source(self):
        def stop(run):
            (run.dir / 'stop').touch()
            self.assertTrue(run.stop_requested.wait(3))
        result = self.execute(action=stop)
        self.assertEqual(result['outcome'], 'aborted')
        self.assertEqual(self.status()['stop_reason'], 'user')
        self.assertTrue((self.run_object.dir / 'source.json').is_file())

    def test_capture_loss_is_failure_and_source_is_not_reusable(self):
        self.result['capture']['lost_ratio'] = 0.02
        with self.assertRaises(MeetingRunError) as exc:
            self.execute(live=True)
        self.assertEqual(exc.exception.code, 'capture_loss')
        self.assertEqual(self.status()['phase'], 'failed')
        self.assertFalse(self.status()['capture']['complete'])
        metadata = json.loads((self.run_object.dir / 'source.json').read_text(encoding='utf-8'))
        self.assertFalse(metadata['complete'])
        self.assertEqual(metadata['capture']['lost_ratio'], 0.02)

    def test_engine_gaps_and_partial_decode_failure_do_not_claim_done(self):
        for updates, expected in (({'gaps': [{'start': 0, 'seconds': 1}], 'lost_seconds': 1}, 'asr_failed'),
                                  ({'ffmpeg_failed': True}, 'decode_failed')):
            with self.subTest(expected=expected):
                self.result.update(gaps=[], ffmpeg_failed=False)
                self.result.update(updates)
                with self.assertRaises(MeetingRunError) as exc:
                    self.execute()
                self.assertEqual(exc.exception.code, expected)
                self.assertEqual(self.status()['phase'], 'failed')

    def test_busy_rejected_before_output_and_engine(self):
        owner = RunLock(self.cfg.state_dir())
        self.addCleanup(owner.release)
        owner.acquire(work_type='lecture', mode='summarize')
        with patch('core.session.WhisperServer') as server, self.assertRaises(MeetingRunError) as exc:
            MeetingRun(self.cfg, self.source).run()
        self.assertEqual((exc.exception.code, exc.exception.exit_code), ('run_active', 3))
        self.assertFalse((self.root / 'outputs').exists())
        server.assert_not_called()

    def test_dual_rejected_before_lock_or_output(self):
        self.cfg.data['audio'].update(capture_mode='dual', backend='pulse',
                                      system_source='chosen.monitor', microphone_source='chosen.mic')
        run = MeetingRun(self.cfg)
        with patch.object(run.lock, 'acquire') as acquire, self.assertRaises(MeetingRunError) as exc:
            run.run()
        self.assertEqual(exc.exception.code, 'capture_unavailable')
        acquire.assert_not_called()
        self.assertFalse((self.root / 'outputs').exists())

    def test_cancel_while_copying_removes_partial_and_never_starts_whisper(self):
        run = MeetingRun(self.cfg, self.source)
        with patch.object(run, '_install_signals'), contextlib.redirect_stdout(io.StringIO()), \
                patch.object(run, '_check_cancel', side_effect=KeyboardInterrupt), \
                patch('core.session.WhisperServer') as server:
            self.assertEqual(run.run()['outcome'], 'aborted')
        self.assertFalse((run.dir / 'source.json').exists())
        self.assertEqual(list(run.dir.glob('*source_audio*')), [])
        server.assert_not_called()
        self.assertIsNone(RunLock(self.cfg.state_dir()).current())

    def test_copy_failure_does_not_start_engine_or_publish_source(self):
        run = MeetingRun(self.cfg, self.source)
        replace = os.replace
        def fail_source_copy(source, target):
            if Path(target).name.startswith('source_audio'):
                raise OSError('disk full')
            return replace(source, target)
        with patch.object(run, '_install_signals'), contextlib.redirect_stdout(io.StringIO()), \
                patch('core.session.os.replace', side_effect=fail_source_copy), \
                patch('core.session.WhisperServer') as server, self.assertRaises(MeetingRunError):
            run.run()
        server.assert_not_called()
        self.assertFalse((run.dir / 'source.json').exists())
        self.assertEqual(list(run.dir.glob('.source_audio*')), [])
        self.assertIsNone(RunLock(self.cfg.state_dir()).current())

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires ffmpeg/ffprobe')
    def test_real_file_decode_validation_with_mock_whisper(self):
        self.assertEqual(self.execute(real_decode=True)['outcome'], 'done')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires ffmpeg/ffprobe')
    def test_invalid_audio_fails_before_whisper(self):
        self.source.write_bytes(b'not audio')
        run = MeetingRun(self.cfg, self.source)
        with patch.object(run, '_install_signals'), contextlib.redirect_stdout(io.StringIO()), \
                patch('core.session.WhisperServer') as server, self.assertRaises(MeetingRunError) as exc:
            run.run()
        self.assertEqual(exc.exception.code, 'source_invalid')
        server.assert_not_called()
        self.assertFalse((run.dir / 'source.json').exists())

    def test_signal_handlers_are_restored_after_run(self):
        previous = signal.getsignal(signal.SIGINT)
        run = MeetingRun(self.cfg, self.source)
        with patch.object(run, '_copy_source', side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run.run()['outcome'], 'aborted')
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_unknown_capture_never_publishes_reusable_recording(self):
        run = MeetingRun(self.cfg)
        directory = self.root / 'session'
        directory.mkdir()
        run.dir = directory
        from core.status import Status
        run.status = Status(directory, interval=60)
        self.addCleanup(run.status.close)
        source = audio_file(directory / 'source_audio.ogg')
        with patch.object(run, '_audio_command', return_value=b'{"streams":[{}],"format":{"duration":"3"}}'), \
                patch('core.session.probe_capture', return_value=None), self.assertRaises(MeetingRunError) as exc:
            run._seal_source(source)
        self.assertEqual(exc.exception.code, 'capture_unverified')
        self.assertFalse(json.loads((directory / 'source.json').read_text(encoding='utf-8'))['complete'])

    def test_failed_startup_retains_import_and_reports_failed(self):
        from core.servers import ServerError
        run = MeetingRun(self.cfg, self.source)
        with patch.object(run, '_install_signals'), contextlib.redirect_stdout(io.StringIO()), \
                patch.object(run, '_audio_command', return_value=b'{"streams":[{}],"format":{"duration":"3"}}'), \
                patch('core.session.probe_capture', return_value=dict(CAPTURE)), \
                patch('core.session.WhisperServer') as whisper, self.assertRaises(MeetingRunError) as exc:
            whisper.return_value.ensure.side_effect = ServerError('private engine log')
            run.run()
        self.assertEqual(exc.exception.code, 'engine_failed')
        self.assertNotIn('private', str(exc.exception))
        self.assertTrue((run.dir / 'source.json').exists())
        self.assertIsNone(RunLock(self.cfg.state_dir()).current())

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires ffmpeg/ffprobe')
    def test_real_transcriber_file_pipeline_with_mock_asr_response(self):
        with wave.open(str(self.source), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', int(12000 * math.sin(i * 440 * 2 * math.pi / 16000)))
                                       for i in range(48000)))
        run = MeetingRun(self.cfg, self.source)
        with patch.object(run, '_install_signals'), contextlib.redirect_stdout(io.StringIO()), \
                patch('core.session.WhisperServer') as whisper, \
                patch('core.transcribe.transcribe_request', return_value='1\n00:00:00,000 --> 00:00:02,000\n測試內容\n') as asr:
            whisper.return_value.url = 'http://unused'
            result = run.run()
        self.addCleanup(run.transcriber.proc.stdout.close)
        self.assertEqual(result['outcome'], 'done')
        asr.assert_called()
        self.assertIn('測試內容', (run.dir / 'transcript.md').read_text(encoding='utf-8'))
        self.assertIn('設計會議', (run.dir / 'transcript.md').read_text(encoding='utf-8'))
        self.assertTrue((run.dir / 'source.json').is_file())

    def test_force_stop_exits_child_and_publishes_one_final_json(self):
        # Entire CLI lives in a disposable child; engine and input are synthetic.
        code = '''
import json, sys, time, subprocess
from pathlib import Path
from unittest.mock import patch
from tests.test_session_meeting_run import config
from core.cli import main
from core import platform as P
root = Path(sys.argv[1])
def transcribe(cfg, directory, url, source, status):
    class Engine:
        proc = None
        def run(self):
            self.proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], **P.spawn_kwargs())
            (root / 'child.pid').write_text(str(self.proc.pid), encoding='utf-8')
            (directory / 'stop_force').touch()
            time.sleep(10)
            raise AssertionError('force did not stop')
    return Engine()
with patch('core.cli.C.load_meeting', return_value=config(root)), patch('core.session.shutil.which', return_value='mock'), patch('core.session.WhisperServer'), patch('core.session.Transcriber', side_effect=transcribe):
    raise SystemExit(main(['meeting', 'run', 'test', '--speakers', '10', '--json']))
'''
        proc = subprocess.run([sys.executable, '-c', code, str(self.root)],
                              cwd=Path(__file__).resolve().parents[1], capture_output=True,
                              text=True, encoding='utf-8', timeout=15)
        self.assertEqual(proc.returncode, 130, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result['outcome'], 'aborted')
        directory = Path(result['session'])
        status = json.loads((directory / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual((status['phase'], status['stop_reason']), ('aborted', 'user'))
        self.assertFalse((directory / 'source.json').exists())
        self.assertIsNone(RunLock(self.cfg.state_dir()).current())
        from core import platform as P
        child_pid = int((self.root / 'child.pid').read_text(encoding='utf-8'))
        self.assertFalse(P.pid_alive(child_pid), 'force stop left the owned synthetic child alive')
