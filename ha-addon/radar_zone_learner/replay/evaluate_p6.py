"""Evaluation of the P6 replay checks (P6 spec 11.2, 11.6; confirmed-zone evaluation, add-on 0.6.0).

The checks that a replay run can show (the others are unit tests, listed in the report as 'evidence: unit tests'):

* **P6-1** Enter within 0.8 s, **P6-2** leave at the edge within 0.5 s, **P6-15** the dwell pass speed (jobs ``p6edge``,
  ``p6edge05``, ``p6edge08``: scripted walkers through every zone, at detection noise 0.02, 0.05 and 0.08 m).
* **P6-3** A target vanishing inside a zone is held until the clear delay (job ``p6hold``).
* **P6-4** No events on radar power transitions and gaps; **P6-5 (a)** zones unavailable while the radars are off;
  **P6-6 (i)** the recorder rows model (job ``p6pow``).
* **P6-7** Learned clear delays (job ``p6hold``); **P6-8** door fast-clear (job ``p6door``); **P6-9** ``ld2412_reliable`` and the
  early clear (jobs ``p6ld``, ``p6ldghost``); **P6-10** time variants (job ``p6setup``); **P6-11** zone evaluation while
  learning is paused (the pair ``p6guests`` / ``p6guestsc``); **P6-14** zone count (job ``p6count``).

Status per check: PASS, FAIL, INCOMPLETE (runs missing, too short, or a scripted step that could not be exercised), REPORTED.
The k-of-n rule is P4's: 4 of 5 seeds, all of them with fewer than 5 (``replay.evaluate._need``); here a seed that fails does not
fail the check while enough seeds still pass (the spec's wording: 'in at least 4 of 5 seeds'); P6-4 and P6-5 (a) are deterministic
rules and need every seed. A step that was not exercised is not a pass. P6-12 and P6-13 (shell, blueprint), P6-5 (b) and P6-6 (ii)
are unit tests, P6-R1 (``replay.compare_runs``), P6-R2 (``replay.compat_check --p6``) and P6-R3 (``replay.resource_probe --p6``)
are separate commands. Interpretations are in ``INTERPRETATION``.

The evaluator reads only the run result (``result["p6"]`` and ``nights[*]["p6"]``, written by ``replay.p6_scenarios``): the truth
is computed here, after the run, from the scripted people and the zone cells the learner reported (``replay.truth.ScriptedTruth``).
"""
from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import evaluate_p4 as E4
from . import jobs as J
from . import world as W
from .evaluate import FAIL, INCOMPLETE, PASS, REPORTED, _need
from .framegen import BASE_MS, OBSERVE_MS
from .p6_scenarios import EVENT_COLUMNS
from .truth import ScriptedTruth, ZoneArea, counted_enter, counted_leave, unrle

ALL_SEEDS = (1, 2, 3, 4, 5)
DAY_MS = W.DAY * 1000
EDGE_SCRIPTS = ("p6edge", "p6edge05", "p6edge08")
EVALUATED_KINDS = ("dwell", "short_task", "bed", "manual")
DWELL_KINDS = ("dwell", "bed", "manual")
GATED_NOISE = (0.02, 0.05)                 # P6-1, P6-2, P6-15 are gated at these noise levels; 0.08 m is reported
ENTER_FRAME_S, ENTER_BATCH_S, ENTER_ARRIVAL_S = 0.8, 0.57, 0.84
LEAVE_FRAME_S = 0.5
EARLY_TOL_MIN_S = 0.2
MIN_PROBES = 10                            # fewer counted crossings in a seed: INCOMPLETE
SPELL_MIN_CLASS = 4                        # P6-3: fewer classified spells of a class in a seed: INCOMPLETE
BORDER_S = 1                               # P6-3: a spell this close to the delay in force (either side) is not judged
HOLD_EMIT_S = 6.0                          # P6-3: a hold is judged on batch time, so the leave comes with the first batch past the deadline (heartbeat 5 s + 1 s)
FAST_CLEAR_S = 3
DOOR_OK_S = FAST_CLEAR_S + 1
EARLY_ABSENT_S = 60
LD_MIN_KNOWN_S, LD_MIN_SHARE = 3600, 0.8
FALSE_EARLY_MAX, FALSE_EARLY_MIN_SPELLS = 0.05, 100
UNSEEN_EARLY_MIN = 0.80                    # P6-9: the share of unseen leaves in an ldr zone that must clear early (the rest: a ghost track)
GHOST_MIN_KNOWN_S = 600
ROWS_LIMIT = 500
ROWS_EPISODES_MIN = 20
EPISODE_BUDGET = 74                        # P6Params.episode_budget_day: informational
COUNT_MATCH_MIN = 0.90
NIGHTLY_SLOT_MS = (3 * 3600 * 1000 + 29 * 60 * 1000, 4 * 3600 * 1000 + 30 * 60 * 1000)     # the learned delay changes here
POWERUP_BIRTH_MS = 3000
GAP_POWER_MS = 15_000
SCOPE_P6 = "P6-1 to P6-11, P6-14 and P6-15 (the replay-evaluated checks); G1-G6, P4-1 to P4-7 and P5-1 to P5-8 not run"

UNIT_EVIDENCE = [
    ("P6-1 to P6-4, P6-14, P6-15 (rules)", "Membership and margin, enter 3-of-10, pass speed, edge leave, hold, deadline, two-person "
     "cases, power-up silence, the censor classes (an unknown reason is POWER), signature emission", "tests/test_zoneeval.py"),
    ("P6-7, P6-9, P6-10 (rules)", "Spells to delay, late returns, caps, ldr, the variants copy, a failing nightly step is contained",
     "tests/test_zoneinfo.py"),
    ("P6-R2 (state)", "zone6 defaults, validation, orphans, unknown keys, byte identity, the sync into the p5 extra",
     "tests/test_p6_state.py, tests/test_p6_types.py"),
    ("P6-5 (b), P6-6 (ii), P6-12", "Availability against the fake Home Assistant, the recorder rows, entities, topics, discovery, "
     "tuning, lifecycle", "tests/test_p6_layer.py, tests/test_p6_recorder.py, tests/test_p6_discovery.py, tests/test_p6_topics.py, "
     "tests/test_p6_lifecycle.py, tests/test_p6_tuning.py, tests/test_p6_service.py"),
    ("P6-11 (spy)", "The evaluator never reads Observation.paused", "tests/test_p6_pause.py"),
    ("P6-13", "The spot-light blueprint (text and structure checks)", "tests/test_p6_blueprint.py"),
]
OTHER_COMMANDS = [
    ("P6-R1", "Nothing of P3, P4 or P5 learning changed",
     "replay/dump_run.py on the baseline tree (git archive 91c05e9) and on this tree for the P5 jobs, then replay/compare_runs.py "
     "--a <base> --b <p6>"),
    ("P6-R2", "State compatibility in both directions", "replay/compat_check.py --p6 --base <baseline tree> --out <dir>"),
    ("P6-R3", "Cost", "replay/resource_probe.py --p6 --out <dir>"),
]

INTERPRETATION = [
    "Truth. The zone truth is computed after the run: the scripted people (replay.p6_people) are noise-free 1 Hz waypoints, "
    "linearly interpolated exactly as replay.framegen does (the last waypoint of a track is held for 0.9 s), and are crossed with the "
    "zone area = the zone's learned 0.2 m cells grown by the margin in force (default 25 cm). A counted crossing goes at least 50 cm past "
    "the area edge along its path (probe_depth) and reaches at least 20 cm from the edge perpendicular to it (a path that skims the "
    "edge is grazing: noise at the edge decides it, not the rule; a measured need: at noise 0.05 m a path 5 cm inside the edge leaves "
    "early in 1 % of the crossings); grazing crossings are not gated. Only crossings after the zone's created_s and entirely within "
    "observed time (Timeline.on) count. A crossing is 'into an empty zone' when no other scripted person is inside it (+-0.5 s) and no "
    "scripted person vanished inside it within the delay in force + 5 s before.",
    "Clocks. sim_ms = t_dev_ms + the boot offset of the observation epoch; the arrival time of an event is the recv_ms of the batch "
    "that carried its decisive frame (P6Recorder.ctx = 'b'). P6-3 judges the emission time: the simulated 'now' at which the "
    "learner returned the event (a tick every 1 s in a P6 job).",
    "P6-1: the frame lag of the enter is gated at 0.8 s + 0.1 s per frame that gave no counted-inside detection (Learner.zone_debug 'k': a "
    "missed frame or a speed-excluded detection) and the batching component (recv_ms of the decisive batch minus the frame time) at 0.57 s; "
    "the sum is reported against the design-level 0.84 s. An enter earlier than the crossing minus max(0.2 s, 4 x noise / speed) is a fail. "
    "For a dwell, bed or manual zone only 0.7 m/s is an enter probe (1.0 and 1.2 m/s are passes: P6-15); a short_task zone takes 0.7, 1.0 "
    "and 1.2 m/s; 0.5 m/s and noise 0.08 m are reported only.",
    "P6-2: a leave probe is a counted crossing from inside to outside of a walker that was entered (an enter event of the zone within the "
    "crossing) while it was alone in the zone; the frame lag is gated at 0.5 s and via 'edge'; the arrival lag is reported (0.1 s + the "
    "firmware batch gate 0.5 s + 0.04 s). The leave that matches the crossing is the first one in [crossing - tolerance, crossing + 3 s]; "
    "an earlier leave that is followed by an enter before the crossing is the flicker of the entry edge (noise puts one detection outside "
    "the area right after the enter: 1-4 of 118 crossings at 0.05 m, counted as flicker_leaves, not a fail), an earlier leave with no "
    "enter after it is an early leave (a fail). The edge flicker table (leave/enter pairs per hour against the signed distance of the sweep "
    "stayers to the area edge) is reported and decides nothing.",
    "P6-3: the last detection of a spell is the last visible waypoint + 0.9 s; the delay in force is the zone's delay_s at that time; the "
    "leave must be emitted in [last_det + d - 0.3 s, last_det + d + 2 s] (0.3 s: the last frames of a spell can be missed) and via "
    "hold_expired; a return before the deadline gives no leave, the zone stays occupied and its count is 0 while held.",
    "P6-4: events whose sim_ms lies in [off start - 1 s, off end + warm-up + 3 s], in the reboot and loss windows, and in every recorded gap "
    "span (a run of dropped messages shorter than 15 s: [span start, span end + 1.5 s]) are counted unless they coincide (1 s) with an "
    "edge of a scripted truth crossing of that zone; the first ZoneStates after a resume with a stayer inside must show occ = true with no "
    "event; a gap must not turn the zones invalid; a hold deadline that falls into a gap moves by the gap.",
    "P6-5 (a): valid must be false from 1 s after the start of a scheduled radar-off window until observing is true again (off end + 12 "
    "s), and from 16 s after the start of the reboot gap or the message loss window (the POWER silence rule) until observing is true again.",
    "P6-6 (i): the spec reads the 14-night run p6pow, which has no busy people (at most 6 episodes a zone and day in the replay), so the "
    "rows model is also applied to the visits of p6guests and p6guestsc (about 200 a day, 40-50 episodes a zone and day: gated) and "
    "reported for p6edge, p6edge05 and p6edge08, whose edge sweep flickers on purpose (80, 300 and 530 episodes a zone and day at "
    "noise 0.02, 0.05 and 0.08 m: not gated, the spec's own arithmetic puts the limit at 121 episodes for the default set and 74 for "
    "all entities recorded). "
    "Rows per zone and local day from the recorded states and events: the default set = occupancy changes + events + 4 rows per "
    "radar-off cycle + 8 attribute rows; all recorded = occupancy changes + events + count changes + 18 rows per cycle + 20 attribute rows "
    "+ 14 slack (the arithmetic of spec 7.5 applied to what the run recorded). INCOMPLETE when no zone-day has more than 20 episodes; "
    "the verdict is conditional on the episode rate.",
    "P6-7: the learned delay must equal the nearest-rank p95 of the state's own captured spells exactly (clamped to 5..600 s); before 10 "
    "spells the delay in force is the default by kind; the delay in force changes only at the nightly run (03:29-04:30 local). The "
    "comparison with the scripted spells that satisfy the recording rule is gated only when the two sets have the same size (3 s: the "
    "spell is measured from the last detection, not from the last waypoint). A tune override and ZoneForget are unit tests.",
    "P6-8: the world's own door D2 can never become an entry_zone suggestion (the learner's door rule needs the region of births "
    "and deaths to reach beyond the walking envelope, which the world's walkers that end at D2 close; measured: 14 nights, 172 door "
    "events near D2, no suggestion), so the job scripts its own door beyond the east wall (replay.jobs.DOOR_SITE) with births only on "
    "days 8-11 and exits from day 12 on; a mover that vanishes inside the desk zone is the 'no door' control. The precondition (an entry_zone suggestion in state shadow within 1.4 m of D2 on the evaluation day, day 12 or later; the scripted door is not the world's D2, see p6_people) is read "
    "from the night records; probes are the scripted exits that vanish moving inside the label zone by the scripted door and the stationary controls.",
    "P6-9: ldr is read from zone6 at the nights and from ZoneStates.ldr; the false early clear rate counts hidden 240 s spells of the "
    "desk stayer (the LD2412 model says present) that ended with an early leave; the unseen leave of each hour must clear early (via "
    "early, 60-100 s after the last detection) when ldr is true and by hold_expired otherwise; the ghost zone is the zone that contains "
    "the world's reflector.",
    "P6-10: the replay checks that variant_now (ZoneState.variant) matches the weekday/weekend class and the hours of the variant id "
    "(wd or we, start and end hour) at the time of every emission that carries one; the rest is unit tests.",
    "P6-11: the projection (emit time, zone, occ, count, valid, held) of ZoneStates, and the ZoneEvent sequence, must be identical in "
    "the pause run and its control; INCOMPLETE if the zone sets differ or no event falls inside a pause window.",
    "P6-14: the count is compared with the truth number of scripted people inside the zone area every second, leaving out the 1.5 s around "
    "each change of the truth; cases A (two people), B (three) and C (one vanishes while another stays) are judged by their events.",
]


