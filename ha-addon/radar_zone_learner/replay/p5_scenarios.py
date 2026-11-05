"""The actors of the P5 replay jobs and what they record (P5 spec 11.6; OA 12 P5 'Verification').

An actor is a user (or an automation) that acts at simulated times, like the P3 ``LabelPlanner`` and the P4 actors of
``replay.p4_scenarios``: ``next_time`` (sim ms since the run's start), ``act(feed)``, ``add_day`` (the world's days),
``on_night`` (the night record after each nightly run), ``on_output`` (every output of the learner) and ``summary``.
The P5 actors are built on ``P5Actor``: a queue of timed steps, a mirror of the review list that is made from the
learner's ``ReviewChanged`` outputs only (so an actor works on a stub learner too) and ``send`` that returns the
outputs of one event.

* ``AnswerScript`` ('p5ans', P5-1): stale and current revisions, a re-delivery, the night window and the bed item.
* ``SetupUser`` ('p5setup', P5-4): the first setup review at the first evening, with a tap counter.
* ``PushWatcher`` ('p5push', P5-2 b, P5-3 c): a fake blueprint for one phone (it acknowledges a request 20 s after it
  sees it) and three restarts of the learner (R1 before the setup request is evaluated, R2 after the request and before
  the acknowledgement, R3 after it).
* ``BedAnswers`` ('p5adv', P5-5 d to f): [Later], [No bed in view] and its undo under 'Advanced'.
* ``RejectUser`` ('p5rej', P5-6): a place is rejected; the memory of the rejection; its undo.
* ``ZoneUser`` ('p5zone', P5-7): zone records by approval with names, a rename, a bed zone, a removal.
* ``GuestsUser`` ('p5guests', its control 'p5guestsc', P5-8 a): 'Guests present - pause learning' for the sessions of the
  world's guest and visitor.
* ``PetsUser`` ('p5pets', P5-8 b): 'Pets in home' on from the first second.

``P5Recorder`` is the run-wide record of the push requests, the refused answers and the saves (the driver owns one in a
P5 job). Interpretations are listed in ``replay.evaluate_p5.INTERPRETATION``.
"""
from __future__ import annotations

import dataclasses
import heapq
import re
from typing import Optional

from radar_zone_learner.core.params import DEFAULT_P5
from radar_zone_learner.core.types import (ITEM_BED, ITEM_ID_RE, ITEM_NIGHT_WINDOW, PUSH_KINDS, PUSH_TARGET_RE,
                                           AnswerIgnored, MarkPress, MarkResult, NameEcho,
                                           NodeCommand, PushAck, PushRequest, ReviewAnswer, ReviewChanged,
                                           ReviewSelect, SaveRequested, SettingSwitch, SpotName)

from . import world as W
from .framegen import BASE_MS
from .p4_scenarios import INF, PLACE_TYPES, Actor, BedStays

DAY_MS = W.DAY * 1000
LOOK_MS = 60_000                   # an actor looks at the review a minute after a nightly run (as the P4 PlaceUser)
STEP_MS = 2_000                    # the time between two taps of one flow
PRESS_EVERY_MS = 30_000            # 'Mark spot here' again every 30 s inside a stay (as the P4 MarkScript)
PUSH_FIELDS = ("seq", "acked", "kind", "count", "head_id", "head_rev", "acks")
_ITEM_RE = re.compile(ITEM_ID_RE)
_TARGET_RE = re.compile(PUSH_TARGET_RE)
BED_NAMES = ("bed", "bett")


# ---------------------------------------------------------------- plain views of the learner's objects
def zone_dict(v) -> dict:
    """A ``ZoneView`` as a plain dict (JSON): the cells as a count, the polygon in cm."""
    return {"id": v.id, "name": v.name, "kind": v.kind, "src": v.src, "ref": v.ref, "rev": v.rev,
            "cx": round(v.cx_m, 3), "cy": round(v.cy_m, 3), "cells": len(v.cells),
            "poly_cm": [list(p) for p in v.poly_cm], "frozen": bool(v.frozen), "night": v.night}


def push_dict(r) -> dict:
    """A ``PushRequest`` as a plain dict."""
    return {"seq": r.seq, "acked": r.acked, "kind": r.kind, "count": r.count, "head_id": r.head_id,
            "head_rev": r.head_rev, "acks": list(r.acks)}


def push_problems(r) -> list:
    """What is wrong with a ``PushRequest`` for check P5-3 (c): its fields must be exactly the seven of the spec and
    hold only int, str-id and None values (no text, no time, no name)."""
    out = []
    try:
        names = tuple(f.name for f in dataclasses.fields(r))
    except TypeError:
        names = ()
    if "attention" in names:                         # P7: a trailing bool (a radar-moved request), no text, no time, no name
        if type(getattr(r, "attention", None)) is not bool:
            out.append("attention=%r" % (getattr(r, "attention", None),))
        names = tuple(n for n in names if n != "attention")
    if names != PUSH_FIELDS:
        out.append("fields %s" % (names,))
    for k in ("seq", "acked", "count"):
        v = getattr(r, k, None)
        if type(v) is not int or v < 0:
            out.append("%s=%r" % (k, v))
    if getattr(r, "kind", None) not in PUSH_KINDS:
        out.append("kind=%r" % (getattr(r, "kind", None),))
    hid, hrev = getattr(r, "head_id", None), getattr(r, "head_rev", None)
    if hid is not None and not (isinstance(hid, str) and _ITEM_RE.match(hid) and hid not in (ITEM_BED, ITEM_NIGHT_WINDOW)):
        out.append("head_id=%r" % (hid,))
    if hrev is not None and type(hrev) is not int:
        out.append("head_rev=%r" % (hrev,))
    if (hid is None) != (hrev is None):
        out.append("head_id and head_rev differ in None-ness")
    acks = getattr(r, "acks", None)
    if not isinstance(acks, tuple) or not all(isinstance(a, str) and _TARGET_RE.match(a) for a in acks):
        out.append("acks=%r" % (acks,))
    return out


