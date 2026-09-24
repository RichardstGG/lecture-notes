# Public sample quality comparison

This procedure uses the fictional eight-minute class in `samples/`. Run it from
the repository root on a machine with the configured whisper and LLM models.
Keep generated sessions and reports outside Git. The script reads files only.

```bash
git rev-parse HEAD
git -C whisper.cpp rev-parse HEAD
git -C llama.cpp rev-parse HEAD
./lec models
/usr/bin/time -p ./lec run courses/examples/example.toml \
  --file samples/test8min.ogg \
  --set paths.output_root=/tmp/lecture-notes-quality-baseline \
  --set system.inhibit_sleep=false
session_dir="$(find /tmp/lecture-notes-quality-baseline -mindepth 1 -maxdepth 1 \
  -type d -name '測試課_*' | sort | tail -n 1)"
python3 -m ui.quality_eval "$session_dir" \
  > /tmp/lecture-notes-quality-baseline/report.json
```

The run prints `<session-name>`. On systems where `/tmp` is unsuitable, choose
another local output directory. The session retains `config.used.toml`,
`transcript.md`, `transcript.srt`, `notes.jsonl`, `notes.md`, status and logs. The
JSON report includes hashes of the audio, script, effective config, prompt and
transcript; Whisper and summary settings; section labels; summary statuses and
model response time; and the same measurements for `samples/expected/`. Record
the three Git revisions and the `/usr/bin/time` result with the report. Engine
binaries may be built from different revisions than their current checkouts, so
verify their build provenance separately if an exact engine comparison matters.
The report format is `schema_version: 2`. Compared with version 1, curated term
results add occurrence counts, and `--baseline` adds optional comparison fields.
The script count is a reading guide because the recording may depart from its
written script.

`audio_matches_sample` checks the session's `status.json` input against the
public audio. It is `null` when that source file or status is unavailable.

For two runs of the same sample, pass the earlier session with `--baseline`:

```bash
control_session="$(find /tmp/control -mindepth 1 -maxdepth 1 \
  -type d -name '測試課_*' | sort | tail -n 1)"
candidate_session="$(find /tmp/candidate -mindepth 1 -maxdepth 1 \
  -type d -name '測試課_*' | sort | tail -n 1)"
python3 -m ui.quality_eval "$candidate_session" \
  --baseline "$control_session" > /tmp/sample-comparison.json
```

The optional `baseline` measurements use the same fields as `current`.
`settings_delta` lists changed quality settings, while `term_changes` lists only
curated terms whose transcript or note-term occurrence count changed. The
baseline config hash and `baseline_audio_matches_sample` are also included.
Output and state directory differences are excluded from `settings_delta`; check
both `config.used.toml` files and model provenance before claiming a controlled
experiment. The prompt hash reflects the file at report time; a prior run does
not store a prompt snapshot. The curated list now includes `父行程` and `子行程`
so the sample's process terminology can be compared directly.

## Read the report in four passes

1. **Transcription:** Compare `terms[*].transcript` with `terms[*].script` and
   inspect the surrounding lines in `transcript.md`. The script is a reading
   guide, not a verbatim audio annotation; exact word error rate would be
   misleading. Note the wrong ASR spelling and timestamp before changing a
   Whisper term or prompt.
2. **Terminology correction:** Compare `terms[*].note_term` with the transcript
   result. A canonical term in notes when the transcript has only a mistaken
   spelling suggests that the glossary helped. Examine
   `verified_note_terms_absent_from_script` manually: the current verifier can
   accept a mistaken ASR word merely because it appears in the transcript.
   This list is a review queue, not a hallucination verdict.
3. **Segmentation and summary:** Compare `sections`, `summary_labels`,
   `summary_status`, `summary_seconds` and `verification` with the reference.
   Read both `notes.jsonl` files for dropped emphasis, unverified terms, and
   missing coverage. The reference has two sections but is not a fixed target.
4. **Note quality:** Check whether points preserve the teacher's explanations,
   whether emphasis quotes actually contain an explicit cue, and whether every
   claim is supported by that section's transcript. Check the two scripted
   emphasis phrases and inspect paraphrases where exact matching fails. Record
   omissions, unsupported claims and factual reversals with section timestamps.

The report makes only presence and count measurements. It cannot judge whether a
point is correct, whether an `explain` field adds outside knowledge, or whether
the speaker actually uttered the script's exact words. Do not use it as an
automated pass/fail gate. `samples/expected/` is a historical run, not ground
truth. For an A/B comparison, keep the audio, model binaries and all settings
constant except one proposed change, run each condition into a separate output
directory, and compare both reports plus the notes manually. Repeat stochastic
summary runs before attributing a small difference to a prompt change.
