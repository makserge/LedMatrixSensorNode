"""Every P7 site is a no-op while `P7Params.enabled` is False (spec 4.10, P7-R1, 9.2 `test_p7_enabled`): a default learner
behaves as the P6 learner; with P7 enabled the learning fields are identical until the first modify, retire or reset."""
import contextlib
import copy
import json
import unittest

from radar_zone_learner.core import Learner, moves, movedfrom, radarmoved, tuningagg, zonework
from radar_zone_learner.core.params import DEFAULT_P5, DEFAULT_P6, DEFAULT_P7, DEFAULT_PARAMS, P5Params
from radar_zone_learner.core.types import (FixedOffsetTime, NightlyDone, NightlyFailed, NodeStatus, ReviewSelect,
                                           TuningLine)

from .p7_harness import BASE_MS, D0, DAY, add_suggestion, add_zone, answer, cover, make, p7on, set_run, use_days
from .test_extraction import desk_day
from .test_extraction import night as xnight

FLAG = P5Params(zone_change_items=True)
Z = (2.0, 4.0)


def disabled(p5=DEFAULT_P5):
    L = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), None, p5, DEFAULT_P6, DEFAULT_P7)
    L.handle(NodeStatus(True, BASE_MS))
    return L


@contextlib.contextmanager
def patched(mod, name, fn):
    real = getattr(mod, name)
    setattr(mod, name, fn)
    try:
        yield
    finally:
        setattr(mod, name, real)


def boom(*a, **k):
    raise AssertionError("a P7 function was reached with P7 disabled")


class SitesTest(unittest.TestCase):
    def test_1_the_attributes_exist_and_stay_at_their_defaults(self):
        L = disabled()
        self.assertFalse(L.p7on)
        self.assertEqual((L.p7params, L.p7_errors, L.p7_requalified), (DEFAULT_P7, 0, set()))
        self.assertTrue(L.zone7.is_default())
        self.assertIsNone(L.planner.zone7)                                          # site 8: no attention

    def test_2_the_nightly_block_is_skipped(self):
        L = disabled()
        with contextlib.ExitStack() as st:
            st.enter_context(patched(radarmoved, "nightly", boom))
            st.enter_context(patched(tuningagg, "nightly", boom))
            st.enter_context(patched(movedfrom, "release", boom))
            outs = L.tick(BASE_MS + 8 * DAY * 1000 + 210 * 60000)
        self.assertTrue(any(isinstance(o, NightlyDone) for o in outs))
        self.assertFalse(any(isinstance(o, (TuningLine, NightlyFailed)) for o in outs))
        self.assertTrue(L.zone7.is_default())

    def test_3_the_coverage_freeze_is_not_consulted(self):
        L = disabled()
        add_zone(L, Z)
        L.zone7.entry("z0001", create=True)["fz"] = True
        self.assertFalse(L.zones()[0].frozen)
        self.assertEqual(L.drift_view(), ())

    def test_4_the_moved_from_test_is_not_applied(self):
        L = disabled()
        L.night = 40
        L.last_run_ord = D0 + 40
        rec = add_zone(L, Z, night=0)
        s = add_suggestion(L, (6.0, 6.0), sid="s0030-1", first=5)
        L.zone7.mf.append({"zid": "z0009", "cells": frozenset(s["cells"]), "cx": 6.0, "cy": 6.0, "day": D0 + 5, "n0": 0,
                           "why": "retired"})
        L.review.touch()
        self.assertIn("s0030-1", [i.id for i in L.review.items(L)])

    def test_5_the_tier_2_path_is_the_p5_one_with_its_order_and_no_retire_items(self):
        L = disabled(FLAG)
        L.night = 40
        L.last_run_ord = D0 + 40
        calls = []
        real = zonework.proposals

        def spy(L_, pending=None):
            calls.append(1)
            return real(L_, pending)
        add_zone(L, Z)
        L.zone7.entry("z0001", create=True).update(rt=99, vs="collapsed", lu=D0 + 3, u=[0.0] * 9 + [1.0] + [0.0] * 14)
        add_suggestion(L, (6.0, 6.0), sid="s0030-1", first=5)
        with contextlib.ExitStack() as st:
            st.enter_context(patched(zonework, "proposals", spy))
            st.enter_context(patched(moves, "plan", boom))
            L.review.touch()
            ids = [i.id for i in L.review.items(L)]
        self.assertTrue(calls)
        self.assertEqual([i for i in ids if i != "bed_needs_data"], ["s0030-1"])
        self.assertFalse(any(i.startswith("ret-") for i in ids))

    def test_5_the_p5_flag_selects_the_scaffolding_only_when_disabled(self):
        # (False, True): the P5 scaffolding; (True, either): the P7 path (the flag is ignored)
        for flag in (True, False):
            L = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), None, P5Params(zone_change_items=flag), DEFAULT_P6, p7on())
            L.handle(NodeStatus(True, BASE_MS))
            with patched(zonework, "proposals", boom):
                L.night = 40
                L.last_run_ord = D0 + 40
                L.review.touch()
                L.review.items(L)                                                   # must not reach zonework.proposals

    def test_6_no_radar_item_and_the_select_is_refused_as_before(self):
        L = disabled()
        L.night = 40
        L.last_run_ord = D0 + 40
        L.zone7.rm.update(open=True, rev=1, st=2)
        self.assertNotIn("radar_moved", [i.id for i in L.review.items(L)])
        L.handle(ReviewSelect("radar_moved", L.now + 1))
        self.assertNotEqual(L.review.select, "radar_moved")
        self.assertFalse(L.review.open_tier1(L, "radar_moved"))

    def test_7_the_p5_modify_answers_run_the_p5_code(self):
        L = disabled(FLAG)
        L.night = 40
        L.last_run_ord = D0 + 40
        rec = add_zone(L, Z)
        L.review.zones[0]["cells"] = frozenset(L.geom.cells_within(Z[0], Z[1], 0.3))
        s = next(x for x in L.ns.suggestions if x["id"] == rec["ref"])
        s["cells"] = frozenset(L.geom.cells_within(Z[0] + 1.2, Z[1], 0.3))
        s["cx"], s["cy"] = Z[0] + 1.2, Z[1]
        s["rev"] = rec["srev"] + 1
        L.review.touch()
        its = [i for i in L.review.items(L) if i.type == "modify_zone"]
        self.assertEqual(len(its), 1)
        self.assertEqual(set(its[0].data), {"tag", "sid", "zid", "zname", "shift_cm", "dir"})      # no r, no src
        answer(L, "mod-z0001", "approve", rev=its[0].rev)
        z = L.review.zones[0]
        self.assertEqual(z["ref"], rec["ref"])                                       # P5: no swap, no also
        self.assertEqual(z["also"], [])
        self.assertEqual(L.zone7.mf, [])                                             # no moved-from mask
        self.assertTrue(L.zone7.is_default())

    def test_10_the_highlight_of_a_modify_item_is_the_zones_ref_suggestion(self):
        L = disabled(FLAG)
        rec = add_zone(L, Z)
        s = next(x for x in L.ns.suggestions if x["id"] == rec["ref"])
        self.assertEqual(L._highlight_cells("mod-z0001"), frozenset(s["cells"]))

    def test_11_no_status_or_counter_keys(self):
        L = disabled()
        a = L.status().attributes
        for k in ("zones_frozen", "zones_blind", "zones_falling", "zones_unused", "zones_recheck", "moved_from",
                  "radar_moved", "places_probation", "places_recheck_held"):
            self.assertNotIn(k, a)
        for k in ("zone7_pruned", "zone7_reset", "p7_errors"):
            self.assertNotIn(k, L.counters())

    def test_13_zone7_is_absent_from_the_export_at_its_defaults_enabled_or_not(self):
        for L in (disabled(), make()):
            self.assertNotIn("p5", L.export_state())
        E = make()
        E.zone7.entry("z0001", create=True)
        self.assertIn("zone7", E.export_state()["p5"])

    def test_forget_and_nightly_leave_loaded_zone7_data_alone_when_disabled(self):
        L = disabled()
        L.night = 40
        L.last_run_ord = D0 + 40
        L.zone7.mf.append({"zid": "z0009", "cells": frozenset([1]), "cx": 0.0, "cy": 0.0, "day": D0, "n0": 0,
                           "why": "retired"})
        L.zone7.snz["radar_moved"] = D0 + 3
        L.zone7.rm["ign"] = 4
        answer(L, None, "forget")
        L.review.on_nightly(L)
        self.assertEqual(len(L.zone7.mf), 1)
        self.assertEqual((L.zone7.snz, L.zone7.rm["ign"]), ({"radar_moved": D0 + 3}, 4))


