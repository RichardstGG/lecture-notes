"""雙來源擷取對真實 PulseAudio／PipeWire 的整合測試（Hardware-adjacent：真的音訊伺服器，虛擬裝置）。

預設跳過。要跑請明確開啟：

    LEC_TEST_PULSE=1 python3 -m unittest tests.test_capture_pulse_integration -v

它會用 `pactl load-module` 建立幾個臨時的虛擬 null sink / remap source（名稱都以
lec_t_ 開頭），結束時卸載。**不會錄到任何真實麥克風或會議**：所有來源都是虛擬裝置，
聲音是測試自己用 paplay 播進去的合成音。

驗證的是 mock 測試證明不了的事：真的 ffmpeg + 真的 pulse 串流 + 真的串流綁定查詢，
兩路起跑時間差之後是否真的對齊、串流被系統轉接時是否真的偵測得到。
"""
import array
import math
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import wave
from pathlib import Path

from . import _pathfix  # noqa: F401
from core import capture as C

ENABLED = (os.environ.get("LEC_TEST_PULSE") == "1" and shutil.which("pactl")
           and shutil.which("ffmpeg") and shutil.which("paplay"))


def pa(*args):
    return subprocess.run(["pactl", *args], capture_output=True, text=True).stdout.strip()


def click_wav(path, seconds=14, first=1.0, every=1.0, rate=48000):
    """每秒一個短促的點擊聲（雙聲道）。"""
    buf = array.array("h", [0]) * (rate * 2 * seconds)
    k = first
    while k + 0.1 < seconds:
        i = int(k * rate)
        for j in range(240):
            v = int(25000 * math.sin(j * 0.4) * (1 - j / 240))
            buf[2 * (i + j)] = buf[2 * (i + j) + 1] = v
        k += every
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(buf.tobytes())


def tone_wav(path, freq, seconds=10, rate=48000):
    n = rate * seconds
    buf = array.array("h", (int(9000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)))
    stereo = array.array("h", bytes(4 * n))
    stereo[0::2] = buf
    stereo[1::2] = buf
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(stereo.tobytes())


def decode(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                          "-ar", str(C.SR), "-"], capture_output=True).stdout
    arr = array.array("h")
    arr.frombytes(raw)
    return arr


def goertzel(arr, freq):
    re = sum(v * math.cos(2 * math.pi * freq * i / C.SR) for i, v in enumerate(arr))
    im = sum(v * math.sin(2 * math.pi * freq * i / C.SR) for i, v in enumerate(arr))
    return math.hypot(re, im) / max(1, len(arr))


def onsets(arr, threshold=6000):
    out, i = [], 0
    while i < len(arr):
        if abs(arr[i]) > threshold:
            out.append(i / C.SR)
            i += C.SR // 2
        else:
            i += 1
    return out


