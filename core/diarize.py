"""會後發言者辨識引擎層（對應 docs/meeting-workbench-contract.md）。

對外只有一個入口：

    diarize_session(request, progress=None, cancel=None) -> DiarizationResult

做法是契約選定的「先切段再轉錄」：以 segmentation + 分群得到發言區間，再把每個
區間的音訊單獨送 whisper 重新轉錄。代號由構造保證正確，沒有對齊步驟；時間戳一律
來自音訊區間與 whisper 自己回報的區段時間，**不按字數估算，也不用 OpenCC 之後的
字元索引貼回原稿**（OpenCC 不保長度：`内存与网络` 5 字 → `記憶體與網路` 6 字）。

分群在子行程跑（core/diarize_worker.py），因為 sherpa-onnx 是第三方套件，只能放在
專案 .venv；`lec` 本身仍只用標準函式庫。這個模組不 import config／session／status，
CLI 與 UI 由呼叫端接線。

不依賴 torch、numpy 或任何網路服務；sherpa-onnx 與 whisper-server 都是本機。
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from . import platform as P
from .transcribe import HALLUCINATIONS, _multipart, normalize_punct
from .util import opencc_available, opencc_convert

SR = 16000
SCHEMA_VERSION = 1
ENGINE_NAME = "sherpa-onnx"
SHERPA_ONNX_VERSION = "1.13.8"     # 已實測的版本；升級要重跑 tests/test_diarize_*.py
MAX_SPEAKERS = 30
UNASSIGNED = "S00"

APP_ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).with_name("diarize_worker.py")

# whisper 偶爾把 prompt 當內容吐回來，尤其在極短或近無聲的區段。
# 在一份 87.7 分鐘的真實會議上：648 行裡有 18 行是 prompt 回吐、6 行命中既有幻覺正則。
_PROMPT_ECHO = re.compile(r"(以下是|與會者|中英夾雜|繁體中文的(會議|大學課堂)內容)")


# ---------------------------------------------------------------- 錯誤分類
class DiarizationError(Exception):
    """帶穩定代碼的失敗。呼叫端用 .code 分類，不要去 parse .message。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DiarizationCancelled(Exception):
    """使用者取消。呼叫端據此寫 aborted + stop_reason=user，並保留上一版成功產物。"""


CODES = (
    "input_invalid",         # 參數不合法
    "source_missing",        # 來源音檔不存在或不可讀
    "source_hash_mismatch",  # 來源與 source.json 記錄不符，禁止重跑
    "model_missing",         # 模型檔不存在
    "engine_unavailable",    # 找不到裝了 sherpa-onnx 的 Python，或它載入失敗
    "decode_failed",         # ffmpeg 解碼失敗
    "segmentation_failed",   # segmentation / 分群失敗
    "asr_failed",            # 重新轉錄失敗
    "empty_result",          # 跑完卻沒有任何可用發言，不可當成功
    "write_failed",          # 產物寫入或原子切換失敗
)


# ---------------------------------------------------------------- 輸入輸出
@dataclass(frozen=True)
class DiarizationRequest:
    session_dir: Path          # 會議 session 根目錄
    source_audio: Path         # session 內的來源音檔（絕對路徑）
    source_sha256: str         # source.json 記錄的雜湊；空字串 = 不驗
    meeting_name: str
    requested_speakers: int    # 1..30，0 或未提供由呼叫端擋掉
    segmentation_model: Path
    embedding_model: Path
    cluster_threshold: float = 0.5          # (0,1]
    segmentation_window_shift: float = 0.1  # > 0
    staging_dir: Path | None = None         # 預設 <session>/.diarize-staging-<uuid>
    whisper_server: str = "http://127.0.0.1:8178"
    whisper_prompt: str = ""
    threads: int = 8
    merge_gap: float = 1.0     # 相鄰同代號區間間隔 <= 此值就併成一次轉錄
    pad: float = 0.15          # 切段前後各留一點，避免咬掉字頭字尾
    min_turn_seconds: float = 0.25   # 短於此的區間不送 whisper（送了只會拿到幻覺）
    opencc: bool = True
    python: Path | None = None       # 指定跑分群的直譯器；預設自動尋找
    unassigned_confidence_floor: float | None = None
    # ^ 低於此信賴度的區間標成 S00。預設關閉：目前沒有任何實測資料可以支持
    #   一個門檻值，硬塞一個數字只會製造假的「未指派」。


