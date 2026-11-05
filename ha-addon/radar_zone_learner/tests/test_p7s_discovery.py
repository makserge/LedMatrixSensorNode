"""P7 discovery (P7 spec 7.1, check P7-11 shell part): the two configs byte for byte, their place on the learner device, no
per-zone entity and no change to the messages of P3 to P6, the unique ids and entity ids, and the sizes."""
import json
import unittest
from pathlib import Path

from radar_zone_learner.core.types import APP_VERSION
from radar_zone_learner.shell.discovery import discovery_messages
from radar_zone_learner.shell.discovery_p7 import (EXPORT_TUNING_ID, TUNING_ID, export_tuning_config, p7_discovery_messages,
                                                   tuning_config)
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.p6 import P6Layer
from radar_zone_learner.shell.p7 import P7Layer
from radar_zone_learner.shell.topics_p6 import build_topics_p6
from radar_zone_learner.shell.topics_p7 import build_topics_p7
from tests.fakes import FakeLearner, quiet_logs
from tests.p6_harness import zone_view

DEVICE = ('{"identifiers":["radar_zones_learner"],"name":"Radar zone learner","model":"radar_zone_learner app",'
          '"sw_version":"%s"}' % APP_VERSION)
ORIGIN = '{"name":"radar_zone_learner","sw_version":"%s"}' % APP_VERSION
BUTTON = "homeassistant/button/radar_zones_learner_export_tuning/config"
SENSOR = "homeassistant/sensor/radar_zones_learner_tuning/config"


def settings(**changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                  debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                  mqtt_password=None, mqtt_ssl=False, data_dir=Path("."))
    values.update(changes)
    return Settings(**values)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.t = build_topics_p7(settings())
        self.msgs = p7_discovery_messages(self.t)
        self.raw = dict(self.msgs)

    def test_two_messages_the_button_then_the_sensor(self):
        self.assertEqual([t for t, _ in self.msgs], [BUTTON, SENSOR])
        for _, payload in self.msgs:
            self.assertIsInstance(payload, bytes)
            self.assertGreater(len(payload), 0)                       # a config, never a removal

    def test_the_button_config_byte_for_byte(self):
        expected = ('{"name":"Export tuning data","unique_id":"radar_zones_learner_export_tuning","default_entity_id":'
                    '"button.radar_zones_learner_export_tuning","command_topic":"radar_zones/learner/export_tuning",'
                    '"payload_press":"PRESS","qos":1,"retain":false,"entity_category":"config","icon":'
                    '"mdi:database-export-outline","availability_topic":"radar_zones/learner/status","device":' + DEVICE +
                    ',"origin":' + ORIGIN + '}')
        self.assertEqual(self.raw[BUTTON].decode(), expected)

    def test_the_sensor_config_byte_for_byte(self):
        expected = ('{"name":"Tuning data","unique_id":"radar_zones_learner_tuning","default_entity_id":'
                    '"sensor.radar_zones_learner_tuning","state_topic":"radar_zones/learner/tuning","entity_category":'
                    '"diagnostic","icon":"mdi:chart-histogram","availability_topic":"radar_zones/learner/status","device":' +
                    DEVICE + ',"origin":' + ORIGIN + '}')
        self.assertEqual(self.raw[SENSOR].decode(), expected)

    def test_the_ids(self):
        self.assertEqual(EXPORT_TUNING_ID, "radar_zones_learner_export_tuning")
        self.assertEqual(TUNING_ID, "radar_zones_learner_tuning")
        self.assertEqual(self.t.discovery("button", EXPORT_TUNING_ID), BUTTON)
        self.assertEqual(self.t.discovery("sensor", TUNING_ID), SENSOR)

    def test_the_button_is_a_config_entity_that_is_not_retained_and_the_sensor_is_diagnostic(self):
        b, s = export_tuning_config(self.t), tuning_config(self.t)
        self.assertEqual(b["entity_category"], "config")
        self.assertEqual(s["entity_category"], "diagnostic")
        self.assertIs(b["retain"], False)
        self.assertEqual(b["qos"], 1)
        self.assertEqual(b["payload_press"], "PRESS")
        self.assertNotIn("command_topic", s)
        self.assertNotIn("json_attributes_topic", s)                  # nothing but the one short state text

    def test_both_are_on_the_learner_device_and_use_the_single_learner_availability(self):
        for cfg in (export_tuning_config(self.t), tuning_config(self.t)):
            self.assertEqual(cfg["device"]["identifiers"], ["radar_zones_learner"])
            self.assertNotIn("via_device", cfg["device"])
            self.assertEqual(cfg["availability_topic"], "radar_zones/learner/status")
            self.assertNotIn("availability", cfg)
            self.assertEqual(cfg["device"]["sw_version"], APP_VERSION)
            self.assertEqual(cfg["origin"], {"name": "radar_zone_learner", "sw_version": APP_VERSION})

    def test_the_command_topic_is_the_one_the_app_subscribes_to_and_the_state_topic_is_the_sensors(self):
        subs = {t for t, _ in self.t.subscriptions()}
        self.assertIn(export_tuning_config(self.t)["command_topic"], subs)
        self.assertEqual(tuning_config(self.t)["state_topic"], self.t.learner_tuning)
        self.assertNotIn(self.t.learner_tuning, subs)
        self.assertNotIn(self.t.learner_tuning_export, subs)

    def test_the_names_carry_no_zone_and_the_payloads_are_small(self):
        for _, payload in self.msgs:
            self.assertLess(len(payload), 700)
            self.assertNotRegex(payload.decode(), r"z0001|Desk|zone name")

    def test_the_discovery_prefix_option_applies(self):
        t = build_topics_p7(settings(discovery_prefix="ha2"))
        self.assertEqual([m[0] for m in p7_discovery_messages(t)],
                         ["ha2/button/radar_zones_learner_export_tuning/config", "ha2/sensor/radar_zones_learner_tuning/config"])


