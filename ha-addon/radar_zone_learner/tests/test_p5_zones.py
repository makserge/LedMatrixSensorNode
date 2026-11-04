"""P5 zones: the record, `Learner.zones()`, merge, modify and 'No, it's a new place', the proposal rules behind
`zone_change_items` (spec 4, §11.3 `test_p5_zones`)."""
import dataclasses
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core import zones as ZN
from radar_zone_learner.core import zonework as ZW
from radar_zone_learner.core.extraction import _new_suggestion
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.params import P5Params
from radar_zone_learner.core.types import (DebugMapChanged, FixedOffsetTime, NodeStatus, ScheduleState, SpotName,
                                           ZoneView)

from .test_extraction import BASE_MS, DAY, cover, night, stay
from .test_p5_answers import ans, echoes, ignored, saves
from .test_p5_items import BED_ITEM, POS, evidence, ids, places
from .test_review import add_place, ready_learner

T8 = BASE_MS + 8 * DAY * 1000
FLAG = P5Params(zone_change_items=True)

Z_POS = (2.0, 4.0)
S_POS = (2.5, 4.0)               # 0.5 m to the right: inside the dilated zone, no shared cell
HOURS = [0.0] * 24
for _h in (9, 10, 11):
    HOURS[_h] = 50.0
HOURS[14] = 25.0


def flag_learner(p5=FLAG):
    L = Learner(P, FixedOffsetTime(0), None, p5)
    L.handle(NodeStatus(True, BASE_MS))
    night(L, 7)
    return L


def ev_hours(hours=HOURS):
    return {"n": 20, "hours_wd": list(hours), "hours_we": [0.0] * 24, "hours_at_suggestion": list(hours)}


def zone_stays(L, pos, hours=(9, 10, 11), extra=(14,)):
    """Stays of the 14 covered days at pos, so that the zone's own hour profile exists (spec 4.7)."""
    for d in range(7):
        cover(L, d)
        for h in hours:
            stay(L, d, h, 3000, pos[0], pos[1])
        for h in extra:
            stay(L, d, h, 1500, pos[0], pos[1])
    L.review.touch()


def merge_scene(p5=FLAG, s_hours=HOURS, sched=None):
    """A zone 'Desk' at Z_POS (approved) and a new place S at S_POS with a similar hour profile."""
    L = flag_learner(p5)
    add_place(L, Z_POS, sid="s0005-1", conf=0.9, evidence=ev_hours())
    if sched is not None:
        L.handle(ScheduleState(sched, 0, 360, T8))
    ans(L, "s0005-1", "approve", name="Desk")
    zone_stays(L, Z_POS)
    zone_stays(L, S_POS)                                               # the place's own stays: an answer refreshes its profile
    add_place(L, S_POS, sid="s0006-1", conf=0.8, evidence=ev_hours(s_hours))
    L.review.touch()
    return L


class RecordTest(unittest.TestCase):
    def test_a_new_zone_record_has_every_field(self):
        L = places(1)
        L.handle(SpotName("Desk", T8))
        ans(L, "s0005-1", "approve")
        z = L.review.zones[0]
        s = L.ns.suggestions[0]
        self.assertEqual(set(z), {"zid", "name", "kind", "src", "ref", "cx", "cy", "cells", "night", "created_s",
                                  "poly_cm", "rev", "upd", "srev", "also"})
        self.assertEqual((z["zid"], z["name"], z["kind"], z["src"], z["ref"]),
                         ("z0001", "Desk", "dwell", "approve", "s0005-1"))
        self.assertEqual((z["cx"], z["cy"], z["cells"]), (s["cx"], s["cy"], frozenset(s["cells"])))
        self.assertEqual((z["night"], z["rev"], z["upd"], z["srev"], z["also"]), (7, 0, 7, s["rev"], []))
        self.assertEqual(z["created_s"] % 300, 0)
        self.assertTrue(3 <= len(z["poly_cm"]) <= 12)
        s["cells"] = frozenset([1])                                    # the suggestion may change later: the zone keeps its own
        self.assertNotEqual(z["cells"], s["cells"])

    def test_the_polygon_lies_within_the_field(self):
        L = places(3)
        for sid in ("s0005-1", "s0005-2", "s0005-3"):
            ans(L, sid, "approve")
        for z in L.zones():
            self.assertTrue(3 <= len(z.poly_cm) <= 12, z.id)
            for x, y in z.poly_cm:
                self.assertTrue(-700 <= x <= 700 and 0 <= y <= 800, (z.id, x, y))
                self.assertIsInstance(x, int)

    def test_ids_are_stable_and_the_revision_rises_only_on_a_change(self):
        L = places(2)
        ans(L, "s0005-1", "approve", name="Desk")
        ans(L, "z0001", "rename", name="Desk")                         # the same name: no change
        self.assertEqual(L.review.zones[0]["rev"], 0)
        ans(L, "z0001", "rename", name="Office")
        self.assertEqual((L.review.zones[0]["zid"], L.review.zones[0]["rev"], L.review.zones[0]["upd"]),
                         ("z0001", 1, 7))
        ans(L, "z0001", "rename", name="Bed")
        self.assertEqual((L.review.zones[0]["rev"], L.review.zones[0]["kind"]), (2, "bed"))
        self.assertEqual(L.zones()[0].rev, 2)


