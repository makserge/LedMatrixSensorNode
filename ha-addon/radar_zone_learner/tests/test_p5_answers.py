"""P5 answers: the matrix of spec 3.3 x {stale, current, duplicate}, every `AnswerIgnored` reason, name consumption,
kind rules, unique names, the night window (edit times, the switch-on box, idempotency, `nw_applied`) and the zone
limit (spec 3, §11.3 `test_p5_answers`)."""
import json
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core.types import (CommandResult, DebugMapChanged, FixedOffsetTime, NameEcho, NightTimeEdit,
                                           NodeCommand, ReviewAnswer, SaveRequested, ScheduleState, SettingSwitch,
                                           SpotName, StatusChanged, AnswerIgnored)

from .test_extraction import BASE_MS, DAY
from .test_p5_items import BED_ITEM, POS, evidence, ids, places
from .test_review import NW_ITEM, add_place, items, ready_learner, with_proposal

T8 = BASE_MS + 8 * DAY * 1000


def ans(L, item, op, rev=None, sched=False, name=None, aid=None, dt=1):
    return L.handle(ReviewAnswer(item, op, rev, sched, (L.now or BASE_MS) + dt, name=name, aid=aid))


def ignored(outs):
    return [(o.item, o.op, o.reason) for o in outs if isinstance(o, AnswerIgnored)]


def saves(outs):
    return [o.reason for o in outs if isinstance(o, SaveRequested)]


def echoes(outs):
    return [o.text for o in outs if isinstance(o, NameEcho)]


def cmds(outs):
    return [o for o in outs if isinstance(o, NodeCommand)]


def rev_of(L, item):
    return [i.rev for i in L.review.items(L) if i.id == item][0]


