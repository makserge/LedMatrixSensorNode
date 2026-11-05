"""Ground truth of a replay run (P3 spec 11.6).

* **Places** (seat level): desk, table, counter, sofaA, sofaB, armchair, and bed in the bedroom world, plus
  the composite sofaAB. Only sessions of the residents P1 and P2 count (guest, visitor and pet spots are
  false-positive spots, as in the simulator's ``label``).
* **Truth grid** per place and day: the learner's splat (0.2 m cells, 5x5 Gaussian kernel, sigma 0.2 m) at
  the session's true position, weight = the session's seconds / 60.
* **Truth region** of a place at night N (D33): the sum of its day grids for days N-14..N-1, blurred with
  the learner's 3x3 binomial blur; the 4-connected component containing the maximum, of the cells at or
  above 0.35 x the maximum. sofaAB uses the sum of the two seats' grids.
* **Cut share**: the session seconds inside the radar-off window over all session seconds (as the
  simulator's truth counters, per day with that day's off intervals).
* **Core place**: used on at least 50% of the run's days (D39).

The splat and the blur are the simulator's (``SIM:33-71``), which the core uses too (P3 spec 7.6); they
are evaluated sparsely here (only cells near the sessions), with the same edge normalisation.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Optional

from . import world as W

PLACES = ("desk", "table", "counter", "sofaA", "sofaB", "armchair", "bed")
DAY_PLACES = ("desk", "table", "counter", "sofaA", "sofaB")
COMPOSITE = {"sofaAB": ("sofaA", "sofaB")}
RESIDENTS = ("P1", "P2")
REGION_FRAC = 0.35
REGION_DAYS = 14
CORE_DAY_SHARE = 0.5

NX, NY, NC = W.NX, W.NY, W.NC
_KER = []
for _di in range(-2, 3):
    for _dj in range(-2, 3):
        _KER.append((_di, _dj, math.exp(-0.5 * ((_di * W.CELL) ** 2 + (_dj * W.CELL) ** 2) / 0.2 ** 2)))
_ks = sum(k for _, _, k in _KER)
KER = [(a, b, k / _ks) for a, b, k in _KER]


def splat_sparse(g: dict, x: float, y: float, w: float) -> None:
    """Add the simulator's splat at (x, y) with weight w to the sparse grid ``g`` (cell -> value)."""
    i0 = int(math.floor((x - W.GX0) / W.CELL))
    j0 = int(math.floor((y - W.GY0) / W.CELL))
    for di, dj, k in KER:
        i = i0 + di
        j = j0 + dj
        if 0 <= i < NX and 0 <= j < NY:
            c = j * NX + i
            g[c] = g.get(c, 0.0) + w * k


def blur_sparse(g: dict) -> dict:
    """The simulator's 3x3 (1 2 1) blur, normalised by the in-grid weights, on the cells it can reach."""
    support = set()
    for c in g:
        i, j = c % NX, c // NX
        for dj in (-1, 0, 1):
            jj = j + dj
            if 0 <= jj < NY:
                for di in (-1, 0, 1):
                    ii = i + di
                    if 0 <= ii < NX:
                        support.add(jj * NX + ii)
    w = (1, 2, 1)
    out = {}
    for c in sorted(support):
        i, j = c % NX, c // NX
        s = 0.0
        ws = 0
        for dj in (-1, 0, 1):
            jj = j + dj
            if not 0 <= jj < NY:
                continue
            for di in (-1, 0, 1):
                ii = i + di
                if not 0 <= ii < NX:
                    continue
                k = w[di + 1] * w[dj + 1]
                s += g.get(jj * NX + ii, 0.0) * k
                ws += k
        out[c] = s / ws
    return out


def nbrs4(c: int):
    i, j = c % NX, c // NX
    if i > 0:
        yield c - 1
    if i < NX - 1:
        yield c + 1
    if j > 0:
        yield c - NX
    if j < NY - 1:
        yield c + NX


