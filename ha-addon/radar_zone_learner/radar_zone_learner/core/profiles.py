"""Time-of-day profiles and modes, the evidence rule, the OA 7.8 time-variant rule, the OA 7.4 freeze
predicate and bed naming (P3 spec 7.9; OA 7.3 step 6, 7.4, 7.8).

Modes follow the project simulator's tod_modes (SIM:626-641); the share rule (SIM:1040-1046) is
computed only as a diagnostic.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from .params import Params


class HourProfile:
    """Dwell minutes, stay counts (by start hour) and date sets per local hour."""

    __slots__ = ("minutes", "count", "dates")

    def __init__(self) -> None:
        self.minutes = [0.0] * 24
        self.count = [0] * 24
        self.dates = [set() for _ in range(24)]


def build_profiles(stays: list, time_model) -> tuple:
    """(weekday profile, weekend profile) of persisted stays (dicts with t0, dur, day).
    A stay is split across local hour boundaries from its rounded t0 (SIM:780-787); weekday =
    Mon-Fri by the stay's local date."""
    wd = HourProfile()
    we = HourProfile()
    for st in stays:
        d = date.fromisoformat(st["day"])
        prof = wd if d.weekday() < 5 else we
        lt = time_model.local(st["t0"] * 1000)
        h0 = lt.sec_of_day // 3600
        prof.count[h0] += 1
        prof.dates[h0].add(st["day"])
        rem = int(st["dur"])
        sod = lt.sec_of_day
        while rem > 0:
            h = sod // 3600
            seg = min(rem, 3600 - sod % 3600)
            prof.minutes[h] += seg / 60.0
            rem -= seg
            sod = (sod + seg) % 86400
    return wd, we


def modes(prof: list, smooth_h: int = 3, on_frac: float = 0.1) -> list:
    """Circular moving mean over smooth_h hours; hours >= on_frac * max are on; maximal runs of on
    hours on the 24 h circle are the modes. Returns [(start_h, end_h, mass, hours)] with end_h
    exclusive; all hours on -> one mode (0, 24)."""
    half = smooth_h // 2
    sm = [sum(prof[(h + k) % 24] for k in range(-half, smooth_h - half)) / smooth_h for h in range(24)]
    pk = max(sm) or 1
    on = [v >= on_frac * pk for v in sm]
    if all(on):
        return [(0, 24, sum(prof), list(range(24)))]
    start = next(h for h in range(24) if not on[h])
    runs = []
    cur = None
    for k in range(1, 25):
        h = (start + k) % 24
        if on[h] and cur is None:
            cur = h
        if (not on[h]) and cur is not None:
            runs.append((cur, h))
            cur = None
    out = []
    for a, b in runs:
        hrs = [(a + i) % 24 for i in range((b - a) % 24)]
        out.append((a, b, sum(prof[h] for h in hrs), hrs))
    return out


def qualifying(mds: list, prof: HourProfile, min_stays: int, min_days: int) -> list:
    """Modes with >= min_stays stays (by start hour) on >= min_days distinct dates (OA 7.3 step 6)."""
    out = []
    for m in mds:
        hrs = m[3]
        n = sum(prof.count[h] for h in hrs)
        days = set()
        for h in hrs:
            days |= prof.dates[h]
        if n >= min_stays and len(days) >= min_days:
            out.append((m[0], m[1], m[2], hrs, n, len(days)))
    return out


def share_split(mds: list, frac: float) -> bool:
    """The simulator's older rule (diagnostic only): >= 2 modes, each >= frac of the mode mass."""
    if len(mds) < 2:
        return False
    tot = sum(m[2] for m in mds) or 1
    return sum(1 for m in mds if m[2] / tot >= frac) >= 2


def window_hours(start_min: Optional[int], end_min: Optional[int]) -> frozenset:
    """Hours h whose [h, h+1) overlaps the window [start, end) on the 24 h circle."""
    if start_min is None or end_min is None or start_min == end_min:
        return frozenset()
    out = set()
    for h in range(24):
        a, b = h * 60, h * 60 + 60
        if start_min < end_min:
            if a < end_min and b > start_min:
                out.add(h)
        else:
            if b > start_min or a < end_min:
                out.add(h)
    return frozenset(out)


def night_share(hours_at: Optional[list], win: frozenset) -> float:
    if not hours_at:
        return 0.0
    tot = sum(hours_at)
    if tot <= 0:
        return 0.0
    return sum(hours_at[h] for h in win) / tot


def frozen(sched: Optional[bool], kind: str, hours_at: Optional[list], win: frozenset, p: Params) -> bool:
    """OA 7.4: frozen while the schedule is ON for a bed, or for a place with >= 50% of its
    confirmation-time hour profile inside the window. An unknown schedule is not ON (D20)."""
    if sched is not True:
        return False
    return kind == "bed" or night_share(hours_at, win) >= p.freeze_night_share


def gap_hours(m1_end: int, m2_start: int) -> list:
    """Hours from m1's end hour to m2's start hour (exclusive), on the circle."""
    out = []
    h = m1_end % 24
    while h != m2_start % 24:
        out.append(h)
        h = (h + 1) % 24
    return out


def suppressed(qual: list, win: frozenset) -> bool:
    """OA 7.8 rule: some pair of circularly adjacent qualifying modes has all its gap hours inside
    the window."""
    if len(qual) < 2 or not win:
        return False
    ms = sorted(qual, key=lambda m: m[0])
    for k in range(len(ms)):
        a = ms[k]
        b = ms[(k + 1) % len(ms)]
        gap = gap_hours(a[1], b[0])
        if gap and all(h in win for h in gap):
            return True
    return False


def bed_named(minutes_wd: list, minutes_we: list, median_s: float, p: Params) -> bool:
    """OA 7.8: hours mostly at night (>= bed_name_share in 21:00-09:00) and median stay >= 2 h."""
    tot = sum(minutes_wd) + sum(minutes_we)
    if tot <= 0:
        return False
    a, b = p.bed_name_hours
    hrs = [h for h in range(24) if (h >= a or h < b)] if a > b else [h for h in range(a, b)]
    night = sum(minutes_wd[h] + minutes_we[h] for h in hrs)
    return night / tot >= p.bed_name_share and median_s >= p.bed_name_median_s


def variants_for(prof_wd: HourProfile, prof_we: HourProfile, we_ready: bool, sched: Optional[bool],
                 is_frozen: bool, win: frozenset, p: Params) -> dict:
    """Variant attributes of a dwell_zone suggestion (spec 7.9.1-7.9.2)."""
    m_wd = modes(prof_wd.minutes, p.tod_smooth_h, p.tod_on_frac)
    m_we = modes(prof_we.minutes, p.tod_smooth_h, p.tod_on_frac)
    q_wd = qualifying(m_wd, prof_wd, p.mode_min_stays, p.mode_min_days)
    q_we = qualifying(m_we, prof_we, p.mode_min_stays, p.mode_min_days) if we_ready else []
    split_wd = len(q_wd) >= 2
    split_we = len(q_we) >= 2
    out = []
    supp = False
    for days, split, q in (("wd", split_wd, q_wd), ("we", split_we, q_we)):
        if not split:
            continue
        if is_frozen and suppressed(q, win):
            supp = True
            continue
        for m in q:
            out.append({"days": days, "start_h": m[0], "end_h": m[1], "stays": m[4], "days_n": m[5]})
    return {"variants": out, "evidence_split": split_wd or split_we,
            "share_split": share_split(m_wd, p.share_rule_frac), "suppressed": supp}
