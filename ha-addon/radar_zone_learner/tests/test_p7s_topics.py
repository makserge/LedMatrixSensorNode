"""P7 topics and the mapper (P7 spec 7.1, check P7-11 shell part): 35 subscriptions, the one new command, a retained command
ignored and cleared, the export press and its payload rules, the three topic names, and the P5 and P6 classes unchanged."""
import dataclasses
import unittest
from pathlib import Path

from radar_zone_learner.core.types import ZoneForget
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.p4_types import ReviewAnswer
from radar_zone_learner.shell.topics import ClearRetained
from radar_zone_learner.shell.topics_p5 import TopicMapperP5, build_topics_p5
from radar_zone_learner.shell.topics_p6 import TopicMapperP6, TopicsP6, build_topics_p6
from radar_zone_learner.shell.topics_p7 import ExportTuningPress, TopicMapperP7, build_topics_p7
from tests.fakes import FakeClientFactory, quiet_logs

CMD = "radar_zones/learner/export_tuning"
STATE = "radar_zones/learner/tuning"
EXPORT = "radar_zones/learner/tuning/export"


def settings(**changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                  debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                  mqtt_password=None, mqtt_ssl=False, data_dir=Path("."))
    values.update(changes)
    return Settings(**values)


class TopicsP7Test(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.t = build_topics_p7(self.s)
        self.m = self.t.make_mapper()

    def test_the_three_names(self):
        self.assertEqual((self.t.learner_export_tuning, self.t.learner_tuning, self.t.learner_tuning_export),
                         (CMD, STATE, EXPORT))

    def test_35_subscriptions_the_p6_34_then_the_export_command_at_qos_one(self):
        base = build_topics_p6(self.s).subscriptions()
        subs = self.t.subscriptions()
        self.assertEqual(len(subs), 35)
        self.assertEqual(subs[:34], base)
        self.assertEqual(subs[34], (CMD, 1))
        self.assertEqual(len({topic for topic, _ in subs}), 35)
        self.assertEqual(dict(subs)[self.t.frames], 0)

    def test_only_the_command_is_an_input_the_sensor_and_the_export_are_not_subscribed(self):
        subs = {topic for topic, _ in self.t.subscriptions()}
        self.assertIn(CMD, subs)
        self.assertNotIn(STATE, subs)
        self.assertNotIn(EXPORT, subs)                                      # the app must never receive its own output

    def test_command_topics_are_the_p6_twenty_one_and_the_export_command(self):
        cmds = self.t.command_topics()
        self.assertEqual(len(cmds), 22)
        self.assertEqual(set(cmds), set(build_topics_p6(self.s).command_topics()) | {CMD})

    def test_the_p5_and_p6_classes_are_unchanged(self):
        t6 = build_topics_p6(self.s)
        self.assertEqual(len(t6.subscriptions()), 34)
        self.assertEqual(len(t6.command_topics()), 21)
        self.assertIs(type(t6.make_mapper()), TopicMapperP6)
        t5 = build_topics_p5(self.s)
        self.assertEqual(len(t5.subscriptions()), 30)
        self.assertIs(type(t5.make_mapper()), TopicMapperP5)
        for f in dataclasses.fields(t6):
            self.assertEqual(getattr(self.t, f.name), getattr(t6, f.name), f.name)
        self.assertIsInstance(self.t, TopicsP6)
        self.assertIs(type(self.m), TopicMapperP7)
        self.assertIsInstance(self.m, TopicMapperP6)

    def test_the_node_prefix_changes_no_p7_topic(self):
        t = build_topics_p7(settings(node_prefix="node_b"))
        self.assertEqual((t.learner_export_tuning, t.learner_tuning, t.learner_tuning_export), (CMD, STATE, EXPORT))
        self.assertEqual(t.node_status, "node_b/sensors/status")


class MapperTest(unittest.TestCase):
    def setUp(self):
        self.t = build_topics_p7(settings())
        self.m = self.t.make_mapper()

    def test_a_press_is_an_export_event(self):
        self.assertEqual(self.m.map(CMD, b"PRESS", False, 5), [ExportTuningPress(5)])
        self.assertEqual(self.m.map(CMD, b" press ", False, 6), [ExportTuningPress(6)])

    def test_anything_else_is_ignored_and_counted(self):
        for bad in (b"", b"ON", b"1", b"export", b"\xff", b"PRESS PRESS"):
            self.assertEqual(self.m.map(CMD, bad, False, 1), [], bad)
        self.assertEqual(self.m.ignored, 6)           # the empty payload is not an own-clear echo here: counted too

    def test_a_retained_press_is_ignored_and_cleared_never_executed(self):
        self.assertEqual(self.m.map(CMD, b"PRESS", True, 1), [ClearRetained(CMD)])
        self.assertEqual(self.m.map(CMD, b"", True, 1), [])                 # an empty retained one is a deletion
        self.assertEqual(self.m.ignored, 0)

    def test_the_echo_of_the_apps_own_clear_is_not_a_command(self):
        self.m.note_cleared(CMD)
        self.assertEqual(self.m.map(CMD, b"", False, 1), [])
        self.assertEqual(self.m.ignored, 0)

    def test_an_oversize_payload_is_dropped_but_a_retained_one_is_cleared(self):
        self.assertEqual(self.m.map(CMD, b"P" * 300, False, 1), [])
        self.assertEqual(self.m.oversize, 1)
        self.assertEqual(self.m.map(CMD, b"P" * 300, True, 1), [ClearRetained(CMD)])

    def test_the_p5_and_p6_topics_still_map_as_before(self):
        self.assertEqual(self.m.map(self.t.zone_forget("z0001"), b"PRESS", False, 7), [ZoneForget("z0001", 7)])
        got = self.m.map(self.t.review_answer, b'{"op":"approve","item":"radar_moved","rev":3}', False, 8)
        self.assertEqual(len(got), 1)
        self.assertIsInstance(got[0], ReviewAnswer)
        self.assertEqual((got[0].item, got[0].op, got[0].rev), ("radar_moved", "approve", 3))

    def test_the_new_item_ids_are_accepted_by_the_answer_and_the_select_parsers(self):
        from radar_zone_learner.shell.topics_p5 import parse_answer_p5, parse_select_p5
        for item in ("ret-z0002", "radar_moved", "mod-z0001", "mrg-s0006-1"):
            ans = parse_answer_p5(('{"op":"reject","item":"%s","rev":1}' % item).encode())
            self.assertEqual(ans["item"], item)
            self.assertEqual(parse_select_p5(item.encode()), ("item", item))
        self.assertEqual(parse_select_p5(b"Retire D"), ("label", "Retire D"))
        self.assertEqual(parse_select_p5(b"Radar moved"), ("label", "Radar moved"))
        self.assertIsNone(parse_answer_p5(b'{"op":"reject","item":"ret-z1","rev":1}'))        # four digits at least


class MqttIoTest(unittest.TestCase):
    """The MQTT thread subscribes to the 35 and clears a retained press."""

    def test_the_client_subscribes_to_35_topics_and_a_retained_press_is_cleared(self):
        import queue
        quiet_logs(self)
        t = build_topics_p7(settings())
        events = queue.Queue()
        factory = FakeClientFactory()
        io = MqttIO(settings(), t, events, client_factory=factory)
        io.start()
        client = factory.last
        client.fire_connect(0)
        self.assertEqual(len(client.subscriptions), 35)
        self.assertEqual(client.subscriptions[-1], (CMD, 1))
        self.assertIs(type(io.mapper), TopicMapperP7)
        client.fire_message(CMD, b"PRESS", retain=True)
        self.assertEqual(client.published_to(CMD), [(CMD, b"", 1, True)])
        self.assertEqual(io.retained_cleared, 1)
        client.fire_message(CMD, b"PRESS", retain=False)
        got = [e for e in list(events.queue) if isinstance(e, ExportTuningPress)]
        self.assertEqual(len(got), 1)


if __name__ == "__main__":
    unittest.main()
