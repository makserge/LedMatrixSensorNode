"""Debug map raster (spec 10.6): ``DebugMapData`` -> width, height, palette, one palette index per pixel.

70 x 40 cells at ``SCALE`` 4 give 280 x 160 px. The image has y up: row 0 is the far edge (y = 8 m), the
radar sits at the bottom centre (x = 0, y = 0) with its +-60 degree field-of-view lines.
Drawing order: heat, envelope edge, failed candidates, suggestions (cell-edge outlines), door and label
markers (5 x 5 px), radar marker and field-of-view lines.
"""
from __future__ import annotations

import math

SCALE = 4

BACKGROUND = 0
HEAT_FIRST, HEAT_LEVELS = 1, 16          # indices 1..16
ENVELOPE_EDGE = 17
FAILED_CANDIDATE = 18
PLACE = 19
BED = 20
REFLECTION = 21
IGNORE = 22
DOOR = 23
LABEL_MANUAL = 24
LABEL_BED = 25
RADAR = 26

_HEAT_STOPS = ((24, 32, 110), (40, 90, 160), (30, 150, 140), (110, 200, 80), (255, 230, 40))


def _hex(text: str) -> tuple:
    return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))


def _heat_ramp() -> list:
    """16 colours from dark blue to yellow, piecewise linear through the stops."""
    out = []
    segments = len(_HEAT_STOPS) - 1
    for k in range(HEAT_LEVELS):
        pos = k / (HEAT_LEVELS - 1) * segments
        s = min(int(pos), segments - 1)
        f = pos - s
        a, b = _HEAT_STOPS[s], _HEAT_STOPS[s + 1]
        out.append(tuple(int(round(a[c] + (b[c] - a[c]) * f)) for c in range(3)))
    return out


PALETTE = ([_hex("#1e1e1e")] + _heat_ramp()
           + [_hex(c) for c in ("#4a4a4a", "#7a7a7a", "#00c8ff", "#ff6ec7", "#a070ff", "#ff8c00",
                                "#00ff7f", "#ffffff", "#ff6ec7", "#c0c0c0")])
assert len(PALETTE) == 27


class _Canvas:
    def __init__(self, data) -> None:
        self.nx, self.ny = int(data.nx), int(data.ny)
        self.cell = float(data.cell_m)
        self.x0, self.y0 = float(data.x0_m), float(data.y0_m)
        self.w, self.h = self.nx * SCALE, self.ny * SCALE
        self.px = bytearray(self.w * self.h)

    def set(self, col: int, row: int, idx: int) -> None:
        if 0 <= col < self.w and 0 <= row < self.h:
            self.px[row * self.w + col] = idx

    def fill_cell(self, i: int, j: int, idx: int) -> None:
        top = (self.ny - 1 - j) * SCALE
        left = i * SCALE
        run = bytes((idx,)) * SCALE
        for r in range(top, top + SCALE):
            start = r * self.w + left
            self.px[start:start + SCALE] = run

    def outline(self, cells, idx: int) -> None:
        """Draws the cell edges of ``cells`` that border a cell outside the set, inside the set's cells."""
        nx, ny, n = self.nx, self.ny, self.nx * self.ny
        cellset = {int(c) for c in cells if 0 <= int(c) < n}
        for c in sorted(cellset):
            i, j = c % nx, c // nx
            left, top = i * SCALE, (ny - 1 - j) * SCALE
            if i == 0 or (c - 1) not in cellset:
                for r in range(top, top + SCALE):
                    self.set(left, r, idx)
            if i == nx - 1 or (c + 1) not in cellset:
                for r in range(top, top + SCALE):
                    self.set(left + SCALE - 1, r, idx)
            if j == ny - 1 or (c + nx) not in cellset:
                for q in range(left, left + SCALE):
                    self.set(q, top, idx)
            if j == 0 or (c - nx) not in cellset:
                for q in range(left, left + SCALE):
                    self.set(q, top + SCALE - 1, idx)

    def to_px(self, x_m: float, y_m: float) -> tuple:
        col = math.floor((x_m - self.x0) * SCALE / self.cell + 1e-9)
        row = self.h - 1 - math.floor((y_m - self.y0) * SCALE / self.cell + 1e-9)
        return min(max(col, 0), self.w - 1), min(max(row, 0), self.h - 1)

    def marker(self, x_m: float, y_m: float, idx: int, half: int = 2) -> None:
        if not (math.isfinite(x_m) and math.isfinite(y_m)):
            return
        col, row = self.to_px(x_m, y_m)
        for r in range(row - half, row + half + 1):
            for q in range(col - half, col + half + 1):
                self.set(q, r, idx)

    def line(self, c0: int, r0: int, c1: int, r1: int, idx: int) -> None:
        """Bresenham line between two pixels, both ends included."""
        dc, dr = abs(c1 - c0), -abs(r1 - r0)
        sc = 1 if c0 < c1 else -1
        sr = 1 if r0 < r1 else -1
        err = dc + dr
        while True:
            self.set(c0, r0, idx)
            if c0 == c1 and r0 == r1:
                return
            e2 = 2 * err
            if e2 >= dr:
                err += dr
                c0 += sc
            if e2 <= dc:
                err += dc
                r0 += sr