def unrle(runs) -> frozenset:
    return frozenset(c for s, n in runs for c in range(s, s + n))


def overlap_coef(a, b) -> float:
    """|a & b| / min(|a|, |b|) (the learner's suppression rule, OA 7.9)."""
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def is_bed_like(s: dict) -> bool:
    """A suggestion record of a night (``Run._record_night``) that is the bed place: the truth overlaps the bed, or the
    learner kind or name hint says bed."""
    return ((s.get("iou") or {}).get("bed", 0.0) >= 0.6 or s.get("kind") == "bed"
            or (s.get("name_hint") or "").strip().casefold() in BED_NAMES)


# ---------------------------------------------------------------- the run-wide record
class P5Recorder:
    """What a P5 job records over the whole run from the learner's outputs: every ``PushRequest`` (time, fields), every
    ``AnswerIgnored``, the ``SaveRequested`` reasons, and the type problems of the push requests (P5-3 c). The driver
    owns one in a P5 job (``Run.p5``) and writes ``result()`` into the run result."""

    def __init__(self):
        self.push_log = []
        self.ignored_log = []
        self.saves = {}
        self.push_problems = []
        self.push_checked = 0

    def on_output(self, o, t_ms: int) -> None:
        if isinstance(o, PushRequest):
            self.push_checked += 1
            probs = push_problems(o)
            if probs:
                self.push_problems.append({"t": t_ms, "problems": probs})
            d = push_dict(o)
            d["t"] = t_ms
            self.push_log.append(d)
        elif isinstance(o, AnswerIgnored):
            self.ignored_log.append({"t": t_ms, "item": o.item, "op": o.op, "reason": o.reason})
        elif isinstance(o, SaveRequested):
            self.saves[o.reason] = self.saves.get(o.reason, 0) + 1

    def result(self, restarts=()) -> dict:
        return {"push_log": self.push_log, "ignored_log": self.ignored_log, "saves": dict(self.saves),
                "push_problems": self.push_problems, "push_checked": self.push_checked,
                "push_fields": list(PUSH_FIELDS), "restarts": list(restarts)}


# ---------------------------------------------------------------- the base actor
class P5Actor(Actor):
    """An actor with a queue of timed steps and a mirror of the review list.

    ``at(t_ms, fn)`` schedules ``fn(feed, t_ms)``; ``next_time`` and ``act`` run the queue in time order (equal times in
    the order they were scheduled). The mirror (``items``: ``ReviewItem`` in list order, ``selected``) comes from the
    ``ReviewChanged`` outputs. ``on_night`` schedules a ``look`` a minute after every nightly run (until ``done``); a
    subclass overrides ``look(feed, t_ms)``. ``send(feed, event)`` feeds one event and returns the outputs it caused."""

    name = "p5actor"

    def __init__(self):
        super().__init__()
        self.queue = []
        self.qseq = 0
        self.items = []
        self.selected = None
        self.outs = None
        self.done = False
        self.last_rec = None
        self.night = None
        self.k = 0                      # a counter that keeps the events of one flow apart by 1 ms each

    # ---- the queue
    def at(self, t_ms: int, fn) -> None:
        self.qseq += 1
        heapq.heappush(self.queue, (int(t_ms), self.qseq, fn))

    def next_time(self) -> int:
        return self.queue[0][0] if self.queue else INF

    def act(self, feed) -> None:
        t, _, fn = heapq.heappop(self.queue)
        fn(feed, t)

    # ---- the mirror and the outputs
    def on_output(self, o) -> None:
        if isinstance(o, ReviewChanged):
            self.items = list(o.items)
            self.selected = o.selected
        if self.outs is not None:
            self.outs.append(o)

    def send(self, feed, ev) -> list:
        """Feeds ``ev`` and returns the outputs of that call (and of the node's answers handled inside it)."""
        self.outs = []
        try:
            feed(ev)
        finally:
            outs, self.outs = self.outs, None
        return outs

    def ms(self, t_ms: int) -> int:
        """Absolute time of an event of the flow at ``t_ms`` (1 ms apart from the previous one)."""
        self.k += 1
        return BASE_MS + t_ms + self.k

    def item(self, item_id: str):
        return next((i for i in self.items if i.id == item_id), None)

    def places(self) -> list:
        return [i for i in self.items if i.tier == 2 and i.type in PLACE_TYPES]

    def item_ids(self) -> set:
        return {i.id for i in self.items}

    # ---- the learner, read
    def learner(self):
        return self.run.learner

    def zone_views(self) -> list:
        try:
            return [zone_dict(v) for v in self.learner().zones()]
        except Exception:
            return []

    def state(self) -> dict:
        try:
            return self.learner().export_state()
        except Exception:
            return {}

    def stale_answers(self) -> Optional[int]:
        try:
            return ((self.learner().status().attributes.get("review") or {}).get("stale_answers"))
        except Exception:
            return None

    def zone_count(self) -> Optional[int]:
        rb = self.readback()
        return rb.get("zones")

    # ---- the nights
    def on_night(self, rec: dict) -> None:
        self.last_rec = rec
        self.night = rec["night"]
        self.nightly(rec)
        if not self.done:
            self.at((self.run.now - BASE_MS) + LOOK_MS, self.look)

    def nightly(self, rec: dict) -> None:
        pass

    def look(self, feed, t: int) -> None:
        pass

    def summary(self) -> dict:
        return {"done": self.done}


def _ign(outs: list) -> list:
    return [(o.item, o.op, o.reason) for o in outs if isinstance(o, AnswerIgnored)]


def _saves(outs: list, reason: str = "review") -> int:
    return sum(1 for o in outs if isinstance(o, SaveRequested) and o.reason == reason)


def _cmds(outs: list) -> list:
    return [o for o in outs if isinstance(o, NodeCommand)]


