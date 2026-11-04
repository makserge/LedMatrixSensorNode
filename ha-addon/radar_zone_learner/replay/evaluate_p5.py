"""Evaluation of the P5 replay checks (OA 12 'P5 Setup review and suggestion UI', Verification; P5 spec 11.2, 11.6).

The checks that a replay run can show (the others are unit tests, listed in the report as 'evidence: unit tests'):

* **P5-1** Scripted answers with stale and current revisions (job ``p5ans``): a stale approve changes nothing and says
  ``stale``; the current one makes exactly one zone and one save; the same message again is ``not_pending``; the night
  window and the bed item the same way. A deterministic logic check: every exercised step in every seed.
* **P5-2 (b)** The learner side of 'an HA restart in mid-flow loses no notification and creates no duplicate' (job
  ``p5push``): one setup request, restarts R1-R3, the interrupted request emitted again with the same ``seq``.
* **P5-3 (c)** Every ``PushRequest`` of every p5 run has only int, str-id and None fields.
* **P5-4** First setup takes at most 8 taps (job ``p5setup``, >= 4 of 5 seeds).
* **P5-5** The bed item (sub-checks 5a to 5h; jobs ``p5setup``, ``p5adv``, ``p4mark``, ``p4app``, ``bed_none_p5setup``);
  the three P4 jobs are also read with the P4 rules (P4-3, P4-4, P4-7: ``p4_reruns``).
* **P5-6** Rejection memory (job ``p5rej``).
* **P5-7** Zone records and the answer paths (job ``p5zone``).
* **P5-8** Privacy controls: (a) guests present - pause learning (``p5guests`` against its control ``p5guestsc``),
  (b) pets (reported).

Status per check: PASS, FAIL, INCOMPLETE (runs missing, too short, or a scripted step that could not be exercised),
REPORTED. The k-of-n rule is P4's: 4 of 5 seeds, all of them with fewer than 5 (``replay.evaluate._need``); a step that
was not exercised is not a pass. P5-R1 (``replay.compare_runs``), P5-R2 (``replay.compat_check``) and P5-R3
(``replay.resource_probe --p5``) are separate commands. Interpretations are in ``INTERPRETATION``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from . import evaluate_p4 as E4
from . import world as W
from .evaluate import FAIL, INCOMPLETE, PASS, REPORTED, _need
from .p5_scenarios import overlap_coef, unrle

ALL_SEEDS = (1, 2, 3, 4, 5)
MAX_TAPS = 8
PLACE_TYPES = ("dwell_zone", "short_task_zone")
RERUN_SCRIPTS = ("mark", "app", "rej")
FIELD_X = (-7.0, 7.0)                 # the radar field of the learner's grid (0.2 m cells, 70 x 40)
FIELD_Y = (0.0, 8.0)
NEAR_GUEST_M = 0.6                    # P5-8 (a): 'within 0.6 m of the guest spot'
STAY_ROUND_S = 300                    # the stored stay times are rounded to 5 minutes
PET_SPOT_M = 0.7                      # P5-8 (b)
SCOPE_P5 = "P5-1 to P5-8 (the replay-evaluated checks) and the P4 re-runs of P5-5; G1-G6 and P4-1 to P4-7 not run"

UNIT_EVIDENCE = [
    ("P5-2 (a)", "An HA restart in mid-flow, the cases C1-C15 of spec 6.5 against the fake Home Assistant",
     "tests/test_p5_push_flow.py, tests/test_fake_ha.py"),
    ("P5-3 (a)(b)", "Push payloads: blueprint text, harness scan of every published payload and notification",
     "tests/test_p5_blueprint.py, tests/test_p5_push_flow.py (PrivacyScan)"),
    ("P5-5 (texts, 5g shell event)", "The bed item texts, the shell event mark_failed, the 'Mark spot here' name rule (Desk)",
     "tests/test_p5_items.py, tests/test_p5_advanced.py, tests/test_p5_review_text.py"),
    ("P5-6 (re-ask, forget)", "The 30-night and doubling boundaries, 'Forget rejections', the dedupe pool",
     "tests/test_p5_suppress.py"),
    ("P5-7 (merge, modify)", "The merge and modify answers and sources, frozen zones, the flag off",
     "tests/test_p5_zones.py, tests/test_p5_answers.py"),
    ("P5-8 (c)", "Every guard of the pause, pets kind rule, Forget all, the capture switch, the persisted settings",
     "tests/test_p5_pause.py, tests/test_p5_pets.py, tests/test_p5_reset.py, tests/test_p5_service.py, "
     "tests/test_p5_persistence.py, tests/test_p5_capture.py"),
    ("P5-9", "Map, live view and 'You are in'", "tests/test_p5_map.py, tests/test_p5_where.py, tests/test_p5_live.py, "
     "tests/test_p5_render.py, tests/test_p5_layer.py"),
    ("P5-10", "Entities, topics and discovery", "tests/test_p5_discovery.py, tests/test_p5_topics.py, "
     "tests/test_p5_select.py"),
]
OTHER_COMMANDS = [
    ("P5-R1", "Nothing of P3 or P4 learning changed",
     "replay/dump_run.py on the baseline tree and on this tree, then replay/compare_runs.py --a <base> --b <p5>"),
    ("P5-R2", "State compatibility in both directions", "replay/compat_check.py --base <baseline tree> --out <dir>"),
    ("P5-R3", "Cost", "replay/resource_probe.py --p5 --out <dir>, and tests/test_p5_items.py (Cost)"),
]

INTERPRETATION = [
    "P5-1: all steps run at the first look (a nightly run + 60 s) at which their item exists: the place steps, then the "
    "night window and the bed item (each at the first look at which its item is open; the proposal of the window can come "
    "a night after the places). A step whose item never exists is listed as not exercised; only the place steps "
    "(stale, current, re-delivery) are required for the check not to be INCOMPLETE, as the spec says.",
    "P5-2 (b): 'the first night with count >= 2' is read from the review list (the learner emits a PushRequest only once "
    "a request exists, seq > 0). R1 restarts the learner 0.5 s before minute 480 (push_after_min) of that day; the fake "
    "blueprint feeds PushAck(seq, 'fake') 20 s after it first sees a request; R2 restarts 10 s after the request is seen "
    "(before the ack), R3 10 s after the acknowledged state is seen. A restart is the state's JSON round trip into a new "
    "learner plus the retained topics (node status, schedule, home_empty) fed again.",
    "P5-4: the evening is 18:00 of the day of the first nightly run that leaves the list non-empty; the scripted user is in "
    "bed at the first stay in bed after it (BedStays) and presses 'Mark spot here' every 30 s from 60 s into the stay "
    "while the radars observe; he gives up after 40 presses (a tap each) and answers the places anyway; every answer is "
    "explicit (item and revision); a select choice is a tap only when it changes the selection (so a bed item that "
    "stays open and selected makes every place cost two taps). The simulator's sleeper is visible for about 5 s in "
    "every 600 s, so a press in a sleeper's bed fails with no_stay almost every time (the first measurement without help: "
    "49, 49, 48, 48, 49 taps, 40 failed presses each, the user gives up after 40). OA 7.6 has the user press while awake "
    "in bed, which the world has no session for, so the job p5setup scripts one (JobSpec.extras: a person X1 at the bed "
    "22:00-23:58 of days 6-9, the mechanism of the two people in bed of p4mark). That is a deviation from spec 11.6, "
    "which has p5setup without extras; the limit of 8 taps is not moved. Taps without failed presses are reported "
    "next to the taps the check reads.",
    "P5-5 (5e): the label 'Undo: No bed in view' is built with shell.review_p5.advanced_label when the shell package "
    "imports; the replay checks the learner's entry (id adv:undo:bed, verb undo, kind bed) either way. The texts the "
    "shell builds for the other sub-checks (select labels, buttons, Review pending attributes) are unit-test evidence.",
    "P5-5 (5c): the replay runs the P4 script unchanged (the night_window item selected, no name typed): kind manual and "
    "a 'manual' view in Learner.zones(). The name Desk (D28) is a unit test.",
    "P5-6: the rejected place is the first open place item that is not the bed place (truth IoU against the bed < 0.6, "
    "kind and name hint not bed). The nights checked for 'no open place item overlaps the mask' are those after the "
    "answer up to the night of the undo (night 18; the mask is gone after it). 'Forget rejections' (step 4) is a unit test, "
    "as the pass line says. The suppression entry must be exactly {type, night, ref} (the P4 form).",
    "P5-7: names reach the learner as SpotName (the first approve), as the answer's name with an answer id (the second "
    "and the bed approve) and as the name of an Advanced rename; the removed zone is the Bett zone (the last one made).",
    "P5-8 (a): the pause is switched on 60 s before the session of a guest or visitor starts and off 90 s after it ends; "
    "the stays near the guest spot (within 1 m) are collected after every nightly run. A stay lies in a paused interval "
    "when its stored [t0, t1], widened by 300 s each side (the 5-minute rounding), overlaps it; a violation is such a stay "
    "within 0.6 m of the guest or visitor spot of a session. 'The status stays learning' is read from samples at the "
    "start, 5 min into the session, in the middle and 5 min before the release, and compared with the control run's "
    "samples at the same times. 'No coverage is credited' is read from covered_days and observed_h_24h at every night "
    "(never above the control, lower at the nights whose last 24 h contain a paused session).",
    "P5-8 (b) pets: reported only (the P3 evaluation never validated pets). 'Within 0.7 m of the dog bed or dog sofa "
    "spot' uses the world's spots; 'no tier-2 item overlaps it' is a cell overlap with the open place items of that night.",
    "A p5 invocation reports P5-1 to P5-8 only. G1-G6 and P4-1 to P4-7 were not run; the three P4 jobs (p4mark, p4app, "
    "p4rej) are read with the P4 rules for the sub-checks they feed (P4-3 mark and approve, P4-4 the failure steps and "
    "manual, P4-7 approve and reject).",
]


# ---------------------------------------------------------------- helpers
def _script(res: dict) -> str:
    return (res.get("job_spec") or {}).get("script") or ""


def is_p5_run(res: dict) -> bool:
    return _script(res).startswith("p5")


def is_rerun(res: dict) -> bool:
    return _script(res) in RERUN_SCRIPTS


def _runs(results: list, script: str, window: Optional[str] = None) -> list:
    out = [r for r in results if _script(r) == script and (window is None or r.get("window") == window)]
    return sorted(out, key=lambda r: (r.get("job", ""), r["seed"]))


def _actor(res: dict, name: str) -> dict:
    return E4._actor(res, name)


def _nights(res: dict) -> list:
    return E4._nights(res)


def _last_night(res: dict) -> int:
    return (E4._last(res) or {"night": 0})["night"]


def _ok(res: dict) -> bool:
    return res.get("status") == "ok" and not res.get("core_errors")


def _combine(statuses: list) -> str:
    return E4._combine(statuses)


def _kofn(rows: list) -> tuple:
    return E4._sub(rows)


def _row(res: dict, status: str, **kw) -> dict:
    d = {"job": res.get("job"), "seed": res.get("seed"), "status": status}
    d.update(kw)
    return d


def _p5(res: dict) -> dict:
    return res.get("p5") or {}


def _night_p5(rec: dict) -> dict:
    return rec.get("p5") or {}


def _step_map(steps: list) -> dict:
    return {s["step"]: s for s in steps or []}


# ---------------------------------------------------------------- P5-1
PLACE_STEPS = ("stale_place", "current_place", "redelivery")
WINDOW_STEPS = ("window_stale", "window_current", "window_second", "window_gone")
BED_STEPS = ("bed_stale", "bed_current")


def crit_p51(results: list) -> dict:
    g = _runs(results, "p5ans")
    rows, bad = [], []
    exercised_places = 0
    seen = {k: 0 for k in PLACE_STEPS + WINDOW_STEPS + BED_STEPS}
    for r in g:
        a = _actor(r, "answer_script")
        steps = _step_map(a.get("steps"))
        failed = [k for k, s in steps.items() if not s.get("ok")]
        place = all(k in steps for k in PLACE_STEPS)
        exercised_places += place
        for k in steps:
            seen[k] += 1
        not_ex = [k for k in PLACE_STEPS + WINDOW_STEPS + BED_STEPS if k not in steps]
        st = FAIL if failed else (PASS if place and _ok(r) else INCOMPLETE)
        rows.append(_row(r, st, nights=_last_night(r), failed=failed, not_exercised=not_ex,
                         steps={k: s.get("ok") for k, s in steps.items()},
                         details={k: s for k, s in steps.items() if not s.get("ok")},
                         rev_moves=a.get("rev_moves")))
        if failed:
            bad.append((r["seed"], failed))
    if not g:
        status = INCOMPLETE
    elif bad:
        status = FAIL
    elif exercised_places == 0 or not all(_ok(r) for r in g):
        status = INCOMPLETE
    else:
        status = PASS
    not_anywhere = [k for k, n in seen.items() if n == 0]
    return {"id": "P5-1", "gated": True, "status": status,
            "title": "Scripted answers with stale and current revisions: stale changes nothing and says so, the current "
                     "one makes one zone and one save, the same message again is not_pending (also for the night window "
                     "and the bed item)",
            "details": {"runs": len(g), "seeds": rows, "seeds_exercising_places": exercised_places,
                        "steps_not_exercised_in_any_seed": not_anywhere},
            "counts": {k: "%d of %d seeds exercised" % (n, len(g)) for k, n in seen.items()}}


# ---------------------------------------------------------------- P5-2 (b)
def _local_day(t_ms: int) -> int:
    return t_ms // (W.DAY * 1000)


def crit_p52(results: list) -> dict:
    g = _runs(results, "p5push")
    rows = []
    for r in g:
        a = _actor(r, "push_watcher")
        log = sorted(a.get("log") or [], key=lambda d: d["t"])
        restarts = a.get("restarts") or []
        points = [x["point"] for x in restarts]
        probs = []
        setup_seqs = sorted({d["seq"] for d in log if d["kind"] == "setup"})
        if setup_seqs != [1]:
            probs.append("setup requests with seq %s (exactly one, seq 1, expected)" % setup_seqs)
        seqs = [d["seq"] for d in log]
        acked = [d["acked"] for d in log]
        if any(b < a_ for a_, b in zip(seqs, seqs[1:])):
            probs.append("seq went down")
        if any(b < a_ for a_, b in zip(acked, acked[1:])):
            probs.append("acked went down")
        per_day = {}                       # the daily requests CREATED on a local day (a request is emitted again on every
        created = {}                       # change of its count or acks: only its first emission is its creation)
        for d in log:
            if d["seq"] not in created:
                created[d["seq"]] = d
        for d in created.values():
            if d["kind"] == "daily":
                per_day.setdefault(_local_day(d["t"]), set()).add(d["seq"])
        if any(len(v) > 1 for v in per_day.values()):
            probs.append("more than one daily request on a day: %s" % {k: sorted(v) for k, v in per_day.items() if len(v) > 1})

        def first_after(point):
            return next((d for d in log if d.get("after_restart") == point), None)
        after = {p: first_after(p) for p in ("R1", "R2", "R3")}
        for p in ("R1", "R2", "R3"):
            d = after[p]
            if p in points and (d is None or d["seq"] != 1 or d["kind"] != "setup"):
                probs.append("the first request after %s is %s (the same seq 1 was expected)" % (p, d and
                             {k: d[k] for k in ("seq", "kind", "acked")}))
        # R2 interrupts a request that is not acknowledged yet: the learner must emit it again, unacknowledged
        d2 = after["R2"]
        if "R2" in points and d2 is not None and (d2["acked"] != 0 or d2["acks"]):
            probs.append("after R2 the request is already acknowledged: %s" % d2)
        # R3: the acknowledgement survived the restart
        d3 = after["R3"]
        if "R3" in points and d3 is not None and "fake" not in d3["acks"]:
            probs.append("after R3 the acknowledgement is lost: %s" % d3)
        # setup_sent stays true from the night after the request on
        req_day = _local_day(log[0]["t"]) if log else None
        sent = [(n["night"], ((_night_p5(n).get("p5") or {}).get("push") or {}).get("setup_sent")) for n in _nights(r)
                if req_day is not None and n["night"] > req_day]
        if any(v is not True for _, v in sent):
            probs.append("setup_sent is not true after the request: %s" % [x for x in sent if x[1] is not True][:3])
        missing = [p for p in ("R1", "R2", "R3") if p not in points]
        if not log:
            st, note = INCOMPLETE, "no push request in the run (no night with two open items?)"
        elif probs:
            st, note = FAIL, "; ".join(probs)
        elif missing or not _ok(r):
            st, note = INCOMPLETE, "restart points not reached: %s" % missing if missing else "run status %s" % r.get("status")
        else:
            st, note = PASS, None
        rows.append(_row(r, st, note=note, restarts=restarts, requests=len(log), setup_seqs=setup_seqs,
                         first_after_restart={p: (d and {k: d[k] for k in ("seq", "acked", "kind", "count", "acks")})
                                              for p, d in after.items()},
                         daily=[{"day": k, "seqs": sorted(v)} for k, v in sorted(per_day.items())]))
    if not g:
        status = INCOMPLETE
    else:
        sts = [x["status"] for x in rows]
        status = FAIL if FAIL in sts else (PASS if all(s == PASS for s in sts) else INCOMPLETE)
    return {"id": "P5-2", "gated": True, "status": status,
            "title": "(b) Learner-side restarts: exactly one setup request (seq 1), the request that R2 interrupts is "
                     "emitted again with the same seq, acked never decreases, at most one daily request a day, "
                     "setup_sent stays true; (a) the HA side is unit-test evidence",
            "details": {"runs": len(g), "seeds": rows}}


# ---------------------------------------------------------------- P5-3 (c)
def crit_p53(results: list) -> dict:
    g = [r for r in results if is_p5_run(r)]
    rows, checked = [], 0
    for r in g:
        p = _p5(r)
        n = p.get("push_checked") or 0
        checked += n
        bad = p.get("push_problems") or []
        expect = ["seq", "acked", "kind", "count", "head_id", "head_rev", "acks"]
        fields_ok = (p.get("push_fields") or expect) == expect
        if "p5" not in r:
            st = INCOMPLETE
        elif bad or not fields_ok:
            st = FAIL
        else:
            st = PASS
        rows.append(_row(r, st, checked=n, problems=bad[:3], fields_ok=fields_ok))
    status = _combine([x["status"] for x in rows] + ([] if checked else [INCOMPLETE])) if rows else INCOMPLETE
    return {"id": "P5-3", "gated": True, "status": status,
            "title": "(c) Every PushRequest of every p5 run has only int, str-id and None fields (no text, no time, "
                     "no name); (a) the blueprint text and (b) the payload scan are unit-test evidence",
            "details": {"runs": len(g), "requests_checked": checked, "seeds": rows}}


# ---------------------------------------------------------------- P5-4
def _taps_if_first_press_ok(a: dict) -> Optional[int]:
    """The walk-through of spec 10.2: the night window (when its item was open), one successful 'Mark spot here', one tap
    for every place (the bed item is then withdrawn and the select falls to the first place). Reported, not gated."""
    if a.get("places") is None:
        return None
    return (1 if a.get("window_approved") else 0) + 1 + a["places"]


def crit_p54(results: list) -> dict:
    g = _runs(results, "p5setup", "w0000_0600")
    rows = []
    for r in g:
        a = _actor(r, "setup_user")
        last = _last_night(r)
        if not a.get("started"):
            st, note = INCOMPLETE, "no review item by night %d" % last
        elif not a.get("done"):
            st, note = INCOMPLETE, "the flow did not finish in %d nights" % last
        elif a["taps"] <= MAX_TAPS:
            st, note = PASS, None
        else:
            st, note = FAIL, None
        taps = a.get("taps")
        failed = a.get("failed_marks")
        rows.append(_row(r, st, note=note, taps=taps, places=a.get("places"), failed_marks=failed,
                         taps_without_failed_marks=(taps - failed) if taps is not None and failed is not None else None,
                         taps_if_the_first_press_succeeded=_taps_if_first_press_ok(a),
                         items_at_start=a.get("items_at_start"), day=a.get("day"), window_approved=a.get("window_approved"),
                         mark=a.get("mark"), mark_reasons=a.get("mark_reasons"), text_entries=a.get("text_entries"),
                         more_than_5_places=(a.get("places") or 0) > 5))
    n = len(rows)
    need = _need(n)
    passes = sum(1 for x in rows if x["status"] == PASS)
    fails = sum(1 for x in rows if x["status"] == FAIL)
    if not rows:
        status = INCOMPLETE
    elif passes >= need:
        status = PASS
    elif fails > n - need:
        status = FAIL
    else:
        status = INCOMPLETE
    miss = [x for x in rows if x["status"] == FAIL]
    note = ("The limit is not moved for a seed with more than 5 places. %s" % (
        ("%d of %d misses have more than 5 places; the others do not." % (
            sum(1 for x in miss if x["more_than_5_places"]), len(miss))) if miss else "No seed missed."))
    return {"id": "P5-4", "gated": True, "status": status,
            "title": "First setup takes at most 8 taps (the first evening, the scripted user of OA 7.9: the night window, "
                     "'Mark spot here' in bed, every place; >= 4 of 5 seeds)",
            "details": {"runs": n, "passes": passes, "need": need, "seeds": rows, "note": note},
            "counts": {"taps <= 8": "%d of %d seeds PASS (need %d): %s" % (passes, n, need, status)}}


# ---------------------------------------------------------------- P5-5
TEXT_A = ("press Mark spot here once", "(recommended; no extra radar-on nights)")
_CLOCK = re.compile(r"\b[0-9]{1,2}:[0-9]{2}\b")


def _bed_items(res: dict) -> list:
    """[(night, item or None)] of the bed item in the night records. A P5 job records the learner's own list at the
    nightly run (``rec["p5"]["items"]``); the P4 record is a mirror of the last ``ReviewChanged``, which is the list
    of the previous night (the P4 jobs have no other)."""
    out = []
    for n in _nights(res):
        its = _night_p5(n).get("items")
        if its is not None:
            it = next((x for x in its if x["id"] == "bed_needs_data"), None)
        else:
            it = ((n.get("p4") or {}).get("items") or {}).get("bed_needs_data")
        out.append((n["night"], it))
    return out


def _night_learning_free(data: dict) -> bool:
    for k, v in (data or {}).items():
        if "night_learning" in str(k).lower() or "night learning" in str(v).lower() or "night_learning" in str(v).lower():
            return False
    return True


def _sub_5a(res: dict) -> dict:
    items = [(n, it) for n, it in _bed_items(res) if it is not None]
    if not items:
        return _row(res, INCOMPLETE, note="the bed item was never open")
    bad = []
    for n, it in items:
        d = it.get("data") or {}
        text = d.get("text") or ""
        if not all(t in text for t in TEXT_A) or not _night_learning_free(d):
            bad.append({"night": n, "text": text[:160], "keys": sorted(d)})
    return _row(res, FAIL if bad else PASS, nights_with_item=len(items), bad=bad[:3],
                text=((items[0][1].get("data") or {}).get("text")))


def _sub_5h_window(res: dict) -> dict:
    items = [(n, it) for n, it in _bed_items(res) if it is not None]
    if not items:
        return _row(res, INCOMPLETE, note="the bed item was never open")
    n, it = items[0]
    d = it.get("data") or {}
    want = "before %02d:%02d or after %02d:%02d" % (d.get("start_min", 0) // 60, d.get("start_min", 0) % 60,
                                                     d.get("end_min", 0) // 60, d.get("end_min", 0) % 60)
    ok = (want in (d.get("text") or "")) and (d.get("start_min"), d.get("end_min")) == (0, 360) \
        and d.get("show_window") is True
    return _row(res, PASS if ok else FAIL, night=n, expected=want, start_min=d.get("start_min"), end_min=d.get("end_min"),
                show_window=d.get("show_window"), text=(d.get("text") or "")[:200])


def _sub_5h_off(res: dict) -> dict:
    items = [(n, it) for n, it in _bed_items(res) if it is not None]
    if not items:
        return _row(res, INCOMPLETE, note="the bed item was never open")
    bad = []
    for n, it in items:
        d = it.get("data") or {}
        text = d.get("text") or ""
        if "before" in text or _CLOCK.search(text) or d.get("show_window") is not False:
            bad.append({"night": n, "text": text[:200], "show_window": d.get("show_window")})
    return _row(res, FAIL if bad else PASS, nights_with_item=len(items), bad=bad[:3])


def _zone_by(views: list, **kw) -> list:
    return [v for v in views or [] if all(v.get(k) == x for k, x in kw.items())]


def _sub_5b_mark(res: dict) -> dict:
    a = _actor(res, "mark")
    m = a.get("bed_mark")
    if m is None:
        tried = next((s for s in (a.get("steps") or []) + (a.get("unfinished") or []) if s["step"] == "bed"), None)
        return _row(res, FAIL if tried and tried.get("attempts") else INCOMPLETE,
                    note="no press made a bed zone (attempts %s)" % (tried or {}).get("attempts"))
    rb = m.get("readback") or {}
    views = rb.get("zone_views")
    if views is None:
        return _row(res, INCOMPLETE, note="no zone view in the readback")
    bed = _zone_by(views, kind="bed", src="mark")
    ok = (m.get("kind") == "bed" and len(bed) >= 1 and all(v["frozen"] for v in bed) and E4._bed_zone_ok(rb))
    return _row(res, PASS if ok else FAIL, night=m.get("night"), attempts=m.get("attempts"), kind=m.get("kind"),
                bed_views=bed, bed_item=rb.get("bed_item"))


def _sub_5b_app(res: dict) -> dict:
    rec = _actor(res, "place_app")
    if not rec.get("suggestion"):
        return _row(res, INCOMPLETE, note="no place at the bed after night 7 (done=%s)" % rec.get("done"))
    after = rec.get("after") or {}
    views = after.get("zone_views")
    if views is None:
        return _row(res, INCOMPLETE, note="no zone view in the readback")
    bed = _zone_by(views, kind="bed", src="approve")
    ok = len(bed) >= 1 and all(v["frozen"] for v in bed) and E4._bed_zone_ok(after)
    return _row(res, PASS if ok else FAIL, night=rec.get("night"), bed_views=bed, bed_item=after.get("bed_item"))


def _sub_5c(res: dict) -> dict:
    s = E4._step(_actor(res, "mark"), "manual")
    if s is None or s.get("skipped"):
        return _row(res, INCOMPLETE, note=(s or {}).get("note") or "step not reached")
    last = s.get("last") or {}
    kinds = last.get("zone_kinds")
    reasons = s.get("reasons") or {}
    if not reasons.get("ok"):
        return _row(res, INCOMPLETE, note="no press succeeded", reasons=reasons)
    if kinds is None:
        return _row(res, INCOMPLETE, note="no zone view in the record")
    ok = last.get("kind") == "manual" and "manual" in kinds and ((last.get("bed_item") or {}).get("state")) == "open"
    return _row(res, PASS if ok else FAIL, kind=last.get("kind"), zone_kinds=kinds, bed_item=last.get("bed_item"))


def _sub_5g(res: dict) -> dict:
    a = _actor(res, "mark")
    out = {}
    sts = []
    for key in ("no_stay", "several_stays"):
        st, note = E4._failure_ok(E4._step(a, key), key)
        out[key] = {"status": st, "note": note}
        sts.append(st)
    return _row(res, _combine(sts), steps=out)


def _bed_step_row(res: dict, *keys) -> dict:
    steps = _step_map(_actor(res, "bed_answers").get("steps"))
    miss = [k for k in keys if k not in steps]
    if miss:
        return _row(res, INCOMPLETE, note="not exercised: %s" % miss)
    bad = [k for k in keys if not steps[k].get("ok")]
    return _row(res, FAIL if bad else PASS, steps={k: steps[k] for k in keys}, failed=bad)


def crit_p55(results: list) -> dict:
    parts = {
        "5a_text": [_sub_5a(r) for r in _runs(results, "p5setup") + _runs(results, "mark")],
        "5b_bed_zone": [_sub_5b_mark(r) for r in _runs(results, "mark")] + [_sub_5b_app(r) for r in _runs(results, "app")],
        "5c_manual": [_sub_5c(r) for r in _runs(results, "mark")],
        "5d_later": [_bed_step_row(r, "bed_later", "bed_back") for r in _runs(results, "p5adv")],
        "5e_no_bed_in_view": [_bed_step_row(r, "bed_reject", "bed_away") for r in _runs(results, "p5adv")],
        "5f_undo": [_bed_step_row(r, "bed_undo") for r in _runs(results, "p5adv")],
        "5g_failures": [_sub_5g(r) for r in _runs(results, "mark")],
        "5h_times": [_sub_5h_window(r) for r in _runs(results, "p5setup", "w0000_0600")]
                    + [_sub_5h_off(r) for r in _runs(results, "p5setup", "none")],
    }
    sts, counts = E4._counts(parts)
    # a sub-check that has no run at all is INCOMPLETE (E4._sub of an empty list)
    return {"id": "P5-5", "gated": True, "status": _combine(sts),
            "title": "The bed item: 'Mark spot here' recommended and no Night learning offered (5a); a press in bed with the "
                     "bed item selected gives kind bed, frozen at once (5b), another item selected gives manual (5c); "
                     "[Later] shows the item again after the next nightly run (5d); [No bed in view] is stored (5e) and "
                     "undone under 'Advanced' (5f); mark_failed for no stay and two stays (5g); the item shows the "
                     "window's times and none while the schedule is OFF (5h)",
            "details": parts, "counts": counts}


def p4_reruns(results: list) -> Optional[dict]:
    """The three P4 jobs read with the P4 rules, for the sub-checks they feed: P4-3 (mark, approve), P4-4 (the failure
    steps and the manual press) and P4-7 (approve, reject). The sub-checks that need other P4 jobs (rename as bed,
    the names, the base lifecycle) are not run in a p5 invocation."""
    sub = [r for r in results if is_rerun(r)]
    if not sub:
        return None
    c3 = E4.crit_p43(sub)["details"]
    c4 = E4.crit_p44(sub)["details"]
    c7 = E4.crit_p47(sub)["details"]
    parts = {"P4-3 mark_spot_from_bed_item": c3["mark_spot_from_bed_item"], "P4-3 approve_as_bed": c3["approve_as_bed"],
             "P4-4 not_observing": c4["not_observing"], "P4-4 no_stay": c4["no_stay"],
             "P4-4 several_stays": c4["several_stays"], "P4-4 manual": c4["manual"],
             "P4-7 approve_as_bed": c7["approve"]}
    sts, counts = E4._counts(parts)
    rej = c7["reject"]
    if any(x["status"] == FAIL for x in rej):
        sts.append(FAIL)
    counts["P4-7 reject (when exercised)"] = "%d runs: %s" % (len(rej), ", ".join(x["status"] for x in rej) or "none")
    return {"status": _combine(sts), "counts": counts, "details": parts,
            "not_run": ["P4-3 rename_as_bed", "P4-4 names", "P4-7 base lifecycle", "P4-1", "P4-2", "P4-5", "P4-6"]}


# ---------------------------------------------------------------- P5-6
def _open_place_cells(rec: dict) -> dict:
    """{item id: cells} of the open place items of a night (the cells come from the night's suggestion records)."""
    by_id = {s["id"]: s for s in rec.get("suggestions") or []}
    out = {}
    for it in (_night_p5(rec).get("items") or []):
        if it["type"] in PLACE_TYPES and it["id"] in by_id:
            out[it["id"]] = unrle(by_id[it["id"]]["cells_rle"])
    return out


def _hits_mask(cells, mask_cells, mask_cell) -> list:
    why = []
    if overlap_coef(cells, mask_cells) >= 0.5:
        why.append("overlap_coef %.2f" % overlap_coef(cells, mask_cells))
    if mask_cell is not None and mask_cell in cells:
        why.append("the mask's centre cell")
    return why


def _p56_row(r: dict) -> dict:
    a = _actor(r, "reject_user")
    rej = a.get("rejected")
    last = _last_night(r)
    if rej is None:
        return _row(r, INCOMPLETE, note="no open place item (not the bed place) by night %d" % last)
    probs = []
    sid, N = rej["id"], rej["night"]
    exp = {"type": "place", "night": N, "ref": sid}
    if rej["ignored"] or rej["suppressions"] != [exp]:
        probs.append("(1) suppression %s, ignored %s" % (rej["suppressions"], rej["ignored"]))
    m = rej.get("mask")
    if not m:
        probs.append("(1) no mask in p5.masks")
        mask_cells, mask_cell = frozenset(), None
    else:
        mask_cells = unrle(m["cells"])
        mask_cell = W.cell_of(m["cx"], m["cy"])
        if m["cells"] != rej["cells_rle"] or abs(m["cx"] - rej["cx"]) > 0.02 or abs(m["cy"] - rej["cy"]) > 0.02 \
                or not isinstance(m["n0"], int) or m["n0"] < 0:
            probs.append("(1) the mask is not the rejected suggestion's region and evidence: %s" % {k: m[k] for k in ("cx", "cy", "n0")})
    undo = a.get("undone")
    undo_n = undo["night"] if undo else None
    nights = _nights(r)
    violations, rev_changes, stale_seen, stale_bad = [], [], 0, []
    for rec in nights:
        n = rec["night"]
        if n <= N:
            continue
        if undo_n is not None and n > undo_n:             # (2) holds while the suppression exists: up to the record of the
            continue                                      # night of the undo (afterwards the suggestion is an ordinary one)
        for iid, cells in _open_place_cells(rec).items():
            why = _hits_mask(cells, mask_cells, mask_cell)
            if why:
                violations.append({"night": n, "item": iid, "why": why})
        s = next((x for x in rec.get("suggestions") or [] if x["id"] == sid), None)
        if s is not None:
            if s["rev"] != rej["rev"]:
                rev_changes.append({"night": n, "rev": s["rev"]})
            if s["state"] == "stale":
                stale_seen += 1
                p5n = _night_p5(rec)
                if sid in (p5n.get("status_sugg_ids") or []) or sid in (p5n.get("map_sugg_ids") or []):
                    stale_bad.append(n)
    if violations:
        probs.append("(2) open place items match the rejected region: %s" % violations[:3])
    if rev_changes:
        probs.append("(2) the rejected suggestion's rev changed: %s" % rev_changes[:3])
    if stale_bad:
        probs.append("(2) the stale rejected suggestion is still in the status attribute or the map on nights %s" % stale_bad)
    # (3) the undo and the return within 3 nights
    back = None
    if undo is not None:
        if undo["ignored"] or undo["suppressions"] or undo["mask"]:
            probs.append("(3) the undo left the suppression or the mask: %s" % undo)
        for rec in nights:
            n = rec["night"]
            if undo_n < n <= undo_n + 3:
                for iid, cells in _open_place_cells(rec).items():
                    if _hits_mask(cells, mask_cells, mask_cell):
                        back = {"night": n, "item": iid}
                        break
            if back:
                break
    if probs:
        st = FAIL
    elif undo is None or stale_seen == 0 or (back is None and last < (undo_n or 0) + 3):
        st = INCOMPLETE
    elif back is None:
        probs.append("(3) no open place item overlaps the old mask within 3 nights of the undo")
        st = FAIL
    else:
        st = PASS
    note = "; ".join(probs) or (None if st == PASS else
                                 "not exercised: undo %s, stale nights seen %d, nights %d" % (undo_n, stale_seen, last))
    return _row(r, st, note=note, rejected=sid, night=N, tag=rej.get("tag"), undo_night=undo_n, back=back,
                stale_nights_seen=stale_seen, mask=({"n0": m["n0"], "cells": len(mask_cells)} if m else None),
                nights_checked=sum(1 for rec in nights if N < rec["night"] <= (undo_n or last)))


def crit_p56(results: list) -> dict:
    g = _runs(results, "p5rej")
    rows = [_p56_row(r) for r in g]
    sts = [x["status"] for x in rows]
    status = INCOMPLETE if not rows else (FAIL if FAIL in sts else (PASS if all(s == PASS for s in sts) else INCOMPLETE))
    return {"id": "P5-6", "gated": True, "status": status,
            "title": "Rejection memory: the suppression keeps the P4 form and the mask is stored (1); no open place item "
                     "overlaps the mask by 0.5 or contains its centre, and the rejected suggestion never changes its rev "
                     "and is absent from status and map once stale (2); the undo under 'Advanced' brings a place back "
                     "within 3 nights (3); (4) 'Forget rejections' and the re-ask boundaries are unit-test evidence",
            "details": {"runs": len(g), "seeds": rows}}


# ---------------------------------------------------------------- P5-7
def _p57_invariants(a: dict) -> list:
    """What must hold over the zone snapshots of one run (spec P5-7 'Expect')."""
    probs = []
    snaps = a.get("snapshots") or []
    seen_ids, last_n, removed = set(), 0, set()
    for s in snaps:
        z = s["zones"]
        recs = [x for x in z if x["src"] == "approve"]
        labs = [x for x in z if x["src"] == "mark"]
        if z != recs + labs or [x["id"] for x in recs] != sorted(x["id"] for x in recs) \
                or [x["id"] for x in labs] != sorted(x["id"] for x in labs):
            probs.append("%s: records then labels, each ordered by id, expected: %s" % (s["label"], [x["id"] for x in z]))
        names = [x["name"].strip().casefold() for x in z if x["name"].strip()]
        if len(names) != len(set(names)):
            probs.append("%s: names are not unique: %s" % (s["label"], [x["name"] for x in z]))
        for x in z:
            pc = x["poly_cm"]
            if len(pc) > 12 or not pc and x["src"] == "approve":
                probs.append("%s: %s has %d polygon vertices" % (s["label"], x["id"], len(pc)))
            if any(not (FIELD_X[0] * 100 <= px <= FIELD_X[1] * 100 and FIELD_Y[0] * 100 <= py <= FIELD_Y[1] * 100)
                   for px, py in pc):
                probs.append("%s: the polygon of %s leaves the radar field" % (s["label"], x["id"]))
            if x["src"] == "approve" and x["id"] in removed:
                probs.append("%s: the zone id %s was reused after its removal" % (s["label"], x["id"]))
        nzn = s.get("next_zone_n")
        if nzn is not None:
            if nzn < last_n:
                probs.append("%s: next_zone_n went down %d -> %d" % (s["label"], last_n, nzn))
            last_n = max(last_n, nzn)
            for x in recs:
                if int(x["id"][1:]) >= nzn:
                    probs.append("%s: %s is not below next_zone_n %d" % (s["label"], x["id"], nzn))
        gone = seen_ids - {x["id"] for x in recs}
        removed |= gone
        seen_ids |= {x["id"] for x in recs}
    # before/after pairs: an answer on one zone leaves the others unchanged (rev rose only on a change)
    by_label = {s["label"]: s for s in snaps}
    for tag in ("desk", "desk2", "bett"):
        b, af = by_label.get("before_" + tag), by_label.get("after_" + tag)
        if b and af:
            bz = {x["id"]: x for x in b["zones"]}
            az = {x["id"]: x for x in af["zones"]}
            for k, x in bz.items():
                if az.get(k) != x:
                    probs.append("after_%s: the zone %s changed although another place was approved" % (tag, k))
    return probs


def crit_p57(results: list) -> dict:
    g = _runs(results, "p5zone")
    rows = []
    for r in g:
        a = _actor(r, "zone_user")
        tasks = a.get("tasks") or []
        todo = a.get("todo") or []
        failed = [t for t in tasks if not t.get("ok")]
        inv = _p57_invariants(a)
        types = {}
        for n in _nights(r):
            for it in (_night_p5(n).get("items") or []):
                if it["type"] in ("merge_into", "modify_zone"):
                    types.setdefault(it["type"], set()).add(it["id"])
        if failed or inv:
            st = FAIL
        elif todo or not _ok(r):
            st = INCOMPLETE
        else:
            st = PASS
        rows.append(_row(r, st, tasks={t["task"]: t["ok"] for t in tasks}, not_reached=todo,
                         failed={t["task"]: {k: v for k, v in t.items() if k not in ("task", "ok")} for t in failed},
                         invariants=inv[:5], nights=_last_night(r),
                         merge_items=sorted(types.get("merge_into", ())), modify_items=sorted(types.get("modify_zone", ()))))
    status = INCOMPLETE if not rows else _combine([x["status"] for x in rows])
    return {"id": "P5-7", "gated": True, "status": status,
            "title": "Zone records and the answer paths: approve with names (Desk, desk -> 'desk 2'), rename keeps the "
                     "zone id and raises rev by one, a bed zone is frozen and withdraws the item, removal opens the place "
                     "again; the list order, polygons, next_zone_n and unique names hold; merge and modify are "
                     "unit-test evidence (their counts here are reported)",
            "details": {"runs": len(g), "seeds": rows}}


# ---------------------------------------------------------------- P5-8
def _intervals(switches: list) -> list:
    out, on = [], None
    for s in sorted(switches, key=lambda x: x["t"]):
        if s["on"] and on is None:
            on = s["t"]
        elif not s["on"] and on is not None:
            out.append((on, s["t"]))
            on = None
    return out


def _night_status(res: dict) -> dict:
    return {n["night"]: (_night_p5(n).get("status") or {}) for n in _nights(res)}


def _p58a_pair(pause: dict, ctrl: Optional[dict]) -> dict:
    a = _actor(pause, "guests_user")
    c = _actor(ctrl, "guests_user") if ctrl is not None else {}
    probs = []
    iv = _intervals(a.get("switches") or [])
    sessions = a.get("sessions") or []
    last = _last_night(pause)
    if not sessions or not iv:
        return _row(pause, INCOMPLETE, note="no guest or visitor session in %d nights" % last)
    # a stay near a guest spot inside a paused interval
    viol = []
    for s in a.get("stays_near") or []:
        if s["dist"] > NEAR_GUEST_M:
            continue
        lo, hi = s["t0_s"] - STAY_ROUND_S, s["t1_s"] + STAY_ROUND_S
        for on, off in iv:
            if lo < off / 1000.0 and on / 1000.0 < hi:
                viol.append({"stay": s, "paused": [on, off]})
                break
    if viol:
        probs.append("%d stays within %.1f m of a guest spot during a pause: %s" % (len(viol), NEAR_GUEST_M, viol[:2]))
    if not (a.get("paused_stays") or 0) > 0:
        probs.append("paused_stays() is %s, expected > 0" % a.get("paused_stays"))
    # the status stays learning
    cs = {(x["session"], x["tag"]): x for x in c.get("samples") or []}
    compared = 0
    for x in a.get("samples") or []:
        y = cs.get((x["session"], x["tag"]))
        if y is None:
            continue
        compared += 1
        if x["state"] != y["state"] or x["radar_valid"] != y["radar_valid"]:
            probs.append("session %s %s: state %s/%s against the control's %s/%s" % (
                x["session"], x["tag"], x["state"], x["radar_valid"], y["state"], y["radar_valid"]))
    learning = sum(1 for y in cs.values() if y["state"] == "learning")
    vague = []                                           # what makes the comparison say nothing (INCOMPLETE, not FAIL)
    if ctrl is not None and learning == 0:
        vague.append("the control never reads 'learning' at a sample: the comparison says nothing")
    # coverage
    ps, cstat = _night_status(pause), _night_status(ctrl) if ctrl is not None else {}
    cov_bad, reduced, need_red = [], 0, 0
    sess_ends = [s["t1"] for s in sessions]
    for n, st in ps.items():
        cst = cstat.get(n)
        if not cst or st.get("covered_days") is None or cst.get("covered_days") is None:
            continue
        if st["covered_days"] > cst["covered_days"] or (st.get("observed_h_24h") or 0) > (cst.get("observed_h_24h") or 0) + 0.05:
            cov_bad.append({"night": n, "pause": [st["covered_days"], st.get("observed_h_24h")],
                            "control": [cst["covered_days"], cst.get("observed_h_24h")]})
        night_s = n * W.DAY + 3 * 3600 + 1800
        if any(night_s - 86400 <= e <= night_s for e in sess_ends):
            need_red += 1
            if (cst.get("observed_h_24h") or 0) - (st.get("observed_h_24h") or 0) > 0.05:
                reduced += 1
    if cov_bad:
        probs.append("coverage above the control's: %s" % cov_bad[:2])
    if need_red and reduced < need_red:
        probs.append("observed_h_24h is not lower than the control's on %d of %d nights after a paused session" % (need_red - reduced, need_red))
    # the control must have a stay at a guest spot (the first statement is meaningful only then)
    ctrl_near = [s for s in (c.get("stays_near") or []) if s["dist"] <= NEAR_GUEST_M]
    if ctrl is None:
        st, note = INCOMPLETE, "no control run (p5guestsc) for seed %s" % pause["seed"]
    elif probs:
        st, note = FAIL, "; ".join(probs)
    elif not ctrl_near:
        st, note = INCOMPLETE, "the control records no stay at a guest or visitor spot: the pause check says nothing"
    elif vague:
        st, note = INCOMPLETE, "; ".join(vague)
    elif not _ok(pause) or not _ok(ctrl) or compared == 0:
        st, note = INCOMPLETE, "run status %s/%s, samples compared %d" % (pause.get("status"), ctrl.get("status"), compared)
    else:
        st, note = PASS, None
    return _row(pause, st, note=note, sessions=len(sessions), intervals=len(iv), stays_near_pause=len(a.get("stays_near") or []),
                stays_near_control=len(ctrl_near), paused_stays=a.get("paused_stays"), samples_compared=compared,
                nights_with_reduced_coverage=[reduced, need_red])


def _p58b_row(r: dict) -> dict:
    spots = {"dogbed": W.SPOT["dogbed"], "dogsofa": W.SPOT["dogsofa"]}
    first_pet = first_near = None
    near_info = None
    over = []
    anything = {}                      # any suggestion near a dog spot, for context (the dog sofa is next to the human sofa)
    for rec in _nights(r):
        for s in rec.get("suggestions") or []:
            for k, p in spots.items():
                d = ((s["cx"] - p[0]) ** 2 + (s["cy"] - p[1]) ** 2) ** 0.5
                if d <= PET_SPOT_M and s["id"] not in anything:
                    anything[s["id"]] = {"id": s["id"], "type": s["type"], "kind": s.get("kind"), "spot": k,
                                         "dist_m": round(d, 2), "sim_label": s.get("sim_label"), "night": rec["night"]}
        pets = [s for s in rec.get("suggestions") or [] if s.get("kind") == "pet" and s["state"] == "shadow"]
        if pets and first_pet is None:
            first_pet = rec["night"]
        for s in pets:
            d = {k: ((s["cx"] - p[0]) ** 2 + (s["cy"] - p[1]) ** 2) ** 0.5 for k, p in spots.items()}
            k = min(d, key=d.get)
            if d[k] <= PET_SPOT_M and first_near is None:
                first_near = rec["night"]
                near_info = {"id": s["id"], "spot": k, "dist_m": round(d[k], 2), "type": s["type"]}
            if d[k] <= PET_SPOT_M:
                cells = unrle(s["cells_rle"])
                ov = [iid for iid, c in _open_place_cells(rec).items() if cells & c]
                if ov:
                    over.append({"night": rec["night"], "suggestion": s["id"], "items": ov})
    return _row(r, REPORTED, first_pet_night=first_pet, first_near_dog_spot_night=first_near, near=near_info,
                suggestions_near_dog_spots=sorted(anything.values(), key=lambda x: (x["night"], x["id"]))[:6],
                tier2_items_overlapping=over[:5], nights=_last_night(r),
                pet_spots=[(_night_p5(n).get("status") or {}).get("pet_spots") for n in _nights(r)][-1:])


def crit_p58(results: list) -> dict:
    pauses = _runs(results, "p5guests")
    ctrl = {r["seed"]: r for r in _runs(results, "p5guestsc")}
    rows = [_p58a_pair(r, ctrl.get(r["seed"])) for r in pauses]
    sts = [x["status"] for x in rows]
    status = INCOMPLETE if not rows else (FAIL if FAIL in sts else (PASS if all(s == PASS for s in sts) else INCOMPLETE))
    pets = [_p58b_row(r) for r in _runs(results, "p5pets")]
    return {"id": "P5-8", "gated": True, "status": status,
            "title": "Privacy controls: (a) with 'Guests present - pause learning' on for the guest and visitor sessions no "
                     "stay lies within 0.6 m of their spot during a pause, paused_stays() > 0, the status stays learning "
                     "and no coverage is credited (against the control run, which does record a stay there); "
                     "(b) pets are reported; (c) the unit tests are evidence",
            "details": {"a_guests": rows, "b_pets_reported": pets}}


# ---------------------------------------------------------------- reported
def rep_p5_taps(results: list) -> dict:
    rows = []
    for r in _runs(results, "p5setup"):
        a = _actor(r, "setup_user")
        if a.get("taps") is None:
            continue
        taps, failed = a["taps"], a.get("failed_marks") or 0
        rows.append({"job": r["job"], "seed": r["seed"], "taps": taps, "places": a.get("places"), "failed_marks": failed,
                     "taps_without_failed_marks": taps - failed,
                     "taps_if_the_first_press_succeeded": _taps_if_first_press_ok(a), "mark_reasons": a.get("mark_reasons"),
                     "mark_ok": ((a.get("mark") or {}).get("ok")), "items_at_start": a.get("items_at_start")})
    return {"id": "P5-4r", "gated": False, "status": REPORTED,
            "title": "The taps of the first setup: with the failed 'Mark spot here' presses (what P5-4 reads), without them, "
                     "and as the walk-through of spec 10.2 counts them (one successful press)",
            "details": rows,
            "note": "A failed press comes from the simulator (a sleeper is visible for about 5 s in every 600 s: no_stay; "
                    "the job p5setup scripts one person awake in bed so that the first press finds a stay), not from the "
                    "review flow; the review's own taps are the second figure."}


def rep_p5_merge_modify(results: list) -> dict:
    rows = []
    for r in _runs(results, "p5zone"):
        mi, mo = set(), set()
        for n in _nights(r):
            for it in (_night_p5(n).get("items") or []):
                (mi if it["type"] == "merge_into" else mo if it["type"] == "modify_zone" else set()).add(it["id"])
        rows.append({"seed": r["seed"], "merge_items": sorted(mi), "modify_items": sorted(mo)})
    return {"id": "P5-7r", "gated": False, "status": REPORTED,
            "title": "How many merge and modify items the simple sources of spec 4.7 produced (zone_change_items on; "
                     "test scaffolding, P7 owns the detector: not a P5 result)", "details": rows}


# ---------------------------------------------------------------- entry
def evaluate_p5(results: list) -> Optional[dict]:
    """The P5 part of the report, or None when no run carries a P5 scenario."""
    if not any(is_p5_run(r) for r in results):
        return None
    criteria = [crit_p51(results), crit_p52(results), crit_p53(results), crit_p54(results), crit_p55(results),
                crit_p56(results), crit_p57(results), crit_p58(results)]
    reported = [rep_p5_taps(results), rep_p5_merge_modify(results)]
    statuses = [c["status"] for c in criteria]
    rr = p4_reruns(results)
    if rr is not None:
        statuses.append(rr["status"])
    return {"runs": sum(1 for r in results if is_p5_run(r) or is_rerun(r)), "criteria": criteria, "reported": reported,
            "p4_reruns": rr, "gate": _combine(statuses),
            "failed": [c["id"] for c in criteria if c["status"] == FAIL] + (["P4 re-runs"] if rr and rr["status"] == FAIL else []),
            "unit_evidence": [list(x) for x in UNIT_EVIDENCE], "other_commands": [list(x) for x in OTHER_COMMANDS],
            "interpretation": INTERPRETATION}


def _compact(d, limit: int = 1500) -> list:
    return E4._compact(d, limit)


def render_p5(p5: dict) -> list:
    """Markdown lines of the P5 section of the report."""
    L = ["", "## P5: setup review and suggestion UI", "",
         "Gate (the replay-evaluated checks P5-1 to P5-8 and the P4 re-runs): **%s**%s. P5 and P4 re-run runs: %d." % (
             p5["gate"], (" (failed: %s)" % ", ".join(p5["failed"])) if p5["failed"] else "", p5["runs"]),
         "", "| ID | Status | Check |", "|---|---|---|"]
    for c in p5["criteria"]:
        L.append("| %s | %s | %s |" % (c["id"], c["status"], c["title"]))
    if p5.get("p4_reruns"):
        L.append("| P4 re-runs | %s | The three P4 jobs read with the P4 rules (P4-3, P4-4, P4-7 sub-checks they feed) |"
                 % p5["p4_reruns"]["status"])
    L += ["", "G1-G6 and P4-1 to P4-7 were **not run** in this invocation, except the three P4 re-runs above.", ""]
    for c in p5["criteria"]:
        L += ["", "### %s: %s" % (c["id"], c["status"]), "", c["title"], ""]
        d = c["details"]
        if c["id"] == "P5-4":
            L += ["| Seed | status | taps | places | failed presses | taps without failed presses | taps if the first press "
                  "had succeeded | day | window approved | mark | note |", "|---|---|---|---|---|---|---|---|---|---|---|"]
            for x in d["seeds"]:
                L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    x["seed"], x["status"], x["taps"], x["places"], x["failed_marks"], x["taps_without_failed_marks"],
                    x["taps_if_the_first_press_succeeded"], x["day"], x["window_approved"],
                    json.dumps((x.get("mark") or {}).get("ok")), x.get("note") or ""))
            L += ["", d["note"]]
        elif c["id"] in ("P5-1", "P5-2", "P5-6", "P5-7"):
            if not d["seeds"]:
                L.append("- no run of this check's job in this invocation: INCOMPLETE")
            for x in d["seeds"]:
                L.append("- seed %s: **%s**%s" % (x["seed"], x["status"], (" (%s)" % x["note"]) if x.get("note") else ""))
                keep = {k: v for k, v in x.items() if k not in ("job", "seed", "status", "note")}
                L.append("  `%s`" % json.dumps(keep, sort_keys=True, separators=(",", ":"))[:1800])
        elif c["id"] == "P5-8":
            for x in d["a_guests"]:
                L.append("- (a) seed %s: **%s**%s" % (x["seed"], x["status"], (" (%s)" % x["note"]) if x.get("note") else ""))
                keep = {k: v for k, v in x.items() if k not in ("job", "seed", "status", "note")}
                L.append("  `%s`" % json.dumps(keep, sort_keys=True, separators=(",", ":"))[:900])
            for x in d["b_pets_reported"]:
                L.append("- (b) pets, seed %s (reported): `%s`" % (x["seed"], json.dumps(
                    {k: v for k, v in x.items() if k not in ("job", "seed", "status")}, sort_keys=True,
                    separators=(",", ":"))[:900]))
        else:
            L += _compact(d)
        if c.get("counts"):
            L += ["", "Seeds behind each sub-check:"] + ["- %s: %s" % (k, v) for k, v in c["counts"].items()]
        L.append("")
    if p5.get("p4_reruns"):
        rr = p5["p4_reruns"]
        L += ["### P4 re-runs: %s" % rr["status"], "", "Seeds behind each sub-check:"] + [
            "- %s: %s" % (k, v) for k, v in rr["counts"].items()] + ["", "Not run here: " + ", ".join(rr["not_run"]), ""]
    L += ["## P5 reported", ""]
    for c in p5["reported"]:
        L += ["### %s" % c["id"], "", c["title"], ""] + _compact(c["details"])
        if c.get("note"):
            L += ["", c["note"]]
        L.append("")
    L += ["## Evidence: unit tests", "", "| Check | What | Where |", "|---|---|---|"]
    for a, b, c in p5["unit_evidence"]:
        L.append("| %s | %s | %s |" % (a, b, c))
    L += ["", "## Separate commands", "", "| Check | What | Command |", "|---|---|---|"]
    for a, b, c in p5["other_commands"]:
        L.append("| %s | %s | `%s` |" % (a, b, c))
    L += ["", "## P5 interpretations", ""] + ["- " + t for t in p5["interpretation"]] + [""]
    return L


