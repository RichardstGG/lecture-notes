#!/usr/bin/env python3
"""課堂即時轉錄（VAD 版）

ffmpeg 持續把音訊以 raw PCM 送進來 → 以能量 VAD 在「停頓處」切段（12–30 秒）
→ 立即送 whisper-server → 以自然段落追加到 transcript.md，並產出 transcript.srt。
只用 Python 標準函式庫。
"""
import argparse
import array
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

SR = 16000
FRAME_MS = 30
FRAME_SAMPLES = SR * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2

HALLUCINATIONS = re.compile(
    r"(字幕|訂閱|點贊|點讚|按讚|感謝觀看|謝謝觀看|明鏡|Amara|請不吝|小鈴鐺|優優獨播)")
END_PUNCT = "。！？!?.…"
ANY_PUNCT = END_PUNCT + "，、；：,;:「」『』（）()"


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def hms(t):
    t = int(max(t, 0))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def srt_ts(t):
    ms = round(max(t, 0) * 1000)
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_ts(ts):
    h, m, rest = ts.strip().split(":")
    s, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if "-->" in line:
                a, b = line.split("-->")
                body = "".join(x.strip() for x in lines[i + 1:])
                if body:
                    cues.append((parse_ts(a), parse_ts(b), body))
                break
    return cues


def normalize_punct(t):
    """whisper 常混用半形標點：中文語境改成全形，保留數字與英文內的 , . :"""
    t = t.strip()
    t = re.sub(r"\.{2,}|。{2,}", "…", t)
    t = re.sub(r"(?<!\d),|,(?!\d)", "，", t)
    t = re.sub(r"(?<![A-Za-z0-9])\.(?!\.)|(?<=[A-Za-z0-9])\.(?![A-Za-z0-9.])", "。", t)
    t = re.sub(r"(?<!\d):|:(?!\d)", "：", t)
    t = t.replace("?", "？").replace("!", "！").replace(";", "；")
    t = re.sub(r"\s*([，。？！：；、])\s*", r"\1", t)
    t = re.sub(r"[，、]+([。？！])", r"\1", t)
    t = re.sub(r"([，。？！：；、])\1+", r"\1", t)
    return t


def frame_rms(frame):
    a = array.array("h", frame)
    if sys.byteorder == "big":
        a.byteswap()
    if not a:
        return 0.0
    return math.sqrt(sum(x * x for x in a) / len(a))


