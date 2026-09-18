"""一次 lec run / lec summarize 的完整流程（server 生命週期、訊號處理、收尾）。"""
import os
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import platform as P
from .config import ConfigError
from .servers import LlamaServer, ServerError, WhisperServer
from .status import RunLock, Status
from .summarize import Summarizer
from .transcribe import Transcriber
from .util import atomic_write, die, log, safe_name


def make_session_dir(cfg):
    root = cfg.path(cfg["paths"]["output_root"])
    now = datetime.now()
    try:
        name = cfg["paths"]["session_name"].format(course=cfg.course_name, date=now)
    except (KeyError, ValueError, IndexError) as e:
        raise ConfigError(f"paths.session_name 格式錯誤（可用 {{course}}、{{date:%Y%m%d}}）：{e}") from None
    d = root / safe_name(name)
    base, n = d, 1
    # 同一天同課名再錄，另開資料夾避免混在一起
    while d.exists() and any(d.iterdir()):
        d = base.with_name(f"{base.name}_{datetime.now():%H%M}" + (f"-{n}" if n > 1 else ""))
        n += 1
    d.mkdir(parents=True, exist_ok=True)
    return d


STOP_FILE, STOP_FORCE_FILE = "stop", "stop_force"


class _Base:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stage = "loading"
        self.whisper = None
        self.llama = None
        self.summarizer = None
        self.status = None
        self.lock = RunLock(cfg.state_dir())
        self.inhibitor = P.Inhibitor()
        self.presses = 0
        self._watcher = None
        self._watch_stop = threading.Event()

    def _install_signals(self):
        signal.signal(signal.SIGINT, self._on_signal)
        if not P.IS_WINDOWS:
            signal.signal(signal.SIGTERM, self._on_signal)

    def _start_stop_watcher(self):
        """UI 與 lec stop 透過輸出資料夾的 stop / stop_force 檔要求停止
        （Windows 無法對別的行程送 SIGINT，三個平台統一走檔案）。"""
        def loop():
            while not self._watch_stop.wait(1):
                try:
                    force, once = self.dir / STOP_FORCE_FILE, self.dir / STOP_FILE
                    if force.exists():
                        force.unlink(missing_ok=True)
                        log("▶ 收到強制停止要求（stop_force）")
                        self._force_exit()
                    if once.exists():
                        once.unlink(missing_ok=True)   # 用掉就刪，避免重複觸發
                        log("▶ 收到停止要求（stop 檔）")
                        self._on_signal(None, None)
                except OSError:
                    pass
        self._watcher = threading.Thread(target=loop, daemon=True)
        self._watcher.start()

    def _clear_stop_files(self):
        for name in (STOP_FILE, STOP_FORCE_FILE):
            try:
                (self.dir / name).unlink(missing_ok=True)
            except OSError:
                pass

    def _force_exit(self):
        log("✖ 強制結束")
        for srv in (self.whisper, self.llama):
            if srv:
                srv.kill_now()
        self.inhibitor.stop()
        if self.status:
            self.status.update(phase="aborted")
            self.status.flush()
        self.lock.release()
        log.close()
        os._exit(130)

    def _on_signal(self, signum, frame):
        raise NotImplementedError

    def _start_llama(self):
        self.llama = LlamaServer(self.cfg)
        if self.status:
            self.status.set_server("llama", "loading")
        self.llama.ensure(self.dir / "llama-server.log")
        if self.status:
            self.status.set_server("llama", "ok")

    def _stop_watcher(self):
        self._watch_stop.set()

    def _cleanup(self):
        self._stop_watcher()
        for srv, key in ((self.llama, "llama"), (self.whisper, "whisper")):
            if srv:
                try:
                    srv.stop()
                except Exception as e:
                    log(f"⚠ 關閉 {srv.name} 失敗：{e}")
                if self.status:
                    self.status.set_server(key, "stopped")
        self.inhibitor.stop()


