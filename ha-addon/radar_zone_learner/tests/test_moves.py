"""P7 moves: the modify detector with succession, the own-suggestion source, the merge detector, ordering, `plan` and the
flag matrix (spec 4.3, checks P7-1 (i) (j), P7-2 (U), P7-10)."""
import dataclasses
import math
import unittest

from radar_zone_learner.core import drift, moves
from radar_zone_learner.core import zonework
from radar_zone_learner.core.params import P5Params

from .p7_harness import (D0, add_stay, add_suggestion, add_zone, cover, evidence, make, p7on, prime_own, set_run,
                         use_days)

Z = (2.0, 4.0)
N = (3.1, 4.0)                      # 1.1 m to the right of Z


def night(L, k):
    set_run(L, k)
    drift.nightly(L, k)


def run_nights(L, a, b):
    for k in range(a, b + 1):
        night(L, k)


def sofa_scene(new=N, old=Z, p7=None, post=17, ev_old=None, ev_new=None, new_hours=(9, 10, 11), new_sid="s0020-1"):
    """A zone at ``old`` used on days 0-13, then not at all; a pending place at ``new`` used on days 14-30. The nightly
    drift step has run on every night from 14 on, so the zone is `collapsed` with a last use."""
    L = make(p7)
    rec = add_zone(L, old, ev=ev_old)
    use_days(L, range(14), old)
    use_days(L, range(14, 14 + post), new, hours=new_hours)
    run_nights(L, 14, 14 + post)
    s = add_suggestion(L, new, sid=new_sid, first=20, ev=ev_new)
    return L, rec, s


