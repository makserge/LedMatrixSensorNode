"""The push flow (check P5-2 a, P5-3 b; spec 6.5, 11.2, 11.5): the real P5Layer and Service behind a fake broker client, a
scripted planner double (the push requests the core's PushPlanner makes, spec 6.2) and ``FakeHa``, the port of the blueprint.

The planner double keeps the request in plain attributes so that an add-on restart reloads it, as ``p5.push`` does. The real
planner has its own tests (``test_p5_notify``, CORE). What is tested here is the shell and the blueprint's protocol: no
notification lost, none visible twice, whatever restarts.
"""
import json
import queue
import re
import unittest

from radar_zone_learner.core.types import PUSH_TARGET_RE, PushAck, PushRequest, ReviewAnswer, SaveRequested
from radar_zone_learner.shell import service as service_mod
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.p5 import P5Layer
from radar_zone_learner.shell.persistence import StateStore
from tests.fake_ha import ACK_TOPIC, TAG, FakeHa
from tests.fakes import FakeLearner, FakeMqttClient
from tests.p4_harness import FixedRandom, Stub
from tests.p5_harness import P5Harness, bed_item, place_item, window_item

PUSH = "radar_zones/review/push"
ANSWER = "radar_zones/review/answer"
SELECT_CFG = "homeassistant/select/radar_zones_review_select/config"
GENERIC = ("Radar setup review is ready (3 items)", "Radar zone review: 3 item(s) to look at")
TIME_RE = re.compile(r"\b[0-9]{1,2}:[0-9]{2}\b")


class PushLearner(FakeLearner):
    """A planner double: ``new_request`` is what the core does when a push is due; ``PushAck`` is handled as in spec 6.2
    rule 5 (the acknowledging phone joins ``acks``, at most 8)."""

    def __init__(self, saved=None):
        super().__init__()
        s = dict(seq=0, acked=0, kind="none", count=0, head_id=None, head_rev=None, acks=[])
        s.update(saved or {})
        self.seq, self.acked, self.kind, self.count = s["seq"], s["acked"], s["kind"], s["count"]
        self.head_id, self.head_rev, self.acks = s["head_id"], s["head_rev"], list(s["acks"])
        self.p5_items = (window_item(rev=3), bed_item(), place_item("s0005-1", 1, "A"), place_item("s0006-1", 1, "B"))
        self.p5_selected = "night_window"
        self.acks_seen = []

    def snapshot(self) -> dict:
        return dict(seq=self.seq, acked=self.acked, kind=self.kind, count=self.count, head_id=self.head_id,
                    head_rev=self.head_rev, acks=list(self.acks))

    def request(self) -> PushRequest:
        return PushRequest(self.seq, self.acked, self.kind, self.count, self.head_id, self.head_rev, tuple(self.acks))

    def push_request(self) -> PushRequest:
        return self.request()

    def new_request(self, kind="setup", count=3, head_id="s0005-1", head_rev=1) -> None:
        """A request is made: a new seq, no acks yet, saved at once (spec 6.2 rule 4)."""
        self.seq += 1
        self.kind, self.count, self.head_id, self.head_rev, self.acks = kind, count, head_id, head_rev, []
        self.tick_outputs = list(self.tick_outputs) + [self.request(), SaveRequested("push")]

    def clear_all(self) -> None:
        """Every item was answered: the count falls to 0 (the blueprint clears the notification)."""
        self.count = 0
        self.tick_outputs = list(self.tick_outputs) + [self.request()]

    def handle(self, event) -> list:
        outs = list(super().handle(event))
        if isinstance(event, PushAck) and event.seq == self.seq:
            self.acks_seen.append((event.seq, event.target))
            self.acked = self.seq
            if event.target and re.match(PUSH_TARGET_RE, event.target) and event.target not in self.acks:
                self.acks = (self.acks + [event.target])[-8:]
            outs += [self.request(), SaveRequested("push")]
        return outs


