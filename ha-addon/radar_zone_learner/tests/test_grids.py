import unittest
from array import array

from radar_zone_learner.core.grids import GRID_NAMES, GridGeom, Grids, decode_array, encode_array, rate, \
    rle_decode, rle_encode
from radar_zone_learner.core.params import DEFAULT_PARAMS as P

G = GridGeom(P)


def zeros():
    return [0.0] * G.nc


class GridsTest(unittest.TestCase):
    def test_geometry(self):
        self.assertEqual((G.nx, G.ny, G.nc), (70, 40, 2800))
        self.assertEqual(G.cell_of(-7.0, 0.0), 0)
        self.assertEqual(G.cell_of(0.0, 0.0), 35)
        self.assertEqual(G.cell_of(6.99, 7.99), 2799)
        self.assertEqual(G.cell_of(7.0, 1.0), -1)
        self.assertEqual(G.cell_of(0.0, -0.01), -1)
        x, y = G.cell_xy(35)
        self.assertAlmostEqual(x, 0.1)
        self.assertAlmostEqual(y, 0.1)
        self.assertEqual(G.n4[71], (70, 72, 1, 141))      # left, right, down, up
        self.assertEqual(len(G.n8[0]), 3)

    def test_kernel(self):
        ks = [k for _, _, k in G.kernel]
        self.assertEqual(len(ks), 25)
        self.assertAlmostEqual(sum(ks), 1.0, places=12)
        d = {(a, b): k for a, b, k in G.kernel}
        for (a, b), k in d.items():
            self.assertAlmostEqual(k, d[(-a, -b)], places=15)
            self.assertAlmostEqual(k, d[(b, a)], places=15)
        self.assertEqual(max(d, key=d.get), (0, 0))

    def test_splat_mass_and_edge(self):
        g = zeros()
        G.splat(g, 0.1, 4.1, 2.0)
        self.assertAlmostEqual(sum(g), 2.0, places=9)
        g = zeros()
        G.splat(g, -6.95, 0.05, 1.0)                     # corner cell: 9 of 25 kernel cells inside
        inside = sum(k for a, b, k in G.kernel if a >= 0 and b >= 0)
        self.assertAlmostEqual(sum(g), inside, places=9)
        self.assertLess(sum(g), 0.6)
        fg = array("f", bytes(4 * G.nc))
        G.splat(fg, 0.1, 4.1, 1.0)
        self.assertAlmostEqual(sum(fg), 1.0, places=5)

    def test_blur_delta(self):
        g = zeros()
        c = 20 * 70 + 30
        g[c] = 16.0
        b = G.blur(g)
        self.assertAlmostEqual(b[c], 4.0)
        self.assertAlmostEqual(b[c + 1], 2.0)
        self.assertAlmostEqual(b[c + 70], 2.0)
        self.assertAlmostEqual(b[c + 71], 1.0)
        self.assertAlmostEqual(b[c + 2], 0.0)
        self.assertAlmostEqual(sum(b), 16.0)
        g = zeros()
        g[0] = 9.0                                      # corner: weights inside 4+2+2+1 = 9
        self.assertAlmostEqual(G.blur(g)[0], 4.0)
        flat = [3.0] * G.nc
        self.assertTrue(all(abs(v - 3.0) < 1e-12 for v in G.blur(flat)))

    def test_decay_half_lives(self):
        gr = Grids(P, G)
        for name in GRID_NAMES:
            gr[name][100] = 8.0
        fs = gr.decay(336.0)
        self.assertAlmostEqual(fs, 0.5)
        self.assertAlmostEqual(gr["dwell_slow"][100], 4.0, places=5)
        self.assertAlmostEqual(gr["ghost"][100], 4.0, places=5)
        self.assertAlmostEqual(gr["dwell_fast"][100], 8.0 * 0.5 ** (336 / 72), places=5)
        self.assertAlmostEqual(gr.eff_s, 336.0)
        gr2 = Grids(P, G)
        gr2["dwell_fast"][5] = 2.0
        gr2.decay(72.0)
        self.assertAlmostEqual(gr2["dwell_fast"][5], 1.0, places=6)
        self.assertAlmostEqual(gr2.eff_f, 72.0)
        gr2.decay(24.0)
        self.assertAlmostEqual(gr2.eff_s, 72.0 * 0.5 ** (24 / 336) + 24.0)
        ref = gr2["walk"]
        gr2.decay(10.0)
        self.assertIs(gr2["walk"], ref)                   # in place

    def test_rate(self):
        self.assertEqual(rate([1.0, 2.0], 48.0), [0.5, 1.0])
        self.assertEqual(rate([0.0], 0.0), [0.0])

    def test_regions_saddle(self):
        r = zeros()
        for c, v in ((1000, 10.0), (1001, 8.0), (1002, 5.0), (1003, 9.0), (1004, 12.0)):
            r[c] = v
        regs, owner = G.regions(r, 0.5, 0.35, 1.05)
        self.assertEqual([pk for pk, _ in regs], [12.0, 10.0])
        self.assertEqual(sorted(regs[0][1]), [1002, 1003, 1004])
        self.assertEqual(sorted(regs[1][1]), [1000, 1001])
        self.assertEqual(owner[1002], 0)

    def test_regions_frac_and_owner(self):
        r = zeros()
        for c, v in ((500, 10.0), (501, 4.0), (502, 3.0)):
            r[c] = v
        regs, owner = G.regions(r, 0.5, 0.35, 1.05)
        self.assertEqual(sorted(regs[0][1]), [500, 501])          # 3.0 < 35 % of 10
        r2 = zeros()
        r2[500] = 7.0                                   # owned by pass 1: skipped
        r2[900] = 6.0
        regs2, _ = G.regions(r2, 0.5, 0.35, 1.05, owner)
        self.assertEqual([sorted(c) for _, c in regs2], [[900]])
        self.assertEqual(G.regions(r, 20.0, 0.35, 1.05)[0], [])

    def test_regions_plateau_and_order(self):
        r = zeros()
        for c in (700, 701, 702, 770, 771, 772):
            r[c] = 2.0
        regs, _ = G.regions(r, 0.5, 0.35, 1.05)
        self.assertEqual(len(regs), 1)
        pk, cells = regs[0]
        self.assertEqual(cells[0], 700)               # ties: the lowest cell first
        self.assertEqual(sorted(cells), [700, 701, 702, 770, 771, 772])
        # LIFO stack, neighbours left, right, down, up: from 700 push 701 then 770; 770 is popped first
        self.assertEqual(cells, [700, 701, 770, 771, 772, 702])

    def test_dilate(self):
        self.assertEqual(len(G.dilate({1000})), 9)
        self.assertEqual(len(G.dilate4({1000}, 3)), 25)
        self.assertEqual(len(G.dilate({0})), 4)

    def test_rle(self):
        cells = {5, 6, 7, 100, 102, 103, 2799}
        runs = rle_encode(cells)
        self.assertEqual(runs, [[5, 3], [100, 1], [102, 2], [2799, 1]])
        self.assertEqual(rle_decode(runs), frozenset(cells))
        self.assertEqual(rle_encode([]), [])

    def test_codec(self):
        a = array("f", [1.5, -2.25, 0.0])
        self.assertEqual(list(decode_array("f", encode_array(a), 3)), [1.5, -2.25, 0.0])
        with self.assertRaises(ValueError):
            decode_array("f", encode_array(a), 4)
        h = array("H", [1, 65535])
        self.assertEqual(list(decode_array("H", encode_array(h), 2)), [1, 65535])

    def test_days_bits(self):
        gr = Grids(P, G)
        gr.days_mark(0.1, 4.1, 739620)
        c = G.cell_of(0.1, 4.1)
        self.assertEqual(gr.days_bits[c], 1)
        gr.days_advance(739622)
        self.assertEqual(gr.days_bits[c], 4)
        gr.days_mark(0.1, 4.1, 739621)
        self.assertEqual(gr.days_bits[c], 6)
        gr.days_advance(739622 + 13)
        self.assertEqual(gr.days_bits[c], 0)             # older than 14 days


if __name__ == "__main__":
    unittest.main()
