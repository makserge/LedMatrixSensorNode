"""Coverage-normalised per-zone drift with freezing (P7 spec 4.2). Owner: CORE.

Every night, for every confirmed zone (records and labels), a fast (3 covered dates) over slow (14 covered dates) ratio of
dwell per observed second, summed only over the hours observed at least ``cov_min_frac`` in both windows. The two windows
are decay-weighted windows over the covered dates (half-lives ``fast_hl`` and ``slow_hl``), derived each night from two
persisted structures that already exist: the stay list and the per-date, per-hour coverage table (DV1). Per zone only what
cannot be re-derived is stored in ``zone7`` (the usual share profile ``u``, the flag run ``lo``, the retire run ``rt``,
the dates ``n``, ``lu`` and the last verdict ``vs``/``fz``).

``verdict`` states, in this order (the first that matches): young, blind, frozen, none, collapsed, ok, falling. A zone
whose usual hours are under ``frozen_share`` observed is frozen (no flag, no modify, no retire). Nothing here touches
the per-frame path; it all runs once a night.
"""
from __future__ import annotations

from datetime import date

from . import radarmoved
from . import zones as zones_mod
from .types import ZoneDrift

_EPS = 1e-9
COUNT_MAX = 99999                                          # the p7state bound of the run counters


class ZRef:
    """One confirmed zone as the drift module sees it (a record or a label)."""
    __slots__ = ("zid", "kind", "cells", "night", "ref", "label", "rec")

    def __init__(self, zid, kind, cells, night, ref, label, rec) -> None:
        self.zid = zid
        self.kind = kind
        self.cells = cells
        self.night = night
        self.ref = ref
        self.label = label
        self.rec = rec


def zone_refs(L) -> list:
    """Records (ordered by zid) then labels (ordered by id), the order of ``Learner.zones()``."""
    out = [ZRef(z["zid"], z["kind"], frozenset(z["cells"]), z["night"], z.get("ref"), False, z)
           for z in sorted(L.review.zones, key=lambda r: r["zid"])]
    out += [ZRef(lb["id"], lb["kind"], frozenset(lb["cells"]), zones_mod._label_night(L, lb), None, True, lb)
            for lb in sorted(L.label_list, key=lambda d: d["id"])]
    return out


# ---------------------------------------------------------------- the windows (4.2.1)
def windows(L, run_ord: int) -> tuple:
    """(d14, d3, w_fast, w_slow): the covered dates of the slow window (ascending), the fast window's, and the weight of
    each date, ``2 ** (-k / half_life)`` with ``k`` the number of covered dates after it (0 for the newest)."""
    p = L.p7params
    d14 = L.obs.covered_dates(run_ord)[-p.slow_dates:]
    d3 = d14[-p.fast_dates:]
    ws = {d: 2.0 ** (-k / p.slow_hl) for k, d in enumerate(reversed(d14))}
    wf = {d: 2.0 ** (-k / p.fast_hl) for k, d in enumerate(reversed(d3))}
    return d14, d3, wf, ws


class Ctx:
    """The night's zone-independent part: windows, coverage per hour, the eligible hours and the stay index."""
    __slots__ = ("p", "win", "d14", "wf", "ws", "covf", "covs", "elig", "by_cell", "run_ord")


