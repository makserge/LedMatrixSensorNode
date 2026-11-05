"""The spot-light blueprint (check P6-13, P6 spec 9, Appendix B.3): no YAML parser in the standard library, so these are text
checks and a structural check of the top-level keys and of the ``!input`` names, like ``test_p5_blueprint.py``. Text checks
cannot validate YAML or Home Assistant's schema: acceptance A1 (the owner loads the blueprint and traces each trigger) is the
validation; the ``for: seconds: !input`` form and the templated conditions and ``data`` are unverified."""
import re
import unittest
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]
BLUEPRINT = ADDON / "blueprints" / "radar_zone_spot_light.yaml"
INPUTS = ["zone", "light", "scene_on", "scene_off", "off_delay", "brightness_pct", "off_after_unavailable"]


def code(text):
    """The text without full-line comments."""
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("#"))


def section(text, key):
    """The lines of one top-level key, up to the next top-level key."""
    out, on = [], False
    for line in text.split("\n"):
        if re.match(r"^[A-Za-z_]+:", line):
            on = line.startswith(key + ":")
        if on:
            out.append(line)
    return "\n".join(out)


class BlueprintTest(unittest.TestCase):
    def setUp(self):
        self.text = BLUEPRINT.read_bytes().decode("utf-8")
        self.code = code(self.text)
        self.head = self.text.split("\nblueprint:\n")[0]

    # ------------------------------------------------------------ file form and header
    def test_file_form(self):
        self.assertNotIn("\r", self.text)
        self.assertNotIn("\t", self.text)
        self.assertTrue(self.text.endswith("\n"))
        self.assertFalse(self.text.endswith("\n\n"))
        self.assertIn("blueprint:\n  name: Radar zone learner - spot occupied -> light/scene\n", self.text)
        self.assertIn("  domain: automation\n", self.text)
        self.assertIn("  homeassistant:\n    min_version: 2024.10.0\n", self.text)
        self.assertTrue(self.text.startswith("# Radar Zone Learner: Radar spot occupied -> light/scene"))

    def test_indentation_is_two_spaces(self):
        for n, line in enumerate(self.text.split("\n"), 1):
            if line.strip() and not line.lstrip().startswith("#"):
                self.assertEqual((len(line) - len(line.lstrip(" "))) % 2, 0, "line %d" % n)

    def test_the_header_says_where_to_copy_it_one_automation_per_spot_and_that_it_never_ran(self):
        self.assertIn("<config>/blueprints/automation/radar_zone_learner/radar_zone_spot_light.yaml", self.head)
        self.assertIn("Create one automation per spot", self.head)
        self.assertIn("It is not an entity and not part of the app", self.head)
        self.assertIn("has not been loaded by Home Assistant yet", self.head)
        self.assertIn("The app has no access to\n# Home Assistant's services", self.head)

    def test_the_header_states_the_stuck_light_consequence(self):
        self.assertIn("STUCK LIGHT", self.head)
        self.assertIn("the light stays on until someone switches it", self.head)
        self.assertIn("unavailable -> off", self.head)
        self.assertIn("off by default", self.head)
        self.assertIn("NOT from unavailable or unknown", self.head)

    def test_no_attribution_or_author_line(self):
        for word in ("Claude", "Co-Authored", "Generated with", "Author", "author:"):
            self.assertNotIn(word, self.text)

    # ------------------------------------------------------------ structure
    def test_the_top_level_keys(self):
        keys = re.findall(r"^([A-Za-z_]+):", self.code, re.M)
        self.assertEqual(keys, ["blueprint", "mode", "variables", "triggers", "actions"])
        for key in ("blueprint", "triggers", "actions", "mode"):
            self.assertIn(key, keys)

    def test_mode_restart(self):
        self.assertIn("\nmode: restart\n", self.text)

    def test_the_seven_inputs_are_declared_once_each_under_blueprint_input(self):
        bp = section(self.code, "blueprint")
        declared = re.findall(r"^    ([a-z_]+):$", bp.split("  input:\n")[1], re.M)
        self.assertEqual([d for d in declared if d in INPUTS], INPUTS)
        self.assertEqual(len(set(INPUTS)), 7)

    def test_every_input_name_used_is_declared_and_every_declared_input_is_used(self):
        bp = section(self.code, "blueprint")
        declared = set(re.findall(r"^    ([a-z_]+):$", bp.split("  input:\n")[1], re.M))
        used = re.findall(r"!input ([a-z_]+)", self.code)
        self.assertTrue(set(used) <= declared, set(used) - declared)
        self.assertEqual(sorted(set(used)), sorted(INPUTS))
        variables = section(self.code, "variables")
        for name, var in (("light", "light_t"), ("scene_on", "scene_on_e"), ("scene_off", "scene_off_e"),
                          ("brightness_pct", "brightness_pct"), ("off_after_unavailable", "off_after_unavailable")):
            self.assertIn("  %s: !input %s\n" % (var, name), variables + "\n")

    def test_input_selectors_and_defaults(self):
        bp = section(self.code, "blueprint")
        self.assertIn("      selector:\n        entity:\n          domain: binary_sensor\n          device_class: occupancy\n", bp)
        self.assertIn("      default: {}\n      selector:\n        target:\n          entity:\n            domain: light\n", bp)
        self.assertEqual(bp.count('      default: ""\n      selector:\n        entity:\n          domain: scene\n'), 2)
        self.assertIn("      default: 60\n", bp)
        self.assertIn("      default: 0\n", bp)
        self.assertIn("      default: false\n      selector:\n        boolean:\n", bp)           # the opt-in is off
        self.assertEqual(bp.count("default:"), 6)                  # the zone has none: it is required

    # ------------------------------------------------------------ the triggers
    def triggers(self):
        return section(self.code, "triggers")

    def test_three_state_triggers_on_the_zone(self):
        trg = self.triggers()
        self.assertEqual(len(re.findall(r"^  - trigger: state$", trg, re.M)), 3)
        self.assertEqual(trg.count("    entity_id: !input zone\n"), 3)
        self.assertEqual([m for m in re.findall(r"^    id: (\w+)$", trg, re.M)], ["occupied", "clear", "back_clear"])

    def test_the_on_trigger_has_to_on_and_no_from_or_not_from(self):
        first = self.triggers().split("  - trigger: state\n")[1]
        self.assertIn('    to: "on"\n', first)
        self.assertNotIn("not_from", first)
        self.assertNotIn("from:", first)
        self.assertNotIn("for:", first)                            # on at once, also from unavailable

    def test_the_off_trigger_has_to_off_not_from_unavailable_and_unknown_and_for_the_input(self):
        second = self.triggers().split("  - trigger: state\n")[2]
        self.assertIn('    to: "off"\n', second)
        self.assertRegex(second, r'    not_from:\n      - "unavailable"\n      - "unknown"\n')
        self.assertIn("    for:\n      seconds: !input off_delay\n", second)
        self.assertNotIn("\n    from:", second)
        self.assertIn("    id: clear\n", second)

    def test_the_back_clear_trigger_is_from_unavailable_to_off_and_conditional_on_its_input(self):
        third = self.triggers().split("  - trigger: state\n")[3]
        self.assertIn('    from: "unavailable"\n', third)
        self.assertIn('    to: "off"\n', third)
        self.assertIn("    for:\n      seconds: !input off_delay\n", third)
        self.assertIn("    id: back_clear\n", third)
        # the input decides in the action: the trigger itself fires, the condition lets it through only when the input is on
        self.assertIn("trigger.id == 'back_clear' and off_after_unavailable", self.code)
        self.assertEqual(self.code.count("off_after_unavailable"), 4)      # the input, its variable (twice), the one use

    def test_nothing_acts_on_unavailable_itself(self):
        trg = self.triggers()
        self.assertNotIn('to: "unavailable"', trg)
        self.assertNotIn("to: unavailable", trg)
        acts = section(self.code, "actions")
        for quoted in ("'unavailable'", '"unavailable"', "== unavailable", "states("):
            self.assertNotIn(quoted, acts)                          # only the input's name mentions it

    # ------------------------------------------------------------ the actions
    def actions(self):
        return section(self.code, "actions")

    def test_a_light_and_a_scene_action_for_the_on_and_for_the_off_path(self):
        act = self.actions()
        on_part, off_part = act.split("- conditions:")[1], act.split("- conditions:")[2]
        self.assertIn("{{ trigger.id == 'occupied' }}", on_part)
        self.assertIn("action: light.turn_on", on_part)
        self.assertIn("action: scene.turn_on", on_part)
        self.assertIn('entity_id: "{{ scene_on_e }}"', on_part)
        self.assertIn("action: light.turn_off", off_part)
        self.assertIn("action: scene.turn_on", off_part)
        self.assertIn('entity_id: "{{ scene_off_e }}"', off_part)
        self.assertEqual(act.count("target: !input light"), 2)

    def test_the_light_and_scene_actions_are_conditional_on_their_inputs(self):
        act = self.actions()
        self.assertEqual(act.count('value_template: "{{ light_t | length > 0 }}"'), 2)
        self.assertEqual(act.count('value_template: "{{ scene_on_e != \'\' }}"'), 1)
        self.assertEqual(act.count('value_template: "{{ scene_off_e != \'\' }}"'), 1)
        self.assertIn("brightness_pct | int(0) > 0", act)
        self.assertEqual(len(re.findall(r"^\s+- if:$", act, re.M)), 4)

    def test_the_clear_path_runs_for_clear_and_for_a_permitted_back_clear_only(self):
        self.assertIn("{{ trigger.id == 'clear' or (trigger.id == 'back_clear' and off_after_unavailable) }}", self.code)

    def test_no_other_service_is_called(self):
        called = sorted(set(re.findall(r"^\s+- action: (\S+)$", self.code, re.M)))
        self.assertEqual(called, ["light.turn_off", "light.turn_on", "scene.turn_on"])
        self.assertNotIn("mqtt.publish", self.code)

    # ------------------------------------------------------------ tidiness
    def test_jinja_tags_are_balanced_and_every_template_value_is_quoted_or_folded(self):
        self.assertEqual(self.code.count("{{"), self.code.count("}}"))
        for line in self.code.split("\n"):
            if re.match(r"^\s*[a-z_A-Z]+: \{\{", line):
                self.fail("an unquoted template value: %r" % line)

    def test_the_two_other_blueprints_are_untouched(self):
        for name, title in (("radar_zone_context.yaml", b"name: Radar zone learner - home context"),
                            ("radar_zone_review_push.yaml", b"name: Radar zone learner - review notifications")):
            data = (ADDON / "blueprints" / name).read_bytes()
            self.assertNotIn(b"\r", data)
            self.assertIn(title, data)

    def test_the_blueprint_is_not_named_like_a_manifest(self):
        self.assertNotIn(BLUEPRINT.name, ("config.yaml", "config.yml", "config.json"))


if __name__ == "__main__":
    unittest.main()
