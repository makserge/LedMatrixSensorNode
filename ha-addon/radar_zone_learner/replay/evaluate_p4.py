"""Evaluation of the P4 replay checks (OA 12 'P4 Night-window proposal, bed item, optional Night learning, state
semantics', Verification, Replay) from replay run results.

The design's Replay bullets, numbered in their order (every check is gated when its runs are present):

* **P4-1** When onsets are observable (00:00-06:00: both edges, 277 of 280 simulated nights; the replay keeps the
  3 onsets after 00:00 that the simulator drops, in the truth and in the lux proxy), the proposal is within 30 min
  of the true 90th percentile of the onsets (start) and the true 10th percentile of the wakes (end).
* **P4-2** Otherwise (22:00-06:00, 23:00-07:00) the proposal is labelled 'from lights-off times' (the lux proxy:
  fewer than 7 radar-observable onsets) and its start is still within 30 min of the true percentile.
* **P4-3** Default bed method: 'Mark spot here' from the bed item, in a replayed stay in bed outside the window,
  creates a zone of kind ``bed``, withdraws the item and turns on the OA 7.4 freeze at once; approving or renaming a
  suggested place as 'Bed' does the same.
* **P4-4** 'Mark spot here' binding and failures: with another item selected the press gives kind ``manual``; with
  the radars off, with no stay of >= 20 s, with two people in view it creates nothing (reason ``not_observing``,
  ``no_stay``, ``several_stays``) and leaves the bed item open; the names 'bett', 'BED', ' Bed ' on approval give
  kind ``bed``, other names do not.
* **P4-5** Night learning is off by default: with nothing started the learner sends no schedule command, on a fresh
  install and after the setup review, so there are no extra radar-on nights; started on a node without P2 it sends
  nothing either.
* **P4-6** When started, Night learning yields the bed by night 8 in >= 4/5 seeds under every window; its commands
  are the schedule OFF at the start and ON at the window end of the 7th night, confirmed by the node's echo.
* **P4-7** bed_needs_data with Night learning off and without 'Mark spot here': withdrawn exactly while a zone of
  kind ``bed`` exists or a place named 'bed' is suggested, shown otherwise, back when that suggestion is rejected;
  reported: in how many seeds and on which night the learner names the bed place 'bed' by itself; approving the
  place at the bed as 'Bed' withdraws the item and turns on the freeze.
* The two Home Assistant bullets are not replay checks (see ``HA_NOTE``): the 'cleared for 15 min' trigger is NOT
  verified here (it needs a running Home Assistant; the blueprint has no such trigger and its test reads text only),
  'the schedule toggle never overriding the user' is covered by unit tests.

Status per check: PASS, FAIL, INCOMPLETE (runs missing, too short, or a step of the scenario could not be
exercised). Interpretations are listed in ``INTERPRETATION`` and written into the report.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from .evaluate import FAIL, INCOMPLETE, PASS, REPORTED, _need, summarize_run

ALL_SEEDS = (1, 2, 3, 4, 5)
TOL_MIN = 30                  # P4-1, P4-2: within 30 min
MIN_RADAR_ONSETS = 7          # nw_min_nights: fewer radar-observable onsets -> the lux proxy
FULL_NIGHTS = 14
BED_BY_NIGHT = 8              # P4-6
NL_NIGHTS = 7
RESTORE_SLACK_MS = 30 * 60 * 1000
DAY_MS = 86400 * 1000

HA_NOTE = ("Not replay checks. (1) The 'cleared for 15 min' trigger with not_from ['unavailable', 'unknown'] is NOT "
           "verified here: it needs a running Home Assistant. The blueprint (blueprints/radar_zone_context.yaml) "
           "contains no such trigger (it only publishes the four context topics), tests/test_blueprint.py checks the "
           "blueprint text only, and README 9.2 merely documents the trigger form. (2) 'The learner's schedule toggle "
           "never overrides a change made by the user' is covered by unit tests (tests/test_nightlearn.py, "
           "tests/test_p4_integration.py): an echo of ON while Night learning runs ends it without a command, and a "
           "restore is refused with changed_by_user.")

INTERPRETATION = [
    "True percentiles are taken over the realised true onsets of nights 0..N-1 and wakes of nights -1..N-2 of the "
    "run (what the learner could have seen by night N), with the learner's own percentile rule (linear "
    "interpolation), censored nights included.",
    "A true onset is radar-observable when the radars observe from 90 s before to 15.5 min after it (the 15 quiet "
    "minutes); a wake when they observe from 30 s before to 11 min after it.",
    "Lights go off 0-4 min after the sleeper reaches the bed and on 0-5 min after the wake (BH1750 every 10 s, "
    "lit 150 lx, dark 0.5-2.5 lx); no lux is known before the first wake of day 0.",
    "The simulator has one sleeper: 'two people in bed' is two scripted people (X1, X2) at the bed from 21:00 to "
    "21:25 of day 8 in the p4mark runs only.",
    "Night learning is started in the first second of the run; the radar timeline is the precomputed one with "
    "nl=7 and is checked against the node's own log of the learner's commands (consistent).",
    "'Mark spot here' with the bed item selected is pressed by a persistent user in P1's stays in bed outside the "
    "window (every 30 s from 60 s into a stay, while the radars observe).",
    "The lux samples are derived from the truth (an ideal lux model): dark 0.5-2.5 lx from the true onset plus 0-4 min "
    "to the true wake plus 0-5 min, 150 lx otherwise, with no daytime dimming, panel glow or other light sources. "
    "The lux onset errors (3-4 min at most) and the start accuracy of P4-2 are therefore those of an ideal lux "
    "sensor: how the real BH1750 tracks sleep is the P0 measurement, still open.",
    "P4-1 is gated at the last night of the run (21): the design names no night. Night 14, where the first proposal "
    "reaches the setup review, is shown next to it and read with the same rule (see its summary line).",
    "The sub-checks of P4-3, P4-4 and P4-7 follow the P3 k-of-n rule scaled to the seeds present (4 of 5; all of them "
    "with fewer than 5): a step that was not exercised (INCOMPLETE) is not a pass, and the seeds behind every PASS "
    "are printed.",
]


# ---------------------------------------------------------------- helpers
def hhmm(nm: Optional[int]) -> Optional[str]:
    """Night-axis minutes (after 12:00) as clock time."""
    if nm is None:
        return None
    m = (int(nm) + 720) % 1440
    return "%02d:%02d" % (m // 60, m % 60)


def _pct(vals: list, q: float) -> Optional[float]:
    if not vals:
        return None
    v = sorted(vals)
    pos = (len(v) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def _p4(res: dict) -> dict:
    return res.get("p4") or {}


def _script(res: dict) -> str:
    return (res.get("job_spec") or {}).get("script") or ""


def _nights(res: dict) -> list:
    return sorted(res.get("nights") or [], key=lambda n: n["night"])


def _last(res: dict) -> Optional[dict]:
    n = _nights(res)
    return n[-1] if n else None


def _at(res: dict, night: int) -> Optional[dict]:
    return next((n for n in _nights(res) if n["night"] == night), None)


def _runs(results: list, window: str, script: str) -> list:
    return sorted([r for r in results if r.get("world") == "bed" and r.get("window") == window
                   and _script(r) == script], key=lambda r: r["seed"])


def _scripted(results: list, script: str) -> list:
    return sorted([r for r in results if _script(r) == script], key=lambda r: (r["job"], r["seed"]))


def _actor(res: dict, name: str) -> dict:
    return (_p4(res).get("actors") or {}).get(name) or {}


def _proposal(rec: Optional[dict]) -> Optional[dict]:
    if not rec:
        return None
    return (((rec.get("p4") or {}).get("night") or {}).get("proposal"))


def _complete(group: list, min_nights: int) -> bool:
    return (sorted(r["seed"] for r in group) == list(ALL_SEEDS)
            and all(r.get("status") == "ok" and (_last(r) or {"night": 0})["night"] >= min_nights for r in group))


def _status(ok: bool, group: list, complete: bool) -> str:
    """FAIL/PASS with the P3 convention: an incomplete group fails only when all 5 seeds ran."""
    if ok and complete:
        return PASS
    if not ok and (complete or len(group) == 5):
        return FAIL
    return INCOMPLETE


def _sub(rows: list) -> tuple:
    """(status, passed, of) of the rows of one sub-check: FAIL when any row fails, PASS when at least
    ``_need(len(rows))`` rows pass (the P3 k-of-n rule, scaled: all of them with fewer than 5 seeds), else INCOMPLETE.
    A row that was not exercised is no pass."""
    sts = [x["status"] for x in rows]
    n, passed = len(rows), sts.count(PASS)
    if FAIL in sts:
        return FAIL, passed, n
    return (PASS if rows and passed >= _need(n) else INCOMPLETE), passed, n


def _counts(parts: dict) -> tuple:
    """(statuses, {sub-check: 'k of n seeds PASS (need m)'}) of a dict of row lists."""
    sts, counts = [], {}
    for k, rows in parts.items():
        st, passed, n = _sub(rows)
        sts.append(st)
        counts[k] = "%d of %d seeds PASS (need %d): %s" % (passed, n, _need(n), st)
    return sts, counts


def _combine(statuses: list) -> str:
    if not statuses:
        return INCOMPLETE
    if FAIL in statuses:
        return FAIL
    if INCOMPLETE in statuses:
        return INCOMPLETE
    return PASS


def true_percentiles(res: dict, night: int) -> dict:
    """True P90 of the onsets (nights 0..night-1) and P10 of the wakes (nights -1..night-2), night-axis minutes."""
    p4 = _p4(res)
    on = [nm for n, nm, late, obs in p4.get("true_onsets", []) if 0 <= n <= night - 1]
    wk = [nm for n, nm, obs in p4.get("true_wakes", []) if -1 <= n <= night - 2]
    return {"p90_onset": _pct(on, 0.9), "p10_wake": _pct(wk, 0.1), "onsets": len(on), "wakes": len(wk)}


def radar_observable(res: dict, night: int) -> dict:
    p4 = _p4(res)
    on = [obs for n, nm, late, obs in p4.get("true_onsets", []) if 0 <= n <= night - 1]
    wk = [obs for n, nm, obs in p4.get("true_wakes", []) if -1 <= n <= night - 2]
    return {"onsets": sum(1 for o in on if o), "of_onsets": len(on), "wakes": sum(1 for o in wk if o),
            "of_wakes": len(wk)}


def _proposal_check(res: dict, night: int, check_end: bool = True) -> dict:
    rec = _at(res, night)
    prop = _proposal(rec)
    tp = true_percentiles(res, night)
    out = {"night": night, "proposal": None, "true_p90_onset": hhmm(tp["p90_onset"]),
           "true_p10_wake": hhmm(tp["p10_wake"]), "true_nights": [tp["onsets"], tp["wakes"]],
           "start_diff_min": None, "end_diff_min": None, "ok": False}
    if prop is None:
        return out
    s_nm = (prop["start_min"] + 720) % 1440
    e_nm = (prop["end_min"] + 720) % 1440
    out["proposal"] = {"start": prop.get("start"), "end": prop.get("end"), "basis": prop.get("basis"),
                       "from_lights_off": prop.get("from_lights_off"), "onsets_n": prop.get("onsets_n"),
                       "wakes_n": prop.get("wakes_n")}
    if tp["p90_onset"] is not None:
        out["start_diff_min"] = round(s_nm - tp["p90_onset"], 1)
    if tp["p10_wake"] is not None:
        out["end_diff_min"] = round(e_nm - tp["p10_wake"], 1)
    ok_s = out["start_diff_min"] is not None and abs(out["start_diff_min"]) <= TOL_MIN
    ok_e = (not check_end) or (out["end_diff_min"] is not None and abs(out["end_diff_min"]) <= TOL_MIN)
    out["ok"] = bool(ok_s and ok_e)
    return out


def _explain(res: dict, night: int) -> str:
    """Why the learner has no proposal at ``night``: the rule of OA 7.5 applied to its recorded evidence."""
    rec = _at(res, night) or {}
    p4n = rec.get("p4") or {}
    per_night = {}                      # one onset per night: radar, else context, else lux (the learner's rule)
    for n, nm, src in p4n.get("onsets", []):
        rank = ("radar", "context", "lux").index(src) if src in ("radar", "context", "lux") else 3
        if n not in per_night or rank < per_night[n][0]:
            per_night[n] = (rank, nm)
    on = [nm for _, nm in per_night.values()]
    wk = [nm for n, nm in p4n.get("wakes", [])]
    if len(on) < MIN_RADAR_ONSETS:
        return "fewer than 7 nights with an onset"
    if len(wk) < MIN_RADAR_ONSETS:
        return "%d wakes (< 7)" % len(wk)
    parts = []
    for name, vals in (("onset", on), ("wake", wk)):
        iqr = _pct(vals, 0.75) - _pct(vals, 0.25)
        if iqr >= 90:
            parts.append("%s IQR %.0f min >= 90" % (name, iqr))
    return "; ".join(parts) or "window length outside 1-16 h"


# ---------------------------------------------------------------- P4-1
def crit_p41(results: list) -> dict:
    g = _runs(results, "w0000_0600", "base")
    rows = []
    for r in g:
        last = (_last(r) or {"night": 0})["night"]
        row = {"seed": r["seed"], "status": r.get("status"), "nights": last,
               "final": _proposal_check(r, last),
               "radar_observable": radar_observable(r, last),
               "late_onset_nights": _p4(r).get("late_nights", [])}
        if last >= FULL_NIGHTS and last != FULL_NIGHTS:
            row["at_14"] = _proposal_check(r, FULL_NIGHTS)
            if row["at_14"]["proposal"] is None:
                row["at_14"]["no_proposal_reason"] = _explain(r, FULL_NIGHTS)
        rows.append(row)
    with_prop = [x for x in rows if x["final"]["proposal"] is not None]
    for x, r in zip(rows, g):
        if x["final"]["proposal"] is None:
            x["no_proposal_reason"] = _explain(r, x["nights"])
    ok = bool(rows) and all(x["final"]["ok"] for x in with_prop) and len(with_prop) >= _need(len(rows))
    st = _status(ok, g, _complete(g, FULL_NIGHTS))
    at14 = [x["at_14"] for x in rows if "at_14" in x]
    summary14, note = None, ("Gated at the last night of the run; no night 14 in this run (it has no more than 14 "
                             "nights).")
    if at14:
        with14 = [a for a in at14 if a["proposal"] is not None]
        ok14 = bool(with14) and all(a["ok"] for a in with14) and len(with14) >= _need(len(rows))
        summary14 = {"night": FULL_NIGHTS, "runs": len(at14), "with_proposal": len(with14),
                     "within_tolerance": sum(1 for a in at14 if a["ok"]), "same_rule_reads": PASS if ok14 else FAIL}
        note = ("Gated at the last night of the run (%d): the design names no night. At night 14, where the first "
                "proposal reaches the setup review, %d of %d runs are within 30 min on both edges (%d have a "
                "proposal); read with the same rule, night 14 would be %s. The P10 of the wakes and the IQR rule "
                "move with the weekday/weekend mix of the first weeks, and the learner's wake evidence is noisier "
                "than the truth (P4-R1)." % (max(x["nights"] for x in rows), summary14["within_tolerance"],
                                             summary14["runs"], summary14["with_proposal"],
                                             summary14["same_rule_reads"]))
    return {"id": "P4-1", "gated": True, "status": st,
            "title": "00:00-06:00: the proposal is within 30 min of the true P90 onset and P10 wake (5 seeds, "
                     "last night, >= 4 of 5 with a proposal; the 3 onsets after 00:00 are kept)",
            "details": {"runs": len(g), "with_proposal": len(with_prop), "seeds": rows, "at_14_summary": summary14,
                        "note": note}}


# ---------------------------------------------------------------- P4-2
def crit_p42(results: list) -> dict:
    details, statuses = [], []
    for win in ("w2200_0600", "w2300_0700"):
        g = _runs(results, win, "base")
        rows, bad = [], []
        for r in g:
            last = (_last(r) or {"night": 0})["night"]
            obs = radar_observable(r, last)
            chk = _proposal_check(r, last, check_end=False)
            rec = _last(r) or {}
            p4n = (rec.get("p4") or {})
            text = (((p4n.get("items") or {}).get("night_window") or {}).get("data") or {}).get("text")
            prop = chk["proposal"]
            must = obs["onsets"] < MIN_RADAR_ONSETS
            problems = []
            if prop is not None:
                if must and not prop.get("from_lights_off"):
                    problems.append("not labelled from lights-off times")
                if must and text is not None and "from lights-off times" not in text:
                    problems.append("item text lacks 'from lights-off times'")
                if chk["start_diff_min"] is None or abs(chk["start_diff_min"]) > TOL_MIN:
                    problems.append("start off by %s min" % chk["start_diff_min"])
            rows.append({"seed": r["seed"], "nights": last, "radar_observable_onsets": obs["onsets"],
                         "of_onsets": obs["of_onsets"], "wakes_observable": obs["wakes"], "of_wakes": obs["of_wakes"],
                         "learner_onsets": ((p4n.get("night") or {}).get("onsets")),
                         "learner_wakes": ((p4n.get("night") or {}).get("wakes")), "check": chk, "text": text,
                         "problems": problems,
                         "no_proposal_reason": _explain(r, last) if prop is None else None})
            bad.extend(problems)
        with_prop = sum(1 for x in rows if x["check"]["proposal"] is not None)
        # a run without a proposal has nothing to label (reported with its reason); the check needs 4 of 5 proposals
        need = _need(len(rows))
        if bad:
            st = _status(False, g, _complete(g, FULL_NIGHTS))
        elif with_prop < need:
            st = INCOMPLETE
        else:
            st = _status(True, g, _complete(g, FULL_NIGHTS))
        details.append({"window": win, "runs": len(g), "with_proposal": with_prop, "status": st, "seeds": rows,
                        "note": "no proposal needs >= 7 observed wakes and an IQR < 90 min; the 07:00 end hides "
                                "about half of the wakes" if win == "w2300_0700" else
                                "a run without a proposal is reported with its reason (the IQR rule), not failed"})
        statuses.append(st)
    return {"id": "P4-2", "gated": True, "status": _combine(statuses),
            "title": "Onsets not radar-observable (22:00-06:00, 23:00-07:00): the proposal is labelled 'from "
                     "lights-off times' and its start is within 30 min of the true P90 (lux proxy under an ideal "
                     "lux model, >= 4 of 5 proposals per window)",
            "details": details,
            "note": "The lux samples of the replay come from an ideal lux model derived from the truth (see the "
                    "interpretations): the start accuracy here holds for an ideal sensor, not for the real BH1750 "
                    "(the P0 measurement is open)."}


# ---------------------------------------------------------------- P4-3
def _bed_zone_ok(rb: Optional[dict]) -> bool:
    """A bed zone exists, the item is withdrawn because of it, the freeze is on, nothing at the bed is unfrozen."""
    rb = rb or {}
    bi = rb.get("bed_item") or {}
    bd = rb.get("bed") or {}
    return (bi.get("state") == "withdrawn" and bi.get("reason") == "bed_zone" and (bd.get("zones") or 0) >= 1
            and bool(bd.get("frozen")) and "bed_needs_data" not in (rb.get("items") or [])
            and all(p.get("frozen") for p in (rb.get("bed_places") or [])))


def crit_p43(results: list) -> dict:
    marks, apps, rens = [], [], []
    for r in _scripted(results, "mark"):
        m = _actor(r, "mark").get("bed_mark")
        if m is None:
            tried = next((s for s in _actor(r, "mark").get("steps", []) + _actor(r, "mark").get("unfinished", [])
                          if s["step"] == "bed"), None)
            marks.append({"job": r["job"], "seed": r["seed"], "status": FAIL if tried and tried["attempts"] else
                          INCOMPLETE, "note": "no press made a zone (attempts %s)" % (tried or {}).get("attempts")})
            continue
        later = [n for n in _nights(r) if n["night"] > (m["night"] or 0)]
        persists = all((((n.get("p4") or {}).get("review") or {}).get("bed_item") or {}).get("state") == "withdrawn"
                       for n in later)
        ok = m["kind"] == "bed" and _bed_zone_ok(m["readback"]) and persists
        marks.append({"job": r["job"], "seed": r["seed"], "status": PASS if ok else FAIL, "night": m["night"],
                      "attempts": m["attempts"], "kind": m["kind"], "readback": m["readback"],
                      "stays_withdrawn_after": persists})
    for r in _scripted(results, "app"):
        rec = _actor(r, "place_app")
        if not rec.get("suggestion"):
            apps.append({"job": r["job"], "seed": r["seed"], "status": INCOMPLETE,
                         "note": "no place at the bed after night 7 (done=%s)" % rec.get("done")})
            continue
        z = rec.get("zone") or {}
        ok = z.get("kind") == "bed" and _bed_zone_ok(rec.get("after"))
        apps.append({"job": r["job"], "seed": r["seed"], "status": PASS if ok else FAIL, "night": rec["night"],
                     "before": (rec.get("before") or {}).get("bed_item"), "zone": z,
                     "after": rec.get("after")})
    for r in _scripted(results, "names"):
        step = next((s for s in (_actor(r, "place_names").get("steps") or []) if s["op"] == "rename"), None)
        if step is None:
            rens.append({"job": r["job"], "seed": r["seed"], "status": INCOMPLETE, "note": "rename not reached"})
            continue
        before_open = ((step.get("before") or {}).get("bed_item") or {}).get("state") == "open"
        ok = step["kind"] == "bed" and _bed_zone_ok(step.get("after"))
        rens.append({"job": r["job"], "seed": r["seed"], "status": PASS if ok else FAIL,
                     "item_open_before": before_open, "after": step.get("after")})
    parts = {"mark_spot_from_bed_item": marks, "approve_as_bed": apps, "rename_as_bed": rens}
    sts, counts = _counts(parts)
    return {"id": "P4-3", "gated": True, "status": _combine(sts),
            "title": "Default bed method: 'Mark spot here' from the bed item (and approving or renaming a place as "
                     "'Bed') makes a zone of kind bed, withdraws the item and turns on the freeze at once",
            "details": parts, "counts": counts}


# ---------------------------------------------------------------- P4-4
def _step(actor: dict, key: str) -> Optional[dict]:
    return next((s for s in (actor.get("steps") or []) if s["step"] == key), None)


def _failure_ok(step: Optional[dict], reason: str) -> tuple:
    """(status, note) of a failed press: the reason, nothing created, the bed item still open."""
    if step is None:
        return INCOMPLETE, "step not reached"
    last = step.get("last") or {}
    reasons = step.get("reasons") or {}
    if reasons.get("ok"):
        return FAIL, "a press succeeded: %s" % reasons
    if reason not in reasons:
        return (FAIL if reasons else INCOMPLETE), "reasons seen: %s" % reasons
    nothing = last.get("labels_before") == last.get("labels_after")
    item_open = ((last.get("bed_item") or {}).get("state")) == "open"
    if not nothing or not item_open:
        return FAIL, "labels %s -> %s, bed item %s" % (last.get("labels_before"), last.get("labels_after"),
                                                         (last.get("bed_item") or {}).get("state"))
    return PASS, None


def crit_p44(results: list) -> dict:
    subs = {k: [] for k in ("not_observing", "no_stay", "several_stays", "manual", "names")}
    for r in _scripted(results, "mark"):
        a = _actor(r, "mark")
        for key in ("not_observing", "no_stay", "several_stays"):
            st, note = _failure_ok(_step(a, key), key)
            subs[key].append({"job": r["job"], "seed": r["seed"], "status": st, "note": note,
                              "reasons": (_step(a, key) or {}).get("reasons")})
        s = _step(a, "manual")
        if s is None or s.get("skipped"):
            subs["manual"].append({"job": r["job"], "seed": r["seed"], "status": INCOMPLETE,
                                   "note": (s or {}).get("note") or "step not reached"})
        else:
            last = s.get("last") or {}
            ok = bool((s.get("reasons") or {}).get("ok")) and last.get("kind") == "manual" \
                and ((last.get("bed_item") or {}).get("state")) == "open"
            subs["manual"].append({"job": r["job"], "seed": r["seed"], "status": PASS if ok else
                                   (FAIL if (s.get("reasons") or {}).get("ok") else INCOMPLETE),
                                   "reasons": s.get("reasons"), "kind": last.get("kind"),
                                   "bed_item": last.get("bed_item")})
    for r in _scripted(results, "names"):
        steps = [s for s in (_actor(r, "place_names").get("steps") or []) if s["op"] == "approve"]
        rows = [{"name": s["name"], "want_bed": s["want_bed"], "kind": s["kind"],
                 "ok": (s["kind"] == "bed") == s["want_bed"] and s["kind"] is not None} for s in steps]
        full = len(rows) >= 5
        if rows and not all(x["ok"] for x in rows):
            st = FAIL
        else:
            st = PASS if full else INCOMPLETE
        subs["names"].append({"job": r["job"], "seed": r["seed"], "status": st, "approvals": rows})
    sts, counts = _counts(subs)
    return {"id": "P4-4", "gated": True, "status": _combine(sts),
            "title": "'Mark spot here': another item selected gives manual; not_observing, no_stay and "
                     "several_stays create nothing and leave the bed item open; the names bett, BED, ' Bed ' give "
                     "kind bed and others do not",
            "details": subs, "counts": counts,
            "note": "The replay checks the learner's MarkResult (ok, reason, kind); the shell turns a failed one "
                    "into the review event mark_failed (tests/test_p4_service.py)."}


# ---------------------------------------------------------------- P4-5
def crit_p45(results: list) -> dict:
    rows = []
    for r in sorted([x for x in results if _script(x) and _script(x) != "nl"], key=lambda x: (x["job"], x["seed"])):
        node = _p4(r).get("node") or {}
        cmds = node.get("commands") or []
        active = [n["night"] for n in _nights(r)
                  if ((n.get("p4") or {}).get("night_learning") or {}).get("active")]
        extra = (node.get("extra_on_h_node"), node.get("extra_on_h_timeline"))
        nl_ev = [(e.get("last_event")) for e in (_actor(r, "nl_user").get("events") or [])]
        ok = not cmds and not active and extra == (0.0, 0.0) and bool(node.get("consistent", True))
        if _script(r) == "nop2":
            ok = ok and nl_ev == ["no_p2"]
        rows.append({"job": r["job"], "seed": r["seed"], "script": _script(r), "nights": (_last(r) or {}).get("night"),
                     "commands": len(cmds), "nights_active": active, "extra_on_h": list(extra),
                     "nl_events": nl_ev, "ok": ok})
    base_full = [x for x in rows if x["script"] == "base" and (x["nights"] or 0) >= FULL_NIGHTS]
    nop2 = [x for x in rows if x["script"] == "nop2"]
    bad = [x for x in rows if not x["ok"]]
    if bad:
        st = FAIL
    elif base_full and nop2:
        st = PASS
    else:
        st = INCOMPLETE
    return {"id": "P4-5", "gated": True, "status": st,
            "title": "Night learning off by default: no schedule command and no extra radar-on night without the "
                     "user's start (also after the setup review); on a node without P2 nothing is sent",
            "details": {"runs": rows, "base_runs_14_nights": len(base_full), "nop2_runs": len(nop2)}}


# ---------------------------------------------------------------- P4-6
def _window_h(window: str) -> tuple:
    a, b = window.split("-")
    am = int(a[:2]) * 60 + int(a[3:])
    bm = int(b[:2]) * 60 + int(b[3:])
    return am, bm, ((bm - am) % 1440) / 60.0


def crit_p46(results: list) -> dict:
    details, statuses = [], []
    for win in ("w0000_0600", "w2200_0600", "w2300_0700", "w0000_0700"):
        g = _runs(results, win, "nl")
        rows = []
        for r in g:
            window = (r.get("job_spec") or {}).get("window")
            am, bm, hours = _window_h(window) if window else (0, 360, 6.0)
            node = _p4(r).get("node") or {}
            cmds = node.get("commands") or []
            restore = (NL_NIGHTS * 86400 + bm * 60) * 1000
            probs = []
            if len(cmds) != 2:
                probs.append("%d commands" % len(cmds))
            else:
                c0, c1 = cmds
                if not (c0["sched"] is False and c0["expect"] is True and c0["t"] <= 5000):
                    probs.append("start command %s" % {k: c0[k] for k in ("sched", "expect", "t")})
                if not (c1["sched"] is True and c1["expect"] is False
                        and restore <= c1["t"] <= restore + RESTORE_SLACK_MS):
                    probs.append("restore command %s (due %s)" % ({k: c1[k] for k in ("sched", "expect", "t")}, restore))
                if any(c.get("result") not in ("ok", None) for c in cmds):
                    probs.append("results %s" % [c.get("result") for c in cmds])
            if abs((node.get("extra_on_h_node") or 0) - NL_NIGHTS * hours) > 0.05:
                probs.append("extra radar-on %s h, expected %s" % (node.get("extra_on_h_node"), NL_NIGHTS * hours))
            if not node.get("consistent") or node.get("sched_end") is not True:
                probs.append("node log and timeline disagree or the schedule did not end ON")
            ev = [e.get("last_event") for e in (_actor(r, "nl_user").get("events") or [])]
            if ev[-1:] != ["finished"]:
                probs.append("last Night learning event %s" % ev[-1:])
            s = summarize_run(r)
            rows.append({"seed": r["seed"], "nights": (_last(r) or {}).get("night"),
                         "bed_found": s["found"].get("bed"), "bed_label_night": s["label_first"].get("bed"),
                         "extra_on_h": node.get("extra_on_h_node"), "events": ev, "problems": probs})
        need = _need(len(g))
        ok_bed = sum(1 for x in rows if x["bed_found"] is not None and x["bed_found"] <= BED_BY_NIGHT)
        cmd_ok = all(not x["problems"] for x in rows)
        ok = bool(rows) and cmd_ok and ok_bed >= need
        st = _status(ok, g, _complete(g, BED_BY_NIGHT + 2))
        details.append({"window": win, "runs": len(g), "bed_by_night_8": ok_bed, "need": need,
                        "margin": ok_bed - need, "status": st, "seeds": rows})
        statuses.append(st)
    return {"id": "P4-6", "gated": True, "status": _combine(statuses),
            "title": "Night learning started: the bed (IoU >= 0.6) by night 8 in >= 4/5 seeds under every window; "
                     "OFF at the start, ON at the 7th window end, 42 h of extra radar-on time at 00:00-06:00",
            "details": details}


# ---------------------------------------------------------------- P4-7
def crit_p47(results: list) -> dict:
    g = _runs(results, "w0000_0600", "base")
    rows, viol = [], []
    for r in g:
        s = summarize_run(r)
        named = None
        shown_open = 0
        for n in _nights(r):
            p4n = n.get("p4") or {}
            rv = p4n.get("review") or {}
            if not rv.get("ready"):
                continue
            state = (rv.get("bed_item") or {}).get("state")
            has_bed_zone = (rv.get("bed") or {}).get("zones", 0) > 0
            suggested = bool(p4n.get("bed_named"))
            if suggested and named is None:
                named = n["night"]
            expect = "withdrawn" if (has_bed_zone or suggested) else "open"
            if state == "open":
                shown_open += 1
            if state != expect:
                viol.append({"seed": r["seed"], "night": n["night"], "state": state, "expected": expect})
        rows.append({"seed": r["seed"], "nights": (_last(r) or {}).get("night"), "named_bed_night": named,
                     "bed_place_found_night": s["found"].get("bed"), "bed_place_label_night": s["label_first"].get("bed"),
                     "nights_item_shown": shown_open})
    named_n = [x["named_bed_night"] for x in rows if x["named_bed_night"] is not None]
    rej = []
    for r in _scripted(results, "rej"):
        rec = _actor(r, "place_rej")
        if not rec.get("suggestion"):
            rej.append({"job": r["job"], "seed": r["seed"], "status": REPORTED,
                        "note": "not exercised: the learner never named a place 'bed' (done=%s)" % rec.get("done")})
            continue
        b = ((rec.get("before") or {}).get("bed_item") or {})
        a = ((rec.get("after") or {}).get("bed_item") or {})
        ok = b.get("state") == "withdrawn" and a.get("state") == "open"
        rej.append({"job": r["job"], "seed": r["seed"], "status": PASS if ok else FAIL, "before": b, "after": a,
                    "night": rec.get("night")})
    app = []
    for r in _scripted(results, "app"):
        rec = _actor(r, "place_app")
        if not rec.get("suggestion"):
            app.append({"job": r["job"], "seed": r["seed"], "status": INCOMPLETE,
                        "note": "no place at the bed after night 7"})
            continue
        z = rec.get("zone") or {}
        ok = z.get("kind") == "bed" and _bed_zone_ok(rec.get("after"))
        app.append({"job": r["job"], "seed": r["seed"], "status": PASS if ok else FAIL, "night": rec["night"],
                    "item_before": (rec.get("before") or {}).get("bed_item"), "zone": z})
    base_ok = bool(rows) and not viol
    st_base = _status(base_ok, g, _complete(g, FULL_NIGHTS))
    sts = [st_base]
    st_app, n_pass, n_app = _sub(app)
    sts.append(st_app)
    s_rej = [x["status"] for x in rej]
    if FAIL in s_rej:
        sts.append(FAIL)              # the reject path is checked when exercised only (the naming is not modelled)
    exercised = sum(1 for x in rej if x["status"] != REPORTED)
    note = ("%d of %d seeds name the bed place 'bed' by themselves in this replay (the simulator does not model "
            "naming). So 'withdrawn while a place named bed is suggested' and 'shown again when that suggestion is "
            "rejected' are not exercised here (reject runs that reached it: %d of %d): they are covered by unit "
            "tests only (tests/test_review.py, the named-suggestion and reject tests). The base rows show the item "
            "open on %s of %s ready nights."
            % (len(named_n), len(rows), exercised, len(rej), sum(x["nights_item_shown"] for x in rows),
               sum(1 for r in g for n in _nights(r) if ((n.get("p4") or {}).get("review") or {}).get("ready"))))
    return {"id": "P4-7", "gated": True, "status": _combine(sts),
            "counts": {"approve_as_bed": "%d of %d seeds PASS (need %d): %s" % (n_pass, n_app, _need(n_app), st_app)},
            "note": note,
            "title": "bed_needs_data: shown until a bed zone exists or a place named 'bed' is suggested, back on "
                     "reject; approving the place at the bed as 'Bed' withdraws it and freezes (00:00-06:00, "
                     "Night learning off, no 'Mark spot here')",
            "details": {"base": {"status": st_base, "seeds": rows, "violations": viol,
                                 "seeds_naming_bed_by_itself": len(named_n), "of": len(rows),
                                 "naming_nights": named_n},
                       "reject": rej, "approve": app}}


# ---------------------------------------------------------------- reported
def rep_evidence(results: list) -> dict:
    """Accuracy of the learner's night evidence against the truth: per source, errors in minutes."""
    rows = []
    for r in sorted([x for x in results if _script(x) in ("base", "nl")], key=lambda x: (x["job"], x["seed"])):
        last = _last(r)
        if not last:
            continue
        p4 = _p4(r)
        on_true = {n: (nm, late, obs) for n, nm, late, obs in p4.get("true_onsets", [])}
        wk_true = {n: (nm, obs) for n, nm, obs in p4.get("true_wakes", [])}
        pn = (last.get("p4") or {})
        err = {}
        for n, nm, src in pn.get("onsets", []):
            if n in on_true:
                err.setdefault(src, []).append(nm - on_true[n][0])
        wk_err = [nm - wk_true[n][0] for n, nm in pn.get("wakes", []) if n in wk_true]
        have_radar = {n for n, nm, src in pn.get("onsets", []) if src == "radar"}
        missed = [n for n, v in on_true.items() if v[2] and 0 <= n <= last["night"] - 1 and n not in have_radar]
        late_rec = [{"night": n, "lux": [nm for m, nm, src in pn.get("onsets", []) if m == n and src == "lux"],
                     "true": on_true[n][0] if n in on_true else None} for n in _p4(r).get("late_nights", [])
                    if n <= last["night"] - 1]

        def stat(v):
            if not v:
                return None
            return {"n": len(v), "median": _pct(v, 0.5), "max_abs": max(abs(x) for x in v),
                    "share_within_30": round(sum(1 for x in v if abs(x) <= TOL_MIN) / len(v), 3)}

        def iqr(v):
            return round(_pct(v, 0.75) - _pct(v, 0.25), 1) if v else None

        learner_wakes = [nm for n, nm in pn.get("wakes", [])]
        true_same = [wk_true[n][0] for n, nm in pn.get("wakes", []) if n in wk_true]
        true_all = [nm for n, nm, obs in p4.get("true_wakes", []) if -1 <= n <= last["night"] - 2]
        rows.append({"job": r["job"], "seed": r["seed"], "nights": last["night"],
                     "onset_error_min": {k: stat(v) for k, v in err.items()}, "wake_error_min": stat(wk_err),
                     "wake_iqr_min": {"learner": iqr(learner_wakes), "true_same_nights": iqr(true_same),
                                      "true_all_nights": iqr(true_all)},
                     "radar_observable_onsets_without_radar_onset": missed, "late_onset_nights": late_rec})
    return {"id": "P4-R1", "gated": False, "status": REPORTED,
            "title": "Night evidence against the truth: onset errors per source, wake errors (median, largest, "
                     "share of nights within 30 min), the learner's wake IQR against the true one, missed radar "
                     "onsets, the onsets after 00:00 (lux onsets under an ideal lux model)",
            "note": "A compressed learner IQR (smaller than the true one) means the detector hides spread, so the "
                    "P10 compared in P4-1 looks better than the per-night wakes are. The lux onset errors are those "
                    "of an ideal lux model derived from the truth, not of the real BH1750.",
            "details": rows}


