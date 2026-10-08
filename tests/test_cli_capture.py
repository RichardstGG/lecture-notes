"""Public capability JSON and fail-closed session boundary (no hardware)."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch
from tests import _pathfix  # noqa: F401
from core import config as C
from core.cli import main
from core.session import LectureRun


class CaptureCliTests(unittest.TestCase):
    def test_capabilities_on_all_platforms_do_not_probe_or_start(self):
        for platform, reason in [('linux', 'engine_not_integrated'),
                                 ('darwin', 'unsupported_platform'), ('win32', 'unsupported_platform')]:
            with self.subTest(platform=platform), patch('core.session.sys.platform', platform), \
                    patch('subprocess.Popen') as spawn, patch.object(C, 'load') as load:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(['capture-capabilities', '--json']), 0)
                result = json.loads(out.getvalue())
                self.assertEqual(result['schema_version'], 1)
                self.assertTrue(result['single']['available'])
                self.assertFalse(result['dual']['available'])
                self.assertEqual(result['dual']['reason_code'], reason)
                spawn.assert_not_called()
                load.assert_not_called()

    def test_dual_fails_before_lock_output_or_engine_for_live_and_file(self):
        data = C.load_toml(C.DEFAULT_FILE)
        data['audio'].update(capture_mode='dual', system_source='out.monitor', microphone_source='mic')
        for input_file in (None, 'missing.wav'):
            run = LectureRun(C.Config(data), input_file)
            with patch.object(run.lock, 'acquire') as acquire, \
                    patch('core.session.make_session_dir') as mkdir, \
                    patch('core.session.WhisperServer') as engine, contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as exc:
                run.run()
            self.assertEqual(exc.exception.code, 1)  # existing lec run error code
            acquire.assert_not_called()
            mkdir.assert_not_called()
            engine.assert_not_called()
