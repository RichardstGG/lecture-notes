"""Gap diagnostics must not become LLM input or summary content."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core.config import Config, DEFAULT_FILE, load_toml
from core.summarize import Section, Summarizer
from core.transcribe import GAP_MARK


class SummaryGapTests(unittest.TestCase):
    def test_prep_filters_only_gap_lines_and_preserves_speech(self):
        body = f'`00:00:01` UNIX 系統\n{GAP_MARK}，00:00:02–00:00:10（timeout）\n> 引述\n`00:00:11` BSD'
        clean = Summarizer._prep_body([Section('00:00:00', 0, body)])
        self.assertEqual(clean, '`00:00:01` UNIX 系統\n> 引述\n`00:00:11` BSD')

    def test_gap_only_block_is_empty_without_llm_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_toml(DEFAULT_FILE)
            data['system']['opencc'] = False
            summary = Summarizer(Config(data), Path(tmp), 'http://unused',
                                 {'name': 'mock', 'disable_thinking': True})
            block = [Section('00:00:00', 0, f'{GAP_MARK}，' + '錯誤' * 100)]
            with (patch.object(summary, 'call_llm') as llm,
                  patch.object(summary, 'warmup') as warmup,
                  patch.object(summary, '_save') as save):
                summary.process(block, block)
            llm.assert_not_called()
            warmup.assert_not_called()
            self.assertEqual(save.call_args.args[0]['status'], 'empty')
            self.assertEqual(save.call_args.args[0]['chars'], 0)

    def test_mixed_block_prompt_does_not_contain_gap_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_toml(DEFAULT_FILE)
            data['system']['opencc'] = False
            summary = Summarizer(Config(data), Path(tmp), 'http://unused',
                                 {'name': 'mock', 'disable_thinking': True})
            body = '`00:00:01` ' + 'UNIX 系統與管線。' * 50
            block = [Section('00:00:00', 0, body + f'\n{GAP_MARK}，secret diagnostic')]
            with (patch.object(summary, 'warmup'),
                  patch.object(summary, 'call_llm', side_effect=RuntimeError('test')) as llm,
                  patch.object(summary, '_save')):
                summary.process(block, block)
            prompt = llm.call_args.args[0]
            self.assertIn('UNIX 系統', prompt)
            self.assertNotIn('secret diagnostic', prompt)
            self.assertNotIn(GAP_MARK, prompt)
