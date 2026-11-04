"""The P5 texts (spec 2.7, 2.10, 2.11, 6.3, 8.6): item texts with the detail setting off and on, the push state,
'You are in', the Advanced labels, the pending attributes."""
import json
import re
import unittest

from radar_zone_learner.core.types import AdvancedEntry, PushRequest, ReviewItem, WhereView
from radar_zone_learner.shell.review import state_text
from radar_zone_learner.shell.review_p5 import (advanced_label, hours_phrase, item_generic_text, item_text_p5,
                                                pending_json, position_phrase, push_state, you_are_in_attr,
                                                you_are_in_text)
from tests.p5_harness import bed_item, place_item, window_item

WINDOW = (True, 0, 360)


def merge_item():
    return ReviewItem("mrg-s0006-1", 1, "merge_into", {"tag": "B", "sid": "s0006-1", "zid": "z0001", "zname": "Desk",
                                                         "ext_cm": 40, "dir": "to the right", "r": 0.8}, 2)


def modify_item():
    return ReviewItem("mod-z0001", 2, "modify_zone", {"tag": "C", "sid": "s0005-1", "zid": "z0001", "zname": "Desk",
                                                        "shift_cm": 60, "dir": "towards the radar"}, 2)


class PlaceTextTest(unittest.TestCase):
    def test_default_text_has_no_hours(self):
        item = place_item(hrs_wd=("09-12", "13-17"), hrs_we=("10-11",))
        text = item_text_p5(item, WINDOW, False)
        self.assertEqual(text, "New place A: a place where you stay, 2.5 m to the right, 2.2 m from the radar. "
                               "Look at the map, then Approve, Rename or Reject.")
        self.assertNotIn(" h", text)
        self.assertIsNone(re.search(r"\d\d-\d\d", text))
        self.assertEqual(item_generic_text(item), text)

    def test_detail_adds_the_typical_hours(self):
        item = place_item(hrs_wd=("09-12", "13-17"), hrs_we=("09-12", "13-17"))
        self.assertTrue(item_text_p5(item, WINDOW, True).endswith("Used mostly 09-12 h and 13-17 h."))

    def test_hours_phrase_cases(self):
        self.assertEqual(hours_phrase((), ()), "")
        self.assertEqual(hours_phrase(("09-12",), ()), "Used mostly 09-12 h.")
        self.assertEqual(hours_phrase((), ("10-11",)), "Used mostly 10-11 h at weekends.")
        self.assertEqual(hours_phrase(("09-12",), ("09-12",)), "Used mostly 09-12 h.")
        self.assertEqual(hours_phrase(("09-12", "13-17"), ("10-11",)),
                         "Used mostly 09-12 h and 13-17 h on weekdays; at weekends 10-11 h.")
        self.assertEqual(hours_phrase(("06-08", "12-14", "19-24"), ()), "Used mostly 06-08 h, 12-14 h and 19-24 h.")
        self.assertEqual(hours_phrase(["09-12"], None), "Used mostly 09-12 h.")

    def test_no_hours_no_sentence_even_with_detail(self):
        self.assertNotIn("Used mostly", item_text_p5(place_item(), WINDOW, True))

    def test_kinds_and_positions(self):
        self.assertIn("a short-task spot", item_text_p5(place_item(kind="short_task"), WINDOW, False))
        self.assertIn("a bed", item_text_p5(place_item(kind="bed"), WINDOW, False))
        self.assertIn("1.0 m to the left, 3.0 m from the radar",
                      item_text_p5(place_item(x_cm=-100, y_cm=300), WINDOW, False))
        self.assertEqual(position_phrase(4, 150), "straight ahead, 1.5 m from the radar")
        self.assertEqual(position_phrase(-4, 150), "straight ahead, 1.5 m from the radar")
        self.assertEqual(position_phrase(5, 150), "0.1 m to the right, 1.5 m from the radar")
        self.assertEqual(position_phrase(-5, 0), "0.1 m to the left, 0.0 m from the radar")

    def test_missing_data_is_tolerated(self):
        bare = ReviewItem("s0005-1", 1, "dwell_zone", {}, 2)
        text = item_text_p5(bare, WINDOW, True)
        self.assertIn("New place ?: a place", text)
        self.assertEqual(item_text_p5(ReviewItem("s0005-1", 1, "dwell_zone", None, 2), WINDOW, False)[:9], "New place")

    def test_no_stay_time_is_ever_in_a_text(self):
        item = place_item(hrs_wd=("09-12",), hrs_we=("10-11",), n=40)
        for detail in (False, True):
            text = item_text_p5(item, WINDOW, detail)
            self.assertIsNone(re.search(r"\b\d{1,2}:\d{2}\b", text))


