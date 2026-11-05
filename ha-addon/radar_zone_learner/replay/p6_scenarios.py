"""The actors and the recorder of the P6 replay jobs (P6 spec 11.1, 11.6 items 1-3 and 6).

A P6 job has no scripted *users* of its own besides the 'Mark spot here' presses of two jobs: the people (walkers,
stayers, door walkers) are part of the frame stream (``replay.p6_people``), so that their truth is exact. What the
jobs need from the driver is

* places to evaluate: ``make_actors`` returns the P5 approval actor (``SetupUser``: the night-7 approvals of the
  P5 approval-only job) for every P6 job, then the P6 actors:
  ``ZoneProbe`` (what zones the learner reports and which scripted spot each covers), ``HoldScript``, ``PowerScript``,
  ``DoorScript`` (one 'Mark spot here' press by the scripted door on day 8), ``LdScript`` (one press at the reflector on
  day 8: the ghost zone) and ``GuestsUser`` (the pause pair ``p6guests`` / ``p6guestsc``, the P5 actor);
* ``P6Recorder``: every ``ZoneEvent`` and ``ZoneStates`` of the learner with the clocks of spec 11.1 (the emission time of
  the simulated ``now``, the ``recv_ms`` of the batch in progress and the boot offset of the observation epoch), the
  timeline of the radars, the dropped messages and the scripted people, and what the learner shows after each nightly run
  (``Run._record_night``): the zones with their cells, ``Learner.zone_states()``, ``zone6`` of the state, the door
  suggestions. ``replay.evaluate_p6`` reads only this record.

Times in the record are simulated milliseconds since the start of day 0 (``recv_ms - BASE_MS``) unless a name says ``dev``.
"""
from __future__ import annotations

from typing import Optional

from radar_zone_learner.core.types import MarkPress, MarkResult, SpotName, ZoneEvent, ZoneStates

from . import world as W
from .batcher import ON
from .framegen import BASE_MS, BOOT_SIM_MS, OBSERVE_MS
from .p4_scenarios import INF, Actor
from .p5_scenarios import GuestsUser, SetupUser
from .truth import rle

DAY_MS = W.DAY * 1000
PRESS_EVERY_MS = 30_000
PRESS_FIRST_MS = 60_000
EVENT_COLUMNS = ("emit_ms", "ctx", "recv_ms", "boot_ms", "zone", "kind", "via", "t_dev_ms", "track", "dwell_s", "variant",
                 "first_in_dev_ms", "k")
STATE_ROW = ("id", "occ", "count", "held", "variant", "ldr", "delay_s", "delay_src", "margin_cm")
MAX_DEBUG = 1000


def _safe(fn, default=None):
    """``fn()``, or ``default`` when it raises (a stub learner without the accessor raises AttributeError before the call)."""
    try:
        return fn()
    except Exception:
        return default


def _get(obj, name: str):
    """The bound method ``obj.name`` or a function that raises (so that ``_safe`` gives the default for a missing accessor)."""
    return getattr(obj, name, None) or (lambda: (_ for _ in ()).throw(AttributeError(name)))


def zone_row(z) -> list:
    """One ``ZoneState`` as the list of ``STATE_ROW`` (JSON)."""
    return [z.id, bool(z.occ), int(z.count), bool(z.held), z.variant, bool(z.ldr), int(z.delay_s), z.delay_src,
            int(z.margin_cm)]


def zone_dict(v) -> dict:
    """A ``ZoneView`` for the record: the cells run-length coded, the position, the kind and when it was made."""
    return {"id": v.id, "name": v.name, "kind": v.kind, "src": v.src, "ref": v.ref, "rev": v.rev,
            "cx": round(v.cx_m, 3), "cy": round(v.cy_m, 3), "cells_rle": rle(v.cells), "n_cells": len(v.cells),
            "created_s": v.created_s, "night": v.night}


