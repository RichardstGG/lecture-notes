"""core/platform.py 的錄音來源解析（純字串處理，不需要真的硬體）。

涵蓋：
- PulseAudio（Linux）：JSON 與 short 格式、缺 pactl
- AVFoundation（macOS）：中文/空白裝置名稱、video/audio 分段
- DirectShow（Windows）：兩種 ffmpeg 輸出格式都要能解析——新版把類型寫在名稱後面
  （"名稱" (audio)，DshowModernFormatTests 用的是 Windows 實機 ffmpeg 9.0.1 的原文），
  舊版用「DirectShow audio devices」區段標題（DshowSourcesTests）。

除了 DshowModernFormatTests 的實機樣本，其餘都是依文件推導的假造輸出。
"""
import unittest
from unittest import mock

from . import _pathfix  # noqa: F401
from core import platform as P


class PulseSourcesTests(unittest.TestCase):
    def test_json_format_filters_and_lowercases_state(self):
        json_out = """[
  {"name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
   "description": "內建麥克風", "state": "RUNNING"},
  {"name": "alsa_input.pci-0000_00_1f.3.analog-stereo.monitor",
   "description": "Monitor of 內建喇叭", "state": "IDLE"}
]"""
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P, "_run", return_value=json_out):
            rows = P._pulse_sources()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "alsa_input.pci-0000_00_1f.3.analog-stereo")
        self.assertEqual(rows[0]["state"], "running")
        self.assertTrue(rows[1]["name"].endswith(".monitor"))

    def test_list_sources_hides_monitor_by_default(self):
        json_out = """[
  {"name": "mic0", "description": "Mic", "state": "RUNNING"},
  {"name": "mic0.monitor", "description": "Monitor", "state": "IDLE"}
]"""
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P, "_run", return_value=json_out):
            rows = P.list_sources(backend="pulse")
            rows_all = P.list_sources(backend="pulse", include_monitors=True)
        self.assertEqual([r["id"] for r in rows], ["mic0"])
        self.assertEqual(len(rows_all), 2)

    def test_short_format_used_when_json_parsing_fails(self):
        calls = []

        def fake_run(cmd, timeout=15):
            calls.append(cmd)
            if "json" in cmd:
                return "pactl: 不支援 -f json（舊版本）"
            return "0\talsa_input.usb-Mic-00.mono-fallback\tmodule-alsa-card.c\ts16le 1ch 16000Hz\tRUNNING\n"

        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P, "_run", side_effect=fake_run):
            rows = P._pulse_sources()
        self.assertEqual(len(calls), 2, "JSON 失敗後應該退回 short 格式再查一次")
        self.assertEqual(rows, [{"id": "alsa_input.usb-Mic-00.mono-fallback",
                                  "name": "alsa_input.usb-Mic-00.mono-fallback",
                                  "description": "", "state": "running"}])

    def test_missing_pactl_returns_none(self):
        with mock.patch.object(P.shutil, "which", return_value=None):
            self.assertIsNone(P._pulse_sources())
            self.assertIsNone(P.list_sources(backend="pulse"))

    def test_default_source_ignores_pactl_errors(self):
        failed = P.subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Connection failure: Connection refused",
        )
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P.subprocess, "run", return_value=failed):
            self.assertIsNone(P.default_source("pulse"))

    def test_default_source_uses_successful_stdout(self):
        success = P.subprocess.CompletedProcess(
            [], 0, stdout="alsa_input.usb-Mic\n", stderr="",
        )
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P.subprocess, "run", return_value=success):
            self.assertEqual(P.default_source("pulse"), "alsa_input.usb-Mic")


class AvfoundationSourcesTests(unittest.TestCase):
    SAMPLE = "\n".join([
        "[AVFoundation indev @ 0x7f8b58704b80] AVFoundation video devices:",
        "[AVFoundation indev @ 0x7f8b58704b80] [0] FaceTime HD Camera",
        "[AVFoundation indev @ 0x7f8b58704b80] AVFoundation audio devices:",
        "[AVFoundation indev @ 0x7f8b58704b80] [0] MacBook Pro 麥克風",
        "[AVFoundation indev @ 0x7f8b58704b80] [1] Background Music (UI Sound)",
        "dummy: Input/output error",
    ])

    def test_parses_only_audio_section(self):
        with mock.patch.object(P, "_run", return_value=self.SAMPLE):
            rows = P._avfoundation_sources()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "MacBook Pro 麥克風")
        self.assertEqual(rows[0]["index"], 0)
        self.assertEqual(rows[1]["name"], "Background Music (UI Sound)")
        self.assertEqual(rows[1]["index"], 1)

    def test_ffmpeg_input_resolves_index_by_name(self):
        with mock.patch.object(P, "_run", return_value=self.SAMPLE):
            args = P.ffmpeg_input("Background Music (UI Sound)", backend="avfoundation")
        self.assertEqual(args, ["-f", "avfoundation", "-thread_queue_size", "512", "-i", ":1"])

    def test_ffmpeg_input_falls_back_to_index_zero(self):
        with mock.patch.object(P, "_run", return_value=self.SAMPLE):
            args = P.ffmpeg_input("找不到的裝置", backend="avfoundation")
        self.assertEqual(args, ["-f", "avfoundation", "-thread_queue_size", "512", "-i", ":0"])


