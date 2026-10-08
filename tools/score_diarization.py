#!/usr/bin/env python3
"""會議發言者辨識的評分器：對照人工標註算 DER／JER／代號一致性。

用法：
  python3 tools/score_diarization.py --ref meeting_labels.txt --hyp <speakers.json | session 資料夾 | .rttm>

  --ref     人工標註：Audacity 匯出的 label（start<TAB>end<TAB>P1）或 RTTM
  --hyp     辨識結果：diarize_session 的 speakers.json、含 diarization.current.json 的
            session 資料夾、RTTM、或同樣格式的 label 檔
  --region  只在這些時間範圍計分，可重複，例如 --region 0:300 --region 600:720
            （只標了一部分音訊時必須用，否則沒標到的地方會被算成漏報）
  --collar  單側寬度，預設 0.25 秒 = NIST md-eval 的 `-c 0.25`。
            注意 pyannote.metrics 的 collar 是「總寬度」，要比對它的數字請用 2 倍的值。
  --json-out 把所有數字寫成 JSON

重要慣例（與別的工具比較數字前請先確認）：
  * 預設計分範圍 = 參考標註的範圍（第一個到最後一個標註）；系統輸出在範圍外一律忽略。
  * collar 只繞著「參考」的發言邊界；標成 UNK 的區間整段不計分。
  * 系統輸出的 S00（無法可靠指派）視為「沒有輸出」，所以會算成漏報，並另外列出秒數。
  * 發言者對應用最大化重疊時間的匈牙利演算法（與 md-eval 相同）；JER 另有自己的對應。
  * JER 只平均「在計分範圍內有說話」的參考發言者（沒說話的人 Jaccard 是 0/0，沒有定義）。
  * 這支工具不訂任何「通過／不通過」的門檻：沒有資料能支持任何數字，要由維護者決定。

只用標準函式庫。
"""
from __future__ import annotations

import argparse
import bisect
import itertools
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

EPS = 1e-9
UNKNOWN_LABEL = "UNK"
UNASSIGNED = "S00"
BANDS = [(0.0, 1.0), (1.0, 2.0), (2.0, 5.0), (5.0, float("inf"))]
MINOR_SHARE = 0.02          # 與契約的 UI 收折規則一致：低於總語音 2% 或少於 30 秒
MINOR_SECONDS = 30.0
SPLIT_SHARE = 0.10          # 一個發言者有 >=10% 的語音落在某群，就算「用到了這一群」
MIN_RELIABLE_SECONDS = 30.0


class ScoreInputError(Exception):
    """輸入檔有問題（格式、時間、標籤），訊息會直接顯示給使用者。"""


# ---------------------------------------------------------------- 讀檔
def _t(x):
    return round(float(x), 6)


def _num(text, where):
    s = text.strip()
    if re.fullmatch(r"-?\d+,\d+", s):        # 有些 locale 會匯出逗號小數點
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        raise ScoreInputError(f"{where}：「{text}」不是數字") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise ScoreInputError(f"{where}：「{text}」不是有限的數字")
    return _t(v)


def read_labels(path):
    """Audacity label：start<TAB>end<TAB>label。回傳 (turns, warnings)。"""
    turns, warns = [], []
    for ln, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("\\"):             # Audacity 的頻率範圍行，不是發言
            continue
        parts = raw.split("\t", 2) if "\t" in raw else line.split(None, 2)
        if len(parts) < 3 or not parts[2].strip():
            raise ScoreInputError(f"{path}:{ln}：需要「開始 結束 標籤」三欄，收到：{line[:60]!r}")
        where = f"{path}:{ln}"
        a, b = _num(parts[0], where), _num(parts[1], where)
        if b < a:
            raise ScoreInputError(f"{where}：結束時間 {b} 早於開始時間 {a}")
        if b - a < EPS:
            warns.append(f"{where}：長度為 0 的標籤（點標籤）已略過")
            continue
        turns.append((a, b, parts[2].strip()))
    return turns, warns


