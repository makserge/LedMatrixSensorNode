"""The P7 item texts and labels (P7 spec 6.4, 6.5, check P7-11 shell part): the four texts with the detail setting off and on,
no zone name, hour or date unless 'Detailed review text' is on, the worst-case lengths, the P5-path items that keep their P5
texts, the push state with the attention kind, the select labels, and the pending attributes with the new items."""
import json
import re
import unittest

from radar_zone_learner.core.params import DEFAULT_P7
from radar_zone_learner.core.types import PushRequest, ReviewItem
from radar_zone_learner.shell.review_p5 import (P7_TEXT_MAX, item_generic_text, item_text_p5, pending_json, push_state)
from radar_zone_learner.shell.select_model import SelectModel
from tests.test_p5_review_text import merge_item as p5_merge_item, modify_item as p5_modify_item
from tests.p5_harness import bed_item, place_item, window_item

WINDOW = (True, 0, 360)
LONGEST_DIR = "away from the radar"
NAME32 = "N" * 32


def mod(src="cand", **kw):
    data = {"tag": "C", "sid": "s0005-1", "zid": "z0001", "zname": "Desk", "shift_cm": 110, "dir": "to the right",
            "r": 0.8, "src": src}
    data.update(kw)
    return ReviewItem("mod-" + data["zid"], 2, "modify_zone", data, 2)


def mrg(**kw):
    data = {"tag": "B", "sid": "s0006-1", "zid": "z0001", "zname": "Desk", "ext_cm": 40, "dir": "to the left", "r": 0.8,
            "src": "cand"}
    data.update(kw)
    return ReviewItem("mrg-" + data["sid"], 1, "merge_into", data, 2)


def ret(**kw):
    data = {"tag": "D", "zid": "z0002", "zname": "Armchair", "weeks": 4}
    data.update(kw)
    return ReviewItem("ret-" + data["zid"], 1, "retire_zone", data, 2)


def radar(**kw):
    data = {"shift_cm": 80, "deg": 10.0, "streak": 2}
    data.update(kw)
    return ReviewItem("radar_moved", 1, "radar_moved", data, 1)


class ModifyTextTest(unittest.TestCase):
    def test_detail_off_names_the_place_by_its_tag_only(self):
        self.assertEqual(
            item_text_p5(mod(), WINDOW, False),
            "Place C moved? Approve = update it (entities and automations stay), Rename = keep both, Reject = ignore the "
            "new spot. It seems to be about 1.1 m to the right from where it was.")

    def test_detail_on_names_the_zone(self):
        self.assertEqual(
            item_text_p5(mod(), WINDOW, True),
            "Has Desk moved here? Approve = update it (entities and automations stay), Rename = keep both, Reject = ignore "
            "the new spot. It seems to be about 1.1 m to the right from where it was.")

    def test_the_action_sentences_come_before_the_distance(self):
        text = item_text_p5(mod(), WINDOW, False)
        self.assertLess(text.index("Approve"), text.index("It seems"))
        self.assertLess(text.index("Reject"), text.index("It seems"))

    def test_a_source_of_either_kind_is_a_p7_item_and_the_source_does_not_change_the_text(self):
        self.assertEqual(item_text_p5(mod("own"), WINDOW, False), item_text_p5(mod("cand"), WINDOW, False))

    def test_the_distance_is_in_metres_to_a_tenth(self):
        for cm, want in ((50, "0.5 m"), (96, "1.0 m"), (400, "4.0 m"), (0, "0.0 m")):
            self.assertIn("about %s to the right" % want, item_text_p5(mod(shift_cm=cm), WINDOW, False))

    def test_a_missing_direction_leaves_no_double_space(self):
        text = item_text_p5(mod(dir=""), WINDOW, False)
        self.assertIn("about 1.1 m from where it was", text)
        self.assertNotIn("  ", text)

    def test_missing_data_is_tolerated(self):
        bare = ReviewItem("mod-z0001", 1, "modify_zone", {"src": "own"}, 2)
        self.assertIn("Place ? moved?", item_text_p5(bare, WINDOW, False))
        self.assertIn("Has the zone moved here?", item_text_p5(bare, WINDOW, True))


