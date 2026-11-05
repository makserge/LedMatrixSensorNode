"""P7 moved-from and retired masks (spec 4.4, check P7-4 (U))."""
import types
import unittest

from radar_zone_learner.core import movedfrom, rejections
from radar_zone_learner.core import zones as ZN
from radar_zone_learner.core.params import DEFAULT_P7

from .p7_harness import D0, add_suggestion, add_zone, answer, make, p7on, set_run
from .test_extraction import desk_day
from .test_extraction import night as xnight

Z = (2.0, 4.0)


def cand(L, pos=Z, cls="dwell", dates=(), per_day=3, dur=2400, cells=None, spread=0.0):
    """A fake extraction candidate at ``pos`` with stays on the given date ordinals (as ``c.mine``)."""
    cells = frozenset(cells if cells is not None else L.geom.cells_within(pos[0], pos[1], 0.3))
    cell = L.geom.cell_of(*pos)
    mine = []
    for d in dates:
        for k in range(per_day):
            st = {"dur": dur, "x": round(pos[0] * 100 + spread * 100 * ((k % 2) * 2 - 1)), "y": round(pos[1] * 100),
                  "ld": 1.0}
            mine.append((st, cell, d))
    return types.SimpleNamespace(cells=cells, cx=pos[0], cy=pos[1], cls=cls, mine=mine)


def with_entry(day=D0 + 10, why="moved_from", p7=None, pos=Z):
    L = make(p7)
    rec = add_zone(L, pos)
    set_run(L, 10)
    movedfrom.add(L, why, rec)
    L.zone7.mf[0]["day"] = day
    return L, rec


class AddTest(unittest.TestCase):
    def test_an_entry_has_the_cells_the_centre_the_date_the_evidence_and_the_reason(self):
        L = make()
        rec = add_zone(L, Z, ev={"n": 24, "hours_wd": [0.0] * 24, "hours_we": [0.0] * 24,
                                 "hours_at_suggestion": [0.0] * 24})
        set_run(L, 12)
        movedfrom.add(L, "moved_from", rec)
        (m,) = L.zone7.mf
        self.assertEqual((m["zid"], m["why"], m["day"], m["n0"], m["cx"], m["cy"]),
                         ("z0001", "moved_from", L.review._today(L), 24, rec["cx"], rec["cy"]))
        self.assertEqual(m["cells"], frozenset(rec["cells"]))
        self.assertIsInstance(m["cells"], frozenset)
        self.assertEqual(movedfrom.key_of(m), ("z0001", "moved_from", L.review._today(L)))

    def test_the_cap_drops_the_oldest(self):
        L = make(p7on(mf_max=3))
        rec = add_zone(L, Z)
        for k in range(5):
            set_run(L, 10 + k)
            movedfrom.add(L, "retired", rec)
        self.assertEqual([m["day"] for m in L.zone7.mf], [D0 + 12, D0 + 13, D0 + 14])
        set_run(L, 14)
        movedfrom.add(L, "retired", rec)                                    # the same mask again: replaced, not duplicated
        self.assertEqual(len(L.zone7.mf), 3)

    def test_two_different_masks_of_one_zone_on_one_day_both_stay(self):
        L = make()
        rec = add_zone(L, Z)
        set_run(L, 10)
        movedfrom.add(L, "moved_from", rec)
        rec["cells"] = frozenset(L.geom.cells_within(5.0, 5.0, 0.3))
        movedfrom.add(L, "moved_from", rec)
        self.assertEqual(len(L.zone7.mf), 2)


