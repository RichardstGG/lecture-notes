"""whisper-server / llama-server 的啟動、沿用、關閉。

擁有權紀錄存在 <state_dir>/servers/<name>.json：
- lec 啟動的 server：模型相同就沿用，不同就重啟；本次執行結束時關閉。
- 手動開啟的 server（沒有擁有權紀錄）：模型相同（或無法判斷）就沿用並提醒，不同就停止並報錯，不動它。
"""
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .util import log, pid_alive, read_json, write_json


class ServerError(Exception):
    pass


def http_get(url, timeout=3):
    """回傳 (status, body)；連不上回傳 (None, None)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return None, None


def _same_file(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (TypeError, OSError):
        return False


class ManagedServer:
    name = "server"
    health_path = "/"

    def __init__(self, host, port, model_path, state_dir, startup_timeout=120):
        self.host, self.port = host, int(port)
        self.url = f"http://{host}:{port}"
        self.model_path = str(model_path)
        self.own_file = Path(state_dir) / "servers" / f"{self.name}.json"
        self.startup_timeout = startup_timeout
        self.pid = None
        self.owned = False        # 本次執行結束時是否由我們關閉
        self.log_path = None

    # -- 子類別實作
    def binary(self):
        raise NotImplementedError

    def build_cmd(self):
        raise NotImplementedError

    def running_model(self, own):
        """回傳目前 server 載入的模型路徑；無法判斷回傳 None。"""
        return own.get("model") if own else None

    def is_ready(self):
        st, _ = http_get(self.url + self.health_path)
        return st == 200

    # -- 共用流程
    def responding(self):
        st, _ = http_get(self.url + self.health_path)
        return st is not None

    def ensure(self, log_path):
        self.log_path = Path(log_path)
        own = read_json(self.own_file)
        if own and not (pid_alive(own.get("pid")) and own.get("port") == self.port):
            own = None
            self.own_file.unlink(missing_ok=True)

        if self.responding():
            model = self.running_model(own)
            if own:
                if _same_file(model, self.model_path):
                    self.pid, self.owned = own["pid"], True
                    log(f"▶ {self.name} 已在執行（lec 先前啟動、模型相同），沿用")
                    self._wait_ready()
                    return "reused"
                log(f"▶ {self.name} 模型不同（{Path(model or '?').name} → {Path(self.model_path).name}），重新啟動")
                self._kill(own["pid"])
            else:
                if model is not None and not _same_file(model, self.model_path):
                    raise ServerError(
                        f"port {self.port} 上有手動開啟的 {self.name}，載入的是 {model}，"
                        f"跟設定的 {self.model_path} 不同。請先關掉它或改設定。")
                note = "" if model else "（無法確認模型）"
                log(f"⚠ {self.name} 已由其他程式開啟{note}，沿用但結束時不會關閉它")
                self._wait_ready()
                return "external"
        self._start()
        return "started"

    def _start(self):
        binary = Path(self.binary())
        if not os.access(binary, os.X_OK):
            raise ServerError(f"找不到可執行檔 {binary}，請確認已編譯")
        if not Path(self.model_path).is_file():
            raise ServerError(f"找不到模型 {self.model_path}")
        log(f"▶ 啟動 {self.name}（{Path(self.model_path).name}，載入中…）")
        fh = open(self.log_path, "a", encoding="utf-8")
        # start_new_session：Ctrl+C 不會直接打斷 server，等收尾時再關
        proc = subprocess.Popen(self.build_cmd(), stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True,
                                env=self._env(binary))
        fh.close()
        self.pid, self.owned = proc.pid, True
        self.own_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.own_file, {"pid": proc.pid, "port": self.port, "model": self.model_path,
                                   "started": time.time()})
        self._wait_ready(proc)
        log(f"▶ {self.name} 就緒（{self.backend_note(binary)}）")

    def backend_note(self, binary):
        """確認是否使用 Vulkan：先看 log；新版靜態編譯的 llama-server 啟動時不一定印出，改問 --list-devices。"""
        for _ in range(3):
            try:
                if "vulkan" in self.log_path.read_text(encoding="utf-8", errors="replace").lower():
                    return "Vulkan 已啟用"
            except OSError:
                pass
            time.sleep(1)
        try:
            out = subprocess.run([str(binary), "--list-devices"], capture_output=True, text=True,
                                 timeout=20, env=self._env(binary))
            devs = [l.strip() for l in (out.stdout + out.stderr).splitlines()
                    if l.strip().lower().startswith("vulkan")]
            if devs:
                return f"Vulkan：{devs[0]}"
        except (OSError, subprocess.TimeoutExpired):
            pass
        return "⚠ 沒偵測到 Vulkan，可能在跑純 CPU"

    @staticmethod
    def _env(binary):
        """把 build 內的共用函式庫資料夾加進 LD_LIBRARY_PATH。
        whisper.cpp / llama.cpp 預設編成 .so，RPATH 寫死編譯時的絕對路徑；
        整個資料夾搬家後不重新編譯也能找到 libggml*.so。"""
        env = os.environ.copy()
        build = Path(binary).resolve().parent.parent
        try:
            dirs = sorted({str(p.parent) for p in build.rglob("lib*.so*")})
        except OSError:
            dirs = []
        if dirs:
            old = env.get("LD_LIBRARY_PATH")
            env["LD_LIBRARY_PATH"] = ":".join(dirs + ([old] if old else []))
        return env

    def _wait_ready(self, proc=None):
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self.is_ready():
                return
            if proc is not None and proc.poll() is not None:
                self.own_file.unlink(missing_ok=True)
                raise ServerError(f"{self.name} 啟動失敗，見 {self.log_path}\n" + self._log_tail())
            time.sleep(1)
        raise ServerError(f"{self.name} {self.startup_timeout} 秒內沒有就緒，見 {self.log_path}")

    def _log_tail(self, n=8):
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join("    " + l for l in lines[-n:])
        except OSError:
            return ""

    @staticmethod
    def _kill(pid, timeout=15):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                return
            except PermissionError:
                os.kill(pid, sig)
            end = time.time() + (timeout if sig == signal.SIGTERM else 5)
            while time.time() < end:
                try:                       # 若是自己的子程序要回收，避免殭屍
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
                if not pid_alive(pid):
                    return
                time.sleep(0.3)

    def stop(self):
        if self.owned and self.pid:
            log(f"▶ 關閉 {self.name}")
            self._kill(self.pid)
            self.own_file.unlink(missing_ok=True)
        self.pid, self.owned = None, False

    def kill_now(self):
        """強制結束時用：不等待。"""
        if self.owned and self.pid:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except OSError:
                pass
            self.own_file.unlink(missing_ok=True)


class WhisperServer(ManagedServer):
    name = "whisper-server"
    health_path = "/"

    def __init__(self, cfg):
        w = cfg["whisper"]
        super().__init__(w["host"], w["port"], cfg.whisper_model_path(),
                         cfg.path(cfg["paths"]["state_dir"]), startup_timeout=120)
        self.whisper_dir = cfg.path(cfg["paths"]["whisper_dir"])
        self.lang, self.threads = w["language"], w["threads"]

    def binary(self):
        return self.whisper_dir / "build" / "bin" / "whisper-server"

    def build_cmd(self):
        return [str(self.binary()), "-m", self.model_path, "-l", self.lang,
                "-t", str(self.threads), "--host", self.host, "--port", str(self.port)]


class LlamaServer(ManagedServer):
    name = "llama-server"
    health_path = "/health"

    def __init__(self, cfg, model=None):
        l = cfg["llm"]
        self.model = cfg.llm_model(model)
        super().__init__(l["host"], l["port"], self.model["path"],
                         cfg.path(cfg["paths"]["state_dir"]), startup_timeout=l["startup_timeout"])
        self.llama_dir = cfg.path(cfg["paths"]["llama_dir"])
        self.l = l

    def binary(self):
        return self.llama_dir / "build" / "bin" / "llama-server"

    def build_cmd(self):
        return [str(self.binary()), "-m", self.model_path, "-ngl", str(self.l["ngl"]),
                "-c", str(self.l["ctx"]), "-np", str(self.l["parallel"]),
                "--host", self.host, "--port", str(self.port), *map(str, self.l["extra_args"])]

    def running_model(self, own):
        st, body = http_get(self.url + "/props")
        if st == 200:
            try:
                path = json.loads(body).get("model_path")
                if path:
                    return path
            except ValueError:
                pass
        return super().running_model(own)
