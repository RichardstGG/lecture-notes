"""core/platform.py：環境偵測（NVIDIA、CUDA、GPU 名稱、記憶體、磁碟、套件管理器）。

setup.py 的互動問題全部依賴這些函式，所以三個平台的分支都用 mock 走過一次；
不需要 NVIDIA 顯卡、Mac 或 Windows。偵測失敗時一律回傳 None／空 list，
不能丟例外——setup.py 一開始就會呼叫它們。
"""
import unittest
from unittest import mock

from . import _pathfix  # noqa: F401
from core import platform as P


def _which(present, prefix="/usr/bin/"):
    return lambda name: (f"{prefix}{name}" if name in present else None)


class NvidiaGpuTests(unittest.TestCase):
    def test_no_nvidia_smi_means_no_gpu(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which(set())), \
                mock.patch.object(P, "_stdout") as stdout:
            self.assertIsNone(P.nvidia_gpu())
        stdout.assert_not_called()          # 沒有 nvidia-smi 就不該多跑一個子行程

    def test_reports_first_card_name(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"nvidia-smi"})), \
                mock.patch.object(P, "_stdout",
                                  return_value="NVIDIA GeForce RTX 4060\nNVIDIA T400\n"):
            self.assertEqual(P.nvidia_gpu(), "NVIDIA GeForce RTX 4060")

    def test_driver_present_but_no_output_still_counts_as_nvidia(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"nvidia-smi"})), \
                mock.patch.object(P, "_stdout", return_value=""):
            self.assertIn("NVIDIA", P.nvidia_gpu())


class CudaToolkitTests(unittest.TestCase):
    NVCC = ("nvcc: NVIDIA (R) Cuda compiler driver\n"
            "Cuda compilation tools, release 12.4, V12.4.131\n")

    def test_no_nvcc_returns_none(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which(set())):
            self.assertIsNone(P.cuda_toolkit())

    def test_parses_release_version(self):
        with mock.patch.object(P.shutil, "which", side_effect=_which({"nvcc"})), \
                mock.patch.object(P, "_stdout", return_value=self.NVCC):
            self.assertEqual(P.cuda_toolkit(), "12.4")

    def test_unparsable_output_still_counts_as_installed(self):
        # nvcc 在 PATH 上就代表 Toolkit 有裝，版本認不出來不該變成「沒裝」
        with mock.patch.object(P.shutil, "which", side_effect=_which({"nvcc"})), \
                mock.patch.object(P, "_stdout", return_value="something else"):
            self.assertIsNotNone(P.cuda_toolkit())


class GpuNamesTests(unittest.TestCase):
    SUMMARY = ("GPU0:\n"
               "\tdeviceName         = Intel(R) Graphics (LNL)\n"
               "GPU1:\n"
               "\tdeviceName         = llvmpipe (LLVM 19.1.7, 256 bits)\n")

    def test_vulkaninfo_devices_are_listed_without_llvmpipe(self):
        # llvmpipe 是 CPU 軟體算圖，列出來會讓人以為有 GPU
        with mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(P, "nvidia_gpu", return_value=None), \
                mock.patch.object(P.shutil, "which", side_effect=_which({"vulkaninfo"})), \
                mock.patch.object(P, "_stdout", return_value=self.SUMMARY):
            self.assertEqual(P.gpu_names(), ["Intel(R) Graphics (LNL)"])

    def test_nvidia_comes_first(self):
        with mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(P, "nvidia_gpu", return_value="NVIDIA GeForce RTX 4060"), \
                mock.patch.object(P.shutil, "which", side_effect=_which({"vulkaninfo"})), \
                mock.patch.object(P, "_stdout", return_value=self.SUMMARY):
            self.assertEqual(P.gpu_names(),
                             ["NVIDIA GeForce RTX 4060", "Intel(R) Graphics (LNL)"])

    def test_no_vulkaninfo_returns_empty_list(self):
        with mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(P, "nvidia_gpu", return_value=None), \
                mock.patch.object(P.shutil, "which", side_effect=_which(set())):
            self.assertEqual(P.gpu_names(), [])

    def test_macos_always_reports_a_gpu(self):
        with mock.patch.object(P, "NAME", "macos"), \
                mock.patch.object(P, "nvidia_gpu", return_value=None):
            self.assertEqual(len(P.gpu_names()), 1)