class DshowSourcesTests(unittest.TestCase):
    """舊版 ffmpeg 的格式：先印「DirectShow audio devices」區段標題，裝置只印 "名稱"。
    （新版格式見 DshowModernFormatTests。兩種都要能解析。）
    """

    SAMPLE = "\n".join([
        "[dshow @ 000001d5b504ac40] DirectShow video devices "
        "(some may be both video and audio devices)",
        '[dshow @ 000001d5b504ac40]  "Integrated Webcam"',
        '[dshow @ 000001d5b504ac40]     Alternative name '
        '"@device_pnp_\\\\?\\usb#vid_0000&pid_0000&mi_00#7&abc&0&0000#{video-guid}"',
        "[dshow @ 000001d5b504ac40] DirectShow audio devices",
        '[dshow @ 000001d5b504ac40]  "麥克風陣列 (Realtek(R) Audio)"',
        '[dshow @ 000001d5b504ac40]     Alternative name '
        '"@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\\wave_{11111111-2222-3333-4444-555555555555}"',
        '[dshow @ 000001d5b504ac40]  "麥克風 (USB Audio Device), 立體聲混音"',
        '[dshow @ 000001d5b504ac40]     Alternative name '
        '"@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\\wave_{66666666-7777-8888-9999-000000000000}"',
        '[dshow @ 000001d5b504ac40]  "麥克風陣列 (Realtek(R) Audio)"',
        '[dshow @ 000001d5b504ac40]     Alternative name '
        '"@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\\wave_{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}"',
        "dummy: Immediate exit requested",
    ])

    def test_only_audio_section_is_kept(self):
        with mock.patch.object(P, "_run", return_value=self.SAMPLE):
            rows = P._dshow_sources()
        names = [r["name"] for r in rows]
        self.assertNotIn("Integrated Webcam", names, "video 區段的裝置不該被列為麥克風")
        self.assertEqual(len(rows), 3)

    def test_alternative_name_used_as_stable_id(self):
        with mock.patch.object(P, "_run", return_value=self.SAMPLE):
            rows = P._dshow_sources()
        ids = [r["id"] for r in rows]
        self.assertTrue(all(i.startswith("@device_cm_") for i in ids))
        self.assertEqual(len(set(ids)), 3, "id 用裝置路徑，就算顯示名稱重複也要能區分")

    def test_duplicate_display_names_are_both_listed(self):
        with mock.patch.object(P, "_run", return_value=self.SAMPLE):
            rows = P._dshow_sources()
        dup = [r for r in rows if r["name"] == "麥克風陣列 (Realtek(R) Audio)"]
        self.assertEqual(len(dup), 2)
        self.assertNotEqual(dup[0]["id"], dup[1]["id"])

    def test_name_with_comma_and_chinese_is_preserved(self):
        with mock.patch.object(P, "_run", return_value=self.SAMPLE):
            rows = P._dshow_sources()
        names = [r["name"] for r in rows]
        self.assertIn("麥克風 (USB Audio Device), 立體聲混音", names)

    def test_device_without_alternative_name_falls_back_to_display_name(self):
        sample = "\n".join([
            "[dshow @ 0x1] DirectShow audio devices",
            '[dshow @ 0x1]  "Simple Mic"',
        ])
        with mock.patch.object(P, "_run", return_value=sample):
            rows = P._dshow_sources()
        self.assertEqual(rows, [{"id": "Simple Mic", "name": "Simple Mic",
                                  "description": "", "state": "", "kind": "audio"}])

    def test_ffmpeg_input_uses_audio_prefix(self):
        args = P.ffmpeg_input("@device_cm_{...}", backend="dshow")
        self.assertEqual(args, ["-f", "dshow", "-thread_queue_size", "512",
                                 "-i", "audio=@device_cm_{...}"])

    def test_missing_ffmpeg_returns_none(self):
        with mock.patch.object(P.shutil, "which", return_value=None):
            self.assertIsNone(P.list_sources(backend="dshow"))



