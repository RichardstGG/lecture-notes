"""Exclusive meeting directory allocation and cross-platform path contracts."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core.config import Config, ConfigError
from core.session import LectureRun, OfflineSummary, make_meeting_session_dir


class MeetingDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cfg = Config({'meeting': {'name': '設計/討論'}, 'paths': {
            'output_root': str(self.root / 'outputs'),
            'meeting_output_root': 'meetings',
            'meeting_session_name': '{meeting}_{date:%Y%m%d_%H%M%S}',
        }})

    def test_meeting_or_unknown_config_cannot_enter_lecture_workflows(self):
        from core import config as C
        (self.root / "transcript.md").write_text("existing transcript", encoding="utf-8")
        for work_type in ('meeting', 'future'):
            cfg = Config(C.load_toml(C.DEFAULT_FILE))
            cfg.data['work'] = {'type': work_type}
            for job in (LectureRun(cfg), OfflineSummary(cfg, self.root)):
                error = io.StringIO()
                with self.subTest(work_type=work_type, job=type(job).__name__), \
                        patch.object(job.lock, 'acquire') as acquire, \
                        contextlib.redirect_stderr(error), self.assertRaises(SystemExit):
                    job.run()
                acquire.assert_not_called()
                self.assertIn("只處理 lecture", error.getvalue())

    def test_timestamp_name_and_empty_collisions_never_reused(self):
        with patch('core.session.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 8, 14, 30)
            paths = [make_meeting_session_dir(self.cfg) for _ in range(3)]
        self.assertEqual([p.name for p in paths], ['設計_討論_20261008_143000',
                                               '設計_討論_20261008_143000-2',
                                               '設計_討論_20261008_143000-3'])
        self.assertTrue(all(p.is_dir() for p in paths))
        self.assertEqual(paths[0].parent, self.root / 'outputs' / 'meetings')

    def test_existing_file_is_not_overwritten(self):
        self.cfg.data['paths']['meeting_session_name'] = 'same'
        parent = self.root / 'outputs' / 'meetings'
        parent.mkdir(parents=True)
        (parent / 'same').write_text('keep', encoding='utf-8')
        self.assertEqual(make_meeting_session_dir(self.cfg).name, 'same-2')
        self.assertEqual((parent / 'same').read_text(encoding='utf-8'), 'keep')

    def test_portable_relative_root_rejects_escape_without_creating_output(self):
        for value in ('', '.', '..', '../outside', 'meeting/../outside', '/absolute', 'C:\\outside',
                      'C:relative', '\\\\server\\share', 'nested//empty', 'nested/CON', 'nested/tail.', 3):
            with self.subTest(value=value):
                self.cfg.data['paths']['meeting_output_root'] = value
                with self.assertRaises(ConfigError):
                    make_meeting_session_dir(self.cfg)
                self.assertFalse((self.root / 'outputs').exists())

    def test_nested_relative_root_works_with_both_separator_styles(self):
        for value in ('archive/meetings', 'archive\\meetings'):
            self.cfg.data['paths']['meeting_output_root'] = value
            path = make_meeting_session_dir(self.cfg)
            self.assertEqual(path.parent, self.root / 'outputs' / 'archive' / 'meetings')

    def test_bad_template_is_rejected_before_creating_any_output(self):
        for template in ('', '{course}', '{meeting.__class__}', '{date', '../{meeting}', '{meeting}/{date}', '{meeting!r}'):
            with self.subTest(template=template):
                self.cfg.data['paths']['meeting_session_name'] = template
                with self.assertRaises(ConfigError):
                    make_meeting_session_dir(self.cfg)
                self.assertFalse((self.root / 'outputs').exists())

    def test_symlink_parent_is_rejected(self):
        output = self.root / 'outputs'
        output.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            (output / 'meetings').symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'symlinks unavailable: {exc}')
        with self.assertRaises(ConfigError):
            make_meeting_session_dir(self.cfg)
        self.assertEqual(list(outside.iterdir()), [])

    def test_independent_processes_allocate_distinct_directories(self):
        code = '''
import json, sys
from core.config import Config
from core.session import LectureRun, OfflineSummary, make_meeting_session_dir
cfg = Config(json.loads(sys.argv[1]))
sys.stdin.readline()
print(make_meeting_session_dir(cfg).name)
'''
        self.cfg.data['paths']['meeting_session_name'] = 'concurrent'
        processes = []
        try:
            for _ in range(4):
                processes.append(subprocess.Popen([sys.executable, '-c', code, json.dumps(self.cfg.data)],
                                                  cwd=Path(__file__).resolve().parents[1],
                                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE, text=True, encoding='utf-8'))
            for process in processes:
                process.stdin.write('\n')
                process.stdin.flush()
            names = []
            for process in processes:
                out, err = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, err)
                names.append(out.strip())
            self.assertEqual(set(names), {'concurrent', 'concurrent-2', 'concurrent-3', 'concurrent-4'})
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
