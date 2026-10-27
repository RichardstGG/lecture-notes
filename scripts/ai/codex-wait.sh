#!/usr/bin/env bash
# usage: codex-wait.sh JOB_ID [timeout_s=570]
# exit: 0 completed | 1 failed/cancelled | 2 timeout (call again) | 3 job not found
# Polls the job json directly, so it works from any cwd.
set -euo pipefail
id=${1:?job id}; end=$((SECONDS + ${2:-570}))
while :; do
  f=$(ls ~/.claude/plugins/data/*/state/*/jobs/"$id".json 2>/dev/null | head -1 || true)
  if [ -n "$f" ]; then
    s=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("status",""))' "$f")
    case "$s" in
      completed) echo completed; exit 0;;
      failed|cancelled) echo "$s"; exit 1;;
    esac
  elif ((SECONDS > 20)); then echo "job not found: $id"; exit 3; fi
  if ((SECONDS >= end)); then echo "timeout (still ${s:-unknown})"; exit 2; fi
  sleep 5
done
