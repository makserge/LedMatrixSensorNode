"""Regression tests for the fixes made after the first P7 replay and the independent review (CHANGELOG 0.7.0, 'Changes after
the first replay and review'): the own-suggestion modify rule, the guest probation, `move_zone`, radar moved, the state
validation, the drift windows and the small bookkeeping findings."""
import json
import random
import unittest

from radar_zone_learner.core import Learner, drift, movedfrom, moves, rejections
from radar_zone_learner.core import radarmoved as RM
from radar_zone_learner.core import zones as ZN
from radar_zone_learner.core.p7state import Zone7
from radar_zone_learner.core.params import DEFAULT_P7
from radar_zone_learner.core.types import FixedOffsetTime, StateError, ZoneForget

from .p7_harness import (BASE_MS, D0, add_stay, add_suggestion, add_zone, answer, cover, evidence, make, p7on, prime_own,
                         set_run, use_days)
from .test_extraction import desk_day
from .test_extraction import night as xnight
from .test_movedfrom import cand, with_entry
from .test_moves import N, ProbationTest, Z, night, run_nights, sofa_scene
from .test_p7_review import mod_scene, open_radar, tier2
from .test_radarmoved import CLUSTERS, add_events, base_world, draw, moved_day, run
from .test_retire import retire_scene


# ---------------------------------------------------------------- the own-suggestion source (replay P7-2, P7-8)
class OwnSourceRuleTest(unittest.TestCase):
    def scene(self, shift):
        L = make()
        rec = add_zone(L, Z)
        use_days(L, range(14), Z)
        for d in range(14, 17):
            cover(L, d)
        run_nights(L, 14, 17)
        own = next(x for x in L.ns.suggestions if x["id"] == rec["ref"])
        own["cells"] = frozenset(L.geom.cells_within(Z[0] + shift, Z[1], 0.3))
        own["cx"], own["cy"] = Z[0] + shift, Z[1]
        own["rev"] = rec["srev"] + 1
        self.assertEqual(L.zone7.entry("z0001")["vs"], "falling")
        return L, rec, own

    def test_a_reshaped_region_9_to_17_cm_from_the_zone_is_never_a_modify(self):
        for shift in (0.09, 0.17, 0.25):                  # what the replay's false 'mod-z0001' items had
            L, rec, own = self.scene(shift)
            prime_own(L)
            self.assertEqual(moves.plan(L, [])[1], [], shift)

    def test_a_shift_of_the_minimum_is_a_modify_only_on_the_second_night(self):
        L, rec, own = self.scene(0.5)
        self.assertEqual(moves.plan(L, [])[1], [])                       # the first night this proposal is seen
        L.last_run_ord += 1                                              # the next nightly run
        (m,) = moves.plan(L, [])[1]
        self.assertEqual((m["src"], m["sid"], m["shift_cm"]), ("own", rec["ref"], 50))

    def test_the_same_night_twice_is_still_one_night(self):
        L, rec, own = self.scene(0.5)
        self.assertEqual(moves.plan(L, [])[1], [])
        self.assertEqual(moves.plan(L, [])[1], [])                       # a second read of the same run

    def test_a_new_revision_of_the_suggestion_restarts_the_count(self):
        L, rec, own = self.scene(0.5)
        prime_own(L)
        own["rev"] += 1                                                  # the region changed again tonight
        self.assertEqual(moves.plan(L, [])[1], [])
        L.last_run_ord += 1
        self.assertEqual(len(moves.plan(L, [])[1]), 1)

    def test_a_proposal_that_lapses_is_forgotten(self):
        L, rec, own = self.scene(0.5)
        prime_own(L)
        own["cells"] = frozenset(rec["cells"])                           # back to the zone's own cells: iou 1
        self.assertEqual(moves.plan(L, [])[1], [])
        self.assertEqual(L.p7_own_seen, {})
        own["cells"] = frozenset(L.geom.cells_within(Z[0] + 0.5, Z[1], 0.3))
        self.assertEqual(moves.plan(L, [])[1], [])                       # starts again

    def test_the_minimum_is_a_parameter(self):
        self.assertEqual(DEFAULT_P7.own_min_m, 0.3)
        L = make(p7on(own_min_m=0.05))
        rec = add_zone(L, Z)
        use_days(L, range(14), Z)
        for d in range(14, 17):
            cover(L, d)
        run_nights(L, 14, 17)
        own = next(x for x in L.ns.suggestions if x["id"] == rec["ref"])
        own["cells"] = frozenset(L.geom.cells_within(Z[0] + 0.1, Z[1], 0.3))
        own["cx"], own["cy"] = Z[0] + 0.1, Z[1]
        own["rev"] = rec["srev"] + 1
        prime_own(L)
        self.assertEqual(len(moves.plan(L, [])[1]), 1)