class ViewTest(unittest.TestCase):
    def test_records_then_labels_in_id_order(self):
        L = places(3)
        ans(L, "s0005-2", "approve", name="B")
        ans(L, "s0005-1", "approve", name="A")
        L.label_list.append({"id": "l0002", "created_s": BASE_MS // 1000, "x": 0.0, "y": 7.9, "name": "", "kind": "manual",
                             "cells": L.geom.cells_within(0.0, 7.9, 0.4)})
        L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000 + 3 * 86400, "x": -6.0, "y": 7.9,
                             "name": "Plant", "kind": "bed", "cells": L.geom.cells_within(-6.0, 7.9, 0.4)})
        v = L.zones()
        self.assertEqual([x.id for x in v], ["z0001", "z0002", "l0001", "l0002"])
        self.assertTrue(all(isinstance(x, ZoneView) for x in v))
        z1, z2, l1, l2 = v
        self.assertEqual((z1.name, z1.kind, z1.src, z1.ref, z1.rev), ("B", "dwell", "approve", "s0005-2", 0))
        self.assertEqual((z2.name, z2.src, z2.ref), ("A", "approve", "s0005-1"))
        self.assertEqual((l1.name, l1.kind, l1.src, l1.ref, l1.rev), ("Plant", "bed", "mark", None, 0))
        self.assertEqual((l2.name, l2.kind), ("", "manual"))
        self.assertEqual(l1.night, 3)                                  # from created_s
        self.assertTrue(3 <= len(l1.poly_cm) <= 12)
        self.assertIsInstance(l1.cells, frozenset)
        self.assertAlmostEqual(z1.cx_m, POS[1][0], delta=0.01)

    def test_frozen_is_the_oa_74_predicate(self):
        L = places(2)
        ans(L, "s0005-1", "approve", name="Bett")                     # bed
        ans(L, "s0005-2", "approve", name="Chair")
        L.ns.suggestions[1]["evidence"]["hours_at_suggestion"] = [0.0] * 24
        L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": 0.0, "y": 7.9, "name": "Spot",
                             "kind": "manual", "cells": frozenset()})
        L.label_list.append({"id": "l0002", "created_s": BASE_MS // 1000, "x": 1.0, "y": 7.9, "name": "Bed2",
                             "kind": "bed", "cells": frozenset()})
        self.assertEqual([z.frozen for z in L.zones()], [False] * 4)   # the schedule is not known to be ON
        L.handle(ScheduleState(False, 0, 360, T8))
        self.assertEqual([z.frozen for z in L.zones()], [False] * 4)   # OFF
        L.handle(ScheduleState(True, 0, 360, T8 + 1))
        self.assertEqual([z.frozen for z in L.zones()], [True, False, False, True])   # beds only
        L.ns.suggestions[1]["evidence"]["hours_at_suggestion"] = [0.0] * 24
        L.ns.suggestions[1]["evidence"]["hours_at_suggestion"][2] = 90.0       # 100% of its hours inside 00:00-06:00
        self.assertTrue(L.zones()[1].frozen)

    def test_a_zone_whose_suggestion_is_gone_is_frozen_only_as_a_bed(self):
        L = places(2)
        ans(L, "s0005-1", "approve", name="Bett")
        ans(L, "s0005-2", "approve", name="Chair")
        L.ns.suggestions.clear()
        L.handle(ScheduleState(True, 0, 360, T8))
        self.assertEqual([z.frozen for z in L.zones()], [True, False])

    def test_zones_changes_are_visible_on_the_learner_debug_map(self):
        L = places(2)
        self.assertEqual(L.debug_map().zones, ())
        outs = ans(L, "s0005-1", "approve", name="Bett")
        self.assertIn(DebugMapChanged("review"), outs)
        L.label_list.append({"id": "l0001", "created_s": 0, "x": 0.0, "y": 7.9, "name": "X", "kind": "manual",
                             "cells": frozenset([5])})
        zm = L.debug_map().zones
        self.assertEqual([b for _, b in zm], [True, False])
        self.assertEqual(zm[1][0], frozenset([5]))


