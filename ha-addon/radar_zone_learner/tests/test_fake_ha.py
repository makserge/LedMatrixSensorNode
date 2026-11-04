"""The fake Home Assistant behaves as spec 11.5 says (H1-H10), so that the P5-2 and P5-3 results mean something."""
import json
import unittest

from tests.fake_ha import ACK_TOPIC, ANSWER_TOPIC, BIRTH_TOPIC, DELAY_S, TAG, FakeHa
from tests.fakes import FakeClock, FakeMqttClient

PUSH = "radar_zones/review/push"
PUSH_MAP = "radar_zones/review/push_map"
PUSH_CFG = "homeassistant/sensor/radar_zones_review_push/config"
MAP_CFG = "homeassistant/image/radar_zones_review_push_map/config"
SETUP = "7|6|setup|3|1|s0005-1||"


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.client = FakeMqttClient("app")
        self.heard = []                                   # what the app would receive from Home Assistant
        self.client.on_message = lambda c, u, msg: self.heard.append((msg.topic, msg.payload.decode("utf-8")))

    def ha(self, **kw):
        return FakeHa(self.client, self.clock, **kw)

    def app_publishes(self, topic, payload, retain=False):
        self.client.publish(topic, payload, qos=1, retain=retain)

    def discover(self, ha):
        self.app_publishes(PUSH_CFG, json.dumps({"state_topic": PUSH}))
        self.app_publishes(MAP_CFG, json.dumps({"image_topic": PUSH_MAP}))
        ha.process()

    def settle(self, ha, seconds=DELAY_S + 1):
        for _ in range(int(seconds)):
            ha.advance(1)


class RetainedTest(Base):
    def test_h1_the_broker_keeps_retained_messages_while_ha_is_down(self):
        ha = self.ha()
        ha.stop()
        self.app_publishes(PUSH, SETUP, retain=True)
        self.app_publishes("radar_zones/review/event", "{}", retain=False)
        ha.process()
        self.assertEqual(ha.broker_retained[PUSH], SETUP.encode())
        self.assertNotIn("radar_zones/review/event", ha.broker_retained)

    def test_h1_an_empty_retained_message_deletes_it(self):
        ha = self.ha()
        self.app_publishes(PUSH, SETUP, retain=True)
        self.app_publishes(PUSH, "", retain=True)
        ha.process()
        self.assertNotIn(PUSH, ha.broker_retained)

    def test_h1_a_non_retained_message_published_while_down_is_lost(self):
        ha = self.ha()
        self.discover(ha)
        ha.stop()
        self.app_publishes(PUSH_MAP, b"\x89PNG-1")
        ha.process()
        ha.start()
        self.app_publishes(PUSH_CFG, json.dumps({"state_topic": PUSH}))
        self.app_publishes(MAP_CFG, json.dumps({"image_topic": PUSH_MAP}))
        ha.process()
        self.assertIsNone(ha.push_map_bytes)                       # the image sent while down never arrived


class EntityTest(Base):
    def test_h2_an_entity_exists_only_after_its_config_was_seen(self):
        ha = self.ha()
        self.app_publishes(PUSH, SETUP, retain=True)
        ha.process()
        self.assertIsNone(ha.push_state)                           # no entity yet
        self.app_publishes(PUSH_CFG, json.dumps({"state_topic": PUSH}))
        ha.process()
        self.assertEqual(ha.push_state, SETUP)                     # the retained payload at once

    def test_h2_a_config_published_while_down_creates_nothing(self):
        ha = self.ha()
        ha.stop()
        self.app_publishes(PUSH_CFG, json.dumps({"state_topic": PUSH}))
        ha.process()
        ha.start()
        self.assertEqual(ha.entities, {})

    def test_h2_images_are_delivered_only_to_an_existing_entity(self):
        ha = self.ha()
        self.app_publishes(PUSH_MAP, b"early")
        ha.process()
        self.app_publishes(MAP_CFG, json.dumps({"image_topic": PUSH_MAP}))
        ha.process()
        self.assertIsNone(ha.push_map_bytes)
        self.app_publishes(PUSH_MAP, b"late")
        ha.process()
        self.assertEqual(ha.push_map_bytes, b"late")

    def test_h3_stop_drops_the_entities_and_cancels_the_runs(self):
        ha = self.ha()
        self.discover(ha)
        self.app_publishes(PUSH, SETUP, retain=True)
        ha.process()
        ha.run_due()
        ha.advance(3)
        ha.stop()
        self.assertFalse(ha.up)
        self.assertEqual(ha.entities, {})
        self.assertIsNone(ha.push_state)
        self.settle(ha)
        self.assertEqual(ha.sends, [])                             # the delayed run was cancelled
        self.assertEqual(self.heard, [])

    def test_h4_start_sends_the_birth_message_to_the_app(self):
        ha = self.ha()
        ha.stop()
        ha.start()
        self.assertTrue(ha.up)
        self.assertEqual(self.heard, [(BIRTH_TOPIC, "online")])


