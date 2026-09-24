"""setup.py：互動式安裝的決策邏輯（Mock test，不會編譯、不會安裝任何東西）。

重點有三個：
1. 後端決策表（有沒有 NVIDIA、有沒有 CUDA Toolkit、有沒有 GPU）要選對，
   而且 Vulkan 在 NVIDIA 上也能跑——CUDA 是「要不要更快」的選擇題，不是必要條件。
2. 缺少的套件只會被**印出來**，絕對不會有人幫使用者執行安裝（不碰 sudo / winget）。
3. `--yes` / `--dry-run` 這些非互動路徑不能停下來等輸入（CI 與 upgrade.py 會用到）。
"""
import io
import unittest
from contextlib import redirect_stdout
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

    def test_nvidia_without_toolkit_does_not_offer_cuda(self):
        out = io.StringIO()
        with redirect_stdout(out):
            backend, why = S.choose_backend(
                env(nvidia="NVIDIA GeForce RTX 4060", cuda=None), answers())
        self.assertEqual(backend, "vulkan")      # 不能選 cuda：沒有 nvcc 編不起來
        self.assertIn("CUDA Toolkit", out.getvalue())
        self.assertIn("沒有 CUDA Toolkit", why)

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
            # 問題順序：要不要裝 llama.cpp → 要不要開始編譯
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
