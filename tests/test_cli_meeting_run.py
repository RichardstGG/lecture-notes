"""CLI JSON/exit contracts; no audio devices or engines."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core import cli
from core.session import MeetingRunError
from tests.test_session_meeting_run import config


class MeetingRunCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = cli.main(['meeting', 'run', 'meeting', '--json', *args])
            except SystemExit as exc:
                result = exc.code
        return result, json.loads(out.getvalue()), err.getvalue()

    def test_parser_and_value_errors_are_one_json_and_exit_two(self):
        for args in ([], ['--speakers', 'abc'], ['--speakers', '0'], ['--speakers', '31'],
                     ['--speakers', '1', '--unknown'], ['--speakers', '1', '--file', 'a.wav', '--source', 'mic']):
            with self.subTest(args=args), patch('core.session.MeetingRun') as job:
                code, data, _ = self.invoke(args)
            self.assertEqual((code, data['accepted'], data['error']['code']), (2, False, 'input_invalid'))
            job.assert_not_called()

    def test_error_mapping_does_not_change_busy_or_failure_codes(self):
        for error, expected in [(MeetingRunError('run_active', 'busy', 3), 3),
                                (MeetingRunError('capture_loss', 'bad capture'), 1),
                                (OSError('private contents'), 1)]:
            with self.subTest(expected=expected), patch('core.cli.C.load_meeting', return_value=config(self.root)), \
                    patch('core.session.MeetingRun') as job:
                job.return_value.run.side_effect = error
                code, result, err = self.invoke(['--speakers', '10'])
            self.assertEqual(code, expected)
            self.assertFalse(result['accepted'])
            self.assertNotIn('private contents', err)

    def test_success_and_cancel_keep_one_json_despite_engine_stdout(self):
        for outcome, code in [('done', 0), ('aborted', 130)]:
            response = {'schema_version': 1, 'accepted': True, 'work_type': 'meeting',
                        'operation': 'run', 'session': str(self.root), 'pid': 99, 'outcome': outcome}
            def execute():
                print('private transcript content')
                return response
            with self.subTest(outcome=outcome), patch('core.cli.C.load_meeting', return_value=config(self.root)), \
                    patch('core.session.MeetingRun') as job:
                job.return_value.run.side_effect = execute
                actual, result, err = self.invoke(['--speakers', '10'])
            self.assertEqual((actual, result, err), (code, response, ''))

    def test_explicit_speakers_has_last_precedence(self):
        with patch('core.cli.C.load_meeting', return_value=config(self.root)) as load, \
                patch('core.session.MeetingRun') as job:
            job.return_value.run.return_value = {'accepted': True, 'outcome': 'done'}
            code, _, _ = self.invoke(['--speakers', '10', '--set', 'diarization.num_speakers=4', '--source', 'chosen'])
        self.assertEqual(code, 0)
        self.assertEqual(load.call_args.kwargs['sets'], ['diarization.num_speakers=4', 'diarization.num_speakers=10'])
        self.assertEqual(load.call_args.kwargs['source'], 'chosen')
