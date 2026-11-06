"""Shared helpers of the P7 core tests (P7 spec 9.2): a Learner built with ``P7Params(enabled=True)``, fake stays and
coverage, a fake clock (the learner's own: time comes from event timestamps, so ``set_run`` positions it), hand-built
suggestions and zones. The helpers write the learner's persisted data directly, so a test of one P7 module does not pay
for a real nightly extraction (seconds, not minutes).
"""
from __future__ import annotations

import dataclasses
from datetime import date

from radar_zone_learner.core import Learner
from radar_zone_learner.core import zones as ZN
from radar_zone_learner.core.extraction import _new_suggestion
from radar_zone_learner.core.params import DEFAULT_P5, DEFAULT_P6, DEFAULT_P7, DEFAULT_PARAMS, P7Params
from radar_zone_learner.core.types import FixedOffsetTime, NodeStatus, ReviewAnswer

BASE_MS = 1767571200000          # Monday 2026-01-05 00:00 UTC (the learner's day 0)
BASE_S = BASE_MS // 1000
DAY = 86400
D0 = date(2026, 1, 5).toordinal()


def p7on(**kw) -> P7Params:
    """P7Params with ``enabled=True`` and any constant overridden."""
    return dataclasses.replace(DEFAULT_P7, enabled=True, **kw)


def make(p7params=None, p5params=DEFAULT_P5, params=DEFAULT_PARAMS, p6params=DEFAULT_P6, state=None) -> Learner:
    """A learner created on day 0 at 00:00 (P7 enabled unless ``p7params`` says otherwise)."""
    L = Learner(params, FixedOffsetTime(0), state, p5params, p6params, p7on() if p7params is None else p7params)
    if state is None:
        L.handle(NodeStatus(True, BASE_MS))
    return L


def iso(d: int) -> str:
    return date.fromordinal(D0 + d).isoformat()


def day_ms(d: int, minute: int = 0) -> int:
    return BASE_MS + d * DAY * 1000 + minute * 60000


def set_run(L, k: int, minute: int = 210) -> None:
    """Positions the learner as if the nightly run of day ``k`` had just set its counters (what the P7 block sees)."""
    L.last_run_ord = D0 + k
    L.night = k
    L.now = day_ms(k, minute)


def cover(L, d: int, seconds: float = 3600.0, hours=range(24)) -> None:
    """Observed seconds of day ``d``: ``seconds`` in each of ``hours`` (0 elsewhere). The default is a fully observed
    day (86,400 s); a date is covered at 3,600 observed seconds."""
    hs = set(hours)
    L.obs.coverage[D0 + d] = [float(seconds) if h in hs else 0.0 for h in range(24)]


def add_stay(L, d: int, h, dur: int, x: float, y: float, ld: float = 1.0, minute: int = 0, arr: str = "walk",
             exit_: str = "moving") -> None:
    """A committed stay of day ``d`` starting at hour ``h`` (+ ``minute``), at (x, y) metres, ``dur`` seconds long."""
    t0 = BASE_S + d * DAY + int(h * 3600) + minute * 60
    L.bh.stays.append({"t0": t0 // 300 * 300, "t1": (t0 + dur) // 300 * 300, "dur": int(dur), "x": round(x * 100),
                       "y": round(y * 100), "sd": 5, "nf": 1, "ld": ld, "he": 0.0, "exit": exit_, "arr": arr,
                       "day": iso(d)})


def use_days(L, days, pos, hours=(9, 10, 11), dur: int = 3000, cover_hours=range(24)) -> None:
    """Covers each day in ``days`` and puts one stay of ``dur`` seconds at ``pos`` in each of ``hours``."""
    for d in days:
        cover(L, d, hours=cover_hours)
        for h in hours:
            add_stay(L, d, h, dur, pos[0], pos[1])


def hours_profile(hours=(9, 10, 11), per=50.0, extra=None) -> list:
    out = [0.0] * 24
    for h in hours:
        out[h] = per
    for h, v in (extra or {}).items():
        out[h] = v
    return out


def evidence(hours=(9, 10, 11), n: int = 20, per: float = 50.0) -> dict:
    prof = hours_profile(hours, per)
    return {"n": n, "core_days": 7, "hours_wd": list(prof), "hours_we": [0.0] * 24, "hours_at_suggestion": list(prof)}


def add_suggestion(L, pos, sid: str = "s0005-1", typ: str = "dwell_zone", first: int = 5, rev: int = 1,
                   ev=None, conf: float = 0.8, radius: float = 0.3, **kw) -> dict:
    """A shadow place suggestion at ``pos``. ``ev`` is the evidence dict (``evidence()`` by default)."""
    s = _new_suggestion(sid, typ, first, L.geom.cells_within(pos[0], pos[1], radius), pos[0], pos[1])
    s["rev"] = rev
    s["conf"] = conf
    s["evidence"] = dict(ev if ev is not None else evidence())
    s.update(kw)
    L.ns.suggestions.append(s)
    return s


def add_zone(L, pos, name: str = "Desk", kind: str = "dwell", sid: str = "s0001-1", ev=None, night: int = 0,
             radius: float = 0.3) -> dict:
    """A zone record made as an approval makes it (source suggestion ``sid`` with the confirmation-time hour profile).
    ``night`` is the zone's creation night."""
    s = add_suggestion(L, pos, sid=sid, ev=ev, radius=radius, first=night)
    saved = L.night
    L.night = night
    try:
        rec = ZN.create_zone(L, s, name, kind, day_ms(night))
    finally:
        L.night = saved
    L.review.touch()
    return rec


def add_label(L, pos, name: str = "Bed", kind: str = "bed", lid: str = "l0001") -> dict:
    lb = {"id": lid, "created_s": BASE_S, "x": pos[0], "y": pos[1], "name": name, "kind": kind,
          "cells": L.geom.cells_within(pos[0], pos[1], 0.4)}
    L.label_list.append(lb)
    L.review.touch()
    return lb


def answer(L, item, op, rev=None, name=None, t=None):
    """A ReviewAnswer through ``Learner.handle`` (outputs returned)."""
    return L.handle(ReviewAnswer(item, op, rev, False, (L.now or BASE_MS) + 1 if t is None else t, name))


def prime_own(L, pending=()) -> None:
    """The own-suggestion modify needs the same (suggestion, revision) on two nightly runs (review fix): this runs the plan
    once as of the previous run, so that the next ``plan`` call (or review read) of the test sees the second night."""
    from radar_zone_learner.core import moves
    run = L.last_run_ord
    L.last_run_ord = run - 1
    try:
        moves.plan(L, list(pending))
    finally:
        L.last_run_ord = run


def ready(L, k: int = 40) -> None:
    """The setup review exists (night >= review_min_nights) and the run counters sit on day ``k``."""
    set_run(L, k)
