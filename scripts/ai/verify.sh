#!/usr/bin/env bash
# Mirror the Linux-runnable subset of CI; never stricter than CI.
# There is no Linux CI job (only the self-hosted Windows station), so this runs the project's own checks.
# --sandbox : used by the Codex worker; steps that need sockets/D-Bus/network are skipped and reported as ENV_LIMITED.
# (no flag) : full run; Claude runs this on the host in Finalize and trusts only this result.
set -euo pipefail
cd "$(dirname "$0")/../.."
SANDBOX=0; [ "${1:-}" = "--sandbox" ] && SANDBOX=1
host_only() { # host_only <label> <cmd...>
  local label=$1; shift
  if ((SANDBOX)); then echo "ENV_LIMITED: skipped $label"; else "$@"; fi
}

mapfile -t sh_files < <(git ls-files --cached --others --exclude-standard '*.sh')
for f in "${sh_files[@]}"; do bash -n "$f"; done

# These two bind real 127.0.0.1 sockets, which the network-less sandbox blocks.
socket_tests=(tests.test_ui_sharing tests.test_summarize_upstreams)
if ((SANDBOX)); then
  mapfile -t mods < <(ls tests/test_*.py | sed 's|/|.|; s|\.py$||' | grep -vxF -f <(printf '%s\n' "${socket_tests[@]}"))
  python3 -m unittest "${mods[@]}"
  echo "ENV_LIMITED: skipped ${socket_tests[*]}"
else
  python3 -m unittest
fi

frontend() {
  # Issue worktrees have no node_modules (gitignored); borrow the main checkout's.
  local main nm=ui/frontend/node_modules
  main=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
  [ -e "$nm" ] || { [ -d "$main/$nm" ] && ln -s "$main/$nm" "$nm"; } \
    || { echo "missing $nm: run 'npm ci' in $main/ui/frontend" >&2; return 1; }
  (cd ui/frontend && npm run --silent lint && npm run --silent test && npm run --silent build)
}
# Vite/vitest write caches into node_modules, which is outside the sandbox's writable root in a worktree.
host_only "frontend lint/test/build" frontend
echo "verify OK"
