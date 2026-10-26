import dataclasses
import unittest
from pathlib import Path

from radar_zone_learner.core.types import ContextState, ScheduleState
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.p4_types import (LuxSample, NightLearningSwitch, RadarObservingMsg, RadarStateMsg,
                                               ReviewAnswer, ReviewSelect)
from radar_zone_learner.shell.topics import ClearRetained, TopicMapper, build_topics
from radar_zone_learner.shell.topics_p4 import TopicMapperP4, build_topics_p4, parse_answer, parse_lux


def settings(**changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                  debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                  mqtt_password=None, mqtt_ssl=False, data_dir=Path("."))
    values.update(changes)
    return Settings(**values)


class TopicsP4Test(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.t = build_topics_p4(self.s)
        self.m = self.t.make_mapper()

    def test_p3_topics_and_mapper_unchanged(self):
        base = build_topics(self.s)
        self.assertEqual(len(base.subscriptions()), 9)
        self.assertIs(type(base.make_mapper()), TopicMapper)
        for f in dataclasses.fields(base):
            self.assertEqual(getattr(self.t, f.name), getattr(base, f.name), f.name)

    def test_subscriptions_extend_p3(self):
        base = build_topics(self.s).subscriptions()
        subs = self.t.subscriptions()
        self.assertEqual(subs[:9], base)
        self.assertEqual(len(subs), 18)
        self.assertEqual(len({topic for topic, _ in subs}), 18)
        topics = {topic for topic, _ in subs}
        for topic in ("led_matrix_node/sensors/state", "led_matrix_node/radar/state",
                      "led_matrix_node/radar/observing", "radar_zones/context/lights_off",
                      "radar_zones/context/sleep_hint", "radar_zones/context/guests",
                      "radar_zones/learner/night_learning/set", "radar_zones/review/select/set",
                      "radar_zones/review/answer"):
            self.assertIn(topic, topics)
        self.assertEqual(dict(subs)[self.t.frames], 0)
        self.assertEqual(self.t.radar_schedule_cmd, "led_matrix_node/radar/schedule")
        self.assertEqual(self.t.radar_valid, "radar_zones/radar/valid")

    def test_node_prefix_applies_to_node_topics_only(self):
        t = build_topics_p4(settings(node_prefix="node_b"))
        self.assertEqual(t.sensors_state, "node_b/sensors/state")
        self.assertEqual(t.radar_night_start_cmd, "node_b/radar/night_start")
        self.assertEqual(t.review_answer, "radar_zones/review/answer")
        self.assertEqual(t.coverage_7d, "radar_zones/learner/coverage_7d")

    def test_p3_inputs_still_mapped(self):
        ev = self.m.map(self.t.schedule_state, b"1", True, 5)
        self.assertEqual(ev, [ScheduleState(True, None, None, 5)])
        self.assertEqual(self.m.map(self.t.context_home_empty, b"on", True, 6), [ContextState("home_empty", True, 6)])
        self.assertEqual(self.m.map("led_matrix_node/other", b"x", False, 1), [])
        self.assertEqual(self.m.ignored, 1)

    # ------------------------------------------------------------ lux
    def test_lux(self):
        out = self.m.map(self.t.sensors_state, b'{"co2":612,"temp":21.5,"lux":3.25}', False, 7)
        self.assertEqual(out, [LuxSample(3.25, 7)])
        self.assertEqual(self.m.map(self.t.sensors_state, b'{"lux":0}', False, 8), [LuxSample(0.0, 8)])

    def test_no_lux_field_is_not_an_error(self):
        self.assertEqual(self.m.map(self.t.sensors_state, b'{"co2":612}', False, 7), [])
        self.assertEqual(self.m.map(self.t.sensors_state, b"{}", False, 7), [])
        self.assertEqual(self.m.ignored, 0)

    def test_bad_lux_is_ignored(self):
        for payload in (b"not json", b"[1]", b'{"lux":"12"}', b'{"lux":true}', b'{"lux":-1}', b'{"lux":1e9}',
                        b'{"lux":NaN}', b'{"lux":null}', b"\xff\xfe"):
            with self.subTest(payload=payload):
                before = self.m.ignored
                self.assertEqual(self.m.map(self.t.sensors_state, payload, False, 1), [])
                self.assertEqual(self.m.ignored, before + 1)

    def test_parse_lux_values(self):
        self.assertEqual(parse_lux(b'{"lux":12}'), 12.0)
        self.assertIsNone(parse_lux(b'{"temp":1}'))
        self.assertIs(parse_lux(b"x"), False)

    # ------------------------------------------------------------ radar topics and contexts
    def test_radar_state_and_observing(self):
        self.assertEqual(self.m.map(self.t.radar_state, b"night_off", True, 3), [RadarStateMsg("night_off", 3)])
        self.assertEqual(self.m.map(self.t.radar_state, b" FAILSAFE_ON ", True, 3), [RadarStateMsg("failsafe_on", 3)])
        self.assertEqual(self.m.map(self.t.radar_state, b"maybe", True, 3), [])
        self.assertEqual(self.m.map(self.t.radar_observing, b"ON", True, 4), [RadarObservingMsg(True, 4)])
        self.assertEqual(self.m.map(self.t.radar_observing, b"off", True, 4), [RadarObservingMsg(False, 4)])
        self.assertEqual(self.m.map(self.t.radar_observing, b"", True, 4), [])
        self.assertEqual(self.m.ignored, 2)

    def test_contexts(self):
        for name, topic in (("lights_off", self.t.context_lights_off), ("sleep_hint", self.t.context_sleep_hint),
                            ("guests", self.t.context_guests)):
            with self.subTest(name=name):
                self.assertEqual(self.m.map(topic, b"on", True, 1), [ContextState(name, True, 1)])
                self.assertEqual(self.m.map(topic, b"OFF", True, 2), [ContextState(name, False, 2)])
                self.assertEqual(self.m.map(topic, b"", True, 3), [ContextState(name, None, 3)])   # cleared
                self.assertEqual(self.m.map(topic, b"maybe", True, 4), [ContextState(name, None, 4)])

    # ------------------------------------------------------------ commands from the user
    def test_night_learning_switch(self):
        t = self.t.night_learning_set
        self.assertEqual(self.m.map(t, b"ON", False, 1), [NightLearningSwitch(True, 1)])
        self.assertEqual(self.m.map(t, b"off", False, 2), [NightLearningSwitch(False, 2)])
        self.assertEqual(self.m.map(t, b"toggle", False, 3), [])
        self.assertEqual(self.m.ignored, 1)

    def test_select(self):
        t = self.t.review_select_set
        self.assertEqual(self.m.map(t, b"Bed zone", False, 1), [ReviewSelect("bed_needs_data", 1)])
        self.assertEqual(self.m.map(t, b"night window", False, 2), [ReviewSelect("night_window", 2)])
        self.assertEqual(self.m.map(t, b"night_window", False, 2), [ReviewSelect("night_window", 2)])
        self.assertEqual(self.m.map(t, b"None", False, 3), [ReviewSelect(None, 3)])
        self.assertEqual(self.m.map(t, b"Desk", False, 4), [])
        self.assertEqual(self.m.ignored, 1)

    def test_answer(self):
        t = self.t.review_answer
        self.assertEqual(self.m.map(t, b'{"op":"approve"}', False, 1),
                         [ReviewAnswer(None, "approve", None, False, 1)])
        self.assertEqual(self.m.map(t, b'{"op":"RZ_REJECT","item":"bed_needs_data","rev":3}', False, 2),
                         [ReviewAnswer("bed_needs_data", "reject", 3, False, 2)])
        self.assertEqual(self.m.map(t, b'{"op":"approve","item":"night_window","sched":true}', False, 3),
                         [ReviewAnswer("night_window", "approve", None, True, 3)])
        for bad in (b"approve", b"[]", b'{"op":"delete"}', b'{"op":1}', b'{"op":"later","item":"desk"}',
                    b'{"op":"later","rev":"1"}', b'{"op":"later","rev":true}', b'{"op":"approve","sched":1}'):
            with self.subTest(bad=bad):
                self.assertEqual(self.m.map(t, bad, False, 9), [])
        self.assertIsNone(parse_answer(b"\xff"))

    def test_retained_commands_are_cleared(self):
        for topic in (self.t.night_learning_set, self.t.review_select_set, self.t.review_answer):
            with self.subTest(topic=topic):
                self.assertEqual(self.m.map(topic, b'{"op":"approve"}', True, 1), [ClearRetained(topic)])
                self.assertEqual(self.m.map(topic, b"", True, 1), [])        # already a deletion
        # the echo of the app's own clear (empty, not retained) is no command
        self.m.note_cleared(self.t.review_answer)
        self.assertEqual(self.m.map(self.t.review_answer, b"", False, 2), [])
        self.assertEqual(self.m.ignored, 0)

    def test_oversize_dropped_before_decoding(self):
        big = b"x" * 300
        for topic in (self.t.sensors_state, self.t.review_answer, self.t.radar_state):
            self.m.map(topic, big, False, 1)
        self.assertEqual(self.m.oversize, 3)
        # a retained oversize command is still cleared
        self.assertEqual(self.m.map(self.t.review_answer, big, True, 1), [ClearRetained(self.t.review_answer)])
        self.assertEqual(self.m.oversize, 3)

    def test_mapper_class(self):
        self.assertIsInstance(self.m, TopicMapperP4)


if __name__ == "__main__":
    unittest.main()
