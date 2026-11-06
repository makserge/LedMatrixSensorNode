"""Evaluation of the P7 replay checks (P7 spec 9.1; drift, moves, retire, radar moved, tuning; add-on 0.7.0).

The checks that a replay run can show (the others are unit tests, listed in the report as 'evidence: unit tests'):

* **P7-1** drift ratio and freezing (job ``p7ctl``): no day zone is frozen by coverage, the bed is frozen on every night.
* **P7-2** a moved sofa is a modify, not a new zone (the six move jobs); **P7-4** moved-from suppression (the same runs, 14 nights
  after the Approve; ``p7mvback`` reported).
* **P7-3** the bed under night-off is never flagged (``p7bed``, ``p7bednl``; ``p7bednf`` reported).
* **P7-5** retire after 21 covered days (``p7ret``).
* **P7-6** a rotated or shifted radar: a reset is suggested at night 30 and stays open; the reset itself (``p7rot``, ``p7rot3``,
  ``p7shift``, ``p7reset``; ``p7rot6`` and ``p7shift5`` reported).
* **P7-7** a one-week guest and a weekly visitor are never suggested (``p7guest``, ``p7guestp``; ``p7guestnp`` reported).
* **P7-8** no false modify, retire or radar-moved item (the 54 runs of the gated set and every other P7 run for the radar item).
* **P7-9** the tuning lines and the tuner (``p7tune``, ``p7tuneh300``, ``p7tuneh900``); **P7-10** the merge detector (reported).

Status per check: PASS, FAIL, INCOMPLETE (runs missing or too short, or a scripted step that could not be exercised), REPORTED. The
k-of-n rule is P4's (``replay.evaluate._need``: 4 of 5 seeds, all of them with fewer than 5), applied to the number of runs the
spec's table has, so a run that is not there is INCOMPLETE and never a pass; a deterministic rule needs every run. P7-11 and P7-12
(shell, core and fake-HA unit tests), P7-R1 (``replay.dump_run`` + ``replay.compare_runs``), P7-R2 (``replay.compat_check --p7``)
and P7-R3 (``replay.resource_probe --p7``) are separate. Interpretations are in ``INTERPRETATION``.

The evaluator reads only the run result (``result["nights"][*]["p7"]``, ``result["p7"]``, written by ``replay.driver``): items,
zones, drift view, radar state, the owner's answers and the tuning lines.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median
from typing import Optional

from radar_zone_learner.core.params import DEFAULT_P7 as P7

from . import jobs as J
from . import tune as TU
from . import world as W
from .evaluate import FAIL, INCOMPLETE, PASS, REPORTED, _need
from .truth import unrle

ALL_SEEDS = (1, 2, 3, 4, 5)
PLACE_TYPES = ("dwell_zone", "short_task_zone")
FP_LABELS = ("FP:guest", "FP:visitor")
SOFA_OLD = J.P6_SPOTS["sofaAB"]                  # the mean of the two sofa seats where they stood before the move
TABLE_SPOT = W.SPOT["table"]
SOFA_RADIUS_M = 1.0                              # a zone record within this of the sofa's mean is the sofa zone
SOFA_IOU_MIN = 0.4                               # P7-2 (c): the moved zone against the truth region of the moved sofa
MOD_WINDOW = (2, 10)                             # P7-2 (a): the modify item opens this many nights after the move
MF_NIGHTS = 14                                   # P7-4: the observation window after the Approve
MF_LAST_APPROVE = 44                             # P7-4: a later Approve leaves less than 14 nights of a 58-night run (the 2.9 m jobs)
NEW_ZONE_NIGHTS = 5                              # P7-2 (d)
OVERLAP_MIN = 0.5                                # the overlap coefficient of the learner's suppression rule
POOLED_MIN = 0.9                                 # P7-2: 'in >=90% of runs'
BED_FIRST_NIGHT_MAX = 8                          # P7-3 p7bed: the bed label exists by this night
BEDNL_FIRST_NIGHT_MAX = 14                       # P7-3 p7bednl: the learned bed zone exists by this night
BED_MIN_NIGHTS = 28                              # P7-3 p7bed: nights evaluated
RADAR_OPEN_NIGHT = 30                            # P7-6: the first night on which the item can be open (the rotation is at 08:00 of day 28)
RADAR_LAST_NIGHT = 34                            # P7-6: still open here without an answer
RESET_FOLLOW_NIGHTS = 14
RESET_DISPLACEMENT_MIN_M = 0.3
RESET_SHARE_MIN = 0.6
RESET_SHIFT_TOL_M = 0.4
GATED_MIN_RUNS = 50                              # P7-8: fewer evaluated runs of the no-modify set are INCOMPLETE
LATE_MIN_NIGHTS = 20                             # P7-1: a control run shorter than this does not tell
EXPORT_LINES = P7.agg_keep_nights

SCOPE_P7 = "P7-1 to P7-10 (the replay-evaluated checks); G1-G6, P4-1 to P4-7, P5-1 to P5-8 and P6-1 to P6-15 not run"


def jn(variant: str, window: str = "w0000_0600") -> str:
    return "bed_%s_%s" % (window, variant)


P72_JOBS = {jn("p7mv11", "none"): 5, jn("p7mv29", "none"): 5, jn("p7mv11"): 5, jn("p7mv29"): 5,
            jn("p7mv11e", "none"): 3, jn("p7mv29e", "none"): 3}
P73_BED_JOBS = {jn("p7bed"): 5, jn("p7bednl"): 5}
P78_NOMOD_JOBS = {jn("p7ctl"): 5, jn("p7ctl", "none"): 5, jn("p7ctle", "none"): 5, jn("p7ret"): 5, jn("p7bed"): 5, jn("p7bednl"): 5,
                  jn("p7bednf"): 5, jn("p7guest"): 5, jn("p7guestp"): 5, jn("p7tune"): 3, jn("p7tuneh300"): 3, jn("p7tuneh900"): 3}
P78_CTL_JOBS = (jn("p7ctl"), jn("p7ctl", "none"), jn("p7ctle", "none"))
P78_RADAR_ALLOWED = ("p7rot", "p7shift", "p7reset", "p7rot6", "p7shift5")      # gated in P7-6 (the first three) or reported

UNIT_EVIDENCE = [
    ("P7-1 (rules), P7-10", "Windows and weights, the ratio and its states, the usual profile and the freeze, the counters, labels and "
     "beds, collapsed, min_slow_stays, footprint; the merge detector, the own-suggestion source, the flag matrix",
     "tests/test_drift.py, tests/test_p7_state.py, tests/test_moves.py"),
    ("P7-4 (rules), P7-5 (rules)", "Moved-from masks and their lift, the retire rule, Keep for 90 days, the guards", "tests/test_movedfrom.py, "
     "tests/test_retire.py"),
    ("P7-6 (rules)", "The radar-moved search, the latch, the hold, Ignore, reset_maps and 'needs re-check'", "tests/test_radarmoved.py, "
     "tests/test_resetmaps.py"),
    ("P7-9 (rules)", "The nightly line, the pooled record and the privacy scan; the tuner", "tests/test_tuningagg.py, tests/test_tune.py"),
    ("P7-11", "Items, ids, tiers, the answer matrix, texts, push and attention, the blueprint", "tests/test_p7_review.py, "
     "tests/test_p7_notify.py, tests/test_p7_texts.py, tests/test_p7_blueprint.py"),
    ("P7-12", "Entities and automations survive a modify (the fake Home Assistant)", "tests/test_p7_entities.py"),
    ("P7-R1 (enabled=False)", "Every P7 site is a no-op while disabled", "tests/test_p7_enabled.py"),
    ("P7-R2 (state)", "zone7 defaults, validation, orphans, unknown keys, byte identity", "tests/test_p7_state.py, tests/test_p7_types.py"),
]
OTHER_COMMANDS = [
    ("P7-R1", "Nothing of P3-P6 learning changed with enabled=False",
     "replay/dump_run.py on the P5 baseline (git archive 91c05e9) and on this tree for the P5 jobs, then replay/compare_runs.py --a <base> --b "
     "<p7> (the P6 jobs: --a <stored P6 run outputs> --b <this tree's runs>, the P6B tree could not be kept: see the report)"),
    ("P7-R2", "State compatibility in both directions", "replay/compat_check.py --p7 --base <baseline tree> --out <dir>"),
    ("P7-R3", "Cost", "replay/resource_probe.py --p7 --out <dir>"),
]

INTERPRETATION = [
    "Runs. A run that is not 'ok' or has a core error is INCOMPLETE for every check (a nightly step that raised is a core failure: the "
    "report lists it under 'runs not ok' with its first error). The spec's run counts are the expected counts: a missing run is INCOMPLETE "
    "and never a pass; the k-of-n rule is read against the expected count (4 of 5, 3 of 3).",
    "Zones. 'Zone' is what Learner.zones() returns: the zone records (ids z....) and the 'Mark spot here' labels (ids l....). The bed of "
    "p7bed is a label, the bed of p7bednl a record. The sofa zone is the zone record within 1.0 m of the mean of the two sofa seats at the "
    "night before the move; a run without one is INCOMPLETE.",
    "P7-1. 'Frozen by coverage' on a day zone is read from the drift view of every night: frozen (ZoneDrift.frozen, which includes the static "
    "predicate) or the states frozen and blind. The share of falling nights of a zone counts the states falling over the nights whose state "
    "is ok, falling or collapsed.",
    "P7-2 (b). A plain place item at the new centre counts when its suggestion is labelled TP:sofaAB or is an unlabelled blob (FP:other) within "
    "1.0 m of the new centre; an item labelled with another true place is not the sofa (the 2.9 m move puts the sofa 0.73 m from the armchair "
    "and 0.41 m from the visitor's spot, so a distance rule alone would flag the armchair).",
    "P7-2 (c). The zone count is compared between the record of the night of the Approve and the next one; the IoU is taken on the cells of the "
    "last night against the truth region of the sofa at the same night (the 14-day region, so it still holds some pre-move mass).",
    "P7-3. A merge item counts against the bed only when it names the bed zone (the bed is never a merge target); a modify, retire or "
    "radar_moved item of p7bed counts wherever it is (P7-8 says the same for modify and retire).",
    "P7-4. The old cells are the sofa zone's cells in the record of the night of the Approve (before the answer). The observation window is "
    "the 14 nights after it; an Approve later than night 41 leaves a shorter window and is INCOMPLETE.",
    "P7-5. n1 is the first night after the retirement with rt >= 1, n_open the first night with the item; the zone is the one within 1.0 m "
    "of the table at night 13.",
    "P7-6. 'Open at night N' is the item radar_moved in the item list of the record of night N. For p7reset a zone counts as re-anchored "
    "when it has a modify item within 0.4 m of the expected displacement 2 r sin(5 degrees); the share is taken over the non-bed zone records "
    "of the reset night whose expected displacement is at least 0.3 m.",
    "P7-7. 'Offered' is any item (plain place, merge or modify, by its suggestion id) whose suggestion is labelled FP:guest or FP:visitor in "
    "the item list of a nightly record.",
    "P7-9. The pooled record is tuningagg.pool of the last 28 lines of the run; tune.propose reads it. The bridge_gap_s proposal is compared "
    "over the three jobs per seed; a run with fewer than 20 spells has no proposal and makes the comparison INCOMPLETE.",
]


# ---------------------------------------------------------------- helpers
def _script(res: dict) -> str:
    return (res.get("job_spec") or {}).get("script") or ""


def is_p7_run(res: dict) -> bool:
    return _script(res).startswith("p7")


def _runs(results: list, job: str) -> list:
    return sorted([r for r in results if r.get("job") == job], key=lambda r: r["seed"])


def _usable(res: dict) -> bool:
    return res.get("status") == "ok" and not res.get("core_errors")


def _combine(statuses: list) -> str:
    if not statuses:
        return INCOMPLETE
    if FAIL in statuses:
        return FAIL
    if INCOMPLETE in statuses:
        return INCOMPLETE
    return PASS


def _gate(rows: list, expected: int, deterministic: bool = False) -> str:
    """PASS / FAIL / INCOMPLETE of per-run rows against the number of runs the spec expects: the k-of-n rule of P4 (a deterministic rule
    needs every expected run to pass; one failure fails it)."""
    sts = [r["status"] for r in rows]
    p, f = sts.count(PASS), sts.count(FAIL)
    if deterministic:
        if f:
            return FAIL
        return PASS if p >= expected else INCOMPLETE
    need = _need(expected)
    if p >= need:
        return PASS
    if f > expected - need:
        return FAIL
    return INCOMPLETE


def _row(res: dict, status: str, **kw) -> dict:
    d = {"job": res.get("job"), "seed": res.get("seed"), "status": status}
    d.update(kw)
    return d


def _bad(res: dict, why: str = "") -> dict:
    """A row for a run that is not usable."""
    return _row(res, INCOMPLETE, reason="the run is not usable: status %s, %s core errors%s" % (
        res.get("status"), res.get("core_errors"), ("; " + why) if why else ""))


def iou(a, b) -> float:
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_coef(a, b) -> float:
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _dist(ax, ay, bx, by) -> float:
    return math.hypot(ax - bx, ay - by)


def _stats(vals: list) -> dict:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return {"n": 0}
    return {"n": len(v), "min": v[0], "median": median(v), "max": v[-1]}


class RunView:
    """One run result read for the checks: the night records and what ``rec["p7"]`` holds."""

    def __init__(self, res: dict):
        self.res = res
        self.nights = {n["night"]: n for n in res.get("nights") or []}
        self.order = sorted(self.nights)
        self.last = self.order[-1] if self.order else 0
        self.js = res.get("job_spec") or {}
        self.script = self.js.get("script") or ""
        self.p7res = res.get("p7") or {}

    def p7(self, n: int) -> dict:
        return (self.nights.get(n) or {}).get("p7") or {}

    def items(self, n: int) -> list:
        return self.p7(n).get("items") or []

    def zones(self, n: int) -> list:
        return self.p7(n).get("zones") or []

    def zone(self, n: int, zid: str) -> Optional[dict]:
        return next((z for z in self.zones(n) if z["id"] == zid), None)

    def records(self, n: int) -> list:
        return [z for z in self.zones(n) if z["id"].startswith("z")]

    def drift(self, n: int, zid: str) -> Optional[dict]:
        return next((d for d in self.p7(n).get("drift") or [] if d["id"] == zid), None)

    def sug(self, n: int) -> dict:
        return {s["id"]: s for s in (self.nights.get(n) or {}).get("suggestions") or []}

    def label_of(self, n: int, item: dict) -> str:
        return (self.sug(n).get(item.get("sid") or item["id"]) or {}).get("sim_label") or ""

    def answers(self) -> list:
        user = self.p7res.get("user") or {}
        if user.get("answers") is not None:
            return list(user["answers"])
        out = []
        for n in self.order:
            out += self.p7(n).get("answers") or []
        return out

    def first_applied(self, item_id: str) -> Optional[int]:
        ns = [a["night"] for a in self.answers() if a.get("item") == item_id and a.get("op") == "approve" and a.get("outcome") == "applied"]
        return min(ns) if ns else None

    def item_nights(self, pred) -> list:
        return [n for n in self.order if any(pred(i) for i in self.items(n))]

    def has_p7(self) -> bool:
        return any(self.p7(n) for n in self.order)


def _sofa_zone(v: RunView, night: int) -> Optional[dict]:
    """The zone record within 1.0 m of the sofa's mean at ``night`` (the nearest), or None."""
    cand = [z for z in v.records(night) if _dist(z["cx"], z["cy"], *SOFA_OLD) <= SOFA_RADIUS_M]
    return min(cand, key=lambda z: _dist(z["cx"], z["cy"], *SOFA_OLD)) if cand else None


