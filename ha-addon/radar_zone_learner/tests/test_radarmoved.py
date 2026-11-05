"""P7 radar-moved detection (spec 4.6, check P7-6 (U) detector parts)."""
import math
import random
import unittest

from radar_zone_learner.core import radarmoved as RM
from radar_zone_learner.core.params import DEFAULT_P7

from .p7_harness import BASE_S, D0, DAY, cover, make, p7on, set_run

CLUSTERS = ((-2.4, 1.2), (1.6, 3.4), (3.0, 6.2))


def rot(p, deg):
    a = math.radians(deg)
    return (p[0] * math.cos(a) - p[1] * math.sin(a), p[0] * math.sin(a) + p[1] * math.cos(a))


def draw(rng, n, clusters=CLUSTERS, sigma=0.12):
    out = []
    for i in range(n):
        cx, cy = clusters[i % len(clusters)]
        out.append((cx + rng.gauss(0, sigma), cy + rng.gauss(0, sigma)))
    return out


def add_events(L, d, pts, kind="door", why="identical"):
    for i, (x, y) in enumerate(pts):
        t = BASE_S + d * DAY + 36000 + i * 7
        if kind == "door":
            L.bh.door_events.append({"t": t, "x": round(x * 100), "y": round(y * 100), "k": "b"})
        else:
            L.bh.ghost_events.append({"t": t, "x": round(x * 100), "y": round(y * 100), "dur": 60, "why": why,
                                      "ld": 1.0, "arr": "static"})


def run(L, k):
    set_run(L, k)
    RM.nightly(L, k)


def base_world(p7=None, ref_days=14, per=24, seed=3):
    """14 covered reference dates with ``per`` door events each, in the radar frame."""
    L = make(p7)
    rng = random.Random(seed)
    for d in range(ref_days):
        cover(L, d)
        add_events(L, d, draw(rng, per))
    return L, rng


def moved_day(L, rng, d, deg=0.0, shift=(0.0, 0.0), per=24):
    cover(L, d)
    pts = [rot(p, deg) for p in draw(rng, per)]
    add_events(L, d, [(x + shift[0], y + shift[1]) for x, y in pts])


class SearchTest(unittest.TestCase):
    def search(self, recent, ref, p7=DEFAULT_P7):
        L = make()
        mask = RM.mask_of(L.geom, ref, p7.rm_tol_rings)
        return RM.search(recent, mask, p7, L.geom)

    def test_a_pure_10_degree_rotation_is_found(self):
        rng = random.Random(1)
        ref = draw(rng, 300)
        recent = [rot(p, 10.0) for p in draw(rng, 40)]
        (dx, dy, deg), hit, hit_id = self.search(recent, ref)
        self.assertLess(deg, 0)                                       # the transform that undoes the turn
        self.assertGreaterEqual(abs(deg), 5.0)                        # equal hits go to the smaller rotation: 7.5 or 10
        self.assertGreaterEqual(hit, 0.7)
        self.assertGreater(hit - hit_id, 0.2)

    def test_a_0_8_m_shift_is_found(self):
        rng = random.Random(2)
        ref = draw(rng, 300)
        recent = [(x + 0.8, y) for x, y in draw(rng, 40)]
        (dx, dy, deg), hit, hit_id = self.search(recent, ref)
        self.assertLess(dx, 0)
        self.assertGreaterEqual(math.hypot(dx, dy), 0.4)
        self.assertGreaterEqual(hit, 0.7)
        self.assertGreater(hit - hit_id, 0.2)

    def test_the_identity_is_best_for_unmoved_data_and_ties_go_to_the_smaller_transform(self):
        rng = random.Random(3)
        ref = draw(rng, 300)
        recent = draw(rng, 40)
        (dx, dy, deg), hit, hit_id = self.search(recent, ref)
        self.assertEqual((dx, dy, deg), (0.0, 0.0, 0.0))
        self.assertEqual(hit, hit_id)
        self.assertGreaterEqual(hit_id, 0.9)

    def test_no_points_gives_zero(self):
        self.assertEqual(self.search([], [(1.0, 1.0)])[1:], (0.0, 0.0))

    def test_the_cost_is_bounded(self):
        import time
        rng = random.Random(4)
        ref = draw(rng, 5000, sigma=0.8)
        recent = draw(rng, 200, sigma=0.8)
        t0 = time.perf_counter()
        self.search(recent, ref)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_the_generic_path_agrees_with_the_fast_one(self):
        import dataclasses
        rng = random.Random(5)
        ref = draw(rng, 300)
        recent = [(x + 0.4, y) for x, y in draw(rng, 40)]
        fast = self.search(recent, ref)
        slow = self.search(recent, ref, dataclasses.replace(DEFAULT_P7, rm_search_step_m=0.2 + 1e-4))
        self.assertEqual(fast[0][2], slow[0][2])
        self.assertAlmostEqual(fast[1], slow[1], delta=0.12)