def _suggestion_colour(s) -> int:
    if getattr(s, "state", "shadow") != "shadow":
        return FAILED_CANDIDATE
    kind_colour = {"dwell_zone": PLACE, "short_task_zone": PLACE, "reflection": REFLECTION,
                   "ignore_zone": IGNORE, "entry_zone": DOOR, "room_outline": ENVELOPE_EDGE}
    if s.type in ("dwell_zone", "short_task_zone") and s.kind == "bed":
        return BED
    return kind_colour.get(s.type, PLACE)


def render(data) -> tuple:
    """Returns ``(width, height, palette, pixels)`` for ``png.encode_png``."""
    cv = _Canvas(data)
    nx, ny = cv.nx, cv.ny

    # 1. heat: log1p(rate) / log1p(max) in 16 levels; below 1/16 of the peak (decayed remains) stays background
    rates = list(data.dwell_rate)[:nx * ny]
    finite = [r for r in rates if isinstance(r, (int, float)) and math.isfinite(r) and r > 0]
    peak = max(finite, default=0.0)
    if peak > 0:
        lpeak = math.log1p(peak)
        for c, r in enumerate(rates):
            if not (isinstance(r, (int, float)) and math.isfinite(r) and r > 0):
                continue
            level = min(HEAT_LEVELS, int(math.log1p(r) / lpeak * HEAT_LEVELS))
            if level >= 1:
                cv.fill_cell(c % nx, c // nx, HEAT_FIRST + level - 1)

    # 2. envelope edge
    cv.outline(data.envelope, ENVELOPE_EDGE)

    # 3. candidates that failed a gate (suggested ones are drawn as suggestions)
    for cand in sorted(data.candidates, key=lambda c: c.cid):
        if cand.outcome == "failed":
            cv.outline(cand.cells, FAILED_CANDIDATE)

    # 4. suggestions: stale ones first (grey), then the active ones on top
    suggestions = sorted(data.suggestions, key=lambda s: (getattr(s, "state", "shadow") == "shadow", s.id))
    for s in suggestions:
        cv.outline(s.cells, _suggestion_colour(s))

    # 5. markers: doors, then labels
    for door in data.doors:
        cv.marker(float(door[0]), float(door[1]), DOOR)
    for label in sorted(data.labels, key=lambda lb: lb.id):
        cv.marker(float(label.x_m), float(label.y_m), LABEL_BED if label.kind == "bed" else LABEL_MANUAL)

    # 6. radar at (0, 0) and the field-of-view lines to the edge of the mapped area
    rc, rr = cv.to_px(0.0, 0.0)
    fov = math.radians(float(data.fov_deg))
    x_min, x_max = cv.x0, cv.x0 + nx * cv.cell
    y_max = cv.y0 + ny * cv.cell
    for sign in (-1.0, 1.0):
        dx, dy = sign * math.sin(fov), math.cos(fov)
        limits = []
        if dx > 1e-12:
            limits.append(x_max / dx)
        elif dx < -1e-12:
            limits.append(x_min / dx)
        if dy > 1e-12:
            limits.append(y_max / dy)
        t = min((v for v in limits if v > 0), default=0.0)
        ec, er = cv.to_px(t * dx, t * dy)
        cv.line(rc, rr, ec, er, RADAR)
    cv.marker(0.0, 0.0, RADAR)

    return cv.w, cv.h, list(PALETTE), bytes(cv.px)
