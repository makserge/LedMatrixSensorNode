"""P5 suppressions: rejection masks, matching, gate 7, the re-ask rule, 'real place' markers, eviction, `fill_masks` and
the nightly tidy-up (spec 5, §11.3 `test_p5_suppress`)."""
import types
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core import geometry
from radar_zone_learner.core import rejections as RJ
from radar_zone_learner.core.types import AdvancedChanged

from .test_extraction import BASE_S, DAY, DESK, MIRROR, cand_at, desk_day, make, night, places, stay
from .test_p5_answers import ans, ignored
from .test_p5_items import POS, evidence, ids
from .test_p5_items import places as place_learner
from .test_review import add_place, ready_learner

BLOCK = frozenset(j * 70 + i for j in (10, 11) for i in range(10, 15))          # 10 cells
FAR = [30 * 0 + 20 * 70 + 40 + k for k in range(10)]                             # cells far from BLOCK


def cand(cells, cx=5.0, cy=7.0, n=5):
    return types.SimpleNamespace(cells=frozenset(cells), cx=cx, cy=cy, n=n)


def rejected_block(n0=18, night_no=7):
    """A ready learner in which a suggestion with the cells BLOCK was rejected at night `night_no`."""
    L = ready_learner()
    s = add_place(L, (-4.0, 1.5), sid="s0005-1", conf=0.8, evidence=evidence(n0))
    s["cells"] = frozenset(BLOCK)
    s["cx"], s["cy"] = -5.0, 3.0
    L.review.touch()
    L.night = night_no
    ans(L, "s0005-1", "reject")
    return L


class RecordTest(unittest.TestCase):
    def test_the_suppression_keeps_the_p4_form_and_the_mask_lives_in_p5(self):
        L = rejected_block()
        self.assertEqual(L.review.suppressions, [{"type": "place", "ref": "s0005-1", "night": 7}])
        self.assertEqual(L.export_state()["suppressions"], [{"type": "place", "night": 7, "ref": "s0005-1"}])
        m = L.p5.masks["s0005-1"]
        self.assertEqual((m["cx"], m["cy"], m["n0"], m["cells"]), (-5.0, 3.0, 18, BLOCK))
        self.assertEqual(L.export_state()["p5"]["masks"]["s0005-1"]["n0"], 18)

    def test_a_second_reject_of_the_same_suggestion_is_not_pending(self):
        L = rejected_block()
        self.assertEqual(ignored(ans(L, "s0005-1", "reject")), [("s0005-1", "reject", "not_pending")])
        self.assertEqual(len(L.review.suppressions), 1)

    def test_a_suggestion_with_empty_evidence_gets_n0_zero(self):
        L = ready_learner()
        add_place(L, POS[0], sid="s0005-1", conf=0.8)
        ans(L, "s0005-1", "reject")
        self.assertEqual(L.p5.masks["s0005-1"]["n0"], 0)
        L.ns.suggestions[0]["evidence"] = {"n": -3}
        self.assertEqual(RJ.evidence_n(L.ns.suggestions[0]), 0)
        self.assertEqual(RJ.evidence_n({"evidence": {"n": 7.9}}), 7)
        self.assertEqual(RJ.evidence_n({}), 0)