def rep_naming(results: list) -> dict:
    g = _runs(results, "w0000_0600", "base")
    rows = []
    for r in g:
        first_named = first_found = None
        for n in _nights(r):
            if first_named is None and (n.get("p4") or {}).get("bed_named"):
                first_named = n["night"]
        s = summarize_run(r)
        first_found = s["found"].get("bed")
        rows.append({"seed": r["seed"], "named_bed_by_itself_night": first_named, "bed_place_found_night": first_found,
                     "label_night": s["label_first"].get("bed")})
    return {"id": "P4-R2", "gated": False, "status": REPORTED,
            "title": "In how many seeds, and on which night, the learner names the bed place 'bed' by itself "
                     "(Night learning off, 00:00-06:00)", "details": {"seeds": rows}}


# ---------------------------------------------------------------- entry
def evaluate_p4(results: list) -> Optional[dict]:
    """The P4 part of the report, or None when no run carries a P4 scenario (a P6 job has a script too, but it is not a
    P4 scenario: it is judged by ``replay.evaluate_p6``)."""
    results = [r for r in results if not _script(r).startswith(("p6", "p7"))]
    if not any(_script(r) for r in results):
        return None
    criteria = [crit_p41(results), crit_p42(results), crit_p43(results), crit_p44(results), crit_p45(results),
                crit_p46(results), crit_p47(results)]
    reported = [rep_evidence(results), rep_naming(results)]
    statuses = [c["status"] for c in criteria]
    return {"runs": sum(1 for r in results if _script(r)), "criteria": criteria, "reported": reported,
            "gate": _combine(statuses), "failed": [c["id"] for c in criteria if c["status"] == FAIL],
            "interpretation": INTERPRETATION, "ha_note": HA_NOTE}


