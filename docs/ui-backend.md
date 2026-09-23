# UI backend

The browser UI uses a local FastAPI service. The service does not import
`core`; it invokes the repository's `lec` entry point as a subprocess and
consumes its JSON and filesystem contracts.

## Run locally

Install the UI-only dependencies in a virtual environment:

```bash
python -m pip install -r ui/backend/requirements.txt
cd ui/frontend
npm install
npm run build
cd ../..
lec --start-ui
```

The service binds only to `127.0.0.1` and defaults to port `8765`. Use
`lec --start-ui --port <port>` to choose another local port. The launcher uses
the repository's `.venv` automatically when it exists. Direct invocation with
`python -m ui.backend [--port <port>]` remains available for development.
Interactive API documentation is available at `/api/docs`. When
`ui/frontend/dist` exists,
the same service hosts the browser UI at `/`; no separate frontend server is
needed for production-style local use.

Pressing `Ctrl+C` stops only the UI service. Open browser SSE connections are
notified immediately and have a one-second fallback shutdown bound, so the
browser tab does not need to be closed. Any detached `lec` run continues and is
rediscovered the next time the UI service starts.

For frontend development, run `npm run dev` from `ui/frontend`. Vite binds to
`127.0.0.1:5173` and proxies `/api` to the FastAPI service on port `8765`.

Optional environment variables:

- `LECTURE_NOTES_ROOT`: repository containing the `lec` entry point
- `LECTURE_NOTES_UI_CLI_TIMEOUT`: subprocess timeout in seconds, default `30`
- `LECTURE_NOTES_UI_POLL_INTERVAL`: status SSE polling interval, default `2`
- `LECTURE_NOTES_OUTPUT_ROOT`: override the session output directory
- `LECTURE_NOTES_UI_MAX_CONTENT_MB`: maximum transcript or notes size, default `16`
- `LECTURE_NOTES_UI_MAX_UPLOAD_GB`: maximum browser-selected media size, default `8`
- `LECTURE_NOTES_UI_UPLOAD_ROOT`: staging directory for browser-selected media;
  defaults to `lecture-notes-ui/uploads` inside the operating system temp directory
- `LECTURE_NOTES_UI_PROCESS_LOG`: background launcher log path; defaults to the
  operating system temporary directory
- `LECTURE_NOTES_UI_FRONTEND_DIST`: optional path to a built frontend directory

## API v1

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Service/API version |
| `GET` | `/api/v1/status` | Pass through the versioned `lec status --json` contract |
| `GET` | `/api/v1/courses` | Pass through `lec courses --json` with response validation |
| `GET` | `/api/v1/models` | Discover configured summary and Whisper models |
| `GET` | `/api/v1/devices` | List microphone sources and the current local selection |
| `PUT` | `/api/v1/devices/current` | Save `audio.source` through `lec devices --save` |
| `POST` | `/api/v1/devices/test` | Record a three-second volume test through `lec devices --test` |
| `GET` | `/api/v1/doctor` | Run environment diagnostics; accepts `course` and `mic` query parameters |
| `POST` | `/api/v1/courses` | Create a course from `config/template.toml` |
| `GET` | `/api/v1/courses/{id}` | Read the editable course TOML source |
| `PUT` | `/api/v1/courses/{id}` | Validate and atomically replace course TOML |
| `PUT` | `/api/v1/courses/{id}/vocabulary` | Update Whisper terms and the summary glossary |
| `POST` | `/api/v1/audio-uploads` | Stream a browser-selected media file into local staging |
| `GET` | `/api/v1/status/stream` | Server-sent status changes and heartbeat comments |
| `GET` | `/api/v1/sessions` | Session history, newest first |
| `GET` | `/api/v1/sessions/{id}` | Session metadata, transcript, and notes |
| `GET` | `/api/v1/sessions/{id}/stream` | Live transcript and notes updates |
| `POST` | `/api/v1/runs` | Start a live or file-mode lecture process |
| `POST` | `/api/v1/runs/stop` | Request normal or forced stop |
| `POST` | `/api/v1/sessions/{id}/summarize` | Start missing or redo summaries |

Mutation requests use JSON. Starting a process returns HTTP `202` with its PID;
an existing active process returns HTTP `409`.
If a valid command finishes during the startup window, such as summarize finding
nothing to do, the response includes `"completed": true` and `"exit_code": 0`.

```json
{
  "course": "測試課",
  "input_file": "/absolute/path/to/lecture.ogg",
  "model": "qwen3-8b",
  "source": "default",
  "overrides": {
    "summary.temperature": 0.2,
    "summary.enabled": true
  }
}
```

Browser-selected files use `POST /api/v1/audio-uploads?filename=<name>` with the
raw file bytes as the request body. The backend writes each request incrementally
to a random local filename, atomically publishes it only after the complete body
arrives, and returns `api_version`, the original `name`, the absolute staged
`path`, and `size_bytes`. The browser then sends that path through the unchanged
`input_file` run field. Common audio and media extensions are accepted; empty,
unsupported, unsafe, and over-limit uploads use the standard error envelope.
The default limit is 8 GiB. Staged copies remain in the operating system temp
directory so a detached `lec` process can keep reading them after the UI service
closes; the original manual-path mode remains available when copying is
undesirable.