class MatchingTest(unittest.TestCase):
    def blocked(self, L, c, night_no=8):
        return RJ.blocks_candidate(L, c, night_no)

    def test_the_overlap_coefficient_boundary(self):
        L = rejected_block()
        first5 = sorted(BLOCK)[:5]
        self.assertAlmostEqual(geometry.overlap_coef(frozenset(first5 + FAR[:5]), BLOCK), 0.5)
        self.assertTrue(self.blocked(L, cand(first5 + FAR[:5])))                   # 0.5: blocked (>=)
        self.assertFalse(self.blocked(L, cand(sorted(BLOCK)[:4] + FAR[:6])))      # 0.4: free
        self.assertFalse(self.blocked(L, cand(FAR)))

    def test_the_centroid_in_the_mask_blocks_whatever_the_overlap(self):
        L = rejected_block()
        x, y = L.geom.cell_xy(sorted(BLOCK)[0])
        c = cand([sorted(BLOCK)[0]] + FAR[:9], cx=x, cy=y)
        self.assertLess(geometry.overlap_coef(c.cells, BLOCK), 0.5)
        self.assertTrue(self.blocked(L, c))

    def test_no_mask_no_block(self):
        L = ready_learner()
        self.assertFalse(self.blocked(L, cand(BLOCK)))
        self.assertEqual(RJ.release_due(L, [cand(BLOCK)], 99), set())

    def test_a_suppression_without_a_mask_blocks_only_by_id(self):
        L = ready_learner()
        L.review.suppressions.append({"type": "place", "ref": "s0003-1", "night": 3})   # a P4 file with no suggestion left
        self.assertFalse(self.blocked(L, cand(BLOCK)))

    def test_an_open_item_never_contains_the_centre_of_a_rejected_region(self):
        """P5-6 (2): the item's overlap coefficient with the mask is below 0.5 and its own centroid is outside the
        mask, but the mask's centroid cell lies inside the item's cells: it is not listed (gate 7 is unchanged)."""
        L = rejected_block()
        mask = L.p5.masks["s0005-1"]
        cx, cy = L.geom.cell_xy(sorted(BLOCK)[0])
        mask["cx"], mask["cy"] = cx, cy                                      # the mask's centre is its first cell
        big = add_place(L, (4.0, 5.0), sid="s0006-1", conf=0.9, evidence=evidence())
        big["cells"] = frozenset([sorted(BLOCK)[0]]) | frozenset(FAR)       # holds that cell, overlap 1 / 10
        big["cx"], big["cy"] = 5.0, 7.0
        L.review.touch()
        self.assertLess(geometry.overlap_coef(big["cells"], BLOCK), 0.5)
        self.assertEqual(ids(L, 2), [])
        self.assertFalse(RJ.blocks_candidate(L, cand(big["cells"], cx=5.0, cy=7.0), 8))   # gate 7: the spec 5.2 rule

    def test_the_open_item_predicate_uses_the_mask_too(self):
        L = rejected_block()
        far = add_place(L, (4.0, 5.0), sid="s0006-1", conf=0.9, evidence=evidence())
        near = add_place(L, (4.5, 5.0), sid="s0006-2", conf=0.9, evidence=evidence())
        near["cells"] = frozenset(sorted(BLOCK)[:6] + FAR[:4])                      # 0.6 of its own, 0.6 of the mask
        near["cx"], near["cy"] = 5.0, 7.0
        L.review.touch()
        self.assertEqual(ids(L, 2), ["s0006-1"])
        self.assertTrue(RJ.is_rejected(L, near))
        self.assertTrue(RJ.is_rejected(L, L.ns.suggestions[0]))
        self.assertFalse(RJ.is_rejected(L, far))


class ReaskTest(unittest.TestCase):
    def test_released_at_thirty_nights_and_doubled_evidence(self):
        L = rejected_block(n0=18, night_no=7)
        c = cand(BLOCK, n=36)
        self.assertTrue(RJ.blocks_candidate(L, c, 36))                              # night - 7 = 29: held
        self.assertFalse(RJ.blocks_candidate(L, c, 37))                             # 30: released
        self.assertTrue(RJ.blocks_candidate(L, cand(BLOCK, n=35), 37))              # 2 * n0 - 1: held
        self.assertFalse(RJ.blocks_candidate(L, cand(BLOCK, n=36), 100))
        self.assertEqual(RJ.release_due(L, [cand(BLOCK, n=36)], 36), set())
        self.assertEqual(RJ.release_due(L, [cand(BLOCK, n=36)], 37), {"s0005-1"})
        self.assertEqual(RJ.release_due(L, [cand(BLOCK, n=35)], 37), set())
        self.assertEqual(RJ.release_due(L, [cand(FAR, n=99)], 37), set())           # no match, nothing to release

    def test_empty_evidence_needs_two_stays(self):
        L = ready_learner()
        add_place(L, POS[0], sid="s0005-1", conf=0.8)
        L.ns.suggestions[0]["cells"] = frozenset(BLOCK)
        L.ns.suggestions[0]["cx"], L.ns.suggestions[0]["cy"] = -5.0, 3.0
        L.review.touch()
        ans(L, "s0005-1", "reject")
        self.assertTrue(RJ.blocks_candidate(L, cand(BLOCK, n=1), 50))
        self.assertFalse(RJ.blocks_candidate(L, cand(BLOCK, n=2), 50))

    def test_apply_release_deletes_the_record_the_mask_and_the_advanced_entry(self):
        L = rejected_block()
        self.assertEqual([e.id for e in L.advanced()], ["adv:undo:s0005-1"])
        self.assertTrue(RJ.apply_release(L, {"s0005-1"}))
        self.assertEqual((L.review.suppressions, L.p5.masks), ([], {}))
        self.assertEqual(L.advanced(), ())
        self.assertFalse(RJ.apply_release(L, {"s0005-1"}))

    def test_a_new_reject_makes_a_new_record_with_the_new_evidence(self):
        L = rejected_block(n0=18)
        RJ.apply_release(L, {"s0005-1"})
        L.ns.suggestions[0]["evidence"]["n"] = 40
        L.review.touch()
        L.night = 40
        ans(L, "s0005-1", "reject")
        self.assertEqual(L.review.suppressions, [{"type": "place", "ref": "s0005-1", "night": 40}])
        self.assertEqual(L.p5.masks["s0005-1"]["n0"], 40)
        self.assertTrue(RJ.blocks_candidate(L, cand(BLOCK, n=79), 99))               # the doubling starts over
        self.assertFalse(RJ.blocks_candidate(L, cand(BLOCK, n=80), 99))