class PlaceMatrixTest(unittest.TestCase):
    def test_approve_stale_current_duplicate(self):
        L = places(2)
        rev = rev_of(L, "s0005-1")
        outs = ans(L, "s0005-1", "approve", rev=rev - 1)                                   # stale
        self.assertEqual(ignored(outs), [("s0005-1", "approve", "stale")])
        self.assertEqual((L.review.zones, saves(outs), L.review.stale_answers), ([], [], 1))
        outs = ans(L, "s0005-1", "approve", rev=rev)                                       # current
        self.assertEqual(ignored(outs), [])
        self.assertEqual(len(L.review.zones), 1)
        self.assertEqual(L.review.zones[0]["ref"], "s0005-1")
        self.assertEqual(saves(outs), ["review"])
        self.assertIn(DebugMapChanged("review"), outs)
        self.assertNotIn("s0005-1", ids(L))
        outs = ans(L, "s0005-1", "approve", rev=rev)                                       # a QoS-1 duplicate
        self.assertEqual(ignored(outs), [("s0005-1", "approve", "not_pending")])
        self.assertEqual((len(L.review.zones), saves(outs)), (1, []))

    def test_rename_stale_current_duplicate(self):
        L = places(2)
        rev = rev_of(L, "s0005-1")
        outs = ans(L, "s0005-1", "rename", rev=rev + 1, name="Desk")
        self.assertEqual(ignored(outs), [("s0005-1", "rename", "stale")])
        outs = ans(L, "s0005-1", "rename", rev=rev, name="Desk")
        self.assertEqual((ignored(outs), L.review.zones[0]["name"]), ([], "Desk"))
        outs = ans(L, "s0005-1", "rename", rev=rev, name="Desk")
        self.assertEqual((ignored(outs), len(L.review.zones)), ([("s0005-1", "rename", "not_pending")], 1))

    def test_reject_stale_current_duplicate(self):
        L = places(2)
        rev = rev_of(L, "s0005-1")
        self.assertEqual(ignored(ans(L, "s0005-1", "reject", rev=rev - 1)), [("s0005-1", "reject", "stale")])
        self.assertEqual(L.review.suppressions, [])
        outs = ans(L, "s0005-1", "reject", rev=rev)
        self.assertEqual((ignored(outs), saves(outs)), ([], ["review"]))
        self.assertEqual(L.review.suppressions, [{"type": "place", "ref": "s0005-1", "night": 7}])
        self.assertEqual(list(L.p5.masks), ["s0005-1"])
        outs = ans(L, "s0005-1", "reject", rev=rev)
        self.assertEqual(ignored(outs), [("s0005-1", "reject", "not_pending")])
        self.assertEqual(len(L.review.suppressions), 1)
        self.assertEqual(ids(L, 2), ["s0005-2"])

    def test_later_stale_current(self):
        L = places(2)
        rev = rev_of(L, "s0005-1")
        self.assertEqual(ignored(ans(L, "s0005-1", "later", rev=rev - 1)), [("s0005-1", "later", "stale")])
        self.assertEqual(L.p5.later, {})
        outs = ans(L, "s0005-1", "later", rev=rev)
        self.assertEqual((ignored(outs), saves(outs)), ([], ["review"]))
        self.assertEqual(list(L.p5.later), ["s0005-1"])
        self.assertEqual(ids(L, 2), ["s0005-2"])

    def test_every_refused_answer_is_a_visible_output_and_forces_the_status(self):
        L = places(1)
        L.tick(T8)                                                    # the first step lists the items
        outs = ans(L, "s9999-9", "approve")
        self.assertEqual([type(o) for o in outs], [AnswerIgnored, StatusChanged])
        self.assertEqual(ignored(outs), [("s9999-9", "approve", "no_such_item")])

    def test_no_item_and_no_such_item(self):
        L = ready_learner()
        L.ns.suggestions.clear()
        L.review.suppressions.append({"type": "bed_needs_data", "night": 7})
        L.review.touch()
        self.assertEqual(L.review.selected(L), None)
        self.assertEqual(ignored(ans(L, None, "approve")), [(None, "approve", "no_item")])
        self.assertEqual(ignored(ans(L, None, "undo")), [(None, "undo", "no_item")])
        self.assertEqual(ignored(ans(L, "s0005-1", "approve")), [("s0005-1", "approve", "no_such_item")])
        self.assertEqual(ignored(ans(L, "mrg-s0005-1", "approve")), [("mrg-s0005-1", "approve", "no_such_item")])
        self.assertEqual(ignored(ans(L, "mod-z0001", "approve")), [("mod-z0001", "approve", "no_such_item")])
        self.assertEqual(ignored(ans(L, "z0009", "rename", name="x")), [("z0009", "rename", "no_such_item")])
        self.assertEqual(ignored(ans(L, "garbage", "later")), [("garbage", "later", "no_such_item")])
        self.assertEqual(ignored(ans(L, "adv:undo:bed", "approve")), [("adv:undo:bed", "approve", "not_applicable")])

    def test_not_applicable_and_not_pending_states(self):
        L = places(3)
        L.ns.suggestions[0]["state"] = "stale"
        L.ns.suggestions[1]["type"] = "reflection"
        L.review.touch()
        self.assertEqual(ignored(ans(L, "s0005-1", "approve")), [("s0005-1", "approve", "not_pending")])
        self.assertEqual(ignored(ans(L, "s0005-1", "later")), [("s0005-1", "later", "not_pending")])
        self.assertEqual(ignored(ans(L, "s0005-2", "approve")), [("s0005-2", "approve", "not_applicable")])
        self.assertEqual(L.review.zones, [])

    def test_a_scripted_caller_can_answer_a_suggestion_that_is_not_listed(self):
        """P4's condition: the suggestion is applicable; the setup review, a label, a snooze or the cap do not matter."""
        L = places(2)
        ans(L, "s0005-1", "later")                                    # snoozed: not listed
        self.assertNotIn("s0005-1", ids(L))
        outs = ans(L, "s0005-1", "approve")
        self.assertEqual((ignored(outs), len(L.review.zones)), ([], 1))
        L = places(1)
        L.night = 3                                                   # the setup review is not ready
        self.assertEqual(ids(L), [])
        ans(L, "s0005-1", "approve")
        self.assertEqual(len(L.review.zones), 1)

    def test_zone_limit(self):
        L = places(2)
        for k in range(100):
            L.review.zones.append({"zid": "z%04d" % (k + 1), "name": "Z%d" % k, "kind": "dwell", "src": "approve",
                                   "ref": None, "cx": 0.0, "cy": 0.0, "cells": frozenset(), "night": 1, "created_s": 0,
                                   "poly_cm": [], "rev": 0, "upd": 1, "srev": 0, "also": []})
        L.review.touch()
        L.handle(SpotName("Desk", T8))
        outs = ans(L, "s0005-1", "approve")
        self.assertEqual(ignored(outs), [("s0005-1", "approve", "zone_limit")])
        self.assertEqual(len(L.review.zones), 100)
        self.assertEqual(L.spot_name, "Desk")                         # a refused answer does not consume the text
        self.assertEqual(echoes(outs), [])
        self.assertEqual(ignored(ans(L, "s0005-1", "rename", name="X")), [("s0005-1", "rename", "zone_limit")])

    def test_malformed_answers_are_silent(self):
        L = places(1)
        for ev in (ReviewAnswer(5, "approve", None, False, T8), ReviewAnswer("s0005-1", None, None, False, T8),
                   ReviewAnswer("s0005-1", 7, None, False, T8), ReviewAnswer("s0005-1", "bogus", None, False, T8),
                   ReviewAnswer(["s0005-1"], "approve", None, False, T8)):
            outs = L.handle(ev)
            self.assertEqual([o for o in outs if isinstance(o, (AnswerIgnored, SaveRequested, NodeCommand))], [], ev)
        self.assertEqual(L.review.zones, [])


