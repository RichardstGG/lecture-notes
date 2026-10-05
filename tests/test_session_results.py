"""CLI/status contract with mocked engines and real status/event files."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core import cli
from core.config import Config, DEFAULT_FILE, load_toml
from core.session import LectureRun


class SessionResultTests(unittest.TestCase):
    def run_result(self, result, *, live=False, summary=True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = load_toml(DEFAULT_FILE)
            data['paths'].update(output_root=str(root / 'outputs'), state_dir=str(root / 'state'))
            data['system']['inhibit_sleep'] = False
            data['summary'].update(enabled=summary, file_mode='after', final_wait=1)
            cfg = Config(data)
            source = root / 'input.ogg'
            source.touch()
            runs = []
            original_run = LectureRun.run

            def capture(run):
                runs.append(run)
                return original_run(run)

            with (patch('core.cli._load', return_value=(cfg, False)),
                  patch('core.session.shutil.which', return_value='/mock/tool'),
                  patch.object(LectureRun, '_install_signals'),
                  patch.object(LectureRun, '_start_stop_watcher'),
                  patch.object(LectureRun, 'run', capture),
                  patch('core.session.WhisperServer') as whisper,
                  patch('core.session.LlamaServer') as llama,
                  patch('core.session.Transcriber') as transcriber,
                  patch('core.session.Summarizer') as summary_cls):
                transcriber.return_value.run.return_value = result
                summarizer = summary_cls.return_value
                summarizer.abort = threading.Event()
                summarizer.run_all.return_value = 0
                worker_stopped = threading.Event()

                def live_worker(finish):
                    finish.wait(2)
                    worker_stopped.set()

                summarizer.run_live.side_effect = live_worker
                args = ['run', 'test'] + ([] if live else ['--file', str(source)])
                code = cli.main(args)
                run = runs[0]
                status = json.loads((run.dir / 'status.json').read_text())
                events = [json.loads(line) for line in (run.dir / 'events.jsonl').read_text().splitlines()]
                self.assertFalse((root / 'state' / 'run.json').exists())
                whisper.return_value.stop.assert_called()
                if result['ffmpeg_failed'] and result['duration'] == 0:
                    summarizer.run_all.assert_not_called()
                    if live and summary:
                        self.assertTrue(worker_stopped.is_set())
                        self.assertTrue(summarizer.abort.is_set())
                        llama.return_value.stop.assert_called()
                    else:
                        llama.assert_not_called()
                return code, status, events

    @staticmethod
    def result():
        return dict(duration=0, speed=0, aborted=False, gaps=[], lost_seconds=0,
                    ffmpeg_returncode=187, ffmpeg_failed=True)

    def test_zero_audio_decode_failure_is_cli_failure_in_both_modes(self):
        for live in (False, True):
            for summary in (False, True):
                with self.subTest(live=live, summary=summary):
                    code, status, events = self.run_result(self.result(), live=live, summary=summary)
                    self.assertEqual(code, 1)
                    self.assertEqual(status['phase'], 'failed')
                    self.assertIn('187', status['last_error'])
                    phases = [e['phase'] for e in events if e['type'] == 'phase']
                    self.assertNotIn('summarizing', phases)
                    self.assertNotIn('done', phases)
                    self.assertEqual(phases[-1], 'failed')

    def test_partial_audio_with_gaps_stays_done_and_records_details(self):
        result = self.result()
        result.update(duration=60, gaps=[{'start': 10, 'seconds': 5, 'reason': 'timeout'}], lost_seconds=5)
        code, status, events = self.run_result(result)
        self.assertEqual((code, status['phase']), (0, 'done'))
        gaps = [e for e in events if e['type'] == 'transcription_gaps']
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['gaps'], result['gaps'])
        self.assertEqual(gaps[0]['lost_seconds'], 5)
        self.assertEqual(gaps[0]['ffmpeg_returncode'], 187)
        self.assertEqual(gaps[0]['schema_version'], 1)
        self.assertIsInstance(gaps[0]['seq'], int)
        self.assertIn('time', gaps[0])

    def test_success_empty_and_normal_stop_keep_existing_success_contract(self):
        for duration, aborted, returncode in [(0, False, 0), (60, False, 0), (0, True, 255)]:
            with self.subTest(duration=duration, aborted=aborted):
                result = self.result()
                result.update(duration=duration, aborted=aborted,
                              ffmpeg_returncode=returncode, ffmpeg_failed=False)
                code, status, events = self.run_result(result, summary=False)
                self.assertEqual((code, status['phase']), (0, 'done'))
                self.assertFalse(any(e['type'] == 'transcription_gaps' for e in events))
