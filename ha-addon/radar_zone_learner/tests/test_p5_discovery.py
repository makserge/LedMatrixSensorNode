"""P5 discovery (check P5-10, spec 8.2): nineteen configs, key order, sizes, names, the changed P4 configs, debug_map."""
import json
import unittest
from pathlib import Path

from radar_zone_learner.shell.discovery import discovery_messages
from radar_zone_learner.shell.discovery_p4 import p4_discovery_messages
from radar_zone_learner.shell.discovery_p5 import (EVENT_TYPES_P5, advanced_discovery_message, p5_discovery_messages)
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.p5 import P5Layer
from radar_zone_learner.shell.review_p5 import advanced_label
from radar_zone_learner.shell.select_model import NO_ENTRY, NO_ITEM
from radar_zone_learner.shell.topics_p5 import build_topics_p5
from radar_zone_learner.core.types import AdvancedEntry
from tests.p5_harness import bed_item, place_item, window_item

DL = ('{"identifiers":["radar_zones_learner"],"name":"Radar zone learner","model":"radar_zone_learner app",'
      '"sw_version":"0.5.0"}')
DR = ('{"identifiers":["radar_zones_review"],"name":"Radar zone review","model":"radar_zone_learner app",'
      '"sw_version":"0.5.0","via_device":"radar_zones_learner"}')
OR = '{"name":"radar_zone_learner","sw_version":"0.5.0"}'
AV = '"availability_topic":"radar_zones/learner/status"'

# three configs byte for byte, as in spec 8.2 (macros expanded)
EXPECTED = {
    "homeassistant/image/radar_zones_review_map/config":
        '{"name":"Review map","unique_id":"radar_zones_review_map","default_entity_id":"image.radar_zones_review_map",'
        '"image_topic":"radar_zones/review/map","content_type":"image/png",' + AV + ',"device":' + DR + ',"origin":' + OR + '}',
    "homeassistant/sensor/radar_zones_review_push/config":
        '{"name":"Review push request","unique_id":"radar_zones_review_push","default_entity_id":'
        '"sensor.radar_zones_review_push","state_topic":"radar_zones/review/push","entity_category":"diagnostic",'
        '"icon":"mdi:bell-badge-outline",' + AV + ',"device":' + DR + ',"origin":' + OR + '}',
    "homeassistant/button/radar_zones_learner_forget_all/config":
        '{"name":"Forget all learned data","unique_id":"radar_zones_learner_forget_all","default_entity_id":'
        '"button.radar_zones_learner_forget_all","command_topic":"radar_zones/learner/forget_all",'
        '"payload_press":"PRESS","qos":1,"retain":false,"entity_category":"config","icon":"mdi:delete-forever",'
        + AV + ',"device":' + DL + ',"origin":' + OR + '}',
}

NEW_NAMES = ["Review map", "Review push map", "Advanced item", "Rename", "Apply advanced", "Show me live",
             "You are in", "Night window start (edit)", "Night window end (edit)", "Approve also switches schedule ON",
             "Review push request", "Forget all learned data", "Forget all confirmation", "Forget rejections",
             "Re-suggest night window", "Guests present - pause learning", "Pets in home", "Detailed review text",
             "Debug capture"]
P3_P4_NAMES = ["Learner status", "Learner debug map", "Mark spot here", "Spot name", "Radar data valid",
               "RF on time 24 h", "RF saving 7 d", "Coverage 7 d", "Night learning", "Review item", "Review pending",
               "Review item text", "Approve", "Reject", "Later", "Review event"]


def settings(**changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                  debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                  mqtt_password=None, mqtt_ssl=False, data_dir=Path("."))
    values.update(changes)
    return Settings(**values)