class RemoveRenameTest(unittest.TestCase):
    def test_unique_name_helper(self):
        L = places(1)
        self.assertEqual(ZN.unique_name(L, "Desk"), "Desk")
        ans(L, "s0005-1", "approve", name="Desk")
        self.assertEqual(ZN.unique_name(L, "desk"), "desk 2")
        self.assertEqual(ZN.unique_name(L, "desk", exclude_id="z0001"), "desk")
        self.assertEqual(ZN.unique_name(L, "  Desk  "), "Desk 2")


class ProposalFlagTest(unittest.TestCase):
    def test_with_the_flag_off_nothing_is_proposed_and_no_item_exists(self):
        L = merge_scene(P5Params())
        self.assertEqual(ZW.proposals(L), ([], []))
        self.assertEqual(ids(L, 2), ["s0006-1"])                       # an ordinary place item
        self.assertFalse(P5Params().zone_change_items)

    def test_with_the_flag_on_the_place_becomes_a_merge_item(self):
        L = merge_scene()
        merges, mods = ZW.proposals(L)
        self.assertEqual(mods, [])
        self.assertEqual(len(merges), 1)
        m = merges[0]
        self.assertEqual((m["sid"], m["zid"], m["zname"], m["ext_cm"], m["dir"]), ("s0006-1", "z0001", "Desk", 50,
                                                                                    "to the right"))
        self.assertGreaterEqual(m["r"], 0.9)
        self.assertEqual(ids(L, 2), ["mrg-s0006-1"])                   # instead of the plain place item
        it = [i for i in L.review.items(L) if i.id == "mrg-s0006-1"][0]
        self.assertEqual((it.type, it.tier, it.rev), ("merge_into", 2, L.ns.suggestions[1]["rev"]))
        self.assertEqual(set(it.data), {"tag", "sid", "zid", "zname", "ext_cm", "dir", "r"})
        self.assertEqual(it.data["tag"], L.p5.tags["s0006-1"])         # the tag of its suggestion

    def test_rules(self):
        # the profile must correlate (r >= 0.6): the opposite hours do not merge
        night_hours = [0.0] * 24
        night_hours[2] = night_hours[3] = night_hours[4] = 50.0
        L = merge_scene(s_hours=night_hours)
        self.assertEqual(ZW.proposals(L)[0], [])
        self.assertEqual(ids(L, 2), ["s0006-1"])
        # no hours at all: no variance, r is 0
        L = merge_scene(s_hours=[0.0] * 24)
        self.assertEqual(ZW.proposals(L)[0], [])
        # not touching: too far
        L = merge_scene()
        s = L.ns.suggestions[1]
        s["cells"] = L.geom.cells_within(5.5, 4.0, 0.3)
        s["cx"], s["cy"] = 5.5, 4.0
        L.review.touch()
        self.assertEqual(ZW.proposals(L)[0], [])
        # the same place seen again (overlap coefficient >= 0.5) stays an ordinary place item
        L = merge_scene()
        s = L.ns.suggestions[1]
        s["cells"] = L.geom.cells_within(Z_POS[0] + 0.1, Z_POS[1], 0.3)
        s["cx"], s["cy"] = Z_POS[0] + 0.1, Z_POS[1]
        L.review.touch()
        self.assertEqual(ZW.proposals(L)[0], [])
        self.assertEqual(ids(L, 2), ["s0006-1"])
        # a source that is the zone's own suggestion or merged already
        L = merge_scene()
        L.review.zones[0]["also"] = ["s0006-1"]
        L.review.touch()
        self.assertEqual(ZW.proposals(L)[0], [])

    def test_bed_and_frozen_zones_are_never_targets(self):
        L = merge_scene()
        L.review.zones[0]["kind"] = "bed"
        L.review.touch()
        self.assertEqual(ZW.proposals(L), ([], []))
        L = merge_scene(sched=True)                                    # a non-bed zone frozen by its hours (OA 7.4)
        L.ns.suggestions[0]["evidence"]["hours_at_suggestion"] = [0.0, 0.0, 90.0] + [0.0] * 21
        L.review.touch()
        self.assertEqual(ZW.proposals(L), ([], []))

    def test_the_best_zone_wins_highest_r_then_lowest_id(self):
        L = merge_scene()
        add_place(L, (3.6, 4.5), sid="s0007-1", conf=0.7, evidence=ev_hours())
        ans(L, "s0007-1", "approve", name="Shelf")
        shifted = [0.0] * 24
        shifted[9] = shifted[10] = 50.0
        for h in range(7):
            stay(L, h, 12, 3000, 3.6, 4.5)                             # a poorer profile than the desk's
        L.review.touch()
        merges = ZW.proposals(L)[0]
        self.assertEqual([(m["sid"], m["zid"]) for m in merges], [("s0006-1", "z0001")])


