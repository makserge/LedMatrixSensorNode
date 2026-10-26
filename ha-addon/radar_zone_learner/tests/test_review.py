"""P4: review items, the bed item and its answers, 'Mark spot here' binding, zones by approval, the
night_window item (OA 7.6, 7.8, 7.9)."""
import json
import random
import unittest
from datetime import date

from radar_zone_learner.core import Learner
from radar_zone_learner.core.extraction import _new_suggestion
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import (CommandResult, FixedOffsetTime, MarkPress, MarkResult, NodeCommand,
                                           ReviewAnswer, ReviewChanged, ReviewSelect, SaveRequested, ScheduleState,
                                           SpotName, StatusChanged)

from .test_extraction import BASE_MS, DAY, make, night
from .test_labels import BED, CHAIR, press, setup
from .test_learner import sit_fn

D0 = date(2026, 1, 5).toordinal()
H = 3600000
BED_ITEM = "bed_needs_data"
NW_ITEM = "night_window"


def ready_learner():
    """A learner whose setup review exists (7 nights have run)."""
    L = make()
    night(L, 7)
    assert L.night == 7
    return L


def items(L):
    return [i.id for i in L.review.items(L)]


def answer(L, item, op, rev=None, sched=False, t=None):
    return L.handle(ReviewAnswer(item, op, rev, sched, (L.now or BASE_MS) + 1 if t is None else t))


def with_proposal(L, window=(0, 360)):
    """Evidence for the proposal 23:00-06:15 (the default window is 00:00-06:00)."""
    L.handle(ScheduleState(True, window[0], window[1], (L.now or BASE_MS) + 1))
    nm = L.night_m
    nm.onsets["radar"] = {D0 + i: 650 for i in range(8)}
    nm.wakes = {D0 + i: 1100 for i in range(8)}
    nm.refresh(L)
    return nm.raw


def add_place(L, pos=BED, typ="dwell_zone", sid="s0005-1", **kw):
    s = _new_suggestion(sid, typ, 5, L.geom.cells_within(pos[0], pos[1], 0.3), pos[0], pos[1])
    s.update(kw)
    L.ns.suggestions.append(s)
    return s


