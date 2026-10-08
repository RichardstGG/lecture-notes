"""發言者辨識評分器 tools/score_diarization.py。

評分器的數字會直接決定「辨識品質夠不夠」，所以驗證分三層：
1. 手算得出答案的小案例（每個案例的期望值在註解裡推導）。
2. 對一份獨立寫的「10 ms 逐格」實作做隨機交叉比對 —— 驗證區間掃描沒有算錯。
3. 用引擎（core/diarize.py）真正產出的 speakers.json 與 manifest，確認 parser 與實際產物同步。

純標準函式庫；第 3 層需要 ffmpeg，沒有就跳過。
"""
import contextlib
import io
import itertools
import json
import random
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from . import _pathfix  # noqa: F401
from tests._pathfix import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import score_diarization as S                                   # noqa: E402


def sc(ref, hyp, regions=None, collar=0.0, dur=None):
    return S.score(ref, hyp, regions, collar, dur)


# ---------------------------------------------------------------- 1. 手算案例
class HandComputedDER(unittest.TestCase):
    def test_perfect_match(self):
        r = sc([(0, 10, "P1"), (10, 20, "P2")], [(0, 10, "S01"), (10, 20, "S02")])
        self.assertAlmostEqual(r["der"]["der_pct"], 0.0)
        self.assertAlmostEqual(r["jer"]["jer_pct"], 0.0)
        self.assertEqual(r["mapping"], {"P1": "S01", "P2": "S02"})

    def test_label_names_do_not_matter(self):
        # 系統把代號反過來叫也一樣：對應是最佳化出來的
        r = sc([(0, 10, "P1"), (10, 20, "P2")], [(0, 10, "S02"), (10, 20, "S01")])
        self.assertAlmostEqual(r["der"]["der_pct"], 0.0)
        self.assertEqual(r["mapping"], {"P1": "S02", "P2": "S01"})

    def test_same_label_namespace_on_both_sides_is_fine(self):
        r = sc([(0, 10, "S01"), (10, 20, "S02")], [(0, 10, "S02"), (10, 20, "S01")])
        self.assertAlmostEqual(r["der"]["der_pct"], 0.0)

    def test_pure_confusion(self):
        # 參考兩個人各 10 秒；系統全部叫同一個人。只有一邊能對上 -> 混淆 10 秒 / 20 秒
        r = sc([(0, 10, "P1"), (10, 20, "P2")], [(0, 20, "S01")])
        d = r["der"]
        self.assertAlmostEqual(d["miss_pct"], 0.0)
        self.assertAlmostEqual(d["false_alarm_pct"], 0.0)
        self.assertAlmostEqual(d["confusion_pct"], 50.0)
        self.assertAlmostEqual(d["der_pct"], 50.0)
        # JER：被配到的那位 Jaccard = 10 / (10+20-10) = 0.5；另一位沒配到 = 0。平均 (0.5 + 1)/2 = 75%
        self.assertAlmostEqual(r["jer"]["jer_pct"], 75.0)

    def test_pure_miss(self):
        r = sc([(0, 10, "P1"), (10, 20, "P2")], [(0, 10, "S01")])
        d = r["der"]
        self.assertAlmostEqual(d["miss_pct"], 50.0)
        self.assertAlmostEqual(d["false_alarm_pct"], 0.0)
        self.assertAlmostEqual(d["confusion_pct"], 0.0)

    def test_false_alarm_only_counts_inside_the_scored_region(self):
        ref, hyp = [(0, 10, "P1")], [(0, 10, "S01"), (10, 20, "S02")]
        # 預設範圍 = 參考的範圍 [0,10]，範圍外的系統輸出忽略
        self.assertAlmostEqual(sc(ref, hyp)["der"]["der_pct"], 0.0)
        # 明確把範圍拉到 20 秒：10–20 沒有參考發言 -> 誤報 10 秒，分母是參考的 10 秒
        r = sc(ref, hyp, regions=[(0, 20)])
        self.assertAlmostEqual(r["der"]["false_alarm_pct"], 100.0)
        self.assertAlmostEqual(r["der"]["der_pct"], 100.0)

    def test_collar_is_one_sided_and_shrinks_both_error_and_denominator(self):
        # 系統把 10 秒的邊界晚了 1 秒才換人。
        ref = [(0, 10, "P1"), (10, 20, "P2")]
        hyp = [(0, 11, "S01"), (11, 20, "S02")]
        self.assertAlmostEqual(sc(ref, hyp, collar=0.0)["der"]["der_pct"], 100 * 1 / 20)
        # collar 0.25：排除 [-.25,.25]（只有 0–.25 在範圍內）、[9.75,10.25]、[19.75,20.25]（只有 19.75–20）
        # 計分 = 20 - .25 - .5 - .25 = 19.0 秒；錯誤 10.25–11 = .75 秒
        r = sc(ref, hyp, collar=0.25)
        self.assertAlmostEqual(r["scored_seconds"], 19.0)
        self.assertAlmostEqual(r["der"]["total_s"], 19.0)
        self.assertAlmostEqual(r["der"]["der_pct"], 100 * 0.75 / 19.0)

    def test_adjacent_segments_of_the_same_speaker_get_no_inner_collar(self):
        # 同一個人的 0–5、5–10 是連續說話，5 秒那裡不是真的邊界
        r = sc([(0, 5, "P1"), (5, 10, "P1")], [(0, 10, "S01")], collar=0.25)
        self.assertAlmostEqual(r["scored_seconds"], 9.5)       # 只有兩端各 0.25

    def test_overlap_speech_counted_per_speaker(self):
        ref = [(0, 10, "P1"), (5, 15, "P2")]
        r = sc(ref, [(0, 10, "S01"), (5, 15, "S02")])
        self.assertAlmostEqual(r["der"]["der_pct"], 0.0)
        # 系統只抓到一個人：5–10 這段兩人同時講卻只輸出一人 -> 漏報 5 秒 / 20 秒
        r = sc(ref, [(0, 10, "S01"), (10, 15, "S02")])
        self.assertAlmostEqual(r["der"]["miss_pct"], 25.0)
        self.assertAlmostEqual(r["der"]["der_pct"], 25.0)

    def test_skip_overlap_variant_drops_overlapped_regions(self):
        ref = [(0, 10, "P1"), (5, 15, "P2")]
        r = sc(ref, [(0, 10, "S01"), (10, 15, "S02")])
        self.assertAlmostEqual(r["der_skip_overlap_pct"], 0.0)

    def test_unk_regions_are_not_scored_and_reported(self):
        ref = [(0, 10, "P1"), (10, 15, "UNK"), (15, 25, "P2")]
        # 系統在 UNK 期間亂叫也不該被算成誤報
        hyp = [(0, 10, "S01"), (10, 15, "S09"), (15, 25, "S02")]
        r = sc(ref, hyp)
        self.assertAlmostEqual(r["der"]["der_pct"], 0.0)
        self.assertAlmostEqual(r["excluded_unk_seconds"], 5.0)
        self.assertAlmostEqual(r["scored_seconds"], 20.0)
        self.assertNotIn("UNK", r["ref_speakers"])

    def test_unk_is_case_insensitive(self):
        r = sc([(0, 10, "P1"), (10, 15, "unk")], [(0, 10, "S01")])
        self.assertAlmostEqual(r["excluded_unk_seconds"], 5.0)

    def test_partial_annotation_needs_regions(self):
        ref = [(0, 10, "P1"), (100, 110, "P1")]
        hyp = [(0, 10, "S01"), (50, 60, "S02"), (100, 110, "S01")]
        # 沒指定範圍：範圍是 0–110，中間 50–60 的系統輸出被算成誤報
        self.assertGreater(sc(ref, hyp)["der"]["false_alarm_pct"], 0)
        # 指定只標註過的兩段：誤報消失
        r = sc(ref, hyp, regions=[(0, 10), (100, 110)])
        self.assertAlmostEqual(r["der"]["der_pct"], 0.0)
        self.assertAlmostEqual(r["scored_seconds"], 20.0)

    def test_s00_counts_as_no_output_and_is_reported(self):
        r = sc([(0, 10, "P1")], [(0, 10, "S00")])
        self.assertAlmostEqual(r["der"]["miss_pct"], 100.0)
        self.assertAlmostEqual(r["hyp_unassigned_seconds"], 10.0)
        self.assertEqual(r["hyp_clusters"], [])

    def test_no_hypothesis_is_all_miss_with_a_warning(self):
        r = sc([(0, 10, "P1")], [])
        self.assertAlmostEqual(r["der"]["miss_pct"], 100.0)
        self.assertTrue(any("沒有任何發言區間" in w for w in r["warnings"]))

    def test_overlapping_labels_of_the_same_hypothesis_speaker_count_once(self):
        r = sc([(0, 10, "P1")], [(0, 8, "S01"), (4, 10, "S01")])
        self.assertAlmostEqual(r["der"]["der_pct"], 0.0)


