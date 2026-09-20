"""Contract tests for the per-course summary glossary."""
import copy
import tempfile
import tomllib
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401
from core.config import (DEFAULT_FILE, EXAMPLES_DIR, Config, _unknown_keys,
                         deep_merge, dump_toml, load_toml)
from core.summarize import Summarizer


class GlossaryConfigTests(unittest.TestCase):
    def default_data(self):
        return load_toml(DEFAULT_FILE)

    def test_glossary_normalizes_string_and_inline_table_entries(self):
        data = self.default_data()
        data["summary"]["glossary"] = {
            " Ritchie ": " C 語言作者 ",
            "Multics": {
                "means": " 分時系統 ",
                "aka": [" MUTIX ", "MUTIX", "Multics", "", "馬提克斯"],
            },
        }

        self.assertEqual(Config(data).glossary(), [
            {"term": "Ritchie", "means": "C 語言作者", "aka": []},
            {"term": "Multics", "means": "分時系統",
             "aka": ["MUTIX", "馬提克斯"]},
        ])

    def test_glossary_is_an_open_table_for_unknown_key_checks(self):
        default = self.default_data()
        override = {"summary": {"glossary": {
            "一個程式只做好一件事": {
                "means": "UNIX 設計哲學之一",
                "aka": ["一個城市只做好一件事"],
            },
        }}}
        self.assertEqual(_unknown_keys(default, override), [])

    def test_dump_toml_round_trip_preserves_glossary_structure(self):
        data = {"summary": {"glossary": {
            "含 空白的術語": {"means": "中文說明", "aka": ["錯字 一", "錯字二"]},
            "Ritchie": "C 語言作者",
        }}}
        self.assertEqual(tomllib.loads(dump_toml(data)), data)

    def test_validate_reports_bad_glossary_values_without_raising(self):
        cases = {
            "number": 7,
            "aka_string": {"means": "說明", "aka": "MUTIX"},
            "aka_non_string": {"means": "說明", "aka": [1]},
            "means_non_string": {"means": 1, "aka": []},
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                data = self.default_data()
                data["summary"]["glossary"] = {"Multics": value}
                errors = Config(data).validate()
                self.assertTrue(any("summary.glossary.Multics 應為字串" in e
                                    for e in errors), errors)

        data = self.default_data()
        data["summary"]["glossary"] = 7
        self.assertIn("summary.glossary 應為 TOML 表格", Config(data).validate())

    def test_deep_merge_keeps_default_compatibility_when_glossary_is_absent(self):
        default = self.default_data()
        old_course = {"course": {"name": "舊課程"}, "summary": {"model": "qwen3-8b"}}
        merged = deep_merge(default, old_course)
        self.assertEqual(merged["summary"]["glossary"], {})
        self.assertEqual(Config(merged).glossary(), [])

    def test_public_example_uses_neutral_test_course_identity(self):
        example_path = EXAMPLES_DIR / "example.toml"
        data = load_toml(example_path)

        self.assertTrue(example_path.is_file())
        self.assertEqual(data["course"]["name"], "測試課")
        self.assertIn("Multics", data["summary"]["glossary"])


class GlossaryPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def summarizer(self, prompt, glossary=None):
        prompt_path = self.root / f"prompt-{len(list(self.root.glob('prompt-*')))}.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        data = copy.deepcopy(load_toml(DEFAULT_FILE))
        data["course"]["name"] = "測試課"
        data["system"]["opencc"] = False
        data["summary"]["prompt_file"] = str(prompt_path)
        data["summary"]["glossary"] = glossary or {}
        return Summarizer(
            Config(data), self.root, "http://127.0.0.1:1",
            {"name": "test", "path": self.root / "model.gguf",
             "disable_thinking": True},
        )

    def test_prompt_contains_glossary_rules_and_values_once(self):
        summarizer = self.summarizer(
            "課程：{course}\n{terms}{glossary}{extra_instructions}",
            {"Multics": {"means": "分時作業系統專案",
                         "aka": ["MUTIX", "馬提克斯"]}},
        )
        prompt = summarizer.system_prompt
        self.assertEqual(prompt.count("# 本課程術語對照表"), 1)
        self.assertIn("- Multics｜可能被聽成：MUTIX、馬提克斯｜是什麼：分時作業系統專案", prompt)
        self.assertIn("不可寫進 explain、points 或任何欄位", prompt)

    def test_empty_glossary_leaves_prompt_unchanged(self):
        with_placeholder = self.summarizer(
            "課程：{course}\n{terms}\n{glossary}# 其他\n{extra_instructions}",
        ).system_prompt
        without_placeholder = self.summarizer(
            "課程：{course}\n{terms}\n# 其他\n{extra_instructions}",
        ).system_prompt
        self.assertEqual(with_placeholder, without_placeholder)
        self.assertNotIn("術語對照表", with_placeholder)

    def test_custom_prompt_without_placeholder_appends_glossary(self):
        prompt = self.summarizer(
            "課程：{course}\n{terms}{extra_instructions}",
            {"Ritchie": "C 語言作者，UNIX 共同開發者"},
        ).system_prompt
        self.assertTrue(prompt.endswith(
            "- Ritchie｜是什麼：C 語言作者，UNIX 共同開發者\n"
        ))
        self.assertEqual(prompt.count("# 本課程術語對照表"), 1)

    def test_verify_accepts_asr_original_from_transcript(self):
        summarizer = self.summarizer("{course}\n{glossary}")
        data = {
            "topic": "Multics",
            "points": [],
            "terms": [{"term": "Multics", "explain": "", "asr_original": "MUTIX"}],
            "emphasis": [],
        }
        report = summarizer._verify(data, "老師提到 MUTIX 專案。", "00:00:00")
        self.assertTrue(data["terms"][0]["verified"])
        self.assertEqual(report["terms_unverified"], [])


if __name__ == "__main__":
    unittest.main()
