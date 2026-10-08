"""setup.py：互動式安裝的決策邏輯（Mock test，不會編譯、不會安裝任何東西）。

重點：
1. 後端決策表（有沒有 NVIDIA、有沒有 CUDA Toolkit、有沒有 GPU）要選對。
   Vulkan 在 NVIDIA 上也能跑。沒有 Toolkit 時仍可選 CUDA，但只印安裝說明。
2. 缺少的套件預設只會被印出來。`--provision` 在 Windows 才會呼叫 winget，而且不裝編譯器或 CUDA。
3. `--yes` / `--dry-run` 不能停下來等輸入。沒有 `--provision` 時也不會下載模型、裝 UI 或啟動服務。

摘要 API 不在這裡詢問。有 GPU、不是 NVIDIA 的 `run_setup([])`，第一個 `"y"` / `"n"`
是要不要裝 llama.cpp。
"""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
import setup as S
from core import platform as P

LINUX_VULKAN = {
    "platform": "linux", "describe": "Linux 6.12（linux）", "arch": "x86_64",
    "python": "3.13.5", "memory_gb": 32.0, "free_disk_gb": 100.0,
    "gpus": ["Intel(R) Graphics (LNL)"], "nvidia": None, "cuda": None,
    "package_manager": "apt", "default_backend": "vulkan",
}


def env(**changes):
    return {**LINUX_VULKAN, **changes}


def answers(*values):
    """假的 input()：依序回答，用完就當成按 Enter（採用預設值）。"""
    queue = list(values)
    return lambda _prompt="": queue.pop(0) if queue else ""


class ChooseBackendTests(unittest.TestCase):
    def test_nvidia_with_cuda_offers_cuda_and_defaults_to_yes(self):
        with redirect_stdout(io.StringIO()):
            backend, why = S.choose_backend(
                env(nvidia="NVIDIA GeForce RTX 4060", cuda="12.4"), answers(""))
        self.assertEqual(backend, "cuda")
        self.assertIn("CUDA", why)

    def test_nvidia_with_cuda_can_be_declined_for_vulkan(self):
        with redirect_stdout(io.StringIO()):
            backend, _ = S.choose_backend(
                env(nvidia="NVIDIA GeForce RTX 4060", cuda="12.4"), answers("n"))
        self.assertEqual(backend, "vulkan")

    def test_nvidia_without_toolkit_defaults_to_vulkan(self):
        out = io.StringIO()
        with redirect_stdout(out):
            backend, why = S.choose_backend(
                env(nvidia="NVIDIA GeForce RTX 4060", cuda=None), answers())
        self.assertEqual(backend, "vulkan")
        self.assertIn("CUDA Toolkit", out.getvalue())
        self.assertIn("沒有 CUDA Toolkit", why)

    def test_nvidia_without_toolkit_can_choose_cuda(self):
        with redirect_stdout(io.StringIO()):
            backend, why = S.choose_backend(
                env(nvidia="NVIDIA GeForce RTX 4060", cuda=None), answers("y"))
        self.assertEqual(backend, "cuda")
        self.assertIn("還沒有 CUDA Toolkit", why)

    def test_intel_igpu_uses_platform_default_without_asking(self):
        asked = []
        with redirect_stdout(io.StringIO()):
            backend, _ = S.choose_backend(env(), lambda prompt="": asked.append(prompt) or "")
        self.assertEqual(backend, "vulkan")
        self.assertEqual(asked, [])

    def test_macos_always_metal(self):
        backend, why = S.choose_backend(
            env(platform="macos", default_backend="metal", gpus=["Apple GPU（Metal）"]),
            answers())
        self.assertEqual(backend, "metal")
        self.assertIn("Metal", why)

    def test_no_gpu_warns_and_needs_confirmation(self):
        out = io.StringIO()
        with redirect_stdout(out):
            backend, _ = S.choose_backend(env(gpus=[]), answers("y"))
        self.assertEqual(backend, "cpu")
        self.assertIn("非常慢", out.getvalue())

    def test_no_gpu_declined_cancels(self):
        with redirect_stdout(io.StringIO()):
            backend, why = S.choose_backend(env(gpus=[]), answers(""))   # 預設是 n
        self.assertIsNone(backend)
        self.assertIn("取消", why)

    def test_explicit_backend_skips_every_question(self):
        asked = []
        backend, why = S.choose_backend(env(nvidia="RTX 4060", cuda="12.4"),
                                        lambda prompt="": asked.append(prompt) or "",
                                        forced="cpu")
        self.assertEqual((backend, asked), ("cpu", []))
        self.assertIn("--backend", why)


