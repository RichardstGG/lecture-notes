"""setup_engines.py：後端／generator 選擇、engines.lock 讀寫、編譯工具檢查。

全部用暫存資料夾與 mock 取代真正的 git clone、cmake 編譯與網路下載，
不需要實際編譯 whisper.cpp / llama.cpp，也不會動到 repo 裡真正的 engines.lock。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import platform as P
import setup_engines as SE


class BackendFlagsTests(unittest.TestCase):
    def test_all_backends_have_cmake_flags(self):
        for backend in ("vulkan", "cuda", "metal", "cpu"):
            self.assertIn(backend, SE.BACKEND_FLAGS)
            self.assertIsInstance(SE.BACKEND_FLAGS[backend], list)

    def test_default_backend_per_platform_matches_platform_module(self):
        # setup_engines 透過 core.platform.engine_backend() 決定預設後端，這裡確認
        # 兩邊沒有各自維護一份互相矛盾的對照表。
        for name, want in (("linux", "vulkan"), ("macos", "metal"), ("windows", "vulkan")):
            with mock.patch.object(P, "NAME", name):
                self.assertEqual(P.engine_backend("auto"), want)
                self.assertIn(want, SE.BACKEND_FLAGS)


class BuildStampTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def test_stamp_changes_with_backend_and_generator(self):
        with mock.patch.object(SE, "out", return_value="deadbeef"):
            base = SE.build_stamp(self.repo, "vulkan", ["-DFOO=1"])
            other_backend = SE.build_stamp(self.repo, "cpu", ["-DFOO=1"])
            with_generator = SE.build_stamp(self.repo, "vulkan", ["-DFOO=1"], generator="Ninja")
        self.assertNotEqual(base, other_backend)
        self.assertNotEqual(base, with_generator)
        self.assertIn("GENERATOR=Ninja", with_generator)
        self.assertNotIn("GENERATOR=", base)


class LockFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_file = Path(self._tmp.name) / "engines.lock"
        self._patch = mock.patch.object(SE, "LOCK_FILE", self.lock_file)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(SE.lock_read(), {})

    def test_write_then_read_roundtrip(self):
        engine_dir = Path(self._tmp.name) / "whisper.cpp"
        engine_dir.mkdir()
        with mock.patch.object(SE, "out", side_effect=["abc123def", "2026-01-01 測試 commit"]):
            SE.lock_write("WHISPER_REF", engine_dir)
        refs = SE.lock_read()
        self.assertEqual(refs["WHISPER_REF"], "abc123def")

    def test_write_preserves_other_keys(self):
        engine_dir = Path(self._tmp.name) / "llama.cpp"
        engine_dir.mkdir()
        with mock.patch.object(SE, "out", side_effect=["sha-whisper", "desc"]):
            SE.lock_write("WHISPER_REF", engine_dir)
        with mock.patch.object(SE, "out", side_effect=["sha-llama", "desc2"]):
            SE.lock_write("LLAMA_REF", engine_dir)
        refs = SE.lock_read()
        self.assertEqual(refs["WHISPER_REF"], "sha-whisper")
        self.assertEqual(refs["LLAMA_REF"], "sha-llama")


class CheckToolsTests(unittest.TestCase):
    def _which(self, present):
        return lambda name: (f"/usr/bin/{name}" if name in present else None)

    def test_linux_missing_vulkan_dev_exits(self):
        with mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(SE.shutil, "which", side_effect=self._which({"git", "cmake", "g++"})), \
                mock.patch.object(SE.subprocess, "run",
                                  return_value=mock.Mock(returncode=1)), \
                self.assertRaises(SystemExit):
            SE.check_tools("vulkan")

    def test_linux_all_present_returns_job_count(self):
        with mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(SE.shutil, "which",
                                  side_effect=self._which({"git", "cmake", "g++", "glslc", "pkg-config"})), \
                mock.patch.object(SE.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(SE.os, "cpu_count", return_value=8):
            self.assertEqual(SE.check_tools("vulkan"), 8)

    def test_windows_missing_vs_build_tools_exits(self):
        with mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(SE.shutil, "which", side_effect=self._which({"git", "cmake"})), \
                mock.patch.object(SE.Path, "exists", return_value=False), \
                self.assertRaises(SystemExit):
            SE.check_tools("cpu")

    def test_windows_vulkan_needs_sdk_or_glslc(self):
        with mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(SE.shutil, "which", side_effect=self._which({"git", "cmake", "cl"})), \
                mock.patch.dict(SE.os.environ, {}, clear=False), \
                self.assertRaises(SystemExit):
            env = dict(SE.os.environ)
            env.pop("VULKAN_SDK", None)
            with mock.patch.dict(SE.os.environ, env, clear=True):
                SE.check_tools("vulkan")

    def test_cuda_needs_nvcc(self):
        with mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(SE.shutil, "which", side_effect=self._which({"git", "cmake", "g++"})), \
                self.assertRaises(SystemExit):
            SE.check_tools("cuda")


class BuildGeneratorPassthroughTests(unittest.TestCase):
    """--generator 應該原封不動傳進 cmake -G，沒指定時完全不加這個參數
    （保留原本讓 cmake 自動判斷 generator 的行為，是相容性修改而非破壞性改動）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / "whisper.cpp"
        self.directory.mkdir()
        (self.directory / "build").mkdir()
        self.calls = []

    def _fake_run(self, cmd, **kw):
        self.calls.append([str(c) for c in cmd])
        return mock.Mock(returncode=0)

    def test_generator_flag_added_when_specified(self):
        with mock.patch.object(SE.subprocess, "run", side_effect=self._fake_run), \
                mock.patch.object(SE, "out", return_value="abc123"), \
                mock.patch.object(SE.shutil, "rmtree"), \
                mock.patch.object(P, "find_engine_bin", return_value=Path("/nonexistent")):
            SE.build(self.directory, "vulkan", [], ["whisper-server"], jobs=2,
                     rebuild=True, generator="Ninja")
        cmake_calls = [c for c in self.calls if c and c[0] == "cmake" and "-S" in c]
        self.assertEqual(len(cmake_calls), 1)
        self.assertIn("-G", cmake_calls[0])
        self.assertEqual(cmake_calls[0][cmake_calls[0].index("-G") + 1], "Ninja")

    def test_no_generator_flag_when_not_specified(self):
        with mock.patch.object(SE.subprocess, "run", side_effect=self._fake_run), \
                mock.patch.object(SE, "out", return_value="abc123"), \
                mock.patch.object(SE.shutil, "rmtree"), \
                mock.patch.object(P, "find_engine_bin", return_value=Path("/nonexistent")):
            SE.build(self.directory, "vulkan", [], ["whisper-server"], jobs=2, rebuild=True)
        cmake_calls = [c for c in self.calls if c and c[0] == "cmake" and "-S" in c]
        self.assertEqual(len(cmake_calls), 1)
        self.assertNotIn("-G", cmake_calls[0])

    def test_skips_rebuild_when_stamp_matches(self):
        with mock.patch.object(SE, "out", return_value="abc123"):
            want = SE.build_stamp(self.directory, "vulkan", ["-DFOO=1"], generator="Ninja")
        (self.directory / "build" / ".lec-build").write_text(want + "\n", encoding="utf-8")
        with mock.patch.object(SE.subprocess, "run", side_effect=self._fake_run), \
                mock.patch.object(SE, "out", return_value="abc123"), \
                mock.patch.object(P, "find_engine_bin", return_value=self.directory), \
                mock.patch.object(Path, "is_file", return_value=True):
            SE.build(self.directory, "vulkan", ["-DFOO=1"], ["whisper-server"], jobs=2,
                     rebuild=False, generator="Ninja")
        self.assertEqual(self.calls, [], "版本、後端與 generator 都沒變時不應該重新編譯")


if __name__ == "__main__":
    unittest.main()
