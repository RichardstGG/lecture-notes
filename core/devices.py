"""錄音來源列表與音量測試（平台差異在 core/platform.py）。"""
import re
import subprocess

from . import platform as P


def list_sources(include_monitors=False, backend=None):
    """回傳 [{id, name, description, state}]；無法列出時回傳 None。"""
    return P.list_sources(backend, include_monitors=include_monitors)


def default_source(backend=None):
    return P.default_source(backend)


def resolve(choice, sources=None, backend=None):
    """編號 / 名稱 / id → 要存進設定的 id；找不到回傳 None。

    跟執行期用的 `platform.resolve_source()` 刻意不同：這個是互動查詢用的
    （`lec devices --test/--save`），找不到就回 None 讓呼叫端當場報錯；
    `resolve_source()` 找不到會把原字串直接交給 ffmpeg，因為設定檔裡可能寫著
    現在還沒插上的裝置，該不該失敗交給 ffmpeg 決定。
    """
    rows = sources if sources is not None else (P.list_sources(backend) or [])
    for i, s in enumerate(rows):
        if choice in (s["id"], s["name"]) or str(choice) == str(s.get("index", i)):
            return s["id"]
    return None


# ---------------------------------------------------------------- 雙來源（會議）：輸出裝置與規劃
def list_outputs(backend=None):
    """回傳 [{id, name, description, state, port}]；無法列出（非 Linux／沒有 pactl）時回傳 None。"""
    return P.list_sinks(backend)


def default_output(backend=None):
    return P.default_sink(backend)


def resolve_output(choice, outputs=None, backend=None):
    """編號 / 名稱 / id → 輸出裝置的 id；找不到回傳 None（跟 resolve() 同一個精神：互動查詢要當場報錯）。"""
    rows = outputs if outputs is not None else (P.list_sinks(backend) or [])
    for i, s in enumerate(rows):
        if choice in (s["id"], s["name"]) or str(choice) == str(i):
            return s["id"]
    return None


def _problem(code, message):
    return {"code": code, "message": message}


def plan_dual(system_output="default", mic="default", backend=None):
    """把「要收錄哪個輸出裝置、哪支麥克風」解析成實際裝置，並預檢。

    回傳 {ok, sources: [SourceSpec 欄位 dict], errors, warnings}：
      - 「default」在這裡就被解析成當下的實際裝置並固定下來；預設裝置之後改變，錄音
        不會跟著換（換了會把別的裝置的聲音當成這一路，見 core/capture.py）。
      - ok 為 False 時 sources 為空，errors 的 code 是穩定分類（不要 parse message）：
        unsupported_platform / no_audio_tools / output_not_found / mic_not_found /
        no_default_output / no_default_mic / mic_is_monitor / same_device
      - warnings 不擋錄音，只是提醒：output_may_echo（輸出像是喇叭，沒有回音消除）、
        no_playback_on_output（別的輸出裝置正在播放聲音，這個卻沒有）。
    """
    backend = P.audio_backend(backend)
    errors, warnings = [], []
    if backend != "pulse":
        errors.append(_problem(
            "unsupported_platform",
            f"雙來源錄音目前只支援 Linux（PulseAudio／PipeWire）。這台是 {P.NAME}：沒有可以直接"
            f"錄輸出裝置的方式（要另外裝虛擬音訊裝置），也沒有實機驗證"))
        return {"ok": False, "sources": [], "errors": errors, "warnings": warnings}
    outputs, inputs = P.list_sinks(backend), P.list_sources(backend)
    if outputs is None or inputs is None:
        errors.append(_problem("no_audio_tools", f"無法列出音訊裝置。{hint()}"))
        return {"ok": False, "sources": [], "errors": errors, "warnings": warnings}

    sink = None
    if system_output in ("", "default", None):
        sink = P.default_sink(backend)
        if not sink:
            errors.append(_problem("no_default_output", "找不到系統預設輸出裝置，請明確指定"))
    else:
        sink = resolve_output(system_output, outputs)
        if sink is None:
            errors.append(_problem("output_not_found",
                                   f"找不到輸出裝置 {system_output}（用 devices.list_outputs() 查詢）"))
    mic_id = None
    if mic in ("", "default", None):
        mic_id = P.default_source(backend)
        if not mic_id:
            errors.append(_problem("no_default_mic", "找不到系統預設麥克風，請明確指定"))
    else:
        mic_id = resolve(mic, inputs, backend)
        if mic_id is None:
            errors.append(_problem("mic_not_found", f"找不到麥克風 {mic}（用 lec devices 查詢）"))
    if mic_id and P.is_monitor_source(mic_id):
        errors.append(_problem(
            "mic_is_monitor",
            f"{mic_id} 是輸出裝置的 monitor，不是麥克風；系統輸出那一路才用 monitor"))
    if sink and mic_id and P.monitor_of(sink) == mic_id:
        errors.append(_problem("same_device", "兩路不能是同一個來源"))
    if errors:
        return {"ok": False, "sources": [], "errors": errors, "warnings": warnings}

    by_id = {s["id"]: s for s in outputs}
    desc = by_id.get(sink, {}).get("description") or sink
    mic_desc = next((s["description"] for s in inputs if s["id"] == mic_id), "") or mic_id
    if "speaker" in by_id.get(sink, {}).get("port", "").lower():
        warnings.append(_problem(
            "output_may_echo",
            f"輸出「{desc}」目前像是喇叭：麥克風會收到遠端的聲音，混音裡遠端會出現兩次"
            f"（沒有回音消除）。建議戴耳機"))
    activity = P.sink_activity(backend)
    if activity is not None and activity.get(sink, 0) == 0 and any(activity.values()):
        busy = [by_id[k]["description"] or k for k, v in activity.items() if v and k in by_id]
        warnings.append(_problem(
            "no_playback_on_output",
            f"目前有聲音從「{'、'.join(busy)}」播出，但「{desc}」沒有：會議的聲音可能不是從"
            f"你選的輸出裝置播放，system 軌會是靜音"))
    return {"ok": True,
            "sources": [{"role": "system", "device": P.monitor_of(sink),
                         "label": f"{desc}（輸出）", "kind": "monitor"},
                        {"role": "mic", "device": mic_id, "label": mic_desc, "kind": "input"}],
            "errors": [], "warnings": warnings,
            "resolved": {"system_output": sink, "mic": mic_id}}