def _new_centre(js: dict) -> tuple:
    cfg = W.make_cfg(move=js.get("move") or 0.0, move_day=js.get("move_day") or 35, move_back=js.get("move_back"))
    d = int(js.get("move_day") or 35)
    a, b = W.spot_pos("sofaA", d, cfg), W.spot_pos("sofaB", d, cfg)
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


# ---------------------------------------------------------------- P7-1
def crit_p71(results: list) -> dict:
    job = jn("p7ctl")
    rows = []
    for res in _runs(results, job):
        if not _usable(res):
            rows.append(_bad(res))
            continue
        v = RunView(res)
        if v.last < LATE_MIN_NIGHTS or not v.has_p7():
            rows.append(_row(res, INCOMPLETE, reason="the run has %d nights (needs %d) or no P7 record" % (v.last, LATE_MIN_NIGHTS)))
            continue
        kind, born = {}, {}
        for n in v.order:
            for z in v.zones(n):
                kind.setdefault(z["id"], z["kind"])
                born.setdefault(z["id"], z.get("night"))
        day_frozen, beds, per_zone = [], {}, {}
        for n in v.order:
            for d in v.p7(n).get("drift") or []:
                zid = d["id"]
                k = kind.get(zid)
                if k in ("dwell", "short_task"):
                    if d["fz"] or d["vs"] in ("frozen", "blind"):
                        day_frozen.append((zid, n, d["vs"]))
                    if (n - (born.get(zid) or 0)) >= 14 and d["vs"] in ("ok", "falling", "collapsed") and not d["fz"]:
                        e = per_zone.setdefault(zid, {"judged": 0, "falling": 0, "collapsed": 0, "max_rt": 0, "max_lo": 0, "used": 0,
                                                      "_lu": None})
                        e["judged"] += 1
                        e["falling"] += d["vs"] == "falling"
                        e["collapsed"] += d["vs"] == "collapsed"
                        e["max_rt"] = max(e["max_rt"], d["rt"])
                        e["max_lo"] = max(e["max_lo"], d["lo"])
                        if e["_lu"] is not None and d["lu"] > e["_lu"]:
                            e["used"] += 1
                        e["_lu"] = d["lu"]
        bed_ids = sorted({zid for zid, k in kind.items() if k == "bed"})
        bed_bad = []
        for zid in bed_ids:
            first = min(n for n in v.order if v.zone(n, zid))
            for n in v.order:
                if n >= first:
                    z = v.zone(n, zid)
                    if z is not None and not z["frozen"]:
                        bed_bad.append((zid, n))
        for z in per_zone.values():
            z.pop("_lu", None)
            z["falling_share"] = round(z["falling"] / z["judged"], 3) if z["judged"] else None
            z["use_share"] = round(z["used"] / max(1, z["judged"] - 1), 3)
        rows.append(_row(res, PASS if not day_frozen else FAIL, nights=v.last, day_frozen=day_frozen[:6], bed_ids=bed_ids,
                         bed_not_frozen=bed_bad[:6], bed_ok=bool(bed_ids) and not bed_bad, zones=per_zone))
    bed_rows = []
    for r in rows:
        if "bed_ok" not in r:                      # a run that is not usable or too short
            bed_rows.append(r)
        else:
            bed_rows.append(dict(r, status=(PASS if r["bed_ok"] else (INCOMPLETE if not r["bed_ids"] else FAIL))))
    a = _gate(rows, 5)
    b = _gate(bed_rows, 5, deterministic=True)
    return {"id": "P7-1", "gated": True, "status": _combine([a, b]),
            "title": "Drift ratio and freezing: no day zone is frozen by coverage, the bed is frozen on every night (p7ctl)",
            "details": {"seeds": rows},
            "counts": {"day zones never frozen by coverage": "%d of 5 seeds PASS (need %d)" % (sum(r["status"] == PASS for r in rows), _need(5)),
                       "bed frozen on every night": "%d of 5 seeds PASS (all needed)" % sum(r["status"] == PASS for r in bed_rows)}}


