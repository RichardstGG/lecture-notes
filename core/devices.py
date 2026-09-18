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
    """編號 / 名稱 / id → 要存進設定的 id；找不到回傳 None。"""
    rows = sources if sources is not None else (P.list_sources(backend) or [])
    for i, s in enumerate(rows):
        if choice in (s["id"], s["name"]) or str(choice) == str(s.get("index", i)):
            return s["id"]
    return None


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
        ("Access is denied", "could not open", "I/O error"),
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
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", *P.ffmpeg_input(target, backend),
           "-t", str(seconds), "-af", "volumedetect", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=seconds + 20)
    except subprocess.TimeoutExpired as e:
        if backend == "avfoundation":
            hint = _PERMISSION_HINTS["avfoundation"][1]
            return None, f"錄音逾時，若麥克風硬體正常，很可能是權限問題：{hint}"
        return None, str(e)
    except OSError as e:
        return None, str(e)
    mean = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
    peak = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
    if not mean:
        last = (r.stderr.strip().splitlines() or ["ffmpeg 沒有輸出"])[-1][:200]
        hint = _permission_hint(backend, r.stderr)
        return None, f"{last}（{hint}）" if hint else last
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
