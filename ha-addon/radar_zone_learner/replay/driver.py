"""Replay driver: feeds the learner core with simulated time (P3 spec 11.5).

* ``Learner(params, FixedOffsetTime(0))``; at ``BASE_MS``: ``NodeStatus(True)``, ``ScheduleState`` (sched ON
  with the job's window, as a P2 node would publish; OFF and 00:00-06:00 without a window) and
  ``ContextState("home_empty", False)``.
* Then, in reception order: the frames messages, ``home_empty`` edges and node status of
  ``replay.framegen.RunStream``, the 'Mark spot here' labels (D37) and a ``tick(now)`` every 10 simulated
  seconds (D38). Ticks and label actions due at or before a message's time run before it.
* After every ``NightlyDone``: the night, all suggestions (with the simulator label and the IoU against
  every truth region), the number of candidates, the labels and the counters. Stops after night
  ``job.nights``.
* CPU budget: after each simulated day, more than ``cpu_budget_s_per_day x days + 30 s`` of process CPU
  stops the run with ``status = "over_budget"``.

P4 additions (OA 12 P4; ``replay.p4_scenarios``): a ``FakeNode`` answers the learner's ``NodeCommand`` with the
schedule echo and a ``CommandResult``; the answers are injected as events at their times. P4 jobs carry the lux
samples (``JobSpec.lux``) and a scenario (``JobSpec.script``): actors such as the Night learning user, the scripted
'Mark spot here' presses and the answers to place suggestions act like the label planner below. After every
nightly run a P4 job also records the learner's night evidence, proposal, review state and zones (``p4``).

P5 additions (P5 spec 11.6; ``replay.p5_scenarios``): the default learner factory is ``LearnerFactory`` (a module-level
class, so a job crosses the spawn boundary of the worker pool as it is): it builds ``Learner(params, time_model, state,
P5Params(zone_change_items=job.zone_change_items))``. ``Run.restart_learner()`` is an add-on restart (export, JSON round
trip, a new learner from the state, the node's retained topics fed again). A P5 job also records, after every nightly
run, the review items, the zones, the Advanced ids, the push request, the suppressions and the ``p5`` section
(``rec["p5"]``), and over the whole run the push requests, the refused answers and the restarts (``result["p5"]``).

P6 additions (P6 spec 11.6; ``replay.p6_scenarios``, ``replay.p6_people``): ``LearnerFactory`` also builds the learner's
``P6Params``; a P6 job runs with the replay tick of ``JobSpec.tick_ms`` (1 s, hold deadlines are judged to that resolution),
carries the scripted people, the quiet days and the drop windows of its ``JobSpec`` in its ``RunStream`` and the
detection noise ``JobSpec.noise_m``; the P5 recorder is also made for it. ``P6Recorder`` (``Run.p6``) records every ``ZoneEvent``
and ``ZoneStates`` with the emission time, the ``recv_ms`` of the batch in progress and the boot offset, and after every
nightly run the zones, ``Learner.zone_states()``, ``zone6`` of the state and the door suggestions (``rec["p6"]``); the run
result gains the section ``p6``.

P7 additions (P7 spec 8.4, 8.5; ``replay.p7_scenarios``): ``LearnerFactory`` builds the learner's ``P7Params`` (enabled for a P7 job
only: every other job has the default, which is a P6 learner); the job's ``rot`` and ``empty_off`` go to the ``RunStream``; the P5 recorder
is also made for a P7 job; after every nightly run the record gains ``rec["p7"]`` (``Run._p7_night``: the items, zones, drift view, radar
state, the owner's answers, the truth region of the moved sofa, the tuning line) and the run result the section ``p7`` (the tuning lines,
the owner's reset readings and summary).

Labels (D37): a desk label in both worlds (``SpotName("Desk")`` at the start of the first desk session of
>= 20 min on a work-from-home day, then ``MarkPress`` every 30 s from 60 s after its start until a
``MarkResult(ok=True)`` or the session ends, then on later such sessions until success), and in the bedroom a
bed label (``SpotName("Bed")`` at the start of each evening bed session from day 1, presses every 30 s from
60 s after its start until success, the session end or the window start, repeated each evening). Presses
are made only while the timeline observes. Labels change kinds and label metrics only.
"""
from __future__ import annotations

import datetime
import heapq
import json
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from radar_zone_learner.core.params import DEFAULT_PARAMS, Params, params_digest
from radar_zone_learner.core.types import (ContextState, FixedOffsetTime, FramesMessage, MarkPress, MarkResult,
                                           NightlyDone, NightlyFailed, NodeCommand, NodeStatus, ReviewChanged,
                                           ScheduleState, SpotName, TuningLine)