class MergeAnswerTest(unittest.TestCase):
    def test_a_stale_revision_is_refused(self):
        L = merge_scene()
        rev = [i.rev for i in L.review.items(L) if i.id == "mrg-s0006-1"][0]
        outs = ans(L, "mrg-s0006-1", "approve", rev=rev + 1)
        self.assertEqual(ignored(outs), [("mrg-s0006-1", "approve", "stale")])
        self.assertEqual((L.review.zones[0]["rev"], L.review.zones[0]["also"]), (0, []))
        self.assertEqual(L.review.stale_answers, 1)

    def test_approve_extends_the_zone(self):
        L = merge_scene()
        z = L.review.zones[0]
        before = (z["zid"], z["name"], set(z["cells"]), z["rev"])
        s = L.ns.suggestions[1]
        rev = [i.rev for i in L.review.items(L) if i.id == "mrg-s0006-1"][0]
        outs = ans(L, "mrg-s0006-1", "approve", rev=rev)
        self.assertEqual(ignored(outs), [])
        self.assertEqual(len(L.review.zones), 1)
        self.assertEqual((z["zid"], z["name"]), before[:2])           # the id and the name do not change
        self.assertEqual(z["cells"], frozenset(before[2]) | frozenset(s["cells"]))
        self.assertEqual((z["rev"], z["upd"], z["also"]), (1, 7, ["s0006-1"]))
        self.assertAlmostEqual(z["cx"], (Z_POS[0] + S_POS[0]) / 2, delta=0.1)
        self.assertTrue(3 <= len(z["poly_cm"]) <= 12)
        self.assertEqual((saves(outs), DebugMapChanged("review") in outs), (["review"], True))
        self.assertEqual(ids(L, 2), [])                                # covered through `also`
        self.assertEqual(ignored(ans(L, "mrg-s0006-1", "approve", rev=rev)),
                         [("mrg-s0006-1", "approve", "no_such_item")])

    def test_also_keeps_the_newest_eight(self):
        L = merge_scene()
        z = L.review.zones[0]
        z["also"] = ["s%04d-1" % k for k in range(10, 18)]
        L.review.touch()
        ans(L, "mrg-s0006-1", "approve")
        self.assertEqual(len(z["also"]), 8)
        self.assertEqual(z["also"][-1], "s0006-1")
        self.assertNotIn("s0010-1", z["also"])

    def test_rename_makes_a_new_place_and_leaves_the_zone(self):
        L = merge_scene()
        z = dict(L.review.zones[0])
        outs = ans(L, "mrg-s0006-1", "rename", name="Shelf")
        self.assertEqual(len(L.review.zones), 2)
        self.assertEqual(L.review.zones[0], z)                        # untouched
        new = L.review.zones[1]
        self.assertEqual((new["name"], new["ref"], new["zid"]), ("Shelf", "s0006-1", "z0002"))
        outs = ans(L, "mrg-s0006-1", "rename", name="Again")
        self.assertEqual(ignored(outs), [("mrg-s0006-1", "rename", "no_such_item")])

    def test_rename_without_a_name_takes_the_auto_name(self):
        L = merge_scene()
        outs = ans(L, "mrg-s0006-1", "rename")
        self.assertEqual((ignored(outs), L.review.zones[1]["name"]), ([], "Place 2"))

    def test_reject_rejects_the_suggestion_as_a_place(self):
        L = merge_scene()
        outs = ans(L, "mrg-s0006-1", "reject")
        self.assertEqual(L.review.suppressions, [{"type": "place", "ref": "s0006-1", "night": 7}])
        self.assertIn("s0006-1", L.p5.masks)
        self.assertEqual(ids(L, 2), [])
        self.assertEqual(len(L.review.zones), 1)

    def test_later_hides_the_merge_item_and_the_plain_place_item(self):
        L = merge_scene()
        outs = ans(L, "mrg-s0006-1", "later")
        self.assertEqual(list(L.p5.later), ["mrg-s0006-1"])
        self.assertEqual(ids(L, 2), [])                                # neither the merge nor the place item
        night(L, 8)
        self.assertEqual(ids(L, 2), ["mrg-s0006-1"])

    def test_a_frozen_or_bed_target_is_not_applicable(self):
        L = merge_scene()
        item = "mrg-s0006-1"
        prop = L.review.resolve(L, item)[1]
        L.review.zones[0]["kind"] = "bed"
        outs = []
        ev = __import__("radar_zone_learner.core.types", fromlist=["x"]).ReviewAnswer(item, "approve", None, False, T8)
        self.assertFalse(L.review._answer_merge(L, ev, item, prop, outs, T8))
        self.assertEqual(ignored(outs), [(item, "approve", "not_applicable")])
        self.assertEqual(L.review.zones[0]["also"], [])

    def test_a_missing_target_zone_is_no_such_item(self):
        L = merge_scene()
        item = "mrg-s0006-1"
        prop = L.review.resolve(L, item)[1]
        L.review.zones.clear()
        from radar_zone_learner.core.types import ReviewAnswer
        outs = []
        self.assertFalse(L.review._answer_merge(L, ReviewAnswer(item, "approve", None, False, T8), item, prop, outs, T8))
        self.assertEqual(ignored(outs), [(item, "approve", "no_such_item")])


