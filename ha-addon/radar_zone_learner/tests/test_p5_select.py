"""The two selects (spec 2.6): labels, options, both modes, the resend, an unknown label, the Advanced select."""
import json
import unittest

from radar_zone_learner.core.types import AdvancedEntry, ReviewItem
from radar_zone_learner.shell.p5_types import AdvancedSelectLabel, ReviewSelectLabel
from radar_zone_learner.shell.p4_types import ReviewSelect
from radar_zone_learner.shell.select_model import (NO_ENTRY, NO_ITEM, SELECT_MODE, SELECT_RESEND_S, AdvancedModel,
                                                   SelectModel)
from tests.fakes import FakeLearner
from tests.p5_harness import P5Harness, bed_item, changed, place_item, window_item

SELECT_CFG = "homeassistant/select/radar_zones_review_select/config"
ADV_CFG = "homeassistant/select/radar_zones_review_advanced/config"
SELECT_STATE = "radar_zones/review/select"
SELECT_SET = "radar_zones/review/select/set"
ADV_STATE = "radar_zones/review/advanced"
ADV_SET = "radar_zones/review/advanced/set"


def merge_item(sid="s0006-1", tag="B", rev=1):
    return ReviewItem("mrg-" + sid, rev, "merge_into", {"tag": tag, "sid": sid, "zid": "z0001", "zname": "Desk",
                                                          "ext_cm": 40, "dir": "to the right", "r": 0.8}, 2)


def modify_item(zid="z0001", tag="C", rev=2):
    return ReviewItem("mod-" + zid, rev, "modify_zone", {"tag": tag, "sid": "s0005-1", "zid": zid, "zname": "Desk",
                                                           "shift_cm": 60, "dir": "to the left"}, 2)


def entry(verb, kind, target, name="", tag="", until="", rev=0):
    return AdvancedEntry("adv:%s:%s" % (verb, target), verb, kind, target, name, tag, until, rev)