class NameTest(unittest.TestCase):
    def test_the_name_text_names_the_zone_and_is_consumed(self):
        L = places(2)
        L.handle(SpotName("Desk", T8))
        outs = ans(L, "s0005-1", "approve")
        self.assertEqual(L.review.zones[0]["name"], "Desk")
        self.assertEqual(echoes(outs), [""])                          # echoed once, empty
        self.assertEqual(saves(outs), ["review"])                     # and no SaveRequested("name") (D11)
        self.assertEqual(L.spot_name, "")
        self.assertEqual(L.export_state()["settings"], {"spot_name": ""})
        ans(L, "s0005-2", "approve")                                  # the next approve gets the auto name
        self.assertEqual(L.review.zones[1]["name"], "Place 2")

    def test_the_regression_of_the_p4_pins(self):
        for name in ("bett", "BED", " Bed ", "Bett", "bed"):
            L = places(1)
            L.handle(SpotName(name, T8))
            outs = ans(L, "s0005-1", "approve")
            self.assertEqual((L.review.zones[0]["kind"], saves(outs)), ("bed", ["review"]), name)

    def test_an_answer_name_wins_over_the_text_and_leaves_the_text(self):
        L = places(1)
        L.handle(SpotName("Typed", T8))
        outs = ans(L, "s0005-1", "approve", name="  Given name  ")
        self.assertEqual(L.review.zones[0]["name"], "Given name")
        self.assertEqual((L.spot_name, echoes(outs)), ("Typed", []))

    def test_a_text_that_is_the_name_of_a_label_is_a_leftover_of_a_mark(self):
        L = places(2)
        L.label_list.append({"id": "l0001", "created_s": 0, "x": 5.9, "y": 0.3, "name": "Bed", "kind": "bed",
                             "cells": frozenset()})
        L.handle(SpotName("bed", T8))                                 # case-insensitive
        outs = ans(L, "s0005-1", "approve")
        z = L.review.zones[0]
        self.assertEqual((z["name"], z["kind"]), ("Place 1", "dwell"))   # not a second frozen bed zone
        self.assertEqual((L.spot_name, echoes(outs)), ("bed", []))
        L.handle(SpotName("Sofa", T8 + 1))
        ans(L, "s0005-2", "approve")
        self.assertEqual(L.review.zones[1]["name"], "Sofa")

    def test_an_answer_with_an_answer_id_never_reads_the_text(self):
        L = places(2)
        L.handle(SpotName("Desk", T8))
        outs = ans(L, "s0005-1", "approve", aid="01HV1")              # a push action: Approve, no name
        self.assertEqual(L.review.zones[0]["name"], "Place 1")
        self.assertEqual((L.spot_name, echoes(outs)), ("Desk", []))
        outs = ans(L, "s0005-2", "rename", aid="01HV2")               # a push Rename without a name
        self.assertEqual(ignored(outs), [("s0005-2", "rename", "name_needed")])
        self.assertEqual(len(L.review.zones), 1)
        ans(L, "s0005-2", "rename", aid="01HV3", name="Chair")
        self.assertEqual(L.review.zones[1]["name"], "Chair")

    def test_rename_needs_a_name(self):
        L = places(1)
        outs = ans(L, "s0005-1", "rename")
        self.assertEqual(ignored(outs), [("s0005-1", "rename", "name_needed")])
        L.handle(SpotName("   ", T8))
        self.assertEqual(ignored(ans(L, "s0005-1", "rename")), [("s0005-1", "rename", "name_needed")])
        L.handle(SpotName("Desk", T8 + 1))
        outs = ans(L, "s0005-1", "rename")
        self.assertEqual((ignored(outs), echoes(outs), L.review.zones[0]["name"]), ([], [""], "Desk"))

    def test_names_are_cut_to_32_characters(self):
        L = places(1)
        ans(L, "s0005-1", "approve", name="x" * 50)
        self.assertEqual(L.review.zones[0]["name"], "x" * 32)

    def test_names_are_unique_among_zones_and_labels(self):
        L = places(4)
        ans(L, "s0005-1", "approve", name="Desk")
        ans(L, "s0005-2", "approve", name="desk")
        ans(L, "s0005-3", "approve", name=" DESK ")
        self.assertEqual([z["name"] for z in L.review.zones], ["Desk", "desk 2", "DESK 3"])
        L.label_list.append({"id": "l0001", "created_s": 0, "x": 0.0, "y": 7.9, "name": "Sofa", "kind": "manual",
                             "cells": frozenset()})
        ans(L, "s0005-4", "approve", name="sofa")
        self.assertEqual(L.review.zones[3]["name"], "sofa 2")
        L2 = places(1)
        ans(L2, "s0005-1", "approve", name="y" * 32)
        L2.ns.suggestions.append(dict(L2.ns.suggestions[0], id="s0005-9"))
        L2.review.touch()
        ans(L2, "s0005-9", "approve", name="y" * 32)                  # the base is cut: 30 characters + " 2"
        self.assertEqual(L2.review.zones[1]["name"], "y" * 30 + " 2")
        self.assertEqual(len(L2.review.zones[1]["name"]), 32)

    def test_the_kind_follows_the_requested_name_before_the_suffix(self):
        L = places(2)
        ans(L, "s0005-1", "approve", name="Bed")
        ans(L, "s0005-2", "approve", name="Bed")                      # an existing zone named Bed: the suffix
        self.assertEqual([(z["name"], z["kind"]) for z in L.review.zones], [("Bed", "bed"), ("Bed 2", "bed")])

    def test_kind_rules(self):
        for name, kind in (("bed", "bed"), ("Bett", "bed"), (" BETT ", "bed"), ("Bed room", "dwell"),
                           ("Bedroom", "dwell"), ("Bettwäsche", "dwell"), ("Desk", "dwell")):
            L = places(1)
            ans(L, "s0005-1", "approve", name=name)
            self.assertEqual(L.review.zones[0]["kind"], kind, name)
        L = places(1, typ="short_task_zone")
        ans(L, "s0005-1", "approve", name="Counter")
        self.assertEqual(L.review.zones[0]["kind"], "short_task")
        L = places(1, typ="short_task_zone")
        ans(L, "s0005-1", "approve", name="Bett")
        self.assertEqual(L.review.zones[0]["kind"], "bed")

    def test_a_bed_zone_freezes_at_once_and_withdraws_the_bed_item(self):
        L = places(1)
        L.handle(ScheduleState(True, 0, 360, T8))
        self.assertEqual(ids(L), [BED_ITEM, "s0005-1"])
        ans(L, "s0005-1", "approve", name="Bett")
        s = L.ns.suggestions[0]
        self.assertEqual((s["kind"], s["name_hint"], s["frozen"]), ("bed", "Bett", True))
        self.assertEqual(ids(L), [])
        self.assertEqual(L.review.bed_state(L), ("withdrawn", "bed_zone"))

    def test_the_learners_own_bed_keeps_its_name_and_kind(self):
        L = places(1, kind="bed", name_hint="bed")
        ans(L, "s0005-1", "approve")
        self.assertEqual((L.review.zones[0]["kind"], L.review.zones[0]["name"]), ("bed", "bed"))
        L = places(1, name_hint="Reading chair")
        ans(L, "s0005-1", "approve")
        self.assertEqual((L.review.zones[0]["kind"], L.review.zones[0]["name"]), ("dwell", "Reading chair"))

    def test_zone_ids_are_monotonic_and_never_reused(self):
        L = places(3)
        for sid in ("s0005-1", "s0005-2"):
            ans(L, sid, "approve")
        self.assertEqual([z["zid"] for z in L.review.zones], ["z0001", "z0002"])
        L.review.zones.pop(0)
        ans(L, "s0005-3", "approve")
        self.assertEqual([z["zid"] for z in L.review.zones], ["z0002", "z0003"])
        self.assertEqual(L.review.next_zone_n, 4)