class DiarizerOutput(NamedTuple):
    turns: list            # [(start_s, end_s, cluster:int, confidence:float)]
    duration: float        # 來源音檔實際長度（秒）
    engine_version: str


@dataclass
class DiarizationResult:
    generation: str
    requested_speakers: int
    actual_speakers: int
    duration_seconds: float
    speakers: list[dict]
    speaker_transcript: str    # session 相對路徑
    speakers_json: str         # session 相對路徑
    manifest: str              # session 相對路徑
    timing: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 進度
class _Progress:
    """把契約要求硬編在這裡，而不是信任各 stage 自己乖乖回報。

    契約：`{stage, processed_seconds, total_seconds, speakers_found}`，
    秒數單調不減且落在 [0, total]；兩個 stage 各自從零重新計
    （所以不要把兩段秒數相加當百分比）。
    """

    def __init__(self, cb):
        self._cb = cb
        self._stage = None
        self._last = 0.0

    def stage(self, name):
        self._stage = name
        self._last = 0.0

    def __call__(self, processed, total, speakers_found):
        if self._cb is None:
            return
        total = max(float(total), 0.0)
        processed = min(max(float(processed), 0.0), total) if total else 0.0
        if processed < self._last:          # 單調不減
            processed = self._last
        self._last = processed
        self._cb({"stage": self._stage,
                  "processed_seconds": round(processed, 3),
                  "total_seconds": round(total, 3),
                  "speakers_found": int(speakers_found)})


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise DiarizationCancelled()


