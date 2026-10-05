"""Read-only term candidate derivation from saved summary records."""
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from .summarize import bigrams, normalize_text


def normalized(value):
    return normalize_text(str(value))[0]


def collect(notes_jsonl):
    path = Path(notes_jsonl)
    entries = {}
    if not path.is_file():
        return []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            label = entry["label"]
            if isinstance(label, str) and isinstance(entry, dict):
                entries[label] = entry
        except (ValueError, KeyError, TypeError):
            continue
    found = {}
    for label, entry in entries.items():
        terms = entry.get("terms", [])
        if not isinstance(terms, list):
            continue
        for item in terms:
            if not isinstance(item, dict) or not isinstance(item.get("term"), str):
                continue
            term = item["term"].strip()
            key = normalized(term)
            if not key:
                continue
            candidate = found.setdefault(key, {
                "term": term, "count": 0, "sections": [], "explain": "",
                "asr_original": "", "verified": True,
            })
            candidate["count"] += 1
            if label not in candidate["sections"]:
                candidate["sections"].append(label)
            explain = item.get("explain", "")
            if isinstance(explain, str) and len(explain) > len(candidate["explain"]):
                candidate["explain"] = explain
            original = item.get("asr_original", "")
            if isinstance(original, str) and original and not candidate["asr_original"]:
                candidate["asr_original"] = original
            candidate["verified"] = candidate["verified"] and item.get("verified", True) is not False
    return list(found.values())


def exclude(candidates, cfg, ignored=None):
    known = {normalized(value) for value in (ignored or [])}
    known.update(normalized(value) for value in cfg["whisper"].get("terms", []))
    for entry in cfg.glossary():
        known.add(normalized(entry["term"]))
        known.update(normalized(value) for value in entry["aka"])
    return [item for item in candidates if normalized(item["term"]) not in known]


def similarity(a, b):
    a, b = normalized(a), normalized(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if (a in b or b in a) and min(len(a), len(b)) / max(len(a), len(b)) >= 0.6:
        return min(len(a), len(b)) / max(len(a), len(b))
    if len(a) == len(b) == 2 and a[0] == b[0] and {a[1], b[1]} == {"線", "纖"}:
        return 0.5
    aa, bb = bigrams(a), bigrams(b)
    score = len(aa & bb) / len(aa | bb)
    # Require stronger character overlap for longer Chinese terms. A single
    # common character makes short unrelated words look deceptively similar.
    if re.search(r"[\u3400-\u9fff]", a) and re.search(r"[\u3400-\u9fff]", b):
        overlap = SequenceMatcher(None, a, b).ratio()
        if overlap >= 0.75:
            score = max(score, overlap)
    return score


def _flags(item):
    result = []
    if not item["explain"].strip() or re.search(r"可能|或許|未明確|不確定", item["explain"]):
        result.append("hedged")
    if item["asr_original"]:
        result.append("asr_corrected")
    if not item["verified"]:
        result.append("unverified")
    return result


def cluster(candidates, threshold=0.5):
    if not 0 <= threshold <= 1:
        raise ValueError("similarity must be between 0 and 1")
    # Greedy representative matching avoids transitive chains joining unrelated words.
    ordered = sorted(candidates, key=lambda x: (-x["count"], x["term"]))
    groups = []
    for item in ordered:
        match = next((group for group in groups
                      if similarity(item["term"], group["term"]) >= threshold), None)
        if match is None:
            groups.append({**item, "flags": _flags(item), "variants": []})
        else:
            match["variants"].append({"term": item["term"], "count": item["count"],
                                      "sections": item["sections"],
                                      "similarity": round(similarity(item["term"], match["term"]), 3)})
            match["count"] += item["count"]
            match["sections"] = sorted(set(match["sections"] + item["sections"]))
            match["flags"] = list(dict.fromkeys(match["flags"] + _flags(item)))
    for group in groups:
        if group["variants"]:
            group["flags"].append("variant_group")
    return groups


def rank(groups):
    return sorted(groups, key=lambda item: (
        -bool(item["variants"]), -len(item["sections"]), -item["count"],
        -int("hedged" in item["flags"]), item["term"],
    ))
