"""Public meeting settings commands; no engine or microphone involved."""
import contextlib
import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core import config as C
from core.cli import main


class MeetingCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.multiple(C, MEETINGS_DIR=self.root / 'meetings',
                                   COURSES_DIR=self.root / 'courses', LOCAL_FILE=self.root / 'local.toml')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        C.MEETINGS_DIR.mkdir()
        C.COURSES_DIR.mkdir()

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_versioned_inventory_uses_meetings_only_with_per_file_errors(self):
        (C.MEETINGS_DIR / '設計.toml').write_text('[meeting]\nname="設計討論"\n[diarization]\nnum_speakers=10', encoding='utf-8')
        (C.MEETINGS_DIR / 'invalid.toml').write_text('invalid=[', encoding='utf-8')
        (C.COURSES_DIR / 'course.toml').write_text('[course]\nname="course"', encoding='utf-8')
        code, out, err = self.invoke(['meetings', '--json'])
        self.assertEqual((code, err), (0, ''))
        data = json.loads(out)
        self.assertEqual(data['schema_version'], 1)
        rows = {row['id']: row for row in data['meetings']}
        self.assertEqual(set(rows), {'設計', 'invalid'})
        self.assertEqual(rows['設計']['name'], '設計討論')
        self.assertEqual(rows['設計']['num_speakers'], 10)
        self.assertEqual(rows['設計']['work_type'], 'meeting')
        self.assertIn('error', rows['invalid'])

    def test_empty_inventory_and_text_are_read_only(self):
        self.assertEqual(json.loads(self.invoke(['meetings', '--json'])[1]), {'schema_version': 1, 'meetings': []})
        self.assertIn('實驗中', self.invoke(['meetings'])[1])
        self.assertEqual(list(C.MEETINGS_DIR.iterdir()), [])

    def test_config_selection_and_existing_lecture_default(self):
        (C.MEETINGS_DIR / 'same.toml').write_text('[meeting]\nname="meeting"', encoding='utf-8')
        (C.COURSES_DIR / 'same.toml').write_text('[course]\nname="lecture"', encoding='utf-8')
        code, out, err = self.invoke(['config', '--work-type', 'meeting', 'same', '--set', 'diarization.num_speakers=5', '--source', 'chosen'])
        self.assertEqual((code, err), (0, ''))
        cfg = tomllib.loads(out)
        self.assertEqual(cfg['work']['type'], 'meeting')
        self.assertEqual(cfg['audio']['source'], 'chosen')
        self.assertFalse(cfg['summary']['enabled'])
        self.assertEqual(cfg['diarization']['num_speakers'], 5)
        lecture = tomllib.loads(self.invoke(['config', 'same'])[1])
        explicit = tomllib.loads(self.invoke(['config', '--work-type', 'lecture', 'same'])[1])
        self.assertEqual(lecture, explicit)
        self.assertEqual(lecture['course']['name'], 'lecture')

    def test_invalid_meeting_config_exit_two_with_no_stdout(self):
        for args in (['missing'], ['../outside'], ['--model', 'model'], ['--upstream', 'lab'],
                     ['--set', 'diarization.num_speakers=31']):
            with self.subTest(args=args):
                code, out, err = self.invoke(['config', '--work-type', 'meeting', *args])
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(err)

    def test_queries_do_not_start_engines_or_create_sessions(self):
        with patch('subprocess.Popen') as spawn:
            self.assertEqual(self.invoke(['config', '--work-type', 'meeting'])[0], 0)
            self.assertEqual(self.invoke(['meetings', '--json'])[0], 0)
        spawn.assert_not_called()
