"""Night learning (P4; OA 4.2 (b), 7.6, 7.7): optional, OFF by default.

Only when the user turns the learner's 'Night learning' switch on does the learner switch the node's 'Radar
night schedule' OFF for 7 nights (the radars then observe through the night, which finds the bed sooner) and
switch it back ON afterwards. The learner talks to the node only through ``NodeCommand`` outputs; the shell
sends them and answers each one with a ``CommandResult`` (the echo check of OA 7.9 lives there). Nothing here
touches a node that does not run the P2 firmware: the first command needs a schedule echo to have been seen.

State (persisted, ``night.night_learning``): ``active``, ``until`` (the ISO date the 7 nights end) and
``set_by_learner`` = the schedule value the learner last wrote (``False`` once it sent the schedule OFF,
``None`` when it wrote nothing or has put it back). It is the learner's record that the OFF may be its own, and
it is kept until the schedule is known to be back ON: an OFF that was sent and not confirmed can still take
effect later (a broker restart, a slow node), so it is never forgotten at 'no_echo'.

Phases (RAM): ``idle``, ``arming`` (OFF sent, no result yet), ``running`` (the schedule is OFF), ``restoring``
(ON sent or to be sent).

- Start: schedule echo ON -> send OFF (``expect_current`` = True), ``set_by_learner`` = False. Echo OFF already ->
  nothing to send, nothing to restore (``set_by_learner`` stays None). No echo ever seen -> refused, nothing sent.
  A refusal in which nothing was published (``no_p2``, ``node_offline``, ``changed_by_user``, ``not_connected``)
  changes nothing: the run does not start.
- No echo for the OFF (``no_echo``, or no answer within ``nl_result_timeout_s``): the run is cancelled, but the
  OFF may still reach the node, so the learner watches the schedule echo for ``nl_unconfirmed_wait_s``: an echo
  of OFF in that time is the learner's own and is put back; the watch ends 'never happened' when the echo
  still shows ON. The same watch follows a stop while the OFF is unconfirmed.
- While running: an echo of ON means somebody else switched it: the learner stops and does not override it.
- End (the window end of the date ``until``): ON is sent only if ``set_by_learner`` is False and the echo is
  still OFF (``expect_current`` = False; the shell refuses it if somebody changed it). The user switching it off
  early restores the same way.
- A restore that is refused because the node is offline, has not shown its schedule yet or the broker link is down
  is no try: it waits ``nl_retry_s`` (and is tried at once when the node comes back or its schedule echo is seen).
  A restore that is sent and not confirmed (``no_echo``) is tried ``nl_restore_tries`` times. When the learner
  gives up (``restore_failed``), or the restore is more than ``nl_restore_late_s`` overdue (``restore_overdue``,
  for example after the app was down for days: the schedule may be the user's own OFF by then), it sends
  nothing, forgets ``set_by_learner`` and tells the shell with the event (the review event ``restore_failed``).
- After a restart the persisted schedule value is not trusted to say 'back ON': that conclusion waits for the
  node's own echo.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from .night import local_epoch_ms
from .params import Params
from .types import NightLearningChanged, NodeCommand, SaveRequested, StateError

PHASES = ("idle", "arming", "running", "restoring")
WAIT_REASONS = ("node_offline", "no_p2", "not_connected")     # refusals in which nothing was published: not a try
GIVE_UP_EVENTS = ("restore_failed", "restore_overdue")        # the shell raises the review event for these


class NightLearning:
    def __init__(self, p: Params, time_model) -> None:
        self.p = p
        self.tm = time_model
        self.active = False
        self.until_ord: Optional[int] = None
        self.set_by: Optional[bool] = None
        self.phase = "idle"
        self.token: Optional[str] = None
        self.deadline_ms: Optional[int] = None
        self.next_try_ms: Optional[int] = None
        self.tries = 0
        self.await_until_ms: Optional[int] = None   # an OFF that was not confirmed: its echo is watched until then
        self.await_event: Optional[str] = None      # the event that stays when the watch ends with the schedule ON
        self.hold = False                           # the last restore was refused (node offline ...): retry on news
        self.due_ms: Optional[int] = None           # when the restore became due (RAM; for 'overdue')
        self.fresh = True                           # a schedule echo came in since the load (see ``load``)
        self.last_event: Optional[str] = None

    # ------------------------------------------------------------ persistence
    def export(self) -> dict:
        return {"active": self.active,
                "until": date.fromordinal(self.until_ord).isoformat() if self.until_ord is not None else None,
                "set_by_learner": self.set_by}

    def load(self, d: dict) -> None:
        self.active = d["active"]
        self.until_ord = date.fromisoformat(d["until"]).toordinal() if d["until"] is not None else None
        self.set_by = d["set_by_learner"]
        # RAM phases after a restart: a running Night learning runs on; a restore that was in flight resumes
        self.phase = "running" if self.active else ("restoring" if self.set_by is False else "idle")
        self.token = None
        self.deadline_ms = None
        self.next_try_ms = 0 if self.phase == "restoring" else None
        self.tries = 0
        self.await_until_ms = None
        self.await_event = None
        self.hold = False
        self.due_ms = None
        self.fresh = False               # the schedule value in the state is only what was last known

    # ------------------------------------------------------------ helpers
    def _emit(self, outs: list, event: Optional[str] = None, save: bool = True) -> None:
        if event is not None:
            self.last_event = event
        until = date.fromordinal(self.until_ord).isoformat() if self.until_ord is not None else None
        outs.append(NightLearningChanged(self.active, until, self.set_by, event))
        if save:
            outs.append(SaveRequested("night_learning"))

    def _until_for_start(self, L, now_ms: int) -> int:
        """The date the 7th night ends on: nights are counted from the first window start after now."""
        start, end = L.window["start"], L.window["end"]
        lt = self.tm.local(now_ms)
        n = self.p.nl_nights
        if start == end:                                  # a window that never happens: 7 days
            return lt.date_ord + n
        first = lt.date_ord if lt.sec_of_day < start * 60 else lt.date_ord + 1
        return first + n - 1 + (1 if end <= start else 0)

    def _restore_ms(self, L, now_ms: int) -> int:
        return local_epoch_ms(self.tm, self.until_ord, L.window["end"], now_ms)

    def _send(self, L, outs: list, now_ms: int, sched: bool, expect: bool, reason: str) -> None:
        self.token = L.next_token("nl")
        self.deadline_ms = now_ms + self.p.nl_result_timeout_s * 1000
        outs.append(NodeCommand(self.token, "schedule", sched, None, None, expect, reason))

    def _clear_token(self) -> None:
        self.token = None
        self.deadline_ms = None

    def _reset(self) -> None:
        """Back to idle: nothing is active and nothing is the learner's to put back."""
        self.active = False
        self.until_ord = None
        self.set_by = None
        self.phase = "idle"
        self._clear_token()
        self.next_try_ms = None
        self.tries = 0
        self.await_until_ms = None
        self.await_event = None
        self.hold = False
        self.due_ms = None

    def _done(self, outs: list, event: str) -> None:
        self._reset()
        self._emit(outs, event)

    def _restoring(self, due_ms: int) -> None:
        """The schedule is to be put back (its OFF is the learner's)."""
        self.active = False
        self.phase = "restoring"
        self.tries = 0
        self.hold = False
        self.due_ms = due_ms
        self.await_until_ms = None
        self.await_event = None

    def _watch(self, now_ms: int, keep_event: Optional[str]) -> None:
        """An OFF that was sent and not confirmed may still take effect: watch the echo for a while."""
        self.await_until_ms = now_ms + self.p.nl_unconfirmed_wait_s * 1000
        self.await_event = keep_event
        self.next_try_ms = self.await_until_ms

    # ------------------------------------------------------------ the user's switch
    def switch(self, L, on: bool, now_ms: int, outs: list) -> None:
        if on:
            if self.active or self.phase == "restoring":
                self._emit(outs, save=False)              # already on (or still putting the schedule back)
                return
            sched = L.window["sched"]
            if sched is None:                             # no P2 node: nothing is sent, the switch goes back off
                self._emit(outs, "no_p2", save=False)
                return
            self.active = True
            self.until_ord = self._until_for_start(L, now_ms)
            self.tries = 0
            if sched is True:
                self.phase = "arming"
                self.set_by = False                       # what the learner writes; undone below if it fails
                self._send(L, outs, now_ms, False, True, "night_learning_start")
            else:
                self.phase = "running"                    # the schedule is OFF already: not ours, never restored
                self.set_by = None
            self._emit(outs, "started")
            return
        if self.active:
            self._stop(L, now_ms, outs, "stopped")
        else:
            self._emit(outs, save=False)

    def _stop(self, L, now_ms: int, outs: list, event: str) -> None:
        was_arming = self.phase == "arming"
        self.active = False
        self.until_ord = None
        self._clear_token()
        if self.set_by is False:
            self._restoring(now_ms)
            if was_arming:
                self._watch(now_ms, None)                 # an OFF that is still unconfirmed may yet reach the node
            else:
                self.next_try_ms = now_ms
        else:
            self._reset()
        self._emit(outs, event)
        self._pump_restore(L, now_ms, outs)

    def _unconfirmed(self, now_ms: int, outs: list) -> None:
        """The OFF was published and its echo did not come. The run is cancelled, but the OFF may still take effect:
        ``set_by_learner`` stays False and the echo is watched (a late OFF is put back, never stranded)."""
        self.until_ord = None
        self._clear_token()
        self._restoring(now_ms)
        self._watch(now_ms, "no_echo")
        self._emit(outs, "no_echo")

    def _give_up(self, outs: list, event: str) -> None:
        """No restore will be sent: the schedule is not the learner's to touch any more; the shell tells the user."""
        self._done(outs, event)

    # ------------------------------------------------------------ what the node says
    def on_schedule(self, L, now_ms: int, outs: list, sched_seen: bool = True) -> None:
        """Called after a ScheduleState has been mirrored into L.window (``sched_seen``: it carried the switch)."""
        if not sched_seen:
            return
        self.fresh = True
        sched = L.window["sched"]
        if self.phase == "running" and sched is True:
            # somebody else switched the schedule ON while Night learning ran: never override it
            self._done(outs, "changed_by_user")
        elif self.phase == "restoring" and self.set_by is False:
            if self.await_until_ms is not None:
                if sched is False:                        # the unconfirmed OFF took effect after all: put it back
                    self.await_until_ms = None
                    self.next_try_ms = now_ms
                    self._pump_restore(L, now_ms, outs)
                # an ON while watching may still be the old value: the watch decides
            elif sched is True:
                self._done(outs, "finished")              # the echo of ON: the schedule is back
            elif self.hold:                               # the node showed itself: a refused restore goes again
                self.next_try_ms = now_ms
                self._pump_restore(L, now_ms, outs)

    def on_node_online(self, L, now_ms: int, outs: list) -> None:
        """The node's status went to online: a restore that waited for it is tried now."""
        if self.phase == "restoring" and self.hold and self.token is None:
            self.next_try_ms = now_ms
            self._pump_restore(L, now_ms, outs)

    def on_result(self, L, res, now_ms: int, outs: list) -> bool:
        """A CommandResult. True when the token was ours."""
        if self.token is None or res.token != self.token:
            return False
        self._clear_token()
        if self.phase == "arming":
            if res.ok:
                self.phase = "running"
                self._emit(outs, "running")
            elif res.reason == "no_echo":                 # published, not confirmed: it may still take effect
                self._unconfirmed(now_ms, outs)
            else:                                         # nothing was published: nothing was changed
                self._done(outs, str(res.reason or "failed")[:40])
        elif self.phase == "restoring":
            if res.ok:
                self._done(outs, "finished")
            elif res.reason == "changed_by_user":
                self._done(outs, "changed_by_user")
            elif res.reason in WAIT_REASONS:
                self._wait(now_ms, str(res.reason))
            else:
                self._retry_or_give_up(now_ms, outs, str(res.reason or "failed")[:40])
        return True

    def _retry_or_give_up(self, now_ms: int, outs: list, why: str) -> None:
        """A restore that was sent and not confirmed (or failed in another way) counts as a try."""
        self.tries += 1
        if self.tries >= self.p.nl_restore_tries:
            self._give_up(outs, "restore_failed")
        else:
            self.hold = False
            self.next_try_ms = now_ms + self.p.nl_retry_s * 1000
            self.last_event = "restore_" + why

    def _wait(self, now_ms: int, why: str) -> None:
        """A refused restore (nothing was published) is no try: it waits, and goes again on news from the node."""
        self.hold = True
        self.next_try_ms = now_ms + self.p.nl_retry_s * 1000
        self.last_event = "restore_" + why

    # ------------------------------------------------------------ time
    def tick(self, L, now_ms: int, outs: list) -> None:
        if self.token is not None and self.deadline_ms is not None and now_ms >= self.deadline_ms:
            self._clear_token()
            if self.phase == "arming":                    # the shell never answered: treat as no echo
                self._unconfirmed(now_ms, outs)
            elif self.phase == "restoring":
                self._retry_or_give_up(now_ms, outs, "no_echo")
        if self.phase == "running" and self.until_ord is not None:
            restore = self._restore_ms(L, now_ms)
            if now_ms >= restore:
                self._end_of_run(L, now_ms, restore, outs)
        self._pump_restore(L, now_ms, outs)

    def _end_of_run(self, L, now_ms: int, restore_ms: int, outs: list) -> None:
        sched = L.window["sched"]
        if self.set_by is not False:
            self._done(outs, "finished")                  # not ours to put back
        elif now_ms - restore_ms > self.p.nl_restore_late_s * 1000:
            self._give_up(outs, "restore_overdue")        # the app was away: that OFF may be the user's by now
        elif sched is False:
            self._restoring(restore_ms)
            self.next_try_ms = now_ms
            self._emit(outs, "ending")
        elif sched is True and not self.fresh:
            return                                        # only the value the state held: wait for the node's echo
        else:
            self._done(outs, "finished")                  # already ON again

    def _overdue(self, L, now_ms: int) -> bool:
        if self.due_ms is None:                           # after a restart: the date of the run's end, else now
            self.due_ms = self._restore_ms(L, now_ms) if self.until_ord is not None else now_ms
        return now_ms - self.due_ms > self.p.nl_restore_late_s * 1000

    def _pump_restore(self, L, now_ms: int, outs: list) -> None:
        if self.phase != "restoring" or self.token is not None:
            return
        if self.set_by is not False:
            self._done(outs, "finished")                  # never ours
            return
        if self._overdue(L, now_ms):
            self._give_up(outs, "restore_overdue")
            return
        if self.await_until_ms is not None:
            if now_ms < self.await_until_ms:
                return                                    # still watching an unconfirmed OFF
            self.await_until_ms = None                    # the watch is over: the echo shows what the node has
        elif self.next_try_ms is not None and now_ms < self.next_try_ms:
            return
        sched = L.window["sched"]
        if sched is True:
            if not self.fresh:
                return                                    # only the value the state held: wait for the node's echo
            self._done(outs, self.await_event or "finished")   # back ON (or the OFF never happened)
        elif sched is False:
            self.hold = False
            self._send(L, outs, now_ms, True, False, "night_learning_end")
        else:                                             # the echo is not known (yet): look again later
            self._wait(now_ms, "no_p2")


def validate(d, p: Params) -> None:
    """Validates ``night.night_learning`` (raises StateError)."""
    if not isinstance(d, dict) or not isinstance(d.get("active"), bool):
        raise StateError("bad night.night_learning")
    sb = d.get("set_by_learner")
    if not (sb is None or isinstance(sb, bool)):
        raise StateError("bad night.night_learning.set_by_learner")
    u = d.get("until")
    if u is not None:
        try:
            date.fromisoformat(u)
        except (TypeError, ValueError):
            raise StateError("bad night.night_learning.until") from None
    if d["active"] and u is None:
        raise StateError("night_learning is active without an end date")