# ---------------------------------------------------------------- P5-1: scripted answers
class AnswerScript(P5Actor):
    """P5-1. At the first look at which a step's item exists (a nightly run + 60 s):

    1. ``stale_place``: approve the first place with ``rev - 1``: one ``AnswerIgnored(item, approve, stale)``, no zone,
       no ``SaveRequested("review")``, ``review.stale_answers`` + 1;
    2. ``current_place``: approve with ``rev``: exactly one zone record with ``ref`` = the item, one save, the item gone;
    3. ``redelivery``: the same message again: ``AnswerIgnored(item, approve, not_pending)``, nothing else;
    4. the night window, when its item exists: ``window_stale`` (``rev - 1``: no ``NodeCommand``), ``window_current`` (one
       ``NodeCommand`` with the item's times), ``window_second`` (a second current approve before the node's echo:
       no second command, ``not_pending``), ``window_gone`` (after the echo the item is gone: ``same``);
    5. the bed item, when it is open: ``bed_stale`` (``later`` with ``rev - 1`` is stale) and ``bed_current`` (``later``
       snoozes it).

    A step whose item never exists is reported as not exercised. ``rev_moves`` (reported): the nights on which the
    revision of an open place changed between two nightly runs."""

    name = "answer_script"

    def __init__(self):
        super().__init__()
        self.steps = []
        self.pending = {"place": True, "window": True, "bed": True}
        self.prev_revs = {}
        self.rev_moves = []

    def nightly(self, rec: dict) -> None:
        revs = {s["id"]: s["rev"] for s in rec["suggestions"] if s["type"] in PLACE_TYPES and s["state"] == "shadow"}
        moved = sorted(sid for sid, r in revs.items() if sid in self.prev_revs and self.prev_revs[sid] != r)
        if moved:
            self.rev_moves.append({"night": rec["night"], "ids": moved})
        self.prev_revs = revs

    def look(self, feed, t: int) -> None:
        if self.pending["place"] and self.places():
            self.pending["place"] = False
            self._place(feed, t)
        if self.pending["window"] and self.item(ITEM_NIGHT_WINDOW) is not None:
            self.pending["window"] = False
            self._window(feed, t)
        if self.pending["bed"] and self.item(ITEM_BED) is not None:
            self.pending["bed"] = False
            self._bed(feed, t)
        self.done = not any(self.pending.values())

    def _rec(self, key: str, ok: bool, **detail) -> None:
        d = {"step": key, "exercised": True, "ok": bool(ok)}
        d.update(detail)
        self.steps.append(d)

    def _place(self, feed, t: int) -> None:
        it = self.places()[0]
        z0, s0 = self.zone_count(), self.stale_answers()
        # 1. a stale answer changes nothing and says so
        outs = self.send(feed, ReviewAnswer(it.id, "approve", it.rev - 1, False, self.ms(t)))
        z1, s1 = self.zone_count(), self.stale_answers()
        ign = _ign(outs)
        self._rec("stale_place", ign == [(it.id, "approve", "stale")] and _saves(outs) == 0 and z1 == z0
                  and s0 is not None and s1 == s0 + 1,
                  item=it.id, rev=it.rev, ignored=ign, saves=_saves(outs), zones=[z0, z1], stale_answers=[s0, s1])
        # 2. the current revision makes one zone
        outs = self.send(feed, ReviewAnswer(it.id, "approve", it.rev, False, self.ms(t)))
        z2 = self.zone_count()
        recs = [z for z in self.zone_views() if z["ref"] == it.id]
        self._rec("current_place", len(recs) == 1 and _saves(outs) == 1 and not _ign(outs) and z2 == z0 + 1
                  and it.id not in self.item_ids(),
                  item=it.id, records=len(recs), saves=_saves(outs), ignored=_ign(outs), zones=[z0, z2],
                  still_listed=it.id in self.item_ids())
        # 3. the same message again (a QoS-1 duplicate) is harmless
        outs = self.send(feed, ReviewAnswer(it.id, "approve", it.rev, False, self.ms(t)))
        z3 = self.zone_count()
        ign = _ign(outs)
        self._rec("redelivery", ign == [(it.id, "approve", "not_pending")] and _saves(outs) == 0 and z3 == z2,
                  item=it.id, ignored=ign, saves=_saves(outs), zones=[z2, z3])

    def _window(self, feed, t: int) -> None:
        it = self.item(ITEM_NIGHT_WINDOW)
        want = (it.data.get("edit_start") if it.data.get("edit_start") is not None else it.data.get("start_min"),
                it.data.get("edit_end") if it.data.get("edit_end") is not None else it.data.get("end_min"))
        outs = self.send(feed, ReviewAnswer(ITEM_NIGHT_WINDOW, "approve", it.rev - 1, False, self.ms(t)))
        ign = _ign(outs)
        self._rec("window_stale", not _cmds(outs) and ign == [(ITEM_NIGHT_WINDOW, "approve", "stale")],
                  rev=it.rev, commands=len(_cmds(outs)), ignored=ign)
        outs = self.send(feed, ReviewAnswer(ITEM_NIGHT_WINDOW, "approve", it.rev, False, self.ms(t)))
        cmds = _cmds(outs)
        got = (cmds[0].start_min, cmds[0].end_min) if cmds else None
        self._rec("window_current", len(cmds) == 1 and cmds[0].kind == "window" and got == want and not _ign(outs),
                  commands=len(cmds), times=list(got) if got else None, expected=list(want), ignored=_ign(outs))
        outs = self.send(feed, ReviewAnswer(ITEM_NIGHT_WINDOW, "approve", it.rev, False, self.ms(t)))
        ign = _ign(outs)
        self._rec("window_second", not _cmds(outs) and ign == [(ITEM_NIGHT_WINDOW, "approve", "not_pending")],
                  commands=len(_cmds(outs)), ignored=ign)
        self.at(t + 5_000, self._window_gone)

    def _window_gone(self, feed, t: int) -> None:
        try:
            state = ((self.learner().status().attributes.get("review") or {}).get("night_window") or {}).get("state")
        except Exception:
            state = None
        gone = ITEM_NIGHT_WINDOW not in self.item_ids()
        self._rec("window_gone", gone and state == "same", state=state, listed=not gone)

    def _bed(self, feed, t: int) -> None:
        it = self.item(ITEM_BED)
        outs = self.send(feed, ReviewAnswer(ITEM_BED, "later", it.rev - 1, False, self.ms(t)))
        st = (self.readback().get("bed_item") or {}).get("state")
        ign = _ign(outs)
        self._rec("bed_stale", ign == [(ITEM_BED, "later", "stale")] and st == "open", rev=it.rev, ignored=ign,
                  state=st)
        outs = self.send(feed, ReviewAnswer(ITEM_BED, "later", it.rev, False, self.ms(t)))
        st = (self.readback().get("bed_item") or {}).get("state")
        self._rec("bed_current", st == "snoozed" and not _ign(outs) and ITEM_BED not in self.item_ids(),
                  state=st, ignored=_ign(outs))

    def summary(self) -> dict:
        todo = [k for k, v in self.pending.items() if v]
        return {"steps": self.steps, "not_exercised": todo, "rev_moves": self.rev_moves, "done": self.done}


