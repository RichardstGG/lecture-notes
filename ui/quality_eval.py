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
    "檔案描述子", "父行程", "子行程", "管線", "一切皆檔案", "一個程式只做好一件事",
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


def audio_matches_sample(session, sample_audio, sample_hash):
    """Return None when a session's input audio can no longer be checked."""
    status_path = session / "status.json"
    if not status_path.is_file():
        return None
    status = json.loads(status_path.read_text(encoding="utf-8"))
    input_file = status.get("input_file")
    if not input_file:
        return None
    source = Path(input_file)
    if not source.is_file():
        return None
    if source.stat().st_size != sample_audio.stat().st_size:
        return False
    return sha256(source) == sample_hash


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
    note_terms = [normalized(term.get("term", ""))
                  for entry in entries if entry.get("status") == "ok"
                  for term in entry.get("terms", [])]
    script_norm = normalized(script)
    transcript_norm = normalized(transcript)
    notes_norm = normalized(note_text)
    terms = {
        term: {"script": normalized(term) in script_norm,
               "script_count": script_norm.count(normalized(term)),
               "transcript": normalized(term) in transcript_norm,
               "transcript_count": transcript_norm.count(normalized(term)),
               "notes": normalized(term) in notes_norm,
               "note_term": normalized(term) in note_terms,
               "note_term_count": note_terms.count(normalized(term))}
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


def quality_settings(config):
    """Select settings that may change transcription or summary quality."""
    return {
        "opencc": config["system"]["opencc"],
        "whisper_model": config["whisper"]["model"],
        "whisper_model_path": config["whisper"]["models"][config["whisper"]["model"]]["path"],
        "whisper_threads": config["whisper"]["threads"],
        "whisper_language": config["whisper"]["language"],
        "whisper_prompt": config["whisper"]["prompt"],
        "whisper_terms": config["whisper"].get("terms", []),
        "summary_enabled": config["summary"]["enabled"],
        "summary_model": config["summary"]["model"],
        "summary_model_path": config["models"][config["summary"]["model"]]["path"],
        "summary_disable_thinking": config["models"][config["summary"]["model"]].get(
            "disable_thinking", False,
        ),
        "summary_file_mode": config["summary"]["file_mode"],
        "summary_prompt_file": config["summary"]["prompt_file"],
        "summary_extra_instructions": config["summary"]["extra_instructions"],
        "summary_temperature": config["summary"]["temperature"],
        "summary_top_p": config["summary"]["top_p"],
        "summary_max_tokens": config["summary"]["max_tokens"],
        "summary_context_sections": config["summary"]["context_sections"],
        "summary_min_chars": config["summary"]["min_chars"],
        "summary_empty_chars": config["summary"]["empty_chars"],
        "summary_quote_match": config["summary"]["quote_match"],
        "summary_unverified_terms": config["summary"]["unverified_terms"],
        "summary_glossary": config["summary"].get("glossary", {}),
        "llm_ctx": config["llm"]["ctx"],
        "llm_extra_args": config["llm"]["extra_args"],
        "section_minutes": config["transcript"]["section_minutes"],
        "para_gap": config["transcript"]["para_gap"],
        "para_max": config["transcript"]["para_max"],
        "vad": config["vad"],
    }


def evaluate(session, sample=SAMPLE, baseline=None):
    session = Path(session)
    sample = Path(sample)
    script_path = sample / "test8min.txt"
    script = script_path.read_text(encoding="utf-8")
    sample_audio = sample / "test8min.ogg"
    sample_hash = sha256(sample_audio)
    config_path = session / "config.used.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    prompt_path = Path(config["summary"]["prompt_file"]).expanduser()
    if not prompt_path.is_absolute():
        prompt_path = ROOT / prompt_path
    settings = quality_settings(config)
    report = {
        "schema_version": 2,
        "sample_audio_sha256": sample_hash,
        "audio_matches_sample": audio_matches_sample(session, sample_audio, sample_hash),
        "script_sha256": sha256(script_path),
        "config_sha256": sha256(config_path),
        "summary_prompt_sha256": sha256(prompt_path) if prompt_path.is_file() else None,
        "settings": settings,
        "current": measure(session, script),
        "expected_reference": measure(sample / "expected", script),
    }
    if baseline is not None:
        baseline = Path(baseline)
        baseline_config_path = baseline / "config.used.toml"
        baseline_config = tomllib.loads(baseline_config_path.read_text(encoding="utf-8"))
        baseline_settings = quality_settings(baseline_config)
        baseline_result = measure(baseline, script)
        current_terms = report["current"]["terms"]
        baseline_terms = baseline_result["terms"]
        report["baseline"] = baseline_result
        report["baseline_config_sha256"] = sha256(baseline_config_path)
        report["baseline_audio_matches_sample"] = audio_matches_sample(
            baseline, sample_audio, sample_hash,
        )
        report["settings_delta"] = {
            key: {"baseline": baseline_settings[key], "current": value}
            for key, value in settings.items() if baseline_settings[key] != value
        }
        report["term_changes"] = {
            term: {
                "baseline_transcript": baseline_terms[term]["transcript"],
                "current_transcript": current_terms[term]["transcript"],
                "baseline_transcript_count": baseline_terms[term]["transcript_count"],
                "current_transcript_count": current_terms[term]["transcript_count"],
                "baseline_note_term": baseline_terms[term]["note_term"],
                "current_note_term": current_terms[term]["note_term"],
                "baseline_note_term_count": baseline_terms[term]["note_term_count"],
                "current_note_term_count": current_terms[term]["note_term_count"],
            }
            for term in TERMS
            if (baseline_terms[term]["transcript_count"] != current_terms[term]["transcript_count"]
                or baseline_terms[term]["note_term_count"] != current_terms[term]["note_term_count"])
        }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare a sample session with the public reference")
    parser.add_argument(
        "session", type=Path,
        help="directory containing config.used.toml and transcript.md",
    )
    parser.add_argument("--sample", type=Path, default=SAMPLE,
                        help="directory containing test8min.txt, test8min.ogg and expected/")
    parser.add_argument("--baseline", type=Path,
                        help="another session directory for a direct A/B comparison")
    args = parser.parse_args(argv)
    print(json.dumps(evaluate(args.session, args.sample, args.baseline),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
