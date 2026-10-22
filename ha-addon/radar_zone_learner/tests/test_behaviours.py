import math
import random
import unittest

from radar_zone_learner.core.behaviours import Behaviours, Fragment
from radar_zone_learner.core.covis import Covis
from radar_zone_learner.core.grids import GridGeom, Grids
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import FixedOffsetTime

T0 = 1767571200.0 + 10 * 3600       # 2026-01-05 10:00 UTC, wall seconds
TM = FixedOffsetTime(0)


class FakeObs:
    def __init__(self, ld=1.0, he=0.0):
        self.ld = ld
        self.he = he
        self.censors = []

    def ld_share_opt(self, a, b):
        if b <= a:
            return 1.0
        if self.ld is None:
            return None                   # unknown: the LD2412 was not observing
        return self.ld(a, b) if callable(self.ld) else self.ld

    def ld_share(self, a, b):
        v = self.ld_share_opt(a, b)
        return 0.0 if v is None else v

    def he_share(self, a, b):
        return self.he

    def censor_between(self, a, b):
        return any(a < c < b for c in self.censors)


class FakeTrack:
    def __init__(self, tid, powerup=False):
        self.id = tid
        self.powerup = powerup
        self.bh = None


class RecCovis(Covis):
    def __init__(self, p):
        super().__init__(p)
        self.frags = []

    def add(self, f):
        self.frags.append(f)
        super().add(f)


GEOM = GridGeom(P)


def make(ld=1.0, he=0.0):
    geom = GEOM
    grids = Grids(P, geom)
    cov = RecCovis(P)
    obs = FakeObs(ld, he)
    bh = Behaviours(P, grids, cov, obs, lambda t: TM.local(int(t * 1000)).date_ord)
    return bh, grids, cov, obs


def ref_fragments(pts, pu, cens):
    """Reference port of the project simulator's offline fragment loop (SIM:452-485)."""
    n = len(pts)
    if n < 3:
        return [], 0
    t0 = pts[0][0]
    k3 = min(3, n - 1)
    sp_b = math.hypot(pts[k3][1] - pts[0][1], pts[k3][2] - pts[0][2]) / max(1, pts[k3][0] - t0)
    walk = 0
    for k in range(1, n):
        if math.hypot(pts[k][1] - pts[k - 1][1], pts[k][2] - pts[k - 1][2]) > 0.3:
            walk += 1
    out = []
    i = 0
    while i < n:
        j = i + 1
        while j < n and math.hypot(pts[j][1] - pts[i][1], pts[j][2] - pts[i][2]) <= 0.5:
            j += 1
        if pts[j - 1][0] - pts[i][0] >= 8:
            seg = pts[i:j]
            cx = sum(p[1] for p in seg) / len(seg)
            cy = sum(p[2] for p in seg) / len(seg)
            out.append((cx, cy, len(seg), seg[0][0], seg[-1][0], j == n and not cens,
                        i == 0 and sp_b <= 0.35 and not pu))
            i = j
        else:
            i += 1
    return out, walk


def rand_track(rng):
    pts = []
    t = T0 + rng.randint(0, 3600)
    x, y = rng.uniform(-3, 3), rng.uniform(0.5, 6)
    n = rng.choice([1, 2, 3, 4, 5, 8, 12, 20, 40, 80, 150])
    mode = "still" if rng.random() < 0.5 else "walk"
    vx = vy = 0.0
    for _ in range(n):
        if rng.random() < 0.08:
            mode = "walk" if mode == "still" else "still"
            a = rng.uniform(0, 2 * math.pi)
            v = rng.uniform(0.4, 1.2)
            vx, vy = v * math.cos(a), v * math.sin(a)
        dt = 2.0 if rng.random() < 0.1 else 1.0
        if mode == "walk":
            x += vx * dt
            y += vy * dt
            pts.append((t, x + rng.gauss(0, 0.05), y + rng.gauss(0, 0.05)))
        else:
            sd = rng.choice([0.002, 0.03, 0.08, 0.2])
            pts.append((t, x + rng.gauss(0, sd), y + rng.gauss(0, sd)))
        t += dt
    return pts


