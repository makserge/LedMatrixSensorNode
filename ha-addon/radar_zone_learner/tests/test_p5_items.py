"""P5 review items: tiers, the place predicate, order and cap, tags, revisions, selection and the cost of `items()`
(spec 2, §11.3 `test_p5_items`)."""
import json
import random
import statistics
import time
import unittest

from radar_zone_learner.core import zones as ZN
from radar_zone_learner.core.extraction import _new_suggestion
from radar_zone_learner.core.types import (MarkResult, ReviewAnswer, ReviewChanged, ReviewSelect, ScheduleState,
                                           SpotName)

from .test_extraction import BASE_MS, DAY, make, night
from .test_labels import BED, CHAIR, press
from .test_learner import sit_fn
from .test_review import NW_ITEM, add_place, answer, items, ready_learner, with_proposal

BED_ITEM = "bed_needs_data"
T8 = BASE_MS + 8 * DAY * 1000

# well separated positions (no overlap between the dilated 0.3 m discs, no label at one covers another)
POS = [(-4.0, 1.5), (-3.0, 3.5), (-1.0, 5.5), (0.5, 2.0), (2.0, 4.0), (3.5, 1.5), (4.5, 5.0), (-5.5, 6.5),
       (1.5, 7.0), (5.5, 3.0), (-2.0, 1.0), (6.0, 7.0), (-6.0, 2.5), (0.0, 6.5), (3.0, 6.5), (-5.0, 4.5)]
HOURS = [0.0] * 24
HOURS[9] = HOURS[10] = HOURS[11] = 60.0
HOURS[14] = 30.0


def evidence(n=18):
    return {"n": n, "hours_wd": list(HOURS), "hours_we": [0.0] * 24, "hours_at_suggestion": list(HOURS)}


def places(n=3, confs=None, first=1, night_no=5, **kw):
    """A ready learner with n shadow place suggestions s0005-1 ... at well separated positions."""
    L = ready_learner()
    confs = confs or [0.9 - 0.05 * i for i in range(n)]
    for i in range(n):
        add_place(L, POS[i], sid="s%04d-%d" % (night_no, first + i), conf=confs[i], evidence=evidence(), **kw)
    return L


def ids(L, tier=None):
    return [i.id for i in L.review.items(L) if tier is None or i.tier == tier]


