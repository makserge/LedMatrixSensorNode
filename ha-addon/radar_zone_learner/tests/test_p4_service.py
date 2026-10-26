"""The P4 layer inside the service, against a fake node and a scripted learner (no paho, no broker)."""
import json
import queue
import unittest

from radar_zone_learner.core.types import MarkResult, StatusChanged
from radar_zone_learner.shell import service as service_mod
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.p4 import P4Layer
from radar_zone_learner.shell.p4_types import (ITEM_BED, ITEM_NIGHT_WINDOW, CommandResult, LuxSample,
                                               NightLearningChanged, NightLearningSwitch, NodeCommand,
                                               ReviewAnswer, ReviewChanged, ReviewItem, ReviewSelect)
from radar_zone_learner.shell.topics import build_topics
from tests.fakes import FakeClientFactory, FakeLearner, FakeNode
from tests.p4_harness import FixedRandom, P4Harness, Stub

VALID = "radar_zones/radar/valid"
NL_STATE = "radar_zones/learner/night_learning"
NL_SET = "radar_zones/learner/night_learning/set"
SELECT_STATE = "radar_zones/review/select"
SELECT_SET = "radar_zones/review/select/set"
PENDING = "radar_zones/review/pending"
PENDING_ATTR = "radar_zones/review/pending/attr"
ITEM_TEXT = "radar_zones/review/item_text"
ANSWER = "radar_zones/review/answer"
EVENT = "radar_zones/review/event"
RF_ON = "radar_zones/scan/rf_on_24h"
RF_SAVING = "radar_zones/scan/rf_saving_7d"
COVERAGE = "radar_zones/learner/coverage_7d"