def context(L, run_ord=None) -> Ctx:
    p = L.p7params
    c = Ctx()
    c.p = p
    c.run_ord = L.last_run_ord if run_ord is None else run_ord
    c.win = windows(L, c.run_ord)
    d14, d3, wf, ws = c.win
    c.d14, c.wf, c.ws = d14, wf, ws
    cov = L.obs.coverage
    covf = [0.0] * 24
    covs = [0.0] * 24
    for d in d14:
        row = cov.get(d)
        if row is None:
            continue
        w = ws[d]
        wfd = wf.get(d)
        for h in range(24):
            v = row[h]
            covs[h] += w * v
            if wfd is not None:
                covf[h] += wfd * v
    c.covf, c.covs = covf, covs
    thr_f = p.cov_min_frac * sum(wf.values()) * 3600.0
    thr_s = p.cov_min_frac * sum(ws.values()) * 3600.0
    c.elig = [h for h in range(24) if covf[h] > _EPS and covs[h] > _EPS and covf[h] >= thr_f and covs[h] >= thr_s] \
        if d14 else []
    # the stay index: cell -> [(date ordinal, ((hour, seconds), ...), dur)], stays of the retained covered dates only
    geom, tm = L.geom, L.tm
    by_cell: dict = {}
    for st in L.bh.stays:
        try:
            d = date.fromisoformat(st["day"]).toordinal()
        except (ValueError, KeyError, TypeError):
            continue
        if d not in ws:
            continue
        cell = geom.cell_of(st["x"] / 100.0, st["y"] / 100.0)
        if cell < 0:
            continue
        sod = tm.local(st["t0"] * 1000).sec_of_day
        rem = int(st["dur"])
        dur = rem
        hs = []
        while rem > 0:                                    # the split of profiles.build_profiles
            h = sod // 3600
            seg = min(rem, 3600 - sod % 3600)
            hs.append((h, seg))
            rem -= seg
            sod = (sod + seg) % 86400
        by_cell.setdefault(cell, []).append((d, tuple(hs), dur))
    c.by_cell = by_cell
    return c


def series(ctx: Ctx, cells) -> tuple:
    """(dw_fast[24], dw_slow[24], stays in the slow window, {date: dwell seconds}) of the stays whose centroid cell lies
    in ``cells`` (the zone's own, undilated cells: one footprint, D39)."""
    dwf = [0.0] * 24
    dws = [0.0] * 24
    n = 0
    per: dict = {}
    ws, wf, by = ctx.ws, ctx.wf, ctx.by_cell
    for cell in cells:
        for d, hs, dur in by.get(cell, ()):
            n += 1
            per[d] = per.get(d, 0) + dur
            w_s = ws[d]
            w_f = wf.get(d)
            for h, sec in hs:
                dws[h] += w_s * sec
                if w_f is not None:
                    dwf[h] += w_f * sec
    return dwf, dws, n, per


def rho_sums(ctx: Ctx, dwf, dws) -> tuple:
    """(sum of rho_fast over the eligible hours, sum of rho_slow over them, rho_slow[24] with 0 outside them)."""
    rf = 0.0
    rs = 0.0
    rho = [0.0] * 24
    for h in ctx.elig:
        rf += dwf[h] / ctx.covf[h]
        v = dws[h] / ctx.covs[h]
        rs += v
        rho[h] = v
    return rf, rs, rho


# ---------------------------------------------------------------- the usual profile (4.2.4)
def _norm(v: list) -> list:
    s = sum(v)
    if s <= 0:
        return []
    return [round(x / s, 3) for x in v]


def _hours_at(L, z: ZRef):
    """The confirmation-time hour profile of the zone's ``ref`` suggestion (never for a label: DF14), or None."""
    if z.label:
        return None
    s = zones_mod.suggestion_of(L, z.ref)
    if s is None:
        return None
    h = (s.get("evidence") or {}).get("hours_at_suggestion")
    if not isinstance(h, list) or len(h) != 24:
        return None
    try:
        vals = [float(x) for x in h]
    except (TypeError, ValueError):
        return None
    return vals if sum(vals) > 0 and all(x >= 0 for x in vals) else None


def _init_u(L, z: ZRef, rho: list) -> list:
    h = _hours_at(L, z)
    if h is not None:
        return _norm(h)
    return _norm(rho)                                     # a label (or a zone whose suggestion is gone): observed hours


