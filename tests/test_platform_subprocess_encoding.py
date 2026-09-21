"""子行程的文字編碼一律用 UTF-8，不依賴作業系統的 locale。

迴歸：Windows（繁體中文）的 locale 編碼是 cp950。subprocess 用 text=True 又沒指定
encoding 時會用 cp950 編碼 stdin／解碼 stdout。whisper 輸出的簡體字（例如「扩」
U+6269）不在 cp950 裡，送進 opencc 時寫入執行緒丟出 UnicodeEncodeError、stdin
沒有關閉，opencc 一直等到 20 秒逾時，那一段逐字稿就沒轉成繁體。
opencc、ffmpeg、whisper/llama-server 的輸出本來就是 UTF-8，所以統一指定 UTF-8。
"""
import re
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import platform as P
from core import util

ROOT = Path(__file__).resolve().parent.parent
CHECKED = ["core/util.py", "core/platform.py", "core/devices.py", "core/doctor.py",
           "core/servers.py", "setup_engines.py"]


class OpenccEncodingTests(unittest.TestCase):
    def test_opencc_pipes_use_utf8(self):
        fake = mock.Mock(stdout="擴充\n")
        with mock.patch.object(util.shutil, "which", return_value="opencc"), \
                mock.patch.object(util.subprocess, "run", return_value=fake) as run:
            self.assertEqual(util.opencc_convert(["扩充"]), ["擴充"])
        kw = run.call_args.kwargs
        self.assertEqual(kw.get("encoding"), "utf-8")
        self.assertEqual(kw.get("input"), "扩充")

    def test_text_outside_cp950_is_encodable_as_utf8(self):
        # 確認這個情境真的會在 cp950 失敗、在 UTF-8 不會（測的是前提，不是程式）
        with self.assertRaises(UnicodeEncodeError):
            "扩".encode("cp950")
        "扩".encode("utf-8")


class PlatformRunEncodingTests(unittest.TestCase):
    def test_device_listing_decodes_as_utf8(self):
        fake = mock.Mock(stdout="", stderr='"麥克風陣列"')
        with mock.patch.object(P.subprocess, "run", return_value=fake) as run, \
                mock.patch.object(P, "IS_WINDOWS", False):
            P._run(["ffmpeg"])
        self.assertEqual(run.call_args.kwargs.get("encoding"), "utf-8")


class NoLocaleDependentTextModeTests(unittest.TestCase):
    """靜態檢查：這些檔案裡的 subprocess 呼叫只要用 text=True，就必須同時指定 encoding。"""

    def test_every_text_mode_call_sets_encoding(self):
        bad = []
        for rel in CHECKED:
            src = (ROOT / rel).read_text(encoding="utf-8")
            for m in re.finditer(r"subprocess\.run\((?:[^()]|\([^()]*\))*\)", src):
                call = m.group(0)
                if "text=True" in call and "encoding=" not in call:
                    line = src[:m.start()].count("\n") + 1
                    bad.append(f"{rel}:{line}")
        self.assertEqual(bad, [], "text=True 但沒有指定 encoding（Windows 會用 cp950）")


if __name__ == "__main__":
    unittest.main()
