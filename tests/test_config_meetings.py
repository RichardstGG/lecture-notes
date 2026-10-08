"""Meeting configuration namespace, validation, merge and snapshot contracts."""
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core import config as C


class MeetingConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.meetings = self.root / 'meetings'
        self.courses = self.root / 'courses'
        self.meetings.mkdir()
        self.courses.mkdir()
        self.local = self.root / 'local.toml'
        self.patch = patch.multiple(C, MEETINGS_DIR=self.meetings, COURSES_DIR=self.courses,
                                   LOCAL_FILE=self.local, APP_ROOT=self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, name='same', text='[meeting]\nname = "會議"\n'):
        path = self.meetings / (name + '.toml')
        path.write_text(text, encoding='utf-8')
        return path

    def test_separate_namespace_and_merge_order(self):
        (self.courses / 'same.toml').write_text('[course]\nname="課堂"\n[whisper]\nlanguage="fr"\n', encoding='utf-8')
        self.local.write_text('[whisper]\nlanguage="en"\n[audio]\nsource="local"\n', encoding='utf-8')
        self.write(text='[meeting]\nname="會議"\n[whisper]\nlanguage="ja"\n[audio]\nsource="meeting"\n')
        cfg = C.load_meeting('same', sets=['whisper.language=zh', 'audio.source=overridden'], source='flag')
        self.assertEqual(cfg.get('whisper.language'), 'zh')
        self.assertEqual(cfg.get('audio.source'), 'flag')
        self.assertEqual(cfg.meeting_name, '會議')
        self.assertEqual(cfg.work_type, 'meeting')
        self.assertEqual(cfg.course_file, self.meetings / 'same.toml')
        self.assertFalse(cfg.get('summary.enabled'))
        lecture, _ = C.load('same')
        self.assertEqual(lecture.course_name, '課堂')
        self.assertEqual(lecture.get('whisper.language'), 'fr')
        self.assertTrue(lecture.get('summary.enabled'))
        self.assertEqual(lecture.work_type, 'lecture')

    def test_no_course_fallback_no_implicit_creation(self):
        (self.courses / 'only-course.toml').write_text('[course]\nname="course"\n', encoding='utf-8')
        with self.assertRaises(C.ConfigError):
            C.load_meeting('only-course')
        self.assertEqual(list(self.meetings.iterdir()), [])

    def test_empty_name_uses_id_and_defaults_query_needs_no_file(self):
        self.write(text='[meeting]\nname=""\n')
        self.assertEqual(C.load_meeting('same').meeting_name, 'same')
        self.assertEqual(C.load_meeting().meeting_name, '未命名會議')

    def test_summary_is_off_even_with_missing_model_or_cli_override(self):
        self.local.write_text('[summary]\nenabled=true\nmodel="missing"\nupstream="missing-api"\n', encoding='utf-8')
        self.write()
        with patch.object(C, 'load_upstreams', side_effect=AssertionError('must not load credentials')):
            cfg = C.load_meeting('same', sets=['summary.enabled=true'])
        self.assertFalse(cfg.get('summary.enabled'))
        self.assertEqual(cfg.get('summary.model'), 'missing')

    def test_snapshots_are_additive_nonmutating_and_unknown_types_stay_unknown(self):
        for raw, expected in [(None, 'lecture'), ('', 'lecture'), ('meeting', 'meeting'), ('future', 'future'), (3, 'unknown')]:
            data = {} if raw is None else {'work': {'type': raw}}
            cfg = C.Config(data)
            self.assertEqual(cfg.work_type, expected)
            self.assertEqual(tomllib.loads(cfg.dump())['work']['type'], expected)
            self.assertEqual(data, {} if raw is None else {'work': {'type': raw}})
        cfg = C.Config({'work': {'type': 'future', 'extension': 'preserved'}})
        self.assertEqual(tomllib.loads(cfg.dump())['work']['extension'], 'preserved')
        self.write()
        cfg = C.load_meeting('same')
        self.assertEqual(tomllib.loads(cfg.dump())['work']['type'], 'meeting')

    def test_speaker_count_zero_is_unset_but_not_an_execution_value(self):
        self.write()
        cfg = C.load_meeting('same')
        self.assertEqual(cfg.diarization_options()['requested_speakers'], 0)
        with self.assertRaises(C.ConfigError):
            cfg.diarization_options(require_speakers=True)
        for value in (1, 30):
            cfg = C.load_meeting('same', sets=[f'diarization.num_speakers={value}'])
            self.assertEqual(cfg.diarization_options(require_speakers=True)['requested_speakers'], value)
        for value in ('-1', '31', 'true', '1.0', '"2"', 'nan'):
            with self.subTest(value=value), self.assertRaises(C.ConfigError):
                C.load_meeting('same', sets=['diarization.num_speakers=' + value])

    def test_threshold_and_shift_reject_nonfinite_bool_and_bounds(self):
        self.write()
        for key, values in [('cluster_threshold', ('0', '-1', '1.1', 'true', 'nan', 'inf', '"0.5"')),
                            ('segmentation_window_shift', ('0', '-0.1', 'true', 'nan', 'inf', '"0.1"'))]:
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(C.ConfigError):
                    C.load_meeting('same', sets=[f'diarization.{key}={value}'])
        self.assertEqual(C.load_meeting('same', sets=['diarization.cluster_threshold=1']).diarization_options()['cluster_threshold'], 1)

    def test_model_paths_resolve_without_model_files(self):
        self.write()
        cfg = C.load_meeting('same')
        opts = cfg.diarization_options()
        self.assertEqual(opts['segmentation_model'].parent, self.root / 'models')
        for value in ('""', 'true', '[]'):
            with self.subTest(value=value), self.assertRaises(C.ConfigError):
                C.load_meeting('same', sets=['diarization.embedding_model=' + value])

    def test_settings_allowlist_and_malformed_shapes(self):
        for text in ('[course]\nname="bad"', '[summary]\nenabled=true', '[paths]\noutput_root="outside"',
                     '[work]\ntype="lecture"', '[meeting]\nunknown=1', 'audio=3', '[meeting]\nname=true',
                     '[whisper]\nterms=3', '[whisper]\nmodel=[]', '[diarization]\nnum_speakers={}'):
            with self.subTest(text=text):
                self.write(text=text)
                with self.assertRaises(C.ConfigError):
                    C.load_meeting('same')
        self.write()
        for expr in ('work.type=lecture', 'course.name=wrong', 'diarization.typo=3', 'meeting.name=3'):
            with self.subTest(expr=expr), self.assertRaises(C.ConfigError):
                C.load_meeting('same', sets=[expr])

    def test_portable_ids_reject_external_paths_and_reserved_names(self):
        for value in ('../course', '/tmp/meeting', 'C:\\meeting', 'a/b', 'a\\b', 'CON', 'com1.txt',
                      '.hidden', '-switch', 'tail.', ' leading', 'trailing ', 'a\x00b', 'a:b', '', '會' * 80):
            with self.subTest(value=value), self.assertRaises(C.ConfigError):
                C.meeting_path(value)
        self.assertEqual(C.meeting_path('設計會議 01').name, '設計會議 01.toml')

    def test_symlink_configuration_is_not_read(self):
        target = self.root / 'private.toml'
        target.write_text('[meeting]\nname="private"', encoding='utf-8')
        link = self.meetings / 'linked.toml'
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f'symlinks unavailable: {exc}')
        with self.assertRaises(C.ConfigError):
            C.load_meeting('linked')