# ---------------------------------------------------------------- helpers
def _script(res: dict) -> str:
    return (res.get("job_spec") or {}).get("script") or ""


def is_p6_run(res: dict) -> bool:
    return _script(res).startswith("p6")


def _runs(results: list, *scripts) -> list:
    return sorted([r for r in results if _script(r) in scripts], key=lambda r: (r.get("job", ""), r["seed"]))


def _ok(res: dict) -> bool:
    return res.get("status") == "ok" and not res.get("core_errors")


def _row(res: dict, status: str, **kw) -> dict:
    d = {"job": res.get("job"), "seed": res.get("seed"), "status": status}
    d.update(kw)
    return d


def _combine(statuses: list) -> str:
    return E4._combine(statuses)


def _kofn(rows: list) -> tuple:
    """(status, passed, of) of per-seed rows by the k-of-n rule: PASS when at least ``_need(n)`` rows pass; FAIL when so many
    rows failed that ``_need(n)`` passes cannot be reached; else INCOMPLETE (a row that was not exercised is no pass)."""
    sts = [x["status"] for x in rows]
    n = len(rows)
    passed = sts.count(PASS)
    if n == 0:
        return INCOMPLETE, 0, 0
    need = _need(n)
    if passed >= need:
        return PASS, passed, n
    if sts.count(FAIL) > n - need:
        return FAIL, passed, n
    return INCOMPLETE, passed, n


def _all(rows: list) -> tuple:
    """Deterministic rule: every row must pass; FAIL when any fails; INCOMPLETE when none fails but one is not exercised."""
    sts = [x["status"] for x in rows]
    if not rows:
        return INCOMPLETE, 0, 0
    if FAIL in sts:
        return FAIL, sts.count(PASS), len(rows)
    return (PASS if all(s == PASS for s in sts) else INCOMPLETE), sts.count(PASS), len(rows)


def _percentile(vals: list, q: float) -> Optional[float]:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def _stats(vals: list) -> dict:
    v = [x for x in vals if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "p50": round(_percentile(v, 0.5), 3), "p95": round(_percentile(v, 0.95), 3), "max": round(max(v), 3)}


def _nearest_rank(spells: list, q: float = 0.95) -> Optional[int]:
    if not spells:
        return None
    s = sorted(spells)
    return s[max(1, math.ceil(q * len(s))) - 1]


def _local_ms(t_ms: float) -> tuple:
    """(day index, ms of the local day) of a simulated time (the replay's time model has offset 0)."""
    d, r = divmod(int(t_ms), DAY_MS)
    return d, r


# ---------------------------------------------------------------- one run, read
class ZoneInfo:
    def __init__(self, z: dict, margin_cm: int):
        self.id = z["id"]
        self.kind = z["kind"]
        self.name = z.get("name")
        self.cells = unrle(z.get("cells_rle") or [])
        self.margin = margin_cm
        self.created_ms = (z.get("created_s") or 0) * 1000 - BASE_MS
        self.area = ZoneArea(self.cells, margin_cm)


class RunView:
    """A run result read for the checks: events, the series of zone states, the timeline, the zones and the scripted truth."""

    def __init__(self, res: dict):
        self.res = res
        self.p6 = res.get("p6") or {}
        cols = self.p6.get("event_columns") or list(EVENT_COLUMNS)
        self.events = []
        for row in self.p6.get("events", []):
            e = dict(zip(cols, row))
            e["sim_ms"] = e["t_dev_ms"] + e["boot_ms"]
            self.events.append(e)
        self.ev_by_zone = defaultdict(list)
        for e in self.events:
            self.ev_by_zone[e["zone"]].append(e)
        self.states = self.p6.get("states", [])
        self.zt = defaultdict(list)
        self.zr = defaultdict(list)
        self.vt, self.vf = [], []
        for st in self.states:
            t, _ctx, _boot, valid, _tdev, rows = st
            self.vt.append(t)
            self.vf.append(bool(valid))
            for r in rows:
                self.zt[r[0]].append(t)
                self.zr[r[0]].append(r)
        tl = self.p6.get("timeline") or {}
        self.timeline = tl
        self.t_end = self.p6.get("t_end_ms")          # the run stops at the last nightly run: nothing after it was fed
        self.on = sorted((a, min(b, self.t_end) if self.t_end is not None else b) for a, b in
                         (tuple(x) for x in tl.get("on", [])) if self.t_end is None or a < self.t_end)
        self._on_start = [a for a, _ in self.on]
        self.people = self.p6.get("people", [])
        self.truth = ScriptedTruth(self.people)
        self.zones = {}
        for z in self.p6.get("zones", []):
            rows = self.zr.get(z["id"])
            margin = rows[0][8] if rows else 25
            self.zones[z["id"]] = ZoneInfo(z, margin)
        self.spec = res.get("job_spec") or {}
        self.noise = self.spec.get("noise_m", 0.02)
        self.nights = sorted(res.get("nights") or [], key=lambda n: n["night"])

    # ---- series
    def zone_row(self, zid: str, t: float):
        ts = self.zt.get(zid)
        if not ts:
            return None
        k = bisect_right(ts, t) - 1
        return self.zr[zid][k] if k >= 0 else None

    def valid_at(self, t: float) -> Optional[bool]:
        k = bisect_right(self.vt, t) - 1
        return self.vf[k] if k >= 0 else None

    def observing(self, t: float) -> bool:
        k = bisect_right(self._on_start, t) - 1
        return k >= 0 and self.on[k][0] <= t < self.on[k][1]

    def observed_between(self, a: float, b: float) -> bool:
        k = bisect_right(self._on_start, a) - 1
        return k >= 0 and self.on[k][0] <= a and b <= self.on[k][1]

    def delay_at(self, zid: str, t: float, default_s: int = 300) -> int:
        r = self.zone_row(zid, t)
        return r[6] if r else default_s

    def events_of(self, zid: str, kind: Optional[str] = None) -> list:
        return [e for e in self.ev_by_zone.get(zid, []) if kind is None or e["kind"] == kind]

    def first_event(self, zid: str, kind: str, lo_sim: float, hi_sim: float, key: str = "sim_ms") -> Optional[dict]:
        for e in self.ev_by_zone.get(zid, []):
            if e["kind"] == kind and lo_sim <= e[key] <= hi_sim:
                return e
        return None

    def zone_for(self, xy: tuple, kinds=EVALUATED_KINDS) -> Optional[ZoneInfo]:
        """The evaluated zone whose area holds ``xy`` with the largest margin to the edge, or None."""
        best, bd = None, -1e9
        for z in self.zones.values():
            if z.kind not in kinds or not z.cells:
                continue
            if z.area.inside(*xy):
                d = z.area.signed_distance(*xy)
                if d > bd:
                    best, bd = z, d
        return best

    def crossings(self, z: ZoneInfo) -> list:
        return self.truth.zone_crossings(z.cells, z.margin)

    def night_p6(self, night: int) -> dict:
        n = next((x for x in self.nights if x["night"] == night), None)
        return (n or {}).get("p6") or {}


def _eligible(view: RunView, z: ZoneInfo, c: dict, others: list) -> bool:
    """The zone is empty for crossing ``c``: no other scripted person inside around it, none vanished inside within the delay."""
    hold = view.delay_at(z.id, c["t_in"]) * 1000
    for o in others:
        if o["person"] == c["person"]:
            continue
        if o["t_in"] <= c["t_out"] + 500 and o["t_out"] >= c["t_in"] - 500:
            return False
        if o["dies_inside"] and c["t_in"] - hold - 5000 <= o["t_out"] <= c["t_in"]:
            return False
    return True


def _alone(view: RunView, z: ZoneInfo, c: dict, others: list) -> bool:
    """No other scripted person is inside the zone while ``c`` is (the leave is then caused by ``c`` alone)."""
    for o in others:
        if o["person"] == c["person"]:
            continue
        if o["t_in"] <= c["t_out"] + 500 and o["t_out"] >= c["t_in"] - 500:
            return False
        if o["dies_inside"] and 0 <= c["t_in"] - o["t_out"] <= view.delay_at(z.id, c["t_in"]) * 1000 + 5000:
            return False
    return True