class MergeTextTest(unittest.TestCase):
    def test_detail_off(self):
        self.assertEqual(
            item_text_p5(mrg(), WINDOW, False),
            "Extend place B? Place B touches a place you confirmed (40 cm to the left) and is used at similar hours. "
            "Approve = extend it, Rename = keep it as a new place, Reject = ignore it.")

    def test_detail_on(self):
        self.assertEqual(
            item_text_p5(mrg(), WINDOW, True),
            "Extend Desk? Place B touches Desk (40 cm to the left) and is used at similar hours. Approve = extend it, "
            "Rename = keep it as a new place, Reject = ignore it.")


class RetireTextTest(unittest.TestCase):
    def test_detail_off_says_nothing_about_which_place_or_how_long(self):
        text = item_text_p5(ret(), WINDOW, False)
        self.assertEqual(
            text, "Remove place D? It has not been used for a long time (the map shows it). Approve = remove it and its "
                  "entities (automations that use them stop working), Reject = keep it for 90 days.")
        self.assertNotIn("Armchair", text)
        self.assertNotIn("weeks", text)

    def test_detail_on_names_the_zone_and_the_weeks(self):
        self.assertEqual(
            item_text_p5(ret(), WINDOW, True),
            "Remove Armchair? It has not been used for 4 weeks. Approve = remove it and its entities (automations that use "
            "them stop working), Reject = keep it for 90 days.")

    def test_the_keep_days_are_the_cores_constant(self):
        self.assertEqual(DEFAULT_P7.keep_days, 90)
        self.assertIn("for %d days." % DEFAULT_P7.keep_days, item_text_p5(ret(), WINDOW, False))


class RadarMovedTextTest(unittest.TestCase):
    def test_the_text_is_the_same_with_and_without_detail(self):
        want = ("The radar seems to have been moved or turned (about 80 cm, 10.0 degrees). Approve = reset the learned maps "
                "(your places are kept and checked again), Reject = ignore this.")
        self.assertEqual(item_text_p5(radar(), WINDOW, False), want)
        self.assertEqual(item_text_p5(radar(), WINDOW, True), want)

    def test_a_turn_with_a_decimal_and_a_negative_turn(self):
        self.assertIn("(about 35 cm, 7.5 degrees)", item_text_p5(radar(shift_cm=35, deg=7.5), WINDOW, False))
        self.assertIn("(about 35 cm, 7.5 degrees)", item_text_p5(radar(shift_cm=35, deg=-7.5), WINDOW, False))
        self.assertIn("(about 0 cm, 0.0 degrees)", item_text_p5(radar(shift_cm=0, deg=0.0), WINDOW, False))

    def test_missing_data_is_tolerated(self):
        bare = ReviewItem("radar_moved", 1, "radar_moved", {}, 1)
        self.assertIn("(about 0 cm, 0.0 degrees)", item_text_p5(bare, WINDOW, False))
        self.assertIn("seems to have been moved", item_text_p5(ReviewItem("radar_moved", 1, "radar_moved", None, 1),
                                                               WINDOW, True))