from . import p4_scenarios as P4
from . import p5_scenarios as P5
from . import p6_people as P6P
from . import p6_scenarios as P6
from . import world as W
from .framegen import BASE_MS, DAY_MS, OBSERVE_MS, RunStream
from .jobs import JobSpec
from .truth import NightTruth, Truth, rle

TICK_MS = 10_000
PRESS_EVERY_MS = 30_000
PRESS_FIRST_MS = 60_000
DESK_MIN_S = 20 * 60
WFH_DAYS = (0, 2, 4)
PLACE_TYPES = ("dwell_zone", "short_task_zone")
MAX_CORE_ERRORS = 100
BUDGET_SLACK_S = 30.0
BASE_DATE = datetime.date(2026, 1, 5)
INF = 1 << 62


def iso_day(d: int) -> str:
    return (BASE_DATE + datetime.timedelta(days=d)).isoformat()


def _hm_min(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


class _LabelSession:
    __slots__ = ("which", "name", "t0", "t1", "named", "next_press")

    def __init__(self, which, name, t0, t1):
        self.which = which
        self.name = name
        self.t0 = t0
        self.t1 = t1
        self.named = False
        self.next_press = t0 + PRESS_FIRST_MS


class LabelPlanner:
    """'Mark spot here' as a persistent user (D37): one active session at a time, presses until success."""

    name = "labels"

    def __init__(self, cfg: dict, timeline, enabled: bool = True):
        self.cfg = cfg
        self.timeline = timeline
        self.enabled = enabled
        self.run = None
        self.sessions = []
        self.state = {}
        if enabled:
            self.state["desk"] = {"ok": False, "night": None, "attempts": 0, "sessions": 0, "label": None,
                                  "kind": None, "fails": {}}
        if enabled and cfg["bed"]:
            self.state["bed"] = {"ok": False, "night": None, "attempts": 0, "sessions": 0, "label": None, "kind": None,
                                 "fails": {}}
        self.current_name = None
        self.pending_press = None

    def attach(self, run) -> None:
        self.run = run

    def on_output(self, o) -> None:
        if isinstance(o, MarkResult):
            self.on_result(o)

    def on_night(self, rec) -> None:
        pass

    def summary(self) -> dict:
        return {}

    def add_day(self, rec) -> None:
        if not self.enabled:
            return
        d = rec.d
        new = []
        if self.cfg["wfh"] and d % 7 in WFH_DAYS:
            for ag, place, mode, t0, t1, x, y in rec.sessions:
                if ag == "P1" and place == "desk" and mode == "sit" and t1 - t0 >= DESK_MIN_S:
                    new.append(_LabelSession("desk", "Desk", t0 * 1000, t1 * 1000))
        if self.cfg["bed"] and d >= 1:
            for ag, place, mode, t0, t1, x, y in rec.sessions:
                if ag == "P1" and place == "bed" and t0 >= d * W.DAY + 12 * 3600:
                    new.append(_LabelSession("bed", "Bed", t0 * 1000, t1 * 1000))
        new.sort(key=lambda s: (s.t0, s.which))
        self.sessions.extend(new)

    def _head(self):
        while self.sessions:
            s = self.sessions[0]
            if self.state[s.which]["ok"] or (s.named and s.next_press >= s.t1):
                self.sessions.pop(0)
                continue
            return s
        return None

    def next_time(self) -> int:
        s = self._head()
        if s is None:
            return INF
        return s.t0 if not s.named else s.next_press

    def act(self, feed: Callable) -> None:
        """Run the next due action (spot name or press) through ``feed(event)``."""
        s = self._head()
        if s is None:
            return
        if not s.named:
            s.named = True
            self.state[s.which]["sessions"] += 1
            if self.current_name != s.name:
                self.current_name = s.name
                feed(SpotName(s.name, BASE_MS + s.t0))
            return
        t = s.next_press
        s.next_press += PRESS_EVERY_MS
        if not self.timeline.observing(t):
            return
        if self.current_name != s.name:
            self.current_name = s.name
            feed(SpotName(s.name, BASE_MS + t))
        self.state[s.which]["attempts"] += 1
        self.pending_press = (s.which, t)
        feed(MarkPress(BASE_MS + t))
        self.pending_press = None

    def on_result(self, res: MarkResult) -> None:
        if self.pending_press is None:
            return
        which, t = self.pending_press
        st = self.state[which]
        if res.ok:
            if not st["ok"]:
                d, sod = divmod(t // 1000, W.DAY)
                st.update(ok=True, night=int(d if sod >= 3.5 * 3600 else d - 1), label=res.label_id, kind=res.kind)
        else:
            st["fails"][res.reason] = st["fails"].get(res.reason, 0) + 1


class LearnerFactory:
    """The default learner of a job: ``Learner(params, time_model, state, P5Params(...))`` with the job's P5 parameters.

    A class at module level (not a lambda or a closure), so that it is picklable: the job crosses the process boundary
    of the worker pool (``run_task``) and the factory is built from it in the worker. ``state`` is None for a new run
    and the exported state of a restart (``Run.restart_learner``).
    """

    def __init__(self, zone_change_items: bool = False):
        self.zone_change_items = bool(zone_change_items)
        self.p6params_ = None                # P6: a P6Params to use instead of the default (None: the default)
        self.p7_enabled = False              # P7: the learner of a P7 job has P7Params(enabled=True); every other job the default
        self.p7_static_freeze = True
        self.p7_span = None                  # P7Params.probation_min_span (None: the default)
        self.p7params_ = None                # P7: a P7Params to use instead of what the job says (None: the job's)

    @classmethod
    def for_job(cls, job: JobSpec) -> "LearnerFactory":
        f = cls(job.zone_change_items)
        f.p7_enabled = bool(job.is_p7)
        f.p7_static_freeze = bool(job.p7_static_freeze)
        f.p7_span = job.p7_span
        return f

    def p5params(self):
        from radar_zone_learner.core.params import P5Params
        return P5Params(zone_change_items=self.zone_change_items)

    def p6params(self):
        from radar_zone_learner.core.params import DEFAULT_P6
        return self.p6params_ if self.p6params_ is not None else DEFAULT_P6

    def p7params(self):
        """``P7Params(enabled=job.is_p7, static_freeze=..., probation_min_span=...)`` for a P7 job; the default (disabled: a P6 learner
        byte for byte, P7-R1) for every other job."""
        from radar_zone_learner.core.params import DEFAULT_P7, P7Params
        if self.p7params_ is not None:
            return self.p7params_
        if not self.p7_enabled:
            return DEFAULT_P7
        kw = {"enabled": True, "static_freeze": self.p7_static_freeze}
        if self.p7_span is not None:
            kw["probation_min_span"] = int(self.p7_span)
        return P7Params(**kw)

    def __call__(self, params, time_model, state=None):
        from radar_zone_learner.core.learner import Learner
        return Learner(params, time_model, state, self.p5params(), self.p6params(), self.p7params())


class Run:
    """One replay run of (job, seed). ``run()`` feeds the learner until night ``stop_night``."""

    def __init__(self, job: JobSpec, seed: int, params: Params = DEFAULT_PARAMS, cpu_budget_s_per_day: float = 6.0,
                 learner_factory: Optional[Callable] = None, horizon_days: Optional[int] = None,
                 budget_slack_s: float = BUDGET_SLACK_S):
        self.job = job
        self.slack = budget_slack_s
        self.seed = seed
        self.params = params
        self.budget = cpu_budget_s_per_day
        self.cfg = job.cfg()
        self.horizon = horizon_days if horizon_days is not None else job.nights + 2
        # P6: scripted people, quiet days, drop windows and the detection noise of the job (all inert for a P3-P5 job)
        self.scripted = P6P.ScriptedPeople(job, seed) if job.is_p6 and job.programs else None
        quiet_keep = ("G", "V") if job.script.startswith("p6guests") else ()
        self.stream = RunStream(self.cfg, seed, self.horizon, job.faults, noise_m=job.noise_m, lux=job.lux,
                                extras=job.extras(), quiet_from_day=job.quiet_from_day if job.is_p6 else None,
                                quiet_keep=quiet_keep, scripted=self.scripted, gaps=job.gaps, rot=job.rot,
                                empty_off=job.empty_off)
        self.tick_ms = job.tick_ms
        self.truth = Truth(self.cfg)
        self.labels = LabelPlanner(self.cfg, self.stream.timeline, enabled=job.labels)
        self.stream.world.on_day.append(self.truth.add_day)
        self.night_truth = NightTruth()
        self.stream.world.on_day.append(self.night_truth.add_day)
        # P4: the node, the user-like actors and the events the node's answers inject
        nbeg, nend = self._window_min()
        self.node = P4.FakeNode(job.p2, job.window is not None, nbeg, nend)
        self.actors = [self.labels] + P4.make_actors(job)
        for a in self.actors:
            a.attach(self)
            self.stream.world.on_day.append(a.add_day)
        self.inject = []                 # heap of (abs_ms, seq, event): the node's echoes and results
        self.inject_seq = 0
        self.review_items = {}           # id -> ReviewItem of the last ReviewChanged
        # the factory takes (params, time_model) for a new learner and a third argument, the exported state, for a restart
        self.factory = learner_factory if learner_factory is not None else LearnerFactory.for_job(job)
        self.learner = self._new_learner(None)
        self.nights = []
        self.status = "running"
        self.core_errors = 0
        self.first_error = None
        self.next_tick = BASE_MS
        self.now = BASE_MS
        self.stop_night = job.nights
        self.last_night = None
        self.events_iter = None
        self.held = None
        self.cpu0 = None
        self.wall0 = None
        self.counts = {"handle": 0, "tick": 0, "presses": 0}
        # P5: what the node and the contexts said last (fed again after a restart), the recorder, the restarts
        self.node_online = True
        self.home_empty = False
        self.restarts = []
        self.p5 = P5.P5Recorder() if (job.is_p5 or job.is_p6 or job.is_p7) else None
        self.p6 = P6.P6Recorder(self) if job.is_p6 else None
        self.p7_tuning = []              # P7: the text of every ``TuningLine`` of the run, oldest first
        self.p7_reset_seen = 0           # P7: how many of the P7 user's reset readings the night records carry already

    def _new_learner(self, state: Optional[dict]):
        if state is None:
            return self.factory(self.params, FixedOffsetTime(0))
        return self.factory(self.params, FixedOffsetTime(0), state)

    def restart_learner(self) -> None:
        """The add-on restarts (P5 spec 11.6): the learner's state goes through a JSON round trip into a new learner
        with the same parameters and time model, and what the broker keeps retained for it (the node's status and
        schedule, the ``home_empty`` context) is fed again, as the shell does after a connect. Everything that lives
        in the learner's RAM only (the selected review item, the spot name, pending commands, the tracker) is lost."""
        state = json.loads(json.dumps(self.learner.export_state(), separators=(",", ":")))
        self.learner = self._new_learner(state)
        self.restarts.append({"t": self.now - BASE_MS, "night": self.last_night})
        self.feed(NodeStatus(self.node_online, self.now))
        self.feed(ScheduleState(self.node.sched, self.node.start_min, self.node.end_min, self.now))
        self.feed(ContextState("home_empty", self.home_empty, self.now))

    # ------------------------------------------------------------ feeding
    def _process(self, outs) -> None:
        for o in outs or ():
            if isinstance(o, NightlyDone):
                self._record_night(o)
            elif isinstance(o, NightlyFailed):         # the core contains a raising nightly run: it is an error here
                self.core_errors += 1
                if self.first_error is None:
                    self.first_error = "nightly run %s failed: %s" % (o.date, o.error)
            elif isinstance(o, NodeCommand):           # the fake node answers: the echo and the result come later
                for t, ev in self.node.command(o, self.now):
                    self.inject_seq += 1
                    heapq.heappush(self.inject, (t, self.inject_seq, ev))
            elif isinstance(o, ReviewChanged):
                self.review_items = {i.id: i for i in o.items}
            elif isinstance(o, TuningLine):          # P7: emitted after NightlyDone, so it joins the record that was just made
                self.p7_tuning.append(o.text)
                if self.job.is_p7 and self.nights and "p7" in self.nights[-1]:
                    self.nights[-1]["p7"]["tuning"].append(o.text)
            if self.p5 is not None:
                self.p5.on_output(o, self.now - BASE_MS)
            if self.p6 is not None:
                self.p6.on_output(o, self.now)
            for a in self.actors:
                a.on_output(o)

    def _window_min(self) -> tuple:
        w = self.job.window
        if w:
            a, b = w.split("-")
            return _hm_min(a), _hm_min(b)
        return 0, 360

    def _call(self, fn, arg) -> None:
        try:
            outs = fn(arg)
        except Exception:        # an internal core bug: counted, the run goes on (as the shell does)
            self.core_errors += 1
            if self.first_error is None:
                self.first_error = traceback.format_exc(limit=8)
            if self.core_errors > MAX_CORE_ERRORS:
                raise _Abort("error")
            return
        self._process(outs)

    def feed(self, ev) -> None:
        self.counts["handle"] += 1
        if isinstance(ev, MarkPress):
            self.counts["presses"] += 1
        elif isinstance(ev, NodeStatus):
            self.node_online = bool(ev.online)
        elif isinstance(ev, ContextState) and ev.name == "home_empty":
            self.home_empty = ev.value
        if self.p6 is None:
            self._call(self.learner.handle, ev)
            return
        batch = isinstance(ev, FramesMessage)
        if batch:
            self.p6.note_batch(ev.recv_ms)
        self.p6.ctx = "b" if batch else "e"
        try:
            self._call(self.learner.handle, ev)
        finally:
            self.p6.ctx = "e"

    def _tick(self, t: int) -> None:
        self.counts["tick"] += 1
        if self.p6 is not None:
            self.p6.ctx = "t"
        try:
            self._call(self.learner.tick, t)
        finally:
            if self.p6 is not None:
                self.p6.ctx = "e"

    def _advance_to(self, t: int) -> None:
        """Ticks, the node's answers and the actors' actions (labels, P4 users) due at or before ``t``, in time
        order: ticks first on equal times, then the node's events, then the actors in list order."""
        while True:
            best, ta = None, INF
            for a in self.actors:
                x = a.next_time()
                if x < INF and BASE_MS + x < ta:
                    best, ta = a, BASE_MS + x
            nt = self.next_tick
            ti = self.inject[0][0] if self.inject else INF
            if min(ta, nt, ti) > t:
                return
            if nt <= ti and nt <= ta:
                self.now = nt
                self._tick(nt)
                self.next_tick += self.tick_ms
            elif ti <= ta:
                _, _, ev = heapq.heappop(self.inject)
                self.now = max(self.now, ti)
                self.feed(ev)
            else:
                self.now = max(self.now, ta)
                best.act(self.feed)
            if self._stopped():
                return

    def _stopped(self) -> bool:
        return self.last_night is not None and self.last_night >= self.stop_night

    def _initial(self) -> None:
        self.feed(NodeStatus(True, BASE_MS))
        self.feed(self.node.initial_event())     # sched ON with the window; OFF without; nothing on a P1 node
        self.feed(ContextState("home_empty", False, BASE_MS))

    def run(self, stop_night: Optional[int] = None) -> str:
        if stop_night is not None:
            self.stop_night = stop_night
        if self.cpu0 is None:
            self.cpu0 = time.process_time()
            self.wall0 = time.perf_counter()
            self._initial()
            self.events_iter = self.stream.events()
        day_done = (self.now - BASE_MS) // DAY_MS
        limit = BASE_MS + (self.stop_night + 1) * DAY_MS
        try:
            while True:
                item = self._next_event()
                if item is None:
                    self.status = "incomplete"
                    return self.status
                recv, _, ev = item
                self._advance_to(recv)
                if self._stopped():
                    self.held = item                   # not fed yet: a later run() or tail() starts with it
                    break
                self.now = recv
                self.feed(ev)
                if self._stopped():
                    break
                d = (recv - BASE_MS) // DAY_MS
                if d > day_done:
                    day_done = d
                    if time.process_time() - self.cpu0 > self.budget * day_done + self.slack:
                        self.status = "over_budget"
                        return self.status
                if recv >= limit:
                    self.status = "incomplete"
                    return self.status
        except _Abort as exc:
            self.status = exc.status
            return self.status
        self.status = "ok"
        return self.status

    def _next_event(self):
        """The next stream event (a held one first), or None at the end. No generator wrapper: closing one
        would close the shared stream generator too."""
        if self.held is not None:
            e, self.held = self.held, None
            return e
        return next(self.events_iter, None)

    def tail(self, end_ms: int):
        """The stream's remaining events (frames, home_empty, node status) before ``end_ms``, not fed.

        For the resource probe: the events after a warm state. Label presses and ticks are not included.
        """
        while True:
            e = self._next_event()
            if e is None:
                return
            if e[0] >= end_ms:
                self.held = e
                return
            yield e

    # ------------------------------------------------------------ recording
    def _record_night(self, nd: NightlyDone) -> None:
        night = nd.night
        self.last_night = night
        regions = self.truth.regions(night)
        sugs = []
        for s in self.learner.suggestions():
            cells = frozenset(s.cells)
            e = {"id": s.id, "rev": s.rev, "type": s.type, "kind": s.kind, "name_hint": s.name_hint, "state": s.state,
                 "first_night": s.first_night, "last_night": s.last_night, "cx": round(s.cx_m, 3),
                 "cy": round(s.cy_m, 3), "cells_rle": rle(cells), "conf": round(s.conf, 4),
                 "variant_evidence_split": bool(s.variant_evidence_split),
                 "variant_share_split": bool(s.variant_share_split),
                 "variant_suppressed": bool(s.variant_suppressed), "frozen": bool(s.frozen),
                 "variants": len(s.variants), "sim_label": self.truth.label(s.cx_m, s.cy_m, night)}
            if s.type in PLACE_TYPES or s.type == "reflection":
                ious = {}
                max_in = []
                for p, (reg, mx) in regions.items():
                    v = len(cells & reg) / len(cells | reg) if reg and cells else 0.0
                    if v > 0:
                        ious[p] = round(v, 6)
                    if mx is not None and mx in cells:
                        max_in.append(p)
                e["iou"] = ious
                e["max_in"] = max_in
            sugs.append(e)
        try:
            ncand = len(self.learner.candidates())
        except Exception:
            ncand = nd.candidates
        rec = {"night": night, "date": nd.date, "candidates": ncand, "new_suggestions": nd.new_suggestions,
               "suggestions": sugs,
               "truth": {p: {"cells": len(reg), "max": mx} for p, (reg, mx) in regions.items()},
               "labels": [{"id": l.id, "kind": l.kind, "name": l.name, "x": round(l.x_m, 3), "y": round(l.y_m, 3)}
                          for l in self.learner.labels()],
               "counters": self._counters()}
        if self.job.is_p4:
            rec["p4"] = self._p4_night(rec)
        if self.job.is_p5 or self.job.is_p6 or self.job.is_p7:
            rec["p5"] = self._p5_night(rec)
        if self.p6 is not None:
            rec["p6"] = self.p6.night(self, rec)
        if self.job.is_p7:                                  # before the truth regions of this night are forgotten (sofa_rle)
            rec["p7"] = self._p7_night(rec)
        self.nights.append(rec)
        self.truth.forget_regions_before(night - 1)
        for a in self.actors:
            a.on_night(rec)

    def _p4_night(self, rec: dict) -> dict:
        """The learner's night evidence, proposal, review state and zones after a nightly run (P4)."""
        L = self.learner
        try:
            attrs = L.status().attributes
        except Exception:
            attrs = {}
        try:
            st = L.export_state()
        except Exception:
            st = {}
        nt = st.get("night") or {}
        base = BASE_DATE.toordinal()

        def idx(iso):
            return datetime.date.fromisoformat(iso).toordinal() - base

        bed_named = [{"id": s["id"], "iou_bed": (s.get("iou") or {}).get("bed", 0.0), "kind": s["kind"],
                      "name_hint": s["name_hint"]}
                     for s in rec["suggestions"]
                     if s["type"] in PLACE_TYPES and s["state"] == "shadow"
                     and (s["kind"] == "bed" or (s["name_hint"] or "").strip().casefold() in ("bed", "bett"))]
        return {
            "night": attrs.get("night"), "review": attrs.get("review"), "night_learning": attrs.get("night_learning"),
            "schedule": attrs.get("schedule"), "radar_valid": attrs.get("radar_valid"),
            "items": {k: {"rev": it.rev, "data": it.data} for k, it in self.review_items.items()},
            "onsets": [[idx(e["n"]), e["m"], e["s"]] for e in nt.get("onsets", [])],
            "wakes": [[idx(e["n"]), e["m"]] for e in nt.get("wakes", [])],
            "zones": [{"zid": z.get("zid"), "name": z.get("name"), "kind": z.get("kind"), "ref": z.get("ref")}
                      for z in st.get("zones", [])],
            "suppressions": [s.get("type") for s in st.get("suppressions", [])],
            "bed_named": bed_named,
        }

    def _p5_night(self, rec: dict) -> dict:
        """What the learner shows after a nightly run for the P5 checks: the open items with their revisions, the
        zones, the Advanced ids, the push request, the suppressions and the ``p5`` section, a few status values (the
        accessors are the learner's own; a missing one leaves its key out)."""
        L = self.learner
        out = {}
        try:
            items, sel = L.review_state()
            out["items"] = []
            for i in items:
                it = {"id": i.id, "rev": i.rev, "type": i.type, "tier": i.tier, "tag": i.data.get("tag", "")}
                if i.tier == 1:                  # the texts and times of the two fixed items (P5-5 a and h)
                    it["data"] = {k: v for k, v in i.data.items() if v is None or isinstance(v, (str, int, float, bool))}
                out["items"].append(it)
            out["selected"] = sel
        except Exception:
            pass
        try:
            out["zones"] = [P5.zone_dict(v) for v in L.zones()]
            out["advanced"] = [e.id for e in L.advanced()]
            out["push_request"] = P5.push_dict(L.push_request())
            out["paused_stays"] = L.paused_stays()
        except Exception:
            pass
        try:
            st = L.export_state()
            p5 = st.get("p5") or {}
            out["suppressions"] = [dict(x) for x in st.get("suppressions", [])]
            out["next_zone_n"] = (st.get("review") or {}).get("next_zone_n")
            out["p5"] = {"masks": p5.get("masks", {}), "push": p5.get("push"), "settings": p5.get("settings"),
                         "later": len(p5.get("later", {})), "tags_n": (p5.get("tags") or {}).get("n"),
                         "real": len(p5.get("real", [])), "nw_applied": p5.get("nw_applied")}
        except Exception:
            pass
        try:
            stat = L.status()
            a = stat.attributes
            rv = a.get("review") or {}
            out["status"] = {"state": stat.state, "radar_valid": a.get("radar_valid"),
                             "covered_days": a.get("covered_days"), "observed_h_24h": a.get("observed_h_24h"),
                             "learning_paused": a.get("learning_paused"), "zones_total": a.get("zones_total"),
                             "pending_places": a.get("pending_places"), "pet_spots": a.get("pet_spots"),
                             "stale_answers": rv.get("stale_answers"), "tier2": rv.get("tier2"),
                             "advanced": rv.get("advanced"), "more": rv.get("more"),
                             "command_pending": rv.get("command_pending"), "settings": a.get("settings")}
            if self.job.script == "p5rej":       # the rejected suggestion is absent from both once it is stale
                out["status_sugg_ids"] = [x["id"] for x in a.get("suggestions", [])]
                out["map_sugg_ids"] = [x.id for x in L.debug_map().suggestions]
        except Exception:
            pass
        return out

    def _p7_actor(self):
        return next((a for a in self.actors if getattr(a, "name", "") == "p7user"), None)

    def _p7_night(self, rec: dict) -> dict:
        """What the learner shows after a nightly run for the P7 checks (P7 spec 8.4): the item list (id, rev, type, tier, tag, sid,
        zid, shift_cm, r, weeks, src), the zones (id, kind, cx, cy, cells_rle, rev, ref, frozen), the drift view (id, vs, ratio, fz,
        lo, rt, lu, rc, held), the number of moved-from masks, the radar-moved state, the P7 status counts, the answers the owner took
        since the last record, ``sofa_rle`` (the truth region of the sofa, for the move jobs from the day before the move), the
        tuning lines (this night's, appended when the learner's ``TuningLine`` arrives) and ``reset`` (the owner's reading after the
        Approve of the radar-moved item). Every accessor is read in its own ``try``: a stub learner leaves its key out."""
        L = self.learner
        night = rec["night"]
        out = {"tuning": []}
        try:
            items, sel = L.review_state()
            out["items"] = []
            for i in items:
                d = i.data
                out["items"].append({"id": i.id, "rev": i.rev, "type": i.type, "tier": i.tier, "tag": d.get("tag", ""),
                                     "sid": d.get("sid") if i.type in ("modify_zone", "merge_into") else i.id,
                                     "zid": d.get("zid"), "shift_cm": d.get("shift_cm"), "r": d.get("r"),
                                     "weeks": d.get("weeks"), "src": d.get("src"), "ext_cm": d.get("ext_cm"),
                                     "deg": d.get("deg"), "streak": d.get("streak")})
            out["selected"] = sel
        except Exception:
            pass
        try:
            out["zones"] = [{"id": v.id, "kind": v.kind, "cx": round(v.cx_m, 3), "cy": round(v.cy_m, 3), "cells_rle": rle(v.cells),
                             "rev": v.rev, "ref": v.ref, "frozen": bool(v.frozen), "name": v.name, "src": v.src, "night": v.night}
                            for v in L.zones()]
        except Exception:
            pass
        try:
            out["drift"] = [{"id": z.id, "vs": z.state, "ratio": z.ratio, "fz": bool(z.frozen), "lo": z.lo, "rt": z.rt,
                             "lu": z.lu, "rc": bool(z.rc), "held": bool(z.held)} for z in L.drift_view()]
        except Exception:
            pass
        try:
            out["mf"] = L.moved_from_count()
        except Exception:
            pass
        try:
            rm = L.radar_moved_state()
            out["rm"] = {"state": rm.state, "rev": rm.rev, "streak": rm.streak, "tx": list(rm.tx), "hold": bool(rm.hold)}
        except Exception:
            pass
        dbg = getattr(L, "p7_radar_dbg", None)
        if isinstance(dbg, dict):
            out["radar_dbg"] = dict(dbg)
        try:
            a = L.status().attributes
            out["status"] = {k: a.get(k) for k in ("zones_total", "zones_frozen", "zones_blind", "zones_falling", "zones_unused",
                                                   "zones_recheck", "moved_from", "radar_moved", "places_probation",
                                                   "places_recheck_held", "pending_places")}
        except Exception:
            pass
        try:
            c = L.counters()
            out["errors"] = {k: c.get(k) for k in ("p7_errors", "zone7_pruned", "zone7_reset")}
        except Exception:
            pass
        user = self._p7_actor()
        out["answers"] = user.take_answers() if user is not None else []
        if user is not None and len(user.resets) > self.p7_reset_seen:
            out["reset"] = dict(user.resets[self.p7_reset_seen])
            self.p7_reset_seen = len(user.resets)
        if self.job.move and night >= self.job.move_day - 1:
            try:
                out["sofa_rle"] = rle(self.truth.regions(night)["sofaAB"][0])
            except Exception:
                pass
        return out

    def _p4_result(self) -> dict:
        tl = self.stream.timeline

        def seen(a_ms, b_ms):         # the radars observe through [a, b) (not off, not warming up)
            return not any(oa < b_ms and a_ms < ob + OBSERVE_MS for oa, ob in tl.offs)

        nt = self.night_truth
        onsets = [[n, nt.nm(n, s), n in nt.late, seen(s * 1000 - 90_000, s * 1000 + 930_000)]
                  for n, s in sorted(nt.onset_s.items())]
        wakes = [[n, nt.nm(n, s), seen(s * 1000 - 30_000, s * 1000 + 660_000)] for n, s in sorted(nt.wake_s.items())]
        return {"script": self.job.script,
                "node": self.node.summary(tl, self.cfg, self.horizon),
                "actors": {a.name: a.summary() for a in self.actors if a is not self.labels},
                "late_nights": list(self.stream.late_nights),
                "true_onsets": onsets, "true_wakes": wakes}

    def _counters(self) -> dict:
        try:
            return dict(self.learner.counters())
        except Exception:
            return {}

    def result(self) -> dict:
        cpu = time.process_time() - self.cpu0 if self.cpu0 is not None else 0.0
        wall = time.perf_counter() - self.wall0 if self.wall0 is not None else 0.0
        sim_days = max(0.0, (self.now - BASE_MS) / DAY_MS)
        n_last = self.last_night if self.last_night is not None else 0
        days = list(range(0, max(0, n_last)))
        regions_at = {}
        for n in (7, 14, 21):
            if n <= n_last:
                regions_at[str(n)] = {p: rle(self.truth.region(p, n)[0]) for p in self.truth.all_places()}
        recall = precision = None
        try:
            attrs = self.learner.status().attributes
            recall = attrs.get("label_recall")
            precision = attrs.get("label_precision")
        except Exception:
            pass
        res = {
            "job": self.job.name,
            "job_spec": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self.job).items()},
            "world": self.job.world, "window": self.job.window_key, "variant": self.job.variant,
            "gated": self.job.gated, "seed": self.seed,
            "params": params_digest(self.params),
            "status": self.status,
            "core_errors": self.core_errors, "first_error": self.first_error,
            "timing": {"cpu_s": round(cpu, 3), "wall_s": round(wall, 3), "sim_days": round(sim_days, 3),
                       "cpu_s_per_day": round(cpu / sim_days, 3) if sim_days > 0 else None},
            "nights": self.nights,
            "truth": {"days": len(days),
                      "cut": {p: (round(v, 6) if v is not None else None) for p, v in self.truth.cut_shares(days).items()},
                      "occupancy_h": {p: round(v, 2) for p, v in self.truth.occupancy_h(days).items()},
                      "core": self.truth.core_places(days),
                      "regions_at": regions_at},
            "labels": {k: {kk: vv for kk, vv in v.items()} for k, v in self.labels.state.items()}
                      | {"recall": recall, "precision": precision},
            "capture": {"seated": self._seated_capture(n_last)},
            "counters": self._counters(),
            "stream": dict(self.stream.stats),
            "calls": dict(self.counts),
        }
        if self.job.is_p4:
            res["p4"] = self._p4_result()
        if self.p5 is not None:
            res["p5"] = self.p5.result(self.restarts)
        if self.p6 is not None:
            res["p6"] = self.p6.result()
        if self.job.is_p7:
            user = self._p7_actor()
            res["p7"] = {"tuning": list(self.p7_tuning), "resets": list(user.resets) if user is not None else [],
                         "user": user.summary() if user is not None else None,
                         "empty_off_s": sum(b - a for a, b in getattr(self.stream, "empty_off", ()))}
        return res

    def _seated_capture(self, n_last: int):
        """Sum of retained stay durations over true 'sit' seconds of P1 and P2 on the same dates (14 days)."""
        days = list(range(max(0, n_last - 14), n_last))
        if not days:
            return None
        dates = {iso_day(d) for d in days}
        try:
            stays = self.learner.stays()
        except Exception:
            return None
        got = sum(s.dur_s for s in stays if s.day in dates)
        true_s = self.truth.sit_seconds(days)
        return round(got / true_s, 4) if true_s > 0 else None


