"""P4: Night learning against a fake node (OA 4.2 (b), 7.6, 7.7): off by default, the schedule is switched OFF for 7
nights and back ON, `set_by_learner` is recorded, a change by the user is never overridden, nothing is sent to a
node without P2."""
import copy
import json
import unittest
from datetime import date

from radar_zone_learner.core import Learner
from radar_zone_learner.core import nightlearn as NL
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import (CommandResult, FixedOffsetTime, NightLearningChanged, NightLearningSwitch,
                                           NodeCommand, NodeStatus, SaveRequested, ScheduleState, StateError,
                                           StatusChanged)

from .test_extraction import BASE_MS, DAY, make

D0 = date(2026, 1, 5).toordinal()
H = 3600000
M = 60000


def day_ms(day, h=0, m=0):
    return BASE_MS + day * DAY * 1000 + h * H + m * M


class FakeNode:
    """The node as the core sees it through the shell: answers every NodeCommand with a CommandResult and an echo.

    ``p2`` False: a node without the schedule topics. ``echo`` False: it never confirms (the shell reports
    no_echo). ``offline``: the shell refuses with node_offline. A command with ``expect_current`` that does not
    match the node's schedule is refused with changed_by_user, as ``shell.nodelink`` does."""

    def __init__(self, L, sched=True, start=0, end=360, p2=True, echo=True, offline=False):
        self.L = L
        self.sched = sched if p2 else None
        self.start, self.end = start, end
        self.p2, self.echo, self.offline = p2, echo, offline
        self.commands: list = []
        self.results: list = []

    def announce(self, now_ms):
        if self.p2:
            return self.L.handle(ScheduleState(self.sched, self.start, self.end, now_ms))
        return []

    def user_sets(self, sched, now_ms):
        self.sched = sched
        return self.drive(self.L.handle(ScheduleState(sched, self.start, self.end, now_ms)), now_ms)

    def respond(self, cmd, now_ms):
        self.commands.append(cmd)
        if not self.p2:
            return [CommandResult(cmd.token, cmd.kind, False, "no_p2", now_ms)]
        if self.offline:
            return [CommandResult(cmd.token, cmd.kind, False, "node_offline", now_ms)]
        if cmd.expect_current is not None and self.sched != cmd.expect_current:
            return [CommandResult(cmd.token, cmd.kind, False, "changed_by_user", now_ms)]
        if not self.echo:
            return [CommandResult(cmd.token, cmd.kind, False, "no_echo", now_ms + 120000)]
        self.sched = cmd.sched
        return [ScheduleState(self.sched, self.start, self.end, now_ms),
                CommandResult(cmd.token, cmd.kind, True, None, now_ms)]

    def drive(self, outs, now_ms):
        """Executes the NodeCommands among ``outs`` and feeds the answers back; returns every output."""
        allouts = list(outs)
        todo = [o for o in outs if isinstance(o, NodeCommand)]
        while todo:
            cmd = todo.pop(0)
            for ev in self.respond(cmd, now_ms):
                new = self.L.handle(ev)
                allouts += new
                todo += [o for o in new if isinstance(o, NodeCommand)]
                if isinstance(ev, CommandResult):
                    self.results.append(ev)
        return allouts


def tick(L, node, ms):
    return node.drive(L.tick(ms), ms)


def nl_changes(outs):
    return [(o.active, o.until, o.set_by_learner) for o in outs if isinstance(o, NightLearningChanged)]


def nl_events(outs):
    """The ``event`` of every NightLearningChanged among ``outs`` (what happened, for the shell)."""
    return [o.event for o in outs if isinstance(o, NightLearningChanged) and o.event]


def make_node(**kw):
    L = make()
    node = FakeNode(L, **kw)
    node.announce(day_ms(0, 0, 1))
    return L, node


def switch(L, node, on, ms):
    return node.drive(L.handle(NightLearningSwitch(on, ms)), ms)