class IdenticalLearningTest(unittest.TestCase):
    """P7-R1 (b) at unit level: with P7 enabled and no modify, retire or reset answer, the learning fields and the review
    items equal those of the default (P6) learner."""

    def pair(self):
        a = Learner(DEFAULT_PARAMS, FixedOffsetTime(0))
        a.handle(NodeStatus(True, BASE_MS))
        b = make()
        return a, b

    @staticmethod
    def learning(L):
        st = L.export_state()
        st = copy.deepcopy(st)
        if "p5" in st:
            st["p5"].pop("zone7", None)
            if not st["p5"]:
                del st["p5"]
        return json.dumps(st, sort_keys=True)

    def test_stays_candidates_suggestions_items_and_state_are_identical_until_the_first_p7_answer(self):
        a, b = self.pair()
        for L in (a, b):
            for d in range(7):
                desk_day(L, d)
            xnight(L, 7)
            desk_day(L, 7)
            xnight(L, 8)
            desk_day(L, 8)
            xnight(L, 9)
        self.assertEqual(self.learning(a), self.learning(b))
        self.assertEqual(a.suggestions(), b.suggestions())
        self.assertEqual(a.candidates(), b.candidates())
        self.assertEqual(a.stays(), b.stays())
        self.assertEqual(a.review_state(), b.review_state())
        for L in (a, b):
            answer(L, "s0009-1", "approve", name="Desk")
            for d in range(9, 14):
                desk_day(L, d)
                xnight(L, d + 1)
        self.assertEqual(self.learning(a), self.learning(b))
        self.assertEqual(a.review_state(), b.review_state())
        self.assertEqual(a.push_request(), b.push_request())
        self.assertEqual(a.zones(), b.zones())
        self.assertEqual(a.suggestions(), b.suggestions())
        self.assertEqual(a.candidates(), b.candidates())
        self.assertEqual(b.p7_errors, 0)
        self.assertIsNotNone(b.zone7.entry("z0001"))                                # the only difference: derived state

    def test_the_outputs_of_a_nightly_run_differ_only_by_the_tuning_line(self):
        a, b = self.pair()
        for L in (a, b):
            for d in range(7):
                desk_day(L, d)
        oa = a.tick(BASE_MS + 7 * DAY * 1000 + 210 * 60000)
        ob = b.tick(BASE_MS + 7 * DAY * 1000 + 210 * 60000)
        ob = [o for o in ob if not isinstance(o, TuningLine)]
        self.assertEqual([type(o).__name__ for o in oa], [type(o).__name__ for o in ob])
        self.assertEqual([o for o in oa if isinstance(o, NightlyDone)], [o for o in ob if isinstance(o, NightlyDone)])


if __name__ == "__main__":
    unittest.main()