# ---------------------------------------------------------------- succession with a last use out of the window (replay P7-8)
class SuccessionOutOfWindowTest(unittest.TestCase):
    FAR = (5.0, 4.0)                                       # 3.0 m from Z: beyond succ_far_m

    def test_a_far_candidate_is_not_taken_when_the_last_use_left_the_retained_dates(self):
        # the replay's table that nobody used any more next to an armchair that was used all along (3.25 m, r 0.86): the
        # stays older than the 14 covered dates are gone, so the share before the last use is 0 for any candidate
        L, rec, s = sofa_scene(new=self.FAR, post=17)
        e = L.zone7.entry("z0001")
        self.assertEqual(e["vs"], "collapsed")
        self.assertLess(e["lu"], drift.context(L).d14[0])
        self.assertEqual(moves.plan(L, [s])[1], [])

    def test_the_same_candidate_noticed_while_the_last_use_is_retained_is_a_modify(self):
        L, rec, s = sofa_scene(new=self.FAR, post=10)
        self.assertGreaterEqual(L.zone7.entry("z0001")["lu"], drift.context(L).d14[0])
        self.assertEqual([m["shift_cm"] for m in moves.plan(L, [s])[1]], [300])

    def test_a_near_candidate_needs_no_succession_and_is_still_taken(self):
        L, rec, s = sofa_scene(post=17)                    # 1.1 m
        self.assertLess(L.zone7.entry("z0001")["lu"], drift.context(L).d14[0])
        self.assertEqual(len(moves.plan(L, [s])[1]), 1)

    def test_an_rc_zone_is_exempt(self):
        L, rec, s = sofa_scene(new=self.FAR, post=17)
        e = L.zone7.entry("z0001")
        e["rc"] = True
        self.assertEqual(len(moves.plan(L, [s])[1]), 1)


# ---------------------------------------------------------------- the guest probation (replay P7-7)
class GuestProbationTest(unittest.TestCase):
    """The scene of ProbationTest: 40 covered dates, a stay of 3000 s at the suggestion on the given dates, run on date 40
    (the window is the covered dates 26 to 39)."""

    def ok(self, dates, first=30, **kw):
        pt = ProbationTest()
        L, s = pt.scene(dates, first=first)
        if kw:
            L.p7params = p7on(**kw)
        return moves.probation_ok(L, s), L, s

    def test_a_one_week_guest_with_a_stray_stay_a_week_before_is_held(self):
        ok, L, s = self.ok([26] + list(range(33, 40)))             # spans 13 covered dates, lies on 8
        self.assertEqual(moves.span_of(L, s), 13)
        self.assertEqual(len(moves.evidence_dates(L, s)), 8)
        self.assertFalse(ok)

    def test_a_guest_with_a_stray_stay_before_and_after_is_held(self):
        ok, L, s = self.ok([27] + list(range(30, 37)) + [39])      # 7 guest dates plus a visitor on each side: 9 dates
        self.assertEqual(len(moves.evidence_dates(L, s)), 9)       # the largest a 14-date window can show of that pattern
        self.assertTrue(ok)                                        # 9 of 14 dates is a seat that is in use

    def test_a_weekly_visitor_is_held(self):
        ok, _, _ = self.ok([26, 33])
        self.assertFalse(ok)
        ok, _, _ = self.ok([26, 29, 32, 35, 38])                   # every third day: 5 dates, span 12
        self.assertFalse(ok)

    def test_a_genuinely_new_seat_with_ten_covered_dates_of_evidence_is_offered(self):
        ok, L, s = self.ok(list(range(29, 40)))                    # 11 dates in a row
        self.assertTrue(ok)
        ok, _, _ = self.ok([28, 29, 30, 31, 32, 34, 35, 36, 38, 39])   # 10 dates, span 11
        self.assertTrue(ok)
        ok, _, _ = self.ok([28, 29, 30, 31, 33, 34, 36, 37, 39])       # 9 dates, span 11
        self.assertTrue(ok)

    def test_eight_dates_of_evidence_do_not_pass_even_with_a_long_span(self):
        ok, _, _ = self.ok([26, 28, 30, 32, 34, 36, 38, 39])
        self.assertFalse(ok)

    def test_the_date_count_is_a_parameter_and_zero_gives_the_span_rule_alone(self):
        ok, _, _ = self.ok([26] + list(range(33, 40)), probation_min_dates=0)
        self.assertTrue(ok)
        ok, _, _ = self.ok([26] + list(range(33, 40)), probation_min_dates=8)
        self.assertTrue(ok)
        ok, _, _ = self.ok([26] + list(range(33, 40)), probation_min_dates=9)
        self.assertFalse(ok)
        self.assertEqual(DEFAULT_P7.probation_min_dates, 9)

    def test_the_exemption_before_night_14_and_the_switch_stay(self):
        ok, _, _ = self.ok([26, 33], first=13)
        self.assertTrue(ok)
        ok, _, _ = self.ok([26, 33], probation_min_span=0)
        self.assertTrue(ok)

    def test_the_review_does_not_offer_the_guest(self):
        pt = ProbationTest()
        L, s = pt.scene([26] + list(range(33, 40)), first=36)
        L.review.touch()
        self.assertNotIn("s0030-1", [i.id for i in L.review.items(L)])
        self.assertEqual(L.review.probation_held, 1)