# ---------------------------------------------------------------- lec run
class LectureRun(_Base):
    def __init__(self, cfg, input_file=None):
        super().__init__(cfg)
        self.input_file = Path(input_file).resolve() if input_file else None
        self.live = self.input_file is None
        self.transcriber = None
        self.finish = threading.Event()

    def _on_signal(self, signum, frame):
        self.presses += 1
        if self.stage == "loading":
            raise KeyboardInterrupt
        if self.stage == "transcribe":
            if self.presses == 1:
                print(flush=True)
                self.transcriber.request_stop()
                if self.status:
                    self.status.event("stop_requested")
                return
            self._force_exit()
        if self.stage == "summarize" and self.summarizer is None:
            raise KeyboardInterrupt
        if self.stage == "summarize" and not self.summarizer.abort.is_set():
            print(flush=True)
            log("▶ 放棄剩餘總結，收尾中…（再按一次 Ctrl+C 強制結束）")
            self.summarizer.abort.set()
            return
        self._force_exit()

    def run(self):
        cfg = self.cfg
        if self.input_file and not self.input_file.is_file():
            die(f"找不到檔案：{self.input_file}")
        for cmd in ("ffmpeg", "curl"):
            if not shutil.which(cmd):
                die(f"缺少指令：{cmd}")

        cur = self.lock.acquire(course=cfg.course_name,
                                mode="live" if self.live else "file")
        if cur:
            die(f"已有 lec 在執行（pid {cur.get('pid')}，課程 {cur.get('course')}，"
                f"{cur.get('session', '')}）。可用 lec status 查看、lec stop 停止。")
        self._install_signals()
        code = 0
        try:
            self.dir = make_session_dir(cfg)
            log.attach(self.dir / "session.log")
            self._clear_stop_files()
            self._start_stop_watcher()
            atomic_write(self.dir / "config.used.toml", cfg.dump())
            self.lock.update(session=str(self.dir))
            self.status = Status(self.dir, cfg["system"]["status_interval"],
                                 course=cfg.course_name, session=str(self.dir),
                                 mode="live" if self.live else "file",
                                 input_file=str(self.input_file or ""),
                                 summary_model=cfg["summary"]["model"] if cfg["summary"]["enabled"] else None)
            log(f"▶ 課程：{cfg.course_name}" + (f"（設定檔 {cfg.course_file.name}）" if cfg.course_file else ""))
            log(f"▶ 輸出資料夾：{self.dir}")
            for w in cfg.warnings:
                log(f"⚠ {w}")
            if cfg["system"]["inhibit_sleep"]:
                self.inhibitor.start(f"lec：{cfg.course_name}", log)

            self.status.phase("loading")
            summary_on = cfg["summary"]["enabled"]
            self.whisper = WhisperServer(cfg)
            self.status.set_server("whisper", "loading")
            self.whisper.ensure(self.dir / "whisper-server.log")
            self.status.set_server("whisper", "ok")

            thread = None
            if summary_on and self.live:
                try:
                    self._start_llama()
                    self.summarizer = Summarizer(cfg, self.dir, self.llama.url, self.llama.model, self.status)
                    thread = threading.Thread(target=self.summarizer.run_live, args=(self.finish,),
                                              daemon=True)
                except (ServerError, ConfigError) as e:
                    log(f"⚠ 無法啟動總結，本次只轉錄：{e}")
                    self.status.set_server("llama", "failed")
                    self.status.error(f"llama-server：{e}")
                    self.summarizer = None

            self.transcriber = Transcriber(cfg, self.dir, self.whisper.url, self.input_file, self.status)
            if self.live:
                log(f"  即時逐字稿：{self.dir / 'transcript.md'}")
                if self.summarizer:
                    log(f"  即時筆記：{self.dir / 'notes.md'}（模型 {self.llama.model['name']}）")
            if thread:
                thread.start()
            self.stage = "transcribe"
            self.status.phase("recording" if self.live else "transcribing")
            result = self.transcriber.run()

            # ---- 總結收尾
            if thread:
                self.stage = "summarize"
                self.status.phase("summarizing")
                wait = cfg["summary"]["final_wait"]
                log(f"▶ 補做最後一段總結（最多等 {wait} 秒，Ctrl+C 可放棄）")
                self.finish.set()
                deadline = time.time() + wait
                while thread.is_alive() and time.time() < deadline and not self.summarizer.abort.is_set():
                    thread.join(0.5)
                if thread.is_alive():
                    self.summarizer.abort.set()
                    log("⚠ 最後一段總結未完成，之後可執行：lec summarize " f"\"{self.dir}\"")
            elif (summary_on and not self.live and cfg["summary"]["file_mode"] == "after"
                  and not result["aborted"]):
                self.stage = "summarize"
                self.status.phase("summarizing")
                # 轉錄完先關 whisper，把 iGPU 讓給 LLM
                self.whisper.stop()
                self.status.set_server("whisper", "stopped")
                try:
                    self._start_llama()
                    self.summarizer = Summarizer(cfg, self.dir, self.llama.url, self.llama.model, self.status)
                    n = self.summarizer.run_all()
                    log(f"✔ 總結完成 {n} 段")
                except (ServerError, ConfigError) as e:
                    log(f"✖ 無法總結：{e}")
                    self.status.error(str(e))
            self.stage = "cleanup"
            self.status.phase("finishing")
        except KeyboardInterrupt:
            log("✖ 已取消")
            code = 130
        except (ServerError, ConfigError) as e:
            log(f"✖ {e}")
            if self.status:
                self.status.error(str(e))
            code = 1
        finally:
            self.stage = "cleanup"
            self._cleanup()
            if self.status:
                self.status.phase("done" if code == 0 else "failed")
                self.status.close()
            self.lock.release()
            if hasattr(self, "dir"):
                log("✔ 結束。輸出：")
                for name in ("transcript.md", "transcript.srt", "notes.md"):
                    if (self.dir / name).exists():
                        log(f"  {self.dir / name}")
            log.close()
        return code