def _compact(d, limit: int = 1800) -> list:
    """One line per top-level entry (a dict key or a list item) as JSON, cut at ``limit`` characters."""
    items = list(d.items()) if isinstance(d, dict) else list(enumerate(d))
    out = []
    for k, v in items:
        text = json.dumps(v, sort_keys=True, separators=(",", ":"))
        if len(text) > limit:
            text = text[:limit] + " ..."
        out.append("- %s: `%s`" % (k, text))
    return out


def render_p4(p4: dict) -> list:
    """Markdown lines of the P4 section of the report."""
    L = ["", "## P4: night window, bed item, Night learning", "",
         "Gate (P4-1 to P4-7): **%s**%s. P4 runs: %d." % (
             p4["gate"], (" (failed: %s)" % ", ".join(p4["failed"])) if p4["failed"] else "", p4["runs"]),
         "", "| ID | Status | Check |", "|---|---|---|"]
    for c in p4["criteria"]:
        L.append("| %s | %s | %s |" % (c["id"], c["status"], c["title"]))
    L += ["", HA_NOTE, ""]
    for c in p4["criteria"]:
        L += ["", "### %s: %s" % (c["id"], c["status"]), "", c["title"], ""]
        d = c["details"]
        if c["id"] == "P4-1":
            L += ["| Seed | nights | proposal | basis | true P90 onset | start diff | true P10 wake | end diff | "
                  "radar-observable onsets | ok | night 14: proposal | start diff | end diff |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for x in d["seeds"]:
                f = x["final"]
                p = f["proposal"] or {}
                a = x.get("at_14") or {}
                ap = a.get("proposal") or {}
                L.append("| %s | %s | %s-%s | %s | %s | %s | %s | %s | %s/%s | %s | %s | %s | %s |" % (
                    x["seed"], x["nights"], p.get("start"), p.get("end"), p.get("basis"), f["true_p90_onset"],
                    f["start_diff_min"], f["true_p10_wake"], f["end_diff_min"], x["radar_observable"]["onsets"],
                    x["radar_observable"]["of_onsets"], f["ok"],
                    ("%s-%s" % (ap.get("start"), ap.get("end"))) if ap else
                    (("none: " + (a.get("no_proposal_reason") or "")) if a else ""),
                    a.get("start_diff_min"), a.get("end_diff_min")))
            if d.get("at_14_summary"):
                a = d["at_14_summary"]
                L += ["", "Night 14 (first proposals reach the setup review): %d of %d runs within 30 min on both edges, "
                      "%d with a proposal; the same rule reads **%s**." % (a["within_tolerance"], a["runs"],
                                                                         a["with_proposal"], a["same_rule_reads"])]
            if d.get("note"):
                L += ["", d["note"]]
        elif c["id"] == "P4-2":
            for w in d:
                L += ["**%s** (%d runs): %s" % (w["window"], w["runs"], w["status"]), "",
                      "| Seed | radar-observable onsets | learner onsets | learner wakes | proposal | basis | label | "
                      "start diff | problems |", "|---|---|---|---|---|---|---|---|---|"]
                for x in w["seeds"]:
                    p = x["check"]["proposal"] or {}
                    L.append("| %s | %s/%s | %s | %s | %s-%s | %s | %s | %s | %s |" % (
                        x["seed"], x["radar_observable_onsets"], x["of_onsets"], x["learner_onsets"],
                        x["learner_wakes"], p.get("start"), p.get("end"), p.get("basis"), p.get("from_lights_off"),
                        x["check"]["start_diff_min"],
                        "; ".join(x["problems"]) or (("no proposal: " + x["no_proposal_reason"])
                                                    if x.get("no_proposal_reason") else "")))
                if w.get("note"):
                    L.append("")
                    L.append(w["note"])
                L.append("")
        elif c["id"] == "P4-6":
            L += ["| Window | runs | bed by night 8 | status |", "|---|---|---|---|"]
            for w in d:
                L.append("| %s | %d | %d (need %d%s) | %s |" % (
                    w["window"], w["runs"], w["bed_by_night_8"], w["need"],
                    ", at the minimum" if w.get("margin") == 0 else "", w["status"]))
            L.append("")
            for w in d:
                for x in w["seeds"]:
                    L.append("- %s seed %s: bed found %s (label %s), extra radar-on %s h%s" % (
                        w["window"], x["seed"], x["bed_found"], x["bed_label_night"], x["extra_on_h"],
                        ("; PROBLEMS: " + "; ".join(x["problems"])) if x["problems"] else ""))
        else:
            L += _compact(d)
        if c.get("counts"):
            L += ["", "Seeds behind each sub-check:"] + ["- %s: %s" % (k, v) for k, v in c["counts"].items()]
        if c.get("note"):
            L += ["", c["note"]]
        L.append("")
    L += ["## P4 reported", ""]
    for c in p4["reported"]:
        L += ["### %s" % c["id"], "", c["title"], ""] + _compact(c["details"]) + [""]
        if c.get("note"):
            L += [c["note"], ""]
    L += ["## P4 interpretations", ""] + ["- " + t for t in p4["interpretation"]] + [""]
    return L