# ---------------------------------------------------------------- move_zone (review: moves F1, movedfrom F1, F4)
class MoveZoneTest(unittest.TestCase):
    def approved(self):
        L, rec, s = mod_scene()
        answer(L, "mod-z0001", "approve", rev=1)
        return L, rec, s

    def test_a_just_moved_zone_has_no_stale_verdict_and_gets_no_second_modify(self):
        L, rec, s = self.approved()
        e = L.zone7.entry("z0001")
        self.assertEqual((e["vs"], e["fz"]), ("young", False))
        s2 = add_suggestion(L, (N[0] + 1.1, N[1]), sid="s0040-1", first=41, ev=evidence(hours=(9, 10, 11)))
        use_days(L, range(31, 46), (N[0] + 1.1, N[1]))
        self.assertEqual(moves.plan(L, [s2])[1], [])
        L.review.touch()
        self.assertEqual([i.id for i in L.review.items(L) if i.type == "modify_zone"], [])

    def test_the_old_ref_does_not_join_also_and_the_old_spot_comes_back_after_the_mask_lifts(self):
        L, rec, s = self.approved()
        z = L.review.zones[0]
        self.assertEqual((z["ref"], z["also"]), ("s0020-1", []))
        self.assertNotIn("s0001-1", ZN.covered_ids(L))
        self.assertEqual(len(L.zone7.mf), 1)
        L.zone7.mf.clear()                                             # the mask lifted (the old spot re-qualified)
        L.review.touch()
        ids = [i.id for i in L.review.items(L)]
        self.assertIn("s0001-1", ids)                                  # a normal pending place again (first night < 14)

    def test_a_needs_recheck_zone_records_no_moved_from_mask(self):
        L, rec, s = mod_scene()
        L.zone7.entry("z0001")["rc"] = True
        L.review.touch()
        answer(L, "mod-z0001", "approve", rev=L.review.items(L)[-1].rev)
        self.assertEqual(L.zone7.mf, [])
        self.assertFalse(L.zone7.entry("z0001")["rc"])

    def test_a_modify_of_a_zone_that_is_not_rc_still_records_the_mask(self):
        L, rec, s = self.approved()
        self.assertEqual([(m["zid"], m["why"]) for m in L.zone7.mf], [("z0001", "moved_from")])


# ---------------------------------------------------------------- retire, forget (review: moves F2, F3, review RM-5)
class RetireBookkeepingTest(unittest.TestCase):
    def test_a_zone_that_drops_out_of_the_plan_closes_its_retire_item(self):
        L, rec = retire_scene()
        (r,) = moves.plan(L, [])[2]
        e = L.zone7.entry("z0001")
        self.assertEqual((e["ro"], e["rr"], r["rev"]), (True, 1, 1))
        L.review.zones[0]["kind"] = "bed"                              # it leaves the plan (a bed takes no retire)
        self.assertEqual(moves.plan(L, [])[2], [])
        self.assertFalse(e["ro"])
        L.review.zones[0]["kind"] = "dwell"
        (r,) = moves.plan(L, [])[2]
        self.assertEqual((e["ro"], e["rr"], r["rev"]), (True, 2, 2))   # reopened: a new revision

    def test_zone_forget_withdraws_the_cached_retire_item(self):
        L, rec = retire_scene()
        L.review.touch()
        self.assertIn("ret-z0001", [i.id for i in tier2(L)])
        self.assertTrue(L._zone_command(ZoneForget("z0001", BASE_MS)))
        self.assertEqual(L.zone7.entry("z0001")["rt"], 0)
        self.assertNotIn("ret-z0001", [i.id for i in tier2(L)])


