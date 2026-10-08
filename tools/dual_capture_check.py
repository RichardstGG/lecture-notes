#!/usr/bin/env python3
"""雙來源錄音的獨立驗收工具（不經過 lec CLI，不載入 whisper／LLM）。

用來做硬體驗收：直接呼叫 core/capture.py 錄一段、檢查分軌、量兩軌的對齊與殘餘漂移。
**錄音時會真的收錄你選的輸出裝置與麥克風**，所以預設會先列出要錄的裝置並等你按 Enter。

    # 列出會錄哪兩個來源（不錄音）
    python3 tools/dual_capture_check.py --plan

    # 錄 5 分鐘到 /tmp/dual_check（預設輸出的 monitor + 預設麥克風）
    python3 tools/dual_capture_check.py --minutes 5 --out /tmp/dual_check

    # 指定裝置；--yes 略過確認
    python3 tools/dual_capture_check.py --minutes 5 --out /tmp/x --system-output <sink> --mic <source> --yes

    # 事後檢查一個輸出資料夾（完整性、缺口、故障、音量、兩軌互相關）
    python3 tools/dual_capture_check.py --analyze /tmp/dual_check

    # 漂移驗收：喇叭播放每分鐘一次的點擊音，錄 60–120 分鐘後量兩軌點擊時間差的趨勢
    python3 tools/dual_capture_check.py --analyze /tmp/dual_check --clicks

錄音中按 Ctrl+C = 正常停止（跟 lec 一樣：分軌封裝完成才結束）；再按一次 = 強制停止。
只用標準函式庫與 ffmpeg。
"""
import argparse
import array
import json
import math
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import capture as C            # noqa: E402
from core import devices                 # noqa: E402
from core import platform as P           # noqa: E402


def show_plan(plan):
    for w in plan["warnings"]:
        print(f"⚠ {w['message']}")
    for e in plan["errors"]:
        print(f"✖ [{e['code']}] {e['message']}")
    if plan["ok"]:
        for s in plan["sources"]:
            print(f"  {s['role']:6s} ← {s['label']}\n         {s['device']}")


def record(args):
    plan = devices.plan_dual(args.system_output, args.mic)
    show_plan(plan)
    if not plan["ok"]:
        return 2
    if args.plan:
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if not args.yes:
        try:
            input(f"\n將錄音 {args.minutes:g} 分鐘（Ctrl+C 提前結束），輸出到 {out}。按 Enter 開始，Ctrl+C 取消…")
        except (KeyboardInterrupt, EOFError):
            print("\n已取消，沒有錄音。")
            return 130
    cap = C.MultiCapture([C.SourceSpec(**s) for s in plan["sources"]], out,
                         mix_path=out / "mix.ogg",
                         on_event=lambda kind, **kv: print(f"  事件 {kind}: " + json.dumps(
                             {k: v for k, v in kv.items() if k != "sources"}, ensure_ascii=False)))
    try:
        cap.start()
    except C.CaptureStartError as e:
        print(f"✖ 無法開始（{e.code}）：{e}")
        return 1
    deadline = time.monotonic() + args.minutes * 60
    presses = 0
    last_print = 0.0
    while True:
        try:
            while True:
                if cap.read(timeout=0.5) == b"":
                    break
                if time.monotonic() >= deadline and not presses:
                    presses = 1
                    cap.request_stop()
                if time.monotonic() - last_print > 10:
                    last_print = time.monotonic()
                    snap = cap.snapshot()
                    print("  " + "｜".join(
                        f"{s['role']}:{s['state']} 峰值 {s['recent_peak_dbfs']} dBFS"
                        f"{'（數位靜音）' if s['digital_silence_now'] else ''}"
                        for s in snap["sources"]))
            break
        except KeyboardInterrupt:
            presses += 1
            if presses == 1:
                print("\n▶ 正常停止中…（再按一次強制停止）")
                cap.request_stop()
            else:
                print("\n✖ 強制停止")
                cap.kill()
    print()
    return analyze(out, clicks=False)


def decode(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                          "-ar", str(C.SR), "-"], capture_output=True).stdout
    arr = array.array("h")
    arr.frombytes(raw)
    return arr


def click_times(arr, factor=6.0, hold=0.4):
    """找出明顯比背景大的短促點擊：門檻 = 整段 RMS 的 factor 倍。回傳秒數。
    逐 5 ms 區塊取極值（不能固定間隔抽樣：會剛好跟訊號週期同相而漏掉）。"""
    if not arr:
        return []
    rms = math.sqrt(sum(v * v for v in arr[::7]) / max(1, len(arr[::7]))) or 1.0
    thr = max(1500, rms * factor)
    block = C.SR // 200
    out, skip_until = [], -1
    for i in range(0, len(arr) - block, block):
        if i < skip_until:
            continue
        seg = arr[i:i + block]
        if max(max(seg), -min(seg)) > thr:
            first = next(j for j, v in enumerate(seg) if abs(v) > thr)      # 樣本等級的起點
            out.append((i + first) / C.SR)
            skip_until = i + int(hold * C.SR)
    return out