class PlaceItemTest(unittest.TestCase):
    def test_tier_two_follows_tier_one_by_confidence(self):
        L = places(3, confs=[0.5, 0.9, 0.7])
        its = L.review.items(L)
        self.assertEqual([i.id for i in its], [BED_ITEM, "s0005-2", "s0005-3", "s0005-1"])
        self.assertEqual([i.tier for i in its], [1, 2, 2, 2])
        self.assertEqual([i.data["tag"] for i in its[1:]], ["A", "B", "C"])         # allocated in list order
        with_proposal(L)
        self.assertEqual(ids(L), [NW_ITEM, BED_ITEM, "s0005-2", "s0005-3", "s0005-1"])
        self.assertEqual([i.tier for i in L.review.items(L)], [1, 1, 2, 2, 2])

    def test_equal_confidence_orders_by_suggestion_id(self):
        L = places(3, confs=[0.7, 0.7, 0.7])
        self.assertEqual(ids(L, 2), ["s0005-1", "s0005-2", "s0005-3"])

    def test_the_item_fields(self):
        L = places(1, confs=[0.81234])
        it = L.review.items(L)[1]
        s = L.ns.suggestions[0]
        self.assertEqual((it.id, it.rev, it.type, it.tier), ("s0005-1", s["rev"], "dwell_zone", 2))
        d = it.data
        self.assertEqual(set(d), {"tag", "kind", "conf", "x_cm", "y_cm", "name_hint", "hrs_wd", "hrs_we", "n",
                                  "frozen"})
        self.assertEqual((d["tag"], d["kind"], d["conf"], d["name_hint"], d["n"], d["frozen"]),
                         ("A", "dwell", 0.812, "", 18, False))
        self.assertEqual((d["x_cm"], d["y_cm"]), (-400, 150))
        self.assertEqual((d["hrs_wd"], d["hrs_we"]), (("09-12", "14-15"), ()))
        hash(tuple(sorted(d.items())))                                    # flat and hashable (ReviewChanged signature)
        add_place(L, POS[1], typ="short_task_zone", sid="s0005-2", conf=0.4, kind="bed", name_hint="bed",
                  evidence=evidence())
        d2 = [i for i in L.review.items(L) if i.id == "s0005-2"][0]
        self.assertEqual((d2.type, d2.data["kind"], d2.data["name_hint"]), ("short_task_zone", "bed", "bed"))

    def test_nothing_before_the_setup_review(self):
        L = make()
        add_place(L, POS[0], conf=0.9, evidence=evidence())
        L.night = 6
        self.assertEqual(ids(L), [])
        L.night = 7
        self.assertEqual(ids(L), [BED_ITEM, "s0005-1"])

    def test_a_suggestion_with_empty_evidence_is_listed_without_a_key_error(self):
        L = ready_learner()
        add_place(L, POS[0], conf=0.6)                                    # evidence {} as P4's fixtures build them
        it = L.review.items(L)[1]
        self.assertEqual((it.data["n"], it.data["hrs_wd"], it.data["hrs_we"]), (0, (), ()))
        L.ns.suggestions[0]["evidence"] = {"n": 5}                        # hours missing
        L.review.touch()
        self.assertEqual(L.review.items(L)[1].data["hrs_wd"], ())
        L.ns.suggestions[0]["evidence"] = {"hours_wd": [0.0] * 24, "hours_we": None}
        L.review.touch()
        self.assertEqual(L.review.items(L)[1].data["n"], 0)

    def test_only_shadow_places_are_listed(self):
        L = places(3)
        L.ns.suggestions[0]["state"] = "stale"
        L.ns.suggestions[1]["type"] = "reflection"
        L.review.touch()
        self.assertEqual(ids(L, 2), ["s0005-3"])
        L.ns.suggestions[0]["state"] = "shadow"
        L.review.touch()
        self.assertEqual(ids(L, 2), ["s0005-1", "s0005-3"])
        for typ in ("ignore_zone", "entry_zone", "room_outline"):
            L.ns.suggestions[2]["type"] = typ
            L.review.touch()
            self.assertNotIn("s0005-3", ids(L), typ)

    def test_a_zone_that_covers_the_place_takes_it_off_the_list(self):
        L = places(3)
        answer(L, "s0005-1", "approve")
        self.assertEqual(ids(L, 2), ["s0005-2", "s0005-3"])
        z = L.review.zones[0]
        self.assertEqual(z["ref"], "s0005-1")
        z["ref"] = None                                                   # covered by 'also' instead
        z["also"] = ["s0005-2"]
        L.review.touch()
        self.assertEqual(ids(L, 2), ["s0005-1", "s0005-3"])

    def test_a_label_covers_the_place_a_distant_one_does_not(self):
        L = places(3)
        L.label_list.append({"id": "l0001", "created_s": 0, "x": POS[0][0] + 0.25, "y": POS[0][1], "name": "Desk",
                             "kind": "manual", "cells": frozenset()})
        L.label_list.append({"id": "l0002", "created_s": 0, "x": POS[1][0] + 0.9, "y": POS[1][1], "name": "Far",
                             "kind": "manual", "cells": frozenset()})     # beyond the dilated cells and 0.6 m
        self.assertEqual(ids(L, 2), ["s0005-2", "s0005-3"])
        self.assertTrue(ZN.label_covers(L, L.ns.suggestions[0]))
        self.assertFalse(ZN.label_covers(L, L.ns.suggestions[1]))

    def test_rejected_by_id_and_by_mask(self):
        L = places(3)
        answer(L, "s0005-1", "reject")
        self.assertEqual(ids(L, 2), ["s0005-2", "s0005-3"])
        # a different suggestion that overlaps the rejected region is hidden by the mask (overlap coefficient 0.5)
        near = add_place(L, (POS[0][0] + 0.1, POS[0][1]), sid="s0006-1", conf=0.95, evidence=evidence())
        self.assertEqual(ids(L, 2), ["s0005-2", "s0005-3"])
        self.assertNotIn("s0006-1", ids(L))
        near["cells"] = L.geom.cells_within(POS[0][0] + 1.5, POS[0][1], 0.3)  # moved away: listed
        near["cx"], near["cy"] = POS[0][0] + 1.5, POS[0][1]
        L.review.touch()
        self.assertIn("s0006-1", ids(L, 2))

    def test_snoozed_items_return_after_the_next_nightly_run(self):
        L = places(3)
        outs = answer(L, "s0005-1", "later")
        self.assertEqual(ids(L, 2), ["s0005-2", "s0005-3"])
        self.assertEqual(L.export_state()["p5"]["later"], {"s0005-1": "2026-01-12"})
        self.assertEqual(L.suggestions()[0].rev, 1)                       # a snooze does not change the revision
        night(L, 8)
        self.assertEqual(ids(L, 2), ["s0005-1", "s0005-2", "s0005-3"])
        self.assertEqual(L.p5.later, {})                                   # the marker is gone

    def test_the_cap_of_twelve_and_the_count_of_the_rest(self):
        L = ready_learner()
        for i in range(15):
            add_place(L, POS[i], sid="s0005-%d" % (i + 1), conf=0.9 - 0.01 * i, evidence=evidence())
        self.assertEqual(len(ids(L, 2)), 12)
        self.assertEqual(ids(L, 2)[:2], ["s0005-1", "s0005-2"])
        a = L.status().attributes["review"]
        self.assertEqual((a["tier2"], a["more"]), (12, 3))
        self.assertEqual(L.status().attributes["pending_places"], 12)
        answer(L, "s0005-1", "reject")
        self.assertEqual(len(ids(L, 2)), 12)                              # the next one moves up
        self.assertEqual(L.status().attributes["review"]["more"], 2)

    def test_tags_are_allocated_once_and_stay_across_nights(self):
        L = places(3)
        tags = {i.id: i.data["tag"] for i in L.review.items(L) if i.tier == 2}
        self.assertEqual(sorted(tags.values()), ["A", "B", "C"])
        night(L, 8)
        self.assertEqual({i.id: i.data["tag"] for i in L.review.items(L) if i.tier == 2}, tags)
        answer(L, "s0005-1", "reject")
        add_place(L, POS[5], sid="s0008-1", conf=0.99, evidence=evidence())
        new = [i for i in L.review.items(L) if i.id == "s0008-1"][0]
        self.assertEqual(new.data["tag"], "D")                            # a new item gets the next tag
        self.assertEqual(L.p5.tag_n, 4)

    def test_tags_of_gone_suggestions_are_pruned_by_the_nightly_run_the_counter_stays(self):
        L = places(3)
        ids(L)
        L.ns.suggestions[:] = [s for s in L.ns.suggestions if s["id"] != "s0005-1"]
        night(L, 8)
        self.assertEqual(set(L.p5.tags), {"s0005-2", "s0005-3"})
        self.assertEqual(L.p5.tag_n, 3)
        self.assertEqual(L.p5.tags["s0005-2"] + L.p5.tags["s0005-3"], "BC")

    def test_revision_rules(self):
        L = places(2, confs=[0.8, 0.7])
        s = L.ns.suggestions[0]
        before = L.review.items(L)[1].rev
        s["conf"] = 0.85                                                  # a confidence change is no revision change
        L.review.touch()
        it = L.review.items(L)[1]
        self.assertEqual((it.rev, it.data["conf"]), (before, 0.85))
        s["rev"] += 1                                                     # a moved region is
        L.review.touch()
        self.assertEqual(L.review.items(L)[1].rev, before + 1)
        self.assertEqual(L.review.rev_of(L, "s0005-1"), before + 1)

    def test_a_changed_region_makes_the_old_revision_stale(self):
        L = places(1)
        rev = L.review.items(L)[1].rev
        L.ns.suggestions[0]["rev"] += 1
        L.review.touch()
        outs = answer(L, "s0005-1", "approve", rev=rev)
        self.assertEqual(L.review.zones, [])
        self.assertEqual(L.review.stale_answers, 1)
        answer(L, "s0005-1", "approve", rev=rev + 1)
        self.assertEqual(len(L.review.zones), 1)


