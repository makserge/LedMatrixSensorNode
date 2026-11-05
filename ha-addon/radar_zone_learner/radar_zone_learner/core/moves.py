"""The pre-emptive move check with succession (modify), the merge detector and the retire proposals (P7 spec 4.3, 4.5).
Owner: CORE.

``plan`` runs at the review layer (``ReviewModel._compute_tier2``) on the open place suggestions and **consumes** the
suggestion a merge or a modify takes, so the extraction and every suggestion id, cell and revision stay exactly as in P6
while ``zone7.mf`` is empty (DV9). Nothing here is on the per-frame path. A proposal is a dict; the item revision of a
modify is a per-zone counter (``zone7.z[zid].mr``, never reset, D21/D40) that is bumped here, a read path, whenever the
proposed ``(sid, suggestion rev)`` differs from the one last shown.
"""
from __future__ import annotations

import math

from . import drift, geometry, radarmoved
from . import zones as zones_mod
from .anchor import direction
from .zonework import _stays14, _suggestion_profile, _zone_profile, pearson

_NO_PLAN = ([], [], [], set())


def lastuse(L, zid: str) -> int:
    """The date ordinal of the zone's last use (``zone7`` ``lu``), 0 = none."""
    e = L.zone7.entry(zid)
    return e["lu"] if e is not None else 0


# ---------------------------------------------------------------- helpers
def _zone_prof(L, z: dict, stays14: list, entry) -> list:
    """The zone's own 14-date stay profile when it holds any dwell, else its usual profile ``u``, else None."""
    prof = _zone_profile(L, z, stays14)
    if sum(prof) > 0:
        return prof
    if entry is not None and entry["u"]:
        return list(entry["u"])
    return None


def _cand_dwell_by_date(ctx, cells) -> dict:
    """{date ordinal: dwell seconds} of the stays of the retained covered dates whose cell lies in ``cells``."""
    per: dict = {}
    by = ctx.by_cell
    for cell in cells:
        for d, _hs, dur in by.get(cell, ()):
            per[d] = per.get(d, 0) + dur
    return per


def _shift(s: dict, z: dict) -> tuple:
    dx = s["cx"] - z["cx"]
    dy = s["cy"] - z["cy"]
    return dx, dy, math.hypot(dx, dy)


def _usual_hours_observed(L, entry, run_ord: int, p) -> bool:
    """4.5 (3): the usual hours were observed over the retire window: the usual-profile mass of the hours observed at
    least ``retire_usual_cov`` of the time, over the newest ``retire_days`` covered dates, is at least
    ``retire_usual_cov``; with fewer covered dates than ``retire_days`` the zone is not retired yet."""
    u = entry["u"]
    if not u:
        return False
    d_cov = L.obs.covered_dates(run_ord)[-p.retire_days:]
    if len(d_cov) < p.retire_days:
        return False
    sec = [0.0] * 24
    for d in d_cov:
        row = L.obs.coverage.get(d)
        if row is not None:
            for h in range(24):
                sec[h] += row[h]
    need = p.retire_usual_cov * len(d_cov) * 3600.0
    return sum(u[h] for h in range(24) if sec[h] >= need) >= p.retire_usual_cov


# ---------------------------------------------------------------- probation (4.3.5) and rc hiding (4.3.7)
def span_of(L, s: dict, ctx=None) -> int:
    """``idx(last stay date) - idx(first stay date)`` over the retained stays whose cell lies in the suggestion's cells,
    ``idx`` the position in the covered dates (so radar-off dates do not shorten it); 0 when there is none."""
    ctx = ctx if ctx is not None else drift.context(L)
    dates = set()
    by = ctx.by_cell
    for cell in s["cells"]:
        for d, _hs, _dur in by.get(cell, ()):
            dates.add(d)
    if not dates:
        return 0
    pos = {d: i for i, d in enumerate(ctx.d14)}
    return pos[max(dates)] - pos[min(dates)]


