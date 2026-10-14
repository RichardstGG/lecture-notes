"""doctor 的工具檢查清單：macOS 缺 swiftc 為失敗，其他平台不受影響。

這裡只驗證清單與嚴重度（Mock test）；`lec doctor` 在真 Mac 上的輸出沒有在這裡驗證。
"""
import unittest

from core import doctor


def _specs(name):
    return {cmd: (need, why) for cmd, need, why in doctor._tool_checks(name)}


class ToolChecksTests(unittest.TestCase):
    def test_macos_requires_swiftc_and_points_to_xcode_select(self):
        need, why = _specs("macos")["swiftc"]
        self.assertTrue(need)
        self.assertIn("xcode-select --install", why)

    def test_other_platforms_have_no_swiftc_check(self):
        for name in ("linux", "windows"):
            with self.subTest(name=name):
                self.assertNotIn("swiftc", _specs(name))

    def test_shared_and_other_platform_severities_are_unchanged(self):
        self.assertEqual(
            {cmd: need for cmd, (need, _) in _specs("linux").items()},
            {"ffmpeg": True, "opencc": False, "git": False, "cmake": False,
             "pactl": False, "systemd-inhibit": False})
        self.assertEqual(
            {cmd: need for cmd, (need, _) in _specs("windows").items()},
            {"ffmpeg": True, "opencc": False, "git": False, "cmake": False})
        mac = {cmd: need for cmd, (need, _) in _specs("macos").items()}
        self.assertEqual(mac, {"ffmpeg": True, "opencc": False, "git": False, "cmake": False,
                               "caffeinate": False, "swiftc": True})


if __name__ == "__main__":
    unittest.main()