class StatusAttributesTest(unittest.TestCase):
    def test_the_counts_and_the_switches(self):
        L = places(3)
        a = L.status().attributes
        self.assertEqual((a["pending_places"], a["zones_total"], a["learning_paused"], a["pet_spots"]), (3, 0, False, 0))
        self.assertEqual(a["settings"], {"approve_sched": False, "guests_pause": False, "pets": False,
                                         "detail_text": False})
        self.assertEqual((a["review"]["tier2"], a["review"]["more"], a["review"]["advanced"],
                          a["review"]["command_pending"]), (3, 0, 0, False))
        answer(L, "s0005-1", "approve")
        L.label_list.append({"id": "l0001", "created_s": 0, "x": 0.0, "y": 7.9, "name": "X", "kind": "manual",
                             "cells": frozenset()})
        a = L.status().attributes
        self.assertEqual((a["pending_places"], a["zones_total"], a["review"]["zones"], a["review"]["advanced"]),
                         (2, 2, 1, 4))
        json.dumps(a)                                                       # JSON-friendly, no name, no time

    def test_command_pending_while_an_approved_window_waits_for_the_echo(self):
        L = ready_learner()
        with_proposal(L)
        self.assertFalse(L.status().attributes["review"]["command_pending"])
        c = [o for o in answer(L, NW_ITEM, "approve") if o.__class__.__name__ == "NodeCommand"][0]
        self.assertTrue(L.status().attributes["review"]["command_pending"])
        from radar_zone_learner.core.types import CommandResult
        L.handle(CommandResult(c.token, "window", True, None, T8 + 1000))
        self.assertFalse(L.status().attributes["review"]["command_pending"])

    def test_no_name_no_time_no_coordinate_in_the_new_keys(self):
        L = places(2)
        answer(L, "s0005-1", "approve")
        L.handle(SpotName("Secret name", T8))
        a = L.status().attributes
        text = json.dumps({k: a[k] for k in ("zones_total", "pending_places", "learning_paused", "settings",
                                             "pet_spots")})
        self.assertNotIn("Secret", text)
        self.assertNotIn("Place 1", text)


