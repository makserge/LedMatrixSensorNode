"""Moved-from and retired masks (P7 spec 4.4). Owner: CORE.

An entry of ``zone7.mf`` is made by the answer that moved or removed a zone: ``{zid, cells, cx, cy, day, n0, why}`` with
``why`` ``moved_from`` (the old cells of a modify, taken before the cells are replaced) or ``retired`` (the cells of a
removed zone). Gate 7 of the nightly extraction fails a candidate that matches an entry unless the candidate re-qualifies
on the stays made after the answer alone; the open-item predicate hides a suggestion that matches an entry. A re-qualified
entry is lifted by ``release`` (nightly, P7 step 2) and is gone for good. Every function returns at once when the list is
empty (``mf`` is empty until the first modify or retire answer), so the extraction is unchanged until then (DV9).
"""
from __future__ import annotations

import math
import statistics

from . import rejections


def key_of(m: dict) -> tuple:
    return (m["zid"], m["why"], m["day"])


def begin(L) -> None:
    """Called first in ``extraction.run_nightly``: the entries re-qualified in the last run are consumed by now."""
    L.p7_requalified.clear()


def add(L, why: str, zone: dict) -> None:
    """Appends an entry from the zone's CURRENT cells (a record dict); at most ``mf_max`` entries, the oldest dropped.
    ``day`` is the local date ordinal of the answer, ``n0`` the evidence ``n`` of the zone's source suggestion."""
    zid = zone["zid"] if "zid" in zone else zone["id"]
    s = next((x for x in L.ns.suggestions if x["id"] == zone.get("ref")), None)
    n0 = rejections.evidence_n(s) if s is not None else 0
    cx = zone["cx"] if "cx" in zone else zone["x"]
    cy = zone["cy"] if "cy" in zone else zone["y"]
    entry = {"zid": zid, "cells": frozenset(zone["cells"]), "cx": cx, "cy": cy, "day": L.review._today(L), "n0": n0,
             "why": why}
    mf = L.zone7.mf
    mf[:] = [m for m in mf if not (key_of(m) == key_of(entry) and m["cells"] == entry["cells"])]   # the same mask again
    mf.append(entry)
    cap = max(1, L.p7params.mf_max)
    if len(mf) > cap:
        del mf[:len(mf) - cap]


def _requalifies(L, c, m: dict) -> bool:
    """The candidate qualifies again on the post-event stays alone (CD:types/modify_zone), by the thresholds of gates 1
    and 2 of the extraction (no new constant)."""
    p = L.p
    post = [x for x in c.mine if x[2] > m["day"]]
    if c.cls == "dwell":
        if len(post) < p.dwell_min_stays:
            return False
        if sum(x[0]["dur"] for x in post) / 60.0 < p.dwell_min_minutes:
            return False
        return len({x[2] for x in post if x[1] in c.cells}) >= p.min_core_days
    if len(post) < p.short_min_stays:
        return False
    xs = [x[0]["x"] / 100.0 for x in post]
    ys = [x[0]["y"] / 100.0 for x in post]
    spread = math.sqrt(statistics.pvariance(xs) + statistics.pvariance(ys))
    return spread < p.short_max_spread_m


def blocks(L, c, night: int) -> bool:
    """Gate 7 (4.4): True when the candidate matches a live entry and does not re-qualify on post-event stays alone.
    False at once when ``zone7.mf`` is empty. A candidate that re-qualifies for an entry records its key in
    ``L.p7_requalified`` (``release`` lifts it)."""
    mf = L.zone7.mf
    if not mf or not L.p7params.enabled:
        return False
    blocked = False
    for m in mf:
        if not rejections.matches_mask(L, c.cells, c.cx, c.cy, m):
            continue
        if _requalifies(L, c, m):
            L.p7_requalified.add(key_of(m))
        else:
            blocked = True
    return blocked


def mask_match(L, s: dict) -> bool:
    """The open-item predicate: does a suggestion match a live entry (the two-way test of ``rejections.mask_match``)?"""
    mf = L.zone7.mf
    if not mf or not L.p7params.enabled:
        return False
    cells = s["cells"]
    for m in mf:
        if rejections.matches_mask(L, cells, s["cx"], s["cy"], m):
            return True
        c = L.geom.cell_of(m["cx"], m["cy"])
        if c >= 0 and c in cells:
            return True
    return False


def release(L) -> int:
    """Nightly (P7 step 2): deletes the entries that re-qualified in tonight's gate 7 and clears the RAM set."""
    keys = L.p7_requalified
    mf = L.zone7.mf
    n = 0
    if keys and mf:
        keep = [m for m in mf if key_of(m) not in keys]
        n = len(mf) - len(keep)
        mf[:] = keep
    keys.clear()
    return n


def clear(L) -> None:
    """Drops every entry ('Forget rejections', ``reset_maps``)."""
    L.zone7.mf.clear()
    L.p7_requalified.clear()
