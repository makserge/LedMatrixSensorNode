"""Ground truth (P3 spec 11.6, 13.4)."""
import math
import unittest
from types import SimpleNamespace

from replay import truth as T
from replay import world as W

DAY = W.DAY

# The simulator's own truth counters (truth_cut_frac, truth_occ_h) of three published option-A runs, 56 days
# (optionA/docwriter/sim/runs/bed_w2200_0600_nl0_1.json, liv_w2200_0600_nl0_1.json,
# optionA/w0006/sim/runs/bed_w0000_0600_nl0_1.json). The design's Section 8 means come from such runs
# (sofa seat A 17.4% and counter 18.7-19.2% inside 22:00-06:00; bed 72.9% inside 00:00-06:00).
PUBLISHED = [
    (1, "22:00-06:00", 1,
     {"bed": 0.832, "desk": 0.004, "counter": 0.199, "table": 0.0, "sofaA": 0.141, "sofaB": 0.027, "armchair": 0.0},
     {"bed": 448.8, "desk": 187.3, "counter": 11.0, "table": 79.7, "sofaA": 62.7, "sofaB": 85.8, "armchair": 9.6}),
    (0, "22:00-06:00", 1,
     {"desk": 0.003, "counter": 0.197, "table": 0.0, "sofaA": 0.142, "sofaB": 0.059, "armchair": 0.0},
     {"desk": 178.5, "counter": 11.5, "table": 76.4, "sofaA": 78.7, "sofaB": 100.4, "armchair": 9.0}),
    (1, "00:00-06:00", 1,
     {"bed": 0.735, "desk": 0.0, "counter": 0.0, "table": 0.0, "sofaA": 0.0, "sofaB": 0.0, "armchair": 0.0},
     {"bed": 448.8, "desk": 187.3, "counter": 11.0, "table": 79.7, "sofaA": 62.7, "sofaB": 85.8, "armchair": 9.6}),
]


def day(d, sessions):
    return SimpleNamespace(d=d, sessions=sessions)


def dense_region(points):
    """Reference: the simulator's dense splat and blur on the full grid, then the 35% component."""
    g = [0.0] * W.NC
    for x, y, w in points:
        i0 = int(math.floor((x - W.GX0) / W.CELL))
        j0 = int(math.floor((y - W.GY0) / W.CELL))
        for di, dj, k in T.KER:
            i, j = i0 + di, j0 + dj
            if 0 <= i < W.NX and 0 <= j < W.NY:
                g[j * W.NX + i] += w * k
    out = [0.0] * W.NC
    wts = (1, 2, 1)
    for j in range(W.NY):
        for i in range(W.NX):
            s = 0.0
            ws = 0
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < W.NX and 0 <= jj < W.NY:
                        k = wts[di + 1] * wts[dj + 1]
                        s += g[jj * W.NX + ii] * k
                        ws += k
            out[j * W.NX + i] = s / ws
    mx = max(out)
    top = out.index(mx)
    reg = {top}
    st = [top]
    while st:
        c = st.pop()
        for n in T.nbrs4(c):
            if n not in reg and out[n] >= 0.35 * mx:
                reg.add(n)
                st.append(n)
    return frozenset(reg), top


