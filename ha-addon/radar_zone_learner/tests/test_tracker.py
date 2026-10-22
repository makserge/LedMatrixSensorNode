import math
import unittest

from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.tracker import Tracker

OFF = 1767571200000


def run(tr, frames, start=None):
    """frames: [(tf, [(x, y), ...])] -> samples [(track id, t_s, x, y)]."""
    out = []
    for tf, dets in frames:
        s = []
        tr.step(tf, dets, OFF, start, s)
        out += [(t.id, ts, x, y) for t, ts, x, y in s]
    return out


class TrackerTest(unittest.TestCase):
    def test_static_target(self):
        tr = Tracker(P)
        frames = [(1000 + 100 * k, [(1.0, 2.0)]) for k in range(50)]
        s = run(tr, frames[:1])
        self.assertEqual(s, [])                   # tentative after hit 1
        self.assertFalse(tr.live[0].confirmed)
        s = run(tr, frames[1:2])
        self.assertTrue(tr.live[0].confirmed)     # confirmed at hit 2
        self.assertEqual(s, [(1, (1000 + OFF) / 1000.0, 1.0, 2.0)])   # birth sample at birth time
        s = run(tr, frames[2:])
        self.assertEqual(len(tr.live), 1)
        times = [round(t * 1000) - OFF for _, t, _, _ in s]
        self.assertEqual(times, [2000, 3000, 4000, 5000])
        self.assertTrue(all(abs(x - 1.0) < 1e-9 and abs(y - 2.0) < 1e-9 for _, _, x, y in s))

    def test_crossing_ids_kept(self):
        tr = Tracker(P)
        frames = []
        for k in range(60):                       # A left->right at y 3.0, B right->left at y 4.5, 1 m/s
            x = -3.0 + 0.1 * k
            frames.append((100 * k, [(x, 3.0), (-x, 4.5)]))
        s = run(tr, frames)
        self.assertEqual(sorted(t.id for t in tr.live), [1, 2])
        by = {}
        for tid, _, x, y in s:
            by.setdefault(tid, []).append((x, y))
        self.assertTrue(all(abs(y - 3.0) < 0.05 for _, y in by[1]))
        self.assertTrue(all(abs(y - 4.5) < 0.05 for _, y in by[2]))
        self.assertGreater(by[1][-1][0], 2.0)
        self.assertLess(by[2][-1][0], -2.0)

    def test_outside_gate_new_track(self):
        tr = Tracker(P)
        run(tr, [(0, [(0.0, 2.0)]), (100, [(0.0, 2.0)])])
        run(tr, [(200, [(0.0, 2.95)])])           # 0.95 m from a still track: outside the 0.9 m gate
        self.assertEqual([t.id for t in tr.live], [1, 2])
        run(tr, [(300, [(0.0, 2.85)])])           # 0.85 m from track 1, but track 2 is nearer
        self.assertEqual(tr.live[1].hits, 2)
        self.assertEqual(tr.live[0].hits, 2)

    def test_ttl_boundary(self):
        tr = Tracker(P)
        run(tr, [(0, [(0.0, 2.0)]), (100, [(0.0, 2.0)])])
        tr.expire(2100)                           # 2000 ms after the last hit: kept
        self.assertEqual(len(tr.live), 1)
        tr.expire(2101)                           # 2001 ms: dies
        self.assertEqual(len(tr.live), 0)
        self.assertEqual(len(tr.dying), 1)
        self.assertEqual(tr.ready(100 + 2999), [])
        r = tr.ready(100 + 3000)                  # classified once the frontier is 3 s past the last hit
        self.assertEqual([t.id for t in r], [1])
        self.assertEqual(tr.dying, [])

    def test_seventh_track_evicts_oldest(self):
        tr = Tracker(P)
        for k in range(6):
            run(tr, [(100 * k, [(-6.0 + 2.0 * k, 1.0)])])
        self.assertEqual(len(tr.live), 6)
        run(tr, [(700, [(6.5, 7.5)])])
        self.assertEqual(len(tr.live), 6)
        self.assertEqual([t.id for t in tr.dying], [1])
        self.assertEqual(tr.live[-1].id, 7)

    def test_tie_goes_to_lower_id(self):
        tr = Tracker(P)
        run(tr, [(0, [(0.0, 2.0), (1.0, 2.0)])])  # two tentative tracks 1 m apart
        run(tr, [(100, [(0.5, 2.0)])])            # equidistant detection
        self.assertEqual(tr.live[0].hits, 2)
        self.assertEqual(tr.live[1].hits, 1)
        tr2 = Tracker(P)
        run(tr2, [(0, [(0.0, 2.0), (1.0, 2.0)])])
        run(tr2, [(100, [(0.5, 2.0)])])
        self.assertEqual([(t.id, t.hits, t.x) for t in tr2.live], [(t.id, t.hits, t.x) for t in tr.live])

    def test_alpha_beta_step(self):
        tr = Tracker(P)
        run(tr, [(0, [(1.0, 2.0)])])
        run(tr, [(200, [(1.2, 2.0)])])
        t = tr.live[0]
        # dt 0.2 s, prediction (1.0, 2.0), residual 0.2 m in x
        self.assertAlmostEqual(t.x, 1.0 + 0.35 * 0.2)
        self.assertAlmostEqual(t.vx, 0.08 / 0.2 * 0.2)
        self.assertAlmostEqual(t.y, 2.0)
        run(tr, [(300, [(1.3, 2.0)])])
        px = 1.07 + 0.08 * 0.1
        self.assertAlmostEqual(t.x, px + 0.35 * (1.3 - px))
        self.assertAlmostEqual(t.vx, 0.08 + 0.08 / 0.1 * (1.3 - px))

    def test_gate_grows_with_speed(self):
        tr = Tracker(P)
        frames = [(100 * k, [(-2.0 + 0.1 * k, 2.0)]) for k in range(20)]
        run(tr, frames)
        t = tr.live[0]
        speed = math.hypot(t.vx, t.vy)
        self.assertGreater(speed, 0.5)
        # hidden for 1.5 s, then 1.0 m beyond the prediction: inside 0.9 + |v| * 1.5
        px = t.x + t.vx * 1.5
        run(tr, [(1900 + 1500, [(px + 0.95, 2.0)])])
        self.assertEqual(len(tr.live), 1)

    def test_censor_all(self):
        tr = Tracker(P)
        run(tr, [(0, [(0.0, 2.0), (3.0, 2.0)]), (100, [(0.0, 2.0)])])
        tr.expire(3000)
        out = tr.censor_all()
        self.assertEqual([t.id for t in out], [1, 2])
        self.assertEqual((tr.live, tr.dying), ([], []))

    def test_powerup_flag(self):
        tr = Tracker(P)
        run(tr, [(5000, [(0.0, 2.0)])], start=2000)
        run(tr, [(5001, [(2.0, 2.0)])], start=2000)
        self.assertEqual([t.powerup for t in tr.live], [True, False])


if __name__ == "__main__":
    unittest.main()
