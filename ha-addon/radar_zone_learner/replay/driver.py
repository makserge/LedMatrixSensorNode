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
from radar_zone_learner.core.types import (ContextState, FixedOffsetTime, MarkPress, MarkResult, NightlyDone,
                                           NightlyFailed, NodeCommand, NodeStatus, ReviewChanged, ScheduleState,
                                           SpotName)

from . import p4_scenarios as P4
from . import p5_scenarios as P5
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

    @classmethod
    def for_job(cls, job: JobSpec) -> "LearnerFactory":
        return cls(job.zone_change_items)

    def p5params(self):
        from radar_zone_learner.core.params import P5Params
        return P5Params(zone_change_items=self.zone_change_items)

    def __call__(self, params, time_model, state=None):
        from radar_zone_learner.core.learner import Learner
        return Learner(params, time_model, state, self.p5params())


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
        self.stream = RunStream(self.cfg, seed, self.horizon, job.faults, lux=job.lux, extras=job.extras())
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
        self.p5 = P5.P5Recorder() if job.is_p5 else None

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
            if self.p5 is not None:
                self.p5.on_output(o, self.now - BASE_MS)
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
        self._call(self.learner.handle, ev)

    def _tick(self, t: int) -> None:
        self.counts["tick"] += 1
        self._call(self.learner.tick, t)

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
                self.next_tick += TICK_MS
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
        if self.job.is_p5:
            rec["p5"] = self._p5_night(rec)
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