class TruthTest(unittest.TestCase):
    def test_one_session_gives_the_kernel_blob(self):
        tr = T.Truth(W.make_cfg(bed=0))
        x, y = 2.53, 2.21
        tr.add_day(day(0, [("P1", "desk", "sit", 9 * 3600, 11 * 3600, x, y)]))
        cells, mx = tr.region("desk", 1)
        ref_cells, ref_mx = dense_region([(x, y, 120.0)])
        self.assertEqual(cells, ref_cells)
        self.assertEqual(mx, ref_mx)
        self.assertEqual(mx, W.cell_of(x, y))
        self.assertTrue(5 <= len(cells) <= 25, len(cells))
        # near the grid edge the sparse blur keeps the simulator's normalisation
        tr2 = T.Truth(W.make_cfg(bed=0))
        tr2.add_day(day(0, [("P1", "desk", "sit", 0, 600, -6.95, 0.05)]))
        self.assertEqual(tr2.region("desk", 1), dense_region([(-6.95, 0.05, 10.0)]))

    def test_sofa_ab_covers_both_seats(self):
        tr = T.Truth(W.make_cfg(bed=0))
        ses = [("P1", "sofaA", "sit", 20 * 3600, 21 * 3600, -2.5, 3.2),
               ("P2", "sofaB", "sit", 20 * 3600, 21 * 3600, -2.5, 3.8)]
        tr.add_day(day(0, ses))
        ab, _ = tr.region("sofaAB", 1)
        a_cells, a_max = tr.region("sofaA", 1)
        b_cells, b_max = tr.region("sofaB", 1)
        self.assertIn(a_max, ab)
        self.assertIn(b_max, ab)
        self.assertGreater(len(ab), max(len(a_cells), len(b_cells)))

    def test_cut_half_inside_the_window(self):
        tr = T.Truth(W.make_cfg(bed=0, nw="22:00-06:00"))
        tr.add_day(day(1, [("P1", "sofaA", "sit", DAY + 21 * 3600, DAY + 23 * 3600, -2.5, 3.2)]))
        self.assertAlmostEqual(tr.cut_shares([1])["sofaA"], 0.5)
        self.assertIsNone(tr.cut_shares([1])["desk"])

    def test_core_place_rule_and_residents_only(self):
        tr = T.Truth(W.make_cfg(bed=0))
        for d in range(10):
            ses = []
            if d < 5:
                ses.append(("P1", "desk", "sit", d * DAY + 9 * 3600, d * DAY + 10 * 3600, 2.55, 2.2))
            if d < 4:
                ses.append(("P2", "armchair", "sit", d * DAY + 18 * 3600, d * DAY + 19 * 3600, -1.0, 1.4))
            ses.append(("G", "table", "sit", d * DAY + 19 * 3600, d * DAY + 20 * 3600, 0.3, 4.4))
            tr.add_day(day(d, ses))
        core = tr.core_places(range(10))
        self.assertIn("desk", core)              # 5 of 10 days
        self.assertNotIn("armchair", core)       # 4 of 10 days
        self.assertNotIn("table", core)          # a guest's sessions do not count
        self.assertEqual(tr.sit_seconds(range(10)), 5 * 3600 + 4 * 3600)

    def test_fourteen_day_window(self):
        tr = T.Truth(W.make_cfg(bed=0))
        tr.add_day(day(0, [("P1", "desk", "sit", 9 * 3600, 10 * 3600, 2.55, 2.2)]))
        for d in range(1, 16):
            tr.add_day(day(d, [("P1", "table", "sit", d * DAY + 19 * 3600, d * DAY + 20 * 3600, 0.3, 4.4)]))
        self.assertTrue(tr.region("desk", 14)[0])
        self.assertEqual(tr.region("desk", 15), (frozenset(), None))

    def test_rle_and_iou(self):
        cells = {5, 6, 7, 70, 71, 140}
        runs = T.rle(cells)
        self.assertEqual(runs, [[5, 3], [70, 2], [140, 1]])
        self.assertEqual(T.unrle(runs), frozenset(cells))
        self.assertAlmostEqual(T.iou({1, 2, 3}, {2, 3, 4}), 0.5)
        self.assertEqual(T.iou(set(), {1}), 0.0)

    def test_matches_the_simulator_truth_counters(self):
        for bed, nw, seed, cut, occ in PUBLISHED:
            cfg = W.make_cfg(bed=bed, nw=nw)
            rw = W.RunWorld(seed, cfg)
            tr = T.Truth(cfg)
            rw.on_day.append(tr.add_day)
            rw.ensure(55)
            got_cut = {k: round(v, 3) for k, v in tr.cut_shares(range(56)).items() if v is not None}
            got_occ = {k: round(v, 1) for k, v in tr.occupancy_h(range(56)).items()}
            self.assertEqual(got_cut, cut, (bed, nw))
            self.assertEqual(got_occ, occ, (bed, nw))
            # and the places of design Section 8.1 are the core places
            self.assertEqual(set(tr.core_places(range(56))),
                             {"desk", "table", "counter", "sofaA", "sofaB"} | ({"bed"} if bed else set()))


if __name__ == "__main__":
    unittest.main()