def read_rttm(path):
    turns = []
    for ln, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        f = raw.split()
        if not f or f[0] != "SPEAKER":
            continue
        if len(f) < 8:
            raise ScoreInputError(f"{path}:{ln}：RTTM 的 SPEAKER 行欄位不足")
        where = f"{path}:{ln}"
        a, d = _num(f[3], where), _num(f[4], where)
        if d < 0:
            raise ScoreInputError(f"{where}：長度為負數")
        if d >= EPS:
            turns.append((a, _t(a + d), f[7]))
    return turns, []


def read_speakers_json(path):
    """diarize_session 的 speakers.json（schema 1）。回傳 (turns, warnings, duration)。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as e:
        raise ScoreInputError(f"{path}：不是合法的 JSON（{e}）") from None
    if not isinstance(d, dict) or "segments" not in d:
        raise ScoreInputError(f"{path}：沒有 segments 欄位，不像 speakers.json")
    if d.get("schema_version") != 1:
        raise ScoreInputError(f"{path}：不支援的 schema_version {d.get('schema_version')!r}（只認 1）")
    turns = []
    for i, s in enumerate(d["segments"]):
        try:
            a, b, spk = s["start_ms"] / 1000.0, s["end_ms"] / 1000.0, s["speaker_id"]
        except (KeyError, TypeError):
            raise ScoreInputError(f"{path}：segments[{i}] 缺少 start_ms／end_ms／speaker_id") from None
        if not isinstance(spk, str) or not spk:
            raise ScoreInputError(f"{path}：segments[{i}] 的 speaker_id 不是字串")
        if b <= a:
            raise ScoreInputError(f"{path}：segments[{i}] 的 end_ms 不大於 start_ms")
        turns.append((_t(a), _t(b), spk))
    return turns, [], d.get("duration_seconds")


def resolve_hyp_path(path):
    p = Path(path)
    if p.is_dir():
        man = p / "diarization.current.json"
        if not man.is_file():
            raise ScoreInputError(f"{p} 裡沒有 diarization.current.json（還沒辨識過，或不是 session 資料夾）")
        try:
            rel = json.loads(man.read_text(encoding="utf-8"))["speakers"]
        except (ValueError, KeyError) as e:
            raise ScoreInputError(f"{man} 讀不懂：{e}") from None
        target = (p / rel).resolve()
        if p.resolve() not in target.parents:
            raise ScoreInputError(f"{man} 指向 session 資料夾之外的路徑：{rel}")
        return target
    return p


def load_turns(path, fmt="auto"):
    """回傳 (turns, warnings, duration_or_None)。"""
    path = resolve_hyp_path(path) if Path(path).is_dir() else Path(path)
    if not path.is_file():
        raise ScoreInputError(f"找不到檔案：{path}")
    if fmt == "auto":
        suffix = path.suffix.lower()
        fmt = "rttm" if suffix == ".rttm" else ("json" if suffix == ".json" else "labels")
    if fmt == "json":
        return read_speakers_json(path)
    if fmt == "rttm":
        t, w = read_rttm(path)
        return t, w, None
    t, w = read_labels(path)
    return t, w, None


# ---------------------------------------------------------------- 區間運算
def union(intervals):
    out = []
    for a, b in sorted(intervals):
        if b - a < EPS:
            continue
        if out and a <= out[-1][1] + EPS:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def subtract(base, cuts):
    cuts = union(cuts)
    out = []
    for a, b in base:
        cur = a
        for c, d in cuts:
            if d <= cur + EPS:
                continue
            if c >= b - EPS:
                break
            if c > cur + EPS:
                out.append((cur, c))
            cur = max(cur, d)
        if b - cur > EPS:
            out.append((cur, b))
    return out


def intersect(xs, ys):
    xs, ys, out, i, j = union(xs), union(ys), [], 0, 0
    while i < len(xs) and j < len(ys):
        a, b = max(xs[i][0], ys[j][0]), min(xs[i][1], ys[j][1])
        if b - a > EPS:
            out.append((a, b))
        if xs[i][1] < ys[j][1]:
            i += 1
        else:
            j += 1
    return out


def merge_same_speaker(turns):
    """同一個人彼此重疊或相接的標註併成一段，避免在連續說話中間憑空多出一個邊界。"""
    by = defaultdict(list)
    for a, b, s in turns:
        by[s].append((a, b))
    return sorted((a, b, s) for s, iv in by.items() for a, b in union(iv))


# ---------------------------------------------------------------- 最佳對應
def assign_max(matrix):
    """矩陣（列×欄）上最大化總和的一對一指派。回傳 {列: 欄}，只含實際配對（值可為 0）。

    Kuhn–Munkres（e-maxx 版本），長方形用 0 補成方陣。
    """
    n = len(matrix)
    m = len(matrix[0]) if n else 0
    if n == 0 or m == 0:
        return {}
    size = max(n, m)
    big = max((v for row in matrix for v in row), default=0.0)
    cost = [[(big - matrix[i][j]) if i < n and j < m else big for j in range(size)]
            for i in range(size)]
    INF = float("inf")
    u, v, p, way = [0.0] * (size + 1), [0.0] * (size + 1), [0] * (size + 1), [0] * (size + 1)
    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, size + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    return {p[j] - 1: j - 1 for j in range(1, size + 1) if p[j] - 1 < n and j - 1 < m}


def brute_force_max(matrix):
    """測試用的對照解：窮舉所有一對一指派。只適合很小的矩陣。"""
    n = len(matrix)
    m = len(matrix[0]) if n else 0
    if n == 0 or m == 0:
        return 0.0
    best = 0.0
    k = min(n, m)
    for rows in itertools.combinations(range(n), k):
        for cols in itertools.permutations(range(m), k):
            best = max(best, sum(matrix[r][c] for r, c in zip(rows, cols)))
    return best


# ---------------------------------------------------------------- 計分區間
class Scoring:
    """把參考與系統輸出切成「兩邊的發言者集合都不變」的基本區間。

    每個基本區間：(開始, 結束, 參考集合, 系統集合, 區段編號)。只保留在計分範圍內的部分。
    """

    def __init__(self, ref, hyp, regions, collar, unk, skip_overlap=False):
        ref = merge_same_speaker(ref)
        # 計分範圍 = 區段 − UNK − 參考邊界附近的 collar
        zones = []
        if collar > 0:
            for a, b, _ in ref:
                zones += [(a - collar, a + collar), (b - collar, b + collar)]
        scored = subtract(union(regions), union(list(unk) + zones))
        self.regions = union(regions)
        self.scored = scored
        self.excluded_unk = sum(b - a for a, b in intersect(union(regions), union(unk)))

        bounds = {x for iv in scored for x in iv}
        events = []
        for a, b, s in ref:
            events += [(a, 0, s, +1), (b, 0, s, -1)]
            bounds.update((a, b))
        for a, b, s in hyp:
            events += [(a, 1, s, +1), (b, 1, s, -1)]
            bounds.update((a, b))
        events.sort(key=lambda e: e[0])
        pts = sorted(bounds)
        starts = [a for a, _ in scored]
        active = ({}, {})
        self.items = []
        k = 0
        for i in range(len(pts) - 1):
            t = pts[i]
            while k < len(events) and events[k][0] <= t + EPS:
                _, w, s, d = events[k]
                active[w][s] = active[w].get(s, 0) + d
                k += 1
            a, b = t, pts[i + 1]
            if b - a < EPS:
                continue
            j = bisect.bisect_right(starts, (a + b) / 2) - 1
            if j < 0 or not (scored[j][0] - EPS <= (a + b) / 2 <= scored[j][1] + EPS):
                continue
            r = tuple(sorted(s for s, c in active[0].items() if c > 0))
            h = tuple(sorted(s for s, c in active[1].items() if c > 0 and s != UNASSIGNED))
            if skip_overlap and len(r) > 1:
                continue
            self.items.append((a, b, r, h, self._region(a, b)))
        self.starts = [it[0] for it in self.items]

    def _region(self, a, b):
        m = (a + b) / 2
        for idx, (x, y) in enumerate(self.regions):
            if x - EPS <= m <= y + EPS:
                return idx
        return -1


# ---------------------------------------------------------------- 指標
def overlap_matrix(items, filt=None):
    mat = defaultdict(float)
    ref_tot, hyp_tot = defaultdict(float), defaultdict(float)
    for a, b, r, h, reg in items:
        if filt is not None and not filt(reg):
            continue
        d = b - a
        for s in r:
            ref_tot[s] += d
        for s in h:
            hyp_tot[s] += d
        for s in r:
            for x in h:
                mat[(s, x)] += d
    return mat, ref_tot, hyp_tot


def best_mapping(items, filt=None):
    """最大化重疊時間的一對一對應 {參考: 系統}（md-eval 的做法）。"""
    mat, ref_tot, hyp_tot = overlap_matrix(items, filt)
    R, H = sorted(ref_tot), sorted(hyp_tot)
    if not R or not H:
        return {}, mat, ref_tot, hyp_tot
    asg = assign_max([[mat.get((r, h), 0.0) for h in H] for r in R])
    return {R[i]: H[j] for i, j in asg.items() if mat.get((R[i], H[j]), 0.0) > EPS}, mat, ref_tot, hyp_tot


def der(items):
    mapping, mat, ref_tot, hyp_tot = best_mapping(items)
    miss = fa = conf = total = 0.0
    for a, b, r, h, _ in items:
        d = b - a
        nr, nh = len(r), len(h)
        correct = sum(1 for s in r if mapping.get(s) in h)
        total += nr * d
        miss += max(0, nr - nh) * d
        fa += max(0, nh - nr) * d
        conf += (min(nr, nh) - correct) * d
    pct = (lambda x: 100.0 * x / total) if total > EPS else (lambda x: float("nan"))
    return {"total_s": total, "miss_s": miss, "false_alarm_s": fa, "confusion_s": conf,
            "miss_pct": pct(miss), "false_alarm_pct": pct(fa), "confusion_pct": pct(conf),
            "der_pct": pct(miss + fa + conf), "mapping": mapping}


def jer(items):
    """每個參考發言者各算一個 Jaccard 誤差再取平均；對應是另外最大化 Jaccard 得到的。"""
    mat, ref_tot, hyp_tot = overlap_matrix(items)
    R, H = sorted(ref_tot), sorted(hyp_tot)
    if not R:
        return {"jer_pct": float("nan"), "per_speaker": {}}
    J = {}
    for r in R:
        for h in H:
            inter = mat.get((r, h), 0.0)
            union_t = ref_tot[r] + hyp_tot[h] - inter
            J[(r, h)] = inter / union_t if union_t > EPS else 0.0
    asg = assign_max([[J[(r, h)] for h in H] for r in R]) if H else {}
    per = {}
    for i, r in enumerate(R):
        j = asg.get(i)
        jac = J[(r, H[j])] if j is not None else 0.0
        # 全部為 0 時演算法會隨便配一個；Jaccard 是 0 就不算「對應到」，不要顯示出來誤導人
        per[r] = {"jaccard": jac, "matched": H[j] if (j is not None and jac > EPS) else None,
                  "ref_seconds": ref_tot[r]}
    return {"jer_pct": 100.0 * sum(1 - v["jaccard"] for v in per.values()) / len(per),
            "per_speaker": per}


def consistency(items):
    """分裂（一個人被拆成幾群）、合併（一群混了幾個人）。"""
    mat, ref_tot, hyp_tot = overlap_matrix(items)
    split, merge = {}, {}
    for r, tot in ref_tot.items():
        used = sorted(((h, mat[(r, h)]) for (rr, h) in mat if rr == r and mat[(r, h)] / tot >= SPLIT_SHARE),
                      key=lambda x: -x[1])
        split[r] = [{"cluster": h, "share": v / tot} for h, v in used]
    for h, tot in hyp_tot.items():
        used = sorted(((r, mat[(r, h)]) for (r, hh) in mat if hh == h and mat[(r, h)] / tot >= SPLIT_SHARE),
                      key=lambda x: -x[1])
        if len(used) > 1:
            merge[h] = [{"speaker": r, "share": v / tot} for r, v in used]
    return split, merge


def cross_time(sc):
    """每個參考發言者在不同時間段裡，最主要對應到的是不是同一個群。"""
    if not sc.items:
        return {}, "—", {}
    if len(sc.regions) >= 2:
        groups = {reg: [it for it in sc.items if it[4] == reg] for reg in range(len(sc.regions))}
        label = "各區段"
        names = {reg: f"區段{reg + 1}" for reg in groups}
    else:
        total = sum(b - a for a, b, *_ in sc.items)
        acc, first, second = 0.0, [], []
        for it in sc.items:
            (first if acc < total / 2 else second).append(it)
            acc += it[1] - it[0]
        groups = {0: first, 1: second}
        label = "前半與後半，以計分時間對半切"
        names = {0: "前半", 1: "後半"}
    per = defaultdict(dict)
    for g, items in groups.items():
        mat, ref_tot, _ = overlap_matrix(items)
        for r, tot in ref_tot.items():
            if tot < 5.0:                              # 太少不足以判斷
                continue
            cands = [(mat[(rr, h)], h) for (rr, h) in mat if rr == r]
            if cands:
                per[r][g] = max(cands)[1]
    out = {}
    for r, d in per.items():
        if len(d) >= 2:
            out[r] = {"clusters": d, "same": len(set(d.values())) == 1}
    return out, label, names


def turn_bands(sc, mapping, ref_turns):
    """依參考發言長度分層：這一段被正確指派的時間比例。"""
    out = []
    for lo, hi in BANDS:
        n = ok = 0
        right = seen = 0.0
        for a, b, r in ref_turns:
            if not (lo <= b - a < hi):
                continue
            i = bisect.bisect_right(sc.starts, a) - 1
            i = max(i, 0)
            t_seen = t_right = 0.0
            while i < len(sc.items) and sc.items[i][0] < b - EPS:
                x, y, R, H, _ = sc.items[i]
                lo2, hi2 = max(x, a), min(y, b)
                if hi2 - lo2 > EPS and r in R:
                    t_seen += hi2 - lo2
                    if mapping.get(r) in H:
                        t_right += hi2 - lo2
                i += 1
            if t_seen < EPS:
                continue
            n += 1
            seen += t_seen
            right += t_right
            if t_right / t_seen >= 0.5:
                ok += 1
        out.append({"lo": lo, "hi": None if hi == float("inf") else hi, "turns": n,
                    "turns_correct": ok, "time_correct_ratio": (right / seen) if seen > EPS else None})
    return out


def overlap_stats(sc):
    ref2 = ref3 = err2 = det2 = 0.0
    mapping = der(sc.items)["mapping"]
    for a, b, r, h, _ in sc.items:
        d = b - a
        if len(r) >= 2:
            ref2 += d
            if len(h) >= 2:
                det2 += d
            right = sum(1 for s in r if mapping.get(s) in h)
            err2 += (len(r) - right) * d
        if len(r) >= 3:
            ref3 += d
    speaker_time = sum((b - a) * len(r) for a, b, r, *_ in sc.items if len(r) >= 2)
    return {"ref_overlap_seconds": ref2, "ref_overlap3_seconds": ref3,
            "overlap_detected_ratio": (det2 / ref2) if ref2 > EPS else None,
            "overlap_speaker_error_ratio": (err2 / speaker_time) if speaker_time > EPS else None}


# ---------------------------------------------------------------- 主流程
def json_safe(x):
    """NaN／Infinity 不是合法的 JSON，換成 null；tuple 與數字鍵也一併轉成 JSON 認得的形狀。"""
    if isinstance(x, float):
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    if isinstance(x, dict):
        return {str(k): json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]
    return x


def parse_regions(specs):
    out = []
    for s in specs:
        m = re.fullmatch(r"\s*([0-9.]+)\s*:\s*([0-9.]+)\s*", s)
        if not m:
            raise ScoreInputError(f"--region 要寫成 開始秒:結束秒，收到 {s!r}")
        a, b = _t(m.group(1)), _t(m.group(2))
        if b <= a:
            raise ScoreInputError(f"--region {s!r}：結束必須大於開始")
        out.append((a, b))
    return out


def shift(turns, offset):
    return [(_t(a + offset), _t(b + offset), s) for a, b, s in turns]


def score(ref_all, hyp, regions=None, collar=0.25, hyp_duration=None):
    """回傳完整結果 dict（也是 --json-out 的內容）。ref_all 含 UNK。"""
    warnings = []
    if not ref_all:
        raise ScoreInputError("參考標註是空的")
    unk = [(a, b) for a, b, s in ref_all if s.upper() == UNKNOWN_LABEL]
    ref = [(a, b, s) for a, b, s in ref_all if s.upper() != UNKNOWN_LABEL]
    if not ref:
        raise ScoreInputError("參考標註裡除了 UNK 沒有任何已知發言者")
    default_region = (min(a for a, _, _ in ref_all), max(b for _, b, _ in ref_all))
    regions = regions or [default_region]
    if hyp_duration is not None:
        for a, b in regions:
            if b > hyp_duration + 1.0:
                warnings.append(f"計分範圍 {a:g}–{b:g} 秒超出辨識結果的音訊長度 {hyp_duration:g} 秒；"
                                "請確認標註與辨識用的是同一份音訊")
    if not hyp:
        warnings.append("辨識結果沒有任何發言區間，所有參考發言都會算成漏報")
    s00 = sum(b - a for a, b, s in hyp if s == UNASSIGNED)

    main = Scoring(ref, hyp, regions, collar, unk)
    d_main = der(main.items)
    if d_main["total_s"] < EPS:
        raise ScoreInputError(
            "計分範圍內沒有任何參考發言可以計分。可能是 --region 與標註的時間對不上、"
            "範圍內全是 UNK，或標註全落在 collar 之內")
    d_nocollar = der(Scoring(ref, hyp, regions, 0.0, unk).items)
    d_skip = der(Scoring(ref, hyp, regions, collar, unk, skip_overlap=True).items)
    j = jer(main.items)
    mapping = d_main["mapping"]
    split, merge = consistency(main.items)
    cross, cross_label, cross_names = cross_time(main)
    ref_turns = merge_same_speaker(ref)

    _, ref_tot, hyp_tot = overlap_matrix(main.items)
    hyp_speech = sum(hyp_tot.values())
    major = [h for h, t in hyp_tot.items()
             if t / hyp_speech >= MINOR_SHARE and t >= MINOR_SECONDS] if hyp_speech > EPS else []
    for r, t in sorted(ref_tot.items()):
        if t < MIN_RELIABLE_SECONDS:
            warnings.append(f"參考發言者 {r} 在計分範圍內只有 {t:.1f} 秒，他的 JER 與分裂／合併判斷不可靠")
    scored_total = sum(b - a for a, b in main.scored)
    if scored_total < 300:
        warnings.append(f"計分的時間只有 {scored_total:.0f} 秒；指標的不確定度會很大，請當成方向性參考")

    return {
        "scored_seconds": scored_total,
        "regions": [list(r) for r in main.regions],
        "excluded_unk_seconds": main.excluded_unk,
        "ref_turns": len(ref_turns), "ref_speakers": sorted(ref_tot),
        "ref_speech_seconds": {k: v for k, v in sorted(ref_tot.items())},
        "hyp_clusters": sorted(hyp_tot), "hyp_major_clusters": sorted(major),
        "hyp_speech_seconds": {k: v for k, v in sorted(hyp_tot.items())},
        "hyp_unassigned_seconds": s00, "collar": collar,
        "der": {k: v for k, v in d_main.items() if k != "mapping"},
        "der_no_collar_pct": d_nocollar["der_pct"], "der_skip_overlap_pct": d_skip["der_pct"],
        "mapping": mapping, "jer": j, "split": split, "merge": merge,
        "cross_time": cross, "cross_time_label": cross_label, "cross_time_names": cross_names,
        "turn_bands": turn_bands(main, mapping, ref_turns),
        "overlap": overlap_stats(main),
        "warnings": warnings,
    }


def offset_scan(ref_all, hyp, regions, collar, span=1.0, step=0.05):
    """診斷：把系統輸出整體平移，看 DER 會不會明顯變好。標註與辨識的時間軸若有固定偏移，
    collar 只能吸收一部分，這個掃描能把它抓出來。不會自動套用。"""
    unk = [(a, b) for a, b, s in ref_all if s.upper() == UNKNOWN_LABEL]
    ref = [(a, b, s) for a, b, s in ref_all if s.upper() != UNKNOWN_LABEL]
    regions = regions or [(min(a for a, _, _ in ref_all), max(b for _, b, _ in ref_all))]
    rows = []
    for k in range(-int(span / step), int(span / step) + 1):
        off = round(k * step, 6)
        rows.append((off, der(Scoring(ref, shift(hyp, off), regions, collar, unk).items)["der_pct"]))
    return rows


# ---------------------------------------------------------------- 報告
def _f(x, nd=1, suffix="%"):
    return "—" if x is None or x != x else f"{x:.{nd}f}{suffix}"


def render(res, ref_path, hyp_path):
    L = []
    d = res["der"]
    L.append("=" * 64)
    L.append("發言者辨識評分")
    L.append("=" * 64)
    L.append(f"參考標註 : {ref_path}")
    L.append(f"辨識結果 : {hyp_path}")
    L.append(f"計分範圍 : {', '.join(f'{a:g}–{b:g}s' for a, b in res['regions'])}"
             f"（實際計分 {res['scored_seconds']:.1f} 秒；UNK 排除 {res['excluded_unk_seconds']:.1f} 秒）")
    L.append(f"參考     : {res['ref_turns']} 段發言、{len(res['ref_speakers'])} 位發言者")
    L.append(f"辨識     : {len(res['hyp_clusters'])} 群（其中 {len(res['hyp_major_clusters'])} 群為主要，"
             f"即 ≥{MINOR_SHARE:.0%} 且 ≥{MINOR_SECONDS:.0f} 秒）"
             + (f"；S00 未指派 {res['hyp_unassigned_seconds']:.1f} 秒（算成漏報）"
                if res["hyp_unassigned_seconds"] > EPS else ""))
    for w in res["warnings"]:
        L.append(f"⚠ {w}")

    L.append("")
    L.append(f"--- DER（collar 單側 {res['collar']:g} 秒） ---")
    L.append(f"  漏報   {_f(d['miss_pct'], 2)}   ({d['miss_s']:.1f}s)")
    L.append(f"  誤報   {_f(d['false_alarm_pct'], 2)}   ({d['false_alarm_s']:.1f}s)")
    L.append(f"  混淆   {_f(d['confusion_pct'], 2)}   ({d['confusion_s']:.1f}s)")
    L.append(f"  DER    {_f(d['der_pct'], 2)}   分母 = 參考發言者時間 {d['total_s']:.1f}s")
    L.append(f"  對照   不留 collar {_f(res['der_no_collar_pct'], 2)}｜排除重疊區 {_f(res['der_skip_overlap_pct'], 2)}")
    L.append(f"  JER    {_f(res['jer']['jer_pct'], 2)}")

    L.append("")
    L.append("--- 各參考發言者 ---")
    L.append(f"  {'發言者':>6} {'語音(s)':>9} {'Jaccard':>8}  對應到的群（DER 對應 / JER 對應）")
    for r in res["ref_speakers"]:
        pj = res["jer"]["per_speaker"].get(r, {})
        L.append(f"  {r:>6} {res['ref_speech_seconds'][r]:9.1f} {_f(pj.get('jaccard', 0) * 100, 1, '%'):>8}  "
                 f"{res['mapping'].get(r, '—')} / {pj.get('matched') or '—'}")

    L.append("")
    L.append("--- 代號一致性（整場 S0x 的核心問題）---")
    any_issue = False
    for r in res["ref_speakers"]:
        used = res["split"].get(r, [])
        if len(used) > 1:
            any_issue = True
            L.append(f"  分裂 {r}：被拆成 {len(used)} 群 → "
                     + "、".join(f"{u['cluster']}({u['share']:.0%})" for u in used))
    for h, parts in sorted(res["merge"].items()):
        any_issue = True
        L.append(f"  合併 {h}：混了 {len(parts)} 個人 → "
                 + "、".join(f"{p['speaker']}({p['share']:.0%})" for p in parts))
    if not any_issue:
        L.append("  沒有發現分裂或合併（以 ≥10% 為門檻）")
    if res["cross_time"]:
        L.append(f"  跨時間最主要對應的群（{res['cross_time_label']}）：")
        names = res["cross_time_names"]
        for r, v in sorted(res["cross_time"].items()):
            cl = "、".join(f"{names.get(k, names.get(str(k), k))}:{c}" for k, c in sorted(v["clusters"].items()))
            L.append(f"    {r}  {'一致 ✔' if v['same'] else '不一致 ✖'}   ({cl})")
    else:
        L.append("  跨時間：沒有任何發言者在兩個以上時間段各有 ≥5 秒語音，無法判斷")

    L.append("")
    L.append("--- 依參考發言長度分層 ---")
    L.append(f"  {'長度':>8} {'發言數':>6} {'>=50%正確':>9} {'時間正確率':>10}")
    for b in res["turn_bands"]:
        lab = f"{b['lo']:g}–{b['hi']:g}s" if b["hi"] else f">{b['lo']:g}s"
        L.append(f"  {lab:>8} {b['turns']:6d} {b['turns_correct']:9d} {_f(None if b['time_correct_ratio'] is None else 100 * b['time_correct_ratio']):>10}")

    o = res["overlap"]
    L.append("")
    L.append("--- 重疊發言 ---")
    if o["ref_overlap_seconds"] > EPS:
        L.append(f"  參考中 ≥2 人同時講 {o['ref_overlap_seconds']:.1f}s（其中 ≥3 人 {o['ref_overlap3_seconds']:.1f}s）")
        L.append(f"  系統也偵測到重疊的比例 {_f(100 * o['overlap_detected_ratio'])}；"
                 f"重疊區間內發言者被漏掉或指派錯的比例 {_f(100 * o['overlap_speaker_error_ratio'])}")
    else:
        L.append("  計分範圍內沒有重疊發言（這份標註無法評估重疊表現）")
    L.append("")
    L.append("註：這支工具不訂通過門檻。數字只對這份樣本、這組設備與擺位有意義。")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="會議發言者辨識評分器",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--hyp", required=True)
    ap.add_argument("--ref-format", choices=["auto", "labels", "rttm"], default="auto")
    ap.add_argument("--hyp-format", choices=["auto", "labels", "rttm", "json"], default="auto")
    ap.add_argument("--region", action="append", default=[], metavar="開始:結束")
    ap.add_argument("--collar", type=float, default=0.25)
    ap.add_argument("--offset", type=float, default=0.0, help="把辨識結果整體平移這麼多秒再計分")
    ap.add_argument("--check-offset", action="store_true", help="掃描 ±1 秒的整體偏移，診斷時間軸是否錯開")
    ap.add_argument("--json-out")
    a = ap.parse_args(argv)
    try:
        if a.collar < 0:
            raise ScoreInputError("--collar 不能是負數")
        ref, w1, _ = load_turns(a.ref, a.ref_format)
        hyp, w2, dur = load_turns(a.hyp, a.hyp_format)
        regions = parse_regions(a.region)
        hyp = shift(hyp, a.offset)
        res = score(ref, hyp, regions or None, a.collar, dur)
        res["warnings"] = [*w1, *w2, *res["warnings"]]
        scan = offset_scan(ref, hyp, regions or None, a.collar) if a.check_offset else None
    except ScoreInputError as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2
    print(render(res, a.ref, a.hyp))
    if scan:
        best = min(scan, key=lambda r: r[1])
        base = next(v for o, v in scan if abs(o) < 1e-9)
        print("\n--- 時間偏移診斷 ---")
        if base - best[1] >= 1.0:
            print(f"  ⚠ 把辨識結果平移 {best[0]:+.2f} 秒後 DER 從 {base:.2f}% 降到 {best[1]:.2f}%。"
                  "標註和辨識的時間軸可能有固定偏移（例如標註時的反應延遲），collar 只吸收得了一部分。"
                  "這不會自動套用；確認原因後再用 --offset。")
        else:
            print(f"  最佳偏移 {best[0]:+.2f} 秒，DER {best[1]:.2f}%；與不偏移（{base:.2f}%）差不到 1 個百分點，時間軸看起來是對齊的。")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(json_safe(res), ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
        print(f"\n已寫入 {a.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