class PointsTest(unittest.TestCase):
    def test_door_and_static_clutter_events_count_and_ld_off_does_not(self):
        L = make()
        cover(L, 0)
        add_events(L, 0, [(1.0, 2.0)] * 3)
        add_events(L, 0, [(1.0, 2.0)] * 4, kind="ghost", why="identical")
        add_events(L, 0, [(1.0, 2.0)] * 5, kind="ghost", why="home_empty")
        add_events(L, 0, [(1.0, 2.0)] * 6, kind="ghost", why="ld_off")
        pts = RM.points(L, [D0])
        self.assertEqual(len(pts[D0]), 12)
        self.assertEqual(pts[D0][0], (1.0, 2.0))
        self.assertEqual(RM.points(L, [D0 + 1]), {D0 + 1: []})              # a date outside the events

    def test_only_the_requested_dates_are_returned(self):
        L = make()
        add_events(L, 0, [(1.0, 1.0)])
        add_events(L, 5, [(2.0, 2.0)])
        self.assertEqual(sorted(RM.points(L, [D0 + 5])), [D0 + 5])


class NightlyTest(unittest.TestCase):
    def test_an_unmoved_frame_is_not_flagged(self):
        L, rng = base_world()
        moved_day(L, rng, 14)
        run(L, 15)
        rm = L.zone7.rm
        self.assertEqual((rm["st"], rm["open"], rm["rev"]), (0, False, 0))
        self.assertEqual(L.p7_radar_dbg["verdict"], "ok")

    def test_a_second_door_that_is_no_longer_used_is_not_flagged(self):
        L, rng = base_world()
        cover(L, 14)
        add_events(L, 14, draw(rng, 24, clusters=CLUSTERS[:1]))               # only one of the three doors
        run(L, 15)
        self.assertEqual(L.zone7.rm["st"], 0)
        self.assertEqual(L.p7_radar_dbg["verdict"], "ok")                       # the gain over the identity is nil

    def test_two_consecutive_moved_nights_open_the_item(self):
        L, rng = base_world()
        moved_day(L, rng, 14, deg=10.0)
        run(L, 15)
        rm = L.zone7.rm
        self.assertEqual((rm["st"], rm["open"], rm["d0"]), (1, False, D0 + 14))
        self.assertEqual(RM.view(L).state, "watching")
        self.assertTrue(RM.hold(L))                                            # a streak holds the other proposals
        moved_day(L, rng, 15, deg=10.0)
        run(L, 16)
        self.assertEqual((rm["st"], rm["open"], rm["rev"]), (2, True, 1))
        self.assertLessEqual(rm["tx"][2], -50)                                 # tenths of a degree: at least 5 degrees
        v = RM.view(L)
        self.assertEqual((v.state, v.rev, v.streak, v.tx[2]), ("open", 1, 2, rm["tx"][2]))

    def test_a_night_without_a_move_resets_the_streak(self):
        L, rng = base_world()
        moved_day(L, rng, 14, deg=10.0)
        run(L, 15)
        self.assertEqual(L.zone7.rm["st"], 1)
        moved_day(L, rng, 15, deg=0.0)
        run(L, 16)
        rm = L.zone7.rm
        self.assertEqual((rm["st"], rm["d0"], rm["open"]), (0, 0, False))
        self.assertFalse(RM.hold(L))

    def test_a_shift_of_0_8_m_opens_the_item_too(self):
        L, rng = base_world()
        for d in (14, 15):
            moved_day(L, rng, d, shift=(0.8, 0.0))
        run(L, 15)
        run(L, 16)
        self.assertTrue(L.zone7.rm["open"])
        self.assertLessEqual(L.zone7.rm["tx"][0], -40)

    def test_a_rotation_of_3_degrees_is_below_the_effective_limit(self):
        L, rng = base_world()
        for d in (14, 15):
            moved_day(L, rng, d, deg=3.0)
            run(L, d + 1)
        self.assertEqual((L.zone7.rm["st"], L.zone7.rm["open"]), (0, False))

    def test_insufficient_data_holds_the_streak(self):
        L, rng = base_world()
        L.zone7.rm.update(st=1, d0=D0 + 13)
        for d in (14, 15, 16):
            cover(L, d)
            add_events(L, d, draw(rng, 3))                                      # 9 points in all: under rm_min_recent
        run(L, 17)
        self.assertEqual((L.zone7.rm["st"], L.p7_radar_dbg["verdict"]), (1, "insufficient"))
        L2 = make()                                                             # too little reference
        rng2 = random.Random(1)
        for d in range(3):
            cover(L2, d)
            add_events(L2, d, draw(rng2, 4))
        moved_day(L2, rng2, 3, deg=10.0)
        run(L2, 4)
        self.assertEqual(L2.p7_radar_dbg["why"], "reference")
        self.assertEqual(L2.zone7.rm["st"], 0)

    def test_recent_grows_to_two_or_three_dates_when_one_has_too_few_points(self):
        L, rng = base_world()
        cover(L, 14)
        add_events(L, 14, [rot(p, 10.0) for p in draw(rng, 6)])
        cover(L, 15)
        add_events(L, 15, [rot(p, 10.0) for p in draw(rng, 6)])
        run(L, 16)
        self.assertEqual(L.p7_radar_dbg["k"], 2)
        self.assertEqual(L.zone7.rm["d0"], D0 + 14)                             # the oldest date of the recent set

    def test_a_covered_date_is_evaluated_once_when_the_run_repeats(self):
        L, rng = base_world()
        moved_day(L, rng, 14, deg=10.0)
        run(L, 15)
        run(L, 15)
        run(L, 15)
        self.assertEqual(L.zone7.rm["st"], 1)
        run(L, 16)                                                              # day 15 is not covered: nothing new
        self.assertEqual(L.zone7.rm["st"], 1)

    def test_the_item_is_latched_and_the_reference_excludes_the_moved_dates(self):
        L, rng = base_world()
        moved_day(L, rng, 14, deg=10.0)
        run(L, 15)
        moved_day(L, rng, 15, deg=10.0)
        run(L, 16)
        self.assertEqual(L.p7_radar_dbg["ref"], 12 * 24)                         # the moved day 14 is not in the reference
        rm = L.zone7.rm
        self.assertTrue(rm["open"])
        # the moved dates keep coming: with no answer the item stays, whatever the hit rate of the identity becomes
        for d in range(16, 34):
            moved_day(L, rng, d, deg=10.0)
            run(L, d + 1)
        self.assertTrue(rm["open"])
        self.assertEqual((rm["rev"], rm["st"]), (1, 2))
        # even if the data would now be unmoved against a reference of moved dates, an open item is never re-evaluated
        dbg = dict(L.p7_radar_dbg)
        moved_day(L, rng, 34, deg=0.0)
        run(L, 35)
        self.assertTrue(rm["open"])
        self.assertEqual(L.p7_radar_dbg, dbg)

    def test_an_unexcluded_reference_would_not_find_the_move_any_more(self):
        # the reason for the exclusion: once the moved dates are most of d14 the plain reference is the new frame
        L, rng = base_world()
        for d in range(14, 27):
            moved_day(L, rng, d, deg=10.0)
        set_run(L, 27)
        d14 = L.obs.covered_dates(L.last_run_ord)[-14:]
        pts = RM.points(L, d14)
        recent = [q for q in pts[d14[-1]]]
        ref = [q for d in d14[:-1] for q in pts[d]]
        _, hit, hit_id = RM.search(recent, RM.mask_of(L.geom, ref, 1), DEFAULT_P7, L.geom)
        self.assertGreaterEqual(hit_id, 0.9)                                    # the identity fits the new frame now

    def test_the_hold_lasts_rm_hold_days_after_the_first_moved_date_then_ends_while_the_item_stays(self):
        L, rng = base_world()
        for d in (14, 15):
            moved_day(L, rng, d, deg=10.0)
            run(L, d + 1)
        rm = L.zone7.rm
        self.assertTrue(rm["open"])
        set_run(L, 14 + 14)
        self.assertTrue(RM.hold(L))
        set_run(L, 14 + 15)
        self.assertFalse(RM.hold(L))
        self.assertTrue(rm["open"])

    def test_a_repeated_detection_after_an_ignore_gets_the_next_revision_after_the_quiet_time(self):
        L, rng = base_world()
        for d in (14, 15):
            moved_day(L, rng, d, deg=10.0)
            run(L, d + 1)
        rm = L.zone7.rm
        self.assertEqual(rm["rev"], 1)
        # Ignore (what the review handler writes)
        rm.update(ign=1, open=False, st=0, d0=0, qt=L.last_run_ord + 14)
        self.assertEqual(RM.view(L).state, "ignored")
        for d in range(16, 22):                                                 # quiet: the detector does not evaluate
            moved_day(L, rng, d, deg=10.0)
            run(L, d + 1)
            self.assertEqual((rm["st"], rm["open"]), (0, False))
        # a new frame (10 degrees more) is a new moved period; the quiet time must be over first
        for d in range(22, 40):
            moved_day(L, rng, d, deg=20.0 if d >= 30 else 10.0)
            run(L, d + 1)
        self.assertEqual((rm["open"], rm["rev"]), (True, 2))                    # a new detection: the next revision
        self.assertEqual(rm["ign"], 1)                                          # the ignored revision is not the open one
        self.assertEqual(RM.view(L).state, "open")

    def test_the_view_reports_none_when_p7_is_disabled(self):
        L = make(DEFAULT_P7)
        self.assertEqual(L.radar_moved_state().state, "none")

    def test_state_names(self):
        base = {"open": False, "st": 0, "ign": 0, "rev": 0}
        self.assertEqual(RM.state_name(base), "none")
        self.assertEqual(RM.state_name(dict(base, st=1)), "watching")
        self.assertEqual(RM.state_name(dict(base, open=True, st=2, rev=1)), "open")
        self.assertEqual(RM.state_name(dict(base, ign=1, rev=1)), "ignored")
        self.assertEqual(RM.state_name(dict(base, ign=1, rev=2)), "none")


if __name__ == "__main__":
    unittest.main()
