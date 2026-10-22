"""Nightly extraction (P3 spec 7.8; OA 7.3 steps 4-5, 7.8): decay and retention, walking envelope,
clutter mask, peak regions, candidate evidence, COVIS, stability, gates and confidence, shadow
suggestions and reflections, doors, room outline, ignore zones, profiles, geometry.

Region growing, evidence and stability follow the project simulator (SIM:555-610, 897-916, 1049-1066)
with the design's rules where the two differ (spec 7.13).
"""
from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Optional

from . import geometry, profiles
from .grids import rate

PLACE_TYPES = ("dwell_zone", "short_task_zone")
MAX_SUGGESTIONS = 200           # stale suggestions are never deleted (D19) unless there are more than this many
GATE_KEYS = ("g1_class", "g2_core_days", "g3_ld_ghost", "g4_covis", "g5_stable", "g6_move", "g7_suppression",
             "conf")


def cid_key(cid: str) -> tuple:
    """Natural order of 'c<night>-<i>' / 's<night>-<k>' ids."""
    try:
        a, b = cid[1:].split("-", 1)
        return (int(a), int(b), cid)
    except (ValueError, IndexError):
        return (1 << 30, 0, cid)


def r3(v: float) -> float:
    return round(float(v), 3)


def _hit_key(h: tuple) -> tuple:
    """Best match: highest IoU, ties to the lower suggestion id."""
    k = cid_key(h[1]["id"])
    return (h[0], -k[0], -k[1])


class NightState:
    """Persisted results of the nightly runs (spec 8.1 'candidates', 'doors_prev', 'outline',
    'suggestions'), plus the last envelope (RAM; the outline's cells after a load)."""

    def __init__(self) -> None:
        self.candidates: list = []       # dicts; "cells" is a frozenset in RAM
        self.doors_prev: list = []
        self.outline: dict = {"cells": frozenset(), "streak": 0, "done": False}
        self.suggestions: list = []      # dicts; "cells" is a frozenset in RAM
        self.envelope: frozenset = frozenset()


class Cand:
    __slots__ = ("cells", "dil", "cx", "cy", "n", "minutes", "core_days", "ld", "gsh", "median", "spread",
                 "cls", "in_env", "arr_static", "exit_moving", "mine", "covis", "covis_src", "streak", "cid",
                 "conf", "conf_base", "gates", "outcome", "sugg_id", "a4", "fsec")

    def __init__(self) -> None:
        self.covis = 0.0
        self.covis_src = None
        self.sugg_id = None
        self.a4 = None
        self.fsec = 0.0


def move_check(cand) -> Optional[str]:
    """Gate 6 hook (OA 7.4 move check). P3 has no confirmed zones, so it always passes (D21)."""
    return None


def _date_ord(iso: str) -> int:
    return date.fromisoformat(iso).toordinal()


