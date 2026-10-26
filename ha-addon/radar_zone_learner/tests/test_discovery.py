import json
import unittest
from pathlib import Path

from radar_zone_learner.shell.discovery import discovery_messages
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.topics import build_topics

# Spec 9.2, byte for byte.
EXPECTED = {
    "homeassistant/sensor/radar_zones_learner_status/config":
        '{"name":"Learner status","unique_id":"radar_zones_learner_status","default_entity_id":"sensor.radar_zones_learner_status","state_topic":"radar_zones/learner/state","json_attributes_topic":"radar_zones/learner/attr","device_class":"enum","options":["starting","learning","not_observing","no_data"],"entity_category":"diagnostic","icon":"mdi:radar","availability_topic":"radar_zones/learner/status","device":{"identifiers":["radar_zones_learner"],"name":"Radar zone learner","model":"radar_zone_learner app","sw_version":"0.4.0"},"origin":{"name":"radar_zone_learner","sw_version":"0.4.0"}}',
    "homeassistant/image/radar_zones_learner_debug_map/config":
        '{"name":"Learner debug map","unique_id":"radar_zones_learner_debug_map","default_entity_id":"image.radar_zones_learner_debug_map","image_topic":"radar_zones/learner/debug_map","content_type":"image/png","entity_category":"diagnostic","availability_topic":"radar_zones/learner/status","device":{"identifiers":["radar_zones_learner"],"name":"Radar zone learner","model":"radar_zone_learner app","sw_version":"0.4.0"},"origin":{"name":"radar_zone_learner","sw_version":"0.4.0"}}',
    "homeassistant/button/radar_zones_review_mark/config":
        '{"name":"Mark spot here","unique_id":"radar_zones_review_mark","default_entity_id":"button.radar_zones_review_mark","command_topic":"radar_zones/review/mark","payload_press":"PRESS","qos":1,"retain":false,"icon":"mdi:map-marker-plus","availability_topic":"radar_zones/learner/status","device":{"identifiers":["radar_zones_review"],"name":"Radar zone review","model":"radar_zone_learner app","sw_version":"0.4.0","via_device":"radar_zones_learner"},"origin":{"name":"radar_zone_learner","sw_version":"0.4.0"}}',
    "homeassistant/text/radar_zones_review_name/config":
        '{"name":"Spot name","unique_id":"radar_zones_review_name","default_entity_id":"text.radar_zones_review_name","command_topic":"radar_zones/review/name/set","state_topic":"radar_zones/review/name","min":0,"max":32,"mode":"text","qos":1,"retain":false,"icon":"mdi:form-textbox","availability_topic":"radar_zones/learner/status","device":{"identifiers":["radar_zones_review"],"name":"Radar zone review","model":"radar_zone_learner app","sw_version":"0.4.0","via_device":"radar_zones_learner"},"origin":{"name":"radar_zone_learner","sw_version":"0.4.0"}}',
}


def settings(**changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                  debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                  mqtt_password=None, mqtt_ssl=False, data_dir=Path("."))
    values.update(changes)
    return Settings(**values)


class DiscoveryTest(unittest.TestCase):
    def test_payloads_byte_equal_to_spec(self):
        s = settings()
        msgs = discovery_messages(s, build_topics(s))
        self.assertEqual([t for t, _ in msgs], list(EXPECTED))
        for topic, payload in msgs:
            with self.subTest(topic=topic):
                self.assertEqual(payload, EXPECTED[topic].encode("utf-8"))
                json.loads(payload)

    def test_debug_map_off_omits_the_image(self):
        s = settings(debug_map=False)
        msgs = dict(discovery_messages(s, build_topics(s)))
        image_topic = "homeassistant/image/radar_zones_learner_debug_map/config"
        # no image config; an empty payload removes an image entity announced before
        self.assertEqual(msgs[image_topic], b"")
        self.assertEqual(sum(1 for p in msgs.values() if p), 3)

    def test_prefixes(self):
        s = settings(discovery_prefix="ha2", node_prefix="node_b")
        topics = build_topics(s)
        msgs = discovery_messages(s, topics)
        self.assertTrue(all(t.startswith("ha2/") for t, _ in msgs))
        self.assertEqual(topics.frames, "node_b/radar/ld2454/frames")
        self.assertEqual(topics.ha_status, "homeassistant/status")      # HA's birth topic does not follow the prefix
        # the app's own topics do not depend on the node prefix
        self.assertEqual(topics.learner_status, "radar_zones/learner/status")
        self.assertFalse(any(t.startswith("node_b/") or t.startswith("led_matrix_node/") for t, _ in msgs))


if __name__ == "__main__":
    unittest.main()