class SelectionTest(unittest.TestCase):
    def test_select_accepts_a_listed_id_and_ignores_others(self):
        L = places(2)
        L.handle(ReviewSelect("s0005-2", T8))
        self.assertEqual(L.review.selected(L), "s0005-2")
        L.handle(ReviewSelect("s9999-9", T8 + 1))                          # not a listed item: nothing changes
        L.handle(ReviewSelect("mrg-s0005-1", T8 + 2))
        L.handle(ReviewSelect("adv:undo:bed", T8 + 3))
        L.handle(ReviewSelect("nonsense", T8 + 4))
        L.handle(ReviewSelect(["x"], T8 + 5))
        self.assertEqual(L.review.selected(L), "s0005-2")
        L.handle(ReviewSelect(BED_ITEM, T8 + 6))                          # a P4 id is accepted open or not
        self.assertEqual(L.review.selected(L), BED_ITEM)
        L.handle(ReviewSelect(None, T8 + 7))
        self.assertIsNone(L.review.selected(L))

    def test_a_stale_place_is_not_selectable(self):
        L = places(2)
        L.ns.suggestions[1]["state"] = "stale"
        L.review.touch()
        L.handle(ReviewSelect("s0005-2", T8))
        self.assertEqual(L.review.selected(L), BED_ITEM)                  # the first item: the choice was not made

    def test_auto_advance_after_an_answer(self):
        L = places(3)
        self.assertEqual(L.review.selected(L), BED_ITEM)
        L.handle(ReviewSelect("s0005-1", T8))
        outs = answer(L, None, "approve")                                 # the selected one
        self.assertEqual(len(L.review.zones), 1)
        self.assertEqual(L.review.zones[0]["ref"], "s0005-1")
        self.assertEqual(L.review.selected(L), BED_ITEM)                  # the chosen item left: the first one again
        rc = [o for o in outs if isinstance(o, ReviewChanged)]
        self.assertEqual(rc[0].selected, BED_ITEM)
        L.handle(ReviewSelect("s0005-2", T8 + 10))
        answer(L, "s0005-2", "reject")
        self.assertEqual(L.review.selected(L), BED_ITEM)

    def test_the_first_item_is_shown_until_the_user_chooses(self):
        L = places(2)
        self.assertEqual(L.review.selected(L), BED_ITEM)
        answer(L, BED_ITEM, "later")
        self.assertEqual(L.review.selected(L), "s0005-1")                 # no tier-1 item: the first place


