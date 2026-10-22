"""Frame generator (P3 spec 11.3, 13.4): timeline, targets per frame, LD2412, coverage."""
import json
import math
import unittest
from types import SimpleNamespace

from radar_zone_learner.core.types import FramesMessage
from replay import framegen as G
from replay import world as W
from replay.batcher import OFF, ON, POWERING, WARMUP

DAY = W.DAY
DAY_MS = DAY * 1000


def _payloads(cfg, seed, until_sim_ms):
    rs = G.RunStream(cfg, seed, horizon_days=3)
    out = []
    for recv, _, ev in rs.events():
        if recv >= G.BASE_MS + until_sim_ms:
            break
        if isinstance(ev, FramesMessage):
            out.append(json.loads(ev.payload))
    return rs, out


class GeneratedRunTest(unittest.TestCase):
    """Bedroom, 22:00-06:00: day 0 and the night to day 1 06:01 (the window and a power-up)."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = W.make_cfg(bed=1, nw="22:00-06:00")
        cls.rs, cls.msgs = _payloads(cls.cfg, 1, DAY_MS + 6 * 3600 * 1000 + 60_000)
        cls.tl = cls.rs.timeline

    def sim_t(self, m):
        return m["t"] + G.BOOT_SIM_MS

    def test_frames_only_where_the_timeline_allows(self):
        listed = 0
        for m in self.msgs:
            for e in m.get("f", []):
                f = self.sim_t(m) - e[1]
                self.assertIn(self.tl.phase_at(f), (WARMUP, ON), f)
                listed += 1
        self.assertGreater(listed, 10000)
        off_a, off_b = 22 * 3600 * 1000, DAY_MS + 6 * 3600 * 1000
        for m in self.msgs:
            for e in m.get("f", []):
                f = self.sim_t(m) - e[1]
                self.assertFalse(off_a <= f < off_b + G.FIRST_FRAME_MS)

    def test_off_heartbeats_every_30_s(self):
        off = [m for m in self.msgs if m["sm"] == "N"]
        self.assertGreater(len(off), 900)          # 8 h / 30 s
        for m in off:
            self.assertEqual((m["n"], m["pw"]), (0, 0))
            for k in ("mt", "p2", "a0", "a1", "f"):
                self.assertNotIn(k, m)
            self.assertEqual(self.tl.phase_at(self.sim_t(m)), OFF)
        gaps = [b["t"] - a["t"] for a, b in zip(off, off[1:])]
        self.assertTrue(all(g == 30000 for g in gaps), set(gaps))
        self.assertLess(self.sim_t(off[0]) - 22 * 3600 * 1000, 40)       # first one when the window starts

    def test_flush_before_off_and_power_up_sequence(self):
        a = 22 * 3600 * 1000
        i = next(k for k, m in enumerate(self.msgs) if m["sm"] == "N")
        before = self.msgs[i - 1]
        self.assertEqual((before["pw"], before["sm"]), (3, "A"))          # the flush carries the ON header
        self.assertLess(abs(self.sim_t(before) - a), 40)
        b = DAY_MS + 6 * 3600 * 1000
        after = [m for m in self.msgs if self.sim_t(m) >= b]
        self.assertTrue(after)
        for m in after:
            ph = self.tl.phase_at(self.sim_t(m))
            if ph in (POWERING, WARMUP):
                self.assertEqual((m["pw"], m["sm"]), (0, "A"))
                self.assertNotIn("mt", m)
                self.assertNotIn("p2", m)
            else:
                self.assertEqual(ph, ON)
                self.assertEqual((m["pw"], m["sm"], m["mt"]), (3, "A", 1))
        warm = [m for m in after if m["pw"] == 0]
        self.assertTrue(warm)
        self.assertTrue(any(m["n"] > 0 for m in warm))                    # warm-up frames exist, with pw 0
        first_obs = min(self.sim_t(m) for m in after if m["pw"] == 3)
        self.assertGreaterEqual(first_obs, b + G.OBSERVE_MS)

    def test_seq_contiguous_and_slots(self):
        prev = None
        for m in self.msgs:
            if prev is not None:
                self.assertEqual(m["s"], prev["s"] + prev["n"])
            prev = m
            for e in m.get("f", []):
                slots = e[2::4]
                self.assertLessEqual(len(slots), 3)
                self.assertEqual(slots, sorted(set(slots)))

    def test_p2_only_while_observing(self):
        on = [m for m in self.msgs if m["pw"] == 3]
        self.assertTrue(all(m["p2"] in (0, 1) for m in on))
        self.assertTrue(any(m["p2"] == 1 for m in on))


def _source(tracks, noise=0.0, p_miss=0.0, seed=1, end_ms=60_000):
    tf = G.TargetFrames(seed, noise_m=noise, p_miss=p_miss)
    tf.add_tracks(0, tracks)
    ep = SimpleNamespace(intervals=[(0, end_ms, 0)])
    return G.EpochFrames(tf, ep)


def _frames(src, upto):
    return {item[2]: item[3] for item in src.drain(upto) if item[0] == "t"}


class TargetsTest(unittest.TestCase):
    def test_priority_and_stable_slots(self):
        def tr(kind, x, y, ts):
            return {"pts": [(t, x, y) for t in ts], "kind": kind, "agent": "x"}
        tracks = [tr("human", 1.0, 2.0, list(range(0, 4)) + list(range(6, 10))),
                  tr("human", -1.0, 3.0, range(0, 10)),
                  tr("mirror", 4.0, 2.0, range(2, 8)),
                  tr("refl", 2.0, 0.8, range(0, 10))]
        fr = _frames(_source(tracks), 10_000)
        xs = lambda ms: {s: (x, y) for s, x, y, v in fr[ms]}     # noqa: E731
        self.assertEqual(xs(0), {0: (1000, 2000), 1: (-1000, 3000), 2: (2000, 800)})
        # t = 2..3: four candidates, the reflector (lowest priority) is dropped; the mirror takes free slot 2
        self.assertEqual(xs(2500), {0: (1000, 2000), 1: (-1000, 3000), 2: (4000, 2000)})
        # t = 4..5: the first human is hidden; the others keep their slots, the reflector takes free slot 0
        self.assertEqual(xs(4500), {0: (2000, 800), 1: (-1000, 3000), 2: (4000, 2000)})
        # t = 6: it returns, the reflector is dropped again and the human takes the lowest free slot
        self.assertEqual(xs(6000), {0: (1000, 2000), 1: (-1000, 3000), 2: (4000, 2000)})
        self.assertTrue(all(len(t) <= 3 for t in fr.values()))
        self.assertEqual(sorted(fr), list(range(0, 10_000, 100)))

    def test_interpolation_and_radial_speed(self):
        tracks = [{"pts": [(0, 0.0, 2.0), (1, 0.0, 3.0), (2, 0.0, 3.0)], "kind": "human", "agent": "a"}]
        fr = _frames(_source(tracks), 3000)
        self.assertEqual(fr[0], ((0, 0, 2000, 100),))          # moving away at 1 m/s: +100 cm/s
        self.assertEqual(fr[500], ((0, 0, 2500, 100),))
        self.assertEqual(fr[1000], ((0, 0, 3000, 0),))          # next point equal: no motion
        self.assertEqual(fr[2900], ((0, 0, 3000, 0),))          # last point: held

    def test_reflector_not_noised_human_noised(self):
        refl = {"pts": [(t, 2.0 + 0.002 * t, 0.8) for t in range(0, 50)], "kind": "refl", "agent": "r"}
        hum = {"pts": [(t, -1.0, 3.0) for t in range(0, 50)], "kind": "human", "agent": "h"}
        src = _source([refl, hum], noise=G.NOISE_M, p_miss=0.0)
        fr = _frames(src, 50_000)
        dx = []
        for ms, tg in fr.items():
            t, q = divmod(ms, 1000)
            q //= 100
            for s, x, y, v in tg:
                if y == 800:
                    if t < 49:
                        self.assertEqual(x, 2000 + 2 * t + round(0.2 * q))
                else:
                    dx.append((x + 1000) / 1000.0)
        sd = math.sqrt(sum(d * d for d in dx) / len(dx))
        self.assertTrue(0.012 < sd < 0.028, sd)
        self.assertLess(abs(sum(dx) / len(dx)), 0.004)

    def test_frame_misses(self):
        hum = {"pts": [(t, -1.0, 3.0) for t in range(0, 2000)], "kind": "human", "agent": "h"}
        fr = _frames(_source([hum], p_miss=G.P_MISS, end_ms=2_000_000), 2_000_000)
        share = len(fr) / 20000.0
        self.assertTrue(0.975 < share < 0.985, share)


class Ld2412Test(unittest.TestCase):
    def test_duty_while_occupied_and_no_false_positive(self):
        ld = G.Ld2412Out(1)
        ld.add_occupancy([(0, 2_000_000), (2_100_000, 2_200_000)])
        on = n = 0
        for s in range(0, 2_000_000, 5):
            on += ld.out(s * 1000)
            n += 1
        self.assertAlmostEqual(on / n, 0.98, delta=0.005)
        self.assertEqual(sum(ld.out(s * 1000) for s in range(2_000_010, 2_099_990, 7)), 0)

    def test_miss_runs_average_20_s(self):
        ld = G.Ld2412Out(2)
        ld.add_occupancy([(0, 400_000)])
        runs, cur = [], 0
        for s in range(0, 400_000):
            if ld.out(s * 1000) == 0:
                cur += 1
            elif cur:
                runs.append(cur)
                cur = 0
        self.assertGreater(len(runs), 250)
        self.assertTrue(15 < sum(runs) / len(runs) < 25, sum(runs) / len(runs))


class TimelineTest(unittest.TestCase):
    def on_share(self, nw, d0, d1):
        tl = G.Timeline(W.make_cfg(nw=nw), 1, d1 + 1)
        a, b = d0 * DAY_MS, d1 * DAY_MS
        on = sum(max(0, min(s.end, b) - max(s.start, a)) for e in tl.epochs for s in e.segments if s.phase == ON)
        return on / (b - a)

    def test_observed_share_matches_design_section_8(self):
        # design 8.2 "Coverage": 0.750 for 00:00-06:00, 0.708 for 00:00-07:00, 0.667 for the 8 h windows
        # (after Night learning; here every night), minus the 12 s warm-up per power-up
        warm = 12 / 86400
        self.assertAlmostEqual(self.on_share("00:00-06:00", 1, 21), 0.750 - warm, places=4)
        self.assertAlmostEqual(self.on_share("00:00-07:00", 1, 21), 17 / 24 - warm, places=4)
        self.assertAlmostEqual(self.on_share("22:00-06:00", 1, 21), 16 / 24 - warm, places=4)
        self.assertAlmostEqual(self.on_share("23:00-07:00", 1, 21), 16 / 24 - warm, places=4)
        # over 56 days the simulator reports 0.7545 for 00:00-06:00 (day 0 has no window)
        self.assertAlmostEqual(self.on_share("00:00-06:00", 0, 56), 0.7545, delta=0.0002)

    def test_power_up_phases(self):
        tl = G.Timeline(W.make_cfg(nw="00:00-06:00"), 1, 3)
        b = DAY_MS + 6 * 3600 * 1000
        self.assertEqual(tl.phase_at(DAY_MS - 1), ON)
        self.assertEqual(tl.phase_at(DAY_MS), OFF)
        self.assertEqual(tl.phase_at(b - 1), OFF)
        self.assertEqual(tl.phase_at(b), POWERING)
        self.assertEqual(tl.phase_at(b + G.FIRST_FRAME_MS), WARMUP)
        self.assertEqual(tl.phase_at(b + G.OBSERVE_MS - 1), WARMUP)
        self.assertEqual(tl.phase_at(b + G.OBSERVE_MS), ON)
        self.assertTrue(tl.observing(b + G.OBSERVE_MS))
        self.assertFalse(tl.observing(b + G.FIRST_FRAME_MS))
        self.assertEqual(tl.phase_at(G.BOOT_SIM_MS), POWERING)
        self.assertEqual(tl.phase_at(0), ON)

    def test_faults_timeline(self):
        tl = G.Timeline(W.make_cfg(nw="00:00-06:00"), 1, 14, faults=True)
        r = (9 * DAY + 14 * 3600) * 1000
        self.assertEqual(len(tl.epochs), 2)
        self.assertNotEqual(tl.epochs[0].boot_id, tl.epochs[1].boot_id)
        self.assertIsNone(tl.phase_at(r + 1000))
        self.assertEqual(tl.phase_at(r + 20_000), WARMUP)
        self.assertEqual(tl.phase_at(r + 32_000), ON)
        self.assertEqual(tl.loss, ((12 * DAY + 10 * 3600) * 1000, (12 * DAY + 10 * 3600 + 600) * 1000))
        self.assertFalse(tl.observing(tl.loss[0] + 1))


class VolumeTest(unittest.TestCase):
    def test_frames_carry_the_world_points(self):
        rs = G.RunStream(W.make_cfg(bed=0), 1, horizon_days=2)
        msgs = 0
        for recv, _, ev in rs.events():
            if recv >= G.BASE_MS + 14 * 3600 * 1000:
                break
            msgs += isinstance(ev, FramesMessage)
        self.assertGreater(rs.targets.points, 2000)
        ratio = rs.targets.target_entries / (10.0 * rs.targets.points)
        self.assertTrue(0.95 < ratio <= 0.98 + 0.003, ratio)     # ~10 frames per 1 Hz point, 2% missed
        self.assertGreater(msgs, 14 * 720)          # 5 s heartbeats plus about 2 msg/s while targets


if __name__ == "__main__":
    unittest.main()
