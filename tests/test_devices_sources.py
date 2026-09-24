"""core/devices.py：來源解析（resolve）與音量判斷（judge_volume）、列不出裝置時的提示。

`devices.resolve()` 與 `platform.resolve_source()` 對「找不到這個來源」刻意給出不同答案，
這裡把兩邊的行為都釘住，免得日後有人把其中一個「順手改成跟另一個一樣」：
`lec devices --test/--save` 要當場報錯，執行期則把原字串交給 ffmpeg 決定。
"""
import unittest
from unittest import mock

from . import _pathfix  # noqa: F401
from core import devices as D
from core import platform as P

SOURCES = [
    {"id": "alsa_input.pci-0000_00_1f.3.analog-stereo", "name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
     "description": "內建麥克風", "state": "idle"},
    {"id": "alsa_input.usb-Blue_Yeti", "name": "alsa_input.usb-Blue_Yeti",
     "description": "USB 麥克風", "state": "suspended"},
]


class ResolveTests(unittest.TestCase):
    def test_matches_by_index(self):
        self.assertEqual(D.resolve("1", SOURCES), SOURCES[1]["id"])
        self.assertEqual(D.resolve(0, SOURCES), SOURCES[0]["id"])

    def test_matches_by_id_or_name(self):
        self.assertEqual(D.resolve("alsa_input.usb-Blue_Yeti", SOURCES), SOURCES[1]["id"])

    def test_unknown_choice_returns_none(self):
        # lec devices --test/--save 靠這個 None 當場報「找不到來源」
        self.assertIsNone(D.resolve("不存在的麥克風", SOURCES))

    def test_given_sources_are_used_without_listing_again(self):
        with mock.patch.object(D.P, "list_sources") as list_sources:
            D.resolve("0", SOURCES)
        list_sources.assert_not_called()

    def test_lists_sources_when_not_given(self):
        with mock.patch.object(D.P, "list_sources", return_value=SOURCES) as list_sources:
            self.assertEqual(D.resolve("1"), SOURCES[1]["id"])
        list_sources.assert_called_once()

    def test_unlistable_devices_do_not_crash(self):
        with mock.patch.object(D.P, "list_sources", return_value=None):
            self.assertIsNone(D.resolve("0"))

    def test_runtime_resolution_passes_unknown_names_to_ffmpeg(self):
        """對照組：執行期的 resolve_source() 找不到不會回 None，而是照原樣交給 ffmpeg
        （設定檔裡可能寫著現在沒插上的裝置，該不該失敗由 ffmpeg 決定）。"""
        with mock.patch.object(P, "list_sources", return_value=SOURCES):
            self.assertEqual(P.resolve_source("不存在的麥克風", backend="pulse"),
                             "不存在的麥克風")
            self.assertIsNone(D.resolve("不存在的麥克風", SOURCES))


class JudgeVolumeTests(unittest.TestCase):
    """判斷結果會直接變成 lec devices --test / lec doctor --mic 的 ✔⚠✖。"""

    def test_normal_level_is_ok(self):
        self.assertEqual(D.judge_volume(-20.0)[0], "✔")

    def test_silence_is_a_failure(self):
        for mean in (None, float("-inf"), -61.0):
            with self.subTest(mean=mean):
                mark, msg = D.judge_volume(mean)
                self.assertEqual(mark, "✖")
                self.assertIn("沒有聲音", msg)

    def test_quiet_is_a_warning(self):
        mark, msg = D.judge_volume(-50.0)
        self.assertEqual(mark, "⚠")
        self.assertIn("音量很小", msg)

    def test_too_loud_is_a_warning(self):
        mark, msg = D.judge_volume(-5.0)
        self.assertEqual(mark, "⚠")
        self.assertIn("破音", msg)

    def test_threshold_boundaries(self):
        # 邊界值釘住：-60 還算「很小」而不是「沒聲音」，-45 與 -10 都落在正常範圍
        self.assertEqual(D.judge_volume(-60.0)[0], "⚠")
        self.assertEqual(D.judge_volume(-45.0)[0], "✔")
        self.assertEqual(D.judge_volume(-10.0)[0], "✔")


class HintTests(unittest.TestCase):
    """列不出裝置時的提示要對應目前的錄音後端。"""

    def test_hint_per_backend(self):
        for backend, needle in (("pulse", "pactl"), ("avfoundation", "brew install ffmpeg"),
                                ("dshow", "PATH")):
            with self.subTest(backend=backend), \
                    mock.patch.object(D.P, "audio_backend", return_value=backend):
                self.assertIn(needle, D.hint())

    def test_unknown_backend_gives_empty_hint(self):
        with mock.patch.object(D.P, "audio_backend", return_value="jack"):
            self.assertEqual(D.hint(), "")


if __name__ == "__main__":
    unittest.main()