class MergeModifyTextTest(unittest.TestCase):
    def test_merge(self):
        text = item_text_p5(merge_item(), WINDOW, False)
        self.assertEqual(text, "Extend Desk? Place B touches it (40 cm to the right) and is used at similar hours. "
                               "Approve to extend it, Rename to keep it as a new place, Reject to ignore it.")
        self.assertEqual(item_text_p5(merge_item(), WINDOW, True), text)             # detail adds nothing

    def test_modify(self):
        text = item_text_p5(modify_item(), WINDOW, False)
        self.assertEqual(text, "Has Desk moved? Its place changed by about 60 cm towards the radar. Approve to update "
                               "it, Rename to keep the old one and add the new place, Reject to leave it.")
        self.assertEqual(item_text_p5(modify_item(), WINDOW, True), text)


class TierOneTextTest(unittest.TestCase):
    def test_the_p4_texts_are_reused(self):
        from radar_zone_learner.shell.review import item_text
        self.assertEqual(item_text_p5(bed_item(), (True, 0, 360), False), item_text(bed_item(), True, 0, 360))
        self.assertIn("before 00:00 or after 06:00", item_text_p5(bed_item(), (True, 0, 360), False))
        self.assertNotIn("before", item_text_p5(bed_item(), (False, 0, 360), False))
        nw = item_text_p5(window_item(), WINDOW, False)
        self.assertIn("between 23:30 and 06:45", nw)                                    # P4 behaviour in both modes
        self.assertEqual(item_text_p5(window_item(), WINDOW, True), nw)

    def test_the_bed_text_has_no_night_learning(self):
        for detail in (False, True):
            self.assertNotIn("night learning", item_text_p5(bed_item(), WINDOW, detail).casefold())
            self.assertIn("press Mark spot here once", item_text_p5(bed_item(), WINDOW, detail))
            self.assertIn("(recommended; no extra radar-on nights)", item_text_p5(bed_item(), WINDOW, detail))

    def test_no_window_given_means_the_schedule_is_unknown(self):
        self.assertNotIn("before", item_text_p5(bed_item(), None, False))
        self.assertIn("before 00:00 or after 06:00", item_text_p5(bed_item(), (True, None, None), False))


