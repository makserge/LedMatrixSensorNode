import json
import unittest
from pathlib import Path

from radar_zone_learner.shell.discovery import discovery_messages
from radar_zone_learner.shell.discovery_p4 import p4_discovery_messages
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.review import bed_text, hhmm, item_text, night_window_text, state_text
from radar_zone_learner.shell.p4_types import ITEM_BED, ITEM_NIGHT_WINDOW, ReviewItem
from radar_zone_learner.shell.topics_p4 import build_topics_p4

# The learner-device entities of design 6.3 that P4 adds, byte for byte (the P3 four are in test_discovery.py).
EXPECTED = {
    "homeassistant/binary_sensor/radar_zones_radar_valid/config":
        '{"name":"Radar data valid","unique_id":"radar_zones_radar_valid","default_entity_id":"binary_sensor.radar_zones_radar_valid","state_topic":"radar_zones/radar/valid","payload_on":"online","payload_off":"offline","entity_category":"diagnostic","icon":"mdi:radar","availability_topic":"radar_zones/learner/status","device":{"identifiers":["radar_zones_learner"],"name":"Radar zone learner","model":"radar_zone_learner app","sw_version":"0.7.0"},"origin":{"name":"radar_zone_learner","sw_version":"0.7.0"}}',
    "homeassistant/switch/radar_zones_learner_night_learning/config":
        '{"name":"Night learning","unique_id":"radar_zones_learner_night_learning","default_entity_id":"switch.radar_zones_learner_night_learning","command_topic":"radar_zones/learner/night_learning/set","state_topic":"radar_zones/learner/night_learning","payload_on":"ON","payload_off":"OFF","state_on":"ON","state_off":"OFF","optimistic":false,"qos":1,"retain":false,"entity_category":"config","icon":"mdi:weather-night","availability_topic":"radar_zones/learner/status","device":{"identifiers":["radar_zones_learner"],"name":"Radar zone learner","model":"radar_zone_learner app","sw_version":"0.7.0"},"origin":{"name":"radar_zone_learner","sw_version":"0.7.0"}}',
    "homeassistant/button/radar_zones_review_reject/config":
        '{"name":"Reject","unique_id":"radar_zones_review_reject","default_entity_id":"button.radar_zones_review_reject","command_topic":"radar_zones/review/answer","payload_press":"{\\"op\\":\\"reject\\"}","qos":1,"retain":false,"icon":"mdi:close","availability_topic":"radar_zones/learner/status","device":{"identifiers":["radar_zones_review"],"name":"Radar zone review","model":"radar_zone_learner app","sw_version":"0.7.0","via_device":"radar_zones_learner"},"origin":{"name":"radar_zone_learner","sw_version":"0.7.0"}}',
}


def settings(**changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                  debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                  mqtt_password=None, mqtt_ssl=False, data_dir=Path("."))
    values.update(changes)
    return Settings(**values)


