#!/usr/bin/env bash
# usage: new-worktree.sh N [base-branch]   -> prints the worktree path (.tmp/wt-issue-N inside the repo)
set -euo pipefail
n=${1:?issue number}
base=${2:-$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)}
base=${base:-main}
root=$(git rev-parse --show-toplevel)
wt=$root/.tmp/wt-issue-$n
git fetch -q origin "$base"
git worktree add --no-track -b "ai/issue-$n" "$wt" "origin/$base" >&2
if [ -f "$wt/.gitmodules" ]; then git -C "$wt" submodule update --init --recursive >&2; fi
touch "$wt/.write-test" && rm "$wt/.write-test"
echo "$wt"