# ---------------------------------------------------------------- P6-1, P6-2, P6-15 (the walkers)
def _edge_run(res: dict) -> dict:
    """The per-run facts of P6-1, P6-2 and P6-15: probes with their lags, per zone kind and walker speed."""
    view = RunView(res)
    noise = view.noise
    enters, leaves, passes, flicker = [], [], [], []
    zones_used = []
    for z in view.zones.values():
        if z.kind not in EVALUATED_KINDS or not z.cells:
            continue
        cr = view.crossings(z)
        walkers = [c for c in cr if c["kind"] == "walker" and c["t_in"] >= z.created_ms
                   and view.observed_between(c["t0_ms"], c["t1_ms"])]
        zones_used.append({"id": z.id, "kind": z.kind, "cells": len(z.cells), "margin": z.margin})
        for c in walkers:
            v = c["speed"]
            dwell = z.kind in DWELL_KINDS
            empty = _eligible(view, z, c, cr)
            tol = max(EARLY_TOL_MIN_S, 4.0 * noise / max(v, 0.1)) * 1000.0
            if counted_enter(c):
                if dwell and v >= 1.0:
                    if empty:
                        passes.append(_pass_probe(view, z, c))
                    continue
                if not empty:
                    continue
                enters.append(_enter_probe(view, z, c, tol))
            if counted_leave(c) and _alone(view, z, c, cr):
                lv = _leave_probe(view, z, c, tol, dwell)
                if lv is not None:
                    leaves.append(lv)
    # the edge sweep: leave/enter pairs against the signed distance of each stayer to the area edge
    sweepers = [p for p in view.people if p["kind"] == "sweep"]
    secs = 0
    for p in sweepers:
        seg = p["tracks"][0][0]
        t0, t1 = seg[0] * 1000, seg[3] * 1000
        best = None
        for z in view.zones.values():
            if z.kind not in EVALUATED_KINDS or not z.cells or t0 < z.created_ms:
                continue
            d = z.area.signed_distance(seg[1], seg[2])
            if best is None or d > best[0]:
                best = (d, z)
        if best is None or best[0] < -0.7 or not view.observed_between(t0, t1):
            continue
        d, z = best
        lv = [e for e in view.events_of(z.id, "leave") if t0 + 1500 <= e["sim_ms"] <= t1 + 500 and e["via"] == "edge"]
        en = [e for e in view.events_of(z.id, "enter") if t0 + 1500 <= e["sim_ms"] <= t1 + 500]
        flicker.append({"zone": z.id, "dist_cm": int(round(d * 100)), "leaves": len(lv), "enters": len(en),
                        "secs": int((t1 - t0) / 1000)})
        secs += int((t1 - t0) / 1000)
    return {"noise": noise, "enters": enters, "leaves": leaves, "passes": passes, "flicker": flicker, "zones": zones_used,
            "sweep_secs": secs}


def _enter_probe(view, z, c, tol_ms) -> dict:
    t_in = c["t_in"]
    ev = view.first_event(z.id, "enter", t_in - 2000, t_in + 5000)
    out = {"zone": z.id, "kind": z.kind, "speed": c["speed"], "t_in": t_in, "person": c["person"], "event": ev is not None}
    if ev is None:
        out.update(ok=False, why="no enter")
        return out
    k = ev.get("k") or 0
    frame = (ev["sim_ms"] - t_in) / 1000.0
    batch = (ev["recv_ms"] - ev["sim_ms"]) / 1000.0 if ev.get("recv_ms") is not None else None
    arrival = (ev["recv_ms"] - t_in) / 1000.0 if ev.get("recv_ms") is not None else None
    early = ev["sim_ms"] < t_in - tol_ms
    ok = frame <= ENTER_FRAME_S + 0.1 * k and (batch is None or batch <= ENTER_BATCH_S) and not early
    out.update(ok=ok, k=k, frame=round(frame, 3), batch=round(batch, 3) if batch is not None else None,
               arrival=round(arrival, 3) if arrival is not None else None, early=early,
               why=None if ok else ("early" if early else ("frame lag" if frame > ENTER_FRAME_S + 0.1 * k else "batch lag")))
    return out


def _leave_probe(view, z, c, tol_ms, dwell) -> Optional[dict]:
    """A leave probe: the leave that matches the exit crossing is the first one in [t_out - tol, t_out + 3 s]. A leave before it that
    is followed by an enter before the crossing is a flicker of the entry edge (noise puts one detection outside the area right
    after the enter: reported, ``flicker_leaves``); a leave before it with no enter after it is an early leave (a fail)."""
    t_out = c["t_out"]
    en = view.first_event(z.id, "enter", c["t_in"] - 2000, t_out + 1000)
    if en is None:
        return None                              # the walker was not entered (a pass): not a leave probe
    ev = view.first_event(z.id, "leave", t_out - tol_ms, t_out + 3000)
    evs = view.events_of(z.id)
    earlier = [e for e in evs if e["kind"] == "leave" and en["sim_ms"] - 500 <= e["sim_ms"] < t_out - tol_ms]
    flicker = early = 0
    for e in earlier:
        if any(x["kind"] == "enter" and e["sim_ms"] < x["sim_ms"] <= t_out for x in evs):
            flicker += 1
        else:
            early += 1
    out = {"zone": z.id, "kind": z.kind, "speed": c["speed"], "t_out": t_out, "person": c["person"], "event": ev is not None,
           "flicker_leaves": flicker}
    if ev is None:
        out.update(ok=False, why="early" if early else "no leave", early=bool(early))
        return out
    frame = (ev["sim_ms"] - t_out) / 1000.0
    arrival = (ev["recv_ms"] - t_out) / 1000.0 if ev.get("recv_ms") is not None else None
    ok = frame <= LEAVE_FRAME_S and ev["via"] == "edge" and not early
    out.update(ok=ok, frame=round(frame, 3), arrival=round(arrival, 3) if arrival is not None else None, via=ev["via"],
               early=bool(early), why=None if ok else ("early" if early else ("via " + ev["via"] if ev["via"] != "edge" else "frame lag")))
    return out


def _pass_probe(view, z, c) -> dict:
    """A walker at 1.0 or 1.2 m/s through a dwell, bed or manual zone: no enter, no count above 0, no event."""
    a, b = c["t_in"] - 500, c["t_out"] + 2500
    evs = [e for e in view.events_of(z.id) if a <= e["sim_ms"] <= b]
    counted = False
    base = view.zone_row(z.id, a)
    if base is not None and base[2] > 0:
        counted = True
    for t, row in zip(view.zt.get(z.id, []), view.zr.get(z.id, [])):
        if a <= t <= b and row[2] > 0:
            counted = True
    return {"zone": z.id, "kind": z.kind, "speed": c["speed"], "t_in": c["t_in"], "events": len(evs), "counted": counted,
            "ok": not evs and not counted}


def crit_edge(results: list) -> dict:
    """P6-1, P6-2 and P6-15 from the edge jobs."""
    runs = _runs(results, *EDGE_SCRIPTS)
    per = []
    for r in runs:
        if not _ok(r):
            per.append((r, None))
            continue
        per.append((r, _edge_run(r)))
    out = {}
    for pid, title in (("P6-1", "A person entering an empty zone gives an enter within 0.8 s (frame time) and 0.57 s of batching"),
                       ("P6-2", "A person leaving a zone gives a leave at the area edge within 0.5 s (frame time)"),
                       ("P6-15", "Walkers at 1.0 and 1.2 m/s through a dwell zone give no enter, no count and no event")):
        by_noise = defaultdict(list)
        for r, d in per:
            if d is None:
                by_noise[(r.get("job_spec") or {}).get("noise_m", 0.02)].append(_row(r, INCOMPLETE, note="the run did not finish"))
                continue
            key = "enters" if pid == "P6-1" else ("leaves" if pid == "P6-2" else "passes")
            probes = d[key]
            gated = probes if pid == "P6-15" else [p for p in probes if p.get("speed") != 0.5]
            bad = [p for p in gated if not p["ok"]]
            if bad:
                st, note = FAIL, "%d of %d probes failed: %s" % (len(bad), len(gated), sorted({p.get("why") or "no" for p in bad}))
            elif len(gated) < MIN_PROBES:
                st, note = INCOMPLETE, "only %d counted crossings exercised (need %d)" % (len(gated), MIN_PROBES)
            else:
                st, note = PASS, None
            row = _row(r, st, note=note, probes=len(gated), failed=len(bad), zones=d["zones"],
                       first_failures=bad[:3])
            if pid == "P6-1":
                row["lag_frame"] = _stats([p.get("frame") for p in gated if p.get("frame") is not None])
                row["lag_batch"] = _stats([p.get("batch") for p in gated if p.get("batch") is not None])
                row["lag_arrival"] = _stats([p.get("arrival") for p in gated if p.get("arrival") is not None])
                row["k"] = _stats([p.get("k") for p in gated if p.get("k") is not None])
                row["arrival_over_0_84"] = sum(1 for p in gated if p.get("arrival") is not None and p["arrival"] > ENTER_ARRIVAL_S)
                row["by_speed"] = _by_speed(gated)
            elif pid == "P6-2":
                row["lag_frame"] = _stats([p.get("frame") for p in gated if p.get("frame") is not None])
                row["lag_arrival_reported"] = _stats([p.get("arrival") for p in gated if p.get("arrival") is not None])
                row["by_speed"] = _by_speed(gated)
            by_noise[d["noise"]].append(row)
        groups = {}
        gated_status = []
        for nz in sorted(by_noise):
            st, k, n = _kofn(by_noise[nz])
            groups[str(nz)] = {"status": st, "seeds": by_noise[nz], "counts": "%d of %d seeds PASS (need %d)" % (k, n, _need(n))}
            if nz in GATED_NOISE:
                gated_status.append(st)
        missing = [nz for nz in GATED_NOISE if nz not in by_noise]
        status = _combine(gated_status + ([INCOMPLETE] if missing else []))
        det = {"by_noise": groups, "missing_noise_levels": missing}
        if pid == "P6-2":
            det["flicker_reported"] = _flicker_table(per)
        if pid == "P6-15":
            det["reported_07_delayed"] = _rep_07(per)
        out[pid] = {"id": pid, "gated": True, "status": status, "title": title, "details": det}
    return out


def _by_speed(probes: list) -> dict:
    d = defaultdict(list)
    for p in probes:
        d["%s/%s" % (p["kind"], p["speed"])].append(p)
    return {k: {"n": len(v), "failed": sum(1 for x in v if not x["ok"])} for k, v in sorted(d.items())}


def _flicker_table(per: list) -> dict:
    """Leave/enter pairs per hour against the signed distance of the sweep stayer to the area edge, per noise level."""
    out = {}
    for r, d in per:
        if d is None:
            continue
        bins = defaultdict(lambda: [0, 0])
        for f in d["flicker"]:
            b = int(round(f["dist_cm"] / 5.0) * 5)
            bins[b][0] += min(f["leaves"], f["enters"])
            bins[b][1] += f["secs"]
        tab = out.setdefault(str(d["noise"]), {})
        for b, (pairs, secs) in sorted(bins.items()):
            cell = tab.setdefault(str(b), [0, 0])
            cell[0] += pairs
            cell[1] += secs
    return {nz: {b: {"pairs": c[0], "hours": round(c[1] / 3600.0, 3),
                     "pairs_per_hour": round(c[0] / (c[1] / 3600.0), 1) if c[1] else None} for b, c in tab.items()}
            for nz, tab in out.items()}


def _rep_07(per: list) -> dict:
    """Reported: 0.7 m/s walkers through dwell zones whose enter was prevented or delayed by speed-excluded frames (k > 0)."""
    out = {}
    for r, d in per:
        if d is None:
            continue
        pr = [p for p in d["enters"] if p["speed"] == 0.7 and p["kind"] in DWELL_KINDS]
        s = out.setdefault(str(d["noise"]), {"n": 0, "no_enter": 0, "k_positive": 0})
        s["n"] += len(pr)
        s["no_enter"] += sum(1 for p in pr if not p["event"])
        s["k_positive"] += sum(1 for p in pr if (p.get("k") or 0) > 0)
    return out