class FlowBase(P5Harness):
    def make_learner(self, saved=None):
        return PushLearner(saved)

    def setUp(self):
        super().setUp()
        self.has = []
        # one broker client for the whole test, also across add-on restarts: FakeHa reads its publish log
        self.fixed_client = FakeMqttClient("app")
        self.mqtt.client_factory = lambda cid: self.fixed_client

    @property
    def client(self):
        return self.fixed_client

    def make_ha(self, **kw):
        ha = FakeHa(self.client, self.clock, **kw)
        self.has.append(ha)
        return ha

    def tick_world(self):
        for _ in range(3):
            self.pump()
            for ha in self.has:
                ha.process()
            self.pump()

    def advance(self, seconds):
        for _ in range(int(seconds)):
            self.clock.advance(1)
            self.svc.step(0)
            self.tick_world()

    def start_world(self):
        self.connect()
        self.tick_world()

    def request(self, **kw):
        self.learner.new_request(**kw)
        self.advance(1)

    def restart_addon(self):
        """The add-on stops and starts again: the learner reloads what it saved, the layer is new, the client is the same."""
        saved = self.learner.snapshot()
        self.svc.shutdown(0)
        client = self.client
        self.learner = self.make_learner(saved)
        self.events = queue.Queue(maxsize=2000)
        self.mqtt = MqttIO(self.settings, self.topics, self.events, client_factory=lambda cid: client,
                           clock_ms=self.clock.clock_ms, monotonic=self.clock.monotonic)
        self.layer = P5Layer(self.topics, self.mqtt.publish, self.dir, monotonic=self.clock.monotonic,
                             clock_ms=self.clock.clock_ms, select_mode=self.select_mode, debug_map=self.debug_map)
        self.store = StateStore(self.dir, validate=lambda state: self.learner)
        self.svc = service_mod.Service(self.settings, lambda state: self.learner, self.mqtt, self.store, Stub(), Stub(),
                                       self.clock.clock_ms, self.clock.monotonic, topics=self.topics, events=self.events,
                                       rng=FixedRandom(), rss=lambda: 24.0, p4=self.layer)
        self.svc.start()
        client.fire_connect(0)
        self.tick_world()

    def stop_ha(self):
        for ha in self.has:
            ha.stop()

    def start_ha(self):
        for ha in self.has:
            ha.start()
        self.tick_world()

    def push_states(self, start=0):
        return [p[1].decode() for p in self.client.published[start:] if p[0] == PUSH]

    def assert_one_notification_each(self, *has):
        for ha in has or self.has:
            self.assertEqual(list(ha.visible), [TAG], ha.phone)
            self.assertEqual(len(ha.sends), len(ha.acks))
            self.assertLessEqual(len(ha.sends), 2, "at most two sends per request and phone")

    def assert_acked(self, *has):
        self.assertEqual(self.learner.acked, self.learner.seq)
        for ha in has or self.has:
            self.assertIn(ha.me, self.learner.acks)