class P4DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.t = build_topics_p4(self.s)
        self.msgs = p4_discovery_messages(self.t)
        self.configs = {topic: json.loads(payload) for topic, payload in self.msgs}

    def test_key_payloads_byte_equal(self):
        got = dict(self.msgs)
        for topic, text in EXPECTED.items():
            with self.subTest(topic=topic):
                self.assertEqual(got[topic], text.encode("utf-8"))

    def test_twelve_entities_and_the_expected_set(self):
        self.assertEqual(len(self.msgs), 12)
        names = sorted(c["name"] for c in self.configs.values())
        self.assertEqual(names, sorted(["Radar data valid", "RF on time 24 h", "RF saving 7 d", "Coverage 7 d",
                                        "Night learning", "Review item", "Review pending", "Review item text",
                                        "Approve", "Reject", "Later", "Review event"]))

    def test_ids_topics_and_availability(self):
        ids = set()
        for topic, cfg in self.configs.items():
            component, object_id = topic.split("/")[1:3]
            self.assertEqual(cfg["unique_id"], object_id)
            self.assertEqual(cfg["default_entity_id"], component + "." + object_id)
            self.assertEqual(cfg["availability_topic"], "radar_zones/learner/status")
            self.assertIn(cfg["device"]["identifiers"][0], ("radar_zones_learner", "radar_zones_review"))
            ids.add(cfg["unique_id"])
        self.assertEqual(len(ids), 12)

    def test_no_clash_with_the_p3_entities(self):
        p3 = {topic for topic, _ in discovery_messages(self.s, build_topics_p4(self.s))}
        self.assertEqual(len(p3), 4)
        self.assertFalse(p3 & {topic for topic, _ in self.msgs})

    def test_no_zone_entities_yet(self):
        # shadow mode until P5/P6: no zone, room occupancy or presence entity
        for cfg in self.configs.values():
            self.assertNotIn("occupancy", cfg["unique_id"])
            self.assertNotIn("zone", cfg["unique_id"].replace("radar_zones_", ""))

    def test_sensor_units(self):
        c = self.configs
        self.assertEqual(c["homeassistant/sensor/radar_zones_scan_rf_on_24h/config"]["unit_of_measurement"], "h")
        self.assertEqual(c["homeassistant/sensor/radar_zones_scan_rf_saving_7d/config"]["unit_of_measurement"], "%")
        self.assertEqual(c["homeassistant/sensor/radar_zones_learner_coverage_7d/config"]["state_topic"],
                         "radar_zones/learner/coverage_7d")

    def test_buttons_post_json_answers(self):
        for op in ("approve", "reject", "later"):
            cfg = self.configs["homeassistant/button/radar_zones_review_%s/config" % op]
            self.assertEqual(cfg["command_topic"], "radar_zones/review/answer")
            self.assertEqual(json.loads(cfg["payload_press"]), {"op": op})

    def test_select_options_match_the_mapper(self):
        cfg = self.configs["homeassistant/select/radar_zones_review_select/config"]
        self.assertEqual(cfg["options"], ["None", "Night window", "Bed zone"])
        self.assertEqual(cfg["command_topic"], "radar_zones/review/select/set")

    def test_event_types(self):
        cfg = self.configs["homeassistant/event/radar_zones_review_event/config"]
        self.assertEqual(cfg["event_types"], ["mark_failed", "device_not_confirmed", "restore_failed"])
        self.assertEqual(cfg["state_topic"], "radar_zones/review/event")

    def test_discovery_prefix(self):
        t = build_topics_p4(settings(discovery_prefix="ha2"))
        self.assertTrue(all(topic.startswith("ha2/") for topic, _ in p4_discovery_messages(t)))


class ReviewTextTest(unittest.TestCase):
    def test_hhmm(self):
        self.assertEqual(hhmm(0), "00:00")
        self.assertEqual(hhmm(1425), "23:45")
        self.assertEqual(hhmm(None), "?")

    def test_bed_text_with_the_schedule_on(self):
        text = bed_text(True, 0, 360)
        self.assertEqual(
            text, "Bed zone: lie in bed and press Mark spot here once, before 00:00 or after 06:00 "
                  "(recommended; no extra radar-on nights). Otherwise the learner finds it by itself, which "
                  "can take 2 weeks or more while the night schedule runs.")

    def test_bed_text_without_the_window_part_while_the_schedule_is_off_or_unknown(self):
        for sched in (False, None):
            text = bed_text(sched, 0, 360)
            self.assertNotIn("before", text)
            self.assertIn("press Mark spot here once (recommended; no extra radar-on nights)", text)

    def test_bed_text_defaults_to_the_default_window(self):
        self.assertIn("before 00:00 or after 06:00", bed_text(True, None, None))
        self.assertIn("before 23:30 or after 06:45", bed_text(True, 1410, 405))

    def test_night_window_text(self):
        text = night_window_text({"start_min": 1410, "end_min": 405, "saving_pct": 30})
        self.assertEqual(text, "Switch both radars off between 23:30 and 06:45. Radar on-time −30%. "
                               "No radar detection during that time.")
        late = night_window_text({"start_min": 1410, "end_min": 405, "late_nights": 3, "from_lights_off": True})
        self.assertIn("still seen in the 30 min before your current start on 3 of the last 14 nights", late)
        self.assertTrue(late.endswith("From lights-off times."))

    def test_ready_text_of_the_core_wins_for_the_night_window(self):
        item = ReviewItem(ITEM_NIGHT_WINDOW, 2, ITEM_NIGHT_WINDOW, {"text": "Custom", "start_min": 1})
        self.assertEqual(item_text(item, True, 0, 360), "Custom")

    def test_the_bed_item_is_rendered_from_the_live_window(self):
        # the core's text was made with the window of that moment; the schedule may have changed since
        bed = ReviewItem(ITEM_BED, 1, ITEM_BED, {"text": "old text before 22:00 or after 07:00", "sched": True})
        self.assertIn("once, before 00:00 or after 06:00 (recommended", item_text(bed, True, 0, 360))
        self.assertNotIn("before", item_text(bed, False, 0, 360))

    def test_state_text_is_cut_at_255(self):
        self.assertEqual(state_text("a" * 255), "a" * 255)
        cut = state_text("b" * 400)
        self.assertEqual(len(cut), 255)
        self.assertTrue(cut.endswith("…"))


if __name__ == "__main__":
    unittest.main()