def modify_scene():
    """A zone 'Desk' made from s0005-1 whose suggestion was later replaced by a new region (rev 2)."""
    L = flag_learner()
    add_place(L, Z_POS, sid="s0005-1", conf=0.9, evidence=ev_hours())
    ans(L, "s0005-1", "approve", name="Desk")
    s = L.ns.suggestions[0]
    s["rev"] = 2
    s["cells"] = L.geom.cells_within(Z_POS[0] + 0.9, Z_POS[1], 0.3)
    s["cx"], s["cy"] = Z_POS[0] + 0.9, Z_POS[1]
    L.review.touch()
    return L


class ModifyAnswerTest(unittest.TestCase):
    def test_the_item(self):
        L = modify_scene()
        merges, mods = ZW.proposals(L)
        self.assertEqual((merges, len(mods)), ([], 1))
        self.assertEqual((mods[0]["zid"], mods[0]["sid"], mods[0]["shift_cm"], mods[0]["dir"], mods[0]["rev"]),
                         ("z0001", "s0005-1", 90, "to the right", 2))
        it = [i for i in L.review.items(L) if i.id == "mod-z0001"][0]
        self.assertEqual((it.type, it.tier, it.rev), ("modify_zone", 2, 2))
        self.assertEqual(set(it.data), {"tag", "sid", "zid", "zname", "shift_cm", "dir"})
        self.assertEqual(it.data["tag"], L.p5.tags["s0005-1"])
        self.assertEqual(ids(L, 2), ["mod-z0001"])

    def test_no_proposal_for_a_small_change_an_old_revision_a_bed_or_a_stale_suggestion(self):
        L = modify_scene()
        L.ns.suggestions[0]["cells"] = L.review.zones[0]["cells"]     # the same region: IoU 1
        L.review.touch()
        self.assertEqual(ZW.proposals(L)[1], [])
        L = modify_scene()
        L.review.zones[0]["srev"] = 2                                 # asked about this revision already
        L.review.touch()
        self.assertEqual(ZW.proposals(L)[1], [])
        L = modify_scene()
        L.review.zones[0]["kind"] = "bed"
        L.review.touch()
        self.assertEqual(ZW.proposals(L)[1], [])
        L = modify_scene()
        L.ns.suggestions[0]["state"] = "stale"
        L.review.touch()
        self.assertEqual(ZW.proposals(L)[1], [])
        L = modify_scene()
        L.review.zones[0]["ref"] = None
        L.review.touch()
        self.assertEqual(ZW.proposals(L)[1], [])

    def test_approve_updates_the_zone_and_keeps_its_id_and_name(self):
        L = modify_scene()
        s = L.ns.suggestions[0]
        outs = ans(L, "mod-z0001", "approve", rev=2)
        z = L.review.zones[0]
        self.assertEqual(ignored(outs), [])
        self.assertEqual((z["zid"], z["name"], z["rev"], z["srev"], z["upd"]), ("z0001", "Desk", 1, 2, 7))
        self.assertEqual((z["cells"], z["cx"], z["cy"]), (frozenset(s["cells"]), s["cx"], s["cy"]))
        self.assertEqual((saves(outs), DebugMapChanged("review") in outs), (["review"], True))
        self.assertEqual(ids(L, 2), [])
        self.assertEqual(ignored(ans(L, "mod-z0001", "approve", rev=2)), [("mod-z0001", "approve", "no_such_item")])

    def test_a_stale_revision_is_refused(self):
        L = modify_scene()
        outs = ans(L, "mod-z0001", "approve", rev=1)
        self.assertEqual(ignored(outs), [("mod-z0001", "approve", "stale")])
        self.assertEqual(L.review.zones[0]["rev"], 0)

    def test_rename_makes_a_new_zone_and_clears_the_old_ref(self):
        L = modify_scene()
        outs = ans(L, "mod-z0001", "rename", name="Desk 2")
        old, new = L.review.zones
        self.assertEqual((old["ref"], old["srev"], old["name"]), (None, 2, "Desk"))
        self.assertEqual((new["name"], new["ref"], new["zid"]), ("Desk 2", "s0005-1", "z0002"))
        self.assertEqual(ids(L, 2), [])
        self.assertEqual(saves(outs), ["review"])

    def test_reject_sets_srev_so_it_is_not_asked_again_for_this_change(self):
        L = modify_scene()
        outs = ans(L, "mod-z0001", "reject")
        self.assertEqual((L.review.zones[0]["srev"], L.review.zones[0]["cells"] == frozenset(
            L.geom.cells_within(Z_POS[0], Z_POS[1], 0.3))), (2, True))
        self.assertEqual((ids(L, 2), L.review.suppressions), ([], []))   # no rejection record
        L.ns.suggestions[0]["rev"] = 3                                # the suggestion's cells were replaced again
        L.review.touch()
        self.assertEqual(ids(L, 2), ["mod-z0001"])

    def test_later_snoozes_to_the_next_nightly_run(self):
        L = modify_scene()
        ans(L, "mod-z0001", "later")
        self.assertEqual(list(L.p5.later), ["mod-z0001"])
        self.assertEqual(ids(L, 2), [])
        night(L, 8)
        self.assertEqual(ids(L, 2), ["mod-z0001"])
        self.assertEqual(L.p5.later, {})

    def test_a_frozen_zone_is_not_applicable(self):
        L = modify_scene()
        from radar_zone_learner.core.types import ReviewAnswer
        prop = L.review.resolve(L, "mod-z0001")[1]
        L.review.zones[0]["kind"] = "bed"
        outs = []
        self.assertFalse(L.review._answer_modify(L, ReviewAnswer("mod-z0001", "approve", None, False, T8), "mod-z0001",
                                                 prop, outs, T8))
        self.assertEqual(ignored(outs), [("mod-z0001", "approve", "not_applicable")])


class FlagOffTest(unittest.TestCase):
    def test_merge_and_modify_ids_are_unknown_with_the_flag_off(self):
        L = Learner(P, FixedOffsetTime(0))
        L.handle(NodeStatus(True, BASE_MS))
        night(L, 7)
        add_place(L, Z_POS, sid="s0005-1", conf=0.9, evidence=ev_hours())
        ans(L, "s0005-1", "approve", name="Desk")
        self.assertEqual(ignored(ans(L, "mrg-s0005-1", "approve")), [("mrg-s0005-1", "approve", "no_such_item")])
        self.assertEqual(ignored(ans(L, "mod-z0001", "approve")), [("mod-z0001", "approve", "no_such_item")])


if __name__ == "__main__":
    unittest.main()
