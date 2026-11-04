"""Merge and modify proposals behind ``P5Params.zone_change_items`` (P5 spec 4.7, A.10). Owner: CORE.

OA 12 P5 lists 'merge' and 'modify' in its scope, but their real detection (the move check with succession, the
coverage ratio) is P7's (OA 7.8, P3S 1.2). What lives here is deliberately simple test scaffolding behind the flag
(default False, D14): with the flag off ``proposals`` returns two empty lists and no merge or modify item exists in
production. P7 replaces ``proposals`` and flips the default.
"""
from __future__ import annotations

import math
from datetime import date

from . import geometry, profiles
from .anchor import direction
from .zones import is_frozen, window_hours


def _profile(minutes_a, minutes_b) -> list:
    return [float(a) + float(b) for a, b in zip(minutes_a, minutes_b)]


def pearson(a, b) -> float:
    """Pearson correlation of two equally long series; 0.0 when either has no variance."""
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    sa = sum((x - ma) ** 2 for x in a)
    sb = sum((y - mb) ** 2 for y in b)
    if sa <= 0.0 or sb <= 0.0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(sa * sb)


def _suggestion_profile(s: dict) -> list:
    ev = s.get("evidence") or {}
    wd = ev.get("hours_wd") or [0.0] * 24
    we = ev.get("hours_we") or [0.0] * 24
    if len(wd) != 24 or len(we) != 24:
        return [0.0] * 24
    return _profile(wd, we)


def _zone_profile(L, z: dict, stays14: list) -> list:
    """Hour profile of the stays of the 14 covered dates whose cell lies in the zone (as the suggestion's own)."""
    geom = L.geom
    cells = z["cells"]
    mine = [st for st in stays14 if geom.cell_of(st["x"] / 100.0, st["y"] / 100.0) in cells]
    wd, we = profiles.build_profiles(mine, L.tm)
    return _profile(wd.minutes, we.minutes)


def _stays14(L) -> list:
    if L.now is None:
        return []
    today = L.tm.local(L.now).date_ord
    d14 = set(L.obs.covered_dates(today)[-L.p.retention_covered_days:])
    out = []
    for st in L.bh.stays:
        try:
            do = date.fromisoformat(st["day"]).toordinal()
        except (ValueError, KeyError, TypeError):
            continue
        if do in d14:
            out.append(st)
    return out


def proposals(L, pending=None) -> tuple:
    """(merges, modifies): lists of dicts, empty with the flag off.

    A merge proposal exists for a pending place suggestion ``S`` (shadow, not covered, not rejected, not snoozed) and a
    zone record ``Z`` (kind not bed, not frozen, ``Z.ref != S.id``, ``S.id not in Z.also``) when ``S.cells`` touches
    the dilated cells of ``Z``, the overlap coefficient is below 0.5 (otherwise it is the same place seen again), and
    the 24-bin hour profiles correlate with r >= merge_min_r. The best Z wins (highest r, then lowest zid). Dict keys:
    ``sid, zid, zname, ext_cm, dir, r, rev``. A modify proposal exists for a zone record ``Z`` (kind not bed, not
    frozen) whose ``ref`` suggestion ``S`` is shadow, with ``S.rev > Z.srev`` and IoU(S, Z) below modify_max_iou. Keys:
    ``zid, zname, sid, shift_cm, dir, rev``."""
    p5 = L.p5params
    if not p5.zone_change_items or not L.review.zones:
        return [], []
    win = window_hours(L)
    geom = L.geom
    zs = sorted((z for z in L.review.zones if z["kind"] != "bed"), key=lambda r: r["zid"])
    zs = [z for z in zs if not is_frozen(L, z["kind"], z.get("ref"), win)]
    if not zs:
        return [], []
    merges = []
    modifies = []
    sug = {s["id"]: s for s in L.ns.suggestions}

    cands = pending if pending is not None else L.review.pending_basic(L)
    if cands and zs:
        stays14 = _stays14(L)
        zprof = {}
        zdil = {}
        for s in cands:
            sprof = _suggestion_profile(s)
            best = None
            for z in zs:
                if z.get("ref") == s["id"] or s["id"] in z.get("also", ()):
                    continue
                dil = zdil.get(z["zid"])
                if dil is None:
                    dil = zdil[z["zid"]] = geom.dilate(z["cells"])
                if not (s["cells"] & dil):
                    continue
                if geometry.overlap_coef(s["cells"], z["cells"]) >= 0.5:
                    continue
                zp = zprof.get(z["zid"])
                if zp is None:
                    zp = zprof[z["zid"]] = _zone_profile(L, z, stays14)
                r = pearson(sprof, zp)
                if r < p5.merge_min_r:
                    continue
                if best is None or r > best[0]:
                    best = (r, z)
            if best is not None:
                r, z = best
                dx = s["cx"] - z["cx"]
                dy = s["cy"] - z["cy"]
                ext = max(10, 10 * int(round(math.hypot(dx, dy) * 10)))
                merges.append({"sid": s["id"], "zid": z["zid"], "zname": z["name"], "ext_cm": ext,
                               "dir": direction(dx, dy), "r": round(r, 3), "rev": s["rev"]})
        merges.sort(key=lambda m: (-m["r"], m["sid"]))

    for z in zs:
        s = sug.get(z.get("ref"))
        if s is None or s["state"] != "shadow":
            continue
        if s["rev"] <= z.get("srev", 0):
            continue
        if geometry.iou(s["cells"], z["cells"]) >= p5.modify_max_iou:
            continue
        dx = s["cx"] - z["cx"]
        dy = s["cy"] - z["cy"]
        modifies.append({"zid": z["zid"], "zname": z["name"], "sid": s["id"],
                         "shift_cm": int(round(100 * math.hypot(dx, dy))), "dir": direction(dx, dy),
                         "rev": s["rev"]})
    return merges, modifies
