"""The P4 shell layer with the real core Learner (the seam of p4_types.py against core/types.py), no broker."""
import json
import unittest

from radar_zone_learner.core.learner import Learner
from radar_zone_learner.core.params import DEFAULT_PARAMS
from radar_zone_learner.core.types import FixedOffsetTime
from tests.p4_harness import P4Harness

NL_SET = "radar_zones/learner/night_learning/set"
NL_STATE = "radar_zones/learner/night_learning"
SELECT_STATE = "radar_zones/review/select"
SELECT_SET = "radar_zones/review/select/set"
PENDING = "radar_zones/review/pending"
ITEM_TEXT = "radar_zones/review/item_text"
ANSWER = "radar_zones/review/answer"
EVENT = "radar_zones/review/event"
SCHEDULE = "led_matrix_node/radar/schedule"


class RealCoreP4Test(P4Harness, unittest.TestCase):
    def make_learner(self):
        self.made = []

        def make(state):
            learner = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), state)
            self.made.append(learner)
            return learner
        self.make = make
        return make(None)

    def test_night_learning_switch_on_with_a_p2_node(self):
        node = self.connect(sched=True)
        self.assertEqual(self.last(NL_STATE), "OFF")
        self.client.fire_message(NL_SET, "ON")
        self.pump(node)
        self.assertEqual(node.received, [(SCHEDULE, "0")])              # the schedule OFF, written once
        self.assertFalse(node.sched)
        self.assertEqual(self.last(NL_STATE), "ON")
        self.assertTrue(self.learner.nl.active)
        self.assertEqual(self.learner.nl.phase, "running")              # the echo came back as a CommandResult
        self.assertEqual({p[0] for p in self.node_publishes()}, {SCHEDULE})

    def test_night_learning_switch_on_without_p2_does_nothing(self):
        node = self.connect(p2=False)
        self.client.published.clear()
        self.client.fire_message(NL_SET, "ON")
        self.pump(node)
        self.run_for(600, node)
        self.assertEqual(self.node_publishes(), [])
        self.assertFalse(self.learner.nl.active)
        self.assertEqual([p[1] for p in self.published(NL_STATE)], [])   # still OFF: nothing new to say

    def test_night_learning_off_by_default_sends_nothing(self):
        node = self.connect(sched=True)
        self.client.published.clear()
        self.run_for(6 * 3600, node, step=300.0)
        self.assertEqual(self.node_publishes(), [])
        self.assertEqual(node.received, [])

    def test_the_user_switching_the_schedule_on_ends_night_learning_without_a_command(self):
        node = self.connect(sched=True)
        self.client.fire_message(NL_SET, "ON")
        self.pump(node)
        node.user_sets_schedule(True)
        self.pump(node)
        self.assertFalse(self.learner.nl.active)
        self.assertEqual(self.last(NL_STATE), "OFF")
        self.assertEqual(node.received, [(SCHEDULE, "0")])              # nothing was sent against the user
        self.assertTrue(node.sched)

    def test_a_late_echo_after_no_echo_does_not_strand_the_schedule(self):
        """The node applied the OFF, its echo came after the 2 minutes: the learner puts the schedule back."""
        node = self.connect(sched=True, echo=False)                      # applies commands, its echo is lost
        self.client.fire_message(NL_SET, "ON")
        self.pump(node)
        self.assertFalse(node.sched)
        self.run_for(130, node)                                          # more than 2 min without an echo
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual([e["event_type"] for e in events], ["device_not_confirmed"])
        self.assertEqual((self.learner.nl.phase, self.learner.nl.set_by), ("restoring", False))
        self.assertEqual(self.last(NL_STATE), "OFF")                     # the run is cancelled
        node.echo = True
        node.user_sets_schedule(False)                                   # the retained state '0' arrives late
        self.pump(node)
        self.assertEqual(node.received, [(SCHEDULE, "0"), (SCHEDULE, "1")])
        self.assertTrue(node.sched)
        self.assertEqual((self.learner.nl.phase, self.learner.nl.set_by), ("idle", None))
        self.run_for(1800, node, step=60.0)
        self.assertEqual(node.received, [(SCHEDULE, "0"), (SCHEDULE, "1")])

    def test_a_broker_outage_over_the_echo_loses_neither_the_event_nor_the_schedule(self):
        """The learner's own link is down for 150 s while the OFF takes effect; the retained echo reaches it after
        the reconnect, the review event is sent then, and the schedule is not left OFF."""
        node = self.connect(sched=True, echo=False)
        self.client.fire_message(NL_SET, "ON")
        self.pump(node)
        self.client.fire_disconnect()
        self.run_for(150, node)                                          # the 2 min pass while nobody listens
        self.assertEqual(self.published(EVENT), [])                      # (the link is down: nothing can go out)
        self.assertEqual((self.learner.nl.phase, self.learner.nl.set_by), ("restoring", False))
        self.client.fire_connect(0)
        node.echo = True
        node.announce()                                                  # the broker's retained state: OFF
        self.pump(node)
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual([e["event_type"] for e in events], ["device_not_confirmed"])    # not lost
        self.assertEqual(node.received, [(SCHEDULE, "0"), (SCHEDULE, "1")])
        self.assertTrue(node.sched)
        self.assertEqual((self.learner.nl.phase, self.learner.nl.set_by), ("idle", None))

    def test_a_restore_refused_by_an_offline_node_goes_when_it_is_back(self):
        node = self.connect(sched=True)
        self.client.fire_message(NL_SET, "ON")
        self.pump(node)
        self.assertEqual(self.learner.nl.phase, "running")
        self.client.fire_message("led_matrix_node/sensors/status", "offline", retain=True)
        self.pump(node)
        self.client.fire_message(NL_SET, "OFF")                          # the user stops it while the node is away
        self.pump(node)
        self.run_for(3 * 3600, node, step=600.0)
        self.assertEqual(node.received, [(SCHEDULE, "0")])               # refused: nothing published, and no give-up
        self.assertEqual((self.learner.nl.phase, self.learner.nl.set_by), ("restoring", False))
        self.assertEqual(self.published(EVENT), [])
        self.client.fire_message("led_matrix_node/sensors/status", "online", retain=True)
        self.pump(node)
        self.assertEqual(node.received, [(SCHEDULE, "0"), (SCHEDULE, "1")])
        self.assertEqual((self.learner.nl.phase, self.learner.nl.set_by), ("idle", None))

    def test_bed_item_and_its_answers(self):
        node = self.connect(sched=True)
        self.learner.night = 7                                          # the setup review is ready
        self.run_for(3, node, step=1.5)
        self.assertEqual(self.last(PENDING), "1")
        self.assertEqual(self.last(SELECT_STATE), "Bed zone")
        self.assertIn("press Mark spot here once, before 00:00 or after 06:00", self.last(ITEM_TEXT))
        node.user_sets_schedule(False)
        self.pump(node)
        self.assertNotIn("before", self.last(ITEM_TEXT))
        attr = json.loads(self.last("radar_zones/review/pending/attr"))
        self.assertEqual(attr["selected"], "bed_needs_data")
        # [Later]: the item goes until the next daily review
        self.client.fire_message(ANSWER, json.dumps({"op": "later"}))
        self.pump(node)
        self.run_for(3, node, step=1.5)
        self.assertEqual(self.last(PENDING), "0")
        self.assertEqual(self.last(SELECT_STATE), "None")

    def test_no_bed_in_view_is_the_reject_button(self):
        node = self.connect(sched=True)
        self.learner.night = 7
        self.run_for(3, node, step=1.5)
        self.assertEqual(self.last(PENDING), "1")
        self.client.fire_message(ANSWER, json.dumps({"op": "reject"}))
        self.pump(node)
        self.run_for(3, node, step=1.5)
        self.assertEqual(self.last(PENDING), "0")
        self.assertEqual([s["type"] for s in self.learner.export_state()["suppressions"]], ["bed_needs_data"])

    def test_mark_press_with_nobody_in_view_fires_mark_failed(self):
        node = self.connect(sched=True)
        self.learner.night = 7
        self.run_for(3, node, step=1.5)
        self.client.fire_message(SELECT_SET, "Bed zone")
        self.pump(node)
        self.client.fire_message("radar_zones/review/mark", "PRESS")
        self.pump(node)
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "mark_failed")
        self.assertIn(events[0]["reason"], ("not_observing", "no_stay"))

    def test_status_attributes_carry_the_p4_keys(self):
        self.connect(sched=True)
        self.svc._execute(self.learner.status())                      # the attributes go out with a status
        attr = json.loads(self.last("radar_zones/learner/attr"))
        self.assertFalse(attr["night_learning"]["active"])
        self.assertTrue(attr["node_schedule_seen"])
        self.assertEqual(attr["review_items"], 0)

    def test_lux_and_contexts_reach_the_real_core(self):
        node = self.connect(sched=True)
        self.client.fire_message("led_matrix_node/sensors/state", '{"lux":1.5}')
        self.client.fire_message("radar_zones/context/lights_off", "on", retain=True)
        self.client.fire_message("radar_zones/context/guests", "on", retain=True)
        self.pump(node)
        self.run_for(3, node, step=1.5)
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.svc.shell_errors, 0)


if __name__ == "__main__":
    unittest.main()