def run_nightly(L, run_ord: int, night: int) -> tuple:
    """One nightly run for local date run_ord. Returns (candidates, new_suggestions)."""
    p, geom, G, obs, bh, tm, ns = L.p, L.geom, L.grids, L.obs, L.bh, L.tm, L.ns

    # 1. decay per covered hour (7.6), COVIS with the same factor
    hours = obs.obs_s_since_extraction / 3600.0
    obs.obs_s_since_extraction = 0.0
    since = L.last_run_ord if L.last_run_ord is not None else L.created_ord
    # observed time cannot exceed the calendar time since the last run: a safeguard against decaying
    # every grid away after a corrupt counter (a day is at most 24 covered hours)
    hours = min(hours, 24.0 * max(1, run_ord - since + 1) if since is not None else 24.0)
    fs = G.decay(hours)
    L.covis.decay(fs)

    # 2. the 14 most recent covered dates strictly before the run's date
    d14 = obs.covered_dates(run_ord)[-p.retention_covered_days:]
    d14_set = set(d14)

    # 3. retention (D15)
    keep_from = run_ord - p.retention_max_days
    if d14:
        keep_from = max(keep_from, d14[0])
    bh.stays = [st for st in bh.stays if _date_ord(st["day"]) >= keep_from]
    bh.door_events = [e for e in bh.door_events if tm.local(e["t"] * 1000).date_ord >= keep_from]
    bh.ghost_events = [e for e in bh.ghost_events if tm.local(e["t"] * 1000).date_ord >= keep_from]

    # 4. DAYS bitmask
    G.days_advance(run_ord - 1)

    # 5. walking envelope (7.8.3)
    W = geom.blur(rate(G["walk"], G.eff_s))
    mw = max(W)
    if mw > 0:
        thr = p.env_walk_frac * mw
        E = frozenset(geom.dilate4([c for c in range(geom.nc) if W[c] >= thr], p.env_dilate_cells))
    else:
        E = frozenset()
    ns.envelope = E

    # 6. clutter mask (7.8.4)
    cr = rate(G["clutter"], G.eff_s)
    dr = rate(G["dwell_slow"], G.eff_s)
    if p.clutter_rule == "abs":
        M = frozenset(c for c in range(geom.nc) if cr[c] >= p.clutter_min_rate)
    else:
        M = frozenset(c for c in range(geom.nc) if cr[c] >= p.clutter_min_rate and cr[c] >= dr[c])

    # 7. regions (7.8.5)
    rd = geom.blur(dr)
    regs, owner = geom.regions(rd, p.peak_min_rate, p.grow_frac, p.grow_mono)
    rs = geom.blur(rate(G["sessions"], G.eff_s))
    regs2, _ = geom.regions(rs, p.peak_min_rate, p.grow_frac, p.grow_mono, owner)

    # 8. candidate evidence (7.8.6)
    ghost = G["ghost"]
    dwell = G["dwell_slow"]
    stays14 = []
    for st in bh.stays:
        do = _date_ord(st["day"])
        if do in d14_set:
            stays14.append((st, geom.cell_of(st["x"] / 100.0, st["y"] / 100.0), do))
    cands = []
    for _, rc in regs + regs2:
        cells = frozenset(rc) - M
        if p.envelope_hard_filter:
            cells = cells & E
        if not cells:
            continue
        dil = frozenset(geom.dilate(cells))
        mine = [m for m in stays14 if m[1] in dil]
        if not mine:
            continue
        c = Cand()
        c.cells = cells
        c.dil = dil
        c.mine = mine
        durs = [m[0]["dur"] for m in mine]
        tot = float(sum(durs)) or 1.0
        c.n = len(mine)
        c.minutes = sum(durs) / 60.0
        c.core_days = len({m[2] for m in mine if m[1] in cells})
        c.ld = sum(m[0]["ld"] * m[0]["dur"] for m in mine) / tot
        sdil = sorted(dil)
        gm = sum(ghost[k] for k in sdil)
        dm = sum(dwell[k] for k in sdil)
        c.gsh = gm / (gm + dm) if gm + dm > 0 else 0.0
        c.median = float(statistics.median(durs))
        xs = [m[0]["x"] / 100.0 for m in mine]
        ys = [m[0]["y"] / 100.0 for m in mine]
        c.cx = sum(x * d for x, d in zip(xs, durs)) / tot
        c.cy = sum(y * d for y, d in zip(ys, durs)) / tot
        c.spread = math.sqrt(statistics.pvariance(xs) + statistics.pvariance(ys))
        c.cls = "short" if c.median < p.class_split_s else "dwell"
        c.in_env = len(cells & E) / len(cells)
        c.arr_static = sum(m[0]["dur"] for m in mine if m[0]["arr"] == "static") / tot
        c.exit_moving = sum(m[0]["dur"] for m in mine if m[0]["exit"] == "moving") / tot
        cands.append(c)

    # gates 1-3
    for c in cands:
        if c.cls == "dwell":
            g1 = c.n >= p.dwell_min_stays and c.minutes >= p.dwell_min_minutes
        else:
            g1 = c.n >= p.short_min_stays and c.spread < p.short_max_spread_m
        c.gates = {"g1_class": g1, "g2_core_days": c.core_days >= p.min_core_days,
                   "g3_ld_ghost": c.ld >= p.ld_share_min and c.gsh < p.ghost_share_max}

    # 6. COVIS for candidates passing gates 1-3 (7.7, gate 4)
    pass13 = [c for c in cands if c.gates["g1_class"] and c.gates["g2_core_days"] and c.gates["g3_ld_ghost"]]
    for c in cands:
        if c.n >= p.covis_min_n or c in pass13:
            # the candidate's own cells: the 0.4 m rounding already pads them by up to 0.2 m, as the
            # simulator's 0.2 m dilation does (SIM:748-757); see Covis.covis. The fragment seconds of
            # these cells are the density of the candidate (SIM:760-761), shared cells included.
            c.a4 = L.covis.cells4(c.cells, geom)
            c.fsec = L.covis.frag_seconds(c.a4)
    for c in pass13:
        best, src = 0.0, None
        for o in cands:
            if o is c or o.n < p.covis_min_n or o.a4 is None or o.fsec <= c.fsec:
                continue
            v = L.covis.covis(c.a4, o.a4)
            if v > best:
                best, src = v, o
        c.covis = best
        c.covis_src = src

    # 7. stability (7.8.8)
    prev = sorted(ns.candidates, key=lambda d: cid_key(d["cid"]))
    new_i = 0
    for c in cands:
        best, best_v = None, -1.0
        for pc in prev:
            v = geometry.iou(pc["cells"], c.cells)
            if v > best_v:
                best, best_v = pc, v
        if best is not None and best_v >= p.stable_iou and \
                math.hypot(best["cx"] - c.cx, best["cy"] - c.cy) < p.stable_shift_m:
            c.streak = best["streak"] + 1
            c.cid = best["cid"]
        else:
            new_i += 1
            c.streak = 1
            c.cid = "c%04d-%d" % (night, new_i)

    # 8. gates 4-7 and the confidence (7.8.7)
    for c in cands:
        g = c.gates
        g["g4_covis"] = c.covis < p.covis_thr
        g["g5_stable"] = c.streak >= p.stable_nights
        g["g6_move"] = move_check(c) is None
        g["g7_suppression"] = True          # no suppressions exist in P3 (P5 adds rejections)
        base = (1.0 - math.exp(-c.n / p.conf_n_scale)) * min(1.0, c.core_days / p.conf_days) \
            * min(1.0, c.streak / p.stable_nights) * (1.0 - c.gsh)
        c.conf_base = base
        c.conf = base * (1.0 - c.covis) if p.conf_includes_covis else base
        g["conf"] = c.conf >= p.conf_min
        others = g["g1_class"] and g["g2_core_days"] and g["g3_ld_ghost"] and g["g5_stable"] and \
            g["g6_move"] and g["g7_suppression"]
        if others and g["g4_covis"] and g["conf"]:
            c.outcome = "suggested"
        elif others and not g["g4_covis"] and base >= p.conf_min:
            c.outcome = "reflection"        # 'it would otherwise pass' (OA 7.8)
        else:
            c.outcome = "failed"

    # 9. shadow suggestions and reflections (7.8.9)
    counter = [0]

    def new_id() -> str:
        counter[0] += 1
        return "s%04d-%d" % (night, counter[0])

    sugg = ns.suggestions
    created = []
    dirty = set()                          # suggestions whose cells changed (geometry needed)
    writes = []                            # (cand, suggestion)

    def dedupe(c, types, typ) -> None:
        pool = [s for s in sugg if s["type"] in types]
        cc = geom.cell_of(c.cx, c.cy)
        hits = []
        for s in pool:
            v = geometry.iou(c.cells, s["cells"])
            if v >= p.hit_iou or math.hypot(c.cx - s["cx"], c.cy - s["cy"]) < p.hit_dist_m \
                    or geometry.overlap_coef(c.cells, s["cells"]) >= p.supp_overlap or cc in s["cells"]:
                hits.append((v, s))
        # each suggestion is updated by at most one candidate per night; a candidate that only hits
        # suggestions already updated tonight is absorbed (as the simulator skips candidates that
        # hit a zone, SIM:949-950)
        avail = [h for h in hits if not h[1].get("_touched")]
        if hits and not avail:
            c.sugg_id = max(hits, key=_hit_key)[1]["id"]
            return
        if avail:
            v, s = max(avail, key=_hit_key)
            s["_touched"] = True
            changed = v < 0.6 or s["type"] != typ
            if changed:
                s["cells"] = c.cells
                s["cx"] = r3(c.cx)
                s["cy"] = r3(c.cy)
                s["rev"] += 1
                if s["type"] != typ:
                    s["type"] = typ
                    if s["kind"] != "bed":
                        s["kind"] = _kind_of(typ)
                dirty.add(s["id"])
            s["last_night"] = night
            s["state"] = "shadow"
            c.sugg_id = s["id"]
            writes.append((c, s))
            return
        s = _new_suggestion(new_id(), typ, night, c.cells, c.cx, c.cy)
        s["_touched"] = True
        sugg.append(s)
        created.append(s)
        dirty.add(s["id"])
        c.sugg_id = s["id"]
        writes.append((c, s))

    for c in sorted([c for c in cands if c.outcome == "suggested"], key=lambda c: (-c.conf, cid_key(c.cid))):
        dedupe(c, PLACE_TYPES, "dwell_zone" if c.cls == "dwell" else "short_task_zone")
    for c in sorted([c for c in cands if c.outcome == "reflection"], key=lambda c: (-c.conf_base, cid_key(c.cid))):
        dedupe(c, ("reflection",), "reflection")
    for c, s in writes:
        src = c.covis_src
        src_id = None
        if src is not None:
            src_id = src.sugg_id or src.cid
        ev = dict(s.get("evidence") or {})
        ev.update(_cand_evidence(c, src_id, geom))
        ev.setdefault("hours_wd", [0.0] * 24)
        ev.setdefault("hours_we", [0.0] * 24)
        ev.setdefault("hours_at_suggestion", None)
        s["evidence"] = ev
        s["conf"] = r3(c.conf if s["type"] != "reflection" else c.conf_base)

    # 10. doors, room outline, ignore zones (7.8.10)
    _doors(L, night, d14_set, E, new_id, created, dirty)
    _outline(L, night, d14, E, new_id, created, dirty)
    _ignore(L, night, d14_set, new_id, created, dirty)

    for s in sugg:
        s.pop("_touched", None)
        if night - s["last_night"] >= p.stale_nights:
            s["state"] = "stale"

    # 11. profiles, bed naming, label kinds, freeze, variants (7.9, 7.10)
    win = profiles.window_hours(L.window["start"], L.window["end"])
    for s in sugg:
        if s["type"] not in PLACE_TYPES or s["state"] != "shadow":
            continue
        mine = [m[0] for m in stays14 if m[1] in s["cells"]]
        wd, we = profiles.build_profiles(mine, tm)
        ev = s["evidence"]
        ev["hours_wd"] = [round(v, 1) for v in wd.minutes]
        ev["hours_we"] = [round(v, 1) for v in we.minutes]
        if ev.get("hours_at_suggestion") is None:
            ev["hours_at_suggestion"] = [round(a + b, 1) for a, b in zip(wd.minutes, we.minutes)]
        if s["type"] == "dwell_zone" and s["kind"] != "bed" and \
                profiles.bed_named(wd.minutes, we.minutes, float(ev.get("median_s", 0.0)), p):
            s["kind"] = "bed"
            if s["name_hint"] is None:
                s["name_hint"] = "bed"
        L.link_labels_one(s)
        is_frozen = profiles.frozen(L.window["sched"], s["kind"], ev["hours_at_suggestion"], win, p)
        s["frozen"] = is_frozen
        if s["type"] == "dwell_zone":
            vr = profiles.variants_for(wd, we, len(d14) >= p.retention_covered_days, L.window["sched"],
                                       is_frozen, win, p)
            s["variants"] = vr["variants"]
            s["variant_evidence_split"] = vr["evidence_split"]
            s["variant_share_split"] = vr["share_split"]
            s["variant_suppressed"] = vr["suppressed"]
        else:
            s["variants"] = []
            s["variant_evidence_split"] = False
            s["variant_share_split"] = False
            s["variant_suppressed"] = False

    # 12. geometry (7.8.8)
    for s in sugg:
        if s["id"] in dirty or not s["poly_cm"]:
            s["poly_cm"] = geometry.polygon_cm(s["cells"], geom, 0.1, p.poly_max_vertices)

    # D19 keeps stale suggestions; only a list longer than MAX_SUGGESTIONS loses its stalest entries
    if len(sugg) > MAX_SUGGESTIONS:
        stale = sorted((s for s in sugg if s["state"] == "stale"), key=lambda s: (s["last_night"], cid_key(s["id"])))
        drop = {id(s) for s in stale[:len(sugg) - MAX_SUGGESTIONS]}
        sugg[:] = [s for s in sugg if id(s) not in drop]

    # 13. store the candidates
    ns.candidates = [_cand_record(c) for c in cands]
    return len(cands), len(created)


