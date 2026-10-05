# UI state contract

This document defines the filesystem contract that a local UI may consume without
importing Python modules from `core`. The current schema version is `1`. A JSON
object with no `schema_version` is a legacy version `0` object and must be read
defensively.

The producer writes JSON snapshots atomically. `events.jsonl` is append-only; a
consumer should tolerate an incomplete final line while the producer is writing.
Unknown fields and unknown event types must be ignored for forward compatibility.

## `run.json`

Location: the configured state directory, normally
`~/.local/state/lecture-notes/run.json`.

It records the single active `lec run` or `lec summarize` process:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | `1` for newly written records |
| `pid` | integer | Owner process ID |
| `started_at` | ISO 8601 string | Process start time |
| `course` | string or null | Course identifier |
| `session` | string or null | Absolute session directory after it is known |
| `mode` | string | `live`, `file`, or `summarize`; legacy records normalize to `run` |

The file exists only while the process owns the run lock. Its presence alone is
not proof that the process is alive; `lec status --json` performs that check.
When idle, that command returns:

```json
{"schema_version": 1, "running": false}
```

When active it returns the normalized `run.json` fields at the top level, adds
`"running": true`, and includes the session snapshot as `status` when available.

## `status.json`

Location: `<session>/status.json` for `lec run` sessions. The file is refreshed at
`system.status_interval` even when values have not changed, so `updated_at` is a
heartbeat as well as a modification timestamp.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Snapshot schema, currently `1` |
| `phase` | string | Current lifecycle phase |
| `pid` | integer | Producer process ID |
| `started_at`, `updated_at` | ISO 8601 string | Start and latest heartbeat times |
| `course` | string or null | Course identifier |
| `session` | string | Absolute session directory |
| `mode` | string or null | `live` or `file` for current producers |
| `input_file` | string | Absolute input path in file mode, otherwise empty |
| `summary_model` | string or null | Configured model when summarization is enabled |
| `elapsed`, `transcribed` | number | Audio seconds observed and transcribed |
| `transcribe_lag` | number or null | Live transcription lag in seconds |
| `queue` | integer | Pending transcription chunks |
| `sections_total`, `sections_summarized` | integer | Transcript and summary progress |
| `llm_busy` | boolean | Whether one summary request is running |
| `llm_section` | string or null | Section currently being summarized |
| `servers` | object | `whisper` and `llama` lifecycle states |
| `errors` | integer | Errors observed during this session |
| `last_error` | string or null | Most recent error message |

Known phases are `starting`, `loading`, `recording`, `transcribing`,
`summarizing`, `finishing`, `done`, `failed`, and `aborted`. Consumers must still
handle unknown phases. Known server states are `not_started`, `loading`, `ok`,
`failed`, and `stopped`.

`lec summarize` currently participates in `run.json` locking with mode
`summarize`, but does not create or refresh `status.json` or `events.jsonl`.

## `events.jsonl`

Location: `<session>/events.jsonl` for `lec run` sessions. Each complete line is a
JSON object with this envelope:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Event schema, currently `1` |
| `seq` | integer | Monotonically increasing sequence within the file |
| `time` | ISO 8601 string | Event time |
| `type` | string | Event discriminator |

Known event payloads are:

- `phase`: `phase`
- `summary`: `label`, `status`, `topic`, `elapsed`
- `error`: `message`
- `stop_requested`: no additional fields

Legacy lines without `schema_version` or `seq` remain valid input. When appending
to an existing file, the producer continues after both the number of valid legacy
records and the highest existing sequence number.

## Transcription failures and gaps

`lec run` now returns exit code `1` and ends with `phase="failed"` when
`Transcriber.run()` reports `ffmpeg_failed=true` and `duration=0`. Previously this
case incorrectly returned `0` / `done`. File-mode summarization is skipped and a
live summary worker is stopped. This is an intentional compatibility correction
for automation consuming the exit code or phase; no new phase is introduced.
A zero-length result without ffmpeg failure retains its existing success behavior.
A partial recording, including recoverable transcription gaps, still finishes
with exit `0` / `done` unless another fatal session error occurs.

When gaps are reported, the session appends one `transcription_gaps` event using
the existing schema-1 envelope (`schema_version`, `seq`, `time`, `type`). Its
`gaps` array contains `{start, seconds, reason}` entries from the transcriber;
`lost_seconds` is the aggregate lost audio duration, and `ffmpeg_returncode` is
the decoder return code (or null when unavailable). Existing error events remain.
Readers must continue ignoring unknown event types; no schema version changes.

Summary prompts omit lines starting with the exported transcription gap marker
(`> ⚠ 轉錄失敗`). These diagnostic lines remain in the saved transcript but do
not count toward the summary's content length. A gap-only block is recorded as
empty without requesting an LLM summary.
