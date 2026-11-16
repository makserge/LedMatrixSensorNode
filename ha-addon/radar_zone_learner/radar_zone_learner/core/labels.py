"""'Mark spot here' labels (P3 spec 7.10; OA 6.3, 7.6): stays in progress, label creation, kinds from
the spot name, linking to place suggestions, and label precision and recall.
"""
from __future__ import annotations

import math
from typing import Optional

PLACE_TYPES = ("dwell_zone", "short_task_zone")
MAX_LABELS = 100


def kind_of_name(name: str, bed_names) -> str:
    """'bed' when the whole name, trimmed and case-insensitive, is one of bed_names (OA 7.6)."""
    return "bed" if name.strip().casefold() in tuple(n.casefold() for n in bed_names) else "manual"


def stays_in_progress(L) -> list:
    """Stays of >= mark_min_stay_s in progress: one per live confirmed track whose fragment buffer
    spans >= frag_min_s, joined to the pending stay it would bridge to. [(t0, t1, cx, cy, n)].
    A run that already meets the EPP rule (sd < epp_sd_m over >= epp_min_s) is a static ghost, not a
    stay, and is skipped (integration fix: the replay labelled a fan while the person was hidden)."""
    p = L.p
    out = []
    for tr in L.tracker.live:
        tb = tr.bh
        if not tr.confirmed or tb is None or not tb.active:
            continue
        run = tb.run()
        if run is None:
            continue
        t0, t1, cx, cy, n, sd = run
        if t1 - t0 < p.frag_min_s:
            continue
        if sd < p.epp_sd_m and t1 - t0 >= p.epp_min_s:
            continue        # an EPP ghost (spec 7.4, OA 7.3) never becomes a stay, so it is no stay in progress
        st = L.bh.bridge_target(t0, cx, cy)
        if st is not None:
            w = st.n + n
            t0 = st.t0
            cx = (st.cx * st.n + cx * n) / w
            cy = (st.cy * st.n + cy * n) / w
            n = w
        if t1 - t0 >= p.mark_min_stay_s:
            out.append((t0, t1, cx, cy, n))
    return out


def mark_candidates(L, now_ms: int) -> tuple:
    """``(stays, detail)`` of ``mark_candidate_sources`` (the 0.7.1 shape, kept for callers that need no source)."""
    merged, detail, _ = mark_candidate_sources(L, now_ms)
    return merged, detail


def mark_candidate_sources(L, now_ms: int) -> tuple:
    """The stays 'Mark spot here' can label (0.7.1): the live stays (``stays_in_progress``) and the recent pending stays
    (the track was lost, but its last fragment ended within ``mark_grace_s`` of the press and the stay is at least
    ``mark_min_stay_s`` long), merged: candidates within ``bridge_dist_m`` of each other are one, at the newest one's
    position. Returns ``(stays, detail)``: ``stays`` as ``stays_in_progress`` ([(t0, t1, cx, cy, n)], newest first) and
    ``detail``, a dict of counts and seconds only (no positions) that the shell reports with a refused press. 0.7.2: a third
    value, ``sources``, parallel to ``stays``: ``("live", 0.0)`` or ``("recent", age_s)`` for each merged stay, so a press can
    say whether the chosen stay came from a live stay or only from a recent pending stay (which stay is chosen is unchanged)."""
    p = L.p
    now_s = now_ms / 1000.0
    live = stays_in_progress(L)
    best = 0.0
    n_live = 0
    for tr in L.tracker.live:
        if not tr.confirmed:
            continue
        n_live += 1
        tb = tr.bh
        run = tb.run() if tb is not None and tb.active else None
        if run is not None:
            best = max(best, run[1] - run[0])
    recent = 0
    cands = [c + ("live", 0.0) for c in live]
    for st in L.bh.pending:
        age = now_s - st.t1
        if age < 0 or age > p.mark_grace_s:
            continue
        recent += 1
        if st.t1 - st.t0 >= p.mark_min_stay_s:
            cands.append((st.t0, st.t1, st.cx, st.cy, st.n, "recent", round(age, 1)))
    cands.sort(key=lambda c: c[1], reverse=True)
    merged: list = []
    sources: list = []
    for c in cands:
        if not any(math.hypot(c[2] - m[2], c[3] - m[3]) <= p.bridge_dist_m for m in merged):
            merged.append(c[:5])
            sources.append((c[5], c[6]))
    last = getattr(L, "last_det_ms", None)
    detail = {"live_confirmed_tracks": n_live, "best_run_s": round(best, 1),
              "last_target_age_s": None if last is None else round(max(0.0, (now_ms - last) / 1000.0), 1),
              "recent_stays": recent}
    if len(merged) > 1:
        detail["stays"] = len(merged)
    return merged, detail, sources


