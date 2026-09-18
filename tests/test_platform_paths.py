"""core/platform.py：狀態資料夾預設路徑、錄音/引擎後端選擇（純函式，mock 模組全域變數）。

NAME / IS_WINDOWS / EXE 是模組載入時依 sys.platform 決定的全域變數；用
mock.patch.object 暫時改成別的平台來測試三個平台的分支，測完自動還原，
不會影響同一個 process 裡其他測試用到的真實平台判斷。
"""
import os
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import platform as P


class DefaultStateDirTests(unittest.TestCase):
    def test_linux_uses_xdg_state_home_when_set(self):
        with mock.patch.object(P, "NAME", "linux"), mock.patch.object(P, "IS_WINDOWS", False), \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-state"}, clear=False):
            self.assertEqual(P.default_state_dir(), Path("/tmp/xdg-state/lecture-notes"))

    def test_linux_falls_back_to_local_state(self):
        env = dict(os.environ)
        env.pop("XDG_STATE_HOME", None)
        with mock.patch.object(P, "NAME", "linux"), mock.patch.object(P, "IS_WINDOWS", False), \
                mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(P.default_state_dir(), Path.home() / ".local" / "state" / "lecture-notes")

    def test_macos_uses_application_support(self):
        with mock.patch.object(P, "NAME", "macos"), mock.patch.object(P, "IS_WINDOWS", False):
            expect = Path.home() / "Library" / "Application Support" / "lecture-notes"
            self.assertEqual(P.default_state_dir(), expect)

    def test_windows_uses_localappdata_when_set(self):
        with mock.patch.object(P, "NAME", "windows"), mock.patch.object(P, "IS_WINDOWS", True), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\test\AppData\Local"}):
            self.assertEqual(P.default_state_dir(),
                              Path(r"C:\Users\test\AppData\Local") / "lecture-notes")

    def test_windows_falls_back_when_localappdata_missing(self):
        env = dict(os.environ)
        env.pop("LOCALAPPDATA", None)
        with mock.patch.object(P, "NAME", "windows"), mock.patch.object(P, "IS_WINDOWS", True), \
                mock.patch.dict(os.environ, env, clear=True):
            expect = Path.home() / "AppData" / "Local" / "lecture-notes"
            self.assertEqual(P.default_state_dir(), expect)


class BackendSelectionTests(unittest.TestCase):
    def test_audio_backend_defaults_per_platform(self):
        for name, want in (("linux", "pulse"), ("macos", "avfoundation"), ("windows", "dshow")):
            with mock.patch.object(P, "NAME", name):
                self.assertEqual(P.audio_backend("auto"), want)
                self.assertEqual(P.audio_backend(""), want)
                self.assertEqual(P.audio_backend(None), want)

    def test_audio_backend_explicit_override_wins(self):
        with mock.patch.object(P, "NAME", "linux"):
            self.assertEqual(P.audio_backend("dshow"), "dshow")

    def test_engine_backend_defaults_per_platform(self):
        for name, want in (("linux", "vulkan"), ("macos", "metal"), ("windows", "vulkan")):
            with mock.patch.object(P, "NAME", name):
                self.assertEqual(P.engine_backend("auto"), want)

    def test_engine_backend_explicit_override_wins(self):
        with mock.patch.object(P, "NAME", "windows"):
            self.assertEqual(P.engine_backend("cpu"), "cpu")


if __name__ == "__main__":
    unittest.main()