class P5DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.t = build_topics_p5(self.s)
        self.msgs = p5_discovery_messages(self.t)
        self.configs = {topic: json.loads(payload) for topic, payload in self.msgs}

    def test_three_payloads_byte_equal(self):
        got = dict(self.msgs)
        for topic, text in EXPECTED.items():
            with self.subTest(topic=topic):
                self.assertEqual(got[topic], text.encode("utf-8"))

    def test_nineteen_configs_eleven_review_eight_learner(self):
        self.assertEqual(len(self.msgs), 19)
        self.assertEqual(sorted(c["name"] for c in self.configs.values()), sorted(NEW_NAMES))
        review = [c for c in self.configs.values() if c["device"]["identifiers"] == ["radar_zones_review"]]
        learner = [c for c in self.configs.values() if c["device"]["identifiers"] == ["radar_zones_learner"]]
        self.assertEqual((len(review), len(learner)), (11, 8))
        self.assertEqual([m[0].split("/")[1] for m in self.msgs[:11]],
                         ["image", "image", "select", "button", "button", "switch", "sensor", "time", "time",
                          "switch", "sensor"])

    def test_ids_topics_and_availability(self):
        for topic, cfg in self.configs.items():
            component, object_id = topic.split("/")[1:3]
            self.assertEqual(cfg["unique_id"], object_id)
            self.assertEqual(cfg["default_entity_id"], component + "." + object_id)
            self.assertEqual(cfg["availability_topic"], "radar_zones/learner/status")
            self.assertEqual(cfg["origin"], {"name": "radar_zone_learner", "sw_version": "0.5.0"})
            self.assertEqual(cfg["device"]["sw_version"], "0.5.0")

    def test_key_order_follows_the_spec(self):
        def keys(uid):
            return list(self.configs[[t for t in self.configs if t.endswith("/%s/config" % uid)][0]])
        tail = ["availability_topic", "device", "origin"]
        self.assertEqual(keys("radar_zones_review_push_map"),
                         ["name", "unique_id", "default_entity_id", "image_topic", "content_type", "entity_category"] + tail)
        self.assertEqual(keys("radar_zones_review_advanced"),
                         ["name", "unique_id", "default_entity_id", "command_topic", "state_topic", "options",
                          "optimistic", "qos", "retain", "icon"] + tail)
        self.assertEqual(keys("radar_zones_review_rename"),
                         ["name", "unique_id", "default_entity_id", "command_topic", "payload_press", "qos", "retain",
                          "icon"] + tail)
        self.assertEqual(keys("radar_zones_review_show_live"),
                         ["name", "unique_id", "default_entity_id", "command_topic", "state_topic", "payload_on",
                          "payload_off", "state_on", "state_off", "optimistic", "qos", "retain", "icon"] + tail)
        self.assertEqual(keys("radar_zones_review_you_are_in"),
                         ["name", "unique_id", "default_entity_id", "state_topic", "json_attributes_topic", "icon"] + tail)
        self.assertEqual(keys("radar_zones_review_nw_start"),
                         ["name", "unique_id", "default_entity_id", "command_topic", "state_topic", "optimistic", "qos",
                          "retain", "icon"] + tail)
        self.assertEqual(keys("radar_zones_review_approve_sched"),
                         ["name", "unique_id", "default_entity_id", "command_topic", "state_topic", "payload_on",
                          "payload_off", "state_on", "state_off", "optimistic", "qos", "retain", "entity_category",
                          "icon"] + tail)
        self.assertEqual(keys("radar_zones_learner_forget_confirm"),
                         ["name", "unique_id", "default_entity_id", "command_topic", "state_topic", "min", "max", "mode",
                          "qos", "retain", "entity_category", "icon"] + tail)
        # the four learner switches have the keys of 'Night learning'
        night_learning = [c for t, c in ((m[0], json.loads(m[1])) for m in p4_discovery_messages(self.t))
                          if c["unique_id"] == "radar_zones_learner_night_learning"][0]
        for uid in ("guests_pause", "pets", "detail_text", "debug_capture"):
            self.assertEqual(keys("radar_zones_learner_" + uid), list(night_learning), uid)

    def test_entities_and_topics(self):
        c = self.configs
        base = "homeassistant/%s/radar_zones_%s/config"
        self.assertEqual(c[base % ("select", "review_advanced")]["command_topic"], "radar_zones/review/advanced/set")
        self.assertEqual(c[base % ("select", "review_advanced")]["state_topic"], "radar_zones/review/advanced")
        self.assertEqual(c[base % ("select", "review_advanced")]["options"], [NO_ENTRY])
        self.assertEqual(c[base % ("image", "review_push_map")]["image_topic"], "radar_zones/review/push_map")
        self.assertEqual(c[base % ("image", "review_push_map")]["entity_category"], "diagnostic")
        self.assertEqual(c[base % ("switch", "review_show_live")]["command_topic"], "radar_zones/review/live/set")
        self.assertEqual(c[base % ("sensor", "review_you_are_in")]["json_attributes_topic"],
                         "radar_zones/review/you_are_in/attr")
        self.assertEqual(c[base % ("time", "review_nw_start")]["command_topic"], "radar_zones/review/nw_start/set")
        self.assertEqual(c[base % ("time", "review_nw_end")]["state_topic"], "radar_zones/review/nw_end")
        self.assertEqual(c[base % ("switch", "review_approve_sched")]["entity_category"], "config")
        self.assertEqual(c[base % ("text", "learner_forget_confirm")]["max"], 16)
        for uid, topic in (("guests_pause", "radar_zones/learner/guests_pause"), ("pets", "radar_zones/learner/pets"),
                           ("detail_text", "radar_zones/learner/detail_text"),
                           ("debug_capture", "radar_zones/learner/debug_capture")):
            cfg = c[base % ("switch", "learner_" + uid)]
            self.assertEqual((cfg["command_topic"], cfg["state_topic"]), (topic + "/set", topic))
            self.assertEqual(cfg["entity_category"], "config")
            self.assertFalse(cfg["optimistic"])

    def test_buttons_post_json_answers(self):
        base = "homeassistant/button/radar_zones_%s/config"
        for uid, payload in (("review_rename", {"op": "rename"}), ("review_apply_advanced", {"op": "advanced"}),
                             ("learner_forget_rejections", {"op": "forget"}),
                             ("learner_resuggest_night_window", {"op": "undo", "item": "night_window"})):
            cfg = self.configs[base % uid]
            self.assertEqual(cfg["command_topic"], "radar_zones/review/answer")
            self.assertEqual(json.loads(cfg["payload_press"]), payload)
            self.assertEqual(cfg["payload_press"], json.dumps(payload, separators=(",", ":")))
        self.assertEqual(self.configs[base % "learner_forget_all"]["command_topic"], "radar_zones/learner/forget_all")
        self.assertEqual(self.configs[base % "learner_forget_rejections"]["entity_category"], "config")
        self.assertNotIn("entity_category", self.configs[base % "review_rename"])

    def test_no_zone_or_occupancy_entity(self):
        for cfg in self.configs.values():
            rest = cfg["unique_id"].replace("radar_zones_", "")
            self.assertNotIn("occupancy", rest)
            self.assertNotIn("zone", rest)

    def test_sizes(self):
        for topic, payload in self.msgs:
            self.assertLess(len(payload), 4096, topic)
            if "advanced" not in topic:
                self.assertLess(len(payload), 1300, topic)

    def test_the_largest_advanced_select_fits(self):
        # 60 entries with the longest labels the verbs can make: the payload stays below 4,096 bytes
        names = ["x" * 24] * 60
        entries = []
        for k in range(60):
            verb = ("rename", "remove")[k % 2]
            entries.append(AdvancedEntry("adv:%s:z%04d" % (verb, k), verb, "zone", "z%04d" % k, names[k], "", "", 0))
        model = __import__("radar_zone_learner.shell.select_model", fromlist=["AdvancedModel"]).AdvancedModel()
        model.update(entries)
        self.assertEqual(len(model.options), 61)
        topic, payload = advanced_discovery_message(self.t, model.options)
        self.assertLess(len(payload), 4096)
        self.assertGreater(len(payload), 2500)
        # the longest single label
        longest = advanced_label(AdvancedEntry("adv:undo:nw", "undo", "night_window", "nw", "", "", "2026-01-14", 0))
        self.assertEqual(longest, "Undo: Night window denied until 2026-01-14")

    def test_the_largest_advanced_select_with_non_ascii_names_is_utf8_not_escaped(self):
        # zone names are the user's (Kueche with an umlaut, CJK): UTF-8 in the payload, 2 or 3 bytes a character
        # instead of a 6 byte escape. The add-on's 4,096 byte bound is for ASCII names; the limit that matters is Home
        # Assistant's 16 KiB for the attributes of an entity (its options), and the widest names stay far below it.
        model_cls = __import__("radar_zone_learner.shell.select_model", fromlist=["AdvancedModel"]).AdvancedModel
        for name in ("K\u00fcche-n\u00f6rdlich-\u00dcberlauf"[:24].ljust(24, "\u00e4"), "\u66f8" * 24):
            entries = []
            for k in range(60):
                verb = ("rename", "remove")[k % 2]
                entries.append(AdvancedEntry("adv:%s:z%04d" % (verb, k), verb, "zone", "z%04d" % k, name, "", "", 0))
            model = model_cls()
            model.update(entries)
            topic, payload = advanced_discovery_message(self.t, model.options)
            self.assertNotIn(b"\\u", payload)                               # no JSON escape of a character
            self.assertLess(len(payload), 8192)
            self.assertEqual(json.loads(payload.decode("utf-8"))["options"], list(model.options))
            self.assertTrue(any(name in o for o in model.options))

    def test_debug_map_false_removes_both_image_entities(self):
        msgs = dict(p5_discovery_messages(self.t, debug_map=False))
        self.assertEqual(msgs["homeassistant/image/radar_zones_review_map/config"], b"")
        self.assertEqual(msgs["homeassistant/image/radar_zones_review_push_map/config"], b"")
        self.assertNotEqual(msgs["homeassistant/sensor/radar_zones_review_push/config"], b"")
        self.assertEqual(len(msgs), 19)

    def test_discovery_prefix(self):
        t = build_topics_p5(settings(discovery_prefix="ha2"))
        self.assertTrue(all(topic.startswith("ha2/") for topic, _ in p5_discovery_messages(t)))


