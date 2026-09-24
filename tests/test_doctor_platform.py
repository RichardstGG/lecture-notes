"""lec doctor 的平台分支與「編譯工具」項目（Mock test，不需要 Mac 或 Windows 實機）。

doctor.run() 裡有幾處會因作業系統而不同：「作業系統」那行的實測／實驗中措辭、
要檢查哪些外部指令（pactl / systemd-inhibit / caffeinate）、錄音後端，以及編譯工具鏈。
macOS 與 Windows 沒有實機可以跑，所以至少用 mock 把三個平台的分支都走過一次，
避免跨平台重構時在這裡靜默壞掉。

只 patch core/platform.py 的 NAME：行程相關的 IS_WINDOWS 是 import 時算好的常數，
不會被動到，所以不會誤觸 ctypes.windll 這種真的只能在 Windows 執行的路徑。
"""
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import doctor as DOC
from core import platform as P


def run_doctor(name, *, missing_tools=(), msvc=None, which=None, stamp_backend=None):
    """在假裝的平台上跑一次 doctor.run()；回傳 ({項目名稱: 項目}, {mock 名稱: mock})。

    stamp_backend：在 whisper 引擎資料夾寫一份 build stamp，模擬「已經用這個後端編過」。
    """
    with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as stack:
        q = Path(d).as_posix()
        sets = [f"paths.whisper_dir='{q}/w'", f"paths.llama_dir='{q}/l'",
                f"paths.output_root='{q}/out'", f"paths.state_dir='{q}/state'",
                # 隨便挑不會有人用的 port，避免撞到本機正在跑的 server
                "whisper.port=1", "llm.port=2"]
        if stamp_backend:
            stamp = P.build_stamp_path(Path(d) / "w")
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(f"deadbeef BACKEND={stamp_backend} -DWHISPER_SDL2=OFF\n",
                             encoding="utf-8")
        mocks = {
            "missing_build_tools": stack.enter_context(
                mock.patch.object(P, "missing_build_tools", return_value=list(missing_tools))),
            "find_msvc": stack.enter_context(mock.patch.object(P, "find_msvc", return_value=msvc)),
        }
        stack.enter_context(mock.patch.object(P, "NAME", name))
        stack.enter_context(mock.patch.object(DOC.devices, "list_sources", return_value=[]))
        stack.enter_context(mock.patch.object(DOC.devices, "default_source", return_value=None))
        if which is not None:
            stack.enter_context(mock.patch.object(DOC.shutil, "which", side_effect=which))
        items = DOC.run(None, sets)
    return {it["name"]: it for it in items}, mocks


class DoctorPlatformBranchTests(unittest.TestCase):
    """三個平台各跑一次 doctor.run()，檢查平台相關的項目。"""

    def test_linux_is_the_only_tested_platform(self):
        item = run_doctor("linux")[0]["作業系統"]
        self.assertEqual(item["status"], DOC.OK)
        self.assertIn("已實測", item["detail"])

    def test_macos_and_windows_stay_experimental(self):
        for name in ("macos", "windows"):
            with self.subTest(name=name):
                item = run_doctor(name)[0]["作業系統"]
                self.assertEqual(item["status"], DOC.WARN)
                self.assertIn("實驗中", item["detail"])

    def test_audio_backend_per_platform(self):
        for name, backend in (("linux", "pulse"), ("macos", "avfoundation"),
                              ("windows", "dshow")):
            with self.subTest(name=name):
                self.assertEqual(run_doctor(name)[0]["錄音後端"]["detail"], backend)

    def test_platform_specific_tools_are_checked(self):
        linux = run_doctor("linux")[0]
        self.assertIn("pactl", linux)
        self.assertIn("systemd-inhibit", linux)
        self.assertNotIn("caffeinate", linux)

        macos = run_doctor("macos")[0]
        self.assertIn("caffeinate", macos)
        self.assertNotIn("pactl", macos)
        self.assertNotIn("systemd-inhibit", macos)

        windows = run_doctor("windows")[0]
        for tool in ("pactl", "systemd-inhibit", "caffeinate"):
            # Windows 用 SetThreadExecutionState 阻止休眠、用 ffmpeg 列裝置，沒有額外指令要裝
            self.assertNotIn(tool, windows)

    def test_shared_tools_are_checked_everywhere(self):
        for name in ("linux", "macos", "windows"):
            with self.subTest(name=name):
                items = run_doctor(name)[0]
                for tool in ("ffmpeg", "opencc", "git", "cmake"):
                    self.assertIn(tool, items)

    def test_missing_ffmpeg_is_a_failure_on_every_platform(self):
        def which(cmd):
            return None if cmd == "ffmpeg" else f"/usr/bin/{cmd}"

        for name in ("linux", "macos", "windows"):
            with self.subTest(name=name):
                items = run_doctor(name, which=which)[0]
                self.assertEqual(items["ffmpeg"]["status"], DOC.FAIL)   # 沒有 ffmpeg 就不能錄音
                self.assertEqual(items["opencc"]["status"], DOC.OK)


