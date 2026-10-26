"""The context blueprint (no YAML parser in the standard library: structural checks on the text)."""
import re
import unittest
from pathlib import Path

from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.topics_p4 import build_topics_p4

ADDON = Path(__file__).resolve().parents[1]
BLUEPRINT = ADDON / "blueprints" / "radar_zone_context.yaml"


class BlueprintTest(unittest.TestCase):
    def setUp(self):
        self.text = BLUEPRINT.read_bytes().decode("utf-8")
        s = Settings(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                     debug_map=True, mqtt_host="h", mqtt_port=1883, mqtt_username=None, mqtt_password=None,
                     mqtt_ssl=False, data_dir=Path("."))
        self.topics = build_topics_p4(s)

    def test_file_form(self):
        self.assertNotIn("\r", self.text)
        self.assertNotIn("\t", self.text)
        self.assertTrue(self.text.endswith("\n"))
        self.assertIn("blueprint:\n  name: Radar zone learner - home context\n", self.text)
        self.assertIn("  domain: automation\n", self.text)
        self.assertIn("    min_version: 2024.10.0\n", self.text)

    def test_every_input_is_declared_and_used(self):
        declared = re.findall(r"^    ([a-z_]+_entities):$", self.text, re.M)
        self.assertEqual(declared, ["home_empty_entities", "guests_entities", "lights_off_entities",
                                    "sleep_hint_entities"])
        used = re.findall(r"!input ([a-z_]+)", self.text)
        self.assertEqual(sorted(set(used)), sorted(declared))
        # each input is read once into a variable and once by its trigger
        for name in declared:
            self.assertEqual(used.count(name), 2, name)
            self.assertIn("      default: []\n", self.text)
        self.assertEqual(self.text.count("          multiple: true\n"), 4)

    def test_publishes_to_the_topics_the_app_subscribes_to(self):
        published = re.findall(r"^          topic: (\S+)$", self.text, re.M)
        self.assertEqual(published, ["radar_zones/context/home_empty", "radar_zones/context/guests",
                                     "radar_zones/context/lights_off", "radar_zones/context/sleep_hint"])
        subscribed = {topic for topic, _ in self.topics.subscriptions()}
        for topic in published:
            self.assertIn(topic, subscribed)
        self.assertEqual(self.text.count("          retain: true\n"), 4)
        self.assertEqual(self.text.count("          qos: 1\n"), 4)
        self.assertEqual(self.text.count("action: mqtt.publish"), 4)

    def test_a_context_without_entities_is_not_published(self):
        conditions = re.findall(r'value_template: "\{\{ (\w+) \| length > 0 \}\}"', self.text)
        self.assertEqual(conditions, ["home_empty_entities", "guests_entities", "lights_off_entities",
                                      "sleep_hint_entities"])

    def test_payloads_are_the_values_the_app_parses(self):
        # the app reads on/off (and an empty message as cleared): nothing else may be published
        values = set(re.findall(r"'(on|off|)'", self.text))
        self.assertTrue(values <= {"on", "off", ""})
        self.assertIn("{{ '' if ns.unknown else ('on' if ns.empty else 'off') }}", self.text)

    def test_jinja_tags_are_balanced(self):
        self.assertEqual(self.text.count("{%"), self.text.count("{%-"))      # whitespace control everywhere
        self.assertEqual(self.text.count("%}"), self.text.count("-%}"))
        for tag in ("for", "if"):
            self.assertEqual(len(re.findall(r"\{%%- *%s " % tag, self.text)),
                             len(re.findall(r"\{%%- *end%s " % tag, self.text)), tag)


if __name__ == "__main__":
    unittest.main()
