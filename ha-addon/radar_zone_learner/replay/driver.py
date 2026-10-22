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

Labels (D37): a desk label in both worlds (``SpotName("Desk")`` at the start of the first desk session of
>= 20 min on a work-from-home day, then ``MarkPress`` every 30 s from 60 s after its start until a
``MarkResult(ok=True)`` or the session ends, then on later such sessions until success), and in the bedroom a
bed label (``SpotName("Bed")`` at the start of each evening bed session from day 1, presses every 30 s from
60 s after its start until success, the session end or the window start, repeated each evening). Presses
are made only while the timeline observes. Labels change kinds and label metrics only.
"""
from __future__ import annotations

import datetime
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from radar_zone_learner.core.params import DEFAULT_PARAMS, Params, params_digest
from radar_zone_learner.core.types import (ContextState, FixedOffsetTime, MarkPress, MarkResult, NightlyDone,
                                           NightlyFailed, NodeStatus, ScheduleState, SpotName)

from . import world as W
from .framegen import BASE_MS, DAY_MS, RunStream
from .jobs import JobSpec
from .truth import Truth, rle

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

    def __init__(self, cfg: dict, timeline):
        self.cfg = cfg
        self.timeline = timeline
        self.sessions = []
        self.state = {"desk": {"ok": False, "night": None, "attempts": 0, "sessions": 0, "label": None, "kind": None,
                               "fails": {}}}
        if cfg["bed"]:
            self.state["bed"] = {"ok": False, "night": None, "attempts": 0, "sessions": 0, "label": None, "kind": None,
                                 "fails": {}}
        self.current_name = None
        self.pending_press = None

    def add_day(self, rec) -> None:
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
        self.stream = RunStream(self.cfg, seed, horizon_days if horizon_days is not None else job.nights + 2, job.faults)
        self.truth = Truth(self.cfg)
        self.labels = LabelPlanner(self.cfg, self.stream.timeline)
        self.stream.world.on_day.append(self.truth.add_day)
        self.stream.world.on_day.append(self.labels.add_day)
        if learner_factory is None:
            from radar_zone_learner.core.learner import Learner

            def learner_factory(p, tm):
                return Learner(p, tm)
        self.learner = learner_factory(params, FixedOffsetTime(0))
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

    # ------------------------------------------------------------ feeding
    def _process(self, outs) -> None:
        for o in outs or ():
            if isinstance(o, MarkResult):
                self.labels.on_result(o)
            elif isinstance(o, NightlyDone):
                self._record_night(o)
            elif isinstance(o, NightlyFailed):         # the core contains a raising nightly run: it is an error here
                self.core_errors += 1
                if self.first_error is None:
                    self.first_error = "nightly run %s failed: %s" % (o.date, o.error)

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
        self._call(self.learner.handle, ev)

    def _tick(self, t: int) -> None:
        self.counts["tick"] += 1
        self._call(self.learner.tick, t)

    def _advance_to(self, t: int) -> None:
        """Ticks and label actions due at or before ``t``, in time order (ticks first on equal times)."""
        while True:
            la = self.labels.next_time()
            ta = BASE_MS + la if la < INF else INF
            nt = self.next_tick
            if min(ta, nt) > t:
                return
            if nt <= ta:
                self.now = nt
                self._tick(nt)
                self.next_tick += TICK_MS
            else:
                self.now = ta
                self.labels.act(self.feed)
            if self._stopped():
                return

    def _stopped(self) -> bool:
        return self.last_night is not None and self.last_night >= self.stop_night

    def _initial(self) -> None:
        w = self.job.window
        if w:
            a, b = w.split("-")
            sched = ScheduleState(True, _hm_min(a), _hm_min(b), BASE_MS)
        else:
            sched = ScheduleState(False, 0, 360, BASE_MS)
        self.feed(NodeStatus(True, BASE_MS))
        self.feed(sched)
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
        self.nights.append(rec)
        self.truth.forget_regions_before(night - 1)

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
        return {
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