# ---------------------------------------------------------------- P5-4: the first setup review
class SetupUser(P5Actor):
    """P5-4. On the first evening after a nightly run has made the item list non-empty (18:00 local of that day; the
    user is in bed at the first stay in bed after it, ``BedStays.next_press``): (i) approve the night window when its item
    is open; (ii) while the bed item is the selected one, 'Mark spot here' every 30 s inside the stay until a press
    succeeds (every press is a tap; a stay that ends first leaves the next stay for the next press); (iii) approve every
    place item in list order with its current revision, no typed names. A tap is one press of a button, one select
    choice that changes the selection, one answer; typing a name would be a text entry (none here). The record:
    ``taps``, ``places``, ``failed_marks``, ``items_at_start``, ``day``."""

    name = "setup_user"
    EVENING_S = 18 * 3600
    MAX_PRESSES = 40

    def __init__(self):
        super().__init__()
        self.stays = None
        self.started = False
        self.taps = 0
        self.log = []                      # (t, kind) per tap
        self.failed_marks = 0
        self.mark = None
        self.window = None
        self.items_at_start = None
        self.places_at_start = None
        self.day = None
        self.eve = None
        self.tries = {}
        self.end_t = None
        self.stuck = None
        self.mark_reasons = {}

    def attach(self, run) -> None:
        super().attach(run)
        self.stays = BedStays(run.stream.timeline)

    def add_day(self, rec) -> None:
        self.stays.add_day(rec)

    # ---- taps
    def tap(self, feed, ev, kind: str, t: int) -> list:
        self.taps += 1
        self.log.append([t, kind])
        return self.send(feed, ev)

    # ---- the flow
    def look(self, feed, t: int) -> None:
        if self.started or not self.items:
            return
        self.started = True
        self.items_at_start = [i.id for i in self.items]
        self.places_at_start = sum(1 for i in self.items if i.tier == 2)
        self.day = t // DAY_MS
        self.eve = self.day * DAY_MS + self.EVENING_S * 1000
        self._first_press(feed, max(t, self.eve))

    def _first_press(self, feed, after: int) -> None:
        tp = self.stays.next_press(after)
        if tp >= INF:                                   # the stay is not known yet: ask again in an hour
            self.at(after + 3_600_000, lambda f, t: self._first_press(f, t))
            return
        self.at(tp - STEP_MS, self._step_window)
        self.at(tp, self._step_press)

    def _step_window(self, feed, t: int) -> None:
        it = self.item(ITEM_NIGHT_WINDOW)
        self.window = it is not None
        if it is not None:
            self.tap(feed, ReviewAnswer(ITEM_NIGHT_WINDOW, "approve", it.rev, False, self.ms(t)), "approve_window", t)

    def _step_press(self, feed, t: int) -> None:
        if self.item(ITEM_BED) is None:                 # a bed zone or a place named bed exists: nothing to press for
            self.mark = {"ok": None, "note": "no bed item", "t": t}
            self._places_from(t + STEP_MS)
            return
        if self._presses() >= self.MAX_PRESSES:         # a user gives up: the places are still answered
            self.mark = {"ok": False, "note": "gave up after %d presses" % self.MAX_PRESSES, "t": t}
            self._places_from(t + STEP_MS)
            return
        if self.selected != ITEM_BED:
            self.tap(feed, ReviewSelect(ITEM_BED, self.ms(t)), "select_bed", t)
        outs = self.tap(feed, MarkPress(self.ms(t)), "mark", t)
        res = next((o for o in outs if isinstance(o, MarkResult)), None)
        key = "ok" if (res is not None and res.ok) else (res.reason if res is not None else "no_result")
        self.mark_reasons[key] = self.mark_reasons.get(key, 0) + 1
        if res is not None and res.ok:
            self.mark = {"ok": True, "kind": res.kind, "label": res.label_id, "t": t, "presses": self._presses()}
            self._places_from(t + STEP_MS)
            return
        self.failed_marks += 1
        nxt = self.stays.next_press(t + PRESS_EVERY_MS)
        if nxt >= INF:
            self.at(t + 3_600_000, self._step_press)
        else:
            self.at(nxt, self._step_press)

    def _presses(self) -> int:
        return sum(1 for _, k in self.log if k == "mark")

    def _places_from(self, t: int) -> None:
        self.at(t, self._step_places)

    def _step_places(self, feed, t: int) -> None:
        pl = [i for i in self.items if i.tier == 2]
        if not pl:
            self.done = True
            self.end_t = t
            return
        it = pl[0]
        n = self.tries.get(it.id, 0)
        if n >= 2:                                       # an answer that is refused twice: stop, do not loop
            self.stuck = it.id
            self.done = True
            self.end_t = t
            return
        self.tries[it.id] = n + 1
        if self.selected != it.id:
            self.tap(feed, ReviewSelect(it.id, self.ms(t)), "select_place", t)
        self.tap(feed, ReviewAnswer(it.id, "approve", it.rev, False, self.ms(t)), "approve_place", t)
        self.at(t + STEP_MS, self._step_places)

    def summary(self) -> dict:
        return {"done": self.done, "started": self.started, "taps": self.taps, "text_entries": 0,
                "places": self.places_at_start, "failed_marks": self.failed_marks,
                "items_at_start": self.items_at_start, "day": self.day, "window_approved": self.window,
                "mark": self.mark, "mark_reasons": self.mark_reasons, "tap_log": self.log, "stuck": self.stuck,
                "open_at_end": sorted(self.item_ids())}


