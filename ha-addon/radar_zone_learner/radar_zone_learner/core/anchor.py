"""``typical_hours`` and ``direction``: the time anchor and the direction words of the item texts (P5 spec 2.11, A.10).

Both are pure. ``typical_hours`` turns the 24-bin dwell minutes of a place (a profile statistic, never a stay list) into
whole-hour ranges; the shell decides whether those ranges reach any topic (only with 'Detailed review text').
"""
from __future__ import annotations


def typical_hours(minutes24, p5) -> tuple:
    """Whole-hour ranges ``("09-12", "13-17")`` in which a place is used mostly (spec 2.11).

    ``m = max(minutes24)``; below ``p5.anchor_min_minutes`` there is no anchor (``()``). Hour ``h`` is on when
    ``minutes24[h] >= max(anchor_min_minutes, anchor_frac * m)``. Consecutive on-hours form a range ``[a, b]`` (no wrap
    over midnight); the ``anchor_max_ranges`` ranges with the largest sum of minutes are kept, in clock order. The text
    is ``"%02d-%02d" % (a, b + 1)`` (the end hour is exclusive, 24 is allowed)."""
    if not minutes24:
        return ()
    vals = [float(v) for v in minutes24][:24]
    m = max(vals)
    if m < p5.anchor_min_minutes:
        return ()
    thr = max(p5.anchor_min_minutes, p5.anchor_frac * m)
    ranges = []                                   # (start hour, end hour inclusive, sum of minutes)
    cur = None
    for h, v in enumerate(vals):
        if v >= thr:
            if cur is None:
                cur = [h, h, v]
            else:
                cur[1] = h
                cur[2] += v
        elif cur is not None:
            ranges.append(tuple(cur))
            cur = None
    if cur is not None:
        ranges.append(tuple(cur))
    keep = sorted(ranges, key=lambda r: (-r[2], r[0]))[:max(0, int(p5.anchor_max_ranges))]
    keep.sort(key=lambda r: r[0])
    return tuple("%02d-%02d" % (a, b + 1) for a, b, _ in keep)


def direction(dx_m: float, dy_m: float) -> str:
    """The direction of a displacement in words (spec 2.11): +x is the right of the map, +y is away from the radar."""
    if abs(dx_m) >= abs(dy_m):
        return "to the right" if dx_m >= 0 else "to the left"
    return "away from the radar" if dy_m >= 0 else "towards the radar"