class PushStateTest(unittest.TestCase):
    def test_the_examples_of_the_spec(self):
        self.assertEqual(push_state(PushRequest(0, 0, "none", 0, None, None, ()), None, False), "0|0|none|0||||")
        self.assertEqual(push_state(PushRequest(7, 6, "setup", 3, "s0005-1", 1, ()), None, False),
                         "7|6|setup|3|1|s0005-1||")
        self.assertEqual(push_state(PushRequest(7, 7, "setup", 3, "s0005-1", 1, ("pa1b2c3d",)), None, False),
                         "7|7|setup|3|1|s0005-1|pa1b2c3d|")
        self.assertEqual(push_state(PushRequest(7, 7, "setup", 3, "s0005-1", 1, ("pa1b2c3d", "pe5f6a7b")), None, False),
                         "7|7|setup|3|1|s0005-1|pa1b2c3d,pe5f6a7b|")

    def test_always_eight_fields(self):
        for req in (PushRequest(0, 0, "none", 0, None, None, ()), PushRequest(3, 3, "daily", 1, None, None, ("p1",)),
                    PushRequest(12, 11, "daily", 4, "mod-z0001", 0, ())):
            for text in (None, "", "hello", "a|b|c"):
                for detail in (False, True):
                    self.assertEqual(len(push_state(req, text, detail).split("|")), 8)

    def test_the_text_only_with_detail(self):
        req = PushRequest(7, 6, "setup", 3, "s0005-1", 1, ())
        self.assertTrue(push_state(req, "New place A: x", False).endswith("|"))
        self.assertEqual(push_state(req, "New place A: x", True), "7|6|setup|3|1|s0005-1||New place A: x")
        self.assertEqual(push_state(req, "", True), "7|6|setup|3|1|s0005-1||")
        self.assertEqual(push_state(req, None, True), "7|6|setup|3|1|s0005-1||")

    def test_pipes_in_the_text_become_slashes(self):
        req = PushRequest(7, 6, "setup", 3, "s0005-1", 1, ())
        state = push_state(req, "a|b||c", True)
        self.assertEqual(state.split("|")[7], "a/b//c")
        self.assertEqual(len(state.split("|")), 8)

    def test_cut_at_255_characters(self):
        req = PushRequest(7, 6, "setup", 3, "s0005-1", 1, ("pa1b2c3d", "pe5f6a7b"))
        state = push_state(req, "x" * 1000, True)
        self.assertEqual(len(state), 255)
        self.assertTrue(state.endswith("…"))
        self.assertEqual(state.split("|")[:7], ["7", "6", "setup", "3", "1", "s0005-1", "pa1b2c3d,pe5f6a7b"])
        self.assertEqual(len(push_state(req, "y" * 10, True)), len(push_state(req, None, False)) + 10)

    def test_newlines_are_folded(self):
        req = PushRequest(7, 6, "setup", 3, "s0005-1", 1, ())
        self.assertEqual(push_state(req, "a\nb  c", True).split("|")[7], "a b c")

    def test_the_request_has_no_text_field(self):
        self.assertNotIn("text", [f for f in PushRequest.__slots__])


class YouAreInTest(unittest.TestCase):
    def label_of(self, item_id):
        return {"s0005-1": "Place A", "z0001": "Desk", "l0001": ""}.get(item_id)

    def text(self, live=True, **kw):
        values = dict(observing=True, people=1, stay_ready=False, item=None, item_kind=None)
        values.update(kw)
        return you_are_in_text(live, WhereView(**values), self.label_of)

    def test_live_off_wins(self):
        self.assertEqual(self.text(live=False, people=1, item="s0005-1", item_kind="place"), "Live view off")

    def test_cases(self):
        self.assertEqual(self.text(observing=False), "Radar not observing")
        self.assertEqual(self.text(people=0), "No one in view")
        self.assertEqual(self.text(people=2, item="s0005-1", item_kind="place"), "Several people in view")
        self.assertEqual(self.text(item="s0005-1", item_kind="place"), "Place A")
        self.assertEqual(self.text(item="z0001", item_kind="zone"), "Desk")
        self.assertEqual(self.text(item="l0001", item_kind="label"), "Marked spot")           # a label with no name
        self.assertEqual(self.text(item=None), "In no known place")
        self.assertEqual(self.text(item="s9999-9", item_kind="place"), "In no known place")

    def test_never_the_payload_none(self):
        for kw in ({}, {"people": 0}, {"observing": False}, {"item": "z0001", "item_kind": "zone"}):
            self.assertNotEqual(self.text(**kw).casefold(), "none")

    def test_attributes_carry_counts_only(self):
        self.assertEqual(json.loads(you_are_in_attr(1, True)), {"people": 1, "stay_ready": True})
        self.assertEqual(you_are_in_attr(0, False), '{"people":0,"stay_ready":false}')


