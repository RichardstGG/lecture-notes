"""core/platform.py：引擎執行檔搜尋（find_engine_bin）與動態函式庫搜尋路徑
（library_dirs / env_with_libs）。用暫存資料夾模擬三種平台的 build 產物佈局，
不需要真的編譯 whisper.cpp / llama.cpp。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import platform as P


class FindEngineBinTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.engine_dir = Path(self._tmp.name) / "whisper.cpp"

    def _touch(self, rel):
        p = self.engine_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
        return p

    def test_posix_plain_bin_layout(self):
        want = self._touch("build/bin/whisper-server")
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P, "EXE", ""):
            self.assertEqual(P.find_engine_bin(self.engine_dir, "whisper-server"), want)

    def test_windows_multiconfig_release_layout(self):
        want = self._touch("build/bin/Release/whisper-server.exe")
        with mock.patch.object(P, "IS_WINDOWS", True), mock.patch.object(P, "EXE", ".exe"):
            self.assertEqual(P.find_engine_bin(self.engine_dir, "whisper-server"), want)

    def test_windows_top_level_release_layout(self):
        want = self._touch("build/Release/llama-server.exe")
        with mock.patch.object(P, "IS_WINDOWS", True), mock.patch.object(P, "EXE", ".exe"):
            self.assertEqual(P.find_engine_bin(self.engine_dir, "llama-server"), want)

    def test_missing_binary_returns_expected_default_path(self):
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P, "EXE", ""):
            got = P.find_engine_bin(self.engine_dir, "whisper-server")
        self.assertEqual(got, self.engine_dir / "build" / "bin" / "whisper-server")
        self.assertFalse(got.exists())

    def test_first_match_wins_when_multiple_layouts_exist(self):
        # bin/ 應該比 bin/Release、Release 優先（見 find_engine_bin 裡的搜尋順序）
        preferred = self._touch("build/bin/whisper-server")
        self._touch("build/bin/Release/whisper-server")
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P, "EXE", ""):
            self.assertEqual(P.find_engine_bin(self.engine_dir, "whisper-server"), preferred)


class LibraryDirsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _make_binary_and_libs(self, exe_rel, lib_rels):
        binary = self.root / exe_rel
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("")
        for rel in lib_rels:
            lib = self.root / rel
            lib.parent.mkdir(parents=True, exist_ok=True)
            lib.write_text("")
        return binary

    def test_linux_finds_shared_objects(self):
        binary = self._make_binary_and_libs("build/bin/whisper-server",
                                             ["build/bin/libggml.so", "build/lib/libwhisper.so.1"])
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P, "NAME", "linux"):
            dirs = P.library_dirs(binary)
        self.assertEqual(set(dirs), {str(self.root / "build" / "bin"), str(self.root / "build" / "lib")})

    def test_macos_finds_dylibs(self):
        binary = self._make_binary_and_libs("build/bin/whisper-server", ["build/bin/libggml.dylib"])
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P, "NAME", "macos"):
            dirs = P.library_dirs(binary)
        self.assertEqual(dirs, [str(self.root / "build" / "bin")])

    def test_windows_finds_dlls(self):
        binary = self._make_binary_and_libs("build/bin/Release/whisper-server.exe",
                                             ["build/bin/Release/ggml.dll"])
        with mock.patch.object(P, "IS_WINDOWS", True), mock.patch.object(P, "NAME", "windows"):
            dirs = P.library_dirs(binary)
        self.assertEqual(dirs, [str(self.root / "build" / "bin" / "Release")])

    def test_no_libs_returns_empty_list(self):
        binary = self._make_binary_and_libs("build/bin/whisper-server", [])
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P, "NAME", "linux"):
            self.assertEqual(P.library_dirs(binary), [])


class EnvWithLibsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.binary = self.root / "build" / "bin" / "whisper-server"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("")
        # 三種平台各自的函式庫副檔名都放一份，讓同一份 fixture 能給三個平台的測試共用
        for name in ("libggml.so", "libggml.dylib", "ggml.dll"):
            (self.binary.parent / name).write_text("")

    def test_linux_uses_ld_library_path_and_keeps_old_value(self):
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P, "NAME", "linux"):
            env = P.env_with_libs(self.binary, {"LD_LIBRARY_PATH": "/opt/existing"})
        self.assertTrue(env["LD_LIBRARY_PATH"].startswith(str(self.binary.parent)))
        self.assertIn("/opt/existing", env["LD_LIBRARY_PATH"])

    def test_windows_uses_path(self):
        with mock.patch.object(P, "IS_WINDOWS", True), mock.patch.object(P, "NAME", "windows"):
            env = P.env_with_libs(self.binary, {"PATH": r"C:\Windows\System32"})
        self.assertIn(str(self.binary.parent), env["PATH"])
        self.assertIn(r"C:\Windows\System32", env["PATH"])

    def test_macos_uses_dyld_library_path(self):
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P, "NAME", "macos"):
            env = P.env_with_libs(self.binary, {})
        self.assertIn("DYLD_LIBRARY_PATH", env)

    def test_defaults_to_os_environ_when_env_not_given(self):
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P, "NAME", "linux"):
            env = P.env_with_libs(self.binary)
        self.assertIsNot(env, os.environ)   # 回傳的是一份副本，不會動到真正的 os.environ
        self.assertIn("LD_LIBRARY_PATH", env)


if __name__ == "__main__":
    unittest.main()