class MemoryAndDiskTests(unittest.TestCase):
    def test_posix_uses_sysconf(self):
        with mock.patch.object(P, "IS_WINDOWS", False), \
                mock.patch.object(P.os, "sysconf",
                                  side_effect=lambda n: {"SC_PHYS_PAGES": 4_000_000,
                                                         "SC_PAGE_SIZE": 4096}[n]):
            self.assertAlmostEqual(P.total_memory_gb(), 16.384, places=3)

    def test_sysconf_failure_returns_none(self):
        for error in (OSError, ValueError):
            with self.subTest(error=error), \
                    mock.patch.object(P, "IS_WINDOWS", False), \
                    mock.patch.object(P.os, "sysconf", side_effect=error):
                self.assertIsNone(P.total_memory_gb())

    def test_free_disk_is_decimal_gb(self):
        with mock.patch.object(P.shutil, "disk_usage",
                               return_value=mock.Mock(free=50_000_000_000)):
            self.assertAlmostEqual(P.free_disk_gb("/x"), 50.0)

    def test_free_disk_on_missing_path_returns_none(self):
        with mock.patch.object(P.shutil, "disk_usage", side_effect=OSError):
            self.assertIsNone(P.free_disk_gb("/no/such/path"))


class PackageManagerTests(unittest.TestCase):
    def test_first_available_manager_per_platform(self):
        for name, present, want in (("linux", {"apt"}, "apt"),
                                    ("linux", {"dnf"}, "dnf"),
                                    ("linux", {"pacman", "dnf"}, "dnf"),   # 依 tuple 的順序
                                    ("macos", {"brew"}, "brew"),
                                    ("windows", {"winget"}, "winget")):
            with self.subTest(name=name, present=present), \
                    mock.patch.object(P, "NAME", name), \
                    mock.patch.object(P.shutil, "which", side_effect=_which(present)):
                self.assertEqual(P.package_manager(), want)

    def test_none_when_nothing_found(self):
        with mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(P.shutil, "which", side_effect=_which(set())):
            self.assertIsNone(P.package_manager())

    def test_windows_does_not_pick_up_apt(self):
        with mock.patch.object(P, "NAME", "windows"), \
                mock.patch.object(P.shutil, "which", side_effect=_which({"apt"})):
            self.assertIsNone(P.package_manager())


class BuildToolKeysTests(unittest.TestCase):
    """missing_build_tool_keys() 與 missing_build_tools() 必須是同一份判斷，
    只差在回傳 key 還是給人看的名稱（setup.py 要 key 才能對照套件名稱）。"""

    def test_keys_and_labels_line_up(self):
        with mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(P.shutil, "which", side_effect=_which(set())), \
                mock.patch.object(P, "_has_vulkan_lib", return_value=False):
            keys = P.missing_build_tool_keys("vulkan")
            labels = P.missing_build_tools("vulkan")
        self.assertEqual(keys, ["cxx", "glslc", "libvulkan"])
        self.assertEqual(labels, [P.BUILD_TOOLS[k] for k in keys])

    def test_every_key_has_a_label(self):
        for backend in ("vulkan", "cuda", "metal", "cpu"):
            for name in ("linux", "macos", "windows"):
                with self.subTest(backend=backend, name=name), \
                        mock.patch.object(P, "NAME", name), \
                        mock.patch.object(P.shutil, "which", side_effect=_which(set())), \
                        mock.patch.object(P, "find_msvc", return_value=None), \
                        mock.patch.object(P, "_has_vulkan_lib", return_value=False):
                    for key in P.missing_build_tool_keys(backend):
                        self.assertIn(key, P.BUILD_TOOLS)


if __name__ == "__main__":
    unittest.main()
