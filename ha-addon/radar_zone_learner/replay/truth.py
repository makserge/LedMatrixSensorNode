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