def hint():
    """列不出裝置時給使用者的建議。"""
    return {"pulse": "請安裝 pactl（Debian/Ubuntu：sudo apt install pulseaudio-utils）",
            "avfoundation": "請確認已安裝 ffmpeg（brew install ffmpeg），並在系統設定允許終端機使用麥克風",
            "dshow": "請確認已安裝 ffmpeg 並在 PATH 中"}.get(P.audio_backend(), "")


#  macOS／Windows 缺乏實機驗證，這裡只能依常見的 ffmpeg 錯誤字樣做啟發式判斷
#（heuristic，非窮舉；實際文字可能因 ffmpeg 版本而不同）。
_PERMISSION_HINTS = {
    "avfoundation": (
        ("not authoriz", "Operation not permitted", "Input/output error"),
        "麥克風權限被拒絕：請至「系統設定 > 隱私權與安全性 > 麥克風」允許執行 lec 的終端機／Python",
    ),
    "dshow": (
        # 不要放 "I/O error"：裝置名稱錯誤、裝置不存在也會出現，實測曾把這種情況誤報成權限問題
        ("Access is denied",),
        "無法開啟麥克風：請至「設定 > 隱私權 > 麥克風」允許桌面應用程式使用麥克風，並確認裝置未被其他程式獨佔",
    ),
}


def _permission_hint(backend, text):
    entry = _PERMISSION_HINTS.get(backend)
    if not entry:
        return None
    needles, hint = entry
    low = text.lower()
    return hint if any(n.lower() in low for n in needles) else None


def test_volume(source, seconds=3, backend=None):
    """錄幾秒，回傳 (mean_db, max_db)；失敗回傳 (None, 錯誤訊息)。"""
    backend = P.audio_backend(backend)
    target = P.resolve_source(source, backend)
    if target is None:   # 列不到任何裝置時 default 會解析成 None；不要把 "audio=None" 交給 ffmpeg
        return None, f"找不到任何錄音裝置（lec devices 沒有列出麥克風）。{hint()}"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", *P.ffmpeg_input(target, backend),
           "-t", str(seconds), "-af", "volumedetect", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=seconds + 20)
    except subprocess.TimeoutExpired as e:
        if backend == "avfoundation":
            perm = _PERMISSION_HINTS["avfoundation"][1]
            return None, f"錄音逾時，若麥克風硬體正常，很可能是權限問題：{perm}"
        return None, str(e)
    except OSError as e:
        return None, str(e)
    mean = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
    peak = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
    if not mean:
        last = (r.stderr.strip().splitlines() or ["ffmpeg 沒有輸出"])[-1][:200]
        perm = _permission_hint(backend, r.stderr)
        return None, f"{last}（{perm}）" if perm else last
    f = lambda m: float("-inf") if m.group(1) == "-inf" else float(m.group(1))
    return f(mean), f(peak) if peak else None


def judge_volume(mean_db):
    if mean_db is None or mean_db == float("-inf") or mean_db < -60:
        return "✖", "幾乎沒有聲音（來源錯誤或被靜音）"
    if mean_db < -45:
        return "⚠", "音量很小，whisper 可能辨識不佳；可調高麥克風增益或靠近講者"
    if mean_db > -10:
        return "⚠", "音量過大，可能破音"
    return "✔", "音量正常"
