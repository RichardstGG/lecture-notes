#!/usr/bin/env bash
# usage: agy-review.sh <pr-url> <base-branch>   (run inside the PR's worktree)
# Antigravity reviews origin/<base>...HEAD headless and sandboxed: it may only read files in this worktree
# (anything else is auto-denied in print mode); this script, not agy, posts the PR comment and,
# only when the review ends with VERDICT: APPROVE, marks the draft PR ready for review.
# The diff is precomputed because agy's sandbox cannot reach a worktree's gitdir.
# exit: 0 posted (prints ready/draft), 1 review failed/empty, 3 agy not installed
set -euo pipefail
url=${1:?pr url}; base=${2:?base branch}
command -v agy >/dev/null || { echo "agy not found; skipping Antigravity review" >&2; exit 3; }
wt=$(git rev-parse --show-toplevel); cd "$wt"
git fetch -q origin "$base"
mkdir -p .tmp; diff=.tmp/agy-review.diff; trap 'rm -f "$diff"' EXIT
git diff "origin/$base...HEAD" > "$diff"
[ -s "$diff" ] || { echo "empty diff against origin/$base" >&2; exit 1; }
# deny overrides the user's global agy grants (e.g. command(gh), command(git push))
settings=$(python3 -c 'import json,sys; print(json.dumps({"permissions": {"allow": [f"read_file({sys.argv[1]})"], "deny": [f"write_file({sys.argv[1]})", "command(gh)", "command(git)"]}}))' "$wt")
prompt="You are reviewing a pull request. The full diff (origin/$base...HEAD) is in $diff; read it, then read surrounding code in this directory as needed.
Do not modify files or run commands. Look for correctness bugs, security issues and missing tests; skip style nits.
Reply in Traditional Chinese (Taiwan) markdown: one-line verdict, then at most 8 findings as \`- path:line — issue\` (plain path, no links).
If there is nothing worth fixing, say so in one line.
End with exactly one last line: VERDICT: APPROVE if nothing must be fixed before merge, otherwise VERDICT: CHANGES."
out=$(agy -p "$prompt" --sandbox --settings "$settings" --print-timeout 600s) || { echo "agy failed" >&2; exit 1; }
[ -n "${out//[[:space:]]/}" ] && [[ $out != jetski:* ]] || { echo "agy produced no review: $out" >&2; exit 1; }
printf '🤖 **Antigravity review**（`%s`，參考用）\n\n%s\n' "$(git rev-parse --short HEAD)" "$out" | gh pr comment "$url" --body-file -
# missing or unclear verdict counts as not approved
if [[ $(grep -oE 'VERDICT: *(APPROVE|CHANGES)' <<<"$out" | tail -1) == *APPROVE ]]; then
  gh pr ready "$url" && echo "agy: APPROVE -> marked ready for review"
else
  echo "agy: not approved -> left as draft"
fi