class DirectTargetTest(unittest.TestCase):
    """A zone record id or a label id as the answer's item: `rename` as P4 sends it, every other op not applicable."""

    def zone(self, name="Desk"):
        L = places(1)
        ans(L, "s0005-1", "approve", name=name)
        return L, L.review.zones[0]["zid"]

    def test_rename_a_zone_uses_the_text_consumes_it_and_bumps_the_revision(self):
        L, zid = self.zone()
        L.handle(SpotName("Office", T8))
        outs = ans(L, zid, "rename")
        z = L.review.zones[0]
        self.assertEqual((z["name"], z["rev"], z["zid"], z["upd"]), ("Office", 1, "z0001", 7))
        self.assertEqual((echoes(outs), saves(outs), L.spot_name), ([""], ["review"], ""))
        self.assertIn(DebugMapChanged("review"), outs)

    def test_a_label_text_leftover_is_not_filtered_for_a_direct_rename(self):
        L, zid = self.zone()
        L.label_list.append({"id": "l0001", "created_s": 0, "x": 5.9, "y": 0.3, "name": "Office", "kind": "manual",
                             "cells": frozenset()})
        L.handle(SpotName("Office", T8))
        outs = ans(L, zid, "rename")                                  # the user pressed Rename for this entry
        self.assertEqual(L.review.zones[0]["name"], "Office 2")

    def test_the_revision_is_checked_when_given(self):
        L, zid = self.zone()
        outs = ans(L, zid, "rename", rev=5, name="X")
        self.assertEqual(ignored(outs), [(zid, "rename", "stale")])
        self.assertEqual(L.review.zones[0]["name"], "Desk")
        outs = ans(L, zid, "rename", rev=0, name="X")
        self.assertEqual((ignored(outs), L.review.zones[0]["name"]), ([], "X"))
        outs = ans(L, zid, "rename", rev=None, name="Y")              # None: not checked (P4's tests and the replay)
        self.assertEqual(L.review.zones[0]["name"], "Y")

    def test_a_bed_name_makes_it_a_bed_and_it_is_never_downgraded(self):
        L, zid = self.zone()
        ans(L, zid, "rename", name="Bett")
        self.assertEqual((L.review.zones[0]["kind"], L.review.bed_state(L)[0]), ("bed", "withdrawn"))
        ans(L, zid, "rename", name="Sofa")
        self.assertEqual((L.review.zones[0]["name"], L.review.zones[0]["kind"]), ("Sofa", "bed"))
        self.assertEqual(L.ns.suggestions[0]["kind"], "bed")

    def test_the_same_name_again_changes_nothing_but_uses_up_the_text(self):
        L, zid = self.zone()
        L.handle(SpotName("Desk", T8))
        outs = ans(L, zid, "rename")
        self.assertEqual((L.review.zones[0]["rev"], echoes(outs), ignored(outs)), (0, [""], []))
        self.assertNotIn(DebugMapChanged("review"), outs)

    def test_rename_needs_a_name_and_other_ops_are_not_applicable(self):
        L, zid = self.zone()
        self.assertEqual(ignored(ans(L, zid, "rename")), [(zid, "rename", "name_needed")])
        for op in ("approve", "reject", "later"):
            self.assertEqual(ignored(ans(L, zid, op)), [(zid, op, "not_applicable")], op)

    def test_rename_a_label(self):
        L = places(1)
        L.label_list.append({"id": "l0001", "created_s": 0, "x": 0.0, "y": 7.9, "name": "Spot", "kind": "manual",
                             "cells": frozenset()})
        outs = ans(L, "l0001", "rename", name="Plant")
        self.assertEqual((L.label_list[0]["name"], L.label_list[0]["kind"]), ("Plant", "manual"))
        ans(L, "l0001", "rename", name="Bed")
        self.assertEqual(L.label_list[0]["kind"], "bed")
        self.assertEqual(ignored(ans(L, "l0001", "approve")), [("l0001", "approve", "not_applicable")])

    def test_a_rename_of_a_place_that_already_has_a_zone_is_not_pending(self):
        L, zid = self.zone()
        outs = ans(L, "s0005-1", "rename", name="Other")
        self.assertEqual(ignored(outs), [("s0005-1", "rename", "not_pending")])
        self.assertEqual(saves(outs), [])
        self.assertEqual(L.review.zones[0]["name"], "Desk")


