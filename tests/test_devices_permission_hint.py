"""core/devices.py：麥克風權限錯誤訊息（macOS TCC 權限、Windows 麥克風權限）。

這部分完全是啟發式（heuristic）：真正的 ffmpeg 錯誤文字沒有實機無法 100% 確認，
這裡只驗證「常見錯誤字樣有被辨識出來、訊息有確實比原始 ffmpeg 輸出更有用」，
不代表已經涵蓋所有 macOS／Windows 實際會遇到的錯誤字樣。
"""
import subprocess
import unittest
from unittest import mock

from . import _pathfix  # noqa: F401
from core import devices as D
from core import platform as P


class PermissionHintTests(unittest.TestCase):
    def test_avfoundation_recognizes_common_denial_text(self):
        hint = D._permission_hint("avfoundation", "Unable to open: not authorized to capture audio")
        self.assertIsNotNone(hint)
        self.assertIn("系統設定", hint)

    def test_dshow_recognizes_access_denied(self):
        hint = D._permission_hint("dshow", "Could not enumerate video devices (Access is Denied)")
        self.assertIsNotNone(hint)

    def test_unrelated_error_returns_none(self):
        self.assertIsNone(D._permission_hint("avfoundation", "No such file or directory"))

    def test_unknown_backend_returns_none(self):
        self.assertIsNone(D._permission_hint("pulse", "not authorized"))


class DshowFalsePositiveTests(unittest.TestCase):
    def test_io_error_from_wrong_device_name_is_not_a_permission_problem(self):
        # 實機：裝置名稱錯誤時 ffmpeg 印的是這段，跟權限無關
        text = ("Could not find audio only device with name [x] among source devices of type audio.\n"
                "Error opening input files: I/O error")
        self.assertIsNone(D._permission_hint("dshow", text))

    def test_no_device_found_gives_clear_message_without_running_ffmpeg(self):
        with mock.patch.object(D.P, "resolve_source", return_value=None), \
                mock.patch.object(D.subprocess, "run") as run:
            mean, msg = D.test_volume("default", backend="dshow")
        self.assertIsNone(mean)
        self.assertIn("找不到任何錄音裝置", msg)
        run.assert_not_called()

class TestVolumeTests(unittest.TestCase):
    def test_avfoundation_timeout_gets_permission_hint(self):
        with mock.patch.object(D.P, "resolve_source", return_value="0"), \
                mock.patch.object(D.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=23)):
            mean, msg = D.test_volume("0", seconds=3, backend="avfoundation")
        self.assertIsNone(mean)
        self.assertIn("系統設定", msg)

    def test_pulse_timeout_returns_plain_message(self):
        with mock.patch.object(D.P, "resolve_source", return_value="default"), \
                mock.patch.object(D.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=23)):
            mean, msg = D.test_volume("default", seconds=3, backend="pulse")
        self.assertIsNone(mean)
        self.assertNotIn("系統設定", msg)

    def test_success_parses_mean_and_peak(self):
        fake = mock.Mock(stderr="[Parsed_volumedetect_0 @ 0x0] mean_volume: -20.0 dB\n"
                                 "[Parsed_volumedetect_0 @ 0x0] max_volume: -5.0 dB\n")
        with mock.patch.object(D.P, "resolve_source", return_value="default"), \
                mock.patch.object(D.subprocess, "run", return_value=fake):
            mean, peak = D.test_volume("default", seconds=3, backend="pulse")
        self.assertEqual(mean, -20.0)
        self.assertEqual(peak, -5.0)

    def test_failure_without_known_pattern_appends_no_hint(self):
        fake = mock.Mock(stderr="Some unrelated ffmpeg error\n")
        with mock.patch.object(D.P, "resolve_source", return_value="default"), \
                mock.patch.object(D.subprocess, "run", return_value=fake):
            mean, msg = D.test_volume("default", seconds=3, backend="pulse")
        self.assertIsNone(mean)
        self.assertEqual(msg, "Some unrelated ffmpeg error")

    def test_failure_with_known_pattern_appends_hint(self):
        fake = mock.Mock(stdout="", stderr="Input/output error\n")
        with mock.patch.object(D.P, "resolve_source", return_value="0"), \
                mock.patch.object(D.P, "_avfoundation_sources", return_value=[]), \
                mock.patch.object(D.subprocess, "run", return_value=fake):
            mean, msg = D.test_volume("0", seconds=3, backend="avfoundation")
        self.assertIsNone(mean)
        self.assertIn("系統設定", msg)


if __name__ == "__main__":
    unittest.main()
