"""Grid geometry, splat, blur, decay, rate, peak regions and cell-set codecs (P3 spec 7.6, 7.8.5).

The grid is the simulator's: 0.2 m cells over x -7..+7 m and y 0..8 m, cell index j * nx + i.
Splat, blur and region growing are ports of the project's simulator (SIM:33-71, 555-579).
"""
from __future__ import annotations

import base64
import math
import sys
from array import array
from typing import Iterable, Optional

from .params import Params

GRID_NAMES = ("dwell_slow", "dwell_fast", "sessions", "walk", "birth_moving", "death_moving",
              "death_still", "ghost", "clutter")


class GridGeom:
    """Geometry derived from Params, with cached neighbour lists and the splat kernel."""

    __slots__ = ("nx", "ny", "nc", "cell", "x0", "y0", "kernel", "n4", "n8")

    def __init__(self, p: Params) -> None:
        self.nx = p.nx
        self.ny = p.ny
        self.nc = p.nx * p.ny
        self.cell = p.cell_m
        self.x0 = p.x0_m
        self.y0 = p.y0_m
        r = p.splat_radius_cells
        sig = p.splat_sigma_m
        ker = []
        for di in range(-r, r + 1):
            for dj in range(-r, r + 1):
                ker.append((di, dj, math.exp(-0.5 * ((di * self.cell) ** 2 + (dj * self.cell) ** 2) / sig ** 2)))
        ks = sum(k for _, _, k in ker)
        self.kernel = tuple((a, b, k / ks) for a, b, k in ker)
        nx, ny = self.nx, self.ny
        n4 = []
        n8 = []
        for c in range(self.nc):
            i, j = c % nx, c // nx
            a = []
            if i > 0:
                a.append(c - 1)
            if i < nx - 1:
                a.append(c + 1)
            if j > 0:
                a.append(c - nx)
            if j < ny - 1:
                a.append(c + nx)
            n4.append(tuple(a))
            b = []
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    if (di or dj) and 0 <= i + di < nx and 0 <= j + dj < ny:
                        b.append((j + dj) * nx + i + di)
            n8.append(tuple(b))
        self.n4 = tuple(n4)
        self.n8 = tuple(n8)

    def cell_of(self, x: float, y: float) -> int:
        i = int(math.floor((x - self.x0) / self.cell))
        j = int(math.floor((y - self.y0) / self.cell))
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return -1

    def cell_xy(self, c: int) -> tuple:
        return (self.x0 + (c % self.nx + 0.5) * self.cell, self.y0 + (c // self.nx + 0.5) * self.cell)

    def splat(self, g, x: float, y: float, w: float) -> None:
        i0 = int(math.floor((x - self.x0) / self.cell))
        j0 = int(math.floor((y - self.y0) / self.cell))
        nx, ny = self.nx, self.ny
        for di, dj, k in self.kernel:
            i = i0 + di
            j = j0 + dj
            if 0 <= i < nx and 0 <= j < ny:
                g[j * nx + i] += w * k

    def blur(self, g) -> list:
        """3x3 binomial blur, each cell divided by the weights inside the grid (SIM:56-71)."""
        nx, ny = self.nx, self.ny
        out = [0.0] * self.nc
        w = (1, 2, 1)
        for j in range(ny):
            for i in range(nx):
                s = 0.0
                ws = 0
                for dj in (-1, 0, 1):
                    jj = j + dj
                    if not 0 <= jj < ny:
                        continue
                    for di in (-1, 0, 1):
                        ii = i + di
                        if not 0 <= ii < nx:
                            continue
                        k = w[di + 1] * w[dj + 1]
                        s += g[jj * nx + ii] * k
                        ws += k
                out[j * nx + i] = s / ws
        return out

    def dilate(self, cells: Iterable[int]) -> set:
        out = set(cells)
        n8 = self.n8
        for c in list(out):
            out.update(n8[c])
        return out

    def dilate4(self, cells: Iterable[int], times: int) -> set:
        m = set(cells)
        n4 = self.n4
        for _ in range(times):
            add = set()
            for c in m:
                add.update(n4[c])
            m |= add
        return m

    def regions(self, rate, minv: float, frac: float, mono: float, owner: Optional[dict] = None):
        """Peak regions (SIM:555-579): maxima sorted by (-value, cell), grown over 4-neighbours by a
        LIFO stack down to frac * peak and never uphill by more than the factor mono.
        Returns (regions, owner): regions is a list of (peak, cells list in growth order)."""
        owner = {} if owner is None else owner
        n4, n8 = self.n4, self.n8
        mx = []
        for c in range(self.nc):
            v = rate[c]
            if v < minv:
                continue
            ok = True
            for n in n8[c]:
                if rate[n] > v:
                    ok = False
                    break
            if ok:
                mx.append((-v, c))
        mx.sort()
        regs = []
        for negpk, c0 in mx:
            if c0 in owner:
                continue
            pk = -negpk
            rid = len(regs)
            cells = [c0]
            owner[c0] = rid
            st = [c0]
            lo = frac * pk
            while st:
                c = st.pop()
                hi = rate[c] * mono
                for n in n4[c]:
                    if n not in owner:
                        rn = rate[n]
                        if rn >= lo and rn <= hi:
                            owner[n] = rid
                            cells.append(n)
                            st.append(n)
            regs.append((pk, cells))
        return regs, owner

    def components4(self, cells: Iterable[int]) -> list:
        """4-connected components, each a sorted list; components sorted by their smallest cell."""
        todo = set(cells)
        n4 = self.n4
        comps = []
        for c in sorted(todo):
            if c not in todo:
                continue
            todo.discard(c)
            comp = [c]
            st = [c]
            while st:
                x = st.pop()
                for n in n4[x]:
                    if n in todo:
                        todo.discard(n)
                        comp.append(n)
                        st.append(n)
            comps.append(sorted(comp))
        return comps

    def cells_within(self, x: float, y: float, radius: float) -> frozenset:
        """Cells whose centre lies within radius of (x, y)."""
        out = []
        r_cells = int(math.ceil(radius / self.cell)) + 1
        i0 = int(math.floor((x - self.x0) / self.cell))
        j0 = int(math.floor((y - self.y0) / self.cell))
        r2 = radius * radius
        for j in range(j0 - r_cells, j0 + r_cells + 1):
            if not 0 <= j < self.ny:
                continue
            for i in range(i0 - r_cells, i0 + r_cells + 1):
                if not 0 <= i < self.nx:
                    continue
                cx = self.x0 + (i + 0.5) * self.cell
                cy = self.y0 + (j + 0.5) * self.cell
                if (cx - x) ** 2 + (cy - y) ** 2 <= r2:
                    out.append(j * self.nx + i)
        return frozenset(out)


def rate(g, eff: float) -> list:
    """Per 24 covered hours (SIM:534-536)."""
    k = 24.0 / max(eff, 1e-9)
    return [v * k for v in g]


class Grids:
    """The nine decayed float32 grids, the DAYS bitmask and the effective covered hours."""

    def __init__(self, p: Params, geom: GridGeom) -> None:
        self.p = p
        self.geom = geom
        nc = geom.nc
        self.g = {name: array("f", bytes(4 * nc)) for name in GRID_NAMES}
        self.days_bits = array("H", bytes(2 * nc))
        self.days_base: Optional[int] = None      # date ordinal
        self.eff_s = 0.0
        self.eff_f = 0.0

    def __getitem__(self, name: str):
        return self.g[name]

    def splat(self, name: str, x: float, y: float, w: float) -> None:
        self.geom.splat(self.g[name], x, y, w)

    def add_walk(self, x: float, y: float) -> None:
        c = self.geom.cell_of(x, y)
        if c >= 0:
            self.g["walk"][c] += 1.0

    def decay(self, hours: float) -> float:
        """Per covered hour (SIM:527-532). Returns the slow factor fs (also used by COVIS)."""
        p = self.p
        fs = 0.5 ** (hours / p.hl_slow_h)
        ff = 0.5 ** (hours / p.hl_fast_h)
        for name in GRID_NAMES:
            g = self.g[name]
            f = ff if name == "dwell_fast" else fs
            if f != 1.0:
                g[:] = array("f", [v * f for v in g])
        self.eff_s = self.eff_s * fs + hours
        self.eff_f = self.eff_f * ff + hours
        return fs

    def rate(self, name: str, slow: bool = True) -> list:
        return rate(self.g[name], self.eff_s if slow else self.eff_f)

    # ------------------------------------------------------------ DAYS bitmask (diagnostic in P3)
    def _mask(self) -> int:
        return (1 << min(16, self.p.days_bits_days)) - 1

    def days_advance(self, date_ord: int) -> None:
        """Move days_base forward to date_ord, shifting every cell's bits."""
        if self.days_base is None:
            self.days_base = date_ord
            return
        k = date_ord - self.days_base
        if k <= 0:
            return
        m = self._mask()
        if k >= 16:
            self.days_bits[:] = array("H", bytes(2 * self.geom.nc))
        else:
            self.days_bits[:] = array("H", [(v << k) & m for v in self.days_bits])
        self.days_base = date_ord

    def days_mark(self, x: float, y: float, date_ord: int) -> None:
        c = self.geom.cell_of(x, y)
        if c < 0:
            return
        self.days_advance(date_ord)
        i = self.days_base - date_ord
        if 0 <= i < min(16, self.p.days_bits_days):
            self.days_bits[c] |= (1 << i)


# ---------------------------------------------------------------- codecs
def _to_le(arr: array) -> bytes:
    if sys.byteorder == "little":
        return arr.tobytes()
    a = array(arr.typecode, arr)
    a.byteswap()
    return a.tobytes()


def _from_le(typecode: str, data: bytes) -> array:
    a = array(typecode)
    a.frombytes(data)
    if sys.byteorder != "little":
        a.byteswap()
    return a


def encode_array(arr: array) -> str:
    return base64.b64encode(_to_le(arr)).decode("ascii")


def decode_array(typecode: str, text: str, count: int) -> array:
    data = base64.b64decode(text.encode("ascii"), validate=True)
    size = array(typecode).itemsize
    if len(data) != size * count:
        raise ValueError("wrong length")
    return _from_le(typecode, data)


def rle_encode(cells: Iterable[int]) -> list:
    """Sorted cell indices -> [[start, length], ...]."""
    out = []
    for c in sorted(cells):
        if out and out[-1][0] + out[-1][1] == c:
            out[-1][1] += 1
        else:
            out.append([c, 1])
    return out


def rle_decode(runs) -> frozenset:
    out = []
    for start, length in runs:
        out.extend(range(start, start + length))
    return frozenset(out)