class UndoForgetTest(unittest.TestCase):
    def test_undo_needs_its_item_and_something_to_undo(self):
        L = places(1)
        self.assertEqual(ignored(ans(L, BED_ITEM, "undo")), [(BED_ITEM, "undo", "not_pending")])
        self.assertEqual(ignored(ans(L, "s0005-1", "undo")), [("s0005-1", "undo", "not_applicable")])
        ans(L, BED_ITEM, "reject")
        self.assertEqual(L.review.bed_state(L)[0], "suppressed")
        outs = ans(L, BED_ITEM, "undo")
        self.assertEqual((ignored(outs), saves(outs), L.review.bed_state(L)[0]), ([], ["review"], "open"))
        self.assertEqual(ignored(ans(L, NW_ITEM, "undo")), [(NW_ITEM, "undo", "not_pending")])

    def test_forget_clears_the_snoozes_of_places_and_the_masks_too(self):
        L = places(3)
        ans(L, "s0005-1", "reject")
        ans(L, "s0005-2", "later")
        self.assertEqual((len(L.review.suppressions), list(L.p5.later), list(L.p5.masks)),
                         (1, ["s0005-2"], ["s0005-1"]))
        rev = L.review.bed_rev
        outs = ans(L, None, "forget")
        self.assertEqual(saves(outs), ["review"])
        self.assertEqual((L.review.suppressions, L.p5.later, L.p5.masks), ([], {}, {}))
        self.assertEqual(L.review.bed_rev, rev + 1)
        self.assertEqual(ids(L, 2), ["s0005-1", "s0005-2", "s0005-3"])
        outs = ans(L, None, "forget")                                 # nothing left: nothing to save, nothing said
        self.assertEqual((saves(outs), ignored(outs)), ([], []))

    def test_forget_keeps_the_real_place_markers_and_the_zones(self):
        L = places(2)
        ans(L, "s0005-1", "approve")
        L.p5.real.append({"ref": "s0004-2", "cx": 1.0, "cy": 1.0, "cells": frozenset([5]), "night": 3})
        ans(L, "s0005-2", "reject")
        ans(L, None, "forget")
        self.assertEqual((len(L.p5.real), len(L.review.zones)), (1, 1))