class ModelSuggestionTests(unittest.TestCase):
    def test_small_memory_suggests_the_4b_model(self):
        model, why = S.suggest_model(env(memory_gb=8.0))
        self.assertEqual(model, S.MODEL_4B)
        self.assertIn("8 GB", why)

    def test_enough_memory_keeps_the_default_model(self):
        self.assertEqual(S.suggest_model(env(memory_gb=32.0))[0], S.MODEL_8B)

    def test_unknown_memory_keeps_the_default_model(self):
        self.assertEqual(S.suggest_model(env(memory_gb=None))[0], S.MODEL_8B)

    def test_model_urls_point_at_the_official_qwen_repos(self):
        for model, needle in ((S.MODEL_8B, "Qwen3-8B-GGUF"), (S.MODEL_4B, "Qwen3-4B-GGUF")):
            with self.subTest(model=model):
                url = S.model_url(model)
                self.assertIn(needle, url)
                self.assertTrue(url.startswith("https://huggingface.co/Qwen/"))
                self.assertTrue(url.endswith(S.MODEL_FILES[model]))


class MissingToolsTests(unittest.TestCase):
    def test_runtime_and_build_tools_are_reported_together(self):
        with mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(S.shutil, "which", side_effect=lambda n: None), \
                mock.patch.object(P, "missing_build_tool_keys", return_value=["cxx", "glslc"]):
            missing, blocking = S.missing_tools("vulkan")
        self.assertEqual(missing, ["ffmpeg", "git", "cmake", "opencc", "pactl", "cxx", "glslc"])
        self.assertNotIn("opencc", blocking)     # 只影響繁體用語轉換，不該擋住安裝
        self.assertIn("ffmpeg", blocking)
        self.assertIn("cxx", blocking)

    def test_pactl_is_only_checked_on_linux(self):
        for name in ("macos", "windows"):
            with self.subTest(name=name), \
                    mock.patch.object(P, "NAME", name), \
                    mock.patch.object(S.shutil, "which", side_effect=lambda n: None), \
                    mock.patch.object(P, "missing_build_tool_keys", return_value=[]):
                self.assertNotIn("pactl", S.missing_tools("metal")[0])

    def test_nothing_missing_when_everything_is_installed(self):
        with mock.patch.object(P, "NAME", "linux"), \
                mock.patch.object(S.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"), \
                mock.patch.object(P, "missing_build_tool_keys", return_value=[]):
            self.assertEqual(S.missing_tools("vulkan"), ([], []))


class InstallLinesTests(unittest.TestCase):
    """只印指令：這些函式不得執行任何安裝。"""

    def test_apt_packages_are_collected_into_one_command(self):
        lines = S.install_lines(["ffmpeg", "cmake", "cxx", "glslc"], "apt")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].strip().startswith("sudo apt install"))
        for package in ("ffmpeg", "cmake", "build-essential", "pkg-config", "glslc"):
            self.assertIn(package, lines[0])

    def test_duplicate_packages_appear_once(self):
        line = S.install_lines(["libvulkan", "libvulkan"], "apt")[0]
        self.assertEqual(line.count("libvulkan-dev"), 1)

    def test_brew_and_winget_use_their_own_names(self):
        self.assertIn("brew install ffmpeg", S.install_lines(["ffmpeg"], "brew")[0])
        self.assertIn("Gyan.FFmpeg", S.install_lines(["ffmpeg"], "winget")[0])

    def test_platform_specific_notes_do_not_leak_to_other_platforms(self):
        """迴歸：opencc 的「Windows 沒有現成套件」說明曾經在偵測不到套件管理器的
        Linux 上被印出來。平台專屬說明只能跟著對應的套件管理器出現。"""
        self.assertIn("Windows", S.install_lines(["opencc"], "winget")[0])
        for manager in ("apt", "brew", None, "dnf"):
            with self.subTest(manager=manager):
                lines = S.install_lines(["opencc"], manager)
                self.assertNotIn("Windows", " ".join(lines))
        self.assertIn("xcode-select", S.install_lines(["cxx"], "brew")[0])
        self.assertNotIn("xcode-select", " ".join(S.install_lines(["cxx"], None)))

    def test_tools_without_a_package_fall_back_to_a_note(self):
        for key, needle in (("msvc", "桌面開發"), ("nvcc", "CUDA Toolkit")):
            with self.subTest(key=key):
                lines = S.install_lines([key], "winget")
                self.assertEqual(len(lines), 1)
                self.assertIn(needle, lines[0])
                self.assertNotIn("winget install", lines[0])

    def test_unknown_package_manager_does_not_invent_a_command(self):
        lines = S.install_lines(["ffmpeg", "cxx"], None)
        self.assertTrue(lines)
        for line in lines:
            self.assertNotIn("install", line.split("：")[0])
        self.assertTrue(any("不猜" in line or "xcode-select" in line for line in lines))

    def test_pacman_and_dnf_are_not_given_guessed_package_names(self):
        # 這兩個發行版的套件名稱沒有實際驗證過，不能亂寫
        for manager in ("dnf", "pacman", "zypper"):
            with self.subTest(manager=manager):
                for line in S.install_lines(["glslc", "libvulkan"], manager):
                    self.assertNotIn(f"{manager} install", line)

    def test_nothing_is_executed(self):
        with mock.patch.object(S.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            S.install_lines(["ffmpeg", "msvc"], "apt")
            S.report_missing(["ffmpeg"], ["ffmpeg"], "apt")
        run.assert_not_called()


class BuildCommandTests(unittest.TestCase):
    def test_both_engines_by_default(self):
        command = S.build_command([], "vulkan")
        self.assertEqual(command[-2:], ["--backend", "vulkan"])
        self.assertTrue(command[1].endswith("setup_engines.py"))
        self.assertNotIn("whisper", command)

    def test_whisper_only_is_passed_through(self):
        self.assertIn("whisper", S.build_command(["whisper"], "metal"))


class NonInteractiveTests(unittest.TestCase):
    """--yes / --dry-run 不能停下來等輸入，否則 CI 與 upgrade.py 會卡死。"""

    def _run(self, argv, environment):
        def boom(_prompt=""):
            raise AssertionError("非互動模式不該詢問任何問題")

        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=environment), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            code = S.run_setup(S.parse_args(argv), boom)
        return code, out.getvalue(), run

    def test_dry_run_prints_the_command_without_building(self):
        code, text, run = self._run(["--dry-run", "--yes"], env())
        self.assertEqual(code, 0)
        self.assertIn("setup_engines.py", text)
        self.assertIn("--dry-run", text)
        self.assertIn("依賴自檢", text)
        self.assertNotIn("外部摘要 API", text)
        run.assert_not_called()

    def test_yes_picks_cuda_when_the_machine_has_it(self):
        code, text, run = self._run(["--yes", "--dry-run"],
                                    env(nvidia="RTX 4060", cuda="12.4"))
        self.assertEqual(code, 0)
        self.assertIn("cuda", text)
        run.assert_not_called()

    def test_yes_builds_and_reports_failure_from_setup_engines(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=env()), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S.subprocess, "run",
                                  return_value=mock.Mock(returncode=2)) as run, \
                redirect_stdout(out):
            code = S.run_setup(S.parse_args(["--yes"]), answers())
        self.assertEqual(code, 2)
        run.assert_called_once()
        self.assertIn("setup_engines.py 失敗", out.getvalue())

    def test_blocking_tools_stop_before_building(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=env()), \
                mock.patch.object(S, "missing_tools", return_value=(["ffmpeg"], ["ffmpeg"])), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            code = S.run_setup(S.parse_args(["--yes"]), answers())
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertIn("ffmpeg", out.getvalue())

    def test_low_disk_space_warns_but_yes_continues(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=env(free_disk_gb=5.0)), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as run, \
                redirect_stdout(out):
            code = S.run_setup(S.parse_args(["--yes"]), answers())
        self.assertEqual(code, 0)
        self.assertIn("可用磁碟只有", out.getvalue())
        run.assert_called_once()


