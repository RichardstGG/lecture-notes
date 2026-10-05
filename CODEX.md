# CODEX.md

Codex-specific project instructions. Follow `AGENTS.md` for shared development, Pull Request, and reporting rules.

## Ownership and branch

- Codex owns `core/cli.py`, `core/config.py`, `core/session.py`, `core/status.py`, `core/summarize.py`, `core/terms.py`, `ui/` (including `ui/quality_eval.py` and `ui/SHARING.md`), UI API/schema, UI contract tests, `prompts/summary.md`, `config/template.toml`, and `meetings/` templates.
- Claude Code owns `core/platform.py`, `core/devices.py`, `core/doctor.py`, `core/servers.py`, `core/transcribe.py`, `core/util.py`, future `core/diarize.py`, `setup.py`, `setup_engines.py`, `upgrade.py`, `linux_setup.sh`, `mac_setup.command`, `windows_setup.bat`, `docs/platform-*.md`, `engines.lock`, and platform/engine-related tests and fixes. Preserve this boundary as documented in `CLAUDE.md`.
- `setup.py` handles first-time installation; `upgrade.py` updates an existing checkout. Both call `setup_engines.py` directly rather than wrapping each other.
- Use `codex/<task-name>` for Codex implementation branches, explicitly based on `origin/main`; use an isolated worktree when the checkout is shared.
- Change only Codex-owned or shared files, except for the `core/util.py` additive exception below. If other work requires Claude-owned files, stop and report a CROSS_AGENT_REQUEST to the user with the fields specified in `CLAUDE.md`; do not include those changes in a Codex commit.
- `README.md`, `AGENTS.md`, `config/default.toml`, and `docs/meeting-workbench-*.md` are shared. Put shared-file changes in a separate commit. In `config/default.toml`, add or change only keys consumed by your code and explain them in the same PR. The agent changing meeting behavior updates the meeting documents in that same PR.
- `core/util.py` additive exception: either agent may add small, pure-stdlib, side-effect-free helper functions without a CROSS_AGENT_REQUEST. Changes to existing function signatures or behavior, or removal of existing functions, still follow the normal ownership boundary.
- Test ownership follows the asserted contract: CLI JSON, API, and status-file contracts belong to Codex; platform and engine behavior belongs to Claude. Name test files after the tested module.
- `samples/*` and `tools/make_sample.py` are frozen; report issues rather than modifying them. For files not covered by the ownership assignments, ask the human maintainer rather than assuming ownership.
