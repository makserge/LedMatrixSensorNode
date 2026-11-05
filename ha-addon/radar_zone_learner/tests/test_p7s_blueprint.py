"""The review push blueprint with the kind ``attention`` (P7 spec 6.5, check P7-11 shell part): no YAML parser in the standard
library, so these are text checks, like ``test_p5_blueprint.py``, plus the Python port of the blueprint in ``tests/fake_ha.py``.

Everything Home Assistant does with the blueprint is an ASSUMPTION: the blueprint was never loaded by Home Assistant, and that
the state trigger fires for the new kind exactly as for ``setup`` and ``daily`` is HA[U9] of the P7 spec (14.2), settled by
acceptance step A6. These tests prove only that the text and the port agree and that the request the layer writes is accepted
by both."""
import re
import unittest
from pathlib import Path

from radar_zone_learner.core.types import PushRequest
from radar_zone_learner.shell.discovery_p5 import p5_discovery_messages
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.review_p5 import push_state
from radar_zone_learner.shell.topics_p7 import build_topics_p7
from tests.fake_ha import FakeHa
from tests.fakes import FakeClock, FakeMqttClient

ADDON = Path(__file__).resolve().parents[1]
BLUEPRINT = ADDON / "blueprints" / "radar_zone_review_push.yaml"


def code(text):
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("#"))


class BlueprintTextTest(unittest.TestCase):
    def setUp(self):
        self.text = BLUEPRINT.read_bytes().decode("utf-8")
        self.code = code(self.text)

    def test_file_form_is_unchanged(self):
        self.assertNotIn("\r", self.text)
        self.assertNotIn("\t", self.text)
        self.assertTrue(self.text.endswith("\n"))
        self.assertFalse(self.text.endswith("\n\n"))
        self.assertIn("\nmode: queued\nmax: 10\nmax_exceeded: silent\n", self.text)

    def test_both_kind_checks_accept_the_three_kinds(self):
        self.assertIn("snap[2] in ['setup', 'daily', 'attention']", self.code)
        self.assertIn("cur[2] in ['setup', 'daily', 'attention']", self.code)
        self.assertEqual(self.code.count("['setup', 'daily', 'attention']"), 2)
        self.assertNotIn("['setup', 'daily']", self.code)

    def test_the_generic_message_for_attention(self):
        generic = self.text.split("# BEGIN generic message\n")[1].split("# END generic message")[0]
        self.assertIn("'Radar needs attention' if kind == 'attention'", generic)
        self.assertIn("'Radar setup review is ready (' ~ count ~ ' items)' if kind == 'setup'", generic)
        self.assertIn("'Radar zone review: ' ~ count ~ ' item(s) to look at'", generic)
        # the generic text still knows only the count and the kind
        outside_quotes = re.sub(r"'[^']*'", "''", generic)
        used = set(re.findall(r"\b(count|kind|seq|rev|item|cur|snap|states|me|detailed_text|acks)\b", outside_quotes))
        self.assertEqual(used, {"count", "kind"})

    def test_the_header_says_to_copy_it_again_and_that_the_kind_is_an_assumption(self):
        head = self.text.split("\nblueprint:\n")[0]
        self.assertIn("kind 'attention'", head)
        self.assertIn("copy this file again", head)
        self.assertIn("HA[U9]", head)
        self.assertIn("has not been loaded by Home Assistant yet", head)

    def test_no_name_time_or_distance_in_any_title_or_message(self):
        for line in self.code.split("\n"):
            if re.match(r"^\s+(title|message): ", line):
                self.assertNotRegex(line, r"\b\d{1,2}:\d{2}\b")
        self.assertNotIn("zname", self.code)
        self.assertNotIn("shift", self.code)
        self.assertNotIn("dist", self.code)

    def test_the_three_actions_the_ack_and_the_answer_handler_are_untouched(self):
        actions = re.findall(r'- action: "(RZ_[A-Z]+_\{\{ rev \}\}_\{\{ item \}\})"\n\s+title: ([A-Za-z]+)', self.text)
        self.assertEqual([t for _, t in actions], ["Approve", "Rename", "Reject"])
        self.assertIn("regex_match('^RZ_(APPROVE|RENAME|REJECT)_[0-9]+_.+$')", self.text)
        self.assertEqual(re.findall(r"^\s+topic: (\S+)$", self.code, re.M),
                         ["radar_zones/review/push_ack", "radar_zones/review/answer"])
        self.assertIn("{{ item != '' and rev != '' }}", self.text)          # an attention request has no item: no actions