# ---------------------------------------------------------------- P7-2 and P7-4
def _p72_row(res: dict) -> dict:
    v = RunView(res)
    js = v.js
    md = int(js.get("move_day") or 35)
    if v.last < md + 2 or not v.has_p7():
        return _row(res, INCOMPLETE, reason="the run has %d nights, the move is on day %d" % (v.last, md))
    sofa = _sofa_zone(v, md - 1)
    if sofa is None:
        return _row(res, INCOMPLETE, no_sofa=True, reason="no zone record within 1.0 m of the sofa at night %d: the place was not learned" % (md - 1))
    zid = sofa["id"]
    new = _new_centre(js)
    true_shift = _dist(new[0], new[1], *SOFA_OLD)
    problems = []
    mod_nights = v.item_nights(lambda i: i["type"] == "modify_zone" and i.get("zid") == zid)
    first = min(mod_nights) if mod_nights else None
    if first is None:
        problems.append("no modify item for the sofa zone")
    elif first < md + MOD_WINDOW[0]:
        problems.append("the modify item opens at night %d, before night %d: nothing could have been learned" % (first, md + MOD_WINDOW[0]))
    elif first > md + MOD_WINDOW[1]:
        problems.append("the modify item opens at night %d, after night %d" % (first, md + MOD_WINDOW[1]))
    dup = []
    for n in v.order:
        if n < md:
            continue
        for i in v.items(n):
            if i["type"] not in PLACE_TYPES:
                continue
            s = v.sug(n).get(i["id"])
            if s and _dist(s["cx"], s["cy"], *new) <= SOFA_RADIUS_M and (s.get("sim_label") == "TP:sofaAB"
                                                                      or (s.get("sim_label") or "").startswith("FP:other")):
                dup.append((n, i["id"], s.get("sim_label")))
    if dup:
        problems.append("a plain place item at the new place: %s" % dup[:3])
    n_ans = v.first_applied("mod-" + zid)
    detail = {"zid": zid, "first_modify_night": first, "nights_to_modify": (first - md) if first is not None else None,
              "approve_night": n_ans, "true_shift_cm": round(true_shift * 100), "no_sofa": False}
    if first is not None:
        it = next((i for i in v.items(first) if i["type"] == "modify_zone" and i.get("zid") == zid), None)
        if it:
            detail["item_shift_cm"] = it.get("shift_cm")
            detail["item_src"] = it.get("src")
    detail["ratio"] = [[n, (v.drift(n, zid) or {}).get("ratio")] for n in v.order if n >= md - 1][:30]
    incomplete = []
    if n_ans is None:
        if first is not None:
            problems.append("the owner's approve was not applied (%s)" % [a["outcome"] for a in v.answers() if a.get("item") == "mod-" + zid][:3])
    elif n_ans + 1 > v.last:
        incomplete.append("the run ends at the night of the approve")
    else:
        before, after = v.records(n_ans), v.records(n_ans + 1)
        if len(before) != len(after):
            problems.append("the number of zone records changed: %d -> %d" % (len(before), len(after)))
        z0, z1 = v.zone(n_ans, zid), v.zone(n_ans + 1, zid)
        if z1 is None:
            problems.append("the sofa zone id is gone after the approve")
        elif z0 is not None and z1["rev"] != z0["rev"] + 1:
            problems.append("rev %s -> %s, not +1" % (z0["rev"], z1["rev"]))
        zl, rle = v.zone(v.last, zid), v.p7(v.last).get("sofa_rle")
        if zl is not None and rle:
            val = iou(unrle(zl["cells_rle"]), unrle(rle))
            detail["iou_last"] = round(val, 3)
            if val < SOFA_IOU_MIN:
                problems.append("IoU %.2f of the moved zone with the truth region, under %.1f" % (val, SOFA_IOU_MIN))
        elif zl is not None:
            incomplete.append("no truth region of the sofa at the last night")
        ids0 = {z["id"] for z in before}
        for n in range(n_ans + 1, min(v.last, n_ans + NEW_ZONE_NIGHTS) + 1):
            for z in v.records(n):
                if z["id"] not in ids0 and _dist(z["cx"], z["cy"], *new) <= SOFA_RADIUS_M:
                    problems.append("a new zone record %s at the new place at night %d" % (z["id"], n))
                    break
    if problems:
        return _row(res, FAIL, problems=problems, **detail)
    if incomplete:
        return _row(res, INCOMPLETE, reason="; ".join(incomplete), **detail)
    return _row(res, PASS, **detail)


def crit_p72(results: list) -> dict:
    by_job, all_rows = {}, []
    for job, expected in P72_JOBS.items():
        rows = [_p72_row(r) if _usable(r) else _bad(r) for r in _runs(results, job)]
        by_job[job] = {"expected": expected, "rows": rows, "status": _gate(rows, expected)}
        all_rows += rows
    total = sum(P72_JOBS.values())
    ok = sum(r["status"] == PASS for r in all_rows)
    fail = sum(r["status"] == FAIL for r in all_rows)
    no_sofa = sum(1 for r in all_rows if r.get("no_sofa"))
    need = math.ceil(POOLED_MIN * total)
    pooled = PASS if ok >= need else (FAIL if fail > total - need else INCOMPLETE)
    if no_sofa > 2:
        pooled = FAIL if pooled == FAIL else INCOMPLETE
    st = _combine([pooled] + [g["status"] for g in by_job.values()])
    first = {job: _stats([r.get("nights_to_modify") for r in g["rows"]]) for job, g in by_job.items()}
    return {"id": "P7-2", "gated": True, "status": st,
            "title": "A moved sofa is a modify, not a new zone (the move jobs, pooled at least 90 percent and 4 of 5 per job)",
            "details": {"by_job": by_job, "reported": {"nights_from_the_move_to_the_first_modify": first,
                                                     "design": "CD:pipeline/7: 1.1 m: 5-6 nights after the move; 2.9 m: nights 39-43; "
                                                               "CD:types/modify_zone: 4-8 nights"}},
            "counts": {"pooled": "%d of %d runs PASS (need %d)%s" % (ok, total, need, "; %d runs without a sofa zone" % no_sofa if no_sofa else ""),
                       **{job: "%s" % g["status"] for job, g in by_job.items()}}}


