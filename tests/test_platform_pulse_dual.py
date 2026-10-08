"""core/platform.py 的雙來源錄音輔助：輸出裝置、串流綁定、播放活動、伺服器種類。

全部用 mock 的 pactl 輸出（欄位依 PipeWire 1.4.2 pipewire-pulse 的實測 JSON 整理，
串流綁定那份 JSON 是實機 `pactl -f json list source-outputs` 的節錄）。
"""
import json
import unittest
from unittest import mock

from . import _pathfix  # noqa: F401
from core import platform as P

SINKS = json.dumps([
    {"index": 59, "name": "alsa_output.speaker", "description": "Speaker", "state": "RUNNING",
     "active_port": "[Out] Speaker"},
    {"index": 60, "name": "bluez_output.AA.1", "description": "耳機", "state": "IDLE",
     "active_port": "headset-output"}])
SOURCES = json.dumps([
    {"index": 61, "name": "alsa_input.mic", "description": "Mic", "state": "RUNNING"},
    {"index": 59, "name": "alsa_output.speaker.monitor", "description": "Monitor", "state": "RUNNING"}])


def fake_run(table):
    def run(cmd, timeout=15):
        key = " ".join(cmd[1:])
        for k, v in table.items():
            if key.endswith(k):
                return v
        return ""
    return run


class SinkTests(unittest.TestCase):
    def patched(self, table, which="/usr/bin/pactl"):
        stack = [mock.patch.object(P.shutil, "which", return_value=which),
                 mock.patch.object(P, "_run", side_effect=fake_run(table))]
        for m in stack:
            m.start()
            self.addCleanup(m.stop)

    def test_list_sinks_reads_json(self):
        self.patched({"list sinks": SINKS})
        rows = P.list_sinks("pulse")
        self.assertEqual([r["id"] for r in rows], ["alsa_output.speaker", "bluez_output.AA.1"])
        self.assertEqual(rows[0]["state"], "running")
        self.assertEqual(rows[0]["port"], "[Out] Speaker")

    def test_list_sinks_is_none_without_pactl_or_off_pulse(self):
        self.patched({"list sinks": SINKS}, which=None)
        self.assertIsNone(P.list_sinks("pulse"))
        self.assertIsNone(P.list_sinks("avfoundation"))
        self.assertIsNone(P.list_sinks("dshow"))

    def test_unparseable_json_is_none_not_an_exception(self):
        self.patched({"list sinks": "pactl: 不支援 -f json"})
        self.assertIsNone(P.list_sinks("pulse"))

    def test_monitor_helpers(self):
        self.assertEqual(P.monitor_of("alsa_output.speaker"), "alsa_output.speaker.monitor")
        self.assertTrue(P.is_monitor_source("x.monitor"))
        self.assertFalse(P.is_monitor_source("alsa_input.mic"))

    def test_server_flavor(self):
        info = "Server Name: PulseAudio (on PipeWire 1.4.2)\nServer Version: 15.0.0\n"
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P, "_stdout", return_value=info):
            self.assertEqual(P.pulse_server(), {"name": "PulseAudio (on PipeWire 1.4.2)",
                                                "version": "15.0.0", "flavor": "pipewire"})
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P, "_stdout", return_value="Server Name: pulseaudio\n"):
            self.assertEqual(P.pulse_server()["flavor"], "pulseaudio")
        with mock.patch.object(P.shutil, "which", return_value=None):
            self.assertIsNone(P.pulse_server())


class BindingTests(unittest.TestCase):
    OUTPUTS = json.dumps([
        {"index": 3857, "driver": "PipeWire", "client": "3831", "source": 61, "corked": False,
         "properties": {"application.process.id": "380577", "application.name": "Lavf61.7.103"}},
        {"index": 3858, "source": 59, "properties": {"application.process.id": 4242}},
        {"index": 3859, "source": 61, "properties": {}}])

    def test_maps_each_recording_stream_pid_to_its_current_source(self):
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P, "_run", side_effect=fake_run({
                    "list source-outputs": self.OUTPUTS, "list sources": SOURCES})):
            bound = P.pulse_record_bindings()
        self.assertEqual(bound, {380577: "alsa_input.mic", 4242: "alsa_output.speaker.monitor"})

    def test_a_moved_stream_shows_the_new_source(self):
        moved = json.loads(self.OUTPUTS)
        moved[0]["source"] = 59                       # 被救援轉接到別的來源
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P, "_run", side_effect=fake_run({
                    "list source-outputs": json.dumps(moved), "list sources": SOURCES})):
            self.assertEqual(P.pulse_record_bindings()[380577], "alsa_output.speaker.monitor")

    def test_unqueryable_is_none_so_callers_can_warn(self):
        with mock.patch.object(P.shutil, "which", return_value=None):
            self.assertIsNone(P.pulse_record_bindings())

    def test_no_streams_is_an_empty_dict_not_none(self):
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P, "_run", side_effect=fake_run({
                    "list source-outputs": "[]", "list sources": SOURCES})):
            self.assertEqual(P.pulse_record_bindings(), {})


class SinkActivityTests(unittest.TestCase):
    def test_counts_only_uncorked_playback_per_sink(self):
        inputs = json.dumps([{"sink": 59, "corked": False}, {"sink": 59, "corked": True},
                             {"sink": 60, "corked": False}, {"sink": 60, "corked": False}])
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(P, "_run", side_effect=fake_run({
                    "list sink-inputs": inputs, "list sinks": SINKS})):
            self.assertEqual(P.sink_activity("pulse"),
                             {"alsa_output.speaker": 1, "bluez_output.AA.1": 2})


if __name__ == "__main__":
    unittest.main()
