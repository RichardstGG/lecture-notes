"""錄音來源（PulseAudio / PipeWire）列表與測試。"""
import json
import re
import shutil
import subprocess


def list_sources(include_monitors=False):
    """回傳 [{index, name, description, state}]；沒有 pactl 時回傳 None。"""
    if not shutil.which("pactl"):
        return None
    out = []
    try:
        r = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True,
                           text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip().startswith("["):
            for s in json.loads(r.stdout):
                out.append({"index": s.get("index"), "name": s.get("name", ""),
                            "description": s.get("description", ""),
                            "state": str(s.get("state", "")).lower()})
        else:
            raise ValueError
    except (ValueError, subprocess.TimeoutExpired, OSError):
        out = []
        r = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                out.append({"index": int(parts[0]) if parts[0].isdigit() else parts[0], "name": parts[1],
                            "description": "", "state": parts[-1].lower() if len(parts) >= 5 else ""})
    if not include_monitors:
        out = [s for s in out if not s["name"].endswith(".monitor")]
    return out


def default_source():
    if not shutil.which("pactl"):
        return None
    try:
        r = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def resolve(choice, sources):
    """編號或名稱 → pulse 來源名稱。"""
    for s in sources or []:
        if str(s["index"]) == str(choice) or s["name"] == choice:
            return s["name"]
    return None


def test_volume(source, seconds=3):
    """錄幾秒，回傳 (mean_db, max_db)；失敗回傳 (None, 錯誤訊息)。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-f", "pulse", "-i", source, "-t", str(seconds),
           "-af", "volumedetect", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 15)
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