class NightWindowTest(unittest.TestCase):
    def learner(self):
        L = ready_learner()
        with_proposal(L)
        return L

    def test_approve_stale_current_duplicate(self):
        L = self.learner()
        rev = L.night_m.proposal_rev
        outs = ans(L, NW_ITEM, "approve", rev=rev - 1)
        self.assertEqual((ignored(outs), cmds(outs)), ([(NW_ITEM, "approve", "stale")], []))
        outs = ans(L, NW_ITEM, "approve", rev=rev)
        c = cmds(outs)
        self.assertEqual(len(c), 1)
        self.assertEqual((c[0].kind, c[0].start_min, c[0].end_min, c[0].sched, c[0].reason),
                         ("window", 1380, 375, None, "approved_window"))
        self.assertEqual((ignored(outs), saves(outs)), ([], ["review"]))
        outs = ans(L, NW_ITEM, "approve", rev=rev)                    # before the node's echo: no second command
        self.assertEqual((ignored(outs), cmds(outs)), ([(NW_ITEM, "approve", "not_pending")], []))
        L.handle(CommandResult(c[0].token, "window", True, None, T8 + 5000))
        L.handle(ScheduleState(True, 1380, 375, T8 + 5001))           # the echo: the item is gone
        self.assertEqual(ids(L), [BED_ITEM])
        outs = ans(L, NW_ITEM, "approve", rev=rev)
        self.assertEqual((ignored(outs), cmds(outs)), ([(NW_ITEM, "approve", "not_pending")], []))

    def test_a_new_revision_may_be_approved_while_an_old_command_is_open(self):
        L = self.learner()
        ans(L, NW_ITEM, "approve")
        L.night_m.onsets["radar"] = {k: 600 for k in L.night_m.onsets["radar"]}    # another proposal: a new revision
        L.night_m.refresh(L)
        self.assertEqual(len(cmds(ans(L, NW_ITEM, "approve"))), 1)

    def test_reject_later_and_the_actions_that_do_not_apply(self):
        L = self.learner()
        outs = ans(L, NW_ITEM, "later")
        self.assertEqual((ignored(outs), L.review.nw_state(L)), ([], "snoozed"))
        self.assertEqual(ignored(ans(L, NW_ITEM, "later")), [(NW_ITEM, "later", "not_pending")])
        L2 = self.learner()
        ans(L2, NW_ITEM, "reject")
        self.assertEqual(L2.review.nw_state(L2), "suppressed")
        L3 = self.learner()
        self.assertEqual(ignored(ans(L3, NW_ITEM, "rename", name="x")), [(NW_ITEM, "rename", "not_applicable")])

    def test_edit_times(self):
        L = self.learner()
        d = L.review.items(L)[0].data
        self.assertEqual((d["edit_start"], d["edit_end"], d["sched_box"]), (None, None, False))
        outs = L.handle(NightTimeEdit("start", 1320, T8 + 10))
        d = L.review.items(L)[0].data
        self.assertEqual((d["edit_start"], d["edit_end"]), (1320, None))
        rc = [o for o in outs if o.__class__.__name__ == "ReviewChanged"]
        self.assertEqual(len(rc), 1)                                  # the item data changed
        L.handle(NightTimeEdit("end", 420, T8 + 11))
        c = cmds(ans(L, NW_ITEM, "approve"))
        self.assertEqual((c[0].start_min, c[0].end_min), (1320, 420))
        self.assertIsNone(L.review.edit)                              # an approve clears it

    def test_an_edit_of_one_field_uses_the_proposal_for_the_other(self):
        L = self.learner()
        L.handle(NightTimeEdit("end", 400, T8 + 10))
        c = cmds(ans(L, NW_ITEM, "approve"))
        self.assertEqual((c[0].start_min, c[0].end_min), (1380, 400))

    def test_invalid_times_are_refused_and_keep_the_edit(self):
        L = self.learner()
        L.handle(NightTimeEdit("start", 100, T8 + 10))
        L.handle(NightTimeEdit("end", 130, T8 + 11))                  # 30 minutes
        outs = ans(L, NW_ITEM, "approve")
        self.assertEqual((ignored(outs), cmds(outs), saves(outs)), ([(NW_ITEM, "approve", "times_invalid")], [], []))
        self.assertEqual(L.review.edit["start"], 100)
        L.handle(NightTimeEdit("start", 60, T8 + 12))
        L.handle(NightTimeEdit("end", 1200, T8 + 13))                 # 1140 minutes, more than 16 hours
        self.assertEqual(ignored(ans(L, NW_ITEM, "approve")), [(NW_ITEM, "approve", "times_invalid")])
        L.handle(NightTimeEdit("end", 1020, T8 + 14))                 # exactly 960
        self.assertEqual(len(cmds(ans(L, NW_ITEM, "approve"))), 1)

    def test_the_edit_applies_to_its_proposal_revision_only(self):
        L = self.learner()
        L.handle(NightTimeEdit("start", 1320, T8 + 10))
        L.night_m.onsets["radar"] = {k: 600 for k in L.night_m.onsets["radar"]}
        L.night_m.refresh(L)
        d = L.review.items(L)[0].data
        self.assertEqual((d["edit_start"], d["edit_end"]), (None, None))
        c = cmds(ans(L, NW_ITEM, "approve"))
        self.assertEqual(c[0].start_min, L.night_m.raw["start_min"])

    def test_reject_and_later_clear_the_edit(self):
        for op in ("reject", "later"):
            L = self.learner()
            L.handle(NightTimeEdit("start", 1320, T8 + 10))
            ans(L, NW_ITEM, op)
            self.assertIsNone(L.review.edit, op)

    def test_an_edit_without_a_proposed_item_is_refused(self):
        L = ready_learner()                                           # no proposal at all
        outs = L.handle(NightTimeEdit("start", 1320, T8))
        self.assertEqual(ignored(outs), [(NW_ITEM, "edit", "no_such_item")])
        L = self.learner()
        ans(L, NW_ITEM, "later")                                      # snoozed
        self.assertEqual(ignored(L.handle(NightTimeEdit("start", 1320, T8 + 20))), [(NW_ITEM, "edit", "no_such_item")])
        L = self.learner()
        for ev in (NightTimeEdit("start", 1440, T8), NightTimeEdit("end", -1, T8), NightTimeEdit("middle", 5, T8),
                   NightTimeEdit("start", True, T8), NightTimeEdit("start", "5", T8)):
            self.assertEqual(ignored(L.handle(ev)), [], ev)           # malformed: silent
        self.assertIsNone(L.review.edit)

    def test_the_edited_window_keeps_the_item_away_after_the_echo(self):
        L = self.learner()
        L.handle(NightTimeEdit("start", 1320, T8 + 10))              # 22:00 instead of 23:00
        c = cmds(ans(L, NW_ITEM, "approve"))[0]
        L.handle(CommandResult(c.token, "window", True, None, T8 + 5000))
        L.handle(ScheduleState(True, 1320, 375, T8 + 5001))           # the node's window: 60 minutes from the proposal
        self.assertEqual(L.review.nw_state(L), "same")
        self.assertEqual(ids(L), [BED_ITEM])
        self.assertEqual(L.export_state()["p5"]["nw_applied"], {"rev": L.night_m.proposal_rev, "start": 1320, "end": 375})
        L2 = Learner(L.p, L.tm, L.export_state())                     # and after a restart
        L2.handle(ScheduleState(True, 1320, 375, T8 + 9000))
        self.assertEqual(L2.review.nw_state(L2), "same")
        # a new proposal revision ends the match
        L.night_m.proposal_rev += 1
        self.assertEqual(L.review.nw_state(L), "proposed")

    def test_an_unconfirmed_window_records_no_marker(self):
        L = self.learner()
        c = cmds(ans(L, NW_ITEM, "approve"))[0]
        outs = L.handle(CommandResult(c.token, "window", False, "no_echo", T8 + 5000))
        self.assertIsNone(L.p5.nw_applied)
        self.assertNotIn("review", saves(outs))
        self.assertEqual(ids(L)[0], NW_ITEM)
        c2 = cmds(ans(L, NW_ITEM, "approve"))                         # may be tried again
        self.assertEqual(len(c2), 1)

    def test_the_switch_on_box_is_ored_with_the_answer_key_and_ticks_once(self):
        L = self.learner()
        outs = L.handle(SettingSwitch("approve_sched", True, T8 + 1))
        self.assertEqual(saves(outs), ["review"])
        self.assertTrue(L.review.items(L)[0].data["sched_box"])
        self.assertTrue(L.status().attributes["settings"]["approve_sched"])
        # a refused approve leaves the box as it was
        L.handle(NightTimeEdit("start", 100, T8 + 2))
        L.handle(NightTimeEdit("end", 130, T8 + 3))
        ans(L, NW_ITEM, "approve")
        self.assertTrue(L.p5.setting("approve_sched"))
        ans(L, NW_ITEM, "approve", rev=L.night_m.proposal_rev - 1)
        self.assertTrue(L.p5.setting("approve_sched"))
        L.handle(NightTimeEdit("start", 1380, T8 + 4))
        L.handle(NightTimeEdit("end", 375, T8 + 5))
        outs = ans(L, NW_ITEM, "approve")
        self.assertTrue(cmds(outs)[0].sched)
        self.assertFalse(L.p5.setting("approve_sched"))                # ticked once
        st = [o for o in outs if isinstance(o, StatusChanged)][-1]
        self.assertFalse(st.attributes["settings"]["approve_sched"])  # the forced status republishes the switch as OFF
        self.assertFalse(L.review.items(L)[0].data["sched_box"])

    def test_the_answer_key_sched_works_without_the_box(self):
        L = self.learner()
        outs = ans(L, NW_ITEM, "approve", sched=True)
        self.assertTrue(cmds(outs)[0].sched)
        self.assertFalse(L.p5.setting("approve_sched"))
        L = self.learner()
        self.assertIsNone(cmds(ans(L, NW_ITEM, "approve"))[0].sched)  # neither: the schedule is left alone (D18)