class InteractiveFlowTests(unittest.TestCase):
    def test_declining_the_build_cancels_without_running_anything(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=env()), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            # 有 GPU、不是 NVIDIA：要不要裝 llama.cpp → 要不要開始編譯
            code = S.run_setup(S.parse_args([]), answers("y", "n"))
        self.assertEqual(code, 1)
        self.assertIn("已取消", out.getvalue())
        run.assert_not_called()

    def test_whisper_only_answer_reaches_the_build_command(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=env()), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as run, \
                redirect_stdout(out):
            # 有 GPU、不是 NVIDIA：不裝 llama → 開始編譯
            code = S.run_setup(S.parse_args([]), answers("n", "y"))
        self.assertEqual(code, 0)
        self.assertIn("whisper", run.call_args[0][0])
        # 沒裝 llama 就不該叫人去下載 LLM 模型（問題文字裡提到 Qwen3-8B 不算）
        self.assertNotIn(S.MODEL_FILES[S.MODEL_8B], out.getvalue())

    def test_old_python_is_rejected_early(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=env(python="3.9.6")), \
                mock.patch.object(S.sys, "version_info", (3, 9, 6)), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            code = S.run_setup(S.parse_args(["--yes"]), answers())
        self.assertEqual(code, 1)
        self.assertIn("3.11", out.getvalue())
        run.assert_not_called()


