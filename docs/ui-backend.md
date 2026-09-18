# UI backend

The browser UI uses a local FastAPI service. The service does not import
`core`; it invokes the repository's `lec` entry point as a subprocess and
consumes its JSON and filesystem contracts.

## Run locally

Install the UI-only dependencies in a virtual environment:

```bash
python -m pip install -r ui/backend/requirements.txt
python -m ui.backend
```

The service binds only to `127.0.0.1` and defaults to port `8765`. Use
`python -m ui.backend --port <port>` to choose another local port. Interactive
API documentation is available at `/api/docs`.

Optional environment variables:

- `LECTURE_NOTES_ROOT`: repository containing the `lec` entry point
- `LECTURE_NOTES_UI_CLI_TIMEOUT`: subprocess timeout in seconds, default `30`
- `LECTURE_NOTES_UI_POLL_INTERVAL`: status SSE polling interval, default `2`
- `LECTURE_NOTES_OUTPUT_ROOT`: override the session output directory
- `LECTURE_NOTES_UI_MAX_CONTENT_MB`: maximum transcript or notes size, default `16`

## API v1

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Service/API version |
| `GET` | `/api/v1/status` | Pass through the versioned `lec status --json` contract |
| `GET` | `/api/v1/courses` | Pass through `lec courses --json` with response validation |
| `GET` | `/api/v1/status/stream` | Server-sent status changes and heartbeat comments |
| `GET` | `/api/v1/sessions` | Session history, newest first |
| `GET` | `/api/v1/sessions/{id}` | Session metadata, transcript, and notes |
| `GET` | `/api/v1/sessions/{id}/stream` | Live transcript and notes updates |

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

Session discovery reads the direct children of the configured output root. The
root comes from `LECTURE_NOTES_OUTPUT_ROOT`, then `config/local.toml`, then
`config/default.toml`. Unrelated directories and symlinks that leave the output
root are ignored. Symlinked transcript, notes, status, and configuration files
are never followed.

The session stream sends a `snapshot` event first. Later `content` events contain
`target` (`transcript` or `notes`), `operation` (`append` or `replace`), `content`,
`updated_at`, and `size_bytes`. Append-only changes send only the delta; rewritten
files such as notes rebuilt by `--redo` send a complete replacement. Unchanged
polls send SSE heartbeat comments.

The backend remains read-only. Starting, stopping, summarizing, and editing
configuration are intentionally deferred to later isolated changes.
