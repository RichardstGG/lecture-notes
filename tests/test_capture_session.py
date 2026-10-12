"""MultiCapture（core/capture.py）的情境測試。

擷取端用 FakeCapture（即時速度的合成 PCM），編碼端是真的 ffmpeg，所以分軌與混音是真的 ogg。
需要 ffmpeg（含 libopus）與 ffprobe；沒有就跳過。每個測試只跑幾秒，起點對齊窗口與逾時
都縮短了，不代表正式參數（正式值見 core/capture.py 開頭的常數）。

驗證層級：Mock test（擷取端是假的）＋ Automated（編碼、檔案、驗證函式是真的）。
"""
import array
import json
import math
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path, PureWindowsPath

from . import _pathfix  # noqa: F401
from . import _capture_fakes as F
from core import capture as C

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
SYSTEM = C.SourceSpec("system", "out.monitor", "測試輸出", "monitor")
MIC = C.SourceSpec("mic", "mic0", "測試麥克風", "input")

FAST = dict(start_window=0.5, start_timeout=3.0, stall_timeout=1.0, verify_interval=0.2)


class Events:
    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()

    def __call__(self, kind, **kv):
        with self.lock:
            self.rows.append((kind, kv))

    def of(self, kind):
        with self.lock:
            return [kv for k, kv in self.rows if k == kind]


@unittest.skipUnless(HAVE_FFMPEG, "需要 ffmpeg 與 ffprobe")
class CaptureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events = Events()

    def make(self, system=None, mic=None, binding=None, **kw):
        self.popen = F.FakePopen({"out.monitor": system or F.FakeCapture(freq=440),
                                  "mic0": mic or F.FakeCapture(freq=880)})
        opts = {**FAST, **kw}
        return C.MultiCapture([SYSTEM, MIC], self.tmp, mix_path=self.tmp / "mix.ogg",
                              backend="pulse", popen=self.popen, binding=binding,
                              on_event=self.events, say=lambda m: None, **opts)

    def run_for(self, cap, seconds, stop=True):
        cap.start()
        pcm = bytearray()
        t_end = time.monotonic() + seconds
        stopped = False
        while True:
            if stop and not stopped and time.monotonic() >= t_end:
                cap.request_stop()
                stopped = True
            data = cap.read(timeout=0.2)
            if data == b"":
                break
            if data:
                pcm += data
            if time.monotonic() > t_end + 25:
                self.fail("擷取沒有在預期時間內結束")
        return bytes(pcm), cap.report()