class FlowTest(FlowBase, unittest.TestCase):
    # ------------------------------------------------------------ C1: the plain case
    def test_c1_a_request_while_ha_is_up(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(20)
        self.assert_one_notification_each()
        self.assertEqual(len(ha.sends), 1)
        self.assert_acked()
        note = ha.visible[TAG]
        self.assertEqual(note["message"], GENERIC[0])
        self.assertEqual([a["title"] for a in note["data"]["actions"]], ["Approve", "Rename", "Reject"])
        self.assertEqual(note["data"]["actions"][0]["action"], "RZ_APPROVE_1_s0005-1")
        self.assertTrue(note["image"].startswith(b"\x89PNG"))                  # the push map was in HA when it was sent
        self.assertEqual(self.learner.acks_seen, [(1, ha.me)])
        self.assertEqual(self.push_states()[-1], "1|1|setup|3|1|s0005-1|%s|" % ha.me)

    def test_c1_the_state_changes_only_with_the_request_and_the_ack(self):
        self.make_ha()
        self.start_world()
        self.request()
        self.advance(40)
        states = list(dict.fromkeys(self.push_states()))
        self.assertEqual(states[0], "0|0|none|0||||")
        self.assertEqual([s.split("|")[0] for s in states], ["0", "1", "1"])
        self.assertEqual(states[1], "1|0|setup|3|1|s0005-1||")

    # ------------------------------------------------------------ C2: HA is down when the request is made
    def test_c2_a_request_while_ha_is_down_is_delivered_when_it_starts(self):
        ha = self.make_ha()
        self.start_world()
        self.stop_ha()
        self.request()
        self.advance(30)
        self.assertEqual(ha.sends, [])
        self.assertEqual(ha.visible, {})
        self.start_ha()
        self.advance(40)
        self.assert_one_notification_each()
        self.assertEqual(len(ha.sends), 1)
        self.assert_acked()

    # ------------------------------------------------------------ C3: HA stops after the send, before the ack
    def test_c3_ha_stops_between_the_notification_and_the_ack(self):
        ha = self.make_ha()
        self.start_world()
        ha.drop_acks = True                                         # the ack never reaches the app
        self.request()
        self.advance(20)
        self.assertEqual(len(ha.sends), 1)
        self.assertEqual(self.learner.acked, 0)
        self.stop_ha()
        ha.drop_acks = False
        self.start_ha()
        self.advance(40)
        self.assert_one_notification_each()
        self.assertEqual(len(ha.sends), 2)                          # a second send with the same tag: replaced
        self.assertEqual(len(ha.visible), 1)
        self.assert_acked()

    # ------------------------------------------------------------ C4: the ack is lost because the add-on is down
    def test_c4_the_ack_is_lost_while_the_addon_is_down(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(8)
        self.svc.shutdown(0)                                        # the add-on stops during the delay
        self.mqtt.connected = False
        ha.drop_acks = True                                         # nobody is there to hear the ack
        self.advance_without_app(ha, 15)
        self.assertEqual(len(ha.sends), 1)
        ha.drop_acks = False
        self.restart_addon()
        self.advance(30)
        self.assert_one_notification_each()
        self.assertEqual(len(ha.sends), 1)                          # the state did not change: no second run
        self.assertEqual(self.learner.acked, 0)                     # the ack is gone; the retry rule (a day later) decides
        self.learner.new_request(kind="setup", count=3)             # the retry: a new seq, acks cleared (spec 6.2 rule 6)
        self.advance(30)
        self.assert_one_notification_each()
        self.assertEqual(len(ha.sends), 2)
        self.assert_acked()

    def advance_without_app(self, ha, seconds):
        for _ in range(int(seconds)):
            self.clock.advance(1)
            ha.process()
            ha.run_due()

    # ------------------------------------------------------------ C5, C6, C7: add-on restarts
    def test_c5_the_addon_restarts_after_the_request_and_before_the_ack(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(5)
        self.restart_addon()
        self.advance(40)
        self.assert_one_notification_each()
        self.assertEqual(len(ha.sends), 1)
        self.assert_acked()
        self.assertEqual(self.learner.seq, 1)                       # no second request

    def test_c6_the_addon_restarts_after_the_ack(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(25)
        self.assert_acked()
        before = list(ha.sends)
        self.restart_addon()
        self.advance(40)
        self.assertEqual(ha.sends, before)                          # nothing again: this phone is in acks
        self.assert_one_notification_each()
        self.assertEqual(self.push_states(), self.push_states())    # (see C13 for the republished state)

    def test_c7_both_restart(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(25)
        self.stop_ha()
        self.restart_addon()
        self.start_ha()
        self.advance(40)
        self.assert_one_notification_each()
        self.assertEqual(len(ha.sends), 1)                          # the ack had been processed: no second send
        self.assert_acked()

    def test_c7_both_restart_before_the_ack_was_processed(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(5)
        self.stop_ha()
        self.restart_addon()
        self.start_ha()
        self.advance(40)
        self.assert_one_notification_each()
        self.assertEqual(len(ha.sends), 1)                          # the run that HA's restart cut is started again
        self.assert_acked()

    # ------------------------------------------------------------ C8: two phones, not in lock step
    def test_c8_two_phones_not_in_lock_step(self):
        a = self.make_ha(phone="phoneA")
        b = self.make_ha(phone="phoneB", start_offset_s=20.0)       # B's run starts after A's ack has come back
        self.start_world()
        self.request()
        self.advance(60)
        self.assert_one_notification_each()
        self.assertEqual((len(a.sends), len(b.sends)), (1, 1))      # A's ack never hid the request from B (D42)
        self.assert_acked(a, b)
        self.assertEqual(sorted(self.learner.acks), sorted([a.me, b.me]))
        self.assertEqual(self.push_states()[-1], "1|1|setup|3|1|s0005-1|%s,%s|" % (a.me, b.me))

    def test_c8_the_same_phone_twice_is_listed_once(self):
        a = self.make_ha(phone="phoneA")
        self.start_world()
        self.request()
        self.advance(20)
        self.client.fire_message(ACK_TOPIC, "1:%s" % a.me)                 # a repeated ack
        self.pump()
        self.assertEqual(self.learner.acks, [a.me])

    # ------------------------------------------------------------ C9, C10: taps
    def test_c9_a_tap_while_ha_is_down_is_lost_and_the_notification_still_shows(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(20)
        self.stop_ha()
        self.assertIsNone(ha.tap_notification("Approve"))
        self.advance(3)
        self.assertEqual([e for e in self.learner.events if isinstance(e, ReviewAnswer)], [])
        self.assertIn(TAG, ha.visible)                              # the notification is still there
        self.start_ha()
        self.advance(5)
        body = ha.tap_notification("Approve")
        self.advance(2)
        self.assertEqual(body["op"], "approve")
        self.assertEqual([(e.op, e.item, e.rev) for e in self.learner.events if isinstance(e, ReviewAnswer)],
                         [("approve", "s0005-1", 1)])

    def test_c10_the_same_tap_delivered_twice_is_applied_once(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(20)
        body = ha.tap_notification("Approve", context_id="01HV9ZZ")
        self.advance(1)
        self.client.fire_message(ANSWER, json.dumps(body))                 # a QoS-1 duplicate
        self.advance(1)
        got = [e for e in self.learner.events if isinstance(e, ReviewAnswer)]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].aid, "01HV9ZZ")
        self.assertEqual(self.published_events(), [])                      # the repeat is silent

    def test_a_tap_for_an_item_that_was_answered_meanwhile_is_a_visible_refusal(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(20)
        self.learner.p5_items = (window_item(rev=3), bed_item(), place_item("s0006-1", 1, "B"))    # s0005-1 was answered
        self.learner.tick_outputs = [self.changed_items()]
        self.advance(2)
        ha.tap_notification("Approve")
        self.advance(2)
        self.assertEqual([e for e in self.learner.events if isinstance(e, ReviewAnswer)], [])
        self.assertEqual(self.published_events(), [{"reason": "no_such_item", "op": "approve", "item": "s0005-1",
                                                    "event_type": "answer_ignored"}])

    def changed_items(self):
        from radar_zone_learner.core.types import ReviewChanged
        return ReviewChanged(tuple(self.learner.p5_items), "night_window")

    def published_events(self):
        return [json.loads(p[1]) for p in self.client.published_to("radar_zones/review/event")]

    # ------------------------------------------------------------ C11: the next day
    def test_c11_a_daily_push_the_next_day_after_a_restart_replaces_the_notification(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(25)
        self.stop_ha()
        self.restart_addon()
        self.start_ha()
        self.advance(20)
        self.request(kind="daily", count=2, head_id="s0006-1", head_rev=1)
        self.advance(25)
        self.assert_one_notification_each()
        self.assertEqual([s["seq"] for s in ha.sends], [1, 2])
        self.assertEqual(ha.visible[TAG]["message"], GENERIC[1].replace("3", "2"))
        self.assertEqual(ha.visible[TAG]["data"]["actions"][0]["action"], "RZ_APPROVE_1_s0006-1")
        self.assert_acked()

    def test_the_notification_is_cleared_when_everything_was_answered(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(25)
        self.learner.clear_all()
        self.advance(25)
        self.assertEqual(ha.visible, {})
        self.assertEqual(ha.clears, [1])

    # ------------------------------------------------------------ C12: one phone acked, HA stops before the other's run
    def test_c12_ha_stops_before_the_second_phones_run_completed(self):
        a = self.make_ha(phone="phoneA")
        b = self.make_ha(phone="phoneB", start_offset_s=10.0)
        self.start_world()
        self.request()
        self.advance(20)                                            # A has sent and acknowledged; B's run is in its delay
        self.assertEqual((len(a.sends), len(b.sends)), (1, 0))
        self.assertEqual(self.learner.acks, [a.me])
        self.stop_ha()
        self.start_ha()
        self.advance(40)
        self.assert_one_notification_each()
        self.assertEqual((len(a.sends), len(b.sends)), (1, 1))      # B is notified, A is not notified again
        self.assert_acked(a, b)

    # ------------------------------------------------------------ C13: the add-on start order
    def test_c13_a_restart_with_a_request_pending_never_overwrites_the_retained_state(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(25)
        mark = len(self.client.published)
        ha.visible_before = dict(ha.visible)
        self.svc.shutdown(0)
        self.restart_addon_first_republish_only()
        after = self.push_states(mark)
        self.assertTrue(after)
        self.assertEqual(set(after), {"1|1|setup|3|1|s0005-1|%s|" % ha.me})     # never the default 0|0|none|0||||
        self.assertEqual(ha.clears, [])
        cfgs = [json.loads(p[1]) for p in self.client.published[mark:] if p[0] == SELECT_CFG]
        self.assertEqual(cfgs[-1]["options"], ["No item", "Night window", "Bed zone", "Place A", "Place B"])
        selects = [p[1].decode() for p in self.client.published[mark:] if p[0] == "radar_zones/review/select"]
        self.assertEqual(selects, ["Night window"])                              # the real state, once
        self.advance(40)
        self.assertEqual(ha.clears, [])
        self.assertEqual(len(ha.sends), 1)
        self.assert_one_notification_each()

    def restart_addon_first_republish_only(self):
        """Starts the add-on and runs only the republish that the connect queues, before the learner's first tick."""
        saved = self.learner.snapshot()
        client = self.client
        self.learner = self.make_learner(saved)
        self.events = queue.Queue(maxsize=2000)
        self.mqtt = MqttIO(self.settings, self.topics, self.events, client_factory=lambda cid: client,
                           clock_ms=self.clock.clock_ms, monotonic=self.clock.monotonic)
        self.layer = P5Layer(self.topics, self.mqtt.publish, self.dir, monotonic=self.clock.monotonic,
                             clock_ms=self.clock.clock_ms, select_mode=self.select_mode, debug_map=self.debug_map)
        self.svc = service_mod.Service(self.settings, lambda state: self.learner, self.mqtt, self.store, Stub(), Stub(),
                                       self.clock.clock_ms, self.clock.monotonic, topics=self.topics, events=self.events,
                                       rng=FixedRandom(), rss=lambda: 24.0, p4=self.layer)
        self.svc.start()
        client.fire_connect(0)
        self.svc.step(0)                                            # the Republish marker: before any tick of the learner
        for ha in self.has:
            ha.process()

    # ------------------------------------------------------------ C14, C15: nothing was delivered
    def test_c14_a_failing_notify_action_leaves_no_trace_and_the_retry_delivers(self):
        ha = self.make_ha(notify_fails=True)
        self.start_world()
        self.request()
        self.advance(40)
        self.assertEqual(ha.sends, [])
        self.assertEqual(ha.visible, {})
        self.assertEqual(ha.acks, [])
        self.assertEqual(self.learner.acked, 0)
        self.assertEqual(self.push_states()[-1], "1|0|setup|3|1|s0005-1||")        # no state change from the failure
        ha.notify_fails = False
        self.learner.new_request(kind="setup", count=3)             # the next local day: the retry rule (spec 6.2 rule 6)
        self.advance(30)
        self.assert_one_notification_each()
        self.assertEqual(self.learner.seq, 2)                       # the one allowed second seq
        self.assert_acked()

    def test_c15_the_blueprint_enabled_after_the_request_was_made(self):
        ha = self.make_ha(start_enabled=False)
        self.start_world()
        self.request()
        self.advance(30)
        ha.enable()
        self.advance(30)
        self.assertEqual(ha.sends, [])                              # nothing fires for an old state
        self.assertEqual(self.learner.acked, 0)
        self.learner.new_request(kind="setup", count=3)
        self.advance(30)
        self.assert_one_notification_each()
        self.assert_acked()

    # ------------------------------------------------------------ ha birth and the second pass
    def test_an_ha_restart_with_the_request_acked_sends_nothing_again(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(25)
        self.stop_ha()
        self.start_ha()
        self.advance(40)
        self.assertEqual(len(ha.sends), 1)
        self.assert_one_notification_each()

    def test_the_push_map_reaches_ha_before_the_notification_is_sent(self):
        ha = self.make_ha()
        self.start_world()
        self.request()
        self.advance(20)
        self.assertIsNotNone(ha.push_map_bytes)
        self.assertEqual(ha.visible[TAG]["image"], ha.push_map_bytes)


class ImageTimingTest(FlowBase, unittest.TestCase):
    """The push map after a restart of Home Assistant (spec 6.4, D31): discovery is not retained and the image is not
    retained, so an image sent before the image entity subscribes is lost. The app sends it in the burst and again 10 s
    later (the second pass); the blueprint sends 15 s after the state arrived. FakeHa's ``image_delay_s`` is the time its
    image platform needs: up to the second pass the notification has its image, later the notification goes out without
    it (text and actions are intact) and nothing else is lost."""

    def restart_with_a_request_pending(self, delay):
        ha = self.make_ha(image_delay_s=delay)
        self.start_world()
        self.advance(30)
        self.stop_ha()
        self.request()                                         # made while Home Assistant is down
        self.advance(5)
        self.start_ha()
        self.advance(45)
        return ha

    def test_an_image_entity_that_exists_before_the_second_pass_gets_the_image(self):
        ha = self.restart_with_a_request_pending(6.0)
        self.assertEqual(len(ha.sends), 1)
        self.assertTrue(ha.visible[TAG]["image"].startswith(b"\x89PNG"))
        self.assert_acked()

    def test_an_image_entity_that_comes_after_the_second_pass_gives_a_notification_without_the_image(self):
        ha = self.restart_with_a_request_pending(14.0)
        self.assertEqual(len(ha.sends), 1)
        self.assertIsNone(ha.visible[TAG]["image"])            # the known limit: the image is not retained
        self.assertEqual(ha.visible[TAG]["message"], GENERIC[0])
        self.assertEqual([a["title"] for a in ha.visible[TAG]["data"]["actions"]], ["Approve", "Rename", "Reject"])
        self.assert_acked()                                    # ... and the request is not lost or repeated


class PrivacyScanTest(FlowBase, unittest.TestCase):
    """P5-3 (b): with the defaults no push payload, notification or action id carries a time or a name."""

    def test_the_defaults_publish_no_time_and_no_name_to_the_push(self):
        a = self.make_ha(phone="phoneA")
        b = self.make_ha(phone="phoneB")
        self.start_world()
        self.request()
        self.advance(30)
        a.tap_notification("Rename", "Desk")
        self.advance(2)
        self.request(kind="daily", count=2, head_id="s0006-1", head_rev=1)
        self.advance(30)
        pushes = [p[1].decode() for p in self.client.published if p[0] == PUSH]
        self.assertTrue(pushes)
        for state in pushes:
            fields = state.split("|")
            self.assertEqual(len(fields), 8)
            self.assertEqual(fields[7], "", state)                      # no text without the opt-in
            self.assertIsNone(TIME_RE.search(state), state)
            self.assertNotIn("Desk", state)
        for ha in (a, b):
            for note in ha.sends:
                self.assertIn(note["message"], ("Radar setup review is ready (3 items)",
                                                "Radar zone review: 2 item(s) to look at"))
                self.assertEqual(note["title"], "Radar zone review")
                self.assertEqual(note["data"]["tag"], TAG)
                self.assertEqual(note["data"]["group"], TAG)
                self.assertEqual({x["title"] for x in note["data"]["actions"]}, {"Approve", "Rename", "Reject"})
                for action in note["data"]["actions"]:
                    self.assertRegex(action["action"], r"^RZ_(APPROVE|RENAME|REJECT)_[0-9]+_"
                                                       r"(s[0-9]{4,}-[0-9]+|mrg-s[0-9]{4,}-[0-9]+|mod-z[0-9]{4,})$")
                self.assertIsNone(TIME_RE.search(json.dumps(note["data"])))
                self.assertNotIn("Desk", json.dumps({k: v for k, v in note.items() if k != "image"}))

    def test_only_one_of_the_two_opt_ins_keeps_the_message_generic(self):
        from radar_zone_learner.core.types import StatusChanged
        detail_on = StatusChanged("learning", {"settings": {"approve_sched": False, "guests_pause": False, "pets": False,
                                                            "detail_text": True}})
        for app_on, bp_on in ((True, False), (False, True)):
            with self.subTest(app=app_on, blueprint=bp_on):
                self.has = []
                self.setUp()
                self.make_ha(detailed_text=bp_on)
                self.start_world()
                if app_on:
                    self.learner.tick_outputs = [detail_on]
                    self.advance(1)
                self.learner.p5_items = (place_item("s0005-1", 1, "A", hrs_wd=("09-12", "13-17")), bed_item())
                self.layer._set_items(self.learner.p5_items, "s0005-1")
                self.request(count=2)
                self.advance(25)
                self.assertEqual(self.has[0].visible[TAG]["message"], "Radar setup review is ready (2 items)")

    def test_with_both_opt_ins_the_message_is_the_head_text_with_the_hours(self):
        from radar_zone_learner.core.types import StatusChanged
        self.make_ha(detailed_text=True)
        self.start_world()
        self.learner.tick_outputs = [StatusChanged("learning", {"settings": {"approve_sched": False, "guests_pause": False,
                                                                             "pets": False, "detail_text": True}})]
        self.advance(1)
        self.learner.p5_items = (place_item("s0005-1", 1, "A", hrs_wd=("09-12", "13-17"), hrs_we=("09-12", "13-17")),
                                 bed_item())
        self.layer._set_items(self.learner.p5_items, "s0005-1")
        self.request(count=2)
        self.advance(25)
        message = self.has[0].visible[TAG]["message"]
        self.assertTrue(message.startswith("New place A:"))
        self.assertIn("Used mostly 09-12 h and 13-17 h.", message)
        self.assertEqual(len(self.push_states()[-1].split("|")), 8)
        self.assertLessEqual(len(self.push_states()[-1]), 255)


if __name__ == "__main__":
    unittest.main()
