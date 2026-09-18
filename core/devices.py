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


def test_volume(source, seconds=3, backend=None):
    """錄幾秒，回傳 (mean_db, max_db)；失敗回傳 (None, 錯誤訊息)。"""
    target = P.resolve_source(source, backend)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", *P.ffmpeg_input(target, backend),
           "-t", str(seconds), "-af", "volumedetect", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=seconds + 20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    mean = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
    peak = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
    if not mean:
        return None, (r.stderr.strip().splitlines() or ["ffmpeg 沒有輸出"])[-1][:200]
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
