#!/usr/bin/env bash
# usage: codex-result.sh JOB_ID -- prints status, error, touched files and Codex's final report.
# Reads the job json directly (companion `result` reports "No job found" for running jobs and from other cwds).
set -euo pipefail
f=$(ls ~/.claude/plugins/data/*/state/*/jobs/"${1:?job id}".json 2>/dev/null | head -1 || true)
[ -n "$f" ] || { echo "job not found: $1" >&2; exit 3; }
python3 - "$f" <<'P'
import json, sys
j = json.load(open(sys.argv[1])); r = j.get("result") or {}
print("status:", j.get("status"), "| phase:", j.get("phase"), "| root:", j.get("workspaceRoot"))
if j.get("errorMessage"): print("error:", j["errorMessage"])
print("touched:", r.get("touchedFiles"))
print(r.get("rawOutput", ""))
P