class LayerDiscoveryTest(unittest.TestCase):
    """The layer's burst is the P6 burst and the two new messages: nothing of P3 to P6 changes."""

    def make(self, cls, build, zones=True):
        quiet_logs(self)
        s = settings()
        topics = build(s)
        learner = FakeLearner()
        if zones:
            learner.p5_zones = (zone_view("z0001", "Desk"),)
            learner.p6_zone("z0001")
        layer = cls(topics, lambda *a, **k: True, None, debug_map=True)
        layer.bind(lambda fn, *a: fn(*a), learner)
        return layer, topics, s

    def test_the_p7_burst_is_the_p6_burst_plus_two(self):
        l6, t6, s = self.make(P6Layer, build_topics_p6)
        l7, t7, _ = self.make(P7Layer, build_topics_p7)
        m6, m7 = l6.discovery(), l7.discovery()
        self.assertEqual(m7[:len(m6)], m6)
        self.assertEqual([t for t, _ in m7[len(m6):]], [BUTTON, SENSOR])
        self.assertEqual(len(m7), len(m6) + 2)

    def test_p3_to_p6_messages_with_no_zone_are_thirty_one_and_the_service_adds_four(self):
        l7, t7, s = self.make(P7Layer, build_topics_p7, zones=False)
        self.assertEqual(len(l7.discovery()), 31 + 2)
        self.assertEqual(len(l7.discovery()) + len(discovery_messages(s, t7)), 31 + 4 + 2)

    def test_every_unique_id_of_the_burst_is_unique_and_no_new_per_zone_entity_exists(self):
        l7, t7, s = self.make(P7Layer, build_topics_p7)
        ids = []
        for topic, payload in l7.discovery() + discovery_messages(s, t7):
            if payload:
                ids.append(json.loads(payload)["unique_id"])
        self.assertEqual(len(ids), len(set(ids)))
        new = [i for i in ids if i in (EXPORT_TUNING_ID, TUNING_ID)]
        self.assertEqual(sorted(new), sorted([EXPORT_TUNING_ID, TUNING_ID]))
        zone_ids = [i for i in ids if i.startswith("radar_spot_")]
        self.assertEqual(len(zone_ids), 6)                              # the one zone's six entities (P6), nothing more

    def test_a_layer_needs_the_p7_topics(self):
        with self.assertRaises(TypeError):
            P7Layer(build_topics_p6(settings()), lambda *a, **k: True)


if __name__ == "__main__":
    unittest.main()
