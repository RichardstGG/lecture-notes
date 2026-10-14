"""Meeting CLI/session contracts with real files and mocked engine boundaries."""
import contextlib
import hashlib
import io
import json
import shutil
import functools
import wave
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core import cli, config as C
from core.diarize import CODES, DiarizationCancelled, DiarizationError
from core.session import MeetingDiarize, MeetingJobError, MeetingRun, verify_meeting_source
from core.status import RunLock, Status


class MeetingSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.meetings = self.root / 'meetings'
        self.meetings.mkdir()
        (self.meetings / 'team.toml').write_text('[meeting]\nname="Team"\n')
        local = self.root / 'local.toml'
        local.write_text(f'[paths]\noutput_root="{self.root / "output"}"\nstate_dir="{self.state}"\n'
                         '[system]\ninhibit_sleep=false\n')
        self.addCleanup(patch.stopall)
        patch.multiple(C, LOCAL_FILE=local, MEETINGS_DIR=self.meetings).start()
        self.source = self.root / 'private original.ogg'
        self.source.write_bytes(b'complete audio')
        self.seg, self.emb = self.root / 'seg.onnx', self.root / 'emb.onnx'
        self.seg.write_bytes(b'segmentation model')
        self.emb.write_bytes(b'embedding model')
        self.whisper = patch('core.session.WhisperServer').start()
        self.whisper.return_value.url = 'http://127.0.0.1:8178'
        self.llama = patch('core.session.LlamaServer').start()
        self.transcriber = patch('core.session.Transcriber').start()
        patch('core.session.source_duration', return_value=12.5).start()
        self.transcriber.return_value.run.return_value = dict(duration=12.5, aborted=False,
            ffmpeg_failed=False, gaps=[], lost_seconds=0, capture=None)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(args))
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(err.getvalue(), '')
        return code, json.loads(out.getvalue())

    def import_audio(self, *extra):
        return self.invoke('meeting', 'run', 'team', '--file', str(self.source),
                           '--speakers', '3', '--json', *extra)

    def session(self):
        code, payload = self.import_audio()
        self.assertEqual(code, 0)
        return Path(payload['session'])

    def diarize_args(self, session, *extra):
        return ('meeting', 'diarize', str(session), '--speakers', '4', '--json',
                '--set', f'diarization.segmentation_model="{self.seg}"',
                '--set', f'diarization.embedding_model="{self.emb}"', *extra)

    @staticmethod
    def files(session):
        status = json.loads((session / 'status.json').read_text())
        events = [json.loads(line) for line in (session / 'events.jsonl').read_text().splitlines()]
        return status, events

    def test_import_persists_source_before_transcriber_and_never_loads_llama(self):
        def transcribe():
            args = self.transcriber.call_args.args
            session, source = args[1], args[3]
            metadata = json.loads((session / 'source.json').read_text())
            self.assertEqual(source, session / 'source_audio.ogg')
            self.assertEqual(source.read_bytes(), self.source.read_bytes())
            self.assertTrue(args[0].get('audio.keep_recording'))
            self.assertFalse(args[0].get('summary.enabled'))
            self.assertEqual(metadata['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
            print('PRIVATE ASR TEXT')
            return dict(duration=12.5, aborted=False, ffmpeg_failed=False)
        self.transcriber.return_value.run.side_effect = transcribe
        code, payload = self.import_audio('--set', 'audio.keep_recording=false', '--set', 'summary.enabled=true')
        self.assertEqual(code, 0)
        self.assertEqual(set(payload), {'schema_version', 'accepted', 'work_type', 'operation', 'session', 'pid', 'outcome'})
        self.assertEqual((payload['accepted'], payload['operation'], payload['outcome']), (True, 'run', 'done'))
        session = Path(payload['session'])
        metadata = json.loads((session / 'source.json').read_text())
        self.assertEqual(metadata, dict(schema_version=1, kind='import', original_name=self.source.name,
            path='source_audio.ogg', size_bytes=len(self.source.read_bytes()), sha256=metadata['sha256'],
            duration_seconds=12.5, created_at=metadata['created_at'], requested_speakers=3))
        cfg = tomllib.loads((session / 'config.used.toml').read_text())
        self.assertEqual(cfg['work']['type'], 'meeting')
        self.assertTrue(cfg['audio']['keep_recording'])
        status, events = self.files(session)
        self.assertEqual([e['phase'] for e in events if e['type'] == 'phase'],
                         ['starting', 'loading', 'transcribing', 'finishing', 'done'])
        self.assertEqual(status['work_type'], 'meeting')
        self.assertIsNone(status['stop_reason'])
        self.llama.assert_not_called()
        self.whisper.return_value.stop.assert_called_once()
        self.assertFalse((self.state / 'run.json').exists())

    def test_import_copy_failure_does_not_start_transcription_or_publish_metadata(self):
        with patch('core.session.shutil.copyfile', side_effect=OSError('disk full')):
            code, payload = self.import_audio()
        self.assertEqual((code, payload['accepted'], payload['error']['code']), (1, False, 'write_failed'))
        self.transcriber.assert_not_called()
        self.whisper.assert_not_called()
        session = next((self.root / 'output' / 'meetings').iterdir())
        self.assertFalse((session / 'source.json').exists())
        self.assertFalse(list(session.glob('.source-*')))
        self.assertEqual(self.files(session)[0]['phase'], 'failed')

    def test_invalid_speakers_and_parser_errors_return_only_json(self):
        for args in [('run', 'team', '--speakers', value) for value in ('0', '31', 'true', 'False', '1.5')] + [
            ('run', 'team'), ('diarize', str(self.root)), ('unknown',), ('run', 'team', '--speakers', '2', '--oops')]:
            with self.subTest(args=args):
                code, payload = self.invoke('meeting', *args, '--json')
                self.assertEqual((code, payload['accepted'], payload['error']['code']), (2, False, 'input_invalid'))
        for cls in (MeetingRun, MeetingDiarize):
            with self.assertRaises(MeetingJobError):
                cls(C.load_meeting('team'), True, self.root)

    def test_busy_is_shared_by_run_diarize_and_lecture(self):
        session = self.session()
        self.transcriber.reset_mock()
        lock = RunLock(self.state)
        self.assertIsNone(lock.acquire(work_type='lecture'))
        try:
            for args in [('meeting', 'run', 'team', '--speakers', '2', '--json'), self.diarize_args(session)]:
                code, payload = self.invoke(*args)
                self.assertEqual((code, payload['error']['code']), (3, 'busy'))
        finally:
            lock.release()
        self.transcriber.assert_not_called()

    def test_diarize_request_progress_completion_and_source_survives_staging_cleanup(self):
        session = self.session()
        original_cfg = (session / 'config.used.toml').read_bytes()
        (session / 'transcript.md').write_text('original transcript')
        self.source.unlink()
        def engine(request, progress, cancel):
            self.whisper.return_value.ensure.assert_called()
            self.assertEqual(request.source_audio, session / 'source_audio.ogg')
            self.assertEqual(request.requested_speakers, 4)
            self.assertEqual(request.meeting_name, 'Team')
            self.assertFalse(cancel())
            for processed in (3, 2, 99):
                progress(dict(stage='segmentation', processed_seconds=processed, total_seconds=12.5, speakers_found=2))
            progress(dict(stage='retranscription', processed_seconds=1, total_seconds=12.5, speakers_found=2))
            return SimpleNamespace(actual_speakers=2, duration_seconds=12.5)
        with patch('core.diarize.diarize_session', side_effect=engine):
            code, payload = self.invoke(*self.diarize_args(session))
        self.assertEqual((code, payload['operation'], payload['outcome']), (0, 'diarize', 'done'))
        record = json.loads((session / 'diarization.request.json').read_text())
        metadata = json.loads((session / 'source.json').read_text())
        self.assertEqual((record['schema_version'], record['spec_version']), (1, 1))
        self.assertEqual(record['source_sha256'], metadata['sha256'])
        self.assertEqual(record['segmentation_model'], str(self.seg))
        self.assertEqual(record['embedding_model_sha256'], hashlib.sha256(self.emb.read_bytes()).hexdigest())
        self.assertEqual(record['cluster_threshold'], 0.5)
        self.assertEqual(record['min_turn_seconds'], 0.25)
        self.assertIsNone(record['unassigned_confidence_floor'])
        status, events = self.files(session)
        phases = [e['phase'] for e in events if e['type'] == 'phase']
        self.assertEqual(phases[-5:], ['starting', 'loading', 'diarizing', 'finishing', 'done'])
        progress = [e for e in events if e['type'] == 'diarization_progress']
        self.assertEqual([e['processed_seconds'] for e in progress], [3, 3, 12.5, 1])
        self.assertTrue(all(e['schema_version'] == 1 and e['actual_speakers'] is None for e in progress))
        self.assertEqual(status['diarization']['actual_speakers'], 2)
        self.assertEqual((session / 'transcript.md').read_text(), 'original transcript')
        self.assertEqual((session / 'config.used.toml').read_bytes(), original_cfg)
        self.llama.assert_not_called()

    def test_source_tampering_refuses_engine_and_preserves_previous_generation(self):
        session = self.session()
        manifest = session / 'diarization.current.json'
        manifest.write_text('{"generation":"0001"}')
        (session / 'source_audio.ogg').write_bytes(b'different audio')
        with patch('core.diarize.diarize_session') as engine:
            code, payload = self.invoke(*self.diarize_args(session))
        self.assertEqual((code, payload['error']['code']), (1, 'source_hash_mismatch'))
        engine.assert_not_called()
        self.assertEqual(manifest.read_text(), '{"generation":"0001"}')
        self.assertFalse((session / 'diarization.request.json').exists())

    def test_source_metadata_rejects_malformed_and_escaping_paths(self):
        session = self.session()
        metadata = json.loads((session / 'source.json').read_text())
        for change in ({'path': '../private original.ogg'}, {'path': str(self.source)}, {'size_bytes': True},
                       {'requested_speakers': True}, {'schema_version': True}, {'duration_seconds': float('nan')}):
            with self.subTest(change=change):
                (session / 'source.json').write_text(json.dumps({**metadata, **change}))
                with self.assertRaises(MeetingJobError):
                    verify_meeting_source(session)
        (session / 'source.json').write_text(json.dumps(metadata))
        source = session / 'source_audio.ogg'
        source.unlink()
        source.symlink_to(self.source)
        with self.assertRaises(MeetingJobError):
            verify_meeting_source(session)

    def test_every_engine_error_code_is_preserved_and_never_leaks_asr_text(self):
        session = self.session()
        manifest = session / 'diarization.current.json'
        manifest.write_text('old successful generation')
        for error in CODES:
            with self.subTest(error=error), patch('core.diarize.diarize_session', side_effect=DiarizationError(error, 'PRIVATE ASR TEXT')):
                code, payload = self.invoke(*self.diarize_args(session))
                self.assertEqual((code, payload['accepted'], payload['error']['code']), (1, False, error))
                status, _ = self.files(session)
                self.assertEqual(status['phase'], 'failed')
                self.assertIn(error, status['last_error'])
                self.assertNotIn('PRIVATE ASR TEXT', json.dumps(payload))
                self.assertEqual(manifest.read_text(), 'old successful generation')

    def test_stop_and_force_during_diarize_cancel_and_keep_previous_outputs(self):
        session = self.session()
        manifest = session / 'diarization.current.json'
        manifest.write_text('previous')
        for stop in ('stop', 'stop_force'):
            def engine(request, progress, cancel):
                (session / stop).touch()
                self.assertTrue(cancel())
                raise DiarizationCancelled()
            with self.subTest(stop=stop), patch('core.diarize.diarize_session', side_effect=engine):
                code, payload = self.invoke(*self.diarize_args(session))
                self.assertEqual((code, payload['accepted'], payload['outcome']), (130, True, 'aborted'))
                status, events = self.files(session)
                self.assertEqual((status['phase'], status['stop_reason']), ('aborted', 'user'))
                self.assertTrue(any(e['type'] == 'diarization_cancelled' for e in events))
                self.assertEqual(manifest.read_text(), 'previous')
                self.assertFalse((self.state / 'run.json').exists())

    def test_live_normal_stop_renames_verified_recording_and_finishes_done(self):
        def capture():
            cfg, session, _, source, status = self.transcriber.call_args.args
            self.assertIsNone(source)
            recording = session / 'recording_120000.ogg'
            recording.write_bytes(b'complete recording')
            self.transcriber.return_value.recording_path = recording
            status.update(stop_reason='user')
            status.event('capture_integrity', lost_ratio=0, audio_seconds=12.5)
            return dict(duration=12.5, aborted=False, ffmpeg_failed=False,
                        capture=dict(lost_ratio=0, audio_seconds=12.5))
        self.transcriber.return_value.run.side_effect = capture
        code, payload = self.invoke('meeting', 'run', 'team', '--speakers', '2', '--json')
        session = Path(payload['session'])
        self.assertEqual((code, payload['outcome']), (0, 'done'))
        self.assertTrue((session / 'source_audio.ogg').is_file())
        self.assertFalse((session / 'recording_120000.ogg').exists())
        self.assertEqual(json.loads((session / 'source.json').read_text())['kind'], 'recording')
        status, events = self.files(session)
        self.assertEqual(status['stop_reason'], 'user')
        self.assertIn('recording', [e['phase'] for e in events if e['type'] == 'phase'])

    def test_bad_capture_and_native_pts_loss_do_not_publish_source(self):
        for report, native in ((None, None), ({'lost_ratio': 0.202}, None),
                               ({'lost_ratio': 0}, {'lost_seconds': 20, 'pts_span_seconds': 100})):
            def capture():
                session, status = self.transcriber.call_args.args[1], self.transcriber.call_args.args[4]
                recording = session / 'recording_120000.ogg'
                recording.write_bytes(b'damaged capture')
                self.transcriber.return_value.recording_path = recording
                if native:
                    status.event('capture_integrity', detail={'native_capture': native})
                return dict(duration=12.5, ffmpeg_failed=False, capture=report)
            with self.subTest(report=report, native=native):
                self.transcriber.return_value.run.side_effect = capture
                code, payload = self.invoke('meeting', 'run', 'team', '--speakers', '2', '--json')
                self.assertEqual((code, payload['error']['code']), (1, 'source_incomplete'))
        self.assertFalse(any((s / 'source.json').exists() for s in (self.root / 'output' / 'meetings').iterdir()))

    def test_diarize_rejects_nonmeeting_and_nondiarization_overrides(self):
        session = self.session()
        code, payload = self.invoke(*self.diarize_args(session, '--set', 'whisper.threads=4'))
        self.assertEqual((code, payload['error']['code']), (2, 'input_invalid'))
        (session / 'config.used.toml').write_text('[work]\ntype="lecture"')
        self.assertEqual(self.invoke(*self.diarize_args(session))[0], 2)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'requires ffmpeg')
    def test_real_generation_publication_retry_and_readonly_reader_with_mock_inference(self):
        from core import diarize as D
        from ui.backend.session_store import SessionStore
        self.source = self.root / 'staging.wav'
        with wave.open(str(self.source), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b'\x01\x01' * 16000 * 13)
        def transcribe():
            session = self.transcriber.call_args.args[1]
            (session / 'transcript.md').write_text('unchanged original')
            return dict(duration=13, aborted=False, ffmpeg_failed=False)
        self.transcriber.return_value.run.side_effect = transcribe
        session = self.session()
        self.source.unlink()
        original_source = (session / 'source_audio.wav').read_bytes()
        def segmentation(request, progress, cancel):
            progress(13, 13, 2)
            return D.DiarizerOutput([(0, 2, 0, 1), (3, 5, 1, 1)], 13, 'mock-inference')
        def asr(request, pcm, prompt):
            return [(0, len(pcm) / 32000, 'contract test speech')]
        engine = functools.partial(D.diarize_session, diarizer=segmentation, asr=asr)
        for generation in ('0001', '0002'):
            with patch('core.diarize.diarize_session', engine):
                self.assertEqual(self.invoke(*self.diarize_args(session))[0], 0)
            manifest = json.loads((session / 'diarization.current.json').read_text())
            self.assertEqual(manifest['generation'], generation)
            self.assertTrue((session / manifest['speaker_transcript']).is_file())
            self.assertFalse(list(session.glob('.diarize-staging-*')))
        store = SessionStore(self.root / 'output')
        detail = store.get(session.relative_to(self.root / 'output').as_posix())
        self.assertEqual(detail['session']['work_type'], 'meeting')
        self.assertEqual(detail['transcript']['content'], 'unchanged original')
        self.assertIn('S01:', detail['speaker_transcript']['content'])
        self.assertIn('S02:', detail['speaker_transcript']['content'])
        prior_manifest = (session / 'diarization.current.json').read_bytes()
        def cancel_segmentation(request, progress, cancel):
            (session / 'stop').touch()
            self.assertTrue(cancel())
            raise D.DiarizationCancelled()
        with patch('core.diarize.diarize_session', functools.partial(D.diarize_session, diarizer=cancel_segmentation, asr=asr)):
            self.assertEqual(self.invoke(*self.diarize_args(session))[0], 130)
        self.assertEqual((session / 'diarization.current.json').read_bytes(), prior_manifest)
        self.assertEqual((session / 'source_audio.wav').read_bytes(), original_source)
        self.assertEqual((session / 'transcript.md').read_text(), 'unchanged original')
        self.assertTrue((session / 'diarization' / '0001' / 'speakers.json').is_file())
        self.assertFalse(list(session.glob('.diarize-staging-*')))

    def test_dual_recording_is_rejected_without_starting_single_source(self):
        code, payload = self.invoke('meeting', 'run', 'team', '--speakers', '2', '--json',
            '--set', 'audio.capture_mode=dual', '--set', 'audio.system_source=output.monitor',
            '--set', 'audio.microphone_source=input.mic')
        self.assertEqual((code, payload['error']['code']), (2, 'input_invalid'))
        self.transcriber.assert_not_called()

    def test_cancel_before_session_allocation_has_json_and_releases_lock(self):
        with patch.object(MeetingRun, '_install_signals', side_effect=KeyboardInterrupt):
            code, payload = self.import_audio()
        self.assertEqual((code, payload['accepted'], payload['error']['code']), (130, False, 'cancelled'))
        self.assertFalse((self.state / 'run.json').exists())

    def test_diarize_preserves_capture_metrics_and_reports_attempt_separately(self):
        from ui.backend.session_store import SessionStore
        for outcome in ('done', 'model_missing', 'aborted'):
            with self.subTest(outcome=outcome):
                session = self.session()
                status, _ = self.files(session)
                original = dict(started_at='2026-10-10T09:00:00+08:00', elapsed=12.5,
                                transcribed=11.5, sections_total=2, sections_summarized=0)
                status.update(original)
                (session / 'status.json').write_text(json.dumps(status))
                (session / 'transcript.md').write_text('original draft')
                def engine(request, progress, cancel):
                    progress(dict(stage='segmentation', processed_seconds=3,
                                  total_seconds=12.5, speakers_found=2))
                    code, runtime = self.invoke('status', '--json')
                    self.assertEqual(code, 0)
                    self.assertTrue(runtime['running'])
                    self.assertEqual(runtime['mode'], 'diarize')
                    self.assertEqual(runtime['status']['phase'], 'diarizing')
                    self.assertEqual(runtime['status']['diarization']['processed_seconds'], 3)
                    if outcome == 'aborted':
                        (session / 'stop').touch()
                        self.assertTrue(cancel())
                        raise DiarizationCancelled()
                    return SimpleNamespace(actual_speakers=2, duration_seconds=12.5)
                if outcome == 'model_missing':
                    self.seg.unlink()
                with patch('core.diarize.diarize_session', side_effect=engine):
                    code, payload = self.invoke(*self.diarize_args(session))
                self.assertEqual(code, {'done': 0, 'model_missing': 1, 'aborted': 130}[outcome])
                current, _ = self.files(session)
                for key, value in original.items():
                    self.assertEqual(current[key], value)
                summary = SessionStore(self.root / 'output').get(session.relative_to(self.root / 'output').as_posix())['session']
                self.assertEqual((summary['phase'], summary['mode']), ('done', 'file'))
                self.assertEqual((summary['started_at'], summary['elapsed'], summary['sections_total']),
                                 (original['started_at'], 12.5, 2))
                self.assertTrue(summary['has_recording'])
                self.assertEqual(summary['diarization_status']['phase'], 'failed' if outcome == 'model_missing' else outcome)
                if outcome == 'model_missing':
                    self.assertIn('model_missing', summary['diarization_status']['last_error'])
                self.seg.write_bytes(b'segmentation model')

    def test_finalization_phase_failure_releases_lock_and_restores_signals(self):
        import signal
        original_phase = Status.phase
        previous = signal.getsignal(signal.SIGINT)
        def phase(status, name, **extra):
            if name == 'finishing':
                raise OSError('injected status write failure')
            return original_phase(status, name, **extra)
        with patch.object(Status, 'phase', phase):
            code, payload = self.import_audio()
        self.assertEqual((code, payload['accepted'], payload['error']['code']), (1, False, 'write_failed'))
        self.assertFalse((self.state / 'run.json').exists())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        lock = RunLock(self.state)
        self.assertIsNone(lock.acquire())
        lock.release()
        session = next((self.root / 'output' / 'meetings').iterdir())
        self.assertEqual(self.files(session)[0]['phase'], 'failed')

    def test_cleanup_failure_and_unexpected_runtime_errors_are_operation_errors(self):
        with patch.object(MeetingRun, '_cleanup', side_effect=TypeError('bug in cleanup')):
            code, payload = self.import_audio()
        self.assertEqual((code, payload['error']['code']), (1, 'asr_failed'))
        self.assertFalse((self.state / 'run.json').exists())
        for exc in (ValueError, TypeError, KeyError, AttributeError):
            with self.subTest(exception=exc), patch.object(MeetingRun, 'run', side_effect=exc('bug')):
                code, payload = self.import_audio()
                self.assertEqual((code, payload['error']['code']), (1, 'asr_failed'))

    def test_manifest_publication_wins_force_stop_before_and_after_engine_return(self):
        session = self.session()
        def engine(request, progress, cancel):
            # Publish, then invoke force in the tiny interval before returning.
            (session / 'diarization.current.json').write_text(json.dumps(
                dict(schema_version=1, generation='0002')))
            (session / 'stop_force').touch()
            self.assertFalse(cancel())
            return SimpleNamespace(actual_speakers=2, duration_seconds=12.5)
        real_cleanup = MeetingDiarize._cleanup
        def cleanup(job):
            # Also exercise the success flag after the engine has returned.
            job._force_exit()
            real_cleanup(job)
        with patch('core.diarize.diarize_session', side_effect=engine), patch.object(MeetingDiarize, '_cleanup', cleanup):
            code, payload = self.invoke(*self.diarize_args(session))
        self.assertEqual((code, payload['outcome']), (0, 'done'))
        status, events = self.files(session)
        self.assertEqual(status['phase'], 'done')
        self.assertIsNone(status['stop_reason'])
        self.assertFalse(any(event['type'] == 'diarization_cancelled' for event in events))
        self.assertFalse((self.state / 'run.json').exists())

    def test_snapshot_diarization_settings_are_explicit_and_errors_name_keys(self):
        session = self.session()
        with C.LOCAL_FILE.open('a') as stream:
            stream.write('[diarization]\ncluster_threshold=0.9\n')
        cfg = cli._meeting_diarize_config(session, [], 4)
        self.assertEqual(cfg.get('diarization.cluster_threshold'), 0.5)
        cfg = cli._meeting_diarize_config(session, ['diarization.cluster_threshold=0.7'], 4)
        self.assertEqual(cfg.get('diarization.cluster_threshold'), 0.7)
        for operation in ('run', 'diarize'):
            args = ('meeting', 'run', 'team', '--speakers', '2', '--json') if operation == 'run' else self.diarize_args(session)
            code, payload = self.invoke(*args, '--set', 'diarization.cluster_threshold=0')
            self.assertEqual(code, 2)
            self.assertIn('diarization.cluster_threshold', payload['error']['message'])
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
            cli.main(['meeting', 'diarize', '--help'])
        self.assertIn('config.used.toml', output.getvalue())
        self.assertIn('local.toml', output.getvalue())

    def test_signals_are_restored_after_import(self):
        import signal
        previous = signal.getsignal(signal.SIGINT)
        self.import_audio()
        self.assertIs(signal.getsignal(signal.SIGINT), previous)


class MeetingProcessTests(unittest.TestCase):
    """Real processes/stop files and OS lock, fake audio engines."""
    SCRIPT = r'''
import json, sys, time
from pathlib import Path
from unittest.mock import patch
from core import cli, config as C
from core.diarize import DiarizationCancelled
root = Path(sys.argv[1])
operation = sys.argv[2]
local = root / 'local.toml'
C.LOCAL_FILE = local
C.MEETINGS_DIR = root / 'meetings'
class Whisper:
    url = 'http://127.0.0.1:8178'
    def __init__(self, cfg): pass
    def ensure(self, path): pass
    def stop(self): pass
    def kill_now(self): pass
class Transcriber:
    def __init__(self, cfg, session, url, source, status):
        self.session, self.status, self.source = session, status, source
        self.stopped = False
        self.proc = None
    def request_stop(self): self.stopped = True
    def run(self):
        if self.source is None:
            self.recording_path = self.session / 'recording_120000.ogg'
            self.recording_path.write_bytes(b'complete recording')
        (root / 'entered-run').touch()
        while not self.stopped: time.sleep(.05)
        self.status.event('capture_integrity', lost_ratio=0)
        return dict(duration=12.5, ffmpeg_failed=False, capture=dict(lost_ratio=0),
                    aborted=self.source is not None and self.stopped)
def diarize(request, progress, cancel):
    (root / 'entered-diarize').touch()
    while not cancel(): time.sleep(.05)
    raise DiarizationCancelled()
with patch('core.session.WhisperServer', Whisper), patch('core.session.Transcriber', Transcriber), \
     patch('core.session.source_duration', return_value=12.5), patch('core.diarize.diarize_session', diarize):
    args = ['meeting', 'run', 'team'] if operation in ('run', 'import') else ['meeting', 'diarize', str(root / 'session')]
    if operation == 'import': args += ['--file', str(root / 'input.ogg')]
    sys.exit(cli.main([*args, '--speakers', '2', '--json']))
'''

    def setup_root(self, root):
        (root / 'meetings').mkdir()
        (root / 'meetings' / 'team.toml').write_text('[meeting]\nname="Team"')
        (root / 'local.toml').write_text(f'[paths]\nstate_dir="{root / "state"}"\noutput_root="{root / "output"}"\n'
            f'[diarization]\nsegmentation_model="{root / "seg"}"\nembedding_model="{root / "emb"}"\n'
            '[system]\ninhibit_sleep=false')
        (root / 'seg').write_bytes(b'model')
        (root / 'emb').write_bytes(b'model')
        session = root / 'session'
        session.mkdir()
        (session / 'config.used.toml').write_text('[work]\ntype="meeting"\n[meeting]\nname="Team"')
        audio = b'complete audio'
        (root / 'input.ogg').write_bytes(audio)
        (session / 'source_audio.ogg').write_bytes(audio)
        (session / 'source.json').write_text(json.dumps(dict(schema_version=1, kind='import',
            original_name='input.ogg', path='source_audio.ogg', size_bytes=len(audio),
            sha256=hashlib.sha256(audio).hexdigest(), duration_seconds=12.5, created_at='2026-10-14T12:00:00+08:00', requested_speakers=2)))

    def start(self, root, operation):
        proc = subprocess.Popen([sys.executable, '-c', self.SCRIPT, str(root), operation],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=3)
        self.addCleanup(cleanup)
        return proc

    def wait_path(self, path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(path.exists(), str(path))

    def test_simultaneous_run_and_diarize_only_one_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.setup_root(root)
            first, second = self.start(root, 'run'), self.start(root, 'diarize')
            self.wait_path(root / 'state' / 'run.json')
            deadline = time.monotonic() + 10
            while first.poll() is None and second.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            busy = first if first.poll() is not None else second
            self.assertEqual(busy.returncode, 3)
            stdout, stderr = busy.communicate(timeout=3)
            self.assertEqual(stderr, '')
            self.assertEqual(json.loads(stdout)['error']['code'], 'busy')
            self.wait_path(root / ('entered-diarize' if busy is first else 'entered-run'))
            active = json.loads((root / 'state' / 'run.json').read_text())
            (Path(active['session']) / 'stop').touch()
            winner = second if busy is first else first
            stdout, stderr = winner.communicate(timeout=10)
            self.assertEqual(stderr, '')
            self.assertTrue(json.loads(stdout)['accepted'])
            self.assertIn(winner.returncode, (0, 130))
            self.assertFalse((root / 'state' / 'run.json').exists())

    def test_live_stop_force_and_ctrl_c_envelopes_and_source_policy(self):
        import signal
        for stop in ('stop', 'stop_force', 'SIGINT'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.setup_root(root)
                proc = self.start(root, 'run')
                self.wait_path(root / 'entered-run')
                active = json.loads((root / 'state' / 'run.json').read_text())
                session = Path(active['session'])
                if stop == 'SIGINT':
                    proc.send_signal(signal.SIGINT)
                else:
                    (session / stop).touch()
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(stderr, '')
                payload = json.loads(stdout)
                self.assertTrue(payload['accepted'])
                self.assertEqual((proc.returncode, payload['outcome']), (130, 'aborted') if stop == 'stop_force' else (0, 'done'))
                status = json.loads((session / 'status.json').read_text())
                self.assertEqual(status['stop_reason'], 'user')
                self.assertEqual((session / 'source.json').exists(), stop != 'stop_force')
                self.assertFalse((root / 'state' / 'run.json').exists())


    def test_import_stop_and_sigint_abort_and_preserve_verified_source(self):
        import signal
        for stop in ('stop', 'SIGINT'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.setup_root(root)
                proc = self.start(root, 'import')
                self.wait_path(root / 'entered-run')
                active = json.loads((root / 'state' / 'run.json').read_text())
                session = Path(active['session'])
                if stop == 'SIGINT':
                    proc.send_signal(signal.SIGINT)
                else:
                    (session / 'stop').touch()
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(stderr, '')
                payload = json.loads(stdout)
                self.assertEqual((proc.returncode, payload['accepted'], payload['outcome']), (130, True, 'aborted'))
                status = json.loads((session / 'status.json').read_text())
                self.assertEqual((status['phase'], status['stop_reason']), ('aborted', 'user'))
                phases = [json.loads(line)['phase'] for line in (session / 'events.jsonl').read_text().splitlines()
                          if json.loads(line)['type'] == 'phase']
                self.assertEqual(phases[-2:], ['finishing', 'aborted'])
                self.assertTrue((session / 'source.json').exists())
                source, metadata = verify_meeting_source(session)
                self.assertEqual(source.read_bytes(), (root / 'input.ogg').read_bytes())
                self.assertEqual(metadata['kind'], 'import')
                self.assertFalse((root / 'state' / 'run.json').exists())


class SourceDurationTests(unittest.TestCase):
    def test_decode_timeout_scales_with_long_audio(self):
        from core.session import source_duration
        for duration, timeout in ((12.5, 120), (10800, 21600)):
            with self.subTest(duration=duration), patch('core.session.subprocess.run', side_effect=[
                SimpleNamespace(returncode=0, stdout=json.dumps({'format': {'duration': duration}}).encode()),
                SimpleNamespace(returncode=0)]) as run:
                self.assertEqual(source_duration(Path('audio.ogg')), duration)
                self.assertEqual(run.call_args.kwargs['timeout'], timeout)