def _kind_of(typ: str) -> str:
    return {"dwell_zone": "dwell", "short_task_zone": "short_task", "reflection": "reflection",
            "ignore_zone": "ignore", "entry_zone": "entry", "room_outline": "outline"}[typ]


def _new_suggestion(sid: str, typ: str, night: int, cells: frozenset, cx: float, cy: float) -> dict:
    return {"id": sid, "rev": 1, "type": typ, "kind": _kind_of(typ), "name_hint": None, "state": "shadow",
            "first_night": night, "last_night": night, "cx": r3(cx), "cy": r3(cy), "cells": frozenset(cells),
            "poly_cm": [], "conf": 0.0, "evidence": {}, "variants": [], "variant_evidence_split": False,
            "variant_share_split": False, "variant_suppressed": False, "frozen": False}


def _cand_evidence(c: Cand, src_id: Optional[str], geom) -> dict:
    mine = c.mine
    tot = float(sum(m[0]["dur"] for m in mine)) or 1.0
    mx = sum(m[0]["x"] * m[0]["dur"] for m in mine) / tot
    my = sum(m[0]["y"] * m[0]["dur"] for m in mine) / tot
    sxx = sum((m[0]["x"] - mx) ** 2 * m[0]["dur"] for m in mine) / tot
    syy = sum((m[0]["y"] - my) ** 2 * m[0]["dur"] for m in mine) / tot
    sxy = sum((m[0]["x"] - mx) * (m[0]["y"] - my) * m[0]["dur"] for m in mine) / tot
    return {"n": c.n, "core_days": c.core_days, "minutes": round(c.minutes, 1), "median_s": round(c.median, 1),
            "ld_share": r3(c.ld), "ghost_share": r3(c.gsh), "covis": r3(c.covis), "covis_src": src_id,
            "streak": c.streak, "spread_m": r3(c.spread), "cls": c.cls, "in_envelope": r3(c.in_env),
            "arr_static_share": r3(c.arr_static), "exit_moving_share": r3(c.exit_moving),
            "bbox_cm": geometry.bbox_cm(c.cells, geom), "mean_cm": [round(mx, 1), round(my, 1)],
            "cov_cm2": [[round(sxx, 1), round(sxy, 1)], [round(sxy, 1), round(syy, 1)]]}


