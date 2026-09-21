"""core/doctor.py 裡的小工具函式，以及只轉錄模式（summary.enabled = false）的判斷。

doctor.run() 本身牽涉 config/servers 等 Codex 負責的模組，只用 --set 覆寫設定、
把引擎路徑指到空資料夾的方式跑一次，不模擬 server。
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


class SummaryModeTests(unittest.TestCase):
    def test_detail_mentions_model_when_on(self):
        self.assertIn("qwen3-8b", DOC.summary_mode_detail(True, "qwen3-8b"))

    def test_detail_mentions_transcribe_only_when_off(self):
        self.assertIn("只轉錄", DOC.summary_mode_detail(False, "qwen3-8b"))

    def test_missing_llama_is_warning_when_summary_off(self):
        st, name, detail = DOC.missing_engine_item("llama-server", "LLAMA_REF", "/x/llama-server", False)
        self.assertEqual((st, name), (DOC.WARN, "llama-server"))
        self.assertIn("只轉錄", detail)

    def test_missing_llama_is_failure_when_summary_on(self):
        st, _, _ = DOC.missing_engine_item("llama-server", "LLAMA_REF", "/x/llama-server", True)
        self.assertEqual(st, DOC.FAIL)

    def test_missing_whisper_is_always_failure(self):
        for on in (True, False):
            st, _, _ = DOC.missing_engine_item("whisper-server", "WHISPER_REF", "/x/whisper-server", on)
            self.assertEqual(st, DOC.FAIL)


class DoctorRunTranscribeOnlyTests(unittest.TestCase):
    """doctor.run() 整體：引擎與模型都不存在時，只轉錄模式不應因 llama 相關項目報錯。"""

    def _run(self, enabled):
        with tempfile.TemporaryDirectory() as d:
            sets = [f"summary.enabled={'true' if enabled else 'false'}",
                    f"paths.whisper_dir='{Path(d).as_posix()}/w'",
                    f"paths.llama_dir='{Path(d).as_posix()}/l'",
                    f"paths.output_root='{Path(d).as_posix()}/out'",
                    f"paths.state_dir='{Path(d).as_posix()}/state'",
                    f"models.qwen3-8b.path='{Path(d).as_posix()}/none.gguf'",
                    # 隨便挑不會有人用的 port，避免撞到本機正在跑的 server
                    "whisper.port=1", "llm.port=2"]
            with mock.patch.object(DOC.devices, "list_sources", return_value=[]), \
                    mock.patch.object(DOC.devices, "default_source", return_value=None):
                items = DOC.run(None, sets)
        return {it["name"]: it["status"] for it in items}

    def test_summary_off_llama_items_are_not_failures(self):
        st = self._run(False)
        self.assertEqual(st["llama-server"], DOC.WARN)
        llm = [v for k, v in st.items() if k.startswith("LLM 模型")]
        self.assertEqual(llm, [DOC.WARN])
        self.assertNotIn("port 2", st)          # 不檢查 llama-server 的 port
        self.assertEqual(st["whisper-server"], DOC.FAIL)   # whisper 仍是必要的

    def test_summary_on_llama_items_are_failures(self):
        st = self._run(True)
        self.assertEqual(st["llama-server"], DOC.FAIL)
        self.assertIn("port 2", st)


if __name__ == "__main__":
    unittest.main()