class ChangedP4ConfigsTest(unittest.TestCase):
    def setUp(self):
        self.t = build_topics_p5(settings())

    def test_the_defaults_are_the_p4_bytes(self):
        default = p4_discovery_messages(self.t)
        self.assertEqual(p4_discovery_messages(self.t, None, None), default)
        cfg = {topic: json.loads(p) for topic, p in default}
        self.assertEqual(cfg["homeassistant/select/radar_zones_review_select/config"]["options"],
                         ["None", "Night window", "Bed zone"])
        self.assertEqual(cfg["homeassistant/event/radar_zones_review_event/config"]["event_types"],
                         ["mark_failed", "device_not_confirmed", "restore_failed"])

    def test_the_select_options_and_event_types_are_replaceable(self):
        msgs = p4_discovery_messages(self.t, [NO_ITEM, "Night window", "Place A"], list(EVENT_TYPES_P5))
        cfg = {topic: json.loads(p) for topic, p in msgs}
        self.assertEqual(cfg["homeassistant/select/radar_zones_review_select/config"]["options"],
                         ["No item", "Night window", "Place A"])
        self.assertEqual(cfg["homeassistant/event/radar_zones_review_event/config"]["event_types"],
                         ["mark_failed", "device_not_confirmed", "restore_failed", "answer_ignored", "forget_refused",
                          "forgotten"])
        self.assertEqual(len(msgs), 12)
        # nothing else changes
        default = dict(p4_discovery_messages(self.t))
        for topic, payload in msgs:
            if "select/" not in topic and "event/" not in topic:
                self.assertEqual(payload, default[topic])


class LayerDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.t = build_topics_p5(self.s)

    def layer(self, **kw):
        return P5Layer(self.t, lambda *a, **k: True, None, **kw)

    def test_thirty_one_messages_and_the_service_four_make_thirty_five(self):
        msgs = self.layer().discovery()
        self.assertEqual(len(msgs), 31)
        self.assertEqual(len(msgs) + len(discovery_messages(self.s, self.t)), 35)
        configs = [json.loads(p) for _, p in msgs] + [json.loads(p) for _, p in discovery_messages(self.s, self.t)]
        uids = [c["unique_id"] for c in configs]
        names = [c["name"] for c in configs]
        self.assertEqual(len(set(uids)), 35)
        self.assertEqual(len(set(names)), 35)
        self.assertEqual(sorted(names), sorted(NEW_NAMES + P3_P4_NAMES))
        self.assertEqual(len({t for t, _ in msgs}), 31)

    def test_no_p3_or_p4_name_or_id_is_reused(self):
        p3 = {json.loads(p)["unique_id"] for _, p in discovery_messages(self.s, self.t)}
        p4 = {json.loads(p)["unique_id"] for _, p in p4_discovery_messages(self.t)}
        p5 = {json.loads(p)["unique_id"] for _, p in p5_discovery_messages(self.t)}
        self.assertFalse(p5 & (p3 | p4))

    def test_with_debug_map_false_three_of_the_thirty_five_are_empty(self):
        layer = self.layer(debug_map=False)
        s = settings(debug_map=False)
        msgs = layer.discovery() + discovery_messages(s, self.t)
        empty = [t for t, p in msgs if p == b""]
        self.assertEqual(sorted(empty), sorted(["homeassistant/image/radar_zones_review_map/config",
                                                "homeassistant/image/radar_zones_review_push_map/config",
                                                "homeassistant/image/radar_zones_learner_debug_map/config"]))
        self.assertEqual(len(msgs), 35)

    def test_the_select_options_follow_the_items_in_both_modes(self):
        layer = self.layer()
        layer._set_items([window_item(), bed_item(), place_item("s0005-1", tag="A")], "night_window")
        cfg = {t: json.loads(p) for t, p in layer.discovery()}
        self.assertEqual(cfg["homeassistant/select/radar_zones_review_select/config"]["options"],
                         ["No item", "Night window", "Bed zone", "Place A"])
        slots = self.layer(select_mode="slots")
        slots._set_items([window_item(), bed_item(), place_item("s0005-1", tag="A")], "night_window")
        cfg = {t: json.loads(p) for t, p in slots.discovery()}
        options = cfg["homeassistant/select/radar_zones_review_select/config"]["options"]
        self.assertEqual(options, ["No item", "Night window", "Bed zone"] + ["Place %d" % k for k in range(1, 13)])
        self.assertEqual(len(options), 15)
        self.assertLess(len(dict(slots.discovery())["homeassistant/select/radar_zones_review_select/config"]), 1300)

    def test_the_event_types_are_the_six(self):
        cfg = {t: json.loads(p) for t, p in self.layer().discovery()}
        self.assertEqual(cfg["homeassistant/event/radar_zones_review_event/config"]["event_types"],
                         list(EVENT_TYPES_P5))
        self.assertEqual(len(EVENT_TYPES_P5), 6)


if __name__ == "__main__":
    unittest.main()
