"""Nightly zone evidence (P6 spec 5.4, 5.6, 5.8). Owner: CORE.

Run once per night after the extraction (``Learner._maybe_nightly``):
- the learned clear delay of a zone is the nearest-rank p95 of its own kept hidden spells, clamped to 5..600 s;
- ``ld2412_reliable`` is the zone's own LD2412-on share while a track was inside (>= 0.8 over >= 3,600 s known);
- the typical hours, the variants and the confidence of the source suggestion are copied into ``zone6.info``, so they
  survive the suggestion going stale (a zone record keeps only ``ref``);
- request entries of variants that no longer exist are dropped.
A failure here is contained by the caller and never touches the review or the suggestions.
"""
from __future__ import annotations

import math
from typing import Optional

from .anchor import typical_hours
from .p6state import variant_id
from .params import P6Params


def learned_delay(spells, p6: P6Params) -> Optional[int]:
    """The nearest-rank quantile (``learn_quantile``) of the spells in whole seconds, clamped to the learned range;
    None below ``spell_min_for_learn`` spells. n = 20 spells give the 19th sorted, n = 10 the 10th, n = 50 the 48th."""
    n = len(spells)
    if n < p6.spell_min_for_learn:
        return None
    s = sorted(int(x) for x in spells)
    rank = int(math.ceil(round(p6.learn_quantile * n, 9)))
    rank = min(max(rank, 1), n)
    v = s[rank - 1]
    return min(max(v, p6.delay_learn_min_s), p6.delay_learn_max_s)


def reliable(ld, p6: P6Params) -> bool:
    """``ld`` = [on_s, known_s]: at least ``ldr_min_known_s`` of known time and an on share of ``ldr_min_share``."""
    if not ld:
        return False
    on_s, known_s = ld[0], ld[1]
    if known_s < p6.ldr_min_known_s or known_s <= 0:
        return False
    return on_s / known_s >= p6.ldr_min_share


def copy_info(L, suggestion: dict, night: int) -> dict:
    """The ``zone6.info`` entry of a zone from its source suggestion (a dict of ``Learner.ns.suggestions``)."""
    ev = suggestion.get("evidence") or {}
    th = {}
    for cls, key in (("wd", "hours_wd"), ("we", "hours_we")):
        rngs = typical_hours(ev.get(key), L.p5params)
        if rngs:
            th[cls] = list(rngs)
    var = []
    for v in (suggestion.get("variants") or [])[:L.p6params.variants_max]:
        s, e = int(v["start_h"]), int(v["end_h"])
        if 0 <= s <= 24 and 0 <= e <= 24 and v.get("days") in ("wd", "we"):
            var.append({"d": v["days"], "s": s, "e": e, "n": max(0, int(v.get("stays", 0)))})
    conf = min(1.0, max(0.0, round(float(suggestion.get("conf", 0.0)), 2)))
    return {"th": th, "var": var, "conf": conf, "upd": night}


def nightly(L, night: int) -> None:
    z6 = L.zone6
    ze = L.zone_eval
    p6 = L.p6params
    ze.flush_ld()
    recs = {z["zid"]: z for z in L.review.zones}
    sugg = {s["id"]: s for s in L.ns.suggestions}
    for zid in ze.zone_ids():
        # learned delay
        sp = z6.spells(zid)
        cd = learned_delay(sp, p6)
        if cd is not None:
            e = z6.delay.setdefault(zid, {"sp": list(sp)})
            e["cd"] = cd
            e["upd"] = night
        # the copy of the source suggestion
        rec = recs.get(zid)
        if rec is not None:
            s = sugg.get(rec.get("ref"))
            if s is not None:
                z6.info[zid] = copy_info(L, s, night)
    # ld2412_reliable
    z6.set_ldr([zid for zid, ld in z6.ld.items() if zid in ze.zones and reliable(ld, p6)])
    # a request for a variant that is gone is dropped
    for zid in list(z6.ve):
        info = z6.info.get(zid)
        ids = {variant_id(v) for v in info["var"]} if info else set()
        keep = [v for v in z6.ve[zid] if v in ids]
        if keep:
            z6.ve[zid] = keep
        else:
            del z6.ve[zid]
    ze.refresh()
