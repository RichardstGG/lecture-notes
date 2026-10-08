"""課堂即時轉錄（VAD 版，由 live_transcribe.py 移植，行為不變）

ffmpeg 持續把音訊以 raw PCM 送進來 → 以能量 VAD 在「停頓處」切段（12–30 秒）
→ 立即送 whisper-server → 以自然段落追加到 transcript.md，並產出 transcript.srt。
只用 Python 標準函式庫。

轉錄失敗的段落不會被默默丟掉：耗盡重試後丟出 TranscribeError，由 _worker 記成
一個「缺口」——寫進 session.log、透過 status.error() 進 events.jsonl，並在
transcript.md 留下看得見的標記。run() 的回傳值也會帶上缺口與 ffmpeg 結束代碼。
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
import urllib.error
import urllib.request
import uuid
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

from . import capture as CAP
from . import platform as P
from .util import hms, log, opencc_available, opencc_convert

SR = 16000
FRAME_MS = 30
FRAME_SAMPLES = SR * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2
READ_BLOCK = FRAME_BYTES * 10
# 落後這麼多個 READ_BLOCK（約 30 秒音訊）就提醒一次；不丟音訊，只是讓它可見。
BACKLOG_WARN_BLOCKS = int(30 * SR * 2 / READ_BLOCK)
# 擷取遺失超過這個比例就當成錯誤回報（存檔是會後處理唯一的來源）。
CAPTURE_LOSS_ERROR = 0.02
# 低於這個比例不吭聲：ogg/opus 的 granule 取整本來就會留下一堆微小空隙，
# 開發機上 10 份正常錄音量到 0.000–0.057%，都是無害的。
CAPTURE_LOSS_WARN = 0.005

HALLUCINATIONS = re.compile(
    r"(字幕|訂閱|點贊|點讚|按讚|感謝觀看|謝謝觀看|明鏡|Amara|請不吝|小鈴鐺|優優獨播)")
END_PUNCT = "。！？!?.…"
# transcript.md 裡標記「這段沒有逐字稿」的開頭；刻意不用 ## 與 `hh:mm:ss`，
# 才不會被 summarize.py 的 HEADER_RE／STAMP_RE 當成小標題或時間錨點。
GAP_MARK = "> ⚠ 轉錄失敗"
ANY_PUNCT = END_PUNCT + "，、；：,;:「」『』（）()"


class TranscribeError(Exception):
    """一段音訊轉錄失敗，且已耗盡重試。"""


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
        self.gaps = 0
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

    def _end_paragraph(self):
        """收掉目前段落，讓下一次寫入從新段落開始。"""
        if self.para_len:
            if self.prev_char not in END_PUNCT:
                self.md.write("。")
            self.md.write("\n")
        self.para_len = 0
        self.prev_char = "。"

    def add_gap(self, start, end, reason):
        """在逐字稿裡留下看得見的缺口。srt 不寫（沒有內容可當字幕）。

        標記自成一段，後面空一行，免得下一段被 Markdown 併進這個引用區塊。"""
        self._end_paragraph()
        self.md.write(f"\n{GAP_MARK}，{hms(start)}–{hms(end)}"
                      f"（{max(end - start, 0):.1f} 秒）沒有逐字稿：{reason}\n")
        self.md.flush()
        self.gaps += 1

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


def capture_gap_report(packets, min_gap=0.001):
    """擷取完整性：封包時長總和 vs 時間戳跨距，差多少就是錄音時被丟掉的音訊。

    `packets` 是 (pts_seconds, duration_seconds) 的可迭代物（可以是 generator，
    三小時的錄音有五十幾萬個封包，不要整份讀進記憶體）。

    為什麼需要這個：ffmpeg 的擷取緩衝溢出時會直接丟音訊，但**結束代碼仍然是 0**，
    逐字稿與存檔都看不出異常。macOS 實機上量到一份 87.7 分鐘的錄音只錄到 70.0
    分鐘的音訊（5,196 個空隙、每秒固定掉約 0.2 秒），而當時完全沒有任何警告。
    """
    total = span_start = span_end = 0.0
    gaps = lost = max_gap = 0.0
    gap_count = 0
    spacing_sum = 0.0
    spacing_count = 0
    prev_end = None
    prev_gap_at = None
    first = True
    for pts, dur in packets:
        if first:
            span_start = pts
            first = False
        total += dur
        if prev_end is not None:
            g = pts - prev_end
            if g >= min_gap:
                gap_count += 1
                lost += g
                max_gap = max(max_gap, g)
                if prev_gap_at is not None:
                    spacing_sum += pts - prev_gap_at
                    spacing_count += 1
                prev_gap_at = pts
        prev_end = pts + dur
        span_end = prev_end
    span = max(span_end - span_start, 0.0)
    return {
        "audio_seconds": round(total, 3),
        "span_seconds": round(span, 3),
        "lost_seconds": round(lost, 3),
        "lost_ratio": round(lost / span, 6) if span > 0 else 0.0,
        "gap_count": gap_count,
        "max_gap_seconds": round(max_gap, 3),
        # 規律的間距代表系統性丟失（某個週期性動作把擷取卡住），
        # 不規律代表偶發的負載尖峰。診斷時差很多。
        "mean_gap_spacing_seconds": (round(spacing_sum / spacing_count, 3)
                                     if spacing_count else None),
    }


def _ffprobe_packets(path):
    """串流讀出 (pts, duration)；ffprobe 不在或讀不到就什麼都不產生。"""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_packets",
           "-show_entries", "packet=pts_time,duration_time",
           "-of", "csv=p=0", str(path)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return
    try:
        for line in proc.stdout:
            parts = line.decode("utf-8", "replace").strip().split(",")
            if len(parts) < 2:
                continue
            try:
                yield float(parts[0]), float(parts[1])
            except ValueError:
                continue
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        proc.wait()


def probe_capture(path):
    """對存檔跑完整性檢查；無法檢查時回傳 None（不要因此讓錄音失敗）。"""
    try:
        if not Path(path).is_file():
            return None
    except OSError:
        return None
    report = capture_gap_report(_ffprobe_packets(path))
    return report if report["span_seconds"] > 0 else None


def _multipart(fields, file_field, file_path):
    """自己組 multipart/form-data（只用標準函式庫，不依賴 curl）。"""
    boundary = "----lec" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                f"{v}\r\n").encode("utf-8")
    name = Path(file_path).name
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{name}\"\r\nContent-Type: audio/wav\r\n\r\n").encode("utf-8")
    out += Path(file_path).read_bytes()
    out += f"\r\n--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def transcribe_request(server, wav_path, prompt):
    """送到 whisper-server /inference。以位元組讀取回應：
    whisper 輸出可能含被拆開的 UTF-8 字元，交給 decode_srt 修復。

    重試用盡仍失敗就丟 TranscribeError。以前這裡回傳空字串，呼叫端分不出
    「這段真的沒人說話」與「這段轉錄失敗」，整段音訊會無聲消失。"""
    body, ctype = _multipart({"response_format": "srt", "temperature": "0.0",
                              "prompt": prompt}, "file", wav_path)
    req = urllib.request.Request(f"{server}/inference", data=body,
                                 headers={"Content-Type": ctype})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
            if not raw.lstrip().startswith(b"{"):
                return decode_srt(raw)
            err = raw.decode("utf-8", "replace").strip()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            err = str(e)
        log(f"  ⚠ 轉錄請求失敗（第 {attempt + 1} 次）: {err[:200]}")
        time.sleep(2)
    raise TranscribeError(err[:200])


class Transcriber:
    """一次錄音（或一個音檔）的轉錄流程。run() 會阻塞到結束；request_stop() 可由 signal handler 呼叫。"""

    # keep_recording 的存檔路徑，由 run() 設定；錄完會檢查完整性。
    # 放在類別上，繞過 __init__ 的子類別（測試 harness）也能安全取用。
    recording_path = None

    # 雙來源錄音（會議）的狀態；放在類別上，繞過 __init__ 的子類別（測試 harness）也安全。
    capture = None
    capture_sources = None
    capture_error = None
    capture_options = {}          # 測試用：注入 popen／binding 等 MultiCapture 參數

    def __init__(self, cfg, session_dir, server_url, input_file=None, status=None,
                 capture_sources=None):
        """capture_sources：兩個 core.capture.SourceSpec（system 與 mic）。

        有給就改走雙來源擷取：兩路分軌存在 <session>/tracks/，混音餵給既有的 VAD／Whisper；
        沒給就是原本的單來源流程，行為完全不變。只能用在即時錄音。
        """
        if capture_sources and input_file:
            raise ValueError("雙來源錄音只能用在即時錄音，不能同時指定 input_file")
        self.capture_sources = list(capture_sources) if capture_sources else None
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
        self.stats = {"audio": 0.0, "work": 0.0, "lost": 0.0}
        self.gaps = []              # 未轉錄的段落；也會寫進 transcript.md
        self.ffmpeg_returncode = None
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
            if self.capture is not None:      # 兩路各自收尾，分軌封裝完成後才會結束
                self.capture.request_stop()
            elif self.proc and self.proc.poll() is None:
                try:      # 讓 ffmpeg 正常寫完 ogg；Windows 無法送 SIGINT，只能終止
                    if P.IS_WINDOWS:
                        self.proc.terminate()
                    else:
                        self.proc.send_signal(signal.SIGINT)
                except OSError:
                    pass
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
            t0 = time.time()
            try:
                tail = self._process(start, pcm, dur, cut_wall, tail)
            except Exception as e:                      # 單段失敗不影響後續
                self.stats["work"] += time.time() - t0
                self._record_gap(start, dur, e)

    def _record_gap(self, start, dur, err):
        """這段音訊沒有進逐字稿：log、status.error、transcript.md 三邊都留紀錄。"""
        reason = str(err).strip() or err.__class__.__name__
        reason = " ".join(reason.split())[:200]
        self.gaps.append({"start": round(start, 1), "seconds": round(dur, 1),
                          "reason": reason})
        self.stats["lost"] += dur
        log(f"  ✖ {hms(start)} 這段未轉錄（{dur:.1f}s）：{reason}")
        try:
            self.writer.add_gap(start, start + dur, reason)
        except OSError as e:
            log(f"  ⚠ 缺口標記寫入失敗：{e}")
        if self.status:
            self.status.error(
                f"轉錄 {hms(start)} 失敗，{dur:.0f} 秒音訊未轉錄：{reason}")

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

    def _drain(self, sink, read=None):
        """只做一件事：把 ffmpeg 的 stdout 盡快讀乾，別讓它因為我們而阻塞。
        read 預設讀 self.proc.stdout；雙來源模式改讀 MultiCapture.read（結束時回傳 b""）。"""
        read = read or (lambda: self.proc.stdout.read(READ_BLOCK))
        try:
            while True:
                data = read()
                if not data:
                    break
                sink.put(data)
        except (OSError, ValueError):            # pipe 被關掉就正常收尾
            pass
        finally:
            sink.put(None)

    # -- 雙來源
    def _capture_event(self, kind, **kv):
        """MultiCapture 的事件 → status／log。來源故障同時算一筆錯誤，UI 才看得到。"""
        if not self.status:
            return
        self.status.event(kind, **kv)
        if kind == "capture_source_failed":
            self.status.error(f"錄音來源故障（{kv['role']}，{kv['code']}）：{kv['detail']}")
        elif kind == "capture_error":
            self.status.error(f"錄音錯誤（{kv['code']}）：{kv['detail']}")

    def _start_capture(self):
        """啟動雙來源擷取；回傳讀取函式。開始時有任何一路沒成功就整個不開始，原因記在
        self.capture_error，讀取函式回傳空資料，後面走跟「沒有讀到任何音訊」相同的收尾。"""
        rec = None
        if self.cfg["audio"]["keep_recording"]:
            rec = self.session / f"recording_{datetime.now():%H%M%S}.ogg"
            self.recording_path = rec
        self.capture = CAP.MultiCapture(self.capture_sources, self.session, mix_path=rec,
                                        backend=self.cfg["audio"].get("backend"),
                                        on_event=self._capture_event,
                                        default_output=P.default_sink,
                                        **self.capture_options)
        if self.stopping:
            self.capture.request_stop()
        try:
            self.capture.start()
        except CAP.CaptureStartError as e:
            self.capture_error = e
            self.recording_path = None
            if e.code == "cancelled":
                log("▶ 啟動前就收到停止要求，沒有錄音")
            else:
                log(f"✖ 雙來源錄音無法開始（{e.code}）：{e}")
                if self.status:
                    self.status.error(f"雙來源錄音無法開始（{e.code}）：{e}")
                    self.status.event("capture_start_failed", **e.as_dict())
            return lambda: b""
        return self.capture.read

    def _report_capture_artifacts(self, report):
        """分軌與混音沒有封裝完整就明講：這些檔案之後會被當成重跑的來源。"""
        parts = [(s.get("track"), s) for s in report.get("sources", [])]
        if report.get("mix"):
            parts.append((report["mix"].get("path"), report["mix"]))
        for rel, rec in parts:
            if rel and not rec.get("complete"):
                msg = f"錄音檔 {rel} 封裝不完整：{rec.get('problem', '原因不明')}（不可當作重跑來源）"
                log(f"✖ {msg}")
                if self.status:
                    self.status.error(msg)

    # -- 主流程
    def run(self):
        if self.capture_sources:
            cmd = None
        elif self.live:
            source = P.resolve_source(self.cfg.audio_source(), self.cfg["audio"].get("backend"))
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                   *P.ffmpeg_input(source, self.cfg["audio"].get("backend")),
                   "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
            if self.cfg["audio"]["keep_recording"]:
                rec = self.session / f"recording_{datetime.now():%H%M%S}.ogg"
                self.recording_path = rec
                cmd += ["-ac", "1", "-ar", str(SR), "-c:a", "libopus", "-b:a", "32k", str(rec)]
        else:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", self.file,
                   "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]

        th = threading.Thread(target=self._worker, daemon=True)
        th.start()
        log(f"VAD 已啟用（每段 {self.min_chunk:.0f}–{self.max_chunk:.0f} 秒，"
            f"停頓 ≥{self.cfg['vad']['silence_ms']}ms 切段）"
            f"{'，OpenCC 繁體轉換開啟' if self.use_opencc else ''}")
        if self.capture_sources:
            read_pcm = self._start_capture()
            if self.capture_error is None:
                log("按 Ctrl+C 結束錄音。")
        else:
            if self.live:
                log(f"🎙 錄音中（{self.cfg['audio']['source']}）。按 Ctrl+C 結束。")

            # 讓 ffmpeg 不被終端機的 Ctrl+C 直接打斷，由我們決定何時停止它
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, **P.spawn_kwargs())
            if self.stopping and self.live:
                self.proc.send_signal(signal.SIGINT)
            read_pcm = None

        # ffmpeg 的 stdout 由專屬執行緒讀乾，VAD 與 status 寫檔都不在讀取路徑上。
        # 以前三件事在同一個迴圈：任何一次停頓都會讓 pipe 填滿 → ffmpeg 阻塞 →
        # 作業系統的擷取緩衝溢出 → 音訊被丟掉，而且**連 keep_recording 的存檔
        # 一起破**（存檔是會後處理唯一的來源）。macOS 實機上這條路徑每秒固定
        # 掉約 0.2 秒；Linux 的 PulseAudio 在 server 端有緩衝所以看不出來。
        raw = queue.Queue()
        reader = threading.Thread(target=self._drain, args=(raw, read_pcm), daemon=True)
        reader.start()

        buf = b""
        last_update = 0.0
        warned_backlog = False
        while True:
            data = raw.get()
            if data is None:
                break
            if not warned_backlog and raw.qsize() > BACKLOG_WARN_BLOCKS:
                warned_backlog = True
                secs = raw.qsize() * READ_BLOCK / 2 / SR
                log(f"⚠ 音訊處理落後約 {secs:.0f} 秒（記憶體會隨之成長）；"
                    f"錄音本身仍在繼續")
                if self.status:
                    self.status.event("audio_backlog", seconds=round(secs, 1))
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
        reader.join(timeout=5)
        capture_report = None
        if self.capture_sources:
            # 沒有單一 ffmpeg：「失敗」= 一開始就沒能開始，或兩路都沒撐到最後。
            # 只有一路故障算降級（degraded），錄音本身是成功的，細節在 capture_session。
            rc = None
            if self.capture_error is not None:
                capture_report = {"status": "failed", "start_error": self.capture_error.as_dict()}
                ffmpeg_failed = self.capture_error.code != "cancelled"
            else:
                capture_report = self.capture.report()
                ffmpeg_failed = capture_report["status"] == "failed"
            self.ffmpeg_returncode = rc
        else:
            rc = self.proc.wait()
            self.ffmpeg_returncode = rc
            ffmpeg_failed = rc not in (0, 255, -2, -15) and not self.stopping
            if ffmpeg_failed:
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
        lost = round(self.stats["lost"], 1)
        if self.gaps:
            log(f"⚠ 轉錄結束：總長 {hms(total)}，轉錄 {speed:.1f}x 速；"
                f"有 {len(self.gaps)} 段共 {lost:.0f} 秒沒有轉錄成功，"
                f"逐字稿中已標出缺口")
        elif ffmpeg_failed and total == 0:
            why = "雙來源擷取失敗，原因見上方" if self.capture_sources else f"ffmpeg 結束代碼 {rc}"
            log(f"✖ 沒有讀到任何音訊（{why}），逐字稿是空的")
        elif ffmpeg_failed:
            why = "兩路來源都故障了" if self.capture_sources else f"ffmpeg 結束代碼 {rc}"
            log(f"⚠ 轉錄結束：總長 {hms(total)}，轉錄 {speed:.1f}x 速；"
                f"但{why}，錄音或音檔可能不完整")
        else:
            log(f"✔ 轉錄完成：總長 {hms(total)}，轉錄 {speed:.1f}x 速")
        capture = self._check_capture()
        if self.capture_sources:
            self._report_capture_artifacts(capture_report)
        self._update(elapsed=round(total, 1), queue=0, sections_total=self.writer.sections)
        result = {"duration": total, "speed": speed, "aborted": self.abort,
                  "gaps": list(self.gaps), "lost_seconds": lost,
                  "ffmpeg_returncode": rc, "ffmpeg_failed": ffmpeg_failed,
                  "capture": capture}
        if self.capture_sources:
            # 只有雙來源模式才有這兩個鍵；單來源回傳的形狀不變
            result["capture_session"] = capture_report
            result["degraded"] = bool(capture_report.get("degraded"))
        return result

    def _check_capture(self):
        """錄音存檔的完整性。ffmpeg 丟音訊時結束代碼仍是 0，所以得自己量。"""
        if not self.recording_path:
            return None
        report = probe_capture(self.recording_path)
        if not report:
            return None
        if report["lost_ratio"] >= CAPTURE_LOSS_ERROR:
            spacing = report["mean_gap_spacing_seconds"]
            regular = (f"，平均每 {spacing:.2f} 秒一次（規律間隔代表系統性問題，"
                       f"不是偶發負載）" if spacing else "")
            msg = (f"錄音存檔遺失 {100 * report['lost_ratio']:.1f}% 的音訊"
                   f"（錄到 {report['audio_seconds'] / 60:.1f} 分，"
                   f"時間跨距 {report['span_seconds'] / 60:.1f} 分；"
                   f"{report['gap_count']} 個空隙{regular}）")
            log(f"✖ {msg}")
            log("  存檔是會後處理唯一的來源，這份錄音不適合用來做發言者辨識。")
            if self.status:
                self.status.error(msg)
        elif report["lost_ratio"] >= CAPTURE_LOSS_WARN:
            log(f"⚠ 錄音存檔有 {report['gap_count']} 個空隙，"
                f"共 {report['lost_seconds']:.1f} 秒（{100 * report['lost_ratio']:.2f}%）")
        if self.status:
            self.status.event("capture_integrity", **report)
        return report
