import random
import unittest

from radar_zone_learner.core import geometry as GM
from radar_zone_learner.core.grids import GridGeom
from radar_zone_learner.core.params import DEFAULT_PARAMS as P

G = GridGeom(P)


def c(i, j):
    return j * 70 + i


def area2(poly):
    return sum(poly[k][0] * poly[(k + 1) % len(poly)][1] - poly[(k + 1) % len(poly)][0] * poly[k][1]
               for k in range(len(poly)))


class GeometryTest(unittest.TestCase):
    def test_rectangle(self):
        rect = {c(i, j) for i in range(10, 14) for j in range(5, 8)}
        poly = GM.polygon_cm(rect, G)
        self.assertEqual(len(poly), 4)
        self.assertEqual(sorted(map(tuple, poly)), [(-500, 100), (-500, 160), (-420, 100), (-420, 160)])
        self.assertGreater(area2(poly), 0)                 # counter-clockwise
        self.assertEqual(GM.bbox_cm(rect, G), [-500, 100, -420, 160])

    def test_single_cell(self):
        self.assertEqual(len(GM.polygon_cm({c(35, 0)}, G)), 4)

    def test_l_shape(self):
        L = {c(i, j) for i in range(10, 16) for j in range(5, 7)} | {c(i, j) for i in range(10, 12) for j in range(7, 11)}
        poly = GM.polygon_cm(L, G)
        self.assertEqual(len(poly), 6)
        self.assertEqual(area2(poly), 2 * 20 * 400)       # 20 cells of 20 cm x 20 cm, twice the area

    def test_hole_and_touching_corner(self):
        U = {c(11, 11), c(12, 11), c(12, 10), c(12, 9), c(11, 9), c(10, 9), c(10, 10)}
        loops = GM._loops(U, 70, 40)
        self.assertEqual(len(loops), 2)                    # outer boundary and the hole, kept apart
        poly = GM.polygon_cm(U, G)
        self.assertEqual(len(poly), 6)
        self.assertGreater(area2(poly), 0)

    def test_largest_component(self):
        cells = {c(5, 5), c(6, 5), c(20, 20)}
        self.assertEqual(GM.largest_component(cells, G), [c(5, 5), c(6, 5)])
        poly = GM.polygon_cm(cells, G)
        self.assertEqual(len(poly), 4)

    def test_dp_to_12(self):
        rng = random.Random(7)
        for _ in range(20):
            blob = {c(30 + rng.randint(0, 12), 10 + rng.randint(0, 12)) for _ in range(120)}
            poly = GM.polygon_cm(blob, G, 0.1, 12)
            self.assertLessEqual(len(poly), 12)
            self.assertGreaterEqual(len(poly), 3)
        stairs = {c(i, j) for i in range(10, 40) for j in range(5, 5 + (i - 10) // 2 + 1)}
        raw = GM.trace_outline(stairs, G)
        self.assertGreater(len(raw), 12)
        poly = GM.polygon_cm(stairs, G)
        self.assertLessEqual(len(poly), 12)

    def test_iou_overlap(self):
        a = frozenset(range(10))
        b = frozenset(range(5, 20))
        self.assertAlmostEqual(GM.iou(a, b), 5 / 20)
        self.assertAlmostEqual(GM.overlap_coef(a, b), 5 / 10)
        self.assertEqual(GM.iou(a, frozenset()), 0.0)
        self.assertEqual(GM.overlap_coef(frozenset(), b), 0.0)
        self.assertEqual(GM.iou(a, a), 1.0)
        self.assertEqual(GM.overlap_coef(frozenset(range(3)), a), 1.0)


if __name__ == "__main__":
    unittest.main()
