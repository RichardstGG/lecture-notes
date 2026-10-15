"""一次 lec run / lec summarize 的完整流程（server 生命週期、訊號處理、收尾）。"""
import os
import hashlib
import json
import math
import tempfile
from dataclasses import asdict
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path, PureWindowsPath

from . import platform as P
from .config import ConfigError, meeting_output_parts
from .servers import LlamaServer, ServerError, WhisperServer
from .status import RunLock, Status, now_iso
from .summarize import Summarizer
from .transcribe import Transcriber
from .util import atomic_write, die, log, read_json, safe_name, write_json


def capture_capabilities():
    """Product integration gate, not a hardware probe. Never opens a microphone."""
    linux = sys.platform.startswith("linux")
    return {
        "schema_version": 1,
        "single": {"available": True, "reason_code": None,
                   "message": "既有單來源流程；裝置可用性須另行檢查"},
        "dual": {"available": False,
                 "reason_code": "engine_not_integrated" if linux else "unsupported_platform",
                 "message": ("雙音源引擎與會議流程尚未整合" if linux else
                             "此平台尚不提供雙音源錄音；第一版限 Linux PulseAudio 相容服務")},
    }


def make_session_dir(cfg):
    root = cfg.path(cfg["paths"]["output_root"])
    now = datetime.now()
    try:
        name = cfg["paths"]["session_name"].format(
            course=safe_name(cfg.course_name), date=now,
        )
    except (KeyError, ValueError, IndexError) as e:
        raise ConfigError(
            f"paths.session_name 格式錯誤（可用 {{course}}、{{date:%Y%m%d}}，"
            f"可用 / 分層）：{e}"
        ) from None
    parts = [safe_name(part) for part in re.split(r"[/\\]", name)
             if part.strip() not in ("", ".", "..")]
    d = root.joinpath(*parts) if parts else root / "session"
    base, n = d, 1
    # 同一天同課名再錄，另開資料夾避免混在一起
    while d.exists() and any(d.iterdir()):
        d = base.with_name(f"{base.name}_{datetime.now():%H%M}" + (f"-{n}" if n > 1 else ""))
        n += 1
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meeting_path_is_within(path, root):
    """Compare resolved paths without Windows' optional extended-length prefix."""
    def identity(value):
        if isinstance(value, PureWindowsPath):
            text = str(value)
            if text.startswith("\\\\?\\"):
                tail = text[4:]
                if tail[:4].upper() == "UNC\\":
                    return type(value)("\\\\" + tail[4:])
                if re.match(r"^[A-Za-z]:\\", tail):
                    return type(value)(tail)
        return value

    return identity(path).is_relative_to(identity(root))


def make_meeting_session_dir(cfg):
    """Allocate a fresh directory, including when an existing collision is empty."""
    root = cfg.path(cfg["paths"]["output_root"]).resolve()
    parts = meeting_output_parts(cfg.get("paths.meeting_output_root"))
    parent = root
    # Refuse symlinked meeting subdirectories, including dangling links.
    for part in parts:
        parent = parent / part
        if parent.is_symlink():
            raise ConfigError("會議輸出路徑不可包含符號連結")
    if not _meeting_path_is_within(parent.resolve(), root):
        raise ConfigError("會議輸出路徑不可超出 output_root")
    name = cfg.meeting_directory_name(datetime.now())
    parent.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        candidate = parent / (name if suffix == 1 else f"{name}-{suffix}")
        try:
            candidate.mkdir()  # Atomic exclusive allocation, never exist_ok=True.
            return candidate
        except FileExistsError:
            suffix += 1


STOP_FILE, STOP_FORCE_FILE = "stop", "stop_force"


