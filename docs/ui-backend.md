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

## API v1

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Service/API version |
| `GET` | `/api/v1/status` | Pass through the versioned `lec status --json` contract |
| `GET` | `/api/v1/courses` | Pass through `lec courses --json` with response validation |
| `GET` | `/api/v1/status/stream` | Server-sent status changes and heartbeat comments |

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

This first backend slice is read-only. Starting, stopping, editing configuration,
session history, and live transcript/note streaming are intentionally deferred to
later isolated changes.