class ItemListTest(unittest.TestCase):
    def test_no_items_before_the_setup_review(self):
        L = make()
        self.assertEqual(items(L), [])
        self.assertFalse(L.review.ready(L))
        self.assertIsNone(L.review.selected(L))
        L.night = 6
        self.assertEqual(items(L), [])
        L.night = 7
        self.assertEqual(items(L), [BED_ITEM])
        self.assertEqual(L.review.selected(L), BED_ITEM)

    def test_order_is_night_window_then_bed(self):
        L = ready_learner()
        with_proposal(L)
        self.assertEqual(items(L), [NW_ITEM, BED_ITEM])
        self.assertEqual(L.review.selected(L), NW_ITEM)                    # the first item unless the user chose
        L.handle(ReviewSelect(BED_ITEM, BASE_MS + 8 * DAY * 1000))
        self.assertEqual(L.review.selected(L), BED_ITEM)
        L.handle(ReviewSelect("nonsense", BASE_MS + 8 * DAY * 1000 + 1))   # an unknown item changes nothing
        self.assertEqual(L.review.selected(L), BED_ITEM)
        L.handle(ReviewSelect(None, BASE_MS + 8 * DAY * 1000 + 2))         # 'None' chosen: nothing is selected
        self.assertIsNone(L.review.selected(L))
        L.handle(ReviewSelect(NW_ITEM, BASE_MS + 8 * DAY * 1000 + 3))
        self.assertEqual(L.review.selected(L), NW_ITEM)

    def test_bed_item_text_follows_the_schedule(self):
        L = ready_learner()
        d = L.review.bed_data(L)
        self.assertFalse(d["show_window"])                                # no schedule known (pre-P2)
        self.assertNotIn("before", d["text"])
        L.handle(ScheduleState(False, 0, 360, BASE_MS + 8 * DAY * 1000))
        self.assertNotIn("before", L.review.bed_data(L)["text"])          # schedule OFF: the radars observe all day
        L.handle(ScheduleState(True, 0, 360, BASE_MS + 8 * DAY * 1000 + 1))
        d = L.review.bed_data(L)
        self.assertTrue(d["show_window"])
        self.assertIn("press Mark spot here once, before 00:00 or after 06:00 (recommended; no extra radar-on "
                      "nights)", d["text"])
        self.assertIn("2 weeks or more", d["text"])
        L.handle(ScheduleState(True, 23 * 60 + 30, 6 * 60 + 45, BASE_MS + 8 * DAY * 1000 + 2))
        self.assertIn("before 23:30 or after 06:45", L.review.bed_data(L)["text"])

    def test_review_changed_is_emitted_on_change_only(self):
        # the 7th nightly run makes the list: it is part of that call's outputs, and only of that one
        L2 = make()
        got = []
        for k in range(8):
            got += [x for x in L2.tick(BASE_MS + k * DAY * 1000 + 210 * 60000) if isinstance(x, ReviewChanged)]
        self.assertEqual(len(got), 1)
        self.assertEqual(([i.id for i in got[0].items], got[0].selected), ([BED_ITEM], BED_ITEM))
        self.assertEqual(got[0].items[0].rev, 0)
        self.assertEqual(got[0].items[0].type, BED_ITEM)
        self.assertIn("text", got[0].items[0].data)
        for t in range(3):
            self.assertEqual([x for x in L2.tick(BASE_MS + 8 * DAY * 1000 + t * 60000) if isinstance(x, ReviewChanged)],
                             [])

    def test_review_changed_is_sent_again_when_the_text_changes(self):
        L = ready_learner()
        outs = L.handle(ScheduleState(False, 0, 360, BASE_MS + 8 * DAY * 1000))          # the node's first echo
        self.assertEqual(len([o for o in outs if isinstance(o, ReviewChanged)]), 1)
        outs = L.handle(ScheduleState(True, 0, 360, BASE_MS + 8 * DAY * 1000 + 1))        # 'before ... or after ...'
        rc = [o for o in outs if isinstance(o, ReviewChanged)]
        self.assertEqual(len(rc), 1)
        self.assertIn("before 00:00 or after 06:00", rc[0].items[0].data["text"])
        self.assertEqual(rc[0].items[0].rev, 0)                                           # the revision is the same
        outs = L.handle(ScheduleState(True, 0, 360, BASE_MS + 8 * DAY * 1000 + 2))
        self.assertEqual([o for o in outs if isinstance(o, ReviewChanged)], [])

    def test_output_order(self):
        L = ready_learner()
        with_proposal(L)
        outs = answer(L, NW_ITEM, "approve", sched=True)
        kinds = [type(o) for o in outs]
        self.assertEqual(kinds, [NodeCommand, ReviewChanged, StatusChanged, SaveRequested][:len(kinds)])
        self.assertIn(NodeCommand, kinds)