class ModifyTest(unittest.TestCase):
    def test_a_moved_sofa_is_a_modify_not_a_new_place(self):
        L, rec, s = sofa_scene()
        self.assertEqual(L.zone7.entry("z0001")["vs"], "collapsed")
        self.assertGreater(L.zone7.entry("z0001")["lu"], 0)
        merges, mods, rets, consumed = moves.plan(L, [s])
        self.assertEqual((merges, rets), ([], []))
        self.assertEqual(len(mods), 1)
        m = mods[0]
        self.assertEqual((m["sid"], m["zid"], m["zname"], m["src"]), ("s0020-1", "z0001", "Desk", "cand"))
        self.assertEqual((m["shift_cm"], m["dir"]), (110, "to the right"))
        self.assertGreaterEqual(m["r"], 0.99)
        self.assertEqual(consumed, {"s0020-1"})
        self.assertEqual(m["rev"], 1)

    def test_a_far_move_with_no_use_after_the_last_use_passes_succession(self):
        # post=10: the last use (day 13) is still among the 14 retained covered dates (replay P7-8 fix, see test_p7_fixes)
        L, rec, s = sofa_scene(new=(5.0, 4.0), post=10)             # 3.0 m: beyond succ_far_m
        _, mods, _, consumed = moves.plan(L, [s])
        self.assertEqual([(m["sid"], m["shift_cm"]) for m in mods], [("s0020-1", 300)])

    def test_succession_a_far_candidate_that_was_in_use_before_the_zone_stopped_is_not_a_move(self):
        L = make()
        add_zone(L, Z)
        use_days(L, range(14), Z)
        use_days(L, range(0, 20), (5.0, 4.0))                       # another seat, used all along
        run_nights(L, 14, 20)
        self.assertEqual(L.zone7.entry("z0001")["vs"], "falling")
        s = add_suggestion(L, (5.0, 4.0), sid="s0020-1", first=20)
        _, mods, _, consumed = moves.plan(L, [s])
        self.assertEqual((mods, consumed), ([], set()))             # 8 of its 14 dates predate the last use of the zone
        # the same candidate close to the zone (under 1.5 m) needs no succession
        L2 = make()
        add_zone(L2, Z)
        use_days(L2, range(14), Z)
        use_days(L2, range(0, 20), N)
        run_nights(L2, 14, 20)
        s2 = add_suggestion(L2, N, sid="s0020-1", first=20)
        self.assertEqual(len(moves.plan(L2, [s2])[1]), 1)

    def test_succession_needs_a_recorded_last_use(self):
        L, rec, s = sofa_scene(new=(5.0, 4.0))
        L.zone7.entry("z0001")["lu"] = 0
        self.assertEqual(moves.plan(L, [s])[1], [])                 # a collapsed zone with lu 0 has no modify
        L.zone7.entry("z0001")["vs"] = "falling"                    # ... and a falling one fails the succession rule
        self.assertEqual(moves.plan(L, [s])[1], [])

    def test_i_collapsed_needs_a_use_and_the_distance_window(self):
        L, rec, s = sofa_scene()
        self.assertEqual(len(moves.plan(L, [s])[1]), 1)
        for dist, expect in ((0.4, 0), (0.5, 1), (1.4, 1), (4.0, 1), (4.1, 0)):
            L2 = make()
            add_zone(L2, Z)
            use_days(L2, range(14), Z)
            pos = (Z[0] + dist, Z[1])
            use_days(L2, range(14, 26), pos)                        # the last use (day 13) stays among the retained dates
            run_nights(L2, 14, 26)
            s2 = add_suggestion(L2, pos, sid="s0020-1", first=20)
            self.assertEqual(len(moves.plan(L2, [s2])[1]), expect, dist)

    def test_the_hour_profile_must_correlate(self):
        L, rec, s = sofa_scene(new_hours=(20, 21, 22), ev_new=evidence(hours=(20, 21, 22)))
        self.assertEqual(moves.plan(L, [s])[1], [])
        L2, _, s2 = sofa_scene(ev_new=evidence(hours=(9, 10, 11, 14)))      # r about 0.87: still above 0.6
        self.assertEqual(len(moves.plan(L2, [s2])[1]), 1)

    def test_a_falling_zone_needs_min_slow_stays_and_a_collapsed_one_a_last_use(self):
        # j: a falling zone with 2 stays in the slow window gives no modify, one with 4 does
        for n_stays, expect in ((2, 0), (4, 1)):
            L = make()
            add_zone(L, Z)
            for d in range(14):
                cover(L, d)
            for d in (3, 8, 11, 12):                                 # a zone used on rare days
                for h in (9, 10, 11):
                    pass
            used_days = (11, 12, 13, 13)[:n_stays]
            # one stay per used day, but two stays on the last day when n_stays = 4
            add_stay(L, 11, 10, 3000, *Z)
            add_stay(L, 12, 10, 3000, *Z)
            if n_stays == 4:
                add_stay(L, 13, 9, 3000, *Z)
                add_stay(L, 13, 10, 3000, *Z)
            for d in range(14, 17):
                cover(L, d)
            run_nights(L, 14, 17)
            self.assertEqual(L.zone7.entry("z0001")["vs"], "falling", n_stays)
            s = add_suggestion(L, N, sid="s0020-1", first=20, ev=evidence(hours=(10,)))
            use_days(L, range(14, 17), N, hours=(10,))
            self.assertEqual(len(moves.plan(L, [s])[1]), expect, n_stays)
        # collapsed with lu = 0 gives none (see test_succession_needs_a_recorded_last_use), with lu > 0 one
        L, rec, s = sofa_scene()
        L.zone7.entry("z0001")["lu"] = 0
        self.assertEqual(moves.plan(L, [s])[1], [])

    def test_zones_that_may_not_move(self):
        # bed, frozen (static), young, blind, none give nothing
        for case in ("bed", "frozen", "young", "blind", "none", "ok"):
            L, rec, s = sofa_scene()
            e = L.zone7.entry("z0001")
            if case == "bed":
                rec["kind"] = "bed"
            elif case == "frozen":
                e["fz"] = True
            elif case == "young":
                e["vs"] = "young"
            elif case == "blind":
                e["vs"], e["fz"] = "blind", True
            elif case == "none":
                e["vs"] = "none"
            elif case == "ok":
                e["vs"] = "ok"
            self.assertEqual(moves.plan(L, [s])[1], [], case)
        L, rec, s = sofa_scene()
        L.window["sched"] = True                                       # the static predicate: a bed only; a desk is not frozen
        self.assertEqual(len(moves.plan(L, [s])[1]), 1)

    def test_the_candidate_is_the_one_with_the_highest_r_then_the_nearer_then_the_lower_id(self):
        L, rec, s = sofa_scene()
        s2 = add_suggestion(L, (2.0, 5.6), sid="s0021-1", first=20, ev=evidence(hours=(9, 10, 11)))   # 1.6 m, same r
        use_days(L, range(14, 31), (2.0, 5.6))
        s3 = add_suggestion(L, (3.1, 5.4), sid="s0019-1", first=20, ev=evidence(hours=(9, 10)))       # r < 1
        _, mods, _, consumed = moves.plan(L, [s3, s2, s])
        self.assertEqual([(m["sid"]) for m in mods], ["s0020-1"])       # r equal (1.0) with s2: the nearer wins
        self.assertEqual(consumed, {"s0020-1"})

    def test_each_suggestion_belongs_to_at_most_one_zone(self):
        L = make()
        add_zone(L, Z, name="A", sid="s0001-1")
        add_zone(L, (2.0, 2.4), name="B", sid="s0002-1")
        for pos in (Z, (2.0, 2.4)):
            use_days(L, range(14), pos)
        use_days(L, range(14, 40), (2.8, 3.2))                          # between the two, about 1 m from both
        run_nights(L, 14, 40)
        s = add_suggestion(L, (2.8, 3.2), sid="s0020-1", first=20)
        _, mods, _, consumed = moves.plan(L, [s])
        self.assertEqual(len(mods), 1)
        self.assertEqual(consumed, {"s0020-1"})

    def test_the_revision_is_a_counter_bumped_when_the_candidate_changes_and_never_reset(self):
        L, rec, s = sofa_scene()
        e = L.zone7.entry("z0001")
        self.assertEqual(moves.plan(L, [s])[1][0]["rev"], 1)
        self.assertEqual((e["mr"], e["ms"]), (1, "s0020-1#1"))
        self.assertEqual(moves.plan(L, [s])[1][0]["rev"], 1)            # the same (sid, rev): the same item revision
        s["rev"] = 2                                                    # the extraction updated the candidate
        self.assertEqual(moves.plan(L, [s])[1][0]["rev"], 2)
        e["ms"] = ""                                                    # an answer clears ms only: mr is never reset
        self.assertEqual(moves.plan(L, [s])[1][0]["rev"], 3)
        self.assertEqual(e["mr"], 3)

    def test_nothing_is_proposed_while_the_radar_hold_is_in_force(self):
        from radar_zone_learner.core import radarmoved
        L, rec, s = sofa_scene()
        real = radarmoved.hold
        radarmoved.hold = lambda L_: True
        try:
            self.assertEqual(moves.plan(L, [s]), ([], [], [], set()))
        finally:
            radarmoved.hold = real
        self.assertEqual(len(moves.plan(L, [s])[1]), 1)