class BedItemTest(unittest.TestCase):
    def test_actions_that_do_not_apply(self):
        L = ready_learner()
        self.assertEqual(ignored(ans(L, BED_ITEM, "approve")), [(BED_ITEM, "approve", "not_applicable")])
        self.assertEqual(ignored(ans(L, BED_ITEM, "rename", name="x")), [(BED_ITEM, "rename", "not_applicable")])
        ans(L, BED_ITEM, "later")
        self.assertEqual(ignored(ans(L, BED_ITEM, "later")), [(BED_ITEM, "later", "not_pending")])     # snoozed
        ans(L, BED_ITEM, "reject")                                    # [No bed in view] also from the snoozed state
        self.assertEqual(L.review.bed_state(L)[0], "suppressed")
        self.assertEqual(ignored(ans(L, BED_ITEM, "reject")), [(BED_ITEM, "reject", "not_pending")])

    def test_stale_for_later_and_current(self):
        L = ready_learner()
        rev = L.review.bed_rev
        self.assertEqual(ignored(ans(L, BED_ITEM, "later", rev=rev - 1)), [(BED_ITEM, "later", "stale")])
        self.assertEqual(ignored(ans(L, BED_ITEM, "later", rev=rev)), [])
        self.assertEqual(L.review.bed_rev, rev + 1)