class DefaultsTest(unittest.TestCase):
    def test_off_by_default_and_nothing_is_ever_sent(self):
        L, node = make_node(sched=True)
        for d in range(1, 12):                                           # a fresh install through the setup review
            outs = tick(L, node, day_ms(d, 3, 30))
            outs += tick(L, node, day_ms(d, 12, 0))
            self.assertEqual([o for o in outs if isinstance(o, (NodeCommand, NightLearningChanged))], [], d)
        self.assertEqual(node.commands, [])
        a = L.status().attributes["night_learning"]
        self.assertEqual(a, {"active": False, "set_by_learner": None, "until": None, "phase": "idle",
                             "last_event": None})
        self.assertEqual(L.export_state()["night"]["night_learning"],
                         {"active": False, "until": None, "set_by_learner": None})

    def test_turning_it_off_when_it_is_off_changes_nothing(self):
        L, node = make_node()
        outs = switch(L, node, False, day_ms(0, 12))
        self.assertEqual(nl_changes(outs), [(False, None, None)])
        self.assertEqual(node.commands, [])

    def test_a_node_without_p2_gets_nothing(self):
        L, node = make_node(p2=False)
        self.assertIsNone(L.window["sched"])
        outs = switch(L, node, True, day_ms(0, 12))
        self.assertEqual(nl_changes(outs), [(False, None, None)])
        self.assertEqual(node.commands, [])
        self.assertEqual(L.nl.last_event, "no_p2")
        self.assertFalse(L.nl.active)
        for d in range(1, 10):
            self.assertEqual([o for o in tick(L, node, day_ms(d, 6, 1)) if isinstance(o, NodeCommand)], [])