class RcZoneTest(unittest.TestCase):
    def rc_scene(self, dist):
        L = make()
        add_zone(L, Z)
        use_days(L, range(14), Z)
        run_nights(L, 14, 14)
        e = L.zone7.entry("z0001")
        e.update(rc=True, rd=D0 + 14)
        pos = (Z[0] + dist, Z[1])
        s = add_suggestion(L, pos, sid="s0020-1", first=20)
        return L, s

    def test_an_rc_zone_takes_a_candidate_from_0_to_4_m_without_ratio_and_succession(self):
        for dist, expect in ((0.2, 1), (3.9, 1), (4.1, 0)):
            L, s = self.rc_scene(dist)
            self.assertEqual(L.zone7.entry("z0001")["vs"], "ok")          # not falling: the ratio condition is dropped
            self.assertEqual(len(moves.plan(L, [s])[1]), expect, dist)

    def test_an_rc_zone_needs_a_usual_profile(self):
        L, s = self.rc_scene(0.2)
        L.zone7.entry("z0001")["u"] = []
        self.assertEqual(moves.plan(L, [s])[1], [])

    def test_a_non_rc_zone_does_not_take_a_candidate_at_0_2_m(self):
        L, s = self.rc_scene(0.2)
        L.zone7.entry("z0001")["rc"] = False
        self.assertEqual(moves.plan(L, [s])[1], [])

    def test_the_own_suggestion_source_needs_no_falling_for_an_rc_zone(self):
        L, s = self.rc_scene(0.2)
        z = L.review.zones[0]
        own = next(x for x in L.ns.suggestions if x["id"] == z["ref"])
        own["cells"] = frozenset(L.geom.cells_within(Z[0] + 0.3, Z[1], 0.3))
        own["cx"], own["cy"] = Z[0] + 0.3, Z[1]
        own["rev"] = z["srev"] + 1
        prime_own(L)                                      # review fix: the own source needs two nightly runs
        _, mods, _, _ = moves.plan(L, [])
        self.assertEqual([(m["sid"], m["src"], m["shift_cm"]) for m in mods], [("s0001-1", "own", 30)])