# ---------------------------------------------------------------- the run-wide recorder
class P6Recorder:
    """What a P6 job records over the whole run from the learner's outputs (the driver owns one: ``Run.p6``).

    ``ctx`` says where an output came from: ``b`` a frames message (the learner returned it while handling the batch whose
    ``recv_ms`` is ``cur_recv``), ``t`` a tick, ``e`` any other event (a node status, a press, ...)."""

    def __init__(self, run):
        self.run = run
        self.ctx = "e"
        self.cur_recv: Optional[int] = None
        self.boot_ms = BOOT_SIM_MS
        self.events = []
        self.states = []
        self.last_rows = {}
        self.last_valid = None
        self.enters_seen = 0
        self.debug_missing = 0

    # ---- the driver tells what it is doing
    def note_batch(self, recv_ms: int) -> None:
        self.cur_recv = recv_ms
        self.boot_ms = self.epoch_boot(recv_ms)

    def epoch_boot(self, recv_ms: int) -> int:
        """The boot offset (simulated ms) of the node boot a frames message of ``recv_ms`` belongs to."""
        tl = self.run.stream.timeline
        t = recv_ms - BASE_MS
        boot = tl.epochs[0].boot
        for e in tl.epochs[1:]:
            if t >= e.start:                      # the first message of the next boot is sent at its start
                boot = e.boot
        return boot

    # ---- outputs
    def on_output(self, o, now_ms: int) -> None:
        if isinstance(o, ZoneEvent):
            self._event(o, now_ms)
        elif isinstance(o, ZoneStates):
            self._states(o, now_ms)

    def _event(self, o: ZoneEvent, now_ms: int) -> None:
        first_in = k = None
        if o.kind == "enter":
            self.enters_seen += 1
            dbg = _safe(_get(self.run.learner, "zone_debug"), ())
            for d in reversed(tuple(dbg or ())[-50:]):
                if d[0] == o.id and d[1] == o.track:
                    first_in, k = d[2], d[4]
                    break
            else:
                self.debug_missing += 1
        recv = self.cur_recv if self.ctx == "b" else None
        self.events.append([now_ms - BASE_MS, self.ctx, (recv - BASE_MS) if recv is not None else None, self.boot_ms, o.id,
                            o.kind, o.via, o.t_dev_ms, o.track, o.dwell_s, o.variant, first_in, k])

    def _states(self, o: ZoneStates, now_ms: int) -> None:
        changed = []
        for z in o.zones:
            row = zone_row(z)
            if self.last_rows.get(z.id) != row:
                self.last_rows[z.id] = row
                changed.append(row)
        if changed or o.valid != self.last_valid:
            self.last_valid = o.valid
            self.states.append([now_ms - BASE_MS, self.ctx, self.boot_ms, bool(o.valid), o.t_dev_ms, changed])

    # ---- the nightly record (``Run._record_night``)
    def night(self, run, rec: dict) -> dict:
        L = run.learner
        out = {}
        views = _safe(_get(L, "zones"), ())
        out["zones"] = [zone_dict(v) for v in views or ()]
        zs = _safe(_get(L, "zone_states"))
        if zs is not None:
            out["zone_states"] = {"valid": bool(zs.valid), "rows": [zone_row(z) for z in zs.zones],
                                  "variants": {z.id: list(z.variants) for z in zs.zones if z.variants},
                                  "requested": {z.id: list(z.requested) for z in zs.zones if z.requested}}
        st = _safe(_get(L, "export_state"), {}) or {}
        z6 = (st.get("p5") or {}).get("zone6")
        if z6:
            out["zone6"] = {k: z6.get(k) for k in ("v", "set", "tune", "delay", "ld", "ldr", "info", "ve") if k in z6}
        out["entry_zones"] = [{"id": s.id, "state": s.state, "cx": round(s.cx_m, 3), "cy": round(s.cy_m, 3),
                               "n_cells": len(s.cells), "first_night": s.first_night, "last_night": s.last_night}
                              for s in _safe(_get(L, "suggestions"), ()) or () if s.type == "entry_zone"]
        out["counters"] = {k: v for k, v in (_safe(_get(L, "counters"), {}) or {}).items() if str(k).startswith("zone")}
        out["now_ms"] = run.now - BASE_MS
        return out

    # ---- the run result
    def timeline(self) -> dict:
        tl = self.run.stream.timeline
        on = []
        for e in tl.epochs:
            for s in e.segments:
                if s.phase != ON:
                    continue
                a, b = s.start, s.end if e.stop_ms is None else min(s.end, e.stop_ms)
                if b <= a:
                    continue
                if on and on[-1][1] == a:
                    on[-1][1] = b
                else:
                    on.append([a, b])
        if tl.loss is not None:                       # the message loss window: nothing observes through it
            la, lb = tl.loss
            cut = []
            for a, b in on:
                if b <= la or a >= lb:
                    cut.append([a, b])
                else:
                    if a < la:
                        cut.append([a, la])
                    if b > lb:
                        cut.append([lb, b])
            on = cut
        return {"offs": [list(x) for x in tl.offs], "on": on,
                "epochs": [{"boot": e.boot, "start": e.start, "stop": e.stop_ms, "id": e.boot_id} for e in tl.epochs],
                "loss": list(tl.loss) if tl.loss is not None else None, "reboot": tl.reboot, "end": tl.end,
                "observe_ms": OBSERVE_MS}

    def result(self) -> dict:
        run = self.run
        sc = run.scripted
        stream = run.stream
        return {"event_columns": list(EVENT_COLUMNS), "events": self.events, "state_row": list(STATE_ROW),
                "states": self.states, "timeline": self.timeline(),
                "dropped": [list(x) for x in stream.dropped], "spans": [list(x) for x in stream.spans],
                "people": sc.log if sc is not None else [], "t_end_ms": run.now - BASE_MS,
                "zones": [zone_dict(v) for v in (_safe(_get(run.learner, "zones"), ()) or ())],
                "enters": self.enters_seen, "enters_without_debug": self.debug_missing}