class BedAnswersTest(unittest.TestCase):
    def test_later_snoozes_until_the_next_nightly_run(self):
        L = ready_learner()
        answer(L, BED_ITEM, "later")
        self.assertEqual(L.review.bed_state(L), ("snoozed", "later"))
        self.assertEqual(items(L), [])
        rev = L.review.bed_rev
        self.assertEqual(rev, 1)
        night(L, 8)
        self.assertEqual(L.review.bed_state(L), ("open", None))
        self.assertEqual(items(L), [BED_ITEM])
        self.assertEqual(L.review.bed_rev, rev + 1)                       # a new revision when it comes back

    def test_no_bed_in_view_is_a_suppression_and_undo_brings_it_back(self):
        L = ready_learner()
        outs = answer(L, BED_ITEM, "reject")
        self.assertIn(SaveRequested("review"), outs)
        self.assertEqual(L.review.bed_state(L), ("suppressed", "no_bed_in_view"))
        self.assertEqual(items(L), [])
        st = L.export_state()
        self.assertEqual(st["suppressions"], [{"type": "bed_needs_data", "night": 7}])
        night(L, 8)
        self.assertEqual(items(L), [])                                    # not snoozed: it stays away
        self.assertEqual(L.review.bed_state(L)[0], "suppressed")
        self.assertTrue(answer(L, BED_ITEM, "undo") is not None)          # under 'Advanced'
        self.assertEqual(L.review.bed_state(L), ("open", None))
        self.assertEqual(L.export_state()["suppressions"], [])

    def test_forget_clears_every_suppression_and_snooze(self):
        L = ready_learner()
        with_proposal(L)
        answer(L, BED_ITEM, "reject")
        answer(L, NW_ITEM, "reject")
        self.assertEqual(items(L), [])
        outs = answer(L, None, "forget")
        self.assertIn(SaveRequested("review"), outs)
        self.assertEqual(items(L), [NW_ITEM, BED_ITEM])
        self.assertEqual(L.export_state()["suppressions"], [])
        outs = answer(L, None, "forget")                                   # nothing left: nothing to save
        self.assertNotIn(SaveRequested("review"), outs)

    def test_a_stale_revision_is_ignored(self):
        L = ready_learner()
        answer(L, BED_ITEM, "later")
        night(L, 8)                                                        # back with revision 2
        outs = answer(L, BED_ITEM, "reject", rev=1)
        self.assertEqual(L.review.stale_answers, 1)
        self.assertNotIn(SaveRequested("review"), outs)
        self.assertEqual(L.review.bed_state(L)[0], "open")
        answer(L, BED_ITEM, "reject", rev=2)
        self.assertEqual(L.review.bed_state(L)[0], "suppressed")

    def test_answers_without_an_item_use_the_selected_one(self):
        L = ready_learner()
        answer(L, None, "later")
        self.assertEqual(L.review.bed_state(L)[0], "snoozed")

    def test_a_zone_of_kind_bed_withdraws_the_item(self):
        L = ready_learner()
        L.label_list.append({"id": "l0001", "created_s": 0, "x": 1.0, "y": 2.0, "name": "Bed", "kind": "bed",
                             "cells": frozenset()})
        self.assertEqual(L.review.bed_state(L), ("withdrawn", "bed_zone"))
        self.assertEqual(items(L), [])
        L.label_list[0]["kind"] = "manual"
        self.assertEqual(L.review.bed_state(L)[0], "open")

    def test_a_suggested_place_named_bed_withdraws_it_until_rejected_or_stale(self):
        L = ready_learner()
        plain = add_place(L, CHAIR, sid="s0001-1")
        self.assertEqual(L.review.bed_state(L)[0], "open")                 # a place without the name does not
        bed = add_place(L, BED, sid="s0002-1", kind="bed", name_hint="bed")
        self.assertEqual(L.review.bed_state(L), ("withdrawn", "bed_suggested"))
        answer(L, "s0002-1", "reject")                                     # rejected: the item is shown again
        self.assertEqual(L.review.bed_state(L)[0], "open")
        self.assertEqual(L.export_state()["suppressions"], [{"type": "place", "night": 7, "ref": "s0002-1"}])
        L2 = ready_learner()
        b2 = add_place(L2, BED, sid="s0002-1", kind="bed", name_hint="bed")
        self.assertEqual(L2.review.bed_state(L2)[0], "withdrawn")
        b2["state"] = "stale"                                              # withdrawn without approval
        self.assertEqual(L2.review.bed_state(L2)[0], "open")
        self.assertEqual((plain["kind"], bed["kind"]), ("dwell", "bed"))

    def test_a_no_bed_answer_wins_over_a_named_suggestion(self):
        L = ready_learner()
        answer(L, BED_ITEM, "reject")
        add_place(L, BED, kind="bed", name_hint="bed")
        self.assertEqual(L.review.bed_state(L), ("suppressed", "no_bed_in_view"))