def windows_env():
    return env(platform="windows", describe="Windows 11（windows）", arch="AMD64",
               package_manager="winget", default_backend="vulkan",
               gpus=["Intel(R) Iris(R) Xe Graphics"])


class WindowsPrebuiltChoiceTests(unittest.TestCase):
    """沒有 Visual Studio 的 Windows 改下載官方預編譯檔；其他平台維持原本的中止。"""

    def test_yes_without_a_compiler_selects_prebuilt_and_dry_run_does_not_download(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=windows_env()), \
                mock.patch.object(S, "missing_tools",
                                  return_value=(["msvc", "vulkan_sdk"], ["msvc", "vulkan_sdk"])), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            code = S.run_setup(S.parse_args(["--yes", "--dry-run"]), answers())
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("--prebuilt", text)
        self.assertIn("--yes：改用官方預編譯檔", text)
        run.assert_not_called()

    def test_yes_without_a_compiler_invokes_prebuilt(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=windows_env()), \
                mock.patch.object(S, "missing_tools",
                                  return_value=(["msvc", "vulkan_sdk"], ["msvc", "vulkan_sdk"])), \
                mock.patch.object(S.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as run, \
                redirect_stdout(out):
            code = S.run_setup(S.parse_args(["--yes"]), answers())
        self.assertEqual(code, 0)
        command = run.call_args[0][0]
        self.assertIn("--prebuilt", command)
        self.assertEqual(command[command.index("--backend") + 1], "vulkan")

    def test_declining_prebuilt_stops_before_any_install(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=windows_env()), \
                mock.patch.object(S, "missing_tools",
                                  return_value=(["msvc"], ["msvc"])), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            # 有 GPU、不是 NVIDIA：要不要裝 llama.cpp → 要不要改用預編譯檔
            code = S.run_setup(S.parse_args([]), answers("y", "n"))
        self.assertEqual(code, 1)
        self.assertNotIn("接下來會做的事", out.getvalue())
        run.assert_not_called()

    def test_linux_missing_compiler_does_not_switch_to_prebuilt(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=env()), \
                mock.patch.object(S, "missing_tools", return_value=(["cxx"], ["cxx"])), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            code = S.run_setup(S.parse_args(["--yes"]), answers())
        self.assertEqual(code, 1)
        self.assertNotIn("--prebuilt", out.getvalue())
        run.assert_not_called()

    def test_missing_ffmpeg_still_blocks_prebuilt(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=windows_env()), \
                mock.patch.object(S, "missing_tools",
                                  return_value=(["ffmpeg", "msvc"], ["ffmpeg", "msvc"])), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            code = S.run_setup(S.parse_args(["--yes", "--prebuilt"]), answers())
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_explicit_prebuilt_is_windows_vulkan_only(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect", return_value=env()), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            linux = S.run_setup(S.parse_args(["--prebuilt", "--yes"]), answers())
        self.assertEqual(linux, 1)
        with mock.patch.object(S, "detect", return_value=windows_env()), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            cuda = S.run_setup(S.parse_args(["--prebuilt", "--backend", "cuda", "--yes"]), answers())
        self.assertEqual(cuda, 1)
        run.assert_not_called()

    def test_cuda_without_toolkit_stops_before_any_download(self):
        out = io.StringIO()
        with mock.patch.object(S, "detect",
                               return_value=env(nvidia="RTX 4060", cuda=None, gpus=["RTX 4060"])), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            code = S.run_setup(S.parse_args([]), answers("y"))
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("也不會下載", text)
        self.assertIn("CUDA Toolkit", text)
        self.assertNotIn("接下來會做的事", text)
        run.assert_not_called()

    def test_nvidia_without_toolkit_can_still_choose_windows_prebuilt(self):
        out = io.StringIO()
        machine = windows_env() | {"nvidia": "RTX 4060", "cuda": None, "gpus": ["RTX 4060"]}
        with mock.patch.object(S, "detect", return_value=machine), \
                mock.patch.object(S, "missing_tools", return_value=(["msvc"], ["msvc"])), \
                mock.patch.object(S.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as run, \
                redirect_stdout(out):
            # 不選 CUDA → 要裝 llama.cpp → 預編譯用預設（是）
            code = S.run_setup(S.parse_args([]), answers("n", "y"))
        self.assertEqual(code, 0, out.getvalue())
        command = run.call_args[0][0]
        self.assertIn("--prebuilt", command)
        self.assertEqual(command[command.index("--backend") + 1], "vulkan")


class DependencyCheckTests(unittest.TestCase):
    def test_compile_only_gaps_are_warnings_and_nothing_is_installed(self):
        out = io.StringIO()
        with mock.patch.object(S.shutil, "which", return_value=None), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(out):
            S.report_dependencies(env(platform="windows", gpus=["Intel Iris Xe"],
                                      nvidia=None, cuda=None))
        text = out.getvalue()
        self.assertIn("依賴自檢", text)
        self.assertIn("✖ ffmpeg", text)
        self.assertIn("⚠ cmake", text)
        self.assertNotIn("✖ cmake", text)
        self.assertIn("Windows 預編譯檔可略過", text)
        self.assertIn("不是 NVIDIA", text)
        run.assert_not_called()

    def test_nvidia_without_toolkit_is_a_warning_not_a_failure(self):
        out = io.StringIO()
        with mock.patch.object(S.shutil, "which", return_value="C:/bin/tool"), \
                redirect_stdout(out):
            S.report_dependencies(env(nvidia="RTX 4060", cuda=None, gpus=["RTX 4060"]))
        text = out.getvalue()
        self.assertIn("⚠ CUDA Toolkit", text)
        self.assertIn("可改用 Vulkan", text)
        self.assertNotIn("✖ CUDA Toolkit", text)


class WindowsProvisionTests(unittest.TestCase):
    """windows_setup.bat 帶 --provision。這裡不呼叫 winget、不下載、不編譯 UI。"""

    def test_runtime_packages_are_the_ones_a_fresh_windows_needs(self):
        packages = [package for _label, package, _ready in S.WINDOWS_RUNTIME]
        self.assertEqual(packages, [
            "Gyan.FFmpeg", "Git.Git", "OpenJS.NodeJS.LTS", "Microsoft.VCRedist.2015+.x64"])
        self.assertNotIn("LunarG.VulkanSDK", packages)
        self.assertNotIn("Kitware.CMake", packages)

    def test_node_versions_match_the_frontend_engines_field(self):
        self.assertTrue(S.node_is_acceptable((22, 22, 2)))
        self.assertTrue(S.node_is_acceptable((24, 20, 0)))
        self.assertTrue(S.node_is_acceptable((26, 0, 0)))
        for version in (None, (), (22, 21, 9), (23, 11, 0), (24, 14, 0), (18, 20, 0)):
            with self.subTest(version=version):
                self.assertFalse(S.node_is_acceptable(version))

    def test_node_version_parses_stdout(self):
        with mock.patch.object(S.shutil, "which", return_value="node"), \
                mock.patch.object(S.subprocess, "run",
                                  return_value=mock.Mock(stdout="24.20.0\n", returncode=0)):
            self.assertEqual(S.node_version(), (24, 20, 0))

    def test_vc_redist_is_not_required_off_windows(self):
        with mock.patch.object(S.os, "name", "posix"):
            self.assertTrue(S.vc_redist_x64_installed())

    def test_declining_winget_installs_nothing(self):
        with mock.patch.object(S, "missing_runtime_packages",
                               return_value=[("ffmpeg", "Gyan.FFmpeg")]), \
                mock.patch.object(S.shutil, "which", return_value="winget"), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(io.StringIO()):
            ready = S.install_missing_runtime(S.parse_args(["--provision"]), answers("n"))
        self.assertFalse(ready)
        run.assert_not_called()

    def test_yes_installs_the_missing_package_without_asking(self):
        pending = [[("ffmpeg", "Gyan.FFmpeg")], []]

        def boom(_prompt=""):
            raise AssertionError("非互動模式不該詢問")

        with mock.patch.object(S, "missing_runtime_packages", side_effect=lambda: pending.pop(0)), \
                mock.patch.object(S.shutil, "which", return_value="winget"), \
                mock.patch.object(S, "refresh_windows_path"), \
                mock.patch.object(S.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as run, \
                redirect_stdout(io.StringIO()):
            ready = S.install_missing_runtime(S.parse_args(["--provision", "--yes"]), boom)
        self.assertTrue(ready)
        command = run.call_args[0][0]
        self.assertEqual(command[:4], ["winget", "install", "--id", "Gyan.FFmpeg"])
        self.assertIn("--disable-interactivity", command)

    def test_missing_winget_does_not_pretend_success(self):
        with mock.patch.object(S, "missing_runtime_packages",
                               return_value=[("Node.js", "OpenJS.NodeJS.LTS")]), \
                mock.patch.object(S.shutil, "which", return_value=None), \
                mock.patch.object(S.subprocess, "run") as run, \
                redirect_stdout(io.StringIO()):
            ready = S.install_missing_runtime(S.parse_args(["--provision", "--yes"]), answers())
        self.assertFalse(ready)
        run.assert_not_called()

    def test_existing_model_is_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "models" / S.MODEL_FILES[S.MODEL_8B]
            dest.parent.mkdir()
            dest.write_bytes(b"already")
            with mock.patch.object(S, "ROOT", root), \
                    mock.patch.object(S.subprocess, "run") as run, \
                    redirect_stdout(io.StringIO()):
                code = S.ensure_llm_model(S.MODEL_8B)
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_model_download_renames_the_part_only_after_curl_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_curl(command, **_kwargs):
                Path(command[command.index("-o") + 1]).write_bytes(b"gguf")
                return mock.Mock(returncode=0)

            with mock.patch.object(S, "ROOT", root), \
                    mock.patch.object(S.shutil, "which", return_value="curl.exe"), \
                    mock.patch.object(S.subprocess, "run", side_effect=fake_curl) as run, \
                    redirect_stdout(io.StringIO()):
                code = S.ensure_llm_model(S.MODEL_8B)
            dest = root / "models" / S.MODEL_FILES[S.MODEL_8B]
            self.assertEqual(code, 0)
            self.assertEqual(dest.read_bytes(), b"gguf")
            self.assertFalse(dest.with_name(dest.name + ".part").exists())
            self.assertIn("-C", run.call_args[0][0])

    def test_failed_model_download_leaves_no_finished_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(S, "ROOT", root), \
                    mock.patch.object(S.shutil, "which", return_value="curl.exe"), \
                    mock.patch.object(S.subprocess, "run",
                                      return_value=mock.Mock(returncode=1)), \
                    redirect_stdout(io.StringIO()):
                code = S.ensure_llm_model(S.MODEL_4B)
            dest = root / "models" / S.MODEL_FILES[S.MODEL_4B]
            self.assertEqual(code, 1)
            self.assertFalse(dest.exists())

    def test_small_memory_model_is_recorded_only_in_local_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.toml"
            with mock.patch.object(S.C, "LOCAL_FILE", local), \
                    redirect_stdout(io.StringIO()):
                S.remember_smaller_model(S.MODEL_8B)
                self.assertFalse(local.exists())
                S.remember_smaller_model(S.MODEL_4B)
            text = local.read_text(encoding="utf-8")
        self.assertIn('model = "qwen3-4b"', text)
        self.assertNotIn("api_key", text)

    def test_finish_skips_the_gguf_for_whisper_only(self):
        args = S.parse_args(["--provision", "--yes"])
        with mock.patch.object(S, "ensure_llm_model") as model, \
                mock.patch.object(S, "ensure_ui", return_value=0), \
                mock.patch.object(S, "maybe_launch_ui", return_value=0), \
                redirect_stdout(io.StringIO()):
            code = S.finish_usable_install(args, env(), ["whisper"], answers())
        self.assertEqual(code, 0)
        model.assert_not_called()

    def test_finish_downloads_the_local_model_and_installs_the_ui(self):
        args = S.parse_args(["--provision", "--yes"])
        with mock.patch.object(S, "ensure_llm_model", return_value=0) as model, \
                mock.patch.object(S, "ensure_ui", return_value=0) as ui, \
                mock.patch.object(S, "maybe_launch_ui", return_value=0) as launch, \
                redirect_stdout(io.StringIO()):
            code = S.finish_usable_install(args, env(memory_gb=32), [], answers())
        self.assertEqual(code, 0)
        self.assertEqual(model.call_args[0][0], S.MODEL_8B)
        ui.assert_called_once()
        launch.assert_called_once()

    def test_yes_prints_the_ui_command_without_starting_it(self):
        with mock.patch.object(S.subprocess, "run") as run, redirect_stdout(io.StringIO()) as out:
            code = S.maybe_launch_ui(S.parse_args(["--provision", "--yes"]), answers("y"))
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("--start-ui", out.getvalue())

    def test_windows_provision_runs_runtime_engines_then_finish(self):
        order = []

        def runtime(*_args, **_kwargs):
            order.append("runtime")
            return True

        def engines(*_args, **_kwargs):
            order.append("engines")
            return mock.Mock(returncode=0)

        def finish(*_args, **_kwargs):
            order.append("finish")
            return 0

        def boom(_prompt=""):
            raise AssertionError("非互動模式不該詢問")

        with mock.patch.object(S, "detect", return_value=windows_env()), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S, "install_missing_runtime", side_effect=runtime), \
                mock.patch.object(S, "finish_usable_install", side_effect=finish), \
                mock.patch.object(S.subprocess, "run", side_effect=engines), \
                redirect_stdout(io.StringIO()):
            code = S.run_setup(S.parse_args(["--provision", "--yes"]), boom)
        self.assertEqual(code, 0)
        self.assertEqual(order, ["runtime", "engines", "finish"])

    def test_linux_provision_does_not_call_winget(self):
        def engines(*_args, **_kwargs):
            return mock.Mock(returncode=0)

        with mock.patch.object(S, "detect", return_value=env()), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S, "install_missing_runtime",
                                  side_effect=AssertionError("Linux 不該呼叫 winget")), \
                mock.patch.object(S, "finish_usable_install", return_value=0) as finish, \
                mock.patch.object(S.subprocess, "run", side_effect=engines), \
                redirect_stdout(io.StringIO()):
            code = S.run_setup(S.parse_args(["--provision", "--yes"]), answers())
        self.assertEqual(code, 0)
        finish.assert_called_once()

    def test_yes_without_provision_stops_after_the_engines(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("沒有 --provision 不該裝模型或 UI")

        with mock.patch.object(S, "detect", return_value=windows_env()), \
                mock.patch.object(S, "missing_tools", return_value=([], [])), \
                mock.patch.object(S, "install_missing_runtime", side_effect=forbidden), \
                mock.patch.object(S, "finish_usable_install", side_effect=forbidden), \
                mock.patch.object(S.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)), \
                redirect_stdout(io.StringIO()):
            code = S.run_setup(S.parse_args(["--yes"]), answers())
        self.assertEqual(code, 0)


class AskYesNoTests(unittest.TestCase):
    def test_empty_answer_takes_the_default(self):
        self.assertTrue(S.ask_yes_no("?", True, answers("")))
        self.assertFalse(S.ask_yes_no("?", False, answers("")))

    def test_accepts_y_yes_and_chinese(self):
        for answer in ("y", "Y", "yes", "是"):
            with self.subTest(answer=answer):
                self.assertTrue(S.ask_yes_no("?", False, answers(answer)))

    def test_anything_else_is_no(self):
        for answer in ("n", "no", "x", "隨便"):
            with self.subTest(answer=answer):
                self.assertFalse(S.ask_yes_no("?", True, answers(answer)))


if __name__ == "__main__":
    unittest.main()
