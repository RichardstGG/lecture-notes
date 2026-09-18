"""core/doctor.py 裡跟平台無關邏輯無關的兩個小工具函式（純檔案/subprocess 操作）。

doctor.run() 本身牽涉 config/servers 等 Codex 負責的模組，不在這裡整套模擬；
這裡只測試 read_lock() 與 git_head() 這兩個獨立、平台中立的小函式。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import doctor as DOC


class ReadLockTests(unittest.TestCase):
    def test_parses_key_value_lines_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "engines.lock"
            p.write_text("# 註解\nWHISPER_REF=abc123  # 說明文字\n\nLLAMA_REF=def456\n",
                         encoding="utf-8")
            refs = DOC.read_lock(p)
        self.assertEqual(refs, {"WHISPER_REF": "abc123", "LLAMA_REF": "def456"})

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(DOC.read_lock("/no/such/file.lock"), {})


class GitHeadTests(unittest.TestCase):
    def test_returns_none_when_not_a_repo(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(DOC.git_head(d))

    def test_returns_none_on_timeout(self):
        with mock.patch.object(DOC.subprocess, "run",
                                side_effect=DOC.subprocess.TimeoutExpired(cmd="git", timeout=5)):
            self.assertIsNone(DOC.git_head("/tmp"))

    def test_returns_stripped_sha(self):
        fake = mock.Mock(stdout="abcdef1234567890\n")
        with mock.patch.object(DOC.subprocess, "run", return_value=fake):
            self.assertEqual(DOC.git_head("/tmp"), "abcdef1234567890")


if __name__ == "__main__":
    unittest.main()