def make_label(L, stay: tuple, now_ms: int, name: Optional[str] = None, kind: Optional[str] = None) -> dict:
    """A label at the stay. ``name`` and ``kind`` override the spot name and the kind it gives (the bed item's
    'Mark spot here' makes kind bed named 'Bed', OA 7.6)."""
    p = L.p
    _, _, cx, cy, _ = stay
    if name is None:
        name = L.spot_name
    lid = "l%04d" % L.next_label_n
    L.next_label_n += 1
    tr = p.time_round_s
    return {"id": lid, "created_s": int(now_ms // 1000 // tr) * tr, "x": round(cx, 3), "y": round(cy, 3),
            "name": name, "kind": kind if kind is not None else kind_of_name(name, p.bed_label_names),
            "cells": L.geom.cells_within(cx, cy, p.label_radius_m)}


def find_same(labels: list, cx: float, cy: float, name: str, radius_m: float) -> Optional[dict]:
    """The label of the same name within radius_m of (cx, cy), or None: a press at a spot that is marked
    already adds nothing (a stuck automation or a double click must not fill the state)."""
    for lb in labels:
        if lb["name"] == name and math.hypot(lb["x"] - cx, lb["y"] - cy) <= radius_m:
            return lb
    return None


def add_label(L, lb: dict) -> None:
    """Appends a label; above MAX_LABELS the oldest manual label goes (a 'bed' label only when none is left)."""
    labels = L.label_list
    labels.append(lb)
    while len(labels) > MAX_LABELS:
        k = next((i for i, x in enumerate(labels[:-1]) if x["kind"] != "bed"), 0)
        del labels[k]


def link_one(s: dict, labels: list, geom) -> None:
    """A place suggestion with a 'bed' label's position in dilate(cells) gets kind bed and the
    label's name; with a 'manual' label only the name. 'bed' wins over 'manual'."""
    if s["type"] not in PLACE_TYPES:
        return
    dil = None
    bed = None
    manual = None
    for lb in sorted(labels, key=lambda d: d["id"]):
        c = geom.cell_of(lb["x"], lb["y"])
        if c < 0:
            continue
        if dil is None:
            dil = geom.dilate(s["cells"])
        if c not in dil:
            continue
        if lb["kind"] == "bed" and bed is None:
            bed = lb
        elif lb["kind"] != "bed" and manual is None:
            manual = lb
    if bed is not None:
        s["kind"] = "bed"
        s["name_hint"] = bed["name"] or "bed"
    elif manual is not None and manual["name"]:
        s["name_hint"] = manual["name"]


def match(lb: dict, s: dict, geom, match_m: float) -> bool:
    c = geom.cell_of(lb["x"], lb["y"])
    if c >= 0 and c in geom.dilate(s["cells"]):
        return True
    return math.hypot(lb["x"] - s["cx"], lb["y"] - s["cy"]) <= match_m


def precision_recall(labels: list, suggestions: list, geom, match_m: float) -> tuple:
    """(label_recall, label_precision) over the labels and the shadow place suggestions; None when
    the denominator is 0 (OA 12 P3)."""
    places = [s for s in suggestions if s["type"] in PLACE_TYPES and s["state"] == "shadow"]
    recall: Optional[float] = None
    precision: Optional[float] = None
    if labels:
        hit = sum(1 for lb in labels if any(match(lb, s, geom, match_m) for s in places))
        recall = round(hit / len(labels), 3)
    if places:
        hit = sum(1 for s in places if any(match(lb, s, geom, match_m) for lb in labels))
        precision = round(hit / len(places), 3)
    return recall, precision