class ForgetRejectionsTest(unittest.TestCase):
    def test_forget_clears_the_quiet_time_of_the_radar_item(self):
        L = make()
        set_run(L, 40)
        add_zone(L, Z)
        L.zone7.rm["qt"] = D0 + 50                                      # quiet after an Ignore, nothing else set
        answer(L, None, "forget")
        self.assertEqual((L.zone7.rm["qt"], L.zone7.rm["ign"]), (0, 0))
        self.assertEqual(RM.state_name(L.zone7.rm), "none")

    def test_forget_after_an_ignore_lets_the_detector_run_again(self):
        L, rng = base_world()
        L.zone7.rm.update(ign=1, rev=1, qt=D0 + 30)
        answer(L, None, "forget")
        moved_day(L, rng, 14, deg=10.0)
        run(L, 15)
        self.assertEqual(L.zone7.rm["st"], 1)


# ---------------------------------------------------------------- moved-from masks (review: movedfrom F2, F3, F5)
class MaskFixesTest(unittest.TestCase):
    def test_release_lifts_only_the_entry_that_requalified_even_with_the_same_zone_reason_and_day(self):
        L, rec = with_entry()
        other = dict(rec)
        other["cells"] = frozenset(L.geom.cells_within(6.0, 2.0, 0.3))
        other["cx"], other["cy"] = 6.0, 2.0
        movedfrom.add(L, "moved_from", other)
        L.zone7.mf[1]["day"] = L.zone7.mf[0]["day"]                     # the same zone, reason and day, other cells
        self.assertEqual(len(L.zone7.mf), 2)
        c = cand(L, dates=range(L.zone7.mf[0]["day"] + 1, L.zone7.mf[0]["day"] + 5))
        self.assertFalse(movedfrom.blocks(L, c, 15))                    # re-qualifies for the first entry only
        self.assertEqual(movedfrom.release(L), 1)
        self.assertEqual([m["cx"] for m in L.zone7.mf], [6.0])

    def test_a_loaded_mask_list_longer_than_mf_max_is_cut_to_the_newest(self):
        L = make()
        rec = add_zone(L, Z)
        for i in range(5):
            movedfrom.add(L, "moved_from", dict(rec, cells=frozenset(L.geom.cells_within(1.0 + i, 2.0, 0.2)),
                                                cx=1.0 + i, cy=2.0))
        self.assertEqual(len(L.zone7.mf), 5)
        st = json.loads(json.dumps(L.export_state()))
        L2 = Learner(L.p, FixedOffsetTime(0), st, L.p5params, L.p6params, p7on(mf_max=3))
        self.assertEqual([m["cx"] for m in L2.zone7.mf], [3.0, 4.0, 5.0])
        L3 = Learner(L.p, FixedOffsetTime(0), st, L.p5params, L.p6params, p7on())
        self.assertEqual(len(L3.zone7.mf), 5)

    def test_a_blocking_rejection_does_not_hide_a_requalifying_moved_from_entry(self):
        L = make()
        for d in range(7):
            desk_day(L, d)
        xnight(L, 7)
        desk_day(L, 7)
        xnight(L, 8)
        desk_day(L, 8)
        xnight(L, 9)
        s = next(x for x in L.ns.suggestions if x["type"] == "dwell_zone")
        zone_like = {"zid": "z0001", "cells": frozenset(s["cells"]), "cx": s["cx"], "cy": s["cy"], "ref": s["id"]}
        movedfrom.add(L, "moved_from", zone_like)
        self.assertEqual(L.zone7.mf[0]["day"], D0 + 9)
        answer(L, s["id"], "reject")                                    # a rejection mask blocks the same region
        self.assertTrue(rejections.mask_match(L, s))
        for k in range(9, 15):
            desk_day(L, k)
            xnight(L, k + 1)
            c = next(c for c in L.candidates() if abs(c.cx_m - 2.55) < 0.4)
            self.assertFalse(c.gates["g7_suppression"], k)               # the rejection still blocks
        self.assertEqual(L.zone7.mf, [])                                 # but the entry re-qualified and was released


