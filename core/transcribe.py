"""課堂即時轉錄（VAD 版，由 live_transcribe.py 移植，行為不變）

ffmpeg 持續把音訊以 raw PCM 送進來 → 以能量 VAD 在「停頓處」切段（12–30 秒）
→ 立即送 whisper-server → 以自然段落追加到 transcript.md，並產出 transcript.srt。
只用 Python 標準函式庫。
"""
import array
import math
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

from .util import hms, log, opencc_available, opencc_convert

SR = 16000
FRAME_MS = 30
FRAME_SAMPLES = SR * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2

HALLUCINATIONS = re.compile(
    r"(字幕|訂閱|點贊|點讚|按讚|感謝觀看|謝謝觀看|明鏡|Amara|請不吝|小鈴鐺|優優獨播)")
END_PUNCT = "。！？!?.…"
ANY_PUNCT = END_PUNCT + "，、；：,;:「」『』（）()"


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
                    try:
                        cues.append((parse_ts(a), parse_ts(b), body))
                    except ValueError:
                        pass
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

    @property
    def total_seconds(self):
        return (self.pos + len(self.frames)) * FRAME_MS / 1000


# ---------------------------------------------------------------- 文件輸出
class TranscriptWriter:
    """把 whisper 片段組成自然段落，只做追加寫入（tail -f 與 Obsidian 皆可即時看）。"""

    prev_char = "。"

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
        self.sections = 0
        self.recent = deque(maxlen=3)
        if self.md.tell() == 0:
            today = datetime.now().strftime("%Y-%m-%d")
            self.md.write(f"---\ncourse: {course}\ndate: {today}\ntype: transcript\n"
                          f"tags: [逐字稿]\n---\n\n# {course} 逐字稿 {today}\n")
            self.md.flush()

    def convert(self, texts):
        return opencc_convert(texts) if self.opencc else texts

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
            if not text or (b - a < 0.05 and len(text) <= 1):
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
                    self.sections += 1
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

    def close(self):
        if self.para_len and self.prev_char not in END_PUNCT:
            self.md.write("。")
        self.md.write("\n")
        self.md.close()
        self.srt.close()


# ---------------------------------------------------------------- 轉錄
def _incomplete_utf8_tail(bs):
    """回傳結尾「不完整 UTF-8 字元」的位元組數（0–3）。"""
    for i in range(1, min(4, len(bs)) + 1):
        b = bs[-i]
        if b & 0xC0 == 0x80:          # 延續位元組，繼續往前找開頭
            continue
        if b >= 0xC0:
            need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            return i if need > i else 0
        return 0
    return 0


def decode_srt(raw):
    """whisper.cpp 會把一個中文字的 UTF-8 位元組拆在相鄰兩句字幕之間，
    直接解碼會失敗（整段遺失）。這裡把句尾不完整的位元組搬到下一句開頭再解碼，
    仍無法修復的位元組直接丟棄。"""
    blocks = re.split(rb"\r?\n\s*\r?\n", raw.strip())
    out, carry = [], b""
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        idx = next((i for i, l in enumerate(lines) if b"-->" in l), None)
        if idx is None:
            continue
        body = carry + b"".join(l.strip() for l in lines[idx + 1:])
        cut = _incomplete_utf8_tail(body)
        carry = body[len(body) - cut:] if cut else b""
        body = body[:len(body) - cut] if cut else body
        head = [l.decode("utf-8", "replace") for l in lines[:idx + 1]]
        text = body.decode("utf-8", "replace").replace("\ufffd", "")
        out.append("\n".join(head + [text]))
    return "\n\n".join(out) + "\n"


def transcribe_request(server, wav_path, prompt):
    cmd = ["curl", "-sS", "--fail", "--max-time", "300", f"{server}/inference",
           "-F", f"file=@{wav_path}", "-F", "response_format=srt",
           "-F", "temperature=0.0", "-F", f"prompt={prompt}"]
    for attempt in range(3):
        # start_new_session：Ctrl+C 不會打斷正在進行的轉錄請求
        # 以位元組讀取：whisper 輸出可能含被拆開的 UTF-8 字元，交給 decode_srt 修復
        r = subprocess.run(cmd, capture_output=True, start_new_session=True)
        if r.returncode == 0 and not r.stdout.lstrip().startswith(b"{"):
            return decode_srt(r.stdout)
        err = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
        log(f"  ⚠ 轉錄請求失敗（第 {attempt + 1} 次）: {err[:200]}")
        time.sleep(2)
    return ""


