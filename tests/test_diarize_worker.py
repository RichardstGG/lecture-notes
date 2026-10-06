"""分群子行程這一層：父行程怎麼啟動、解析、取消 diarize_worker.py。

分三層：
1. 假 worker 腳本 —— 測父行程的協定解析、錯誤分類、當機、取消會真的結束行程。
2. 真的 diarize_worker.py + 假的 sherpa_onnx 模組 —— 測 worker 本身沒有打錯字
   （mock 測試最容易漏掉的就是這一段膠水）。
3. 真的 sherpa-onnx 與模型 —— 有裝才跑，否則跳過。

前兩層只需要 python 與 ffmpeg，不需要任何模型。
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import textwrap
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import diarize as D
from core import platform as P

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def write_wav(path, seconds=3.0, sr=16000):
    frames = bytearray()
    for i in range(int(seconds * sr)):
        frames += struct.pack("<h", 2000 if (i // 800) % 2 else -2000)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return path


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.audio = write_wav(self.root / "a.wav")
        self.seg = self.root / "seg.onnx"
        self.emb = self.root / "emb.onnx"
        self.seg.write_bytes(b"x")
        self.emb.write_bytes(b"x")

    def req(self, **kw):
        base = dict(session_dir=self.root, source_audio=self.audio, source_sha256="",
                    meeting_name="m", requested_speakers=2,
                    segmentation_model=self.seg, embedding_model=self.emb,
                    staging_dir=self.staging)
        base.update(kw)
        return D.DiarizationRequest(**base)

    def script(self, body, name="worker.py"):
        path = self.root / name
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def run_worker(self, worker, cancel=None, progress=None):
        events = []
        with mock.patch.object(D, "WORKER", worker), \
             mock.patch.object(D, "find_python", return_value=Path(sys.executable)):
            out = D._default_diarizer(
                self.req(), progress or (lambda p, t, s: events.append((p, t, s))), cancel)
        return out, events


# ---------------------------------------------------------------- 協定（假 worker）
class TestProtocol(Base):
    def test_happy_path_parses_progress_and_result(self):
        w = self.script("""
            import json, sys
            sys.stdin.read()
            for p in (0.0, 5.0, 10.0):
                print(json.dumps({"type": "progress", "processed": p, "total": 10.0}), flush=True)
            print(json.dumps({"type": "result", "duration": 10.0, "engine_version": "9.9",
                              "turns": [[0.0, 4.0, 3, 0.9], [5.0, 9.0, 1, 0.8]]}), flush=True)
        """)
        out, events = self.run_worker(w)
        self.assertEqual(out.duration, 10.0)
        self.assertEqual(out.engine_version, "9.9")
        self.assertEqual(out.turns, [(0.0, 4.0, 3, 0.9), (5.0, 9.0, 1, 0.8)])
        self.assertEqual([e[0] for e in events], [0.0, 5.0, 10.0])
        self.assertTrue(all(e[1] == 10.0 for e in events))

    def test_request_reaches_worker_on_stdin(self):
        w = self.script(f"""
            import json, sys
            req = json.loads(sys.stdin.read())
            open({str(self.root / 'seen.json')!r}, "w").write(json.dumps(req))
            print(json.dumps({{"type": "result", "duration": 1.0, "turns": []}}), flush=True)
        """)
        self.run_worker(w)
        seen = json.loads((self.root / "seen.json").read_text())
        self.assertEqual(seen["audio"], str(self.audio))
        self.assertEqual(seen["num_speakers"], 2)
        self.assertEqual(seen["threshold"], 0.5)
        self.assertEqual(seen["window_shift"], 0.1)
        self.assertEqual(seen["segmentation_model"], str(self.seg))
        self.assertEqual(seen["embedding_model"], str(self.emb))

    def test_error_event_keeps_its_code(self):
        w = self.script("""
            import json, sys
            sys.stdin.read()
            print(json.dumps({"type": "error", "code": "decode_failed", "message": "壞檔"}))
            sys.exit(2)
        """)
        with self.assertRaises(D.DiarizationError) as cm:
            self.run_worker(w)
        self.assertEqual(cm.exception.code, "decode_failed")
        self.assertIn("壞檔", cm.exception.message)

    def test_unknown_error_code_is_not_passed_through(self):
        w = self.script("""
            import json, sys
            sys.stdin.read()
            print(json.dumps({"type": "error", "code": "made_up", "message": "x"}))
            sys.exit(2)
        """)
        with self.assertRaises(D.DiarizationError) as cm:
            self.run_worker(w)
        self.assertEqual(cm.exception.code, "segmentation_failed")

    def test_crash_without_result_reports_exit_code_and_stderr_tail(self):
        w = self.script("""
            import sys
            sys.stdin.read()
            sys.stderr.write("Segmentation fault: onnx exploded\\n")
            sys.exit(139)
        """)
        with self.assertRaises(D.DiarizationError) as cm:
            self.run_worker(w)
        self.assertEqual(cm.exception.code, "segmentation_failed")
        self.assertIn("139", cm.exception.message)
        self.assertIn("onnx exploded", cm.exception.message)

    def test_oom_kill_gets_a_memory_hint(self):
        w = self.script("""
            import os, signal, sys
            sys.stdin.read()
            os.kill(os.getpid(), signal.SIGKILL)
        """)
        if os.name == "nt":
            self.skipTest("POSIX only")
        with self.assertRaises(D.DiarizationError) as cm:
            self.run_worker(w)
        self.assertIn("記憶體", cm.exception.message)

    def test_garbage_lines_are_ignored_not_fatal(self):
        w = self.script("""
            import json, sys
            sys.stdin.read()
            print("not json at all")
            print(json.dumps({"type": "progress", "processed": 1.0, "total": 2.0}))
            print(json.dumps({"type": "result", "duration": 2.0, "turns": [[0.0, 1.0, 0, 1.0]]}))
        """)
        out, _ = self.run_worker(w)
        self.assertEqual(len(out.turns), 1)

    def test_worker_that_exits_before_reading_stdin_does_not_hang_or_crash_parent(self):
        w = self.script("""
            import sys
            sys.exit(3)
        """)
        with self.assertRaises(D.DiarizationError) as cm:
            self.run_worker(w)
        self.assertEqual(cm.exception.code, "segmentation_failed")


# ---------------------------------------------------------------- 取消
class TestCancel(Base):
    SLEEPER = """
        import json, os, sys, time
        sys.stdin.read()
        open({pidfile!r}, "w").write(str(os.getpid()))
        print(json.dumps({{"type": "progress", "processed": 1.0, "total": 100.0}}), flush=True)
        time.sleep(120)
    """

    def test_cancel_kills_the_worker_process(self):
        pidfile = self.root / "pid"
        w = self.script(self.SLEEPER.format(pidfile=str(pidfile)))
        seen = []

        def cancel():
            return bool(seen)

        t0 = time.monotonic()
        with self.assertRaises(D.DiarizationCancelled):
            self.run_worker(w, cancel=cancel, progress=lambda p, t, s: seen.append(p))
        self.assertLess(time.monotonic() - t0, 20, "取消要在數秒內生效，不能等 worker 睡完")
        pid = int(pidfile.read_text())
        for _ in range(40):
            if not P.pid_alive(pid):
                break
            time.sleep(0.25)
        self.assertFalse(P.pid_alive(pid), "取消後 worker 行程必須真的消失")

    def test_cancel_while_worker_is_silent(self):
        # 還沒有任何輸出就取消（例如模型還在載入）：輪詢逾時也要能中斷
        pidfile = self.root / "pid"
        w = self.script(f"""
            import os, sys, time
            sys.stdin.read()
            open({str(pidfile)!r}, "w").write(str(os.getpid()))
            time.sleep(120)
        """)
        t0 = time.monotonic()
        with self.assertRaises(D.DiarizationCancelled):
            self.run_worker(w, cancel=lambda: pidfile.exists())
        self.assertLess(time.monotonic() - t0, 20)
        pid = int(pidfile.read_text())
        for _ in range(40):
            if not P.pid_alive(pid):
                break
            time.sleep(0.25)
        self.assertFalse(P.pid_alive(pid))


# ---------------------------------------------------------------- 直譯器探索
class TestFindPython(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LEC_DIARIZE_PYTHON", None)

    def test_none_found_gives_engine_unavailable_with_install_hint(self):
        req = D.DiarizationRequest(
            session_dir=Path("."), source_audio=Path("."), source_sha256="",
            meeting_name="m", requested_speakers=2,
            segmentation_model=Path("."), embedding_model=Path("."),
            staging_dir=Path(tempfile.gettempdir()))
        with mock.patch.object(D.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(D, "_venv_python", return_value=Path("/nonexistent/python")):
            with self.assertRaises(D.DiarizationError) as cm:
                D._default_diarizer(req, lambda *a: None, None)
        self.assertEqual(cm.exception.code, "engine_unavailable")
        self.assertIn("pip install sherpa-onnx==" + D.SHERPA_ONNX_VERSION, cm.exception.message)
        self.assertIn("LEC_DIARIZE_PYTHON", cm.exception.message)

    def test_current_interpreter_is_used_when_it_has_sherpa(self):
        with mock.patch.object(D.importlib.util, "find_spec", return_value=object()):
            self.assertEqual(D.find_python(), Path(sys.executable))

    def test_explicit_python_beats_the_venv(self):
        fake = Path(sys.executable)
        with mock.patch.object(D.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(D, "_can_import_sherpa", return_value=True):
            self.assertEqual(D.find_python(fake), fake)

    def test_env_var_is_honoured(self):
        os.environ["LEC_DIARIZE_PYTHON"] = sys.executable
        with mock.patch.object(D.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(D, "_can_import_sherpa", return_value=True):
            self.assertEqual(D.find_python(), Path(sys.executable))

    def test_interpreter_without_sherpa_is_skipped(self):
        with mock.patch.object(D.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(D, "_can_import_sherpa", return_value=False), \
             mock.patch.object(D, "_venv_python", return_value=Path(sys.executable)):
            self.assertIsNone(D.find_python(Path(sys.executable)))

    def test_venv_path_follows_the_platform_layout(self):
        rel = D._venv_python().relative_to(D.APP_ROOT / ".venv")
        self.assertEqual(rel, Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python"))


# ---------------------------------------------------------------- 真的 worker + 假的 sherpa
FAKE_SHERPA = '''
__version__ = "0.0-fake"
CALLS = {}

class _Cfg:
    def __init__(self, *a, **kw):
        self.args, self.kw = a, kw
    def validate(self):
        return True

class OfflineSpeakerSegmentationPyannoteModelConfig(_Cfg): pass
class OfflineSpeakerSegmentationModelConfig(_Cfg): pass
class SpeakerEmbeddingExtractorConfig(_Cfg): pass
class FastClusteringConfig(_Cfg): pass

class OfflineSpeakerDiarizationConfig(_Cfg):
    def validate(self):
        import os
        return os.environ.get("FAKE_SHERPA_INVALID") != "1"

class _Seg:
    def __init__(self, s, e, spk, conf):
        self.start, self.end, self.speaker, self.confidence = s, e, spk, conf

class _Result:
    def __init__(self, segs): self.segs = segs
    def sort_by_start_time(self): return list(self.segs)

class OfflineSpeakerDiarization:
    def __init__(self, cfg):
        import json, os
        out = os.environ.get("FAKE_SHERPA_DUMP")
        if out:
            c = cfg.kw
            json.dump({
                "num_clusters": c["clustering"].kw["num_clusters"],
                "threshold": c["clustering"].kw["threshold"],
                "window_shift": c["segmentation"].kw["pyannote"].kw["window_shift_ratio"],
                "seg_model": c["segmentation"].kw["pyannote"].kw["model"],
                "emb_model": c["embedding"].kw["model"],
                "threads": c["segmentation"].kw["num_threads"],
            }, open(out, "w"))
    def process(self, samples, callback=None):
        import os
        if os.environ.get("FAKE_SHERPA_RAISE") == "1":
            raise RuntimeError("boom inside process")
        n = len(samples)
        for done in (1, 2, 3):
            if callback is not None and callback(done, 3) != 0:
                break
        return _Result([_Seg(0.0, 1.0, 5, 0.9), _Seg(1.2, 2.0, 2, 0.7)])
'''


@unittest.skipUnless(HAVE_FFMPEG, "需要 ffmpeg")
class TestRealWorkerWithFakeSherpa(Base):
    def setUp(self):
        super().setUp()
        self.stub = self.root / "stub"
        self.stub.mkdir()
        (self.stub / "sherpa_onnx.py").write_text(FAKE_SHERPA, encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"PYTHONPATH": str(self.stub)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dump = self.root / "dump.json"

    def run_real(self, **env):
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(D, "find_python", return_value=Path(sys.executable)):
            events = []
            out = D._default_diarizer(self.req(), lambda p, t, s: events.append((p, t, s)), None)
        return out, events

    def test_worker_runs_end_to_end_against_the_fake_module(self):
        out, events = self.run_real()
        self.assertEqual(out.engine_version, "0.0-fake")
        self.assertAlmostEqual(out.duration, 3.0, places=1)
        self.assertEqual(out.turns, [(0.0, 1.0, 5, 0.9), (1.2, 2.0, 2, 0.7)])
        self.assertGreaterEqual(len(events), 4)
        self.assertEqual(events[0][0], 0.0)
        self.assertAlmostEqual(events[-1][0], out.duration, places=1)

    def test_worker_builds_the_sherpa_config_from_the_request(self):
        self.run_real(FAKE_SHERPA_DUMP=str(self.dump))
        seen = json.loads(self.dump.read_text())
        self.assertEqual(seen["num_clusters"], 2)
        self.assertEqual(seen["threshold"], 0.5)
        self.assertEqual(seen["window_shift"], 0.1)
        self.assertEqual(seen["seg_model"], str(self.seg))
        self.assertEqual(seen["emb_model"], str(self.emb))
        self.assertEqual(seen["threads"], 8)

    def test_invalid_sherpa_config_is_input_invalid(self):
        with self.assertRaises(D.DiarizationError) as cm:
            self.run_real(FAKE_SHERPA_INVALID="1")
        self.assertEqual(cm.exception.code, "input_invalid")

    def test_exception_inside_process_is_segmentation_failed(self):
        with self.assertRaises(D.DiarizationError) as cm:
            self.run_real(FAKE_SHERPA_RAISE="1")
        self.assertEqual(cm.exception.code, "segmentation_failed")
        self.assertIn("boom inside process", cm.exception.message)

    def test_undecodable_audio_is_decode_failed(self):
        (self.root / "bad.wav").write_bytes(b"this is not audio")
        with mock.patch.object(D, "find_python", return_value=Path(sys.executable)):
            with self.assertRaises(D.DiarizationError) as cm:
                D._default_diarizer(self.req(source_audio=self.root / "bad.wav"),
                                    lambda *a: None, None)
        self.assertEqual(cm.exception.code, "decode_failed")

    def test_missing_sherpa_in_the_chosen_interpreter_is_engine_unavailable(self):
        # PYTHONPATH 不放 stub，直譯器就 import 不到 sherpa_onnx
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(self.root / "empty")}), \
             mock.patch.object(D, "find_python", return_value=Path(sys.executable)):
            with self.assertRaises(D.DiarizationError) as cm:
                D._default_diarizer(self.req(), lambda *a: None, None)
        self.assertEqual(cm.exception.code, "engine_unavailable")

    def test_full_pipeline_through_the_real_worker(self):
        # 真 worker + 假 sherpa + 假 ASR：從 diarize_session 一路走到產物
        session = self.root / "session"
        session.mkdir()
        src = write_wav(session / "source_audio.wav", seconds=3.0)

        def asr(req, pcm, prompt):
            return [(0.0, 0.5, "測試")]

        req = D.DiarizationRequest(
            session_dir=session, source_audio=src, source_sha256="", meeting_name="整合",
            requested_speakers=2, segmentation_model=self.seg, embedding_model=self.emb,
            opencc=False)
        with mock.patch.object(D, "find_python", return_value=Path(sys.executable)):
            r = D.diarize_session(req, asr=asr)
        self.assertEqual(r.actual_speakers, 2)
        js = json.loads((session / r.speakers_json).read_text())
        self.assertEqual(js["engine"]["version"], "0.0-fake")
        self.assertEqual([s["speaker_id"] for s in js["segments"]], ["S01", "S02"])
        self.assertEqual(list(session.glob(".diarize-staging-*")), [])


# ---------------------------------------------------------------- 真的 sherpa-onnx
def _real_models():
    base = os.environ.get("LEC_TEST_DIARIZE_MODELS")
    dirs = [Path(base)] if base else [D.APP_ROOT / "models"]
    for d in dirs:
        seg = d / "sherpa-onnx-pyannote-segmentation-3-0.onnx"
        emb = d / "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
        if seg.is_file() and emb.is_file():
            return seg, emb
    return None


@unittest.skipUnless(HAVE_FFMPEG, "需要 ffmpeg")
@unittest.skipUnless(_real_models() and D.find_python(),
                     "需要真的 sherpa-onnx 與兩個模型（LEC_TEST_DIARIZE_MODELS 指向模型資料夾）")
class TestRealSherpa(Base):
    """冒煙測試：不測準確度，只確認真引擎的綁定、協定與取消是通的。"""

    def setUp(self):
        super().setUp()
        self.seg, self.emb = _real_models()
        sample = D.APP_ROOT / "samples" / "test8min.ogg"
        if not sample.is_file():
            self.skipTest("找不到 samples/test8min.ogg")
        self.clip = self.root / "clip.wav"
        import subprocess
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-t", "40",
                        "-i", str(sample), "-ac", "1", "-ar", "16000", str(self.clip)],
                       check=True)

    def test_real_engine_returns_turns_for_a_single_speaker_clip(self):
        out = D._default_diarizer(
            self.req(source_audio=self.clip, requested_speakers=1,
                     segmentation_model=self.seg, embedding_model=self.emb),
            lambda *a: None, None)
        self.assertAlmostEqual(out.duration, 40.0, delta=1.0)
        self.assertTrue(out.turns)
        self.assertEqual({t[2] for t in out.turns}, {out.turns[0][2]},
                         "num_speakers=1 時所有區間都該屬於同一個 cluster")
        self.assertNotEqual(out.engine_version, "unknown")

    def test_real_engine_can_be_cancelled_mid_run(self):
        seen = []
        t0 = time.monotonic()
        with self.assertRaises(D.DiarizationCancelled):
            D._default_diarizer(
                self.req(source_audio=self.clip, requested_speakers=1,
                         segmentation_model=self.seg, embedding_model=self.emb),
                lambda p, t, s: seen.append(p), lambda: len(seen) >= 2)
        self.assertGreaterEqual(len(seen), 2, "要真的跑到中途才算測到中途取消")
        self.assertLess(time.monotonic() - t0, 30)


if __name__ == "__main__":
    unittest.main()