class ChangeSignalTest(unittest.TestCase):
    def test_review_changed_on_change_only(self):
        L = places(2)
        got = L.tick(T8)
        self.assertEqual(len([o for o in got if isinstance(o, ReviewChanged)]), 1)
        for k in range(1, 4):
            self.assertEqual([o for o in L.tick(T8 + k * 1000) if isinstance(o, ReviewChanged)], [])
        add_place(L, POS[2], sid="s0006-1", conf=0.99, evidence=evidence())
        rc = [o for o in L.tick(T8 + 5000) if isinstance(o, ReviewChanged)]
        self.assertEqual(len(rc), 1)
        self.assertEqual([i.id for i in rc[0].items], [BED_ITEM, "s0006-1", "s0005-1", "s0005-2"])
        L.ns.suggestions[0]["conf"] = 0.1234                              # the confidence is part of the data
        L.review.touch()
        self.assertEqual(len([o for o in L.tick(T8 + 9000) if isinstance(o, ReviewChanged)]), 1)

    def test_review_state_is_available_before_the_first_step(self):
        L = places(2)
        its, sel = L.review_state()
        self.assertEqual(([i.id for i in its], sel), ([BED_ITEM, "s0005-1", "s0005-2"], BED_ITEM))
        self.assertIsInstance(its, tuple)


class MarkBindingTest(unittest.TestCase):
    def setup_learner(self, name):
        from .test_labels import setup
        L, f = setup()
        L.night = 7
        add_place(L, POS[3], sid="s0005-1", conf=0.9, evidence=evidence())      # an open place, far from the bed
        L.handle(ReviewSelect("s0005-1", BASE_MS + 9000))
        L.handle(SpotName(name, BASE_MS + 9500))
        rng = random.Random(1)
        f.targets_until(40000, sit_fn(rng, BED))
        return L, f

    def test_a_place_item_selected_gives_kind_manual_for_a_name_that_is_no_bed(self):
        L, f = self.setup_learner("Desk")
        outs = press(L, f)
        self.assertEqual(outs[0], MarkResult(True, None, "l0001", "manual"))
        self.assertEqual(L.labels()[0].name, "Desk")
        self.assertEqual(L.review.bed_state(L)[0], "open")                 # the bed item stays open
        self.assertIn(BED_ITEM, ids(L))

    def test_the_bed_names_keep_their_p4_reading_whatever_item_is_selected(self):
        L, f = self.setup_learner("Bett")
        self.assertEqual(press(L, f)[0], MarkResult(True, None, "l0001", "bed"))      # D28
        self.assertEqual(L.review.bed_state(L), ("withdrawn", "bed_zone"))

    def test_the_bed_item_binding_is_unchanged(self):
        L, f = self.setup_learner("")
        L.handle(ReviewSelect(BED_ITEM, BASE_MS + 9800))
        self.assertTrue(L.review.bound_to_bed(L))
        self.assertEqual(press(L, f)[0], MarkResult(True, None, "l0001", "bed"))