# ---------------------------------------------------------------- 工具
def _sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _ts(seconds):
    """HH:MM:SS.mmm"""
    ms = int(round(max(seconds, 0.0) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _decode_pcm16(path, start, dur):
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
             "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise DiarizationError("decode_failed", f"ffmpeg 無法執行：{e}") from e
    if p.returncode != 0:
        tail = (p.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
        raise DiarizationError(
            "decode_failed", f"ffmpeg 切出 {start:.1f}s+{dur:.1f}s 失敗：{tail[:200]}")
    return p.stdout


# ---------------------------------------------------------------- 分群（子行程）
def _venv_python():
    rel = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return APP_ROOT / ".venv" / rel


def _can_import_sherpa(python):
    try:
        r = subprocess.run([str(python), "-c", "import sherpa_onnx"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def find_python(explicit=None):
    """找一個裝了 sherpa-onnx 的直譯器；找不到回傳 None。

    順序：指定的 → 環境變數 LEC_DIARIZE_PYTHON → 目前這個直譯器 → 專案 .venv。
    專案 .venv 是 repo 放第三方套件的既定位置（UI 的 fastapi 也在那裡）。
    """
    cands = []
    if explicit:
        cands.append(Path(explicit))
    env = os.environ.get("LEC_DIARIZE_PYTHON")
    if env:
        cands.append(Path(env))
    if importlib.util.find_spec("sherpa_onnx") is not None:
        return Path(sys.executable)
    cands.append(_venv_python())
    for c in cands:
        if c.is_file() and _can_import_sherpa(c):
            return c
    return None


def _install_hint():
    return (f"找不到安裝了 sherpa-onnx 的 Python。請安裝：\n"
            f"  {_venv_python()} -m pip install sherpa-onnx=={SHERPA_ONNX_VERSION}\n"
            f"也可以用環境變數 LEC_DIARIZE_PYTHON 指定一個已安裝的直譯器。")


def _tail(path, n=4):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return " | ".join(lines[-n:])[:400]


def _default_diarizer(req, progress, cancel):
    """啟動 diarize_worker.py。取消時直接結束它，所以取消是可靠的、不依賴 sherpa。"""
    python = find_python(req.python)
    if python is None:
        raise DiarizationError("engine_unavailable", _install_hint())
    payload = json.dumps({
        "audio": str(req.source_audio),
        "segmentation_model": str(req.segmentation_model),
        "embedding_model": str(req.embedding_model),
        "num_speakers": req.requested_speakers,
        "threshold": req.cluster_threshold,
        "window_shift": req.segmentation_window_shift,
        "threads": req.threads,
    }).encode("utf-8")

    log_path = Path(req.staging_dir) / "worker.log"
    try:
        with open(log_path, "wb") as errf:
            proc = subprocess.Popen([str(python), str(WORKER)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=errf,
                                    **P.spawn_kwargs())
    except OSError as e:
        raise DiarizationError("engine_unavailable", f"無法啟動分群行程：{e}") from e

    lines = queue.Queue()

    def pump():
        try:
            for raw in proc.stdout:
                lines.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
    except OSError:
        pass                       # worker 提早結束，下面會從結束代碼與 log 判斷

    result = error = None
    try:
        while True:
            try:
                raw = lines.get(timeout=0.25)
            except queue.Empty:
                _cancel_worker_if_asked(proc, cancel)
                continue
            if raw is None:
                break
            _cancel_worker_if_asked(proc, cancel)
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "progress":
                progress(ev.get("processed", 0.0), ev.get("total", 0.0), 0)
            elif kind == "result":
                result = ev
            elif kind == "error":
                error = ev
    finally:
        if proc.poll() is None:
            P.kill_tree(proc.pid, timeout=5)
        proc.wait()
        reader.join(timeout=2)
        try:
            proc.stdout.close()
        except OSError:
            pass

    rc = proc.returncode
    if error is not None:
        code = error.get("code")
        raise DiarizationError(code if code in CODES else "segmentation_failed",
                               str(error.get("message", "分群失敗")))
    if result is None:
        hint = "（可能是記憶體不足被系統結束）" if rc in (-9, 137) else ""
        raise DiarizationError(
            "segmentation_failed",
            f"分群行程非預期結束（代碼 {rc}）{hint}：{_tail(log_path) or '沒有輸出'}")
    return DiarizerOutput(
        turns=[(float(a), float(b), int(c), float(d)) for a, b, c, d in result["turns"]],
        duration=float(result["duration"]),
        engine_version=str(result.get("engine_version", "unknown")))


def _cancel_worker_if_asked(proc, cancel):
    if cancel is not None and cancel():
        P.kill_tree(proc.pid, timeout=5)
        raise DiarizationCancelled()


# ---------------------------------------------------------------- 重新轉錄
def _default_asr(req, pcm, prompt):
    """送 whisper-server /inference 取 verbose_json，回傳它自己的 segment 時間。

    用 verbose_json 而不是 srt：srt 模式下段落級 end 會被釘在 mel window 的 30.00s
    （實測 11/23 個 cue 超出實際 chunk 3.39–7.17 秒）；verbose_json 的 segment 時間
    實測 0 個超界。不需要 -dtw，所以沿用正式流程啟動 whisper-server 的參數即可。
    """
    tmp = Path(req.staging_dir) / f"_asr_{uuid.uuid4().hex}.wav"
    try:
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm)
        body, ctype = _multipart(
            {"response_format": "verbose_json", "temperature": "0.0",
             "no_language_probabilities": "true", "prompt": prompt},
            "file", tmp)
        rq = urllib.request.Request(f"{req.whisper_server}/inference", data=body,
                                    headers={"Content-Type": ctype})
        err = "未知錯誤"
        for _ in range(3):
            try:
                with urllib.request.urlopen(rq, timeout=900) as r:
                    raw = r.read()
                js = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(js, dict) and "segments" in js:
                    return [(float(s.get("start", 0.0)), float(s.get("end", 0.0)),
                             str(s.get("text", ""))) for s in js["segments"]]
                err = raw.decode("utf-8", "replace").strip()[:200]
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
                err = str(e)[:200]
            time.sleep(2)
        # 跟 transcribe_request 同樣的紀律：重試用盡就丟錯，不要回空字串讓
        # 「真的沒人說話」和「轉錄失敗」混在一起。
        raise DiarizationError("asr_failed", f"重新轉錄失敗：{err}")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------- 驗證
def _validate(req):
    if isinstance(req.requested_speakers, bool) or \
            not isinstance(req.requested_speakers, int) or \
            not 1 <= req.requested_speakers <= MAX_SPEAKERS:
        raise DiarizationError(
            "input_invalid",
            f"預計人數必須是 1..{MAX_SPEAKERS} 的整數，收到 {req.requested_speakers!r}")
    if not 0 < req.cluster_threshold <= 1:
        raise DiarizationError(
            "input_invalid", f"cluster_threshold 必須在 (0,1]，收到 {req.cluster_threshold!r}")
    if not req.segmentation_window_shift > 0:
        raise DiarizationError("input_invalid", "segmentation_window_shift 必須 > 0")
    src = Path(req.source_audio)
    if not src.is_file():
        raise DiarizationError("source_missing", f"來源音檔不存在：{src}")
    for p, what in ((req.segmentation_model, "segmentation"),
                    (req.embedding_model, "embedding")):
        if not Path(p).is_file():
            raise DiarizationError("model_missing", f"{what} 模型不存在：{p}")
    actual = _sha256(src)
    if req.source_sha256 and actual != req.source_sha256:
        raise DiarizationError(
            "source_hash_mismatch",
            f"來源雜湊不符，禁止重跑（記錄 {req.source_sha256[:12]}…，實際 {actual[:12]}…）")
    return actual


# ---------------------------------------------------------------- 區間處理
def _relabel(turns, floor):
    """sherpa 的 cluster 編號沒有意義，依「首次出現順序」重新編成 S01、S02…

    契約要求代號依首次出現順序編號，所以這一步不能省。
    """
    order, out = {}, []
    for start, end, cluster, conf in turns:
        if floor is not None and conf < floor:
            out.append((start, end, UNASSIGNED))
            continue
        if cluster not in order:
            order[cluster] = f"S{len(order) + 1:02d}"
        out.append((start, end, order[cluster]))
    return out


def _merge(turns, gap):
    """相鄰且同代號、間隔 <= gap 就合併。

    合併只跨「連續音訊」，所以時間戳仍然是真的。不要把同一個人散在各處的區間
    打包進同一次請求來省時間——那會逼我們事後猜每句話的時間。
    """
    merged = []
    for start, end, spk in sorted(turns, key=lambda t: (t[0], t[1])):
        if merged and merged[-1][2] == spk and start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, spk])
    return merged


def _usable(text):
    t = text.strip()
    return bool(t) and not HALLUCINATIONS.search(t) and not _PROMPT_ECHO.search(t)


# ---------------------------------------------------------------- 主流程
def diarize_session(request: DiarizationRequest, progress=None, cancel=None,
                    diarizer=_default_diarizer, asr=_default_asr) -> DiarizationResult:
    """會後辨識一個 session。同步執行；`cancel` 在每個可中斷區間被輪詢。

    `diarizer(req, progress, cancel) -> DiarizerOutput` 與 `asr(req, pcm, prompt)`
    可注入，讓 error／cancel／retry／原子輸出可以用假引擎測，不必載入模型。
    """
    t_start = time.monotonic()
    session = Path(request.session_dir)
    src = Path(request.source_audio)
    prog = _Progress(progress)

    source_sha = _validate(request)
    _check_cancel(cancel)

    staging = Path(request.staging_dir) if request.staging_dir else \
        session / f".diarize-staging-{uuid.uuid4().hex}"
    req = dataclasses.replace(request, staging_dir=staging)
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise DiarizationError("write_failed", f"無法建立暫存目錄：{e}") from e

    try:
        # ---- stage 1：segmentation + 分群
        prog.stage("segmentation")
        prog(0.0, 0.0, 0)
        out = diarizer(req, prog, cancel)
        _check_cancel(cancel)
        duration = out.duration

        turns = _relabel(out.turns, req.unassigned_confidence_floor)
        merged = _merge(turns, req.merge_gap)
        found = len({s for _, _, s in turns if s != UNASSIGNED})
        prog(duration, duration, found)

        # ---- stage 2：逐區間重新轉錄
        prog.stage("retranscription")
        plan = [m for m in merged if m[1] - m[0] >= req.min_turn_seconds]
        plan_total = sum(b - a for a, b, _ in plan) or 1.0
        prog(0.0, plan_total, found)

        segments, done = [], 0.0
        for start, end, spk in plan:
            _check_cancel(cancel)
            a = max(0.0, start - req.pad)
            b = min(duration, end + req.pad)
            if b - a > 0.05:
                pcm = _decode_pcm16(src, a, b - a)
                if len(pcm) >= SR // 4:
                    for s0, s1, text in asr(req, pcm, req.whisper_prompt):
                        if not _usable(text):
                            continue
                        # whisper 的時間是這一段音訊內的相對時間，加上切點就是真實時間。
                        abs0 = a + max(s0, 0.0)
                        abs1 = min(max(a + max(s1, s0), abs0 + 0.001), b)
                        segments.append({"start_ms": int(round(abs0 * 1000)),
                                         "end_ms": int(round(abs1 * 1000)),
                                         "speaker_id": spk,
                                         "text": normalize_punct(text)})
            done += max(b - a, 0.0)
            prog(done, plan_total, found)

        if req.opencc and opencc_available() and segments:
            # 分群完成之後才做繁簡轉換：OpenCC 不保長度，先轉就會讓任何字元位置
            # 對應錯位。這裡逐段一一對應地轉文字，不涉及位置。
            for seg, text in zip(segments, opencc_convert([s["text"] for s in segments])):
                seg["text"] = text

        segments = [s for s in segments if s["text"].strip()]
        segments.sort(key=lambda s: (s["start_ms"], s["end_ms"]))
        if not segments:
            raise DiarizationError(
                "empty_result",
                "辨識跑完但沒有任何可用發言，不當成功（可能是來源幾乎無聲、"
                "或全部被幻覺過濾掉）")

        # ---- 統計
        per = {}
        for s in segments:
            d = per.setdefault(s["speaker_id"], {"speech_ms": 0, "segment_count": 0})
            d["speech_ms"] += s["end_ms"] - s["start_ms"]
            d["segment_count"] += 1
        speakers = [{"id": k, "speech_seconds": round(v["speech_ms"] / 1000, 3),
                     "segment_count": v["segment_count"]}
                    for k, v in sorted(per.items(), key=lambda kv: -kv[1]["speech_ms"])]
        # actual_speakers：非 S00 且至少有一段非空文字的代號數。不強制等於 requested。
        actual = len([s for s in speakers if s["id"] != UNASSIGNED])

        timing = {"total_seconds": round(time.monotonic() - t_start, 2),
                  "audio_seconds": round(duration, 2)}
        payload = {
            "schema_version": SCHEMA_VERSION,
            "source_sha256": source_sha,
            "requested_speakers": req.requested_speakers,
            "actual_speakers": actual,
            "duration_seconds": round(duration, 3),
            "speakers": speakers,
            "segments": segments,
            "engine": {
                "name": ENGINE_NAME,
                "version": out.engine_version,
                "segmentation_model": Path(req.segmentation_model).name,
                "segmentation_model_sha256": _sha256(req.segmentation_model),
                "embedding_model": Path(req.embedding_model).name,
                "embedding_model_sha256": _sha256(req.embedding_model),
                "params": {
                    "num_speakers": req.requested_speakers,
                    "cluster_threshold": req.cluster_threshold,
                    "segmentation_window_shift": req.segmentation_window_shift,
                    "merge_gap": req.merge_gap,
                    "pad": req.pad,
                    "min_turn_seconds": req.min_turn_seconds,
                },
            },
        }
        _verify(payload)
        return _commit(session, staging, payload, req, timing)
    except (DiarizationError, DiarizationCancelled):
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as e:                       # 不吞例外，但要分類
        shutil.rmtree(staging, ignore_errors=True)
        raise DiarizationError(
            "segmentation_failed", f"未預期的失敗：{type(e).__name__}: {e}") from e


def _verify(payload):
    """落地前自檢。契約的區間不變式在這裡擋，不要等 UI 去發現。"""
    segs = payload["segments"]
    if not segs:
        raise DiarizationError("empty_result", "沒有任何發言區間")
    prev = -1
    for s in segs:
        if not (isinstance(s["start_ms"], int) and isinstance(s["end_ms"], int)):
            raise DiarizationError("write_failed", "區間時間必須是整數毫秒")
        if s["start_ms"] >= s["end_ms"]:
            raise DiarizationError(
                "write_failed", f"區間 start_ms >= end_ms：{s['start_ms']} >= {s['end_ms']}")
        if s["start_ms"] < prev:
            raise DiarizationError("write_failed", "區間未依開始時間遞增")
        prev = s["start_ms"]
        if not re.fullmatch(r"S\d{2,}", s["speaker_id"]):
            raise DiarizationError("write_failed", f"代號格式錯誤：{s['speaker_id']!r}")
        if not s["text"].strip():
            raise DiarizationError("write_failed", "出現空白文字的區間")
    ids = {s["speaker_id"] for s in segs}
    declared = {s["id"] for s in payload["speakers"]}
    if ids != declared:
        raise DiarizationError(
            "write_failed", f"speakers 與 segments 的代號不一致：{declared ^ ids}")
    if payload["actual_speakers"] != len([i for i in ids if i != UNASSIGNED]):
        raise DiarizationError("write_failed", "actual_speakers 與區間不符")


def _render_md(meeting_name, payload):
    lines = [f"# {meeting_name} · 發言者逐字稿", ""]
    for s in payload["segments"]:
        lines.append(f"[{_ts(s['start_ms'] / 1000)}–{_ts(s['end_ms'] / 1000)}] "
                     f"{s['speaker_id']}: {s['text']}")
    return "\n".join(lines) + "\n"


def _next_generation(root):
    root.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in root.iterdir():
        if p.is_dir() and p.name.isdigit():
            n = max(n, int(p.name))
    return f"{n + 1:04d}"


def _commit(session, staging, payload, req, timing):
    """staging 全部寫好並自檢通過，才搬到新的 generation，最後原子換 manifest。

    這樣崩在任何一點都不會出現「兩個最終檔一新一舊」：manifest 沒換，
    讀取端看到的就還是上一版成功結果。
    """
    try:
        (staging / "speakers.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        (staging / "transcript.speakers.md").write_text(
            _render_md(req.meeting_name, payload), encoding="utf-8")
        for name in ("speakers.json", "transcript.speakers.md"):
            if not (staging / name).is_file() or (staging / name).stat().st_size == 0:
                raise DiarizationError("write_failed", f"{name} 寫入後是空的")
        json.loads((staging / "speakers.json").read_text(encoding="utf-8"))
        (staging / "worker.log").unlink(missing_ok=True)

        root = session / "diarization"
        gen = _next_generation(root)
        target = root / gen
        if target.exists():
            raise DiarizationError("write_failed", f"generation 目錄已存在：{target}")
        os.rename(staging, target)          # 同一檔案系統內的目錄 rename 是原子的

        rel_md = f"diarization/{gen}/transcript.speakers.md"
        rel_js = f"diarization/{gen}/speakers.json"
        manifest = {"schema_version": SCHEMA_VERSION, "generation": gen,
                    "speaker_transcript": rel_md, "speakers": rel_js}
        tmp = session / f".diarization.current.json.{uuid.uuid4().hex}"
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, session / "diarization.current.json")
    except DiarizationError:
        raise
    except OSError as e:
        raise DiarizationError("write_failed", f"產物寫入失敗：{e}") from e

    return DiarizationResult(
        generation=gen,
        requested_speakers=payload["requested_speakers"],
        actual_speakers=payload["actual_speakers"],
        duration_seconds=payload["duration_seconds"],
        speakers=payload["speakers"],
        speaker_transcript=rel_md,
        speakers_json=rel_js,
        manifest="diarization.current.json",
        timing=timing)
