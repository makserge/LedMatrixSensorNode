"""The review push blueprint (P5 spec 6.6, 11.4a, P5-3 a): no YAML parser in the standard library, so these are text checks,
like ``test_blueprint.py``. The logic is checked against the Python port in ``tests/fake_ha.py``."""
import re
import unittest
from pathlib import Path

from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.topics_p5 import build_topics_p5

ADDON = Path(__file__).resolve().parents[1]
BLUEPRINT = ADDON / "blueprints" / "radar_zone_review_push.yaml"
INPUTS = ["notify_device", "push_entity", "map_entity", "open_path", "detailed_text"]


def code(text):
    """The text without full-line comments."""
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("#"))


class BlueprintTest(unittest.TestCase):
    def setUp(self):
        self.text = BLUEPRINT.read_bytes().decode("utf-8")
        self.code = code(self.text)
        s = Settings(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                     debug_map=True, mqtt_host="h", mqtt_port=1883, mqtt_username=None, mqtt_password=None,
                     mqtt_ssl=False, data_dir=Path("."))
        self.topics = build_topics_p5(s)

    # ------------------------------------------------------------ file form and header
    def test_file_form(self):
        self.assertNotIn("\r", self.text)
        self.assertNotIn("\t", self.text)
        self.assertTrue(self.text.endswith("\n"))
        self.assertFalse(self.text.endswith("\n\n"))
        self.assertIn("blueprint:\n  name: Radar zone learner - review notifications\n", self.text)
        self.assertIn("  domain: automation\n", self.text)
        self.assertIn("  homeassistant:\n    min_version: 2024.10.0\n", self.text)
        self.assertTrue(self.text.startswith("# Radar Zone Learner: review notifications"))

    def test_the_header_says_where_to_copy_it_and_that_it_never_ran(self):
        head = self.text.split("\nblueprint:\n")[0]
        self.assertIn("<config>/blueprints/automation/radar_zone_learner/radar_zone_review_push.yaml", head)
        self.assertIn("Create one automation per phone", head)
        self.assertIn("has not been loaded by Home Assistant yet", head)

    def test_mode_queued(self):
        self.assertIn("\nmode: queued\nmax: 10\nmax_exceeded: silent\n", self.text)

    def test_no_continue_on_error_anywhere(self):
        self.assertNotIn("continue_on_error", self.text)

    # ------------------------------------------------------------ inputs
    def test_the_five_inputs_are_declared_once_each(self):
        declared = re.findall(r"^    ([a-z_]+):$", self.text.split("\nmode: queued")[0].split("  input:\n")[1], re.M)
        names = [d for d in declared if d in INPUTS]
        self.assertEqual(names, INPUTS)
        self.assertEqual(len(set(INPUTS)), 5)

    def test_input_selectors_and_defaults(self):
        head = self.text.split("\nmode: queued")[0]
        self.assertIn("      selector:\n        device:\n          filter:\n            - integration: mobile_app\n", head)
        self.assertIn("      default: sensor.radar_zones_review_push\n", head)
        self.assertIn("      default: image.radar_zones_review_push_map\n", head)
        self.assertIn("      default: /config/devices/dashboard\n", head)
        self.assertIn("      default: false\n      selector:\n        boolean:\n", head)          # the opt-in is off
        self.assertEqual(head.count("domain: sensor\n"), 1)
        self.assertEqual(head.count("domain: image\n"), 1)

    def test_every_input_is_used_where_the_reference_text_uses_it(self):
        used = re.findall(r"!input ([a-z_]+)", self.code)
        self.assertEqual(sorted(set(used)), sorted(INPUTS))
        variables = self.code.split("\nvariables:\n")[1].split("\ntriggers:")[0]
        for name in ("push_entity", "map_entity", "open_path", "detailed_text"):
            self.assertIn("  %s: !input %s\n" % (name, name), variables)
        self.assertIn("  notify_device_id: !input notify_device\n", variables)
        # the trigger and the device actions use the inputs directly
        triggers = self.code.split("\ntriggers:\n")[1].split("\nactions:")[0]
        self.assertIn("    entity_id: !input push_entity\n", triggers)
        self.assertEqual(len(re.findall(r"^\s+- device_id: !input notify_device$", self.code, re.M)), 3)   # clear, notify, notify
        self.assertEqual(used.count("push_entity"), 2)
        self.assertEqual(used.count("notify_device"), 4)

    def test_the_phone_id_is_p_and_seven_characters(self):
        self.assertIn("  me: \"{{ 'p' ~ notify_device_id[:7] }}\"\n", self.text)
        # the leading letter: a rendered id of digits or like 1e45 would become a number
        self.assertNotIn("me: \"{{ notify_device_id[:8] }}\"", self.text)

    # ------------------------------------------------------------ triggers
    def test_triggers(self):
        triggers = self.code.split("\ntriggers:\n")[1].split("\nactions:")[0]
        self.assertEqual(triggers.rstrip("\n"),
                         "  - trigger: state\n    entity_id: !input push_entity\n    id: request\n"
                         "  - trigger: event\n    event_type: mobile_app_notification_action\n    id: action")
        for key in ("from:", "to:", "not_from:", "not_to:", "event_data:", "attribute:", "for:"):
            self.assertNotIn(key, triggers)

    # ------------------------------------------------------------ the request branch
    def test_the_run_start_check_and_the_re_check_test_this_phone_and_never_seq_acked(self):
        self.assertNotIn("acked", self.code)
        self.assertRegex(self.text, r"snap\[0\] \| int > 0 and snap\[2\] in \['setup', 'daily'\] and snap\[3\] \| int > 0 "
                                    r"and me not in snap\[6\]\.split\(','\)")
        self.assertRegex(self.text, r"cur\[0\] == snap\[0\] and cur\[2\] in \['setup', 'daily'\] and cur\[3\] \| int\(0\) > 0 "
                                    r"and me not in cur\[6\]\.split\(','\)")
        self.assertEqual(self.code.count("me not in"), 2)
        self.assertNotRegex(self.code, r"snap\[[0-9]\] > snap|cur\[[0-9]\] > cur|\[0\] \| int > .*\[1\]")

    def test_the_state_has_eight_fields_and_is_checked(self):
        self.assertIn("snap | length == 8", self.text)
        self.assertIn("cur | length == 8", self.text)
        self.assertIn("(trigger.to_state.state if trigger.to_state is not none else '').split('|')", self.text)
        self.assertIn("{{ states(push_entity).split('|') }}", self.text)

    def test_the_delay_is_fifteen_seconds_between_the_two_checks(self):
        self.assertEqual(self.code.count("delay:"), 1)
        self.assertIn('                  - delay: "00:00:15"\n', self.text)
        at = self.text.index('delay: "00:00:15"')
        self.assertLess(self.text.index("me not in snap[6]"), at)
        self.assertGreater(self.text.index("me not in cur[6]"), at)

    def test_the_clear_branch_needs_seq_above_zero(self):
        self.assertIn("{{ snap[3] | int == 0 and snap[0] | int > 0 }}", self.text)
        clear = self.text.split("snap[3] | int == 0 and snap[0] | int > 0")[1].split("- conditions:")[0]
        self.assertIn("message: clear_notification", clear)
        self.assertIn("tag: \"{{ tag }}\"", clear)

    def test_the_ack_is_seq_and_me_with_qos_one_not_retained(self):
        self.assertRegex(self.text, r'topic: radar_zones/review/push_ack\n\s+payload: "\{\{ seq \}\}:\{\{ me \}\}"\n'
                                    r'\s+qos: 1\n\s+retain: false\n')
        i = self.text.index("topic: radar_zones/review/push_ack")
        last_notify = self.text.rindex("type: notify", 0, i)
        self.assertLess(last_notify, i)                                                       # after the notify steps
        self.assertLess(self.text.rindex("tag: \"{{ tag }}\"", 0, i), i)

    # ------------------------------------------------------------ the notification
    def test_the_notification_data_keys(self):
        # the clear step and the two notify steps (with and without actions) carry the tag; the two notify steps the rest
        self.assertEqual(self.code.count('tag: "{{ tag }}"'), 3)
        for line in ("group: radar_zone_review", "alert_once: true", 'clickAction: "{{ open_path }}"',
                     'url: "{{ open_path }}"', 'image: "/api/image_proxy/{{ map_entity }}"'):
            self.assertEqual(self.code.count(line), 2, line)
        self.assertIn("  tag: radar_zone_review\n", self.text)
        self.assertEqual(self.code.count("title: Radar zone review"), 2)
        self.assertEqual(self.code.count("domain: mobile_app\n"), 3)
        self.assertEqual(self.code.count("type: notify\n"), 3)

    def test_exactly_three_actions_in_the_fixed_order_with_fixed_titles(self):
        actions = re.findall(r'- action: "(RZ_[A-Z]+_\{\{ rev \}\}_\{\{ item \}\})"\n\s+title: ([A-Za-z]+)', self.text)
        self.assertEqual(actions, [("RZ_APPROVE_{{ rev }}_{{ item }}", "Approve"), ("RZ_RENAME_{{ rev }}_{{ item }}", "Rename"),
                                   ("RZ_REJECT_{{ rev }}_{{ item }}", "Reject")])
        self.assertEqual(self.code.count('action: "RZ_'), 3)
        self.assertEqual(self.code.count("behavior: textInput"), 1)
        rename = self.text.split('action: "RZ_RENAME_')[1].split("- action:")[0]
        self.assertIn("behavior: textInput", rename)

    def test_the_actions_are_left_out_when_there_is_no_item(self):
        self.assertIn("{{ item != '' and rev != '' }}", self.text)
        default = self.text.split("                    default:\n")[1].split("                  - action: mqtt.publish")[0]
        self.assertNotIn("actions:", default)
        self.assertIn("message: \"{{ message }}\"", default)

    def test_the_message_variables(self):
        self.assertIn("# BEGIN generic message\n", self.text)
        self.assertIn("# END generic message\n", self.text)
        generic = self.text.split("# BEGIN generic message\n")[1].split("# END generic message")[0]
        self.assertEqual(generic.count("message_generic:"), 1)
        outside_quotes = re.sub(r"'[^']*'", "''", generic)
        used = set(re.findall(r"\b(count|kind|seq|rev|item|cur|snap|states|me|detailed_text|acks)\b", outside_quotes))
        self.assertEqual(used, {"count", "kind"})                           # the generic text knows only these two
        self.assertIn("Radar setup review is ready (' ~ count ~ ' items)", generic)
        self.assertIn("Radar zone review: ' ~ count ~ ' item(s) to look at", generic)
        self.assertIn("message: \"{{ cur[7] if detailed_text and cur[7] | length > 0 else message_generic }}\"", self.text)

    def test_the_item_text_is_read_only_from_the_eighth_field_and_only_with_the_opt_in(self):
        self.assertEqual(self.code.count("states("), 1)                      # only the push entity itself
        self.assertNotIn("state_attr(", self.code)
        self.assertEqual(len(re.findall(r"cur\[7\]", self.code)), 2)
        self.assertEqual(len(re.findall(r"snap\[7\]", self.code)), 0)
        self.assertEqual(self.code.count("detailed_text"), 4)                # the input, its variable (twice), the one use
        self.assertEqual(len(re.findall(r"detailed_text and cur\[7\]", self.code)), 1)

    # ------------------------------------------------------------ the tap handler
    def test_the_answer_handler(self):
        self.assertIn("regex_match('^RZ_(APPROVE|RENAME|REJECT)_[0-9]+_.+$')", self.text)
        self.assertIn("parts: \"{{ trigger.event.data.action.split('_', 3) }}\"", self.text)
        self.assertIn("op: \"{{ parts[1] | lower }}\"", self.text)
        self.assertIn("rev: \"{{ parts[2] | int }}\"", self.text)
        self.assertIn("item: \"{{ parts[3] }}\"", self.text)
        self.assertIn("{'op': op, 'item': item, 'rev': rev, 'aid': trigger.event.context.id}", self.text)
        self.assertIn("{%- set nm = (trigger.event.data.reply_text | default('', true) | string | trim)[:32] -%}", self.text)
        self.assertIn("{%- if op == 'rename' and nm != '' -%}{%- set d = dict(d, name=nm) -%}{%- endif -%}", self.text)
        self.assertIn("{{ d | tojson }}", self.text)

    def test_the_name_is_built_inside_the_payload_template_not_in_variables(self):
        variables = self.code.split("\nvariables:\n")[1].split("\ntriggers:")[0]
        self.assertNotIn("name:", variables)
        self.assertNotIn("reply_text", variables)
        inner = self.code.split("\nactions:\n")[1]
        self.assertEqual(len(re.findall(r"^\s+name: ", inner, re.M)), 0)      # no `name:` entry under any variables:
        self.assertEqual(self.code.count("reply_text"), 1)

    def test_only_two_topics_are_published_and_the_app_subscribes_to_both(self):
        published = re.findall(r"^\s+topic: (\S+)$", self.code, re.M)
        self.assertEqual(published, ["radar_zones/review/push_ack", "radar_zones/review/answer"])
        subscribed = {topic for topic, _ in self.topics.subscriptions()}
        for topic in published:
            self.assertIn(topic, subscribed)
        self.assertEqual(self.code.count("action: mqtt.publish"), 2)
        self.assertEqual(len(re.findall(r"^\s+qos: 1$", self.code, re.M)), 2)
        self.assertEqual(len(re.findall(r"^\s+retain: false$", self.code, re.M)), 2)
        self.assertNotIn("retain: true", self.code)

    def test_the_answer_payload_keys_are_the_ones_the_parser_reads(self):
        from radar_zone_learner.shell.topics_p5 import parse_answer_p5
        import json
        sample = {"op": "rename", "item": "s0005-1", "rev": 3, "aid": "01HV9ZZ", "name": "Desk"}
        self.assertEqual(parse_answer_p5(json.dumps(sample).encode())["name"], "Desk")
        self.assertEqual(set(sample), {"op", "item", "rev", "aid", "name"})
        for key in sample:
            self.assertIn("'%s'" % key if key != "name" else "name=nm", self.text)

    # ------------------------------------------------------------ privacy and tidiness
    def test_the_titles_and_texts_never_carry_a_time_or_a_name(self):
        for line in self.code.split("\n"):
            if re.match(r"^\s+(title|message): ", line):
                self.assertNotRegex(line, r"\b\d{1,2}:\d{2}\b")
        self.assertNotIn("zname", self.code)
        self.assertNotIn("name_hint", self.code)

    def test_jinja_tags_are_balanced(self):
        self.assertEqual(self.code.count("{{"), self.code.count("}}"))
        self.assertEqual(self.code.count("{%"), self.code.count("%}"))
        self.assertEqual(len(re.findall(r"\{%-? *if ", self.code)), len(re.findall(r"\{%-? *endif", self.code)))

    def test_every_template_value_is_quoted_or_folded(self):
        for line in self.code.split("\n"):
            stripped = line.strip()
            if re.match(r"^[a-z_A-Z]+: \{\{", stripped):
                self.fail("an unquoted template value: %r" % line)

    def test_indentation_is_two_spaces(self):
        for n, line in enumerate(self.text.split("\n"), 1):
            if line.strip() and not line.lstrip().startswith("#"):
                self.assertEqual((len(line) - len(line.lstrip(" "))) % 2, 0, "line %d" % n)

    def test_the_context_blueprint_is_untouched(self):
        context = (ADDON / "blueprints" / "radar_zone_context.yaml").read_bytes()
        self.assertNotIn(b"\r", context)
        self.assertIn(b"name: Radar zone learner - home context", context)


if __name__ == "__main__":
    unittest.main()