class HandComputedJER(unittest.TestCase):
    def test_half_covered(self):
        # P1 說 10 秒，系統只抓到前 5 秒：Jaccard = 5/10
        r = sc([(0, 10, "P1")], [(0, 5, "S01")])
        self.assertAlmostEqual(r["jer"]["jer_pct"], 50.0)

    def test_jer_has_its_own_best_mapping(self):
        # P1 0–10；系統把它拆成 S01(0–7) 與 S02(7–10)。Jaccard 最高的是 S01 = 7/10
        r = sc([(0, 10, "P1")], [(0, 7, "S01"), (7, 10, "S02")])
        self.assertAlmostEqual(r["jer"]["per_speaker"]["P1"]["jaccard"], 0.7)
        self.assertEqual(r["jer"]["per_speaker"]["P1"]["matched"], "S01")
        self.assertAlmostEqual(r["jer"]["jer_pct"], 30.0)

    def test_zero_overlap_is_not_reported_as_a_match(self):
        # S02 只在參考沒人說話的 10–20 秒出現（落在計分範圍內，所以是候選），與 P2 的重疊是 0。
        # 演算法會把 P2 隨便配給 S02；Jaccard 是 0 就不能顯示成「對應到」。
        r = sc([(0, 10, "P1"), (20, 30, "P2")], [(0, 10, "S01"), (10, 20, "S02")], regions=[(0, 30)])
        self.assertEqual(r["jer"]["per_speaker"]["P1"]["matched"], "S01")
        self.assertIsNone(r["jer"]["per_speaker"]["P2"]["matched"])
        self.assertEqual(r["jer"]["per_speaker"]["P2"]["jaccard"], 0.0)

    def test_extra_hypothesis_clusters_do_not_inflate_jer_by_themselves(self):
        r = sc([(0, 10, "P1")], [(0, 10, "S01"), (0, 0.001, "S02")])
        self.assertAlmostEqual(r["jer"]["jer_pct"], 0.0, places=2)