# ---------------------------------------------------------------- P5-2 b: the push request and learner restarts
class PushWatcher(P5Actor):
    """P5-2 (b) and P5-3 (c). A fake blueprint for one phone (target ``fake``): 20 s of simulated time after it sees a
    request whose ``acks`` do not list ``fake`` it feeds ``PushAck(seq, recv_ms, "fake")``. Three restarts of the learner
    (``Run.restart_learner``): R1 just before the setup request is evaluated (at ``push_after_min`` of the first day
    with at least two open items), R2 10 s after the request is seen (before the acknowledgement), R3 10 s after the
    acknowledgement is seen. Every request the learner emits is logged with the restart that preceded it."""

    name = "push_watcher"
    ACK_DELAY_MS = 20_000
    RESTART_AFTER_MS = 10_000
    PHONE = "fake"

    def __init__(self, push_after_min: Optional[int] = None):
        super().__init__()
        self.push_after_min = DEFAULT_P5.push_after_min if push_after_min is None else push_after_min    # 08:00 local
        self.log = []
        self.acks_sent = {}                # seq -> time of the ack
        self.ack_due = {}                  # seq -> time the ack is due
        self.restarts = []
        self.r1_planned = False
        self.r2_planned = False
        self.r3_planned = False
        self.count_day = None

    def on_output(self, o) -> None:
        super().on_output(o)
        if isinstance(o, PushRequest):
            t = self.run.now - BASE_MS
            d = push_dict(o)
            d.update(t=t, after_restart=(self.restarts[-1]["point"] if self.restarts else None))
            self.log.append(d)
            if self.PHONE not in o.acks and o.seq > 0 and o.seq not in self.ack_due:
                self.ack_due[o.seq] = t + self.ACK_DELAY_MS
                self.at(t + self.ACK_DELAY_MS, lambda f, tt, seq=o.seq: self._ack(f, tt, seq))
            if o.seq == 1 and self.PHONE not in o.acks and not self.r2_planned:
                self.r2_planned = True
                self.at(t + self.RESTART_AFTER_MS, lambda f, tt: self._restart(f, tt, "R2"))
            if o.seq == 1 and self.PHONE in o.acks and not self.r3_planned:
                self.r3_planned = True
                self.at(t + self.RESTART_AFTER_MS, lambda f, tt: self._restart(f, tt, "R3"))

    def look(self, feed, t: int) -> None:
        if not self.r1_planned and len(self.items) >= 2:
            self.r1_planned = True
            self.count_day = t // DAY_MS
            due = self.count_day * DAY_MS + self.push_after_min * 60_000
            self.at(max(t, due - 500), lambda f, tt: self._restart(f, tt, "R1"))

    def _restart(self, feed, t: int, point: str) -> None:
        seq = self.log[-1]["seq"] if self.log else 0
        # recorded first: the new learner's first step (inside ``restart_learner``) already emits its requests
        self.restarts.append({"point": point, "t": t, "seq_before": seq, "night": self.night})
        self.run.restart_learner()

    def _ack(self, feed, t: int, seq: int) -> None:
        self.acks_sent[seq] = t
        self.send(feed, PushAck(seq, self.ms(t), self.PHONE))

    def summary(self) -> dict:
        return {"log": self.log, "restarts": self.restarts, "acks_sent": {str(k): v for k, v in self.acks_sent.items()},
                "count_day": self.count_day, "done": self.done}