# ---------------------------------------------------------------- P6-3 (hold)
def _spell_judgements(view: RunView, spots_kinds=("stayer",)) -> list:
    """One judgement per scripted hidden spell (job p6hold): class, expected window, what the learner did."""
    out = []
    for p in view.people:
        if p["kind"] != "stayer" or not p.get("spells") or p.get("note") in ("gap_hidden",):
            continue
        spot = p.get("spot")
        xy = J.P6_SPOTS.get(spot)
        z = view.zone_for(xy) if xy else None
        for s in p["spells"]:
            t_last = s["t"] * 1000.0 + 900.0                       # the last detection of the visible stretch
            rec = {"person": p["id"], "spot": spot, "zone": z.id if z else None, "last_det": t_last, "g": s["g"], "ret": s["ret"],
                   "unseen": bool(s.get("unseen"))}
            if z is None or t_last < z.created_ms or not view.observed_between(t_last - 5000, t_last + 5000):
                rec["class"] = "not_judged"
                out.append(rec)
                continue
            d = view.delay_at(z.id, t_last, J.P6_DEFAULT_DELAY_S.get(z.kind, 300))
            if view.t_end is not None and t_last + (d + 5) * 1000 > view.t_end:
                rec["class"] = "not_judged"                         # the run ended before the hold could
                out.append(rec)
                continue
            rec["delay"] = d
            g = s["g"]
            if g is None:
                rec["class"] = "none"
            elif g < d - BORDER_S:
                rec["class"] = "hold_return"
            elif g <= d + BORDER_S:
                rec["class"] = "borderline"                      # the return and the deadline are within a second of each other
            else:
                rec["class"] = "late" if g <= 2 * d else "long"
            out.append(rec)
    return out


def _judge_hold(view: RunView, j: dict) -> dict:
    z, t_last, d = j["zone"], j["last_det"], j["delay"]
    g = j["g"]
    ldr_now = bool((view.zone_row(z, t_last) or [0] * 9)[5])
    if j["class"] == "borderline":
        j["ok"], j["why"] = True, None
        return j
    if j["class"] == "hold_return":
        end = t_last + g * 1000.0
        leave = [e for e in view.events_of(z, "leave") if t_last < e["emit_ms"] <= end]
        if leave and ldr_now and all(e["via"] == "early" for e in leave):
            j["ok"], j["why"], j["early_ldr"] = True, None, True    # an early clear of an ld2412_reliable zone: P6-9 judges it
            return j
        row = view.zone_row(z, end - 500)
        mid = view.zone_row(z, t_last + min(g * 1000.0 - 1000.0, 4000.0))
        held_count = mid[2] if mid else None
        j["ok"] = not leave and bool(row and row[1]) and (held_count in (0, None))
        j["why"] = None if j["ok"] else ("leave event" if leave else ("not occupied" if not (row and row[1]) else "count %s while held" % held_count))
        return j
    lo, hi = t_last + d * 1000.0 - 300.0, t_last + d * 1000.0 + HOLD_EMIT_S * 1000.0
    ev = None
    for e in view.events_of(z, "leave"):
        if e["emit_ms"] >= t_last - 1000.0:
            ev = e
            break
    if ev is None:
        j["ok"], j["why"] = False, "no leave"
        return j
    j["emit_lag_s"] = round((ev["emit_ms"] - (t_last + d * 1000.0)) / 1000.0, 3)
    j["via"] = ev["via"]
    if ev["via"] == "early":
        # allowed only for an ld2412_reliable zone and not before the 60 s of LD2412 absence (P6-9 judges the rate)
        early_ok = ldr_now and ev["emit_ms"] - t_last >= EARLY_ABSENT_S * 1000 - 300
        j["ok"], j["why"], j["early_ldr"] = early_ok, None if early_ok else "via early without ldr or before 60 s", True
    elif ev["via"] != "hold_expired":
        j["ok"], j["why"] = False, "via " + ev["via"]
    elif ev["emit_ms"] < lo:
        j["ok"], j["why"] = False, "emitted before the deadline"
    elif ev["emit_ms"] > hi:
        j["ok"], j["why"] = False, "emitted %.1f s late" % ((ev["emit_ms"] - hi) / 1000.0 + HOLD_EMIT_S)
    else:
        j["ok"], j["why"] = True, None
    return j


def crit_p63(results: list) -> dict:
    rows = []
    for r in _runs(results, "p6hold"):
        if not _ok(r):
            rows.append(_row(r, INCOMPLETE, note="the run did not finish"))
            continue
        view = RunView(r)
        js = [_judge_hold(view, j) for j in _spell_judgements(view) if j["class"] != "not_judged"]
        by = defaultdict(list)
        for j in js:
            by[j["class"]].append(j)
        bad = [j for j in js if not j["ok"]]
        counts = {k: len(v) for k, v in by.items()}
        need = ("hold_return", "none")
        late_n = counts.get("late", 0) + counts.get("long", 0)
        early_n = sum(1 for j in js if j.get("early_ldr"))
        if bad:
            st, note = FAIL, "%d of %d spells failed: %s" % (len(bad), len(js), sorted({j["why"] for j in bad}))
        elif any(counts.get(c, 0) < SPELL_MIN_CLASS for c in need) or late_n < SPELL_MIN_CLASS:
            st, note = INCOMPLETE, "a class has fewer than %d classified spells: %s" % (SPELL_MIN_CLASS, counts)
        else:
            st, note = PASS, None
        rows.append(_row(r, st, note=note, classes=counts, early_clears_in_ldr_zones=early_n, failed=bad[:4],
                         emit_lag_s=_stats([j.get("emit_lag_s") for j in js if j.get("emit_lag_s") is not None])))
    st, k, n = _kofn(rows)
    return {"id": "P6-3", "gated": True, "status": st, "title": "A target vanishing inside a zone is held until the clear delay",
            "details": {"seeds": rows}, "counts": {"seeds": "%d of %d seeds PASS (need %d)" % (k, n, _need(n))}}


# ---------------------------------------------------------------- P6-4 and P6-5 (power)
def _power_windows(view: RunView) -> list:
    tl = view.timeline
    obs = tl.get("observe_ms", OBSERVE_MS)
    out = []
    for a, b in tl.get("offs", []):
        out.append(("off", a - 1000, b + obs + POWERUP_BIRTH_MS))
    ep = tl.get("epochs") or []
    if tl.get("reboot") is not None and len(ep) > 1:
        out.append(("reboot", tl["reboot"] - 1000, ep[1]["start"] + obs + POWERUP_BIRTH_MS))
    if tl.get("loss"):
        out.append(("loss", tl["loss"][0] - 1000, tl["loss"][1] + POWERUP_BIRTH_MS + 1000))
    for a, b, cause, n in view.p6.get("spans", []):
        if a is None:
            continue
        # a short run of dropped messages is a GAP only inside observed time (next to a power transition it belongs to that window)
        if b - a < GAP_POWER_MS and view.observed_between(a - 5000, b + 5000):
            out.append(("gap", a, b + 1500))
    return out


def _explained(view: RunView, ev: dict) -> bool:
    z = view.zones.get(ev["zone"])
    if z is None:
        return False
    for c in view.crossings(z):
        for t in (c["t_in"], c["t_out"]):
            if abs(t - ev["sim_ms"]) <= 1000:
                return True
    return False


def crit_p64(results: list) -> dict:
    rows = []
    for r in _runs(results, "p6pow"):
        if not _ok(r):
            rows.append(_row(r, INCOMPLETE, note="the run did not finish"))
            continue
        view = RunView(r)
        wins = _power_windows(view)
        bad = []
        explained = 0
        for e in view.events:
            for kind, a, b in wins:
                if a <= e["sim_ms"] <= b:
                    if _explained(view, e):
                        explained += 1
                    else:
                        bad.append({"window": kind, "zone": e["zone"], "kind": e["kind"], "via": e["via"],
                                    "sim_ms": e["sim_ms"], "window_ms": [a, b]})
                    break
        # a GAP must not make the zones invalid
        gap_invalid = 0
        for kind, a, b in wins:
            if kind == "gap":
                gap_invalid += sum(1 for t, v in zip(view.vt, view.vf) if a <= t <= b and not v)
        # the resume with a stayer already inside: occ = true, no event before it
        resumes, resume_bad = 0, []
        obs_ms = view.timeline.get("observe_ms", OBSERVE_MS)
        for a, b in view.timeline.get("offs", []):
            t_probe = b + obs_ms + POWERUP_BIRTH_MS + 2000
            if view.t_end is not None and t_probe + 5000 > view.t_end:
                continue                                    # the run ended before this resume
            for p in view.people:
                if p.get("note") != "overnight":
                    continue
                xy = J.P6_SPOTS.get(p.get("spot"))
                z = view.zone_for(xy) if xy else None
                seg = p["tracks"][0][0]
                if z is None or not (seg[0] * 1000 <= a and seg[3] * 1000 >= t_probe) or z.created_ms > a:
                    continue
                k = bisect_right(view.vt, b)
                first = next(((t, st) for t, st in zip(view.vt[k:], view.states[k:]) if st[3]), None)
                if first is None:
                    resume_bad.append({"zone": z.id, "off_end": b, "why": "no valid state after the resume"})
                    resumes += 1
                    continue
                t_first, st = first
                resumes += 1
                row = view.zone_row(z.id, t_first)
                evs = [e for e in view.events_of(z.id) if b < e["emit_ms"] <= t_first + 1]
                if not (row and row[1]) or evs:
                    resume_bad.append({"zone": z.id, "off_end": b, "occ": bool(row and row[1]), "events": len(evs)})
        # a hold deadline that falls into a gap moves by the gap
        gap_hold, gap_hold_bad = 0, []
        for p in view.people:
            if p.get("note") != "gap_hidden":
                continue
            xy = J.P6_SPOTS.get(p.get("spot"))
            z = view.zone_for(xy) if xy else None
            if z is None:
                continue
            t_last = p["spells"][0]["t"] * 1000.0 + 900.0
            d = view.delay_at(z.id, t_last, 60)
            gaps = [(a, b) for a, b, c, n in view.p6.get("spans", []) if a is not None and t_last <= a <= t_last + d * 1000.0 and "gap" in c]
            if not gaps:
                continue
            gap_total = sum(min(g[1], t_last + d * 1000.0 + 60000) - g[0] for g in gaps)
            ev = next((e for e in view.events_of(z.id, "leave") if e["emit_ms"] >= t_last), None)
            gap_hold += 1
            lo = t_last + d * 1000.0 + J.P6_GAP_S * 1000.0 - 300.0
            if ev is None or ev["emit_ms"] < lo:
                gap_hold_bad.append({"zone": z.id, "t_last": t_last, "emit": ev["emit_ms"] if ev else None, "lower_bound": lo,
                                     "gap_ms": gap_total})
        n_gap_spans = sum(1 for w in wins if w[0] == "gap")
        problems = []
        if bad:
            problems.append("%d events in power or gap windows" % len(bad))
        if gap_invalid:
            problems.append("%d state emissions with valid=False inside a gap" % gap_invalid)
        if resume_bad:
            problems.append("%d resumes with a stayer inside were not silent" % len(resume_bad))
        if gap_hold_bad:
            problems.append("%d holds expired inside a gap" % len(gap_hold_bad))
        if problems:
            st = FAIL
        elif resumes == 0 or n_gap_spans == 0:
            st = INCOMPLETE
            problems.append("not exercised: %d resumes with a stayer, %d gap spans" % (resumes, n_gap_spans))
        else:
            st = PASS
        rows.append(_row(r, st, note="; ".join(problems) or None, windows=len(wins), gap_spans=n_gap_spans,
                         events_in_windows=len(bad), explained=explained, bad=bad[:5], resumes=resumes, resume_bad=resume_bad[:3],
                         gap_holds=gap_hold, gap_hold_bad=gap_hold_bad[:3], valid_false_in_gap=gap_invalid))
    st, k, n = _all(rows)
    return {"id": "P6-4", "gated": True, "status": st, "title": "No events on radar power transitions, resumes and gaps",
            "details": {"seeds": rows}, "counts": {"seeds": "%d of %d seeds PASS (all needed)" % (k, n)}}


