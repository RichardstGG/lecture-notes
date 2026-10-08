"""core/capture.py 的 TrackAligner：雙來源對齊的純邏輯（用模擬時鐘，不需要任何裝置）。

這裡驗證的是「兩路時間軸怎麼對上」：起點偏移補零、資料晚到與真正掉音要分得開、
裝置時脈與電腦時鐘的漂移要被修掉。模擬方式：裝置在真實時間 t 產生第 k 個樣本，
讀取端在樣本產生後加上固定延遲與抖動才讀到；漂移用「裝置實際取樣率 = 標稱 × (1+ε)」表示。
"""
import array
import random
import unittest

from . import _pathfix  # noqa: F401
from core import capture as C

SR = C.SR
CHUNK = 1600          # 100 ms


def simulate(seconds, aligner_factory, drift=0.0, latency=0.02, jitter=0.0, stall=None,
             loss=None, seed=1, start=100.0):
    """回傳 (aligner, 輸出 bytes, 輸入樣本總數)。

    drift：裝置取樣率偏差（-1e-4 = 慢 100 ppm）。
    stall：(開始秒, 長度秒)：這段期間資料暫時沒送出，之後一次補上（管線卡住，資料沒掉）。
    loss：(開始秒, 長度秒)：這段期間裝置的樣本真的遺失（讀取端永遠收不到）。
    """
    rng = random.Random(seed)
    rate = SR * (1 + drift)
    total = int(seconds * rate)
    chunks = []
    k = 0
    lost_until = 0
    while k + CHUNK <= total:
        t_end = start + (k + CHUNK) / rate
        if loss:
            ls, ll = loss
            if t_end - start > ls and t_end - start <= ls + ll:
                lost_until = k + CHUNK          # 這塊裝置有產生，但讀取端沒收到
                k += CHUNK
                continue
        a = t_end + latency + rng.uniform(0, jitter)
        if stall and stall[0] <= t_end - start < stall[0] + stall[1]:
            a = start + stall[0] + stall[1] + 0.01      # 卡住期間的資料在結束時一起到
        chunks.append((a, bytes(CHUNK * 2)))
        k += CHUNK
    chunks.sort(key=lambda c: c[0])
    first = chunks[0]
    base0 = min(a - (i + 1) * CHUNK / SR for i, (a, _) in enumerate(chunks[:20]))
    al = aligner_factory(first[0], base0)
    out = bytearray()
    n_in = 0
    for a, pcm in chunks:
        out += al.feed(a, pcm)
        n_in += len(pcm) // 2
    out += al.finish()
    return al, bytes(out), n_in


def factory(t0, **kw):
    return lambda a_first, base0: C.TrackAligner(t0, base0, a_first, **kw)