class JERConvention(unittest.TestCase):
    def test_speakers_with_no_scored_speech_are_not_in_the_average(self):
        # P2 完全不在計分範圍內：JER 只看 P1。若把 P2 算成 100% 誤差，會變成 50%。
        ref = [(0, 10, "P1"), (100, 110, "P2")]
        r = sc(ref, [(0, 10, "S01")], regions=[(0, 10)])
        self.assertAlmostEqual(r["jer"]["jer_pct"], 0.0)
        self.assertEqual(list(r["jer"]["per_speaker"]), ["P1"])


class ConsistencyReporting(unittest.TestCase):
    def test_split_is_detected(self):
        r = sc([(0, 20, "P1")], [(0, 10, "S01"), (10, 20, "S02")])
        used = r["split"]["P1"]
        self.assertEqual({u["cluster"] for u in used}, {"S01", "S02"})
        self.assertAlmostEqual(used[0]["share"], 0.5)

    def test_merge_is_detected(self):
        r = sc([(0, 10, "P1"), (10, 20, "P2")], [(0, 20, "S01")])
        self.assertEqual({p["speaker"] for p in r["merge"]["S01"]}, {"P1", "P2"})

    def test_a_small_leak_below_ten_percent_is_not_a_split(self):
        r = sc([(0, 100, "P1")], [(0, 95, "S01"), (95, 100, "S02")])
        self.assertEqual(len(r["split"]["P1"]), 1)

    def test_cross_time_consistency_across_regions(self):
        # P1 在後段被另一個代號叫走（S01 -> S03）；P2 前後一致
        ref = [(0, 10, "P1"), (10, 20, "P2"), (100, 110, "P1"), (110, 120, "P2")]
        hyp = [(0, 10, "S01"), (10, 20, "S02"), (100, 110, "S03"), (110, 120, "S02")]
        r = sc(ref, hyp, regions=[(0, 20), (100, 120)])
        self.assertFalse(r["cross_time"]["P1"]["same"])
        self.assertTrue(r["cross_time"]["P2"]["same"])
        self.assertEqual(r["cross_time_label"], "各區段")

    def test_cross_time_falls_back_to_first_and_second_half(self):
        ref = [(0, 20, "P1"), (20, 40, "P1")]
        hyp = [(0, 20, "S01"), (20, 40, "S02")]
        r = sc(ref, hyp)
        self.assertFalse(r["cross_time"]["P1"]["same"])
        self.assertIn("前半", r["cross_time_label"])
        self.assertEqual(r["cross_time_names"], {0: "前半", 1: "後半"})

    def test_cross_time_ignores_speakers_with_too_little_speech(self):
        # P2 在前半與後半都有出現，但各只有 2 秒（< 5 秒不足以判斷）。
        # 若拿掉這條規則，P2 會被報告成「不一致」；P1 各有 18 秒，照常判斷。
        ref = [(0, 18, "P1"), (18, 22, "P2"), (22, 40, "P1")]
        hyp = [(0, 20, "S01"), (20, 40, "S02")]
        r = sc(ref, hyp)
        self.assertNotIn("P2", r["cross_time"])
        self.assertIn("P1", r["cross_time"])
        self.assertFalse(r["cross_time"]["P1"]["same"])


