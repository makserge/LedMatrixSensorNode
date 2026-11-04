"""P5 topics and the mapper (spec 8.4, 3.1, 6.4): subscriptions, command topics, every input, the answer parser."""
import dataclasses
import json
import unittest
from pathlib import Path

from radar_zone_learner.core.types import ContextState, ScheduleState
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.p4_types import (LuxSample, NightLearningSwitch, RadarObservingMsg, RadarStateMsg,
                                               ReviewAnswer, ReviewSelect)
from radar_zone_learner.shell.p5_types import (AdvancedSelectLabel, CaptureSwitch, ForgetConfirmText, ForgetPress,
                                               LiveSwitch, NightTimeEdit, PushAck, ReviewSelectLabel, SettingSwitch)
from radar_zone_learner.shell.topics import ClearRetained, TopicMapper
from radar_zone_learner.shell.topics_p4 import TopicMapperP4, TopicsP4, build_topics_p4
from radar_zone_learner.shell.topics_p5 import (TopicMapperP5, build_topics_p5, parse_answer_p5, parse_forget_confirm,
                                                parse_push_ack, parse_select_p5)

INPUTS = ["radar_zones/review/advanced/set", "radar_zones/review/live/set", "radar_zones/review/nw_start/set",
          "radar_zones/review/nw_end/set", "radar_zones/review/approve_sched/set", "radar_zones/review/push_ack",
          "radar_zones/learner/guests_pause/set", "radar_zones/learner/pets/set",
          "radar_zones/learner/detail_text/set", "radar_zones/learner/debug_capture/set",
          "radar_zones/learner/forget_confirm/set", "radar_zones/learner/forget_all"]


def settings(**changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                  debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                  mqtt_password=None, mqtt_ssl=False, data_dir=Path("."))
    values.update(changes)
    return Settings(**values)


