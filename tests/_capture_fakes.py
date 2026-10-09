"""雙來源擷取測試用的假 ffmpeg 擷取行程：以即時速度把合成的 PCM 寫進管線。

只假造「擷取」那一端（會真的跑 ffmpeg 的編碼端照常執行），所以產出的 ogg 是真的。
"""
import array
import itertools
import math
import os
import signal
import subprocess
import threading
import time

SR = 16000
_pids = itertools.count(900001)


def sine(freq, start, n, amp=8000):
    return array.array("h", (int(amp * math.sin(2 * math.pi * freq * (start + i) / SR))
                             for i in range(n))).tobytes()


# Windows 沒有 signal.SIGKILL；測試只需要「有被強制結束」這個記號，不需要真的訊號編號。
_KILL = getattr(signal, "SIGKILL", 9)


class FakeCapture:
    """假的擷取行程。

    freq        正弦波頻率；silent=True 時送數位靜音（0）
    start_delay 行程啟動後多久才開始送資料（模擬兩個 ffmpeg 起跑時間不同）
    die_after   送出這麼多秒的音訊後結束（exit_code）
    stall_after 送出這麼多秒後「活著但不再送資料」
    loss        [(開始秒, 長度秒)]：裝置時間照走但樣本沒送出（真的掉音）
    rate        裝置實際取樣率相對標稱的倍數（漂移）
    """

    def __init__(self, freq=440, silent=False, start_delay=0.0, die_after=None, exit_code=1,
                 stall_after=None, loss=(), rate=1.0, stderr_text="", never_start=False):
        self.pid = next(_pids)
        self.freq, self.silent, self.start_delay = freq, silent, start_delay
        self.die_after, self.exit_code, self.stall_after = die_after, exit_code, stall_after
        self.loss, self.rate, self.never_start = list(loss), rate, never_start
        self.returncode = None
        self.signals = []
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "rb")
        self._w = w
        er, ew = os.pipe()
        self.stderr = os.fdopen(er, "rb")
        self._ew = ew
        self._stderr_text = stderr_text
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._feed, daemon=True)
        self._thread.start()

    def _finish(self, code):
        if self.returncode is None:
            self.returncode = code
        for fd in (self._w, self._ew):
            try:
                os.close(fd)
            except OSError:
                pass

    def _feed(self):
        if self._stderr_text and self.never_start is False and self.die_after == 0:
            os.write(self._ew, (self._stderr_text + "\n").encode())
        if self.die_after == 0:
            self._finish(self.exit_code)
            return
        if self.never_start:
            self._halt.wait()
            self._finish(-9)
            return
        if self._halt.wait(self.start_delay):
            self._finish(-9)
            return
        t0 = time.monotonic()
        sent = produced = 0
        while not self._halt.is_set():
            elapsed = time.monotonic() - t0
            target = int(elapsed * SR * self.rate)
            if self.die_after is not None and produced / SR >= self.die_after:
                if self._stderr_text:
                    os.write(self._ew, (self._stderr_text + "\n").encode())
                self._finish(self.exit_code)
                return
            stalled = self.stall_after is not None and produced / SR >= self.stall_after
            n = target - produced
            if n >= 320:
                n = min(n, 1600)
                at = produced / SR
                lost = any(s <= at < s + d for s, d in self.loss)
                if not stalled and not lost:
                    data = bytes(n * 2) if self.silent else sine(self.freq, produced, n)
                    try:
                        os.write(self._w, data)
                    except OSError:
                        self._finish(-13)
                        return
                produced += n
            time.sleep(0.01)
        self._finish(255 if any(s == signal.SIGINT for s in self.signals) else -9)

    # subprocess.Popen 介面
    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        while self.returncode is None:
            if end is not None and time.monotonic() > end:
                raise subprocess.TimeoutExpired("fake", timeout)
            time.sleep(0.01)
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self._halt.set()

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        # 先讓假行程收尾，再記錄訊號：_halt 沒被設定的話 _feed 執行緒永遠不會停，
        # 測試會卡到逾時，直譯器結束時 daemon 執行緒還會對已關閉的 stderr 寫入而崩潰。
        try:
            self.signals.append(_KILL)
        finally:
            self._halt.set()


class FakePopen:
    """依裝置名稱回傳預先設定的 FakeCapture；其他指令（編碼）交給真的 subprocess。"""

    def __init__(self, devices):
        self.devices = devices
        self.captures = {}
        self.encoder_cmds = []

    def __call__(self, cmd, **kw):
        if cmd[-1] == "pipe:1" and "-i" in cmd:
            device = cmd[cmd.index("-i") + 1]
            cap = self.devices[device]
            self.captures[device] = cap
            return cap
        self.encoder_cmds.append(cmd)
        return subprocess.Popen(cmd, **kw)