# ---------------------------------------------------------------- actors
class P6Actor(Actor):
    """An actor of a P6 job (the ``Actor`` interface of ``replay.p4_scenarios``): it records, or presses."""
    name = "p6actor"

    def summary(self) -> dict:
        return {}


class ZoneProbe(P6Actor):
    """Reports the zones the learner makes: the night of the first zone and, after every nightly run, which scripted spot
    each zone covers (the zone's cells grown by the margin contain the spot). Spec 11.1: the zones under test are the ones
    ``Learner.zones()`` reports; the replay confirms which they are per seed."""

    name = "zone_probe"

    def __init__(self):
        super().__init__()
        self.nights = {}

    def on_night(self, rec: dict) -> None:
        from .jobs import P6_SPOTS
        from .truth import ZoneArea
        try:
            views = self.run.learner.zones()
        except Exception:                      # a stub learner without ``zones``
            views = ()
        cover = {}
        for v in views:
            area = ZoneArea(v.cells, 25)
            for spot, (x, y) in P6_SPOTS.items():
                if area.inside(x, y):
                    cover.setdefault(spot, []).append(v.id)
        self.nights[rec["night"]] = {"zones": [[v.id, v.kind] for v in views], "spots": cover}

    def summary(self) -> dict:
        first = next((n for n, r in sorted(self.nights.items()) if r["zones"]), None)
        last = self.nights[max(self.nights)] if self.nights else {}
        return {"first_zone_night": first, "last": last, "nights": {str(n): r for n, r in sorted(self.nights.items())}}


class HoldScript(P6Actor):
    """P6-3 and P6-7: the hidden spells are scripted in the stream; this records the plan and, after each nightly run, the
    clear delay in force of every zone (the capture of ``zone6.delay`` itself is ``Run._record_night``)."""

    name = "hold_script"

    def __init__(self):
        super().__init__()
        self.delays = {}

    def on_night(self, rec: dict) -> None:
        zs = _safe(_get(self.run.learner, "zone_states"))
        if zs is not None:
            self.delays[rec["night"]] = {z.id: [z.delay_s, z.delay_src] for z in zs.zones}

    def summary(self) -> dict:
        return {"spells": [list(x) for x in self.run.job.spells], "delays": {str(n): d for n, d in sorted(self.delays.items())}}