class FuzzTest(unittest.TestCase):
    """Random answers, selections, switches, acknowledgements and ticks never raise, and the learner keeps its
    invariants and round-trips through its state (a fixed seed: a regression net for the P5 paths)."""

    def invariants(self, L):
        zids = [z["zid"] for z in L.review.zones]
        self.assertEqual(len(zids), len(set(zids)))
        rn = [z["name"].strip().casefold() for z in L.review.zones]
        self.assertEqual(len(rn), len(set(rn)))
        for z in L.review.zones:
            self.assertTrue(z["rev"] >= 0 and len(z["poly_cm"]) <= 12 and len(z["also"]) <= 8)
        refs = {sp["ref"] for sp in L.review.suppressions if sp["type"] == "place"}
        self.assertLessEqual(set(L.p5.masks), refs)
        self.assertEqual(len(set(L.p5.tags.values())), len(L.p5.tags))
        self.assertGreaterEqual(L.p5.tag_n, len(L.p5.tags))
        items = L.review.items(L)
        self.assertEqual(len({i.id for i in items}), len(items))
        self.assertLessEqual(len([i for i in items if i.tier == 2]), 12)
        txt = json.dumps(L.export_state(), separators=(",", ":"))
        L2 = Learner(L.p, L.tm, json.loads(txt), L.p5params)
        self.assertEqual(json.dumps(L2.export_state(), separators=(",", ":")), txt)
        adv = L.advanced()
        self.assertEqual(len({e.id for e in adv}), len(adv))
        pr = L.push_request()
        self.assertLessEqual(pr.acked, pr.seq)

    def event(self, rng, L, t):
        from radar_zone_learner.core.types import MarkPress, PushAck, ReviewSelect
        k = rng.random()
        if k < 0.40:
            op = rng.choices(["approve", "reject", "later", "rename", "undo", "forget", "advanced", "bogus", None, 5],
                             [10, 4, 4, 5, 1, 0.3, 5, 0.3, 0.2, 0.2])[0]
            pool = [i.id for i in L.review.items(L)] + [s["id"] for s in L.ns.suggestions] +                 [z["zid"] for z in L.review.zones] + [lb["id"] for lb in L.label_list] +                 ["mrg-s0005-1", "mod-z0001", "garbage", "s9999-9", NW_ITEM, BED_ITEM, None] +                 [e.id for e in L.advanced()] + ["adv:undo:bed", "adv:undo:nw", "adv:remove:z0001"]
            item = rng.choice(pool)
            cur = L.review.rev_of(L, item) if isinstance(item, str) else None
            return ReviewAnswer(item, op, rng.choice([None, 0, 1, 2, cur, cur]), rng.random() < 0.3, t,
                                name=rng.choice([None, None, "Desk", "bed", "Bett", "x" * 40, "  ", "Desk 2", ""]),
                                aid=rng.choice([None, None, "a1"]))
        if k < 0.50:
            return ReviewSelect(rng.choice([i.id for i in L.review.items(L)] + ["nonsense", None]), t)
        if k < 0.56:
            return NightTimeEdit(rng.choice(["start", "end", "x"]), rng.choice([0, 100, 1380, 1439, 1440, -1, 375]), t)
        if k < 0.64:
            return SettingSwitch(rng.choice(["approve_sched", "guests_pause", "pets", "detail_text", "zzz"]),
                                 rng.random() < 0.5, t)
        if k < 0.72:
            return PushAck(rng.choice([0, 1, 2, L.p5.push["seq"]]), t, rng.choice(["", "pa1b2c3d", "pe5f6a7b", "bad!"]))
        if k < 0.76:
            return SpotName(rng.choice(["Desk", "Bed", "", "Sofa", "bett"]), t)
        if k < 0.80:
            return ScheduleState(rng.choice([True, False, None]), rng.choice([0, 1380, None]),
                                 rng.choice([360, 375, None]), t)
        if k < 0.84:
            return MarkPress(t)
        return None

    def run_seed(self, seed, steps):
        import random
        rng = random.Random(seed)
        L = places(6)
        with_proposal(L)
        t = T8
        for step in range(steps):
            t += rng.choice([50, 500, 1000, 60000, 3600000, 20 * 3600000])
            ev = self.event(rng, L, t)
            if ev is None:
                L.tick(t)
            else:
                L.handle(ev)
            if step % 20 == 0:
                self.invariants(L)
            if rng.random() < 0.03:
                add_place(L, rng.choice(POS), sid="s%04d-%d" % (5 + step // 40, 1 + step % 40), conf=rng.random(),
                          evidence=evidence())
                L.review.touch()
        self.invariants(L)
        return L

    def test_random_events(self):
        for seed in (1, 2):
            L = self.run_seed(seed, 500)
            self.assertGreater(L.p5.tag_n, 0)


if __name__ == "__main__":
    unittest.main()
