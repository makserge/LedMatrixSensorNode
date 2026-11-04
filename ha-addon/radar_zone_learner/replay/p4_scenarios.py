"""P4 scenarios for the replay driver: a fake node, Night learning, the bed item and scripted answers (OA 12 P4).

The driver (``replay.driver.Run``) owns one ``FakeNode`` and a list of *actors*; an actor is a user (or an
automation) that acts at simulated times, like the P3 ``LabelPlanner``:

* ``FakeNode``: what the P2 firmware does with the learner's ``NodeCommand`` (the shell's ``NodeLink`` and the
  node together): the schedule switch and the night times, the retained echo (``ScheduleState``) after 150 ms and
  the ``CommandResult``. A command with ``expect_current`` that differs from the node's schedule is refused
  (``changed_by_user``), and nothing is applied to a node without P2 (``no_p2``). The radar timeline of the stream
  is precomputed (``JobSpec.nl`` = the Night learning nights); ``summary`` checks it against the node's own log, so
  a learner that does not command what the timeline assumes shows as ``consistent`` False.
* ``NightLearningUser`` ('nl', 'nop2'): turns Night learning on in the first second of the run.
* ``MarkScript`` ('mark'): presses 'Mark spot here' (OA 7.6, 7.9): with the radars off, with nobody in view, with
  several people in view (two scripted people at the bed, the world has one sleeper), with the night_window item
  selected (kind manual) and with the bed item selected, during P1's stays in bed (before the window start and
  after its end).
* ``PlaceUser`` ('app', 'rej', 'names'): answers place suggestions: approve the place at the bed as 'Bed', reject
  the place the learner names 'bed', and approve places under different names (kinds) and rename a zone.

Every actor records what it did and what the learner showed right after (``summary``); ``replay.evaluate_p4`` turns
that into the numbered P4 checks.

P5 (``replay.p5_scenarios``): the actors of the P5 jobs are built on the same ``Actor`` interface; ``make_actors`` hands a
job whose script starts with ``p5`` to ``p5_scenarios.make_actors``. ``Actor.readback`` also gives the P5 view of the
learner (``zone_views``, ``advanced``, ``push``) and the 'mark' script records the kinds of the confirmed zones after
each press (``zone_kinds``), which P5-5b and P5-5c read.
"""
from __future__ import annotations

from typing import Optional

from radar_zone_learner.core.types import (ITEM_BED, ITEM_NIGHT_WINDOW, CommandResult, MarkPress, MarkResult,
                                           NightLearningChanged, NightLearningSwitch, NodeCommand, ReviewAnswer,
                                           ReviewChanged, ReviewSelect, ScheduleState, SpotName)

from . import world as W
from .framegen import BASE_MS, OBSERVE_MS

INF = 1 << 62
ECHO_MS = 150                      # the node's retained state topic follows a command within this time
PRESS_EVERY_MS = 30_000
PRESS_FIRST_MS = 60_000
PLACE_TYPES = ("dwell_zone", "short_task_zone")


