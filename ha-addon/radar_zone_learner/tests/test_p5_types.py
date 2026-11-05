"""P5 core interface: the types, constants, parameters and the output order (spec Appendix A, §11.3 `test_p5_types`)."""
import dataclasses
import re
import unittest

from radar_zone_learner.core import types as T
from radar_zone_learner.core import learner as LM
from radar_zone_learner.core.params import DEFAULT_P5, DEFAULT_PARAMS, P5Params, params_digest

# the digest of the committed Params (P4 and P5 share it: D4 says Params does not change)
P4_PARAMS_DIGEST = "dbe9b3e9fea0"


class TypesTest(unittest.TestCase):
    def test_review_answer_keeps_its_five_positional_fields(self):
        a = T.ReviewAnswer("s0005-1", "approve", 2, False, 99)
        self.assertEqual((a.item, a.op, a.rev, a.sched, a.recv_ms), ("s0005-1", "approve", 2, False, 99))
        self.assertEqual((a.name, a.aid), (None, None))
        b = T.ReviewAnswer(None, "rename", None, False, 1, name="Desk", aid="01HV")
        self.assertEqual((b.name, b.aid), ("Desk", "01HV"))

    def test_review_item_tier_defaults_to_one(self):
        i = T.ReviewItem("night_window", 1, "night_window", {"a": 1})
        self.assertEqual(i.tier, 1)
        self.assertEqual(T.ReviewItem("s0005-1", 1, "dwell_zone", {}, 2).tier, 2)

    def test_push_ack_and_request(self):
        self.assertEqual(T.PushAck(3, 10).target, "")
        self.assertEqual(T.PushAck(3, 10, "pa1b2c3d").target, "pa1b2c3d")
        r = T.PushRequest(1, 0, "setup", 2, "s0005-1", 1)
        self.assertEqual(r.acks, ())
        self.assertEqual(T.PushRequest(0, 0, "none", 0, None, None, ()), T.PushRequest(0, 0, "none", 0, None, None))

    def test_new_inputs(self):
        self.assertEqual(T.NightTimeEdit("start", 1380, 5).minute, 1380)
        self.assertEqual(T.SettingSwitch("pets", True, 5).on, True)
        for cls in (T.NightTimeEdit, T.SettingSwitch, T.PushAck):
            self.assertIn(cls, T.InputEvent.__args__)

    def test_debug_map_data_without_the_new_fields(self):
        d = T.DebugMapData(70, 40, 0.2, -7.0, 0.0, (0.0,) * 2800, frozenset(), (), (), (), (), 60.0)
        self.assertEqual((d.zones, d.others, d.highlight, d.focus, d.live), ((), (), frozenset(), None, ()))

    def test_constants(self):
        self.assertEqual(T.ANSWER_OPS, ("approve", "reject", "later", "rename", "undo", "forget", "advanced"))
        self.assertEqual(T.ANSWER_IGNORED_REASONS, ("no_item", "no_such_item", "stale", "not_pending", "not_applicable",
                                                    "name_needed", "zone_limit", "times_invalid", "zone_busy"))
        self.assertEqual(T.PUSH_KINDS, ("none", "setup", "daily"))
        self.assertEqual(T.SETTING_NAMES, ("approve_sched", "guests_pause", "pets", "detail_text"))
        self.assertEqual(T.ZONE_SRC, ("approve", "mark"))
        self.assertIn("pet", T.KINDS)
        self.assertEqual(len(T.SUGGESTION_TYPES), 6)                      # unchanged on purpose
        self.assertEqual(T.ITEM_IDS, ("night_window", "bed_needs_data"))
        self.assertEqual(T.MARK_FAIL_REASONS, ("not_observing", "no_stay", "several_stays", "too_soon"))
        self.assertEqual(T.NODE_COMMAND_KINDS, ("schedule", "window"))
        self.assertEqual((T.SCHEMA_VERSION, T.MIN_SCHEMA_VERSION, T.APP_VERSION), (4, 3, "0.7.0"))

    def test_id_grammars(self):
        item = re.compile(T.ITEM_ID_RE)
        for ok in ("night_window", "bed_needs_data", "s0005-1", "s12345-22", "mrg-s0005-1", "mod-z0001", "ret-z0001",
                   "radar_moved"):
            self.assertTrue(item.match(ok), ok)
        for bad in ("night", "s5-1", "mrg-z0001", "mod-s0005-1", "s0005-1|x", "adv:undo:bed", "", "z0001", "ret-s0005-1",
                    "ret-z1", "radar_moved2", "radar_moved|x"):
            self.assertFalse(item.match(bad), bad)
        adv = re.compile(T.ADV_ID_RE)
        for ok in ("adv:undo:bed", "adv:undo:nw", "adv:undo:s0005-1", "adv:real:s0004-2", "adv:rename:z0001",
                   "adv:remove:l0001"):
            self.assertTrue(adv.match(ok), ok)
        for bad in ("adv:forget:z0001", "adv:undo:", "adv:undo:a b", "undo:bed", "adv:undo:" + "x" * 25):
            self.assertFalse(adv.match(bad), bad)
        tgt = re.compile(T.PUSH_TARGET_RE)
        self.assertTrue(tgt.match("pa1b2c3d") and tgt.match("p"))
        for bad in ("", "p" * 13, "a-b", "a b"):
            self.assertFalse(tgt.match(bad), bad)

    def test_every_new_output_class_is_ordered(self):
        for cls in (T.AnswerIgnored, T.AdvancedChanged, T.PushRequest):
            self.assertIn(cls, LM._OUTPUT_ORDER)
            self.assertIn(cls, T.Output.__args__)
        order = list(LM._OUTPUT_ORDER)
        p4 = [T.NodeCommand, T.ReviewChanged, T.StatusChanged, T.SaveRequested]    # the P4 order of test_review.py
        self.assertEqual(sorted(order.index(c) for c in p4), [order.index(c) for c in p4])
        self.assertLess(order.index(T.AnswerIgnored), order.index(T.ReviewChanged))
        self.assertLess(order.index(T.ReviewChanged), order.index(T.AdvancedChanged))
        self.assertLess(order.index(T.AdvancedChanged), order.index(T.PushRequest))
        self.assertLess(order.index(T.PushRequest), order.index(T.StatusChanged))
        self.assertEqual(LM._REASON_PRIORITY["push"], 3)

    def test_new_classes_are_frozen_and_slotted(self):
        for obj in (T.AnswerIgnored("a", "approve", "stale"), T.AdvancedChanged(()), T.PushRequest(0, 0, "none", 0,
                                                                                                    None, None),
                    T.NightTimeEdit("start", 0, 1), T.SettingSwitch("pets", True, 1), T.PushAck(1, 1),
                    T.WhereView(True, 0, False, None, None)):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(obj, dataclasses.fields(obj)[0].name, 1)
            self.assertFalse(hasattr(obj, "__dict__"))