class EvictionTest(unittest.TestCase):
    def test_the_oldest_place_record_goes_first_and_takes_its_mask(self):
        L = ready_learner()
        L.review.suppressions.append({"type": "bed_needs_data", "night": 1})
        L.review.suppressions.append({"type": "night_window", "until_ord": 10 ** 6, "night": 1})
        for k in range(198):
            sid = "s%04d-1" % (100 + k)
            L.review.suppressions.append({"type": "place", "ref": sid, "night": 2})
            L.p5.masks[sid] = {"cx": 0.0, "cy": 0.0, "cells": frozenset([k]), "n0": 1}
        self.assertEqual(len(L.review.suppressions), 200)
        s = add_place(L, POS[0], sid="s0005-1", conf=0.8, evidence=evidence())
        ans(L, "s0005-1", "reject")
        sup = L.review.suppressions
        self.assertEqual(len(sup), 200)
        self.assertEqual([x["type"] for x in sup[:2]], ["bed_needs_data", "night_window"])   # the old ones stay
        self.assertEqual(sup[-1]["ref"], "s0005-1")
        self.assertNotIn("s0100-1", L.p5.masks)                                     # the oldest place record went
        self.assertEqual(sup[2]["ref"], "s0101-1")
        self.assertIn("s0005-1", L.p5.masks)

    def test_without_a_place_record_the_oldest_of_any_type_goes(self):
        L = ready_learner()
        for k in range(200):
            L.review.suppressions.append({"type": "bed_needs_data", "night": k})
        add_place(L, POS[0], sid="s0005-1", conf=0.8, evidence=evidence())
        ans(L, "s0005-1", "reject")
        sup = L.review.suppressions
        self.assertEqual((len(sup), sup[0]["night"], sup[-1]["type"]), (200, 1, "place"))

    def test_the_mask_count_is_capped(self):
        L = ready_learner()
        for k in range(205):
            L.p5.masks["s%04d-1" % k] = {"cx": 0.0, "cy": 0.0, "cells": frozenset([k]), "n0": 1}
        RJ.evict(L)
        self.assertEqual(len(L.p5.masks), 200)
        self.assertNotIn("s0000-1", L.p5.masks)


