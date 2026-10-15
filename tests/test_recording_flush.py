"""錄音存檔要逐 Ogg page 寫進磁碟，強停／當機時才不會整段遺失。

ffmpeg 7 起寫檔有 256 KiB 緩衝；32 kbps 的 Opus 要累積約 45 秒才落地。沒有
-flush_packets 1 時，SIGKILL（強停、os._exit、OOM）會讓短錄音變成 0 bytes。
macOS 驗收（兩段強停錄音的 SHA-256 都是空檔）與 Linux 重現結果相同。

驗證層級：Mock test（兩條錄音指令都帶旗標）＋Automated test（真的 ffmpeg 被 SIGKILL
之後檔案仍可解碼）。真 ffmpeg 的部分需要 ffmpeg（含 libopus）與 ffprobe，沒有就跳過。
"""
import math
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import capture as CAP
from core import transcribe as T
from tests.test_transcribe_dual import make_cfg

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
SR = 16000


def tone(seconds):
    return b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / SR)))
                    for i in range(int(seconds * SR)))


class RecordingCommandTests(unittest.TestCase):
    def test_single_source_recording_flushes_every_page(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        t = T.Transcriber(make_cfg(), tmp, "http://x")
        seen = {}

        class StopImmediately(Exception):
            pass

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            raise StopImmediately

        with mock.patch.object(T.subprocess, "Popen", fake_popen), \
                mock.patch.object(T.P, "resolve_source", return_value="mic0"):
            with self.assertRaises(StopImmediately):
                t.run()
        cmd = seen["cmd"]
        self.assertTrue(cmd[-1].endswith(".ogg"))
        self.assertEqual(cmd[-3:-1], ["-flush_packets", "1"],
                         "旗標必須套在錄音檔這個輸出上（ffmpeg 的輸出選項放在檔名前）")
        t.q.put(None)
        t.writer.close()

    def test_dual_source_track_encoder_flushes_every_page(self):
        seen = {}

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            return mock.Mock()

        CAP._Encoder(Path("track.ogg"), fake_popen, "mic")
        self.assertEqual(seen["cmd"][-3:], ["-flush_packets", "1", "track.ogg"])


@unittest.skipUnless(HAVE_FFMPEG, "需要 ffmpeg（含 libopus）與 ffprobe")
class SigkillSurvivalTests(unittest.TestCase):
    def test_sigkilled_encoder_still_leaves_a_decodable_recording(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "track.ogg"
        enc = CAP._Encoder(path, subprocess.Popen, "mic")
        self.addCleanup(lambda: enc.proc.poll() is None and enc.proc.kill())
        # ffmpeg 從 pipe 讀原始 PCM 要先累積約 3～6 秒才開始輸出，所以送 8 秒。
        # 8 秒約 32 KB 的 Opus，仍遠小於 256 KiB 的寫檔緩衝：沒有逐 page 落地就會是 0 bytes。
        enc.write(tone(8.0))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (not path.exists() or path.stat().st_size < 4096):
            time.sleep(0.05)
        enc.proc.kill()               # 模擬強停／當機：不關 stdin、不讓 ffmpeg 收尾
        enc.proc.wait()
        self.assertGreater(path.stat().st_size, 0, "SIGKILL 後存檔是空的：寫檔緩衝沒有逐 page 落地")
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        self.assertGreaterEqual(float(probe.stdout.strip() or 0), 1.0,
                                "被 SIGKILL 前已編碼的音訊要能解出來")
        check = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr[-300:])


if __name__ == "__main__":
    unittest.main()
