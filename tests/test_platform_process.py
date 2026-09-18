"""core/platform.py：行程操作（spawn flags、存活判斷、中斷/強制關閉、防休眠）。

全部用 mock 取代真正的 subprocess/ctypes/os 呼叫，不會建立或殺掉真的行程，
也不需要 Windows 或 macOS 實機——Windows 分支的 ctypes.windll 在 Linux 上
本來就不存在，用 mock.patch.object(..., create=True) 假造。
"""
import os
import signal
import types
import unittest
from unittest import mock

from . import _pathfix  # noqa: F401
from core import platform as P


class SpawnKwargsTests(unittest.TestCase):
    """CREATE_NEW_PROCESS_GROUP / CREATE_NO_WINDOW 只在 Windows 上的 subprocess 模組才存在，
    在 Linux/macOS 上用 mock 補一個假值上去，才能測試 Windows 分支的邏輯。"""

    def test_posix_uses_new_session(self):
        with mock.patch.object(P, "IS_WINDOWS", False):
            self.assertEqual(P.spawn_kwargs(), {"start_new_session": True})

    def test_windows_uses_new_process_group(self):
        with mock.patch.object(P, "IS_WINDOWS", True), \
                mock.patch.object(P.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True):
            self.assertEqual(P.spawn_kwargs(), {"creationflags": 0x200})

    def test_no_detach_returns_empty(self):
        self.assertEqual(P.spawn_kwargs(detach=False), {})


class PidAliveTests(unittest.TestCase):
    def test_zero_or_none_is_never_alive(self):
        self.assertFalse(P.pid_alive(0))
        self.assertFalse(P.pid_alive(None))

    def test_posix_current_process_is_alive(self):
        with mock.patch.object(P, "IS_WINDOWS", False):
            self.assertTrue(P.pid_alive(os.getpid()))

    def test_posix_lookup_error_means_dead(self):
        with mock.patch.object(P, "IS_WINDOWS", False), \
                mock.patch.object(P.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(P.pid_alive(123456))

    def test_posix_permission_error_means_alive(self):
        with mock.patch.object(P, "IS_WINDOWS", False), \
                mock.patch.object(P.os, "kill", side_effect=PermissionError):
            self.assertTrue(P.pid_alive(1))

    def _fake_kernel32(self, open_handle, exit_code):
        k32 = mock.Mock()
        k32.OpenProcess.return_value = open_handle

        def get_exit_code(handle, byref_code):
            byref_code._obj.value = exit_code
            return 1
        k32.GetExitCodeProcess.side_effect = get_exit_code
        return k32

    def test_windows_still_active_process(self):
        STILL_ACTIVE = 259
        fake_ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(kernel32=self._fake_kernel32(handle_val := 42, STILL_ACTIVE)),
            c_ulong=P.ctypes.c_ulong, byref=P.ctypes.byref,
        )
        with mock.patch.object(P, "IS_WINDOWS", True), mock.patch.object(P, "ctypes", fake_ctypes):
            self.assertTrue(P.pid_alive(999))

    def test_windows_exited_process(self):
        fake_ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(kernel32=self._fake_kernel32(42, 0)),
            c_ulong=P.ctypes.c_ulong, byref=P.ctypes.byref,
        )
        with mock.patch.object(P, "IS_WINDOWS", True), mock.patch.object(P, "ctypes", fake_ctypes):
            self.assertFalse(P.pid_alive(999))

    def test_windows_open_process_fails_means_dead(self):
        k32 = mock.Mock()
        k32.OpenProcess.return_value = 0
        fake_ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(kernel32=k32),
            c_ulong=P.ctypes.c_ulong, byref=P.ctypes.byref,
        )
        with mock.patch.object(P, "IS_WINDOWS", True), mock.patch.object(P, "ctypes", fake_ctypes):
            self.assertFalse(P.pid_alive(999))