# ---------------------------------------------------------------- radar moved (review: RM-1, RM-2, RM-4, RM-5)
class RadarFixesTest(unittest.TestCase):
    def test_a_sparse_night_does_not_count_the_same_date_twice(self):
        L, rng = base_world()
        moved_day(L, rng, 14, deg=10.0)
        run(L, 15)
        self.assertEqual(L.zone7.rm["st"], 1)
        cover(L, 15)                                                     # nobody home: no points on the newest date
        run(L, 16)
        self.assertEqual((L.zone7.rm["st"], L.zone7.rm["open"]), (1, False))
        self.assertEqual(L.p7_radar_dbg["why"], "newest")
        moved_day(L, rng, 16, deg=10.0)                                   # a real second night
        run(L, 17)
        self.assertTrue(L.zone7.rm["open"])

    def test_a_new_door_habit_is_not_a_moved_radar(self):
        L, rng = base_world()
        cover(L, 14)
        add_events(L, 14, [(0.8 + rng.gauss(0, 0.1), 3.4 + rng.gauss(0, 0.1)) for _ in range(28)])   # one new cluster
        run(L, 15)
        dbg = L.p7_radar_dbg
        self.assertGreaterEqual(dbg["hit"], 0.7, dbg)                      # a shift of 0.8 m lays it over one old cluster
        self.assertEqual((L.zone7.rm["st"], dbg["verdict"], dbg["why"]), (0, "ok", "clusters"))
        L2 = make(p7on(rm_min_clusters=1))                                 # without the guard it is flagged
        rng2 = random.Random(3)
        for d in range(14):
            cover(L2, d)
            add_events(L2, d, draw(rng2, 24))
        cover(L2, 14)
        add_events(L2, 14, [(0.8 + rng2.gauss(0, 0.1), 3.4 + rng2.gauss(0, 0.1)) for _ in range(28)])
        run(L2, 15)
        self.assertEqual(L2.zone7.rm["st"], 1)

    def test_a_real_rotation_is_still_found(self):
        for seed in (1, 2, 3):
            L, rng = base_world(seed=seed)
            moved_day(L, rng, 14, deg=10.0)
            run(L, 15)
            self.assertEqual(L.zone7.rm["st"], 1, (seed, L.p7_radar_dbg))
            self.assertNotIn("why", L.p7_radar_dbg)

    def test_an_anomalous_reference_date_marks_the_search_as_noise(self):
        L = make()
        rng = random.Random(3)
        for d in range(13):
            cover(L, d)
            add_events(L, d, draw(rng, 24))
        cover(L, 13)                                                       # the newest reference date sits 0.8 m left
        add_events(L, 13, [(x - 0.8, y) for x, y in draw(rng, 24)])
        cover(L, 14)
        add_events(L, 14, [(x + 0.8, y) for x, y in draw(rng, 24)])        # tonight 0.8 m right: 'moved' against the rest
        run(L, 15)
        self.assertEqual(L.p7_radar_dbg["why"], "noise", L.p7_radar_dbg)
        self.assertEqual(L.zone7.rm["st"], 0)

    def test_a_streak_that_never_completes_releases_the_hold_after_rm_hold_days(self):
        L, rng = base_world()
        L.zone7.rm.update(st=1, d0=D0 + 14, tx=[40, 0, 0], n=D0 + 14)
        set_run(L, 14 + 14)
        self.assertTrue(RM.hold(L))                                        # at most rm_hold_days: still held
        set_run(L, 14 + 15)
        self.assertFalse(RM.hold(L))                                       # older: not held, whatever nightly did
        self.assertEqual(RM.view(L).state, "watching")

    def test_nightly_clears_a_stale_streak_and_its_first_date(self):
        L, rng = base_world()
        for d in range(14, 30):
            cover(L, d)
            add_events(L, d, draw(rng, 24))
        L.zone7.rm.update(st=1, d0=D0 + 14, tx=[40, 0, 0], n=D0 + 28)
        run(L, 30)
        rm = L.zone7.rm
        self.assertEqual((rm["st"], rm["d0"]), (0, 0))
        self.assertFalse(RM.hold(L))

    def test_a_latched_item_keeps_its_own_bound(self):
        L, rng = base_world()
        open_radar(L)
        set_run(L, 10 + 14)
        self.assertTrue(RM.hold(L))
        set_run(L, 10 + 15)
        self.assertFalse(RM.hold(L))

    def test_reset_keeps_an_existing_outline_suggestion_alive_and_forgets_the_walk_total(self):
        L = make()
        s = add_suggestion(L, (0.0, 3.0), sid="s0003-1", typ="room_outline", first=3)
        L.ns.outline = {"cells": frozenset([1, 2, 3]), "streak": 4, "done": True}
        L.bh.walk_points_total = 5000
        RM.reset_maps(L)
        self.assertTrue(L.ns.outline["done"])                              # the update branch revives s0003-1
        self.assertEqual(L.bh.walk_points_total, 0)
        self.assertEqual(s["state"], "stale")
        L2 = make()
        L2.ns.outline = {"cells": frozenset([1, 2, 3]), "streak": 4, "done": True}
        RM.reset_maps(L2)
        self.assertFalse(L2.ns.outline["done"])                            # nothing to revive: the outline is rebuilt