def _cand_record(c: Cand) -> dict:
    src = c.covis_src
    return {"cid": c.cid, "cells": c.cells, "cx": r3(c.cx), "cy": r3(c.cy), "streak": c.streak,
            "cls": c.cls, "n": c.n, "core_days": c.core_days, "minutes": round(c.minutes, 1),
            "median_s": round(c.median, 1), "ld_share": r3(c.ld), "ghost_share": r3(c.gsh),
            "covis": r3(c.covis), "covis_src": (src.sugg_id or src.cid) if src is not None else None,
            "conf": r3(c.conf), "gates": dict((k, bool(c.gates[k])) for k in GATE_KEYS),
            "outcome": c.outcome}


def _doors(L, night, d14_set, E, new_id, created, dirty) -> None:
    p, geom, G, tm, ns, bh = L.p, L.geom, L.grids, L.tm, L.ns, L.bh
    bm = G["birth_moving"]
    dm = G["death_moving"]
    drate = geom.blur(rate([bm[k] + dm[k] for k in range(geom.nc)], G.eff_s))
    dregs, _ = geom.regions(drate, p.door_min_rate, p.grow_frac, p.grow_mono)
    found = []
    for _, rc in dregs:
        cells = frozenset(rc)
        if not any(n not in E for c in rc for n in geom.n4[c]):
            continue
        m = sum(drate[c] for c in rc) or 1.0
        cx = sum(geom.cell_xy(c)[0] * drate[c] for c in rc) / m
        cy = sum(geom.cell_xy(c)[1] * drate[c] for c in rc) / m
        pv = [d for d in ns.doors_prev if geometry.iou(d["cells"], cells) >= 0.5]
        streak = pv[0]["streak"] + 1 if pv else 1
        found.append({"cells": cells, "cx": r3(cx), "cy": r3(cy), "streak": streak})
    events = []
    for e in bh.door_events:
        do = tm.local(e["t"] * 1000).date_ord
        if do in d14_set:
            events.append((geom.cell_of(e["x"] / 100.0, e["y"] / 100.0), do))
    for d in found:
        if d["streak"] < 3:
            continue
        dil = geom.dilate(d["cells"])
        evs = [e for e in events if e[0] in dil]
        days = len({e[1] for e in evs})
        if len(evs) < p.door_min_passages or days < p.door_min_days:
            continue
        ex = [s for s in ns.suggestions if s["type"] == "entry_zone"
              and math.hypot(s["cx"] - d["cx"], s["cy"] - d["cy"]) < 0.6]
        ev = {"passages": len(evs), "days": days, "streak": d["streak"]}
        if ex:
            s = ex[0]
            s["last_night"] = night
            s["state"] = "shadow"
            s["evidence"] = ev
            continue
        s = _new_suggestion(new_id(), "entry_zone", night, d["cells"], d["cx"], d["cy"])
        s["conf"] = 1.0
        s["evidence"] = ev
        ns.suggestions.append(s)
        created.append(s)
        dirty.add(s["id"])
    ns.doors_prev = found