class OwnSuggestionTest(unittest.TestCase):
    def own_scene(self, state="falling"):
        L = make()
        rec = add_zone(L, Z)
        use_days(L, range(14), Z)
        if state == "falling":
            for d in range(14, 17):
                cover(L, d)
            run_nights(L, 14, 17)
        else:
            run_nights(L, 14, 14)
        own = next(x for x in L.ns.suggestions if x["id"] == rec["ref"])
        own["cells"] = frozenset(L.geom.cells_within(Z[0] + 0.5, Z[1], 0.3))
        own["cx"], own["cy"] = Z[0] + 0.5, Z[1]
        own["rev"] = rec["srev"] + 1
        prime_own(L)                                      # review fix: the own source needs two nightly runs
        return L, rec, own

    def test_a_shifted_own_suggestion_gives_a_modify_with_sid_ref_and_src_own(self):
        L, rec, own = self.own_scene()
        self.assertEqual(L.zone7.entry("z0001")["vs"], "falling")
        merges, mods, rets, consumed = moves.plan(L, [])
        self.assertEqual(len(mods), 1)
        m = mods[0]
        self.assertEqual((m["sid"], m["src"], m["zid"], m["shift_cm"]), (rec["ref"], "own", "z0001", 50))

    def test_none_when_the_zone_is_ok_or_the_suggestion_did_not_change(self):
        L, rec, own = self.own_scene(state="ok")
        self.assertEqual(moves.plan(L, [])[1], [])
        L2, rec2, own2 = self.own_scene()
        own2["rev"] = rec2["srev"]                                    # unchanged revision
        self.assertEqual(moves.plan(L2, [])[1], [])
        L3, rec3, own3 = self.own_scene()
        own3["cells"] = frozenset(rec3["cells"])                      # the same cells: IoU 1.0 >= 0.6
        self.assertEqual(moves.plan(L3, [])[1], [])