class BandsAndOverlap(unittest.TestCase):
    def test_turn_length_bands(self):
        ref = [(0, 0.5, "P1"), (2, 5, "P2"), (10, 20, "P1")]
        hyp = [(0, 0.5, "S01"), (2, 5, "S01"), (10, 20, "S01")]      # S01 對應 P1：P2 的 3 秒是錯的
        bands = {(b["lo"], b["hi"]): b for b in sc(ref, hyp)["turn_bands"]}
        self.assertEqual(bands[(0.0, 1.0)]["turns_correct"], 1)
        self.assertEqual(bands[(2.0, 5.0)]["turns"], 1)
        self.assertEqual(bands[(2.0, 5.0)]["turns_correct"], 0)
        self.assertAlmostEqual(bands[(2.0, 5.0)]["time_correct_ratio"], 0.0)
        self.assertEqual(bands[(5.0, None)]["turns_correct"], 1)
        self.assertIsNone(bands[(1.0, 2.0)]["time_correct_ratio"])

    def test_overlap_statistics(self):
        ref = [(0, 10, "P1"), (5, 15, "P2")]
        o = sc(ref, [(0, 10, "S01")])["overlap"]
        self.assertAlmostEqual(o["ref_overlap_seconds"], 5.0)
        self.assertAlmostEqual(o["overlap_detected_ratio"], 0.0)
        # 重疊區 5 秒 x 2 人 = 10 人秒；P1 對上了、P2 沒有 -> 5 人秒錯 = 50%
        self.assertAlmostEqual(o["overlap_speaker_error_ratio"], 0.5)

    def test_no_overlap_is_reported_as_unmeasurable(self):
        o = sc([(0, 10, "P1")], [(0, 10, "S01")])["overlap"]
        self.assertIsNone(o["overlap_detected_ratio"])

    def test_triple_overlap_is_counted_separately(self):
        ref = [(0, 10, "P1"), (0, 10, "P2"), (0, 10, "P3")]
        o = sc(ref, [(0, 10, "S01")])["overlap"]
        self.assertAlmostEqual(o["ref_overlap3_seconds"], 10.0)


class WarningsAndErrors(unittest.TestCase):
    def test_short_speaker_and_short_sample_warn(self):
        r = sc([(0, 100, "P1"), (100, 110, "P2")], [(0, 110, "S01")])
        text = " ".join(r["warnings"])
        self.assertIn("P2", text)
        self.assertIn("只有", text)

    def test_empty_reference_is_an_error(self):
        with self.assertRaises(S.ScoreInputError):
            sc([], [(0, 1, "S01")])

    def test_reference_with_only_unk_is_an_error(self):
        with self.assertRaises(S.ScoreInputError):
            sc([(0, 10, "UNK")], [(0, 10, "S01")])

    def test_region_with_no_reference_speech_is_an_error_not_nan(self):
        with self.assertRaises(S.ScoreInputError) as cm:
            sc([(0, 10, "P1")], [(0, 10, "S01")], regions=[(50, 60)])
        self.assertIn("沒有任何參考發言", str(cm.exception))

    def test_region_beyond_audio_length_warns(self):
        r = sc([(0, 100, "P1")], [(0, 100, "S01")], dur=60.0)
        self.assertTrue(any("超出辨識結果的音訊長度" in w for w in r["warnings"]))


# ---------------------------------------------------------------- 匈牙利演算法
class AssignmentTests(unittest.TestCase):
    def test_matches_brute_force_on_random_matrices(self):
        rnd = random.Random(1234)
        for _ in range(400):
            n, m = rnd.randint(1, 5), rnd.randint(1, 5)
            mat = [[rnd.choice([0, 0, rnd.random() * 10]) for _ in range(m)] for _ in range(n)]
            asg = S.assign_max(mat)
            got = sum(mat[i][j] for i, j in asg.items())
            self.assertAlmostEqual(got, S.brute_force_max(mat), places=9, msg=str(mat))
            self.assertEqual(len(set(asg.values())), len(asg), "一對一")

    def test_empty(self):
        self.assertEqual(S.assign_max([]), {})
        self.assertEqual(S.assign_max([[]]), {})


class IntervalArithmetic(unittest.TestCase):
    def test_union_subtract_intersect(self):
        self.assertEqual(S.union([(5, 8), (0, 3), (2, 4)]), [(0, 4), (5, 8)])
        self.assertEqual(S.subtract([(0, 10)], [(2, 3), (5, 7)]), [(0, 2), (3, 5), (7, 10)])
        self.assertEqual(S.subtract([(0, 10)], [(-5, 20)]), [])
        self.assertEqual(S.intersect([(0, 5), (8, 12)], [(3, 10)]), [(3, 5), (8, 10)])


# ---------------------------------------------------------------- 2. 與獨立實作交叉比對
FRAME = 0.01


