#!/usr/bin/env bash
# Guard around codex-companion: only task/status/result; `task` must name its workspace and mode.
# (companion treats unknown args such as --help as a prompt and dispatches a real job.)
set -euo pipefail
C="$(ls -d ~/.claude/plugins/cache/openai-codex/codex/*/ | sort -V | tail -1)scripts/codex-companion.mjs"
[ -f "$C" ] || { echo "codex-companion not found" >&2; exit 64; }
cmd=${1:-}; shift || true
case "$cmd" in
  task)
    case " $* " in *" --cwd "*|*" -C "*) ;; *) echo "task requires --cwd <worktree>" >&2; exit 64;; esac
    case " $* " in *" --write "*) ;; *) echo "task requires --write (otherwise sandbox is read-only)" >&2; exit 64;; esac ;;
  status|result) ;;
  *) echo "usage: companion.sh task|status|result ..." >&2; exit 64;;
esac
exec node "$C" "$cmd" "$@"
