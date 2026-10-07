"""Windows station contract v1. Raw logs never leave the station automatically."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import uuid

PROFILES = ("basic", "gpu", "microphone")
EXITS = {"passed": 0, "failed": 1, "blocked": 2, "timed_out": 3, "error": 4}


class Blocked(Exception):
    pass


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def control_dir():
    return Path(os.environ["LOCALAPPDATA"]) / "lecture-notes-test-runner"


def require_window(control, profile, now=None):
    if profile == "basic":
        return
    now = time.time() if now is None else now
    try:
        window = json.loads((control / "window.json").read_text(encoding="utf-8"))
        expires = window["expires_at"]
        profiles = window["profiles"]
        if (window["schema_version"] != 1 or not isinstance(profiles, list)
                or any(p not in ("gpu", "microphone") for p in profiles) or profile not in profiles
                or isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not now < expires <= now + 4 * 3600):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise Blocked("test_window_closed") from None


def set_window(control, profiles, minutes):
    if not 1 <= minutes <= 240 or not set(profiles) <= {"gpu", "microphone"}:
        raise ValueError("Only gpu/microphone windows of 1..240 minutes are allowed")
    write_json(control / "window.json", {
        "schema_version": 1, "profiles": profiles, "expires_at": time.time() + minutes * 60,
    })


def assert_checkout(repo, sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Blocked("invalid_sha")
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if actual != sha:
        raise Blocked("sha_mismatch")
    # Only the dedicated Actions checkout may be used. Never overwrite local config.
    for relative in ("config/local.toml", "config/upstreams.toml"):
        if (repo / relative).exists():
            raise Blocked("checkout_has_private_config")


def hardware_config(control, profile):
    try:
        config = json.loads((control / "machine.json").read_text(encoding="utf-8"))
        if config["schema_version"] != 1:
            raise ValueError
        fields = ("whisper_dir", "whisper_model", "input_audio") if profile == "gpu" else ("microphone",)
        if any(not isinstance(config.get(k), str) or not config[k].strip() for k in fields):
            raise ValueError
        if profile == "gpu":
            if not all(Path(config[k]).is_absolute() and Path(config[k]).exists() for k in fields):
                raise ValueError
        return config
    except (OSError, ValueError, TypeError, KeyError):
        raise Blocked("hardware_not_configured") from None


def require_vulkan(config):
    stamp = Path(config["whisper_dir"]) / "build" / ".lec-build"
    if not stamp.is_file() or "BACKEND=vulkan" not in stamp.read_text(encoding="utf-8").split():
        raise Blocked("vulkan_build_not_configured")


def commands(repo, profile, config, local_run):
    python = sys.executable
    if profile == "basic":
        # No model loading, doctor --mic, or device probing in the basic profile.
        return [("dependencies", [python, "-c", "import fastapi, uvicorn, httpx"]),
                ("unittest", [python, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]),
                ("cli_help", [python, "lec", "--help"])]
    if profile == "microphone":
        return [("microphone_capture", [python, "lec", "devices", "--test", config["microphone"]])]
    # CLI arguments override all relevant paths; never copy personal local.toml.
    overrides = {
        "paths.output_root": str(local_run / "outputs"),
        "paths.state_dir": str(local_run / "state"),
        "paths.whisper_dir": config["whisper_dir"],
        "whisper.models.runner.path": config["whisper_model"],
        "whisper.model": "runner", "whisper.port": 28178,
        "system.inhibit_sleep": False, "system.opencc": False,
    }
    cmd = [python, "lec", "run", "windows-runner", "--file", config["input_audio"], "--transcribe-only"]
    for key, value in overrides.items():
        cmd.extend(["--set", f"{key}={json.dumps(value, ensure_ascii=False)}"])
    return [("file_transcription", cmd)]


def execute_step(command, repo, log, control, profile, deadline):
    require_window(control, profile)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Do not pass Actions control channels to tested code.
    for key in list(env):
        if key.startswith(("GITHUB_", "ACTIONS_", "RUNNER_")) or key in {"GH_TOKEN", "GITHUB_TOKEN"}:
            env.pop(key)
    with log.open("wb") as stream:
        proc = subprocess.Popen(command, cwd=repo, env=env, stdout=stream, stderr=subprocess.STDOUT)
        try:
            while proc.poll() is None:
                require_window(control, profile)
                if time.monotonic() >= deadline:
                    raise TimeoutError
                time.sleep(0.5)
            return proc.returncode
        finally:
            if proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                proc.wait(timeout=15)


def run(args):
    control = control_dir()
    run_id = uuid.uuid4().hex
    local_run = control / "runs" / run_id
    local_run.mkdir(parents=True)
    report = {"schema_version": 1, "request_id": args.request_id, "local_run_id": run_id,
              "sha": args.sha, "profile": args.profile, "status": "error", "steps": [],
              "python": platform.python_version(), "os": platform.system(),
              "os_release": platform.release(), "started_at": int(time.time()),
              "evidence": "automated_including_mocks" if args.profile == "basic" else "hardware_smoke"}
    lock = None
    started = time.monotonic()
    try:
        if sys.platform != "win32":
            raise Blocked("native_windows_required")
        from windows_job import contain_process_tree
        job_handle = contain_process_tree()  # noqa: F841; OS closes on exit
        try:
            lock = (control / "station.lock").open("x")
        except FileExistsError:
            raise Blocked("station_busy_or_stale_lock") from None
        lock.write(str(os.getpid()))
        lock.flush()
        assert_checkout(args.repo, args.sha)
        require_window(control, args.profile)
        config = {} if args.profile == "basic" else hardware_config(control, args.profile)
        if args.profile == "gpu":
            import socket
            require_vulkan(config)
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", 28178)) == 0:
                    raise Blocked("test_port_busy")
            report["acceleration_verified"] = False
            report["configured_backend"] = "vulkan"
        deadline = started + args.timeout_seconds
        report["status"] = "passed"
        for name, command in commands(args.repo, args.profile, config, local_run):
            code = execute_step(command, args.repo, local_run / f"{name}.log",
                                control, args.profile, deadline)
            report["steps"].append({"name": name, "exit_code": code})
            if name == "unittest":
                content = (local_run / f"{name}.log").read_text(encoding="utf-8", errors="replace")
                count = re.search(r"Ran (\d+) tests? in", content)
                skipped = re.search(r"skipped=(\d+)", content)
                report["tests_run"] = int(count[1]) if count else 0
                report["tests_skipped"] = int(skipped[1]) if skipped else 0
                if not report["tests_run"] or report["tests_run"] == report["tests_skipped"]:
                    report.update(status="failed", reason="no_tests_executed")
                    break
            if code:
                report["status"] = "failed"
                break
    except Blocked as exc:
        report.update(status="blocked", reason=str(exc))
    except TimeoutError:
        report.update(status="timed_out", reason="execution_deadline")
    except Exception as exc:
        # Paths, device names, transcripts and arbitrary test output stay local.
        (local_run / "runner-error.txt").write_text(repr(exc), encoding="utf-8")
        report.update(status="error", reason="runner_error")
    finally:
        if lock is not None:
            lock.close()
            (control / "station.lock").unlink()
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        write_json(local_run / "summary.json", report)
        write_json(args.report_dir / "summary.json", report)
    print(json.dumps(report))
    return EXITS[report["status"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    opening = sub.add_parser("open-window")
    opening.add_argument("--profiles", nargs="+", choices=("gpu", "microphone"), required=True)
    opening.add_argument("--minutes", type=int, default=30)
    sub.add_parser("close-window")
    running = sub.add_parser("run")
    running.add_argument("--repo", type=Path, required=True)
    running.add_argument("--sha", required=True)
    running.add_argument("--profile", choices=PROFILES, required=True)
    running.add_argument("--request-id", required=True)
    running.add_argument("--report-dir", type=Path, required=True)
    running.add_argument("--timeout-seconds", type=int, default=1800, help="1..3600 seconds")
    args = parser.parse_args()
    if args.command == "run":
        if not 1 <= args.timeout_seconds <= 3600:
            running.error("--timeout-seconds must be 1..3600")
        return run(args)
    if args.command == "open-window":
        set_window(control_dir(), args.profiles, args.minutes)
        print("Test window opened; microphone profile records 3 seconds of live audio.")
    else:
        (control_dir() / "window.json").unlink(missing_ok=True)
        print("Hardware test window closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
