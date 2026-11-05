"""P6 discovery (check P6-12, spec 8.1, 8.4, 8.7, Appendix B.1): six configs for each zone, the availability shapes, the
unique ids and the epoch, the variant entities, the cap, the sizes, the removal messages, and the 31 messages of P5 that must
not change."""
import json
import unittest
from pathlib import Path

from radar_zone_learner.shell.discovery_p5 import p5_discovery_messages
from radar_zone_learner.shell.discovery_p6 import (DEVICE_VIA, base_uid, device_id, entity_zones, p6_discovery_messages,
                                                   p6_removal_messages, variant_sensor_topic, variant_switch_topic,
                                                   zone_config_topics, zone_messages, zone_title)
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.p5 import P5Layer
from radar_zone_learner.shell.p6 import P6Layer
from radar_zone_learner.shell.p6_types import ZoneEntry
from radar_zone_learner.shell.topics_p5 import build_topics_p5
from radar_zone_learner.shell.topics_p6 import build_topics_p6
from tests.fakes import FakeLearner
from tests.p6_harness import zone_view

OR = '{"name":"radar_zone_learner","sw_version":"0.7.0"}'
AV_ALL = ('"availability":[{"topic":"radar_zones/learner/status","payload_available":"online","payload_not_available":'
          '"offline"},{"topic":"led_matrix_node/sensors/status","payload_available":"online","payload_not_available":'
          '"offline"},{"topic":"radar_zones/zone/availability","payload_available":"online","payload_not_available":'
          '"offline"}],"availability_mode":"all"')
AV_ONE = '"availability_topic":"radar_zones/learner/status"'


def settings(**changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                  debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                  mqtt_password=None, mqtt_ssl=False, data_dir=Path("."))
    values.update(changes)
    return Settings(**values)


def device(zid="z0001", name="Desk", epoch=0):
    return ('{"identifiers":["%s"],"name":"%s","model":"radar_zone_learner zone","sw_version":"0.7.0",'
            '"via_device":"Led_Matrix_Node"}' % (device_id(zid, epoch), name))