class TidyTest(unittest.TestCase):
    def test_fill_masks_gives_a_p4_suppression_its_mask_when_the_suggestion_exists(self):
        L = ready_learner()
        add_place(L, POS[0], sid="s0005-1", conf=0.8, evidence=evidence(12))
        L.review.suppressions.append({"type": "place", "ref": "s0005-1", "night": 3})
        L.review.suppressions.append({"type": "place", "ref": "s0001-1", "night": 3})
        L.p5.masks["s0009-9"] = {"cx": 0.0, "cy": 0.0, "cells": frozenset([1]), "n0": 1}   # a mask without a record
        self.assertTrue(RJ.fill_masks(L))
        self.assertEqual(sorted(L.p5.masks), ["s0005-1"])
        self.assertEqual(L.p5.masks["s0005-1"]["n0"], 12)
        self.assertFalse(RJ.fill_masks(L))

    def test_the_nightly_run_deletes_place_records_that_have_no_mask_and_no_suggestion(self):
        L = ready_learner()
        add_place(L, POS[0], sid="s0005-1", conf=0.8, evidence=evidence(12))
        L.review.suppressions.append({"type": "place", "ref": "s0005-1", "night": 3})   # its suggestion exists
        L.review.suppressions.append({"type": "place", "ref": "s0001-1", "night": 3})   # its suggestion is gone
        L.review.suppressions.append({"type": "bed_needs_data", "night": 3})
        night(L, 8)
        self.assertEqual([(s["type"], s.get("ref")) for s in L.review.suppressions],
                         [("place", "s0005-1"), ("bed_needs_data", None)])
        self.assertIn("s0005-1", L.p5.masks)

    def test_a_loaded_p4_file_gets_its_masks(self):
        L = rejected_block()
        st = L.export_state()
        del st["p5"]["masks"]
        L2 = Learner(L.p, L.tm, st)
        self.assertEqual(L2.p5.masks["s0005-1"]["cells"], BLOCK)


def desk_learner():
    """The P3 desk scenario: 7 days of desk stays and three nights give the place suggestion s0009-1 at night 9."""
    L = make()
    for d in range(7):
        desk_day(L, d)
    for k in (7, 8, 9):
        night(L, k)
    return L


class NightlyGate7Test(unittest.TestCase):
    def test_a_rejected_region_fails_gate_7_and_the_suggestion_is_never_touched_again(self):
        L = desk_learner()
        s = [x for x in L.suggestions() if x.type == "dwell_zone"][0]
        self.assertEqual((s.id, s.rev, s.state), ("s0009-1", 1, "shadow"))
        self.assertEqual(ids(L, 2), ["s0009-1"])
        ans(L, "s0009-1", "reject")
        self.assertEqual(ids(L, 2), [])
        for d in range(9, 18):
            desk_day(L, d)
            night(L, d + 1)
            c = cand_at(L, DESK)
            self.assertFalse(c.gates["g7_suppression"], d)
            self.assertEqual(c.outcome, "failed", d)
            now = [x for x in L.suggestions() if x.id == "s0009-1"][0]
            self.assertEqual((now.rev, now.last_night), (1, 9), d)                  # never updated after the answer
            self.assertEqual(ids(L, 2), [], d)
        self.assertEqual([x.id for x in L.suggestions() if x.type == "dwell_zone"], ["s0009-1"])   # no new suggestion
        self.assertEqual(now.state, "stale")                                         # 7 nights unmatched
        self.assertNotIn("s0009-1", [x["id"] for x in L.status().attributes["suggestions"]])        # off the status list

    def test_undo_brings_the_suggestion_back_while_it_is_still_shadow(self):
        L = desk_learner()
        ans(L, "s0009-1", "reject")
        desk_day(L, 9)
        night(L, 10)
        self.assertEqual(ids(L, 2), [])
        outs = ans(L, "adv:undo:s0009-1", "advanced")
        self.assertEqual((ignored(outs), L.review.suppressions, L.p5.masks), ([], [], {}))
        self.assertEqual(ids(L, 2), ["s0009-1"])
        desk_day(L, 10)
        night(L, 11)
        self.assertEqual([x.id for x in L.suggestions() if x.type == "dwell_zone"], ["s0009-1"])
        self.assertEqual(cand_at(L, DESK).outcome, "suggested")

    def test_forget_lets_the_region_come_back(self):
        L = desk_learner()
        ans(L, "s0009-1", "reject")
        desk_day(L, 9)
        night(L, 10)
        self.assertEqual(cand_at(L, DESK).outcome, "failed")
        ans(L, None, "forget")
        desk_day(L, 10)
        night(L, 11)
        self.assertEqual(cand_at(L, DESK).outcome, "suggested")
        self.assertEqual(ids(L, 2), ["s0009-1"])

    def test_the_reask_rule_releases_the_region_in_the_same_run_that_matches_it(self):
        L = desk_learner()
        ans(L, "s0009-1", "reject")
        L.p5.masks["s0009-1"]["n0"] = 5                                             # a rejection made on thin evidence
        for d in range(9, 38):
            desk_day(L, d)
            night(L, d + 1)
            if d + 1 < 39:
                self.assertEqual(cand_at(L, DESK).outcome, "failed", d)
                self.assertEqual(len(L.review.suppressions), 1, d)
        self.assertEqual(len(L.review.suppressions), 1)                             # night 38: 29 nights after the answer
        desk_day(L, 38)
        night(L, 39)                                                                # night 39: 30 nights
        c = cand_at(L, DESK)
        self.assertTrue(c.gates["g7_suppression"])
        self.assertEqual(c.outcome, "suggested")
        self.assertEqual((L.review.suppressions, L.p5.masks), ([], {}))             # released: record and mask deleted
        s = [x for x in L.suggestions() if x.type == "dwell_zone"]
        self.assertEqual([x.id for x in s], ["s0009-1"])                            # matched and updated, not duplicated
        self.assertEqual((s[0].state, s[0].last_night), ("shadow", 39))
        self.assertEqual(ids(L, 2), ["s0009-1"])
        self.assertEqual(L.advanced(), ())


