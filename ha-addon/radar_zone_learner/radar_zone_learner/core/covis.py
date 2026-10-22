"""Co-visibility of stay fragments on a 0.4 m grid (P3 spec 7.7; OA 7.3 step 2; CD learning_pipeline (g)).

For every fragment F, the overlap seconds with each concurrent fragment G of another track go into a
sparse, decayed pair matrix; fragsec holds each cell's fragment seconds. The raw fragments are kept in
RAM for frag_ram_h hours only (OA 10).

Every second of a fragment is credited at most once per partner cell (the simulator takes the union of
the source's intervals, SIM:762-769): when two concurrent tracks of the denser place sit in one 0.4 m
cell, the C fragment they both overlap counts once, so covis cannot exceed 1 that way. Fragments keep
the intervals already credited per partner cell for as long as they are in the RAM list. Two source
tracks in different cells still credit the same second once each; a pair matrix cannot do better, so
covis() is clamped to 1 (a mirror of a two-seat sofa reads 1.0x, a real place never above 0.4 in the replay).
"""
from __future__ import annotations

import base64
import math
import struct
import sys
from array import array
from bisect import bisect_right, insort
from typing import Iterable

from .params import Params


def _credit(cov: dict, cell: int, s: float, e: float) -> float:
    """Seconds of [s, e] that cov[cell] (sorted, disjoint intervals) does not hold yet; adds [s, e] to it."""
    iv = cov.get(cell)
    if iv is None:
        cov[cell] = [(s, e)]
        return e - s
    covered = 0.0
    out = []
    ns, ne = s, e
    for a, b in iv:
        lo = a if a > s else s
        hi = b if b < e else e
        if hi > lo:
            covered += hi - lo
        if b < ns or a > ne:
            out.append((a, b))
        else:
            if a < ns:
                ns = a
            if b > ne:
                ne = b
    out.append((ns, ne))
    out.sort()
    cov[cell] = out
    return (e - s) - covered