def _p74_row(res: dict) -> dict:
    v = RunView(res)
    md = int(v.js.get("move_day") or 35)
    sofa = _sofa_zone(v, md - 1) if v.has_p7() else None
    if sofa is None:
        return _row(res, INCOMPLETE, reason="no sofa zone before the move")
    n_ans = v.first_applied("mod-" + sofa["id"])
    if n_ans is None:
        return _row(res, INCOMPLETE, reason="no applied modify answer for the sofa zone")
    if n_ans > MF_LAST_APPROVE or v.last < n_ans + MF_NIGHTS:
        return _row(res, INCOMPLETE, approve_night=n_ans, reason="the approve is at night %d and the run ends at night %d: the %d nights "
                                                                "after it are not all there" % (n_ans, v.last, MF_NIGHTS))
    z0 = v.zone(n_ans, sofa["id"])
    if z0 is None:
        return _row(res, INCOMPLETE, reason="the sofa zone is not in the record of the night of the approve")
    old = unrle(z0["cells_rle"])
    hits = []
    for n in range(n_ans + 1, n_ans + MF_NIGHTS + 1):
        for i in v.items(n):
            if i["type"] in PLACE_TYPES:
                s = v.sug(n).get(i["id"])
                if s and overlap_coef(unrle(s["cells_rle"]), old) >= OVERLAP_MIN:
                    hits.append((n, i["id"]))
    return _row(res, FAIL if hits else PASS, approve_night=n_ans, old_spot_items=hits[:4])


def _p7mvback(results: list) -> list:
    out = []
    for res in _runs(results, jn("p7mvback")):
        if not _usable(res):
            out.append(_bad(res))
            continue
        v = RunView(res)
        back = v.js.get("move_back")
        sofa = _sofa_zone(v, int(v.js.get("move_day") or 28) - 1) if v.has_p7() else None
        n_ans = v.first_applied("mod-" + sofa["id"]) if sofa else None
        if sofa is None or n_ans is None or back is None:
            out.append(_row(res, INCOMPLETE, reason="no sofa zone or no applied modify"))
            continue
        z0 = v.zone(n_ans, sofa["id"])
        if z0 is None:
            out.append(_row(res, INCOMPLETE, reason="the sofa zone is not in the record of the night of the approve"))
            continue
        old = unrle(z0["cells_rle"])
        first = None
        for n in v.order:
            if n >= back and any(i["type"] in PLACE_TYPES and overlap_coef(unrle((v.sug(n).get(i["id"]) or {}).get("cells_rle") or []), old) >= OVERLAP_MIN
                                 for i in v.items(n)):
                first = n
                break
        out.append(_row(res, REPORTED, approve_night=n_ans, sofa_back_day=back, old_spot_offered_night=first,
                        nights_after_return=(first - back) if first is not None else None, last_night=v.last))
    return out


def crit_p74(results: list) -> dict:
    by_job, all_rows = {}, []
    for job, expected in P72_JOBS.items():
        rows = [_p74_row(r) if _usable(r) else _bad(r) for r in _runs(results, job)]
        by_job[job] = {"expected": expected, "rows": rows, "status": _gate(rows, expected)}
        all_rows += rows
    return {"id": "P7-4", "gated": True, "status": _combine([g["status"] for g in by_job.values()]),
            "title": "Moved-from suppression: the old sofa spot is not offered as a new place in the 14 nights after the approve",
            "details": {"by_job": by_job, "p7mvback": _p7mvback(results)},
            "counts": {job: g["status"] for job, g in by_job.items()}}


# ---------------------------------------------------------------- P7-3
def _bed_refs(v: RunView, zid: str) -> list:
    """Items that refer to the bed zone: a modify or retire of it, a merge that names it as the target; a radar_moved item anywhere."""
    out = []
    for n in v.order:
        for i in v.items(n):
            if (i["type"] in ("modify_zone", "retire_zone") and i.get("zid") == zid) or (i["type"] == "merge_into" and i.get("zid") == zid) \
                    or i["type"] == "radar_moved":
                out.append((n, i["id"]))
    return out


def _p73_bed_row(res: dict) -> dict:
    v = RunView(res)
    beds = {}
    for n in v.order:
        for z in v.zones(n):
            if z["kind"] == "bed":
                beds.setdefault(z["id"], n)
    if not beds:
        return _row(res, INCOMPLETE, reason="no bed zone")
    zid = min(beds, key=lambda k: beds[k])
    first = beds[zid]
    if first > BED_FIRST_NIGHT_MAX:
        return _row(res, INCOMPLETE, reason="the bed zone exists from night %d only (by night %d needed)" % (first, BED_FIRST_NIGHT_MAX))
    nights = [n for n in v.order if n >= first and v.zone(n, zid)]
    if len(nights) < BED_MIN_NIGHTS:
        return _row(res, INCOMPLETE, reason="%d nights evaluated, %d needed" % (len(nights), BED_MIN_NIGHTS))
    unfrozen = [n for n in nights if not v.zone(n, zid)["frozen"]]
    flagged = [n for n in nights if v.drift(n, zid) and (v.drift(n, zid)["lo"] or v.drift(n, zid)["rt"])]
    problems = []
    if unfrozen:
        problems.append("ZoneView.frozen false on nights %s" % unfrozen[:6])
    if flagged:
        problems.append("lo or rt above 0 on nights %s" % flagged[:6])
    other = []
    for n in v.order:
        for i in v.items(n):
            if i["type"] in ("modify_zone", "retire_zone", "radar_moved") or (i["type"] == "merge_into" and i.get("zid") == zid):
                other.append((n, i["id"]))
    if other:
        problems.append("modify, merge, retire or radar_moved items: %s" % other[:4])
    return _row(res, FAIL if problems else PASS, bed=zid, first_night=first, nights=len(nights), problems=problems)


def _p73_nl_row(res: dict) -> dict:
    v = RunView(res)
    beds = {}
    for n in v.order:
        for z in v.records(n):
            if z["kind"] == "bed":
                beds.setdefault(z["id"], (n, z.get("night")))
    if not beds:
        return _row(res, INCOMPLETE, reason="no bed zone record")
    zid = min(beds, key=lambda k: beds[k][0])
    first, born = beds[zid]
    if first > BEDNL_FIRST_NIGHT_MAX:
        return _row(res, INCOMPLETE, reason="the bed zone exists from night %d only (by night %d needed)" % (first, BEDNL_FIRST_NIGHT_MAX))
    born = first if born is None else born
    judged = [n for n in v.order if n - born >= 7 and v.zone(n, zid)]
    if not judged:
        return _row(res, INCOMPLETE, reason="the zone is not yet 7 nights old at the end of the run")
    bad = [n for n in judged if (v.drift(n, zid) or {}).get("lo", 0) >= P7.flag_nights or (v.drift(n, zid) or {}).get("rt", 0) != 0]
    refs = _bed_refs(v, zid)
    frozen_cov = [n for n in judged if (v.drift(n, zid) or {}).get("vs") in ("frozen", "blind")]
    not_frozen = [n for n in judged if n not in frozen_cov]
    problems = []
    if bad:
        problems.append("lo >= %d or rt > 0 on nights %s" % (P7.flag_nights, bad[:6]))
    if refs:
        problems.append("items refer to the bed: %s" % refs[:4])
    return _row(res, FAIL if problems else PASS, bed=zid, born=born, nights_judged=len(judged), problems=problems,
                frozen_by_coverage_share=round(len(frozen_cov) / len(judged), 3), not_frozen_nights=not_frozen[:20])