class FragmentEquivalenceTest(unittest.TestCase):
    def test_equals_simulator_loop(self):
        rng = random.Random(465485)
        total = 0
        for k in range(500):
            pts = rand_track(rng)
            pu = rng.random() < 0.15
            cens = rng.random() < 0.3
            bh, grids, cov, _ = make()
            tr = FakeTrack(k + 1, pu)
            for t, x, y in pts:
                bh.on_sample(tr, t, x, y)
            bh.finish(tr, censored=cens)
            want, walk = ref_fragments(pts, pu, cens)
            got = [(f.cx, f.cy, f.n, f.t0, f.t1, f.open_end, f.open_start) for f in cov.frags]
            self.assertEqual(len(got), len(want), k)
            for g, w in zip(got, want):
                self.assertAlmostEqual(g[0], w[0], places=12)
                self.assertAlmostEqual(g[1], w[1], places=12)
                self.assertEqual(g[2:], w[2:], k)
            self.assertEqual(bh.walk_points_total, walk, k)
            inside = sum(1 for i in range(1, len(pts)) if len(pts) >= 3
                         and math.hypot(pts[i][1] - pts[i - 1][1], pts[i][2] - pts[i - 1][2]) > 0.3
                         and grids.geom.cell_of(pts[i][1], pts[i][2]) >= 0)
            self.assertEqual(sum(grids["walk"]), inside)
            total += len(want)
        self.assertGreater(total, 200)


class FoldedEquivalenceTest(unittest.TestCase):
    def test_folded_equals_simulator_loop(self):
        """With fold_n = 12 nearly every run is folded; fragments equal the simulator's offline loop."""
        rng = random.Random(465486)
        folded = 0
        for k in range(500):
            pts = rand_track(rng)
            pu = rng.random() < 0.15
            cens = rng.random() < 0.3
            bh, grids, cov, _ = make()
            bh.fold_n = 12
            tr = FakeTrack(k + 1, pu)
            for t, x, y in pts:
                bh.on_sample(tr, t, x, y)
                if tr.bh is not None and tr.bh.F is not None:
                    folded += 1
                    self.assertEqual(len(tr.bh.B), 1)
            bh.finish(tr, censored=cens)
            want, walk = ref_fragments(pts, pu, cens)
            got = [(f.cx, f.cy, f.n, f.t0, f.t1, f.open_end, f.open_start) for f in cov.frags]
            self.assertEqual(len(got), len(want), k)
            for g, w in zip(got, want):
                self.assertAlmostEqual(g[0], w[0], places=10)
                self.assertAlmostEqual(g[1], w[1], places=10)
                self.assertEqual(g[2:], w[2:], k)
            for f in cov.frags:
                seg = [q for q in pts if f.t0 <= q[0] <= f.t1]
                n = len(seg)
                cx = sum(q[1] for q in seg) / n
                cy = sum(q[2] for q in seg) / n
                self.assertAlmostEqual(f.sd, math.sqrt(sum((q[1] - cx) ** 2 + (q[2] - cy) ** 2 for q in seg) / n),
                                       places=7)
            self.assertEqual(bh.walk_points_total, walk, k)
        self.assertGreater(folded, 500)

    def test_static_target_keeps_no_samples(self):
        bh, _, cov, _ = make()
        tr = FakeTrack(1)
        rng = random.Random(7)
        for k in range(6 * 3600):                          # 6 h of a target that does not move
            bh.on_sample(tr, T0 + k, 2.0 + rng.gauss(0, 0.004), 3.0 + rng.gauss(0, 0.004))
        tb = tr.bh
        self.assertEqual(len(tb.B), 1)                     # the anchor; the rest is in running sums
        self.assertEqual(tb.F[0], 6 * 3600)
        run = tb.run()                                     # what 'Mark spot here' sees
        self.assertEqual((run[0], run[1], run[4]), (T0, T0 + 6 * 3600 - 1, 6 * 3600))
        self.assertAlmostEqual(run[5], math.hypot(0.004, 0.004), delta=0.0005)
        bh.finish(tr, censored=False)
        f = cov.frags[0]
        self.assertEqual((f.n, f.t0, f.t1), (6 * 3600, T0, T0 + 6 * 3600 - 1))
        self.assertAlmostEqual(f.cx, 2.0, delta=0.001)
        self.assertEqual([g["why"] for g in bh.ghost_events], ["identical"])      # sd 0.0057 < 1 cm: an EPP ghost

    def test_folded_run_breaks_and_starts_over(self):
        bh, _, cov, _ = make()
        bh.fold_n = 20
        tr = FakeTrack(1)
        pts = [(T0 + k, 1.0, 2.0 + 0.01 * (k % 3)) for k in range(100)]
        pts += [(T0 + 100 + k, 3.0, 2.0 + 0.01 * (k % 3)) for k in range(100)]
        for t, x, y in pts:
            bh.on_sample(tr, t, x, y)
        self.assertEqual([(f.n, f.start_index, f.end) for f in cov.frags], [(100, 0, "break")])
        self.assertEqual(tr.bh.B_start, 100)
        bh.finish(tr, censored=False)
        self.assertEqual([(f.n, f.start_index, f.open_start) for f in cov.frags], [(100, 0, True), (100, 100, False)])