class PrivacyAndLengthTest(unittest.TestCase):
    ITEMS = (mod(), mod("own"), mrg(), ret(), radar())

    def test_without_detail_no_text_carries_a_zone_name(self):
        # the zone is called 'Armchair'/'Desk' in every item above: neither word may appear with the setting off
        for item in self.ITEMS:
            text = item_text_p5(item, WINDOW, False)
            self.assertNotIn("Desk", text, item.id)
            self.assertNotIn("Armchair", text, item.id)
            self.assertEqual(item_generic_text(item), text)

    def test_no_text_carries_an_hour_or_a_date(self):
        for item in self.ITEMS:
            for detail in (False, True):
                text = item_text_p5(item, WINDOW, detail)
                self.assertIsNone(re.search(r"\b\d{1,2}:\d{2}\b", text), (item.id, detail))
                self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", text), (item.id, detail))
                self.assertIsNone(re.search(r"\b\d{1,2}-\d{2} h\b", text), (item.id, detail))
                self.assertNotIn("Used mostly", text)

    def test_worst_case_lengths(self):
        """The longest tag, name, distance, direction and weeks of spec 6.4: every text fits in 255 characters (a Home
        Assistant state), and the lengths are the ones the spec states."""
        worst = {
            "mod": (mod(tag="AAA", zname=NAME32, shift_cm=400, dir=LONGEST_DIR), (185, 217)),
            "ret": (ret(tag="AAA", zname=NAME32, weeks=99), (183, 184)),
            "radar": (radar(shift_cm=100, deg=10.0), (171, 171)),
            "mrg": (mrg(tag="AAA", zname=NAME32, ext_cm=400, dir=LONGEST_DIR), (190, 224)),
        }
        for name, (item, (off, on)) in worst.items():
            self.assertEqual(len(item_text_p5(item, WINDOW, False)), off, name)
            self.assertEqual(len(item_text_p5(item, WINDOW, True)), on, name)
            self.assertLessEqual(len(item_text_p5(item, WINDOW, True)), P7_TEXT_MAX)

    def test_a_longer_name_than_the_core_allows_is_still_cut_to_255(self):
        for item in (mod(zname="x" * 400), mrg(zname="x" * 400), ret(zname="x" * 400)):
            text = item_text_p5(item, WINDOW, True)
            self.assertLessEqual(len(text), 255, item.id)
        self.assertTrue(item_text_p5(mod(zname="x" * 400), WINDOW, True).endswith("…"))
        # a name the core allows (32 characters) never costs an action sentence: the longest text keeps all three
        text = item_text_p5(mod(zname=NAME32, shift_cm=400, dir=LONGEST_DIR), WINDOW, True)
        for word in ("Approve = update it", "Rename = keep both", "Reject = ignore the new spot"):
            self.assertIn(word, text)

    def test_the_item_text_entity_state_stays_within_255(self):
        from radar_zone_learner.shell.review import state_text
        for item in self.ITEMS:
            text = item_text_p5(item, WINDOW, True)
            self.assertEqual(state_text(text), text)


class P5PathUnchangedTest(unittest.TestCase):
    """A merge or modify item without the key ``src`` was made by the P5 scaffolding: it keeps the P5 text."""

    def test_the_p5_modify_text(self):
        item = p5_modify_item()
        self.assertNotIn("src", item.data)
        self.assertEqual(item_text_p5(item, WINDOW, False),
                         "Has Desk moved? Its place changed by about 60 cm towards the radar. Approve to update it, Rename "
                         "to keep the old one and add the new place, Reject to leave it.")
        self.assertEqual(item_text_p5(item, WINDOW, True), item_text_p5(item, WINDOW, False))

    def test_the_p5_merge_text(self):
        item = p5_merge_item()
        self.assertNotIn("src", item.data)
        self.assertEqual(item_text_p5(item, WINDOW, False),
                         "Extend Desk? Place B touches it (40 cm to the right) and is used at similar hours. Approve to "
                         "extend it, Rename to keep it as a new place, Reject to ignore it.")

    def test_the_same_data_with_a_source_gets_the_p7_text(self):
        data = dict(p5_modify_item().data, src="cand")
        text = item_text_p5(ReviewItem("mod-z0001", 2, "modify_zone", data, 2), WINDOW, False)
        self.assertTrue(text.startswith("Place C moved?"))

    def test_the_place_and_the_two_p4_items_are_untouched(self):
        self.assertIn("New place A:", item_text_p5(place_item(), WINDOW, False))
        self.assertIn("radar", item_text_p5(window_item(), WINDOW, False).lower())
        self.assertIn("bed", item_text_p5(bed_item(), WINDOW, False).lower())


