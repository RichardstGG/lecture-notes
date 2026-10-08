"""core/doctor.py 裡的小工具函式，以及只轉錄模式（summary.enabled = false）的判斷。

doctor.run() 本身牽涉 config/servers 等 Codex 負責的模組，只用 --set 覆寫設定、
把引擎路徑指到空資料夾的方式跑一次，不模擬 server。
"""
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import doctor as DOC


class ParseGpuDevicesTests(unittest.TestCase):
    def test_metal_uses_mtl_prefix(self):
        out = "Available devices:\n  MTL0: Apple M3 Pro (36864 MiB, 36863 MiB free)\n"
        self.assertEqual(DOC.parse_gpu_devices(out), ["MTL0: Apple M3 Pro (36864 MiB, 36863 MiB free)"])

    def test_vulkan_and_cuda(self):
        out = "Vulkan0: Intel(R) Arc(TM) 140V\nCUDA0: NVIDIA RTX 4060 (8188 MiB)\n"
        self.assertEqual(len(DOC.parse_gpu_devices(out)), 2)

    def test_no_gpu_ignores_noise_and_cpu(self):
        out = "load_backend: loaded CPU backend\nAvailable devices:\nCPU0: Apple M3\n"
        self.assertEqual(DOC.parse_gpu_devices(out), [])


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

    def test_ignores_parent_repository(self):
        # 預編譯目錄放在這個 checkout 裡、自己沒有 .git。git -C 會走到上層 repo。
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=repo) as d:
            self.assertIsNone(DOC.git_head(d))

    def test_returns_none_on_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".git").mkdir()
            with mock.patch.object(DOC.subprocess, "run",
                                    side_effect=DOC.subprocess.TimeoutExpired(cmd="git", timeout=5)):
                self.assertIsNone(DOC.git_head(d))

    def test_returns_stripped_sha(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".git").mkdir()
            fake = mock.Mock(stdout="abcdef1234567890\n")
            with mock.patch.object(DOC.subprocess, "run", return_value=fake):
                self.assertEqual(DOC.git_head(d), "abcdef1234567890")


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

    def test_detail_names_the_external_upstream(self):
        text = DOC.summary_mode_detail(True, "qwen3-8b", "lab")
        self.assertIn("lab", text)
        self.assertIn("whisper", text)

    def test_missing_llama_is_warning_for_external_upstream(self):
        st, _, detail = DOC.missing_engine_item(
            "llama-server", "LLAMA_REF", "/x/llama-server", True, "lab")
        self.assertEqual(st, DOC.WARN)
        self.assertIn("lab", detail)
        self.assertNotIn("只轉錄", detail)

    def test_transcribe_only_message_wins_over_an_external_upstream(self):
        st, _, detail = DOC.missing_engine_item(
            "llama-server", "LLAMA_REF", "/x/llama-server", False, "lab")
        self.assertEqual(st, DOC.WARN)
        self.assertIn("只轉錄", detail)


class DoctorRunTranscribeOnlyTests(unittest.TestCase):
    """doctor.run() 整體：引擎與模型都不存在時，只轉錄模式不應因 llama 相關項目報錯。"""

    def _items(self, enabled, extra=(), upstream_text=None):
        with tempfile.TemporaryDirectory() as d:
            sets = [f"summary.enabled={'true' if enabled else 'false'}",
                    f"paths.whisper_dir='{Path(d).as_posix()}/w'",
                    f"paths.llama_dir='{Path(d).as_posix()}/l'",
                    f"paths.output_root='{Path(d).as_posix()}/out'",
                    f"paths.state_dir='{Path(d).as_posix()}/state'",
                    f"models.qwen3-8b.path='{Path(d).as_posix()}/none.gguf'",
                    # 隨便挑不會有人用的 port，避免撞到本機正在跑的 server
                    "whisper.port=1", "llm.port=2", *extra]
            upstreams = Path(d) / "upstreams.toml"
            if upstream_text is not None:
                upstreams.write_text(upstream_text, encoding="utf-8")
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(DOC.devices, "list_sources", return_value=[]))
                stack.enter_context(mock.patch.object(DOC.devices, "default_source", return_value=None))
                if upstream_text is not None:
                    stack.enter_context(mock.patch.object(DOC.C, "UPSTREAMS_FILE", upstreams))
                return DOC.run(None, sets)

    def _run(self, enabled):
        return {it["name"]: it["status"] for it in self._items(enabled)}

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

    def test_external_upstream_does_not_require_local_llama_or_gguf(self):
        text = '[upstreams.lab]\nname = "Lab GPU"\nbase_url = "https://example.test/v1"\nmodel = "qwen-lab"\n'
        items = self._items(True, extra=["summary.upstream=lab"], upstream_text=text)
        st = {it["name"]: it for it in items}
        self.assertEqual(st["llama-server"]["status"], DOC.WARN)
        self.assertIn("lab", st["llama-server"]["detail"])
        self.assertEqual(st["whisper-server"]["status"], DOC.FAIL)
        self.assertEqual(st["摘要上游"]["status"], DOC.OK)
        self.assertIn("Lab GPU", st["摘要上游"]["detail"])
        self.assertNotIn("example.test", st["摘要上游"]["detail"])
        llm = next(it for it in items if it["name"].startswith("LLM 模型"))
        self.assertEqual(llm["status"], DOC.WARN)
        self.assertIn("GGUF", llm["detail"])
        self.assertNotIn("port 2", st)
        self.assertIn("外部 API", st["總結"]["detail"])

    def test_missing_upstream_id_fails_without_requiring_llama(self):
        items = self._items(True, extra=["summary.upstream=lab"], upstream_text="[upstreams]\n")
        st = {it["name"]: it["status"] for it in items}
        self.assertEqual(st["摘要上游"], DOC.FAIL)
        self.assertEqual(st["llama-server"], DOC.WARN)
        self.assertEqual(st["whisper-server"], DOC.FAIL)

    def test_unreadable_upstreams_file_fails_closed(self):
        items = self._items(True, extra=["summary.upstream=lab"],
                            upstream_text='api_key = "test-key-value"\n[[[\n')
        upstream = next(it for it in items if it["name"] == "摘要上游")
        self.assertEqual(upstream["status"], DOC.FAIL)
        self.assertNotIn("test-key-value", upstream["detail"])

    def test_transcribe_only_ignores_a_selected_upstream(self):
        items = self._items(False, extra=["summary.upstream=lab"], upstream_text="[upstreams]\n")
        names = {it["name"] for it in items}
        st = {it["name"]: it for it in items}
        self.assertNotIn("摘要上游", names)
        self.assertEqual(st["llama-server"]["status"], DOC.WARN)
        self.assertIn("只轉錄", st["llama-server"]["detail"])


if __name__ == "__main__":
    unittest.main()