class ParamsTest(unittest.TestCase):
    def test_p5params_is_frozen_and_separate(self):
        self.assertEqual(DEFAULT_P5, P5Params())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            DEFAULT_P5.review_max_places = 3
        self.assertFalse(DEFAULT_P5.zone_change_items)                      # merge and modify stay off until P7
        self.assertEqual((DEFAULT_P5.review_max_places, DEFAULT_P5.later_max, DEFAULT_P5.tags_max,
                          DEFAULT_P5.zones_also_max), (12, 400, 400, 8))
        self.assertEqual((DEFAULT_P5.push_after_min, DEFAULT_P5.push_before_min, DEFAULT_P5.push_min_items),
                         (480, 1260, 2))
        self.assertEqual((DEFAULT_P5.reject_reask_factor, DEFAULT_P5.reject_reask_nights), (2.0, 30))
        self.assertEqual((DEFAULT_P5.pet_min_days, DEFAULT_P5.pet_he_share), (4, 0.5))
        self.assertEqual(len(dataclasses.fields(P5Params)), 21)

    def test_params_and_its_digest_are_not_touched(self):
        self.assertEqual(params_digest(DEFAULT_PARAMS), P4_PARAMS_DIGEST)
        self.assertNotIn("zone_change_items", repr(DEFAULT_PARAMS))


if __name__ == "__main__":
    unittest.main()
