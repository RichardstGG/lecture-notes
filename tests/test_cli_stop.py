"""Stop commands only write requests to the current session, never OS signals."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core.cli import main
from core.status import RunLock


class StopContractTests(unittest.TestCase):
    def test_normal_and_force_target_the_single_active_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / 'meeting'
            session.mkdir()
            owner = RunLock(root)
            owner.acquire(work_type='meeting', mode='diarize', session=str(session))
            try:
                with patch('core.cli._state_dir', return_value=root), \
                        patch('core.platform.interrupt') as interrupt, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['stop']), 0)
                    self.assertEqual(main(['stop', '--force']), 0)
                self.assertTrue((session / 'stop').exists())
                self.assertTrue((session / 'stop_force').exists())
                self.assertTrue(owner.held)
                interrupt.assert_not_called()
            finally:
                owner.release()

    def test_starting_without_session_returns_error_instead_of_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = RunLock(tmp)
            owner.acquire()
            try:
                with patch('core.cli._state_dir', return_value=Path(tmp)), \
                        patch('core.platform.interrupt') as interrupt, \
                        contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as result:
                    main(['stop', '--force'])
                self.assertEqual(result.exception.code, 1)
                self.assertIn('請稍後重試', err.getvalue())
                interrupt.assert_not_called()
            finally:
                owner.release()

    def test_lock_io_errors_are_reported_as_cli_errors(self):
        for args in (["status", "--json"], ["stop"]):
            with self.subTest(args=args), patch('core.cli._state_dir', return_value=Path('unused')), \
                    patch('core.status.RunLock.current', side_effect=OSError('unreadable')), \
                    contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as result:
                main(args)
            self.assertEqual(result.exception.code, 1)
            self.assertIn('無法讀取工作鎖', err.getvalue())

    def test_pending_lock_without_pid_never_crashes_or_sends_signal(self):
        with patch('core.cli._state_dir', return_value=Path('unused')), \
                patch('core.status.RunLock.current', return_value=RunLock._pending()), \
                patch('core.platform.interrupt') as interrupt, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            main(['stop'])
        self.assertEqual(result.exception.code, 1)
        interrupt.assert_not_called()
