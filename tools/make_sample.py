#!/usr/bin/env python3
"""把朗讀 samples/test8min.txt 的錄音，處理成可隨 repo 發布的範例音檔。

這支腳本只有在要重做範例音檔時才需要跑；一般使用者直接使用 repo 內附的音檔即可。
只需要 ffmpeg，沒有其他相依套件。

用法：
  python3 tools/make_sample.py 我的錄音.wav        正規化音量並壓成 opus
  python3 tools/make_sample.py --record            直接用麥克風錄（Ctrl+C 結束）
  python3 tools/make_sample.py 錄音.wav --start 0:03 --duration 8:00   裁掉頭尾
  python3 tools/make_sample.py 錄音.wav --out samples/other.ogg

輸出固定是單聲道 Opus 24 kbps，跟 lec 實際錄音的規格一致，
八分鐘大約 1.4 MB。錄音本身不做降噪，環境音留著才像真的上課。
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "samples" / "test8min.txt"
OUT = ROOT / "samples" / "test8min.ogg"
RAW = ROOT / "samples" / "_raw_recording.wav"   # --record 的暫存檔，已被 .gitignore 擋掉


def die(msg):
    print(f"✖ {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd, **kw):
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], **kw).returncode


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def hms(secs):
    return f"{int(secs // 60):02d}:{int(secs % 60):02d}"


def show(path):
    """專案內的檔案印相對路徑，專案外的印原樣。"""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def record(dest, source=None):
    """用 lec 的平台抽象層錄音，三個平台都能用。Ctrl+C 結束。"""
    from core import platform as P

    src = P.resolve_source(source)
    print(f"▶ 錄音來源：{src or 'default'}")
    print(f"▶ 開始念 {show(SCRIPT)}，念完按 Ctrl+C 結束")
    print("  用平常上課的語速，不用刻意咬字；念錯了就停下來重講一次，不用重錄。\n")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           *P.ffmpeg_input(src), "-ac", "1", "-ar", "48000", str(dest)]
    try:
        run(cmd)
    except KeyboardInterrupt:
        pass
    if not dest.is_file() or probe(dest) < 1:
        die("沒有錄到東西，先用 lec devices --test 檢查麥克風")
    print(f"\n✔ 錄到 {hms(probe(dest))}")
    return dest


def encode(src, dest, start=None, duration=None):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start:
        cmd += ["-ss", start]
    cmd += ["-i", str(src)]
    if duration:
        cmd += ["-t", duration]
    # 只做音量正規化與去低頻隆隆聲，不降噪：環境音留著才像真的教室
    cmd += ["-af", "highpass=f=80,loudnorm=I=-20:TP=-2:LRA=11",
            "-ar", "16000", "-ac", "1", "-c:a", "libopus", "-b:a", "24k",
            "-vbr", "constrained", "-application", "voip", str(dest)]
    if run(cmd) != 0:
        die("ffmpeg 轉檔失敗")


def main():
    ap = argparse.ArgumentParser(description="產生範例測試音檔")
    ap.add_argument("source_file", nargs="?", help="錄音檔（ffmpeg 讀得到的格式都行）")
    ap.add_argument("--record", action="store_true", help="直接用麥克風錄，Ctrl+C 結束")
    ap.add_argument("--source", help="錄音來源（同 lec run --source）")
    ap.add_argument("--start", help="從第幾秒開始，例如 0:03")
    ap.add_argument("--duration", help="取多長，例如 10:00")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        die("找不到 ffmpeg")
    if args.record == bool(args.source_file):
        die("請給一個錄音檔，或用 --record 現場錄（兩者擇一）")

    src = record(RAW, args.source) if args.record else Path(args.source_file)
    if not src.is_file():
        die(f"找不到 {src}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    encode(src, out, args.start, args.duration)

    secs = probe(out)
    print(f"\n✔ {out}｜{hms(secs)}｜{out.stat().st_size / 1e6:.2f} MB")
    if not 7 * 60 <= secs <= 10 * 60:
        print(f"⚠ 長度 {hms(secs)} 偏離 8 分鐘不少，"
              f"可以用 --start / --duration 裁一段，或重念一次")
    print(f"  下一步：lec run 測試課 --file {show(out)}")


if __name__ == "__main__":
    main()