def _p73_nf(results: list) -> list:
    out = []
    for res in _runs(results, jn("p7bednf")):
        if not _usable(res):
            out.append(_bad(res))
            continue
        v = RunView(res)
        beds = {}
        for n in v.order:
            for z in v.zones(n):
                if z["kind"] == "bed":
                    beds.setdefault(z["id"], n)
        if not beds:
            out.append(_row(res, INCOMPLETE, reason="no bed zone"))
            continue
        zid = min(beds, key=lambda k: beds[k])
        ds = [(n, v.drift(n, zid)) for n in v.order if v.drift(n, zid)]
        flag = next((n for n, d in ds if d["lo"] >= P7.flag_nights), None)
        out.append(_row(res, REPORTED, bed=zid, first_flag_night=flag, max_rt=max((d["rt"] for _, d in ds), default=0),
                        frozen_nights=sum(1 for n, d in ds if d["fz"]), nights=len(ds),
                        expected="not frozen and flagged (about night 16): the static predicate is why DV3 keeps the OR"))
    return out


def crit_p73(results: list) -> dict:
    bed = [_p73_bed_row(r) if _usable(r) else _bad(r) for r in _runs(results, jn("p7bed"))]
    nl = [_p73_nl_row(r) if _usable(r) else _bad(r) for r in _runs(results, jn("p7bednl"))]
    a = _gate(bed, 5, deterministic=True)
    b = _gate(nl, 5)
    return {"id": "P7-3", "gated": True, "status": _combine([a, b]),
            "title": "The bed under night-off: no flag (p7bed all seeds, p7bednl 4 of 5)",
            "details": {"p7bed": bed, "p7bednl": nl, "p7bednf": _p73_nf(results),
                        "reported": {"design": "CD:pipeline/7: the bed is frozen on 36-40 of 56 nights (64-71 percent) and never flagged"}},
            "counts": {"p7bed": a, "p7bednl": b}}


# ---------------------------------------------------------------- P7-5
def _table_zone(v: RunView, night: int) -> Optional[dict]:
    cand = [z for z in v.records(night) if _dist(z["cx"], z["cy"], *TABLE_SPOT) <= SOFA_RADIUS_M]
    return min(cand, key=lambda z: _dist(z["cx"], z["cy"], *TABLE_SPOT)) if cand else None


def _p75_row(res: dict) -> dict:
    v = RunView(res)
    rday = int((v.js.get("retire") or ["table", 14])[1])
    z = _table_zone(v, min(v.last, rday - 1)) if v.has_p7() else None
    if z is None:
        return _row(res, INCOMPLETE, reason="no zone record at the table before it was retired")
    zid = z["id"]
    n1 = next((n for n in v.order if n > rday and (v.drift(n, zid) or {}).get("rt", 0) >= 1), None)
    n_open = next((n for n in v.order if any(i["id"] == "ret-" + zid for i in v.items(n))), None)
    others = [(n, i["id"]) for n in v.order for i in v.items(n) if i["type"] == "retire_zone" and i.get("zid") != zid]
    mods = v.item_nights(lambda i: i["type"] == "modify_zone" and i.get("zid") == zid)
    if n1 is None or n_open is None:
        # the item is 'late' once the run is past the latest night it may open (3 nights after the 21st low verdict); a verdict that
        # never fell is a failure once the run is 10 nights past the retirement
        failed = (v.last >= rday + 10) if n1 is None else (v.last >= n1 + P7.retire_days + 2)
        return _row(res, FAIL if failed else INCOMPLETE, zid=zid, n1=n1, n_open=n_open,
                    reason="the retire item did not open (n1 %s, n_open %s, last night %d)" % (n1, n_open, v.last), other_retire=others[:3],
                    modify_nights=mods[:3])
    lag = n_open - n1
    rt = (v.drift(n_open, zid) or {}).get("rt")
    problems = []
    if not P7.retire_days - 1 <= lag <= P7.retire_days + 2:
        problems.append("the item opens %d nights after the first low verdict, not %d to %d" % (lag, P7.retire_days - 1, P7.retire_days + 2))
    if rt is None or rt < P7.retire_days:
        problems.append("rt is %s when the item opens, under %d" % (rt, P7.retire_days))
    if others:
        problems.append("a retire item for another zone: %s" % others[:3])
    if mods:
        problems.append("a modify item for the retired table zone at nights %s" % mods[:3])
    n_ans = v.first_applied("ret-" + zid)
    detail = {"zid": zid, "n1": n1, "n_open": n_open, "lag": lag, "rt_at_open": rt, "approve_night": n_ans, "other_retire": others[:3]}
    incomplete = []
    if n_ans is None:
        problems.append("the owner's approve was not applied")
    else:
        old = unrle(v.zone(n_ans, zid)["cells_rle"]) if v.zone(n_ans, zid) else frozenset()
        if n_ans + 1 > v.last:
            incomplete.append("the run ends at the night of the approve")
        else:
            if v.zone(n_ans + 1, zid) is not None:
                problems.append("the zone is still there after the approve")
            back = []
            for n in range(n_ans + 1, v.last + 1):
                for i in v.items(n):
                    if i["type"] in PLACE_TYPES:
                        s = v.sug(n).get(i["id"])
                        if s and overlap_coef(unrle(s["cells_rle"]), old) >= OVERLAP_MIN:
                            back.append((n, i["id"]))
            if back:
                problems.append("a plain place item over the old cells after the removal: %s" % back[:3])
            detail["nights_after_approve"] = v.last - n_ans
    if problems:
        return _row(res, FAIL, problems=problems, **detail)
    if incomplete:
        return _row(res, INCOMPLETE, reason="; ".join(incomplete), **detail)
    return _row(res, PASS, **detail)


def crit_p75(results: list) -> dict:
    rows = [_p75_row(r) if _usable(r) else _bad(r) for r in _runs(results, jn("p7ret"))]
    other = [r for r in rows if r.get("other_retire")]
    st = _gate(rows, 5)
    if other:
        st = FAIL                                          # no retire item for any other zone in any run (deterministic)
    return {"id": "P7-5", "gated": True, "status": st,
            "title": "Retire after 21 covered days (the table retired on day 14): the item opens 20 to 23 nights after the first low verdict",
            "details": {"seeds": rows},
            "counts": {"seeds": "%d of 5 seeds PASS (need %d)" % (sum(r["status"] == PASS for r in rows), _need(5))}}


# ---------------------------------------------------------------- P7-6
def _radar_item(i: dict) -> bool:
    return i["type"] == "radar_moved" or i["id"] == "radar_moved"


def _radar_row(res: dict, latch: bool = True) -> dict:
    """The detection of one radar run: the item opens at night 30 (or 31), never before, and (``latch``: no answer is taken) stays open
    to the last night."""
    v = RunView(res)
    if not v.has_p7() or v.last < RADAR_LAST_NIGHT:
        return _row(res, INCOMPLETE, reason="the run has %d nights, %d needed" % (v.last, RADAR_LAST_NIGHT))
    nights = v.item_nights(_radar_item)
    first = min(nights) if nights else None
    before = [n for n in nights if n < RADAR_OPEN_NIGHT]
    latched = bool(nights) and all(n in nights for n in range(first, v.last + 1))
    dbg = {n: v.p7(n).get("radar_dbg") for n in range(RADAR_OPEN_NIGHT - 3, RADAR_OPEN_NIGHT + 3) if v.p7(n).get("radar_dbg")}
    held = [n for n in v.order if (v.p7(n).get("rm") or {}).get("hold")]
    detail = {"first_open_night": first, "latched_to_end": latched, "hold_nights": [min(held), max(held)] if held else None,
              "radar_dbg": dbg}
    problems = []
    if before:
        problems.append("the item is open before night %d: %s" % (RADAR_OPEN_NIGHT, before[:3]))
    if first is None:
        problems.append("the item never opened")
    elif first > RADAR_OPEN_NIGHT + 1:
        problems.append("the item opens at night %d, after night %d" % (first, RADAR_OPEN_NIGHT + 1))
    if latch and first is not None and not latched:
        problems.append("the item closed again without an answer")
    detail["open_at_30"] = first == RADAR_OPEN_NIGHT
    return _row(res, FAIL if problems else PASS, problems=problems, **detail)


def _p76_detect(results: list, job: str) -> dict:
    rows = [_radar_row(r) if _usable(r) else _bad(r) for r in _runs(results, job)]
    at30 = sum(1 for r in rows if r.get("open_at_30") and r["status"] == PASS)
    st = _gate(rows, 5, deterministic=True)
    if st == PASS and at30 < _need(5):
        st = FAIL
    return {"status": st, "rows": rows, "open_at_30": "%d of %d seeds at night %d (need %d)" % (at30, len(rows), RADAR_OPEN_NIGHT, _need(5)),
            "opening_nights": {r["seed"]: r.get("first_open_night") for r in rows}}