def peak_region(g: dict, frac: float = REGION_FRAC):
    """(region cells, max cell) of a blurred sparse grid: the 4-connected component of cells >= frac x max
    that contains the maximum (ties: the lowest cell index). Empty grid -> (frozenset(), None)."""
    if not g:
        return frozenset(), None
    mx = max(g.values())
    if mx <= 0:
        return frozenset(), None
    top = min(c for c, v in g.items() if v == mx)
    thr = frac * mx
    region = {top}
    stack = [top]
    while stack:
        c = stack.pop()
        for n in nbrs4(c):
            if n not in region and g.get(n, 0.0) >= thr:
                region.add(n)
                stack.append(n)
    return frozenset(region), top


def iou(a, b) -> float:
    if not a or not b:
        return 0.0
    a = a if isinstance(a, (set, frozenset)) else set(a)
    b = b if isinstance(b, (set, frozenset)) else set(b)
    return len(a & b) / len(a | b)


def rle(cells) -> list:
    """Run-length list ``[[start, length], ...]`` over sorted cell indices (the state file's encoding)."""
    out = []
    for c in sorted(cells):
        if out and out[-1][0] + out[-1][1] == c:
            out[-1][1] += 1
        else:
            out.append([c, 1])
    return out


def unrle(runs) -> frozenset:
    return frozenset(c for s, n in runs for c in range(s, s + n))


