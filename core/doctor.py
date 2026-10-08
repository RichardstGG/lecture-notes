"""lec doctor：檢查執行環境，回報問題時請附上輸出。"""
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as C
from . import devices
from . import platform as P
from .servers import LlamaServer, WhisperServer, http_get

OK, WARN, FAIL = "✔", "⚠", "✖"


# llama-server --list-devices 的裝置行長得像「  MTL0: Apple M3 Pro (...)」「Vulkan0: ...」「CUDA0: ...」，
# 各後端的前綴不同（Metal 是 MTL，不是 Metal），所以用「名稱+數字:」判斷，不列舉前綴。
_DEVICE_LINE = re.compile(r"^[A-Za-z][A-Za-z_]*\d+:\s")


def parse_gpu_devices(text):
    return [x.strip() for x in text.splitlines()
            if _DEVICE_LINE.match(x.strip()) and not x.strip().lower().startswith("cpu")]


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
    """這個目錄自己的 HEAD。沒有自己的 .git 就回 None。

    官方預編譯檔放在專案裡時，目錄不是獨立的 checkout。`git -C` 會往上找到
    lecture-notes 這個 repo，那個 commit 不是引擎版本，不能拿來跟 engines.lock 比。
    """
    if not (Path(d) / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def summary_mode_detail(summary_on, model, upstream="local"):
    """「總結」一行的說明文字。"""
    if summary_on and upstream not in (None, "", "local"):
        return f"開啟（外部 API 上游 {upstream}；語音轉錄仍用本機 whisper）"
    if summary_on:
        return f"開啟（模型 {model}）"
    return ("關閉：只轉錄，不會啟動 llama.cpp"
            "（summary.enabled = false；之後可在別台電腦用 lec summarize 補做總結）")


def _engine_setup_hint():
    """告訴使用者下一步該執行的安裝指令。Windows 沒有編譯器時走預編譯檔。"""
    if P.NAME == "windows":
        return "python setup_engines.py（沒有編譯環境時加上 --prebuilt）"
    return "python3 setup_engines.py"


def missing_engine_item(name, key, binary, summary_on, upstream="local"):
    """找不到引擎執行檔時的 (status, name, detail)。

    只轉錄模式（summary.enabled = false）不會啟動 llama-server，
    外部 API 上游也不使用本機 llama.cpp。這兩種情況缺 llama 只算警告。
    whisper.cpp 一律是必要的。
    """
    if key == "LLAMA_REF" and not summary_on:
        return (WARN, name,
                f"找不到 {binary}（只轉錄模式不需要；要總結時執行 setup_engines.py llama）")
    if key == "LLAMA_REF" and upstream not in (None, "", "local"):
        return (WARN, name,
                f"找不到 {binary}（總結使用外部 API 上游 {upstream}，不需要本機 llama.cpp）")
    return (FAIL, name, f"找不到 {binary}（執行 {_engine_setup_hint()}）")


def dual_capture_item(backend=None):
    """「雙來源錄音（會議）」那一行：回傳 (status, name, detail)。

    這是選用功能，所以環境不齊只算警告，不讓 lec doctor 因此回傳失敗。只查詢裝置，
    不會錄任何聲音（要實測請用硬體驗收步驟，見 docs/platform-dual-capture.md）。
    """
    name = "雙來源錄音"
    if P.audio_backend(backend) != "pulse":
        return (WARN, name, "尚未支援：這個平台錄不到輸出裝置（要另外裝虛擬音訊裝置），"
                            "也沒有實機驗證；目前只有 Linux 提供")
    if not shutil.which("pactl"):
        return (WARN, name, f"無法檢查：找不到 pactl。{devices.hint()}")
    try:
        plan = devices.plan_dual(backend=backend)
        server = P.pulse_server()
    except Exception as e:                                   # doctor 不能因為這個檢查而中斷
        return (WARN, name, f"檢查失敗：{e}")
    flavor = {"pipewire": "PipeWire（pulse 相容層）", "pulseaudio": "PulseAudio"}.get(
        (server or {}).get("flavor"), "未知的音訊伺服器")
    if not plan["ok"]:
        return (WARN, name, f"{flavor}｜{plan['errors'][0]['message']}")
    system, mic = plan["sources"]
    detail = (f"{flavor}｜系統輸出 {system['label']}｜麥克風 {mic['label']}"
              f"｜實驗中（尚未完成硬體驗收）；建議戴耳機，沒有回音消除，"
              f"monitor 會收到該輸出裝置的所有聲音")
    if plan["warnings"]:
        return (WARN, name, detail + "｜" + "；".join(w["message"] for w in plan["warnings"]))
    return (OK, name, detail)


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
    upstream = cfg.get("summary.upstream", "local") or "local"
    local_llm = summary_on and upstream == "local"
    add(OK, "總結", summary_mode_detail(summary_on, cfg["summary"]["model"], upstream))
    if summary_on and upstream != "local":
        try:
            specs = C.load_upstreams()
        except C.ConfigError as exc:
            add(FAIL, "摘要上游", str(exc))
        else:
            spec = specs.get(upstream)
            if spec:
                add(OK, "摘要上游", f"{upstream}（{spec['name']}）")
            else:
                add(FAIL, "摘要上游", f"config/upstreams.toml 沒有 {upstream}")

    # ---- 引擎與模型
    lock = read_lock(C.APP_ROOT / "engines.lock")
    w, l = WhisperServer(cfg), LlamaServer(cfg)
    for srv, key, d in ((w, "WHISPER_REF", w.whisper_dir), (l, "LLAMA_REF", l.llama_dir)):
        b = Path(srv.binary())
        if not os.access(b, os.X_OK):
            add(*missing_engine_item(srv.name, key, b, summary_on, upstream))
            continue
        head, want = git_head(d), lock.get(key)
        if want and head and head != want:
            add(WARN, srv.name, f"版本 {head[:9]} 與 engines.lock 的 {want[:9]} 不同（setup_engines.py 會切回）")
        else:
            add(OK, srv.name, f"{b}" + (f"（{head[:9]}）" if head else ""))

    # ---- 編譯工具鏈（判斷與 setup_engines.py 共用 P.missing_build_tools，兩邊不會不一致）
    #      後端優先看 build stamp 記的「當初實際編譯用的後端」，沒有紀錄才用平台預設，
    #      免得用 --backend cuda 編過的機器被提醒缺 Vulkan 的 glslc。
    #      已經編好引擎或改用官方預編譯檔的人不需要編譯環境，所以缺工具只算警告。
    built = P.built_backend(w.whisper_dir) or P.built_backend(l.llama_dir)
    eng_backend = built or P.engine_backend()
    msvc = P.find_msvc() if P.NAME == "windows" else None
    where = f"{eng_backend} 後端（{'已編譯的後端' if built else '本平台預設'}）"
    if msvc:
        where += f"｜Visual Studio {msvc['version']}"
    missing = P.missing_build_tools(eng_backend, msvc=msvc)
    add(OK if not missing else WARN, "編譯工具",
        f"{where}：齊全" if not missing else
        f"{where}：缺少 {'、'.join(missing)}；已編好引擎或用預編譯檔可忽略，"
        "要自己編譯請見 README 的安裝步驟")

    for label, path in (("whisper 模型", Path(cfg.whisper_model_path())),
                        (f"LLM 模型（{cfg['summary']['model']}）", Path(cfg.llm_model()["path"]))):
        if path.is_file():
            add(OK, label, f"{path.name}（{path.stat().st_size / 1e9:.1f} GB）")
        elif label.startswith("whisper") or local_llm:
            add(FAIL, label, f"找不到 {path}")
        elif upstream not in (None, "", "local"):
            add(WARN, label, f"找不到 {path}（外部 API 不需要本機 GGUF）")
        else:
            add(WARN, label, f"找不到 {path}（只轉錄模式不需要）")

    b = Path(l.binary())
    if os.access(b, os.X_OK):
        try:
            r = subprocess.run([str(b), "--list-devices"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                               env=l._env(b))
            devs = parse_gpu_devices(r.stdout + r.stderr)
            add(OK if devs else WARN, "GPU", "；".join(devs) if devs else "沒有偵測到 GPU，會用 CPU（很慢）")
        except (OSError, subprocess.TimeoutExpired) as e:
            add(WARN, "GPU", f"無法執行 --list-devices：{e}")

    # ---- port（只轉錄時不會啟動 llama-server，不用檢查它的 port）
    for srv in ((w, l) if local_llm else (w,)):
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
    add(*dual_capture_item(backend))
    if mic:
        mean, peak = devices.test_volume(source, backend=backend)
        if mean is None:
            add(FAIL, "錄音測試", str(peak))
        else:
            st, msg = devices.judge_volume(mean)
            add(OK if st == "✔" else (WARN if st == "⚠" else FAIL), "錄音測試（3 秒）",
                f"平均 {mean:.1f} dB、峰值 {peak:.1f} dB：{msg}")
    return items