def _p76_none(results: list, job: str) -> dict:
    rows = []
    for res in _runs(results, job):
        if not _usable(res):
            rows.append(_bad(res))
            continue
        v = RunView(res)
        nights = v.item_nights(_radar_item)
        rows.append(_row(res, FAIL if nights else (PASS if v.has_p7() else INCOMPLETE), open_nights=nights[:5],
                         dbg={n: v.p7(n).get("radar_dbg") for n in range(RADAR_OPEN_NIGHT - 2, RADAR_OPEN_NIGHT + 2) if v.p7(n).get("radar_dbg")}))
    return {"status": _gate(rows, 5, deterministic=True), "rows": rows}


def _expected_shift(cx: float, cy: float, deg: float = 10.0) -> float:
    return 2.0 * math.hypot(cx, cy) * math.sin(math.radians(deg / 2.0))


def _reset_row(res: dict) -> dict:
    v = RunView(res)
    base = _radar_row(res, latch=False)
    resets = (v.p7res.get("resets") or [])
    if not resets:
        return _row(res, base["status"] if base["status"] != PASS else INCOMPLETE, reason="no approve of radar_moved was taken", detection=base)
    r0 = resets[0]
    n = r0["night"]
    problems = []
    if r0.get("grids_nonzero") != 0:
        problems.append("%s grid cells are not zero right after the reset" % r0.get("grids_nonzero"))
    if r0.get("candidates") != 0:
        problems.append("%s candidates right after the reset" % r0.get("candidates"))
    if n + 1 > v.last:
        return _row(res, INCOMPLETE, reason="the run ends at the night of the approve", reset=r0)
    after = v.zones(n + 1)
    before = v.zones(n)
    if len(after) < len(before):
        problems.append("a zone was removed by the reset: %d -> %d" % (len(before), len(after)))
    drift = {d["id"]: d for d in v.p7(n + 1).get("drift") or []}
    not_rc = [z["id"] for z in after if not (drift.get(z["id"]) or {}).get("rc")]
    if not_rc:
        problems.append("zones without 'needs re-check' after the reset: %s" % not_rc[:4])
    cov0 = ((v.nights.get(n) or {}).get("p5") or {}).get("status", {}).get("covered_days")
    cov1 = ((v.nights.get(n + 1) or {}).get("p5") or {}).get("status", {}).get("covered_days")
    if cov0 is not None and cov1 is not None and cov1 < cov0:
        problems.append("the coverage table shrank: %s -> %s covered days" % (cov0, cov1))
    gone = [m for m in range(n + 1, v.last + 1) if any(_radar_item(i) for i in v.items(m))]
    if gone:
        problems.append("the item is open again at nights %s" % gone[:3])
    upto = min(v.last, n + RESET_FOLLOW_NIGHTS)
    targets, shifts = [], []
    for z in before:
        if not z["id"].startswith("z") or z["kind"] == "bed":
            continue
        exp = _expected_shift(z["cx"], z["cy"])
        if exp < RESET_DISPLACEMENT_MIN_M:
            continue
        mods = [i for m in range(n + 1, upto + 1) for i in v.items(m) if i["type"] == "modify_zone" and i.get("zid") == z["id"]]
        shift = mods[0].get("shift_cm") if mods else None
        ok = bool(mods) and shift is not None and abs(shift / 100.0 - exp) <= RESET_SHIFT_TOL_M
        targets.append({"zid": z["id"], "expected_cm": round(exp * 100), "shift_cm": shift, "ok": ok})
        shifts.append(ok)
    share = (sum(shifts) / len(shifts)) if shifts else None
    if share is None:
        return _row(res, INCOMPLETE, reason="no non-bed zone record is displaced by %.1f m or more" % RESET_DISPLACEMENT_MIN_M, reset=r0)
    if upto < n + RESET_FOLLOW_NIGHTS and share < RESET_SHARE_MIN:
        return _row(res, INCOMPLETE, reason="only %d of the %d follow-up nights are there" % (upto - n, RESET_FOLLOW_NIGHTS), reset=r0,
                    reanchored=targets)
    if share < RESET_SHARE_MIN:
        problems.append("%.0f percent of the displaced zones have a modify item at the expected shift, under %.0f" % (
            100 * share, 100 * RESET_SHARE_MIN))
    return _row(res, FAIL if (problems or base["status"] == FAIL) else PASS, problems=problems + base.get("problems", []), reset=r0,
                reanchored=targets, reanchored_share=round(share, 3), detection=base)


def _p76_opt(results: list) -> list:
    out = []
    for job in (jn("p7rot6"), jn("p7shift5")):
        for res in _runs(results, job):
            if not _usable(res):
                out.append(_bad(res))
                continue
            v = RunView(res)
            nights = v.item_nights(_radar_item)
            out.append(_row(res, REPORTED, first_open_night=min(nights) if nights else None, last_night=v.last,
                            dbg={n: v.p7(n).get("radar_dbg") for n in range(RADAR_OPEN_NIGHT - 2, RADAR_OPEN_NIGHT + 3)
                                 if v.p7(n).get("radar_dbg")}))
    return out


def crit_p76(results: list) -> dict:
    rot, shift = _p76_detect(results, jn("p7rot")), _p76_detect(results, jn("p7shift"))
    rot3 = _p76_none(results, jn("p7rot3"))
    reset_rows = [_reset_row(r) if _usable(r) else _bad(r) for r in _runs(results, jn("p7reset"))]
    reset = {"status": _gate(reset_rows, 5), "rows": reset_rows}
    parts = {"p7rot": rot["status"], "p7shift": shift["status"], "p7rot3": rot3["status"], "p7reset": reset["status"]}
    return {"id": "P7-6", "gated": True, "status": _combine(list(parts.values())),
            "title": "Radar rotated or shifted: a reset is suggested at night 30, stays open until answered; 3 degrees is not flagged; the reset",
            "details": {"p7rot": rot, "p7shift": shift, "p7rot3": rot3, "p7reset": reset, "reported": _p76_opt(results)},
            "counts": parts}


# ---------------------------------------------------------------- P7-7
def _offered(v: RunView) -> list:
    out = []
    for n in v.order:
        for i in v.items(n):
            lab = v.label_of(n, i)
            if lab in FP_LABELS:
                out.append((n, i["id"], i["type"], lab))
    return out


def _seat_first_offered(v: RunView) -> dict:
    first = {}
    for n in v.order:
        for i in v.items(n):
            if i["type"] in PLACE_TYPES:
                lab = v.label_of(n, i)
                if lab.startswith("TP:"):
                    first.setdefault(lab[3:], n)
    return first


def crit_p77(results: list) -> dict:
    by_job = {}
    for job in (jn("p7guest"), jn("p7guestp")):
        rows = []
        for res in _runs(results, job):
            if not _usable(res):
                rows.append(_bad(res))
                continue
            v = RunView(res)
            if not v.has_p7() or v.last < 28:
                rows.append(_row(res, INCOMPLETE, reason="the run has %d nights" % v.last))
                continue
            off = _offered(v)
            rows.append(_row(res, FAIL if off else PASS, offered=off[:4], nights=v.last))
        by_job[job] = {"rows": rows, "status": _gate(rows, 5, deterministic=True)}
    np_rows = []
    for res in _runs(results, jn("p7guestnp")):
        if not _usable(res):
            np_rows.append(_bad(res))
            continue
        v = RunView(res)
        off = _offered(v)
        np_rows.append(_row(res, REPORTED, offered=off[:6], first_offered_night=off[0][0] if off else None, last_night=v.last))
    seats = {}
    for job in (jn("p7ctl"), jn("p7ctlnp")):
        seats[job] = {r["seed"]: _seat_first_offered(RunView(r)) for r in _runs(results, job) if _usable(r)}
    return {"id": "P7-7", "gated": True, "status": _combine([g["status"] for g in by_job.values()]),
            "title": "A one-week guest and a weekly visitor are never suggested (p7guest and p7guestp, all seeds)",
            "details": {"by_job": by_job, "p7guestnp": np_rows,
                        "reported": {"first_night_each_real_seat_is_offered": seats,
                                     "note": "p7ctl has the probation rule, p7ctlnp has not: the cost of DV6 on a genuinely new seat"}},
            "counts": {job: g["status"] for job, g in by_job.items()}}


# ---------------------------------------------------------------- P7-8
def _kinds(v: RunView, typ: str) -> list:
    return [(n, i["id"]) for n in v.order for i in v.items(n) if i["type"] == typ]