class FirstSetupTest(unittest.TestCase):
    """The walk-through of spec 10.2 on the core: the night window, the bed (a press in bed) and the places, in the
    order the list shows them, by the taps a user would make."""

    def test_seven_taps_for_five_places(self):
        from radar_zone_learner.core.types import CommandResult, NodeCommand
        from .test_labels import setup
        L, f = setup()
        L.night = 7
        L.handle(ScheduleState(True, 0, 360, BASE_MS + 5000))             # schedule ON (P2 commissioning)
        with_proposal(L, window=(0, 360))
        for i in range(5):
            add_place(L, POS[3 + i], sid="s0005-%d" % (i + 1), conf=0.9 - 0.05 * i, evidence=evidence())
        rng = random.Random(1)
        f.targets_until(45000, sit_fn(rng, BED))                          # the user lies in bed with the phone
        self.assertEqual(ids(L), [NW_ITEM, BED_ITEM] + ["s0005-%d" % i for i in range(1, 6)])
        taps = 0
        # tap 1: Approve the night window (the first item)
        self.assertEqual(L.review.selected(L), NW_ITEM)
        outs = L.handle(ReviewAnswer(None, "approve", None, False, BASE_MS + f.next_ms + 10))
        taps += 1
        c = [o for o in outs if isinstance(o, NodeCommand)][0]
        L.handle(CommandResult(c.token, "window", True, None, BASE_MS + f.next_ms + 20))
        L.handle(ScheduleState(True, c.start_min, c.end_min, BASE_MS + f.next_ms + 30))
        self.assertEqual(L.review.selected(L), BED_ITEM)                  # the select falls to the next item
        # tap 2: Mark spot here, bound to the bed item
        outs = press(L, f)
        taps += 1
        self.assertEqual(outs[0], MarkResult(True, None, "l0001", "bed"))
        self.assertEqual(L.review.selected(L), "s0005-1")                 # the bed item is withdrawn: the first place
        # taps 3-7: Approve each place with its current revision
        for i in range(1, 6):
            sid = "s0005-%d" % i
            self.assertEqual(L.review.selected(L), sid)
            outs = L.handle(ReviewAnswer(None, "approve", rev_of_item(L, sid), False, BASE_MS + f.next_ms + 100 * i))
            taps += 1
        self.assertEqual(taps, 7)
        self.assertEqual(ids(L), [])
        self.assertEqual([z["name"] for z in L.review.zones], ["Place %d" % i for i in range(1, 6)])
        self.assertEqual(len(L.zones()), 6)                                # five records and the bed label
        self.assertTrue(L.status().attributes["review"]["bed"]["frozen"])  # the OA 7.4 freeze applies at once


def rev_of_item(L, item_id):
    return [i.rev for i in L.review.items(L) if i.id == item_id][0]


