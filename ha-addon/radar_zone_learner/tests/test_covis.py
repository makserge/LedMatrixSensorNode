import dataclasses
import unittest

from radar_zone_learner.core.covis import Covis
from radar_zone_learner.core.grids import GridGeom
from radar_zone_learner.core.params import DEFAULT_PARAMS as P

G = GridGeom(P)
T0 = 1767571200.0


class F:
    def __init__(self, t0, t1, cx, cy, tid):
        self.t0, self.t1, self.cx, self.cy, self.track_id = t0, t1, cx, cy, tid


class CovisTest(unittest.TestCase):
    def test_grid(self):
        c = Covis(P)
        self.assertEqual((c.nx4, c.ny4, c.nc4), (35, 20, 700))
        self.assertEqual(c.c4(-7.0, 0.0), 0)
        self.assertEqual(c.c4(6.9, 7.9), 699)
        self.assertEqual(c.c4(7.1, 1.0), -1)

    def test_overlap_pairs_symmetric(self):
        c = Covis(P)
        c.add(F(T0, T0 + 100, 2.5, 2.2, 1))
        c.add(F(T0 + 40, T0 + 200, 3.5, 2.2, 2))
        a, b = c.c4(2.5, 2.2), c.c4(3.5, 2.2)
        self.assertEqual(c.pairs[a * 700 + b], 60.0)
        self.assertEqual(c.pairs[b * 700 + a], 60.0)
        self.assertEqual(c.fragsec[a], 100.0)
        self.assertEqual(c.fragsec[b], 160.0)
        c.add(F(T0 + 300, T0 + 400, 3.5, 2.2, 3))         # no overlap
        self.assertEqual(len(c.pairs), 2)

    def test_same_track_nothing(self):
        c = Covis(P)
        c.add(F(T0, T0 + 100, 2.5, 2.2, 1))
        c.add(F(T0 + 40, T0 + 200, 3.5, 2.2, 1))
        self.assertEqual(c.pairs, {})

    def test_same_cell_counts_twice(self):
        c = Covis(P)
        c.add(F(T0, T0 + 10, 2.5, 2.2, 1))
        c.add(F(T0, T0 + 10, 2.5, 2.25, 2))
        a = c.c4(2.5, 2.2)
        self.assertEqual(c.pairs, {a * 700 + a: 20.0})

    def test_ram_list_24h(self):
        c = Covis(P)
        c.add(F(T0, T0 + 100, 2.5, 2.2, 1))
        c.add(F(T0 + 90000, T0 + 90100, 3.5, 2.2, 2))
        self.assertEqual(len(c.recent), 1)                # the first one is older than 24 h
        c.add(F(T0 + 50, T0 + 90050, 0.0, 2.2, 3))        # would overlap the pruned one
        self.assertEqual(sum(c.pairs.values()), 2 * 50.0)

    def test_decay_prune_cap(self):
        c = Covis(P)
        c.pairs = {1: 4.0, 2: 1.5, 3: 0.5}
        c.fragsec[0] = 10.0
        c.decay(0.5)
        self.assertEqual(c.pairs, {1: 2.0})
        self.assertEqual(c.fragsec[0], 5.0)
        p2 = dataclasses.replace(P, covis_cap=3)
        c = Covis(p2)
        c.pairs = {10: 5.0, 11: 2.0, 12: 2.0, 13: 9.0, 14: 3.0}
        c.decay(1.0)
        self.assertEqual(c.pairs, {10: 5.0, 13: 9.0, 14: 3.0})
        c = Covis(p2)
        c.pairs = {10: 5.0, 11: 2.0, 12: 2.0, 13: 9.0}
        c.decay(1.0)
        self.assertEqual(c.pairs, {10: 5.0, 12: 2.0, 13: 9.0})      # ties: the lower key goes first

    def test_query_and_shared_cells(self):
        c = Covis(P)
        a, b, s = c.c4(2.5, 2.2), c.c4(3.7, 2.2), c.c4(3.1, 2.2)
        c.fragsec[a] = 100.0
        c.fragsec[s] = 50.0
        c.pairs[a * 700 + b] = 90.0
        c.pairs[a * 700 + s] = 1000.0                    # a shared cell is no source cell: left out
        c.pairs[s * 700 + b] = 30.0                      # a fragment in a shared cell belongs to C (SIM:762-769)
        A = frozenset([a, s])
        B = frozenset([b, s])
        self.assertEqual(c.frag_seconds(A), 150.0)
        self.assertAlmostEqual(c.covis(A, B), (90.0 + 30.0) / 150.0)
        self.assertEqual(c.covis(frozenset([s]), frozenset([s])), 0.0)    # no source cell outside C
        self.assertEqual(c.covis(frozenset([c.c4(-5, 1)]), B), 0.0)       # no fragment seconds
        c.pairs[a * 700 + b] = 9000.0
        self.assertEqual(c.covis(A, B), 1.0)                              # clamped

    def test_concurrent_source_tracks_in_one_cell_count_once(self):
        c = Covis(P)
        c.add(F(T0, T0 + 100, 3.5, 2.2, 1))               # two people in the denser place, one 0.4 m cell
        c.add(F(T0, T0 + 100, 3.5, 2.25, 2))
        c.add(F(T0 + 20, T0 + 60, 2.5, 2.2, 3))           # the candidate seat: 40 s, all of it concurrent
        a, b = c.c4(2.5, 2.2), c.c4(3.5, 2.2)
        self.assertEqual(c.pairs[a * 700 + b], 40.0)      # not 80
        self.assertEqual(c.pairs[b * 700 + a], 80.0)      # each of the two source fragments saw 40 s of it
        self.assertEqual(c.covis(frozenset([a]), frozenset([b])), 1.0)
        # a second candidate fragment over the same stretch adds its own seconds, but the source fragments
        # are not credited twice for them
        c.add(F(T0 + 20, T0 + 60, 2.5, 2.25, 4))
        self.assertEqual(c.pairs[a * 700 + b], 80.0)
        self.assertEqual(c.pairs[b * 700 + a], 80.0)
        self.assertEqual(c.covis(frozenset([a]), frozenset([b])), 1.0)

    def test_overlaps_of_one_fragment_with_two_fragments_of_a_cell_are_a_union(self):
        c = Covis(P)
        c.add(F(T0, T0 + 50, 3.5, 2.2, 1))                # source, first half
        c.add(F(T0 + 30, T0 + 100, 3.5, 2.2, 2))          # source, second half, overlapping the first
        c.add(F(T0 + 10, T0 + 90, 2.5, 2.2, 3))           # candidate: 10..90 is covered by 0..50 and 30..100
        a, b = c.c4(2.5, 2.2), c.c4(3.5, 2.2)
        self.assertEqual(c.pairs[a * 700 + b], 80.0)      # 40 + 60 would be 100
        self.assertEqual(c.pairs[b * 700 + a], 40.0 + 60.0)

    def test_two_source_cells_credit_each_their_share(self):
        c = Covis(P)
        c.add(F(T0, T0 + 100, 3.5, 2.2, 1))
        c.add(F(T0, T0 + 100, 4.5, 2.2, 2))               # another cell: the pair matrix cannot unite them
        c.add(F(T0, T0 + 100, 2.5, 2.2, 3))
        a, b1, b2 = c.c4(2.5, 2.2), c.c4(3.5, 2.2), c.c4(4.5, 2.2)
        self.assertEqual((c.pairs[a * 700 + b1], c.pairs[a * 700 + b2]), (100.0, 100.0))
        self.assertEqual(c.covis(frozenset([a]), frozenset([b1, b2])), 1.0)    # clamped, never above 1

    def test_cells4(self):
        c = Covis(P)
        cells = {G.cell_of(2.45, 2.15), G.cell_of(2.65, 2.15)}
        self.assertEqual(c.cells4(cells, G), frozenset([c.c4(2.45, 2.15), c.c4(2.65, 2.15)]))

    def test_codec(self):
        c = Covis(P)
        c.add(F(T0, T0 + 100, 2.5, 2.2, 1))
        c.add(F(T0 + 40, T0 + 200, 3.5, 2.2, 2))
        d = c.export()
        c2 = Covis(P)
        c2.load(d)
        self.assertEqual(c2.pairs, c.pairs)
        self.assertEqual(c2.fragsec, c.fragsec)
        self.assertEqual(c2.export(), d)


if __name__ == "__main__":
    unittest.main()
