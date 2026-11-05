"""Radar-moved detection and the reset of the learned maps (P7 spec 4.6, 4.7). Owner: CORE.

Features are two timestamped point sets in the radar frame: the door events and the static clutter events (ghost events of
``why`` ``identical`` or ``home_empty``; ``ld_off`` is excluded: a person in an LD2412 blind spot makes those, and a person
moves with the furniture). A rotation of the radar rotates every point about the origin and a translation shifts them; a
moved sofa moves neither. ``recent`` (the newest 1-3 covered dates, as many as give ``rm_min_recent`` points) is matched to
the dilated cell mask of the ``reference`` points of the other dates over rigid transforms (rotation +-10 degrees in 2.5
degree steps, shift +-1 m in 0.2 m steps); the night is *moved* when the best transform is large enough, its hit rate is at
least ``rm_corr_min`` and it beats the identity by ``rm_gain_min``. Two consecutive moved nights open a latched item that
stays open until it is answered; while a streak counts or the item is open the other proposals and the drift counters are
held (the *radar hold*). Approve resets the learned maps in place and keeps the zones as 'needs re-check'.
"""
from __future__ import annotations

import math
from array import array

from .covis import Covis
from .grids import GRID_NAMES
from .types import RadarMovedView

_PAD = 48                          # cells of padding of the encoded mask keys: a shifted point never wraps to another row
_EPS = 1e-9
_STATIC_WHY = ("identical", "home_empty")


# ---------------------------------------------------------------- state names, view, hold
def state_name(rm: dict) -> str:
    """One of RADAR_STATES: ``open`` while the latched item is open, ``watching`` while a streak counts, ``ignored``
    after an Ignore until the next detection, else ``none``."""
    if rm["open"]:
        return "open"
    if rm["st"] >= 1:
        return "watching"
    if rm["ign"] and rm["ign"] == rm["rev"]:
        return "ignored"
    return "none"


def hold(L) -> bool:
    """The radar hold (4.6): a streak is counting and the item is not open, or the item is open and at most
    ``rm_hold_days`` days have passed since the first moved date."""
    rm = L.zone7.rm
    if rm["open"]:
        run = L.last_run_ord
        return bool(run is not None and rm["d0"] > 0 and run - rm["d0"] <= L.p7params.rm_hold_days)
    return rm["st"] >= 1


def view(L) -> RadarMovedView:
    rm = L.zone7.rm
    return RadarMovedView(state_name(rm), rm["rev"], rm["st"], tuple(rm["tx"]), hold(L))


# ---------------------------------------------------------------- features
def points(L, d14: list) -> dict:
    """{date ordinal: [(x_m, y_m), ...]} of the door events and the static clutter events of the dates in ``d14``."""
    ds = set(d14)
    out = {d: [] for d in d14}
    local = L.tm.local
    for e in L.bh.door_events:
        d = local(e["t"] * 1000).date_ord
        if d in ds:
            out[d].append((e["x"] / 100.0, e["y"] / 100.0))
    for e in L.bh.ghost_events:
        if e.get("why") in _STATIC_WHY:
            d = local(e["t"] * 1000).date_ord
            if d in ds:
                out[d].append((e["x"] / 100.0, e["y"] / 100.0))
    return out