def _refresh_u(u: list, rho: list, elig: list, p) -> list:
    """``u <- normalise((1 - alpha) * u + alpha * normalise(rho_slow))`` over the eligible hours; the mass of the hours
    that were not observed keeps its weight (it is what keeps a bed learned during Night learning frozen). The caller
    has checked that the eligible hours hold at least ``usual_full_cov`` of the mass."""
    total = sum(u)
    if total <= 0:
        return u
    s = sum(rho[h] for h in elig)
    if s <= 0:
        return u
    m = sum(u[h] for h in elig)
    out = list(u)
    for h in elig:
        out[h] = (1.0 - p.usual_alpha) * u[h] + p.usual_alpha * m * rho[h] / s
    return _norm(out)


# ---------------------------------------------------------------- the verdict (4.2.3)
class Res:
    __slots__ = ("state", "ratio", "fz", "u", "u_init", "rho", "n_stays", "per", "frozen_cov")

    def __init__(self) -> None:
        self.state = "young"
        self.ratio = None
        self.fz = False
        self.u = []
        self.u_init = False
        self.rho = [0.0] * 24
        self.n_stays = 0
        self.per = {}
        self.frozen_cov = False


def judge(L, z: ZRef, entry: dict, ctx: Ctx, held: bool) -> Res:
    """The verdict of one zone for the night described by ``ctx`` (pure: nothing is written). ``entry`` is the zone's
    ``zone7`` entry or the defaults; ``held`` says the night is held (rc or the radar hold: ``u`` is not initialised)."""
    p = ctx.p
    r = Res()
    r.u = entry["u"]
    dwf, dws, r.n_stays, r.per = series(ctx, z.cells)
    if L.night - z.night < p.min_zone_age_nights or len(ctx.d14) < p.min_zone_age_nights:
        r.state = "young"
        return r
    if not ctx.elig:
        r.state = "blind"
        r.fz = True
        return r
    rf, rs, r.rho = rho_sums(ctx, dwf, dws)
    if not r.u and not held:
        u = _init_u(L, z, r.rho)
        if u:
            r.u = u
            r.u_init = True
    r.frozen_cov = bool(r.u) and sum(r.u[h] for h in ctx.elig) < p.frozen_share
    static = p.static_freeze and zones_mod.is_frozen(L, z.kind, z.ref)
    if r.frozen_cov or static:
        r.state = "frozen"
        r.fz = r.frozen_cov
        r.ratio = (rf / rs) if rs > _EPS else (0.0 if r.u else None)
        return r
    if not r.u and (r.n_stays < p.min_slow_stays or rs <= _EPS):
        r.state = "none"
        return r
    if rs <= _EPS:
        r.state = "collapsed"
        r.ratio = 0.0
        return r
    r.ratio = rf / rs
    r.state = "falling" if r.ratio < p.ratio_falling else "ok"
    return r


def verdict(L, zone, entry, win=None) -> ZoneDrift:
    """The verdict as a ``ZoneDrift`` (pure). ``zone`` is a ``ZRef``, a zone record or a label dict; ``entry`` the zone's
    ``zone7`` entry or None (the defaults). The held flag is read from the learner."""
    z = zone if isinstance(zone, ZRef) else _zref_of(L, zone)
    e = entry if entry is not None else _default_entry()
    ctx = context(L) if win is None else win if isinstance(win, Ctx) else context(L)
    held = bool(e["rc"] or radarmoved.hold(L))
    r = judge(L, z, e, ctx, held)
    return _to_drift(L, z, e, r, held)


def _default_entry() -> dict:
    from .p7state import default_entry
    return default_entry()


def _zref_of(L, zone: dict) -> ZRef:
    if "zid" in zone:
        return ZRef(zone["zid"], zone["kind"], frozenset(zone["cells"]), zone["night"], zone.get("ref"), False, zone)
    return ZRef(zone["id"], zone["kind"], frozenset(zone["cells"]), zones_mod._label_night(L, zone), None, True, zone)


def _to_drift(L, z: ZRef, e: dict, r: Res, held: bool) -> ZoneDrift:
    ratio = -1.0 if r.ratio is None or r.state in ("young", "blind", "none") else round(r.ratio, 3)
    return ZoneDrift(z.zid, r.state, ratio, zones_mod.is_frozen(L, z.kind, z.ref, zid=z.zid), e["lo"], e["rt"],
                     e["lu"], bool(e["rc"]), held)