class GateTest(unittest.TestCase):
    def test_a_candidate_that_matches_and_has_only_pre_event_stays_fails_gate_7(self):
        L, rec = with_entry()
        c = cand(L, dates=range(D0, D0 + 11))                                # all stays on or before the answer date
        self.assertTrue(movedfrom.blocks(L, c, 12))
        self.assertEqual(L.p7_requalified, set())

    def test_the_same_candidate_with_enough_post_event_stays_passes_and_records_the_key(self):
        L, rec = with_entry()
        c = cand(L, dates=list(range(D0, D0 + 11)) + list(range(D0 + 11, D0 + 15)))       # 4 post-event core days
        self.assertEqual(sum(1 for m in c.mine if m[2] > D0 + 10), 12)
        self.assertFalse(movedfrom.blocks(L, c, 15))
        self.assertEqual(L.p7_requalified, {("z0001", "moved_from", D0 + 10)})
        self.assertEqual(movedfrom.release(L), 1)                              # a lifted entry is gone for good
        self.assertEqual((L.zone7.mf, L.p7_requalified), ([], set()))
        self.assertFalse(movedfrom.blocks(L, c, 16))

    def test_each_threshold_of_the_dwell_class_is_needed(self):
        for dates, per_day, dur, expect_block in (
                (range(D0 + 11, D0 + 15), 3, 2400, False),                      # 12 stays, 480 min, 4 days
                (range(D0 + 11, D0 + 14), 3, 2400, True),                       # 3 core days only
                (range(D0 + 11, D0 + 15), 1, 2400, True),                       # 4 stays (< 6)
                (range(D0 + 11, D0 + 15), 2, 100, True),                        # 8 stays but 13 minutes (< 20)
        ):
            L, rec = with_entry()
            c = cand(L, dates=dates, per_day=per_day, dur=dur)
            self.assertEqual(movedfrom.blocks(L, c, 15), expect_block, (list(dates), per_day, dur))

    def test_a_short_task_candidate_needs_12_stays_and_the_spread_rule(self):
        L, rec = with_entry()
        c = cand(L, cls="short", dates=range(D0 + 11, D0 + 15), per_day=3, dur=60)          # 12 stays, no spread
        self.assertFalse(movedfrom.blocks(L, c, 15))
        L, rec = with_entry()
        c = cand(L, cls="short", dates=range(D0 + 11, D0 + 15), per_day=2, dur=60)          # 8 stays
        self.assertTrue(movedfrom.blocks(L, c, 15))
        L, rec = with_entry()
        c = cand(L, cls="short", dates=range(D0 + 11, D0 + 15), per_day=3, dur=60, spread=1.5)   # spread too wide
        self.assertTrue(movedfrom.blocks(L, c, 15))

    def test_a_candidate_matches_by_overlap_or_by_its_centroid_cell(self):
        L, rec = with_entry()
        near = cand(L, pos=(Z[0] + 0.1, Z[1]), dates=[])
        self.assertTrue(movedfrom.blocks(L, near, 12))                           # overlap coefficient >= 0.5
        far = cand(L, pos=(6.0, 6.0), dates=[])
        self.assertFalse(movedfrom.blocks(L, far, 12))                           # (h) a non-matching candidate is untouched
        centre = cand(L, pos=Z, dates=[], cells=frozenset({L.geom.cell_of(*Z)}) | L.geom.cells_within(6.0, 6.0, 0.6))
        self.assertTrue(movedfrom.blocks(L, centre, 12))                         # its centroid cell lies in the mask

    def test_a_retired_entry_behaves_alike(self):
        L, rec = with_entry(why="retired")
        c = cand(L, dates=range(D0, D0 + 11))
        self.assertTrue(movedfrom.blocks(L, c, 12))
        c = cand(L, dates=range(D0 + 11, D0 + 15))
        self.assertFalse(movedfrom.blocks(L, c, 15))
        self.assertEqual(L.p7_requalified, {("z0001", "retired", D0 + 10)})

    def test_blocks_returns_at_once_with_an_empty_list(self):
        L = make()
        real = rejections.matches_mask

        def boom(*a, **k):
            raise AssertionError("must not be called")
        rejections.matches_mask = boom
        try:
            self.assertFalse(movedfrom.blocks(L, cand(L, dates=[]), 5))
            self.assertFalse(movedfrom.mask_match(L, add_suggestion(L, Z)))
        finally:
            rejections.matches_mask = real

    def test_nothing_applies_when_p7_is_disabled(self):
        L, rec = with_entry(p7=DEFAULT_P7)
        self.assertFalse(movedfrom.blocks(L, cand(L, dates=[]), 12))
        self.assertFalse(movedfrom.mask_match(L, add_suggestion(L, Z, sid="s0031-1")))

    def test_begin_clears_the_requalified_set(self):
        L, rec = with_entry()
        L.p7_requalified.add(("z0001", "moved_from", D0 + 10))
        movedfrom.begin(L)
        self.assertEqual(L.p7_requalified, set())


class OpenItemTest(unittest.TestCase):
    def test_a_shadow_suggestion_that_matches_a_live_entry_is_never_an_item(self):
        L, rec = with_entry()
        L.night = 40
        L.last_run_ord = D0 + 40
        ZN.remove(L, "z0001")                                                    # the old zone's own suggestion is uncovered
        s = add_suggestion(L, Z, sid="s0030-1", first=20)
        other = add_suggestion(L, (6.0, 6.0), sid="s0031-1", first=5)
        L.review.touch()
        self.assertTrue(movedfrom.mask_match(L, s))
        self.assertFalse(movedfrom.mask_match(L, other))
        ids = [i.id for i in L.review.items(L)]
        self.assertNotIn("s0030-1", ids)
        self.assertNotIn("s0001-1", ids)                                         # the zone's own old suggestion matches too
        self.assertIn("s0031-1", ids)

    def test_forget_rejections_clears_the_entries(self):
        L, rec = with_entry()
        L.night = 40
        outs = answer(L, None, "forget")
        self.assertEqual(L.zone7.mf, [])
        movedfrom.add(L, "retired", rec)
        movedfrom.clear(L)
        self.assertEqual(L.zone7.mf, [])


class NightlyIntegrationTest(unittest.TestCase):
    """The gate inside the real extraction: the old spot stays withheld until it re-qualifies on post-answer stays."""

    def test_the_old_spot_of_a_retired_zone_is_withheld_then_lifted(self):
        L = make()
        for d in range(7):
            desk_day(L, d)
        xnight(L, 7)
        desk_day(L, 7)
        xnight(L, 8)
        desk_day(L, 8)
        outs = xnight(L, 9)
        self.assertEqual(len([s for s in L.suggestions() if s.type == "dwell_zone"]), 1)
        sid = "s0009-1"
        answer(L, sid, "approve", name="Desk")
        zrec = L.review.zones[0]
        movedfrom.add(L, "retired", zrec)                                        # what a retire answer does
        ZN.remove(L, zrec["zid"])
        day = L.zone7.mf[0]["day"]
        self.assertEqual(day, D0 + 9)
        g7 = []
        for k in range(9, 14):
            desk_day(L, k)
            xnight(L, k + 1)
            c = next(c for c in L.candidates() if abs(c.cx_m - 2.55) < 0.4)
            g7.append(c.gates["g7_suppression"])
        # the post-event stays are those of days 10 and later: day 9 + k ... the gate lifts at the 4th post-event core day
        self.assertEqual(g7[:3], [False, False, False])
        self.assertTrue(g7[-1])
        self.assertEqual(L.zone7.mf, [])                                         # released by step 2 of the P7 block
        self.assertEqual(L.p7_errors, 0)


if __name__ == "__main__":
    unittest.main()
