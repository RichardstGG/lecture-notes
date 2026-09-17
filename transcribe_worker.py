#!/usr/bin/env python3
"""監看 segments/ 資料夾，依序把完成的分段送到 whisper-server，
合併成帶絕對時間戳的 transcript.srt 與 transcript.md。只用標準函式庫。"""
import argparse
import json
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 靜音時 whisper 常見的幻覺句
HALLUCINATIONS = re.compile(
    r"(字幕|訂閱|點贊|點讚|按讚|感謝觀看|謝謝觀看|明鏡|Amara|請不吝|小鈴鐺)"
)


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def ts_to_sec(ts):
    h, m, rest = ts.split(":")
    s, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def sec_to_srt(t):
    t = max(t, 0)
    ms = round(t * 1000)
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def sec_to_hms(t):
    t = int(max(t, 0))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def parse_srt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if "-->" in line:
                a, b = [x.strip() for x in line.split("-->")]
                body = " ".join(lines[i + 1:]).strip()
                if body:
                    cues.append((ts_to_sec(a), ts_to_sec(b), body))
                break
    return cues


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def build_input(prev, cur, overlap, tmp):
    """把上一段最後 overlap 秒接在本段前面。回傳 (檔案, 實際重疊秒數)。"""
    if prev is None or overlap <= 0:
        return cur, 0.0
    ov = min(overlap, duration(prev))
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-sseof", f"-{ov}", "-i", str(prev), "-i", str(cur),
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)],
        check=True)
    return tmp, ov


def transcribe(server, wav, prompt):
    cmd = ["curl", "-sS", "--fail", "--max-time", "900", f"{server}/inference",
           "-F", f"file=@{wav}",
           "-F", "response_format=srt",
           "-F", "temperature=0.0",
           "-F", f"prompt={prompt}"]
    for attempt in range(3):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            body = r.stdout
            if body.lstrip().startswith("{"):   # 伺服器回傳 JSON 錯誤
                raise RuntimeError(json.loads(body))
            return body
        log(f"  轉錄請求失敗（第 {attempt + 1} 次）: {r.stderr.strip()}")
        time.sleep(3)
    raise RuntimeError("whisper-server 連續失敗 3 次")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--course", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--prompt", default="")
    ap.add_argument("--overlap", type=float, default=4)
    ap.add_argument("--keep", type=int, default=1)
    args = ap.parse_args()

    signal.signal(signal.SIGINT, signal.SIG_IGN)

    session = Path(args.session)
    seg_dir = session / "segments"
    srt_path = session / "transcript.srt"
    md_path = session / "transcript.md"
    tmp = seg_dir / "_overlap_tmp.wav"
    done_flag = seg_dir / "DONE"

    if not md_path.exists():
        md_path.write_text(
            "---\n"
            f"course: {args.course}\n"
            f"date: {datetime.now():%Y-%m-%d}\n"
            "type: transcript\n"
            "tags: [逐字稿]\n"
            "---\n\n"
            f"# {args.course} 逐字稿 {datetime.now():%Y-%m-%d}\n\n",
            encoding="utf-8")

    idx = 0
    seg_start = 0.0      # 本段在整堂課中的起始秒數
    last_end = 0.0       # 已寫出的最後一句結束時間（去除重疊重複用）
    cue_no = 0
    tail_text = ""       # 最近的文字，當下一段的 prompt
    prev_path = None

    log(f"worker 啟動，監看 {seg_dir}")
    while True:
        cur = seg_dir / f"seg_{idx:03d}.wav"
        nxt = seg_dir / f"seg_{idx + 1:03d}.wav"
        finished = done_flag.exists()

        if not cur.exists():
            if finished:
                break
            time.sleep(2)
            continue
        # 下一段已出現、或錄音已結束，才代表本段寫完
        if not (nxt.exists() or finished):
            time.sleep(2)
            continue

        try:
            cur_dur = duration(cur)
        except Exception:
            cur_dur = 0.0
        if cur_dur < 0.5:
            log(f"seg_{idx:03d} 太短（{cur_dur:.1f}s），略過")
        else:
            t0 = time.time()
            wav, ov = build_input(prev_path, cur, args.overlap, tmp)
            prompt = (args.prompt + " " + tail_text).strip()
            srt = transcribe(args.server, wav, prompt)
            offset = seg_start - ov

            new_cues = []
            for a, b, body in parse_srt(srt):
                a, b = a + offset, b + offset
                if b <= last_end + 0.3:          # 重疊區已寫過的句子
                    continue
                if HALLUCINATIONS.search(body):
                    continue
                new_cues.append((max(a, last_end), b, body))

            with srt_path.open("a", encoding="utf-8") as f:
                for a, b, body in new_cues:
                    cue_no += 1
                    f.write(f"{cue_no}\n{sec_to_srt(a)} --> {sec_to_srt(b)}\n{body}\n\n")
            with md_path.open("a", encoding="utf-8") as f:
                f.write(f"## {sec_to_hms(seg_start)}\n\n")
                for a, _, body in new_cues:
                    f.write(f"`{sec_to_hms(a)}` {body}\n")
                f.write("\n")

            if new_cues:
                last_end = new_cues[-1][1]
                tail_text = "".join(c[2] for c in new_cues)[-120:]
            elapsed = time.time() - t0
            log(f"seg_{idx:03d} 完成：{len(new_cues)} 句，"
                f"音檔 {cur_dur:.0f}s / 耗時 {elapsed:.0f}s（{cur_dur / max(elapsed, 0.1):.1f}x）")

        if prev_path is not None and not args.keep:
            prev_path.unlink(missing_ok=True)
        prev_path = cur
        seg_start += cur_dur
        idx += 1

    tmp.unlink(missing_ok=True)
    if prev_path is not None and not args.keep:
        prev_path.unlink(missing_ok=True)
    log(f"全部完成，共 {idx} 段、{cue_no} 句，總長 {sec_to_hms(seg_start)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"❌ 錯誤: {e}")
        sys.exit(1)
