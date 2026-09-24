#!/usr/bin/env python3
"""Update an existing lecture-notes checkout and rebuild its local runtime.

First-time installation goes through setup.py instead (it detects the machine,
asks which backend to build and lists any missing system packages). This script
assumes everything is already installed and only brings it up to date.

The script is intentionally dependency-free so an older checkout can update
itself before the project virtual environment exists.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MIN_PYTHON = (3, 11)


class UpgradeError(Exception):
    pass


def step(message):
    print(f"\n==== {message} ====", flush=True)


def run(command, *, cwd=None, capture=False):
    command = [str(part) for part in command]
    print("  $ " + " ".join(command), flush=True)
    try:
        result = subprocess.run(
            command, cwd=str(cwd or ROOT), capture_output=capture,
            text=capture, errors="replace" if capture else None,
        )
    except OSError as exc:
        raise UpgradeError(f"無法執行 {command[0]}：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        suffix = f"\n{detail}" if detail else ""
        raise UpgradeError(f"指令失敗（{result.returncode}）：{' '.join(command)}{suffix}")
    return result


def git_output(*arguments):
    return run(["git", "-C", ROOT, *arguments], capture=True).stdout.strip()


def update_checkout():
    if not (ROOT / ".git").is_dir():
        raise UpgradeError(f"{ROOT} 不是 Git checkout，請先 clone lecture-notes")
    branch = git_output("branch", "--show-current")
    if not branch:
        raise UpgradeError("目前是 detached HEAD；請先切回 main 再升級")
    changes = git_output("status", "--porcelain", "--untracked-files=no")
    if changes:
        raise UpgradeError(
            "偵測到 tracked 檔案有未提交修改；請先 commit 或處理後再升級。\n" + changes
        )
    before = git_output("rev-parse", "HEAD")
    print(f"▶ 更新分支 {branch}")
    run(["git", "-C", ROOT, "pull", "--ff-only"])
    after = git_output("rev-parse", "HEAD")
    return before != after


def engine_command(args):
    command = [sys.executable, ROOT / "setup_engines.py"]
    if args.backend != "auto":
        command += ["--backend", args.backend]
    if args.generator:
        command += ["--generator", args.generator]
    if args.rebuild:
        command.append("--rebuild")
    if args.import_models:
        command += ["--import-models", args.import_models]
    if args.whisper_only:
        command.append("whisper")
    return command


def venv_python():
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return ROOT / ".venv" / relative


def ensure_no_active_run():
    result = run([sys.executable, ROOT / "lec", "status", "--json"], capture=True)
    try:
        status = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise UpgradeError("無法解析 lec status；為避免中斷課程，已停止升級") from exc
    if status.get("running"):
        course = status.get("course") or "未知課程"
        raise UpgradeError(f"目前仍有課程在執行（{course}）；請先正常停止後再升級")


def install_ui():
    python = venv_python()
    if not python.is_file():
        step("建立 Python virtual environment")
        run([sys.executable, "-m", "venv", ROOT / ".venv"])
    step("更新 UI backend dependencies")
    run([python, "-m", "pip", "install", "-r", ROOT / "ui/backend/requirements.txt"])

    npm = shutil.which("npm")
    if not npm:
        raise UpgradeError("找不到 npm；Web UI 需要 Node.js 22.22+")
    frontend = ROOT / "ui/frontend"
    step("更新並編譯 frontend")
    run([npm, "ci"], cwd=frontend)
    run([npm, "run", "build"], cwd=frontend)


def perform_upgrade(args):
    if not args.skip_engines:
        ensure_no_active_run()
        step("更新並編譯本機引擎")
        run(engine_command(args))
    if not args.skip_ui:
        install_ui()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="更新 lecture-notes、編譯引擎並重建 Web UI",
    )
    parser.add_argument(
        "--whisper-only", action="store_true",
        help="只取得／編譯 whisper.cpp，不處理 llama.cpp",
    )
    parser.add_argument(
        "--backend", default="auto", choices=["auto", "vulkan", "cuda", "metal", "cpu"],
        help="傳給 setup_engines.py 的運算後端",
    )
    parser.add_argument("--generator", metavar="NAME", help="傳給 cmake -G 的 generator")
    parser.add_argument("--rebuild", action="store_true", help="強制重編本機引擎")
    parser.add_argument("--import-models", metavar="DIR", help="從指定資料夾搬入模型")
    parser.add_argument("--skip-pull", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-engines", action="store_true", help="略過本機引擎更新")
    parser.add_argument("--skip-ui", action="store_true", help="略過 UI dependencies 與 frontend build")
    return parser.parse_args(argv)


def main(argv=None):
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw_args)
    if sys.version_info < MIN_PYTHON:
        print("✖ upgrade.py 需要 Python 3.11+", file=sys.stderr)
        return 1
    try:
        if not args.skip_pull:
            step("更新 lecture-notes")
            if update_checkout():
                # Continue with the newly pulled workflow instead of the old
                # in-memory copy of this script.
                step("改用更新後的 upgrade.py")
                result = run([
                    sys.executable, ROOT / "upgrade.py", "--skip-pull", *raw_args,
                ])
                return result.returncode
        perform_upgrade(args)
    except UpgradeError as exc:
        print(f"✖ {exc}", file=sys.stderr, flush=True)
        return 1

    step("升級完成")
    print("下一步：./lec doctor（Windows：python lec doctor）")
    if args.whisper_only:
        print("只轉錄：./lec run <課名> --transcribe-only")
    if not args.skip_ui:
        print("啟動 UI：./lec --start-ui"
              if os.name != "nt" else "啟動 UI：python lec --start-ui")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