def _outline(L, night, d14, E, new_id, created, dirty) -> None:
    p, ns, bh = L.p, L.ns, L.bh
    o = ns.outline
    if E:
        if o["cells"] and geometry.iou(E, o["cells"]) >= p.outline_iou:
            o["streak"] += 1
        else:
            o["streak"] = 1
    else:
        o["streak"] = 0
    o["cells"] = E
    ev = {"walk_points": bh.walk_points_total, "covered_days": len(d14), "streak": o["streak"]}
    if not o["done"]:
        if o["streak"] >= p.outline_nights and len(d14) >= p.outline_min_days \
                and bh.walk_points_total >= p.outline_min_walk_s:
            cx, cy = _cells_centroid(E, L.geom)
            s = _new_suggestion(new_id(), "room_outline", night, E, cx, cy)
            s["conf"] = 1.0
            s["evidence"] = ev
            ns.suggestions.append(s)
            created.append(s)
            dirty.add(s["id"])
            o["done"] = True
        return
    if not E:
        return
    for s in ns.suggestions:
        if s["type"] == "room_outline":
            if geometry.iou(s["cells"], E) < 0.6:
                s["rev"] += 1
            if s["cells"] != E:
                s["cells"] = E
                cx, cy = _cells_centroid(E, L.geom)
                s["cx"], s["cy"] = r3(cx), r3(cy)
                dirty.add(s["id"])
            s["last_night"] = night
            s["state"] = "shadow"
            s["evidence"] = ev
            break