def crit_p65(results: list) -> dict:
    rows = []
    for r in _runs(results, "p6pow"):
        if not _ok(r):
            rows.append(_row(r, INCOMPLETE, note="the run did not finish"))
            continue
        view = RunView(r)
        tl = view.timeline
        obs = tl.get("observe_ms", OBSERVE_MS)
        first_valid = next((t for t, v in zip(view.vt, view.vf) if v), None)
        wins = []
        for a, b in tl.get("offs", []):
            wins.append(("off", a + 1000, b + obs, b + obs))
        ep = tl.get("epochs") or []
        if tl.get("reboot") is not None and len(ep) > 1:
            wins.append(("reboot", tl["reboot"] + 16000, ep[1]["start"] + obs, ep[1]["start"] + obs))
        if tl.get("loss"):
            wins.append(("loss", tl["loss"][0] + 16000, tl["loss"][1], tl["loss"][1]))
        judged, bad, lags = 0, [], []
        for kind, a, b, obs_t in wins:
            if first_valid is None or a < first_valid:
                continue
            judged += 1
            # valid=True anywhere in [a, b) is a violation
            viol = None
            k = bisect_right(view.vt, a) - 1
            if k >= 0 and view.vf[k]:
                viol = a
            for t, v in zip(view.vt[max(k, 0) + 1:], view.vf[max(k, 0) + 1:]):
                if t >= b:
                    break
                if v and viol is None:
                    viol = t
            if viol is not None:
                bad.append({"window": kind, "start": a, "end": b, "valid_at": viol})
            nxt = next((t for t, v in zip(view.vt, view.vf) if v and t >= b), None)
            if nxt is not None:
                lags.append((nxt - obs_t) / 1000.0)
        if bad:
            st, note = FAIL, "%d of %d windows had valid=True while the radars were off" % (len(bad), judged)
        elif judged == 0:
            st, note = INCOMPLETE, "no window after the first ZoneStates (no zone exists yet)"
        else:
            st, note = PASS, None
        rows.append(_row(r, st, note=note, windows_judged=judged, bad=bad[:4], resume_lag_s=_stats(lags)))
    st, k, n = _all(rows)
    return {"id": "P6-5", "gated": True, "status": st,
            "title": "Zones are unavailable (valid False) while the radars are off (part a: the core's valid flag; part b is a unit test)",
            "details": {"seeds": rows}, "counts": {"seeds": "%d of %d seeds PASS (all needed)" % (k, n)}}


# ---------------------------------------------------------------- P6-6 (rows)
def rows_model(view: RunView, zid: str) -> dict:
    """{day: {episodes, occ_rows, event_rows, count_rows, cycles, default, all}} for one zone."""
    days = defaultdict(lambda: {"episodes": 0, "occ_rows": 0, "event_rows": 0, "count_rows": 0, "cycles": 0})
    prev_occ, prev_count = None, None
    for t, row in zip(view.zt.get(zid, []), view.zr.get(zid, [])):
        d = _local_ms(t)[0]
        occ, count = row[1], row[2]
        if prev_occ is None or occ != prev_occ:
            if prev_occ is not None or occ:
                days[d]["occ_rows"] += 1
            if occ and not prev_occ:
                days[d]["episodes"] += 1
        if prev_count is None or count != prev_count:
            days[d]["count_rows"] += 1
        prev_occ, prev_count = occ, count
    for e in view.events_of(zid):
        days[_local_ms(e["emit_ms"])[0]]["event_rows"] += 1
    pv = None
    for t, v in zip(view.vt, view.vf):
        if pv is True and v is False:
            days[_local_ms(t)[0]]["cycles"] += 1
        pv = v
    for d, x in days.items():
        x["default"] = x["occ_rows"] + x["event_rows"] + 4 * x["cycles"] + 8
        x["all"] = x["occ_rows"] + x["event_rows"] + x["count_rows"] + 18 * x["cycles"] + 20 + 14
    return days


ROWS_SCRIPTS = ("p6pow", "p6guests", "p6guestsc")             # gated: p6pow (the spec's run, few episodes) and the busy visits of the pair
ROWS_REPORTED = ("p6edge", "p6edge05", "p6edge08")            # reported only: the edge sweep flickers on purpose (hundreds of episodes)


def _rows_row(r: dict) -> dict:
    if not _ok(r):
        return _row(r, INCOMPLETE, note="the run did not finish")
    view = RunView(r)
    mx_def = mx_all = 0
    mx_ep = 0
    over = 0
    zdays = 0
    for zid, z in view.zones.items():
        if z.kind not in EVALUATED_KINDS:
            continue
        for d, x in rows_model(view, zid).items():
            zdays += 1
            mx_def, mx_all, mx_ep = max(mx_def, x["default"]), max(mx_all, x["all"]), max(mx_ep, x["episodes"])
            over += 1 if x["episodes"] > EPISODE_BUDGET else 0
    if mx_def >= ROWS_LIMIT or mx_all >= ROWS_LIMIT:
        st, note = FAIL, "rows per zone and day: default %d, all recorded %d (limit %d)" % (mx_def, mx_all, ROWS_LIMIT)
    elif mx_ep <= ROWS_EPISODES_MIN:
        st, note = INCOMPLETE, "no zone-day has more than %d episodes (the most: %d): the budget is untested" % (ROWS_EPISODES_MIN, mx_ep)
    else:
        st, note = PASS, None
    return _row(r, st, note=note, zone_days=zdays, max_rows_default=mx_def, max_rows_all=mx_all, max_episodes_day=mx_ep,
                zone_days_over_episode_budget=over)


def crit_p66(results: list) -> dict:
    rows = [_rows_row(r) for r in _runs(results, *ROWS_SCRIPTS)]
    reported = [_rows_row(r) for r in _runs(results, *ROWS_REPORTED)]
    sts = [x["status"] for x in rows]
    # FAIL when a gated run is over the limit; PASS when at least one run had zone-days busy enough to test the budget and none failed
    st = FAIL if FAIL in sts else (PASS if PASS in sts else INCOMPLETE)
    return {"id": "P6-6", "gated": True, "status": st,
            "title": "Recorder rows per zone per day < 500 (a proxy on the recorded outputs, conditional on the episode rate; part ii is a "
                     "unit test)", "details": {"seeds": rows, "reported_edge_flicker": reported},
            "counts": {"runs": "%d of %d gated runs PASS, %d FAIL (the busy runs test the budget; p6pow has too few episodes)" % (
                sts.count(PASS), len(sts), sts.count(FAIL))}}


# ---------------------------------------------------------------- P6-7 (learned delays)
def _night_rows(view: RunView) -> list:
    return [(n["night"], n.get("p6") or {}) for n in view.nights]


def crit_p67(results: list) -> dict:
    rows = []
    for r in _runs(results, "p6hold"):
        if not _ok(r):
            rows.append(_row(r, INCOMPLETE, note="the run did not finish"))
            continue
        view = RunView(r)
        problems, notes = [], {}
        learned_zones = 0
        series = defaultdict(list)
        spell_js = {}
        for j in _spell_judgements(view):
            if j["zone"]:
                spell_js.setdefault(j["zone"], []).append(j)
        for night, rec in _night_rows(view):
            z6 = (rec.get("zone6") or {}).get("delay") or {}
            rows_now = {x[0]: x for x in ((rec.get("zone_states") or {}).get("rows") or [])}
            for zid, dd in z6.items():
                sp, cd = dd.get("sp") or [], dd.get("cd")
                z = view.zones.get(zid)
                st_row = rows_now.get(zid)
                series[zid].append([night, len(sp), cd, st_row[6] if st_row else None, st_row[7] if st_row else None])
                if len(sp) >= 10:
                    want = max(5, min(600, _nearest_rank(sp)))
                    if cd != want:
                        problems.append("night %s %s: learned %s != nearest-rank q95 %s of %d spells" % (night, zid, cd, want, len(sp)))
                    elif st_row and st_row[7] == "learned" and st_row[6] != want:
                        problems.append("night %s %s: delay_s %s != learned %s" % (night, zid, st_row[6], want))
                    if cd is not None and not 5 <= cd <= 600:
                        problems.append("night %s %s: learned %s outside 5..600" % (night, zid, cd))
                    if cd == want:
                        learned_zones += 1
                elif cd is not None:
                    problems.append("night %s %s: a learned delay with only %d spells" % (night, zid, len(sp)))
                elif st_row and z is not None and st_row[7] != "default":
                    problems.append("night %s %s: delay_src %s with %d spells" % (night, zid, st_row[7], len(sp)))
        # the delay in force changes only at the nightly run
        late_changes = []
        for zid in view.zones:
            prev = None
            for t, row in zip(view.zt.get(zid, []), view.zr.get(zid, [])):
                if prev is not None and row[6] != prev:
                    ms = _local_ms(t)[1]
                    if not NIGHTLY_SLOT_MS[0] <= ms <= NIGHTLY_SLOT_MS[1]:
                        late_changes.append({"zone": zid, "t": t, "from": prev, "to": row[6]})
                prev = row[6]
        if late_changes:
            problems.append("%d changes of delay_s outside the nightly run" % len(late_changes))
        # the captured spells against the scripted spells that satisfy the recording rule (reported; gated when the sizes match)
        cmp = {}
        last = _night_rows(view)[-1][1] if view.nights else {}
        for zid, dd in ((last.get("zone6") or {}).get("delay") or {}).items():
            sp = dd.get("sp") or []
            js = [j for j in spell_js.get(zid, []) if j["class"] in ("hold_return", "late") and j.get("g") is not None]
            scripted = [j["g"] for j in js]
            if len(sp) >= 10 and dd.get("cd") is not None:
                q = _nearest_rank(scripted) if scripted else None
                cmp[zid] = {"captured": len(sp), "scripted_recorded": len(scripted), "learned": dd.get("cd"), "q95_scripted": q}
                if q is not None and len(sp) == len(scripted) and abs(max(5, min(600, _nearest_rank(sp))) - max(5, min(600, q))) > 3:
                    problems.append("%s: learned %s differs from the scripted q95 %s by more than 3 s" % (zid, dd.get("cd"), q))
            if sp:
                dmax = max([x[3] for x in series.get(zid, []) if x[3]] or [300])
                if max(sp) > 2 * dmax + 2:
                    problems.append("%s: a captured spell of %d s is longer than twice the delay in force" % (zid, max(sp)))
        if problems:
            st, note = FAIL, "; ".join(problems[:3])
        elif learned_zones == 0:
            st, note = INCOMPLETE, "no zone reached 10 captured spells and a learned delay"
        else:
            st, note = PASS, None
        rows.append(_row(r, st, note=note, learned_zone_nights=learned_zones, series={z: s[-6:] for z, s in series.items()},
                         compare=cmp, late_changes=late_changes[:3], problems=problems[:8]))
    st, k, n = _kofn(rows)
    return {"id": "P6-7", "gated": True, "status": st, "title": "Learned clear delays: the mechanism (nearest-rank p95, bounds, only at the "
            "nightly run)", "details": {"seeds": rows, "reported": _p67_reported(results)},
            "counts": {"seeds": "%d of %d seeds PASS (need %d)" % (k, n, _need(n))}}