class Covis:
    def __init__(self, p: Params) -> None:
        self.p = p
        self.cell = p.covis_cell_m
        self.x0 = p.x0_m
        self.y0 = p.y0_m
        self.nx4 = int(round(p.nx * p.cell_m / p.covis_cell_m))
        self.ny4 = int(round(p.ny * p.cell_m / p.covis_cell_m))
        self.nc4 = self.nx4 * self.ny4
        self.fragsec = [0.0] * self.nc4
        self.pairs: dict = {}
        self.recent: list = []        # (t1, seq, t0, c4, track_id, credited), ascending t1
        self._seq = 0
        self._newest = -math.inf

    def c4(self, x: float, y: float) -> int:
        i = int(math.floor((x - self.x0) / self.cell))
        j = int(math.floor((y - self.y0) / self.cell))
        if 0 <= i < self.nx4 and 0 <= j < self.ny4:
            return j * self.nx4 + i
        return -1

    def add(self, f) -> None:
        """Add one fragment (any object with t0, t1, cx, cy, track_id)."""
        if f.t1 > self._newest:
            self._newest = f.t1
        lo = self._newest - self.p.frag_ram_h * 3600.0
        rec = self.recent
        if rec and rec[0][0] < lo:
            k = bisect_right(rec, (lo, -1))
            del rec[:k]
        a = self.c4(f.cx, f.cy)
        if a < 0:
            return
        nc4 = self.nc4
        pairs = self.pairs
        mine: dict = {}               # partner cell -> intervals of F already credited
        i = bisect_right(rec, (f.t0, math.inf))
        while i < len(rec):
            g1, _, g0, b, gtid, theirs = rec[i]
            i += 1
            if gtid == f.track_id:
                continue
            s = f.t0 if f.t0 > g0 else g0
            e = f.t1 if f.t1 < g1 else g1
            if e > s:
                w1 = _credit(mine, b, s, e)        # seconds of F in cell a seen with a fragment in cell b
                w2 = _credit(theirs, a, s, e)      # seconds of G in cell b seen with a fragment in cell a
                if w1 > 0:
                    k1 = a * nc4 + b
                    pairs[k1] = pairs.get(k1, 0.0) + w1
                if w2 > 0:
                    k2 = b * nc4 + a
                    pairs[k2] = pairs.get(k2, 0.0) + w2
        self.fragsec[a] += f.t1 - f.t0
        self._seq += 1
        insort(rec, (f.t1, self._seq, f.t0, a, f.track_id, mine))

    def decay(self, fs: float) -> None:
        p = self.p
        self.fragsec = [v * fs for v in self.fragsec]
        pr = p.covis_prune_s
        self.pairs = {k: v * fs for k, v in self.pairs.items() if v * fs >= pr}
        if len(self.pairs) > p.covis_cap:
            items = sorted(self.pairs.items(), key=lambda kv: (kv[1], kv[0]))
            drop = len(items) - p.covis_cap
            self.pairs = dict(sorted(items[drop:]))
        else:
            self.pairs = dict(sorted(self.pairs.items()))

    # ------------------------------------------------------------ query (spec 7.7)
    def cells4(self, cells: Iterable[int], geom) -> frozenset:
        """0.4 m cells of the centres of the given 0.2 m cells."""
        out = set()
        for c in cells:
            x, y = geom.cell_xy(c)
            k = self.c4(x, y)
            if k >= 0:
                out.add(k)
        return frozenset(out)

    def frag_seconds(self, A: Iterable[int]) -> float:
        fsec = self.fragsec
        return sum(fsec[a] for a in sorted(A))

    def covis(self, A: frozenset, B: frozenset) -> float:
        """covis(C | C') with A = cells4(C.cells), B = cells4(C'.cells): the share of C's fragment seconds
        that were concurrent with a fragment of C' outside C's cells.

        Simulator rule (SIM:762-769): a fragment in a cell of both sets belongs to C, and the source's
        fragments are those that are not C's. Integration fix of spec 7.7, which built A and B from the
        0.2 m-dilated cells: rounding a dilated set to 0.4 m reaches up to 0.6 m past C, which took in the
        source's own fragments for a mirror 0.9 m from its seat and kept covis at 0.2-0.6 where the
        simulator measures 1.0. A is C's own cells; the 0.4 m rounding already pads them by up to 0.2 m,
        as the simulator's 0.2 m dilation does."""
        a_cells = sorted(A)
        fa = self.frag_seconds(a_cells)
        if fa <= 0:
            return 0.0
        nc4 = self.nc4
        pairs = self.pairs
        b_only = sorted(B - A)
        s = 0.0
        for a in a_cells:
            base = a * nc4
            for b in b_only:
                v = pairs.get(base + b)
                if v is not None:
                    s += v
        return min(1.0, s / fa)

    # ------------------------------------------------------------ codec (spec 8.1)
    def export(self) -> dict:
        fs = array("f", self.fragsec)
        if sys.byteorder != "little":
            fs.byteswap()
        buf = bytearray()
        for k in sorted(self.pairs):
            buf += struct.pack("<If", k, self.pairs[k])
        return {"enc": "pairs-u32le-f32le-b64",
                "fragsec": base64.b64encode(fs.tobytes()).decode("ascii"),
                "pairs": base64.b64encode(bytes(buf)).decode("ascii")}

    def load(self, d: dict) -> None:
        raw = base64.b64decode(d["fragsec"].encode("ascii"), validate=True)
        if len(raw) != 4 * self.nc4:
            raise ValueError("fragsec length")
        fs = array("f")
        fs.frombytes(raw)
        if sys.byteorder != "little":
            fs.byteswap()
        vals = list(fs)
        if not all(math.isfinite(v) for v in vals):
            raise ValueError("fragsec not finite")
        raw = base64.b64decode(d["pairs"].encode("ascii"), validate=True)
        if len(raw) % 8:
            raise ValueError("pairs length")
        pairs = {}
        lim = self.nc4 * self.nc4
        for k, v in struct.iter_unpack("<If", raw):
            if k >= lim or not math.isfinite(v):
                raise ValueError("pair entry")
            pairs[k] = v
        self.fragsec = vals
        self.pairs = pairs