def _cells_centroid(cells, geom) -> tuple:
    if not cells:
        return 0.0, 0.0
    xs = [geom.cell_xy(c) for c in sorted(cells)]
    return sum(x for x, _ in xs) / len(xs), sum(y for _, y in xs) / len(xs)


def _ignore(L, night, d14_set, new_id, created, dirty) -> None:
    p, geom, G, tm, ns, bh = L.p, L.geom, L.grids, L.tm, L.ns, L.bh
    gh = G["ghost"]
    cl = G["clutter"]
    dw = G["dwell_slow"]
    gc = [gh[k] + cl[k] for k in range(geom.nc)]
    iregs, _ = geom.regions(geom.blur(rate(gc, G.eff_s)), p.peak_min_rate, p.grow_frac, p.grow_mono)
    events = []
    for e in bh.ghost_events:
        do = tm.local(e["t"] * 1000).date_ord
        if do in d14_set:
            events.append((geom.cell_of(e["x"] / 100.0, e["y"] / 100.0), do, e))
    for _, rc in iregs:
        cells = frozenset(rc)
        dil = geom.dilate(cells)
        sdil = sorted(dil)
        sg = sum(gc[k] for k in sdil)
        sd = sum(dw[k] for k in sdil)
        share = sg / (sg + sd) if sg + sd > 0 else 0.0
        if share < p.ignore_min_share:
            continue
        evs = [e for e in events if e[0] in dil]
        days = len({e[1] for e in evs})
        if days < p.ignore_min_days:
            continue
        tot = float(sum(e[2]["dur"] for e in evs)) or 1.0
        ld = sum(e[2]["ld"] * e[2]["dur"] for e in evs) / tot
        static = sum(e[2]["dur"] for e in evs if e[2]["arr"] == "static") / tot
        if ld >= p.ignore_max_ld or static < p.ignore_min_static:
            continue
        cx = sum(e[2]["x"] * e[2]["dur"] for e in evs) / tot / 100.0
        cy = sum(e[2]["y"] * e[2]["dur"] for e in evs) / tot / 100.0
        ev = {"share": r3(share), "days": days, "ld": r3(ld), "static_share": r3(static), "events": len(evs)}
        cc = geom.cell_of(cx, cy)
        hits = []
        for s in ns.suggestions:
            if s["type"] != "ignore_zone":
                continue
            v = geometry.iou(cells, s["cells"])
            if v >= p.hit_iou or math.hypot(cx - s["cx"], cy - s["cy"]) < p.hit_dist_m \
                    or geometry.overlap_coef(cells, s["cells"]) >= p.supp_overlap or cc in s["cells"]:
                hits.append((v, s))
        if hits:
            v, s = max(hits, key=_hit_key)
            if s.get("_touched"):
                continue
            s["_touched"] = True
            if v < 0.6:
                s["cells"] = cells
                s["cx"], s["cy"] = r3(cx), r3(cy)
                s["rev"] += 1
                dirty.add(s["id"])
            s["last_night"] = night
            s["state"] = "shadow"
            s["conf"] = r3(share)
            s["evidence"] = ev
            continue
        s = _new_suggestion(new_id(), "ignore_zone", night, cells, cx, cy)
        s["_touched"] = True
        s["conf"] = r3(share)
        s["evidence"] = ev
        ns.suggestions.append(s)
        created.append(s)
        dirty.add(s["id"])