class MergeTest(unittest.TestCase):
    def merge_scene(self, pos, ev=None, state_ok=True):
        L = make()
        add_zone(L, Z)
        use_days(L, range(20), Z)
        run_nights(L, 14, 20)
        s = add_suggestion(L, pos, sid="s0020-1", first=20, ev=ev)
        return L, s

    def test_a_touching_candidate_with_a_similar_profile_is_a_merge_into_an_ok_zone(self):
        L, s = self.merge_scene((2.5, 4.0))
        self.assertEqual(L.zone7.entry("z0001")["vs"], "ok")
        merges, mods, rets, consumed = moves.plan(L, [s])
        self.assertEqual((mods, rets), ([], []))
        self.assertEqual(len(merges), 1)
        m = merges[0]
        self.assertEqual((m["sid"], m["zid"], m["src"], m["rev"]), ("s0020-1", "z0001", "cand", s["rev"]))
        self.assertEqual(consumed, {"s0020-1"})
        self.assertGreaterEqual(m["r"], 0.99)

    def test_an_overlap_of_half_or_more_is_the_same_place_and_gives_nothing(self):
        L, s = self.merge_scene((2.1, 4.0))
        self.assertEqual(moves.plan(L, [s])[0], [])

    def test_a_distant_candidate_or_a_different_profile_gives_nothing(self):
        L, s = self.merge_scene((3.5, 4.0))                           # not touching within 0.4 m
        self.assertEqual(moves.plan(L, [s])[0], [])
        L2, s2 = self.merge_scene((2.5, 4.0), ev=evidence(hours=(20, 21, 22)))
        self.assertEqual(moves.plan(L2, [s2])[0], [])

    def test_a_falling_zone_gives_a_modify_when_the_conditions_hold_and_never_a_merge(self):
        L = make()
        add_zone(L, Z)
        use_days(L, range(14), Z)
        for d in range(14, 17):
            cover(L, d)
        run_nights(L, 14, 17)
        self.assertEqual(L.zone7.entry("z0001")["vs"], "falling")
        use_days(L, range(14, 17), (2.5, 4.0))
        s = add_suggestion(L, (2.5, 4.0), sid="s0020-1", first=20)
        merges, mods, _, _ = moves.plan(L, [s])
        self.assertEqual((merges, [m["sid"] for m in mods]), ([], ["s0020-1"]))          # a modify wins over a merge
        s["evidence"] = evidence(hours=(20, 21))                                         # no modify, still no merge
        self.assertEqual(moves.plan(L, [s])[:2], ([], []))

    def test_a_zone_that_may_not_merge_gives_nothing(self):
        for case in ("bed", "frozen", "young", "blind", "none", "rc", "few"):
            L, s = self.merge_scene((2.5, 4.0))
            e = L.zone7.entry("z0001")
            rec = L.review.zones[0]
            if case == "bed":
                rec["kind"] = "bed"
            elif case == "frozen":
                e["fz"] = True
            elif case == "rc":
                e["rc"] = True
            elif case == "few":
                L.bh.stays[:] = L.bh.stays[:3]
            else:
                e["vs"] = case
            self.assertEqual(moves.plan(L, [s])[0], [], case)

    def test_the_zones_own_suggestion_and_its_also_list_are_never_merge_candidates(self):
        L, s = self.merge_scene((2.5, 4.0))
        L.review.zones[0]["also"] = ["s0020-1"]
        self.assertEqual(moves.plan(L, [s])[0], [])

    def test_a_merge_candidate_is_found_by_iou_without_touching(self):
        L, s = self.merge_scene((2.0, 4.0))
        s["cells"] = frozenset(L.review.zones[0]["cells"]) | frozenset(L.geom.cells_within(2.6, 4.0, 0.3))
        s["cx"], s["cy"] = 2.3, 4.0
        self.assertTrue(zonework.pearson([1.0, 2.0], [1.0, 2.0]) > 0.99)
        # overlap coefficient is 1.0 (the zone is inside it): the same place
        self.assertEqual(moves.plan(L, [s])[0], [])


class OrderingAndFlagTest(unittest.TestCase):
    def test_the_proposals_have_a_deterministic_order(self):
        L = make()
        for k, pos in enumerate(((2.0, 2.0), (2.0, 5.0))):
            add_zone(L, pos, name="Z%d" % k, sid="s000%d-1" % (k + 1))
            use_days(L, range(14), pos)
        use_days(L, range(14, 40), (3.1, 2.0))
        use_days(L, range(14, 40), (3.1, 5.0))
        run_nights(L, 14, 40)
        ss = [add_suggestion(L, (3.1, 5.0), sid="s0021-1", first=20),
              add_suggestion(L, (3.1, 2.0), sid="s0020-1", first=20)]
        _, mods, _, consumed = moves.plan(L, ss)
        self.assertEqual([m["zid"] for m in mods], ["z0001", "z0002"])
        self.assertEqual([m["sid"] for m in mods], ["s0020-1", "s0021-1"])
        self.assertEqual(consumed, {"s0020-1", "s0021-1"})

    def test_plan_is_empty_when_p7_is_not_enabled(self):
        L, rec, s = sofa_scene(p7=dataclasses.replace(p7on(), enabled=False))
        self.assertEqual(moves.plan(L, [s]), ([], [], [], set()))

    def test_disabled_runs_zonework_proposals_a_pair_and_enabled_ignores_the_flag(self):
        flag = P5Params(zone_change_items=True)
        from radar_zone_learner.core import Learner
        from radar_zone_learner.core.params import DEFAULT_P6, DEFAULT_PARAMS
        from radar_zone_learner.core.types import FixedOffsetTime, NodeStatus
        from .p7_harness import BASE_MS
        L = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), None, flag, DEFAULT_P6)           # P7 disabled
        L.handle(NodeStatus(True, BASE_MS))
        self.assertFalse(L.p7on)
        self.assertEqual(len(zonework.proposals(L, [])), 2)                               # a pair, as P5
        L7 = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), None, P5Params(zone_change_items=False), DEFAULT_P6, p7on())
        L7.handle(NodeStatus(True, BASE_MS))
        self.assertTrue(L7.p7on)
        self.assertEqual(len(moves.plan(L7, [])), 4)                                      # the flag is not read
        for fl in (True, False):
            Lx = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), None, P5Params(zone_change_items=fl), DEFAULT_P6, p7on())
            Lx.handle(NodeStatus(True, BASE_MS))
            self.assertEqual(Lx.review.items(Lx), [])
            self.assertTrue(hasattr(Lx.review, "_compute_tier2_p7"))