class TopicsP5Test(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.t = build_topics_p5(self.s)
        self.m = self.t.make_mapper()

    def test_subscriptions_extend_p4(self):
        base = build_topics_p4(self.s).subscriptions()
        subs = self.t.subscriptions()
        self.assertEqual(subs[:18], base)
        self.assertEqual(len(subs), 30)
        self.assertEqual(len({topic for topic, _ in subs}), 30)
        self.assertEqual([topic for topic, _ in subs[18:]], INPUTS)
        self.assertTrue(all(qos == 1 for _, qos in subs[18:]))
        self.assertEqual(dict(subs)[self.t.frames], 0)

    def test_command_topics(self):
        cmds = self.t.command_topics()
        self.assertEqual(len(cmds), 17)
        self.assertEqual(set(cmds), set(build_topics_p4(self.s).command_topics()) | set(INPUTS))
        for topic in cmds:
            self.assertIn(topic, {t for t, _ in self.t.subscriptions()})

    def test_p4_classes_are_unchanged(self):
        t4 = build_topics_p4(self.s)
        self.assertEqual(len(t4.subscriptions()), 18)
        self.assertEqual(len(t4.command_topics()), 5)
        self.assertIs(type(t4.make_mapper()), TopicMapperP4)
        for f in dataclasses.fields(t4):
            self.assertEqual(getattr(self.t, f.name), getattr(t4, f.name), f.name)
        self.assertIsInstance(self.t, TopicsP4)
        self.assertIs(type(self.m), TopicMapperP5)

    def test_output_topics(self):
        t = self.t
        self.assertEqual(t.review_map, "radar_zones/review/map")
        self.assertEqual(t.review_push_map, "radar_zones/review/push_map")
        self.assertEqual(t.review_advanced_state, "radar_zones/review/advanced")
        self.assertEqual(t.review_live_state, "radar_zones/review/live")
        self.assertEqual(t.review_you_are_in, "radar_zones/review/you_are_in")
        self.assertEqual(t.review_you_are_in_attr, "radar_zones/review/you_are_in/attr")
        self.assertEqual((t.review_nw_start, t.review_nw_end),
                         ("radar_zones/review/nw_start", "radar_zones/review/nw_end"))
        self.assertEqual(t.review_approve_sched, "radar_zones/review/approve_sched")
        self.assertEqual(t.review_push, "radar_zones/review/push")
        self.assertEqual(t.learner_forget_confirm, "radar_zones/learner/forget_confirm")
        self.assertEqual(t.learner_debug_capture, "radar_zones/learner/debug_capture")

    def test_node_prefix_applies_to_node_topics_only(self):
        t = build_topics_p5(settings(node_prefix="node_b"))
        self.assertEqual(t.frames, "node_b/radar/ld2454/frames")
        self.assertEqual(t.review_push, "radar_zones/review/push")

    # ------------------------------------------------------------ the old inputs still work
    def test_p3_and_p4_inputs_still_mapped(self):
        self.assertEqual(self.m.map(self.t.schedule_state, b"1", True, 5), [ScheduleState(True, None, None, 5)])
        self.assertEqual(self.m.map(self.t.context_home_empty, b"on", True, 6), [ContextState("home_empty", True, 6)])
        self.assertEqual(self.m.map(self.t.sensors_state, b'{"lux":3.5}', False, 7), [LuxSample(3.5, 7)])
        self.assertEqual(self.m.map(self.t.radar_state, b"night_off", True, 3), [RadarStateMsg("night_off", 3)])
        self.assertEqual(self.m.map(self.t.radar_observing, b"ON", True, 4), [RadarObservingMsg(True, 4)])
        self.assertEqual(self.m.map(self.t.night_learning_set, b"ON", False, 1), [NightLearningSwitch(True, 1)])
        self.assertEqual(self.m.map("led_matrix_node/other", b"x", False, 1), [])
        self.assertEqual(self.m.ignored, 1)

    # ------------------------------------------------------------ the select
    def test_select_static_labels_and_ids(self):
        t = self.t.review_select_set
        self.assertEqual(self.m.map(t, b"Bed zone", False, 1), [ReviewSelect("bed_needs_data", 1)])
        self.assertEqual(self.m.map(t, b"night window", False, 2), [ReviewSelect("night_window", 2)])
        self.assertEqual(self.m.map(t, b"night_window", False, 2), [ReviewSelect("night_window", 2)])
        self.assertEqual(self.m.map(t, b"No item", False, 3), [ReviewSelect(None, 3)])
        self.assertEqual(self.m.map(t, b"None", False, 3), [ReviewSelect(None, 3)])         # P4 automations
        self.assertEqual(self.m.map(t, b"", False, 3), [ReviewSelect(None, 3)])
        self.assertEqual(self.m.map(t, b"s0005-1", False, 4), [ReviewSelect("s0005-1", 4)])
        self.assertEqual(self.m.map(t, b"mod-z0001", False, 4), [ReviewSelect("mod-z0001", 4)])

    def test_select_other_text_is_a_label_for_the_layer(self):
        t = self.t.review_select_set
        self.assertEqual(self.m.map(t, b"Place A", False, 1), [ReviewSelectLabel("Place A", 1)])
        self.assertEqual(self.m.map(t, b"  Extend B ", False, 1), [ReviewSelectLabel("Extend B", 1)])
        self.assertEqual(self.m.map(t, b"x" * 64, False, 1), [ReviewSelectLabel("x" * 64, 1)])
        self.assertEqual(self.m.map(t, b"x" * 65, False, 1), [])
        self.assertEqual(self.m.map(t, b"\xff\xfe", False, 1), [])
        self.assertEqual(self.m.ignored, 2)
        self.assertEqual(parse_select_p5(b"Moved C"), ("label", "Moved C"))

    def test_advanced_select(self):
        t = self.t.review_advanced_set
        self.assertEqual(self.m.map(t, b"Undo: No bed in view", False, 1),
                         [AdvancedSelectLabel("Undo: No bed in view", 1)])
        self.assertEqual(self.m.map(t, b"No entry", False, 2), [AdvancedSelectLabel("No entry", 2)])
        self.assertEqual(self.m.map(t, b"", False, 3), [])
        self.assertEqual(self.m.map(t, b"x" * 65, False, 3), [])
        self.assertEqual(self.m.ignored, 2)

    # ------------------------------------------------------------ switches, times, ack, forget
    def test_live_and_capture_switches(self):
        for topic, cls in ((self.t.review_live_set, LiveSwitch), (self.t.learner_debug_capture_set, CaptureSwitch)):
            with self.subTest(topic=topic):
                self.assertEqual(self.m.map(topic, b"ON", False, 1), [cls(True, 1)])
                self.assertEqual(self.m.map(topic, b"off", False, 2), [cls(False, 2)])
                self.assertEqual(self.m.map(topic, b"1", False, 3), [cls(True, 3)])
                self.assertEqual(self.m.map(topic, b"toggle", False, 4), [])

    def test_the_four_settings(self):
        for name, topic in (("approve_sched", self.t.review_approve_sched_set),
                            ("guests_pause", self.t.learner_guests_pause_set), ("pets", self.t.learner_pets_set),
                            ("detail_text", self.t.learner_detail_text_set)):
            with self.subTest(name=name):
                self.assertEqual(self.m.map(topic, b"ON", False, 1), [SettingSwitch(name, True, 1)])
                self.assertEqual(self.m.map(topic, b"OFF", False, 2), [SettingSwitch(name, False, 2)])
                self.assertEqual(self.m.map(topic, b"maybe", False, 3), [])

    def test_time_edits(self):
        self.assertEqual(self.m.map(self.t.review_nw_start_set, b"23:45:00", False, 1), [NightTimeEdit("start", 1425, 1)])
        self.assertEqual(self.m.map(self.t.review_nw_end_set, b"06:15", False, 2), [NightTimeEdit("end", 375, 2)])
        self.assertEqual(self.m.map(self.t.review_nw_end_set, b"24:00:00", False, 3), [])
        self.assertEqual(self.m.map(self.t.review_nw_end_set, b"soon", False, 3), [])

    def test_push_ack(self):
        t = self.t.review_push_ack
        self.assertEqual(self.m.map(t, b"7:pa1b2c3d", False, 1), [PushAck(7, 1, "pa1b2c3d")])
        self.assertEqual(self.m.map(t, b"7", False, 2), [PushAck(7, 2, "")])                # a plain <seq>
        self.assertEqual(self.m.map(t, b"1234567890:p1", False, 3), [PushAck(1234567890, 3, "p1")])
        for bad in (b"12345678901:p1", b"7:", b"7:p1 2", b"7:p_1", b"x:p1", b"-1", b"7:" + b"a" * 13, b"", b"\xff"):
            with self.subTest(bad=bad):
                self.assertEqual(self.m.map(t, bad, False, 9), [])
        self.assertEqual(parse_push_ack(b"7:AbC123"), (7, "AbC123"))

    def test_forget_confirm_text_and_press(self):
        t = self.t.learner_forget_confirm_set
        self.assertEqual(self.m.map(t, b"FORGET", False, 1), [ForgetConfirmText("FORGET", 1)])
        self.assertEqual(self.m.map(t, b"", False, 2), [ForgetConfirmText("", 2)])           # the user cleared it
        self.assertEqual(self.m.map(t, b"x" * 40, False, 3), [ForgetConfirmText("x" * 16, 3)])   # cut to 16 characters
        self.assertEqual(self.m.map(t, " forget ".encode(), False, 4), [ForgetConfirmText(" forget ", 4)])   # as typed
        self.assertEqual(self.m.map(t, b"\xff", False, 5), [])
        self.assertIsNone(parse_forget_confirm(b"\xff"))
        press = self.t.learner_forget_all
        self.assertEqual(self.m.map(press, b"PRESS", False, 6), [ForgetPress(6)])
        self.assertEqual(self.m.map(press, b"press", False, 7), [ForgetPress(7)])
        self.assertEqual(self.m.map(press, b"FORGET", False, 8), [])

    # ------------------------------------------------------------ retained commands and sizes
    def test_a_retained_message_on_every_command_topic_is_cleared(self):
        for topic in self.t.command_topics():
            with self.subTest(topic=topic):
                self.assertEqual(self.m.map(topic, b'{"op":"approve"}', True, 1), [ClearRetained(topic)])
                self.assertEqual(self.m.map(topic, b"", True, 1), [])                        # already a deletion
        self.assertEqual(self.m.ignored, 0)

    def test_the_echo_of_the_apps_own_clear_is_no_command(self):
        self.m.note_cleared(self.t.learner_forget_all)
        self.assertEqual(self.m.map(self.t.learner_forget_all, b"", False, 2), [])
        self.assertEqual(self.m.ignored, 0)

    def test_size_caps(self):
        base = {"op": "approve", "name": "x" * 20}
        big = json.dumps(dict(base, aid="a" * 64, item="s0005-1")).encode()
        self.assertLess(len(big), 256)
        pad = json.dumps({"op": "rename", "name": "ä" * 32, "aid": "a" * 64, "item": "s0005-1"}, ).encode()
        self.assertGreater(len(pad), 256)                       # escaped non-ASCII characters: the reason for 512
        self.assertLessEqual(len(pad), 512)
        self.assertEqual(len(self.m.map(self.t.review_answer, pad, False, 1)), 1)
        self.assertEqual(self.m.oversize, 0)
        self.assertEqual(self.m.map(self.t.review_answer, b" " * 513, False, 1), [])
        self.assertEqual(self.m.oversize, 1)
        # every other command topic keeps 256
        for topic in (self.t.review_live_set, self.t.review_push_ack, self.t.learner_forget_confirm_set,
                      self.t.review_select_set, self.t.sensors_state):
            self.m.map(topic, b"x" * 257, False, 1)
        self.assertEqual(self.m.oversize, 6)
        # a retained oversize command is still cleared
        self.assertEqual(self.m.map(self.t.review_answer, b"x" * 600, True, 1), [ClearRetained(self.t.review_answer)])
        self.assertEqual(self.m.map(self.t.review_live_set, b"x" * 600, True, 1), [ClearRetained(self.t.review_live_set)])
        self.assertEqual(self.m.oversize, 6)


class ParseAnswerTest(unittest.TestCase):
    def setUp(self):
        self.t = build_topics_p5(settings())
        self.m = self.t.make_mapper()

    def parse(self, obj):
        return parse_answer_p5(json.dumps(obj).encode())

    def test_minimal_and_full(self):
        self.assertEqual(self.parse({"op": "approve"}),
                         {"op": "approve", "item": None, "rev": None, "sched": False, "name": None, "aid": None})
        full = {"op": "RZ_RENAME", "item": "s0005-1", "rev": 3, "sched": True, "name": " Desk ", "aid": "01HV.x_y-9"}
        self.assertEqual(self.parse(full), {"op": "rename", "item": "s0005-1", "rev": 3, "sched": True,
                                            "name": "Desk", "aid": "01HV.x_y-9"})

    def test_every_op(self):
        for op in ("approve", "reject", "later", "rename", "undo", "forget", "advanced"):
            self.assertEqual(self.parse({"op": op})["op"], op)
            self.assertEqual(self.parse({"op": " RZ_" + op.upper() + " "})["op"], op)
        for bad in ("delete", "", "rz_", 1, None, ["approve"]):
            self.assertIsNone(self.parse({"op": bad}))

    def test_item_grammar(self):
        for good in ("night_window", "bed_needs_data", "s0005-1", "s12345-17", "mrg-s0005-1", "mod-z0001",
                     "adv:undo:bed", "adv:undo:nw", "adv:real:s0005-1", "adv:rename:z0001", "adv:remove:l0002",
                     "adv:undo:" + "a" * 24):
            with self.subTest(good=good):
                self.assertEqual(self.parse({"op": "advanced", "item": good})["item"], good)
        for bad in ("desk", "", "s5-1", "mrg-z0001", "mod-s0005-1", "adv:undo:", "adv:wipe:bed",
                    "adv:undo:" + "a" * 25, "z0001", "l0001", 5, ["s0005-1"], "s0005-1 "):
            with self.subTest(bad=bad):
                self.assertIsNone(self.parse({"op": "approve", "item": bad}))

    def test_rev_and_sched(self):
        self.assertEqual(self.parse({"op": "later", "rev": 0})["rev"], 0)
        for bad in ("1", True, 1.5, [1]):
            self.assertIsNone(self.parse({"op": "later", "rev": bad}))
        for bad in (1, "true", None, 0):
            self.assertIsNone(self.parse({"op": "approve", "sched": bad}))

    def test_name_rules(self):
        self.assertEqual(self.parse({"op": "rename", "name": "x" * 40})["name"], "x" * 32)       # cut
        self.assertEqual(self.parse({"op": "rename", "name": "Büro"})["name"], "Büro")
        # a template in Home Assistant turns a typed 2 into a number: converted with str()
        self.assertEqual(self.parse({"op": "rename", "name": 2})["name"], "2")
        self.assertEqual(self.parse({"op": "rename", "name": 15.5})["name"], "15.5")
        self.assertEqual(self.parse({"op": "rename", "name": True})["name"], "True")
        for bad in ("", "   ", "a\nb", "a\x01b", ["x"], {"x": 1}):
            with self.subTest(bad=bad):
                self.assertIsNone(self.parse({"op": "rename", "name": bad}))
        self.assertEqual(self.parse({"op": "rename", "name": None})["name"], None)

    def test_aid(self):
        self.assertEqual(self.parse({"op": "approve", "aid": "a" * 64})["aid"], "a" * 64)
        for bad in ("a" * 65, "", "a b", "a/b", 5, ["x"]):
            self.assertIsNone(self.parse({"op": "approve", "aid": bad}))

    def test_unknown_keys_are_ignored_and_junk_is_refused(self):
        self.assertEqual(self.parse({"op": "approve", "color": "red"})["op"], "approve")
        for payload in (b"approve", b"[]", b"\xff", b"", b"null", b"1"):
            self.assertIsNone(parse_answer_p5(payload))

    def test_the_mapper_builds_the_core_event(self):
        t = self.t.review_answer
        self.assertEqual(self.m.map(t, b'{"op":"rename","item":"s0005-1","rev":2,"name":"Desk","aid":"x1"}', False, 7),
                         [ReviewAnswer("s0005-1", "rename", 2, False, 7, name="Desk", aid="x1")])
        self.assertEqual(self.m.map(t, b'{"op":"approve"}', False, 8), [ReviewAnswer(None, "approve", None, False, 8)])
        self.assertEqual(self.m.map(t, b'{"op":"undo","item":"night_window"}', False, 9),
                         [ReviewAnswer("night_window", "undo", None, False, 9)])
        self.assertEqual(self.m.map(t, b'{"op":"nope"}', False, 9), [])
        self.assertEqual(self.m.ignored, 1)

    def test_the_p4_mapper_keeps_its_old_parser(self):
        m4 = build_topics_p4(settings()).make_mapper()
        self.assertEqual(m4.map(self.t.review_answer, b'{"op":"later","item":"s0005-1"}', False, 1), [])
        self.assertIs(m4.__class__, TopicMapperP4)
        self.assertIsInstance(self.m, TopicMapper)


if __name__ == "__main__":
    unittest.main()