# ---------------------------------------------------------------- VAD 切段
class VadChunker:
    """以自適應噪音底線判斷靜音，在停頓中點切段。"""

    def __init__(self, min_s, max_s, silence_ms, sensitivity):
        self.min_frames = int(min_s * 1000 / FRAME_MS)
        self.max_frames = int(max_s * 1000 / FRAME_MS)
        self.sil_frames = max(1, int(silence_ms / FRAME_MS))
        self.ratio = sensitivity           # 高於噪音底線幾倍算有聲
        self.history = deque(maxlen=int(15000 / FRAME_MS))  # 最近 15 秒 RMS
        self.frames, self.rms, self.silent = [], [], []
        self.run = 0
        self.pos = 0                       # 目前 buffer 起點（以 frame 計）
        self._thr = 300.0
        self._tick = 0

    def threshold(self):
        self._tick += 1
        if self._tick % 10 == 0 and len(self.history) >= 20:   # 每 300ms 更新一次
            floor = sorted(self.history)[len(self.history) // 10]  # 第 10 百分位
            self._thr = max(floor * self.ratio, 60.0)
        return self._thr

    def push(self, frame):
        r = frame_rms(frame)
        self.history.append(r)
        is_sil = r < self.threshold()
        self.frames.append(frame)
        self.rms.append(r)
        self.silent.append(is_sil)
        self.run = self.run + 1 if is_sil else 0

        n = len(self.frames)
        if n >= self.min_frames and self.run >= self.sil_frames:
            return self._cut(n - self.run // 2)
        if n >= self.max_frames:
            return self._cut(self._best_cut())
        return None

    def _best_cut(self):
        n = len(self.frames)
        lo = max(self.min_frames // 2, n - int(8000 / FRAME_MS))
        best, best_len, run = None, 0, 0
        for i in range(lo, n):             # 最後 8 秒內最長的靜音
            run = run + 1 if self.silent[i] else 0
            if run > best_len:
                best_len, best = run, i - run // 2
        if best is not None and best_len >= 3:
            return best
        w = 10                             # 沒有靜音：找最安靜的 300ms
        scores = [(sum(self.rms[i:i + w]), i + w // 2) for i in range(lo, n - w)]
        return min(scores)[1] if scores else n

    def _cut(self, idx):
        chunk = b"".join(self.frames[:idx])
        voiced = self.silent[:idx].count(False) / max(idx, 1)
        start = self.pos
        self.pos += idx
        self.frames = self.frames[idx:]
        self.rms = self.rms[idx:]
        self.silent = self.silent[idx:]
        self.run = 0
        for s in reversed(self.silent):
            if not s:
                break
            self.run += 1
        return start * FRAME_MS / 1000, chunk, voiced

    def flush(self):
        if not self.frames:
            return None
        return self._cut(len(self.frames))


# ---------------------------------------------------------------- 文件輸出
class TranscriptWriter:
    """把 whisper 片段組成自然段落，只做追加寫入（tail -f 與 Obsidian 皆可即時看）。"""

    def __init__(self, session, course, section_s, para_gap, para_max, opencc):
        self.md = (session / "transcript.md").open("a", encoding="utf-8")
        self.srt = (session / "transcript.srt").open("a", encoding="utf-8")
        self.section_s = section_s
        self.para_gap = para_gap
        self.para_max = para_max
        self.opencc = opencc
        self.cue_no = 0
        self.last_end = -1e9
        self.para_len = 0
        self.next_section = 0.0
        self.recent = deque(maxlen=3)
        if self.md.tell() == 0:
            today = datetime.now().strftime("%Y-%m-%d")
            self.md.write(f"---\ncourse: {course}\ndate: {today}\ntype: transcript\n"
                          f"tags: [逐字稿]\n---\n\n# {course} 逐字稿 {today}\n")
            self.md.flush()

    def convert(self, texts):
        if not self.opencc or not texts:
            return texts
        try:
            out = subprocess.run(["opencc", "-c", "s2twp.json"], input="\n".join(texts),
                                 capture_output=True, text=True, timeout=20, check=True).stdout
            lines = out.rstrip("\n").split("\n")
            if len(lines) != len(texts):
                return texts
            return [l.replace("臺", "台") for l in lines]   # 台灣日常寫法用「台」
        except Exception:
            return texts

    @staticmethod
    def split_sentences(a, b, text):
        """把一個長片段依句號切成多句，時間依字數比例分配。"""
        parts = [p for p in re.findall(r"[^。！？]+[。！？]*", text) if p.strip()]
        if len(parts) <= 1:
            return [(a, b, text)]
        total = sum(len(p) for p in parts)
        out, t = [], a
        for p in parts:
            d = (b - a) * len(p) / total
            out.append((t, t + d, p.strip()))
            t += d
        return out

    def add(self, cues):
        cues = [(a, b, normalize_punct(t)) for a, b, t in cues]
        cues = [c for c in cues if c[2] and not HALLUCINATIONS.search(c[2])]
        texts = self.convert([c[2] for c in cues])
        units = []
        for (a, b, _), text in zip(cues, texts):
            units += self.split_sentences(a, b, text)
        tail = ""
        for a, b, text in units:
            if b - a < 0.05 and len(text) <= 1:
                continue
            if list(self.recent).count(text) >= 2:      # whisper 重複迴圈
                continue
            self.recent.append(text)

            self.cue_no += 1
            self.srt.write(f"{self.cue_no}\n{srt_ts(a)} --> {srt_ts(b)}\n{text}\n\n")

            new_section = a >= self.next_section
            new_para = (new_section or self.para_len == 0
                        or (a - self.last_end >= self.para_gap and self.para_len >= 40)
                        or self.para_len >= self.para_max)
            if new_para:
                if self.para_len:
                    if self.prev_char not in END_PUNCT:
                        self.md.write("。")
                    self.md.write("\n")
                if new_section:
                    self.md.write(f"\n## {hms(a)}\n")
                    while self.next_section <= a:
                        self.next_section += self.section_s
                self.md.write(f"\n`{hms(a)}` {text}")
                self.para_len = len(text)
            else:
                if self.prev_char not in ANY_PUNCT and text[0] not in ANY_PUNCT:
                    sep = " " if (self.prev_char.isascii() and text[0].isascii()) else "，"
                    self.md.write(sep)
                self.md.write(text)
                self.para_len += len(text)
            self.prev_char = text[-1]
            self.last_end = b
            tail += text
        self.md.flush()
        self.srt.flush()
        return tail

    prev_char = "。"

    def close(self):
        if self.para_len and self.prev_char not in END_PUNCT:
            self.md.write("。")
        self.md.write("\n")
        self.md.close()
        self.srt.close()


# ---------------------------------------------------------------- 轉錄執行緒
def transcribe(server, wav_path, prompt):
    cmd = ["curl", "-sS", "--fail", "--max-time", "300", f"{server}/inference",
           "-F", f"file=@{wav_path}", "-F", "response_format=srt",
           "-F", "temperature=0.0", "-F", f"prompt={prompt}"]
    for attempt in range(3):
        # start_new_session：Ctrl+C 不會打斷正在進行的轉錄請求
        r = subprocess.run(cmd, capture_output=True, text=True, start_new_session=True)
        if r.returncode == 0 and not r.stdout.lstrip().startswith("{"):
            return r.stdout
        log(f"  ⚠ 轉錄請求失敗（第 {attempt + 1} 次）: {(r.stderr or r.stdout).strip()[:200]}")
        time.sleep(2)
    return ""


def worker(q, args, writer, tmp_dir, stats):
    tail = ""
    while True:
        item = q.get()
        if item is None:
            break
        start, pcm, voiced, cut_wall = item
        dur = len(pcm) / 2 / SR
        if voiced < args.min_voiced:
            log(f"{hms(start)} 略過 {dur:4.1f}s（幾乎無人聲）")
            continue
        try:
            tail = process_chunk(start, pcm, dur, cut_wall, q, args, writer, tmp_dir, stats, tail)
        except Exception as e:                      # 單段失敗不影響後續
            log(f"  ⚠ {hms(start)} 這段處理失敗：{e}")
    wav = tmp_dir / "chunk.wav"
    if wav.exists():
        wav.unlink()


def process_chunk(start, pcm, dur, cut_wall, q, args, writer, tmp_dir, stats, tail):
    wav_path = tmp_dir / "chunk.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    t0 = time.time()
    srt = transcribe(args.server, wav_path, (args.prompt + tail[-100:]).strip())
    cues = [(start + a, start + b, t) for a, b, t in parse_srt(srt)]
    new_tail = writer.add(cues)
    if new_tail:
        tail = new_tail
    stats["audio"] += dur
    stats["work"] += time.time() - t0
    lag = f"｜延遲 {time.time() - cut_wall:4.1f}s" if args.live else ""
    log(f"{hms(start)} +{dur:4.1f}s → {len(cues):2d} 句，轉錄 {time.time() - t0:4.1f}s"
        f"{lag}｜佇列 {q.qsize()}")
    return tail


# ---------------------------------------------------------------- 主程式
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--course", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--source", default="default")
    ap.add_argument("--file", default="")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--min-chunk", type=float, default=12)
    ap.add_argument("--max-chunk", type=float, default=30)
    ap.add_argument("--file-min-chunk", type=float, default=24)
    ap.add_argument("--silence-ms", type=int, default=500)
    ap.add_argument("--sensitivity", type=float, default=2.5)
    ap.add_argument("--min-voiced", type=float, default=0.08)
    ap.add_argument("--section-min", type=float, default=5)
    ap.add_argument("--para-gap", type=float, default=2.5)
    ap.add_argument("--para-max", type=int, default=220)
    ap.add_argument("--keep-recording", type=int, default=1)
    ap.add_argument("--opencc", type=int, default=1)
    args = ap.parse_args()
    args.live = not args.file
    if not args.live:   # 處理檔案時不求即時，段落拉長填滿 whisper 的 30 秒視窗，效率較高
        args.min_chunk = max(args.min_chunk, min(args.file_min_chunk, args.max_chunk - 4))

    session = Path(args.session)
    session.mkdir(parents=True, exist_ok=True)
    tmp_dir = session / ".tmp"
    tmp_dir.mkdir(exist_ok=True)

    use_opencc = bool(args.opencc and shutil.which("opencc"))
    writer = TranscriptWriter(session, args.course, args.section_min * 60,
                              args.para_gap, args.para_max, use_opencc)
    chunker = VadChunker(args.min_chunk, args.max_chunk, args.silence_ms, args.sensitivity)

    if args.live:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "pulse", "-i", args.source,
               "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
        if args.keep_recording:
            rec = session / f"recording_{datetime.now():%H%M%S}.ogg"
            cmd += ["-ac", "1", "-ar", str(SR), "-c:a", "libopus", "-b:a", "32k", str(rec)]
    else:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", args.file,
               "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]

    stop = {"count": 0}
    q = queue.Queue()

    def on_sigint(*_):
        stop["count"] += 1
        if stop["count"] == 1:
            print(flush=True)
            if args.live:
                log("▶ 停止錄音，處理剩餘音訊中…（再按一次 Ctrl+C 強制結束）")
            else:
                dropped = 0
                try:
                    while True:
                        q.get_nowait()
                        dropped += 1
                except queue.Empty:
                    pass
                stop["file_abort"] = True
                q.put(None)          # 讓 worker 做完目前這段就結束
                log(f"▶ 中止處理，捨棄尚未轉錄的 {dropped} 段，完成目前這段後結束")
        else:
            log("✖ 強制結束")
            os._exit(130)

    signal.signal(signal.SIGINT, on_sigint)

    stats = {"audio": 0.0, "work": 0.0}
    th = threading.Thread(target=worker, args=(q, args, writer, tmp_dir, stats), daemon=True)
    th.start()

    log(f"VAD 已啟用（每段 {args.min_chunk:.0f}–{args.max_chunk:.0f} 秒，停頓 ≥{args.silence_ms}ms 切段）"
        f"{'，OpenCC 繁體轉換開啟' if use_opencc else ''}")
    if args.live:
        log(f"🎙 錄音中（{args.source}）。按 Ctrl+C 結束。")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    buf = b""
    while True:
        data = proc.stdout.read(FRAME_BYTES * 10)
        if not data:
            break
        buf += data
        while len(buf) >= FRAME_BYTES:
            frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
            out = chunker.push(frame)
            if out and not stop.get("file_abort"):
                q.put((*out, time.time()))
    proc.wait()
    last = chunker.flush()
    if last and len(last[1]) > SR and not stop.get("file_abort"):        # 最後不足 1 秒就不送
        q.put((*last, time.time()))
    q.put(None)
    th.join()
    writer.close()
    try:
        tmp_dir.rmdir()
    except OSError:
        pass
    speed = stats["audio"] / stats["work"] if stats["work"] else 0
    log(f"✔ 完成：總長 {hms(chunker.pos * FRAME_MS / 1000)}，轉錄 {speed:.1f}x 速")


if __name__ == "__main__":
    main()