# ---------------------------------------------------------------- the report of a p5 invocation
def build_report(results: list, unit_g5b: Optional[bool] = None) -> dict:
    """``report.json`` content of an invocation that has p5 runs. Without any other run it is a pure P5 report (the
    P3 and P4 criteria are not evaluated: their runs are not there); runs that are neither P5 nor one of the three P4
    re-runs are given to ``replay.evaluate.evaluate`` and the P5 section is attached to that report."""
    from . import evaluate as E
    p5res = [r for r in results if is_p5_run(r) or is_rerun(r)]
    others = [r for r in results if not (is_p5_run(r) or is_rerun(r))]
    p5 = evaluate_p5(p5res)
    summaries = [E.summarize_run(r) for r in p5res]
    if others:
        report = E.evaluate(others, unit_g5b=unit_g5b)
        report["p5"] = p5
        failed = report["failed"] + p5["failed"]
        report["failed"] = failed
        report["gate"] = _combine([report["gate"], p5["gate"]])
        bad = report["bad_runs"] + [{"job": s["job"], "seed": s["seed"], "status": s["status"],
                                     "core_errors": s["core_errors"], "first_error": s["first_error"]}
                                    for s in summaries if s["status"] != "ok" or s["core_errors"]]
        report["bad_runs"] = bad
        report["exit_code"] = 1 if failed or any(b["status"] != "ok" for b in bad) else 0
        report["job_matrix"] = report["job_matrix"] + E.job_matrix(summaries)
        return report
    bad = [{"job": s["job"], "seed": s["seed"], "status": s["status"], "core_errors": s["core_errors"],
            "first_error": s["first_error"]} for s in summaries if s["status"] != "ok" or s["core_errors"]]
    failed = p5["failed"]
    params = sorted({r.get("params") for r in p5res if r.get("params")})
    return {"schema": 1, "kind": "p5", "runs": len(p5res), "params_digests": params, "gate": p5["gate"], "failed": failed,
            "scope": SCOPE_P5, "job_matrix": E.job_matrix(summaries),
            "exit_code": 1 if failed or any(b["status"] != "ok" for b in bad) else 0, "bad_runs": bad,
            "criteria": [], "reported": [], "p5": p5}