def frag(cx, cy, t0, t1, n=None, open_start=True, open_end=True, end="death", death="still", tid=1, sd=0.05):
    if n is None:
        n = int(t1 - t0) + 1
    return Fragment(cx, cy, sd, n, t0, t1, 0, tid, end, open_start, open_end, death)


class StayTest(unittest.TestCase):
    def test_bridge_gap_boundary(self):
        for gap, bridged in ((900, True), (901, False)):
            bh, _, _, _ = make()
            bh.on_fragment(frag(1.0, 2.0, T0, T0 + 60))
            bh.on_fragment(frag(1.0, 2.0, T0 + 60 + gap, T0 + 120 + gap, open_end=False, death="moving"))
            bh.flush(T0 + 10 ** 5)
            self.assertEqual(len(bh.stays), 1 if bridged else 2, gap)
            if bridged:
                st = bh.stays[0]
                self.assertEqual((st["nf"], st["arr"], st["exit"]), (2, "bridge", "moving"))
                self.assertEqual(st["dur"], 120 + gap)

    def test_bridge_distance_boundary(self):
        for d, bridged in ((0.6, True), (0.61, False)):
            bh, _, _, _ = make()
            bh.on_fragment(frag(0.0, 2.0, T0, T0 + 60))
            bh.on_fragment(frag(d, 2.0, T0 + 100, T0 + 160, open_end=False))
            bh.flush(T0 + 10 ** 5)
            self.assertEqual(len(bh.stays), 1 if bridged else 2, d)

    def test_bridge_ld_boundary(self):
        for share, bridged in ((0.9, True), (0.89, False)):
            ld = lambda a, b, s=share: s if b - a < 50000 else 1.0    # ms: the 40 s gap vs the stays
            bh, _, _, _ = make(ld=ld)
            bh.on_fragment(frag(1.0, 2.0, T0, T0 + 60))
            bh.on_fragment(frag(1.0, 2.0, T0 + 100, T0 + 160, open_end=False))
            bh.flush(T0 + 10 ** 5)
            self.assertEqual(len(bh.stays), 1 if bridged else 2, share)

    def test_bridge_blocked_by_censor(self):
        bh, _, _, obs = make()
        obs.censors = [int((T0 + 80) * 1000)]
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 60))
        bh.on_fragment(frag(1.0, 2.0, T0 + 100, T0 + 160, open_end=False))
        bh.flush(T0 + 10 ** 5)
        self.assertEqual(len(bh.stays), 2)

    def test_not_open_start_never_bridges(self):
        bh, _, _, _ = make()
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 60))
        bh.on_fragment(frag(1.0, 2.0, T0 + 100, T0 + 160, open_start=False, open_end=False))
        bh.flush(T0 + 10 ** 5)
        self.assertEqual([s["arr"] for s in bh.stays], ["walk", "static"])

    def test_commit_min_duration(self):
        bh, grids, _, _ = make()
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 19.9, open_end=False))
        self.assertEqual(bh.stays, [])
        self.assertEqual(sum(grids["dwell_slow"]), 0.0)
        bh.on_fragment(frag(1.0, 2.0, T0 + 100, T0 + 120, open_end=False))
        self.assertEqual(len(bh.stays), 1)
        self.assertAlmostEqual(sum(grids["dwell_slow"]), 20 / 60.0, places=5)
        self.assertAlmostEqual(sum(grids["sessions"]), 1.0, places=5)

    def test_ld_off_ghost(self):
        bh, grids, _, _ = make(ld=0.49)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual(bh.stays, [])
        self.assertEqual(bh.ghost_events[0]["why"], "ld_off")
        self.assertAlmostEqual(sum(grids["ghost"]), 10.0, places=4)
        self.assertEqual(sum(grids["clutter"]), 0.0)

    def test_home_empty_clutter(self):
        bh, grids, _, _ = make(he=0.51)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual(bh.stays, [])
        self.assertEqual(bh.ghost_events[0]["why"], "home_empty")
        self.assertAlmostEqual(sum(grids["clutter"]), 10.0, places=4)
        bh, grids, _, _ = make(he=0.5)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual(len(bh.stays), 1)

    def test_exit_kinds(self):
        bh, _, _, _ = make()
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 60, open_end=False, end="death", death="moving"))
        bh.on_fragment(frag(2.0, 2.0, T0, T0 + 60, open_end=False, end="death", death="still"))
        bh.on_fragment(frag(3.0, 2.0, T0, T0 + 60, open_end=False, end="break", death=None))
        bh.on_fragment(frag(-1.0, 2.0, T0, T0 + 60, open_end=False, end="censor", death=None))
        bh.on_fragment(frag(-2.0, 2.0, T0, T0 + 60))
        bh.flush_censored()
        self.assertEqual([s["exit"] for s in bh.stays], ["moving", "still", "moved", "censored", "censored"])

    def test_rounding(self):
        bh, _, _, _ = make(ld=0.97531)
        t0 = T0 + 299.9
        bh.on_fragment(frag(1.23456, 2.34567, t0, t0 + 3544.6, open_end=False, sd=0.0614))
        st = bh.stays[0]
        self.assertEqual(st["t0"] % 300, 0)
        self.assertEqual(st["t0"], int(T0))
        self.assertEqual(st["t1"], int(T0) + 3600)
        self.assertEqual(st["dur"], 3540)
        self.assertEqual((st["x"], st["y"], st["sd"]), (123, 235, 6))
        self.assertEqual(st["ld"], 0.98)
        self.assertEqual(st["day"], "2026-01-05")
        self.assertEqual(list(st), ["t0", "t1", "dur", "x", "y", "sd", "nf", "ld", "he", "exit", "arr", "day"])

    def test_pending_flush_and_hold(self):
        bh, _, _, _ = make()
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 60))
        bh.flush(T0 + 60 + 900)
        self.assertEqual(len(bh.pending), 1)
        bh.hold_times = [(T0 + 500, 1.2, 2.0)]   # a live track born inside the window, close: may still bridge
        bh.flush(T0 + 60 + 901)
        self.assertEqual(len(bh.pending), 1)
        bh.hold_times = [(T0 + 30, 1.2, 2.0)]    # born before the stay ended: cannot bridge it
        bh.flush(T0 + 60 + 901)
        self.assertEqual(len(bh.pending), 0)
        self.assertEqual(len(bh.stays), 1)

    def test_far_track_does_not_hold(self):
        # a static track 3 m away can never bridge (bridge_dist 0.6 m + fragment radius 0.5 m): it must not
        # keep the stay from being committed for as long as it sits
        bh, _, _, _ = make()
        bh.on_fragment(frag(1.0, 3.0, T0, T0 + 300))
        bh.hold_times = [(T0 + 400, -2.0, 4.0)]
        bh.flush(T0 + 300 + 901)
        self.assertEqual((len(bh.pending), len(bh.stays)), (0, 1))
        # ... at the edge of the reach it does
        bh, _, _, _ = make()
        bh.on_fragment(frag(1.0, 3.0, T0, T0 + 300))
        bh.hold_times = [(T0 + 400, 1.0 + 1.1, 3.0)]
        bh.flush(T0 + 300 + 901)
        self.assertEqual((len(bh.pending), len(bh.stays)), (1, 0))
        bh.hold_times = [(T0 + 400, 1.0 + 1.11, 3.0)]
        bh.flush(T0 + 300 + 902)
        self.assertEqual((len(bh.pending), len(bh.stays)), (0, 1))

    def test_ignore_holds_commits(self):
        bh, _, _, _ = make()
        bh.on_fragment(frag(1.0, 3.0, T0, T0 + 300))
        bh.hold_times = [(T0 + 400, 1.0, 3.0)]
        bh.flush(T0 + 300 + 901, ignore_holds=True)
        self.assertEqual((len(bh.pending), len(bh.stays)), (0, 1))

    def test_unknown_ld2412_is_not_absence(self):
        # known absent: ghost evidence; unknown (pw bit1 = 0): the stay is kept, nothing is added to GHOST
        bh, grids, _, _ = make(ld=0.0)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual((len(bh.stays), [g["why"] for g in bh.ghost_events]), (0, ["ld_off"]))
        bh, grids, _, _ = make(ld=None)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual((len(bh.stays), bh.ghost_events), (1, []))
        self.assertEqual(bh.stays[0]["ld"], 0.0)               # gate 3 counts it against the place
        self.assertAlmostEqual(sum(grids["dwell_slow"]), 10.0, places=4)
        self.assertEqual(sum(grids["ghost"]), 0.0)
        # bridging still needs 90% known-present time in the gap
        bh, _, _, _ = make(ld=None)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 60))
        bh.on_fragment(frag(1.0, 2.0, T0 + 100, T0 + 160, open_end=False))
        bh.flush(T0 + 10 ** 5)
        self.assertEqual(len(bh.stays), 2)

    def test_event_lists_are_capped(self):
        import radar_zone_learner.core.behaviours as BM
        bh, _, _, _ = make()
        old = (BM.MAX_STAYS, BM.MAX_DOOR_EVENTS, BM.MAX_GHOST_EVENTS)
        BM.MAX_STAYS, BM.MAX_DOOR_EVENTS, BM.MAX_GHOST_EVENTS = 5, 4, 3
        try:
            for k in range(12):
                bh.on_fragment(frag(1.0, 2.0, T0 + 1000 * k, T0 + 1000 * k + 60, open_end=False))
                bh._door_event(T0 + k * 300, 1.0, 2.0, "b")
                bh._ghost_event(T0 + k * 300, 1.0, 2.0, 400.0, "identical", 0.0, "static")
        finally:
            BM.MAX_STAYS, BM.MAX_DOOR_EVENTS, BM.MAX_GHOST_EVENTS = old
        self.assertEqual((len(bh.stays), len(bh.door_events), len(bh.ghost_events)), (5, 4, 3))
        self.assertEqual([e["t"] for e in bh.ghost_events], [int((T0 + k * 300) // 300) * 300 for k in (9, 10, 11)])
        self.assertEqual(bh.stays[-1]["t0"], int((T0 + 11000) // 300) * 300)


def feed(bh, tr, pts, censored=False):
    for t, x, y in pts:
        bh.on_sample(tr, t, x, y)
    bh.finish(tr, censored=censored)


class ClassesTest(unittest.TestCase):
    def walk_in_sit(self, t0, n_sit=30):
        pts = [(t0 + k, -2.0 + 0.8 * k, 2.0) for k in range(5)]
        pts += [(t0 + 5 + k, 1.2, 2.0) for k in range(n_sit)]
        return pts

    def test_moving_birth_still_vanish(self):
        bh, grids, cov, _ = make()
        feed(bh, FakeTrack(1), self.walk_in_sit(T0))
        self.assertAlmostEqual(sum(grids["birth_moving"]), 1.0, places=5)
        self.assertEqual(sum(grids["death_moving"]), 0.0)
        self.assertAlmostEqual(sum(grids["death_still"]), 1.0, places=5)
        self.assertEqual([e["k"] for e in bh.door_events], ["b"])
        f = cov.frags[-1]
        self.assertFalse(f.open_start)           # moving birth
        self.assertTrue(f.open_end)
        self.assertEqual(bh.pending[0].arr, "walk")

    def test_powerup_and_censored(self):
        bh, grids, cov, _ = make()
        pts = [(T0 + k, -2.0 + 0.8 * k, 2.0) for k in range(5)]
        pts += [(T0 + 5 + k, 1.2, 2.0) for k in range(20)]
        pts += [(T0 + 25 + k, 1.2 + 0.8 * k, 2.0) for k in range(5)]
        feed(bh, FakeTrack(1, powerup=True), pts, censored=True)
        self.assertEqual(sum(grids["birth_moving"]), 0.0)       # power-up birth: no door evidence
        self.assertEqual(sum(grids["death_moving"]), 0.0)       # censored death: nothing
        self.assertEqual(sum(grids["death_still"]), 0.0)
        self.assertEqual(bh.door_events, [])
        bh, grids, _, _ = make()
        feed(bh, FakeTrack(2), pts)
        self.assertEqual([e["k"] for e in bh.door_events], ["b", "d"])
        self.assertAlmostEqual(sum(grids["death_moving"]), 1.0, places=5)
        e = bh.door_events[0]
        self.assertEqual((e["x"], e["y"], e["t"] % 300), (-200, 200, 0))

    def test_static_birth_open_start(self):
        bh, _, cov, _ = make()
        feed(bh, FakeTrack(1), [(T0 + k, 1.0, 2.0 + 0.01 * (k % 2)) for k in range(30)])
        self.assertTrue(cov.frags[0].open_start)
        self.assertEqual(bh.pending[0].arr, "static")

    def test_short_track_no_behaviour(self):
        bh, grids, cov, _ = make()
        feed(bh, FakeTrack(1), [(T0, -2.0, 2.0), (T0 + 1, -1.0, 2.0)])
        self.assertEqual(sum(grids["walk"]), 0.0)
        self.assertEqual(sum(grids["death_moving"]), 0.0)
        self.assertEqual(cov.frags, [])

    def test_epp_ghost(self):
        bh, grids, cov, _ = make()
        rng = random.Random(1)
        feed(bh, FakeTrack(1), [(T0 + k, 2.0 + rng.gauss(0, 0.003), 0.8 + rng.gauss(0, 0.003))
                                for k in range(400)])
        self.assertEqual(len(cov.frags), 1)
        self.assertEqual(bh.stays, [])
        self.assertEqual(bh.pending, [])
        g = bh.ghost_events[0]
        self.assertEqual((g["why"], g["arr"], g["dur"]), ("identical", "static", 400))     # 399 s
        self.assertAlmostEqual(sum(grids["ghost"]), 399 / 60.0, places=4)


if __name__ == "__main__":
    unittest.main()