class RunTest(Base):
    def setUp(self):
        super().setUp()
        self.ha1 = self.ha()
        self.discover(self.ha1)
        self.app_publishes(PUSH_MAP, b"\x89PNG-head")
        self.ha1.process()

    def request(self, state=SETUP, ha=None):
        self.app_publishes(PUSH, state, retain=True)
        (ha or self.ha1).process()

    def test_h5_the_first_state_after_creation_triggers_and_an_unchanged_one_does_not(self):
        self.request()
        self.settle(self.ha1)
        self.assertEqual(len(self.ha1.sends), 1)
        self.request(SETUP)                                        # the same state again: no change, no trigger
        self.settle(self.ha1)
        self.assertEqual(len(self.ha1.sends), 1)

    def test_h6_the_notification_after_15_seconds_with_three_actions_and_the_image(self):
        self.request()
        self.ha1.advance(14)
        self.assertEqual(self.ha1.sends, [])                       # the delay of 15 s
        self.ha1.advance(1.5)
        self.assertEqual(len(self.ha1.sends), 1)
        note = self.ha1.visible[TAG]
        self.assertEqual(note["title"], "Radar zone review")
        self.assertEqual(note["message"], "Radar setup review is ready (3 items)")
        self.assertEqual([a["title"] for a in note["data"]["actions"]], ["Approve", "Rename", "Reject"])
        self.assertEqual([a["action"] for a in note["data"]["actions"]],
                         ["RZ_APPROVE_1_s0005-1", "RZ_RENAME_1_s0005-1", "RZ_REJECT_1_s0005-1"])
        self.assertEqual(note["data"]["actions"][1]["behavior"], "textInput")
        self.assertEqual(note["data"]["tag"], TAG)
        self.assertEqual(note["image"], b"\x89PNG-head")            # the bytes in the entity at that moment
        self.assertEqual(self.ha1.acks, ["7:pphoneA"])
        self.assertEqual(self.heard[-1], (ACK_TOPIC, "7:pphoneA"))

    def test_h6_the_daily_sentence_and_a_request_without_a_head(self):
        self.request("8|7|daily|2||||")
        self.settle(self.ha1)
        note = self.ha1.visible[TAG]
        self.assertEqual(note["message"], "Radar zone review: 2 item(s) to look at")
        self.assertNotIn("actions", note["data"])

    def test_h6_the_run_start_check_costs_nothing_for_a_state_this_phone_acked(self):
        self.request("7|7|setup|3|1|s0005-1|pphoneA|")
        self.settle(self.ha1)
        self.assertEqual(self.ha1.sends, [])

    def test_h6_another_phones_ack_never_stops_this_run(self):
        self.request("7|7|setup|3|1|s0005-1|pOTHER|")              # seq == acked, but this phone is not in acks
        self.settle(self.ha1)
        self.assertEqual(len(self.ha1.sends), 1)

    def test_h6_the_re_check_stops_a_run_when_the_state_changed_during_the_delay(self):
        self.request()
        self.ha1.advance(5)
        self.request("7|7|setup|3|1|s0005-1|pphoneA|")             # acked meanwhile (an earlier run of this automation)
        self.settle(self.ha1)
        self.assertEqual(self.ha1.sends, [])
        self.assertEqual(self.ha1.acks, [])

    def test_h6_a_newer_request_is_sent_by_its_own_run_queued_behind(self):
        self.request()
        self.ha1.advance(5)
        self.request("8|6|daily|1|2|s0006-1||")
        self.settle(self.ha1, 40)
        self.assertEqual([s["seq"] for s in self.ha1.sends], [8])    # the first run's re-check saw seq 8 and stopped
        self.assertEqual(self.ha1.acks, ["8:pphoneA"])

    def test_h6_count_zero_clears_the_notification_but_only_with_seq_above_zero(self):
        self.request()
        self.settle(self.ha1)
        self.assertIn(TAG, self.ha1.visible)
        self.request("0|0|none|0||||")                             # a default state never clears a real notification
        self.settle(self.ha1)
        self.assertIn(TAG, self.ha1.visible)
        self.assertEqual(self.ha1.clears, [])
        self.request("7|7|none|0||||")
        self.settle(self.ha1)
        self.assertNotIn(TAG, self.ha1.visible)
        self.assertEqual(self.ha1.clears, [7])

    def test_h6_the_same_tag_replaces_the_first_copy(self):
        self.request()
        self.settle(self.ha1)
        self.request("8|7|daily|2|1|s0006-1||")
        self.settle(self.ha1)
        self.assertEqual(len(self.ha1.sends), 2)
        self.assertEqual(len(self.ha1.visible), 1)
        self.assertEqual(self.ha1.visible[TAG]["seq"], 8)

    def test_h6_a_failing_notify_action_stops_before_the_ack(self):
        ha = self.ha(phone="phoneB", notify_fails=True)
        self.discover(ha)
        self.request(ha=ha)
        self.settle(ha)
        self.assertEqual(ha.sends, [])
        self.assertEqual(ha.acks, [])
        self.assertEqual(ha.failures, 1)
        self.assertEqual(ha.visible, {})

    def test_h6_detailed_text_needs_the_blueprint_input_and_a_text_in_the_state(self):
        text_state = "7|6|setup|3|1|s0005-1||New place A: a bed"
        for detailed, want in ((False, "Radar setup review is ready (3 items)"), (True, "New place A: a bed")):
            ha = self.ha(detailed_text=detailed, phone="p%d" % detailed)
            self.discover(ha)
            self.request(text_state, ha=ha)
            self.settle(ha)
            self.assertEqual(ha.visible[TAG]["message"], want)
        ha = self.ha(detailed_text=True, phone="phoneZ")
        self.discover(ha)
        self.request(SETUP, ha=ha)                                 # no text in the state: generic
        self.settle(ha)
        self.assertEqual(ha.visible[TAG]["message"], "Radar setup review is ready (3 items)")

    def test_h6_garbage_states_do_nothing(self):
        for state in ("hello", "7|6|setup|3|1|s0005-1|", "x|6|setup|3|1|s0005-1||", "7|6|weekly|3|1|s0005-1||"):
            self.request(state)
            self.settle(self.ha1)
        self.assertEqual(self.ha1.sends, [])

    def test_the_ack_is_dropped_when_the_test_says_the_app_is_down(self):
        self.ha1.drop_acks = True
        self.request()
        self.settle(self.ha1)
        self.assertEqual(self.ha1.acks, ["7:pphoneA"])             # it was made ...
        self.assertEqual([h for h in self.heard if h[0] == ACK_TOPIC], [])     # ... but never arrived


