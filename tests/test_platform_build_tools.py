"""core/platform.py：編譯工具鏈偵測（find_msvc / cxx_compiler / missing_build_tools）。

setup_engines.py（編譯前的前置檢查）與 lec doctor（環境檢查）共用這些函式，
所以這裡直接測 core/platform.py，兩個呼叫端的行為才不會分歧。
全部用 mock 取代 vswhere、pkg-config 與 PATH 查詢，不需要 Windows 或 Vulkan SDK。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import platform as P


def _which(present, prefix="/usr/bin/"):
    """假的 shutil.which：只有 present 裡的名字找得到。"""
    return lambda name: (f"{prefix}{name}" if name in present else None)


class FindMsvcTests(unittest.TestCase):
    """find_msvc()：用 vswhere 判斷「有沒有裝 C++ 工具」，而不是只看資料夾存不存在
    （迴歸：只裝 VS Installer 時資料夾存在，舊版 check_tools 誤判工具齊全，
    cmake 退回 NMake Makefiles 後失敗）。"""

    def test_no_vswhere_returns_none(self):
        with mock.patch.object(P.Path, "exists", return_value=False):
            self.assertIsNone(P.find_msvc())

    def test_vswhere_without_vc_tools_returns_none(self):
        with mock.patch.object(P.Path, "exists", return_value=True), \
                mock.patch.object(P, "_stdout", return_value="[]"):
            self.assertIsNone(P.find_msvc())

    def test_vswhere_with_vc_tools(self):
        raw = '[{"installationVersion": "17.11.35222.181", "installationPath": "C:\\\\VS\\\\2022"}]'
        with mock.patch.object(P.Path, "exists", return_value=True), \
                mock.patch.object(P, "_stdout", return_value=raw) as stdout:
            got = P.find_msvc()
        self.assertEqual(got["version"], "17.11.35222.181")
        self.assertEqual(got["path"], "C:\\VS\\2022")
        # -requires <C++ 工具元件> 是這個判斷的關鍵，不能被拿掉
        self.assertIn(P.VC_TOOLS, [str(a) for a in stdout.call_args[0][0]])

    def test_garbage_output_returns_none(self):
        with mock.patch.object(P.Path, "exists", return_value=True), \
                mock.patch.object(P, "_stdout", return_value="not json"):
            self.assertIsNone(P.find_msvc())

    def test_stdout_ignores_stderr_and_failures(self):
        # vswhere 的 JSON 不能混進 stderr，失敗也只回空字串（呼叫端會當成「沒裝」）
        with mock.patch.object(P.subprocess, "run",
                               return_value=mock.Mock(stdout=' [{"a": 1}] \n', stderr="warning")):
            self.assertEqual(P._stdout(["vswhere"]), '[{"a": 1}]')
        with mock.patch.object(P.subprocess, "run", side_effect=OSError):
            self.assertEqual(P._stdout(["vswhere"]), "")
        with mock.patch.object(P.subprocess, "run",
                               side_effect=P.subprocess.TimeoutExpired(cmd="vswhere", timeout=60)):
            self.assertEqual(P._stdout(["vswhere"]), "")


class BuiltBackendTests(unittest.TestCase):
    """built_backend()：從 setup_engines.py 寫的 build stamp 讀出實際編譯用的後端。
    lec doctor 靠這個決定要檢查哪些編譯工具（CUDA 機器不該被提醒缺 glslc）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.engine_dir = Path(self._tmp.name) / "whisper.cpp"

    def _write(self, text):
        stamp = P.build_stamp_path(self.engine_dir)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(text, encoding="utf-8")
        return stamp

    def test_reads_backend_from_real_stamp_format(self):
        # setup_engines.build_stamp() 的格式：<sha> BACKEND=<後端> [GENERATOR=…] <cmake 參數>
        self._write("da54572229bcf64ba367d96c7ef15770376c4280 BACKEND=cuda "
                    "-DWHISPER_BUILD_TESTS=OFF -DWHISPER_SDL2=OFF\n")
        self.assertEqual(P.built_backend(self.engine_dir), "cuda")

    def test_reads_backend_when_a_generator_was_recorded(self):
        self._write("abc123 BACKEND=vulkan GENERATOR=Ninja -DFOO=1\n")
        self.assertEqual(P.built_backend(self.engine_dir), "vulkan")

    def test_no_stamp_returns_none(self):
        self.assertIsNone(P.built_backend(self.engine_dir))

    def test_stamp_without_backend_returns_none(self):
        self._write("abc123 -DFOO=1\n")
        self.assertIsNone(P.built_backend(self.engine_dir))

    def test_empty_backend_value_returns_none(self):
        self._write("abc123 BACKEND= -DFOO=1\n")
        self.assertIsNone(P.built_backend(self.engine_dir))

    def test_stamp_path_matches_what_setup_engines_writes(self):
        self.assertEqual(P.build_stamp_path("/x/whisper.cpp"),
                         Path("/x/whisper.cpp/build/.lec-build"))


class CxxCompilerTests(unittest.TestCase):
    def test_prefers_cxx_then_clang_then_gcc(self):
        for present, want in (({"c++", "g++"}, "c++"), ({"clang++", "g++"}, "clang++"),
                              ({"g++"}, "g++")):
            with mock.patch.object(P.shutil, "which", side_effect=_which(present)):
                self.assertEqual(P.cxx_compiler(), f"/usr/bin/{want}")

    def test_none_when_no_compiler(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which(set())):
            self.assertIsNone(P.cxx_compiler())


class MissingBuildToolsLinuxTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(P, "NAME", "linux"))
        # VULKAN_SDK 會讓 libvulkan 的判斷直接通過，測試裡先清掉
        env = {k: v for k, v in P.os.environ.items() if k != "VULKAN_SDK"}
        self.enterContext(mock.patch.dict(P.os.environ, env, clear=True))

    def test_all_present_returns_empty(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"g++", "glslc", "pkg-config"})), \
                mock.patch.object(P.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.assertEqual(P.missing_build_tools("vulkan"), [])

    def test_missing_compiler_and_glslc(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"pkg-config"})), \
                mock.patch.object(P.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.assertEqual(P.missing_build_tools("vulkan"), ["C++ 編譯器", "glslc"])

    def test_missing_libvulkan_when_pkg_config_says_no(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"g++", "glslc", "pkg-config"})), \
                mock.patch.object(P.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertEqual(P.missing_build_tools("vulkan"), ["libvulkan-dev"])

    def test_vulkan_sdk_env_replaces_pkg_config(self):
        with mock.patch.dict(P.os.environ, {"VULKAN_SDK": "/opt/vulkan"}), \
                mock.patch.object(P.shutil, "which", side_effect=_which({"g++", "glslc"})):
            self.assertEqual(P.missing_build_tools("vulkan"), [])

    def test_pkg_config_failure_is_not_a_crash(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"g++", "glslc", "pkg-config"})), \
                mock.patch.object(P.subprocess, "run", side_effect=OSError):
            self.assertEqual(P.missing_build_tools("vulkan"), ["libvulkan-dev"])

    def test_cpu_backend_only_needs_a_compiler(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"g++"})):
            self.assertEqual(P.missing_build_tools("cpu"), [])

    def test_cuda_needs_nvcc(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"g++"})):
            self.assertEqual(P.missing_build_tools("cuda"), ["CUDA Toolkit（nvcc）"])
        with mock.patch.object(P.shutil, "which", side_effect=_which({"g++", "nvcc"})):
            self.assertEqual(P.missing_build_tools("cuda"), [])

    def test_auto_backend_resolves_to_platform_default(self):
        # backend 省略時要跟 engine_backend() 一致（Linux = vulkan，所以會檢查 glslc）
        with mock.patch.object(P.shutil, "which", side_effect=_which({"g++"})):
            self.assertIn("glslc", P.missing_build_tools())


class MissingBuildToolsMacosTests(unittest.TestCase):
    def test_metal_only_needs_a_compiler(self):
        with mock.patch.object(P, "NAME", "macos"), \
                mock.patch.object(P.shutil, "which", side_effect=_which({"clang++"})):
            self.assertEqual(P.missing_build_tools(), [])          # auto → metal

    def test_missing_compiler_reported(self):
        with mock.patch.object(P, "NAME", "macos"), \
                mock.patch.object(P.shutil, "which", side_effect=_which(set())):
            self.assertEqual(P.missing_build_tools(), ["C++ 編譯器"])


class MissingBuildToolsWindowsTests(unittest.TestCase):
    MSVC = {"version": "17.11.35222.181", "path": "C:/VS/2022"}

    def setUp(self):
        self.enterContext(mock.patch.object(P, "NAME", "windows"))
        env = {k: v for k, v in P.os.environ.items() if k != "VULKAN_SDK"}
        self.enterContext(mock.patch.dict(P.os.environ, env, clear=True))

    def test_vs_installer_without_cpp_workload_is_missing(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which(set(), "C:/bin/")), \
                mock.patch.object(P, "find_msvc", return_value=None):
            missing = P.missing_build_tools("cpu")
        self.assertEqual(len(missing), 1)
        self.assertIn("使用 C++ 的桌面開發", missing[0])

    def test_probes_vswhere_by_default(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which(set(), "C:/bin/")), \
                mock.patch.object(P, "find_msvc", return_value=self.MSVC) as find:
            self.assertEqual(P.missing_build_tools("cpu"), [])
        find.assert_called_once_with()

    def test_given_msvc_skips_vswhere(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which(set(), "C:/bin/")), \
                mock.patch.object(P, "find_msvc") as find:
            self.assertEqual(P.missing_build_tools("cpu", msvc=self.MSVC), [])
        find.assert_not_called()

    def test_developer_prompt_cl_counts_as_compiler(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"cl"}, "C:/bin/")), \
                mock.patch.object(P, "find_msvc", return_value=None):
            self.assertEqual(P.missing_build_tools("cpu"), [])

    def test_ninja_alone_is_not_a_compiler(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"ninja"}, "C:/bin/")), \
                mock.patch.object(P, "find_msvc", return_value=None):
            self.assertEqual(len(P.missing_build_tools("cpu")), 1)

    def test_vulkan_needs_sdk_or_glslc(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"cl"}, "C:/bin/")):
            self.assertEqual(P.missing_build_tools("vulkan", msvc=self.MSVC), ["Vulkan SDK"])
        with mock.patch.object(P.shutil, "which", side_effect=_which({"cl", "glslc"}, "C:/bin/")):
            self.assertEqual(P.missing_build_tools("vulkan", msvc=self.MSVC), [])
        with mock.patch.dict(P.os.environ, {"VULKAN_SDK": "C:/VulkanSDK"}), \
                mock.patch.object(P.shutil, "which", side_effect=_which({"cl"}, "C:/bin/")):
            self.assertEqual(P.missing_build_tools("vulkan", msvc=self.MSVC), [])

    def test_windows_does_not_look_for_posix_compilers(self):
        # g++ 在 PATH 上也不算：cmake 在 Windows 走 MSVC / Ninja，不吃 g++
        with mock.patch.object(P.shutil, "which", side_effect=_which({"g++"}, "C:/bin/")), \
                mock.patch.object(P, "find_msvc", return_value=None):
            self.assertEqual(len(P.missing_build_tools("cpu")), 1)


if __name__ == "__main__":
    unittest.main()