class NormalRunTests(CaptureCase):
    def test_both_sources_recorded_to_separate_tracks_and_a_mix(self):
        cap = self.make()
        pcm, rep = self.run_for(cap, 3)
        self.assertEqual(rep["status"], "complete")
        self.assertFalse(rep["degraded"])
        for rel in ("tracks/system.ogg", "tracks/mic.ogg", "mix.ogg"):
            self.assertTrue((self.tmp / rel).is_file(), rel)
        by_role = {s["role"]: s for s in rep["sources"]}
        for role in ("system", "mic"):
            self.assertTrue(by_role[role]["complete"], by_role[role].get("problem"))
            self.assertEqual(len(by_role[role]["sha256"]), 64)
        # 兩軌等長（同一條時間軸），混音也是
        d1, d2 = (by_role[r]["duration_seconds"] for r in ("system", "mic"))
        self.assertAlmostEqual(d1, d2, delta=0.15)
        self.assertAlmostEqual(rep["mix"]["duration_seconds"], max(d1, d2), delta=0.15)
        self.assertAlmostEqual(len(pcm) // 2, rep["mix"]["duration_seconds"] * C.SR, delta=16)
        self.assertTrue(rep["mix"]["complete"])

    def test_staggered_start_is_aligned_and_recorded(self):
        """麥克風的 ffmpeg 晚 0.2 秒才開始送資料：不能假裝是同時開始，要補零並記下偏移。"""
        cap = self.make(mic=F.FakeCapture(freq=880, start_delay=0.2))
        _, rep = self.run_for(cap, 3)
        by_role = {s["role"]: s for s in rep["sources"]}
        self.assertEqual(by_role["system"]["start_offset_ms"], 0.0)
        self.assertAlmostEqual(by_role["mic"]["start_offset_ms"], 200, delta=60)
        self.assertAlmostEqual(by_role["system"]["duration_seconds"],
                               by_role["mic"]["duration_seconds"], delta=0.15)

    def test_mix_contains_both_sources(self):
        cap = self.make()
        pcm, _ = self.run_for(cap, 3)
        arr = array.array("h")
        arr.frombytes(pcm)

        def power(freq):
            seg = arr[C.SR:C.SR * 2]
            re = sum(v * math.cos(2 * math.pi * freq * i / C.SR) for i, v in enumerate(seg))
            im = sum(v * math.sin(2 * math.pi * freq * i / C.SR) for i, v in enumerate(seg))
            return math.hypot(re, im) / len(seg)

        self.assertGreater(power(440), 2000)
        self.assertGreater(power(880), 2000)
        self.assertLess(power(1500), 200)

    def test_capture_json_marks_complete_and_verify_agrees(self):
        cap = self.make()
        self.run_for(cap, 2)
        meta = json.loads((self.tmp / "capture.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "complete")
        self.assertEqual(meta["schema_version"], 1)
        self.assertEqual(meta["timebase"]["clock"], "host_monotonic")
        v = C.verify_capture(self.tmp)
        self.assertEqual(v["state"], "complete", v["problems"])
        self.assertTrue(all(v["usable"].values()))
        self.assertEqual(sorted(v["usable"]), ["mix.ogg", "tracks/mic.ogg", "tracks/system.ogg"])

    def test_started_event_reports_offsets(self):
        cap = self.make()
        self.run_for(cap, 1.5)
        started = self.events.of("capture_started")
        self.assertEqual(len(started), 1)
        self.assertEqual([s["role"] for s in started[0]["sources"]], ["system", "mic"])


class PathHelperTests(unittest.TestCase):
    """capture.json 的路徑格式：與寫入的平台無關，一律是相對 session、以 `/` 分隔。
    這些是純函式測試，不需要 ffmpeg，Linux 上也能用 PureWindowsPath 驗證 Windows 的行為。"""

    def test_windows_relative_path_is_serialised_with_forward_slashes(self):
        base = PureWindowsPath(r"C:\Users\USERNA~1\sessions\m1")
        self.assertEqual(C._rel(base / "tracks" / "mic.ogg", base), "tracks/mic.ogg")
        self.assertEqual(C._rel(base / "mix.ogg", base), "mix.ogg")

    def test_plain_str_of_a_windows_path_would_have_used_backslashes(self):
        # 這就是修正前的行為（str(path.relative_to(...))）；留著說明為什麼需要 _rel
        base = PureWindowsPath(r"C:\s")
        self.assertEqual(str((base / "tracks" / "mic.ogg").relative_to(base)), r"tracks\mic.ogg")

    def test_legacy_backslash_paths_are_normalised_on_read(self):
        self.assertEqual(C._portable_rel(r"tracks\mic.ogg"), "tracks/mic.ogg")
        self.assertEqual(C._portable_rel("tracks/mic.ogg"), "tracks/mic.ogg")
        self.assertEqual(C._portable_rel("mix.ogg"), "mix.ogg")

    def test_non_string_values_pass_through(self):
        self.assertIsNone(C._portable_rel(None))


class PortableMetadataTests(CaptureCase):
    EXPECTED = ["mix.ogg", "tracks/mic.ogg", "tracks/system.ogg"]

    def _finished_session(self):
        cap = self.make()
        self.run_for(cap, 2)
        return json.loads((self.tmp / "capture.json").read_text(encoding="utf-8"))

    def _rewrite_with_backslashes(self, status=None):
        """把 capture.json 改成 Windows 舊版會寫出的樣子。"""
        path = self.tmp / "capture.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        for src in meta["sources"]:
            if src.get("track"):
                src["track"] = src["track"].replace("/", "\\")
        meta["mix"]["path"] = meta["mix"]["path"].replace("/", "\\")
        if status:
            meta["status"] = status
        path.write_text(json.dumps(meta), encoding="utf-8")

    def test_capture_json_never_contains_backslashes_in_paths(self):
        meta = self._finished_session()
        paths = [s["track"] for s in meta["sources"]] + [meta["mix"]["path"]]
        self.assertEqual(sorted(paths), self.EXPECTED)
        self.assertFalse(any("\\" in p for p in paths), paths)

    def test_verify_keys_are_posix_paths(self):
        self._finished_session()
        self.assertEqual(sorted(C.verify_capture(self.tmp)["usable"]), self.EXPECTED)

    def test_legacy_backslash_capture_json_still_verifies_as_complete(self):
        self._finished_session()
        self._rewrite_with_backslashes()
        v = C.verify_capture(self.tmp)
        self.assertEqual(v["state"], "complete", v["problems"])
        self.assertEqual(sorted(v["usable"]), self.EXPECTED, "鍵一律是 POSIX 格式，不是舊檔裡的反斜線")
        self.assertTrue(all(v["usable"].values()))

    def test_legacy_backslash_paths_still_find_files_when_not_complete(self):
        # 沒有正常結束的錄音走 playable 分支；舊格式的路徑一樣要找得到檔案
        self._finished_session()
        self._rewrite_with_backslashes(status="aborted")
        v = C.verify_capture(self.tmp)
        self.assertEqual(v["state"], "incomplete")
        self.assertEqual(sorted(v["playable"]), self.EXPECTED)
        self.assertTrue(all(v["playable"].values()), v["playable"])
        self.assertFalse(any(v["usable"].values()), "沒有封裝紀錄，不可當重跑來源")

    def test_legacy_backslash_in_a_nested_mix_path_is_normalised_too(self):
        # 混音檔通常在 session 根目錄，沒有分隔符可轉；放進子資料夾才會真的用到 mix 的正規化
        (self.tmp / "audio").mkdir()
        self.popen = F.FakePopen({"out.monitor": F.FakeCapture(freq=440), "mic0": F.FakeCapture(freq=880)})
        cap = C.MultiCapture([SYSTEM, MIC], self.tmp, mix_path=self.tmp / "audio" / "mix.ogg",
                             backend="pulse", popen=self.popen, binding=None, on_event=self.events,
                             say=lambda m: None, **FAST)
        self.run_for(cap, 2)
        meta = json.loads((self.tmp / "capture.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["mix"]["path"], "audio/mix.ogg", "寫入端也要是 `/`")
        self._rewrite_with_backslashes()
        self.assertEqual(json.loads((self.tmp / "capture.json").read_text(encoding="utf-8"))["mix"]["path"],
                         "audio\\mix.ogg", "測試前提：確實改成了舊格式")
        v = C.verify_capture(self.tmp)
        self.assertEqual(v["state"], "complete", v["problems"])
        self.assertIn("audio/mix.ogg", v["usable"])
        self.assertTrue(all(v["usable"].values()))

    def test_a_real_corruption_is_still_reported_with_the_posix_name(self):
        self._finished_session()
        (self.tmp / "tracks" / "mic.ogg").write_bytes(b"truncated")
        self._rewrite_with_backslashes()
        v = C.verify_capture(self.tmp)
        self.assertEqual(v["state"], "invalid")
        self.assertFalse(v["usable"]["tracks/mic.ogg"])
        self.assertTrue(any("tracks/mic.ogg" in problem for problem in v["problems"]), v["problems"])


class SilenceIsNotAFaultTests(CaptureCase):
    def test_digital_silence_is_reported_as_levels_not_as_failure(self):
        cap = self.make(system=F.FakeCapture(silent=True))
        _, rep = self.run_for(cap, 3)
        self.assertFalse(rep["degraded"])
        self.assertEqual(rep["faults"], [])
        sysm = {s["role"]: s for s in rep["sources"]}["system"]
        self.assertIsNone(sysm["levels"]["peak_dbfs"])
        self.assertGreater(sysm["levels"]["digital_silence_seconds"], 2.0)
        self.assertEqual(self.events.of("capture_source_failed"), [])
        self.assertTrue(sysm["complete"])


class SourceFailureTests(CaptureCase):
    def test_one_source_dying_midway_keeps_the_other_and_says_so(self):
        cap = self.make(mic=F.FakeCapture(freq=880, die_after=2.0, exit_code=1,
                                          stderr_text="Connection killed"))
        pcm, rep = self.run_for(cap, 5)
        self.assertTrue(rep["degraded"])
        self.assertEqual(rep["status"], "complete")
        self.assertEqual(len(rep["faults"]), 1)
        fault = rep["faults"][0]
        self.assertEqual((fault["role"], fault["code"]), ("mic", "stream_ended"))
        self.assertIn("Connection killed", fault["detail"])
        self.assertAlmostEqual(fault["at_seconds"], 2.0, delta=0.5)
        by_role = {s["role"]: s for s in rep["sources"]}
        self.assertIsNotNone(by_role["mic"]["fault"])
        self.assertIsNone(by_role["system"]["fault"])
        # 麥克風軌在故障處結束；系統軌錄完整段；混音跟系統軌一樣長
        self.assertLess(by_role["mic"]["duration_seconds"], by_role["system"]["duration_seconds"] - 1.5)
        self.assertGreater(by_role["system"]["duration_seconds"], 4.0)
        self.assertAlmostEqual(rep["mix"]["duration_seconds"],
                               by_role["system"]["duration_seconds"], delta=0.2)
        self.assertTrue(by_role["mic"]["complete"], "故障前錄到的部分仍要正常封裝")
        ev = self.events.of("capture_source_failed")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["remaining"], ["system"])
        self.assertEqual(C.verify_capture(self.tmp)["state"], "degraded")

    def test_alive_but_silent_pipe_is_a_stall_fault_not_silence(self):
        cap = self.make(mic=F.FakeCapture(freq=880, stall_after=1.5))
        _, rep = self.run_for(cap, 5)
        fault = rep["faults"][0]
        self.assertEqual((fault["role"], fault["code"]), ("mic", "stalled"))
        self.assertAlmostEqual(fault["at_seconds"], 1.5, delta=0.5)
        self.assertGreater(fault["detected_at_seconds"], fault["at_seconds"] + 0.8)
        self.assertTrue(rep["degraded"])

    def test_both_sources_failing_ends_the_session_as_failed(self):
        cap = self.make(system=F.FakeCapture(die_after=1.5), mic=F.FakeCapture(freq=880, die_after=2.0))
        pcm, rep = self.run_for(cap, 6, stop=False)
        self.assertEqual(rep["status"], "failed")
        self.assertEqual(sorted(f["role"] for f in rep["faults"]), ["mic", "system"])
        self.assertTrue(pcm)

    def test_loss_inside_a_live_source_is_filled_and_recorded(self):
        # 缺口要短於 stall_timeout（測試用 1 秒）：更長的「完全沒資料」是擷取停滯故障，不是缺口
        cap = self.make(mic=F.FakeCapture(freq=880, loss=[(1.5, 0.6)]))
        _, rep = self.run_for(cap, 5, stop=True)
        mic = {s["role"]: s for s in rep["sources"]}["mic"]
        self.assertEqual(len(mic["gaps"]), 1)
        self.assertAlmostEqual(mic["gaps"][0]["seconds"], 0.6, delta=0.25)
        self.assertEqual(len(self.events.of("capture_gap")), 1)
        self.assertTrue(rep["degraded"])
        self.assertEqual(rep["faults"], [])
        self.assertAlmostEqual(mic["duration_seconds"],
                               {s["role"]: s for s in rep["sources"]}["system"]["duration_seconds"],
                               delta=0.3)


class DriftEndToEndTests(CaptureCase):
    def test_a_slow_device_clock_is_corrected_so_tracks_stay_aligned(self):
        """麥克風的裝置時脈慢 0.2%（誇張的值，才能在幾秒內看到效果）：
        不修正的話兩軌會逐漸錯開（12 秒約 24 ms）；修正後輸出等長，且補的是慢的那一路。"""
        # 基準線視窗縮成 3 秒，才看得到幾秒內的修正（正式值 30 秒：修正落後約一個視窗，
        # 穩態誤差 = 漂移量 × 視窗，100 ppm 時只有 3 ms）
        cap = self.make(mic=F.FakeCapture(freq=880, rate=0.998), aligner_opts=dict(window=3.0))
        _, rep = self.run_for(cap, 12)
        by_role = {s["role"]: s for s in rep["sources"]}
        self.assertAlmostEqual(by_role["system"]["samples_out"] / C.SR,
                               by_role["mic"]["samples_out"] / C.SR, delta=0.06)
        self.assertGreater(by_role["mic"]["drift"]["slip_inserted"], 100,
                           "慢的那一路要被補樣本")
        self.assertEqual(by_role["mic"]["drift"]["slip_dropped"] < 40, True)
        self.assertEqual(by_role["system"]["drift"]["slip_inserted"] < 40, True)
        self.assertEqual(rep["faults"], [])
        self.assertEqual(by_role["mic"]["gaps"], [])


class BindingTests(CaptureCase):
    """來源消失時系統會把錄音串流默默轉接到預設來源（實測：PipeWire 1.4.2）。"""

    def test_stream_moved_to_another_device_is_a_fault_and_its_audio_is_dropped(self):
        mic = F.FakeCapture(freq=880)
        system = F.FakeCapture(freq=440)
        t0 = time.monotonic()

        def binding():
            moved = time.monotonic() - t0 > 2.2
            return {system.pid: "out.monitor", mic.pid: "other_mic" if moved else "mic0"}

        cap = self.make(system=system, mic=mic, binding=binding)
        _, rep = self.run_for(cap, 5)
        fault = rep["faults"][0]
        self.assertEqual((fault["role"], fault["code"]), ("mic", "source_moved"))
        self.assertIn("other_mic", fault["detail"])
        self.assertIn("mic0", fault["detail"])
        mic_meta = {s["role"]: s for s in rep["sources"]}["mic"]
        # 轉接之後來的資料不能進分軌：分軌最多到最後一次確認綁定正確的時間
        self.assertLess(mic_meta["duration_seconds"], 2.4)
        self.assertEqual(self.popen.captures["mic0"].returncode is not None, True)

    def test_wrong_binding_at_start_is_refused(self):
        mic, system = F.FakeCapture(freq=880), F.FakeCapture(freq=440)
        cap = self.make(system=system, mic=mic,
                        binding=lambda: {system.pid: "out.monitor", mic.pid: "intruder"})
        with self.assertRaises(C.CaptureStartError) as cm:
            cap.start()
        self.assertEqual(cm.exception.code, "wrong_source")
        self.assertEqual(cm.exception.role, "mic")
        self.assertFalse((self.tmp / "tracks").exists(), "啟動失敗不能留下輸出")

    def test_unqueryable_binding_warns_but_records(self):
        cap = self.make(binding=lambda: None)
        _, rep = self.run_for(cap, 2)
        self.assertEqual(rep["status"], "complete")
        self.assertTrue(any(w["code"] == "binding_unverifiable" for w in rep["warnings"]))

    def test_default_output_change_is_warned_not_followed(self):
        seq = iter(["sinkA", "sinkA", "sinkB"] + ["sinkB"] * 50)
        C.DEFAULT_WATCH_EVERY, saved = 0.3, C.DEFAULT_WATCH_EVERY
        self.addCleanup(setattr, C, "DEFAULT_WATCH_EVERY", saved)
        cap = self.make(default_output=lambda: next(seq))
        _, rep = self.run_for(cap, 3)
        warns = [w for w in rep["warnings"] if w["code"] == "default_output_changed"]
        self.assertEqual(len(warns), 1)
        self.assertEqual((warns[0]["previous"], warns[0]["current"]), ("sinkA", "sinkB"))
        self.assertEqual(rep["faults"], [])


class StartFailureTests(CaptureCase):
    def test_source_that_fails_to_open_aborts_everything(self):
        bad = F.FakeCapture(die_after=0, exit_code=1, stderr_text="No such entity")
        cap = self.make(mic=bad)
        with self.assertRaises(C.CaptureStartError) as cm:
            cap.start()
        self.assertEqual(cm.exception.code, "start_failed")
        self.assertEqual(cm.exception.role, "mic")
        self.assertIn("No such entity", str(cm.exception))
        self.assertIsNotNone(self.popen.captures["out.monitor"].returncode, "另一路也要被收掉")
        self.assertFalse((self.tmp / "tracks").exists())
        self.assertFalse((self.tmp / "mix.ogg").exists())
        self.assertEqual(cap.read(timeout=1), b"", "start 失敗後 read 不能卡住")

    def test_source_that_never_sends_audio_times_out(self):
        cap = self.make(mic=F.FakeCapture(never_start=True), start_timeout=1.2)
        with self.assertRaises(C.CaptureStartError) as cm:
            cap.start()
        self.assertEqual(cm.exception.code, "start_timeout")
        self.assertEqual(cm.exception.role, "mic")

    def test_stop_during_start_is_cancelled_not_failed(self):
        cap = self.make(mic=F.FakeCapture(never_start=True), start_timeout=5)
        threading.Timer(0.4, cap.request_stop).start()
        with self.assertRaises(C.CaptureStartError) as cm:
            cap.start()
        self.assertEqual(cm.exception.code, "cancelled")

    def test_refuses_to_overwrite_existing_tracks(self):
        (self.tmp / "tracks").mkdir()
        (self.tmp / "tracks" / "mic.ogg").write_bytes(b"precious")
        cap = self.make()
        with self.assertRaises(C.CaptureStartError) as cm:
            cap.start()
        self.assertEqual(cm.exception.code, "output_exists")
        self.assertEqual((self.tmp / "tracks" / "mic.ogg").read_bytes(), b"precious")

    def test_refusing_to_start_never_deletes_an_existing_mix(self):
        """回歸：output_exists 拒絕開始後的清理，曾經會把既有的混音檔一起刪掉。"""
        (self.tmp / "mix.ogg").write_bytes(b"precious mix")
        cap = self.make()
        with self.assertRaises(C.CaptureStartError) as cm:
            cap.start()
        self.assertEqual(cm.exception.code, "output_exists")
        self.assertEqual((self.tmp / "mix.ogg").read_bytes(), b"precious mix")

    def test_failed_start_removes_only_what_it_created(self):
        bad = F.FakeCapture(die_after=0, stderr_text="No such entity")
        cap = self.make(mic=bad)
        (self.tmp / "notes.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(C.CaptureStartError):
            cap.start()
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["notes.txt"])

    def test_rejects_bad_source_sets(self):
        for specs in ([SYSTEM], [SYSTEM, C.SourceSpec("system", "x", "", "")],
                      [SYSTEM, C.SourceSpec("mic", "out.monitor", "", "")],
                      [SYSTEM, C.SourceSpec("mic", "default", "", "")]):
            cap = C.MultiCapture(specs, self.tmp, backend="pulse", popen=F.FakePopen({}),
                                 say=lambda m: None)
            with self.assertRaises(C.CaptureStartError) as cm:
                cap.start()
            self.assertEqual(cm.exception.code, "invalid_sources")

    def test_non_linux_backend_is_refused_with_a_clear_reason(self):
        for backend in ("avfoundation", "dshow"):
            cap = C.MultiCapture([SYSTEM, MIC], self.tmp, backend=backend, say=lambda m: None)
            with self.assertRaises(C.CaptureStartError) as cm:
                cap.start()
            self.assertEqual(cm.exception.code, "unsupported_platform")
            self.assertIn("實機驗證", str(cm.exception))


class StopTests(CaptureCase):
    def test_forced_kill_is_not_mistaken_for_a_complete_recording(self):
        cap = self.make()
        cap.start()
        time.sleep(1.5)
        cap.kill()
        rep = cap.report()
        self.assertEqual(rep["status"], "aborted")
        v = C.verify_capture(self.tmp)
        self.assertEqual(v["state"], "incomplete")
        self.assertFalse(any(v["usable"].values()), "強制停止的產物不可當成可重跑的來源")
        # 但檔案多半還能播，留給人工搶救
        self.assertTrue(all(v["playable"].values()), v["playable"])
        self.assertEqual(sorted(v["playable"]), ["mix.ogg", "tracks/mic.ogg", "tracks/system.ogg"])

    def test_crash_leaves_checkpoint_that_verify_calls_incomplete(self):
        """程序崩潰：capture.json 還停在 recording，檔案就算存在也不能當可重跑的來源。"""
        cap = self.make()
        cap.start()
        time.sleep(1.0)
        meta = json.loads((self.tmp / "capture.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "recording")
        v = C.verify_capture(self.tmp)
        self.assertEqual(v["state"], "incomplete")
        self.assertFalse(any(v["usable"].values()))
        cap.kill()
        cap.wait(30)

    def test_normal_stop_returns_after_tracks_are_finalised(self):
        cap = self.make()
        t = time.monotonic()
        _, rep = self.run_for(cap, 1.5)
        self.assertLess(time.monotonic() - t, 15)
        self.assertTrue(all(s["complete"] for s in rep["sources"]))

    def test_snapshot_reports_per_source_state(self):
        cap = self.make(mic=F.FakeCapture(freq=880, die_after=1.5))
        cap.start()
        time.sleep(3.0)
        snap = cap.snapshot()
        states = {s["role"]: s["state"] for s in snap["sources"]}
        self.assertEqual(states, {"system": "recording", "mic": "failed"})
        self.assertTrue(snap["degraded"])
        cap.request_stop()
        cap.wait(30)


class VerifyCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_missing_means_legacy_or_absent(self):
        self.assertEqual(C.verify_capture(self.tmp)["state"], "missing")

    def test_garbage_metadata_is_invalid_not_a_crash(self):
        (self.tmp / "capture.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(C.verify_capture(self.tmp)["state"], "invalid")

    def test_tampered_or_truncated_track_is_detected(self):
        (self.tmp / "tracks").mkdir()
        track = self.tmp / "tracks" / "mic.ogg"
        track.write_bytes(b"x" * 100)
        meta = {"schema_version": 1, "kind": "multi_source_capture", "status": "complete",
                "degraded": False, "faults": [], "mix": None,
                "sources": [{"role": "mic", "track": "tracks/mic.ogg", "complete": True,
                             "bytes": 100, "sha256": C._sha256(track)}]}
        (self.tmp / "capture.json").write_text(json.dumps(meta), encoding="utf-8")
        self.assertEqual(C.verify_capture(self.tmp)["state"], "complete")
        track.write_bytes(b"y" * 100)                       # 同大小但內容被改
        v = C.verify_capture(self.tmp)
        self.assertEqual(v["state"], "invalid")
        self.assertIn("SHA-256", v["problems"][0])
        self.assertFalse(v["usable"]["tracks/mic.ogg"])
        track.write_bytes(b"x" * 40)                        # 被截斷
        self.assertIn("大小", C.verify_capture(self.tmp)["problems"][0])
        track.unlink()
        self.assertIn("不存在", C.verify_capture(self.tmp)["problems"][0])

    def test_track_flagged_incomplete_at_finalisation_is_never_usable(self):
        (self.tmp / "tracks").mkdir()
        (self.tmp / "tracks" / "mic.ogg").write_bytes(b"x")
        meta = {"schema_version": 1, "kind": "multi_source_capture", "status": "complete",
                "degraded": False, "faults": [], "mix": None,
                "sources": [{"role": "mic", "track": "tracks/mic.ogg", "complete": False,
                             "problem": "編碼行程中斷", "bytes": 1, "sha256": None}]}
        (self.tmp / "capture.json").write_text(json.dumps(meta), encoding="utf-8")
        v = C.verify_capture(self.tmp)
        self.assertEqual(v["state"], "invalid")
        self.assertFalse(v["usable"]["tracks/mic.ogg"])


if __name__ == "__main__":
    unittest.main()