class DedupeSkipTest(unittest.TestCase):
    def test_a_candidate_that_gate_7_let_through_does_not_update_the_rejected_suggestion(self):
        """The candidate hits the rejected suggestion by the centroid distance (hit_dist_m, 0.5 m) while its overlap
        coefficient with the mask stays below 0.5 and its centroid cell is outside the mask: gate 7 lets it through.
        Without the skip it would update the rejected suggestion (rev + 1 when IoU < 0.6, state back to shadow); with
        it, it becomes a new suggestion. (The spec's example, IoU 0.35 with an overlap coefficient of 0.45, cannot
        exist: IoU is at most oc / (2 - oc), 0.29 for 0.45; the same hit by IoU needs oc >= 0.4615.)"""
        L = desk_learner()
        base = cand_at(L, DESK)
        cx, cy = base.cx_m, base.cy_m
        centre = L.geom.cell_of(cx, cy)
        far_from_centre = sorted(base.cells, key=lambda c: -((L.geom.cell_xy(c)[0] - cx) ** 2
                                                              + (L.geom.cell_xy(c)[1] - cy) ** 2))
        keep = far_from_centre[:2]
        self.assertNotIn(centre, keep)
        mask_cells = frozenset(keep) | frozenset(range(2400, 2400 + 8))
        oc = geometry.overlap_coef(base.cells, mask_cells)
        self.assertLess(oc, 0.5)
        self.assertEqual(centre in mask_cells, False)
        sug = [s for s in L.ns.suggestions if s["id"] == "s0009-1"][0]
        sug["cells"] = frozenset(mask_cells)                      # the suggestion, as it stood when it was rejected
        sug["cx"], sug["cy"] = cx + 0.3, cy                       # its centre is 0.3 m from the candidate's: a hit
        L.review.touch()
        ans(L, "s0009-1", "reject")
        self.assertEqual(L.p5.masks["s0009-1"]["cells"], mask_cells)
        rev, last = sug["rev"], sug["last_night"]
        desk_day(L, 9)
        night(L, 10)
        c = cand_at(L, DESK)
        self.assertEqual(c.cells, base.cells)                     # the candidate is the one the numbers were built on
        self.assertTrue(c.gates["g7_suppression"])                # not blocked: overlap below 0.5, centroid outside
        sug = [s for s in L.ns.suggestions if s["id"] == "s0009-1"][0]
        self.assertEqual((sug["rev"], sug["last_night"]), (rev, last))               # untouched
        new = [s for s in L.suggestions() if s.type == "dwell_zone" and s.id != "s0009-1"]
        self.assertEqual(len(new), 1)                             # a new suggestion and an open item
        self.assertEqual(ids(L, 2), [new[0].id])