class RunTest(unittest.TestCase):
    def test_off_for_7_nights_then_back_on(self):
        L, node = make_node(sched=True)
        outs = switch(L, node, True, day_ms(0, 20))
        cmd = node.commands[0]
        self.assertEqual((cmd.kind, cmd.sched, cmd.expect_current, cmd.reason),
                         ("schedule", False, True, "night_learning_start"))
        self.assertEqual((cmd.start_min, cmd.end_min), (None, None))
        self.assertFalse(node.sched)
        self.assertEqual(nl_changes(outs)[0], (True, "2026-01-12", False))     # set_by_learner: it wrote OFF
        kinds = [type(o) for o in outs if isinstance(o, (NodeCommand, NightLearningChanged, StatusChanged))]
        self.assertEqual(kinds[:3], [NodeCommand, NightLearningChanged, StatusChanged])    # the output order
        self.assertTrue(any(isinstance(o, SaveRequested) and o.reason == "night_learning" for o in outs))
        self.assertEqual(L.nl.phase, "running")
        st = L.export_state()["night"]["night_learning"]
        self.assertEqual(st, {"active": True, "until": "2026-01-12", "set_by_learner": False})
        # nothing happens for 7 nights: the nights of days 1..7 start at 00:00; the last one ends 06:00 on day 7
        for d in range(1, 7):
            self.assertEqual([o for o in tick(L, node, day_ms(d, 12)) if isinstance(o, NodeCommand)], [])
        self.assertEqual([o for o in tick(L, node, day_ms(7, 5, 59)) if isinstance(o, NodeCommand)], [])
        self.assertTrue(L.nl.active)
        outs = tick(L, node, day_ms(7, 6, 0))
        end = node.commands[1]
        self.assertEqual((end.sched, end.expect_current, end.reason), (True, False, "night_learning_end"))
        self.assertTrue(node.sched)
        self.assertIn((False, None, None), nl_changes(outs))
        self.assertEqual(nl_changes(outs)[0], (False, "2026-01-12", False))   # 'ending': the restore is out
        self.assertEqual(L.nl.last_event, "finished")
        self.assertEqual(L.export_state()["night"]["night_learning"],
                         {"active": False, "until": None, "set_by_learner": None})
        self.assertEqual(len(node.commands), 2)
        self.assertEqual([o for o in tick(L, node, day_ms(8, 6, 0)) if isinstance(o, NodeCommand)], [])

    def test_the_schedule_is_already_off(self):
        L, node = make_node(sched=False)
        outs = switch(L, node, True, day_ms(0, 20))
        self.assertEqual(node.commands, [])
        self.assertEqual(nl_changes(outs), [(True, "2026-01-12", None)])         # not ours: set_by_learner None
        tick(L, node, day_ms(7, 6, 1))
        self.assertEqual(node.commands, [])                                      # and never put back
        self.assertFalse(L.nl.active)
        self.assertFalse(node.sched)
        self.assertEqual(L.nl.last_event, "finished")

    def test_a_second_switch_on_does_nothing(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        switch(L, node, True, day_ms(0, 20, 1))
        self.assertEqual(len(node.commands), 1)

    def test_nights_are_counted_from_the_first_window_start(self):
        for (h, m, until) in ((2, 0, "2026-01-12"),      # inside tonight's window: the first night is the next one
                              (0, 0, "2026-01-12"),
                              (23, 59, "2026-01-12")):
            L, node = make_node()
            switch(L, node, True, day_ms(0, h, m))
            self.assertEqual(L.nl.export()["until"], until, (h, m))
        # a window across midnight: 23:00-07:00. Started at 20:00 the first night starts at 23:00 the same day
        L, node = make_node(start=23 * 60, end=7 * 60)
        switch(L, node, True, day_ms(0, 20))
        self.assertEqual(L.nl.export()["until"], "2026-01-12")                   # 7th start day 6 23:00, end day 7
        L, node = make_node(start=23 * 60, end=7 * 60)
        switch(L, node, True, day_ms(0, 23, 30))                                 # after tonight's start
        self.assertEqual(L.nl.export()["until"], "2026-01-13")
        # no window at all: 7 days
        L, node = make_node(start=300, end=300)
        switch(L, node, True, day_ms(0, 20))
        self.assertEqual(L.nl.export()["until"], "2026-01-12")

    def test_the_restore_time_is_the_window_end_of_the_until_date(self):
        L, node = make_node(start=23 * 60, end=7 * 60)
        switch(L, node, True, day_ms(0, 20))
        self.assertEqual([o for o in tick(L, node, day_ms(7, 6, 59)) if isinstance(o, NodeCommand)], [])
        tick(L, node, day_ms(7, 7, 0))
        self.assertEqual(len(node.commands), 2)


class FailureTest(unittest.TestCase):
    def test_the_node_does_not_confirm_the_start(self):
        """The OFF was published: it may still take effect, so it stays the learner's own and is watched."""
        L, node = make_node(echo=False)
        outs = switch(L, node, True, day_ms(0, 20))
        res = node.results[-1]
        self.assertEqual((res.ok, res.reason), (False, "no_echo"))
        self.assertEqual(nl_changes(outs)[-1], (False, None, False))             # cancelled, set_by_learner kept
        self.assertFalse(L.nl.active)
        self.assertEqual(L.nl.last_event, "no_echo")
        self.assertEqual((L.nl.phase, L.nl.set_by), ("restoring", False))
        self.assertEqual(L.export_state()["night"]["night_learning"],
                         {"active": False, "until": None, "set_by_learner": False})
        self.assertEqual(len(node.commands), 1)                                  # and no second attempt
        # the echo stays ON for the whole watch (15 min): the OFF never happened, nothing to put back
        tick(L, node, day_ms(0, 20, 10))
        self.assertEqual((L.nl.phase, L.nl.set_by), ("restoring", False))
        tick(L, node, day_ms(0, 20, 18))                                         # 15 min after the 20:02 verdict
        self.assertEqual((L.nl.phase, L.nl.set_by, L.nl.last_event), ("idle", None, "no_echo"))
        self.assertEqual(len(node.commands), 1)
        self.assertEqual(L.export_state()["night"]["night_learning"],
                         {"active": False, "until": None, "set_by_learner": None})

    def test_a_late_off_echo_after_no_echo_is_put_back(self):
        """The node applied the OFF, its echo came after the 2 min: the schedule must not stay OFF."""
        L, node = make_node(echo=False)
        switch(L, node, True, day_ms(0, 20))
        self.assertEqual(L.nl.phase, "restoring")
        node.echo = True
        node.sched = False                                                       # it had applied the OFF
        outs = node.drive(L.handle(ScheduleState(False, 0, 360, day_ms(0, 20, 3))), day_ms(0, 20, 3))
        self.assertEqual([(c.sched, c.expect_current, c.reason) for c in node.commands],
                         [(False, True, "night_learning_start"), (True, False, "night_learning_end")])
        self.assertTrue(node.sched)
        self.assertEqual((L.nl.phase, L.nl.set_by, L.nl.active), ("idle", None, False))
        self.assertEqual(nl_changes(outs)[-1], (False, None, None))
        tick(L, node, day_ms(0, 21))
        self.assertEqual(len(node.commands), 2)

    def test_a_late_off_echo_inside_the_watch_but_after_a_restart(self):
        L, node = make_node(echo=False)
        switch(L, node, True, day_ms(0, 20))
        st = json.loads(json.dumps(L.export_state()))
        L2 = Learner(P, FixedOffsetTime(0), st)
        self.assertEqual((L2.nl.phase, L2.nl.set_by, L2.nl.active), ("restoring", False, False))
        node2 = FakeNode(L2, sched=False)                                        # the retained echo: the OFF is in force
        node2.drive(node2.announce(day_ms(0, 20, 30)), day_ms(0, 20, 30))
        tick(L2, node2, day_ms(0, 20, 31))
        self.assertEqual([(c.sched, c.expect_current) for c in node2.commands], [(True, False)])
        self.assertEqual((L2.nl.phase, L2.nl.set_by), ("idle", None))
        self.assertTrue(node2.sched)

    def test_a_stored_schedule_value_is_not_trusted_after_a_restart(self):
        """The state holds the schedule as last seen (ON); the node's own echo, not yet in, may say OFF."""
        L, node = make_node(echo=False)
        switch(L, node, True, day_ms(0, 20))
        st = json.loads(json.dumps(L.export_state()))
        L2 = Learner(P, FixedOffsetTime(0), st)
        self.assertIs(L2.window["sched"], True)                                  # the stale value
        node2 = FakeNode(L2, sched=False)
        for k in range(1, 6):                                                    # ticks before the echo: no verdict
            tick(L2, node2, day_ms(0, 20, 20 + k))
        self.assertEqual((L2.nl.phase, L2.nl.set_by), ("restoring", False))
        node2.drive(node2.announce(day_ms(0, 20, 27)), day_ms(0, 20, 27))
        tick(L2, node2, day_ms(0, 20, 28))
        self.assertTrue(node2.sched)                                             # put back once the echo said OFF
        self.assertEqual(L2.nl.set_by, None)

    def test_the_echo_of_on_after_a_restart_ends_it(self):
        L, node = make_node(echo=False)
        switch(L, node, True, day_ms(0, 20))
        L2 = Learner(P, FixedOffsetTime(0), json.loads(json.dumps(L.export_state())))
        node2 = FakeNode(L2, sched=True)
        node2.drive(node2.announce(day_ms(0, 20, 30)), day_ms(0, 20, 30))
        self.assertEqual((L2.nl.phase, L2.nl.set_by, L2.nl.last_event), ("idle", None, "finished"))
        self.assertEqual(node2.commands, [])

    def test_refused_commands(self):
        L, node = make_node()
        node.offline = True
        switch(L, node, True, day_ms(0, 20))
        self.assertFalse(L.nl.active)
        self.assertEqual(L.nl.last_event, "node_offline")
        L, node = make_node()
        L.handle(NightLearningSwitch(True, day_ms(0, 20)))
        tok = L.nl.token
        L.handle(CommandResult(tok, "schedule", False, "no_p2", day_ms(0, 20, 1)))
        self.assertEqual((L.nl.active, L.nl.last_event), (False, "no_p2"))

    def test_the_shell_never_answers(self):
        L, node = make_node()
        L.handle(NightLearningSwitch(True, day_ms(0, 20)))                       # no node.drive: no result
        self.assertTrue(L.nl.active)
        L.tick(day_ms(0, 20, 4))
        self.assertTrue(L.nl.active)
        outs = L.tick(day_ms(0, 20, 5))                                          # 300 s: treated as no echo
        self.assertFalse(L.nl.active)
        self.assertEqual(L.nl.last_event, "no_echo")
        self.assertEqual(nl_changes(outs)[-1], (False, None, False))             # the OFF may still arrive: watched
        self.assertEqual(L.nl.phase, "restoring")

    def test_a_result_for_another_token_is_ignored(self):
        L, node = make_node()
        L.handle(NightLearningSwitch(True, day_ms(0, 20)))
        L.handle(CommandResult("nl999", "schedule", False, "no_echo", day_ms(0, 20, 1)))
        self.assertTrue(L.nl.active)


class UserChangeTest(unittest.TestCase):
    def test_a_change_by_the_user_cancels_it_and_is_never_overridden(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        outs = node.user_sets(True, day_ms(3, 12))                               # the user switches the schedule ON
        self.assertEqual(nl_changes(outs), [(False, None, None)])
        self.assertEqual(L.nl.last_event, "changed_by_user")
        self.assertFalse(L.nl.active)
        self.assertEqual(L.nl.phase, "idle")
        for d in range(4, 12):
            self.assertEqual([o for o in tick(L, node, day_ms(d, 6, 1)) if isinstance(o, NodeCommand)], [])
        self.assertEqual(len(node.commands), 1)
        self.assertTrue(node.sched)

    def test_the_echo_of_our_own_off_is_not_a_change(self):
        L, node = make_node()
        L.handle(NightLearningSwitch(True, day_ms(0, 20)))                       # arming: the old ON may still arrive
        L.handle(ScheduleState(True, 0, 360, day_ms(0, 20, 0) + 500))
        self.assertTrue(L.nl.active)
        self.assertEqual(L.nl.phase, "arming")

    def test_at_the_end_a_changed_schedule_is_left_alone(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        node.sched = True                                                        # changed without an echo reaching us
        outs = tick(L, node, day_ms(7, 6, 0))
        self.assertEqual(node.commands[1].expect_current, False)                 # the shell refuses: changed_by_user
        self.assertEqual(node.results[-1].reason, "changed_by_user")
        self.assertEqual(L.nl.last_event, "changed_by_user")
        self.assertFalse(L.nl.active)
        self.assertIsNone(L.nl.set_by)
        self.assertEqual(len(node.commands), 2)                                  # no retry
        self.assertIn((False, None, None), nl_changes(outs))

    def test_an_echo_of_on_at_the_end_means_nothing_to_restore(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        L.window["sched"] = True                                                 # the user's ON, seen at the very end
        L.nl.phase = "running"
        tick(L, node, day_ms(7, 6, 0))
        self.assertEqual(len(node.commands), 1)
        self.assertEqual(L.nl.last_event, "finished")


class StopAndRetryTest(unittest.TestCase):
    def test_switching_it_off_early_puts_the_schedule_back(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        outs = switch(L, node, False, day_ms(2, 12))
        self.assertEqual(node.commands[1].sched, True)
        self.assertEqual(node.commands[1].expect_current, False)
        self.assertTrue(node.sched)
        self.assertEqual(nl_changes(outs)[0], (False, None, False))              # off at once, restore pending
        self.assertEqual(nl_changes(outs)[-1], (False, None, None))              # confirmed
        self.assertFalse(L.nl.active)
        self.assertEqual(L.nl.phase, "idle")
        self.assertEqual(L.nl.last_event, "finished")

    def test_stopping_before_the_start_was_confirmed(self):
        """The OFF may still reach the node: the learner waits for its echo, then puts the schedule back."""
        L, node = make_node()
        L.handle(NightLearningSwitch(True, day_ms(0, 20)))                       # the OFF is out, not answered yet
        outs = node.drive(L.handle(NightLearningSwitch(False, day_ms(0, 20, 1))), day_ms(0, 20, 1))
        self.assertEqual(node.commands, [])                                      # nothing yet: wait for the echo
        self.assertEqual((L.nl.active, L.nl.phase, L.nl.set_by), (False, "restoring", False))
        old = L.window["sched"]
        outs = node.drive(L.handle(ScheduleState(True, 0, 360, day_ms(0, 20, 1) + 500)), day_ms(0, 20, 1) + 500)
        self.assertEqual(node.commands, [])                                      # a stale ON echo is not the answer
        node.sched = False
        outs = node.drive(L.handle(ScheduleState(False, 0, 360, day_ms(0, 20, 2))), day_ms(0, 20, 2))
        self.assertEqual([(c.sched, c.expect_current) for c in node.commands], [(True, False)])
        self.assertTrue(node.sched)
        self.assertEqual((L.nl.phase, L.nl.set_by), ("idle", None))
        self.assertTrue(old)

    def test_stopping_before_the_start_was_confirmed_and_it_never_happened(self):
        L, node = make_node()
        L.handle(NightLearningSwitch(True, day_ms(0, 20)))
        L.handle(NightLearningSwitch(False, day_ms(0, 20, 1)))
        node.drive(L.tick(day_ms(0, 20, 3)), day_ms(0, 20, 3))
        self.assertEqual(L.nl.phase, "restoring")                                # the watch (15 min) is running
        node.drive(L.tick(day_ms(0, 20, 14)), day_ms(0, 20, 14))
        self.assertEqual(L.nl.phase, "restoring")
        node.drive(L.tick(day_ms(0, 20, 17)), day_ms(0, 20, 17))
        self.assertEqual(node.commands, [])                                      # the echo stayed ON: nothing to undo
        self.assertEqual((L.nl.phase, L.nl.set_by, L.nl.last_event), ("idle", None, "finished"))

    def test_an_unconfirmed_restore_is_sent_again_and_gives_up(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        node.echo = False                                                        # the node never confirms the ON
        t = day_ms(7, 6, 0)
        outs = tick(L, node, t)
        self.assertEqual(len(node.commands), 2)
        self.assertEqual(L.nl.phase, "restoring")
        self.assertEqual(L.nl.set_by, False)                                     # still its OFF to put back
        tick(L, node, t + 9 * M)
        self.assertEqual(len(node.commands), 2)                                  # waits nl_retry_s (10 min)
        gave_up = []
        for k in range(1, 15):                                                   # (each missing echo takes 2 min)
            gave_up += nl_events(tick(L, node, t + k * 13 * M))
        self.assertEqual(len(node.commands), 13)                                 # the start, then nl_restore_tries = 12
        self.assertEqual((L.nl.phase, L.nl.active, L.nl.set_by), ("idle", False, None))
        self.assertEqual(L.nl.last_event, "restore_failed")
        self.assertEqual(gave_up, ["restore_failed"])                            # the shell is told, once
        self.assertEqual(nl_events(outs), ["ending"])
        n = len(node.commands)
        tick(L, node, t + 30 * 13 * M)
        self.assertEqual(len(node.commands), n)

    def test_a_refused_restore_is_no_try_and_waits_for_the_node(self):
        """A node that is offline at the window end for hours still gets the restore when it comes back."""
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        node.offline = True
        t = day_ms(7, 6, 0)
        tick(L, node, t)
        for k in range(1, 31):                                                   # 5 hours of refusals, every 10 min
            events = nl_events(tick(L, node, t + k * 10 * M + 1000))
            self.assertEqual(events, [], k)
        self.assertEqual((L.nl.phase, L.nl.set_by, L.nl.last_event), ("restoring", False, "restore_node_offline"))
        self.assertEqual(len(node.commands), 32)                                 # the start, then 31 refused tries
        self.assertEqual({r.reason for r in node.results[1:]}, {"node_offline"})
        node.offline = False
        outs = tick(L, node, t + 31 * 10 * M + 1000)
        self.assertTrue(node.sched)
        self.assertEqual((L.nl.phase, L.nl.set_by, L.nl.last_event), ("idle", None, "finished"))
        self.assertEqual(nl_events(outs)[-1], "finished")

    def test_the_node_coming_back_is_tried_at_once(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        node.offline = True
        t = day_ms(7, 6, 0)
        tick(L, node, t)
        self.assertEqual(L.nl.last_event, "restore_node_offline")
        n = len(node.commands)
        node.offline = False
        outs = node.drive(L.handle(NodeStatus(True, t + 2 * M)), t + 2 * M)      # 2 min later, not 10
        self.assertEqual(len(node.commands), n + 1)
        self.assertTrue(node.sched)
        self.assertEqual(L.nl.phase, "idle")
        self.assertTrue(any(isinstance(o, NodeCommand) for o in outs))

    def test_the_schedule_echo_of_a_returning_node_is_tried_at_once(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        node.offline = True
        t = day_ms(7, 6, 0)
        tick(L, node, t)
        n = len(node.commands)
        node.offline = False
        node.drive(L.handle(ScheduleState(False, 0, 360, t + 2 * M)), t + 2 * M)
        self.assertEqual(len(node.commands), n + 1)
        self.assertTrue(node.sched)

    def test_a_restore_that_is_a_day_overdue_is_not_sent(self):
        """The node was offline for more than 24 h: that OFF may be the user's by now. Nothing is sent, the shell is told."""
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        node.offline = True
        t = day_ms(7, 6, 0)
        tick(L, node, t)
        seen = []
        for k in range(1, 160):                                                  # 26 h of refusals
            seen += nl_events(tick(L, node, t + k * 10 * M + 1000))
        self.assertEqual(seen, ["restore_overdue"])
        self.assertEqual((L.nl.phase, L.nl.set_by, L.nl.last_event), ("idle", None, "restore_overdue"))
        node.offline = False
        n = len(node.commands)
        tick(L, node, t + 30 * 3600 * 1000)
        node.drive(L.handle(NodeStatus(True, t + 30 * 3600 * 1000)), t + 30 * 3600 * 1000)
        self.assertEqual(len(node.commands), n)                                  # and never later
        self.assertFalse(node.sched)

    def test_a_run_that_was_not_seen_to_its_end_is_not_restored_over_the_user(self):
        """The app was down for 30 days; the OFF in the node may be the user's own: no command, an event."""
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        L2 = Learner(P, FixedOffsetTime(0), json.loads(json.dumps(L.export_state())))
        node2 = FakeNode(L2, sched=False)
        seen = nl_events(node2.drive(node2.announce(day_ms(30, 12)), day_ms(30, 12)))
        for k in range(0, 12):
            seen += nl_events(tick(L2, node2, day_ms(30, 12, 1) + k * M))
        self.assertEqual(node2.commands, [])                                     # nothing was published
        self.assertEqual(seen, ["restore_overdue"])
        self.assertEqual((L2.nl.phase, L2.nl.set_by, L2.nl.active), ("idle", None, False))
        self.assertFalse(node2.sched)

    def test_a_run_that_ended_a_few_hours_ago_is_still_restored(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        L2 = Learner(P, FixedOffsetTime(0), json.loads(json.dumps(L.export_state())))
        node2 = FakeNode(L2, sched=False)
        node2.drive(node2.announce(day_ms(7, 12)), day_ms(7, 12))               # back at noon of the last day
        tick(L2, node2, day_ms(7, 12, 1))
        self.assertEqual([(c.sched, c.expect_current) for c in node2.commands], [(True, False)])
        self.assertTrue(node2.sched)

    def test_a_restore_that_works_on_the_second_try(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        node.offline = True
        t = day_ms(7, 6, 0)
        tick(L, node, t)
        node.offline = False
        tick(L, node, t + 10 * M + 1000)
        self.assertTrue(node.sched)
        self.assertEqual((L.nl.phase, L.nl.last_event, L.nl.set_by), ("idle", "finished", None))
        self.assertEqual(len(node.commands), 3)


class RestartTest(unittest.TestCase):
    def reload(self, L):
        st = json.loads(json.dumps(L.export_state()))
        return Learner(P, FixedOffsetTime(0), st)

    def test_a_running_night_learning_survives_a_restart(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        L2 = self.reload(L)
        self.assertEqual((L2.nl.active, L2.nl.set_by, L2.nl.phase), (True, False, "running"))
        self.assertEqual(L2.status().attributes["night_learning"]["until"], "2026-01-12")
        node2 = FakeNode(L2, sched=False)
        node2.announce(day_ms(3, 12))                                            # the retained echo: OFF, as we left it
        self.assertTrue(L2.nl.active)
        tick(L2, node2, day_ms(7, 6, 0))
        self.assertEqual([(c.sched, c.expect_current) for c in node2.commands], [(True, False)])
        self.assertTrue(node2.sched)

    def test_a_user_change_during_the_restart_is_seen_in_the_retained_echo(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        L2 = self.reload(L)
        node2 = FakeNode(L2, sched=True)                                         # the user put it back meanwhile
        node2.announce(day_ms(3, 12))
        self.assertFalse(L2.nl.active)
        self.assertEqual(L2.nl.last_event, "changed_by_user")
        tick(L2, node2, day_ms(7, 6, 0))
        self.assertEqual(node2.commands, [])

    def test_a_restore_in_flight_resumes_after_a_restart(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        node.offline = True
        switch(L, node, False, day_ms(2, 12))                                    # stop: the restore could not be sent
        self.assertEqual((L.nl.active, L.nl.set_by, L.nl.phase), (False, False, "restoring"))
        L2 = self.reload(L)
        self.assertEqual((L2.nl.active, L2.nl.set_by, L2.nl.phase), (False, False, "restoring"))
        node2 = FakeNode(L2, sched=False)
        node2.drive(node2.announce(day_ms(2, 13)), day_ms(2, 13))               # the echo: OFF, as we left it
        tick(L2, node2, day_ms(2, 13, 1))
        self.assertEqual([(c.sched, c.expect_current) for c in node2.commands], [(True, False)])
        self.assertEqual((L2.nl.phase, L2.nl.set_by), ("idle", None))

    def test_a_refused_restore_after_a_restart_is_tried_again(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        node.offline = True
        switch(L, node, False, day_ms(2, 12))
        L2 = self.reload(L)                       # the shell has not seen the node's retained echo yet: no_p2
        self.assertIs(L2.window["sched"], False)  # the last known value is persisted
        node2 = FakeNode(L2, sched=False, p2=False)
        tick(L2, node2, day_ms(2, 13))
        self.assertEqual([r.reason for r in node2.results], ["no_p2"])
        self.assertEqual((L2.nl.phase, L2.nl.set_by), ("restoring", False))
        tick(L2, node2, day_ms(2, 13, 5))
        self.assertEqual(len(node2.commands), 1)                                 # waits 10 min
        node2.p2 = True
        node2.sched = False
        tick(L2, node2, day_ms(2, 13, 12))
        self.assertEqual(len(node2.commands), 2)
        self.assertEqual((L2.nl.phase, L2.nl.set_by), ("idle", None))


class StateTest(unittest.TestCase):
    def test_validation(self):
        good = {"active": True, "until": "2026-01-12", "set_by_learner": False}
        NL.validate(good, P)
        NL.validate({"active": False, "until": None, "set_by_learner": None}, P)
        NL.validate({"active": False, "until": "2026-01-12", "set_by_learner": False}, P)
        for mut in ({"active": 1}, {"set_by_learner": "no"}, {"until": "x"}, {"until": None}, {"until": 5}):
            bad = dict(good)
            bad.update(mut)
            with self.assertRaises(StateError):
                NL.validate(bad, P)
        with self.assertRaises(StateError):
            NL.validate(None, P)

    def test_export_roundtrip_through_the_learner_state(self):
        L, node = make_node()
        switch(L, node, True, day_ms(0, 20))
        st = json.loads(json.dumps(L.export_state()))
        self.assertEqual(st["night"]["night_learning"], {"active": True, "until": "2026-01-12",
                                                          "set_by_learner": False})
        bad = copy.deepcopy(st)
        bad["night"]["night_learning"]["until"] = None
        with self.assertRaises(StateError):
            Learner(P, FixedOffsetTime(0), bad)

    def test_status_is_published_on_every_change(self):
        L, node = make_node()
        outs = switch(L, node, True, day_ms(0, 20))
        sts = [o for o in outs if isinstance(o, StatusChanged)]
        self.assertTrue(sts)
        self.assertEqual(sts[-1].attributes["night_learning"]["active"], True)
        self.assertEqual(sts[-1].attributes["night_learning"]["set_by_learner"], False)
        order = [type(o) for o in outs if isinstance(o, (NodeCommand, NightLearningChanged, StatusChanged))]
        self.assertEqual(order[:2], [NodeCommand, NightLearningChanged])         # commands first, status after


if __name__ == "__main__":
    unittest.main()
