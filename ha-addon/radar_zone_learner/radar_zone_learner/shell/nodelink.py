"""The learner's only way to the node: the night schedule switch and the night window times (design 4.2, 7.9).

- Writes go to ``<node>/radar/schedule`` (``1``/``0``), ``<node>/radar/night_start`` and ``.../night_end``
  (``HH:MM:SS``, what an MQTT ``time`` entity sends), never retained, through the same topics as the user.
  Nothing else under the node prefix is ever published, in particular nothing to ``radar/power``: the learner
  never powers the radars directly.
- **Nothing is sent to a node without the P2 firmware.** The P2 node publishes ``radar/schedule/state``
  retained, so its echo arrives right after the subscription; until one has been seen, a command is refused
  (``no_p2``) and nothing is published. The same applies while the node is known to be offline.
- **Never over a change by somebody else.** A command may carry ``expect_current``, the schedule value the
  learner believes the node has. If the node's last echo differs, the user changed it: nothing is sent
  (``changed_by_user``).
- **Echo check.** A command counts as done when the node's state topics show the new values; if they do not
  within 2 minutes the result is ``no_echo`` (the service raises 'device did not confirm', OA 7.9).
- **Order.** An approved window writes ``night_start`` and ``night_end`` first and the schedule switch last.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from .p4_types import NODE_COMMAND_KINDS, CommandResult, NodeCommand

ECHO_TIMEOUT_MS = 120_000          # OA 7.9: the retained state topics echo within 2 min

log = logging.getLogger("radar_zone_learner.nodelink")


def hhmmss(minute: int) -> str:
    return "%02d:%02d:00" % (minute // 60, minute % 60)


def _valid_minute(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1440


@dataclass
class _Pending:
    token: str
    kind: str
    want_sched: Optional[bool]
    want_start: Optional[int]
    want_end: Optional[int]
    deadline_ms: int


class NodeLink:
    def __init__(self, topics, publish: Callable[..., bool]) -> None:
        self.topics = topics
        self._publish = publish
        # the node's last echo (None = never seen)
        self.sched: Optional[bool] = None
        self.start_min: Optional[int] = None
        self.end_min: Optional[int] = None
        self.node_online: Optional[bool] = None
        self._pending: list = []
        self._logged_no_p2 = False
        self.sent = 0
        self.confirmed = 0
        self.unconfirmed = 0
        self.refused = 0

    # ------------------------------------------------------------ what the node says
    @property
    def p2_seen(self) -> bool:
        """The node has published its schedule state: it runs the P2 firmware."""
        return self.sched is not None

    @property
    def pending(self) -> int:
        return len(self._pending)

    def on_node_status(self, online: bool) -> None:
        self.node_online = bool(online)

    def on_schedule_state(self, ev, now_ms: int) -> list:
        """Takes the three echo values of a ``ScheduleState`` and returns the results it completes."""
        if ev.sched is not None:
            self.sched = bool(ev.sched)
        if _valid_minute(ev.start_min):
            self.start_min = ev.start_min
        if _valid_minute(ev.end_min):
            self.end_min = ev.end_min
        done = []
        for p in list(self._pending):
            if self._satisfied(p):
                self._pending.remove(p)
                self.confirmed += 1
                done.append(CommandResult(p.token, p.kind, True, None, now_ms))
        return done

    def _satisfied(self, p: _Pending) -> bool:
        return ((p.want_sched is None or self.sched == p.want_sched)
                and (p.want_start is None or self.start_min == p.want_start)
                and (p.want_end is None or self.end_min == p.want_end))

    # ------------------------------------------------------------ commands
    def send(self, cmd: NodeCommand, now_ms: int) -> list:
        """Publishes the command (or refuses it); returns the results that are known at once."""
        bad = self._refusal(cmd)
        if bad is not None:
            return [self._refuse(cmd, bad, now_ms)]
        want_sched = cmd.sched
        want_start = want_end = None
        if cmd.kind == "window":
            want_start, want_end = cmd.start_min, cmd.end_min
        # only what differs from the node's own state is sent. The times go first and the schedule switch last
        # (OA 7.8: 'Approve writes #3/#4 and, if ticked, sets #2 ON'): with the switch first the node would apply
        # the OLD window at once, and an approval inside the old window but outside the new one would power the
        # radars down and up again before the new times arrive.
        todo = []
        if want_start is not None and self.start_min != want_start:
            todo.append((self.topics.radar_night_start_cmd, hhmmss(want_start)))
        if want_end is not None and self.end_min != want_end:
            todo.append((self.topics.radar_night_end_cmd, hhmmss(want_end)))
        if want_sched is not None and self.sched != want_sched:
            todo.append((self.topics.radar_schedule_cmd, "1" if want_sched else "0"))
        results = []
        for p in list(self._pending):                  # a newer command of the same kind replaces the older
            if p.kind == cmd.kind:
                self._pending.remove(p)
                results.append(CommandResult(p.token, p.kind, False, "superseded", now_ms))
        if not todo:
            results.append(CommandResult(cmd.token, cmd.kind, True, "unchanged", now_ms))
            return results
        for topic, payload in todo:
            if not self._publish(topic, payload, qos=1, retain=False):
                results.append(self._refuse(cmd, "not_connected", now_ms))
                return results
        self.sent += 1
        log.info("sent %s to the node (%s)", cmd.kind, cmd.reason or "no reason given")
        self._pending.append(_Pending(cmd.token, cmd.kind, want_sched, want_start, want_end,
                                      now_ms + ECHO_TIMEOUT_MS))
        return results

    def _refusal(self, cmd: NodeCommand) -> Optional[str]:
        if cmd.kind not in NODE_COMMAND_KINDS:
            return "bad_command"
        if cmd.kind == "schedule" and cmd.sched is None:
            return "bad_command"
        if cmd.kind == "window":
            if cmd.start_min is None and cmd.end_min is None:
                return "bad_command"
            for v in (cmd.start_min, cmd.end_min):
                if v is not None and not _valid_minute(v):
                    return "bad_command"
        if not self.p2_seen:
            if not self._logged_no_p2:
                log.info("the node has not published its schedule state (no P2 firmware): "
                         "commands to the node are not sent")
                self._logged_no_p2 = True
            return "no_p2"
        if self.node_online is False:
            return "node_offline"
        if cmd.expect_current is not None and self.sched != cmd.expect_current:
            return "changed_by_user"
        return None

    def _refuse(self, cmd: NodeCommand, reason: str, now_ms: int) -> CommandResult:
        self.refused += 1
        log.info("command %s not sent: %s", cmd.kind, reason)
        return CommandResult(cmd.token, cmd.kind, False, reason, now_ms)

    def poll(self, now_ms: int) -> list:
        """Results of commands whose echo did not come in time."""
        late = [p for p in self._pending if now_ms >= p.deadline_ms]
        out = []
        for p in late:
            self._pending.remove(p)
            self.unconfirmed += 1
            log.warning("the node did not confirm %s within %d s", p.kind, ECHO_TIMEOUT_MS // 1000)
            out.append(CommandResult(p.token, p.kind, False, "no_echo", now_ms))
        return out