# ---------------------------------------------------------------- P5-5 d to f: the bed item answers
class BedAnswers(P5Actor):
    """P5-5 d to f, in three looks (a nightly run + 60 s each):

    1. the first look with the bed item open: ``later`` (``bed_later``): the item is snoozed at once and leaves the list;
    2. the next look: the item is open again and listed, ``bed_rev`` rose by 2 (``bed_back``); then ``reject`` ([No bed in
       view], ``bed_reject``): one ``bed_needs_data`` suppression in the state, the Advanced entry ``adv:undo:bed``, the
       item away;
    3. the next look: the item is still away (``bed_away``); the answer ``advanced`` with ``adv:undo:bed``
       (``bed_undo``): the suppression and the entry are gone, the item is open and listed, ``bed_rev`` rose by 1."""

    name = "bed_answers"

    def __init__(self):
        super().__init__()
        self.phase = "later"
        self.steps = []
        self.rev0 = None
        self.rev_reject = None

    def _bed(self) -> dict:
        return self.readback().get("bed_item") or {}

    def _supp(self) -> list:
        return [s for s in (self.state().get("suppressions") or []) if s.get("type") == "bed_needs_data"]

    def _entry(self):
        try:
            return next((e for e in self.learner().advanced() if e.id == "adv:undo:bed"), None)
        except Exception:
            return None

    @staticmethod
    def _label(entry) -> Optional[str]:
        """The select label the shell builds for an Advanced entry (``shell.review_p5.advanced_label``), when the shell
        package is importable; None otherwise (the label text is pinned by the shell's own tests)."""
        try:
            from radar_zone_learner.shell.review_p5 import advanced_label
            return advanced_label(entry)
        except Exception:
            return None

    def _rec(self, key: str, ok: bool, **detail) -> None:
        d = {"step": key, "exercised": True, "ok": bool(ok)}
        d.update(detail)
        self.steps.append(d)

    def look(self, feed, t: int) -> None:
        it = self.item(ITEM_BED)
        if self.phase == "later" and it is not None:
            self.rev0 = it.rev
            outs = self.send(feed, ReviewAnswer(ITEM_BED, "later", it.rev, False, self.ms(t)))
            b = self._bed()
            self._rec("bed_later", b.get("state") == "snoozed" and ITEM_BED not in self.item_ids()
                      and not _ign(outs) and b.get("rev") == it.rev + 1,
                      state=b.get("state"), rev=[it.rev, b.get("rev")], listed=ITEM_BED in self.item_ids(),
                      ignored=_ign(outs), night=self.night)
            self.phase = "back"
        elif self.phase == "back":
            b = self._bed()
            ok = b.get("state") == "open" and it is not None and b.get("rev") == self.rev0 + 2
            self._rec("bed_back", ok, state=b.get("state"), rev=[self.rev0, b.get("rev")], listed=it is not None,
                      night=self.night)
            if it is None:
                self.phase = "done"
                return
            self.rev_reject = it.rev
            outs = self.send(feed, ReviewAnswer(ITEM_BED, "reject", it.rev, False, self.ms(t)))
            supp, entry = self._supp(), self._entry()
            b = self._bed()
            label = self._label(entry) if entry is not None else None
            self._rec("bed_reject", len(supp) == 1 and entry is not None and ITEM_BED not in self.item_ids()
                      and not _ign(outs) and (label is None or label == "Undo: No bed in view"),
                      suppressions=len(supp), entry=(entry.id if entry else None),
                      entry_kind=([entry.verb, entry.kind] if entry else None), label=label,
                      state=b.get("state"), listed=ITEM_BED in self.item_ids(), ignored=_ign(outs),
                      rev=[self.rev_reject, b.get("rev")], night=self.night)
            self.phase = "away"
        elif self.phase == "away":
            b = self._bed()
            entry = self._entry()
            self._rec("bed_away", b.get("state") == "suppressed" and ITEM_BED not in self.item_ids()
                      and entry is not None, state=b.get("state"), listed=ITEM_BED in self.item_ids(),
                      entry=(entry.id if entry else None), night=self.night)
            rev_before = b.get("rev")
            outs = self.send(feed, ReviewAnswer("adv:undo:bed", "advanced", None, False, self.ms(t)))
            b = self._bed()
            supp, entry = self._supp(), self._entry()
            self._rec("bed_undo", len(supp) == 0 and entry is None and b.get("state") == "open"
                      and ITEM_BED in self.item_ids() and b.get("rev") == rev_before + 1 and not _ign(outs),
                      suppressions=len(supp), entry=(entry.id if entry else None), state=b.get("state"),
                      listed=ITEM_BED in self.item_ids(), rev=[rev_before, b.get("rev")], ignored=_ign(outs),
                      night=self.night)
            self.phase = "done"
            self.done = True

    def summary(self) -> dict:
        return {"steps": self.steps, "phase": self.phase, "done": self.done}


# ---------------------------------------------------------------- P5-6: the memory of a rejection
class RejectUser(P5Actor):
    """P5-6. At the first look with an open place item that is not the bed place: ``reject`` it with its current
    revision, and record the rejected suggestion (id, ``rev``, cells), the suppression entry as the state exports it
    (the P4 form) and ``p5.masks`` (cells, centroid, ``n0``). At the first look from night ``undo_night`` (18): the
    answer ``advanced`` with ``adv:undo:<sid>``, and what is left of the suppression and the mask. The nights in
    between are checked from the night records (``replay.evaluate_p5.crit_p56``). The ``forget`` path is covered by the
    unit tests (spec P5-6 'Pass')."""

    name = "reject_user"

    def __init__(self, undo_night: int = 18):
        super().__init__()
        self.undo_night = undo_night
        self.rejected = None
        self.undone = None

    def look(self, feed, t: int) -> None:
        rec = self.last_rec
        if self.rejected is None:
            by_id = {s["id"]: s for s in rec["suggestions"]}
            it = next((i for i in self.places() if i.id in by_id and not is_bed_like(by_id[i.id])), None)
            if it is None:
                return
            s = by_id[it.id]
            outs = self.send(feed, ReviewAnswer(it.id, "reject", it.rev, False, self.ms(t)))
            st = self.state()
            supp = [dict(x) for x in (st.get("suppressions") or []) if x.get("type") == "place" and x.get("ref") == it.id]
            mask = ((st.get("p5") or {}).get("masks") or {}).get(it.id)
            self.rejected = {"id": it.id, "rev": it.rev, "night": rec["night"], "t": t, "tag": it.data.get("tag"),
                             "cells_rle": s["cells_rle"], "cx": s["cx"], "cy": s["cy"], "conf": s["conf"],
                             "ignored": _ign(outs), "saves": _saves(outs), "suppressions": supp, "mask": mask,
                             "still_listed": it.id in self.item_ids(), "advanced": [e["id"] for e in self._adv()]}
        elif self.undone is None and rec["night"] >= self.undo_night:
            sid = self.rejected["id"]
            outs = self.send(feed, ReviewAnswer("adv:undo:" + sid, "advanced", None, False, self.ms(t)))
            st = self.state()
            supp = [x for x in (st.get("suppressions") or []) if x.get("type") == "place" and x.get("ref") == sid]
            mask = ((st.get("p5") or {}).get("masks") or {}).get(sid)
            self.undone = {"night": rec["night"], "t": t, "ignored": _ign(outs), "suppressions": len(supp),
                           "mask": mask is not None, "advanced": [e["id"] for e in self._adv()]}
            self.done = True

    def _adv(self) -> list:
        try:
            return [{"id": e.id} for e in self.learner().advanced()]
        except Exception:
            return []

    def summary(self) -> dict:
        return {"rejected": self.rejected, "undone": self.undone, "undo_night": self.undo_night, "done": self.done}


