# CODEX.md

Codex-specific project instructions. Follow `AGENTS.md` for shared development, Pull Request, and reporting rules.

## Ownership and branch

- Codex owns `core/cli.py`, `core/config.py`, `core/session.py`, `core/status.py`, `ui/`, UI API/schema, and UI contract tests.
- Claude Code owns `core/platform.py`, `core/devices.py`, `core/doctor.py`, `setup_engines.py`, `docs/platform-*.md`, and platform-related tests and fixes. Preserve this boundary as documented in `CLAUDE.md`.
- Use `codex/<task-name>` for Codex implementation branches.
- Change only Codex-owned files. If work requires Claude-owned files, stop and report a CROSS_AGENT_REQUEST to the user with the fields specified in `CLAUDE.md`; do not include those changes in a Codex commit.
- `README.md` is shared. Put any change to it in a separate commit.
