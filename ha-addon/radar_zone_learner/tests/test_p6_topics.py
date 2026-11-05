"""P6 topics and the mapper (P6 spec 8.2, Appendix B.1, check P6-12): the 34 subscriptions, the four command patterns, the
builders, every zone command, the drops and their counters, a retained message on a command topic."""
import dataclasses
import json
import unittest
from pathlib import Path

from radar_zone_learner.core.types import ContextState, ScheduleState
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.p4_types import RadarStateMsg, ReviewAnswer
from radar_zone_learner.shell.p6_types import VariantRequest, ZoneForget, ZoneTune
from radar_zone_learner.shell.topics import ClearRetained
from radar_zone_learner.shell.topics_p5 import TopicMapperP5, TopicsP5, build_topics_p5
from radar_zone_learner.shell.topics_p6 import (TopicMapperP6, build_topics_p6, classify_zone_topic, parse_on_off,
                                                parse_press, parse_zone_number)
from tests.fakes import FakeClientFactory, quiet_logs

PATTERNS = ["radar_zones/zone/+/clear_delay/set", "radar_zones/zone/+/margin/set", "radar_zones/zone/+/forget",
            "radar_zones/zone/+/+/entity/set"]


def settings(**changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                  debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                  mqtt_password=None, mqtt_ssl=False, data_dir=Path("."))
    values.update(changes)
    return Settings(**values)