def oracle(ref, hyp, regions, collar, unk, skip_overlap=False):
    """獨立寫的 10 ms 逐格版本：不共用主程式的任何計算。所有時間都是 10 ms 的整數倍。"""
    ends = [b for _, b, _ in ref + hyp] + [r[1] for r in regions] + [b for _, b in unk]
    n = int(round(max(ends) / FRAME)) + 2
    sec = lambda x: int(round(x / FRAME))
    spk_ref = {}
    for a, b, s in ref:
        spk_ref.setdefault(s, [0] * n)
        for f in range(sec(a), sec(b)):
            spk_ref[s][f] = 1
    spk_hyp = {}
    for a, b, s in hyp:
        if s == "S00":
            continue
        spk_hyp.setdefault(s, [0] * n)
        for f in range(sec(a), sec(b)):
            spk_hyp[s][f] = 1
    scored = [0] * n
    for a, b in regions:
        for f in range(sec(a), sec(b)):
            scored[f] = 1
    for a, b in unk:
        for f in range(sec(a), sec(b)):
            scored[f] = 0
    c = sec(collar)
    for s, arr in spk_ref.items():
        for f in range(n):
            prev = arr[f - 1] if f > 0 else 0
            if arr[f] != prev:                      # 邊界在 f 的左緣
                for g in range(max(0, f - c), min(n, f + c)):
                    scored[g] = 0
    frames = []
    for f in range(n):
        if not scored[f]:
            continue
        R = {s for s, a in spk_ref.items() if a[f]}
        H = {s for s, a in spk_hyp.items() if a[f]}
        if skip_overlap and len(R) > 1:
            continue
        frames.append((R, H))
    RS, HS = sorted(spk_ref), sorted(spk_hyp)
    ov = {(r, h): sum(1 for R, H in frames if r in R and h in H) for r in RS for h in HS}
    k = min(len(RS), len(HS))
    best, best_map = -1, {}
    for rows in itertools.combinations(RS, k):
        for cols in itertools.permutations(HS, k):
            v = sum(ov[(r, h)] for r, h in zip(rows, cols))
            if v > best:
                best, best_map = v, dict(zip(rows, cols))
    miss = fa = conf = tot = 0
    for R, H in frames:
        nr, nh = len(R), len(H)
        correct = sum(1 for r in R if best_map.get(r) in H)
        tot += nr
        miss += max(0, nr - nh)
        fa += max(0, nh - nr)
        conf += min(nr, nh) - correct
    rt = {r: sum(1 for R, _ in frames if r in R) for r in RS}
    ht = {h: sum(1 for _, H in frames if h in H) for h in HS}
    # JER 只平均「在計分範圍內有說話」的參考發言者：沒說話的人 Jaccard 是 0/0，沒有定義
    # （DIHARD 的定義也是如此）。
    RJ = [r for r in RS if rt[r] > 0]
    if RJ:
        kk = min(len(RJ), len(HS))
        J = {(r, h): ov[(r, h)] / (rt[r] + ht[h] - ov[(r, h)]) if rt[r] + ht[h] - ov[(r, h)] else 0.0
             for r in RJ for h in HS}
        best_total = 0.0
        for rows in itertools.combinations(RJ, kk):
            for cols in itertools.permutations(HS, kk):
                best_total = max(best_total, sum(J[(r, h)] for r, h in zip(rows, cols)))
        jer = 100.0 * (len(RJ) - best_total) / len(RJ)
    else:
        jer = float("nan")
    return {"tot": tot * FRAME, "miss": miss * FRAME, "fa": fa * FRAME, "conf": conf * FRAME, "jer": jer}