def crit_p78(results: list) -> dict:
    rows, evaluated, violations = [], 0, []
    merges, falling = {}, {}
    for job, expected in P78_NOMOD_JOBS.items():
        for res in _runs(results, job):
            if not _usable(res):
                rows.append(_bad(res))
                continue
            v = RunView(res)
            if not v.has_p7():
                rows.append(_row(res, INCOMPLETE, reason="no P7 record"))
                continue
            evaluated += 1
            mods, rets, rads = _kinds(v, "modify_zone"), _kinds(v, "retire_zone"), [(n, i["id"]) for n in v.order for i in v.items(n) if _radar_item(i)]
            table = _table_zone(v, min(v.last, 13)) if job == jn("p7ret") else None
            rets_bad = [x for x in rets if not (table is not None and x[1] == "ret-" + table["id"])]
            bad = {}
            if mods:
                bad["modify"] = mods[:3]
            if rets_bad:
                bad["retire"] = rets_bad[:3]
            if rads:
                bad["radar_moved"] = rads[:3]
            rows.append(_row(res, FAIL if bad else PASS, nights=v.last, **{"violations": bad}))
            if bad:
                violations.append((res["job"], res["seed"], sorted(bad)))
            merges[(res["job"], res["seed"])] = len(_kinds(v, "merge_into"))
            fz = {}
            for n in v.order:
                for d in v.p7(n).get("drift") or []:
                    if d["vs"] in ("ok", "falling", "collapsed") and not d["fz"]:
                        e = fz.setdefault(d["id"], [0, 0])
                        e[0] += 1
                        e[1] += d["vs"] == "falling"
            falling[(res["job"], res["seed"])] = {k: round(b / a, 2) for k, (a, b) in fz.items() if a}
    other = []
    for res in results:
        if not is_p7_run(res) or not _usable(res) or res.get("job") in P78_NOMOD_JOBS:
            continue
        v = RunView(res)
        script = v.script
        rads = [(n, i["id"]) for n in v.order for i in v.items(n) if _radar_item(i)]
        if rads and script not in P78_RADAR_ALLOWED:
            other.append((res["job"], res["seed"], "radar_moved", rads[:2]))
        if script in ("p7mv11", "p7mv29", "p7mv11e", "p7mv29e", "p7mvback") and v.has_p7():
            md = int(v.js.get("move_day") or 35)
            sofa = _sofa_zone(v, md - 1)
            if sofa is not None and v.item_nights(lambda i, z=sofa["id"]: i["type"] == "retire_zone" and i.get("zid") == z):
                other.append((res["job"], res["seed"], "the moved sofa zone has a retire item", []))
    ctl_bad = [x for x in violations if x[0] in P78_CTL_JOBS]
    st = FAIL if (violations or other) else (PASS if evaluated >= GATED_MIN_RUNS else INCOMPLETE)
    return {"id": "P7-8", "gated": True, "status": st,
            "title": "No false modify, retire or radar-moved item (the 54 runs of the gated set; every other P7 run for the radar item)",
            "details": {"rows": rows, "evaluated_runs": evaluated, "violations": violations, "ctl_violations": ctl_bad, "other_runs": other,
                        "reported": {"merge_items_per_run": {"%s s%s" % k: n for k, n in sorted(merges.items())},
                                     "falling_share_of_unfrozen_nights": {"%s s%s" % k: v for k, v in sorted(falling.items())}}},
            "counts": {"runs": "%d of the 54 runs evaluated (at least %d needed)" % (evaluated, GATED_MIN_RUNS),
                       "violations": str(len(violations) + len(other))}}


# ---------------------------------------------------------------- P7-9
TUNE_JOBS = (jn("p7tune"), jn("p7tuneh300"), jn("p7tuneh900"))


def _p79_run(res: dict) -> dict:
    from radar_zone_learner.core import tuningagg
    lines = list((res.get("p7") or {}).get("tuning") or [])
    if not lines:
        return _row(res, INCOMPLETE, reason="the run has no tuning line")
    problems = []
    for k, text in enumerate(lines):
        bad = tuningagg.scan(text)
        if bad:
            problems.append("line %d: %s" % (k, bad[:2]))
            break
        if len(text.encode("utf-8")) > P7.agg_night_bytes_max:
            problems.append("line %d is %d bytes" % (k, len(text.encode("utf-8"))))
            break
    pooled = tuningagg.pool(lines[-EXPORT_LINES:])
    bad = tuningagg.scan(pooled)
    if bad:
        problems.append("pooled record: %s" % bad[:2])
    size = len(json.dumps(pooled, separators=(",", ":")).encode("utf-8"))
    if size > P7.agg_export_bytes_max:
        problems.append("the pooled record is %d bytes" % size)
    try:
        rep = TU.propose(pooled)
    except ValueError as exc:
        return _row(res, FAIL, problems=problems + ["tune.py refused the pooled record: %s" % exc])
    missing = [r["key"] for r in rep["rows"] if r["proposal"] is None and not r["reason"]]
    if missing:
        problems.append("rows without a proposal or a reason: %s" % missing)
    bridge = next(r for r in rep["rows"] if r["key"] == "bridge_gap_s")
    spells = sum(pooled["sum"].get("hs") or [])
    return _row(res, FAIL if problems else PASS, problems=problems, lines=len(lines), pooled_bytes=size, spells=spells,
                covered_dates=rep["covered_dates"], bridge_gap_s=bridge["proposal"], bridge_reason=bridge["reason"],
                delay_default_s=next(r for r in rep["rows"] if r["key"] == "delay_default_s")["proposal"],
                proposals={r["key"]: r["proposal"] for r in rep["rows"] if r["proposal"] is not None})


def crit_p79(results: list) -> dict:
    by_job = {job: [_p79_run(r) if _usable(r) else _bad(r) for r in _runs(results, job)] for job in TUNE_JOBS}
    rows = [r for rs in by_job.values() for r in rs]
    privacy = _combine([r["status"] for r in rows] + ([INCOMPLETE] if len(rows) < 9 else []))
    seeds = {}
    for job, rs in by_job.items():
        for r in rs:
            seeds.setdefault(r["seed"], {})[job] = r
    cmp_rows = []
    for seed in sorted(seeds):
        got = [seeds[seed].get(j) for j in TUNE_JOBS]
        if any(g is None or g["status"] == INCOMPLETE for g in got):
            cmp_rows.append({"seed": seed, "status": INCOMPLETE, "reason": "a job of the three is missing or not usable"})
            continue
        b = [g.get("bridge_gap_s") for g in got]
        if any(x is None for x in b):
            cmp_rows.append({"seed": seed, "status": INCOMPLETE, "bridge_gap_s": b,
                             "reason": "no bridge_gap_s proposal: %s" % [g.get("bridge_reason") for g in got]})
            continue
        ok = b[0] <= b[1] <= b[2] and b[0] != b[2]
        cmp_rows.append({"seed": seed, "status": PASS if ok else FAIL, "bridge_gap_s": b,
                         "spells": [g.get("spells") for g in got]})
    cmp_status = _gate(cmp_rows, 3, deterministic=True)       # the k-of-n rule of 3 seeds: all of them
    st = _combine([privacy, cmp_status]) if rows else INCOMPLETE
    return {"id": "P7-9", "gated": True, "status": st,
            "title": "The tuning lines carry no private value and the tuner proposes bridge_gap_s upward with the hidden-spell mean (p7tune, h300, h900)",
            "details": {"by_job": by_job, "bridge_gap_comparison": cmp_rows,
                        "note": "the P6 spells are right-censored by the delay in force, so the values saturate: reported, not compared with the true q95"},
            "counts": {"privacy and rows": privacy, "bridge_gap_s not decreasing": "%s (%d of 3 seeds PASS)" % (
                cmp_status, sum(r["status"] == PASS for r in cmp_rows))}}


# ---------------------------------------------------------------- P7-10 (reported) and the run list
def rep_p710(results: list) -> dict:
    rows = []
    for res in _runs(results, jn("p7ctl")):
        if not _usable(res):
            rows.append(_bad(res))
            continue
        v = RunView(res)
        m = _kinds(v, "merge_into")
        rows.append(_row(res, REPORTED, merge_nights=sorted({n for n, _ in m})[:10], merge_items=len({i for _, i in m})))
    return {"id": "P7-10", "gated": False, "status": REPORTED,
            "title": "The merge detector in a control run (reported: a merge is not a false alarm by itself; the rules are unit tests)",
            "details": {"seeds": rows}}