class _Base:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stage = "loading"
        self.whisper = None
        self.llama = None
        self.summarizer = None
        self.summary_thread = None
        self.status = None
        self.lock = RunLock(cfg.state_dir())
        self.inhibitor = P.Inhibitor()
        self.presses = 0
        self._watcher = None
        self._watch_stop = threading.Event()

    def _acquire_lock(self, **info):
        try:
            return self.lock.acquire(**info)
        except OSError as exc:
            die(f"無法取得工作鎖：{exc}")

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

    def _summary_target(self):
        remote = self.cfg.remote_summary()
        if remote is not None:
            return remote
        if self.llama is None:
            self.llama = LlamaServer(self.cfg)
        return self.llama.url, self.llama.model

    def _start_llama(self):
        if self.cfg.get("summary.upstream", "local") != "local":
            if self.status:
                self.status.update(summary_connection="ready")
            return
        if self.llama is None:
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
        if self.summarizer:
            self.summarizer.abort.set()
        if self.summary_thread and self.cfg.get("summary.upstream", "local") != "local":
            self.summary_thread.join(1)
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
        if cfg.work_type != "lecture":
            die("此入口只處理 lecture；會議或未知工作類型不可使用課堂流程")
        # Reject before acquiring the shared lock, creating files or starting engines.
        # File input must not silently discard a requested dual capture either.
        if cfg.capture_plan()["mode"] == "dual":
            die("capture_unavailable：" + capture_capabilities()["dual"]["message"])
        if self.input_file and not self.input_file.is_file():
            die(f"找不到檔案：{self.input_file}")
        for cmd in ("ffmpeg", "curl"):
            if not shutil.which(cmd):
                die(f"缺少指令：{cmd}")

        cur = self._acquire_lock(course=cfg.course_name,
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
                                 summary_model=(cfg.get("summary.upstream", "local") if cfg.get("summary.upstream", "local") != "local"
                                                else cfg["summary"]["model"]) if cfg["summary"]["enabled"] else None,
                                 summary_upstream=cfg.get("summary.upstream", "local"))
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
                    target = self._summary_target()
                    self._start_llama()
                    self.summarizer = Summarizer(cfg, self.dir, *target, self.status)
                    thread = threading.Thread(target=self.summarizer.run_live, args=(self.finish,),
                                              daemon=True)
                except (ServerError, ConfigError) as e:
                    log(f"⚠ 無法啟動總結，本次只轉錄：{e}")
                    if cfg.get("summary.upstream", "local") == "local":
                        self.status.set_server("llama", "failed")
                    else:
                        self.status.update(summary_connection="failed")
                    self.status.error(f"摘要上游：{e}")
                    self.summarizer = None

            self.transcriber = Transcriber(cfg, self.dir, self.whisper.url, self.input_file, self.status)
            if self.live:
                log(f"  即時逐字稿：{self.dir / 'transcript.md'}")
                if self.summarizer:
                    log(f"  即時筆記：{self.dir / 'notes.md'}（模型 {self.summarizer.model['name']}）")
            if thread:
                self.summary_thread = thread
                thread.start()
            self.stage = "transcribe"
            self.status.phase("recording" if self.live else "transcribing")
            result = self.transcriber.run()
            if result.get("gaps"):
                self.status.event("transcription_gaps", gaps=result["gaps"],
                                  lost_seconds=result["lost_seconds"],
                                  ffmpeg_returncode=result.get("ffmpeg_returncode"))
            if result.get("ffmpeg_failed") and result["duration"] == 0:
                code = 1
                self.status.error(
                    f"沒有讀到任何音訊（ffmpeg 結束代碼 {result.get('ffmpeg_returncode')}）")

            # ---- 總結收尾
            if code and thread:
                self.summarizer.abort.set()
                self.finish.set()
                thread.join(cfg["summary"]["final_wait"])
            elif thread:
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
                    if cfg.get("summary.upstream", "local") != "local":
                        thread.join(1)  # remote waiter observes abort within 0.1s
                    log("⚠ 最後一段總結未完成，之後可執行：lec summarize " f"\"{self.dir}\"")
            elif (summary_on and not self.live and cfg["summary"]["file_mode"] == "after"
                  and not result["aborted"] and code == 0):
                self.stage = "summarize"
                self.status.phase("summarizing")
                # 轉錄完先關 whisper，把 iGPU 讓給 LLM
                self.whisper.stop()
                self.status.set_server("whisper", "stopped")
                try:
                    target = self._summary_target()
                    self._start_llama()
                    self.summarizer = Summarizer(cfg, self.dir, *target, self.status)
                    n = self.summarizer.run_all()
                    log(f"✔ 總結完成 {n} 段")
                except (ServerError, ConfigError) as e:
                    log(f"✖ 無法總結：{e}")
                    if cfg.get("summary.upstream", "local") != "local":
                        self.status.update(summary_connection="failed")
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
            log("▶ 停止摘要並收尾（再按一次 Ctrl+C 強制結束）")
            self.summarizer.abort.set()
            return
        self._force_exit()

    def run(self):
        if self.cfg.work_type != "lecture":
            die("此入口只處理 lecture；會議或未知工作類型不可使用課堂流程")
        if not (self.dir / "transcript.md").exists():
            die(f"{self.dir} 裡沒有 transcript.md")
        cur = self._acquire_lock(course=self.cfg.course_name, session=str(self.dir), mode="summarize")
        if cur:
            die(f"已有 lec 在執行（pid {cur.get('pid')}，{cur.get('session', '')}），請等它結束")
        self._install_signals()
        log.attach(self.dir / "session.log")
        self._clear_stop_files()
        self._start_stop_watcher()
        code = 0
        try:
            log(f"▶ 離線總結：{self.dir}（課程 {self.cfg.course_name}，上游 {self.cfg.get('summary.upstream', 'local')}）")
            for w in self.cfg.warnings:
                log(f"⚠ {w}")
            target = self._summary_target()
            self.summarizer = Summarizer(self.cfg, self.dir, *target)
            # 先確認有事可做，避免白白載入模型
            if self.redo and self.redo != "all":
                self.summarizer.redo_block(self.redo)
            elif not self.redo and not self.summarizer.next_block(self.summarizer.read_sections(), done=True):
                log("▷ 沒有尚未總結的段落（要重做請加 --redo <時間> 或 --redo all）")
                return code
            previous = read_json(self.dir / "status.json", {})
            initial = previous if isinstance(previous, dict) else {}
            initial.update(course=self.cfg.course_name, session=str(self.dir), pid=os.getpid(),
                           mode="summarize", summary_upstream=self.cfg.get("summary.upstream", "local"),
                           summary_model=target[1]["name"], summary_connection=None,
                           llm_busy=False, llm_section=None,
                           servers={"whisper": "not_started", "llama": "not_started"})
            self.status = Status(self.dir, self.cfg["system"]["status_interval"], **initial)
            self.summarizer.status = self.status
            self.status.phase("loading")
            if self.cfg["system"]["inhibit_sleep"]:
                self.inhibitor.start(f"lec summarize：{self.cfg.course_name}", log)
            self._start_llama()
            self.stage = "summarize"
            self.status.phase("summarizing")
            t0 = time.time()
            n = self.summarizer.redo(self.redo) if self.redo else self.summarizer.run_all()
            log(f"✔ 完成 {n} 段，耗時 {time.time() - t0:.0f}s：{self.dir / 'notes.md'}")
        except KeyboardInterrupt:
            log("✖ 已取消")
            code = 130
        except (ServerError, ConfigError, ValueError) as e:
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
            log.close()
        return code