class MarkBindingTest(unittest.TestCase):
    def sit(self, L, f, pos=BED, until=40000, seed=1):
        rng = random.Random(seed)
        f.targets_until(until, sit_fn(rng, pos))

    def test_bed_item_press_makes_a_bed_zone_named_bed(self):
        L, f = setup()
        L.night = 7
        self.sit(L, f)
        outs = press(L, f)
        self.assertEqual(outs[0], MarkResult(True, None, "l0001", "bed"))
        lb = L.labels()[0]
        self.assertEqual((lb.name, lb.kind), ("Bed", "bed"))
        self.assertAlmostEqual(lb.x_m, BED[0], delta=0.05)
        rc = [o for o in outs if isinstance(o, ReviewChanged)]
        self.assertEqual(len(rc), 1)
        self.assertEqual((rc[0].items, rc[0].selected), ((), None))        # the bed item is withdrawn at once
        a = L.status().attributes
        self.assertEqual((a["review"]["bed_item"]["state"], a["review"]["bed_item"]["reason"]),
                         ("withdrawn", "bed_zone"))
        self.assertEqual(a["review"]["bed"], {"zones": 1, "frozen": False})   # the schedule is not known to be ON
        self.assertTrue(any(isinstance(o, SaveRequested) and o.reason == "label" for o in outs))

    def test_the_name_text_is_kept_but_the_kind_is_bed(self):
        L, f = setup()
        L.night = 7
        L.handle(SpotName("Master", BASE_MS + 10000))
        self.sit(L, f)
        outs = press(L, f)
        self.assertEqual(outs[0], MarkResult(True, None, "l0001", "bed"))
        self.assertEqual(L.labels()[0].name, "Master")

    def test_another_selected_item_gives_kind_manual(self):
        L, f = setup()
        L.night = 7
        with_proposal(L)
        self.assertEqual(L.review.selected(L), NW_ITEM)                    # the night window is shown first
        L.handle(SpotName("Desk", BASE_MS + 10000))
        self.sit(L, f)
        outs = press(L, f)
        self.assertEqual(outs[0], MarkResult(True, None, "l0001", "manual"))
        self.assertEqual(L.labels()[0].name, "Desk")
        self.assertEqual(L.review.bed_state(L)[0], "open")                 # the item stays open
        self.assertEqual(items(L), [NW_ITEM, BED_ITEM])

    def test_another_selected_item_with_a_bed_name_still_gives_kind_bed(self):
        """A deliberate reading of OA 7.6, recorded in core/review.py: the P3 name rule holds for a press that is
        not bound to the bed item, so a user who typed 'Bett' gets a bed even while the night window is shown."""
        for name in ("Bett", "BED", " Bed "):
            L, f = setup()
            L.night = 7
            with_proposal(L)
            self.assertEqual(L.review.selected(L), NW_ITEM)
            L.handle(SpotName(name, BASE_MS + 10000))
            self.sit(L, f)
            outs = press(L, f)
            self.assertEqual(outs[0], MarkResult(True, None, "l0001", "bed"), name)
            self.assertEqual(L.review.bed_state(L), ("withdrawn", "bed_zone"), name)
            self.assertEqual(items(L), [NW_ITEM], name)
        L, f = setup()                                                     # 'bedroom' is not the name of a bed
        L.night = 7
        with_proposal(L)
        L.handle(SpotName("Bedroom", BASE_MS + 10000))
        self.sit(L, f)
        self.assertEqual(press(L, f)[0], MarkResult(True, None, "l0001", "manual"))

    def test_after_selecting_the_bed_item_the_same_press_makes_the_bed(self):
        L, f = setup()
        L.night = 7
        with_proposal(L)
        L.handle(ReviewSelect(BED_ITEM, BASE_MS + 9000))
        self.sit(L, f)
        self.assertEqual(press(L, f)[0], MarkResult(True, None, "l0001", "bed"))

    def test_none_selected_gives_the_p3_rule_too(self):
        L, f = setup()
        L.night = 7
        L.handle(ReviewSelect(None, BASE_MS + 9000))
        L.handle(SpotName("Desk", BASE_MS + 9500))
        self.sit(L, f)
        self.assertEqual(press(L, f)[0], MarkResult(True, None, "l0001", "manual"))
        self.assertEqual(items(L), [BED_ITEM])

    def test_a_chosen_item_that_is_gone_falls_back_to_the_first(self):
        L = ready_learner()
        L.handle(ReviewSelect(BED_ITEM, BASE_MS + 9 * DAY * 1000))
        answer(L, BED_ITEM, "reject")
        self.assertIsNone(L.review.selected(L))                             # no items left
        answer(L, BED_ITEM, "undo")
        self.assertEqual(L.review.selected(L), BED_ITEM)

    def test_before_the_setup_review_the_p3_rule_applies(self):
        L, f = setup()
        self.sit(L, f)
        self.assertEqual(press(L, f)[0], MarkResult(True, None, "l0001", "manual"))
        L, f = setup()
        L.handle(SpotName("Bett", BASE_MS + 10000))
        self.sit(L, f)
        self.assertEqual(press(L, f)[0], MarkResult(True, None, "l0001", "bed"))

    def test_failures_leave_the_item_open(self):
        L = ready_learner()
        outs = L.handle(MarkPress(BASE_MS + 8 * DAY * 1000))
        self.assertEqual(outs[0], MarkResult(False, "not_observing", None, None))
        self.assertEqual(items(L), [BED_ITEM])
        L, f = setup()
        L.night = 7
        self.sit(L, f, until=12000)                                        # 2 s: no stay of 20 s
        self.assertEqual(press(L, f)[0], MarkResult(False, "no_stay", None, None))
        self.assertEqual(items(L), [BED_ITEM])
        L, f = setup()
        L.night = 7
        rng = random.Random(2)
        f.targets_until(35000, lambda t: sit_fn(rng, BED)(t) + sit_fn(rng, CHAIR)(t))
        self.assertEqual(press(L, f)[0], MarkResult(False, "several_stays", None, None))
        self.assertEqual(items(L), [BED_ITEM])
        self.assertEqual(L.labels(), ())

    def test_the_freeze_applies_at_once(self):
        L, f = setup()
        L.night = 7
        L.handle(ScheduleState(True, 0, 360, BASE_MS + 5000))
        s = add_place(L, BED, evidence={"hours_at_suggestion": [0.0] * 24, "hours_wd": [0.0] * 24,
                                        "hours_we": [0.0] * 24})
        self.assertFalse(L.suggestions()[0].frozen)
        self.sit(L, f)
        press(L, f)
        self.assertEqual((s["kind"], s["name_hint"], s["frozen"]), ("bed", "Bed", True))
        self.assertEqual(L.status().attributes["review"]["bed"], {"zones": 1, "frozen": True})
        self.assertEqual(L.review.bed_cells(L) & L.geom.cells_within(BED[0], BED[1], 0.3),
                         L.geom.cells_within(BED[0], BED[1], 0.3))