# ---------------------------------------------------------------- lec summarize
class OfflineSummary(_Base):
    def __init__(self, cfg, session_dir, redo=None):
        super().__init__(cfg)
        self.dir = Path(session_dir).resolve()
        self.redo = redo

    def _on_signal(self, signum, frame):
        self.presses += 1
        if self.stage == "loading":
            raise KeyboardInterrupt
        if self.summarizer and not self.summarizer.abort.is_set():
            print(flush=True)
            log("▶ 完成目前這段後停止（再按一次 Ctrl+C 強制結束）")
            self.summarizer.abort.set()
            return
        self._force_exit()

    def run(self):
        if not (self.dir / "transcript.md").exists():
            die(f"{self.dir} 裡沒有 transcript.md")
        cur = self.lock.acquire(course=self.cfg.course_name, session=str(self.dir), mode="summarize")
        if cur:
            die(f"已有 lec 在執行（pid {cur.get('pid')}，{cur.get('session', '')}），請等它結束")
        self._install_signals()
        log.attach(self.dir / "session.log")
        self._clear_stop_files()
        self._start_stop_watcher()
        code = 0
        try:
            log(f"▶ 離線總結：{self.dir}（課程 {self.cfg.course_name}，模型 {self.cfg['summary']['model']}）")
            for w in self.cfg.warnings:
                log(f"⚠ {w}")
            self.llama = LlamaServer(self.cfg)
            self.summarizer = Summarizer(self.cfg, self.dir, self.llama.url, self.llama.model)
            # 先確認有事可做，避免白白載入模型
            if self.redo and self.redo != "all":
                self.summarizer.redo_block(self.redo)
            elif not self.redo and not self.summarizer.next_block(self.summarizer.read_sections(), done=True):
                log("▷ 沒有尚未總結的段落（要重做請加 --redo <時間> 或 --redo all）")
                return code
            if self.cfg["system"]["inhibit_sleep"]:
                self.inhibitor.start(f"lec summarize：{self.cfg.course_name}", log)
            self._start_llama()
            self.stage = "summarize"
            t0 = time.time()
            n = self.summarizer.redo(self.redo) if self.redo else self.summarizer.run_all()
            log(f"✔ 完成 {n} 段，耗時 {time.time() - t0:.0f}s：{self.dir / 'notes.md'}")
        except KeyboardInterrupt:
            log("✖ 已取消")
            code = 130
        except (ServerError, ConfigError, ValueError) as e:
            log(f"✖ {e}")
            code = 1
        finally:
            self.stage = "cleanup"
            self._cleanup()
            self.lock.release()
            log.close()
        return code
