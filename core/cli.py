"""lec 指令列介面。UI 也透過這些指令操作（不直接 import core）。"""
import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

from . import config as C
from .status import RunLock
from .util import die, hms, read_json

EPILOG = """範例：
  lec run 計算機概論                         上課：即時轉錄＋總結，Ctrl+C 結束
  lec run 計算機概論 --file 錄音.mp3         處理音檔（轉錄完再總結）
  lec run 計算機概論 --model qwen3-4b        臨時換模型
  lec run 計算機概論 --set vad.sensitivity=3 --set summary.temperature=0.3
  lec summarize outputs/UNIXops_20260916                補做尚未完成的總結
  lec summarize outputs/UNIXops_20260916 --redo 00:05:02
  lec summarize outputs/UNIXops_20260916 --redo all --model qwen3-4b
  lec config 計算機概論                      印出合併後的設定
  lec courses / lec new 課名 / lec status / lec stop
"""


def _add_overrides(p):
    p.add_argument("--model", help="總結模型（[models.*] 的名稱，例如 qwen3-8b、qwen3-4b）")
    p.add_argument("--set", action="append", default=[], metavar="區塊.鍵=值",
                   help="覆寫任一設定，可重複使用")


def _state_dir():
    cfg, _ = C.load(None)
    return cfg.path(cfg["paths"]["state_dir"])


def _load(args, **kw):
    try:
        return C.load(sets=args.set, model=getattr(args, "model", None),
                      source=getattr(args, "source", None), **kw)
    except C.ConfigError as e:
        die(str(e))


# ---------------------------------------------------------------- 子指令
def cmd_run(args):
    from .session import LectureRun
    cfg, created = _load(args, course_arg=args.course, create_missing=True)
    if created:
        print(f"▷ 已建立課程設定檔 {C.COURSES_DIR / (args.course + '.toml')}（目前使用預設值，之後可修改）")
    return LectureRun(cfg, args.file).run()


def cmd_summarize(args):
    from .session import OfflineSummary
    d = Path(args.session).expanduser().resolve()
    if not d.is_dir():
        die(f"找不到資料夾：{d}")
    used = d / "config.used.toml"
    if args.course:
        cfg, _ = _load(args, course_arg=args.course)
    else:
        # 預設用「目前」的課程設定檔（方便改完 prompt 重做）；找不到才用當時的 config.used.toml
        name = None
        if used.exists():
            name = C.load_toml(used).get("course", {}).get("name")
        if not name:
            try:
                m = re.search(r"^course:\s*(.+)$", (d / "transcript.md").read_text(encoding="utf-8"), re.M)
                name = m.group(1).strip() if m else None
            except OSError:
                pass
        if name and C.course_path(name)[0].exists():
            cfg, _ = _load(args, course_arg=name)
        elif used.exists():
            cfg, _ = _load(args, course_file=used)
        else:
            cfg, _ = _load(args, course_arg=name)
    if args.redo and args.redo != "all" and not re.fullmatch(r"\d{2}:\d{2}:\d{2}", args.redo):
        die("--redo 需要 hh:mm:ss（逐字稿小標題時間）或 all")
    return OfflineSummary(cfg, d, args.redo).run()


def cmd_config(args):
    cfg, _ = _load(args, course_arg=args.course)
    for w in cfg.warnings:
        print(f"⚠ {w}", file=sys.stderr)
    src = cfg.course_file or "（無課程設定檔）"
    sys.stdout.write(C.dump_toml(cfg.data, f"合併後的設定：default.toml + {src}"))
    return 0