def render_markdown(report: dict) -> str:
    from . import evaluate as E
    p5 = report.get("p5")
    if report.get("kind") != "p5":
        text = E.render_markdown(report)
        return text + "\n".join(render_p5(p5)) + "\n" if p5 else text
    L = ["# P5 replay report (G1-G6 and P4-1 to P4-7 not run)", "",
         "Gate (%s): **%s**%s. Runs: %d. Params digest: %s." % (
             report["scope"], report["gate"], (" (failed: %s)" % ", ".join(report["failed"])) if report["failed"] else "",
             report["runs"], ", ".join(report["params_digests"]) or "–"), "",
         "**G1-G6 and P4-1 to P4-7 were not run in this invocation** (the three P4 jobs are re-runs for P5-5 only).", ""]
    if report.get("job_matrix"):
        L += ["Jobs run (job, seeds, nights, simulated days):", "", "| Job | Seeds | Nights | Sim. days |", "|---|---|---|---|"]
        for m in report["job_matrix"]:
            L.append("| %s | %s | %s | %s |" % (m["job"], ",".join(map(str, m["seeds"])), ",".join(map(str, m["nights"])),
                                               m["sim_days"]))
    if report["gate"] == INCOMPLETE:
        L += ["", "INCOMPLETE: a check lacks runs or a scripted step could not be exercised (see the sections below)."]
    if report["bad_runs"]:
        L += ["", "Runs that did not finish cleanly:", ""]
        for b in report["bad_runs"]:
            L.append("- %s seed %s: %s, core errors %s" % (b["job"], b["seed"], b["status"], b["core_errors"]))
    return "\n".join(L + render_p5(p5)) + "\n"


def write_report(report: dict, out_dir) -> None:
    """``report.json`` and ``report.md`` in ``out_dir``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, sort_keys=False), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