class AdvancedLabelTest(unittest.TestCase):
    def label(self, verb, kind, target, **kw):
        values = dict(name="", tag="", until="", rev=0)
        values.update(kw)
        return advanced_label(AdvancedEntry("adv:%s:%s" % (verb, target), verb, kind, target, **values))

    def test_labels(self):
        self.assertEqual(self.label("undo", "bed", "bed"), "Undo: No bed in view")
        self.assertEqual(self.label("undo", "night_window", "nw", until="2026-02-01"),
                         "Undo: Night window denied until 2026-02-01")
        self.assertEqual(self.label("undo", "place", "s0005-1", tag="A"), "Undo: Place A rejected")
        self.assertEqual(self.label("undo", "place", "s0005-1"), "Undo: Place ? rejected")
        self.assertEqual(self.label("real", "reflection", "s0004-2", tag="AB"), "Real place: Reflection AB")
        self.assertEqual(self.label("rename", "zone", "z0001", name="Desk"), "Rename zone: Desk")
        self.assertEqual(self.label("remove", "zone", "z0001", name="Desk"), "Remove zone: Desk")
        self.assertEqual(self.label("rename", "label", "l0001", name="Bed"), "Rename mark: Bed")
        self.assertEqual(self.label("remove", "label", "l0001", name="Bed"), "Remove mark: Bed")
        self.assertEqual(self.label("remove", "label", "l0001"), "Remove mark: Marked spot")

    def test_a_tag_lookup_for_an_entry_without_a_tag(self):
        e = AdvancedEntry("adv:undo:s0005-1", "undo", "place", "s0005-1", "", "", "", 0)
        self.assertEqual(advanced_label(e, lambda sid: "C"), "Undo: Place C rejected")

    def test_the_name_is_cut_at_24_characters(self):
        self.assertEqual(self.label("rename", "zone", "z0001", name="n" * 24), "Rename zone: " + "n" * 24)
        self.assertEqual(self.label("rename", "zone", "z0001", name="n" * 25), "Rename zone: " + "n" * 23 + "…")


class PendingJsonTest(unittest.TestCase):
    def rows(self, n, text="t"):
        return [{"id": "s%04d-1" % k, "rev": 1, "type": "dwell_zone", "tier": 2, "label": "Place %d" % k, "text": text}
                for k in range(n)]

    def test_shape_and_key_order(self):
        out = pending_json("s0000-1", self.rows(2), 3)
        obj = json.loads(out)
        self.assertEqual(list(obj), ["selected", "items", "more"])
        self.assertEqual(list(obj["items"][0]), ["id", "rev", "type", "tier", "label", "text"])
        self.assertEqual((obj["selected"], obj["more"]), ("s0000-1", 3))
        self.assertEqual(pending_json(None, [], 0), '{"selected":null,"items":[],"more":0}')

    def test_at_most_fifteen_items(self):
        self.assertEqual(len(json.loads(pending_json(None, self.rows(20), 0))["items"]), 15)

    def test_the_texts_are_cut_when_the_json_gets_too_long(self):
        # 15 texts of 255 characters that need 6 bytes each in ASCII-escaped JSON: cut to 120, then to 60
        out = pending_json(None, self.rows(15, "−" * 255), 0)
        self.assertLessEqual(len(out.encode("ascii")), 8000)
        texts = [i["text"] for i in json.loads(out)["items"]]
        self.assertTrue(all(len(t) <= 60 for t in texts))
        self.assertTrue(all(t.endswith("…") for t in texts))
        mid = pending_json(None, self.rows(15, "−" * 70), 0)           # 15 x 70 x 6 = 6.3 KB + framing: cut to 120 only
        self.assertLessEqual(len(mid.encode("ascii")), 8000)
        # texts below the limit are kept whole
        # a typical list is not cut
        small = pending_json(None, self.rows(5, "x" * 255), 0)
        self.assertEqual([len(i["text"]) for i in json.loads(small)["items"]], [255] * 5)

    def test_ascii_only(self):
        out = pending_json(None, self.rows(1, "Radar on-time −30%"), 0)
        self.assertTrue(out.isascii())
        self.assertEqual(json.loads(out)["items"][0]["text"], "Radar on-time −30%")


class StateTextTest(unittest.TestCase):
    def test_cut_at_255(self):
        self.assertEqual(len(state_text("z" * 400)), 255)


if __name__ == "__main__":
    unittest.main()