def rep_runs(results: list) -> dict:
    rows = []
    for r in sorted([x for x in results if is_p7_run(x)], key=lambda x: (x.get("job", ""), x["seed"])):
        v = RunView(r)
        p7 = r.get("p7") or {}
        final = {}
        if v.order:
            last = v.p7(v.last)
            final = {"zones": [[z["id"], z["kind"], z["cx"], z["cy"]] for z in last.get("zones") or []],
                     "status": last.get("status"), "errors": last.get("errors")}
        rows.append({"job": r.get("job"), "seed": r.get("seed"), "status": r.get("status"), "core_errors": r.get("core_errors"),
                     "nights": v.last, "tuning_lines": len(p7.get("tuning") or []), "answers": len(v.answers()),
                     "resets": len(p7.get("resets") or []), "final": final})
    return {"id": "P7-runs", "title": "What each run ended with (zones, P7 status counts, errors)", "status": REPORTED, "details": rows}


# ---------------------------------------------------------------- entry
def evaluate_p7(results: list) -> Optional[dict]:
    """The P7 part of the report, or None when no run carries a P7 scenario."""
    if not any(is_p7_run(r) for r in results):
        return None
    criteria = [crit_p71(results), crit_p72(results), crit_p73(results), crit_p74(results), crit_p75(results), crit_p76(results),
                crit_p77(results), crit_p78(results), crit_p79(results)]
    reported = [rep_p710(results), rep_runs(results)]
    return {"runs": sum(1 for r in results if is_p7_run(r)), "criteria": criteria, "reported": reported,
            "gate": _combine([c["status"] for c in criteria]), "failed": [c["id"] for c in criteria if c["status"] == FAIL],
            "unit_evidence": [list(x) for x in UNIT_EVIDENCE], "other_commands": [list(x) for x in OTHER_COMMANDS],
            "interpretation": INTERPRETATION}


def _dump(x, limit: int) -> str:
    t = json.dumps(x, sort_keys=True, separators=(",", ":"))
    return t if len(t) <= limit else t[:limit] + " ..."


def _seed_lines(rows: list, limit: int = 900) -> list:
    out = []
    for x in rows:
        keep = {k: v for k, v in x.items() if k not in ("job", "seed", "status")}
        out.append("  - %s s%s: **%s** `%s`" % ((x.get("job") or "").replace("bed_", "", 1), x.get("seed"), x["status"], _dump(keep, limit)))
    return out


def render_p7(p7: dict) -> list:
    """Markdown lines of the P7 section of the report."""
    L = ["", "## P7: drift, moves, retire, radar moved, tuning", "",
         "Gate (the replay-evaluated checks P7-1 to P7-9): **%s**%s. P7 runs: %d." % (
             p7["gate"], (" (failed: %s)" % ", ".join(p7["failed"])) if p7["failed"] else "", p7["runs"]),
         "", "| ID | Status | Check |", "|---|---|---|"]
    for c in p7["criteria"]:
        L.append("| %s | %s | %s |" % (c["id"], c["status"], c["title"]))
    L += ["", "G1-G6, P4-1 to P4-7, P5-1 to P5-8 and P6-1 to P6-15 were **not run** in this invocation (P7-R1 compares the P5 and P6 jobs with "
          "enabled=False against their baselines).", ""]
    for c in p7["criteria"]:
        L += ["", "### %s: %s" % (c["id"], c["status"]), "", c["title"], ""]
        d = c["details"]
        for k, v in (c.get("counts") or {}).items():
            L.append("- %s: %s" % (k, v))
        L.append("")
        for k, v in d.items():
            if k == "seeds":
                L += _seed_lines(v)
            elif k == "by_job":
                for job, g in v.items():
                    rows = g["rows"] if isinstance(g, dict) and "rows" in g else g
                    st = g.get("status") if isinstance(g, dict) else ""
                    L.append("- %s %s" % (job, ("(%s)" % st) if st else ""))
                    L += _seed_lines(rows, 700)
            elif isinstance(v, dict) and "rows" in v:
                L.append("- %s: **%s** %s" % (k, v["status"], _dump({kk: vv for kk, vv in v.items() if kk not in ("rows", "status")}, 400)))
                L += _seed_lines(v["rows"], 800)
            elif isinstance(v, list) and v and isinstance(v[0], dict) and "status" in v[0]:
                L.append("- %s" % k)
                L += _seed_lines(v, 800)
            else:
                L.append("- %s: `%s`" % (k, _dump(v, 1800)))
        L.append("")
    L += ["## P7 reported", ""]
    for c in p7["reported"]:
        L += ["### %s" % c["id"], "", c["title"], ""]
        det = c["details"]
        rows = det["seeds"] if isinstance(det, dict) and "seeds" in det else det
        for row in rows:
            L.append("- %s s%s %s: `%s`" % (row.get("job"), row.get("seed"), row.get("status"),
                                            _dump({k: v for k, v in row.items() if k not in ("job", "seed", "status")}, 600)))
        L.append("")
    L += ["## Evidence: unit tests", "", "| Check | What | Where |", "|---|---|---|"]
    for a, b, c in p7["unit_evidence"]:
        L.append("| %s | %s | %s |" % (a, b, c))
    L += ["", "## Separate commands", "", "| Check | What | Command |", "|---|---|---|"]
    for a, b, c in p7["other_commands"]:
        L.append("| %s | %s | `%s` |" % (a, b, c))
    L += ["", "## P7 interpretations", ""] + ["- " + t for t in p7["interpretation"]] + [""]
    return L


def build_report(results: list, unit_g5b: Optional[bool] = None) -> dict:
    """``report.json`` content of an invocation that has P7 runs. Runs that are not P7 runs are given to the P6, P5 or P3/P4 report and the
    P7 section is attached to it; without any other run it is a pure P7 report."""
    from . import evaluate as E
    p7res = [r for r in results if is_p7_run(r)]
    others = [r for r in results if not is_p7_run(r)]
    p7 = evaluate_p7(p7res)
    summaries = [E.summarize_run(r) for r in p7res]
    bad = [{"job": s["job"], "seed": s["seed"], "status": s["status"], "core_errors": s["core_errors"],
            "first_error": s["first_error"]} for s in summaries if s["status"] != "ok" or s["core_errors"]]
    if others:
        from . import evaluate_p5 as E5
        from . import evaluate_p6 as E6
        if any(E6.is_p6_run(r) for r in others):
            report = E6.build_report(others, unit_g5b=unit_g5b)
        elif any(E5.is_p5_run(r) or E5.is_rerun(r) for r in others):
            report = E5.build_report(others, unit_g5b=unit_g5b)
        else:
            report = E.evaluate(others, unit_g5b=unit_g5b)
        report["p7"] = p7
        failed = report["failed"] + p7["failed"]
        report["failed"] = failed
        report["gate"] = _combine([report["gate"], p7["gate"]])
        report["bad_runs"] = report["bad_runs"] + bad
        report["exit_code"] = 1 if failed or any(b["status"] != "ok" for b in report["bad_runs"]) else 0
        report["job_matrix"] = report["job_matrix"] + E.job_matrix(summaries)
        return report
    params = sorted({r.get("params") for r in p7res if r.get("params")})
    return {"schema": 1, "kind": "p7", "runs": len(p7res), "params_digests": params, "gate": p7["gate"], "failed": p7["failed"],
            "scope": SCOPE_P7, "job_matrix": E.job_matrix(summaries),
            "exit_code": 1 if p7["failed"] or any(b["status"] != "ok" or b["core_errors"] for b in bad) else 0, "bad_runs": bad,
            "criteria": [], "reported": [], "p7": p7}


def render_markdown(report: dict) -> str:
    from . import evaluate as E
    p7 = report.get("p7")
    if report.get("kind") != "p7":
        if report.get("p6"):
            from . import evaluate_p6 as E6
            text = E6.render_markdown(report)
        elif report.get("p5"):
            from . import evaluate_p5 as E5
            text = E5.render_markdown(report)
        else:
            text = E.render_markdown(report)
        return text + "\n".join(render_p7(p7)) + "\n" if p7 else text
    L = ["# P7 replay report (G1-G6, P4-1 to P4-7, P5-1 to P5-8 and P6 not run)", "",
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
            L.append("- %s seed %s: %s, core errors %s%s" % (b["job"], b["seed"], b["status"], b["core_errors"],
                                                          (": " + str(b["first_error"])[:300]) if b.get("first_error") else ""))
    return "\n".join(L + render_p7(p7)) + "\n"


def write_report(report: dict, out_dir) -> None:
    """``report.json`` and ``report.md`` in ``out_dir``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, sort_keys=False), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