class _Abort(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


def run_job(job: JobSpec, seed: int, params: Params = DEFAULT_PARAMS, cpu_budget_s_per_day: float = 6.0,
            learner_factory: Optional[Callable] = None) -> dict:
    """Run one job and seed; returns the run result (P3 spec 11.7.1)."""
    r = Run(job, seed, params, cpu_budget_s_per_day, learner_factory)
    r.run()
    return r.result()


def run_job_to_file(job: JobSpec, seed: int, params: Params, cpu_budget_s_per_day: float, out_dir: str) -> dict:
    """Worker entry: run, write ``<out_dir>/runs/<job>_s<seed>.json``, return a short summary."""
    import json
    try:
        res = run_job(job, seed, params, cpu_budget_s_per_day)
    except Exception:
        res = {"job": job.name, "seed": seed, "status": "error", "first_error": traceback.format_exc(limit=12),
               "world": job.world, "window": job.window_key, "variant": job.variant, "gated": job.gated,
               "timing": {"cpu_s": None, "wall_s": None, "sim_days": 0.0, "cpu_s_per_day": None}, "nights": []}
    path = Path(out_dir) / "runs" / ("%s_s%d.json" % (job.name, seed))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(res, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return {"job": job.name, "seed": seed, "status": res["status"], "timing": res.get("timing"),
            "path": str(path), "core_errors": res.get("core_errors", 0)}


def run_task(args: tuple) -> dict:
    """Pool entry (``multiprocessing`` spawn): ``args`` = (job, seed, params, budget, out_dir)."""
    return run_job_to_file(*args)