class Truth:
    """Accumulates the truth of one run from the world's days (``add_day``) and answers per night."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.places = tuple(p for p in PLACES if p != "bed" or cfg.get("bed"))
        self.grids = defaultdict(dict)          # (place, day) -> sparse grid
        self.used = defaultdict(set)            # place -> days with a session
        self.by_day = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # day -> place -> [occ, cut]
        self.sit_s = defaultdict(float)         # day -> true 'sit' seconds of P1 and P2
        self.sessions = defaultdict(list)       # day -> [(agent, place, mode, t0, t1, x, y)] (all agents)
        self.days = set()
        self._regions = {}

    def add_day(self, rec) -> None:
        d = rec.d
        self.days.add(d)
        off = sorted(W.night_window_off(d, self.cfg))
        self.sessions[d] = list(rec.sessions)
        for ag, place, mode, t0, t1, x, y in rec.sessions:
            if ag not in RESIDENTS:
                continue
            dur = t1 - t0
            if mode == "sit":
                self.sit_s[d] += dur
            if place not in self.places:
                continue
            cut = W.overlap(off, t0, t1)
            self.by_day[d][place][0] += dur
            self.by_day[d][place][1] += cut
            self.used[place].add(d)
            splat_sparse(self.grids[(place, d)], x, y, dur / 60.0)

    # ------------------------------------------------------------ per run
    def cut_shares(self, days) -> dict:
        """{place: share of its true use inside the radar-off window} over the given days."""
        out = {}
        for p in self.places:
            occ = sum(self.by_day[d][p][0] for d in days if d in self.by_day)
            cut = sum(self.by_day[d][p][1] for d in days if d in self.by_day)
            out[p] = (cut / occ) if occ > 0 else None
        return out

    def occupancy_h(self, days) -> dict:
        return {p: sum(self.by_day[d][p][0] for d in days if d in self.by_day) / 3600.0 for p in self.places}

    def core_places(self, days) -> list:
        days = list(days)
        if not days:
            return []
        return [p for p in self.places if len(self.used[p] & set(days)) >= CORE_DAY_SHARE * len(days)]

    def sit_seconds(self, days) -> float:
        return sum(self.sit_s.get(d, 0.0) for d in days)

    # ------------------------------------------------------------ per night
    def _grid(self, place: str, night: int) -> dict:
        members = COMPOSITE.get(place, (place,))
        g = {}
        for d in range(max(0, night - REGION_DAYS), night):
            for m in members:
                for c, v in self.grids.get((m, d), {}).items():
                    g[c] = g.get(c, 0.0) + v
        return g

    def region(self, place: str, night: int):
        """(cells, max cell) of the truth region of ``place`` at ``night`` (days night-14..night-1)."""
        key = (place, night)
        if key not in self._regions:
            self._regions[key] = peak_region(blur_sparse(self._grid(place, night)))
        return self._regions[key]

    def all_places(self) -> tuple:
        return self.places + (("sofaAB",) if "sofaA" in self.places else ())

    def regions(self, night: int) -> dict:
        return {p: self.region(p, night) for p in self.all_places()}

    def forget_regions_before(self, night: int) -> None:
        for k in [k for k in self._regions if k[1] < night]:
            del self._regions[k]

    def label(self, x: float, y: float, night: int) -> str:
        """The simulator's truth label at the suggestion's night (``label(cx, cy, d, cfg)``, d = night - 1)."""
        return W.label(x, y, max(0, night - 1), self.cfg)


class NightTruth:
    """The true sleep onsets and wakes of the run (P4, OA 7.5), per night = the evening's day index ``n``.

    The onset of night n is the start of P1's bed sleep of the evening of day n (also when it falls after 00:00,
    which the simulator drops: ``WorldDay.late``); the wake of night n is the end of P1's bed sleep on day n + 1.
    Night -1 has a wake only (day 0 starts with the sleeper in bed). Values in sim seconds; ``nm`` is the learner's
    night axis: minutes after 12:00 of the evening's date.
    """

    def __init__(self):
        self.onset_s = {}
        self.wake_s = {}
        self.late = []

    def add_day(self, rec) -> None:
        d = rec.d
        for ag, place, mode, t0, t1, x, y in rec.sessions:
            if ag == "P1" and place == "bed" and mode == "sleep":
                if t0 == d * W.DAY:
                    self.wake_s[d - 1] = t1
                else:
                    self.onset_s[d] = t0
        for ag, s0 in rec.late:
            if ag == "P1":
                self.onset_s[d] = s0
                self.late.append(d)

    @staticmethod
    def nm(night: int, t_s: int) -> int:
        return (t_s - night * W.DAY - 12 * 3600) // 60

    def onsets_nm(self, nights) -> dict:
        return {n: self.nm(n, self.onset_s[n]) for n in nights if n in self.onset_s}

    def wakes_nm(self, nights) -> dict:
        return {n: self.nm(n, self.wake_s[n]) for n in nights if n in self.wake_s}


# ---------------------------------------------------------------- P6: scripted people against a zone (P6 spec 11.1)
PROBE_DEPTH_M = 0.5                  # a counted crossing goes this far past the area edge (spec 11.1, ``probe_depth_cm``)
PROBE_PEAK_M = 0.20                  # a counted crossing also reaches this far from the area edge (perpendicular): not a graze (PROPOSAL)
DEPTH_STEP = 5                       # the depth is read every 5th sample (50 ms)
SAMPLE_MS = 10                       # the truth path is sampled this finely, the crossing times are refined to 1 ms
FINAL_HOLD_MS = 900                  # ``framegen`` holds the last waypoint of a track for 10 frames: present until +0.9 s


def _rect(c: int) -> tuple:
    i, j = c % NX, c // NX
    x0 = W.GX0 + i * W.CELL
    y0 = W.GY0 + j * W.CELL
    return x0, y0, x0 + W.CELL, y0 + W.CELL


def _rect_dist(x: float, y: float, r: tuple) -> float:
    dx = max(r[0] - x, 0.0, x - r[2])
    dy = max(r[1] - y, 0.0, y - r[3])
    return math.hypot(dx, dy)


class ZoneArea:
    """The zone's test region: the 0.2 m cell mask grown by ``margin_cm`` (P6 spec 4.3). A point is inside when it lies in the
    mask or within the margin of the nearest mask cell rectangle; a negative margin shrinks the area (inside the mask and
    farther than ``|margin|`` from every cell that is not in it)."""

    def __init__(self, cells, margin_cm: int = 25):
        self.cells = frozenset(cells)
        self.m = margin_cm / 100.0
        self.rad = int(math.ceil(abs(self.m) / W.CELL)) + 1
        rects = [_rect(c) for c in self.cells]
        grow = max(self.m, 0.0) + 1e-9
        if rects:
            self.bbox = (min(r[0] for r in rects) - grow, min(r[1] for r in rects) - grow,
                         max(r[2] for r in rects) + grow, max(r[3] for r in rects) + grow)
        else:
            self.bbox = (1.0, 1.0, 0.0, 0.0)

    def in_bbox(self, x: float, y: float) -> bool:
        b = self.bbox
        return b[0] <= x <= b[2] and b[1] <= y <= b[3]

    def _near(self, x: float, y: float, want_in_mask: bool) -> float:
        """Distance to the nearest cell rectangle that is (or is not) in the mask, searched within ``rad`` cells; cells
        outside the grid count as not in the mask."""
        i0 = int(math.floor((x - W.GX0) / W.CELL))
        j0 = int(math.floor((y - W.GY0) / W.CELL))
        best = 1e9
        for dj in range(-self.rad, self.rad + 1):
            for di in range(-self.rad, self.rad + 1):
                i, j = i0 + di, j0 + dj
                if 0 <= i < NX and 0 <= j < NY:
                    c = j * NX + i
                    if (c in self.cells) != want_in_mask:
                        continue
                    r = _rect(c)
                elif want_in_mask:
                    continue
                else:
                    x0 = W.GX0 + i * W.CELL
                    y0 = W.GY0 + j * W.CELL
                    r = (x0, y0, x0 + W.CELL, y0 + W.CELL)
                d = _rect_dist(x, y, r)
                if d < best:
                    best = d
        return best

    def inside(self, x: float, y: float) -> bool:
        if not self.in_bbox(x, y):
            return False
        c = W.cell_of(x, y)
        if self.m >= 0:
            if c in self.cells:
                return True
            return self._near(x, y, True) <= self.m
        return c in self.cells and self._near(x, y, False) > -self.m

    def _mask_dist(self, x: float, y: float) -> float:
        """Distance to the nearest mask cell rectangle (searched within ``rad`` cells, else over the whole mask)."""
        d = self._near(x, y, True)
        if d > 1e8:
            d = min((_rect_dist(x, y, _rect(c)) for c in self.cells), default=9.0)
        return d

    def signed_distance(self, x: float, y: float) -> float:
        """Metres to the area edge: positive inside, negative outside (non-convex corners are approximated)."""
        c = W.cell_of(x, y)
        if self.m >= 0:
            if c in self.cells:
                return self.m + min(self._near(x, y, False), 5.0)
            return self.m - self._mask_dist(x, y)
        if c not in self.cells:
            return -self._mask_dist(x, y)
        return min(self._near(x, y, False), 5.0) + self.m


def _pieces(points: list) -> list:
    """Linear pieces ``(t0_ms, x0, y0, t1_ms, x1, y1)`` of a track's waypoints plus the final hold of the last waypoint."""
    out = [(a[0] * 1000, a[1], a[2], b[0] * 1000, b[1], b[2]) for a, b in zip(points, points[1:])]
    t, x, y = points[-1]
    out.append((t * 1000, x, y, t * 1000 + FINAL_HOLD_MS, x, y))
    return out