Normal and forced stop bodies are `{"force": false}` and `{"force": true}`.
Summarize accepts optional `redo` (`all` or `hh:mm:ss`), `model`, and `course`.
The API only constructs fixed `lec` argument arrays; it never invokes a shell.
Background children use a separate process group/session and do not depend on the
browser connection remaining open.

To run without an LLM, send the existing override in the start request:
`{"overrides": {"summary.enabled": false}}`. The browser's **只轉錄** switch
uses this form; the CLI provides the equivalent `lec run --transcribe-only`
convenience flag.

CLI failures use a stable envelope:

```json
{
  "error": {
    "code": "cli_failed",
    "message": "human-readable summary",
    "exit_code": 1,
    "stderr": "optional diagnostic text"
  }
}
```

Unavailable and timed-out CLI processes return HTTP `503`. Failed commands and
invalid CLI JSON/contracts return HTTP `502`. The backend preserves unknown
status and course fields so that additive CLI contract changes remain compatible.

Model discovery invokes the fixed `lec models --json` command. Its versioned
response contains `summary` and `whisper` groups, each with the configured
`selected` model id and a `models` array. Every model entry reports its id,
resolved path, and whether that path is an installed regular file; installed
files also report `size_bytes`. Summary entries additionally expose
`disable_thinking`. Unknown fields are preserved for additive compatibility.
The browser uses the summary inventory for its run override selector and
disables entries whose local model file is missing.

Device discovery invokes the fixed `lec devices --json` command. The HTTP
response adds `api_version` and exposes `current`, the operating-system
`default`, and a dynamic `sources` array. Saving a source accepts
`{"source": "<device id>"}` and delegates to `lec devices --save`, which updates
the ignored `config/local.toml`; it does not edit that file directly. Device
tests use the same request shape and synchronously return the CLI's three-second
volume-test message.

Doctor responses wrap `lec doctor [course] [--mic] --json` with `api_version`,
the selected options, summary counts, and the original diagnostic items. Exit
code `1` is expected when diagnostics contain failures and still returns HTTP
`200`; malformed output or failure to run the command uses the normal API error
envelope.

Session discovery recursively searches the configured output root for session
marker files. Nested session ids are slash-separated paths relative to that
root, for example `UNIXops/20260921`. The root comes from
`LECTURE_NOTES_OUTPUT_ROOT`, then `config/local.toml`, then
`config/default.toml`. Unrelated directories and symlinks are ignored.
Symlinked transcript, notes, status, and configuration files are never followed.

The session stream sends a `snapshot` event first. Later `content` events contain
`target` (`transcript` or `notes`), `operation` (`append` or `replace`), `content`,
`updated_at`, and `size_bytes`. Append-only changes send only the delta; rewritten
files such as notes rebuilt by `--redo` send a complete replacement. Unchanged
polls send SSE heartbeat comments.

The backend can start and stop a lecture process and request session
summarization through fixed `lec` argument arrays.

Course creation accepts `{"id": "資料結構"}`. Course ids are portable filename
stems: path separators, option-like leading hyphens, hidden-file leading dots,
control characters, Windows-reserved characters, and reserved device names are
rejected. Existing files are never overwritten during creation.

Course detail responses contain `api_version`, `id`, `file`, the original TOML
`content`, and normalized `vocabulary` fields for `whisper.terms` and
`summary.glossary`. Malformed vocabulary remains available through `content`,
with `vocabulary` set to `null` so it can be repaired in the raw editor. Raw updates
accept `{"content": "..."}`. The service first parses TOML, then asks
`lec config <temporary-file>` to validate the fully merged configuration. Only
valid content is atomically moved into place; a failed parse or validation leaves
the existing course untouched. Symlinked course files are not read or replaced,
and course content is limited to 1 MiB.

Structured vocabulary updates accept `terms` and normalized glossary entries:

```json
{
  "terms": ["UNIX", "POSIX"],
  "glossary": [
    {"term": "Multics", "means": "分時系統專案", "aka": ["MUTIX", "Multix"]}
  ]
}
```

The update rewrites only the `whisper.terms` assignment and
`[summary.glossary]` entries in canonical TOML. Other settings and comments are
preserved except formatting or inline comments attached directly to those managed
values, and the same full CLI validation and atomic replacement rules apply.

Local machine configuration editing remains deferred to a later isolated
change.

## Frontend foundation

The React/TypeScript frontend currently provides:

- local service connection state and live run status over SSE
- live transcription, queue, and summary progress
- live launch plus file-mode drag-and-drop/file selection with a manual path fallback
- persistent graceful-stop feedback and forced stop
- course creation, structured vocabulary/glossary editing, and validated raw TOML editing
- session history with streamed transcript and notes content
- missing-summary action for sessions that have a transcript
- responsive desktop and mobile layouts

The model override selector is populated from the versioned model-discovery
contract. It displays installed summary models with their file sizes, disables
configured models whose files are missing, and keeps the selected course model
as the default. Model names are never hard-coded in the frontend.