@unittest.skipUnless(ENABLED, "需要 LEC_TEST_PULSE=1，以及 pactl／ffmpeg／paplay（會建立臨時虛擬裝置）")
class PulseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.modules = []
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self.unload)

    def load(self, *args):
        self.modules.append(pa("load-module", *args))

    def unload(self):
        for m in reversed(self.modules):
            pa("unload-module", m)

    def run_capture(self, specs, play, seconds, mid=None):
        events = []
        cap = C.MultiCapture(specs, self.tmp, mix_path=self.tmp / "mix.ogg", backend="pulse",
                             say=lambda m: None, on_event=lambda k, **kv: events.append((k, kv)))
        cap.start()
        players = [subprocess.Popen(["paplay", f"--device={dev}", str(wav)]) for dev, wav in play]
        t = time.monotonic()
        did_mid = False
        while time.monotonic() - t < seconds:
            time.sleep(0.2)
            if mid and not did_mid and time.monotonic() - t > mid[0]:
                did_mid = True
                mid[1](cap)
        cap.request_stop()
        while cap.read(0.5) != b"":
            pass
        for p in players:
            p.kill()
        return cap.report(), events

    def test_two_real_streams_do_not_cross_talk(self):
        self.load("module-null-sink", "sink_name=lec_t_remote")
        self.load("module-null-sink", "sink_name=lec_t_mic")
        tone_wav(self.tmp / "a.wav", 440)
        tone_wav(self.tmp / "b.wav", 1000)
        specs = [C.SourceSpec("system", "lec_t_remote.monitor", "remote", "monitor"),
                 C.SourceSpec("mic", "lec_t_mic.monitor", "mic", "input")]
        rep, _ = self.run_capture(specs, [("lec_t_remote", self.tmp / "a.wav"),
                                          ("lec_t_mic", self.tmp / "b.wav")], 7)
        self.assertEqual(rep["status"], "complete", rep["faults"])
        self.assertFalse(rep["degraded"])
        system, mic = decode(self.tmp / "tracks/system.ogg"), decode(self.tmp / "tracks/mic.ogg")
        window = slice(C.SR * 3, C.SR * 4)
        self.assertGreater(goertzel(system[window], 440), 1500)
        self.assertLess(goertzel(system[window], 1000), 150)
        self.assertGreater(goertzel(mic[window], 1000), 1500)
        self.assertLess(goertzel(mic[window], 440), 150)
        self.assertTrue(all(s["complete"] for s in rep["sources"]))
        self.assertEqual(C.verify_capture(self.tmp)["state"], "complete")

    def test_start_offset_between_two_ffmpeg_processes_is_aligned_away(self):
        """兩路錄同一份點擊聲（remap source 與原 monitor 內容完全相同），
        兩個 ffmpeg 的起跑時間差再大，對齊後的兩軌點擊位置誤差都要在幾毫秒內。"""
        self.load("module-null-sink", "sink_name=lec_t_s")
        self.load("module-remap-source", "master=lec_t_s.monitor", "source_name=lec_t_remap")
        click_wav(self.tmp / "c.wav")
        specs = [C.SourceSpec("system", "lec_t_s.monitor", "s", "monitor"),
                 C.SourceSpec("mic", "lec_t_remap", "r", "input")]
        rep, _ = self.run_capture(specs, [("lec_t_s", self.tmp / "c.wav")], 12)
        self.assertEqual(rep["status"], "complete", rep["faults"])
        a, b = onsets(decode(self.tmp / "tracks/system.ogg")), onsets(decode(self.tmp / "tracks/mic.ogg"))
        self.assertGreaterEqual(min(len(a), len(b)), 8)
        errors_ms = [abs(x - y) * 1000 for x, y in zip(a, b)]
        self.assertLess(max(errors_ms), 15, f"兩軌點擊位置誤差（ms）：{errors_ms}")
        offsets = [s["start_offset_ms"] for s in rep["sources"]]
        self.assertLess(max(offsets), 400)

    def test_stream_silently_moved_by_the_system_is_detected(self):
        """來源消失時 PipeWire／PulseAudio 會把錄音串流轉接到別的來源，ffmpeg 不會察覺。
        這裡用 move-source-output 模擬同一件事（不碰真實麥克風）。"""
        self.load("module-null-sink", "sink_name=lec_t_remote")
        self.load("module-null-sink", "sink_name=lec_t_mic")
        specs = [C.SourceSpec("system", "lec_t_remote.monitor", "remote", "monitor"),
                 C.SourceSpec("mic", "lec_t_mic.monitor", "mic", "input")]
        moved_at = []

        def move(cap):
            import json
            pid = cap._src[1].proc.pid
            for o in json.loads(pa("-f", "json", "list", "source-outputs")):
                if str(o["properties"].get("application.process.id")) == str(pid):
                    pa("move-source-output", str(o["index"]), "lec_t_remote.monitor")
                    moved_at.append(time.monotonic() - cap._t0)

        rep, events = self.run_capture(specs, [], 9, mid=(3, move))
        self.assertTrue(moved_at, "測試本身沒有成功轉接串流")
        fault = rep["faults"][0]
        self.assertEqual((fault["role"], fault["code"]), ("mic", "source_moved"))
        self.assertLess(fault["detected_at_seconds"] - moved_at[0], 3.0, "要在幾秒內發現")
        mic = {s["role"]: s for s in rep["sources"]}["mic"]
        self.assertLess(mic["duration_seconds"], moved_at[0] + 0.3, "轉接之後的音訊不能進分軌")
        self.assertEqual(rep["status"], "complete")
        self.assertIn("capture_source_failed", [k for k, _ in events])


if __name__ == "__main__":
    unittest.main()