def track_position(points: list, t_ms: float):
    """The truth position (x, y) at sim ms ``t_ms``, or None when the person is not there."""
    if not points or t_ms < points[0][0] * 1000 or t_ms > points[-1][0] * 1000 + FINAL_HOLD_MS:
        return None
    for p in _pieces(points):
        if p[0] <= t_ms <= p[3]:
            f = (t_ms - p[0]) / (p[3] - p[0]) if p[3] > p[0] else 0.0
            return p[1] + (p[4] - p[1]) * f, p[2] + (p[5] - p[2]) * f
    return None


def track_crossings(points: list, area: ZoneArea) -> list:
    """The truth crossings of one track with a zone area (P6 spec 11.1): a list of dicts, one per stretch inside, in time
    order: ``t_in``, ``t_out`` (sim ms of the area edge, the path sampled every ``SAMPLE_MS`` and refined to 1 ms),
    ``born_inside`` / ``dies_inside`` (the track begins / ends inside), and the path lengths in metres: ``before_m`` (outside,
    before ``t_in``), ``inside_m`` and ``after_m`` (outside, after ``t_out``). A track that never meets the area gives []."""
    if not points or not area.cells:
        return []
    pieces = _pieces(points)
    bx0, by0, bx1, by1 = area.bbox
    if all(max(p[1], p[4]) < bx0 or min(p[1], p[4]) > bx1 or max(p[2], p[5]) < by0 or min(p[2], p[5]) > by1
           for p in pieces):
        return []
    samples = []                           # (t_ms, x, y, cumulative path length)
    s = 0.0
    prev = None
    for p in pieces:
        t = p[0]
        step = SAMPLE_MS if (p[4] != p[1] or p[5] != p[2]) else max(1, p[3] - p[0])      # a stationary piece: its two ends only
        while True:
            f = (t - p[0]) / (p[3] - p[0]) if p[3] > p[0] else 0.0
            x, y = p[1] + (p[4] - p[1]) * f, p[2] + (p[5] - p[2]) * f
            last_of_piece = t >= p[3]
            if not last_of_piece or p is pieces[-1]:
                if prev is not None:
                    s += math.hypot(x - prev[0], y - prev[1])
                samples.append((t, x, y, s))
                prev = (x, y)
            if last_of_piece:
                break
            t = min(t + step, p[3])
    flags = [area.inside(x, y) for _, x, y, _ in samples]
    if not any(flags):
        return []

    def refine(k0: int, k1: int, want: bool) -> tuple:
        """The time and path length where the flag between samples k0 and k1 changes to ``want``."""
        a, b = samples[k0], samples[k1]
        lo, hi = a[0], b[0]
        for _ in range(12):
            mid = (lo + hi) / 2.0
            f = (mid - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
            x, y = a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f
            if area.inside(x, y) == want:
                hi = mid
            else:
                lo = mid
        f = (hi - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
        return hi, a[3] + (b[3] - a[3]) * f

    total = samples[-1][3]
    out = []
    cur = None
    k_in = 0

    def depth(k0: int, k1: int) -> float:
        """The greatest distance to the area edge (perpendicular) the path reaches between samples k0 and k1 (every 50 ms)."""
        return max((area.signed_distance(samples[k][1], samples[k][2]) for k in range(k0, k1, DEPTH_STEP)), default=0.0)

    for k, fl in enumerate(flags):
        if fl and cur is None:
            k_in = k
            if k == 0:
                cur = {"t_in": samples[0][0], "s_in": 0.0, "born_inside": True}
            else:
                t, sl = refine(k - 1, k, True)
                cur = {"t_in": t, "s_in": sl, "born_inside": False}
        elif not fl and cur is not None:
            t, sl = refine(k - 1, k, False)
            out.append(dict(cur, t_out=t, s_out=sl, dies_inside=False, depth_m=depth(k_in, k)))
            cur = None
    if cur is not None:
        out.append(dict(cur, t_out=samples[-1][0], s_out=total, dies_inside=True, depth_m=depth(k_in, len(samples))))
    for c in out:
        c["depth_m"] = round(c["depth_m"], 3)
        c["before_m"] = round(c.pop("s_in"), 4)
        c["inside_m"] = round(c["s_out"] - c["before_m"], 4)
        c["after_m"] = round(total - c.pop("s_out"), 4)
        c["t_in"] = round(c["t_in"], 1)
        c["t_out"] = round(c["t_out"], 1)
    return out


def counted_enter(c: dict, depth_m: float = PROBE_DEPTH_M) -> bool:
    """A counted enter crossing: the path comes from outside and goes at least ``depth_m`` inside the area edge (along the path) and
    at least ``PROBE_PEAK_M`` deep (a path that skims the edge is a grazing crossing: noise decides it, not the rule)."""
    return (not c["born_inside"]) and c["inside_m"] >= depth_m and c.get("depth_m", 1.0) >= PROBE_PEAK_M


def counted_leave(c: dict, depth_m: float = PROBE_DEPTH_M) -> bool:
    """A counted leave crossing: the path was at least ``depth_m`` inside (and ``PROBE_PEAK_M`` deep) and goes at least ``depth_m``
    outside."""
    return ((not c["dies_inside"]) and c["after_m"] >= depth_m and c["inside_m"] >= depth_m
            and c.get("depth_m", 1.0) >= PROBE_PEAK_M)


class ScriptedTruth:
    """The truth of the scripted people of a run (``replay.p6_people.ScriptedPeople.log``) against zone areas, computed after
    the run from the learned cells and the margin in force (P6 spec 11.1: ``Truth.zone_crossings``)."""

    def __init__(self, people: list):
        from .p6_people import track_points
        self.people = people
        self._tracks = []                  # (person, [(t_s, x, y), ...], bbox)
        for p in people:
            for tr in p["tracks"]:
                pts = track_points(tr)
                if pts:
                    xs = [q[1] for q in pts]
                    ys = [q[2] for q in pts]
                    self._tracks.append((p, pts, (min(xs), min(ys), max(xs), max(ys))))
        self._cache = {}

    def zone_crossings(self, cells, margin_cm: int = 25) -> list:
        """Every crossing of every scripted track with the zone area: the dicts of ``track_crossings`` plus ``person`` (the
        record's id), ``kind``, ``spot``, ``speed``, ``angle``, ``dir`` and ``t0_ms`` / ``t1_ms`` (the track's first and
        last ms)."""
        key = (frozenset(cells), margin_cm)
        if key in self._cache:
            return self._cache[key]
        area = ZoneArea(cells, margin_cm)
        bx0, by0, bx1, by1 = area.bbox
        out = []
        for p, pts, bb in self._tracks:
            if bb[2] < bx0 or bb[0] > bx1 or bb[3] < by0 or bb[1] > by1:
                continue
            for c in track_crossings(pts, area):
                c.update(person=p["id"], kind=p["kind"], spot=p.get("spot"), speed=p.get("speed"), angle=p.get("angle"),
                         dir=p.get("dir"), t0_ms=pts[0][0] * 1000, t1_ms=pts[-1][0] * 1000 + FINAL_HOLD_MS)
                out.append(c)
        out.sort(key=lambda c: (c["t_in"], c["person"]))
        self._cache[key] = out
        return out

    def zone_inside_intervals(self, cells, margin_cm: int = 25) -> list:
        """``[(t_in_ms, t_out_ms, person_id), ...]`` of every person inside the zone area, in time order."""
        return sorted((c["t_in"], c["t_out"], c["person"]) for c in self.zone_crossings(cells, margin_cm))

    def inside_count(self, cells, margin_cm: int, t_ms: float) -> int:
        """The number of scripted people inside the zone area at sim ms ``t_ms``."""
        return sum(1 for a, b, _ in self.zone_inside_intervals(cells, margin_cm) if a <= t_ms <= b)

    @staticmethod
    def stayer_signed_distance(person: dict, cells, margin_cm: int = 25) -> Optional[float]:
        """The signed distance (m) to the area edge of a stationary person's position (P6-2 edge flicker)."""
        try:
            seg = person["tracks"][0][0]
        except (KeyError, IndexError):
            return None
        return ZoneArea(cells, margin_cm).signed_distance(seg[1], seg[2])