class TopicsP6Test(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.t = build_topics_p6(self.s)
        self.m = self.t.make_mapper()

    def test_subscriptions_extend_p5_by_four_patterns_all_qos_one(self):
        base = build_topics_p5(self.s).subscriptions()
        subs = self.t.subscriptions()
        self.assertEqual(len(subs), 34)
        self.assertEqual(subs[:30], base)
        self.assertEqual([topic for topic, _ in subs[30:]], PATTERNS)
        self.assertTrue(all(qos == 1 for _, qos in subs[30:]))
        self.assertEqual(len({topic for topic, _ in subs}), 34)
        self.assertEqual(dict(subs)[self.t.frames], 0)

    def test_command_topics_are_the_p5_seventeen_and_the_four_patterns(self):
        cmds = self.t.command_topics()
        self.assertEqual(len(cmds), 21)
        self.assertEqual(set(cmds), set(build_topics_p5(self.s).command_topics()) | set(PATTERNS))

    def test_p5_classes_are_unchanged(self):
        t5 = build_topics_p5(self.s)
        self.assertEqual(len(t5.subscriptions()), 30)
        self.assertEqual(len(t5.command_topics()), 17)
        self.assertIs(type(t5.make_mapper()), TopicMapperP5)
        for f in dataclasses.fields(t5):
            self.assertEqual(getattr(self.t, f.name), getattr(t5, f.name), f.name)
        self.assertIsInstance(self.t, TopicsP5)
        self.assertIs(type(self.m), TopicMapperP6)

    def test_the_builders(self):
        t = self.t
        self.assertEqual(t.zone_occupancy("z0001"), "radar_zones/zone/z0001/occupancy")
        self.assertEqual(t.zone_count("z0001"), "radar_zones/zone/z0001/count")
        self.assertEqual(t.zone_event("l0002"), "radar_zones/zone/l0002/event")
        self.assertEqual(t.zone_attributes("z0001"), "radar_zones/zone/z0001/attributes")
        self.assertEqual(t.zone_clear_delay("z0001"), "radar_zones/zone/z0001/clear_delay")
        self.assertEqual(t.zone_clear_delay_set("z0001"), "radar_zones/zone/z0001/clear_delay/set")
        self.assertEqual(t.zone_margin("z0001"), "radar_zones/zone/z0001/margin")
        self.assertEqual(t.zone_margin_set("z0001"), "radar_zones/zone/z0001/margin/set")
        self.assertEqual(t.zone_forget("z0001"), "radar_zones/zone/z0001/forget")
        self.assertEqual(t.zone_variant("z0001", "wd0917"), "radar_zones/zone/z0001/wd0917/occupancy")
        self.assertEqual(t.zone_variant_entity("z0001", "wd0917"), "radar_zones/zone/z0001/wd0917/entity")
        self.assertEqual(t.zone_variant_entity_set("z0001", "wd0917"), "radar_zones/zone/z0001/wd0917/entity/set")
        self.assertEqual(t.zone_availability, "radar_zones/zone/availability")
        self.assertEqual(t.node_status, "led_matrix_node/sensors/status")
        self.assertEqual(t.learner_status, "radar_zones/learner/status")

    def test_node_prefix_applies_to_the_node_topics_only(self):
        t = build_topics_p6(settings(node_prefix="node_b"))
        self.assertEqual(t.node_status, "node_b/sensors/status")
        self.assertEqual(t.zone_availability, "radar_zones/zone/availability")
        self.assertEqual(t.zone_occupancy("z0001"), "radar_zones/zone/z0001/occupancy")

    def test_every_published_topic_matches_no_subscription_pattern(self):
        # the app must never receive its own output: no output topic has the shape of a command pattern
        for topic in (self.t.zone_occupancy("z0001"), self.t.zone_count("z0001"), self.t.zone_event("z0001"),
                      self.t.zone_attributes("z0001"), self.t.zone_clear_delay("z0001"), self.t.zone_margin("z0001"),
                      self.t.zone_variant("z0001", "wd0917"), self.t.zone_variant_entity("z0001", "wd0917"),
                      self.t.zone_availability):
            self.assertIsNone(classify_zone_topic(topic), topic)


class ClassifyTest(unittest.TestCase):
    def test_the_four_shapes(self):
        self.assertEqual(classify_zone_topic("radar_zones/zone/z0001/clear_delay/set"), ("number", "z0001", "cd"))
        self.assertEqual(classify_zone_topic("radar_zones/zone/l0001/margin/set"), ("number", "l0001", "mg"))
        self.assertEqual(classify_zone_topic("radar_zones/zone/z0001/forget"), ("forget", "z0001", None))
        self.assertEqual(classify_zone_topic("radar_zones/zone/z0001/wd0917/entity/set"),
                         ("request", "z0001", "wd0917"))

    def test_other_topics_are_not_zone_commands(self):
        for topic in ("radar_zones/review/answer", "radar_zones/zone/z0001/clear_delay", "radar_zones/zone/z0001/set",
                      "radar_zones/zone/z0001/occupancy/set", "radar_zones/zone/z0001/x/y/entity/set",
                      "radar_zones/zone/z0001/wd0917/entity", "radar_zones/zone/z0001/forget/set",
                      "radar_zones/zone/availability", "other/zone/z0001/forget", ""):
            self.assertIsNone(classify_zone_topic(topic), topic)


class ParserTest(unittest.TestCase):
    def test_numbers_are_decimal_integers_in_range(self):
        self.assertEqual(parse_zone_number(b"120", "cd"), 120)
        self.assertEqual(parse_zone_number(b" 120 ", "cd"), 120)
        self.assertEqual(parse_zone_number(b"120.0", "cd"), 120)
        self.assertEqual(parse_zone_number(b"+5", "mg"), 5)
        self.assertEqual((parse_zone_number(b"3", "cd"), parse_zone_number(b"900", "cd")), (3, 900))
        self.assertEqual((parse_zone_number(b"-50", "mg"), parse_zone_number(b"100", "mg")), (-50, 100))
        for bad in (b"2", b"901", b"0", b"-1", b"abc", b"", b"1e2", b"nan", b"inf", b"12.5", b"12,5", b"1 2", b"\xff",
                    b"1000000", b"--5", b"True"):
            self.assertIsNone(parse_zone_number(bad, "cd"), bad)
        for bad in (b"-51", b"101", b"5.5", b"x"):
            self.assertIsNone(parse_zone_number(bad, "mg"), bad)

    def test_on_off_only(self):
        self.assertIs(parse_on_off(b"ON"), True)
        self.assertIs(parse_on_off(b"off"), False)
        for bad in (b"1", b"0", b"true", b"", b"toggle", b"\xff"):
            self.assertIsNone(parse_on_off(bad), bad)

    def test_press(self):
        self.assertTrue(parse_press(b"PRESS"))
        self.assertTrue(parse_press(b" press "))
        self.assertFalse(parse_press(b"ON"))
        self.assertFalse(parse_press(b""))


class MapperTest(unittest.TestCase):
    def setUp(self):
        self.t = build_topics_p6(settings())
        self.m = self.t.make_mapper()

    def test_a_tuning_command(self):
        self.assertEqual(self.m.map(self.t.zone_clear_delay_set("z0001"), b"120", False, 5),
                         [ZoneTune("z0001", "cd", 120, 5)])
        self.assertEqual(self.m.map(self.t.zone_margin_set("l0002"), b"-25", False, 6),
                         [ZoneTune("l0002", "mg", -25, 6)])

    def test_a_forget_press_and_a_variant_request(self):
        self.assertEqual(self.m.map(self.t.zone_forget("z0001"), b"PRESS", False, 7), [ZoneForget("z0001", 7)])
        self.assertEqual(self.m.map(self.t.zone_variant_entity_set("z0001", "wd0917"), b"ON", False, 8),
                         [VariantRequest("z0001", "wd0917", True, 8)])
        self.assertEqual(self.m.map(self.t.zone_variant_entity_set("z0001", "we1022"), b"OFF", False, 9),
                         [VariantRequest("z0001", "we1022", False, 9)])
        self.assertEqual(self.m.zone_cmds_ignored, 0)

    def test_out_of_range_and_non_numbers_are_dropped_and_counted(self):
        topic = self.t.zone_clear_delay_set("z0001")
        for bad in (b"2", b"901", b"x", b"", b"1e3", b"nan"):
            self.assertEqual(self.m.map(topic, bad, False, 1), [], bad)
        self.assertEqual(self.m.map(self.t.zone_margin_set("z0001"), b"101", False, 1), [])
        self.assertEqual(self.m.map(self.t.zone_margin_set("z0001"), b"-51", False, 1), [])
        self.assertEqual(self.m.zone_cmds_ignored, 8)
        self.assertEqual(self.m.ignored, 8)

    def test_a_bad_forget_payload_or_request_payload_is_dropped(self):
        self.assertEqual(self.m.map(self.t.zone_forget("z0001"), b"ON", False, 1), [])
        self.assertEqual(self.m.map(self.t.zone_variant_entity_set("z0001", "wd0917"), b"1", False, 1), [])
        self.assertEqual(self.m.map(self.t.zone_variant_entity_set("z0001", "wd0917"), b"toggle", False, 1), [])
        self.assertEqual(self.m.zone_cmds_ignored, 3)

    def test_a_malformed_zone_id_or_variant_id_is_dropped(self):
        self.assertEqual(self.m.map("radar_zones/zone/foo/forget", b"PRESS", False, 1), [])
        self.assertEqual(self.m.map("radar_zones/zone/z1/forget", b"PRESS", False, 1), [])
        self.assertEqual(self.m.map("radar_zones/zone/x0001/clear_delay/set", b"60", False, 1), [])
        self.assertEqual(self.m.map("radar_zones/zone/z0001/wd9/entity/set", b"ON", False, 1), [])
        self.assertEqual(self.m.map("radar_zones/zone/z0001/xx0917/entity/set", b"ON", False, 1), [])
        self.assertEqual(self.m.zone_cmds_ignored, 5)

    def test_a_retained_message_on_a_command_topic_is_ignored_and_cleared(self):
        for topic in (self.t.zone_clear_delay_set("z0001"), self.t.zone_margin_set("z0001"), self.t.zone_forget("z0001"),
                      self.t.zone_variant_entity_set("z0001", "wd0917"), "radar_zones/zone/bad/forget"):
            self.assertEqual(self.m.map(topic, b"60", True, 1), [ClearRetained(topic)], topic)
            self.assertEqual(self.m.map(topic, b"", True, 1), [], topic)             # an empty retained one is a deletion
        self.assertEqual(self.m.zone_cmds_ignored, 0)

    def test_the_echo_of_the_apps_own_clear_is_not_a_command(self):
        topic = self.t.zone_forget("z0001")
        self.m.note_cleared(topic)
        self.assertEqual(self.m.map(topic, b"", False, 1), [])
        self.assertEqual(self.m.ignored, 0)

    def test_oversize_is_dropped_before_anything_is_copied_but_a_retained_one_is_cleared(self):
        topic = self.t.zone_clear_delay_set("z0001")
        self.assertEqual(self.m.map(topic, b"1" * 300, False, 1), [])
        self.assertEqual(self.m.oversize, 1)
        self.assertEqual(self.m.map(topic, b"1" * 300, True, 1), [ClearRetained(topic)])

    def test_known_zones_and_variants_are_checked_in_the_mapper_when_the_layer_says_so(self):
        self.m.known = (frozenset({"z0001"}), {"z0001": frozenset({"wd0917"})})
        self.assertEqual(self.m.map(self.t.zone_forget("z0002"), b"PRESS", False, 1), [])
        self.assertEqual(self.m.map(self.t.zone_clear_delay_set("l0001"), b"60", False, 1), [])
        self.assertEqual(self.m.map(self.t.zone_variant_entity_set("z0001", "we1022"), b"ON", False, 1), [])
        self.assertEqual(self.m.map(self.t.zone_variant_entity_set("z0002", "wd0917"), b"ON", False, 1), [])
        self.assertEqual(self.m.zone_cmds_ignored, 4)
        self.assertEqual(self.m.map(self.t.zone_forget("z0001"), b"PRESS", False, 2), [ZoneForget("z0001", 2)])
        self.assertEqual(self.m.map(self.t.zone_variant_entity_set("z0001", "wd0917"), b"ON", False, 3),
                         [VariantRequest("z0001", "wd0917", True, 3)])
        self.m.known = (frozenset(), {})                                         # no zone at all
        self.assertEqual(self.m.map(self.t.zone_forget("z0001"), b"PRESS", False, 4), [])

    def test_the_p3_p4_p5_inputs_still_map(self):
        t = self.t
        self.assertEqual(self.m.map(t.schedule_state, b"1", True, 5), [ScheduleState(True, None, None, 5)])
        self.assertEqual(self.m.map(t.context_home_empty, b"on", True, 6), [ContextState("home_empty", True, 6)])
        self.assertEqual(self.m.map(t.radar_state, b"night_off", True, 3), [RadarStateMsg("night_off", 3)])
        got = self.m.map(t.review_answer, b'{"op":"approve","item":"s0005-1","rev":1}', False, 9)
        self.assertEqual(len(got), 1)
        self.assertIsInstance(got[0], ReviewAnswer)
        big = json.dumps({"op": "rename", "item": "s0005-1", "rev": 1, "name": "ä" * 32, "aid": "a" * 64}).encode()
        self.assertGreater(len(big), 256)
        self.assertEqual(len(self.m.map(t.review_answer, big, False, 9)), 1)    # the 512-byte answer cap is kept
        self.assertEqual(self.m.map("led_matrix_node/other", b"x", False, 1), [])
        self.assertEqual(self.m.zone_cmds_ignored, 0)

    def test_a_state_topic_of_a_zone_is_not_a_command(self):
        # the app is not subscribed to it; if a message arrived it would only count as ignored, never as a command
        self.assertEqual(self.m.map(self.t.zone_clear_delay("z0001"), b"120", False, 1), [])
        self.assertEqual(self.m.zone_cmds_ignored, 0)
        self.assertEqual(self.m.ignored, 1)


class MqttIoTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        import queue
        self.t = build_topics_p6(settings())
        self.events = queue.Queue(maxsize=100)
        self.factory = FakeClientFactory()
        self.io = MqttIO(settings(), self.t, self.events, client_factory=self.factory)
        self.io.start()
        self.c = self.factory.last
        self.c.fire_connect(0)
        while not self.events.empty():
            self.events.get_nowait()

    def test_it_subscribes_to_the_thirty_four_topics(self):
        self.assertEqual(len(self.c.subscriptions), 34)
        self.assertEqual([t for t, _ in self.c.subscriptions[30:]], PATTERNS)
        self.assertIs(type(self.io.mapper), TopicMapperP6)

    def test_a_command_reaches_the_queue_and_a_retained_one_is_cleared(self):
        self.c.fire_message(self.t.zone_clear_delay_set("z0001"), b"90")
        ev = self.events.get_nowait()
        self.assertEqual((type(ev), ev.zid, ev.field, ev.value), (ZoneTune, "z0001", "cd", 90))
        self.c.fire_message(self.t.zone_forget("z0001"), b"PRESS")
        ev = self.events.get_nowait()
        self.assertEqual((type(ev), ev.zid), (ZoneForget, "z0001"))
        self.c.published.clear()
        self.c.fire_message(self.t.zone_forget("z0001"), b"PRESS", retain=True)
        self.assertTrue(self.events.empty())
        self.assertEqual(self.c.published, [(self.t.zone_forget("z0001"), b"", 1, True)])
        self.assertEqual(self.io.retained_cleared, 1)


if __name__ == "__main__":
    unittest.main()
