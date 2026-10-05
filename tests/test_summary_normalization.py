"""Unicode policy parity and original-quote source span regressions."""
import json
import tempfile
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401
from core.summarize import Summarizer, bigrams, normalize_text
from core import terms
from ui.quality_eval import normalized


class NormalizationTests(unittest.TestCase):
    def test_core_terms_and_ui_agree_on_unicode_policy(self):
        cases = {
            'ＵＮＩＸ': 'unix', 'Ⅳ': 'iv', 'Straße': 'strasse',
            'ﬃ': 'ffi', 'Cafe\u0301': 'café', 'ＣＡＦÉ': 'café',
            'İ': 'i', '① ㍿': '1株式会社',
            'a\u0315\u0300': 'à', '각': '각',
            '半形 ｶﾞ': '半形ガ', 'x\u0301\u0323': 'x',
            'UNIX，檔案 描述子！': 'unix檔案描述子', '…': '',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                actual, spans = normalize_text(text)
                self.assertEqual(actual, expected)
                self.assertEqual(normalized(text), actual)
                self.assertEqual(terms.normalized(text), actual)
                self.assertEqual(len(spans), len(actual))
                self.assertTrue(all(0 <= a < b <= len(text) for a, b in spans))

    def test_expansions_and_compositions_preserve_original_spans(self):
        self.assertEqual(normalize_text('Ⅳßﬃ'),
                         ('ivssffi', [(0, 1)] * 2 + [(1, 2)] * 2 + [(2, 3)] * 3))
        self.assertEqual(normalize_text('e\u0301'), ('é', [(0, 2)]))
        self.assertEqual(normalize_text('각'), ('각', [(0, 3)]))
        self.assertEqual(normalize_text('ｶﾞ'), ('ガ', [(0, 2)]))
        # NFC can compose across an uncomposed lower-class mark. The canonical
        # decomposition mapping must not assume each output consumes a prefix.
        self.assertEqual(normalize_text('a\u0327\u0301'), ('á', [(0, 3)]))

    def test_verified_quote_keeps_original_spelling_and_timestamp(self):
        summary = Summarizer.__new__(Summarizer)
        summary.s = {'quote_match': 1.0, 'unverified_terms': 'drop'}
        for original, quote in [('ＵＮＩＸ Ⅳ', 'UNIX IV'), ('Straße', 'STRASSE'),
                                ('Cafe\u0301', 'Café'), ('각', '각'),
                                ('a\u0327\u0301', 'á')]:
            with self.subTest(original=original):
                body = f'`00:00:03` 開場。\n`00:01:23` {original}。後續說明。'
                data = {'emphasis': [{'quote': quote, 'note': '重點'}],
                        'terms': [{'term': quote, 'asr_original': '', 'explain': ''}]}
                report = summary._verify(data, body, '00:00:00')
                self.assertEqual(report, {'emphasis_dropped': [], 'terms_unverified': []})
                self.assertEqual(data['emphasis'][0]['quote'], original)
                self.assertEqual(data['emphasis'][0]['time'], '00:01:23')
                self.assertEqual(data['emphasis'][0]['match'], 1.0)
                self.assertTrue(data['terms'][0]['verified'])

    def test_empty_normalized_quote_is_dropped_even_with_zero_threshold(self):
        summary = Summarizer.__new__(Summarizer)
        summary.s = {'quote_match': 0, 'unverified_terms': 'drop'}
        data = {'emphasis': [{'quote': '…', 'note': ''}], 'terms': []}
        report = summary._verify(data, '`00:00:01` UNIX', '00:00:00')
        self.assertEqual(data['emphasis'], [])
        self.assertEqual(report['emphasis_dropped'], ['…'])

    def test_terms_deduplicate_width_variants_without_merging_distinct_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'notes.jsonl'
            path.write_text(json.dumps({'label': '00:00:00', 'terms': [
                {'term': term} for term in ['ＵＮＩＸ', 'UNIX', 'Multics', 'MUTIX']
            ]}), encoding='utf-8')
            candidates = terms.collect(path)
            self.assertEqual(len(candidates), 3)
            self.assertEqual(candidates[0]['count'], 2)
            self.assertEqual(terms.similarity('ＵＮＩＸ', 'UNIX'), 1.0)
            self.assertLess(terms.similarity('Multics', 'MUTIX'), 1.0)

    def test_public_bigrams_preserves_short_input_behavior(self):
        self.assertEqual(bigrams('unix'), {'un', 'ni', 'ix'})
        self.assertEqual(bigrams('u'), {'u'})
        self.assertEqual(bigrams(''), {''})
