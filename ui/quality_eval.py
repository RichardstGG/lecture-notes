"""Read-only quality snapshot for the public eight-minute sample.

Run with ``python -m ui.quality_eval SESSION_DIR`` from the repository root.
The scores are diagnostics, not a claim that generated notes are factually correct.
"""

import argparse
import hashlib
import json
import re
import tomllib
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples"
TERMS = (
    "Multics", "UNIX", "BSD", "inode", "fork", "exec", "PID", "POSIX",
    "檔案描述子", "管線", "一切皆檔案", "一個程式只做好一件事",
)
EMPHASIS = ("期中考一定會考", "大家把它抄下來")
SECTION = re.compile(r"^## (\d\d:\d\d:\d\d)$", re.MULTILINE)
SRT_CUE = re.compile(r"^(\d\d:\d\d:\d\d,\d{3}) --> (\d\d:\d\d:\d\d,\d{3})$",
                     re.MULTILINE)


def normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in value if char.isalnum())


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_entries(path):
    entries = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                entries[entry["label"]] = entry  # redo appends a newer result
    return list(entries.values())


def measure(directory, script):
    transcript_path = directory / "transcript.md"
    if not transcript_path.is_file():
        raise FileNotFoundError(f"Missing transcript: {transcript_path}")
    transcript = transcript_path.read_text(encoding="utf-8")
    srt_path = directory / "transcript.srt"
    cues = SRT_CUE.findall(srt_path.read_text(encoding="utf-8")) if srt_path.is_file() else []
    entries = read_entries(directory / "notes.jsonl")
    note_text = "\n".join(
        json.dumps({key: entry.get(key) for key in ("topic", "points", "terms")},
                   ensure_ascii=False)
        for entry in entries if entry.get("status") == "ok"
    )
    note_terms = {normalized(term.get("term", ""))
                  for entry in entries if entry.get("status") == "ok"
                  for term in entry.get("terms", [])}
    script_norm = normalized(script)
    transcript_norm = normalized(transcript)
    notes_norm = normalized(note_text)
    terms = {
        term: {"script": normalized(term) in script_norm,
               "transcript": normalized(term) in transcript_norm,
               "notes": normalized(term) in notes_norm,
               "note_term": normalized(term) in note_terms}
        for term in TERMS
    }
    unsupported_terms = sorted({
        term["term"] for entry in entries if entry.get("status") == "ok"
        for term in entry.get("terms", [])
        if term.get("verified") and normalized(term["term"]) not in script_norm
    })
    emphasis = {
        phrase: {"script": normalized(phrase) in script_norm,
                 "transcript": normalized(phrase) in transcript_norm,
                 "notes": normalized(phrase) in notes_norm}
        for phrase in EMPHASIS
    }
    return {
        "transcript_sha256": sha256(transcript_path),
        "notes_jsonl_sha256": (
            sha256(directory / "notes.jsonl")
            if (directory / "notes.jsonl").is_file() else None
        ),
        "srt_cues": len(cues),
        "srt_end": cues[-1][1] if cues else None,
        "sections": SECTION.findall(transcript),
        "summary_status": {status: sum(e.get("status") == status for e in entries)
                           for status in ("ok", "empty", "failed")},
        "summary_labels": [entry["label"] for entry in entries],
        "summary_seconds": round(sum(e.get("elapsed", 0) for e in entries), 1),
        "note_counts": {
            "points": sum(len(e.get("points", [])) for e in entries),
            "terms": sum(len(e.get("terms", [])) for e in entries),
            "emphasis": sum(len(e.get("emphasis", [])) for e in entries),
        },
        "verification": {
            "emphasis_dropped": sum(len(e.get("verify", {}).get("emphasis_dropped", []))
                                    for e in entries),
            "terms_unverified": sum(len(e.get("verify", {}).get("terms_unverified", []))
                                    for e in entries),
        },
        "terms": terms,
        "verified_note_terms_absent_from_script": unsupported_terms,
        "emphasis": emphasis,
    }


def evaluate(session, sample=SAMPLE):
    session = Path(session)
    sample = Path(sample)
    script_path = sample / "test8min.txt"
    script = script_path.read_text(encoding="utf-8")
    config_path = session / "config.used.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    prompt_path = Path(config["summary"]["prompt_file"]).expanduser()
    if not prompt_path.is_absolute():
        prompt_path = ROOT / prompt_path
    settings = {
        "whisper_model": config["whisper"]["model"],
        "whisper_threads": config["whisper"]["threads"],
        "whisper_language": config["whisper"]["language"],
        "whisper_prompt": config["whisper"]["prompt"],
        "whisper_terms": config["whisper"].get("terms", []),
        "summary_model": config["summary"]["model"],
        "summary_prompt_file": config["summary"]["prompt_file"],
        "summary_temperature": config["summary"]["temperature"],
        "summary_top_p": config["summary"]["top_p"],
        "summary_max_tokens": config["summary"]["max_tokens"],
        "summary_context_sections": config["summary"]["context_sections"],
        "summary_min_chars": config["summary"]["min_chars"],
        "summary_empty_chars": config["summary"]["empty_chars"],
        "summary_quote_match": config["summary"]["quote_match"],
        "summary_unverified_terms": config["summary"]["unverified_terms"],
        "summary_glossary": config["summary"].get("glossary", {}),
        "section_minutes": config["transcript"]["section_minutes"],
        "para_gap": config["transcript"]["para_gap"],
        "para_max": config["transcript"]["para_max"],
        "vad": config["vad"],
    }
    return {
        "sample_audio_sha256": sha256(sample / "test8min.ogg"),
        "script_sha256": sha256(script_path),
        "config_sha256": sha256(config_path),
        "summary_prompt_sha256": sha256(prompt_path) if prompt_path.is_file() else None,
        "settings": settings,
        "current": measure(session, script),
        "expected_reference": measure(sample / "expected", script),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare a sample session with the public reference")
    parser.add_argument(
        "session", type=Path,
        help="directory containing config.used.toml and transcript.md",
    )
    parser.add_argument("--sample", type=Path, default=SAMPLE,
                        help="directory containing test8min.txt, test8min.ogg and expected/")
    args = parser.parse_args(argv)
    print(json.dumps(evaluate(args.session, args.sample), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