class InterruptTests(unittest.TestCase):
    def test_windows_cannot_send_sigint(self):
        with mock.patch.object(P, "IS_WINDOWS", True):
            self.assertFalse(P.interrupt(123))

    def test_posix_sends_sigint(self):
        with mock.patch.object(P, "IS_WINDOWS", False), \
                mock.patch.object(P.os, "kill") as kill:
            self.assertTrue(P.interrupt(123))
            kill.assert_called_once_with(123, signal.SIGINT)

    def test_posix_returns_false_on_oserror(self):
        with mock.patch.object(P, "IS_WINDOWS", False), \
                mock.patch.object(P.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(P.interrupt(123))


class KillTests(unittest.TestCase):
    def test_windows_kill_tree_uses_taskkill(self):
        with mock.patch.object(P, "IS_WINDOWS", True), \
                mock.patch.object(P.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                mock.patch.object(P.subprocess, "run") as run:
            P.kill_tree(123)
            args = run.call_args[0][0]
            self.assertEqual(args[:4], ["taskkill", "/PID", "123", "/T"])
            self.assertIn("/F", args)

    def test_windows_kill_now_uses_taskkill(self):
        with mock.patch.object(P, "IS_WINDOWS", True), \
                mock.patch.object(P.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                mock.patch.object(P.subprocess, "run") as run:
            P.kill_now(123)
            self.assertTrue(run.called)

    def test_posix_kill_tree_returns_immediately_if_already_dead(self):
        with mock.patch.object(P, "IS_WINDOWS", False), \
                mock.patch.object(P.os, "killpg", side_effect=ProcessLookupError) as killpg, \
                mock.patch.object(P.time, "sleep") as sleep:
            P.kill_tree(123, timeout=5)
            killpg.assert_called_once_with(123, signal.SIGTERM)
            sleep.assert_not_called()

    def test_posix_kill_tree_falls_back_to_kill_on_permission_error(self):
        calls = []

        def fake_killpg(pid, sig):
            calls.append(sig)
            raise PermissionError

        with mock.patch.object(P, "IS_WINDOWS", False), \
                mock.patch.object(P.os, "killpg", side_effect=fake_killpg), \
                mock.patch.object(P.os, "kill") as kill, \
                mock.patch.object(P, "pid_alive", return_value=False), \
                mock.patch.object(P.os, "waitpid", side_effect=ChildProcessError):
            P.kill_tree(123, timeout=1)
            self.assertTrue(kill.called, "killpg 沒權限時應該 fallback 到 os.kill")

    def test_posix_kill_now_sends_sigkill(self):
        with mock.patch.object(P, "IS_WINDOWS", False), \
                mock.patch.object(P.os, "killpg") as killpg:
            P.kill_now(123)
            killpg.assert_called_once_with(123, signal.SIGKILL)


class InhibitorTests(unittest.TestCase):
    def test_windows_start_stop_calls_set_thread_execution_state(self):
        k32 = mock.Mock()
        k32.SetThreadExecutionState.return_value = 1
        fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=k32))
        inhibitor = P.Inhibitor()
        with mock.patch.object(P, "IS_WINDOWS", True), mock.patch.object(P, "ctypes", fake_ctypes):
            inhibitor.start("test", log=lambda *_a, **_k: None)
            self.assertTrue(inhibitor.windows_held)
            first_call = k32.SetThreadExecutionState.call_args_list[0][0][0]
            self.assertEqual(first_call, 0x80000000 | 0x00000001)
            inhibitor.stop()
            self.assertFalse(inhibitor.windows_held)
            last_call = k32.SetThreadExecutionState.call_args_list[-1][0][0]
            self.assertEqual(last_call, 0x80000000)

    def test_posix_start_skips_when_tool_missing(self):
        messages = []
        inhibitor = P.Inhibitor()
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(P.shutil, "which", return_value=None):
            inhibitor.start("test", log=messages.append)
        self.assertIsNone(inhibitor.proc)
        self.assertTrue(any("systemd-inhibit" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