class SelectModelDynamicTest(unittest.TestCase):
    def setUp(self):
        self.m = SelectModel("dynamic")
        self.items = [window_item(), bed_item(), place_item("s0005-1", tag="A"), place_item("s0006-1", tag="B"),
                      merge_item("s0007-1", "C"), modify_item("z0001", "D")]

    def test_default_mode_is_dynamic(self):
        self.assertEqual(SELECT_MODE, "dynamic")
        self.assertEqual(SELECT_RESEND_S, 2.0)

    def test_empty_state(self):
        self.assertEqual(self.m.options, [NO_ITEM])
        self.assertEqual(self.m.state(), "No item")
        self.assertNotEqual(self.m.state(), "None")

    def test_labels_and_options(self):
        self.assertTrue(self.m.update(self.items, "s0005-1"))
        self.assertEqual(self.m.options, ["No item", "Night window", "Bed zone", "Place A", "Place B", "Extend C",
                                          "Moved D"])
        self.assertEqual(self.m.state(), "Place A")
        self.assertEqual(self.m.label_of("mrg-s0007-1"), "Extend C")
        self.assertEqual(self.m.label_of("mod-z0001"), "Moved D")
        self.assertEqual(self.m.label_of("nope"), "")
        self.assertEqual(self.m.place_label("mrg-s0007-1"), "Place C")        # the text of 'You are in'
        self.assertEqual(self.m.place_label("mod-z0001"), "Place D")
        self.assertEqual(self.m.place_label("s0005-1"), "Place A")

    def test_update_reports_a_change_of_the_option_list_only(self):
        self.assertTrue(self.m.update(self.items, None))
        self.assertFalse(self.m.update(self.items, "s0006-1"))                # only the state changes
        self.assertEqual(self.m.state(), "Place B")
        self.assertTrue(self.m.update(self.items[:-1], "s0006-1"))
        self.assertFalse(self.m.update(list(self.items[:-1]), None))
        self.assertEqual(self.m.state(), "No item")

    def test_a_selected_id_that_is_not_in_the_list_gives_no_item(self):
        self.m.update(self.items, "s9999-9")
        self.assertEqual(self.m.state(), "No item")
        self.assertIsNone(self.m.selected)

    def test_resolve(self):
        self.m.update(self.items, None)
        for text, want in (("Night window", "night_window"), ("night window", "night_window"),
                           ("night_window", "night_window"), ("Bed zone", "bed_needs_data"),
                           ("bed_needs_data", "bed_needs_data"), ("Place A", "s0005-1"), ("  place b ", "s0006-1"),
                           ("Extend C", "mrg-s0007-1"), ("Moved D", "mod-z0001")):
            with self.subTest(text=text):
                self.assertEqual(self.m.resolve(text), (True, want))
        for none in ("No item", "no item", "None", "none", "", "  "):
            self.assertEqual(self.m.resolve(none), (True, None))
        for unknown in ("Place Z", "Desk", "Place 1", "Extend A"):
            self.assertIsNone(self.m.resolve(unknown))

    def test_a_static_label_resolves_even_when_the_item_is_not_open(self):
        self.m.update([place_item("s0005-1", tag="A")], None)
        self.assertEqual(self.m.resolve("Bed zone"), (True, "bed_needs_data"))     # as in P4: the core decides
        self.assertEqual(self.m.resolve("Night window"), (True, "night_window"))

    def test_at_most_fifteen_options(self):
        items = [window_item(), bed_item()] + [place_item("s%04d-1" % (n + 1), tag="T%d" % n) for n in range(20)]
        self.m.update(items, None)
        self.assertEqual(len(self.m.options), 15)
        self.assertEqual(self.m.options[-1], "Place T11")

    def test_a_missing_tag_falls_back_to_the_id_and_labels_stay_unique(self):
        bare = ReviewItem("s0005-1", 1, "dwell_zone", {}, 2)
        twin = place_item("s0006-1", tag="s0005-1")
        self.m.update([bare, twin], None)
        self.assertEqual(self.m.options, ["No item", "Place s0005-1", "Place s0005-1 (2)"])
        self.assertEqual(self.m.resolve("Place s0005-1 (2)"), (True, "s0006-1"))

    def test_every_option_fits_in_state_text(self):
        self.m.update(self.items, None)
        for label in self.m.options:
            self.assertLessEqual(len(label), 40)


class SelectModelP7LabelsTest(unittest.TestCase):
    """P7 (spec 6.4, 12.4): the labels ``Retire <tag>`` and ``Radar moved`` are added; the existing labels are unchanged."""

    def setUp(self):
        self.m = SelectModel("dynamic")
        self.items = [window_item(), bed_item(), place_item("s0005-1", tag="A"), merge_item("s0007-1", "C"),
                      modify_item("z0001", "D"),
                      ReviewItem("ret-z0002", 1, "retire_zone", {"tag": "E", "zid": "z0002", "zname": "Chair", "weeks": 4}, 2)]

    def test_retire_label_and_the_existing_labels(self):
        self.m.update(self.items, None)
        self.assertEqual(self.m.options, ["No item", "Night window", "Bed zone", "Place A", "Extend C", "Moved D",
                                          "Retire E"])
        self.assertEqual(self.m.resolve("Retire E"), (True, "ret-z0002"))

    def test_radar_moved_is_a_static_label_after_the_two_p4_ones(self):
        radar = ReviewItem("radar_moved", 2, "radar_moved", {"shift_cm": 80, "deg": 10.0, "streak": 2}, 1)
        self.m.update([window_item(), bed_item(), radar] + self.items[2:], "radar_moved")
        self.assertEqual(self.m.options[:5], ["No item", "Night window", "Bed zone", "Radar moved", "Place A"])
        self.assertEqual(self.m.state(), "Radar moved")
        self.assertEqual(self.m.resolve("Radar moved"), (True, "radar_moved"))