class AttentionFlowTest(unittest.TestCase):
    """The port of the blueprint (``tests/fake_ha.py``) fed with the state the shell writes for a radar-moved request."""

    def setUp(self):
        s = Settings(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant", debug_map=True,
                     mqtt_host="h", mqtt_port=1883, mqtt_username=None, mqtt_password=None, mqtt_ssl=False,
                     data_dir=Path("."))
        self.t = build_topics_p7(s)
        self.client = FakeMqttClient("app")
        self.clock = FakeClock()
        self.ha = FakeHa(self.client, self.clock)
        self.ha.drop_acks = True                          # nobody is listening for the ack here
        for topic, payload in p5_discovery_messages(self.t, debug_map=False):
            self.client.publish(topic, payload, 1, False)
        self.ha.process()

    def push(self, req, text=None, detail=False):
        self.client.publish(self.t.review_push, push_state(req, text, detail), 1, True)
        self.ha.process()
        self.ha.advance(16)

    def test_an_attention_request_gives_one_generic_notification_without_actions(self):
        self.push(PushRequest(5, 4, "daily", 2, None, None, (), True))
        self.assertEqual(len(self.ha.sends), 1)
        note = self.ha.sends[0]
        self.assertEqual(note["message"], "Radar needs attention")
        self.assertNotIn("actions", note["data"])
        self.assertEqual(note["title"], "Radar zone review")
        self.assertEqual(len(self.ha.acks), 1)
        self.assertTrue(self.ha.acks[0].startswith("5:p"))

    def test_the_attention_text_is_generic_with_the_detail_switch_on_too(self):
        self.ha.detailed_text = True
        self.push(PushRequest(5, 4, "daily", 2, None, None, (), True), text=None, detail=True)
        self.assertEqual(self.ha.sends[0]["message"], "Radar needs attention")

    def test_a_daily_request_with_a_head_still_has_the_three_actions_and_the_count_text(self):
        self.push(PushRequest(6, 5, "daily", 3, "mod-z0001", 2, (), False))
        note = self.ha.sends[0]
        self.assertEqual(note["message"], "Radar zone review: 3 item(s) to look at")
        self.assertEqual([a["title"] for a in note["data"]["actions"]], ["Approve", "Rename", "Reject"])
        self.assertEqual(note["data"]["actions"][0]["action"], "RZ_APPROVE_2_mod-z0001")

    def test_a_setup_request_is_unchanged(self):
        self.push(PushRequest(1, 0, "setup", 3, "s0005-1", 1, (), False))
        self.assertEqual(self.ha.sends[0]["message"], "Radar setup review is ready (3 items)")

    def test_an_unknown_kind_is_not_sent(self):
        self.client.publish(self.t.review_push, "9|8|bogus|2|||||", 1, True)
        self.ha.process()
        self.ha.advance(16)
        self.assertEqual(self.ha.sends, [])

    def test_the_count_falling_to_zero_clears_the_notification_after_an_attention_request(self):
        self.push(PushRequest(5, 4, "daily", 2, None, None, (), True))
        self.assertIn("radar_zone_review", self.ha.visible)
        self.client.publish(self.t.review_push, push_state(PushRequest(5, 5, "daily", 0, None, None, (), False), None, False),
                            1, True)
        self.ha.process()
        self.ha.advance(1)
        self.assertNotIn("radar_zone_review", self.ha.visible)

    def test_the_push_state_never_carries_a_name_a_time_or_a_distance(self):
        state = push_state(PushRequest(5, 4, "daily", 2, None, None, ("pabc1234",), True), None, False)
        self.assertEqual(state, "5|4|attention|2|||pabc1234|")
        self.assertNotRegex(state, r"\b\d{1,2}:\d{2}\b")


if __name__ == "__main__":
    unittest.main()