class AgainstIndependentFrameImplementation(unittest.TestCase):
    def random_case(self, rnd):
        speakers = [f"P{i}" for i in range(rnd.randint(1, 4))]
        hyp_names = [f"S{i:02d}" for i in range(1, rnd.randint(2, 5))]
        total = rnd.randint(30, 120)

        def turns(names, n):
            out = []
            for _ in range(n):
                a = rnd.randint(0, total * 100 - 20) / 100.0
                out.append((a, round(a + rnd.randint(10, 1200) / 100.0, 2), rnd.choice(names)))
            return out
        ref = turns(speakers, rnd.randint(3, 14))
        hyp = turns(hyp_names + (["S00"] if rnd.random() < 0.3 else []), rnd.randint(3, 16))
        unk = []
        if rnd.random() < 0.4:
            a = rnd.randint(0, total * 100 - 600) / 100.0
            unk = [(a, round(a + rnd.randint(50, 500) / 100.0, 2))]
        return ref, hyp, unk

    def test_random_scenarios_match_the_frame_oracle(self):
        rnd = random.Random(20261006)
        checked = 0
        for case in range(250):
            ref, hyp, unk = self.random_case(rnd)
            collar = rnd.choice([0.0, 0.25, 0.1])
            regions = [(0.0, max(b for _, b, _ in ref + [(0, 1, 0)]) + 0.5)]
            if rnd.random() < 0.3:
                a = rnd.randint(0, 40) / 1.0
                regions = [(a, a + rnd.randint(20, 60))]
            ref_all = ref + [(a, b, "UNK") for a, b in unk]
            skip = rnd.random() < 0.25
            exp = oracle(ref, hyp, regions, collar, unk, skip_overlap=skip)
            if exp["tot"] < 0.05:
                continue
            main = S.Scoring(S.merge_same_speaker(ref), hyp, regions, collar, unk, skip_overlap=skip)
            got = S.der(main.items)
            tag = f"case {case} collar={collar} skip={skip}"
            self.assertAlmostEqual(got["total_s"], exp["tot"], places=6, msg=tag)
            self.assertAlmostEqual(got["miss_s"], exp["miss"], places=6, msg=tag)
            self.assertAlmostEqual(got["false_alarm_s"], exp["fa"], places=6, msg=tag)
            self.assertAlmostEqual(got["confusion_s"], exp["conf"], places=6, msg=tag)
            if not skip:
                self.assertAlmostEqual(S.jer(main.items)["jer_pct"], exp["jer"], places=4, msg=tag)
            checked += 1
        self.assertGreater(checked, 150, "要有足夠多的有效案例，否則交叉比對沒有意義")


# ---------------------------------------------------------------- 3. 讀檔
class Parsing(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)

    def write(self, name, text, enc="utf-8"):
        p = self.d / name
        p.write_text(text, encoding=enc)
        return p

    def test_audacity_labels(self):
        p = self.write("a.txt", "0.500000\t3.250000\tP1\n3.0\t4.0\tP 2 b\n\n# 註解\n")
        turns, warns = S.read_labels(p)
        self.assertEqual(turns, [(0.5, 3.25, "P1"), (3.0, 4.0, "P 2 b")])
        self.assertEqual(warns, [])

    def test_audacity_frequency_lines_bom_and_comma_decimals(self):
        p = self.write("a.txt", "0,5\t1,5\tP1\n\\\t100.0\t200.0\n2.0\t3.0\tP2\n", enc="utf-8-sig")
        turns, _ = S.read_labels(p)
        self.assertEqual(turns, [(0.5, 1.5, "P1"), (2.0, 3.0, "P2")])

    def test_point_labels_are_skipped_with_a_warning(self):
        p = self.write("a.txt", "1.0\t1.0\t備註\n2.0\t3.0\tP1\n")
        turns, warns = S.read_labels(p)
        self.assertEqual(turns, [(2.0, 3.0, "P1")])
        self.assertEqual(len(warns), 1)
        self.assertIn(":1", warns[0])

    def test_bad_label_lines_name_the_line(self):
        for text, needle in (("1.0\t0.5\tP1\n", ":1"), ("x\t2\tP1\n", "不是數字"),
                             ("1.0\t2.0\n", "三欄"), ("nan\t2\tP1\n", "有限")):
            with self.subTest(text=text):
                with self.assertRaises(S.ScoreInputError) as cm:
                    S.read_labels(self.write("b.txt", text))
                self.assertIn(needle, str(cm.exception))

    def test_rttm(self):
        p = self.write("a.rttm", "SPEAKER f 1 1.50 2.00 <NA> <NA> P1 <NA> <NA>\n"
                                 "SPEAKER f 1 4.00 0.00 <NA> <NA> P2 <NA> <NA>\n")
        turns, _ = S.read_rttm(p)
        self.assertEqual(turns, [(1.5, 3.5, "P1")])        # 長度 0 的略過

    def test_speakers_json(self):
        p = self.write("s.json", json.dumps({"schema_version": 1, "duration_seconds": 30.0, "segments": [
            {"start_ms": 1000, "end_ms": 2500, "speaker_id": "S01", "text": "x"}]}))
        turns, _, dur = S.read_speakers_json(p)
        self.assertEqual(turns, [(1.0, 2.5, "S01")])
        self.assertEqual(dur, 30.0)

    def test_speakers_json_rejects_wrong_shapes(self):
        cases = {"not json": "{",
                 "no segments": json.dumps({"schema_version": 1}),
                 "wrong schema": json.dumps({"schema_version": 2, "segments": []}),
                 "missing field": json.dumps({"schema_version": 1, "segments": [{"start_ms": 1}]}),
                 "zero length": json.dumps({"schema_version": 1, "segments": [
                     {"start_ms": 5, "end_ms": 5, "speaker_id": "S01"}]})}
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(S.ScoreInputError):
                    S.read_speakers_json(self.write("s.json", text))

    def test_format_is_detected_from_extension(self):
        self.write("x.rttm", "SPEAKER f 1 0 1 <NA> <NA> P1 <NA> <NA>\n")
        self.write("x.json", json.dumps({"schema_version": 1, "segments": [
            {"start_ms": 0, "end_ms": 1000, "speaker_id": "S01"}]}))
        self.write("x.txt", "0\t1\tP1\n")
        for name in ("x.rttm", "x.json", "x.txt"):
            turns, _, _ = S.load_turns(self.d / name)
            self.assertEqual(len(turns), 1, name)

    def test_missing_file(self):
        with self.assertRaises(S.ScoreInputError) as cm:
            S.load_turns(self.d / "nope.txt")
        self.assertIn("找不到檔案", str(cm.exception))

    def test_session_directory_resolves_through_the_manifest(self):
        gen = self.d / "diarization" / "0001"
        gen.mkdir(parents=True)
        (gen / "speakers.json").write_text(json.dumps({"schema_version": 1, "segments": [
            {"start_ms": 0, "end_ms": 1000, "speaker_id": "S01"}]}), encoding="utf-8")
        (self.d / "diarization.current.json").write_text(json.dumps(
            {"schema_version": 1, "generation": "0001",
             "speaker_transcript": "diarization/0001/transcript.speakers.md",
             "speakers": "diarization/0001/speakers.json"}), encoding="utf-8")
        turns, _, _ = S.load_turns(self.d)
        self.assertEqual(turns, [(0.0, 1.0, "S01")])

    def test_directory_without_manifest_explains_itself(self):
        with self.assertRaises(S.ScoreInputError) as cm:
            S.load_turns(self.d)
        self.assertIn("diarization.current.json", str(cm.exception))

    def test_manifest_cannot_point_outside_the_session(self):
        outside = self.d / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        sess = self.d / "sess"
        sess.mkdir()
        (sess / "diarization.current.json").write_text(
            json.dumps({"speakers": "../outside.json"}), encoding="utf-8")
        with self.assertRaises(S.ScoreInputError) as cm:
            S.load_turns(sess)
        self.assertIn("之外", str(cm.exception))