def probation_ok(L, s: dict, ctx=None) -> bool:
    """A plain place item is open only if its suggestion was first made before ``probation_from_night`` or the span of
    its evidence is at least ``probation_min_span`` covered dates (0 switches the rule off)."""
    p = L.p7params
    if p.probation_min_span <= 0 or s["first_night"] < p.probation_from_night:
        return True
    return span_of(L, s, ctx) >= p.probation_min_span


def rc_hidden(L, s: dict) -> bool:
    """A plain place item that overlaps a zone that needs re-check (overlap coefficient at least ``rc_overlap``, or its
    centroid cell inside the zone) is not shown: it would invite a duplicate next to a stale zone."""
    p = L.p7params
    cell = L.geom.cell_of(s["cx"], s["cy"])
    for z in L.review.zones:
        e = L.zone7.entry(z["zid"])
        if e is None or not e["rc"]:
            continue
        if geometry.overlap_coef(s["cells"], z["cells"]) >= p.rc_overlap or (cell >= 0 and cell in z["cells"]):
            return True
    return False


# ---------------------------------------------------------------- the plan
def plan(L, pending: list, ctx=None) -> tuple:
    """(merges, modifies, retires, consumed): lists of proposal dicts in a deterministic order (modifies by zid, merges
    by (-r, sid), retires by zid) and the set of suggestion ids a merge or a modify takes. Empty while the radar hold is
    in force or when P7 is not enabled. Writes ``zone7.mr/ms`` (4.3.3) and ``rr/ro`` (6.1)."""
    p = L.p7params
    if not p.enabled or L.last_run_ord is None or radarmoved.hold(L):
        return [], [], [], set()
    zone7 = L.zone7
    p5 = L.p5params
    run_ord = L.last_run_ord
    ctx = ctx if ctx is not None else drift.context(L)
    stays14 = _stays14(L)
    geom = L.geom
    sug = {s["id"]: s for s in L.ns.suggestions}

    # the zones that can take part: records that are not beds and not frozen, with the stored verdict
    zs = []
    for z in sorted(L.review.zones, key=lambda r: r["zid"]):
        if z["kind"] == "bed" or zones_mod.is_frozen(L, z["kind"], z.get("ref"), zid=z["zid"]):
            continue
        e = zone7.entry(z["zid"])
        if e is None:
            continue
        zs.append((z, e))

    # modify: the zone's own suggestion moved (4.3.3a), or a different pending candidate (4.3.3)
    modifies = {}                                          # zid -> proposal
    cand_pairs = []                                        # (-r, D, sid, zid, s, z, dx, dy)
    targets = []
    for z, e in zs:
        rc = bool(e["rc"])
        state = e["vs"]
        if rc:
            ok = bool(e["u"])
        elif state == "falling":
            ok = drift.series(ctx, frozenset(z["cells"]))[2] >= p.min_slow_stays
        elif state == "collapsed":
            ok = e["lu"] > 0
        else:
            ok = False
        if ok:
            targets.append((z, e, rc))
    for z, e, rc in targets:
        s_own = sug.get(z.get("ref"))
        if s_own is not None and s_own["state"] == "shadow" and s_own["rev"] > z.get("srev", 0) \
                and geometry.iou(s_own["cells"], z["cells"]) < p5.modify_max_iou:
            dx, dy, d = _shift(s_own, z)
            zp = _zone_prof(L, z, stays14, e)
            r = pearson(_suggestion_profile(s_own), zp) if zp is not None else 0.0
            modifies[z["zid"]] = {"sid": s_own["id"], "zid": z["zid"], "zname": z["name"],
                                  "shift_cm": int(round(100 * d)), "dir": direction(dx, dy), "rev": 0,
                                  "r": round(r, 3), "src": "own", "_s": s_own}
            continue
        zp = _zone_prof(L, z, stays14, e)
        if zp is None:
            continue
        lo = 0.0 if rc else p.move_min_m
        for s in pending:
            if s["id"] == z.get("ref"):
                continue
            dx, dy, d = _shift(s, z)
            if not lo <= d <= p.move_max_m:
                continue
            r = pearson(_suggestion_profile(s), zp)
            if r < p.move_min_r:
                continue
            if d > p.succ_far_m and not rc:
                lu = e["lu"]
                if lu <= 0:
                    continue
                per = _cand_dwell_by_date(ctx, s["cells"])
                total = sum(per.values())
                if total <= 0 or sum(v for dd, v in per.items() if dd <= lu) / total > p.succ_max_share:
                    continue
            cand_pairs.append((-r, d, s["id"], z["zid"], s, z, dx, dy))
    cand_pairs.sort(key=lambda t: t[:4])
    consumed: set = set()
    for negr, d, sid, zid, s, z, dx, dy in cand_pairs:
        if zid in modifies or sid in consumed:
            continue
        consumed.add(sid)
        modifies[zid] = {"sid": sid, "zid": zid, "zname": z["name"], "shift_cm": int(round(100 * d)),
                         "dir": direction(dx, dy), "rev": 0, "r": round(-negr, 3), "src": "cand", "_s": s}
    for m in modifies.values():
        consumed.add(m["sid"])
    mods = []
    for zid in sorted(modifies):
        m = modifies[zid]
        e = zone7.entry(zid)
        key = "%s#%d" % (m["sid"], m["_s"]["rev"])
        if e["ms"] != key:                                 # a new (suggestion, revision): a new item revision (D40)
            e["mr"] += 1
            e["ms"] = key
        m["rev"] = e["mr"]
        del m["_s"]
        mods.append(m)

    # merge: a pending place that touches a zone that is in use and used at similar hours (4.3.6)
    merges = []
    mergeable = []
    for z, e in zs:
        if e["rc"] or e["vs"] != "ok" or z["zid"] in modifies:
            continue
        if drift.series(ctx, frozenset(z["cells"]))[2] < p.min_slow_stays:
            continue
        mergeable.append((z, e))
    if mergeable:
        zinfo = []
        for z, e in mergeable:
            zp = _zone_prof(L, z, stays14, e)
            if zp is not None:
                zinfo.append((z, zp, geom.dilate(geom.dilate(z["cells"]))))
        for s in pending:
            if s["id"] in consumed:
                continue
            sprof = _suggestion_profile(s)
            best = None
            for z, zp, dil2 in zinfo:
                if z.get("ref") == s["id"] or s["id"] in z.get("also", ()):
                    continue
                if not ((s["cells"] & dil2) or geometry.iou(s["cells"], z["cells"]) >= p.merge_iou):
                    continue
                if geometry.overlap_coef(s["cells"], z["cells"]) >= 0.5:
                    continue
                r = pearson(sprof, zp)
                if r < p.merge_min_r:
                    continue
                if best is None or r > best[0] or (r == best[0] and z["zid"] < best[1]["zid"]):
                    best = (r, z)
            if best is not None:
                r, z = best
                dx, dy, d = _shift(s, z)
                merges.append({"sid": s["id"], "zid": z["zid"], "zname": z["name"],
                               "ext_cm": max(10, 10 * int(round(d * 10))), "dir": direction(dx, dy), "rev": s["rev"],
                               "r": round(r, 3), "src": "cand"})
                consumed.add(s["id"])
        merges.sort(key=lambda m: (-m["r"], m["sid"]))

    # retire (4.5)
    merged_zids = {m["zid"] for m in merges}
    retires = []
    ret_zids = set()
    for z, e in zs:
        zid = z["zid"]
        if e["rc"] or e["vs"] in ("young", "blind", "frozen", "none"):
            continue
        if e["rt"] < p.retire_days or zid in modifies or zid in merged_zids:
            continue
        if e["kp"] > run_ord or not _usual_hours_observed(L, e, run_ord, p):
            continue
        retires.append({"zid": zid, "zname": z["name"], "weeks": max(3, e["rt"] // 7), "rev": 0})
        ret_zids.add(zid)
    for z, e in zs:                                        # the revision: bumped when the item opens after being closed
        zid = z["zid"]
        if zid in ret_zids and not e["ro"]:
            e["rr"] = min(e["rr"] + 1, 99999)
            e["ro"] = True
        elif zid not in ret_zids and e["ro"]:
            e["ro"] = False
    for r in retires:
        r["rev"] = zone7.entry(r["zid"])["rr"]
    return merges, mods, retires, consumed