def _subsample(pts: list, n: int) -> list:
    if len(pts) <= n:
        return pts
    m = len(pts)
    return [pts[i * m // n] for i in range(n)]


def mask_of(geom, pts: list, rings: int) -> frozenset:
    """The cells of the reference points, dilated by ``rings`` 8-neighbour rings."""
    cells = set()
    for x, y in pts:
        c = geom.cell_of(x, y)
        if c >= 0:
            cells.add(c)
    for _ in range(rings):
        cells = geom.dilate(cells)
    return frozenset(cells)


# ---------------------------------------------------------------- the search
def search(recent: list, ref_cells, p7, geom) -> tuple:
    """((dx_m, dy_m, deg), hit_star, hit_identity): the best rigid transform ``p -> Rot(deg) p + (dx, dy)`` of the recent
    points (at most ``rm_max_points``, evenly subsampled) onto the reference mask, and the hit rates of the best
    transform and of the identity. Ties go to the smaller shift, then the smaller rotation."""
    pts = _subsample(recent, p7.rm_max_points)
    n = len(pts)
    if n == 0:
        return (0.0, 0.0, 0.0), 0.0, 0.0
    nx, cell = geom.nx, geom.cell
    w = nx + 2 * _PAD
    mset = set()
    for c in ref_cells:
        mset.add((c // nx + _PAD) * w + (c % nx + _PAD))
    ns = int(round(p7.rm_search_m / p7.rm_search_step_m)) if p7.rm_search_step_m > 0 else 0
    nr = int(round(p7.rm_rot_max_deg / p7.rm_rot_step_deg)) if p7.rm_rot_step_deg > 0 else 0
    k = p7.rm_search_step_m / cell
    fast = k >= 1.0 - _EPS and abs(k - round(k)) < 1e-6
    ks = int(round(k))
    best = None                                     # (-hits, |shift|, |deg|, dx, dy, deg)
    ident = 0
    for r in range(-nr, nr + 1):
        deg = r * p7.rm_rot_step_deg
        a = math.radians(deg)
        ca, sa = math.cos(a), math.sin(a)
        rot = [(x * ca - y * sa, x * sa + y * ca) for x, y in pts]
        if fast:
            base = []
            for rx, ry in rot:
                i0 = int(math.floor((rx - geom.x0) / cell))
                j0 = int(math.floor((ry - geom.y0) / cell))
                base.append((j0 + _PAD) * w + i0 + _PAD)
            for kj in range(-ns, ns + 1):
                for ki in range(-ns, ns + 1):
                    off = kj * ks * w + ki * ks
                    hits = 0
                    for b in base:
                        if b + off in mset:
                            hits += 1
                    dx, dy = ki * p7.rm_search_step_m, kj * p7.rm_search_step_m
                    if r == 0 and ki == 0 and kj == 0:
                        ident = hits
                    key = (-hits, math.hypot(dx, dy), abs(deg), dx, dy, deg)
                    if best is None or key < best:
                        best = key
        else:
            for kj in range(-ns, ns + 1):
                for ki in range(-ns, ns + 1):
                    dx, dy = ki * p7.rm_search_step_m, kj * p7.rm_search_step_m
                    hits = 0
                    for rx, ry in rot:
                        c = geom.cell_of(rx + dx, ry + dy)
                        if c >= 0 and (c // nx + _PAD) * w + (c % nx + _PAD) in mset:
                            hits += 1
                    if r == 0 and ki == 0 and kj == 0:
                        ident = hits
                    key = (-hits, math.hypot(dx, dy), abs(deg), dx, dy, deg)
                    if best is None or key < best:
                        best = key
    return (best[3], best[4], best[5]), -best[0] / n, ident / n


# ---------------------------------------------------------------- the nightly step (4.6)
def nightly(L, night: int) -> None:
    """P7 step 3: writes ``zone7.rm``. A covered date is evaluated once (a restart repeats the run of the same date)."""
    p = L.p7params
    rm = L.zone7.rm
    run = L.last_run_ord
    if run is None:
        return
    d14 = L.obs.covered_dates(run)[-p.slow_dates:]
    if not d14:
        return
    d_last = d14[-1]
    if d_last <= rm["n"]:
        return
    rm["n"] = d_last
    if rm["open"] or rm["qt"] > run:                       # latched, or quiet after an Ignore: only the date is marked
        return
    pts = points(L, d14)
    k = next((kk for kk in range(1, p.rm_recent_max_dates + 1)
              if sum(len(pts[d]) for d in d14[-kk:]) >= p.rm_min_recent), None)
    if k is None:
        L.p7_radar_dbg = {"night": night, "verdict": "insufficient", "why": "recent"}
        return                                             # insufficient: the streak is held
    recent_dates = d14[-k:]
    ref_dates = d14[:-k]
    if rm["st"] >= 1 and rm["d0"] > 0:                     # moved dates must not enter the reference while a streak counts
        ref_dates = [d for d in ref_dates if d < rm["d0"]]
    recent = [q for d in recent_dates for q in pts[d]]
    ref = [q for d in ref_dates for q in pts[d]]
    if len(ref) < p.rm_min_ref:
        L.p7_radar_dbg = {"night": night, "verdict": "insufficient", "why": "reference", "recent": len(recent),
                          "ref": len(ref)}
        return
    geom = L.geom
    (dx, dy, deg), hit_star, hit_id = search(recent, mask_of(geom, ref, p.rm_tol_rings), p, geom)
    big = math.hypot(dx, dy) >= p.rm_shift_min_m - _EPS or abs(deg) >= p.rm_rot_min_deg - _EPS
    moved = bool(big and hit_star >= p.rm_corr_min - _EPS and hit_star - hit_id >= p.rm_gain_min - _EPS)
    L.p7_radar_dbg = {"night": night, "verdict": "moved" if moved else "ok", "k": k, "recent": len(recent),
                      "ref": len(ref), "hit": round(hit_star, 3), "hit_id": round(hit_id, 3),
                      "tx": [round(dx * 100), round(dy * 100), round(deg * 10)]}
    if moved:
        rm["st"] += 1
        rm["tx"] = [int(round(dx * 100)), int(round(dy * 100)), int(round(deg * 10))]
        if rm["st"] == 1:
            rm["d0"] = recent_dates[0]
        if rm["st"] >= p.rm_nights:
            rm["open"] = True
            rm["rev"] += 1
    else:
        rm["st"] = 0
        rm["d0"] = 0


# ---------------------------------------------------------------- the reset (4.7)
def _zero(arr) -> None:
    arr[:] = array(arr.typecode, bytes(arr.itemsize * len(arr)))


def reset_maps(L) -> None:
    """The in-place clearing of 4.7: the nine grids and their clocks, the DAYS bitmask, the covis matrix, the stay, door
    and ghost lists, the candidates, ``doors_prev``, the outline and the envelope; every suggestion becomes ``stale``;
    ``p5.masks``, ``p5.real``, ``p5.later``, the ``place`` suppressions and ``zone7.mf`` go (all of them are in the old
    frame). Kept: the coverage table (time-based), the night model and window mirror, the settings, the push counters, the
    ``night_window`` and ``bed_needs_data`` suppressions and ``review.later`` (nothing to do with the radar pose), the zone
    records and labels with their ``zone6`` data, the tags."""
    G = L.grids
    for name in GRID_NAMES:
        _zero(G.g[name])
    _zero(G.days_bits)
    G.days_base = None
    G.eff_s = 0.0
    G.eff_f = 0.0
    L.obs.obs_s_since_extraction = 0.0
    fresh = Covis(L.p)
    L.covis = fresh
    L.bh.covis = fresh
    L.bh.stays.clear()
    L.bh.door_events.clear()
    L.bh.ghost_events.clear()
    ns = L.ns
    ns.candidates = []
    ns.doors_prev = []
    ns.outline = {"cells": frozenset(), "streak": 0, "done": False}
    ns.envelope = frozenset()
    for s in ns.suggestions:
        s["state"] = "stale"
    L.p5.masks.clear()
    L.p5.real.clear()
    L.p5.later.clear()
    L.review.suppressions = [s for s in L.review.suppressions if s["type"] != "place"]
    L.zone7.mf.clear()
    L.p7_requalified.clear()
    L.review.touch()
    L.rv_dirty = True


def summary(L) -> dict:
    """Read-only counts for the replay's check of the reset: the number of non-zero cells over the nine grids, the
    candidates, the stays and the door and ghost events."""
    nonzero = 0
    for name in GRID_NAMES:
        nonzero += sum(1 for v in L.grids.g[name] if v != 0.0)
    return {"grids_nonzero": nonzero, "candidates": len(L.ns.candidates), "stays": len(L.bh.stays),
            "door_events": len(L.bh.door_events), "ghost_events": len(L.bh.ghost_events)}