class LeadPaddingTests(unittest.TestCase):
    def test_later_starting_source_gets_leading_silence(self):
        # 時間軸原點比這一路的起點早 0.25 秒（另一路先開始）
        al, out, n_in = simulate(5, lambda a, b: C.TrackAligner(b - 0.25, b, a))
        self.assertEqual(al.lead_samples, 4000)
        self.assertEqual(out[:8000], bytes(8000))
        self.assertAlmostEqual(len(out) / 2 / SR, 5.25, delta=0.05)

    def test_earliest_source_has_no_padding(self):
        al, out, n_in = simulate(3, lambda a, b: C.TrackAligner(b, b, a))
        self.assertEqual(al.lead_samples, 0)
        self.assertEqual(len(out) // 2, n_in)

    def test_sample_positions_are_preserved(self):
        """補零之後每個樣本的位置要對：用遞增的計數器當內容檢查。"""
        al = C.TrackAligner(0.0, 0.1, 0.0)
        pcm = array.array("h", range(1600)).tobytes()
        out = al.feed(0.2, pcm) + al.finish()
        samples = array.array("h")
        samples.frombytes(out)
        self.assertEqual(al.lead_samples, 1600)
        self.assertEqual(list(samples[:1600]), [0] * 1600)
        self.assertEqual(list(samples[1600:1610]), list(range(10)))


class JitterAndStallTests(unittest.TestCase):
    def test_arrival_jitter_is_not_a_gap_and_barely_slips(self):
        al, out, n_in = simulate(120, lambda a, b: C.TrackAligner(b, b, a), jitter=0.05)
        self.assertEqual(al.gaps, [])
        self.assertAlmostEqual(len(out) / 2 / SR, 120, delta=0.1)
        self.assertLess(al.slip_inserted + al.slip_dropped, 40)

    def test_a_stalled_pipe_that_catches_up_is_not_a_gap(self):
        """管線卡住 2 秒後資料一次到齊：資料沒掉，不能補零（否則會把時間軸拉長）。"""
        al, out, n_in = simulate(60, lambda a, b: C.TrackAligner(b, b, a), stall=(20, 2.0))
        self.assertEqual(al.gaps, [])
        self.assertEqual(len(out) // 2, n_in + al.slip_inserted - al.slip_dropped)
        self.assertAlmostEqual(len(out) / 2 / SR, 60, delta=0.15)


class GapTests(unittest.TestCase):
    def test_real_loss_is_filled_with_silence_and_recorded(self):
        al, out, n_in = simulate(60, lambda a, b: C.TrackAligner(b, b, a), loss=(20, 0.5))
        self.assertEqual(len(al.gaps), 1)
        gap = al.gaps[0]
        self.assertAlmostEqual(gap["seconds"], 0.5, delta=0.12)
        self.assertAlmostEqual(gap["at_seconds"], 20.0, delta=0.3)
        # 補零後輸出長度要等於經過的時間，後面的內容才不會整段往前偏
        self.assertAlmostEqual(len(out) / 2 / SR, 60, delta=0.15)

    def test_two_separate_losses(self):
        rng_al, out, n_in = simulate(
            90, lambda a, b: C.TrackAligner(b, b, a), loss=(20, 1.0))
        self.assertEqual(len(rng_al.gaps), 1)
        self.assertAlmostEqual(rng_al.gaps[0]["seconds"], 1.0, delta=0.12)

    def test_loss_below_threshold_is_corrected_as_drift_not_reported(self):
        al, out, n_in = simulate(120, lambda a, b: C.TrackAligner(b, b, a), loss=(20, 0.1))
        self.assertEqual(al.gaps, [], "低於 gap_min 的落差不當成缺口")
        self.assertGreater(al.slip_inserted, 1000, "但時間軸仍要被慢慢拉回來")
        self.assertAlmostEqual(len(out) / 2 / SR, 120, delta=0.25)

    def test_unconfirmed_suspect_gap_is_not_filled_at_finish(self):
        """資料在疑似掉音後 0.3 秒就結束（來源死了）：沒有證據，不補零。"""
        al = C.TrackAligner(0.0, 0.0, 0.0)
        al.feed(0.1, bytes(3200))                       # 正常
        al.feed(2.0, bytes(3200))                       # 晚了 1.8 秒才到
        al.feed(2.1, bytes(3200))
        tail = al.finish()
        self.assertEqual(al.gaps, [])
        self.assertEqual(al.n_out, 3 * 1600)
        self.assertEqual(len(tail), 2 * 3200)     # 疑似缺口之後的兩塊，原樣放行


class DriftTests(unittest.TestCase):
    def test_slow_device_clock_gets_samples_inserted(self):
        al, out, n_in = simulate(1200, lambda a, b: C.TrackAligner(b, b, a), drift=-100e-6)
        self.assertEqual(al.gaps, [])
        # 20 分鐘慢 100 ppm = 0.12 秒；輸出長度要追上真實時間
        self.assertAlmostEqual(len(out) / 2 / SR, 1200, delta=0.1)
        self.assertGreater(al.slip_inserted, 1500)
        self.assertEqual(al.slip_dropped < 20, True)
        ppm = al.stats()["clock_error_ppm"]
        self.assertAlmostEqual(ppm, -100, delta=15)

    def test_fast_device_clock_gets_samples_dropped(self):
        al, out, n_in = simulate(1200, lambda a, b: C.TrackAligner(b, b, a), drift=+100e-6)
        self.assertAlmostEqual(len(out) / 2 / SR, 1200, delta=0.1)
        self.assertGreater(al.slip_dropped, 1500)
        self.assertAlmostEqual(al.stats()["clock_error_ppm"], 100, delta=15)

    def test_two_drifting_sources_stay_aligned_with_each_other(self):
        """重點不是單軌，而是兩軌互相對得上：慢 80 ppm 與快 60 ppm 的裝置跑 30 分鐘。"""
        a1, o1, _ = simulate(1800, lambda a, b: C.TrackAligner(b, b, a), drift=-80e-6, seed=1)
        a2, o2, _ = simulate(1800, lambda a, b: C.TrackAligner(b, b, a), drift=+60e-6, seed=2)
        self.assertLess(abs(len(o1) - len(o2)) / 2 / SR, 0.15)
        # 不修正的話，兩軌會差 1800 × 140e-6 = 0.25 秒
        raw_diff = abs(a1.n_in - a2.n_in) / SR
        self.assertGreater(raw_diff, 0.2)

    def test_excessive_drift_is_flagged_by_the_estimate(self):
        al, out, n_in = simulate(300, lambda a, b: C.TrackAligner(b, b, a), drift=-900e-6)
        self.assertLess(al.stats()["clock_error_ppm"], -C.DRIFT_WARN_PPM)

    def test_start_estimate_error_converges(self):
        """第一段資料晚到 40 ms，起點估計偏高；之後基準線下降，輸出要把多補的零收回來。"""
        al, out, n_in = simulate(60, lambda a, b: C.TrackAligner(b, b + 0.04, a))
        self.assertGreater(al.slip_dropped, 0)
        self.assertAlmostEqual(len(out) / 2 / SR, 60, delta=0.08)

    def test_no_drift_means_no_slips_to_speak_of(self):
        al, out, n_in = simulate(600, lambda a, b: C.TrackAligner(b, b, a), jitter=0.01)
        self.assertLess(al.slip_inserted + al.slip_dropped, 20)
        self.assertEqual(al.gaps, [])


class MixTests(unittest.TestCase):
    def test_mix_adds_and_saturates(self):
        a = array.array("h", [1000, 30000, -30000]).tobytes()
        b = array.array("h", [2000, 30000, -30000]).tobytes()
        out = array.array("h")
        out.frombytes(C.mix_pcm(a, b))
        self.assertEqual(list(out), [3000, 32767, -32768])

    def test_single_source_passes_through_unchanged(self):
        a = array.array("h", [5, -6, 7]).tobytes()
        self.assertEqual(C.mix_pcm(a, b""), a)

    def test_shorter_track_is_zero_padded(self):
        a = array.array("h", [1, 2, 3, 4]).tobytes()
        b = array.array("h", [10, 20]).tobytes()
        out = array.array("h")
        out.frombytes(C.mix_pcm(a, b))
        self.assertEqual(list(out), [11, 22, 3, 4])


if __name__ == "__main__":
    unittest.main()
