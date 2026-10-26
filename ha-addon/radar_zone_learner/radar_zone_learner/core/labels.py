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