# ------------------------------------------------------------- meeting jobs
class MeetingJobError(Exception):
    """Safe public error: engine diagnostics remain in local session logs."""
    def __init__(self, code, message, exit_code=1):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def source_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def source_duration(path):
    """Check the saved audio, including decode errors, before publishing metadata."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)], capture_output=True, timeout=30)
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        if probe.returncode or not math.isfinite(duration) or duration <= 0:
            raise ValueError
        check = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
             "-map", "0:a:0", "-f", "null", "-"], capture_output=True, timeout=max(120, duration * 2))
        if check.returncode:
            raise ValueError
        return duration
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        raise MeetingJobError("decode_failed", "來源音檔無法完整解碼") from None


def verify_meeting_source(session):
    metadata = read_json(session / "source.json")
    if not isinstance(metadata, dict):
        raise MeetingJobError("source_missing", "缺少可重跑的來源紀錄")
    name = metadata.get("path")
    duration = metadata.get("duration_seconds")
    if (type(metadata.get("schema_version")) is not int or metadata["schema_version"] != 1
            or metadata.get("kind") not in ("recording", "import")
            or not isinstance(name, str) or not re.fullmatch(r"source_audio\.[A-Za-z0-9]+", name)
            or not isinstance(metadata.get("original_name"), str)
            or type(metadata.get("size_bytes")) is not int or metadata["size_bytes"] <= 0
            or not isinstance(metadata.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"])
            or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0
            or not isinstance(metadata.get("created_at"), str)
            or type(metadata.get("requested_speakers")) is not int
            or not 1 <= metadata["requested_speakers"] <= 30):
        raise MeetingJobError("input_invalid", "來源紀錄格式不合法", 2)
    source = session / name
    if source.is_symlink() or not source.is_file():
        raise MeetingJobError("source_missing", "session 內來源音檔不存在")
    # The engine intentionally hashes again at its own boundary; this check
    # protects orchestration, while the second detects changes before inference.
    if source.stat().st_size != metadata["size_bytes"] or source_sha256(source) != metadata["sha256"]:
        raise MeetingJobError("source_hash_mismatch", "來源大小或雜湊不符，禁止重跑")
    return source.resolve(), metadata


class _MeetingJob(_Base):
    operation = None

    def __init__(self, cfg, speakers):
        super().__init__(cfg)
        if type(speakers) is not int or not 1 <= speakers <= 30:
            raise MeetingJobError("input_invalid", "預計人數必須是 1..30 的整數", 2)
        self.speakers = speakers
        self.cancelled = threading.Event()
        self.error_code = self.error_message = None
        self.outcome = None
        self.on_force = None
        self._old_signals = {}
        self._terminal_published = False
        self._diarization_published = False
        self._forced = False
        self._manifest_before = None

    def _install_signals(self):
        for sig in ([signal.SIGINT] if P.IS_WINDOWS else [signal.SIGINT, signal.SIGTERM]):
            self._old_signals[sig] = signal.getsignal(sig)
        super()._install_signals()

    def _on_signal(self, signum, frame):
        if self.stage == "cleanup":
            if signum is None:
                return
            self.presses += 1
            if self.presses > 1:
                self._force_cleanup_exit()
            return
        if self._has_published_diarization():
            return
        self.presses += 1
        if self.presses > 1:
            self._force_exit()
        self.cancelled.set()
        if self.status:
            self.status.update(stop_reason="user")
        if self.stage == "transcribe" and self.transcriber:
            self.transcriber.request_stop()
        elif self.stage != "diarize" and signum is not None:
            raise KeyboardInterrupt

    def _has_published_diarization(self):
        if self.operation != "diarize":
            return False
        if self._diarization_published:
            return True
        # The engine switches the manifest just before returning. Observe that
        # commit point too, closing the gap before the success flag is set.
        current = read_json(self.dir / "diarization.current.json")
        return (isinstance(current, dict) and current != self._manifest_before
                and current.get("schema_version") == 1
                and isinstance(current.get("generation"), str))

    def _force_exit(self):
        if self.stage == "cleanup" or self._has_published_diarization():
            return
        self._forced = True
        # During segmentation the engine owns a detached worker. Let its cancel
        # poll kill/reap that worker before releasing the shared lock. Killing
        # only this parent would leave inference running in the background.
        self.cancelled.set()
        if (self.operation == "diarize" and self.stage == "diarize"
                and self.status.get("diarization")["stage"] == "segmentation"):
            self.outcome = "aborted"
            if not self._terminal_published:
                self.status.event("diarization_cancelled", **self.status.get("diarization"))
                self.status.phase("finishing", stop_reason="user")
                self.status.phase("aborted", stop_reason="user")
                self._terminal_published = True
            if self.whisper:
                self.whisper.kill_now()
            return
        # Unfinished recordings do not receive reusable source metadata.
        transcriber = getattr(self, "transcriber", None)
        if transcriber:
            # Mark intentional capture termination before kill so the reader
            # cannot report the expected ffmpeg -9 as a source failure.
            transcriber.stopping = True
            transcriber.abort = True
            if transcriber.proc:
                transcriber.proc.kill()
        self.outcome = "aborted"
        for srv in (self.whisper, self.llama):
            if srv:
                srv.kill_now()
        self.inhibitor.stop()
        if self.status:
            if self.operation == "diarize":
                self.status.event("diarization_cancelled", **(self.status.get("diarization") or {}))
            self.status.phase("finishing", stop_reason="user")
            self.status.phase("aborted", stop_reason="user")
            self.status.close()
        if self.on_force:
            self.on_force()
        log.close()
        # Release only after publishing the final status/envelope.
        self.lock.release()
        os._exit(130)

    def _log_cleanup_error(self, exc):
        # Secondary diagnostics stay local; never replace the primary error or
        # print private engine/ASR exception text in a CLI envelope or stderr.
        try:
            with (self.dir / "session.log").open("a", encoding="utf-8") as stream:
                stream.write(f"[{now_iso()}] cleanup: {type(exc).__name__}: {exc}\n")
        except OSError:
            pass

    def _force_cleanup_exit(self):
        """Two interrupts stop teardown, preserving the already decided result."""
        terminal = self.status.get("phase") if self.status else None
        if terminal in ("done", "failed", "aborted"):
            self.outcome = terminal
        elif self._has_published_diarization():
            self.outcome = "done"
        try:
            for srv in (self.whisper, self.llama):
                if srv:
                    try:
                        srv.kill_now()  # ManagedServer kills only its owned PID.
                    except Exception as exc:
                        self._log_cleanup_error(exc)
            if self.status and terminal != self.outcome:
                if terminal != "finishing":
                    self.status.phase("finishing")
                self.status.phase(self.outcome)
            if self.on_force:
                self.on_force()
        finally:
            try:
                self.lock.release()
            finally:
                for sig, previous in self._old_signals.items():
                    try:
                        signal.signal(sig, previous)
                    except Exception as exc:
                        self._log_cleanup_error(exc)
                try:
                    log.close()
                finally:
                    os._exit(130)

    def _poll_cancel(self):
        if self._has_published_diarization():
            return False
        if (self.dir / STOP_FORCE_FILE).exists():
            self._force_exit()
        if (self.dir / STOP_FILE).exists():
            self.cancelled.set()
        return self.cancelled.is_set()

    def _execute(self):
        raise NotImplementedError

    def run(self):
        mode = "diarize" if self.operation == "diarize" else ("file" if self.input_file else "live")
        try:
            busy = self.lock.acquire(work_type="meeting", mode=mode, course=self.cfg.meeting_name)
        except OSError:
            raise MeetingJobError("write_failed", "無法取得工作鎖") from None
        if busy:
            raise MeetingJobError("busy", "已有工作執行中", 3)
        code = 0
        try:
            self._install_signals()
            if self.operation == "run":
                self.dir = make_meeting_session_dir(self.cfg)
            log.attach(self.dir / "session.log")
            self._clear_stop_files()
            self.lock.update(session=str(self.dir))
            initial = {}
            if self.operation == "diarize":
                previous = read_json(self.dir / "status.json", {})
                if isinstance(previous, dict):
                    initial = {key: previous[key] for key in (
                        "started_at", "elapsed", "transcribed", "transcribe_lag",
                        "sections_total", "sections_summarized", "input_file") if key in previous}
                self._manifest_before = read_json(self.dir / "diarization.current.json")
            self.status = Status(self.dir, self.cfg.get("system.status_interval"),
                                 work_type="meeting", course=self.cfg.meeting_name, mode=mode, **initial)
            self.status.phase("starting")
            self.status.phase("loading")
            self._start_stop_watcher()
            if self.cfg.get("system.inhibit_sleep"):
                self.inhibitor.start(f"lec meeting：{self.cfg.meeting_name}", log)
            self._execute()
        except KeyboardInterrupt:
            self.cancelled.set()
            code = 130
        except MeetingJobError as exc:
            code = exc.exit_code
            self.error_code, self.error_message = exc.code, str(exc)
        except ServerError:
            code = 1
            self.error_code, self.error_message = "engine_unavailable", "無法啟動轉錄引擎"
        except (OSError, ConfigError):
            code = 1
            self.error_code, self.error_message = "write_failed", "會議作業無法完成，請檢查本機設定與檔案"
        except Exception:
            code = 1
            self.error_code, self.error_message = "asr_failed", "會議作業發生未預期錯誤"
        finally:
            if self._forced:
                code = 130
            self.outcome = "aborted" if code == 130 else ("done" if code == 0 else "failed")
            self.stage = "cleanup"
            code = self._finalize(code)
        return code

    def _finalize(self, code):
        """Best-effort status/cleanup; lifetime lock and signals are unconditional."""
        def failure(exc):
            nonlocal code
            code = 1
            if not self.error_code:
                self.error_code = "write_failed" if isinstance(exc, OSError) else "asr_failed"
                self.error_message = "會議作業收尾失敗"
            else:
                self._log_cleanup_error(exc)

        def attempt(action):
            try:
                action()
            except Exception as exc:
                failure(exc)

        try:
            if self._terminal_published and not self._has_published_diarization():
                code = 130
            # Stop/join the watcher first so it cannot publish a competing
            # terminal state while finalization is underway.
            attempt(self._stop_watcher)
            if self._watcher:
                attempt(lambda: self._watcher.join(timeout=2))
            if self.status:
                if self.error_code:
                    attempt(lambda: self.status.error(f"{self.error_code}: {self.error_message}"))
                if code == 130:
                    attempt(lambda: self.status.update(stop_reason="user"))
                if not self._terminal_published:
                    attempt(lambda: self.status.phase("finishing"))
            attempt(self._cleanup)
            self.outcome = "aborted" if code == 130 else ("done" if code == 0 else "failed")
            if self.status:
                if not self._terminal_published:
                    attempt(lambda: self.status.phase(self.outcome))
                # If terminal phase publication failed, still try to publish
                # a failed snapshot without invoking the failed phase hook.
                if code == 1:
                    attempt(lambda: self.status.update(phase="failed", last_error=f"{self.error_code}: {self.error_message}"))
                try:
                    self.status.close()
                except Exception as exc:
                    # The terminal phase is already public. A failed final flush
                    # is diagnostic only; do not contradict its result/envelope.
                    self._log_cleanup_error(exc)
        finally:
            try:
                self.lock.release()
            finally:
                for sig, previous in self._old_signals.items():
                    attempt(lambda sig=sig, previous=previous: signal.signal(sig, previous))
                attempt(log.close)
        self.outcome = "aborted" if code == 130 else ("done" if code == 0 else "failed")
        return code

    def _start_whisper(self):
        self.whisper = WhisperServer(self.cfg)
        self.status.set_server("whisper", "loading")
        self.whisper.ensure(self.dir / "whisper-server.log")
        self.status.set_server("whisper", "ok")


class MeetingRun(_MeetingJob):
    operation = "run"

    def __init__(self, cfg, speakers, input_file=None, *, original_name=None):
        super().__init__(cfg, speakers)
        if original_name is not None:
            if not input_file or not isinstance(original_name, str):
                raise MeetingJobError("input_invalid", "原始檔名需搭配匯入音檔", 2)
            original_name = "".join(char for char in original_name if char.isprintable()).strip()[:255]
            if not original_name:
                raise MeetingJobError("input_invalid", "原始檔名不可為空白或不可列印字元", 2)
        self.original_name = original_name
        self.input_file = Path(input_file).expanduser().resolve() if input_file else None
        self.transcriber = None
        if cfg.capture_plan()["mode"] != "single":
            raise MeetingJobError("input_invalid", "雙來源錄音入口尚未啟用", 2)
        cfg.data["audio"]["keep_recording"] = True
        cfg.data["summary"]["enabled"] = False

    def _save_source(self, source, kind, original_name):
        duration = source_duration(source)
        size = source.stat().st_size
        if size <= 0:
            raise MeetingJobError("source_missing", "來源音檔是空檔")
        write_json(self.dir / "source.json", {
            "schema_version": 1, "kind": kind, "original_name": original_name,
            "path": source.name, "size_bytes": size, "sha256": source_sha256(source),
            "duration_seconds": duration, "created_at": now_iso(),
            "requested_speakers": self.speakers,
        })

    def _execute(self):
        atomic_write(self.dir / "config.used.toml", self.cfg.dump())
        source = None
        if self.input_file:
            if not self.input_file.is_file():
                raise MeetingJobError("source_missing", "找不到匯入音檔", 2)
            suffix = self.input_file.suffix.lstrip(".")
            if not re.fullmatch(r"[A-Za-z0-9]+", suffix):
                raise MeetingJobError("input_invalid", "匯入音檔需要合法副檔名", 2)
            source = self.dir / f"source_audio.{suffix}"
            fd, temporary = tempfile.mkstemp(prefix=".source-", dir=self.dir)
            os.close(fd)
            try:
                shutil.copyfile(self.input_file, temporary)
                os.replace(temporary, source)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self._save_source(source, "import", self.original_name or self.input_file.name)
        if self._poll_cancel():
            raise KeyboardInterrupt
        self._start_whisper()
        if self._poll_cancel():
            raise KeyboardInterrupt
        self.transcriber = Transcriber(self.cfg, self.dir, self.whisper.url, source, self.status)
        self.stage = "transcribe"
        self.status.phase("transcribing" if source else "recording")
        if self.cancelled.is_set():
            self.transcriber.request_stop()
        result = self.transcriber.run()
        if self._forced:
            raise KeyboardInterrupt
        if self.cancelled.is_set() or result.get("aborted"):
            self.status.update(stop_reason="user")
        if self.input_file and result.get("aborted"):
            raise KeyboardInterrupt
        if result.get("gaps"):
            self.status.event("transcription_gaps", gaps=result["gaps"],
                              lost_seconds=result.get("lost_seconds"),
                              ffmpeg_returncode=result.get("ffmpeg_returncode"))
        if result.get("ffmpeg_failed"):
            raise MeetingJobError("decode_failed", "音訊擷取或解碼未完整完成")
        if not self.input_file:
            from .transcribe import CAPTURE_LOSS_ERROR
            capture = result.get("capture")
            if not capture or capture.get("lost_ratio", 1) >= CAPTURE_LOSS_ERROR:
                raise MeetingJobError("source_incomplete", "錄音完整性檢查未通過，禁止會後辨識")
            # Native PTS gaps may be hidden by the saved container timestamps.
            for line in (self.dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event.get("type") == "capture_integrity":
                    native = event.get("detail", {}).get("native_capture")
                    if native and native.get("lost_seconds", 0) / max(native.get("pts_span_seconds", 0), 1e-9) >= CAPTURE_LOSS_ERROR:
                        raise MeetingJobError("source_incomplete", "原生擷取完整性檢查未通過")
            recording = self.transcriber.recording_path
            if not recording or not Path(recording).is_file():
                raise MeetingJobError("source_missing", "錄音未保存來源音檔")
            # Integrity is checked above; publish metadata only after decode verification.
            source = self.dir / "source_audio.ogg"
            os.replace(recording, source)
            self._save_source(source, "recording", Path(recording).name)


class MeetingDiarize(_MeetingJob):
    operation = "diarize"

    def __init__(self, cfg, speakers, session_dir):
        super().__init__(cfg, speakers)
        self.dir = Path(session_dir).expanduser().resolve()
        self._manifest_before = read_json(self.dir / "diarization.current.json")

    def _progress(self, event):
        previous = self.status.get("diarization") or {}
        total = max(0.0, float(event["total_seconds"]))
        processed = min(total, max(0.0, float(event["processed_seconds"])))
        if previous.get("stage") == event["stage"]:
            processed = min(total, max(processed, previous.get("processed_seconds", 0)))
        data = dict(event, processed_seconds=processed, total_seconds=total,
                    requested_speakers=self.speakers, actual_speakers=None)
        self.status.update(diarization=data)
        self.status.event("diarization_progress", **data)
        self.status.flush()

    def _execute(self):
        from .diarize import DiarizationRequest, DiarizationCancelled, DiarizationError, diarize_session
        source, metadata = verify_meeting_source(self.dir)
        options = self.cfg.diarization_options(require_speakers=True)
        options["requested_speakers"] = self.speakers
        for key in ("segmentation_model", "embedding_model"):
            if not options[key].is_file():
                raise MeetingJobError("model_missing", "缺少會後辨識模型")
        self.whisper = WhisperServer(self.cfg)
        request = DiarizationRequest(
            session_dir=self.dir, source_audio=source, source_sha256=metadata["sha256"],
            meeting_name=self.cfg.meeting_name, whisper_server=self.whisper.url,
            whisper_prompt=self.cfg.whisper_prompt(), threads=self.cfg.get("whisper.threads"),
            opencc=self.cfg.get("system.opencc"), **options)
        record = {key: str(value) if isinstance(value, Path) else value
                  for key, value in asdict(request).items()}
        record.update(schema_version=1, spec_version=1, created_at=now_iso(),
                      segmentation_model_sha256=source_sha256(request.segmentation_model),
                      embedding_model_sha256=source_sha256(request.embedding_model))
        write_json(self.dir / "diarization.request.json", record)
        self.status.set_server("whisper", "loading")
        self.whisper.ensure(self.dir / "whisper-server.log")
        self.status.set_server("whisper", "ok")
        if self._poll_cancel():
            raise KeyboardInterrupt
        self.stage = "diarize"
        self.status.phase("diarizing", diarization={
            "stage": "segmentation", "processed_seconds": 0, "total_seconds": metadata["duration_seconds"],
            "requested_speakers": self.speakers, "speakers_found": 0, "actual_speakers": None})
        try:
            result = diarize_session(request, progress=self._progress, cancel=self._poll_cancel)
            self._diarization_published = True
            self._terminal_published = False
            self.status.update(stop_reason=None)
            self.stage = "cleanup"
        except DiarizationCancelled:
            if not self._terminal_published:
                self.status.event("diarization_cancelled", **(self.status.get("diarization") or {}))
            raise KeyboardInterrupt from None
        except DiarizationError as exc:
            if self.cancelled.is_set():
                self.status.event("diarization_cancelled", **(self.status.get("diarization") or {}))
                raise KeyboardInterrupt from None
            raise MeetingJobError(exc.code, "會後辨識失敗，請依錯誤代碼檢查本機環境") from None
        data = self.status.get("diarization")
        data.update(actual_speakers=result.actual_speakers,
                    total_seconds=result.duration_seconds, processed_seconds=result.duration_seconds)
        self.status.update(diarization=data)
