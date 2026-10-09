"""一次 lec run / lec summarize 的完整流程（server 生命週期、訊號處理、收尾）。"""
import os
import copy
import hashlib
import json
import math
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import platform as P
from .config import Config, ConfigError, meeting_output_parts
from .servers import LlamaServer, ServerError, WhisperServer
from .status import RunLock, Status, now_iso
from .summarize import Summarizer
from .transcribe import CAPTURE_LOSS_ERROR, Transcriber, probe_capture
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
    if not parent.resolve().is_relative_to(root):
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


class MeetingRunError(Exception):
    """Stable CLI error, safe to display without engine/transcript log contents."""

    def __init__(self, code, message, exit_code=1):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


class MeetingRun(_Base):
    """Single-source meeting ingestion; no summary or automatic diarization."""

    def __init__(self, cfg, input_file=None, on_force=None):
        # The engine still calls course_name for transcript headings. Adapt only
        # this private copy; never load or write a course configuration.
        cfg = Config(copy.deepcopy(cfg.data), cfg.course_file, list(cfg.warnings))
        cfg.data['summary']['enabled'] = False
        cfg.data['audio']['keep_recording'] = True
        cfg.data['course'] = {'name': cfg.meeting_name}
        super().__init__(cfg)
        self.input_file = Path(input_file).expanduser().resolve() if input_file else None
        self.live = self.input_file is None
        self.transcriber = None
        self.stop_requested = threading.Event()
        self._probe = None
        self._on_force = on_force
        self._terminal = threading.RLock()
        self._finished = False
        self._signals = {}

    def _install_signals(self):
        for sig in ([signal.SIGINT] if P.IS_WINDOWS else [signal.SIGINT, signal.SIGTERM]):
            self._signals[sig] = signal.getsignal(sig)
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, frame):
        if self.stop_requested.is_set():
            self._force_exit()
            return
        self.stop_requested.set()
        if self.status:
            self.status.update(stop_reason='user')
            self.status.event('stop_requested')
        if self.stage == 'transcribe' and self.transcriber and self.transcriber.proc is not None:
            self.transcriber.request_stop()

    def _start_stop_watcher(self):
        def watch():
            while not self._watch_stop.wait(0.2):
                try:
                    if (self.dir / STOP_FORCE_FILE).exists():
                        self._force_exit()
                    once = self.dir / STOP_FILE
                    if once.exists():
                        once.unlink(missing_ok=True)
                        self._on_signal(None, None)
                    # A stop during startup must also catch a server whose PID
                    # was published after the stop request. Only owned servers
                    # are terminated by this public engine method.
                    if self.stop_requested.is_set() and self.stage == 'loading' and self.whisper:
                        self.whisper.kill_now()
                    if (self.stop_requested.is_set() and self.stage == 'transcribe'
                            and self.transcriber and self.transcriber.proc is not None):
                        # Wait for the engine's handle before request_stop. In
                        # particular Windows cannot send SIGINT to a new child.
                        self.transcriber.request_stop()
                except OSError:
                    pass
        self._watcher = threading.Thread(target=watch, daemon=True)
        self._watcher.start()

    def _force_exit(self):
        # Match the existing CLI hard-exit contract, but also terminate the
        # transcriber's capture process and emit the one final meeting response.
        with self._terminal:
            if self._finished:
                return
            self._finished = True
            try:
                for proc in (self._probe, getattr(self.transcriber, 'proc', None)):
                    if proc and proc.poll() is None:
                        P.kill_now(proc.pid)
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
                if self.whisper:
                    self.whisper.kill_now()
                self.inhibitor.stop()
                if self.status:
                    self.status.phase('aborted', stop_reason='user')
                    self.status.close()
            finally:
                try:
                    self.lock.release()
                finally:
                    try:
                        log.close()
                        if self._on_force:
                            self._on_force(self._result('aborted'))
                    finally:
                        os._exit(130)

    def _result(self, outcome):
        return {'schema_version': 1, 'accepted': True, 'work_type': 'meeting',
                'operation': 'run', 'session': str(self.dir), 'pid': os.getpid(),
                'outcome': outcome}

    def _check_cancel(self):
        if self.stop_requested.is_set():
            raise KeyboardInterrupt

    def _copy_source(self):
        suffix = self.input_file.suffix.lower()
        dest = self.dir / ('source_audio' + suffix)
        tmp = self.dir / ('.' + dest.name + '.tmp')
        try:
            with self.input_file.open('rb') as source, tmp.open('xb') as target:
                while chunk := source.read(1024 * 1024):
                    self._check_cancel()
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            self._check_cancel()
            os.replace(tmp, dest)
        except OSError:
            raise MeetingRunError('source_copy_failed', '無法保存匯入來源；請檢查磁碟空間與檔案權限') from None
        finally:
            tmp.unlink(missing_ok=True)
        return dest

    def _audio_command(self, command, *, cancellable):
        # Suppress decoder diagnostics: only stable errors reach CLI JSON/stderr.
        self._probe = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       stdin=subprocess.DEVNULL, **P.spawn_kwargs())
        try:
            while True:
                if cancellable:
                    self._check_cancel()
                try:
                    out, _ = self._probe.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if self._probe.returncode:
                raise MeetingRunError('source_invalid', '來源音檔無法完整解碼；不可用於會後辨識')
            return out
        finally:
            if self._probe.poll() is None:
                P.kill_now(self._probe.pid)
            self._probe.communicate()
            self._probe = None

    def _seal_source(self, path, *, capture=None, cancellable=False, failed=False):
        """Validate bytes before publishing the only source manifest."""
        out = self._audio_command(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                                  '-show_entries', 'stream=index:format=duration', '-of', 'json',
                                  str(path)], cancellable=cancellable)
        try:
            probe = json.loads(out)
            duration = float(probe['format']['duration'])
            if not probe['streams'] or not math.isfinite(duration) or duration <= 0:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise MeetingRunError('source_invalid', '來源音檔沒有有效音訊長度') from None
        self._audio_command(['ffmpeg', '-v', 'error', '-nostdin', '-xerror', '-i', str(path),
                             '-map', '0:a:0', '-f', 'null', '-'], cancellable=cancellable)
        digest = hashlib.sha256()
        size = 0
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                if cancellable:
                    self._check_cancel()
                size += len(chunk)
                digest.update(chunk)
        if capture is None:
            capture = probe_capture(path)
        verified = (isinstance(capture, dict) and type(capture.get('lost_ratio')) in (int, float)
                    and math.isfinite(capture['lost_ratio']) and 0 <= capture['lost_ratio'] <= 1)
        complete = bool(verified and capture['lost_ratio'] < CAPTURE_LOSS_ERROR and not failed)
        metadata = {'schema_version': 1, 'kind': 'recording' if self.live else 'import',
                    'original_name': path.name if self.live else self.input_file.name,
                    'path': path.name, 'size_bytes': size, 'sha256': digest.hexdigest(),
                    'duration_seconds': duration, 'created_at': now_iso(),
                    'requested_speakers': self.cfg.get('diarization.num_speakers'),
                    'complete': complete, 'capture': capture}
        write_json(self.dir / 'source.json', metadata)
        self.status.update(capture={'complete': complete, 'report': capture})
        self.status.event('source_saved', path=path.name, complete=complete)
        if not verified:
            raise MeetingRunError('capture_unverified', '無法驗證來源擷取完整性；不可用於會後辨識')
        if capture['lost_ratio'] >= CAPTURE_LOSS_ERROR:
            raise MeetingRunError('capture_loss', '來源遺失至少 2% 音訊；這份錄音不適合做發言者辨識')
        if failed:
            raise MeetingRunError('capture_failed', '音訊擷取未正常完成；已保存的來源不可用於會後辨識')

    def run(self):
        cfg = self.cfg
        if cfg.work_type != 'meeting':
            raise MeetingRunError('input_invalid', '此入口只處理 meeting', 2)
        cfg.diarization_options(require_speakers=True)
        if cfg.capture_plan()['mode'] != 'single':
            raise MeetingRunError('capture_unavailable', capture_capabilities()['dual']['message'])
        if self.input_file and not self.input_file.is_file():
            raise MeetingRunError('source_missing', '找不到匯入音檔', 2)
        if self.input_file and not re.fullmatch(r'\.[a-z0-9]{1,12}', self.input_file.suffix.lower()):
            raise MeetingRunError('input_invalid', '音檔需要一般英數副檔名', 2)
        for tool in ('ffmpeg', 'ffprobe'):
            if not shutil.which(tool):
                raise MeetingRunError('dependency_missing', f'缺少指令：{tool}')
        if self.lock.acquire(work_type='meeting', course=cfg.meeting_name,
                             mode='live' if self.live else 'file'):
            raise MeetingRunError('run_active', '已有工作執行中；請使用 lec status 查看', 3)
        outcome = 'failed'
        try:
            self._install_signals()
            self.dir = make_meeting_session_dir(cfg)
            log.attach(self.dir / 'session.log')
            self.status = Status(self.dir, cfg['system']['status_interval'], work_type='meeting',
                                 course=cfg.meeting_name, mode='live' if self.live else 'file')
            self.status.phase('starting')
            atomic_write(self.dir / 'config.used.toml', cfg.dump())
            self.lock.update(session=str(self.dir))
            self._start_stop_watcher()
            self.status.phase('loading')
            source = None
            if not self.live:
                source = self._copy_source()
                self._seal_source(source, cancellable=True)
            self._check_cancel()
            if cfg['system']['inhibit_sleep']:
                self.inhibitor.start(f'lec meeting：{cfg.meeting_name}', log)
            self.whisper = WhisperServer(cfg)
            self.status.set_server('whisper', 'loading')
            self.whisper.ensure(self.dir / 'whisper-server.log')
            self.status.set_server('whisper', 'ok')
            self._check_cancel()
            self.transcriber = Transcriber(cfg, self.dir, self.whisper.url, source, self.status)
            self.stage = 'transcribe'
            if self.stop_requested.is_set():
                self.transcriber.writer.close()
                raise KeyboardInterrupt
            self.status.phase('recording' if self.live else 'transcribing')
            result = self.transcriber.run()
            self.stage = 'finishing'
            self.status.phase('finishing')
            if self.live:
                recording = self.transcriber.recording_path
                if not recording or not Path(recording).is_file():
                    raise MeetingRunError('source_missing', '錄音沒有留下可驗證來源；不可用於會後辨識')
                source = self.dir / 'source_audio.ogg'
                os.replace(recording, source)
                self._seal_source(source, capture=result.get('capture'), failed=result.get('ffmpeg_failed', False))
            if result.get('aborted') or (not self.live and self.stop_requested.is_set()):
                outcome = 'aborted'
            elif result.get('ffmpeg_failed'):
                raise MeetingRunError('decode_failed', '轉錄時解碼失敗；原始來源已保留')
            elif result.get('gaps'):
                self.status.event('transcription_gaps', gaps=result['gaps'],
                                  lost_seconds=result.get('lost_seconds', 0))
                raise MeetingRunError('asr_failed', '部分音訊未能轉錄；原稿已標示缺口，來源已保留')
            else:
                outcome = 'done'
        except KeyboardInterrupt:
            self.stop_requested.set()
            outcome = 'aborted'
        except MeetingRunError as exc:
            if self.status:
                self.status.error(f'{exc.code}：{exc}')
            raise
        except (ServerError, OSError) as exc:
            if isinstance(exc, ServerError) and self.stop_requested.is_set():
                outcome = 'aborted'
                return self._result(outcome)
            code = 'engine_failed' if isinstance(exc, ServerError) else 'write_failed'
            message = ('Whisper 服務無法啟動；請檢查 session 的 whisper-server.log' if code == 'engine_failed'
                       else '工作檔案操作失敗；請檢查磁碟空間與權限')
            if self.status:
                self.status.error(f'{code}：{message}')
            raise MeetingRunError(code, message) from None
        finally:
            self._stop_watcher()
            if self._watcher:
                self._watcher.join(2)
            try:
                proc = getattr(self.transcriber, 'proc', None)
                if proc and proc.poll() is None:
                    P.kill_now(proc.pid)
                self._cleanup()
            finally:
                with self._terminal:
                    try:
                        if self.status:
                            if self.status.get('phase') != 'finishing':
                                self.status.phase('finishing')
                            self.status.phase(outcome, stop_reason='user' if self.stop_requested.is_set() else None)
                    finally:
                        try:
                            if self.status:
                                self.status.close()
                        finally:
                            self._finished = True
                            try:
                                self.lock.release()
                            finally:
                                log.close()
                                for sig, previous in self._signals.items():
                                    signal.signal(sig, previous)
        return self._result(outcome)


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