class P4ServiceTest(P4Harness, unittest.TestCase):
    def make_learner(self):
        return FakeLearner()

    # ------------------------------------------------------------ start and discovery
    def test_start_publishes_p4_entities_and_default_states(self):
        self.connect()
        configs = [p[0] for p in self.client.published if p[0].endswith("/config")]
        self.assertEqual(len(configs), 4 + 12)
        self.assertTrue(all(p[3] is False for p in self.client.published if p[0].endswith("/config")))
        self.assertEqual(self.published(NL_STATE)[0][1:], (b"OFF", 1, True))        # Night learning: OFF by default
        self.assertEqual(self.published(VALID)[0][1:], (b"offline", 1, True))
        self.assertEqual(self.last(SELECT_STATE), "None")
        self.assertEqual(self.last(PENDING), "0")
        self.assertEqual(self.last(ITEM_TEXT), "None")
        self.assertEqual(self.last(RF_ON), "None")                                  # no radar state seen: unknown
        self.assertEqual(self.last(RF_SAVING), "None")
        self.assertEqual(self.last(COVERAGE), "0")
        attr = json.loads(self.last("radar_zones/learner/attr"))
        self.assertFalse(attr["node_schedule_seen"])
        self.assertEqual(attr["node_cmds_sent"], 0)

    def test_ha_birth_republishes_p4_discovery_and_states(self):
        self.connect()
        self.client.published.clear()
        self.client.fire_message("homeassistant/status", "online")
        self.pump()
        self.clock.advance(1.5)
        self.svc.step(0)
        self.assertEqual(len([p for p in self.client.published if p[0].endswith("/config")]), 16)
        self.assertEqual(self.last(NL_STATE), "OFF")
        self.assertEqual(self.last(VALID), "offline")

    def test_every_input_topic_is_subscribed(self):
        self.connect()
        self.assertEqual(len(self.client.subscriptions), 18)

    # ------------------------------------------------------------ radar data valid
    def test_valid_follows_the_learner_the_observing_topic_and_the_node(self):
        self.connect()
        self.svc._execute(StatusChanged("learning", {}))
        self.assertEqual(self.last(VALID), "online")
        self.client.fire_message("led_matrix_node/radar/observing", "OFF", retain=True)
        self.pump()
        self.assertEqual(self.last(VALID), "offline")                   # 'Radar observing' OFF
        self.client.fire_message("led_matrix_node/radar/observing", "ON", retain=True)
        self.pump()
        self.assertEqual(self.last(VALID), "online")
        self.client.fire_message("led_matrix_node/sensors/status", "offline", retain=True)
        self.pump()
        self.assertEqual(self.last(VALID), "offline")                   # the node's LWT
        self.client.fire_message("led_matrix_node/sensors/status", "online", retain=True)
        self.pump()
        self.assertEqual(self.last(VALID), "online")
        self.svc._execute(StatusChanged("not_observing", {}))           # frame silence, radars off or warming up
        self.assertEqual(self.last(VALID), "offline")
        self.svc._execute(StatusChanged("no_data", {}))
        self.assertEqual(self.last(VALID), "offline")
        self.svc._execute(StatusChanged("learning", {}))
        self.assertEqual(self.last(VALID), "online")
        self.svc.shutdown(0)
        self.assertEqual(self.last(VALID), "offline")

    def test_valid_is_published_once_per_change(self):
        self.connect()
        n = len(self.published(VALID))
        for _ in range(3):
            self.svc._execute(StatusChanged("not_observing", {}))
        self.assertEqual(len(self.published(VALID)), n)

    # ------------------------------------------------------------ Night learning and the node
    def night_learning_responder(self):
        def respond(ev):
            if isinstance(ev, NightLearningSwitch) and ev.on:
                return [NightLearningChanged(True, "2026-01-12", None),
                        NodeCommand("nl-start", "schedule", sched=False, expect_current=True,
                                    reason="night_learning_start")]
            if isinstance(ev, NightLearningSwitch):
                return [NightLearningChanged(False, None, None),
                        NodeCommand("nl-end", "schedule", sched=True, expect_current=False,
                                    reason="night_learning_end")]
            return []
        return respond

    def test_night_learning_is_off_and_the_node_gets_nothing(self):
        self.svc.start()
        self.client.fire_connect(0)
        node = FakeNode(self.client, sched=True)
        node.announce()
        self.pump(node)
        self.client.published.clear()
        self.run_for(24 * 3600, node, step=600.0)
        self.assertEqual(self.node_publishes(), [])                    # no schedule OFF, no radar-on nights
        self.assertEqual(node.received, [])
        self.assertTrue(self.p4.node.p2_seen)

    def test_night_learning_start_and_end_against_the_fake_node(self):
        self.learner.responder = self.night_learning_responder()
        self.svc.start()
        self.client.fire_connect(0)
        node = FakeNode(self.client, sched=True)
        node.announce()
        self.pump(node)
        self.client.fire_message(NL_SET, "ON")
        self.pump(node)
        self.assertEqual(node.received, [("led_matrix_node/radar/schedule", "0")])
        self.assertFalse(node.sched)
        self.assertEqual(self.last(NL_STATE), "ON")
        results = [e for e in self.learner.events if isinstance(e, CommandResult)]
        self.assertEqual([(r.token, r.ok, r.reason) for r in results], [("nl-start", True, None)])
        self.assertEqual(self.published(EVENT), [])                    # the echo came: nothing to report
        self.assertEqual(self.p4.node.confirmed, 1)
        # night 8: back ON, because nobody else changed it
        self.client.fire_message(NL_SET, "OFF")
        self.pump(node)
        self.assertEqual(node.received[-1], ("led_matrix_node/radar/schedule", "1"))
        self.assertTrue(node.sched)
        self.assertEqual(self.last(NL_STATE), "OFF")
        results = [e for e in self.learner.events if isinstance(e, CommandResult)]
        self.assertEqual([(r.token, r.ok) for r in results], [("nl-start", True), ("nl-end", True)])
        # only the schedule topic was ever written, never the power switch
        topics = {p[0] for p in self.node_publishes()}
        self.assertEqual(topics, {"led_matrix_node/radar/schedule"})

    def test_night_learning_does_nothing_on_a_node_without_p2(self):
        self.learner.responder = self.night_learning_responder()
        self.svc.start()
        self.client.fire_connect(0)
        node = FakeNode(self.client, p2=False)
        node.announce()
        self.pump(node)
        self.client.published.clear()
        self.client.fire_message(NL_SET, "ON")
        self.pump(node)
        self.run_for(300, node)
        self.assertEqual(self.node_publishes(), [])                    # nothing under led_matrix_node/
        results = [e for e in self.learner.events if isinstance(e, CommandResult)]
        self.assertEqual([(r.token, r.ok, r.reason) for r in results], [("nl-start", False, "no_p2")])
        self.assertEqual(self.published(EVENT), [])                    # a refusal is not a device failure

    def test_missing_echo_raises_device_not_confirmed(self):
        self.learner.responder = self.night_learning_responder()
        self.svc.start()
        self.client.fire_connect(0)
        node = FakeNode(self.client, sched=True, echo=False)
        node.announce()
        self.pump(node)
        self.client.fire_message(NL_SET, "ON")
        self.pump(node)
        self.assertEqual(node.received, [("led_matrix_node/radar/schedule", "0")])
        self.run_for(90, node)
        self.assertEqual(self.published(EVENT), [])
        self.run_for(60, node)                                          # more than 2 min passed
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual(events, [{"kind": "schedule", "event_type": "device_not_confirmed"}])
        self.assertEqual(self.published(EVENT)[0][2:], (1, False))      # an event: not retained
        results = [e for e in self.learner.events if isinstance(e, CommandResult)]
        self.assertEqual([(r.token, r.ok, r.reason) for r in results], [("nl-start", False, "no_echo")])
        self.run_for(300, node)
        self.assertEqual(len(self.published(EVENT)), 1)                 # once
        self.assertEqual(self.p4.attributes()["node_cmds_unconfirmed"], 1)       # shown with the next status attrs
        self.assertEqual(self.svc.attributes(StatusChanged("learning", {}))["node_cmds_unconfirmed"], 1)

    def test_the_learner_never_overrides_a_change_by_the_user(self):
        self.learner.responder = self.night_learning_responder()
        self.svc.start()
        self.client.fire_connect(0)
        node = FakeNode(self.client, sched=True)
        node.announce()
        self.pump(node)
        self.client.fire_message(NL_SET, "ON")
        self.pump(node)
        self.assertFalse(node.sched)
        node.user_sets_schedule(True)                                   # the user switches the schedule ON again
        self.pump(node)
        before = list(node.received)
        self.client.fire_message(NL_SET, "OFF")                         # the learner wants to switch it ON
        self.pump(node)
        self.assertEqual(node.received, before)                         # not sent: the user's change stands
        results = [e for e in self.learner.events if isinstance(e, CommandResult)]
        self.assertEqual(results[-1].reason, "changed_by_user")
        self.assertTrue(node.sched)

    def test_approved_window_is_written_and_confirmed(self):
        def respond(ev):
            if isinstance(ev, ReviewAnswer) and ev.op == "approve":
                return [NodeCommand("w1", "window", sched=ev.sched or None, start_min=1410, end_min=405,
                                    reason="approved_window")]
            return []
        self.learner.responder = respond
        self.svc.start()
        self.client.fire_connect(0)
        node = FakeNode(self.client, sched=False)
        node.announce()
        self.pump(node)
        self.learner.tick_outputs = [ReviewChanged((ReviewItem(ITEM_NIGHT_WINDOW, 4, ITEM_NIGHT_WINDOW,
                                                               {"start_min": 1410, "end_min": 405}),),
                                                   ITEM_NIGHT_WINDOW)]
        self.run_for(2, node, step=1.5)
        self.client.fire_message(ANSWER, json.dumps({"op": "approve", "sched": True}))
        self.pump(node)
        self.assertEqual(node.received, [("led_matrix_node/radar/night_start", "23:30:00"),
                                         ("led_matrix_node/radar/night_end", "06:45:00"),
                                         ("led_matrix_node/radar/schedule", "1")])     # the times first, then ON
        results = [e for e in self.learner.events if isinstance(e, CommandResult)]
        self.assertEqual([(r.token, r.ok) for r in results], [("w1", True)])

    # ------------------------------------------------------------ review items
    def two_items(self, selected):
        return ReviewChanged((ReviewItem(ITEM_NIGHT_WINDOW, 2, ITEM_NIGHT_WINDOW,
                                         {"start_min": 1410, "end_min": 405, "saving_pct": 30}),
                              ReviewItem(ITEM_BED, 1, ITEM_BED, {})), selected)

    def test_review_items_select_and_answers(self):
        def respond(ev):
            if isinstance(ev, ReviewSelect):
                return [self.two_items(ev.item)]
            return []
        self.learner.responder = respond
        node = self.connect(sched=True)
        self.client.published.clear()
        self.learner.tick_outputs = [self.two_items(ITEM_NIGHT_WINDOW)]
        self.run_for(2, node, step=1.5)
        self.assertEqual(self.last(SELECT_STATE), "Night window")
        self.assertEqual(self.last(PENDING), "2")
        self.assertIn("Switch both radars off between 23:30 and 06:45.", self.last(ITEM_TEXT))
        attr = json.loads(self.last(PENDING_ATTR))
        self.assertEqual(attr["selected"], ITEM_NIGHT_WINDOW)
        self.assertEqual([(i["id"], i["rev"]) for i in attr["items"]], [(ITEM_NIGHT_WINDOW, 2), (ITEM_BED, 1)])
        # the user picks the bed item
        self.client.fire_message(SELECT_SET, "Bed zone")
        self.pump(node)
        self.assertIn(ReviewSelect(ITEM_BED, self.learner.events[-1].recv_ms), self.learner.events)
        self.assertEqual(self.last(SELECT_STATE), "Bed zone")
        self.assertIn("Bed zone: lie in bed and press Mark spot here once, before 00:00 or after 06:00",
                      self.last(ITEM_TEXT))
        # the schedule is switched OFF: the 'before ... or after ...' part goes
        node.user_sets_schedule(False)
        self.pump(node)
        self.assertNotIn("before", self.last(ITEM_TEXT))
        self.assertNotIn("before", json.loads(self.last(PENDING_ATTR))["items"][1]["text"])
        # [No bed in view] = the Reject button: the item and its revision are filled in
        self.client.fire_message(ANSWER, json.dumps({"op": "reject"}))
        self.pump(node)
        answers = [e for e in self.learner.events if isinstance(e, ReviewAnswer)]
        self.assertEqual([(a.item, a.op, a.rev, a.sched) for a in answers], [(ITEM_BED, "reject", 1, False)])

    def test_stale_and_orphan_answers_are_dropped(self):
        self.connect()
        self.learner.tick_outputs = [self.two_items(ITEM_NIGHT_WINDOW)]
        self.run_for(2, step=1.5)
        self.client.fire_message(ANSWER, json.dumps({"op": "approve", "item": ITEM_NIGHT_WINDOW, "rev": 1}))
        self.pump()
        self.assertEqual([e for e in self.learner.events if isinstance(e, ReviewAnswer)], [])   # stale revision
        self.client.fire_message(ANSWER, json.dumps({"op": "approve", "item": ITEM_NIGHT_WINDOW, "rev": 2}))
        self.pump()
        self.assertEqual(len([e for e in self.learner.events if isinstance(e, ReviewAnswer)]), 1)
        self.learner.tick_outputs = [ReviewChanged((), None)]                                   # everything answered
        self.run_for(2, step=1.5)
        self.assertEqual(self.last(PENDING), "0")
        self.assertEqual(self.last(SELECT_STATE), "None")
        self.client.fire_message(ANSWER, json.dumps({"op": "later"}))
        self.pump()
        self.assertEqual(len([e for e in self.learner.events if isinstance(e, ReviewAnswer)]), 1)
        self.assertEqual(self.p4.dropped_answers, 2)

    def test_mark_failed_event(self):
        self.connect()
        self.svc._execute(MarkResult(False, "several_stays", None, None))
        self.svc._execute(MarkResult(True, None, "l0001", "bed"))
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual(events, [{"reason": "several_stays", "event_type": "mark_failed"}])

    def test_restore_failed_event(self):
        """The core gives up putting the schedule back: the user is told (README 9.5), once per give-up."""
        self.connect()
        self.svc._execute(NightLearningChanged(False, None, None, "restore_failed"))
        self.svc._execute(NightLearningChanged(False, None, None, "restore_overdue"))
        self.svc._execute(NightLearningChanged(False, None, None, "finished"))
        self.svc._execute(NightLearningChanged(False, None, None, "no_echo"))
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual(events, [{"reason": "no_confirmation", "event_type": "restore_failed"},
                                  {"reason": "overdue", "event_type": "restore_failed"}])
        self.assertEqual(self.published(EVENT)[0][2:], (1, False))

    def test_review_events_wait_for_the_broker_link(self):
        """A missing echo is most often an outage: the event that reports it must not be lost with it."""
        self.connect()
        self.client.fire_disconnect()
        self.svc._execute(MarkResult(False, "no_stay", None, None))
        self.svc._execute(NightLearningChanged(False, None, None, "restore_overdue"))
        self.assertEqual(self.published(EVENT), [])
        self.run_for(5)
        self.assertEqual(self.published(EVENT), [])                     # still down: still waiting
        self.client.fire_connect(0)
        self.pump()
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual([e["event_type"] for e in events], ["mark_failed", "restore_failed"])   # in order
        self.run_for(30)
        self.client.fire_message("homeassistant/status", "online")
        self.pump()
        self.run_for(15)
        self.assertEqual(len(self.published(EVENT)), 2)                 # each one only once

    def test_only_the_newest_waiting_events_are_kept(self):
        self.connect()
        self.client.fire_disconnect()
        for k in range(25):
            self.svc._execute(MarkResult(False, "several_stays" if k % 2 else "no_stay", None, None))
        self.client.fire_connect(0)
        self.pump()
        self.assertEqual(len(self.published(EVENT)), 20)

    # ------------------------------------------------------------ other inputs
    def test_lux_and_contexts_reach_the_core(self):
        self.connect()
        self.client.fire_message("led_matrix_node/sensors/state", '{"co2":600,"lux":2.5}')
        self.client.fire_message("radar_zones/context/guests", "on", retain=True)
        self.client.fire_message("radar_zones/context/lights_off", "off", retain=True)
        self.pump()
        self.assertIn(LuxSample(2.5, self.clock.clock_ms()), self.learner.events)
        names = [(e.name, e.value) for e in self.learner.events if e.__class__.__name__ == "ContextState"]
        self.assertEqual(names, [("guests", True), ("lights_off", False)])

    def test_radar_topics_do_not_reach_the_core(self):
        self.connect()
        self.client.fire_message("led_matrix_node/radar/state", "night_off", retain=True)
        self.client.fire_message("led_matrix_node/radar/observing", "OFF", retain=True)
        self.pump()
        self.assertEqual(self.learner.events, [])

    def test_retained_p4_commands_are_cleared_and_not_executed(self):
        self.connect()
        self.client.published.clear()
        for topic in (NL_SET, SELECT_SET, ANSWER):
            self.client.fire_message(topic, "ON", retain=True)
        self.pump()
        cleared = [p for p in self.client.published if p[1] == b"" and p[3] is True]
        self.assertEqual({p[0] for p in cleared}, {NL_SET, SELECT_SET, ANSWER})
        self.assertEqual(self.learner.events, [])

    # ------------------------------------------------------------ RF and coverage numbers
    def test_rf_numbers_from_the_radar_state(self):
        self.connect()
        self.client.fire_message("led_matrix_node/radar/state", "on", retain=True)
        self.pump()
        self.svc._execute(StatusChanged("learning", {}))
        self.run_for(3 * 3600, step=60.0)                              # 3 h on and observing
        self.client.fire_message("led_matrix_node/radar/state", "night_off", retain=True)
        self.pump()
        self.svc._execute(StatusChanged("not_observing", {}))
        self.run_for(3600, step=60.0)                                  # 1 h off
        self.svc._republish()                                          # all states again, now
        self.assertEqual(self.last(RF_ON), "3")
        self.assertEqual(self.last(RF_SAVING), "25")
        self.assertEqual(self.last(COVERAGE), "2")                     # 3 h of 168 h
        # a node that goes offline leaves the numbers as they are and counts neither on nor off
        self.client.fire_message("led_matrix_node/sensors/status", "offline", retain=True)
        self.pump()
        self.run_for(2 * 3600, step=60.0)
        self.svc._republish()
        self.assertEqual(self.last(RF_SAVING), "25")

    def test_rf_numbers_are_saved_and_loaded(self):
        self.connect()
        self.client.fire_message("led_matrix_node/radar/state", "on", retain=True)
        self.pump()
        self.run_for(2 * 3600, step=60.0)
        self.svc.shutdown(0)
        self.assertTrue((self.dir / "rf_stats.json").exists())
        again = P4Layer(self.topics, lambda *a, **k: True, self.dir)
        again.start(self.clock.clock_ms())
        self.assertEqual(again.rf.rf_on_24h(self.clock.clock_ms() / 1000.0), 2.0)

    # ------------------------------------------------------------ robustness
    def test_an_error_in_the_p4_layer_does_not_stop_the_core(self):
        self.connect()

        def boom(ev, now_ms):
            raise RuntimeError("p4 bug")
        self.p4.on_event = boom
        self.client.fire_message("radar_zones/context/guests", "on", retain=True)
        self.pump()
        self.assertEqual(len(self.learner.events), 1)                  # the event still went to the core
        self.assertEqual(self.svc.shell_errors, 1)

    def test_without_the_p4_layer_the_app_is_the_p3_app(self):
        settings = self.settings
        factory = FakeClientFactory()
        events = queue.Queue()
        t3 = build_topics(settings)
        mqtt = MqttIO(settings, t3, events, client_factory=factory, clock_ms=self.clock.clock_ms,
                      monotonic=self.clock.monotonic)
        svc = service_mod.Service(settings, lambda state: FakeLearner(state), mqtt, self.store, Stub(), Stub(),
                                  self.clock.clock_ms, self.clock.monotonic, topics=t3, events=events,
                                  rng=FixedRandom())
        svc.start()
        factory.last.fire_connect(0)
        svc.step(0)
        self.assertEqual(len([p for p in factory.last.published if p[0].endswith("/config")]), 4)
        self.assertEqual(len(factory.last.subscriptions), 9)
        self.assertEqual(factory.last.published_to(VALID), [])


if __name__ == "__main__":
    unittest.main()