class PushStateTest(unittest.TestCase):
    def test_a_daily_request_is_unchanged(self):
        req = PushRequest(7, 6, "daily", 3, "s0005-1", 2, ("pabc1234",))
        self.assertEqual(push_state(req, None, False), "7|6|daily|3|2|s0005-1|pabc1234|")

    def test_an_attention_request_writes_the_kind_attention(self):
        req = PushRequest(8, 7, "daily", 2, None, None, (), True)
        state = push_state(req, None, False)
        self.assertEqual(state, "8|7|attention|2||||")
        self.assertEqual(len(state.split("|")), 8)
        self.assertLessEqual(len(state), 255)

    def test_a_request_of_the_real_planner_for_a_radar_item_is_written_as_attention(self):
        """The core's planner (not a hand-made request): kind stays ``daily`` in its state, the head is None, and the shell
        writes ``attention`` in the third field; the head item and its revision fields are empty."""
        from tests.test_p7_notify import MOD, RADAR, ready_learner, step
        L = ready_learner()
        (req,) = step(L, [RADAR(), MOD()], 40)
        self.assertEqual((req.kind, req.attention, req.head_id), ("daily", True, None))
        fields = push_state(req, None, False).split("|")
        self.assertEqual(len(fields), 8)
        self.assertEqual((fields[2], fields[3], fields[4], fields[5], fields[7]), ("attention", "2", "", "", ""))
        self.assertEqual(L.p5.push["kind"], "daily")

    def test_the_core_keeps_kind_daily_and_the_shell_is_the_only_writer_of_the_word(self):
        from radar_zone_learner.core.types import PUSH_KINDS
        self.assertNotIn("attention", PUSH_KINDS)                                   # a 0.6.0 app must still load the state

    def test_an_attention_request_has_no_head_and_so_no_text_even_with_detail_on(self):
        req = PushRequest(8, 7, "daily", 2, None, None, (), True)
        self.assertEqual(push_state(req, None, True), "8|7|attention|2||||")
        self.assertEqual(push_state(req, "", True), "8|7|attention|2||||")

    def test_the_other_kinds_are_untouched_by_a_false_flag(self):
        for kind in ("none", "setup", "daily"):
            self.assertIn("|%s|" % kind, push_state(PushRequest(1, 0, kind, 1, "s0005-1", 1, (), False), None, False))

    def test_a_request_object_without_the_field_still_works(self):
        class Old:
            seq, acked, kind, count, head_id, head_rev, acks = 3, 2, "daily", 1, "s0005-1", 1, ()
        self.assertEqual(push_state(Old(), None, False), "3|2|daily|1|1|s0005-1||")


class LabelTest(unittest.TestCase):
    def test_retire_and_radar_moved_labels_in_the_dynamic_select(self):
        m = SelectModel("dynamic")
        items = [window_item(), bed_item(), radar(), place_item("s0005-1", tag="A"), mod(tag="C"), mrg(tag="B"),
                 ret(tag="D")]
        self.assertTrue(m.update(items, None))
        self.assertEqual(m.options, ["No item", "Night window", "Bed zone", "Radar moved", "Place A", "Moved C", "Extend B",
                                     "Retire D"])
        self.assertEqual(m.label_of("radar_moved"), "Radar moved")
        self.assertEqual(m.label_of("ret-z0002"), "Retire D")
        self.assertEqual(m.resolve("Retire D"), (True, "ret-z0002"))
        self.assertEqual(m.resolve("radar moved"), (True, "radar_moved"))
        self.assertEqual(m.resolve("radar_moved"), (True, "radar_moved"))
        self.assertEqual(m.place_label("ret-z0002"), "Place D")                     # the text of 'You are in'

    def test_radar_moved_is_not_cut_by_the_tier_two_cap(self):
        m = SelectModel("dynamic")
        items = [radar()] + [place_item("s%04d-1" % (n + 1), tag="T%d" % n) for n in range(20)]
        m.update(items, None)
        self.assertEqual(m.options[1], "Radar moved")
        self.assertEqual(len(m.options), 1 + 1 + 12)

    def test_a_selected_radar_moved_item_shows_its_label(self):
        m = SelectModel("dynamic")
        m.update([radar()], "radar_moved")
        self.assertEqual(m.state(), "Radar moved")
        self.assertEqual(m.selected, "radar_moved")

    def test_the_slot_mode_options_are_still_the_fixed_fifteen_and_the_radar_item_takes_a_slot(self):
        m = SelectModel("slots")
        first = m.options
        self.assertEqual(first, ["No item", "Night window", "Bed zone"] + ["Place %d" % k for k in range(1, 13)])
        self.assertFalse(m.update([radar(), place_item("s0005-1", tag="A")], "radar_moved"))
        self.assertEqual(m.options, first)
        self.assertEqual(m.label_of("radar_moved"), "Place 1")
        self.assertEqual(m.resolve("Place 1"), (True, "radar_moved"))
        self.assertEqual(m.resolve("Radar moved"), (True, "radar_moved"))

    def test_the_unique_suffix_keeps_two_retire_items_with_one_tag_apart(self):
        m = SelectModel("dynamic")
        m.update([ret(tag="D"), ret(tag="D", zid="z0003")], None)
        self.assertEqual(m.options, ["No item", "Retire D", "Retire D (2)"])