class Transcriber:
    """一次錄音（或一個音檔）的轉錄流程。run() 會阻塞到結束；request_stop() 可由 signal handler 呼叫。"""

    def __init__(self, cfg, session_dir, server_url, input_file=None, status=None):
        self.cfg = cfg
        self.session = Path(session_dir)
        self.server = server_url
        self.file = str(input_file) if input_file else ""
        self.live = not self.file
        self.status = status
        self.prompt = cfg.whisper_prompt()

        v = cfg["vad"]
        self.min_chunk, self.max_chunk = v["min_chunk"], v["max_chunk"]
        if not self.live:   # 處理檔案時不求即時，段落拉長填滿 whisper 的 30 秒視窗，效率較高
            self.min_chunk = max(self.min_chunk, min(v["file_min_chunk"], self.max_chunk - 4))
        self.min_voiced = v["min_voiced"]
        self.chunker = VadChunker(self.min_chunk, self.max_chunk, v["silence_ms"], v["sensitivity"])

        t = cfg["transcript"]
        self.use_opencc = bool(cfg["system"]["opencc"] and opencc_available())
        self.writer = TranscriptWriter(self.session, cfg.course_name, t["section_minutes"] * 60,
                                       t["para_gap"], t["para_max"], self.use_opencc)
        self.q = queue.Queue()
        self.proc = None
        self.abort = False
        self.stopping = False
        self.stats = {"audio": 0.0, "work": 0.0}
        self.tmp_dir = self.session / ".tmp"
        self.tmp_dir.mkdir(exist_ok=True)

    # -- 控制
    def request_stop(self):
        """第一次 Ctrl+C：即時模式停止錄音、轉完剩餘段落；檔案模式捨棄佇列、完成目前這段。"""
        if self.stopping:
            return
        self.stopping = True
        if self.live:
            log("▶ 停止錄音，處理剩餘音訊中…（再按一次 Ctrl+C 強制結束）")
            if self.proc and self.proc.poll() is None:
                self.proc.send_signal(signal.SIGINT)     # 讓 ffmpeg 正常寫完 ogg
        else:
            self.abort = True
            dropped = 0
            try:
                while True:
                    self.q.get_nowait()
                    dropped += 1
            except queue.Empty:
                pass
            self.q.put(None)
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            log(f"▶ 中止處理，捨棄尚未轉錄的 {dropped} 段，完成目前這段後結束")

    def _update(self, **kv):
        if self.status:
            self.status.update(**kv)

    # -- 轉錄執行緒
    def _worker(self):
        tail = ""
        while True:
            item = self.q.get()
            if item is None:
                break
            start, pcm, voiced, cut_wall = item
            dur = len(pcm) / 2 / SR
            if voiced < self.min_voiced:
                log(f"{hms(start)} 略過 {dur:4.1f}s（幾乎無人聲）")
                continue
            try:
                tail = self._process(start, pcm, dur, cut_wall, tail)
            except Exception as e:                      # 單段失敗不影響後續
                log(f"  ⚠ {hms(start)} 這段處理失敗：{e}")
                if self.status:
                    self.status.error(f"轉錄 {hms(start)} 失敗：{e}")

    def _process(self, start, pcm, dur, cut_wall, tail):
        wav_path = self.tmp_dir / "chunk.wav"
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm)
        t0 = time.time()
        srt = transcribe_request(self.server, wav_path, (self.prompt + tail[-100:]).strip())
        cues = [(start + a, start + b, t) for a, b, t in parse_srt(srt)]
        new_tail = self.writer.add(cues)
        if new_tail:
            tail = new_tail
        self.stats["audio"] += dur
        self.stats["work"] += time.time() - t0
        lag = time.time() - cut_wall
        lag_s = f"｜延遲 {lag:4.1f}s" if self.live else ""
        log(f"{hms(start)} +{dur:4.1f}s → {len(cues):2d} 句，轉錄 {time.time() - t0:4.1f}s"
            f"{lag_s}｜佇列 {self.q.qsize()}")
        self._update(transcribed=round(start + dur, 1), queue=self.q.qsize(),
                     transcribe_lag=round(lag, 1) if self.live else None,
                     sections_total=self.writer.sections)
        return tail

    # -- 主流程
    def run(self):
        if self.live:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-f", "pulse", "-i", self.cfg.audio_source(),
                   "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
            if self.cfg["audio"]["keep_recording"]:
                rec = self.session / f"recording_{datetime.now():%H%M%S}.ogg"
                cmd += ["-ac", "1", "-ar", str(SR), "-c:a", "libopus", "-b:a", "32k", str(rec)]
        else:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", self.file,
                   "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]

        th = threading.Thread(target=self._worker, daemon=True)
        th.start()
        log(f"VAD 已啟用（每段 {self.min_chunk:.0f}–{self.max_chunk:.0f} 秒，"
            f"停頓 ≥{self.cfg['vad']['silence_ms']}ms 切段）"
            f"{'，OpenCC 繁體轉換開啟' if self.use_opencc else ''}")
        if self.live:
            log(f"🎙 錄音中（{self.cfg['audio']['source']}）。按 Ctrl+C 結束。")

        # start_new_session：由我們決定何時送 SIGINT 給 ffmpeg（UI 只會對 lec 的 pid 送訊號）
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, start_new_session=True)
        if self.stopping and self.live:
            self.proc.send_signal(signal.SIGINT)
        buf = b""
        last_update = 0.0
        while True:
            data = self.proc.stdout.read(FRAME_BYTES * 10)
            if not data:
                break
            if self.abort:
                continue
            buf += data
            while len(buf) >= FRAME_BYTES:
                frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                out = self.chunker.push(frame)
                if out:
                    self.q.put((*out, time.time()))
            now = time.time()
            if now - last_update >= 1:
                last_update = now
                self._update(elapsed=round(self.chunker.total_seconds, 1), queue=self.q.qsize())
        rc = self.proc.wait()
        if rc not in (0, 255, -2, -15) and not self.stopping:
            log(f"⚠ ffmpeg 結束代碼 {rc}（音源或音檔可能有問題）")
            if self.status:
                self.status.error(f"ffmpeg 結束代碼 {rc}")
        last = self.chunker.flush()
        if last and len(last[1]) > SR and not self.abort:        # 最後不足 1 秒就不送
            self.q.put((*last, time.time()))
        self.q.put(None)
        th.join()
        self.writer.close()
        wav = self.tmp_dir / "chunk.wav"
        wav.unlink(missing_ok=True)
        total = self.chunker.pos * FRAME_MS / 1000
        speed = self.stats["audio"] / self.stats["work"] if self.stats["work"] else 0
        log(f"✔ 轉錄完成：總長 {hms(total)}，轉錄 {speed:.1f}x 速")
        self._update(elapsed=round(total, 1), queue=0, sections_total=self.writer.sections)
        return {"duration": total, "speed": speed, "aborted": self.abort}