class RegionParsing(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(S.parse_regions(["0:300", " 600.5 : 720 "]), [(0.0, 300.0), (600.5, 720.0)])

    def test_invalid(self):
        for bad in ("300", "a:b", "5:00", "10:5", "-1:5"):
            with self.subTest(bad), self.assertRaises(S.ScoreInputError):
                S.parse_regions([bad])


# ---------------------------------------------------------------- 指令列
class CommandLine(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        (self.d / "ref.txt").write_text("0\t60\tP1\n60\t120\tP2\n", encoding="utf-8")
        (self.d / "hyp.txt").write_text("0\t60\tS01\n60\t120\tS02\n", encoding="utf-8")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = S.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_happy_path(self):
        code, out, _ = self.run_cli("--ref", str(self.d / "ref.txt"), "--hyp", str(self.d / "hyp.txt"))
        self.assertEqual(code, 0)
        self.assertIn("DER", out)
        self.assertIn("0.00%", out)
        self.assertIn("不訂通過門檻", out)

    def test_input_errors_exit_2_with_a_readable_message(self):
        code, out, err = self.run_cli("--ref", str(self.d / "nope.txt"), "--hyp", str(self.d / "hyp.txt"))
        self.assertEqual(code, 2)
        self.assertIn("找不到檔案", err)
        self.assertEqual(out, "")
        code, _, err = self.run_cli("--ref", str(self.d / "ref.txt"), "--hyp", str(self.d / "hyp.txt"),
                                    "--collar", "-1")
        self.assertEqual(code, 2)

    def test_json_out_is_strict_json_even_with_unmeasurable_values(self):
        out = self.d / "r.json"
        # 沒有重疊發言 -> 重疊指標是 None；單一發言者 -> 有些值可能是 NaN，都不能產生非法 JSON
        code, _, _ = self.run_cli("--ref", str(self.d / "ref.txt"), "--hyp", str(self.d / "hyp.txt"),
                                  "--json-out", str(out))
        self.assertEqual(code, 0)

        def refuse(c):
            raise AssertionError(f"JSON 裡不該出現 {c}")
        data = json.loads(out.read_text(encoding="utf-8"), parse_constant=refuse)
        self.assertEqual(data["der"]["der_pct"], 0.0)
        self.assertIsNone(data["overlap"]["overlap_detected_ratio"])

    def test_region_flag_limits_scoring(self):
        (self.d / "hyp2.txt").write_text("0\t60\tS01\n60\t120\tS01\n", encoding="utf-8")
        _, out, _ = self.run_cli("--ref", str(self.d / "ref.txt"), "--hyp", str(self.d / "hyp2.txt"),
                                 "--region", "0:60", "--collar", "0")
        self.assertIn("實際計分 60.0 秒", out)

    def test_report_names_the_time_groups_readably(self):
        (self.d / "ref3.txt").write_text("0\t20\tP1\n100\t120\tP1\n", encoding="utf-8")
        (self.d / "hyp3.txt").write_text("0\t20\tS01\n100\t120\tS02\n", encoding="utf-8")
        _, out, _ = self.run_cli("--ref", str(self.d / "ref3.txt"), "--hyp", str(self.d / "hyp3.txt"),
                                 "--region", "0:20", "--region", "100:120", "--collar", "0")
        self.assertIn("區段1:S01", out)
        self.assertIn("區段2:S02", out)
        self.assertIn("不一致 ✖", out)
        _, out, _ = self.run_cli("--ref", str(self.d / "ref.txt"), "--hyp", str(self.d / "hyp.txt"))
        self.assertNotIn("（前半與後半，以計分時間對半切））", out)

    def test_offset_flag_shifts_the_hypothesis(self):
        (self.d / "late.txt").write_text("1\t61\tS01\n61\t121\tS02\n", encoding="utf-8")
        _, aligned, _ = self.run_cli("--ref", str(self.d / "ref.txt"), "--hyp", str(self.d / "late.txt"),
                                     "--offset", "-1", "--collar", "0")
        self.assertIn("DER    0.00%", aligned)

    def test_check_offset_finds_a_fixed_shift(self):
        # 標註比辨識早 0.5 秒：整體平移能把 DER 降下來
        ref = "".join(f"{i * 6}\t{i * 6 + 6}\tP{i % 2}\n" for i in range(20))
        hyp = "".join(f"{i * 6 + 0.5}\t{i * 6 + 6.5}\tS{i % 2}\n" for i in range(20))
        (self.d / "r2.txt").write_text(ref, encoding="utf-8")
        (self.d / "h2.txt").write_text(hyp, encoding="utf-8")
        _, out, _ = self.run_cli("--ref", str(self.d / "r2.txt"), "--hyp", str(self.d / "h2.txt"),
                                 "--collar", "0", "--check-offset")
        self.assertIn("-0.50", out)
        self.assertIn("固定偏移", out)

    def test_check_offset_stays_quiet_when_aligned(self):
        _, out, _ = self.run_cli("--ref", str(self.d / "ref.txt"), "--hyp", str(self.d / "hyp.txt"),
                                 "--check-offset")
        self.assertIn("對齊", out)
        self.assertNotIn("固定偏移", out)


# ---------------------------------------------------------------- 4. 與引擎真實產物同步
def _wav(path, seconds):
    frames = bytearray()
    for i in range(int(seconds * 16000)):
        frames += struct.pack("<h", 2000 if (i // 800) % 2 else -2000)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(frames))


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class AgainstTheRealEngineArtifacts(unittest.TestCase):
    """用 core.diarize.diarize_session 真正寫出的檔案餵評分器，parser 與產物格式漂移時這裡會先掛。"""

    def test_scores_a_session_produced_by_diarize_session(self):
        from core import diarize as D
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        sess = root / "sess"
        sess.mkdir()
        _wav(sess / "source_audio.wav", 30.0)
        (root / "seg.onnx").write_bytes(b"x")
        (root / "emb.onnx").write_bytes(b"x")
        turns = [(0.0, 8.0, 0, 1.0), (10.0, 18.0, 1, 1.0), (20.0, 28.0, 0, 1.0)]
        req = D.DiarizationRequest(
            session_dir=sess, source_audio=sess / "source_audio.wav", source_sha256="",
            meeting_name="m", requested_speakers=2, segmentation_model=root / "seg.onnx",
            embedding_model=root / "emb.onnx", opencc=False)
        D.diarize_session(
            req, diarizer=lambda r, p, c: D.DiarizerOutput(turns, 30.0, "fake"),
            asr=lambda r, pcm, prompt: [(0.0, len(pcm) / 2 / 16000, "測試")])

        ref = root / "ref.txt"
        ref.write_text("0\t8\tP1\n10\t18\tP2\n20\t28\tP1\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = S.main(["--ref", str(ref), "--hyp", str(sess), "--collar", "0.25",
                           "--json-out", str(root / "r.json")])
        self.assertEqual(code, 0, err.getvalue())
        res = json.loads((root / "r.json").read_text(encoding="utf-8"))
        # 引擎在每段前後各補 0.15 秒的 pad（whisper 時間貼回去後仍在 collar 0.25 之內）
        self.assertEqual(res["mapping"], {"P1": "S01", "P2": "S02"})
        self.assertLess(res["der"]["der_pct"], 5.0)
        self.assertEqual(res["hyp_clusters"], ["S01", "S02"])
        self.assertNotIn("找不到", err.getvalue())


if __name__ == "__main__":
    unittest.main()