class SelectModelSlotsTest(unittest.TestCase):
    def setUp(self):
        self.m = SelectModel("slots")
        self.items = [window_item(), bed_item(), place_item("s0005-1", tag="A"), place_item("s0006-1", tag="B"),
                      merge_item("s0007-1", "C")]

    def test_options_never_change(self):
        first = self.m.options
        self.assertEqual(first, ["No item", "Night window", "Bed zone"] + ["Place %d" % k for k in range(1, 13)])
        self.assertFalse(self.m.update(self.items, None))
        self.assertFalse(self.m.update(self.items[:2], None))
        self.assertEqual(self.m.options, first)
        self.assertEqual(self.m.slot_labels(), ["Place %d" % k for k in range(1, 13)])

    def test_slot_k_is_the_k_th_tier_two_item(self):
        self.m.update(self.items, "s0006-1")
        self.assertEqual(self.m.state(), "Place 2")
        self.assertEqual(self.m.label_of("s0005-1"), "Place 1")
        self.assertEqual(self.m.label_of("mrg-s0007-1"), "Place 3")
        self.assertEqual(self.m.place_label("mrg-s0007-1"), "Place 3")
        self.assertEqual(self.m.resolve("Place 3"), (True, "mrg-s0007-1"))
        self.assertEqual(self.m.resolve("Place 1"), (True, "s0005-1"))
        self.assertIsNone(self.m.resolve("Place 4"))                           # an empty slot
        # the list shifts: slot 1 now means another place
        self.m.update(self.items[:2] + self.items[3:], None)
        self.assertEqual(self.m.resolve("Place 1"), (True, "s0006-1"))

    def test_static_labels_and_none(self):
        self.m.update(self.items, "night_window")
        self.assertEqual(self.m.state(), "Night window")
        self.assertEqual(self.m.resolve("Bed zone"), (True, "bed_needs_data"))
        self.assertEqual(self.m.resolve("No item"), (True, None))

    def test_a_bad_mode_is_refused(self):
        with self.assertRaises(ValueError):
            SelectModel("fixed")


class AdvancedModelTest(unittest.TestCase):
    def setUp(self):
        self.m = AdvancedModel()
        self.entries = [entry("undo", "bed", "bed"), entry("undo", "night_window", "nw", until="2026-02-01"),
                        entry("undo", "place", "s0005-1", tag="A"), entry("real", "reflection", "s0004-2", tag="B"),
                        entry("rename", "zone", "z0001", name="Desk", rev=2),
                        entry("remove", "zone", "z0001", name="Desk", rev=2),
                        entry("rename", "label", "l0001", name="Bed"), entry("remove", "label", "l0001", name="Bed")]

    def test_empty(self):
        self.assertEqual(self.m.options, [NO_ENTRY])
        self.assertEqual(self.m.state(), "No entry")

    def test_labels(self):
        self.assertTrue(self.m.update(self.entries))
        self.assertEqual(self.m.options, ["No entry", "Undo: No bed in view", "Undo: Night window denied until 2026-02-01",
                                          "Undo: Place A rejected", "Real place: Reflection B", "Rename zone: Desk",
                                          "Remove zone: Desk", "Rename mark: Bed", "Remove mark: Bed"])
        self.assertFalse(self.m.update(list(self.entries)))

    def test_repeated_labels_get_a_number(self):
        twins = [entry("rename", "zone", "z0001", name="Desk"), entry("rename", "zone", "z0002", name="Desk"),
                 entry("rename", "zone", "z0003", name="Desk")]
        self.m.update(twins)
        self.assertEqual(self.m.options, ["No entry", "Rename zone: Desk", "Rename zone: Desk (2)",
                                          "Rename zone: Desk (3)"])
        self.assertEqual(self.m.resolve("Rename zone: Desk (3)"), (True, "adv:rename:z0003"))

    def test_a_long_name_is_cut_at_24_characters(self):
        self.m.update([entry("rename", "zone", "z0001", name="n" * 40)])
        self.assertEqual(self.m.options[1], "Rename zone: " + "n" * 23 + "…")

    def test_a_missing_tag_is_a_question_mark(self):
        self.m.update([entry("undo", "place", "s0005-1")])
        self.assertEqual(self.m.options[1], "Undo: Place ? rejected")

    def test_selection(self):
        self.m.update(self.entries)
        self.assertEqual(self.m.resolve("Remove zone: Desk"), (True, "adv:remove:z0001"))
        self.assertEqual(self.m.resolve("No entry"), (True, None))
        self.assertEqual(self.m.resolve("none"), (True, None))
        self.assertIsNone(self.m.resolve("Remove zone: Other"))
        self.m.select("adv:remove:z0001")
        self.assertEqual(self.m.state(), "Remove zone: Desk")
        self.assertEqual(self.m.selected, "adv:remove:z0001")
        self.assertEqual(self.m.entry("adv:remove:z0001").target, "z0001")
        # the entry goes away: the selection is dropped
        self.m.update([e for e in self.entries if e.id != "adv:remove:z0001"])
        self.assertIsNone(self.m.selected)
        self.assertEqual(self.m.state(), "No entry")
        self.m.select("adv:remove:z9999")
        self.assertIsNone(self.m.selected)

    def test_at_most_sixty_entries(self):
        many = [entry("rename", "zone", "z%04d" % k, name="Zone %d" % k) for k in range(80)]
        self.m.update(many)
        self.assertEqual(len(self.m.options), 61)


