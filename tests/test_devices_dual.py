"""devices.plan_dual：把「哪個輸出裝置＋哪支麥克風」解析成實際裝置並預檢（mock，不碰真實裝置）。"""
import unittest
from unittest import mock

from . import _pathfix  # noqa: F401
from core import devices
from core import doctor
from core import platform as P

SINKS = [{"id": "spk", "name": "spk", "description": "喇叭", "state": "running", "port": "analog-output-speaker"},
         {"id": "bt", "name": "bt", "description": "藍牙耳機", "state": "idle", "port": "headset-output"}]
SOURCES = [{"id": "mic_usb", "name": "mic_usb", "description": "USB 麥克風", "state": "idle"},
           {"id": "mic_int", "name": "mic_int", "description": "內建麥克風", "state": "idle"}]


def patched(sinks=SINKS, sources=SOURCES, default_sink="bt", default_source="mic_int",
            activity=None):
    return _Patch(sinks, sources, default_sink, default_source, activity)


class _Patch:
    def __init__(self, sinks, sources, default_sink, default_source, activity):
        self.patches = [
            mock.patch.object(P, "list_sinks", return_value=sinks),
            mock.patch.object(P, "list_sources", return_value=sources),
            mock.patch.object(P, "default_sink", return_value=default_sink),
            mock.patch.object(P, "default_source", return_value=default_source),
            mock.patch.object(P, "sink_activity", return_value=activity)]

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *a):
        for p in self.patches:
            p.stop()


class PlanDualTests(unittest.TestCase):
    def plan(self, **kw):
        return devices.plan_dual(backend="pulse", **kw)

    def test_default_output_and_mic_are_resolved_and_pinned(self):
        with patched():
            plan = self.plan()
        self.assertTrue(plan["ok"])
        system, mic = plan["sources"]
        self.assertEqual((system["role"], system["device"], system["kind"]),
                         ("system", "bt.monitor", "monitor"))
        self.assertEqual((mic["role"], mic["device"], mic["kind"]), ("mic", "mic_int", "input"))
        self.assertNotIn("default", (system["device"], mic["device"]),
                         "default 會在錄音中途改變，必須在這裡固定成實際裝置")
        self.assertEqual(plan["resolved"], {"system_output": "bt", "mic": "mic_int"})
        self.assertEqual(plan["warnings"], [])

    def test_explicit_choices_by_id_name_or_index(self):
        with patched():
            plan = self.plan(system_output="spk", mic="mic_usb")
        self.assertEqual([s["device"] for s in plan["sources"]], ["spk.monitor", "mic_usb"])
        with patched():
            plan = self.plan(system_output="1", mic="mic_usb")
        self.assertEqual(plan["sources"][0]["device"], "bt.monitor")

    def test_unknown_devices_are_errors_with_stable_codes(self):
        with patched():
            self.assertEqual(self.plan(system_output="nope")["errors"][0]["code"], "output_not_found")
            self.assertEqual(self.plan(mic="nope")["errors"][0]["code"], "mic_not_found")
        for plan in (self.plan(system_output="nope"), self.plan(mic="nope")):
            self.assertFalse(plan["ok"])
            self.assertEqual(plan["sources"], [])

    def test_mic_must_not_be_a_monitor(self):
        with patched(sources=SOURCES + [{"id": "spk.monitor", "name": "spk.monitor",
                                         "description": "Monitor", "state": "idle"}]):
            plan = self.plan(system_output="bt", mic="spk.monitor")
        self.assertEqual([e["code"] for e in plan["errors"]], ["mic_is_monitor"])

    def test_same_source_twice_is_refused(self):
        src = SOURCES + [{"id": "bt.monitor", "name": "bt.monitor", "description": "", "state": ""}]
        with patched(sources=src):
            plan = self.plan(system_output="bt", mic="bt.monitor")
        self.assertIn("same_device", [e["code"] for e in plan["errors"]])

    def test_no_defaults_is_an_error_not_a_guess(self):
        with patched(default_sink=None):
            self.assertEqual(self.plan()["errors"][0]["code"], "no_default_output")
        with patched(default_source=None):
            self.assertEqual(self.plan()["errors"][0]["code"], "no_default_mic")

    def test_speaker_output_warns_about_echo(self):
        with patched():
            plan = self.plan(system_output="spk")
        self.assertTrue(plan["ok"], "只是警告，不擋錄音")
        self.assertEqual([w["code"] for w in plan["warnings"]], ["output_may_echo"])

    def test_warns_when_playback_is_on_another_output(self):
        with patched(activity={"spk": 2, "bt": 0}):
            plan = self.plan(system_output="bt")
        self.assertTrue(plan["ok"])
        self.assertEqual([w["code"] for w in plan["warnings"]], ["no_playback_on_output"])
        self.assertIn("喇叭", plan["warnings"][0]["message"])

    def test_no_warning_when_nothing_is_playing_anywhere(self):
        with patched(activity={"spk": 0, "bt": 0}):
            self.assertEqual(self.plan(system_output="bt")["warnings"], [])

    def test_tools_missing_is_reported(self):
        with patched(sinks=None):
            self.assertEqual(self.plan()["errors"][0]["code"], "no_audio_tools")

    def test_other_platforms_are_refused_not_silently_attempted(self):
        for backend in ("avfoundation", "dshow"):
            plan = devices.plan_dual(backend=backend)
            self.assertFalse(plan["ok"])
            self.assertEqual(plan["errors"][0]["code"], "unsupported_platform")
            self.assertIn("實機驗證", plan["errors"][0]["message"])


class DoctorItemTests(unittest.TestCase):
    def test_other_platforms_warn_not_fail(self):
        status, name, detail = doctor.dual_capture_item("avfoundation")
        self.assertEqual((status, name), (doctor.WARN, "雙來源錄音"))
        self.assertIn("尚未支援", detail)

    def test_linux_ready_with_headphones_is_ok_and_still_labelled_experimental(self):
        with patched(), mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P, "pulse_server", return_value={"flavor": "pipewire"}):
            status, _, detail = doctor.dual_capture_item("pulse")
        self.assertEqual(status, doctor.OK)
        self.assertIn("PipeWire", detail)
        self.assertIn("實驗中", detail)
        self.assertIn("藍牙耳機", detail)

    def test_linux_speaker_output_is_a_warning(self):
        with patched(default_sink="spk"), mock.patch.object(P.shutil, "which", return_value="/x"), \
                mock.patch.object(P, "pulse_server", return_value={"flavor": "pulseaudio"}):
            status, _, detail = doctor.dual_capture_item("pulse")
        self.assertEqual(status, doctor.WARN)
        self.assertIn("戴耳機", detail)

    def test_missing_pactl_is_a_warning_with_the_install_hint(self):
        with mock.patch.object(P.shutil, "which", return_value=None):
            status, _, detail = doctor.dual_capture_item("pulse")
        self.assertEqual(status, doctor.WARN)
        self.assertIn("pactl", detail)

    def test_a_crashing_probe_does_not_break_doctor(self):
        with mock.patch.object(P.shutil, "which", return_value="/x"), \
                mock.patch.object(devices, "plan_dual", side_effect=RuntimeError("boom")):
            status, _, detail = doctor.dual_capture_item("pulse")
        self.assertEqual(status, doctor.WARN)
        self.assertIn("boom", detail)


if __name__ == "__main__":
    unittest.main()