class CacheTest(unittest.TestCase):
    """`items()` is cached by a change counter and the local date (D39): every event that changes the list drops it."""

    def test_second_call_is_the_same_list_without_recomputation(self):
        L = places(3)
        a = L.review.tier2(L)
        b = L.review.tier2(L)
        self.assertIs(a, b)

    def test_invalidating_events(self):
        L = places(3)
        base = L.review.tier2(L)
        # a zone
        answer(L, "s0005-1", "approve")
        z = L.review.tier2(L)
        self.assertIsNot(z, base)
        self.assertEqual([i.id for i in z], ["s0005-2", "s0005-3"])
        # a rejection
        answer(L, "s0005-2", "reject")
        self.assertEqual([i.id for i in L.review.tier2(L)], ["s0005-3"])
        # a snooze
        answer(L, "s0005-3", "later")
        self.assertEqual(L.review.tier2(L), ())
        # the next nightly run ends the snooze
        night(L, 8)
        self.assertEqual([i.id for i in L.review.tier2(L)], ["s0005-3"])
        # a new suggestion, a removed one
        add_place(L, POS[4], sid="s0008-1", conf=0.95, evidence=evidence())
        self.assertEqual([i.id for i in L.review.tier2(L)], ["s0008-1", "s0005-3"])
        L.ns.suggestions.pop()
        self.assertEqual([i.id for i in L.review.tier2(L)], ["s0005-3"])
        # a label
        L.label_list.append({"id": "l0001", "created_s": 0, "x": POS[2][0], "y": POS[2][1], "name": "X",
                             "kind": "manual", "cells": frozenset()})
        self.assertEqual(L.review.tier2(L), ())

    def test_the_local_date_invalidates_the_snooze_marker_check(self):
        L = places(2)
        answer(L, "s0005-1", "later")
        self.assertEqual(ids(L, 2), ["s0005-2"])
        k0 = L.review._key2(L)
        L.now = BASE_MS + 9 * DAY * 1000                                  # a new local day without a nightly run
        self.assertEqual(ids(L, 2), ["s0005-2"])                          # still the same run date: still snoozed
        self.assertNotEqual(L.review._key2(L), k0)                         # but the cache key moved with the date

    def test_a_schedule_change_updates_the_tier_one_text(self):
        L = places(1)
        txt0 = L.review.items(L)[0].data["text"]
        L.handle(ScheduleState(True, 0, 360, T8))
        self.assertNotEqual(L.review.items(L)[0].data["text"], txt0)
        self.assertIn("before 00:00 or after 06:00", L.review.items(L)[0].data["text"])
        self.assertEqual(ids(L), [BED_ITEM, "s0005-1"])

    def test_cost(self):
        """200 suggestions, 100 zone records, 100 labels, 400 tags and 200 suppressions (spec 11.2 P5-R3 a)."""
        L = ready_learner()
        g = L.geom
        k = 0
        for j in range(12):
            for i in range(23):
                if k >= 200:
                    break
                x, y = -6.6 + 0.6 * i, 0.6 + 0.6 * j
                s = _new_suggestion("s%04d-%d" % (5, k + 1), "dwell_zone", 5, g.cells_within(x, y, 0.25), x, y)
                s["conf"] = 0.5 + 0.002 * k
                s["evidence"] = evidence()
                L.ns.suggestions.append(s)
                k += 1
        sug = L.ns.suggestions
        for k in range(100):                                              # records and labels at the first 100
            s = sug[k]
            ZN.create_zone(L, s, "Zone %d" % k, "dwell", T8)
            L.label_list.append({"id": "l%04d" % (k + 1), "created_s": 0, "x": s["cx"], "y": s["cy"], "name": "L%d" % k,
                                 "kind": "manual", "cells": g.cells_within(s["cx"], s["cy"], 0.4)})
        for k in range(100, 200):                                         # 100 rejections (and masks) of the rest
            if k >= 150:
                break
            answer(L, sug[k]["id"], "reject")
        L.p5.tags.clear()                                                 # filler: 400 tags and 200 suppressions
        for k in range(400):
            L.p5.alloc_tag("t%04d-1" % k)
        while len(L.review.suppressions) < 200:
            L.review.suppressions.append({"type": "place", "ref": "s9%03d-1" % len(L.review.suppressions),
                                          "night": 3})
        L.review.touch()
        self.assertEqual(len(L.p5.tags), 400)
        self.assertEqual(len(L.review.suppressions), 200)
        t0 = time.perf_counter()
        its = L.review.items(L)
        cold = time.perf_counter() - t0
        self.assertEqual(len([i for i in its if i.tier == 2]), 12)
        warm = []
        for _ in range(200):
            t0 = time.perf_counter()
            L.review.items(L)
            warm.append(time.perf_counter() - t0)
        self.assertLess(statistics.median(warm), 0.0002)                   # spec: a second call at most 0.2 ms
        ticks = []
        now = T8
        L.tick(now)
        for k in range(30):
            now += 1000
            t0 = time.perf_counter()
            L.tick(now)
            ticks.append(time.perf_counter() - t0)
        print("\n  P5-R3 (a): cold items() %.1f ms, warm median %.3f ms, tick median %.2f ms max %.2f ms"
              % (cold * 1000, statistics.median(warm) * 1000, statistics.median(ticks) * 1000, max(ticks) * 1000))
        self.assertLessEqual(statistics.median(ticks), 0.005)
        self.assertLessEqual(max(ticks), 0.020)


if __name__ == "__main__":
    unittest.main()