def _p67_reported(results: list) -> list:
    out = []
    for r in _runs(results, "p6hold"):
        if not _ok(r):
            continue
        view = RunView(r)
        js = [j for j in _spell_judgements(view) if j["class"] != "not_judged" and j.get("g") is not None]
        scripted = [j["g"] for j in js]
        final = {}
        if view.nights:
            last = (view.nights[-1].get("p6") or {}).get("zone6", {}).get("delay") or {}
            final = {z: d.get("cd") for z, d in last.items()}
        prem_before = prem_after = n_before = n_after = 0
        for j in js:
            if j["class"] == "not_judged" or not j["ret"]:
                continue
            row = view.zone_row(j["zone"], j["last_det"])
            learned = bool(row and row[7] == "learned")
            leave = [e for e in view.events_of(j["zone"], "leave") if j["last_det"] < e["emit_ms"] <= j["last_det"] + j["g"] * 1000.0]
            if learned:
                n_after += 1
                prem_after += 1 if leave else 0
            else:
                n_before += 1
                prem_before += 1 if leave else 0
        share = None
        if final:
            lv = [v for v in final.values() if v]
            if lv and scripted:
                share = round(sum(1 for g in scripted if g <= max(lv)) / len(scripted), 3)
        out.append({"seed": r["seed"], "learned": final, "q95_scripted_table": _nearest_rank(scripted),
                    "capture_share": share, "capture_warn": bool(share is not None and share < 0.90),
                    "premature_leave_share_before": round(prem_before / n_before, 3) if n_before else None,
                    "premature_leave_share_after": round(prem_after / n_after, 3) if n_after else None})
    return out