def slope_ppm(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return 0.0 if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 1e6


def pair_clicks(a, b, window=0.3):
    pairs, j = [], 0
    for t in a:
        while j < len(b) and b[j] < t - window:
            j += 1
        if j < len(b) and abs(b[j] - t) <= window:
            pairs.append((t, b[j] - t))
    return pairs


def analyze(out, clicks):
    out = Path(out)
    v = C.verify_capture(out)
    print(f"完整性：{v['state']}")
    for p in v["problems"]:
        print(f"  ✖ {p}")
    meta = v.get("meta")
    if not meta:
        return 1
    print(f"狀態：{meta['status']}｜降級：{meta['degraded']}｜時間基準原點：{meta['timebase']['origin_wall']}")
    for s in meta["sources"]:
        d = s["drift"]
        print(f"\n[{s['role']}] {s['label']}\n  裝置 {s['device']}\n"
              f"  起點偏移 {s['start_offset_ms']} ms｜長度 {s['duration_seconds']} 秒｜"
              f"有效至 {s['valid_until_seconds']} 秒\n"
              f"  峰值 {s['levels']['peak_dbfs']} dBFS｜數位靜音合計 {s['levels']['digital_silence_seconds']} 秒"
              f"（最長連續 {s['levels']['longest_digital_silence_seconds']} 秒）\n"
              f"  缺口 {len(s['gaps'])} 個共 {s['gap_seconds']} 秒｜時脈誤差 {d['clock_error_ppm']} ppm"
              f"（補 {d['slip_inserted']}／丟 {d['slip_dropped']} 樣本）｜"
              f"封裝 {'完整' if s.get('complete') else '不完整：' + str(s.get('problem'))}")
        for g in s["gaps"]:
            print(f"    缺口 @ {g['at_seconds']} 秒，長 {g['seconds']} 秒")
        if s["fault"]:
            f = s["fault"]
            print(f"  ✖ 故障 {f['code']} @ {f['at_seconds']} 秒（{f['detected_at_seconds']} 秒才發現）：{f['detail']}")
    for w in meta.get("warnings", []):
        print(f"⚠ {w['code']} @ {w.get('at_seconds')} 秒：{w['detail']}")
    if clicks:
        tracks = {s["role"]: out / s["track"] for s in meta["sources"] if s.get("track")}
        a, b = (click_times(decode(tracks[r])) for r in ("system", "mic"))
        pairs = pair_clicks(a, b)
        print(f"\n點擊對照：system {len(a)} 個、mic {len(b)} 個、配成對 {len(pairs)} 個")
        if len(pairs) < 3:
            print("  配對太少，無法判斷。喇叭要夠大聲、麥克風要收得到，且播放的是每分鐘一次的點擊音。")
            return 1
        delays = [d * 1000 for _, d in pairs]
        n = max(1, len(pairs) // 10)
        first, last = sorted(delays[:n])[n // 2], sorted(delays[-n:])[n // 2]
        ppm = slope_ppm([t for t, _ in pairs], [d for _, d in pairs])
        print(f"  mic 相對 system 的延遲：開頭 {first:+.1f} ms → 結尾 {last:+.1f} ms"
              f"（變化 {last - first:+.1f} ms），趨勢 {ppm:+.1f} ppm")
        print("  延遲本身包含喇叭到麥克風的聲音傳播與裝置延遲，固定的部分不用管；"
              "要看的是「變化」，也就是殘餘漂移。")
    return 0 if v["state"] in ("complete", "degraded") else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true", help="只列出會錄的來源，不錄音")
    ap.add_argument("--analyze", metavar="DIR", help="檢查一個已錄好的資料夾")
    ap.add_argument("--clicks", action="store_true", help="--analyze 時另外量兩軌點擊的時間差趨勢")
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--out", default="dual_check")
    ap.add_argument("--system-output", default="default", help="輸出裝置（sink 名稱或編號）")
    ap.add_argument("--mic", default="default")
    ap.add_argument("--yes", action="store_true", help="不要等 Enter 確認")
    args = ap.parse_args()
    P.force_utf8()
    if args.analyze:
        return analyze(args.analyze, args.clicks)
    return record(args)


if __name__ == "__main__":
    sys.exit(main())