class TapTest(Base):
    def setUp(self):
        super().setUp()
        self.ha1 = self.ha()
        self.discover(self.ha1)
        self.app_publishes(PUSH, SETUP, retain=True)
        self.ha1.process()
        self.settle(self.ha1)
        self.heard.clear()

    def answer(self):
        return json.loads([h for h in self.heard if h[0] == ANSWER_TOPIC][-1][1])

    def test_h7_a_tap_becomes_the_answer_json_with_the_context_id(self):
        body = self.ha1.tap_notification("Approve", context_id="01HV9ZZ")
        self.assertEqual(body, {"op": "approve", "item": "s0005-1", "rev": 1, "aid": "01HV9ZZ"})
        self.assertEqual(self.answer(), body)

    def test_h7_rename_carries_the_reply_text_cut_to_32_and_never_an_empty_name(self):
        self.ha1.tap_notification("Rename", "  " + "x" * 40)
        self.assertEqual(self.answer()["name"], "x" * 32)
        self.ha1.tap_notification("Rename", "")
        self.assertNotIn("name", self.answer())
        self.ha1.tap_notification("Rename", None)
        self.assertNotIn("name", self.answer())
        self.ha1.tap_notification("Rename", 2)
        self.assertEqual(self.answer()["name"], "2")               # a string: the blueprint builds it inside the template

    def test_h7_the_op_ids_split_on_the_first_three_underscores(self):
        body = self.ha1.phone_tap("RZ_REJECT_12_night_window")
        self.assertEqual((body["op"], body["rev"], body["item"]), ("reject", 12, "night_window"))

    def test_h7_a_tap_while_ha_is_down_is_lost(self):
        self.ha1.stop()
        self.assertIsNone(self.ha1.tap_notification("Approve"))
        self.assertIsNone(self.ha1.phone_tap("RZ_APPROVE_1_s0005-1"))
        self.assertEqual(self.ha1.taps_lost, 2)                    # both taps were lost; the notification still shows
        self.assertIn(TAG, self.ha1.visible)
        self.assertEqual([h for h in self.heard if h[0] == ANSWER_TOPIC], [])

    def test_h7_other_action_ids_are_ignored(self):
        self.assertIsNone(self.ha1.phone_tap("OTHER"))
        self.assertIsNone(self.ha1.phone_tap("RZ_LATER_1_s0005-1"))
        self.assertIsNone(self.ha1.phone_tap("RZ_APPROVE_x_s0005-1"))
        self.assertEqual([h for h in self.heard if h[0] == ANSWER_TOPIC], [])