# ---------------------------------------------------------------- P6-8 (door)
def crit_p68(results: list) -> dict:
    rows = []
    d2 = J.DOOR_SITE
    for r in _runs(results, "p6door"):
        if not _ok(r):
            rows.append(_row(r, INCOMPLETE, note="the run did not finish"))
            continue
        view = RunView(r)
        z = view.zone_for(J.DOOR_PROBE)
        door_nights = {}
        for night, rec in _night_rows(view):
            near = [e for e in rec.get("entry_zones") or []
                    if e["state"] == "shadow" and math.hypot(e["cx"] - d2[0], e["cy"] - d2[1]) <= 0.4 + 1.0]
            door_nights[night] = bool(near)
        if z is None:
            rows.append(_row(r, INCOMPLETE, note="no zone by the scripted door (the label press did not succeed)", door_nights=door_nights))
            continue
        first_door = min((n for n, v in door_nights.items() if v), default=None)

        def door_at(day: int) -> bool:
            ns = [n for n in door_nights if n <= day]
            return bool(ns) and door_nights[max(ns)]

        desk = view.zone_for(J.P6_SPOTS["desk"])
        probes_door, probes_nodoor, controls, movers, skipped, bad = 0, 0, 0, 0, 0, []
        for p in view.people:
            if p["kind"] not in ("door_exit", "door_sitter", "mover_vanish"):
                continue
            zz = desk if p["kind"] == "mover_vanish" else z
            if zz is None:
                continue
            t_last = p["spells"][0]["t"] * 1000.0 + 900.0
            day = int(t_last // DAY_MS)
            if t_last < zz.created_ms or not view.observed_between(t_last - 5000, t_last + 5000):
                continue
            door = door_at(day) and p["kind"] != "mover_vanish"   # the door suggestion in the latest nightly record (03:30 of the day)
            occ_now = view.zone_row(zz.id, t_last + 1000)
            if not (occ_now and occ_now[1]):       # the walker was not entered (a short chord or a speed-excluded pass)
                skipped += 1
                continue
            ev = next((e for e in view.events_of(zz.id, "leave") if e["emit_ms"] >= t_last - 500), None)
            d = view.delay_at(zz.id, t_last, 300)
            if p["kind"] == "door_exit" and door:
                probes_door += 1
                ok = ev is not None and ev["via"] == "door_death" and 0 <= ev["emit_ms"] - t_last <= DOOR_OK_S * 1000 \
                    and ev["emit_ms"] - t_last >= FAST_CLEAR_S * 1000 - 300
                if not ok:
                    bad.append({"person": p["id"], "kind": "exit_with_door", "day": day, "via": ev["via"] if ev else None,
                                "lag_s": round((ev["emit_ms"] - t_last) / 1000.0, 2) if ev else None})
            else:
                if p["kind"] == "door_exit":
                    probes_nodoor += 1
                elif p["kind"] == "mover_vanish":
                    movers += 1
                else:
                    controls += 1
                ldr_now = bool((view.zone_row(zz.id, t_last) or [0] * 9)[5])
                ok = ev is not None and ((ev["via"] == "hold_expired" and ev["emit_ms"] - t_last >= d * 1000 - 300)
                                         or (ev["via"] == "early" and ldr_now and ev["emit_ms"] - t_last >= EARLY_ABSENT_S * 1000 - 300))
                if p["kind"] == "mover_vanish":         # a mover's track can split at its end (a second track leaves by the edge):
                    ok = ev is not None and ev["via"] != "door_death"          # the control only asks that no door clears it
                if not ok:
                    bad.append({"person": p["id"], "kind": p["kind"], "door": door, "day": day, "via": ev["via"] if ev else None,
                                "lag_s": round((ev["emit_ms"] - t_last) / 1000.0, 2) if ev else None})
        if bad:
            st, note = FAIL, "%d probes failed" % len(bad)
        elif first_door is None:
            st, note = INCOMPLETE, "precondition: no entry_zone suggestion in state shadow within 1.4 m of the scripted door in any night"
        elif probes_door < 4 or controls < 1 or movers < 1:
            st, note = INCOMPLETE, "exits with a door %d (need 4), sitters %d, movers %d (no door anywhere near)" % (
                probes_door, controls, movers)
        else:
            st, note = PASS, None
        rows.append(_row(r, st, note=note, zone=z.id, zone_kind=z.kind, first_door_night=first_door, door_nights=door_nights,
                         exits_with_door=probes_door, exits_without_door=probes_nodoor, sitters=controls, movers_at_desk=movers,
                         skipped_not_entered=skipped,
                         bad=bad[:5]))
    st, k, n = _kofn(rows)
    return {"id": "P6-8", "gated": True, "status": st, "title": "Door fast-clear: a moving death inside a learned door clears after 3 s",
            "details": {"seeds": rows}, "counts": {"seeds": "%d of %d seeds PASS (need %d)" % (k, n, _need(n))}}


# ---------------------------------------------------------------- P6-9 (ld2412_reliable, early clear)
def _ld_nights(view: RunView, zid: str) -> list:
    out = []
    for night, rec in _night_rows(view):
        z6 = rec.get("zone6") or {}
        ld = (z6.get("ld") or {}).get(zid)
        out.append((night, ld, zid in (z6.get("ldr") or [])))
    return out


def crit_p69(results: list) -> dict:
    rows = []
    hidden_total = false_total = 0
    for r in _runs(results, "p6ld"):
        if not _ok(r):
            rows.append(_row(r, INCOMPLETE, note="the run did not finish"))
            continue
        view = RunView(r)
        z = view.zone_for(J.P6_SPOTS["desk"])
        if z is None:
            rows.append(_row(r, INCOMPLETE, note="no zone at the desk"))
            continue
        problems = []
        first_ldr = None
        known_max = 0
        for night, ld, ldr in _ld_nights(view, z.id):
            on_s, known_s = (ld or [0, 0])[:2] if ld else (0, 0)
            known_max = max(known_max, known_s)
            want = known_s >= LD_MIN_KNOWN_S and known_s > 0 and on_s / known_s >= LD_MIN_SHARE
            if ldr != want:
                problems.append("night %s: ldr %s but known %s s, share %s" % (night, ldr, known_s, round(on_s / known_s, 3) if known_s else None))
            if ldr and first_ldr is None:
                first_ldr = night
        hidden = false_early = 0
        unseen_ldr = unseen_ldr_ok = unseen_ldr_blocked = unseen_non = unseen_non_ok = 0
        unseen_bad = []
        for p in view.people:
            if p["kind"] != "stayer" or p.get("spot") != "desk":
                continue
            for s in p.get("spells") or []:
                t_last = s["t"] * 1000.0 + 900.0
                if t_last < z.created_ms or not view.observed_between(t_last - 5000, t_last + 5000):
                    continue
                if view.t_end is not None and t_last + 600_000 > view.t_end:
                    continue                                        # the run ended inside the hold
                row = view.zone_row(z.id, t_last)
                ldr = bool(row and row[5])
                if s["ret"]:
                    if ldr:
                        hidden += 1
                        end = t_last + s["g"] * 1000.0
                        if any(e["via"] == "early" and t_last < e["sim_ms"] < end for e in view.events_of(z.id, "leave")):
                            false_early += 1
                elif s.get("unseen"):
                    ev = next((e for e in view.events_of(z.id, "leave") if e["emit_ms"] >= t_last - 500), None)
                    d = view.delay_at(z.id, t_last, 300)
                    lag = (ev["emit_ms"] - t_last) / 1000.0 if ev else None
                    if ldr:
                        unseen_ldr += 1
                        ok = ev is not None and ev["via"] == "early" and EARLY_ABSENT_S - 0.3 <= lag <= EARLY_ABSENT_S + 40
                        unseen_ldr_ok += 1 if ok else 0
                        if not ok and ev is not None and ev["via"] == "hold_expired" and lag >= d - 0.3:
                            unseen_ldr_blocked += 1      # a track was seen somewhere in the room (the world's reflector ghost)
                            ok = True
                    else:
                        unseen_non += 1
                        ok = ev is not None and ev["via"] == "hold_expired" and lag >= d - 0.3
                        unseen_non_ok += 1 if ok else 0
                    if not ok:
                        unseen_bad.append({"ldr": ldr, "via": ev["via"] if ev else None, "lag_s": round(lag, 1) if lag is not None else None})
        hidden_total += hidden
        false_total += false_early
        if unseen_bad:
            problems.append("%d unseen leaves behaved wrongly (%s)" % (len(unseen_bad), unseen_bad[:2]))
        if unseen_ldr and unseen_ldr_ok < UNSEEN_EARLY_MIN * unseen_ldr:
            problems.append("only %d of %d unseen leaves in an ldr zone cleared early (the others were held to the deadline)" % (
                unseen_ldr_ok, unseen_ldr))
        if problems:
            st, note = FAIL, "; ".join(problems[:3])
        elif first_ldr is None:
            st, note = INCOMPLETE, "ldr never became true (the most known time: %d s of %d)" % (known_max, LD_MIN_KNOWN_S)
        elif unseen_ldr == 0:
            st, note = INCOMPLETE, "no unseen leave in an ldr zone"
        else:
            st, note = PASS, None
        rows.append(_row(r, st, note=note, zone=z.id, first_ldr_night=first_ldr, known_s_max=known_max, hidden_spells_in_ldr=hidden,
                         false_early=false_early, unseen_ldr=[unseen_ldr_ok, unseen_ldr], unseen_ldr_blocked_to_deadline=unseen_ldr_blocked, unseen_not_ldr=[unseen_non_ok, unseen_non]))
    ghost_rows = []
    for r in _runs(results, "p6ldghost"):
        if not _ok(r):
            ghost_rows.append(_row(r, INCOMPLETE, note="the run did not finish"))
            continue
        view = RunView(r)
        z = view.zone_for(W.REFLECTOR, kinds=("manual", "dwell", "short_task", "bed"))
        if z is None:
            ghost_rows.append(_row(r, INCOMPLETE, note="no zone at the reflector (the label press did not succeed)"))
            continue
        nl = _ld_nights(view, z.id)
        known = max([(x[1] or [0, 0])[1] for x in nl] or [0])
        on = max([(x[1] or [0, 0])[0] for x in nl] or [0])
        share = round(on / known, 3) if known else None
        ldr_any = any(x[2] for x in nl)
        if ldr_any or (share is not None and known >= GHOST_MIN_KNOWN_S and share >= LD_MIN_SHARE):
            st, note = FAIL, "the ghost zone became ldr or has an LD2412-on share of %s" % share
        elif known < GHOST_MIN_KNOWN_S:
            st, note = INCOMPLETE, "known_s %d is below %d: too little ghost time to judge a share" % (known, GHOST_MIN_KNOWN_S)
        else:
            st, note = PASS, None
        ghost_rows.append(_row(r, st, note=note, zone=z.id, known_s=known, on_s=on, share=share, ldr=ldr_any,
                               unreachable_by_time_rule=known < LD_MIN_KNOWN_S))
    sub_ld, k1, n1 = _kofn(rows)
    sub_gh, k2, n2 = _kofn(ghost_rows)
    rate = (false_total / hidden_total) if hidden_total else None
    if hidden_total < FALSE_EARLY_MIN_SPELLS:
        st_rate = INCOMPLETE
    else:
        st_rate = FAIL if rate > FALSE_EARLY_MAX else PASS
    status = _combine([sub_ld, sub_gh, st_rate])
    return {"id": "P6-9", "gated": True, "status": status,
            "title": "ld2412_reliable from the zone's LD2412 evidence, the early clear, the ghost zone and the false early clear rate",
            "details": {"p6ld": rows, "p6ldghost": ghost_rows,
                        "false_early_rate": {"hidden_spells": hidden_total, "false_early": false_total,
                                             "rate": round(rate, 4) if rate is not None else None, "limit": FALSE_EARLY_MAX,
                                             "status": st_rate}},
            "counts": {"p6ld": "%d of %d seeds PASS (need %d)" % (k1, n1, _need(n1)),
                       "p6ldghost": "%d of %d seeds PASS (need %d)" % (k2, n2, _need(n2))}}


# ---------------------------------------------------------------- P6-10 (variants)
def _variant_ok(vid: str, t_ms: float) -> bool:
    """Is ``vid`` (wd|we + start hour + end hour) the variant of the local time ``t_ms`` (the replay's day 0 is a Monday)."""
    day, ms = _local_ms(t_ms)
    cls = "we" if day % 7 >= 5 else "wd"
    h = ms // 3600000
    s, e = int(vid[2:4]), int(vid[4:6])
    inside = (s <= h < e) if s < e else (h >= s or h < e)
    return vid[:2] == cls and inside


def crit_p610(results: list) -> dict:
    rows = []
    for r in _runs(results, "p6setup"):
        if not _ok(r):
            rows.append(_row(r, INCOMPLETE, note="the run did not finish"))
            continue
        view = RunView(r)
        with_var, bad = {}, []
        for night, rec in _night_rows(view):
            for zid, vs in ((rec.get("zone_states") or {}).get("variants") or {}).items():
                with_var[zid] = vs
        seen = 0
        for zid in view.zones:
            for t, row in zip(view.zt.get(zid, []), view.zr.get(zid, [])):
                if row[4]:
                    seen += 1
                    if not _variant_ok(row[4], t):
                        bad.append({"zone": zid, "t": t, "variant": row[4]})
        info = {}
        for night, rec in _night_rows(view):
            info = {z: bool(v) for z, v in ((rec.get("zone6") or {}).get("info") or {}).items()}
        if bad:
            st, note = FAIL, "%d state emissions carry a variant that does not match the time" % len(bad)
        elif not with_var:
            st, note = INCOMPLETE, "no zone has variants in this run"
        else:
            st, note = PASS, None
        rows.append(_row(r, st, note=note, zones_with_variants=with_var, emissions_with_variant=seen, zone6_info=info, bad=bad[:3]))
    st, k, n = _kofn(rows)
    return {"id": "P6-10", "gated": True, "status": st, "title": "Time variants: variant_now matches the variant's days and hours "
            "(the rest is unit tests)", "details": {"seeds": rows}, "counts": {"seeds": "%d of %d seeds PASS (need %d)" % (k, n, _need(n))}}


# ---------------------------------------------------------------- P6-11 (pause pair)
def _projection(view: RunView) -> list:
    """The ZoneStates series projected to (emit_ms, valid, [(zone, occ, count, held), ...]) without the emissions that change
    none of these (the signature of a ZoneStates also holds ldr, delay_s and variant)."""
    out = []
    last = {}
    lastv = None
    for st in view.states:
        t, _ctx, _boot, valid, _tdev, rows = st
        ch = []
        for r in rows:
            proj = (r[1], r[2], r[3])
            if last.get(r[0]) != proj:
                last[r[0]] = proj
                ch.append((r[0],) + proj)
        if ch or valid != lastv:
            lastv = valid
            out.append((t, bool(valid), tuple(sorted(ch))))
    return out


def crit_p611(results: list) -> dict:
    a = {r["seed"]: r for r in _runs(results, "p6guests")}
    b = {r["seed"]: r for r in _runs(results, "p6guestsc")}
    rows = []
    for seed in sorted(set(a) | set(b)):
        ra, rb = a.get(seed), b.get(seed)
        if ra is None or rb is None:
            rows.append({"job": "p6guests/p6guestsc", "seed": seed, "status": INCOMPLETE, "note": "one run of the pair is missing"})
            continue
        if not (_ok(ra) and _ok(rb)):
            rows.append(_row(ra, INCOMPLETE, note="a run of the pair did not finish"))
            continue
        va, vb = RunView(ra), RunView(rb)
        za = sorted((z["id"], tuple(map(tuple, z["cells_rle"]))) for z in va.p6.get("zones", []))
        zb = sorted((z["id"], tuple(map(tuple, z["cells_rle"]))) for z in vb.p6.get("zones", []))
        if za != zb:
            rows.append(_row(ra, INCOMPLETE, note="the zone sets of the pair differ (ids or cells)", zones=[len(za), len(zb)]))
            continue
        guests = ((ra.get("p4") or {}).get("actors") or {}).get("guests_user") or {}
        wins = []
        on = None
        for sw in guests.get("switches") or []:
            if sw["on"]:
                on = sw["t"]
            elif on is not None:
                wins.append((on, sw["t"]))
                on = None
        key = lambda e: (e["emit_ms"], e["zone"], e["kind"], e["via"], e["track"], e["dwell_s"], e["sim_ms"])   # noqa: E731
        ea, eb = sorted(map(key, va.events)), sorted(map(key, vb.events))
        pa, pb = _projection(va), _projection(vb)
        in_pause = sum(1 for e in va.events if any(w0 <= e["emit_ms"] <= w1 for w0, w1 in wins))
        diffs = []
        if ea != eb:
            diffs.append("the ZoneEvent sequences differ (%d against %d events)" % (len(ea), len(eb)))
        if pa != pb:
            diffs.append("the projected ZoneStates differ (%d against %d emissions)" % (len(pa), len(pb)))
        if diffs:
            st, note = FAIL, "; ".join(diffs)
        elif not wins or in_pause == 0:
            st, note = INCOMPLETE, "no ZoneEvent inside a pause window (%d windows): the pause was not exercised" % len(wins)
        else:
            st, note = PASS, None
        rows.append(_row(ra, st, note=note, pause_windows=len(wins), events=len(ea), events_in_pause=in_pause,
                         emissions=len(pa), paused_stays=[guests.get("paused_stays"),
                                                          (((rb.get("p4") or {}).get("actors") or {}).get("guests_user") or {}).get("paused_stays")]))
    st, k, n = _kofn(rows)
    return {"id": "P6-11", "gated": True, "status": st, "title": "Zone evaluation keeps running while learning is paused",
            "details": {"seeds": rows}, "counts": {"seeds": "%d of %d seeds PASS (need %d)" % (k, n, _need(n))}}


# ---------------------------------------------------------------- P6-14 (count)
def crit_p614(results: list) -> dict:
    rows = []
    for r in _runs(results, "p6count"):
        if not _ok(r):
            rows.append(_row(r, INCOMPLETE, note="the run did not finish"))
            continue
        view = RunView(r)
        z = view.zone_for(J.P6_SPOTS["bed"])
        people = [p for p in view.people if p["kind"] == "count"]
        if z is None or not people:
            rows.append(_row(r, INCOMPLETE, note="no zone at the bed or no scripted count people"))
            continue
        cr = view.crossings(z)
        ch = set()
        for c in cr:
            ch.add(c["t_in"])
            ch.add(c["t_out"])
            if c["dies_inside"]:
                ch.add(c["t_out"] + 2000)             # the tracker keeps a vanished track for its ttl (2 s)
        changes = sorted(ch)
        days = sorted({p["d"] for p in people})
        match = total = 0
        max_truth = 0
        held_bad = 0
        for d in days:
            ps = [p for p in people if p["d"] == d]
            t0 = min(p["tracks"][0][0][0] for p in ps) * 1000
            t1 = max(p["tracks"][0][-1][3] for p in ps) * 1000 + 3000
            if t0 < z.created_ms or not view.observed_between(t0, t1):
                continue
            iv = [(c["t_in"], c["t_out"]) for c in cr if t0 - 5000 <= c["t_in"] <= t1]
            t = t0
            while t < t1:
                i = bisect_left(changes, t)
                near = (i < len(changes) and changes[i] - t <= 1500) or (i > 0 and t - changes[i - 1] <= 1500)
                if not near:
                    truth = min(3, sum(1 for a_, b_ in iv if a_ <= t <= b_))
                    row = view.zone_row(z.id, t + 200)
                    total += 1
                    max_truth = max(max_truth, truth)
                    if row is not None and min(row[2], 3) == truth:
                        match += 1
                t += 1000
        for t, row in zip(view.zt.get(z.id, []), view.zr.get(z.id, [])):
            if row[3] and row[2] > 0:
                held_bad += 1
        # the cases: A2 leaves while A1 stays; C2 vanishes while C1 stays
        case_bad = []
        by_note = defaultdict(dict)
        for p in people:
            by_note[p["d"]][p["note"]] = p
        def t_out(p):
            cs = [c for c in cr if c["person"] == p["id"]]
            return cs[-1]["t_out"] if cs else None

        for d, cases in by_note.items():
            a1, a2, c1, c2 = cases.get("A1"), cases.get("A2"), cases.get("C1"), cases.get("C2")
            if a1 and a2 and t_out(a1) and t_out(a2) and view.observed_between(a1["tracks"][0][0][0] * 1000, t_out(a1) + 1000):
                t_a2, t_a1 = t_out(a2), t_out(a1)
                lv = [e for e in view.events_of(z.id, "leave") if t_a2 - 500 <= e["sim_ms"] <= t_a1 - 1000]
                row = view.zone_row(z.id, t_a2 + 4000)
                if lv or not (row and row[1] and row[2] == 1):
                    case_bad.append({"case": "A", "day": d, "leaves": len(lv), "row": row})
            if c1 and c2 and t_out(c1) and t_out(c2) and view.observed_between(c1["tracks"][0][0][0] * 1000, t_out(c1) + 1000):
                t_v, t_c1 = t_out(c2), t_out(c1)
                lv = [e for e in view.events_of(z.id, "leave") if t_v <= e["sim_ms"] <= t_c1 - 1000]
                row = view.zone_row(z.id, t_v + 6000)
                if lv or not (row and row[1] and row[2] == 1):
                    case_bad.append({"case": "C", "day": d, "leaves": len(lv), "row": row})
        share = match / total if total else None
        if share is None or max_truth < 3:
            st, note = INCOMPLETE, "the cases were not exercised (samples %d, the most people inside %d)" % (total, max_truth)
        elif share < COUNT_MATCH_MIN or case_bad or held_bad:
            st, note = FAIL, "count matches %s of the time; case failures %d; held rows with a count %d" % (round(share, 3), len(case_bad), held_bad)
        else:
            st, note = PASS, None
        rows.append(_row(r, st, note=note, zone=z.id, samples=total, match_share=round(share, 4) if share is not None else None,
                         max_people_inside=max_truth, case_failures=case_bad[:3], held_with_count=held_bad))
    st, k, n = _kofn(rows)
    return {"id": "P6-14", "gated": True, "status": st, "title": "Zone count equals the number of people inside", "details": {"seeds": rows},
            "counts": {"seeds": "%d of %d seeds PASS (need %d)" % (k, n, _need(n))}}


# ---------------------------------------------------------------- reported: the zone set per seed
def rep_zones(results: list) -> dict:
    rows = []
    for r in sorted([x for x in results if is_p6_run(x)], key=lambda x: (x.get("job", ""), x["seed"])):
        p6 = r.get("p6") or {}
        zs = p6.get("zones") or []
        probe = (((r.get("p4") or {}).get("actors") or {}).get("zone_probe")) or {}
        rows.append({"job": r["job"], "seed": r["seed"], "status": r.get("status"), "core_errors": r.get("core_errors"),
                     "zones": [[z["id"], z["kind"], z["cx"], z["cy"], z["n_cells"]] for z in zs],
                     "first_zone_night": probe.get("first_zone_night"), "spots": (probe.get("last") or {}).get("spots"),
                     "events": len(p6.get("events", [])), "state_emissions": len(p6.get("states", [])),
                     "enters": p6.get("enters"), "enters_without_debug": p6.get("enters_without_debug"),
                     "people": len(p6.get("people", []))})
    return {"id": "P6-zones", "title": "The zones the learner reported per run (the zones under test), and what the run recorded",
            "status": REPORTED, "details": rows}


# ---------------------------------------------------------------- entry
def evaluate_p6(results: list) -> Optional[dict]:
    """The P6 part of the report, or None when no run carries a P6 scenario."""
    if not any(is_p6_run(r) for r in results):
        return None
    edge = crit_edge(results)
    criteria = [edge["P6-1"], edge["P6-2"], crit_p63(results), crit_p64(results), crit_p65(results), crit_p66(results),
                crit_p67(results), crit_p68(results), crit_p69(results), crit_p610(results), crit_p611(results),
                crit_p614(results), edge["P6-15"]]
    reported = [rep_zones(results)]
    statuses = [c["status"] for c in criteria]
    return {"runs": sum(1 for r in results if is_p6_run(r)), "criteria": criteria, "reported": reported, "gate": _combine(statuses),
            "failed": [c["id"] for c in criteria if c["status"] == FAIL], "unit_evidence": [list(x) for x in UNIT_EVIDENCE],
            "other_commands": [list(x) for x in OTHER_COMMANDS], "interpretation": INTERPRETATION}


def _compact(d, limit: int = 1500) -> list:
    return E4._compact(d, limit)


def _dump(x, limit: int) -> str:
    t = json.dumps(x, sort_keys=True, separators=(",", ":"))
    return t if len(t) <= limit else t[:limit] + " ..."


def render_p6(p6: dict) -> list:
    """Markdown lines of the P6 section of the report."""
    L = ["", "## P6: confirmed-zone evaluation", "",
         "Gate (the replay-evaluated checks P6-1 to P6-11, P6-14, P6-15): **%s**%s. P6 runs: %d." % (
             p6["gate"], (" (failed: %s)" % ", ".join(p6["failed"])) if p6["failed"] else "", p6["runs"]),
         "", "| ID | Status | Check |", "|---|---|---|"]
    for c in p6["criteria"]:
        L.append("| %s | %s | %s |" % (c["id"], c["status"], c["title"]))
    L += ["", "G1-G6, P4-1 to P4-7 and P5-1 to P5-8 were **not run** in this invocation (P6-R1 compares the P5 jobs on the baseline "
          "tree and on this one).", ""]
    for c in p6["criteria"]:
        L += ["", "### %s: %s" % (c["id"], c["status"]), "", c["title"], ""]
        d = c["details"]
        if c["id"] in ("P6-1", "P6-2", "P6-15"):
            for nz, g in sorted(d["by_noise"].items()):
                L.append("- noise %s m: **%s** (%s)" % (nz, g["status"], g["counts"]))
                for x in g["seeds"]:
                    keep = {k: v for k, v in x.items() if k not in ("job", "seed", "status", "zones")}
                    L.append("  - seed %s: **%s** `%s`" % (x["seed"], x["status"], _dump(keep, 700)))
            if d.get("missing_noise_levels"):
                L.append("- missing runs at noise %s: INCOMPLETE" % d["missing_noise_levels"])
            for k in ("flicker_reported", "reported_07_delayed"):
                if k in d:
                    L.append("- %s: `%s`" % (k, _dump(d[k], 1800)))
        elif c["id"] == "P6-9":
            for k in ("p6ld", "p6ldghost"):
                for x in d[k]:
                    keep = {kk: v for kk, v in x.items() if kk not in ("job", "seed", "status")}
                    L.append("- %s seed %s: **%s** `%s`" % (k, x["seed"], x["status"], _dump(keep, 600)))
            L.append("- false early clear rate: `%s`" % _dump(d["false_early_rate"], 400))
        elif "seeds" in d:
            if not d["seeds"]:
                L.append("- no run of this check's job in this invocation: INCOMPLETE")
            for x in d["seeds"]:
                keep = {k: v for k, v in x.items() if k not in ("job", "seed", "status")}
                L.append("- seed %s: **%s** `%s`" % (x["seed"], x["status"], _dump(keep, 1200)))
            if d.get("reported"):
                L.append("- reported: `%s`" % _dump(d["reported"], 1800))
        else:
            L += _compact(d)
        if c.get("counts"):
            L += ["", "Seeds behind each sub-check:"] + ["- %s: %s" % (k, v) for k, v in c["counts"].items()]
        L.append("")
    L += ["## P6 reported", ""]
    for c in p6["reported"]:
        L += ["### %s" % c["id"], "", c["title"], ""]
        for row in c["details"]:
            L.append("- %s s%s %s: `%s`" % (row["job"], row["seed"], row["status"], _dump({k: v for k, v in row.items() if k not in ("job", "seed", "status")}, 600)))
        L.append("")
    L += ["## Evidence: unit tests", "", "| Check | What | Where |", "|---|---|---|"]
    for a, b, c in p6["unit_evidence"]:
        L.append("| %s | %s | %s |" % (a, b, c))
    L += ["", "## Separate commands", "", "| Check | What | Command |", "|---|---|---|"]
    for a, b, c in p6["other_commands"]:
        L.append("| %s | %s | `%s` |" % (a, b, c))
    L += ["", "## P6 interpretations", ""] + ["- " + t for t in p6["interpretation"]] + [""]
    return L


def build_report(results: list, unit_g5b: Optional[bool] = None) -> dict:
    """``report.json`` content of an invocation that has P6 runs. Runs that are not P6 runs are given to the P5 or the P3/P4
    report and the P6 section is attached to it; without any other run it is a pure P6 report."""
    from . import evaluate as E
    from . import evaluate_p5 as E5
    p6res = [r for r in results if is_p6_run(r)]
    others = [r for r in results if not is_p6_run(r)]
    p6 = evaluate_p6(p6res)
    summaries = [E.summarize_run(r) for r in p6res]
    bad = [{"job": s["job"], "seed": s["seed"], "status": s["status"], "core_errors": s["core_errors"],
            "first_error": s["first_error"]} for s in summaries if s["status"] != "ok" or s["core_errors"]]
    if others:
        report = E5.build_report(others, unit_g5b=unit_g5b) if any(E5.is_p5_run(r) or E5.is_rerun(r) for r in others) \
            else E.evaluate(others, unit_g5b=unit_g5b)
        report["p6"] = p6
        failed = report["failed"] + p6["failed"]
        report["failed"] = failed
        report["gate"] = _combine([report["gate"], p6["gate"]])
        report["bad_runs"] = report["bad_runs"] + bad
        report["exit_code"] = 1 if failed or any(b["status"] != "ok" for b in report["bad_runs"]) else 0
        report["job_matrix"] = report["job_matrix"] + E.job_matrix(summaries)
        return report
    params = sorted({r.get("params") for r in p6res if r.get("params")})
    return {"schema": 1, "kind": "p6", "runs": len(p6res), "params_digests": params, "gate": p6["gate"], "failed": p6["failed"],
            "scope": SCOPE_P6, "job_matrix": E.job_matrix(summaries),
            "exit_code": 1 if p6["failed"] or any(b["status"] != "ok" for b in bad) else 0, "bad_runs": bad,
            "criteria": [], "reported": [], "p6": p6}


def render_markdown(report: dict) -> str:
    from . import evaluate as E
    p6 = report.get("p6")
    if report.get("kind") != "p6":
        from . import evaluate_p5 as E5
        text = E5.render_markdown(report) if report.get("p5") else E.render_markdown(report)
        return text + "\n".join(render_p6(p6)) + "\n" if p6 else text
    L = ["# P6 replay report (G1-G6, P4-1 to P4-7 and P5-1 to P5-8 not run)", "",
         "Gate (%s): **%s**%s. Runs: %d. Params digest: %s." % (
             report["scope"], report["gate"], (" (failed: %s)" % ", ".join(report["failed"])) if report["failed"] else "",
             report["runs"], ", ".join(report["params_digests"]) or "-"), ""]
    if report.get("job_matrix"):
        L += ["Jobs run (job, seeds, nights, simulated days):", "", "| Job | Seeds | Nights | Sim. days |", "|---|---|---|---|"]
        for m in report["job_matrix"]:
            L.append("| %s | %s | %s | %s |" % (m["job"], ",".join(map(str, m["seeds"])), ",".join(map(str, m["nights"])), m["sim_days"]))
    if report["gate"] == INCOMPLETE:
        L += ["", "INCOMPLETE: a check lacks runs or a scripted step could not be exercised (see the sections below)."]
    if report["bad_runs"]:
        L += ["", "Runs that did not finish cleanly:", ""]
        for b in report["bad_runs"]:
            L.append("- %s seed %s: %s, core errors %s" % (b["job"], b["seed"], b["status"], b["core_errors"]))
    return "\n".join(L + render_p6(p6)) + "\n"


def write_report(report: dict, out_dir) -> None:
    """``report.json`` and ``report.md`` in ``out_dir``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, sort_keys=False), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