class DshowModernFormatTests(unittest.TestCase):
    """新版 ffmpeg（實測 Windows 上 gyan.dev 9.0.1 full build）的格式：沒有區段標題，
    類型直接寫在名稱後面。迴歸：只認區段標題的解析器在這種輸出上一個裝置都列不到，
    `lec devices` 空白、default 解析成 None，錄音測試變成 `audio=None` → I/O error。
    SAMPLE 是實機輸出原文。"""

    SAMPLE = "\n".join([
        '[in#0 @ 00000220aef14080] "Integrated Camera" (video)',
        '[in#0 @ 00000220aef14080]   Alternative name "@device_pnp_\\\\?\\usb#vid_174f&pid_244c&mi_00'
        '#6&29ecd27f&1&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\\global"',
        '[in#0 @ 00000220aef14080] "OBS Virtual Camera" (none)',
        '[in#0 @ 00000220aef14080]   Alternative name "@device_sw_{860BB310-5D01-11D0-BD3B-00A0C911CE86}'
        '\\{A3FCE0F5-3493-419F-958A-ABA1250EC20B}"',
        '[in#0 @ 00000220aef14080] "麥克風排列 (Realtek(R) Audio)" (audio)',
        '[in#0 @ 00000220aef14080]   Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}'
        '\\wave_{DDAAF580-FA22-411C-9EAB-576F318FEDC7}"',
        "Error opening input file dummy.",
    ])

    def test_lists_only_the_audio_device(self):
        rows = P.parse_dshow_devices(self.SAMPLE)
        self.assertEqual([r["name"] for r in rows], ["麥克風排列 (Realtek(R) Audio)"])

    def test_alternative_name_becomes_id(self):
        rows = P.parse_dshow_devices(self.SAMPLE)
        self.assertEqual(rows[0]["id"], "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}"
                                        "\\wave_{DDAAF580-FA22-411C-9EAB-576F318FEDC7}")

    def test_default_resolves_to_the_microphone(self):
        with mock.patch.object(P, "_run", return_value=self.SAMPLE), \
                mock.patch.object(P.shutil, "which", return_value="ffmpeg"):
            self.assertEqual(P.resolve_source("default", backend="dshow"),
                             P.parse_dshow_devices(self.SAMPLE)[0]["id"])

    def test_audio_video_combined_type(self):
        rows = P.parse_dshow_devices('[in#0 @ 0] "Capture Card" (audio, video)')
        self.assertEqual([r["name"] for r in rows], ["Capture Card"])

    def test_alt_name_of_skipped_device_is_not_attached_to_previous(self):
        sample = "\n".join([
            '[in#0 @ 0] "Mic A" (audio)',
            '[in#0 @ 0]   Alternative name "@device_cm_A"',
            '[in#0 @ 0] "Cam" (video)',
            '[in#0 @ 0]   Alternative name "@device_pnp_CAM"',
        ])
        self.assertEqual([r["id"] for r in P.parse_dshow_devices(sample)], ["@device_cm_A"])


class ResolveAndInputTests(unittest.TestCase):
    def test_ffmpeg_input_rejects_unknown_backend(self):
        with self.assertRaises(ValueError):
            P.ffmpeg_input("x", backend="coreaudio")

    def test_pulse_ffmpeg_input_defaults_to_default_source(self):
        self.assertEqual(P.ffmpeg_input(None, backend="pulse"),
                          ["-f", "pulse", "-i", "default"])
        self.assertEqual(P.ffmpeg_input("mysrc", backend="pulse"),
                          ["-f", "pulse", "-i", "mysrc"])

    def test_resolve_source_matches_by_index_or_id(self):
        rows = [{"id": "a", "name": "麥克風A"}, {"id": "b", "name": "麥克風B"}]
        with mock.patch.object(P, "list_sources", return_value=rows):
            self.assertEqual(P.resolve_source("1", backend="dshow"), "b")
            self.assertEqual(P.resolve_source("麥克風A", backend="dshow"), "a")
            self.assertEqual(P.resolve_source("不存在", backend="dshow"), "不存在",
                              "找不到就照原樣交給 ffmpeg，而不是報錯")


if __name__ == "__main__":
    unittest.main()