class ZoneByApprovalTest(unittest.TestCase):
    def approve(self, name, typ="dwell_zone", **kw):
        L = ready_learner()
        L.handle(ScheduleState(True, 0, 360, BASE_MS + 8 * DAY * 1000))
        s = add_place(L, BED, typ=typ, evidence={"hours_at_suggestion": [0.0] * 24, "hours_wd": [0.0] * 24,
                                                 "hours_we": [0.0] * 24}, **kw)
        if name is not None:
            L.handle(SpotName(name, BASE_MS + 8 * DAY * 1000 + 1))
        outs = answer(L, s["id"], "approve")
        return L, s, outs

    def test_bed_names_give_kind_bed(self):
        for name in ("bett", "BED", " Bed ", "Bett", "bed"):
            L, s, outs = self.approve(name)
            z = L.review.zones[0]
            self.assertEqual(z["kind"], "bed", name)
            self.assertEqual((z["src"], z["ref"], z["zid"]), ("approve", "s0005-1", "z0001"))
            self.assertEqual((s["kind"], s["frozen"]), ("bed", True), name)          # frozen at once (schedule ON)
            self.assertEqual(L.review.bed_state(L), ("withdrawn", "bed_zone"), name)
            self.assertIn(SaveRequested("review"), outs)
            self.assertIn("review", L.export_state())

    def test_other_names_do_not(self):
        for name in ("Desk", "bedroom", "Bed room", "", "Bettwäsche"):
            L, s, _ = self.approve(name)
            z = L.review.zones[0]
            self.assertEqual(z["kind"], "dwell", name)
            self.assertEqual(L.review.bed_state(L)[0], "open", name)
        L, s, _ = self.approve("Counter", typ="short_task_zone")
        self.assertEqual(L.review.zones[0]["kind"], "short_task")
        self.assertEqual(L.review.zones[0]["name"], "Counter")

    def test_unchanged_approval_keeps_the_learners_own_bed(self):
        L, s, _ = self.approve(None, kind="bed", name_hint="bed")
        self.assertEqual((L.review.zones[0]["kind"], L.review.zones[0]["name"]), ("bed", "bed"))
        L, s, _ = self.approve(None)
        self.assertEqual((L.review.zones[0]["kind"], L.review.zones[0]["name"]), ("dwell", "Place 1"))

    def test_approving_twice_or_a_rejected_place_does_nothing(self):
        L, s, _ = self.approve("Desk")
        outs = answer(L, s["id"], "approve")
        self.assertNotIn(SaveRequested("review"), outs)
        self.assertEqual(len(L.review.zones), 1)
        L = ready_learner()
        s = add_place(L, BED)
        answer(L, s["id"], "reject")
        self.assertNotIn(SaveRequested("review"), answer(L, s["id"], "approve"))
        self.assertEqual(L.review.zones, [])

    def test_rename_a_zone_to_bed_and_back(self):
        L, s, _ = self.approve("Desk")
        zid = L.review.zones[0]["zid"]
        L.handle(SpotName("Bett", BASE_MS + 8 * DAY * 1000 + 5))
        outs = answer(L, zid, "rename")
        self.assertIn(SaveRequested("review"), outs)
        self.assertEqual((L.review.zones[0]["name"], L.review.zones[0]["kind"]), ("Bett", "bed"))
        self.assertEqual(L.review.bed_state(L)[0], "withdrawn")
        self.assertEqual(s["kind"], "bed")
        L.handle(SpotName("Sofa", BASE_MS + 8 * DAY * 1000 + 6))
        answer(L, zid, "rename")
        self.assertEqual((L.review.zones[0]["name"], L.review.zones[0]["kind"]), ("Sofa", "bed"))   # not downgraded
        # renaming a place suggestion the zone was made from works the same
        outs = answer(L, s["id"], "rename")
        self.assertNotIn(SaveRequested("review"), outs)

    def test_zones_survive_a_restart(self):
        L, s, _ = self.approve("Bed")
        st = json.loads(json.dumps(L.export_state()))
        L2 = Learner(P, FixedOffsetTime(0), st)
        self.assertEqual(L2.review.zones, L.review.zones)
        self.assertEqual(L2.review.next_zone_n, 2)
        self.assertEqual(L2.review.bed_state(L2), ("withdrawn", "bed_zone"))