class TwoPhonesTest(Base):
    def test_h9_two_instances_have_their_own_id_tags_and_runs(self):
        a, b = self.ha(phone="phoneA"), self.ha(phone="phoneB")
        self.assertNotEqual(a.me, b.me)
        self.discover(a)
        self.discover(b)
        self.app_publishes(PUSH, SETUP, retain=True)
        a.process()
        b.process()
        for _ in range(17):
            self.clock.advance(1)
            a.process()
            b.process()
            a.run_due()
            b.run_due()
        self.assertEqual(len(a.sends), 1)
        self.assertEqual(len(b.sends), 1)
        self.assertEqual(sorted(a.acks + b.acks), ["7:pphoneA", "7:pphoneB"])
        self.assertIsNot(a.visible, b.visible)

    def test_h10_a_start_offset_delays_every_run_of_that_instance(self):
        a, b = self.ha(phone="phoneA"), self.ha(phone="phoneB", start_offset_s=10.0)
        self.discover(a)
        self.discover(b)
        self.app_publishes(PUSH, SETUP, retain=True)
        a.process()
        b.process()
        for _ in range(17):
            self.clock.advance(1)
            a.process()
            b.process()
            a.run_due()
            b.run_due()
        self.assertEqual(len(a.sends), 1)
        self.assertEqual(b.sends, [])                              # its run started at 10 s and waits until 25 s
        for _ in range(10):
            self.clock.advance(1)
            b.process()
            b.run_due()
        self.assertEqual(len(b.sends), 1)

    def test_h10_an_automation_enabled_late_does_not_fire_for_an_old_state(self):
        late = self.ha(start_enabled=False)
        self.discover(late)
        self.app_publishes(PUSH, SETUP, retain=True)
        late.process()
        self.settle(late)
        self.assertEqual(late.sends, [])
        late.enable()
        self.settle(late)
        self.assertEqual(late.sends, [])                           # no state change since: nothing fires
        self.app_publishes(PUSH, "8|7|daily|2|1|s0006-1||", retain=True)
        late.process()
        self.settle(late)
        self.assertEqual(len(late.sends), 1)

    def test_a_restart_that_republishes_the_discovery_makes_the_entity_fire_again(self):
        ha = self.ha()
        self.discover(ha)
        self.app_publishes(PUSH, SETUP, retain=True)
        ha.process()
        self.settle(ha)
        ha.stop()
        ha.start()
        self.discover(ha)                                          # the app's discovery burst after the birth message
        self.settle(ha)
        self.assertEqual(len(ha.sends), 2)                         # the state is new to the entity: the run starts again
        self.assertEqual(len(ha.visible), 1)


if __name__ == "__main__":
    unittest.main()
