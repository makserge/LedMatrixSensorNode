"""Evaluation of the P3 pass criteria (P3 spec 11.7) from replay run results.

Matching (11.7.2, D33, D36): a place suggestion (``dwell_zone``/``short_task_zone``, its cells as of night N)
**finds** place P at N if IoU(cells, truth region of P at N) >= 0.6, or, for sofaA/sofaB, if
IoU(cells, truth region of sofaAB) >= 0.6 and the seat's truth maximum cell is in the cells. The sofa is found
**as one place** when some suggestion has IoU >= 0.6 against sofaAB. A **false** place suggestion has a
simulator label starting with ``FP`` at its first night; a **ghost** place is a false one labelled
``FP:mirror_*``, ``FP:inghost`` or ``FP:reflector``. The simulator's own measure (first night by label) is
reported beside every IoU result, and ``iou_fail_label_ok`` flags an IoU failure whose label matched; it
never turns a FAIL into a PASS.

Criteria (11.7.3): G1-G6 are gated, R1-R7 are reported. A criterion is INCOMPLETE when the results lack its
runs (for example the smoke and quick profiles); its partial numbers are still shown, with the k-of-n rule
scaled to the seeds present ("provisional"). ``exit_code`` is 1 when a gated criterion FAILs or a run is over
budget, failed or incomplete, else 0.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

IOU_MIN = 0.6
PLACE_TYPES = ("dwell_zone", "short_task_zone")
SEATS = ("sofaA", "sofaB")
GHOST_PREFIXES = ("FP:mirror_", "FP:inghost", "FP:reflector")
G1_WINDOWS = ("w0000_0600", "w2200_0600", "w2300_0700", "w0000_0700")
WORLDS = ("bed", "liv")
DAY_PLACES = ("desk", "table", "counter", "sofaA", "sofaB")
ALL_SEEDS = (1, 2, 3, 4, 5)
G1_NIGHT = 7
G4_NIGHT = 14
G3_NIGHTS = 21
GATE_CUT = 0.10
G2_CUT = 0.001
CM4_FACTOR = 10.0
DESIGN_CAPTURE = {"h300": (0.56, 0.60), "h900": (0.19, 0.22)}

# Simulator reference (P3 spec 11.7.4): first-suggestion nights per seed 1-5 by the simulator's label, from the
# published option-A runs (56 nights, the completed design's flags, no Night learning); None = not suggested.
SIM_FIRST = {
    "bed_none": {"desk": [4, 6, 6, 5, 5], "table": [5, 5, 6, 6, 6], "counter": [4, 5, 4, 4, 4],
                 "sofaAB": [7, 5, 6, 6, 7], "bed": [5, 6, 8, 12, 6], "armchair": [None, None, None, None, 52]},
    "liv_none": {"desk": [5, 5, 5, 5, 4], "table": [5, 6, 5, 5, 6], "counter": [5, 5, 4, 5, 5],
                 "sofaAB": [6, 5, 7, 6, 6], "armchair": [41, 36, 36, 12, None]},
    "bed_w0000_0600": {"desk": [4, 6, 6, 5, 5], "table": [5, 5, 6, 6, 6], "counter": [4, 5, 4, 4, 4],
                       "sofaAB": [7, 5, 6, 6, 7], "bed": [9, 13, 14, 50, 8], "armchair": [None, None, None, None, 53]},
    "liv_w0000_0600": {"desk": [5, 5, 5, 5, 4], "table": [5, 6, 5, 5, 6], "counter": [5, 5, 4, 5, 5],
                       "sofaAB": [6, 5, 7, 6, 6], "armchair": [41, 36, 36, 12, None]},
    "bed_w2200_0600": {"desk": [4, 6, 6, 5, 5], "table": [5, 5, 6, 6, 6], "counter": [5, 5, 4, 4, 5],
                       "sofaAB": [None, 5, 7, 7, None], "sofaB": [8, None, None, None, 7],
                       "armchair": [None, None, None, None, 53]},
    "liv_w2200_0600": {"desk": [5, 5, 5, 5, 4], "table": [5, 6, 5, 5, 6], "counter": [5, 6, 5, 5, 5],
                       "sofaAB": [6, 6, 7, 6, 7], "armchair": [41, 36, 34, 12, None]},
    "bed_w2300_0700": {"desk": [4, 6, 6, 5, 5], "table": [5, 5, 6, 6, 6], "counter": [4, 5, 4, 4, 4],
                       "sofaAB": [7, 5, 6, 6, 7], "armchair": [None, None, None, None, 53]},
    "liv_w2300_0700": {"desk": [5, 5, 5, 5, 4], "table": [5, 6, 5, 5, 6], "counter": [5, 6, 4, 5, 5],
                       "sofaAB": [6, 5, 7, 6, 6], "armchair": [41, 36, 34, 12, None]},
    "bed_w0000_0700": {"desk": [4, 6, 6, 5, 5], "table": [5, 5, 6, 6, 6], "counter": [4, 5, 4, 4, 4],
                       "sofaAB": [7, 5, 6, 6, 7], "bed": [12, 41, 50, None, None], "armchair": [None, None, None, None, 53]},
    "liv_w0000_0700": {"desk": [5, 5, 5, 5, 4], "table": [5, 6, 5, 5, 6], "counter": [5, 6, 4, 5, 5],
                       "sofaAB": [6, 5, 7, 6, 6], "armchair": [41, 36, 36, 12, None]},
}
# The simulator's cut shares per seed 1-5 over 56 days (its truth counters), places with any cut.
SIM_CUT = {
    "bed_w0000_0600": {"bed": [0.735, 0.73, 0.718, 0.73, 0.731]},
    "bed_w2200_0600": {"bed": [0.832, 0.821, 0.814, 0.821, 0.828], "desk": [0.004, 0.003, 0.003, 0.003, 0.002],
                       "counter": [0.199, 0.212, 0.208, 0.198, 0.145], "sofaA": [0.141, 0.176, 0.162, 0.194, 0.197],
                       "sofaB": [0.027, 0.045, 0.029, 0.031, 0.032]},
    "liv_w2200_0600": {"desk": [0.003, 0.002, 0.001, 0.0, 0.001], "counter": [0.197, 0.186, 0.196, 0.203, 0.155],
                       "sofaA": [0.142, 0.131, 0.222, 0.16, 0.216], "sofaB": [0.059, 0.032, 0.03, 0.032, 0.029]},
    "bed_w2300_0700": {"bed": [0.927, 0.918, 0.915, 0.923, 0.921], "counter": [0.026, 0.025, 0.036, 0.063, 0.04],
                       "sofaA": [0.002, 0.0, 0.002, 0.0, 0.001]},
    "liv_w2300_0700": {"counter": [0.034, 0.05, 0.03, 0.045, 0.043], "sofaA": [0.005, 0.002, 0.005, 0.002, 0.007]},
    "bed_w0000_0700": {"bed": [0.838, 0.833, 0.825, 0.837, 0.831], "counter": [0.019, 0.022, 0.032, 0.063, 0.04]},
    "liv_w0000_0700": {"counter": [0.024, 0.046, 0.022, 0.035, 0.043]},
}

PASS, FAIL, INCOMPLETE, REPORTED = "PASS", "FAIL", "INCOMPLETE", "REPORTED"


# ---------------------------------------------------------------- per run
def _finds(s: dict, place: str) -> bool:
    iou = s.get("iou") or {}
    if iou.get(place, 0.0) >= IOU_MIN:
        return True
    return place in SEATS and iou.get("sofaAB", 0.0) >= IOU_MIN and place in (s.get("max_in") or [])


def _is_ghost(label: str) -> bool:
    return any(label.startswith(p) for p in GHOST_PREFIXES)


def summarize_run(res: dict) -> dict:
    """The per-run facts the criteria need, from one run result (P3 spec 11.7.1)."""
    nights = sorted(res.get("nights") or [], key=lambda n: n["night"])
    places = set()
    for n in nights:
        places.update((n.get("truth") or {}).keys())
    places.update((res.get("truth") or {}).get("cut", {}).keys())
    if "sofaA" in places:
        places.add("sofaAB")
    found, found_own, label_first = {}, {}, {}
    first_seen = {}
    false_places, ghost_places, reflections = [], [], []
    bed_ids, split_nights, g5b_viol = set(), {}, []
    share_split_nights = {}
    for n in nights:
        N = n["night"]
        for s in n["suggestions"]:
            sid = s["id"]
            if s["type"] in PLACE_TYPES:
                for p in places:
                    if p not in found and _finds(s, p):
                        found[p] = N
                    if p not in found_own and (s.get("iou") or {}).get(p, 0.0) >= IOU_MIN:
                        found_own[p] = N
                if sid not in first_seen:
                    lab = s.get("sim_label") or ""
                    first_seen[sid] = (s["first_night"], lab)
                    if lab.startswith("FP"):
                        false_places.append({"id": sid, "night": s["first_night"], "label": lab, "type": s["type"],
                                             "cx": s["cx"], "cy": s["cy"]})
                        if _is_ghost(lab):
                            ghost_places.append(false_places[-1])
                    elif lab.startswith("TP:"):
                        p = lab[3:]
                        for q in [p] + (list(SEATS) if p == "sofaAB" else []):
                            if q not in label_first or s["first_night"] < label_first[q]:
                                label_first[q] = s["first_night"]
                if (s.get("iou") or {}).get("bed", 0.0) >= IOU_MIN:
                    bed_ids.add(sid)
                if s.get("variant_evidence_split"):
                    split_nights.setdefault(sid, []).append(N)
                if s.get("variant_share_split"):
                    share_split_nights.setdefault(sid, []).append(N)
                if s.get("kind") == "bed" and s.get("variant_evidence_split") and not s.get("variant_suppressed"):
                    g5b_viol.append({"id": sid, "night": N})
            elif s["type"] == "reflection" and sid not in first_seen:
                first_seen[sid] = (s["first_night"], s.get("sim_label") or "")
                reflections.append({"id": sid, "night": s["first_night"], "label": s.get("sim_label")})
    bed_split = sorted({N for sid in bed_ids for N in split_nights.get(sid, [])})
    bed_share_split = sorted({N for sid in bed_ids for N in share_split_nights.get(sid, [])})
    truth = res.get("truth") or {}
    timing = res.get("timing") or {}
    labels = res.get("labels") or {}
    return {
        "job": res.get("job"), "seed": res.get("seed"), "world": res.get("world"), "window": res.get("window"),
        "variant": res.get("variant") or "", "gated": res.get("gated"), "status": res.get("status"),
        "nights": nights[-1]["night"] if nights else 0,
        "found": found, "found_own": found_own, "label_first": label_first,
        "false_places": false_places, "ghost_places": ghost_places, "reflections": reflections,
        "bed_split_nights": bed_split, "bed_share_split_nights": bed_share_split, "g5b_violations": g5b_viol,
        "cut": truth.get("cut") or {}, "core": truth.get("core") or [],
        "capture": (res.get("capture") or {}).get("seated"),
        "labels": labels, "counters": res.get("counters") or {}, "stream": res.get("stream") or {},
        "cpu_s": timing.get("cpu_s"), "sim_days": timing.get("sim_days"), "cpu_s_per_day": timing.get("cpu_s_per_day"),
        "core_errors": res.get("core_errors", 0), "first_error": res.get("first_error"),
    }


# ---------------------------------------------------------------- helpers
def _group(runs: list, world: str, window: str, variant: str = "") -> list:
    return sorted([r for r in runs if r["world"] == world and r["window"] == window and r["variant"] == variant],
                  key=lambda r: r["seed"])


def _need(n_seeds: int, k: int = 4, of: int = 5) -> int:
    """The k-of-n rule; scaled ('provisional') when fewer than ``of`` seeds are present."""
    return k if n_seeds >= of else max(1, math.ceil(k / of * n_seeds))


def _complete(group: list, min_nights: int) -> bool:
    return (sorted(r["seed"] for r in group) == list(ALL_SEEDS)
            and all(r["nights"] >= min_nights and r["status"] == "ok" for r in group))


def _mean(vals) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _combine(statuses: list) -> str:
    if not statuses:
        return INCOMPLETE
    if FAIL in statuses:
        return FAIL
    if INCOMPLETE in statuses:
        return INCOMPLETE
    return PASS


def _sim_key(world: str, window: str) -> str:
    return "%s_%s" % (world, window)


def gated_set(group: list, places=None) -> dict:
    """Core places with < 10% of their true use inside the window (seat level, D36), with the reasons."""
    n = len(group)
    out = {}
    cand = places or sorted({p for r in group for p in r["cut"]})
    for p in cand:
        core_n = sum(1 for r in group if p in r["core"])
        cut = _mean(r["cut"].get(p) for r in group)
        out[p] = {"core_seeds": core_n, "cut_mean": cut,
                  "gated": n > 0 and core_n * 2 > n and cut is not None and cut < GATE_CUT}
    return out


def _place_rule(group: list, place: str, deadline: int) -> dict:
    nights = [r["found"].get(place) for r in group]
    labels = [r["label_first"].get(place) for r in group]
    ok = sum(1 for v in nights if v is not None and v <= deadline)
    need = _need(len(group))
    flag = [r["seed"] for r in group
            if not (r["found"].get(place) is not None and r["found"][place] <= deadline)
            and r["label_first"].get(place) is not None and r["label_first"][place] <= deadline]
    return {"seeds": [r["seed"] for r in group], "found_nights": nights, "label_nights": labels,
            "ok_seeds": ok, "need": need, "pass": ok >= need, "iou_fail_label_ok": flag}


# ---------------------------------------------------------------- criteria
def crit_g1(runs: list) -> dict:
    details, statuses = [], []
    for world in WORLDS:
        for win in G1_WINDOWS:
            g = _group(runs, world, win)
            complete = _complete(g, G1_NIGHT)
            entry = {"world": world, "window": win, "runs": len(g), "complete": complete}
            if not g:
                entry["status"] = INCOMPLETE
                statuses.append(INCOMPLETE)
                details.append(entry)
                continue
            gs = gated_set(g)
            entry["gated_set"] = sorted(p for p, v in gs.items() if v["gated"])
            entry["not_gated"] = {p: v for p, v in gs.items() if not v["gated"]}
            places = {}
            for p in entry["gated_set"]:
                places[p] = _place_rule(g, p, G1_NIGHT) | {"cut_mean": gs[p]["cut_mean"]}
            entry["places"] = places
            st = PASS if all(v["pass"] for v in places.values()) else FAIL
            if not complete:
                st = FAIL if st == FAIL and len(g) == 5 else INCOMPLETE
                entry["provisional"] = True
            entry["status"] = st
            statuses.append(st)
            details.append(entry)
    return {"id": "G1", "gated": True, "status": _combine(statuses),
            "title": "Every core place with < 10% of its true use inside the window is suggested by night 7 at "
                     "IoU >= 0.6 in >= 4/5 seeds (each window x both worlds)",
            "details": details}


def crit_g2(runs: list) -> dict:
    details, statuses = [], []
    for world in WORLDS:
        g = _group(runs, world, "w0000_0600")
        complete = _complete(g, G1_NIGHT)
        entry = {"world": world, "runs": len(g), "complete": complete}
        if not g:
            statuses.append(INCOMPLETE)
            entry["status"] = INCOMPLETE
            details.append(entry)
            continue
        world_check = {p: [r["cut"].get(p) for r in g] for p in DAY_PLACES}
        world_bad = {p: v for p, v in world_check.items() if any(x is None or x >= G2_CUT for x in v)}
        gs = gated_set(g, DAY_PLACES)
        places = {p: _place_rule(g, p, G1_NIGHT) | {"gated": gs[p]["gated"]} for p in DAY_PLACES}
        sofa = _place_rule(g, "sofaAB", G1_NIGHT)
        seat_only = [{"seed": r["seed"], "seats": {s: r["found_own"].get(s) for s in SEATS if r["found_own"].get(s) is not None}}
                     for r in g if r["found"].get("sofaAB") is None and any(r["found_own"].get(s) is not None for s in SEATS)]
        ok = (not world_bad and all(v["gated"] and v["pass"] for v in places.values()) and sofa["pass"])
        st = PASS if ok else FAIL
        if not complete:
            st = FAIL if st == FAIL and len(g) == 5 else INCOMPLETE
            entry["provisional"] = True
        entry.update({"cut_by_seed": world_check, "world_check_failure": world_bad, "places": places,
                      "sofa_as_one": sofa, "seat_only_suggestions": seat_only, "status": st})
        statuses.append(st)
        details.append(entry)
    return {"id": "G2", "gated": True, "status": _combine(statuses),
            "title": "00:00-06:00: no day place has use inside the window; desk, table, counter and the sofa as "
                     "one place are all gated and found by night 7 (both worlds)",
            "details": details}


def crit_g3(runs: list) -> dict:
    details, statuses = [], []
    for world in WORLDS:
        g = _group(runs, world, "w0000_0600")
        complete = _complete(g, G3_NIGHTS)
        fps = [dict(fp, seed=r["seed"]) for r in g for fp in r["false_places"] if fp["night"] <= G3_NIGHTS]
        st = FAIL if fps else (PASS if complete else INCOMPLETE)
        details.append({"world": world, "runs": len(g), "complete": complete, "false_places": fps, "status": st})
        statuses.append(st)
    return {"id": "G3", "gated": True, "status": _combine(statuses),
            "title": "0 false place suggestions (00:00-06:00, both worlds, 5 seeds, nights <= 21)", "details": details}


def crit_g4(runs: list) -> dict:
    g = _group(runs, "bed", "w0000_0600")
    complete = _complete(g, G4_NIGHT)
    if not g:
        return {"id": "G4", "gated": True, "status": INCOMPLETE, "title": _G4_TITLE, "details": []}
    rule = _place_rule(g, "bed", G4_NIGHT)
    st = PASS if rule["pass"] else FAIL
    if not complete:
        st = FAIL if st == FAIL and len(g) == 5 else INCOMPLETE
    cut = _mean(r["cut"].get("bed") for r in g)
    return {"id": "G4", "gated": True, "status": st, "title": _G4_TITLE,
            "details": [rule | {"complete": complete, "cut_mean": cut, "provisional": not complete,
                                "bed_label": [r["labels"].get("bed", {}).get("night") for r in g]}]}


_G4_TITLE = "The bed, Night learning off, found by the learner by night 14 in >= 4/5 seeds (bedroom, 00:00-06:00)"


def crit_g5a(runs: list) -> dict:
    g = _group(runs, "bed", "w0000_0600")
    complete = _complete(g, 1)
    split = [{"seed": r["seed"], "nights": r["bed_split_nights"]} for r in g if r["bed_split_nights"]]
    share = [{"seed": r["seed"], "nights": r["bed_share_split_nights"]} for r in g if r["bed_share_split_nights"]]
    st = FAIL if split else (PASS if complete else INCOMPLETE)
    return {"id": "G5a", "gated": True, "status": st,
            "title": "No bed time-variant split by the evidence rule (bedroom, 00:00-06:00, 5 seeds, all nights)",
            "details": [{"runs": len(g), "complete": complete, "runs_with_split": split,
                         "share_rule_splits_logged": share}]}


def crit_g5b(runs: list, unit_ok: Optional[bool]) -> dict:
    g = _group(runs, "bed", "w0000_0600")
    viol = [{"seed": r["seed"], "violations": r["g5b_violations"]} for r in g if r["g5b_violations"]]
    if viol or unit_ok is False:
        st = FAIL
    elif unit_ok is None or not _complete(g, 1):
        st = INCOMPLETE
    else:
        st = PASS
    return {"id": "G5b", "gated": True, "status": st,
            "title": "The OA 7.8 rule suppresses a split for a bed of kind 'bed' (unit test + replay)",
            "details": [{"unit_test": unit_ok, "replay_runs": len(g), "replay_violations": viol}]}


def crit_g6(runs: list) -> dict:
    g = _group(runs, "bed", "w0000_0600", "ghost")
    complete = _complete(g, G3_NIGHTS)
    ghosts = [dict(x, seed=r["seed"]) for r in g for x in r["ghost_places"]]
    st = FAIL if ghosts else (PASS if complete else INCOMPLETE)
    return {"id": "G6", "gated": True, "status": st,
            "title": "0 ghost places with moderate seated-person ghosts (bedroom 00:00-06:00, gs 0.15, gi 0.1)",
            "details": [{"runs": len(g), "complete": complete, "ghost_places": ghosts,
                         "false_places": [dict(x, seed=r["seed"]) for r in g for x in r["false_places"]],
                         "reflections": [dict(x, seed=r["seed"]) for r in g for x in r["reflections"]]}]}


def rep_r1(runs: list) -> dict:
    rows = []
    for world in WORLDS:
        base = {r["seed"]: r for r in _group(runs, world, "none")}
        win = {r["seed"]: r for r in _group(runs, world, "w0000_0600")}
        sim_b = SIM_FIRST.get(_sim_key(world, "none"), {})
        sim_w = SIM_FIRST.get(_sim_key(world, "w0000_0600"), {})
        for p in ("desk", "table", "counter", "sofaAB", "sofaA", "sofaB", "bed"):
            if world == "liv" and p == "bed":
                continue
            seeds = sorted(set(base) | set(win))
            if not seeds:
                continue
            row = {"world": world, "place": p, "seeds": seeds,
                   "none": [base[s]["found"].get(p) if s in base else None for s in seeds],
                   "w0000_0600": [win[s]["found"].get(p) if s in win else None for s in seeds],
                   "none_label": [base[s]["label_first"].get(p) if s in base else None for s in seeds],
                   "w0000_0600_label": [win[s]["label_first"].get(p) if s in win else None for s in seeds],
                   "sim_none": [sim_b.get(p, [None] * 5)[s - 1] for s in seeds],
                   "sim_w0000_0600": [sim_w.get(p, [None] * 5)[s - 1] for s in seeds]}
            row["same_night"] = sum(1 for s in seeds if s in base and s in win
                                    and base[s]["found"].get(p) == win[s]["found"].get(p))
            rows.append(row)
    return {"id": "R1", "gated": False, "status": REPORTED,
            "title": "Expected outcome: with 00:00-06:00 every core place on the same night as without a window",
            "details": rows}


def rep_r2(runs: list) -> dict:
    rows = []
    for world in WORLDS:
        g = _group(runs, world, "w2200_0600")
        sim = SIM_FIRST.get(_sim_key(world, "w2200_0600"), {})
        for r in g:
            rows.append({"world": world, "seed": r["seed"], "sofa_as_one": r["found"].get("sofaAB"),
                         "sofaA_own": r["found_own"].get("sofaA"), "sofaB_own": r["found_own"].get("sofaB"),
                         "sofaA": r["found"].get("sofaA"), "sofaB": r["found"].get("sofaB"),
                         "counter": r["found"].get("counter"),
                         "sim_sofaAB": sim.get("sofaAB", [None] * 5)[r["seed"] - 1],
                         "sim_sofaB": sim.get("sofaB", [None] * 5)[r["seed"] - 1],
                         "sim_counter": sim.get("counter", [None] * 5)[r["seed"] - 1]})
    summary = {}
    for world in WORLDS:
        ws = [x for x in rows if x["world"] == world]
        if ws:
            summary[world] = {"sofa_as_one": sum(1 for x in ws if x["sofa_as_one"] is not None),
                              "seat_B_only": sum(1 for x in ws if x["sofa_as_one"] is None and x["sofaB_own"] is not None),
                              "counter": sum(1 for x in ws if x["counter"] is not None), "runs": len(ws)}
    return {"id": "R2", "gated": False, "status": REPORTED,
            "title": "22:00-06:00: bedroom sofa as one place in 3/5, seat B only in 2/5 (nights 7-8); counter 5/5 "
                     "(simulator)", "details": {"summary": summary, "runs": rows}}


def rep_r3(runs: list) -> dict:
    rows = []
    for world in WORLDS:
        for win in G1_WINDOWS:
            g = _group(runs, world, win)
            if not g:
                continue
            places = sorted({p for r in g for p in r["cut"]})
            sim = SIM_CUT.get(_sim_key(world, win), {})
            for p in places:
                m = _mean(r["cut"].get(p) for r in g)
                rows.append({"world": world, "window": win, "place": p, "cut_mean": m,
                             "cut_by_seed": [r["cut"].get(p) for r in g], "ge_10pct": m is not None and m >= GATE_CUT,
                             "sim_cut_mean_56d": _mean(sim.get(p, [])) if p in sim else 0.0})
    return {"id": "R3", "gated": False, "status": REPORTED,
            "title": "Places with >= 10% of their use inside the window (reported with the cut shares)",
            "details": rows}


def rep_r4(runs: list) -> dict:
    rows = []
    for variant in ("", "h300", "h900", "v3h300"):
        for r in _group(runs, "bed", "w0000_0600", variant):
            if variant == "" and r["seed"] > 3:
                continue
            rows.append({"variant": variant or "base", "seed": r["seed"], "capture": r["capture"],
                         "found": {p: r["found"].get(p) for p in ("desk", "table", "counter", "sofaAB", "bed")},
                         "false_places": len(r["false_places"]),
                         "design_capture": DESIGN_CAPTURE.get(variant)})
    return {"id": "R4", "gated": False, "status": REPORTED,
            "title": "Dropout sweep (Hm 300, 900; Vm 3 + Hm 300): seated capture, first nights, false places",
            "details": rows}


def rep_r5(runs: list) -> dict:
    rows = []
    for r in runs:
        lb = r["labels"] or {}
        rows.append({"job": r["job"], "seed": r["seed"],
                     "desk": {k: (lb.get("desk") or {}).get(k) for k in ("ok", "night", "attempts", "kind")},
                     "bed": ({k: (lb.get("bed") or {}).get(k) for k in ("ok", "night", "attempts", "kind")}
                             if "bed" in lb else None),
                     "recall": lb.get("recall"), "precision": lb.get("precision")})
    return {"id": "R5", "gated": False, "status": REPORTED, "title": "'Mark spot here' labels: success night, "
            "attempts, recall, precision", "details": rows}


def rep_r6(runs: list) -> dict:
    base = {r["seed"]: r for r in _group(runs, "bed", "w0000_0600")}
    rows = []
    for r in _group(runs, "bed", "w0000_0600", "faults"):
        b = base.get(r["seed"])
        diffs = {}
        if b is not None:
            horizon = r["nights"]             # compare within the fault run's nights (14) only
            for p in ("desk", "table", "counter", "sofaAB", "bed"):
                x = r["found"].get(p)
                y = b["found"].get(p)
                y = y if y is not None and y <= horizon else None
                if x is None and y is None:
                    continue
                diffs[p] = (x - y) if (x is not None and y is not None) else None    # None: found in one only
        rows.append({"seed": r["seed"], "found": r["found"], "base_found": b["found"] if b else None,
                     "night_diff": diffs, "within_1": all(v is not None and abs(v) <= 1 for v in diffs.values()) if b else None,
                     "counters": {k: r["counters"].get(k) for k in ("seq_gaps", "frames_lost", "duplicates", "overlaps",
                                                                   "boots", "stale_boot", "censor_events", "parse_errors")},
                     "stream": r["stream"]})
    return {"id": "R6", "gated": False, "status": REPORTED,
            "title": "Faults: first nights within +-1 of the fault-free run; counters", "details": rows}


def rep_r7(runs: list) -> dict:
    per = [r["cpu_s_per_day"] for r in runs if r["cpu_s_per_day"]]
    total_cpu = sum(r["cpu_s"] or 0 for r in runs)
    total_days = sum(r["sim_days"] or 0 for r in runs)
    mean = total_cpu / total_days if total_days else None
    return {"id": "R7", "gated": False, "status": REPORTED,
            "title": "Cost: CPU s per simulated day (generator + core) and projected CM4 share",
            "details": {"runs": len(runs), "cpu_s": round(total_cpu, 1), "sim_days": round(total_days, 2),
                        "cpu_s_per_day_mean": round(mean, 3) if mean else None,
                        "cpu_s_per_day_max": max(per) if per else None,
                        "projected_cm4_pct_upper": round(mean * CM4_FACTOR / 86400 * 100, 4) if mean else None,
                        "note": "includes the frame generator; the core alone is measured by resource_probe.py"}}


def evaluate(results: list, unit_g5b: Optional[bool] = None) -> dict:
    """The report (``report.json`` content) for a list of run results."""
    runs = [summarize_run(r) for r in results]
    criteria = [crit_g1(runs), crit_g2(runs), crit_g3(runs), crit_g4(runs), crit_g5a(runs),
                crit_g5b(runs, unit_g5b), crit_g6(runs)]
    reported = [rep_r1(runs), rep_r2(runs), rep_r3(runs), rep_r4(runs), rep_r5(runs), rep_r6(runs), rep_r7(runs)]
    bad_runs = [{"job": r["job"], "seed": r["seed"], "status": r["status"], "core_errors": r["core_errors"],
                 "first_error": r["first_error"]} for r in runs if r["status"] != "ok" or r["core_errors"]]
    gate = _combine([c["status"] for c in criteria])
    failed = [c["id"] for c in criteria if c["status"] == FAIL]
    exit_code = 1 if failed or any(b["status"] != "ok" for b in bad_runs) else 0
    params = sorted({r.get("params") for r in results if r.get("params")})
    return {"schema": 1, "runs": len(runs), "params_digests": params, "gate": gate, "failed": failed,
            "exit_code": exit_code, "bad_runs": bad_runs, "criteria": criteria, "reported": reported,
            "run_summaries": [{k: r[k] for k in ("job", "seed", "status", "nights", "found", "label_first",
                                                  "false_places", "capture", "cpu_s_per_day")} for r in runs]}


# ---------------------------------------------------------------- report files
def _fmt(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return "%.3f" % v
    return str(v)


def _nights(vals) -> str:
    return " ".join(_fmt(v) for v in vals)


def write_report(report: dict, out_dir: Path) -> None:
    """``report.json`` and ``report.md`` in ``out_dir``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, sort_keys=False), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")


