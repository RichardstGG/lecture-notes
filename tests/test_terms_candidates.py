"""Saved-note term candidate derivation contract."""
import json
import tempfile
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401
from core.terms import collect, exclude, cluster, rank, similarity


class FakeConfig:
    def __getitem__(self, key):
        return {"whisper": {"terms": ["UNIX"]}}[key]

    def glossary(self):
        return [{"term": "光纖", "means": "", "aka": ["光線"]}]


class TermTests(unittest.TestCase):
    def test_collect_last_label_and_bad_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.jsonl"
            path.write_text("\n".join([
                json.dumps({"label": "00:00:00", "terms": [{"term": "old"}]}),
                "broken",
                json.dumps({"label": "00:00:00", "terms": [{"term": "光線", "explain": "可能指光纖"}]}),
                json.dumps({"label": "00:05:00", "terms": [{"term": "光 線", "explain": "", "verified": False}]}),
            ]), encoding="utf-8")
            result = collect(path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["count"], 2)
            self.assertEqual(result[0]["sections"], ["00:00:00", "00:05:00"])
            self.assertEqual(result[0]["explain"], "可能指光纖")
            self.assertFalse(result[0]["verified"])
            self.assertEqual(collect(Path(directory) / "missing"), [])

    def test_exclude_and_group(self):
        items = [{"term": term, "count": count, "sections": ["00:00:00"],
                  "explain": "", "asr_original": "", "verified": True}
                 for term, count in [("UNIX", 1), ("光線", 4), ("光纖", 3),
                                     ("電訊號", 2), ("電的訊號", 1), ("小考", 1)]]
        self.assertEqual([x["term"] for x in exclude(items, FakeConfig(), ["小考"])],
                         ["電訊號", "電的訊號"])
        groups = rank(cluster(items[1:5]))
        self.assertEqual(len(groups), 2)
        self.assertIn("variant_group", groups[0]["flags"])
        self.assertEqual(sum(x["count"] for x in groups), 10)
        self.assertRaises(ValueError, cluster, items, 1.1)
        self.assertLess(similarity("POSIX", "processID"), 0.5)
        self.assertLess(similarity("BID", "iNode"), 0.5)


if __name__ == "__main__":
    unittest.main()
