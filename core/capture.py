"""雙來源同步擷取（會議用）：系統輸出（monitor）＋使用者麥克風，同一個 session。

只用 Python 標準函式庫與 ffmpeg。第一版只支援 Linux 的 PulseAudio 相容介面
（含 PipeWire 的 pipewire-pulse）；macOS／Windows 沒有可以直接錄「輸出裝置」的方式，
也沒有實機驗證，不在這裡假裝支援。

流程（每一路各自獨立，任何一路壞掉都不會拖垮另一路）：

    ffmpeg(來源) → 16 kHz 單聲道 PCM → 讀取執行緒（記下每塊資料的到達時間）
        → 隔離區（等串流綁定驗證）→ TrackAligner（對齊到共同時間軸）
        ├─ ffmpeg 編碼 → tracks/<role>.ogg   （分軌，日後會後轉錄與發言者辨識用）
        └─ 混音 → 即時 VAD／Whisper，另存一份 ogg

時間基準：
  這台機器的單調時鐘（time.monotonic）。兩路各自的第一個樣本相對時間軸原點 T0 的偏移
  （start_offset）、掉音（gap）與時脈漂移（slip）都記在 capture.json。**同時啟動不代表
  同步**：兩個 ffmpeg 行程的起跑時間差、各裝置的緩衝延遲都不一樣，實測起跑差約 40 ms。
  對齊後每個分軌檔的 t=0 就是 T0，兩軌與混音共用同一條時間軸。

  取樣率不同（44.1／48 kHz 的裝置）由 ffmpeg 轉成 16 kHz；裝置時脈與電腦時鐘的差異
  （幾十 ppm，兩小時約 0.4 秒）由 TrackAligner 以丟／補樣本修正。

誠實的限制（也寫在 docs/platform-dual-capture.md）：
  - 沒有回音消除。不戴耳機時麥克風會收到遠端的聲音，混音裡遠端會出現兩次。
  - monitor 收的是「整個輸出裝置」的聲音，通知音、音樂、別的程式都會進來；
    本程式不知道哪一段是會議 app。
  - 本程式拿到的是作業系統的音訊，不是會議 app 的狀態：會議 app 的 mute 與回音消除
    不會套用在這裡。
  - 來源角色（system／mic）不等於人物身分；本機麥克風可能收到多人。
  - 單路掉音超過 gap_min 才會被認定為缺口；更短的缺口被當成時脈漂移修正掉。
"""
import array
import hashlib
import json
import math
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import platform as P
from .util import atomic_write, hms, log

SR = 16000
SAMPLE_BYTES = 2
READ_BYTES = 3200                 # 100 ms；read1() 有多少給多少，不等滿
META_SCHEMA = 1
# 角色是「來源是什麼」，不是「誰在講話」
ROLE_SYSTEM, ROLE_MIC = "system", "mic"
ROLES = (ROLE_SYSTEM, ROLE_MIC)

START_WINDOW = 2.0                # 開錄時先收這麼多秒，用來估起點與對齊
START_TIMEOUT = 15.0              # 兩路都要在這個時間內開始送資料，否則視為開啟失敗
STALL_TIMEOUT = 5.0               # 活著的行程這麼久沒有資料 = 擷取故障（不是靜音）
VERIFY_INTERVAL = 1.0             # 查詢串流綁定的間隔；同時也是隔離區的最大長度
VERIFY_FAIL_AFTER = 3             # 連續幾次找不到串流就視為故障
GAP_MIN = 0.15                    # 秒；超過這個、且持續下去的落差才算「掉音」
GAP_CONFIRM = 0.6                 # 秒；落差要持續這麼久才確認（排除管線暫時卡住）
DRIFT_WINDOW = 30.0               # 秒；估計基準線的視窗
SETTLE_SECONDS = 60               # 前幾秒的修正多半是起點估計誤差，不算漂移
MIN_DRIFT_SECONDS = 120           # 起點收斂後至少要有這麼長的資料才報時脈誤差
DEADBAND = 8                      # 樣本；差距小於這個不修正（0.5 ms）
MAX_SLIP = 4                      # 每 100 ms 最多丟／補幾個樣本（約 2500 ppm）
DRIFT_WARN_PPM = 500              # 超過這個漂移量要警告（一般晶振只有數十 ppm）
CHECKPOINT_EVERY = 30.0           # 秒；capture.json 的中途存檔間隔
DEFAULT_WATCH_EVERY = 5.0
FINALIZE_TIMEOUT = 20.0


# ---------------------------------------------------------------- 資料與錯誤
@dataclass(frozen=True)
class SourceSpec:
    """一路要擷取的來源。device 必須是已解析的實際裝置 id（不接受 "default"）。"""
    role: str                     # "system"（輸出裝置的 monitor）或 "mic"（麥克風）
    device: str                   # pulse 來源名稱，例如 "xxx.monitor"
    label: str = ""               # 給人看的名稱
    kind: str = ""                # "monitor" 或 "input"；資訊用

    def as_dict(self):
        return {"role": self.role, "device": self.device, "label": self.label,
                "kind": self.kind}


class CaptureStartError(Exception):
    """開始時沒有成功開啟所有來源。已啟動的行程都已收掉，沒有留下任何輸出檔。

    code 是穩定的分類碼，呼叫端不要 parse message：
      unsupported_platform / invalid_sources / ffmpeg_missing / start_failed /
      start_timeout / wrong_source / output_exists / cancelled
    """

    def __init__(self, code, message, role=None, device=None):
        super().__init__(message)
        self.code = code
        self.role = role
        self.device = device

    def as_dict(self):
        return {"code": self.code, "message": str(self), "role": self.role,
                "device": self.device}


def _dbfs(peak):
    return None if peak <= 0 else round(20 * math.log10(peak / 32768), 1)


# ---------------------------------------------------------------- 混音（純函式）
def _samples(data):
    arr = array.array("h")
    arr.frombytes(data)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr


def _bytes(arr):
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def mix_pcm(*tracks):
    """把等長（較短者補零）的 s16le PCM 相加，超出範圍就截頂（不縮放，單路時音量不變）。"""
    tracks = [t for t in tracks if t]
    if not tracks:
        return b""
    n = max(len(t) for t in tracks) // SAMPLE_BYTES
    arrs = [_samples(t[:len(t) // SAMPLE_BYTES * SAMPLE_BYTES]) for t in tracks]
    if len(arrs) == 1:
        return _bytes(arrs[0])
    total = array.array("i", bytes(4 * n))
    for a in arrs:
        for i, v in enumerate(a):
            total[i] += v
    return _bytes(array.array("h", (32767 if v > 32767 else -32768 if v < -32768 else v
                                    for v in total)))


# ---------------------------------------------------------------- 時間軸對齊（純邏輯）
class TrackAligner:
    """把一路擷取校正到共同時間軸。完全不碰 I/O 與時鐘，所有時間都由呼叫端給。

    輸入：一連串 (a_end, pcm)，a_end 是「這塊資料讀到的時間」（單調時鐘，秒）。
    記 n 為到這一塊為止累計的輸入樣本數，則 phi = a_end - n / SR 是「這一路的起點時間」
    （含固定延遲）。理想狀態下 phi 是常數；實際上：

      - 管線偶爾卡住 → 資料晚到 → phi 暫時變大，之後追回來（暫時的，不是掉音）
      - 資料真的遺失 → phi 變大**而且不再回來**（持續超過 gap_confirm）→ 補零並記為缺口
      - 裝置時脈比電腦時鐘慢／快 → phi 緩慢上升／下降 → 以丟／補樣本修正（漂移）

    基準線 base 取最近 DRIFT_WINDOW 秒內 phi 的最小值（延遲只會讓 phi 變大，所以最小值最
    接近真實起點）。窗口內的趨勢偏差約為 漂移量 × 窗口 = 100 ppm × 30 s = 3 ms，可忽略。

    輸出：從時間軸原點 t0 開始、連續不斷的 PCM（開頭補零對齊起點，缺口補零，漂移丟／補樣本）。
    """

    def __init__(self, t0, base0, a_first, sr=SR, gap_min=GAP_MIN, gap_confirm=GAP_CONFIRM,
                 window=DRIFT_WINDOW, deadband=DEADBAND, max_slip=MAX_SLIP):
        self.sr, self.t0 = sr, t0
        self.gap_min, self.gap_confirm = gap_min, gap_confirm
        self.window, self.deadband, self.max_slip = window, deadband, max_slip
        self.base = base0
        self._hist = deque([(a_first, base0)])
        self.n_in = 0
        self.n_out = 0
        self.lead_samples = max(0, int(round((base0 - t0) * sr)))
        self.applied = self.lead_samples        # 相對「輸入樣本數」多出來（或少掉）的輸出樣本
        self.slip_inserted = 0
        self.slip_dropped = 0
        self._settled = None                     # 起點估計收斂之後的 (輸入樣本數, 淨補樣本數)
        self.gap_samples = 0
        self.gaps = []                           # {"at_seconds", "seconds"}
        self._pending = None                     # 疑似掉音：等確認的資料塊
        self._pending_since = 0.0
        self._pending_min = 0.0
        self._out = bytearray()
        self._emit(b"\0" * (self.lead_samples * SAMPLE_BYTES))

    # -- 輸入
    def feed(self, a_end, pcm):
        """餵一塊資料，回傳目前可以輸出的對齊後 PCM（可能是空的）。"""
        n = len(pcm) // SAMPLE_BYTES
        if n == 0:
            return self._take()
        pcm = pcm[:n * SAMPLE_BYTES]
        self.n_in += n
        chunk = (a_end, a_end - self.n_in / self.sr, pcm)
        excess = chunk[1] - self.base
        if self._pending is None:
            if excess <= self.gap_min:
                self._accept(chunk)
            else:
                self._pending, self._pending_since, self._pending_min = [chunk], a_end, excess
        elif excess <= self.gap_min:
            # 追回來了：先前只是資料晚到，不是掉音
            pending, self._pending = self._pending, None
            for c in pending:
                self._accept(c, record=False)
            self._accept(chunk)
        else:
            self._pending.append(chunk)
            self._pending_min = min(self._pending_min, excess)
            if a_end - self._pending_since >= self.gap_confirm:
                self._confirm_gap()
        return self._take()

    def finish(self):
        """結束輸入。尚未確認的疑似缺口照原樣放行（沒有證據就不補零）。"""
        pending, self._pending = self._pending or [], None
        for c in pending:
            self._accept(c, record=False)
        return self._take()

    # -- 內部
    def _emit(self, data):
        self._out += data
        self.n_out += len(data) // SAMPLE_BYTES

    def _take(self):
        data, self._out = bytes(self._out), bytearray()
        return data

    def _accept(self, chunk, record=True):
        a_end, phi, pcm = chunk
        if record:
            self._hist.append((a_end, phi))
            while self._hist and self._hist[0][0] < a_end - self.window:
                self._hist.popleft()
            self.base = min(p for _, p in self._hist)
        self._emit(self._slip(pcm))

    def _slip(self, pcm):
        """漂移修正：基準線跟輸出樣本數差太多時，每塊最多丟／補 max_slip 個樣本。"""
        n = len(pcm) // SAMPLE_BYTES
        if self._settled is None and self.n_in >= SETTLE_SECONDS * self.sr:
            self._settled = (self.n_in, self.slip_inserted - self.slip_dropped)
        target = int(round((self.base - self.t0) * self.sr))
        diff = target - self.applied
        if abs(diff) < self.deadband:
            return pcm
        k = min(abs(diff), max(1, int(round(self.max_slip * n / (self.sr // 10)))), max(0, n - 1))
        if k <= 0:
            return pcm
        if diff > 0:                              # 這路樣本比時間少 → 補
            self.applied += k
            self.slip_inserted += k
            return pcm + pcm[-SAMPLE_BYTES:] * k
        self.applied -= k                         # 這路樣本比時間多 → 丟
        self.slip_dropped += k
        return pcm[:-k * SAMPLE_BYTES]

    def _confirm_gap(self):
        g = max(1, int(round(self._pending_min * self.sr)))
        self.gaps.append({"at_seconds": round(self.n_out / self.sr, 3),
                          "seconds": round(g / self.sr, 3)})
        self._emit(b"\0" * (g * SAMPLE_BYTES))
        self.applied += g
        self.gap_samples += g
        pending, self._pending = self._pending, None
        self._hist = deque((a, p) for a, p, _ in pending)      # 新的基準線
        self.base = min(p for _, p in self._hist)
        for c in pending:
            self._accept(c, record=False)

    # -- 報告
    def stats(self):
        # 時脈誤差只用「起點估計收斂之後」的修正量算：開頭那幾毫秒是估計誤差，不是漂移。
        # 樣本比時間少（補樣本）= 裝置時脈比標稱慢 → 負的 ppm。資料不夠長就不報，免得誤導。
        ppm = None
        if self._settled is not None and self.n_in - self._settled[0] >= MIN_DRIFT_SECONDS * self.sr:
            net = self.slip_inserted - self.slip_dropped - self._settled[1]
            ppm = round(-net / (self.n_in - self._settled[0]) * 1e6, 1)
        return {"samples_in": self.n_in, "samples_out": self.n_out,
                "lead_seconds": round(self.lead_samples / self.sr, 4),
                "gap_count": len(self.gaps), "gap_seconds": round(self.gap_samples / self.sr, 3),
                "slip_inserted": self.slip_inserted, "slip_dropped": self.slip_dropped,
                "clock_error_ppm": ppm}


# ---------------------------------------------------------------- 編碼行程
class _Encoder:
    """吃 16 kHz 單聲道 s16le，輸出 ogg/opus（跟 keep_recording 同一組參數）。"""

    def __init__(self, path, popen, name):
        self.path, self.name, self.failed = Path(path), name, None
        self.written = 0
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
               "-c:a", "libopus", "-b:a", "32k", str(self.path)]
        self.proc = popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, **P.spawn_kwargs())

    def write(self, data):
        if self.failed or not data:
            return
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
            self.written += len(data) // SAMPLE_BYTES
        except (OSError, ValueError) as e:
            self.failed = f"{self.name} 的編碼行程中斷：{e}"

    def close(self, timeout=FINALIZE_TIMEOUT):
        """關掉輸入並等它把檔案封裝好。回傳 None 表示正常，否則是原因。"""
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            self.failed = self.failed or f"{self.name} 的編碼行程 {timeout:.0f} 秒內沒有結束"
            return self.failed
        if rc != 0 and not self.failed:
            try:
                tail = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
            except (OSError, ValueError):
                tail = ""
            self.failed = f"{self.name} 的編碼行程結束代碼 {rc}：{tail[-200:]}"
        return self.failed


# ---------------------------------------------------------------- 一路來源的執行期狀態
class _Source:
    def __init__(self, idx, spec):
        self.idx, self.spec = idx, spec
        self.proc = None
        self.stderr = deque(maxlen=20)
        self.first_chunks = []            # 開錄對齊前先存著的 (a_end, pcm)
        self.quarantine = deque()         # 等串流綁定驗證的 (a_end, pcm)
        self.aligner = None
        self.track = None                 # _Encoder
        self.track_path = None
        self.ready = bytearray()          # 已對齊、尚未混音的 PCM
        self.eof = False                  # 讀取執行緒已結束
        self.ended = False                # 不會再有新資料（正常結束或故障）
        self.fault = None
        self.first_a = None
        self.last_a = None                # 最後一次收到資料的時間
        self.verified_until = -math.inf   # 這個時間以前到達的資料已確認來自正確的裝置
        self.verify_misses = 0
        self.raw_samples = 0
        self.peak = 0
        self.recent_peak = 0
        self.silent_samples = 0
        self.longest_silent = 0
        self.silent_run = 0
        self.valid_until = None           # 最後一個有效樣本在時間軸上的位置（秒）
        self.exit_code = None
        self.err_thread = None
        self.track_reported = False


# ---------------------------------------------------------------- 多來源擷取
class MultiCapture:
    """同一個 session 的多路擷取。start() → read()… → （request_stop）→ report()。

    start() 會等所有來源都送出資料、串流綁定驗證通過才回傳；任何一路失敗就丟
    CaptureStartError，並且收掉已啟動的所有行程、不留下輸出檔。
    開始之後某一路故障：事件 capture_source_failed、另一路繼續，report 標 degraded。
    """

    def __init__(self, specs, session_dir, mix_path=None, tracks_dir="tracks",
                 meta_name="capture.json", backend=None, popen=subprocess.Popen,
                 input_args=None, binding="auto", default_output=None, on_event=None,
                 clock=time.monotonic, wall=time.time, say=log,
                 start_window=START_WINDOW, start_timeout=START_TIMEOUT,
                 stall_timeout=STALL_TIMEOUT, verify_interval=VERIFY_INTERVAL,
                 aligner_opts=None):
        self.specs = list(specs)
        self.dir = Path(session_dir)
        self.mix_path = Path(mix_path) if mix_path else None
        self.tracks_dir = self.dir / tracks_dir
        self.meta_path = self.dir / meta_name
        self.backend = P.audio_backend(backend)
        self._popen = popen
        self._input_args = input_args
        self._binding = P.pulse_record_bindings if binding == "auto" and self.backend == "pulse" \
            else (None if binding == "auto" else binding)
        self._default_output = default_output
        self._on_event = on_event
        self._clock, self._wall, self._say = clock, wall, say
        self.start_window, self.start_timeout = start_window, start_timeout
        self.stall_timeout, self.verify_interval = stall_timeout, verify_interval
        self._aligner_opts = dict(aligner_opts or {})

        self._src = [_Source(i, s) for i, s in enumerate(self.specs)]
        self._inbox = queue.Queue()
        self._out = queue.Queue()
        self._mix = None
        self._thread = None
        self._created = []                # 這次自己建立的輸出檔；啟動失敗時只清這些
        self._stop_requested = False      # 只用單純的屬性：request_stop 會在 signal handler 裡被呼叫
        self._killed = False
        self._started = False
        self._finalized = threading.Event()
        self._t0 = None
        self._wall0 = None
        self._mixed_samples = 0
        self._status = "recording"
        self._error = None
        self._mix_reported = False
        self._faults = []
        self._warnings = []
        self._default_seen = None
        self.report_data = None

    # ------------------------------------------------------------ 事件
    def _event(self, kind, **kv):
        if self._on_event:
            try:
                self._on_event(kind, **kv)
            except Exception as e:                     # 回報管道壞掉不能拖垮錄音
                self._say(f"⚠ 事件回報失敗（{kind}）：{e}")

    def _warn(self, code, detail, **kv):
        rec = {"code": code, "detail": detail, **kv}
        self._warnings.append({**rec, "at_seconds": self._now_rel()})
        self._say(f"⚠ {detail}")
        self._event("capture_warning", **rec)

    def _now_rel(self):
        return None if self._t0 is None else round(self._clock() - self._t0, 3)

    # ------------------------------------------------------------ 啟動
    def _validate(self):
        roles = [s.role for s in self.specs]
        if len(self.specs) != 2 or len(set(roles)) != 2 or any(r not in ROLES for r in roles):
            raise CaptureStartError("invalid_sources",
                                    f"需要 system 與 mic 各一路，收到 {roles}")
        if len({s.device for s in self.specs}) != 2:
            raise CaptureStartError("invalid_sources", "兩路不能是同一個裝置")
        for s in self.specs:
            if s.device in ("", "default", None):
                raise CaptureStartError("invalid_sources",
                                        f"{s.role} 必須指定實際裝置（不接受 default；"
                                        f"預設裝置會在錄音中途改變）", s.role, s.device)
        if self._input_args is None and self.backend != "pulse":
            raise CaptureStartError(
                "unsupported_platform",
                f"雙來源錄音目前只支援 Linux（pulse 後端），這台是 {P.NAME}／{self.backend}。"
                f"macOS 與 Windows 錄不到輸出裝置，也沒有實機驗證")
        if self._popen is subprocess.Popen and not self._has("ffmpeg"):
            raise CaptureStartError("ffmpeg_missing", "找不到 ffmpeg")
        for s in self.specs:
            path = self.tracks_dir / f"{s.role}.ogg"
            if path.exists():
                raise CaptureStartError("output_exists",
                                        f"{path} 已經存在，不覆蓋既有錄音", s.role, s.device)
        if self.mix_path and self.mix_path.exists():
            raise CaptureStartError("output_exists", f"{self.mix_path} 已經存在，不覆蓋既有錄音")

    @staticmethod
    def _has(cmd):
        import shutil
        return shutil.which(cmd) is not None

    def _capture_cmd(self, spec):
        args = self._input_args(spec) if self._input_args else P.ffmpeg_input(spec.device,
                                                                           self.backend)
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *args,
                "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]

    def start(self):
        if self._started:
            raise RuntimeError("start() 只能呼叫一次")
        self._started = True
        try:
            self._start()
        except BaseException:
            self._abort_start()
            self._status = "failed"
            raise

    def _start(self):
        self._validate()
        self.tracks_dir.mkdir(parents=True, exist_ok=True)
        launched = self._clock()
        for s in self._src:
            try:
                s.proc = self._popen(self._capture_cmd(s.spec), stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, **P.spawn_kwargs())
            except OSError as e:
                raise CaptureStartError("ffmpeg_missing", f"無法啟動 ffmpeg：{e}",
                                        s.spec.role, s.spec.device)
            threading.Thread(target=self._read_stdout, args=(s,), daemon=True).start()
            s.err_thread = threading.Thread(target=self._read_stderr, args=(s,), daemon=True)
            s.err_thread.start()

        # 等每一路都送出足夠的資料，並且逐路檢查有沒有提早死掉
        need = int(self.start_window * SR)
        deadline = launched + self.start_timeout

        def have(s):
            return sum(len(p) for _, p in s.first_chunks) // SAMPLE_BYTES

        while True:
            if self._stop_requested:
                raise CaptureStartError("cancelled", "啟動中收到停止要求")
            self._drain_inbox_for_start()
            for s in self._src:
                if s.eof and have(s) < need:
                    raise CaptureStartError(
                        "start_failed",
                        f"{s.spec.role}（{s.spec.device}）"
                        f"{'開啟失敗' if not have(s) else '開錄後立刻中斷'}：{self._stderr_tail(s)}",
                        s.spec.role, s.spec.device)
            if all(have(s) >= need for s in self._src):
                break
            if self._clock() > deadline:
                s = next(s for s in self._src if have(s) < need)
                raise CaptureStartError(
                    "start_timeout",
                    f"{s.spec.role}（{s.spec.device}）{self.start_timeout:.0f} 秒內沒有送出音訊",
                    s.spec.role, s.spec.device)
            time.sleep(0.02)

        self._verify_start_bindings(deadline)

        # 時間軸原點 = 兩路中較早開始的那一路；較晚的那一路開頭補零
        bases = {}
        for s in self._src:
            n, phis = 0, []
            for a, p in s.first_chunks:
                n += len(p) // SAMPLE_BYTES
                phis.append(a - n / SR)
            bases[s.idx] = min(phis)
            s.first_a = s.first_chunks[0][0]
        self._t0 = min(bases.values())
        self._wall0 = self._wall() - (self._clock() - self._t0)
        for s in self._src:
            s.aligner = TrackAligner(self._t0, bases[s.idx], s.first_a, **self._aligner_opts)
            s.track_path = self.tracks_dir / f"{s.spec.role}.ogg"
        try:
            for s in self._src:
                s.track = _Encoder(s.track_path, self._popen, f"{s.spec.role} 分軌")
                self._created.append(s.track_path)
            if self.mix_path:
                self._mix = _Encoder(self.mix_path, self._popen, "混音")
                self._created.append(self.mix_path)
        except OSError as e:
            raise CaptureStartError("ffmpeg_missing", f"無法啟動編碼行程：{e}")

        self._write_meta()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()
        offsets = {s.spec.role: round(s.aligner.lead_samples / SR * 1000, 1) for s in self._src}
        self._say("🎙 雙來源錄音中："
                  + "；".join(f"{s.spec.role}={s.spec.label or s.spec.device}"
                             f"（起點偏移 {offsets[s.spec.role]} ms）" for s in self._src))
        self._event("capture_started",
                    sources=[{**s.spec.as_dict(), "start_offset_ms": offsets[s.spec.role]}
                             for s in self._src],
                    origin_wall=self._iso(self._wall0))

    def _drain_inbox_for_start(self):
        try:
            while True:
                idx, a, data = self._inbox.get_nowait()
                s = self._src[idx]
                if data is None:
                    s.eof = True
                    continue
                s.first_chunks.append((a, data))
        except queue.Empty:
            pass

    def _verify_start_bindings(self, deadline):
        """串流真的綁在我們指定的裝置上嗎？（來源消失時系統可能偷偷把串流轉接到別的來源）"""
        if not self._binding:
            for s in self._src:
                s.verified_until = math.inf
            return
        pending = set(range(len(self._src)))
        while pending:
            bound = self._binding()
            if bound is None:
                self._warn("binding_unverifiable",
                           "無法查詢錄音串流綁定的裝置（pactl 不可用），"
                           "來源中途被系統轉接到別的裝置時不會被發現")
                for s in self._src:
                    s.verified_until = math.inf
                return
            for i in list(pending):
                s = self._src[i]
                name = bound.get(s.proc.pid)
                if name is None:
                    continue
                if name != s.spec.device:
                    raise CaptureStartError(
                        "wrong_source",
                        f"{s.spec.role}：要求 {s.spec.device}，但串流被綁到 {name}",
                        s.spec.role, s.spec.device)
                pending.discard(i)
            if not pending:
                break
            if self._clock() > deadline:
                s = self._src[min(pending)]
                raise CaptureStartError(
                    "start_timeout",
                    f"{s.spec.role}：找不到錄音串流，無法確認它綁在 {s.spec.device}",
                    s.spec.role, s.spec.device)
            time.sleep(0.2)
        now = self._clock()
        for s in self._src:
            s.verified_until = now

    def _abort_start(self):
        for s in self._src:
            if s.proc is not None and s.proc.poll() is None:
                try:
                    s.proc.kill()
                except OSError:
                    pass
        for s in self._src:
            if s.proc is not None:
                try:
                    s.proc.wait(5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            for enc in (s.track,):
                if enc is not None:
                    enc.close(5)
        if self._mix is not None:
            self._mix.close(5)
        # 啟動失敗不留任何輸出檔；但只刪這次自己建立的，既有的錄音（例如因為
        # output_exists 而拒絕開始的那個）絕不能動
        for p in self._created:
            Path(p).unlink(missing_ok=True)
        try:
            self.tracks_dir.rmdir()
        except OSError:
            pass
        self._out.put(None)
        self._finalized.set()

    # ------------------------------------------------------------ 讀取執行緒
    def _read_stdout(self, s):
        carry = b""
        try:
            while True:
                data = s.proc.stdout.read1(READ_BYTES)
                if not data:
                    break
                a = self._clock()
                data = carry + data
                carry = data[-1:] if len(data) % SAMPLE_BYTES else b""
                if carry:
                    data = data[:-1]
                if data:
                    self._inbox.put((s.idx, a, data))
        except (OSError, ValueError):
            pass
        finally:
            self._inbox.put((s.idx, self._clock(), None))

    def _read_stderr(self, s):
        try:
            for line in s.proc.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    s.stderr.append(text)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _stderr_tail(s):
        if s.err_thread is not None:            # ffmpeg 剛死時 stderr 可能還沒讀完
            s.err_thread.join(0.5)
        return " / ".join(list(s.stderr)[-3:])[-300:] or "（ffmpeg 沒有輸出訊息）"

    # ------------------------------------------------------------ 主迴圈
    def _run(self):
        try:
            self._loop()
        except Exception as e:                          # 協調器本身壞掉：收乾淨、讓 read() 結束
            self._error = f"{e.__class__.__name__}: {e}"
            self._status = "failed"
            self._say(f"✖ 擷取協調器錯誤，錄音結束：{self._error}")
            self._event("capture_error", code="internal_error", detail=self._error)
            self._kill_sources()
        finally:
            self._finalize()

    def _loop(self):
        for s in self._src:                             # 開錄前先存著的資料，走同一條處理路徑
            for a, data in s.first_chunks:
                self._on_chunk(s, a, data)
            s.first_chunks = []
        last_verify = last_checkpoint = last_watch = self._clock()
        stop_sent = None
        while not self._all_done():
            try:
                idx, a, data = self._inbox.get(timeout=0.1)
                s = self._src[idx]
                if data is None:
                    self._on_eof(s, a)
                else:
                    self._on_chunk(s, a, data)
                while True:                              # 一次處理完手邊所有資料
                    idx, a, data = self._inbox.get_nowait()
                    s = self._src[idx]
                    self._on_eof(s, a) if data is None else self._on_chunk(s, a, data)
            except queue.Empty:
                pass
            now = self._clock()
            if self._killed:
                break
            if self._stop_requested:
                if stop_sent is None:
                    stop_sent = now
                elif now - stop_sent > 5:                # 請它停它不停
                    for s in self._src:
                        if not s.eof and s.proc.poll() is None:
                            s.proc.kill()
            else:
                self._check_stalls(now)
            if self._binding and now - last_verify >= self.verify_interval and not self._stop_requested:
                last_verify = now
                self._verify(now)
            self._release_quarantine()
            self._mix_ready()
            if self._default_output and now - last_watch >= DEFAULT_WATCH_EVERY:
                last_watch = now
                self._watch_default_output()
            if now - last_checkpoint >= CHECKPOINT_EVERY:
                last_checkpoint = now
                self._write_meta()
        for s in self._src:
            self._release_quarantine(only=s, force=True)
            self._end_source(s)
        self._mix_ready(final=True)

    def _all_done(self):
        return all(s.ended for s in self._src) and not any(s.ready for s in self._src)

    def _on_chunk(self, s, a, data):
        if s.ended:
            return
        s.last_a = a
        s.raw_samples += len(data) // SAMPLE_BYTES
        arr = _samples(data)
        peak = max(max(arr), -min(arr))
        s.peak = max(s.peak, peak)
        s.recent_peak = peak
        if peak == 0:                                    # 數位靜音：正常靜音，不是故障
            s.silent_run += len(arr)
            s.silent_samples += len(arr)
            s.longest_silent = max(s.longest_silent, s.silent_run)
        else:
            s.silent_run = 0
        s.quarantine.append((a, data))

    def _on_eof(self, s, a):
        s.eof = True
        try:
            s.exit_code = s.proc.wait(2)
        except (subprocess.TimeoutExpired, OSError):
            s.exit_code = s.proc.poll()
        if s.ended or s.fault:
            return
        if self._stop_requested or self._killed:             # 我們要它停的：正常收尾
            self._release_quarantine(only=s, force=True)
            self._end_source(s)
            return
        self._fail(s, "stream_ended",
                   f"{s.spec.role}（{s.spec.device}）的擷取行程意外結束"
                   f"（結束代碼 {s.exit_code}）：{self._stderr_tail(s)}", s.last_a)

    def _check_stalls(self, now):
        for s in self._src:
            if s.ended or s.eof or s.fault:
                continue
            last = s.last_a if s.last_a is not None else self._t0
            if now - last > self.stall_timeout:
                self._fail(s, "stalled",
                           f"{s.spec.role}（{s.spec.device}）已 {now - last:.1f} 秒沒有送出任何音訊"
                           f"（這是擷取故障，不是靜音：靜音時資料仍會持續送來）", last)

    def _verify(self, now):
        bound = self._binding()
        for s in self._src:
            if s.ended or s.eof:
                continue
            if bound is None:
                if s.verified_until != math.inf:
                    self._warn("binding_unverifiable", "無法查詢錄音串流綁定的裝置（pactl 不可用）",
                               role=s.spec.role)
                    s.verified_until = math.inf
                continue
            name = bound.get(s.proc.pid)
            if name == s.spec.device:
                s.verified_until, s.verify_misses = now, 0
            elif name is None:
                s.verify_misses += 1
                if s.verify_misses >= VERIFY_FAIL_AFTER:
                    self._fail(s, "binding_lost",
                               f"{s.spec.role}：找不到錄音串流，無法確認它還綁在 {s.spec.device}",
                               s.verified_until)
            else:
                self._fail(s, "source_moved",
                           f"{s.spec.role}：錄音串流被系統從 {s.spec.device} 轉接到 {name}"
                           f"（來源消失時系統可能自動換到別的來源；本程式不會跟著換，"
                           f"轉接之後的音訊已丟棄）",
                           s.verified_until)

    def _release_quarantine(self, only=None, force=False):
        for s in ([only] if only else self._src):
            while not s.ended and s.quarantine and (force or s.quarantine[0][0] <= s.verified_until):
                a, data = s.quarantine.popleft()
                self._align(s, a, data)

    def _align(self, s, a, data):
        before = len(s.aligner.gaps)
        out = s.aligner.feed(a, data)
        s.valid_until = round(s.aligner.n_out / SR, 3)
        if len(s.aligner.gaps) > before:
            for gap in s.aligner.gaps[before:]:
                self._say(f"⚠ {s.spec.role} 掉音 {gap['seconds']:.2f} 秒（時間軸 {hms(gap['at_seconds'])}），"
                          f"已補靜音並記錄")
                self._event("capture_gap", role=s.spec.role, device=s.spec.device, **gap)
        self._push(s, out)

    def _push(self, s, pcm):
        if not pcm:
            return
        s.track.write(pcm)
        if s.track.failed and not s.track_reported:
            s.track_reported = True
            self._say(f"✖ {s.track.failed}")
            self._event("capture_error", code="track_write_failed", role=s.spec.role,
                        detail=s.track.failed)
        s.ready += pcm

    def _end_source(self, s):
        """這一路不會再有資料：把對齊器裡剩下的放出來，關掉它的分軌編碼。"""
        if s.ended:
            return
        s.ended = True
        if s.aligner is not None:
            out = s.aligner.finish()
            s.valid_until = round(s.aligner.n_out / SR, 3)
            self._push(s, out)
        if s.track is not None:
            s.track.close()

    def _fail(self, s, code, detail, last_good):
        """某一路故障：記錄、停掉它、另一路繼續。"""
        if s.fault or s.ended:
            return
        at = None if last_good is None else round(max(0.0, last_good - self._t0), 3)
        s.fault = {"role": s.spec.role, "device": s.spec.device, "code": code, "detail": detail,
                   "at_seconds": at, "detected_at_seconds": self._now_rel(),
                   "detected_wall": self._iso(self._wall())}
        self._faults.append(s.fault)
        # 驗證不過的資料不能進分軌：隔離區裡晚於最後確認時間的資料全部丟掉
        if code in ("source_moved", "binding_lost"):
            while s.quarantine and s.quarantine[-1][0] > s.verified_until:
                s.quarantine.pop()
        if s.proc.poll() is None:
            try:
                s.proc.kill()
            except OSError:
                pass
        remaining = [o.spec.role for o in self._src if o is not s and not o.ended and not o.fault]
        self._say(f"✖ 來源故障：{detail}（時間軸 {hms(at or 0)}）"
                  f"{'；另一路繼續錄音：' + '、'.join(remaining) if remaining else '；已沒有可用來源'}")
        self._event("capture_source_failed", **s.fault, remaining=remaining)
        # 先把這一路已驗證的資料處理完再結束它（驗證不過的部分上面已經丟掉）
        self._release_quarantine(only=s, force=True)
        self._end_source(s)

    def _mix_ready(self, final=False):
        live = [s for s in self._src if not s.ended]
        if live:
            n = min(len(s.ready) for s in live)
        elif any(s.ready for s in self._src):
            n = max(len(s.ready) for s in self._src)
        else:
            return
        n -= n % SAMPLE_BYTES
        if n <= 0:
            return
        parts = []
        for s in self._src:
            take = bytes(s.ready[:n])
            del s.ready[:len(take)]
            if take:
                parts.append(take if len(take) == n else take + b"\0" * (n - len(take)))
        mixed = mix_pcm(*parts)
        self._mixed_samples += len(mixed) // SAMPLE_BYTES
        if self._mix is not None:
            self._mix.write(mixed)
            if self._mix.failed and not self._mix_reported:
                self._mix_reported = True
                self._say(f"✖ {self._mix.failed}")
                self._event("capture_error", code="mix_write_failed", detail=self._mix.failed)
        self._out.put(mixed)

    def _watch_default_output(self):
        try:
            now = self._default_output()
        except Exception:
            return
        if now is None:
            return
        if self._default_seen is None:
            self._default_seen = now
        elif now != self._default_seen:
            old, self._default_seen = self._default_seen, now
            self._warn("default_output_changed",
                       f"系統預設輸出從 {old} 改成 {now}。本程式仍收錄原本選定的輸出裝置，"
                       f"若會議聲音已經跟著換到新裝置，system 軌會是靜音",
                       previous=old, current=now)

    # ------------------------------------------------------------ 收尾
    def _finalize(self):
        try:
            if self._status == "recording":
                if self._killed:
                    self._status = "aborted"
                elif all(s.fault for s in self._src):
                    self._status = "failed"           # 沒有任何一路撐到最後
                else:
                    self._status = "complete"
            for s in self._src:
                self._end_source(s)
            if self._mix is not None:
                self._mix.close()
            for s in self._src:
                if s.proc is not None and s.proc.poll() is None:
                    s.proc.kill()
            self._write_meta(final=True)
            if self.report_data and self.report_data.get("degraded"):
                self._say("⚠ 錄音結束，但有來源中途故障：" + "；".join(
                    f"{f['role']}（{f['code']}，時間軸 {hms(f['at_seconds'] or 0)}）"
                    for f in self._faults))
        except Exception as e:
            self._say(f"✖ 擷取收尾失敗：{e}")
        finally:
            self._out.put(None)
            self._finalized.set()

    def _kill_sources(self):
        for s in self._src:
            if s.proc is not None and s.proc.poll() is None:
                try:
                    s.proc.kill()
                except OSError:
                    pass

    # ------------------------------------------------------------ 對外介面
    def read(self, timeout=None):
        """取下一塊混音（16 kHz 單聲道 s16le）；結束時回傳 b""。timeout 到了回傳 None。"""
        try:
            data = self._out.get(timeout=timeout)
        except queue.Empty:
            return None
        if data is None:
            self._out.put(None)                          # 讓重複呼叫也能看到結束
            return b""
        return data

    def request_stop(self):
        """正常停止：請每一路擷取行程結束，已收到的資料會處理完、分軌封裝完成。
        可以在 signal handler 裡呼叫（不取鎖、不等待）。"""
        self._stop_requested = True
        for s in self._src:
            proc = s.proc
            if proc is not None and proc.poll() is None:
                try:
                    if P.IS_WINDOWS:
                        proc.terminate()
                    else:
                        proc.send_signal(signal.SIGINT)
                except OSError:
                    pass

    def kill(self):
        """強制停止：立刻終止所有行程。分軌檔不保證完整，capture.json 標記 aborted。"""
        self._killed = True
        self._status = "aborted"
        self._kill_sources()

    def wait(self, timeout=None):
        return self._finalized.wait(timeout)

    def report(self):
        self.wait()
        return self.report_data

    def snapshot(self):
        """即時狀態（給 status 用）：每一路是否還活著、多久沒資料、近期音量。"""
        now = self._clock()
        rows = []
        for s in self._src:
            rows.append({"role": s.spec.role, "device": s.spec.device,
                         "state": "failed" if s.fault else "ended" if s.ended else "recording",
                         "seconds_since_data": None if s.last_a is None else round(now - s.last_a, 1),
                         "recent_peak_dbfs": _dbfs(s.recent_peak),
                         "digital_silence_now": s.silent_run > SR * 0.5,
                         "fault": s.fault})
        return {"degraded": bool(self._faults), "sources": rows}

    # ------------------------------------------------------------ capture.json
    @staticmethod
    def _iso(epoch):
        return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")

    def _source_meta(self, s, final):
        al = s.aligner.stats() if s.aligner else {}
        meta = {**s.spec.as_dict(),
                "track": None if s.track_path is None else _rel(s.track_path, self.dir),
                "start_offset_ms": round(al.get("lead_seconds", 0) * 1000, 1),
                "samples_in": al.get("samples_in", 0), "samples_out": al.get("samples_out", 0),
                "duration_seconds": round(al.get("samples_out", 0) / SR, 3),
                "valid_until_seconds": s.valid_until,
                "gaps": list(s.aligner.gaps) if s.aligner else [],
                "gap_seconds": al.get("gap_seconds", 0.0),
                "drift": {"clock_error_ppm": al.get("clock_error_ppm"),
                          "slip_inserted": al.get("slip_inserted", 0),
                          "slip_dropped": al.get("slip_dropped", 0)},
                "levels": {"peak_dbfs": _dbfs(s.peak),
                           "digital_silence_seconds": round(s.silent_samples / SR, 1),
                           "longest_digital_silence_seconds": round(s.longest_silent / SR, 1)},
                "fault": s.fault, "exit_code": s.exit_code}
        ppm = meta["drift"]["clock_error_ppm"]
        meta["drift"]["excessive"] = bool(ppm is not None and abs(ppm) > DRIFT_WARN_PPM)
        if final and s.track_path is not None:
            meta.update(_artifact(s.track_path, s.track.failed if s.track else None,
                                  meta["duration_seconds"]))
        return meta

    def _write_meta(self, final=False):
        sources = [self._source_meta(s, final) for s in self._src]
        mix = None
        if self.mix_path:
            mix = {"path": (_rel(self.mix_path, self.dir) if self.mix_path.is_relative_to(self.dir)
                            else self.mix_path.as_posix()),
                   "duration_seconds": round(self._mixed_samples / SR, 3),
                   "method": "saturating_sum"}
            if final:
                mix.update(_artifact(self.mix_path, self._mix.failed if self._mix else None,
                                     mix["duration_seconds"]))
        status = self._status if final else "recording"
        degraded = bool(self._faults) or any(m.get("gap_seconds") for m in sources)
        data = {"schema_version": META_SCHEMA, "kind": "multi_source_capture",
                "status": status, "sample_rate": SR, "channels": 1,
                "timebase": {"clock": "host_monotonic",
                             "origin_wall": self._iso(self._wall0) if self._wall0 else None,
                             "note": "所有分軌與混音的 t=0 都是這個原點；起點偏移已補零對齊"},
                "sources": sources, "mix": mix,
                "degraded": degraded, "faults": list(self._faults),
                "warnings": list(self._warnings), "error": self._error,
                "written_wall": self._iso(self._wall())}
        try:
            atomic_write(self.meta_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        except OSError as e:
            self._say(f"⚠ capture.json 寫入失敗：{e}")
        if final:
            self.report_data = data


# ---------------------------------------------------------------- 產物驗證
def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _probe_duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _rel(path, base):
    """session 內的相對路徑，一律用 `/` 分隔。

    capture.json 是跨平台的紀錄：`str(Path)` 在 Windows 會變成 `tracks\\mic.ogg`，在別的平台
    讀就成了一個叫那個怪名字的檔案，verify_capture 會判成找不到。"""
    return path.relative_to(base).as_posix()


def _portable_rel(rel):
    """讀 capture.json 時把路徑正規化成 `/` 分隔，這樣在 Windows 寫出的 `tracks\\mic.ogg` 也認得。

    角色只有 system／mic、混音檔名固定，檔名不會合法地含反斜線，所以直接轉換是安全的。"""
    return rel.replace("\\", "/") if isinstance(rel, str) else rel


def _artifact(path, failure, expected_seconds):
    """封裝完成後的檔案資訊。complete 要同時滿足：編碼行程沒壞、檔案存在、
    音長跟寫入的樣本數吻合（容許 opus 封裝的取整誤差）。"""
    path = Path(path)
    info = {"complete": False, "bytes": None, "sha256": None, "probed_seconds": None}
    if failure:
        info["problem"] = failure
        return info
    try:
        info["bytes"] = path.stat().st_size
    except OSError:
        info["problem"] = "檔案不存在"
        return info
    info["probed_seconds"] = _probe_duration(path)
    if info["probed_seconds"] is None:
        info["problem"] = "ffprobe 讀不出音長"
    elif abs(info["probed_seconds"] - expected_seconds) > max(1.0, expected_seconds * 0.005):
        info["problem"] = (f"檔案音長 {info['probed_seconds']:.1f} 秒與寫入的 "
                           f"{expected_seconds:.1f} 秒不符")
    else:
        info["complete"] = True
        info["sha256"] = _sha256(path)
    return info


def verify_capture(session_dir, meta_name="capture.json", deep=True):
    """事後判斷一個 session 的擷取產物哪些完整、哪些不能拿來重跑。

    回傳 {"state", "usable": {檔案: bool}, "playable": {...}, "problems": [...], ...}：
    usable = 核對通過、可以當重跑來源；playable 只在 incomplete 時有值，表示檔案雖然沒有
    封裝紀錄，但 ffprobe 還讀得出來（可人工搶救，不保證完整）。
      state = complete   正常結束，所有檔案核對通過
              incomplete 強制停止或程序崩潰：capture.json 停在 recording／aborted，
                         檔案可能能播放，但沒有封裝紀錄與雜湊，不可當作可重跑的來源
              degraded   完整但有來源中途故障或掉音（仍可使用，缺口已記在 capture.json）
              missing    沒有 capture.json（舊的單來源錄音，或根本沒有）
              invalid    capture.json 讀不懂，或檔案與紀錄不符
    deep=True 會重算 SHA-256（錄音很大時需要幾秒）。
    """
    d = Path(session_dir)
    meta_path = d / meta_name
    if not meta_path.exists():
        return {"state": "missing", "usable": {}, "problems": ["沒有 capture.json"]}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["kind"] == "multi_source_capture"
    except (OSError, ValueError, KeyError, AssertionError) as e:
        return {"state": "invalid", "usable": {}, "problems": [f"capture.json 讀不懂：{e}"]}
    usable, problems = {}, []
    artifacts = [(_portable_rel(s.get("track")), s) for s in meta.get("sources", [])]
    if meta.get("mix"):
        artifacts.append((_portable_rel(meta["mix"].get("path")), meta["mix"]))
    final = meta.get("status") == "complete"
    for rel, rec in artifacts:
        if not rel:
            continue
        ok = False
        p = d / rel
        if not final:
            problems.append(f"{rel}：錄音沒有正常結束（status={meta.get('status')}），沒有封裝紀錄")
        elif not p.is_file():
            problems.append(f"{rel}：檔案不存在")
        elif not rec.get("complete"):
            problems.append(f"{rel}：封裝時就不完整（{rec.get('problem', '原因不明')}）")
        elif p.stat().st_size != rec.get("bytes"):
            problems.append(f"{rel}：大小與紀錄不符")
        elif deep and _sha256(p) != rec.get("sha256"):
            problems.append(f"{rel}：SHA-256 與紀錄不符（檔案被改過或損壞）")
        else:
            ok = True
        usable[rel] = ok
    playable = {}
    if not final:                      # 搶救用：沒有封裝紀錄，但檔案可能還能播（編碼行程會在輸入結束時自行封裝）
        for rel, _ in artifacts:
            if rel and (d / rel).is_file():
                playable[rel] = _probe_duration(d / rel) is not None
    if not final:
        state = "incomplete"
    elif problems:
        state = "invalid"
    elif meta.get("degraded"):
        state = "degraded"
    else:
        state = "complete"
    return {"state": state, "usable": usable, "playable": playable, "problems": problems,
            "faults": meta.get("faults", []), "meta": meta}
