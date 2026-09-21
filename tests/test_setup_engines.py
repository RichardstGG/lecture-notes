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



class FindMsvcTests(unittest.TestCase):
    """find_msvc()：用 vswhere 判斷「有沒有裝 C++ 工具」，而不是只看資料夾存不存在
    （迴歸：只裝 VS Installer 時資料夾存在，舊版 check_tools 誤判工具齊全，
    cmake 退回 NMake Makefiles 後失敗）。"""

    def test_no_vswhere_returns_none(self):
        with mock.patch.object(SE.Path, "exists", return_value=False):
            self.assertIsNone(SE.find_msvc())

    def test_vswhere_without_vc_tools_returns_none(self):
        with mock.patch.object(SE.Path, "exists", return_value=True), \
                mock.patch.object(SE, "out", return_value="[]"):
            self.assertIsNone(SE.find_msvc())

    def test_vswhere_with_vc_tools(self):
        raw = '[{"installationVersion": "17.11.35222.181", "installationPath": "C:\\\\VS\\\\2022"}]'
        with mock.patch.object(SE.Path, "exists", return_value=True), \
                mock.patch.object(SE, "out", return_value=raw) as out:
            got = SE.find_msvc()
        self.assertEqual(got["version"], "17.11.35222.181")
        self.assertIn(SE.VC_TOOLS, [str(a) for a in out.call_args[0][0]])

    def test_garbage_output_returns_none(self):
        with mock.patch.object(SE.Path, "exists", return_value=True), \
                mock.patch.object(SE, "out", return_value="not json"):
            self.assertIsNone(SE.find_msvc())


class PickGeneratorTests(unittest.TestCase):
    MSVC_2022 = {"version": "17.11.35222.181", "path": "C:/VS/2022"}
    MSVC_2026 = {"version": "18.0.11111.1", "path": "C:/VS/2026"}

    def _which(self, present):
        return lambda name: (f"C:/bin/{name}.exe" if name in present else None)

    def test_explicit_generator_wins(self):
        with mock.patch.object(P, "NAME", "windows"):
            self.assertEqual(SE.pick_generator("Ninja", self.MSVC_2022), "Ninja")

    def test_non_windows_leaves_it_to_cmake(self):
        with mock.patch.object(P, "NAME", "linux"):
            self.assertIsNone(SE.pick_generator(None, None))

    def test_developer_prompt_leaves_it_to_cmake(self):
        with mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(SE.shutil, "which", side_effect=self._which({"cl"})):
            self.assertIsNone(SE.pick_generator(None, self.MSVC_2022))

    def test_plain_powershell_picks_matching_vs_generator(self):
        help_text = "Generators\n* Visual Studio 17 2022 = Generates Visual Studio 2022 project files."
        with mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(SE.shutil, "which", side_effect=self._which(set())), \
                mock.patch.object(SE, "out", return_value=help_text):
            self.assertEqual(SE.pick_generator(None, self.MSVC_2022), "Visual Studio 17 2022")

    def test_cmake_too_old_for_installed_vs_exits_with_clear_message(self):
        help_text = "Generators\n* Visual Studio 17 2022 = ..."   # 舊版 cmake 沒有 VS 18 2026
        with mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(SE.shutil, "which", side_effect=self._which(set())), \
                mock.patch.object(SE, "out", return_value=help_text), \
                self.assertRaises(SystemExit):
            SE.pick_generator(None, self.MSVC_2026)

    def test_unknown_future_vs_falls_back_to_cmake(self):
        with mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(SE.shutil, "which", side_effect=self._which(set())):
            self.assertIsNone(SE.pick_generator(None, {"version": "99.0", "path": ""}))


class CheckToolsMsvcTests(unittest.TestCase):
    def _which(self, present):
        return lambda name: (f"C:/bin/{name}.exe" if name in present else None)

    def test_vs_folder_alone_is_not_enough(self):
        # VS Installer 的資料夾存在、但 vswhere 找不到 C++ 工具 → 應該報缺少，而不是「工具齊全」
        with mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(SE.shutil, "which", side_effect=self._which({"git", "cmake", "nvcc"})), \
                self.assertRaises(SystemExit):
            SE.check_tools("cuda", msvc=None)

    def test_ninja_alone_is_not_a_compiler(self):
        with mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(SE.shutil, "which",
                                  side_effect=self._which({"git", "cmake", "nvcc", "ninja"})), \
                self.assertRaises(SystemExit):
            SE.check_tools("cuda", msvc=None)

    def test_vswhere_found_msvc_passes(self):
        with mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(SE.shutil, "which", side_effect=self._which({"git", "cmake", "nvcc"})), \
                mock.patch.object(SE.os, "cpu_count", return_value=16):
            self.assertEqual(SE.check_tools("cuda", msvc={"version": "17.11", "path": "C:/VS"}), 16)


class ConfigureHintTests(unittest.TestCase):
    def test_windows_configure_failure_prints_hint(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(SE, "out", return_value="abc123"), \
                mock.patch.object(SE.shutil, "rmtree"), \
                mock.patch.object(P, "find_engine_bin", return_value=Path("/nonexistent")), \
                mock.patch.object(SE.subprocess, "run", return_value=mock.Mock(returncode=1)), \
                mock.patch("sys.stderr") as err, \
                self.assertRaises(SystemExit):
            SE.build(Path(d), "cuda", ["-DFOO=1"], ["whisper-server"], jobs=2, rebuild=True)
        printed = "".join(str(c.args[0]) for c in err.write.call_args_list)
        self.assertIn("使用 C++ 的桌面開發", printed)

    def test_effective_generator_used_but_not_stamped(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "build").mkdir()
            with mock.patch.object(SE, "out", return_value="abc123"), \
                    mock.patch.object(SE.shutil, "rmtree"), \
                    mock.patch.object(P, "find_engine_bin", return_value=Path("/nonexistent")), \
                    mock.patch.object(SE.subprocess, "run",
                                      side_effect=lambda c, **k: calls.append([str(x) for x in c])
                                      or mock.Mock(returncode=0)):
                SE.build(Path(d), "cuda", ["-DFOO=1"], ["whisper-server"], jobs=2, rebuild=True,
                         generator=None, cmake_generator="Visual Studio 17 2022")
            stamp = (Path(d) / "build" / ".lec-build").read_text(encoding="utf-8")
        configure = [c for c in calls if "-S" in c][0]
        self.assertEqual(configure[configure.index("-G") + 1], "Visual Studio 17 2022")
        self.assertNotIn("GENERATOR=", stamp)


if __name__ == "__main__":
    unittest.main()
