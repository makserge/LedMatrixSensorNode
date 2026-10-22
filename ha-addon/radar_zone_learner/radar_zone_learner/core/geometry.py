"""Cell-set geometry (P3 spec 7.8.8): outline polygon, Douglas-Peucker, IoU, overlap coefficient."""
from __future__ import annotations

import math
from typing import Iterable

# Unit directions (dx, dy): 0 east, 1 north, 2 west, 3 south.
_DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1))


def iou(a, b) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def overlap_coef(a, b) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def largest_component(cells: Iterable[int], geom) -> list:
    """The largest 4-connected component (ties: the one with the smallest cell), sorted."""
    comps = geom.components4(cells)
    if not comps:
        return []
    return max(comps, key=lambda c: (len(c), -c[0]))


def _loops(cells: set, nx: int, ny: int) -> list:
    """Closed boundary loops along cell edges, interior on the left (counter-clockwise outside,
    clockwise around holes). Vertices are integer lattice corners (i, j). Every directed boundary
    edge has exactly one successor; where two loops touch at a corner, each loop keeps to its own
    exterior region (it turns right there)."""
    def inside(i, j):
        return 0 <= i < nx and 0 <= j < ny and (j * nx + i) in cells

    edges = set()
    for c in cells:
        i, j = c % nx, c // nx
        if not inside(i, j - 1):
            edges.add((i, j, 0))              # bottom side, going east
        if not inside(i + 1, j):
            edges.add((i + 1, j, 1))          # right side, going north
        if not inside(i, j + 1):
            edges.add((i + 1, j + 1, 2))      # top side, going west
        if not inside(i - 1, j):
            edges.add((i, j + 1, 3))          # left side, going south
    loops = []
    while edges:
        e0 = min(edges)
        e = e0
        loop = []
        while True:
            edges.discard(e)
            vi, vj, d = e
            loop.append((vi, vj))
            wi, wj = vi + _DIRS[d][0], vj + _DIRS[d][1]
            if d == 0:
                left, right = inside(wi, wj), inside(wi, wj - 1)
            elif d == 1:
                left, right = inside(wi - 1, wj), inside(wi, wj)
            elif d == 2:
                left, right = inside(wi - 1, wj - 1), inside(wi - 1, wj)
            else:
                left, right = inside(wi, wj - 1), inside(wi - 1, wj - 1)
            if left and not right:
                nd = d                        # straight on
            elif not left and not right:
                nd = (d + 1) % 4              # convex corner: turn left
            else:
                nd = (d - 1) % 4              # concave corner or touching loops: turn right
            e = (wi, wj, nd)
            if e == e0 or e not in edges:
                break
        loops.append(loop)
    return loops


def _area2(poly: list) -> float:
    s = 0.0
    n = len(poly)
    for k in range(n):
        x1, y1 = poly[k]
        x2, y2 = poly[(k + 1) % n]
        s += x1 * y2 - x2 * y1
    return s


def _drop_collinear(poly: list) -> list:
    n = len(poly)
    if n < 4:
        return poly
    out = []
    for k in range(n):
        a = poly[k - 1]
        b = poly[k]
        c = poly[(k + 1) % n]
        if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) != 0:
            out.append(b)
    return out


def trace_outline(cells: Iterable[int], geom) -> list:
    """Outer boundary of the largest 4-connected component as corner coordinates in metres,
    counter-clockwise, collinear corners removed."""
    comp = largest_component(cells, geom)
    if not comp:
        return []
    loops = _loops(set(comp), geom.nx, geom.ny)
    best = max(loops, key=lambda lp: (_area2(lp), -len(lp)))
    poly = _drop_collinear(best)
    return [(geom.x0 + i * geom.cell, geom.y0 + j * geom.cell) for i, j in poly]


def _seg_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _dp_open(pts: list, tol: float) -> list:
    if len(pts) <= 2:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        best, idx = -1.0, -1
        for k in range(a + 1, b):
            d = _seg_dist(pts[k], pts[a], pts[b])
            if d > best:
                best, idx = d, k
        if idx >= 0 and best > tol:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [q for q, kp in zip(pts, keep) if kp]


def dp_closed(poly: list, tol: float) -> list:
    """Douglas-Peucker on a closed polygon: split at vertex 0 and the vertex farthest from it."""
    n = len(poly)
    if n <= 3:
        return list(poly)
    p0 = poly[0]
    far = max(range(1, n), key=lambda k: (math.hypot(poly[k][0] - p0[0], poly[k][1] - p0[1]), -k))
    a = _dp_open(poly[:far + 1], tol)
    b = _dp_open(poly[far:] + [poly[0]], tol)
    return a[:-1] + b[:-1]


def simplify(poly: list, tol: float, max_vertices: int) -> list:
    """DP with tol, multiplied by 1.5 until <= max_vertices; never below 3 vertices."""
    if len(poly) <= max_vertices:
        return list(poly)
    cur = dp_closed(poly, tol)
    while len(cur) > max_vertices:
        tol *= 1.5
        nxt = dp_closed(poly, tol)
        if len(nxt) < 3:
            break
        cur = nxt
    return cur


def polygon_cm(cells: Iterable[int], geom, tol: float = 0.1, max_vertices: int = 12) -> list:
    """Suggestion polygon: [[x_cm, y_cm], ...], counter-clockwise, <= max_vertices vertices."""
    poly = trace_outline(cells, geom)
    if not poly:
        return []
    poly = simplify(poly, tol, max_vertices)
    return [[int(math.floor(x * 100 + 0.5)), int(math.floor(y * 100 + 0.5))] for x, y in poly]


def bbox_cm(cells: Iterable[int], geom) -> list:
    """[x_min, y_min, x_max, y_max] of the cells' outer edges, in cm."""
    cs = list(cells)
    if not cs:
        return [0, 0, 0, 0]
    i0 = min(c % geom.nx for c in cs)
    i1 = max(c % geom.nx for c in cs) + 1
    j0 = min(c // geom.nx for c in cs)
    j1 = max(c // geom.nx for c in cs) + 1

    def f(v):
        return int(math.floor(v * 100 + 0.5))

    return [f(geom.x0 + i0 * geom.cell), f(geom.y0 + j0 * geom.cell),
            f(geom.x0 + i1 * geom.cell), f(geom.y0 + j1 * geom.cell)]