def cmd_courses(args):
    files = sorted(C.COURSES_DIR.glob("*.toml"))
    if args.json:
        out = []
        for f in files:
            try:
                cfg, _ = C.load(f.stem)
                out.append({"file": str(f), "id": f.stem, "name": cfg.course_name,
                            "model": cfg["summary"]["model"], "terms": len(cfg["whisper"]["terms"])})
            except C.ConfigError as e:
                out.append({"file": str(f), "id": f.stem, "error": str(e)})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if not files:
        print(f"（{C.COURSES_DIR} 中沒有課程設定檔，可用 lec new 課名 建立）")
    for f in files:
        try:
            cfg, _ = C.load(f.stem)
            print(f"{f.stem:<16} 模型 {cfg['summary']['model']:<10} 術語 {len(cfg['whisper']['terms'])} 個")
        except C.ConfigError as e:
            print(f"{f.stem:<16} ✖ {e}")
    return 0


def cmd_new(args):
    try:
        p = C.create_course_file(args.course)
    except C.ConfigError as e:
        die(str(e))
    print(f"✔ 已建立 {p}")
    return 0


def cmd_status(args):
    lock = RunLock(_state_dir())
    cur = lock.current()
    if not cur:
        print(json.dumps({"running": False}) if args.json else "目前沒有 lec 在執行")
        return 0
    st = read_json(Path(cur.get("session", "")) / "status.json", {}) if cur.get("session") else {}
    if args.json:
        print(json.dumps({"running": True, **cur, "status": st}, ensure_ascii=False, indent=2))
        return 0
    print(f"執行中：pid {cur['pid']}，課程 {cur.get('course')}，{cur.get('mode', 'run')}")
    print(f"  資料夾：{cur.get('session', '')}")
    if st:
        lag = st.get("transcribe_lag")
        print(f"  階段：{st.get('phase')}｜已錄 {hms(st.get('elapsed', 0))}"
              + (f"｜延遲 {lag}s" if lag is not None else "") + f"｜佇列 {st.get('queue')}")
        print(f"  總結：{st.get('sections_summarized')}/{st.get('sections_total')} 節"
              + ("（LLM 處理中）" if st.get("llm_busy") else "")
              + f"｜錯誤 {st.get('errors')}")
    return 0


def cmd_stop(args):
    lock = RunLock(_state_dir())
    cur = lock.current()
    if not cur:
        print("目前沒有 lec 在執行")
        return 0
    pid = int(cur["pid"])
    os.kill(pid, signal.SIGINT)
    if args.force:
        time.sleep(0.3)
        os.kill(pid, signal.SIGINT)
        print(f"✔ 已強制結束 pid {pid}")
    else:
        print(f"✔ 已送出停止訊號給 pid {pid}（會轉完剩餘段落、補做最後一段總結）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lec", description="課堂筆記系統",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
    sub = ap.add_subparsers(dest="cmd", metavar="指令")

    p = sub.add_parser("run", help="錄音（或處理音檔）→ 逐字稿 → 即時總結")
    p.add_argument("course", help="課名（對應 courses/<課名>.toml）或 .toml 路徑")
    p.add_argument("--file", help="處理既有音檔，而不是麥克風即時錄音")
    p.add_argument("--source", help="錄音來源（[audio.sources.*] 的名稱或 pulse 來源全名）")
    _add_overrides(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("summarize", help="對已完成的逐字稿補做或重做總結")
    p.add_argument("session", help="輸出資料夾（含 transcript.md）")
    p.add_argument("--course", help="改用指定的課程設定（預設依資料夾內的課名）")
    p.add_argument("--redo", metavar="hh:mm:ss|all", help="重做某一段（小標題時間）或全部")
    _add_overrides(p)
    p.set_defaults(func=cmd_summarize)

    p = sub.add_parser("config", help="印出合併後生效的設定")
    p.add_argument("course", nargs="?")
    p.add_argument("--source")
    _add_overrides(p)
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("courses", help="列出課程設定檔")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_courses)

    p = sub.add_parser("new", help="從範本建立課程設定檔")
    p.add_argument("course")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("status", help="查看目前執行狀態")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("stop", help="停止目前的 lec run（等同 Ctrl+C）")
    p.add_argument("--force", action="store_true", help="強制結束（等同連按兩次 Ctrl+C）")
    p.set_defaults(func=cmd_stop)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    return args.func(args) or 0