# ---------------------------------------------------------------- P5-7: zone records and the answer paths
class ZoneUser(P5Actor):
    """P5-7. Tasks, each run at the first look at which its precondition holds, in this order:

    1. ``approve_desk``: ``SpotName("Desk")`` and approve the first place (no name in the answer: the name text is read
       and consumed): a record named ``Desk``, the text echoed as empty;
    2. ``approve_desk2``: approve a second place with the answer's name ``desk`` and an answer id: the stored name is
       ``desk 2`` (unique names, spec 3.5);
    3. ``rename``: ``advanced`` with ``adv:rename:<zid of Desk>`` and the name ``Office``: the zone id stays, the revision
       rises by one, the other zones are unchanged;
    4. ``approve_bett``: approve a place with the name ``Bett``: kind ``bed``, the bed item withdrawn, the zone frozen
       while the schedule is ON;
    5. ``remove``: ``advanced`` with ``adv:remove:<zid of Bett>``: the record is gone and its source suggestion is an open
       item again (as a place item, or as a merge item).

    Every task records the zones before and after (plain dicts) and ``next_zone_n``."""

    name = "zone_user"

    def __init__(self):
        super().__init__()
        self.todo = ["approve_desk", "approve_desk2", "rename", "approve_bett", "remove"]
        self.tasks = []
        self.snapshots = []
        self.zid = {}
        self.refs = {}
        self.queued = False

    def look(self, feed, t: int) -> None:
        if self.todo and not self.queued:
            self.queued = True
            self.at(t, self._step)

    def _snap(self, label: str, t: int) -> dict:
        st = self.state()
        s = {"label": label, "t": t, "night": self.night, "zones": self.zone_views(),
             "next_zone_n": (st.get("review") or {}).get("next_zone_n")}
        self.snapshots.append(s)
        return s

    def _step(self, feed, t: int) -> None:
        self.queued = False
        if not self.todo:
            return
        key = self.todo[0]
        ran = getattr(self, "_t_" + key)(feed, t)
        if ran:
            self.todo.pop(0)
            if not self.todo:
                self.done = True
            else:
                self.queued = True
                self.at(t + STEP_MS, self._step)

    def _free_places(self) -> list:
        taken = {z["ref"] for z in self.zone_views()}
        return [i for i in self.places() if i.id not in taken]

    def _task(self, key: str, ok: bool, **detail) -> None:
        d = {"task": key, "ok": bool(ok), "night": self.night}
        d.update(detail)
        self.tasks.append(d)

    def _t_approve_desk(self, feed, t: int) -> bool:
        pl = self._free_places()
        if not pl:
            return False
        it = pl[0]
        before = self._snap("before_desk", t)
        self.send(feed, SpotName("Desk", self.ms(t)))
        outs = self.send(feed, ReviewAnswer(it.id, "approve", it.rev, False, self.ms(t)))
        after = self._snap("after_desk", t)
        z = next((x for x in after["zones"] if x["ref"] == it.id), None)
        echo = [o.text for o in outs if isinstance(o, NameEcho)]
        if z is not None:
            self.zid["desk"] = z["id"]
        self._task("approve_desk", z is not None and z["name"] == "Desk" and echo == [""] and not _ign(outs)
                   and len(after["zones"]) == len(before["zones"]) + 1,
                   item=it.id, zone=z, echo=echo, ignored=_ign(outs))
        return True

    def _t_approve_desk2(self, feed, t: int) -> bool:
        pl = self._free_places()
        if not pl:
            return False
        it = pl[0]
        before = self._snap("before_desk2", t)
        outs = self.send(feed, ReviewAnswer(it.id, "approve", it.rev, False, self.ms(t), name="desk", aid="p5zone-2"))
        after = self._snap("after_desk2", t)
        z = next((x for x in after["zones"] if x["ref"] == it.id), None)
        self._task("approve_desk2", z is not None and z["name"] == "desk 2" and not _ign(outs)
                   and len(after["zones"]) == len(before["zones"]) + 1,
                   item=it.id, zone=z, ignored=_ign(outs))
        return True

    def _t_rename(self, feed, t: int) -> bool:
        zid = self.zid.get("desk")
        if zid is None:
            self._task("rename", False, note="no Desk zone")
            return True
        before = self._snap("before_rename", t)
        outs = self.send(feed, ReviewAnswer("adv:rename:" + zid, "advanced", None, False, self.ms(t), name="Office",
                                            aid="p5zone-3"))
        after = self._snap("after_rename", t)
        b = {z["id"]: z for z in before["zones"]}
        a = {z["id"]: z for z in after["zones"]}
        same_ids = list(b) == list(a)
        z = a.get(zid)
        others_same = all(a[k] == b[k] for k in b if k != zid and k in a)
        self._task("rename", same_ids and z is not None and z["name"] == "Office" and z["rev"] == b[zid]["rev"] + 1
                   and others_same and not _ign(outs),
                   zid=zid, rev=[b[zid]["rev"], z["rev"] if z else None], same_ids=same_ids, others_same=others_same,
                   ignored=_ign(outs))
        return True

    def _t_approve_bett(self, feed, t: int) -> bool:
        pl = self._free_places()
        if not pl:
            return False
        it = pl[0]
        before = self._snap("before_bett", t)
        outs = self.send(feed, ReviewAnswer(it.id, "approve", it.rev, False, self.ms(t), name="Bett", aid="p5zone-4"))
        after = self._snap("after_bett", t)
        z = next((x for x in after["zones"] if x["ref"] == it.id), None)
        rb = self.readback()
        bed = rb.get("bed_item") or {}
        if z is not None:
            self.zid["bett"] = z["id"]
            self.refs["bett"] = it.id
        self._task("approve_bett", z is not None and z["kind"] == "bed" and z["frozen"] is True
                   and bed.get("state") == "withdrawn" and not _ign(outs),
                   item=it.id, zone=z, bed_item=bed, ignored=_ign(outs),
                   schedule=(self.state().get("night") or {}).get("active_window"))
        return True

    def _t_remove(self, feed, t: int) -> bool:
        zid = self.zid.get("bett")
        if zid is None:
            self._task("remove", False, note="no Bett zone")
            return True
        sid = self.refs["bett"]
        before = self._snap("before_remove", t)
        outs = self.send(feed, ReviewAnswer("adv:remove:" + zid, "advanced", None, False, self.ms(t), aid="p5zone-5"))
        after = self._snap("after_remove", t)
        ids = self.item_ids()
        back = sid in ids or ("mrg-" + sid) in ids
        gone = zid not in {z["id"] for z in after["zones"]}
        self._task("remove", gone and len(after["zones"]) == len(before["zones"]) - 1 and back and not _ign(outs),
                   zid=zid, item=sid, open_again=back, listed=sorted(i for i in ids if sid in i), ignored=_ign(outs))
        return True

    def nightly(self, rec: dict) -> None:
        pass

    def summary(self) -> dict:
        return {"tasks": self.tasks, "snapshots": self.snapshots, "todo": list(self.todo), "done": self.done}