class PowerScript(P6Actor):
    """P6-4 and P6-5: the radar-off windows, the fault run and the scripted gaps belong to the stream; this records the plan."""

    name = "power_script"

    def summary(self) -> dict:
        tl = self.run.stream.timeline
        return {"gaps": [list(g) for g in self.run.job.gaps], "reboot": tl.reboot,
                "loss": list(tl.loss) if tl.loss else None, "offs": len(tl.offs)}


class PressScript(P6Actor):
    """One 'Mark spot here' at a stationary stayer: a press every 30 s from 60 s into the stay while the radars observe,
    until a press succeeds or the stay ends (the stay is an extra of ``JobSpec.extras``)."""

    name = "press_script"

    def __init__(self, label: str, spot_name: str, day: int, t0_s: int, t1_s: int):
        super().__init__()
        self.label = label
        self.spot_name = spot_name
        self.t0 = day * DAY_MS + t0_s * 1000
        self.t1 = day * DAY_MS + t1_s * 1000
        self.next_press = self.t0 + PRESS_FIRST_MS
        self.named = False
        self.done = False
        self.attempts = 0
        self.results = {}
        self.pending = False
        self.mark = None

    def next_time(self) -> int:
        if self.done or self.next_press >= self.t1:
            return INF
        return self.next_press

    def act(self, feed) -> None:
        t = self.next_press
        self.next_press += PRESS_EVERY_MS
        if not self.run.stream.timeline.observing(t):
            return
        if not self.named:
            self.named = True
            feed(SpotName(self.spot_name, BASE_MS + t))
        self.attempts += 1
        self.pending = True
        feed(MarkPress(BASE_MS + t + 1))
        self.pending = False

    def on_output(self, o) -> None:
        if isinstance(o, MarkResult) and self.pending:
            key = "ok" if o.ok else str(o.reason)
            self.results[key] = self.results.get(key, 0) + 1
            if o.ok and self.mark is None:
                self.mark = {"label": o.label_id, "kind": o.kind, "t": self.run.now - BASE_MS}
                self.done = True

    def summary(self) -> dict:
        return {"label": self.label, "spot_name": self.spot_name, "attempts": self.attempts, "results": self.results,
                "mark": self.mark, "done": self.done}


class DoorScript(PressScript):
    """P6-8: the label zone by the scripted door (a stayer 0.25 m inside it on day 8, 10:00-11:30, a press every 30 s)."""

    name = "door_script"

    def __init__(self):
        from .jobs import REFLECTOR_LABEL_DAY, REFLECTOR_LABEL_S
        super().__init__("door", "Door spot", REFLECTOR_LABEL_DAY, *REFLECTOR_LABEL_S)


class LdScript(PressScript):
    """P6-9 (ghost): the label zone at the reflector (an extra Y1 there on day 8, 10:00-11:30, not fed to the LD2412 model)."""

    name = "ld_script"

    def __init__(self):
        from .jobs import REFLECTOR_LABEL_DAY, REFLECTOR_LABEL_S
        super().__init__("ghost", "Fan", REFLECTOR_LABEL_DAY, *REFLECTOR_LABEL_S)


def make_actors(job) -> list:
    """The actors of a P6 job: the P5 approval actor (the places to evaluate), the zone probe and the job's own script."""
    s = job.script
    actors = [SetupUser(), ZoneProbe()]
    if s in ("p6hold", "p6ld"):
        actors.append(HoldScript())
    elif s == "p6pow":
        actors.append(PowerScript())
    elif s == "p6door":
        actors.append(DoorScript())
    elif s == "p6ldghost":
        actors.append(LdScript())
    elif s == "p6guests":
        actors.append(GuestsUser(True))
    elif s == "p6guestsc":
        actors.append(GuestsUser(False))
    return actors