class ProbationTest(unittest.TestCase):
    def scene(self, dates, first=30):
        L = make()
        for d in range(40):
            cover(L, d)
        for d in dates:
            add_stay(L, d, 10, 3000, 5.0, 4.0)
        set_run(L, 40)
        s = add_suggestion(L, (5.0, 4.0), sid="s0030-1", first=first)
        return L, s

    def test_span_counts_covered_date_positions(self):
        L, s = self.scene([30, 32, 39])
        self.assertEqual(moves.span_of(L, s), 9)
        L2, s2 = self.scene([39])
        self.assertEqual(moves.span_of(L2, s2), 0)
        L3, s3 = self.scene([])
        self.assertEqual(moves.span_of(L3, s3), 0)

    def test_radar_off_dates_do_not_shorten_the_span(self):
        L, s = self.scene([30, 39])
        for d in range(31, 35):
            L.obs.coverage.pop(D0 + d)                                  # radar off on these dates
        self.assertEqual(moves.span_of(L, s), 9 - 4)                    # the window counts covered dates only
        self.assertEqual(len(drift.windows(L, D0 + 40)[0]), 14)

    def test_a_new_plain_place_needs_ten_dates_of_evidence_after_night_14(self):
        L, s = self.scene([36, 37, 38, 39], first=36)
        self.assertFalse(moves.probation_ok(L, s))
        L2, s2 = self.scene(list(range(28, 40)), first=36)
        self.assertTrue(moves.probation_ok(L2, s2))                      # span 11
        L3, s3 = self.scene([36, 37, 38, 39], first=13)
        self.assertTrue(moves.probation_ok(L3, s3))                      # made before night 14: exempt
        L4 = make(p7on(probation_min_span=0))
        s4 = add_suggestion(L4, (5.0, 4.0), sid="s0030-1", first=36)
        set_run(L4, 40)
        self.assertTrue(moves.probation_ok(L4, s4))                      # 0 switches the rule off


class RcHiddenTest(unittest.TestCase):
    def test_a_plain_item_overlapping_an_rc_zone_is_hidden(self):
        L = make()
        rec = add_zone(L, Z)
        e = L.zone7.entry("z0001", create=True)
        s = add_suggestion(L, (Z[0] + 0.1, Z[1]), sid="s0020-1")
        self.assertFalse(moves.rc_hidden(L, s))
        e["rc"] = True
        self.assertTrue(moves.rc_hidden(L, s))
        far = add_suggestion(L, (6.0, 6.0), sid="s0021-1")
        self.assertFalse(moves.rc_hidden(L, far))
        inside = add_suggestion(L, Z, sid="s0022-1", radius=0.2)             # its centroid cell lies in the zone ...
        inside["cells"] = frozenset({L.geom.cell_of(*Z)}) | L.geom.cells_within(6.0, 6.0, 0.6)
        self.assertLess(len(inside["cells"] & rec["cells"]) / min(len(inside["cells"]), len(rec["cells"])), 0.5)
        self.assertTrue(moves.rc_hidden(L, inside))                          # ... though the overlap coefficient is low


if __name__ == "__main__":
    unittest.main()