def reflection_day(L, d, tid):
    """One day of the P3 reflection scenario: a desk and its mirror seen together (SIM: a reflection of the desk)."""
    desk_day(L, d)
    for h in (9, 11, 14):
        t0 = BASE_S + d * DAY + h * 3600
        stay(L, d, h + 0.1, 1800, MIRROR[0], MIRROR[1])
        a = types.SimpleNamespace(t0=t0, t1=t0 + 2400, cx=DESK[0], cy=DESK[1], track_id=tid)
        b = types.SimpleNamespace(t0=t0 + 360, t1=t0 + 2160, cx=MIRROR[0], cy=MIRROR[1], track_id=tid + 1)
        tid += 2
        L.covis.add(a)
        L.covis.add(b)
    return tid


class RealPlaceTest(unittest.TestCase):
    def reflection_learner(self):
        L = make()
        tid = 1
        for d in range(7):
            tid = reflection_day(L, d, tid)
        for k in (7, 8, 9):
            night(L, k)
        return L, tid

    def test_a_real_place_answer_lets_the_reflection_region_pass_gate_4(self):
        L, tid = self.reflection_learner()
        refl = [s for s in L.suggestions() if s.type == "reflection"]
        self.assertEqual(len(refl), 1)
        self.assertEqual(len(places(L)), 1)                                         # only the desk is a place
        self.assertEqual(cand_at(L, MIRROR, 0.3).outcome, "reflection")
        entry = [e for e in L.advanced() if e.verb == "real"]
        self.assertEqual([(e.id, e.kind) for e in entry], [("adv:real:" + refl[0].id, "reflection")])
        outs = ans(L, entry[0].id, "advanced")
        self.assertEqual((ignored(outs), len(L.p5.real)), ([], 1))
        self.assertEqual([e for e in L.advanced() if e.verb == "real"], [])         # a marker matches: it leaves the list
        self.assertEqual(ignored(ans(L, entry[0].id, "advanced")), [(entry[0].id, "advanced", "not_pending")])
        for d in range(7, 10):
            tid = reflection_day(L, d, tid)
            night(L, d + 1)
        m = cand_at(L, MIRROR, 0.3)
        self.assertTrue(m.gates["g4_covis"])
        self.assertEqual(m.outcome, "suggested")
        self.assertEqual(len(places(L)), 2)                                         # the mirror is a place now
        mirror = [p for p in places(L) if abs(p.cx_m - MIRROR[0]) < 0.3][0]
        self.assertAlmostEqual(mirror.conf, m.conf, places=3)
        self.assertGreaterEqual(m.conf, 0.5)

    def test_without_the_answer_the_reflection_stays_a_reflection(self):
        L, tid = self.reflection_learner()
        for d in range(7, 10):
            tid = reflection_day(L, d, tid)
            night(L, d + 1)
        self.assertEqual(cand_at(L, MIRROR, 0.3).outcome, "reflection")
        self.assertEqual(len(places(L)), 1)

    def test_the_marker_survives_forget_and_is_permanent(self):
        L, tid = self.reflection_learner()
        refl = [s for s in L.suggestions() if s.type == "reflection"][0]
        ans(L, "adv:real:" + refl.id, "advanced")
        ans(L, None, "forget")
        self.assertEqual(len(L.p5.real), 1)
        st = L.export_state()
        self.assertEqual(st["p5"]["real"][0]["ref"], refl.id)
        self.assertEqual(st["suppressions"], [])                                   # not stored as a suppression (0.4.0 loads it)
        L2 = Learner(L.p, L.tm, st)
        self.assertEqual(len(L2.p5.real), 1)

    def test_real_place_markers_are_capped_at_fifty(self):
        L = ready_learner()
        for k in range(55):
            RJ.add_real(L, {"id": "s%04d-1" % k, "cx": 0.0, "cy": 0.0, "cells": frozenset([k])}, 5)
        self.assertEqual(len(L.p5.real), 50)
        self.assertEqual(L.p5.real[0]["ref"], "s0005-1")
        self.assertTrue(RJ.real_matches(L, frozenset([54]), 9.0, 9.0))
        self.assertFalse(RJ.real_matches(L, frozenset([1000]), 9.0, 9.0))


if __name__ == "__main__":
    unittest.main()