class NightWindowItemTest(unittest.TestCase):
    def test_the_proposal_is_an_item_with_data(self):
        L = ready_learner()
        L.night_m.act[D0 + 3] = 1 << ((23 * 60 + 40 - 720) // 5)           # activity at 23:40 on one night
        with_proposal(L)
        it = L.review.items(L)[0]
        self.assertEqual((it.id, it.type, it.rev), (NW_ITEM, NW_ITEM, L.night_m.proposal_rev))
        d = it.data
        self.assertEqual((d["start_min"], d["end_min"]), (23 * 60, 6 * 60 + 15))
        self.assertEqual((d["saving_pct"], d["current_saving_pct"]), (30.2, 25.0))
        self.assertEqual((d["from_lights_off"], d["basis"]), (False, "radar"))
        self.assertEqual((d["late_nights"], d["late_of"]), (1, 8))     # 8 nights of this learner, one late
        self.assertIn("Switch both radars off between 23:00 and 06:15. Radar on-time -30%.", d["text"])
        self.assertNotIn("lights-off", d["text"])
        L.night_m.onsets["radar"] = {}
        L.night_m.onsets["lux"] = {D0 + i: 650 for i in range(8)}
        L.night_m.refresh(L)
        d = L.review.items(L)[0].data
        self.assertTrue(d["from_lights_off"])
        self.assertTrue(d["text"].endswith("(from lights-off times)"))

    def test_the_text_names_the_late_nights(self):
        L = ready_learner()
        for i in (2, 3, 4):
            L.night_m.act[D0 + i] = 1 << ((23 * 60 + 40 - 720) // 5)
        with_proposal(L)
        d = L.review.items(L)[0].data
        self.assertEqual(d["late_nights"], 3)
        self.assertIn("Activity was still seen in the 30 min before your current start on 3 of the last", d["text"])

    def test_same_as_the_current_window_is_applied_and_not_an_item(self):
        L = ready_learner()
        with_proposal(L, window=(23 * 60, 6 * 60 + 15))
        self.assertEqual(L.review.nw_state(L), "same")
        self.assertEqual(items(L), [BED_ITEM])
        L.handle(ScheduleState(True, 23 * 60 + 10, 6 * 60 + 10, BASE_MS + 9 * DAY * 1000))    # within 15 min
        self.assertEqual(L.review.nw_state(L), "same")
        L.handle(ScheduleState(True, 23 * 60 - 20, 6 * 60 + 15, BASE_MS + 9 * DAY * 1000 + 1))
        self.assertEqual(L.review.nw_state(L), "proposed")

    def test_approve_sends_the_window_and_the_switch_if_ticked(self):
        L = ready_learner()
        with_proposal(L)
        rev = L.night_m.proposal_rev
        outs = answer(L, NW_ITEM, "approve", rev=rev, sched=True)
        cmd = [o for o in outs if isinstance(o, NodeCommand)]
        self.assertEqual(len(cmd), 1)
        c = cmd[0]
        self.assertEqual((c.kind, c.sched, c.start_min, c.end_min, c.reason), ("window", True, 1380, 375,
                                                                              "approved_window"))
        outs = L.handle(CommandResult(c.token, "window", True, None, BASE_MS + 9 * DAY * 1000))
        self.assertEqual(L.review.last_event, "window_applied")
        L.handle(ScheduleState(True, 1380, 375, BASE_MS + 9 * DAY * 1000 + 1))     # the node's echo
        self.assertEqual(L.review.nw_state(L), "same")
        self.assertEqual(items(L), [BED_ITEM])
        # not ticked: the schedule switch is left alone
        L = ready_learner()
        with_proposal(L)
        c = [o for o in answer(L, NW_ITEM, "approve", sched=False) if isinstance(o, NodeCommand)][0]
        self.assertIsNone(c.sched)

    def test_a_window_that_the_node_did_not_confirm_stays_an_item(self):
        L = ready_learner()
        with_proposal(L)
        c = [o for o in answer(L, NW_ITEM, "approve") if isinstance(o, NodeCommand)][0]
        L.handle(CommandResult(c.token, "window", False, "no_echo", BASE_MS + 9 * DAY * 1000))
        self.assertEqual(L.review.last_event, "window_not_confirmed")
        self.assertEqual(items(L)[0], NW_ITEM)
        L.handle(CommandResult("other", "window", True, None, BASE_MS + 9 * DAY * 1000 + 1))   # not ours: ignored
        self.assertEqual(L.review.last_event, "window_not_confirmed")

    def test_a_stale_answer_sends_nothing(self):
        L = ready_learner()
        with_proposal(L)
        outs = answer(L, NW_ITEM, "approve", rev=L.night_m.proposal_rev - 1)
        self.assertEqual([o for o in outs if isinstance(o, NodeCommand)], [])
        self.assertEqual(L.review.stale_answers, 1)

    def test_deny_suppresses_for_30_days(self):
        L = ready_learner()
        with_proposal(L)
        answer(L, NW_ITEM, "reject")
        self.assertEqual(L.review.nw_state(L), "suppressed")
        self.assertEqual(items(L), [BED_ITEM])
        today = L.review._today(L)
        sup = L.export_state()["suppressions"]
        self.assertEqual(sup, [{"type": "night_window", "night": 7, "until": date.fromordinal(today + 30).isoformat()}])
        L.now = BASE_MS + (7 + 29) * DAY * 1000
        self.assertEqual(L.review.nw_state(L), "suppressed")
        L.now = BASE_MS + (7 + 30) * DAY * 1000
        self.assertEqual(L.review.nw_state(L), "proposed")
        L.now = BASE_MS + 8 * DAY * 1000
        answer(L, NW_ITEM, "undo")                                          # 'Re-suggest night window'
        self.assertEqual(L.review.suppressions, [])

    def test_later_snoozes_until_the_next_nightly_run(self):
        L = ready_learner()
        with_proposal(L)
        answer(L, NW_ITEM, "later")
        self.assertEqual(L.review.nw_state(L), "snoozed")
        night(L, 8)
        self.assertEqual(L.review.nw_state(L), "proposed")

    def test_expired_suppressions_are_dropped_at_the_nightly_run(self):
        L = ready_learner()
        with_proposal(L)
        answer(L, NW_ITEM, "reject")
        L.review.suppressions[0]["until_ord"] = L.review._today(L)
        L.review.on_nightly(L)
        self.assertEqual(L.review.suppressions, [])

    def test_without_the_p2_node_there_is_no_window_item(self):
        L = ready_learner()
        nm = L.night_m
        nm.onsets["radar"] = {D0 + i: 650 for i in range(8)}
        nm.wakes = {D0 + i: 1100 for i in range(8)}
        nm.refresh(L)
        self.assertIsNone(L.window["sched"])
        self.assertEqual(L.review.nw_state(L), "no_p2")                      # the proposal exists, nothing to change
        self.assertEqual(items(L), [BED_ITEM])
        self.assertEqual(L.status().attributes["night"]["proposal"]["start"], "23:00")
        outs = answer(L, NW_ITEM, "approve")
        self.assertEqual([o for o in outs if isinstance(o, NodeCommand)], [])
        L.handle(ScheduleState(False, 0, 360, BASE_MS + 9 * DAY * 1000))     # the node's first echo: P2 exists
        self.assertEqual(items(L), [NW_ITEM, BED_ITEM])

    def test_malformed_answers_and_selects_are_ignored(self):
        L = ready_learner()
        with_proposal(L)
        for ev in (ReviewAnswer(5, "approve", None, False, BASE_MS + 9 * DAY * 1000),
                   ReviewAnswer(NW_ITEM, None, None, False, BASE_MS + 9 * DAY * 1000),
                   ReviewAnswer("zzz", "approve", None, False, BASE_MS + 9 * DAY * 1000),
                   ReviewAnswer("s9999-9", "approve", None, False, BASE_MS + 9 * DAY * 1000),
                   ReviewAnswer(NW_ITEM, "bogus", None, False, BASE_MS + 9 * DAY * 1000),
                   ReviewSelect(["x"], BASE_MS + 9 * DAY * 1000)):
            outs = L.handle(ev)
            self.assertEqual([o for o in outs if isinstance(o, (NodeCommand, SaveRequested))], [], ev)
        self.assertEqual(items(L), [NW_ITEM, BED_ITEM])

    def test_status_attributes(self):
        L = ready_learner()
        with_proposal(L)
        a = L.status().attributes
        json.dumps(a)
        self.assertEqual(a["review"]["items"], [NW_ITEM, BED_ITEM])
        self.assertEqual(a["review"]["night_window"], {"state": "proposed", "rev": L.night_m.proposal_rev})
        self.assertEqual(a["night"]["proposal"]["start"], "23:00")
        self.assertEqual(a["night"]["proposal"]["end"], "06:15")
        self.assertEqual(a["night"]["onsets"], {"radar": 8, "context": 0, "lux": 0})
        self.assertEqual(a["night"]["wakes"], 8)
        self.assertIs(a["radar_valid"], False)                              # no frames yet


if __name__ == "__main__":
    unittest.main()