# ---------------------------------------------------------------- the node
class FakeNode:
    """The node's schedule state and its answers to ``NodeCommand`` (P2 firmware stand-in)."""

    def __init__(self, p2: bool, sched: bool, start_min: int, end_min: int):
        self.p2 = p2
        self.sched = sched if p2 else None
        self.start_min = start_min if p2 else None
        self.end_min = end_min if p2 else None
        self.log = []              # one dict per command: t (sim ms), kind, sched, reason, applied, result
        self.off_runs = []         # [off_ms, on_ms or None]: the schedule was OFF between (set by commands)

    def initial_event(self) -> ScheduleState:
        return ScheduleState(self.sched, self.start_min, self.end_min, BASE_MS)

    def command(self, cmd: NodeCommand, now_ms: int) -> list:
        """The events [(abs_ms, event)] the node causes: the echo and the result."""
        t = now_ms - BASE_MS
        e = {"t": t, "token": cmd.token, "kind": cmd.kind, "sched": cmd.sched, "start": cmd.start_min,
             "end": cmd.end_min, "expect": cmd.expect_current, "reason": cmd.reason, "applied": False,
             "result": None}
        self.log.append(e)
        out = []

        def result(ok, why):
            e["result"] = why if not ok else (why or "ok")
            out.append((now_ms + ECHO_MS + 1, CommandResult(cmd.token, cmd.kind, ok, why, now_ms + ECHO_MS + 1)))

        if not self.p2:
            result(False, "no_p2")
            return out
        if cmd.expect_current is not None and self.sched != cmd.expect_current:
            result(False, "changed_by_user")
            return out
        changed = False
        if cmd.sched is not None and cmd.sched != self.sched:
            self._set_sched(cmd.sched, t)
            changed = True
        if cmd.kind == "window":
            if cmd.start_min is not None and cmd.start_min != self.start_min:
                self.start_min = cmd.start_min
                changed = True
            if cmd.end_min is not None and cmd.end_min != self.end_min:
                self.end_min = cmd.end_min
                changed = True
        e["applied"] = changed
        if changed:
            out.append((now_ms + ECHO_MS, ScheduleState(self.sched, self.start_min, self.end_min, now_ms + ECHO_MS)))
            result(True, None)
        else:
            result(True, "unchanged")
        return out

    def user_sets_schedule(self, on: bool, now_ms: int) -> list:
        """The user changes the schedule switch: the echo event (for scenarios; not used by the P4 jobs)."""
        self._set_sched(on, now_ms - BASE_MS)
        return [(now_ms + ECHO_MS, ScheduleState(self.sched, self.start_min, self.end_min, now_ms + ECHO_MS))]

    def _set_sched(self, on: bool, t: int) -> None:
        if on == self.sched:
            return
        self.sched = on
        if on is False:
            self.off_runs.append([t, None])
        elif self.off_runs and self.off_runs[-1][1] is None:
            self.off_runs[-1][1] = t

    def summary(self, timeline, cfg: dict, horizon_days: int) -> dict:
        """The node's log, and the extra radar-on time the commands caused against the timeline's."""
        nominal = []
        nc = dict(cfg, nl=0)
        for d in range(horizon_days + 1):
            nominal.extend(W.night_window_off(d, nc))
        nominal = [tuple(x) for x in W.union(nominal)]
        end_s = (horizon_days + 1) * W.DAY
        runs = [(a // 1000, (b // 1000) if b is not None else end_s) for a, b in self.off_runs]
        by_node = sum(W.overlap(sorted(runs), a, b) for a, b in nominal)
        actual = sorted(timeline.off_seconds())
        by_timeline = sum(b - a for a, b in nominal) - sum(W.overlap(actual, a, b) for a, b in nominal)
        return {"p2": self.p2, "commands": list(self.log), "off_runs_s": [list(r) for r in runs],
                "sched_end": self.sched, "extra_on_h_node": round(by_node / 3600.0, 3),
                "extra_on_h_timeline": round(by_timeline / 3600.0, 3),
                "consistent": abs(by_node - by_timeline) <= 60}


# ---------------------------------------------------------------- actors
class Actor:
    """A user at the review: ``next_time`` (sim ms since the run's start) and ``act(feed)`` like the P3
    ``LabelPlanner``; ``add_day`` gets the world's days, ``on_night`` the night record after each nightly run,
    ``on_output`` every output of the learner."""
    name = "actor"

    def __init__(self):
        self.run = None

    def attach(self, run) -> None:
        self.run = run

    def next_time(self) -> int:
        return INF

    def act(self, feed) -> None:
        pass

    def on_output(self, o) -> None:
        pass

    def add_day(self, rec) -> None:
        pass

    def on_night(self, rec: dict) -> None:
        pass

    def summary(self) -> dict:
        return {}

    # what the learner shows right now (the status attributes), for 'at once' checks
    def readback(self) -> dict:
        L = self.run.learner
        try:
            a = L.status().attributes
            sugg = [{"id": s.id, "kind": s.kind, "frozen": bool(s.frozen)} for s in L.suggestions()
                    if s.type in PLACE_TYPES and s.state == "shadow" and s.kind == "bed"]
            nl = len(L.labels())
        except Exception:
            return {}
        rv = a.get("review") or {}
        out = {"night": a.get("nights"), "items": rv.get("items"), "selected": rv.get("selected"),
               "bed_item": rv.get("bed_item"), "bed": rv.get("bed"), "zones": rv.get("zones"), "labels": nl,
               "bed_places": sugg}
        # P5 (spec 11.6): the confirmed zones as plain dicts (records, then labels), the Advanced entry ids and the
        # push request. ``zones`` above keeps its P4 meaning (the number of zone records), so the view is
        # ``zone_views``. A learner without the P5 accessors (a stub) leaves these keys out.
        try:
            from .p5_scenarios import push_dict, zone_dict
            out["zone_views"] = [zone_dict(v) for v in L.zones()]
            out["advanced"] = [e.id for e in L.advanced()]
            out["push"] = push_dict(L.push_request())
        except Exception:
            pass
        return out

    def zones(self) -> list:
        try:
            return [dict(z) for z in self.run.learner.export_state().get("zones", [])]
        except Exception:
            return []


class NightLearningUser(Actor):
    """Starts Night learning in the first second of the run ('nl'), also on a node without P2 ('nop2')."""
    name = "nl_user"

    def __init__(self, at_ms: int = 1000):
        super().__init__()
        self.at = at_ms
        self.fired = False
        self.events = []

    def next_time(self) -> int:
        return INF if self.fired else self.at

    def act(self, feed) -> None:
        self.fired = True
        feed(NightLearningSwitch(True, BASE_MS + self.at))

    def on_output(self, o) -> None:
        if isinstance(o, NightLearningChanged):
            try:
                nl = self.run.learner.status().attributes.get("night_learning") or {}
            except Exception:
                nl = {}
            self.events.append({"t": self.run.now - BASE_MS, "active": o.active, "until": o.until,
                                "set_by_learner": o.set_by_learner, "phase": nl.get("phase"),
                                "last_event": nl.get("last_event")})

    def summary(self) -> dict:
        return {"events": self.events}


# ---------------------------------------------------------------- 'Mark spot here'
class BedStays:
    """P1's stays in bed from the world (evening before the window, morning after it), sim ms."""

    def __init__(self, timeline):
        self.tl = timeline
        self.stays = []
        self.idx = 0

    def add_day(self, rec) -> None:
        for ag, place, mode, t0, t1, x, y in rec.sessions:
            if ag == "P1" and place == "bed":
                self.stays.append((t0 * 1000, t1 * 1000))

    def next_observing(self, t: int) -> int:
        tl = self.tl
        for _ in range(8):
            if tl.observing(t):
                return t
            nxt = None
            for a, b in tl.offs:
                if a <= t < b + OBSERVE_MS:
                    nxt = b + OBSERVE_MS
                    break
            t = nxt if nxt is not None else t + 1000
        return t

    def next_press(self, after: int, until: Optional[int] = None) -> Optional[int]:
        """The next press time >= ``after``: inside a stay, 60 s after its start at the earliest, while the radars
        observe. INF while no such stay is known yet; None past ``until``."""
        while self.idx < len(self.stays) and self.stays[self.idx][1] <= after:
            self.idx += 1
        for k in range(self.idx, len(self.stays)):
            a, b = self.stays[k]
            if until is not None and a >= until:
                return None
            t = self.next_observing(max(after, a + PRESS_FIRST_MS))
            if t < b:
                return t
        return INF


class _Step:
    __slots__ = ("key", "select", "plan", "stop", "pre", "started", "done", "after", "t", "attempts", "results",
                 "note", "skipped")

    def __init__(self, key, select, plan, stop, pre=None):
        self.key = key
        self.select = select           # review item to select before each press, or None
        self.plan = plan               # plan(after_ms) -> press time, INF (wait) or None (exhausted)
        self.stop = stop               # "first" | "ok" | "reason:<r>"
        self.pre = pre                 # pre(script) -> bool; False skips the step
        self.started = False
        self.done = False
        self.after = 0
        self.t = INF
        self.attempts = 0
        self.results = []
        self.note = None
        self.skipped = False


class MarkScript(Actor):
    """The scripted presses of the 'mark' scenario (see the module docstring), step by step:

    1. ``not_observing``: day 8 02:00, the radars are off (window 00:00-06:00), the bed item selected;
    2. ``no_stay``: the middle of a home-empty spell of >= 30 min on day 8, nobody in view;
    3. ``several_stays``: day 8 21:01:30-21:24, two scripted people in bed (``JobSpec.extras``);
    4. ``manual``: the night_window item selected (if the learner shows it), in P1's next stay in bed;
    5. ``bed``: the bed item selected, in P1's stays in bed until a press succeeds.
    """
    name = "mark"
    DAY = W.DAY * 1000

    def __init__(self):
        super().__init__()
        self.steps = []
        self.stays = None
        self.items = set()
        self.selected = None
        self.pending = None
        self.he_slot = None
        self.floor = 0                 # time of the last press: the next step starts a minute after it
        self.log = []
        self.bed_mark = None

    def attach(self, run) -> None:
        super().attach(run)
        self.stays = BedStays(run.stream.timeline)
        t1 = 8 * self.DAY + 2 * 3600 * 1000
        e0 = 8 * self.DAY + 21 * 3600 * 1000
        e1 = e0 + 1500 * 1000

        def one(at):
            return lambda after: at if after <= at else None

        def two(after):
            if self.he_slot is None:
                return INF
            return self.he_slot if after <= self.he_slot else None

        def three(after):
            t = max(after, e0 + 90_000)
            return t if t < e1 else None

        def four(after):
            return self.stays.next_press(max(after, e1), e1 + 2 * self.DAY)

        def five(after):
            return self.stays.next_press(max(after, e1))

        def need_nw(script):
            return ITEM_NIGHT_WINDOW in script.items

        self.steps = [_Step("not_observing", ITEM_BED, one(t1), "first"),
                      _Step("no_stay", ITEM_BED, two, "first"),
                      _Step("several_stays", ITEM_BED, three, "reason:several_stays"),
                      _Step("manual", ITEM_NIGHT_WINDOW, four, "ok", pre=need_nw),
                      _Step("bed", ITEM_BED, five, "ok")]

    def add_day(self, rec) -> None:
        self.stays.add_day(rec)
        if self.he_slot is None and rec.d >= 8:
            for a, b in W.union([tuple(x) for x in rec.he]):
                if b - a >= 1800:
                    self.he_slot = int((a + b) // 2) * 1000
                    break

    def _head(self) -> Optional[_Step]:
        while self.steps and self.steps[0].done:
            self.steps.pop(0)
        return self.steps[0] if self.steps else None

    def next_time(self) -> int:
        while True:
            st = self._head()
            if st is None:
                return INF
            if not st.started:
                st.started = True
                if st.pre is not None and not st.pre(self):
                    st.done = st.skipped = True
                    st.note = "not exercised: the night_window item is not shown"
                    self.log.append(self._entry(st, None))
                    continue
            t = st.plan(max(st.after, self.floor + PRESS_FIRST_MS if self.floor else 0))
            if t is None:
                st.done = True
                st.note = st.note or "no more press times"
                self.log.append(self._entry(st, None))
                continue
            st.t = t
            return t

    def act(self, feed) -> None:
        st = self._head()
        if st is None or st.t >= INF:
            return
        t = st.t
        if st.select is not None and self.selected != st.select:
            self.selected = st.select
            feed(ReviewSelect(st.select, BASE_MS + t))
        st.attempts += 1
        st.after = t + PRESS_EVERY_MS
        self.floor = t
        self.pending = (st, t, len(self.run.learner.labels()))
        feed(MarkPress(BASE_MS + t + 1))
        self.pending = None

    def on_output(self, o) -> None:
        if isinstance(o, ReviewChanged):
            self.items = {i.id for i in o.items}
        elif isinstance(o, MarkResult) and self.pending is not None:
            st, t, nlabels = self.pending
            rb = self.readback()
            rec = {"t": t, "ok": o.ok, "reason": o.reason, "kind": o.kind, "labels_before": nlabels,
                   "labels_after": rb.get("labels"), "bed_item": rb.get("bed_item"), "bed": rb.get("bed"),
                   "night": rb.get("night"),
                   # P5-5b/5c: the kinds of the confirmed zones right after the press (None: no P5 accessor)
                   "zone_kinds": ([v["kind"] for v in rb["zone_views"]] if "zone_views" in rb else None)}
            st.results.append(rec)
            if (st.stop == "first" or (st.stop == "ok" and o.ok)
                    or (st.stop.startswith("reason:") and o.reason == st.stop[7:])):
                st.done = True
                self.log.append(self._entry(st, rec, rb))
                if st.key == "bed" and o.ok:
                    self.bed_mark = {"night": rb.get("night"), "t": t, "kind": o.kind, "attempts": st.attempts,
                                     "readback": rb}

    @staticmethod
    def _entry(st: _Step, last, rb=None) -> dict:
        reasons = {}
        for r in st.results:
            k = "ok" if r["ok"] else (r["reason"] or "?")
            reasons[k] = reasons.get(k, 0) + 1
        return {"step": st.key, "skipped": st.skipped, "note": st.note, "attempts": st.attempts,
                "reasons": reasons, "last": last, "readback": rb}

    def summary(self) -> dict:
        pending = []
        for st in self.steps:
            if not st.done:
                pending.append(self._entry(st, st.results[-1] if st.results else None))
        return {"steps": self.log, "unfinished": pending, "bed_mark": self.bed_mark}


# ---------------------------------------------------------------- answering place suggestions
NAMES_PLAN = (("approve", "Desk", False), ("approve", "Bedroom", False), ("rename", "Bed", True),
              ("approve", "bett", True), ("approve", "BED", True), ("approve", " Bed ", True))


class PlaceUser(Actor):
    """Answers place suggestions one minute after a nightly run: 'app' approves the place at the bed as 'Bed',
    'rej' rejects the place the learner names 'bed', 'names' approves places under different names and renames
    a zone (``NAMES_PLAN``: the third element is whether the kind must come out as bed)."""

    def __init__(self, mode: str):
        super().__init__()
        self.mode = mode
        self.name = "place_" + mode
        self.due = None
        self.last = None
        self.done = False
        self.record = {}
        self.steps = []
        self.k = 0
        self.zone_ids = []

    def on_night(self, rec: dict) -> None:
        if self.done:
            return
        self.last = rec
        self.due = (self.run.now - BASE_MS) + 60_000

    def next_time(self) -> int:
        return INF if self.due is None or self.done else self.due

    def act(self, feed) -> None:
        t = self.due
        self.due = None
        getattr(self, "_act_" + self.mode)(feed, t)

    def _act_app(self, feed, t: int) -> None:
        rec = self.last
        if rec["night"] < self.run.params.review_min_nights:
            return
        cand = [s for s in rec["suggestions"] if s["type"] in PLACE_TYPES and s["state"] == "shadow"
                and (s.get("iou") or {}).get("bed", 0.0) >= 0.6]
        if not cand:
            return
        s = max(cand, key=lambda x: x["iou"]["bed"])
        before = self.readback()
        feed(SpotName("Bed", BASE_MS + t))
        feed(ReviewAnswer(s["id"], "approve", s["rev"], False, BASE_MS + t + 1000))
        z = [x for x in self.zones() if x.get("ref") == s["id"]]
        self.record = {"night": rec["night"], "suggestion": s["id"], "iou_bed": s["iou"]["bed"],
                       "before": before, "after": self.readback(),
                       "zone": ({"name": z[0]["name"], "kind": z[0]["kind"]} if z else None)}
        self.done = True

    def _act_rej(self, feed, t: int) -> None:
        rec = self.last
        L = self.run.learner
        if rec["night"] < self.run.params.review_min_nights:
            return
        cand = [s for s in L.suggestions() if s.type in PLACE_TYPES and s.state == "shadow"
                and (s.kind == "bed" or (s.name_hint or "").strip().casefold() in ("bed", "bett"))]
        if not cand:
            return
        s = cand[0]
        before = self.readback()
        feed(ReviewAnswer(s.id, "reject", s.rev, False, BASE_MS + t))
        self.record = {"night": rec["night"], "suggestion": s.id, "name_hint": s.name_hint, "before": before,
                       "after": self.readback()}
        self.done = True

    def _act_names(self, feed, t: int) -> None:
        rec = self.last
        L = self.run.learner
        steps = self.record.setdefault("steps", [])
        while self.k < len(NAMES_PLAN):
            op, name, want_bed = NAMES_PLAN[self.k]
            at = BASE_MS + t + 2000 * (self.k + 1)
            if op == "approve":
                used = {z.get("ref") for z in self.zones()}
                free = [s for s in L.suggestions() if s.type in PLACE_TYPES and s.state == "shadow"
                        and s.id not in used]
                if not free:
                    break
                s = free[0]
                feed(SpotName(name, at))
                feed(ReviewAnswer(s.id, "approve", s.rev, False, at + 1))
                z = [x for x in self.zones() if x.get("ref") == s.id]
                steps.append({"op": "approve", "name": name, "want_bed": want_bed, "night": rec["night"],
                              "suggestion": s.id, "kind": z[0]["kind"] if z else None,
                              "zone_name": z[0]["name"] if z else None})
                if z:
                    self.zone_ids.append(z[0]["zid"])
            else:
                if not self.zone_ids:
                    break
                before = self.readback()
                feed(SpotName(name, at))
                feed(ReviewAnswer(self.zone_ids[0], "rename", None, False, at + 1))
                z = [x for x in self.zones() if x.get("zid") == self.zone_ids[0]]
                steps.append({"op": "rename", "name": name, "want_bed": want_bed, "night": rec["night"],
                              "zone": self.zone_ids[0], "kind": z[0]["kind"] if z else None,
                              "before": before, "after": self.readback()})
            self.k += 1
        if self.k >= len(NAMES_PLAN):
            self.done = True

    def summary(self) -> dict:
        d = dict(self.record)
        d["done"] = self.done
        d["mode"] = self.mode
        return d


def make_actors(job) -> list:
    """The actors of a job's scenario (none for a P3 job and for 'base')."""
    s = job.script
    if s.startswith("p5"):
        from . import p5_scenarios
        return p5_scenarios.make_actors(job)
    if s in ("nl", "nop2"):
        return [NightLearningUser()]
    if s == "mark":
        return [MarkScript()]
    if s in ("app", "rej", "names"):
        return [PlaceUser(s)]
    return []