def render_markdown(report: dict) -> str:
    L = ["# P3 replay report", ""]
    L.append("Gate (G1-G6): **%s**%s. Runs: %d. Params digest: %s." % (
        report["gate"], (" (failed: %s)" % ", ".join(report["failed"])) if report["failed"] else "",
        report["runs"], ", ".join(report["params_digests"]) or "–"))
    if report["gate"] == INCOMPLETE:
        L.append("")
        L.append("INCOMPLETE: the results do not contain every run a criterion needs (use `--profile p3`). "
                 "Partial numbers below use the k-of-n rule scaled to the seeds present.")
    if report["bad_runs"]:
        L += ["", "Runs that did not finish cleanly:", ""]
        for b in report["bad_runs"]:
            L.append("- %s seed %s: %s, core errors %s" % (b["job"], b["seed"], b["status"], b["core_errors"]))
    L += ["", "## Gated criteria", "", "| ID | Status | Criterion |", "|---|---|---|"]
    for c in report["criteria"]:
        L.append("| %s | %s | %s |" % (c["id"], c["status"], c["title"]))
    for c in report["criteria"]:
        L += ["", "### %s: %s" % (c["id"], c["status"]), "", c["title"], ""]
        if c["id"] in ("G1",):
            for d in c["details"]:
                L.append("**%s %s** (%d runs%s): %s" % (d["world"], d["window"], d["runs"],
                                                       ", provisional" if d.get("provisional") else "", d["status"]))
                if "places" in d:
                    L += ["", "| Place | cut | found night (IoU) per seed | by label | ok/need | IoU fail, label ok |",
                          "|---|---|---|---|---|---|"]
                    for p, v in d["places"].items():
                        L.append("| %s | %s | %s | %s | %d/%d | %s |" % (
                            p, _fmt(v.get("cut_mean")), _nights(v["found_nights"]), _nights(v["label_nights"]),
                            v["ok_seeds"], v["need"], v["iou_fail_label_ok"] or ""))
                    if d.get("not_gated"):
                        L.append("")
                        L.append("Not gated: " + ", ".join("%s (core in %d seeds, cut %s)" % (p, v["core_seeds"], _fmt(v["cut_mean"]))
                                                          for p, v in d["not_gated"].items()))
                L.append("")
        elif c["id"] == "G2":
            for d in c["details"]:
                L.append("**%s** (%d runs): %s" % (d["world"], d["runs"], d["status"]))
                if "places" in d:
                    if d["world_check_failure"]:
                        L.append("World check failure (cut >= 0.1%%): %s" % d["world_check_failure"])
                    L += ["", "| Place | gated | found night per seed | by label | ok/need |", "|---|---|---|---|---|"]
                    for p, v in list(d["places"].items()) + [("sofa as one", d["sofa_as_one"] | {"gated": True})]:
                        L.append("| %s | %s | %s | %s | %d/%d |" % (p, v.get("gated"), _nights(v["found_nights"]),
                                                                 _nights(v["label_nights"]), v["ok_seeds"], v["need"]))
                    if d["seat_only_suggestions"]:
                        L.append("")
                        L.append("Seat-only suggestions: %s" % d["seat_only_suggestions"])
                L.append("")
        else:
            for d in c["details"]:
                L.append("- " + json.dumps(d, sort_keys=True))
    L += ["", "## Reported", ""]
    for c in report["reported"]:
        L += ["### %s" % c["id"], "", c["title"], ""]
        if c["id"] == "R1":
            L += ["| World | Place | none (IoU) | 00:00-06:00 (IoU) | none (label) | 00:00-06:00 (label) | simulator none | simulator 00:00-06:00 | same night |",
                  "|---|---|---|---|---|---|---|---|---|"]
            for r in c["details"]:
                L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %d |" % (
                    r["world"], r["place"], _nights(r["none"]), _nights(r["w0000_0600"]), _nights(r["none_label"]),
                    _nights(r["w0000_0600_label"]), _nights(r["sim_none"]), _nights(r["sim_w0000_0600"]), r["same_night"]))
        elif c["id"] == "R3":
            L += ["| World | Window | Place | cut (mean) | >= 10% | simulator, 56 days |", "|---|---|---|---|---|---|"]
            for r in c["details"]:
                if (r["cut_mean"] or 0) > 0 or r["sim_cut_mean_56d"]:
                    L.append("| %s | %s | %s | %s | %s | %s |" % (r["world"], r["window"], r["place"], _fmt(r["cut_mean"]),
                                                                 r["ge_10pct"], _fmt(r["sim_cut_mean_56d"])))
        elif c["id"] == "R4":
            L += ["| Variant | Seed | seated capture | design capture | desk | table | counter | sofa | bed | false |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
            for r in c["details"]:
                f = r["found"]
                L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    r["variant"], r["seed"], _fmt(r["capture"]), _fmt(r["design_capture"]), _fmt(f["desk"]),
                    _fmt(f["table"]), _fmt(f["counter"]), _fmt(f["sofaAB"]), _fmt(f["bed"]), r["false_places"]))
        elif c["id"] == "R5":
            L += ["| Job | Seed | desk ok/night/attempts | bed ok/night/attempts | recall | precision |",
                  "|---|---|---|---|---|---|"]
            for r in c["details"]:
                d, b = r["desk"], r["bed"]
                L.append("| %s | %s | %s/%s/%s | %s | %s | %s |" % (
                    r["job"], r["seed"], d["ok"], _fmt(d["night"]), d["attempts"],
                    ("%s/%s/%s" % (b["ok"], _fmt(b["night"]), b["attempts"])) if b else "–",
                    _fmt(r["recall"]), _fmt(r["precision"])))
        elif c["id"] == "R7":
            L.append("```")
            L.append(json.dumps(c["details"], indent=1))
            L.append("```")
        else:
            L.append("```")
            L.append(json.dumps(c["details"], indent=1, sort_keys=True))
            L.append("```")
        L.append("")
    return "\n".join(L) + "\n"
