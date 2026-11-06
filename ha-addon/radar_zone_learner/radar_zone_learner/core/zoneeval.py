"""Confirmed-zone evaluation (P6 spec 4, 5): occupancy, count and enter/leave events of every confirmed zone. Owner: CORE.

Pure and deterministic: no clock, file, socket or random access. Time comes only from the device times of frames, the
device time at the end of a batch, the core's wall time handed in by the learner, and ``on_tick``. The evaluator reads
the tracker's confirmed tracks (raw detection ``rx``, ``ry``, filtered speed, ``powerup``) and never writes to the
tracker; learning does not read the evaluator.

Rules (each cites the design key `CD` of the spec, or says PROPOSAL):
- enter: a confirmed track has >= 3 of its last 10 detections inside the zone area (the cell mask grown by the margin,
  25 cm); in a dwell, bed or manual zone a detection of a track moving at >= 0.8 m/s is not counted;
- leave: one detection outside the area removes the track at once; the zone clears when nobody is inside and no
  vanished-inside hold is pending;
- vanish: no detection for 2 s while inside holds the zone until ``last detection + clear delay``;
- door fast clear: a moving death (> 0.35 m/s over its last 3 s) inside a door neighbourhood clears after 3 s;
- early clear: only for ld2412_reliable zones, only shortens a hold (LD2412 absent and no track anywhere for 60 s);
- power transitions (POWER) suspend every zone with no event; a gap (GAP) keeps the occupancy and suspends the deadline;
- a track born within ``powerup_birth_ms`` of an observed chain's start enters silently (initial snapshot).
"""
from __future__ import annotations

import math
from collections import deque
from typing import Optional

from .p6state import Zone6, variant_id
from .params import P6Params
from .types import (ZONE_KINDS_DWELL, ZONE_KINDS_EVALUATED, ZoneEvent, ZoneState, ZoneStates)

_floor = math.floor              # (a module level name: one lookup less per detection)
FRAME_MS = 100                   # the frame period of the node (10 Hz); only used for the diagnostics
SETTLE_EXTRA_MS = 500            # zones turn valid this long after the power-up snapshot window (spec 5.7, D20)
SPEED_WINDOW_MS = 3000           # the door rule's speed is read over the last 3 s
SPEED_MIN_SPAN_MS = 2000         # ... and needs at least this much track
HIST_MAX = 40                    # positions kept per track (3.5 s at 10 Hz)
LD_DT_MAX_MS = 6000              # a batch interval longer than this adds no LD2412 evidence
DEBUG_MAX = 1000
EVENTS_24H_MS = 86400 * 1000


class _Ring:
    """The last detections of one track near one zone: (device ms, inside, counted)."""
    __slots__ = ("e",)

    def __init__(self, n: int) -> None:
        self.e: deque = deque(maxlen=n)


class _Trk:
    __slots__ = ("tid", "rings", "member", "last_det", "hist", "powerup")

    def __init__(self, tid: int, powerup: bool) -> None:
        self.tid = tid
        self.rings: dict = {}            # zid -> _Ring, only for zones the track is not a member of
        self.member: set = set()         # zids whose S contains the track
        self.last_det = 0
        self.hist: deque = deque(maxlen=HIST_MAX)    # (device ms, x_m, y_m), raw detections
        self.powerup = powerup


class _V:
    """A track that vanished inside a zone: the hold waits for ``last_det + delay`` (+ gap shift)."""
    __slots__ = ("tid", "last_det", "door", "shift", "gap")

    def __init__(self, tid: int, last_det: int, door: bool, shift: int = 0, gap: bool = False) -> None:
        self.tid = tid
        self.last_det = last_det
        self.door = door
        self.shift = shift
        self.gap = gap               # made by a GAP (nobody saw the person vanish): its return is no hidden spell


class _Z:
    __slots__ = ("zid", "kind", "cells", "dwell", "margin_cm", "m", "kk", "occ", "since", "S", "V", "last_in",
                 "late", "ld_on_ms", "ld_known_ms", "ep_date", "ep_n")

    def __init__(self, zid: str, kind: str, cells: frozenset) -> None:
        self.zid = zid
        self.kind = kind
        self.cells = cells
        self.dwell = kind in ZONE_KINDS_DWELL
        self.margin_cm = 25
        self.m = 0.25
        self.kk = 0
        self.occ = False
        self.since = 0
        self.S: dict = {}                # track id -> device ms of its last inside detection
        self.V: list = []                # _V
        self.last_in = 0
        self.late: Optional[tuple] = None    # (last_det, delay ms) after a hold_expired clear
        self.ld_on_ms = 0.0
        self.ld_known_ms = 0.0
        self.ep_date = 0
        self.ep_n = 0

    def reset_runtime(self) -> None:
        self.occ = False
        self.since = 0
        self.S = {}
        self.V = []
        self.last_in = 0
        self.late = None