class SelectLayerTest(P5Harness, unittest.TestCase):
    def make_learner(self):
        return FakeLearner()

    def configs(self, topic=SELECT_CFG):
        return [json.loads(p[1]) for p in self.published(topic)]

    def test_config_state_and_resend_when_the_options_change(self):
        self.connect()
        self.assertEqual(self.configs()[-1]["options"], ["No item"])
        self.client.published.clear()
        self.learner.tick_outputs = [changed([window_item(), bed_item(), place_item()], "night_window")]
        self.run_for(1)
        cfgs = self.configs()
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0]["options"], ["No item", "Night window", "Bed zone", "Place A"])
        order = [p[0] for p in self.client.published]
        self.assertLess(order.index(SELECT_CFG), order.index(SELECT_STATE))      # the config first, then the state
        self.assertEqual(self.last(SELECT_STATE), "Night window")
        n = len(self.published(SELECT_STATE))
        self.run_for(1)
        self.assertEqual(len(self.published(SELECT_STATE)), n)                   # not yet: the resend is 2 s later
        self.run_for(1.5)
        self.assertEqual(len(self.published(SELECT_STATE)), n + 1)               # the forced resend
        self.assertEqual(self.last(SELECT_STATE), "Night window")
        self.run_for(5)
        self.assertEqual(len(self.published(SELECT_STATE)), n + 1)               # once

    def test_no_config_when_only_the_state_changes(self):
        self.connect()
        self.learner.tick_outputs = [changed([window_item(), bed_item()], "night_window")]
        self.run_for(1)
        self.client.published.clear()
        self.learner.tick_outputs = [changed([window_item(), bed_item()], "bed_needs_data")]
        self.run_for(1)
        self.assertEqual(self.configs(), [])
        self.assertEqual(self.last(SELECT_STATE), "Bed zone")

    def test_a_label_of_the_dynamic_select_reaches_the_core_as_an_item(self):
        self.connect()
        self.learner.tick_outputs = [changed([window_item(), place_item(), place_item("s0006-1", tag="B")], None)]
        self.run_for(1)
        self.client.fire_message(SELECT_SET, "Place B")
        self.client.fire_message(SELECT_SET, "No item")
        self.client.fire_message(SELECT_SET, "Bed zone")
        self.pump()
        sel = [e for e in self.learner.events if isinstance(e, ReviewSelect)]
        self.assertEqual([e.item for e in sel], ["s0006-1", None, "bed_needs_data"])

    def test_an_unknown_label_is_ignored_and_the_config_and_state_go_out_again(self):
        self.connect()
        self.learner.tick_outputs = [changed([window_item(), place_item()], "night_window")]
        self.run_for(1)
        self.client.published.clear()
        self.client.fire_message(SELECT_SET, "Place Z")                           # an option that is gone
        self.pump()
        self.assertEqual([e for e in self.learner.events if isinstance(e, (ReviewSelect, ReviewSelectLabel))], [])
        self.assertEqual(self.configs()[-1]["options"], ["No item", "Night window", "Place A"])
        self.assertEqual(self.last(SELECT_STATE), "Night window")
        order = [p[0] for p in self.client.published]
        self.assertLess(order.index(SELECT_CFG), order.index(SELECT_STATE))
        self.assertEqual(self.layer.unknown_labels, 1)

    def test_slot_mode_never_republishes_the_config(self):
        self.layer.sel = SelectModel("slots")
        self.connect()
        first = self.configs()[-1]["options"]
        self.assertEqual(len(first), 15)
        self.client.published.clear()
        self.learner.tick_outputs = [changed([window_item(), place_item(), place_item("s0006-1", tag="B")], "s0006-1")]
        self.run_for(1)
        self.assertEqual(self.configs(), [])
        self.assertEqual(self.last(SELECT_STATE), "Place 2")
        self.client.fire_message(SELECT_SET, "Place 1")
        self.pump()
        self.assertEqual([e.item for e in self.learner.events if isinstance(e, ReviewSelect)], ["s0005-1"])
        self.learner.tick_outputs = [changed([window_item(), place_item("s0006-1", tag="B")], "s0006-1")]
        self.run_for(1)
        self.assertEqual(self.last(SELECT_STATE), "Place 1")                     # the slot now means place B

    def test_the_advanced_select_syncs_like_the_review_select(self):
        self.connect()
        self.assertEqual(self.configs(ADV_CFG)[-1]["options"], ["No entry"])
        self.client.published.clear()
        entries = (entry("undo", "bed", "bed"), entry("rename", "zone", "z0001", name="Desk"))
        from radar_zone_learner.core.types import AdvancedChanged
        self.learner.tick_outputs = [AdvancedChanged(entries)]
        self.run_for(1)
        self.assertEqual(self.configs(ADV_CFG)[-1]["options"], ["No entry", "Undo: No bed in view", "Rename zone: Desk"])
        self.assertEqual(self.published(ADV_STATE), [])                          # the state did not change: not repeated
        self.client.published.clear()
        self.run_for(2.5)
        self.assertEqual(self.last(ADV_STATE), "No entry")                       # the forced resend, 2 s after the config
        self.assertEqual(len(self.published(ADV_STATE)), 1)
        # picking an entry changes the state only; it reaches no core event
        self.client.fire_message(ADV_SET, "Rename zone: Desk")
        self.pump()
        self.assertEqual(self.last(ADV_STATE), "Rename zone: Desk")
        self.assertEqual(self.layer.adv.selected, "adv:rename:z0001")
        self.assertEqual([e for e in self.learner.events if isinstance(e, (ReviewSelect, AdvancedSelectLabel))], [])
        # an unknown label: config and state again, selection unchanged
        self.client.published.clear()
        self.client.fire_message(ADV_SET, "Remove zone: Gone")
        self.pump()
        self.assertEqual(self.configs(ADV_CFG)[-1]["options"], ["No entry", "Undo: No bed in view", "Rename zone: Desk"])
        self.assertEqual(self.last(ADV_STATE), "Rename zone: Desk")
        self.client.fire_message(ADV_SET, "No entry")
        self.pump()
        self.assertEqual(self.last(ADV_STATE), "No entry")
        self.assertIsNone(self.layer.adv.selected)

    def test_the_second_pass_sends_both_select_states_again(self):
        self.connect()
        self.learner.tick_outputs = [changed([window_item(), place_item()], "night_window")]
        self.run_for(3)
        self.client.published.clear()
        self.layer.second_pass(self.clock.clock_ms())
        self.assertEqual(self.last(SELECT_STATE), "Night window")
        self.assertEqual(self.last(ADV_STATE), "No entry")


if __name__ == "__main__":
    unittest.main()