# ---------------------------------------------------------------- p7state (review T1) and drift (review F1, F2)
class StateFixesTest(unittest.TestCase):
    def good(self):
        u = [0.0] * 24
        u[9] = u[10] = u[11] = 1.0 / 3
        return {"v": 1, "z": {"z0001": {"u": u}}, "mf": []}

    def test_a_huge_integer_in_u_is_a_state_error_not_an_overflow(self):
        d = self.good()
        d["z"]["z0001"]["u"][3] = 10 ** 400
        with self.assertRaises(StateError):
            Zone7.validate(d, 2800)
        z = Zone7()
        z.load(d, 2800)                                                   # a value problem: defaults, counted
        self.assertEqual((z.reset_count, z.z), (1, {}))

    def test_a_huge_integer_in_a_mask_centre_is_a_state_error(self):
        for k in ("cx", "cy"):
            d = self.good()
            m = {"zid": "z0001", "cells": [[10, 3]], "cx": 1.0, "cy": 2.0, "day": 739490, "n0": 1, "why": "moved_from"}
            m[k] = -10 ** 400
            d["mf"] = [m]
            with self.assertRaises(StateError):
                Zone7.validate(d, 2800)

    def test_ordinary_values_still_load(self):
        d = self.good()
        d["mf"] = [{"zid": "z0001", "cells": [[10, 3]], "cx": 1, "cy": -2, "day": 739490, "n0": 1, "why": "moved_from"}]
        Zone7.validate(d, 2800)
        d["z"]["z0001"]["u"][0] = float("nan")
        with self.assertRaises(StateError):
            Zone7.validate(d, 2800)


class DriftFixesTest(unittest.TestCase):
    def test_an_hour_observed_exactly_half_the_time_is_eligible_whatever_the_date_count(self):
        for n in range(8, 15):
            L = make()
            add_zone(L, Z)
            for d in range(n):
                cover(L, d)
                L.obs.coverage[D0 + d][6] = 1800.0
            set_run(L, n)
            self.assertIn(6, drift.context(L).elig, n)

    def test_an_hour_just_under_half_is_not_eligible(self):
        L = make()
        add_zone(L, Z)
        for d in range(10):
            cover(L, d)
            L.obs.coverage[D0 + d][6] = 1790.0
        set_run(L, 10)
        self.assertNotIn(6, drift.context(L).elig)

    def test_covered_dates_older_than_the_stay_retention_are_not_in_the_window(self):
        L = make()
        for d in list(range(10)) + [31, 32, 33]:
            cover(L, d)
        run_ord = D0 + 34
        d14 = drift.windows(L, run_ord)[0]
        self.assertEqual(d14, [D0 + d for d in (6, 7, 8, 9, 31, 32, 33)])    # 28 calendar days, as the extraction keeps
        L.obs.coverage.pop(D0 + 9)
        self.assertEqual(drift.windows(L, run_ord)[0][0], D0 + 6)

    def test_blind_and_held_flags(self):
        L = make()
        add_zone(L, Z)
        use_days(L, range(14), Z)
        night(L, 14)
        L.zone7.entry("z0001")["rc"] = True
        v = drift.view(L)[0]
        self.assertTrue(v.held)
        self.assertTrue(v.rc)


if __name__ == "__main__":
    unittest.main()