def _rect_dist(x: float, y: float, x0: float, y0: float, x1: float, y1: float) -> float:
    dx = x0 - x if x < x0 else (x - x1 if x > x1 else 0.0)
    dy = y0 - y if y < y0 else (y - y1 if y > y1 else 0.0)
    return math.hypot(dx, dy)


class ZoneEval:
    def __init__(self, p6: P6Params, geom, time_model, zone6: Zone6, silence_ms: int = 15000,
                 powerup_ms: int = 3000) -> None:
        self.p6 = p6
        self.geom = geom
        self._gx0 = geom.x0
        self._gy0 = geom.y0
        self._gcs = geom.cell
        self._gnx = geom.nx
        self._gny = geom.ny
        self._next_trk_clean = 0
        self.tm = time_model
        self.z6 = zone6
        self.silence_ms = silence_ms
        self.settle_ms = powerup_ms + SETTLE_EXTRA_MS
        self._dflt = dict(p6.delay_default_s)
        self.zones: dict = {}                      # zid -> _Z, evaluated kinds only
        self.cmap: dict = {}                       # cell index -> tuple of _Z that can contain a detection there
        self.trk: dict = {}                        # track id -> _Trk
        self._act: set = set()                     # zones with a member, a hold or a late-return window
        self._door_cells: frozenset = frozenset()
        self._door_k = 0
        self.suspended = True                      # every zone starts invalid; the first observed batch resumes
        self.valid = False
        self._valid_at = 0
        self._gap_dev: Optional[int] = None        # device ms at which a pending GAP began
        self.last_end_dev = 0
        self.last_recv: Optional[int] = None
        self.wall_ms = 0
        self._minute = -1
        self._lt = None
        self._last_any_t: Optional[int] = None     # device ms of the last confirmed detection anywhere
        self.absent_since: Optional[int] = None    # device ms of the first LD2412-absent batch of the current run
        self._prev_ld_t: Optional[int] = None
        self._events: list = []
        self._dirty = True
        self._rows: list = []                      # the ZoneState of every zone, in zone order (only dirty rows are rebuilt)
        self._ridx: dict = {}                      # zid -> index in _rows
        self._zd: set = set()                      # zids whose row is stale
        self._zall = True                          # every cached state is stale
        self._out_sig = None
        self._iview: dict = {}                     # zid -> ``_info_view`` of the zone6.info entry it was made from
        self._debug: deque = deque(maxlen=DEBUG_MAX)
        self._ev_wall: deque = deque()             # wall ms of the events of the last 24 h
        self.counters = {"zones_skipped_kind": 0}

    # ------------------------------------------------------------ configuration
    def _mark(self, z) -> None:
        self._zd.add(z.zid)
        self._dirty = True

    def _mark_all(self) -> None:
        self._zall = True
        self._dirty = True

    def attach(self, zone6: Zone6) -> None:
        self.z6 = zone6
        self._geo()
        self._mark_all()

    def sync(self, zones, door_cells) -> list:
        """``zones``: (zid, kind, cells) of every confirmed zone; ``door_cells``: cell sets of the shadow door
        suggestions. Zones of a kind that is not evaluated are skipped. Returns the evaluated zone ids."""
        new: dict = {}
        skipped = 0
        for zid, kind, cells in zones:
            if kind not in ZONE_KINDS_EVALUATED:
                skipped += 1
                continue
            z = self.zones.get(zid)
            if z is None:
                z = _Z(zid, kind, frozenset(cells))
            else:
                z.kind = kind
                z.cells = frozenset(cells)
                z.dwell = kind in ZONE_KINDS_DWELL
            new[zid] = z
        for zid in set(self.zones) - set(new):
            for T in self.trk.values():
                T.rings.pop(zid, None)
                T.member.discard(zid)
        self.zones = dict(sorted(new.items()))
        self._act = {z for z in self._act if z.zid in self.zones and self.zones[z.zid] is z}
        self.counters["zones_skipped_kind"] = skipped
        for zid in [k for k in self._iview if k not in self.zones]:
            del self._iview[zid]
        dc: set = set()
        for cells in door_cells:
            dc.update(cells)
        self._door_cells = frozenset(dc)
        self._door_k = int(math.ceil(self.p6.door_radius_m / self.geom.cell)) + 1
        self._geo()
        self._mark_all()
        return list(self.zones)

    def refresh(self) -> None:
        """Tuning, nightly values or the zone6 content changed: recompute the geometry and the states."""
        self._geo()
        self._mark_all()

    def margin_in_force(self, zid: str) -> int:
        t = self.z6.tune.get(zid)
        if t is not None and "mg" in t:
            return t["mg"]
        return self.p6.margin_cm

    def delay_in_force(self, zid: str, kind: Optional[str] = None) -> tuple:
        """(seconds, source): the tune override, else the learned delay, else the default by kind (D12)."""
        t = self.z6.tune.get(zid)
        if t is not None and "cd" in t:
            return t["cd"], "manual"
        cd = self.z6.learned(zid)
        if cd is not None:
            return cd, "learned"
        if kind is None:
            z = self.zones.get(zid)
            kind = z.kind if z is not None else "dwell"
        return self._dflt.get(kind, 300), "default"

    def _delay_ms(self, z: _Z) -> int:
        return self.delay_in_force(z.zid, z.kind)[0] * 1000

    def _geo(self) -> None:
        """Margins and the cell -> zones candidate map (D31). For every candidate cell the map holds the zone and the
        rectangles of the few cells that can decide the exact test: ``None`` = the cell is in the mask (inside for a
        margin >= 0); a tuple of rectangles = the mask cells within reach of a ring cell (margin > 0), or the cells
        that are not in the mask next to a mask cell (margin < 0)."""
        g = self.geom
        cs = g.cell
        nx, ny = g.nx, g.ny
        x0g, y0g = g.x0, g.y0
        cmap: dict = {}
        for z in self.zones.values():
            mg = self.margin_in_force(z.zid)
            z.margin_cm = mg
            z.m = mg / 100.0
            k = int(math.ceil(abs(z.m) / cs - 1e-9)) if z.m != 0 else 0
            z.kk = k
            cells = z.cells
            ring: dict = {}
            if z.m >= 0:
                for c in cells:
                    cmap.setdefault(c, []).append((z, None))
                if z.m > 0:
                    for c in cells:
                        i = c % nx
                        j = c // nx
                        rect = (x0g + i * cs, y0g + j * cs, x0g + (i + 1) * cs, y0g + (j + 1) * cs)
                        for dj in range(-k, k + 1):
                            jj = j + dj
                            if 0 <= jj < ny:
                                for di in range(-k, k + 1):
                                    ii = i + di
                                    if 0 <= ii < nx:
                                        key = jj * nx + ii
                                        if key not in cells:
                                            ring.setdefault(key, []).append(rect)
                    for key, rects in ring.items():
                        cmap.setdefault(key, []).append((z, tuple(rects)))
            else:
                for c in cells:
                    i = c % nx
                    j = c // nx
                    rects = []
                    for dj in range(-k, k + 1):
                        jj = j + dj
                        for di in range(-k, k + 1):
                            ii = i + di
                            if 0 <= ii < nx and 0 <= jj < ny and (jj * nx + ii) in cells:
                                continue
                            rects.append((x0g + ii * cs, y0g + jj * cs, x0g + (ii + 1) * cs, y0g + (jj + 1) * cs))
                    cmap.setdefault(c, []).append((z, tuple(rects)))
        self.cmap = {key: tuple(v) for key, v in cmap.items()}

    # ------------------------------------------------------------ geometry
    def _zones_at(self, x: float, y: float) -> tuple:
        cs = self._gcs
        i = _floor((x - self._gx0) / cs)
        j = _floor((y - self._gy0) / cs)
        nx = self._gnx
        if i < 0 or j < 0 or i >= nx or j >= self._gny:
            return ()
        cand = self.cmap.get(j * nx + i)
        if not cand:
            return ()
        out = []
        for z, rects in cand:
            if rects is None:
                out.append(z)
                continue
            r = z.m
            if r > 0:
                r2 = r * r
                for rx0, ry0, rx1, ry1 in rects:
                    dx = rx0 - x if x < rx0 else (x - rx1 if x > rx1 else 0.0)
                    dy = ry0 - y if y < ry0 else (y - ry1 if y > ry1 else 0.0)
                    if dx * dx + dy * dy <= r2:
                        out.append(z)
                        break
            else:
                r2 = r * r
                for rx0, ry0, rx1, ry1 in rects:
                    dx = rx0 - x if x < rx0 else (x - rx1 if x > rx1 else 0.0)
                    dy = ry0 - y if y < ry0 else (y - ry1 if y > ry1 else 0.0)
                    if dx * dx + dy * dy <= r2:
                        break
                else:
                    out.append(z)
        return tuple(out)

    def _near_door(self, x: float, y: float) -> bool:
        dc = self._door_cells
        if not dc:
            return False
        g = self.geom
        cs = g.cell
        i = int(math.floor((x - g.x0) / cs))
        j = int(math.floor((y - g.y0) / cs))
        k = self._door_k
        r = self.p6.door_radius_m
        for jj in range(max(0, j - k), min(g.ny, j + k + 1)):
            for ii in range(max(0, i - k), min(g.nx, i + k + 1)):
                if (jj * g.nx + ii) in dc:
                    cx0 = g.x0 + ii * cs
                    cy0 = g.y0 + jj * cs
                    if _rect_dist(x, y, cx0, cy0, cx0 + cs, cy0 + cs) <= r:
                        return True
        return False

    def _door_exit(self, T: _Trk) -> bool:
        """The 'moving death inside a learned door' class: the last detection is in a door neighbourhood and the net
        speed over the last 3 s is above ``door_speed_ms``."""
        if not T.hist or not self._door_cells:
            return False
        t1, x1, y1 = T.hist[-1]
        if not self._near_door(x1, y1):
            return False
        first = None
        for t, x, y in T.hist:
            if t >= t1 - SPEED_WINDOW_MS:
                first = (t, x, y)
                break
        if first is None:
            return False
        span = t1 - first[0]
        if span < SPEED_MIN_SPAN_MS:
            return False
        v = math.hypot(x1 - first[1], y1 - first[2]) / (span / 1000.0)
        return v > self.p6.door_speed_ms

    # ------------------------------------------------------------ batch and time
    def begin_batch(self, start_dev: int, end_dev: int) -> None:
        """An observed batch (frames, maybe none) starts: a suspended evaluator resumes, a pending GAP ends."""
        if self.suspended:
            self.suspended = False
            self.valid = False
            self._valid_at = start_dev + self.settle_ms
            self.last_end_dev = start_dev
            self._next_trk_clean = 0
            self._mark_all()
        if self._gap_dev is not None and start_dev - self._gap_dev >= self.silence_ms:
            self._power()                           # the unobserved span was a power loss (D42): resume as from POWER
            self.suspended = False
            self.valid = False
            self._valid_at = start_dev + self.settle_ms
            self.last_end_dev = start_dev
            self._mark_all()
        if self._gap_dev is not None:
            shift = max(0, start_dev - self._gap_dev)
            if shift:
                for z in self._act:
                    for v in z.V:
                        v.shift += shift
            self._gap_dev = None

    def on_time(self, t: int, now_ms: int) -> None:
        """The end of a batch: device time ``t`` (its last covered frame), core wall time ``now_ms``."""
        self.wall_ms = now_ms
        if self.suspended:
            return
        self.last_end_dev = t
        self.last_recv = now_ms
        self._sweep(t)
        self._variant_check(now_ms)

    def on_tick(self, now_ms: int) -> None:
        """A tick. Hold deadlines are judged on the device time of real batches only: a tick cannot know whether the
        next batch is lost, and an event must never come from an unobserved span (spec 5.7, D42), so the tick only
        turns the zones valid after the power-up settle time (and moves the local time of the variants)."""
        self.wall_ms = now_ms
        if not self.suspended and self.last_recv is not None:
            age = now_ms - self.last_recv
            if self._gap_dev is not None and age >= self.silence_ms:
                self._power()                       # POWER wins (D42): a pending GAP that never ended is a power loss
            elif 0 <= age < self.silence_ms:
                est = self.last_end_dev + age
                if not self.valid and est >= self._valid_at:
                    self.valid = True
                    self._mark_all()
                if self._act and self._gap_dev is None:
                    self._vanish_members(est)       # a stayer's hold is never ended on extrapolated time ...
                    for z in self._acts():
                        if z.V and not z.S:
                            self._expire(z, est, True)      # ... but a moving death through a door is (spec 5.5)
        self._variant_check(now_ms)

    def _variant_check(self, now_ms: int) -> None:
        m = now_ms // 60000
        if m != self._minute:
            self._minute = m
            self._lt = self.tm.local(now_ms)
            self._mark_all()

    # ------------------------------------------------------------ censoring (spec 5.7)
    def on_censor(self, cls: str, at_dev: int) -> None:
        if cls == "GAP":
            self._gap(at_dev)
        else:
            self._power()

    def _power(self) -> None:
        """POWER: every zone becomes invalid, every runtime state and evidence in progress is dropped, no event."""
        if self.suspended:
            return
        self.suspended = True
        self.valid = False
        for z in self.zones.values():
            z.reset_runtime()
        self._act.clear()
        self.trk.clear()
        self._gap_dev = None
        self.absent_since = None
        self._prev_ld_t = None
        self._last_any_t = None
        self.last_recv = None
        self._next_trk_clean = 0
        self._mark_all()

    def _gap(self, at_dev: int) -> None:
        """GAP: tracks end, the occupancy is kept, the members become vanished-inside holds whose deadline does not
        run while the span is unobserved (D42)."""
        if self.suspended:
            return
        for z in self.zones.values():
            if z.S:
                for tid, ld in z.S.items():
                    z.V.append(_V(tid, ld, False, 0, True))
                z.S = {}
                self._act.add(z)
        self.trk.clear()
        if self._gap_dev is None:
            self._gap_dev = at_dev
        self.absent_since = None
        self._prev_ld_t = None
        self._mark_all()

    # ------------------------------------------------------------ frames
    def on_frame(self, t: int, live) -> None:
        """One listed frame at device time ``t``; ``live`` = the tracker's live tracks after ``step`` for that frame."""
        if self.suspended:
            return
        self._sweep(t)
        if not self.zones:
            return
        for tr in live:
            if tr.last_dev == t and tr.confirmed:
                self._detect(t, tr)

    def _detect(self, t: int, tr) -> None:
        p = self.p6
        T = self.trk.get(tr.id)
        if T is None:
            T = self.trk[tr.id] = _Trk(tr.id, bool(tr.powerup))
        x = tr.rx
        y = tr.ry
        T.last_det = t
        self._last_any_t = t
        if self._door_cells:
            T.hist.append((t, x, y))
        inside = self._zones_at(x, y)
        member = T.member
        if not inside and not member and not T.rings:
            return
        speed = -1.0                                  # the filtered speed, read only for a zone that the track is not in yet
        n_mem = 0                                     # the zones of ``inside`` that the track is a member of
        for z in inside:
            zid = z.zid
            if zid in member:
                z.S[tr.id] = t
                z.last_in = t
                n_mem += 1
                continue
            if speed < 0.0:
                speed = math.hypot(tr.vx, tr.vy)
            counted = (speed < p.pass_speed_ms) if z.dwell else True
            ring = T.rings.get(zid)
            if ring is None:
                ring = T.rings[zid] = _Ring(p.enter_n)
            ring.e.append((t, True, counted))
            if counted and sum(1 for e in ring.e if e[2]) >= p.enter_k:
                self._join(z, T, t, ring)
                n_mem += 1                            # _join made the track a member of z
        if n_mem != len(member) or T.rings:
            ins_ids = {z.zid for z in inside}
            if n_mem != len(member):                  # a member zone that the detection is not in: the track left it
                for zid in sorted(zid for zid in member if zid not in ins_ids):
                    member.discard(zid)
                    z = self.zones.get(zid)
                    if z is not None and tr.id in z.S:
                        self._leave_edge(z, T, t)
            if T.rings:
                for zid in [zid for zid in T.rings if zid not in ins_ids]:
                    ring = T.rings[zid]
                    ring.e.append((t, False, False))
                    if not any(e[1] for e in ring.e):
                        del T.rings[zid]

    def _join(self, z: _Z, T: _Trk, t: int, ring: _Ring) -> None:
        p = self.p6
        T.rings.pop(z.zid, None)
        if z.V:
            real = [v.last_det for v in z.V if not v.gap]
            if real:                                  # a return after a GAP is not a hidden spell (spec 5.4)
                self._spell(z.zid, (t - max(real)) / 1000.0)
            z.V = []
        T.member.add(z.zid)
        z.S[T.tid] = t
        z.last_in = t
        self._act.add(z)
        self._mark(z)
        if z.occ:
            return
        z.occ = True
        z.since = t
        late = z.late
        z.late = None
        silent = T.powerup and not self.valid       # the power-up snapshot: only while the zones are not valid yet
        if late is not None and not silent:
            ld, d_ms = late
            if 0 < t - ld <= p.late_return_factor * d_ms:
                self._spell(z.zid, (t - ld) / 1000.0)
        if silent:
            return
        first = None
        n_counted = 0
        for e in ring.e:
            if e[1] and first is None:
                first = e[0]
            if first is not None and e[2]:
                n_counted += 1
        if first is None:
            first = t
        frames = int(round((t - first) / FRAME_MS)) + 1
        self._debug.append((z.zid, T.tid, first, t, max(0, frames - n_counted)))
        self._emit(z, "enter", "", t, T.tid)
        self._episode(z)

    def _leave_edge(self, z: _Z, T: _Trk, t: int) -> None:
        z.S.pop(T.tid, None)
        self._mark(z)
        if not z.S and not z.V:
            self._clear(z, "edge", t, T.tid)

    # ------------------------------------------------------------ holds
    def _vanish(self, z: _Z, tid: int, last_det: int) -> None:
        z.S.pop(tid, None)
        T = self.trk.get(tid)
        door = False
        if T is not None:
            T.member.discard(z.zid)
            door = self._door_exit(T)
        z.V.append(_V(tid, last_det, door, 0))
        self._mark(z)

    def _deadline(self, v: _V, d_ms: int) -> int:
        return v.last_det + (self.p6.fast_clear_s * 1000 if v.door else d_ms) + v.shift

    def _acts(self):
        """The active zones in a fixed order (zone id): a hash seed must not change the order of simultaneous events."""
        a = self._act
        return list(a) if len(a) < 2 else sorted(a, key=lambda e: e.zid)      # a copy: the loops discard zones

    def _vanish_members(self, t: int) -> None:
        """Members not detected for ``vanish_ms`` become vanished-inside holds (no event, only the state: count 0, held)."""
        vanish_ms = self.p6.vanish_ms
        for z in self._acts():
            if z.S:
                for tid, ld in list(z.S.items()):
                    if t - ld > vanish_ms:
                        self._vanish(z, tid, ld)

    def _sweep(self, t: int) -> None:
        if self.suspended:
            return
        if not self.valid and t >= self._valid_at:
            self.valid = True
            self._mark_all()
        if self.trk and t >= self._next_trk_clean:
            self._next_trk_clean = t + 5000
            old = [tid for tid, T in self.trk.items() if t - T.last_det > self.p6.vanish_ms * 3 and not T.member]
            for tid in old:
                del self.trk[tid]
        if not self._act:
            return
        vanish_ms = self.p6.vanish_ms
        if self._gap_dev is None:
            # the usual frame: every active zone has members, none of them silent, no hold and no late-return window, so the loop
            # below would do nothing; skip it (and the sorting of the zones) then
            lim = t - vanish_ms
            for z in self._act:
                if z.V or z.late is not None or not z.S:
                    break
                for ld in z.S.values():
                    if ld < lim:
                        break
                else:
                    continue
                break
            else:
                return
        for z in self._acts():
            if z.S and self._gap_dev is None:
                for tid, ld in list(z.S.items()):
                    if t - ld > vanish_ms:
                        self._vanish(z, tid, ld)
            if self._gap_dev is not None:
                continue
            if z.V:
                self._expire(z, t)
            if z.V and not z.S:
                self._early(z, t)
            if z.late is not None and t - z.late[0] > self.p6.late_return_factor * z.late[1]:
                z.late = None
            if not (z.S or z.V or z.late is not None):
                self._act.discard(z)

    def _expire(self, z: _Z, t: int, doors_only: bool = False) -> None:
        d_ms = self._delay_ms(z)
        rem = []
        last = None
        for v in z.V:
            if doors_only and not v.door:
                rem.append(v)
                continue
            dl = self._deadline(v, d_ms)
            if dl <= t:
                if last is None or dl > last[0]:
                    last = (dl, v)
            else:
                rem.append(v)
        if last is None:
            return
        z.V = rem
        self._dirty = True
        if not rem and not z.S:
            dl, v = last
            self._clear(z, "door_death" if v.door else "hold_expired", dl, v.tid)
            if not v.door:
                z.late = (v.last_det, d_ms)

    def _early(self, z: _Z, t: int) -> None:
        """Early clear (spec 5.6): ld2412_reliable zones only, only shortens a hold."""
        if self.absent_since is None or not self.z6.is_ldr(z.zid):
            return
        v = max(z.V, key=lambda e: e.last_det)
        start = max(self.absent_since, self._last_any_t if self._last_any_t is not None else 0, v.last_det)
        win = self.p6.early_absent_s * 1000
        if t - start >= win:
            self._clear(z, "early", start + win, v.tid)

    def _clear(self, z: _Z, via: str, t: int, tid: int) -> None:
        since = z.since
        z.occ = False
        z.S = {}
        z.V = []
        self._mark(z)
        for T in self.trk.values():
            T.member.discard(z.zid)
        self._emit(z, "leave", via, t, tid, max(0, (t - since) // 1000))
        if not z.late:
            self._act.discard(z)

    def _spell(self, zid: str, secs: float) -> None:
        """A hidden spell (spec 5.4): not recorded while the zones are not valid."""
        if not self.valid:
            return
        p = self.p6
        n = int(round(secs))
        if n < 1:
            n = 1
        if n > 86400:
            n = 86400
        e = self.z6.delay.setdefault(zid, {"sp": []})
        sp = e["sp"]
        sp.append(n)
        if len(sp) > p.spell_keep:
            del sp[:len(sp) - p.spell_keep]

    # ------------------------------------------------------------ LD2412 (spec 5.6)
    def on_ld(self, t_ms: int, p2: Optional[int], ld_on: bool) -> None:
        """The LD2412 bit of an observed batch: ``p2`` (1 = present) is valid only while ``ld_on`` (pw bit 1)."""
        if self.suspended:
            return
        if ld_on and p2 is not None:
            prev = self._prev_ld_t
            if prev is not None and 0 < t_ms - prev <= LD_DT_MAX_MS:
                dt = t_ms - prev
                for z in self.zones.values():
                    if z.S:
                        z.ld_known_ms += dt
                        if p2:
                            z.ld_on_ms += dt
            self._prev_ld_t = t_ms
            if p2:
                self.absent_since = None
            elif self.absent_since is None:
                self.absent_since = t_ms
        else:
            self._prev_ld_t = None
            self.absent_since = None
        if self._gap_dev is None and self._act and self.absent_since is not None:
            for z in self._acts():
                if z.V and not z.S:
                    self._early(z, t_ms)

    def flush_ld(self) -> None:
        """Moves the whole seconds of the LD2412 evidence into ``zone6.ld`` (nightly and before every export)."""
        for z in self.zones.values():
            on_s = int(z.ld_on_ms // 1000)
            kn_s = int(z.ld_known_ms // 1000)
            if on_s or kn_s:
                e = self.z6.ld.setdefault(z.zid, [0, 0])
                e[0] += on_s
                e[1] += kn_s
                if e[0] > e[1]:
                    e[0] = e[1]
                z.ld_on_ms -= on_s * 1000
                z.ld_known_ms -= kn_s * 1000

    # ------------------------------------------------------------ events and bookkeeping
    def _info_view(self, zid: str, info: dict) -> tuple:
        """``(info, typical hours, confidence, variant ids, [(days, start, end, id)])`` of a ``zone6.info`` entry. The entries
        of ``zone6.info`` are replaced as a whole (nightly copy, load), never changed in place, so the view is kept for as long as
        the entry is the same object (a state row is rebuilt every time a zone changes: formatting the hours and the variant ids
        each time was the larger part of that)."""
        c = self._iview.get(zid)
        if c is not None and c[0] is info:
            return c
        parts = []
        for cls in ("wd", "we"):
            rngs = info["th"].get(cls)
            if rngs:
                parts.append("%s %s" % (cls, ",".join(rngs)))
        conf = round(float(info["conf"]), 2) if info.get("conf") is not None else None
        vl = [(v["d"], v["s"], v["e"], variant_id(v)) for v in info["var"]]
        c = self._iview[zid] = (info, "; ".join(parts), conf, tuple(v[3] for v in vl), vl)
        return c

    def _variant_now(self, zid: str) -> str:
        info = self.z6.info.get(zid)
        lt = self._lt
        if not info or not info["var"] or lt is None:
            return ""
        cls = "wd" if lt.weekday < 5 else "we"
        h = lt.sec_of_day // 3600
        for d, s, e, vid in self._info_view(zid, info)[4]:
            if d != cls:
                continue
            if (s <= h < e) if s <= e else (h >= s or h < e):
                return vid
        return ""

    def _emit(self, z: _Z, kind: str, via: str, t: int, tid: int, dwell_s: int = 0) -> None:
        self._events.append(ZoneEvent(z.zid, kind, via, int(t), int(tid), int(dwell_s), self._variant_now(z.zid)))
        w = self.wall_ms
        self._ev_wall.append(w)
        lo = w - EVENTS_24H_MS
        while self._ev_wall and self._ev_wall[0] < lo:
            self._ev_wall.popleft()

    def _episode(self, z: _Z) -> None:
        d = self.tm.local(self.wall_ms).date_ord
        if z.ep_date != d:
            z.ep_date = d
            z.ep_n = 0
        z.ep_n += 1

    def drain(self) -> list:
        """The ZoneEvents since the last call, in the order they happened."""
        ev = self._events
        self._events = []
        return ev

    def forget(self, zid: str) -> None:
        """``ZoneForget``: evidence and tuning of one zone are gone (the caller cleared ``zone6``); the evaluator drops
        its evidence in progress and recomputes."""
        z = self.zones.get(zid)
        if z is not None:
            z.ld_on_ms = 0.0
            z.ld_known_ms = 0.0
            z.late = None
        self.refresh()

    # ------------------------------------------------------------ outputs
    def zone_ids(self) -> list:
        return list(self.zones)

    def _zstate(self, zid: str, z: _Z) -> ZoneState:
        z6 = self.z6
        d_s, src = self.delay_in_force(zid, z.kind)
        info = z6.info.get(zid)
        th = ""
        conf = None
        variants: tuple = ()
        if info:
            _, th, conf, variants, _ = self._info_view(zid, info)
        return ZoneState(zid, bool(z.occ), min(len(z.S), self.p6.count_max), bool(z.occ and not z.S),
                         self._variant_now(zid), z6.is_ldr(zid), d_s, src, z.margin_cm, variants,
                         tuple(z6.ve.get(zid, ())), th, conf)

    def states(self) -> ZoneStates:
        """Every zone state; only the rows of the zones marked dirty since the last call are rebuilt (the other rows are
        the same objects, so the signature comparison of an unchanged zone costs nothing)."""
        zones = self.zones
        if self._zall or len(self._rows) != len(zones):
            self._rows = [self._zstate(zid, z) for zid, z in zones.items()]
            self._ridx = {zid: i for i, zid in enumerate(zones)}
            self._zall = False
            self._zd.clear()
        elif self._zd:
            idx = self._ridx
            rows = self._rows
            for zid in self._zd:
                i = idx.get(zid)
                z = zones.get(zid)
                if i is not None and z is not None:
                    rows[i] = self._zstate(zid, z)
            self._zd.clear()
        return ZoneStates(bool(self.valid and not self.suspended), self.last_end_dev, tuple(self._rows))

    def take_states(self) -> Optional[ZoneStates]:
        """The states when their signature differs from the last one taken (D6), else None. With no zone nothing is
        emitted until a zone has been emitted once (the signature of 'no zones' is the initial one)."""
        if not self._dirty:
            return None
        self._dirty = False
        if not self.zones:
            sig = None
        else:
            st = self.states()
            sig = (st.valid, st.zones)
        if sig == self._out_sig:
            return None
        self._out_sig = sig
        if sig is None:
            return ZoneStates(bool(self.valid and not self.suspended), self.last_end_dev, ())
        return st

    def snapshot(self) -> tuple:
        """Read-only ((id, occ, valid), ...) for P7."""
        v = bool(self.valid and not self.suspended)
        return tuple((zid, bool(z.occ), v) for zid, z in self.zones.items())

    def debug(self) -> tuple:
        return tuple(self._debug)

    def events_24h(self) -> int:
        lo = self.wall_ms - EVENTS_24H_MS
        while self._ev_wall and self._ev_wall[0] < lo:
            self._ev_wall.popleft()
        return len(self._ev_wall)

    def zones_occupied(self) -> int:
        return sum(1 for z in self.zones.values() if z.occ)

    def zones_over_budget(self) -> int:
        d = self.tm.local(self.wall_ms).date_ord
        b = self.p6.episode_budget_day
        return sum(1 for z in self.zones.values() if z.ep_date == d and z.ep_n > b)
