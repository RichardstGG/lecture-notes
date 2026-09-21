"""lec doctor：檢查執行環境，回報問題時請附上輸出。"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as C
from . import devices
from . import platform as P
from .servers import LlamaServer, WhisperServer, http_get

OK, WARN, FAIL = "✔", "⚠", "✖"


def read_lock(path):
    refs = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                refs[k.strip()] = v.strip()
    except OSError:
        pass
    return refs


def git_head(d):
    try:
        r = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def summary_mode_detail(summary_on, model):
    """「總結」一行的說明文字。"""
    if summary_on:
        return f"開啟（模型 {model}）"
    return ("關閉：只轉錄，不會啟動 llama.cpp"
            "（summary.enabled = false；之後可在別台電腦用 lec summarize 補做總結）")


def missing_engine_item(name, key, binary, summary_on):
    """找不到引擎執行檔時的 (status, name, detail)。

    只轉錄模式（summary.enabled = false）不會啟動 llama-server，
    所以缺 llama.cpp 只算警告，不算錯誤；whisper.cpp 一律是必要的。
    """
    if key == "LLAMA_REF" and not summary_on:
        return (WARN, name,
                f"找不到 {binary}（只轉錄模式不需要；要總結時執行 setup_engines.py llama）")
    return (FAIL, name, f"找不到 {binary}（執行 python3 setup_engines.py）")


def run(course=None, sets=(), mic=False):
    items = []

    def add(status, name, detail=""):
        items.append({"status": status, "name": name, "detail": detail})

    # ---- 系統
    add(OK if sys.version_info >= (3, 11) else FAIL, "Python", platform.python_version())
    tested = {"linux": "已實測", "macos": "實驗中", "windows": "實驗中"}[P.NAME]
    add(OK if P.NAME == "linux" else WARN, "作業系統", f"{P.describe()}｜{tested}")
    add(OK, "錄音後端", P.audio_backend())
    tools = [("ffmpeg", True, "錄音與解碼"), ("opencc", False, "台灣繁體用語轉換"),
             ("git", False, "setup_engines.py 取得原始碼"), ("cmake", False, "編譯引擎")]
    if P.NAME == "linux":
        tools += [("pactl", False, "列出麥克風"), ("systemd-inhibit", False, "上課時阻止休眠")]
    elif P.NAME == "macos":
        tools += [("caffeinate", False, "上課時阻止休眠")]
    for cmd, need, why in tools:
        p = shutil.which(cmd)
        add(OK if p else (FAIL if need else WARN), cmd, p or f"找不到（{why}）")

    # ---- 設定
    try:
        cfg, _ = C.load(course, sets=sets)
    except C.ConfigError as e:
        add(FAIL, "設定", str(e))
        return items
    src = f"default.toml{' + local.toml' if C.LOCAL_FILE.exists() else ''}"
    if cfg.course_file:
        src += f" + {cfg.course_file.name}"
    add(OK if not cfg.warnings else WARN, "設定", src + ("；" + "；".join(cfg.warnings) if cfg.warnings else ""))

    # ---- 總結模式
    summary_on = bool(cfg["summary"]["enabled"])
    add(OK, "總結", summary_mode_detail(summary_on, cfg["summary"]["model"]))

    # ---- 引擎與模型
    lock = read_lock(C.APP_ROOT / "engines.lock")
    w, l = WhisperServer(cfg), LlamaServer(cfg)
    for srv, key, d in ((w, "WHISPER_REF", w.whisper_dir), (l, "LLAMA_REF", l.llama_dir)):
        b = Path(srv.binary())
        if not os.access(b, os.X_OK):
            add(*missing_engine_item(srv.name, key, b, summary_on))
            continue
        head, want = git_head(d), lock.get(key)
        if want and head and head != want:
            add(WARN, srv.name, f"版本 {head[:9]} 與 engines.lock 的 {want[:9]} 不同（setup_engines.py 會切回）")
        else:
            add(OK, srv.name, f"{b}" + (f"（{head[:9]}）" if head else ""))
    for label, path in (("whisper 模型", Path(cfg.whisper_model_path())),
                        (f"LLM 模型（{cfg['summary']['model']}）", Path(cfg.llm_model()["path"]))):
        if path.is_file():
            add(OK, label, f"{path.name}（{path.stat().st_size / 1e9:.1f} GB）")
        elif label.startswith("whisper") or summary_on:
            add(FAIL, label, f"找不到 {path}")
        else:
            add(WARN, label, f"找不到 {path}（只轉錄模式不需要）")

    b = Path(l.binary())
    if os.access(b, os.X_OK):
        try:
            r = subprocess.run([str(b), "--list-devices"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                               env=l._env(b))
            devs = [x.strip() for x in (r.stdout + r.stderr).splitlines()
                    if x.strip().lower().startswith(("vulkan", "cuda", "metal"))]
            add(OK if devs else WARN, "GPU", "；".join(devs) if devs else "沒有偵測到 GPU，會用 CPU（很慢）")
        except (OSError, subprocess.TimeoutExpired) as e:
            add(WARN, "GPU", f"無法執行 --list-devices：{e}")

    # ---- port（只轉錄時不會啟動 llama-server，不用檢查它的 port）
    for srv in ((w, l) if summary_on else (w,)):
        st, _ = http_get(srv.url + srv.health_path, timeout=2)
        add(OK if st is None else WARN, f"port {srv.port}",
            "空閒" if st is None else f"已有程式在使用（可能是先前的 {srv.name}；lec 會嘗試沿用）")

    # ---- 資料夾
    for label, p in (("輸出資料夾", cfg.path(cfg["paths"]["output_root"])),
                     ("狀態資料夾", cfg.state_dir())):
        try:
            p.mkdir(parents=True, exist_ok=True)
            ok = os.access(p, os.W_OK)
        except OSError:
            ok = False
        add(OK if ok else FAIL, label, str(p) + ("" if ok else "（無法寫入）"))

    # ---- 麥克風
    source = cfg.audio_source()
    backend = cfg["audio"].get("backend")
    sources = devices.list_sources(backend=backend)
    if sources is None:
        add(WARN, "麥克風", f"{source}（無法列出裝置：{devices.hint()}）")
    elif source not in ("default", "") and source not in [s["id"] for s in sources] \
            and source not in [s["name"] for s in sources]:
        add(FAIL, "麥克風", f"設定的來源 {source} 不存在；用 lec devices 查詢並 --save")
    else:
        shown = source if source not in ("default", "") else \
            f"default → {devices.default_source(backend) or '?'}"
        add(OK, "麥克風", shown)
    if mic:
        mean, peak = devices.test_volume(source, backend=backend)
        if mean is None:
            add(FAIL, "錄音測試", str(peak))
        else:
            st, msg = devices.judge_volume(mean)
            add(OK if st == "✔" else (WARN if st == "⚠" else FAIL), "錄音測試（3 秒）",
                f"平均 {mean:.1f} dB、峰值 {peak:.1f} dB：{msg}")
    return items
