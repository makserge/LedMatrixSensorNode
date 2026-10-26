import unittest
from pathlib import Path

from radar_zone_learner.core.types import ScheduleState
from radar_zone_learner.shell import nodelink as nodelink_mod
from radar_zone_learner.shell.nodelink import ECHO_TIMEOUT_MS, NodeLink, hhmmss
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.p4_types import CommandResult, NodeCommand
from radar_zone_learner.shell.topics_p4 import build_topics_p4
from tests.fakes import quiet_logs

SCHED = "led_matrix_node/radar/schedule"
START = "led_matrix_node/radar/night_start"
END = "led_matrix_node/radar/night_end"


class Pub:
    def __init__(self):
        self.sent = []
        self.ok = True

    def __call__(self, topic, payload, qos=1, retain=False):
        if not self.ok:
            return False
        self.sent.append((topic, payload, qos, retain))
        return True


def state(sched=None, start=None, end=None, ms=0):
    return ScheduleState(sched, start, end, ms)


class NodeLinkTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        s = Settings(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                     debug_map=True, mqtt_host="h", mqtt_port=1883, mqtt_username=None, mqtt_password=None,
                     mqtt_ssl=False, data_dir=Path("."))
        self.pub = Pub()
        self.link = NodeLink(build_topics_p4(s), self.pub)

    def p2_node(self, sched=True, start=0, end=360):
        self.link.on_node_status(True)
        self.link.on_schedule_state(state(sched, start, end), 1000)

    def test_hhmmss(self):
        self.assertEqual(hhmmss(0), "00:00:00")
        self.assertEqual(hhmmss(1425), "23:45:00")

    # ------------------------------------------------------------ no P2: nothing is sent
    def test_nothing_is_sent_before_the_p2_echo(self):
        self.assertFalse(self.link.p2_seen)
        for cmd in (NodeCommand("a", "schedule", sched=False),
                    NodeCommand("b", "window", start_min=1410, end_min=390, sched=True)):
            res = self.link.send(cmd, 5000)
            self.assertEqual(res, [CommandResult(cmd.token, cmd.kind, False, "no_p2", 5000)])
        self.assertEqual(self.pub.sent, [])
        self.assertEqual(self.link.refused, 2)
        self.assertEqual(self.link.pending, 0)

    def test_a_node_that_only_sent_start_and_end_times_is_not_p2(self):
        self.link.on_schedule_state(state(None, 0, 360), 1)         # times without the schedule switch
        self.assertFalse(self.link.p2_seen)
        self.assertEqual(self.link.send(NodeCommand("a", "schedule", sched=False), 2)[0].reason, "no_p2")
        self.assertEqual(self.pub.sent, [])

    def test_refused_while_the_node_is_offline(self):
        self.p2_node()
        self.link.on_node_status(False)
        res = self.link.send(NodeCommand("a", "schedule", sched=False), 5000)
        self.assertEqual(res[0].reason, "node_offline")
        self.assertEqual(self.pub.sent, [])

    # ------------------------------------------------------------ schedule command and echo
    def test_schedule_command_and_echo(self):
        self.p2_node(sched=True)
        res = self.link.send(NodeCommand("nl1", "schedule", sched=False, expect_current=True,
                                         reason="night_learning_start"), 10_000)
        self.assertEqual(res, [])
        self.assertEqual(self.pub.sent, [(SCHED, "0", 1, False)])       # not retained, QoS 1
        self.assertEqual(self.link.pending, 1)
        # a stale echo (the old value) confirms nothing
        self.assertEqual(self.link.on_schedule_state(state(True, 0, 360), 11_000), [])
        done = self.link.on_schedule_state(state(False, 0, 360), 12_000)
        self.assertEqual(done, [CommandResult("nl1", "schedule", True, None, 12_000)])
        self.assertEqual(self.link.pending, 0)
        self.assertEqual(self.link.confirmed, 1)
        self.assertEqual(self.link.poll(10_000 + ECHO_TIMEOUT_MS + 1), [])

    def test_no_echo_within_two_minutes(self):
        self.p2_node(sched=True)
        self.link.send(NodeCommand("nl1", "schedule", sched=False), 10_000)
        self.assertEqual(self.link.poll(10_000 + ECHO_TIMEOUT_MS - 1), [])
        late = self.link.poll(10_000 + ECHO_TIMEOUT_MS)
        self.assertEqual(late, [CommandResult("nl1", "schedule", False, "no_echo", 10_000 + ECHO_TIMEOUT_MS)])
        self.assertEqual(self.link.unconfirmed, 1)
        self.assertEqual(self.link.poll(10_000 + 2 * ECHO_TIMEOUT_MS), [])   # reported once
        self.assertEqual(ECHO_TIMEOUT_MS, 120_000)

    def test_already_the_value_sends_nothing(self):
        self.p2_node(sched=False)
        res = self.link.send(NodeCommand("x", "schedule", sched=False), 5000)
        self.assertEqual(res, [CommandResult("x", "schedule", True, "unchanged", 5000)])
        self.assertEqual(self.pub.sent, [])
        self.assertEqual(self.link.pending, 0)

    def test_never_over_a_change_by_the_user(self):
        self.p2_node(sched=True)
        self.link.send(NodeCommand("off", "schedule", sched=False, expect_current=True), 1000)
        self.link.on_schedule_state(state(False, 0, 360), 2000)       # the echo: learner's OFF is in force
        self.pub.sent.clear()
        # night 8: the learner wants to switch back ON because it believes the schedule is OFF (its own write);
        # but the user switched it ON in the meantime, and later OFF again by hand
        self.link.on_schedule_state(state(True, 0, 360), 3000)
        res = self.link.send(NodeCommand("on", "schedule", sched=True, expect_current=False), 4000)
        self.assertEqual(res, [CommandResult("on", "schedule", False, "changed_by_user", 4000)])
        self.assertEqual(self.pub.sent, [])

    def test_expect_current_matching_sends(self):
        self.p2_node(sched=False)
        self.assertEqual(self.link.send(NodeCommand("on", "schedule", sched=True, expect_current=False), 4000), [])
        self.assertEqual(self.pub.sent, [(SCHED, "1", 1, False)])

    def test_publish_failure_is_a_refusal(self):
        self.p2_node(sched=True)
        self.pub.ok = False
        res = self.link.send(NodeCommand("a", "schedule", sched=False), 5000)
        self.assertEqual(res[0].reason, "not_connected")
        self.assertEqual(self.link.pending, 0)

    # ------------------------------------------------------------ window command
    def test_window_command_sends_only_what_differs(self):
        self.p2_node(sched=False, start=0, end=360)
        res = self.link.send(NodeCommand("w1", "window", start_min=1410, end_min=360, reason="approved_window"), 1000)
        self.assertEqual(res, [])
        self.assertEqual(self.pub.sent, [(START, "23:30:00", 1, False)])
        self.assertEqual(self.link.on_schedule_state(state(False, 1410, 360), 2000),
                         [CommandResult("w1", "window", True, None, 2000)])

    def test_window_command_with_the_switch_ticked(self):
        self.p2_node(sched=False, start=0, end=360)
        self.link.send(NodeCommand("w2", "window", sched=True, start_min=1425, end_min=405), 1000)
        # the times first, the switch last: the node never applies the old window with the schedule ON in between
        self.assertEqual(self.pub.sent, [(START, "23:45:00", 1, False), (END, "06:45:00", 1, False),
                                         (SCHED, "1", 1, False)])
        # the echo comes in three messages: only the last one completes the command
        self.assertEqual(self.link.on_schedule_state(state(True, 0, 360), 1100), [])
        self.assertEqual(self.link.on_schedule_state(state(True, 1425, 360), 1200), [])
        self.assertEqual(len(self.link.on_schedule_state(state(True, 1425, 405), 1300)), 1)

    def test_the_schedule_switch_is_published_after_the_times(self):
        """Approve writes #3/#4 and, if ticked, sets #2 ON (OA 7.8), in that order, whichever values differ."""
        self.p2_node(sched=False, start=0, end=360)
        self.link.send(NodeCommand("w", "window", sched=True, start_min=1410, end_min=360), 1000)   # end unchanged
        self.assertEqual([t for t, *_ in self.pub.sent], [START, SCHED])
        self.pub.sent.clear()
        self.p2_node(sched=False, start=0, end=360)
        self.link.send(NodeCommand("w3", "window", sched=True, start_min=0, end_min=405), 2000)     # start unchanged
        self.assertEqual([t for t, *_ in self.pub.sent], [END, SCHED])
        self.pub.sent.clear()
        self.link.send(NodeCommand("w4", "window", sched=True, start_min=1, end_min=2), 3000)
        self.assertEqual([t for t, *_ in self.pub.sent], [START, END, SCHED])

    def test_window_that_is_already_set(self):
        self.p2_node(sched=True, start=1410, end=390)
        res = self.link.send(NodeCommand("w", "window", sched=True, start_min=1410, end_min=390), 1000)
        self.assertEqual(res[0].reason, "unchanged")
        self.assertEqual(self.pub.sent, [])

    def test_a_newer_command_of_the_same_kind_supersedes(self):
        self.p2_node(sched=True)
        self.link.send(NodeCommand("a", "schedule", sched=False), 1000)
        res = self.link.send(NodeCommand("b", "schedule", sched=False), 2000)
        self.assertEqual(res, [CommandResult("a", "schedule", False, "superseded", 2000)])
        self.assertEqual(self.link.pending, 1)
        self.assertEqual(len(self.pub.sent), 2)
        self.assertEqual(self.link.on_schedule_state(state(False, 0, 360), 3000),
                         [CommandResult("b", "schedule", True, None, 3000)])

    def test_bad_commands(self):
        self.p2_node()
        for cmd in (NodeCommand("a", "schedule"), NodeCommand("b", "window"), NodeCommand("c", "power", sched=False),
                    NodeCommand("d", "window", start_min=1440), NodeCommand("e", "window", end_min=True)):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.link.send(cmd, 1)[0].reason, "bad_command")
        self.assertEqual(self.pub.sent, [])

    def test_only_the_three_schedule_topics_are_ever_used(self):
        self.p2_node(sched=False, start=0, end=360)
        self.link.send(NodeCommand("a", "schedule", sched=True), 1)
        self.link.send(NodeCommand("b", "window", sched=False, start_min=1, end_min=2), 2)
        used = {topic for topic, *_ in self.pub.sent}
        self.assertEqual(used, {SCHED, START, END})
        self.assertFalse([t for t in used if t.endswith("/power")])
        self.assertTrue(hasattr(nodelink_mod, "ECHO_TIMEOUT_MS"))


if __name__ == "__main__":
    unittest.main()