class ZoneConfigsTest(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.t = build_topics_p6(self.s)
        self.entry = ZoneEntry("z0001", "Desk")
        self.msgs = zone_messages(self.t, self.entry)
        self.raw = dict(self.msgs)
        self.cfg = {topic: json.loads(p) for topic, p in self.msgs}

    def one(self, component, suffix=""):
        return self.cfg["homeassistant/%s/radar_spot_z0001%s/config" % (component, suffix)]

    def test_six_configs_in_the_order_of_spec_8_1(self):
        self.assertEqual([t for t, _ in self.msgs], [
            "homeassistant/binary_sensor/radar_spot_z0001/config", "homeassistant/sensor/radar_spot_z0001_count/config",
            "homeassistant/event/radar_spot_z0001_event/config",
            "homeassistant/number/radar_spot_z0001_clear_delay/config",
            "homeassistant/number/radar_spot_z0001_margin/config", "homeassistant/button/radar_spot_z0001_forget/config"])

    def test_the_occupancy_config_byte_for_byte(self):
        expected = ('{"name":"Desk occupancy","unique_id":"radar_spot_z0001","default_entity_id":'
                    '"binary_sensor.radar_spot_z0001","state_topic":"radar_zones/zone/z0001/occupancy",'
                    '"device_class":"occupancy","payload_on":"ON","payload_off":"OFF","json_attributes_topic":'
                    '"radar_zones/zone/z0001/attributes",' + AV_ALL + ',"device":' + device() + ',"origin":' + OR + '}')
        self.assertEqual(self.raw["homeassistant/binary_sensor/radar_spot_z0001/config"].decode(), expected)

    def test_the_event_and_count_configs(self):
        ev = self.one("event", "_event")
        self.assertEqual(ev["state_topic"], "radar_zones/zone/z0001/event")
        self.assertEqual(ev["event_types"], ["enter", "leave"])
        self.assertEqual(ev["unique_id"], "radar_spot_z0001_event")
        self.assertEqual(ev["default_entity_id"], "event.radar_spot_z0001_event")
        cnt = self.one("sensor", "_count")
        self.assertEqual(cnt["state_topic"], "radar_zones/zone/z0001/count")
        self.assertIs(cnt["enabled_by_default"], False)
        self.assertEqual(cnt["entity_category"], "diagnostic")
        self.assertEqual(cnt["state_class"], "measurement")
        self.assertEqual(cnt["default_entity_id"], "sensor.radar_spot_z0001_count")

    def test_the_tuning_numbers(self):
        cd = self.one("number", "_clear_delay")
        self.assertEqual((cd["min"], cd["max"], cd["step"], cd["unit_of_measurement"], cd["mode"]), (3, 900, 1, "s", "box"))
        self.assertEqual((cd["command_topic"], cd["state_topic"]),
                         ("radar_zones/zone/z0001/clear_delay/set", "radar_zones/zone/z0001/clear_delay"))
        mg = self.one("number", "_margin")
        self.assertEqual((mg["min"], mg["max"], mg["step"], mg["unit_of_measurement"]), (-50, 100, 5, "cm"))
        self.assertEqual((mg["command_topic"], mg["state_topic"]),
                         ("radar_zones/zone/z0001/margin/set", "radar_zones/zone/z0001/margin"))
        for cfg in (cd, mg):
            self.assertEqual(cfg["entity_category"], "config")
            self.assertEqual((cfg["qos"], cfg["retain"], cfg["optimistic"]), (1, False, False))

    def test_the_forget_button(self):
        b = self.one("button", "_forget")
        self.assertEqual((b["command_topic"], b["payload_press"]), ("radar_zones/zone/z0001/forget", "PRESS"))
        self.assertEqual((b["entity_category"], b["qos"], b["retain"]), ("config", 1, False))

    def test_the_availability_shape_per_entity_type(self):
        # state entities: the three-entry list and the mode all, never an availability_topic next to it
        for cfg in (self.one("binary_sensor"), self.one("sensor", "_count"), self.one("event", "_event")):
            self.assertEqual(cfg["availability_mode"], "all")
            self.assertEqual(cfg["availability"], [
                {"topic": "radar_zones/learner/status", "payload_available": "online", "payload_not_available": "offline"},
                {"topic": "led_matrix_node/sensors/status", "payload_available": "online",
                 "payload_not_available": "offline"},
                {"topic": "radar_zones/zone/availability", "payload_available": "online",
                 "payload_not_available": "offline"}])
            self.assertNotIn("availability_topic", cfg)
        # configuration entities: the learner's availability only, they must work while the radars are off
        for cfg in (self.one("number", "_clear_delay"), self.one("number", "_margin"), self.one("button", "_forget")):
            self.assertEqual(cfg["availability_topic"], "radar_zones/learner/status")
            self.assertNotIn("availability", cfg)
            self.assertNotIn("availability_mode", cfg)

    def test_the_node_status_topic_follows_the_node_prefix(self):
        t = build_topics_p6(settings(node_prefix="node_b"))
        cfg = json.loads(dict(zone_messages(t, self.entry))["homeassistant/binary_sensor/radar_spot_z0001/config"])
        self.assertEqual(cfg["availability"][1]["topic"], "node_b/sensors/status")

    def test_the_device_and_the_origin(self):
        for cfg in self.cfg.values():
            self.assertEqual(cfg["device"], {"identifiers": ["radar_zone_z0001"], "name": "Desk",
                                             "model": "radar_zone_learner zone", "sw_version": "0.7.0",
                                             "via_device": "Led_Matrix_Node"})
            self.assertEqual(cfg["origin"], {"name": "radar_zone_learner", "sw_version": "0.7.0"})
        self.assertEqual(DEVICE_VIA, "Led_Matrix_Node")

    def test_unique_ids_default_entity_ids_and_names(self):
        uids = [c["unique_id"] for c in self.cfg.values()]
        self.assertEqual(uids, ["radar_spot_z0001", "radar_spot_z0001_count", "radar_spot_z0001_event",
                                "radar_spot_z0001_clear_delay", "radar_spot_z0001_margin", "radar_spot_z0001_forget"])
        for topic, cfg in self.cfg.items():
            comp = topic.split("/")[1]
            self.assertEqual(cfg["default_entity_id"], comp + "." + cfg["unique_id"])
        self.assertEqual([c["name"] for c in self.cfg.values()],
                         ["Desk occupancy", "Desk target count", "Desk enter/leave", "Desk clear delay", "Desk margin",
                          "Desk forget learned data"])

    def test_no_p3_p4_p5_unique_id_or_name_is_reused(self):
        t5 = build_topics_p5(self.s)
        old = {json.loads(p)["unique_id"] for _, p in P5Layer(t5, lambda *a, **k: True, None).discovery()}
        old |= {"radar_zones_learner_status", "radar_zones_learner_debug_map", "radar_zones_review_mark",
                "radar_zones_review_name"}
        self.assertFalse(old & {c["unique_id"] for c in self.cfg.values()})

    def test_no_zone_name_in_any_topic(self):
        entry = ZoneEntry("z0001", "Kuechenzeile", ("wd0917",), ("wd0917",))
        for topic, _ in zone_messages(self.t, entry):
            self.assertNotIn("Kuechenzeile", topic)
            self.assertNotIn("kuechenzeile", topic.lower())

    def test_an_unnamed_label_is_spot_and_its_id(self):
        self.assertEqual(zone_title("", "l0003"), "Spot l0003")
        self.assertEqual(zone_title("  ", "l0003"), "Spot l0003")
        self.assertEqual(zone_title("Bed", "l0003"), "Bed")
        msgs = dict(zone_messages(self.t, ZoneEntry("l0003", "")))
        cfg = json.loads(msgs["homeassistant/binary_sensor/radar_spot_l0003/config"])
        self.assertEqual(cfg["name"], "Spot l0003 occupancy")
        self.assertEqual(cfg["device"]["name"], "Spot l0003")


class EpochTest(unittest.TestCase):
    def setUp(self):
        self.t = build_topics_p6(settings())

    def test_the_epoch_is_in_the_unique_id_and_the_device_identifier_when_above_zero(self):
        self.assertEqual(base_uid("z0001"), "radar_spot_z0001")
        self.assertEqual(base_uid("z0001", 0), "radar_spot_z0001")
        self.assertEqual(base_uid("z0001", 2), "radar_spot_e2_z0001")
        self.assertEqual(device_id("z0001"), "radar_zone_z0001")
        self.assertEqual(device_id("l0001", 3), "radar_zone_e3_l0001")
        msgs = dict(zone_messages(self.t, ZoneEntry("z0001", "Desk", ("wd0917",), ("wd0917",)), None, 1))
        self.assertEqual(len(msgs), 8)
        for topic, payload in msgs.items():
            cfg = json.loads(payload)
            self.assertIn("radar_spot_e1_z0001", cfg["unique_id"])
            self.assertEqual(cfg["device"]["identifiers"], ["radar_zone_e1_z0001"])
            self.assertEqual(cfg["default_entity_id"].split(".", 1)[1], cfg["unique_id"])
            self.assertIn("radar_spot_e1_z0001", topic)
            # the topics of the data keep the plain zone id
            for key in ("state_topic", "command_topic", "json_attributes_topic"):
                if key in cfg:
                    self.assertTrue(cfg[key].startswith("radar_zones/zone/z0001/"), cfg[key])

    def test_epoch_zero_and_one_never_share_a_unique_id(self):
        a = {json.loads(p)["unique_id"] for _, p in zone_messages(self.t, ZoneEntry("z0001", "Desk", ("wd0917",)), None, 0)}
        b = {json.loads(p)["unique_id"] for _, p in zone_messages(self.t, ZoneEntry("z0001", "Desk", ("wd0917",)), None, 1)}
        self.assertFalse(a & b)


class VariantEntitiesTest(unittest.TestCase):
    def setUp(self):
        self.t = build_topics_p6(settings())

    def test_a_request_switch_for_every_variant_and_a_sensor_only_for_the_requested(self):
        entry = ZoneEntry("z0001", "Desk", ("wd0917", "we1022"), ("we1022",))
        msgs = zone_messages(self.t, entry)
        self.assertEqual(len(msgs), 6 + 2 + 1)
        cfg = {t: json.loads(p) for t, p in msgs}
        sw = cfg["homeassistant/switch/radar_spot_z0001_vreq_wd0917/config"]
        self.assertEqual(sw["name"], "Desk - wd0917 sensor")
        self.assertEqual(sw["unique_id"], "radar_spot_z0001_vreq_wd0917")
        self.assertEqual((sw["command_topic"], sw["state_topic"]),
                         ("radar_zones/zone/z0001/wd0917/entity/set", "radar_zones/zone/z0001/wd0917/entity"))
        self.assertEqual((sw["entity_category"], sw["payload_on"], sw["state_on"], sw["optimistic"], sw["qos"],
                          sw["retain"]), ("config", "ON", "ON", False, 1, False))
        self.assertEqual(sw["availability_topic"], "radar_zones/learner/status")
        self.assertNotIn("availability", sw)
        self.assertNotIn("homeassistant/binary_sensor/radar_spot_z0001_v_wd0917/config", cfg)
        ss = cfg["homeassistant/binary_sensor/radar_spot_z0001_v_we1022/config"]
        self.assertEqual(ss["device_class"], "occupancy")
        self.assertEqual(ss["state_topic"], "radar_zones/zone/z0001/we1022/occupancy")
        self.assertEqual(ss["availability_mode"], "all")
        self.assertEqual(len(ss["availability"]), 3)
        self.assertNotIn("availability_topic", ss)
        self.assertEqual(ss["name"], "Desk - we1022 occupancy")

    def test_a_requested_id_that_is_not_a_variant_gets_no_sensor(self):
        entry = ZoneEntry("z0001", "Desk", ("wd0917",), ("wd0917", "we1022"))
        self.assertEqual(len(zone_messages(self.t, entry)), 6 + 1 + 1)

    def test_no_variants_no_switches(self):
        self.assertEqual(len(zone_messages(self.t, ZoneEntry("z0001", "Desk"))), 6)

    def test_the_requested_mapping_overrides_the_entry(self):
        entry = ZoneEntry("z0001", "Desk", ("wd0917",), ())
        msgs = p6_discovery_messages(self.t, [entry], {"z0001": ("wd0917",)})
        self.assertEqual(len(msgs), 8)
        msgs = p6_discovery_messages(self.t, [ZoneEntry("z0001", "Desk", ("wd0917",), ("wd0917",))], {})
        self.assertEqual(len(msgs), 7)


class SetAndCapTest(unittest.TestCase):
    def setUp(self):
        self.t = build_topics_p6(settings())

    def test_the_cap_is_twenty_four_zones(self):
        entries = [ZoneEntry("z%04d" % k, "Z%d" % k) for k in range(1, 31)]
        msgs = p6_discovery_messages(self.t, entries, cap=24)
        self.assertEqual(len(msgs), 24 * 6)
        self.assertEqual(len({t for t, _ in msgs}), 144)
        uids = {json.loads(p)["unique_id"] for _, p in msgs}
        self.assertEqual(len(uids), 144)
        self.assertEqual(len(p6_discovery_messages(self.t, entries, cap=2)), 12)
        self.assertEqual(p6_discovery_messages(self.t, entries, cap=0), [])

    def test_entity_zones_filters_the_kinds_and_keeps_the_order(self):
        views = [zone_view("z0001", "A", "dwell"), zone_view("z0002", "B", "entry"), zone_view("z0003", "C", "bed"),
                 zone_view("z0004", "D", "transit"), zone_view("z0005", "E", "short_task"),
                 zone_view("l0001", "F", "manual"), zone_view("l0002", "G", "pet"), zone_view("l0003", "H", "ignore"),
                 zone_view("l0004", "I", "reflection")]
        self.assertEqual([v.id for v in entity_zones(views, 24)], ["z0001", "z0003", "z0005", "l0001"])
        self.assertEqual([v.id for v in entity_zones(views, 2)], ["z0001", "z0003"])

    def test_the_discovery_prefix_applies(self):
        t = build_topics_p6(settings(discovery_prefix="ha2"))
        self.assertTrue(all(topic.startswith("ha2/") for topic, _ in zone_messages(t, ZoneEntry("z0001", "Desk"))))


class SizeTest(unittest.TestCase):
    def setUp(self):
        self.t = build_topics_p6(settings())

    def test_every_payload_is_below_its_limit_also_with_a_forty_character_name_with_umlauts(self):
        name = ("äöüßÄÖÜ" * 6)[:40]
        entry = ZoneEntry("l0123", name, ("wd0917", "we1022", "wd1217", "we0710", "wd1822", "we1618"),
                          ("wd0917", "we1022", "wd1217", "we0710", "wd1822", "we1618"))
        msgs = zone_messages(self.t, entry, epoch=12)
        self.assertEqual(len(msgs), 6 + 6 + 6)
        for topic, payload in msgs:
            self.assertLess(len(payload), 1300, topic)           # spec 8.7: one zone config below 1.3 KB
            self.assertLess(len(payload), 4096, topic)
            self.assertNotIn(b"\\u", payload)                    # UTF-8, not escaped
            json.loads(payload.decode("utf-8"))

    def test_the_longest_ascii_name_too(self):
        entry = ZoneEntry("z9999", "x" * 40, ("wd0917",), ("wd0917",))
        for _, payload in zone_messages(self.t, entry, epoch=99):
            self.assertLess(len(payload), 1300)


class RemovalTest(unittest.TestCase):
    def setUp(self):
        self.t = build_topics_p6(settings())

    def test_a_whole_zone_is_six_empty_configs(self):
        msgs = p6_removal_messages(self.t, ["z0001"])
        self.assertEqual(msgs, [(t, b"") for t in zone_config_topics(self.t, "z0001")])
        self.assertEqual([t for t, _ in msgs], [t for t, _ in zone_messages(self.t, ZoneEntry("z0001", "Desk"))])

    def test_the_topics_of_a_removal_are_the_topics_of_the_configs(self):
        entry = ZoneEntry("z0001", "Desk", ("wd0917", "we1022"), ("wd0917",))
        added = {t for t, _ in zone_messages(self.t, entry, epoch=2)}
        gone = {t for t, _ in p6_removal_messages(self.t, ["z0001"], [("z0001", "wd0917"), ("z0001", "we1022")],
                                                  [("z0001", "wd0917")], epoch=2)}
        self.assertEqual(added, gone)

    def test_a_request_switch_and_a_variant_sensor_alone(self):
        self.assertEqual(p6_removal_messages(self.t, switches=[("z0001", "wd0917")]),
                         [(variant_switch_topic(self.t, "z0001", "wd0917"), b"")])
        self.assertEqual(p6_removal_messages(self.t, sensors=[("z0001", "wd0917")]),
                         [(variant_sensor_topic(self.t, "z0001", "wd0917"), b"")])
        self.assertEqual(p6_removal_messages(self.t), [])


class LayerDiscoveryTest(unittest.TestCase):
    """The layer's discovery: the 31 messages of P5 are byte for byte the same, then six for each zone."""

    def setUp(self):
        self.s = settings()
        self.t = build_topics_p6(self.s)

    def layer(self, learner):
        layer = P6Layer(self.t, lambda *a, **k: True, None, debug_map=False)
        layer.bind(lambda fn, *a: fn(*a), learner)
        return layer

    def test_with_no_zone_the_messages_are_exactly_those_of_p5(self):
        p5 = P5Layer(build_topics_p5(self.s), lambda *a, **k: True, None, debug_map=False).discovery()
        p6 = self.layer(FakeLearner()).discovery()
        self.assertEqual(len(p6), 31)
        self.assertEqual(p6, p5)

    def test_the_p5_messages_stay_first_and_unchanged_with_zones(self):
        learner = FakeLearner()
        learner.p5_zones = (zone_view("z0001", "Desk"), zone_view("l0001", ""))
        p5 = P5Layer(build_topics_p5(self.s), lambda *a, **k: True, None, debug_map=False).discovery()
        p6 = self.layer(learner).discovery()
        self.assertEqual(p6[:31], p5)
        self.assertEqual(len(p6), 31 + 12)
        self.assertEqual(len({t for t, _ in p6}), 43)

    def test_the_total_per_burst_is_the_p5_thirty_five_plus_six_per_zone(self):
        learner = FakeLearner()
        learner.p5_zones = tuple(zone_view("z%04d" % k, "Z%d" % k) for k in range(1, 4))
        p6 = self.layer(learner).discovery()
        self.assertEqual(len(p6) + 4, 35 + 18)

    def test_p5_discovery_still_has_no_zone_entity(self):
        for _, payload in p5_discovery_messages(build_topics_p5(self.s)):
            uid = json.loads(payload)["unique_id"]
            self.assertNotIn("radar_spot", uid)


if __name__ == "__main__":
    unittest.main()