class RealCoreItemsTest(unittest.TestCase):
    """The items a real P7 learner makes (the contract of Appendix A.6) go through the texts and the select without a missing
    key: the data keys the shell reads are the ones the core writes."""

    def test_a_real_modify_item(self):
        from tests.test_moves import sofa_scene
        L, rec, s = sofa_scene()
        L.review.touch()
        item = [i for i in L.review.items(L) if i.type == "modify_zone"][0]
        off, on = item_text_p5(item, WINDOW, False), item_text_p5(item, WINDOW, True)
        self.assertTrue(off.startswith("Place %s moved?" % item.data["tag"]), off)
        self.assertTrue(on.startswith("Has Desk moved here?"), on)
        self.assertNotIn("Desk", off)
        self.assertIn("about 1.1 m to the right from where it was.", off)
        m = SelectModel("dynamic")
        m.update([item], None)
        self.assertEqual(m.options, ["No item", "Moved %s" % item.data["tag"]])

    def test_a_real_retire_item(self):
        from tests.test_retire import retire_scene
        L, rec = retire_scene()
        L.review.touch()
        item = [i for i in L.review.items(L) if i.type == "retire_zone"][0]
        off, on = item_text_p5(item, WINDOW, False), item_text_p5(item, WINDOW, True)
        self.assertTrue(off.startswith("Remove place %s?" % item.data["tag"]), off)
        self.assertRegex(on, r"^Remove Desk\? It has not been used for \d+ weeks\.")
        self.assertNotIn("Desk", off)
        m = SelectModel("dynamic")
        m.update([item], None)
        self.assertEqual(m.options, ["No item", "Retire %s" % item.data["tag"]])

    def test_a_real_radar_moved_item(self):
        from tests.p7_harness import make, set_run
        from tests.test_p7_review import open_radar
        L = make()
        set_run(L, 40)
        open_radar(L)
        item = [i for i in L.review.items(L) if i.type == "radar_moved"][0]
        text = item_text_p5(item, WINDOW, False)
        self.assertIn("(about 80 cm, 10.0 degrees)", text)
        self.assertEqual(text, item_text_p5(item, WINDOW, True))
        m = SelectModel("dynamic")
        m.update([item], "radar_moved")
        self.assertEqual((m.options, m.state()), (["No item", "Radar moved"], "Radar moved"))
        self.assertEqual(m.resolve("Radar moved"), (True, "radar_moved"))


class PendingAttributesTest(unittest.TestCase):
    def test_a_full_list_with_the_new_items_fits_the_attribute_budget_and_carries_no_name(self):
        items = [window_item(), bed_item(), radar(), mod(), mrg(), ret()] + [
            place_item("s%04d-1" % (n + 1), tag="T%d" % n) for n in range(9)]
        rows = [{"id": i.id, "rev": i.rev, "type": i.type, "tier": i.tier, "label": "x", "text": item_text_p5(i, WINDOW, False)}
                for i in items]
        payload = pending_json(None, rows, 0)
        self.assertLessEqual(len(payload.encode("ascii")), 8000)
        obj = json.loads(payload)
        self.assertEqual([r["id"] for r in obj["items"]][:6], ["night_window", "bed_needs_data", "radar_moved", "mod-z0001",
                                                              "mrg-s0006-1", "ret-z0002"])
        for row in obj["items"]:
            self.assertNotIn("Desk", row["text"])
            self.assertNotIn("Armchair", row["text"])


if __name__ == "__main__":
    unittest.main()