def view(L) -> tuple:
    """``Learner.drift_view()`` (P7 enabled): one ``ZoneDrift`` per zone record, then per label. The state is the stored
    verdict of the last nightly run; the ratio is recomputed from the same data."""
    refs = zone_refs(L)
    if not refs:
        return ()
    zone7 = L.zone7
    hold = radarmoved.hold(L)
    ctx = context(L) if L.last_run_ord is not None else None
    out = []
    for z in refs:
        e = zone7.entry(z.zid)
        frozen = zones_mod.is_frozen(L, z.kind, z.ref, zid=z.zid)
        if e is None or ctx is None:
            out.append(ZoneDrift(z.zid, "young", -1.0, frozen, 0, 0, 0, False, bool(hold)))
            continue
        held = bool(e["rc"] or hold)
        r = judge(L, z, e, ctx, held)
        state = e["vs"]
        ratio = -1.0 if r.ratio is None or state in ("young", "blind", "none") else round(r.ratio, 3)
        out.append(ZoneDrift(z.zid, state, ratio, frozen, e["lo"], e["rt"], e["lu"], bool(e["rc"]), held))
    return tuple(out)


def zone_ratio(L, zid: str):
    """The current ratio of one zone (None when it is not computed), for the answer counters of the tuning data."""
    ctx = context(L)
    for z in zone_refs(L):
        if z.zid == zid:
            e = L.zone7.entry(zid) or _default_entry()
            r = judge(L, z, e, ctx, bool(e["rc"] or radarmoved.hold(L)))
            return None if r.ratio is None or r.state in ("young", "blind", "none") else r.ratio
    return None


# ---------------------------------------------------------------- the nightly step (4.2.3 to 4.2.6)
def nightly(L, night: int) -> None:
    """Writes ``zone7`` for every record and label: the verdict, then ``u``, ``lo``, ``rt``, ``n``, ``lu``, ``fz`` and
    ``vs`` (4.2.3-4.2.6). Entries are created at a zone's first verdict. Called only when P7 is enabled."""
    run = L.last_run_ord
    if run is None:
        return
    p = L.p7params
    zone7 = L.zone7
    ctx = context(L, run)
    hold = radarmoved.hold(L)
    d_last = ctx.d14[-1] if ctx.d14 else 0
    for z in zone_refs(L):
        e = zone7.entry(z.zid, create=True)
        if e["rc"] and e["rd"] and run - e["rd"] >= p.recheck_days:
            e["rc"] = False                                # 'needs re-check' ends by itself (4.7)
        held = bool(e["rc"] or hold)
        r = judge(L, z, e, ctx, held)
        e["vs"] = r.state
        e["fz"] = r.fz
        if r.u_init and not held:
            e["u"] = r.u
        new_date = d_last > e["n"]                         # a covered date counts once, however often the run repeats
        if r.state in ("ok", "falling", "collapsed") and not held:
            if new_date:
                ratio = r.ratio
                e["lo"] = min(e["lo"] + 1, COUNT_MAX) if ratio < p.ratio_flag else 0
                if ratio < p.ratio_retire:
                    e["rt"] = min(e["rt"] + 1, COUNT_MAX)
                elif ratio >= p.retire_clear:
                    e["rt"] = 0
            last = max((d for d, sec in r.per.items() if sec >= p.lastuse_min_dwell_s), default=0)
            if last > e["lu"]:
                e["lu"] = last
            if new_date and r.state == "ok" and e["u"] and not r.u_init:
                total = sum(e["u"])
                if total > 0 and sum(e["u"][h] for h in ctx.elig) >= p.usual_full_cov * total:
                    e["u"] = _refresh_u(e["u"], r.rho, ctx.elig, p)
        if new_date:
            e["n"] = d_last                                # the date is seen (held, frozen, blind, none, young too)