class DoctorBuildToolsItemTests(unittest.TestCase):
    """「編譯工具」項目：判斷來自 P.missing_build_tools()（與 setup_engines.py 同一份），
    缺工具只算警告——已經編好引擎或改用官方預編譯檔的人不需要編譯環境。"""

    def test_complete_toolchain_is_ok_and_names_the_backend(self):
        item = run_doctor("linux")[0]["編譯工具"]
        self.assertEqual(item["status"], DOC.OK)
        self.assertIn("齊全", item["detail"])
        self.assertIn("vulkan 後端", item["detail"])
        self.assertIn("本平台預設", item["detail"])   # 還沒編過，只能用平台預設猜

    def test_metal_backend_shown_on_macos(self):
        self.assertIn("metal 後端", run_doctor("macos")[0]["編譯工具"]["detail"])

    def test_backend_falls_back_to_platform_default_without_a_build_stamp(self):
        _, mocks = run_doctor("windows")
        mocks["missing_build_tools"].assert_called_once_with("vulkan", msvc=None)

    def test_build_stamp_backend_wins_over_platform_default(self):
        """用 --backend cuda 編過的機器不該被提醒缺 Vulkan 的 glslc。"""
        items, mocks = run_doctor("windows", stamp_backend="cuda")
        mocks["missing_build_tools"].assert_called_once_with("cuda", msvc=None)
        self.assertIn("cuda 後端", items["編譯工具"]["detail"])
        self.assertIn("已編譯的後端", items["編譯工具"]["detail"])

    def test_missing_tools_are_a_warning_not_a_failure(self):
        item = run_doctor("linux", missing_tools=["glslc", "libvulkan-dev"])[0]["編譯工具"]
        self.assertEqual(item["status"], DOC.WARN)   # FAIL 會讓 lec doctor 回傳 exit code 1
        self.assertIn("glslc", item["detail"])
        self.assertIn("libvulkan-dev", item["detail"])
        self.assertIn("預編譯檔", item["detail"])    # 給不想裝編譯環境的人一條出路

    def test_windows_shows_the_visual_studio_version_found_by_vswhere(self):
        item = run_doctor("windows", msvc={"version": "17.11.35222.181",
                                           "path": "C:/VS"})[0]["編譯工具"]
        self.assertEqual(item["status"], DOC.OK)
        self.assertIn("Visual Studio 17.11.35222.181", item["detail"])

    def test_windows_without_msvc_reports_what_is_missing(self):
        item = run_doctor("windows", missing_tools=["Visual Studio 的「使用 C++ 的桌面開發」"
                                                    "工作負載（MSVC 編譯器）"])[0]["編譯工具"]
        self.assertEqual(item["status"], DOC.WARN)
        self.assertIn("使用 C++ 的桌面開發", item["detail"])

    def test_vswhere_is_only_called_on_windows(self):
        for name, expected in (("linux", False), ("macos", False), ("windows", True)):
            with self.subTest(name=name):
                _, mocks = run_doctor(name)
                self.assertEqual(mocks["find_msvc"].called, expected)


if __name__ == "__main__":
    unittest.main()