# ---------------------------------------------------------------- P5-8 a: guests present - pause learning
class GuestsUser(P5Actor):
    """P5-8 (a). ``pause`` True ('p5guests'): ``SettingSwitch("guests_pause", True)`` 60 s before every session of the
    world's guest ('G') and visitor ('V') and OFF 90 s after its end (the release can come at any time after the last
    sample: the pause is judged at the time of the evidence); False ('p5guestsc', the control): the same sessions and the
    same samples, no switch. Samples of the learner's status at the start, 5 min into the session, at its middle and
    5 min before the release (``state``, ``radar_valid``, ``learning_paused``). After every nightly run the stays near a
    guest or visitor spot (within 1 m) are collected, so a stay that the learner forgets later is still known.
    The check is ``replay.evaluate_p5.crit_p58``."""

    name = "guests_user"
    ON_LEAD_MS = 60_000
    OFF_LAG_MS = 90_000
    NEAR_M = 1.0

    def __init__(self, pause: bool = True):
        super().__init__()
        self.pause = pause
        self.sessions = []
        self.active = 0
        self.switches = []
        self.samples = []
        self.near = {}

    def add_day(self, rec) -> None:
        for ag, place, mode, t0, t1, x, y in rec.sessions:
            if ag not in ("G", "V"):
                continue
            on, off = t0 * 1000 - self.ON_LEAD_MS, t1 * 1000 + self.OFF_LAG_MS
            k = len(self.sessions)
            self.sessions.append({"agent": ag, "place": place, "t0": t0, "t1": t1, "x": round(x, 3), "y": round(y, 3),
                                  "on": on, "off": off})
            self.at(on, lambda f, t, k=k: self._on(f, t, k))
            self.at(off, lambda f, t, k=k: self._off(f, t, k))
            for tag, ts in (("start", on + 1), ("on+5min", on + 300_000), ("middle", (on + off) // 2),
                            ("off-5min", off - 300_000)):
                self.at(ts, lambda f, t, k=k, tag=tag: self._sample(f, t, k, tag))

    def _sample(self, feed, t: int, k: int, tag: str) -> None:
        try:
            st = self.learner().status()
            a = st.attributes
            self.samples.append({"session": k, "tag": tag, "t": t, "state": st.state, "radar_valid": a.get("radar_valid"),
                                 "learning_paused": a.get("learning_paused")})
        except Exception:
            pass

    def _on(self, feed, t: int, k: int) -> None:
        self.active += 1
        if self.pause and self.active == 1:
            self.send(feed, SettingSwitch("guests_pause", True, self.ms(t)))
            self.switches.append({"t": t, "on": True})

    def _off(self, feed, t: int, k: int) -> None:
        self.active -= 1
        if self.pause and self.active == 0:
            self.send(feed, SettingSwitch("guests_pause", False, self.ms(t)))
            self.switches.append({"t": t, "on": False})

    def nightly(self, rec: dict) -> None:
        try:
            stays = self.learner().stays()
        except Exception:
            return
        for st in stays:
            for k, s in enumerate(self.sessions):
                d = ((st.x_m - s["x"]) ** 2 + (st.y_m - s["y"]) ** 2) ** 0.5
                if d <= self.NEAR_M:
                    self.near[(st.t0_s, st.t1_s, round(st.x_m, 2), round(st.y_m, 2))] = {
                        "t0_s": st.t0_s, "t1_s": st.t1_s, "dur_s": st.dur_s, "x": round(st.x_m, 2),
                        "y": round(st.y_m, 2), "session": k, "dist": round(d, 3)}
                    break

    def summary(self) -> dict:
        try:
            paused = self.learner().paused_stays()
        except Exception:
            paused = None
        return {"pause": self.pause, "sessions": self.sessions, "switches": self.switches, "samples": self.samples,
                "stays_near": sorted(self.near.values(), key=lambda x: (x["t0_s"], x["t1_s"])),
                "paused_stays": paused, "done": self.done}


# ---------------------------------------------------------------- P5-8 b: pets in home
class PetsUser(P5Actor):
    """P5-8 (b): 'Pets in home' on in the first second of the run. The result is read from the night records (reported)."""

    name = "pets_user"

    def __init__(self, at_ms: int = 1000):
        super().__init__()
        self.at_ms = at_ms
        self.fired = False
        self.at(at_ms, self._on)

    def _on(self, feed, t: int) -> None:
        self.fired = True
        self.send(feed, SettingSwitch("pets", True, BASE_MS + t))

    def summary(self) -> dict:
        return {"on": self.fired, "at_ms": self.at_ms}


def make_actors(job) -> list:
    """The actors of a P5 job (``job.script`` starts with ``p5``)."""
    s = job.script
    if s == "p5setup":
        return [SetupUser()]
    if s == "p5ans":
        return [AnswerScript()]
    if s == "p5push":
        return [PushWatcher()]
    if s == "p5adv":
        return [BedAnswers()]
    if s == "p5rej":
        return [RejectUser()]
    if s == "p5zone":
        return [ZoneUser()]
    if s == "p5guests":
        return [GuestsUser(True)]
    if s == "p5guestsc":
        return [GuestsUser(False)]
    if s == "p5pets":
        return [PetsUser()]
    return []
