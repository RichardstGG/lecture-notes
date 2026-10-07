"""Dispatch an explicitly selected trusted revision; requires authenticated gh."""
import argparse
import json
import re
import subprocess
import uuid


def dispatch_args(ref, sha, profile, reason, request_id):
    if not (ref == "main" or re.fullmatch(r"(codex|claude)/[A-Za-z0-9._/-]+", ref)):
        raise ValueError("Only main, codex/* and claude/* branches are accepted")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("A full lowercase commit SHA is required")
    if profile not in {"basic", "gpu", "microphone"} or not reason.strip():
        raise ValueError("A valid profile and non-empty reason are required")
    if not re.fullmatch(r"[a-z0-9-]{1,64}", request_id):
        raise ValueError("Invalid request ID")
    return ["gh", "workflow", "run", "windows-station.yml", "--repo", "RichardstGG/lecture-notes",
            "--ref", "main", "-f", f"target_ref={ref}", "-f", f"target_sha={sha}",
            "-f", f"profile={profile}", "-f", f"request_id={request_id}", "-f", f"reason={reason}"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--profile", required=True, choices=("basic", "gpu", "microphone"))
    parser.add_argument("--reason", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    request_id = uuid.uuid4().hex
    command = dispatch_args(args.ref, args.sha, args.profile, args.reason, request_id)
    if args.dry_run:
        print(json.dumps(command, ensure_ascii=False))
    else:
        subprocess.run(command, check=True)
        print(f"Dispatched request {request_id}; this is not a test result.")
        print('Find its run: gh run list --repo RichardstGG/lecture-notes --workflow windows-station.yml --json databaseId,displayTitle,status,conclusion,url')
        print('Match the request ID in displayTitle, then use gh run watch RUN_ID and gh run download RUN_ID.')


if __name__ == "__main__":
    main()
