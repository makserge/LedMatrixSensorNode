"""Frame generator: the simulator world as 10 Hz LD2454-like frames in P1 batches (P3 spec 11.3).

One ``RunStream`` per (job, seed) turns the day-by-day world of ``replay.world`` into the input events of
the learner core, in reception order:

* the radar timeline (11.3.1): device boot at simulated -120 s; per night-window interval ``[a, b)``:
  frames and observing stop at ``a``, OFF heartbeats every 30 s, power-up at ``b``, frames from ``b + 3 s``
  (warm-up, ``pw`` 0), observing (``pw`` 3) from ``b + 12 s``; ``sm`` "A" outside the window, "N" inside;
* targets per frame (11.3.2): every 1 Hz track point gives up to 10 frames (linear interpolation to the
  next point when that is 1 s later, else held), 2 cm Gaussian noise per axis (not for the static
  reflector), radial ``v``, 2% frame misses, at most 3 targets by priority, stable slots;
* LD2412 OUT (11.3.3): 1 while the room is occupied, except dropout runs (on-runs mean 980 s, misses mean
  20 s, so 2% of occupied time), no false positives; ``p2`` = OUT at publish time while ``pw`` = 3;
* ``home_empty`` edges and the node status; the optional faults of 11.3.4;
* P4 (OA 7.5): the BH1750 lux samples every 10 s (``LuxModel``; off by default, ``lux=True``), the sleep onsets
  after 00:00 that the simulator drops (``late_overlay``: the sleeper walks in at the true onset), and optional
  scripted extra people (``extras``, for the 'two people in bed' check of OA 12 P4);
* P6 (spec 11.1, 11.6): ``quiet_from_day`` drops the world's own agents (the reflector ``refl``, the first-day extras and
  the agents named in ``quiet_keep`` stay), ``scripted`` (``replay.p6_people.ScriptedPeople``) adds the scripted people of
  a day (1 Hz waypoints, per-day random stream, fed to the LD2412 model except where flagged), ``gaps`` drops scripted
  windows of messages, and every dropped message and every run of dropped messages is recorded (``dropped``, ``spans``);
* P7 (spec 8.2): ``rot`` rotates and shifts every emitted point about the origin from a simulated time on (a radar that was
  turned or moved, W2) and ``empty_off`` switches the radars off during the home-empty intervals of the world (W5, the user's
  own automation); neither draws a random number;
* the MqttTask loop and the P1 batcher of ``replay.batcher``; latency 5-30 ms per message.

All random numbers come from separate ``random.Random`` streams per run, seeded from the run's seed; the
world's own stream (``replay.world.RunWorld``) is untouched, so the world equals the simulator's.
Times inside are simulated ms (0 = day 0 00:00); reception times are ``BASE_MS`` + simulated ms.
"""
from __future__ import annotations

import heapq
import math
import random
from collections import deque
from typing import Iterator, Optional

from radar_zone_learner.core.types import ContextState, FramesMessage, LuxSample, NodeStatus

from . import world as W
from .batcher import FRAME_PHASES, OFF, ON, POWERING, WARMUP, MqttLoop, Segment

BASE_MS = 1767571200000          # 2026-01-05 00:00 UTC, a Monday (P3 spec 4.4)
BOOT_SIM_MS = -120_000           # device boot, simulated ms
FRAME_MS = 100
NOISE_M = 0.02
P_MISS = 0.02
FIRST_FRAME_MS = 3000            # first frame after power-up
OBSERVE_MS = 12000               # observing (pw 3) after power-up (LD2412_STATIC_WARMUP_MS)
LD_MISS_MEAN_S = 20.0
LD_ON_MEAN_S = 980.0
LAT_MIN_MS, LAT_MAX_MS = 5, 30
XY_MAX = 32767
CHUNK_MS = 3_600_000
DAY_MS = W.DAY * 1000
PRIO = {"human": 0, "pet": 0, "mirror": 1, "inghost": 1, "refl": 2}
NOISED = frozenset(("human", "pet", "mirror", "inghost"))
INF = 1 << 62

# P4: lux proxy (OA 7.5) and scripted extras
LUX_PERIOD_MS = 10_000           # <node>/sensors/state every MQTT_PUBLISH_INTERVAL (include/constants.h)
LUX_BRIGHT = 150.0               # lights on, a lit room (lux)
LUX_DARK = (0.5, 2.5)            # lights off: the panel's own glow, below night_lux (5 lx)
LUX_OFF_LEAD_S = (0, 240)        # the lights go off 0-4 min after the sleeper reaches the bed ...
LUX_ON_LEAD_S = (0, 300)         # ... and on again 0-5 min after the wake
EXTRA_SIGMA_M = 0.07

# faults (11.3.4)
FAULT_LOSS_P = 0.001
FAULT_REBOOT = (9, 14 * 3600)          # day 9, 14:00
FAULT_REBOOT_DOWN_MS = 20_000          # no frames and no messages for 20 s, then 12 s warm-up
FAULT_WILL_MS = 22_000                 # the broker's will: 22 s after the last message
FAULT_LOSS = (12, 10 * 3600, 600)      # day 12, 10:00, 10 minutes


def _rng(seed: int, k: int) -> random.Random:
    return random.Random(seed * 7919 + k)


def boot_ids(seed: int, count: int) -> list:
    r = _rng(seed, 3)
    return ['%08x' % r.getrandbits(32) for _ in range(count)]


# ---------------------------------------------------------------- radar timeline (11.3.1)
class Epoch:
    """One boot of the node: boot time, boot id, phase segments and frame intervals (simulated ms)."""

    def __init__(self, boot_sim_ms: int, boot_id: str, segments: list, stop_ms: Optional[int]):
        self.boot = boot_sim_ms
        self.boot_id = boot_id
        self.segments = segments
        self.stop_ms = stop_ms
        self.intervals = []                     # (fs, fe, seq0): frames on the 100 ms grid in [fs, fe)
        seq = 0
        cur = None
        for s in segments:
            if s.phase in FRAME_PHASES:
                if cur is not None and cur[1] == s.start:
                    cur[1] = s.end
                else:
                    cur = [s.start, s.end]
                    self.intervals.append(cur)
        out = []
        for fs, fe in self.intervals:
            out.append((fs, fe, seq))
            seq += (fe - fs) // FRAME_MS
        self.intervals = out

    @property
    def start(self) -> int:
        return self.segments[0].start


def _phase_segments(start: int, power_up_at: Optional[int], offs: list, end: int, first_frames: Optional[int] = None,
                    first_observe: Optional[int] = None) -> list:
    """Segments from ``start`` to ``end``: an optional power-up at ``power_up_at``, then the off intervals."""
    segs = []

    def add(a, b, ph):
        if b > a:
            segs.append(Segment(a, b, ph))

    t = start
    if power_up_at is not None:
        ff = first_frames if first_frames is not None else power_up_at + FIRST_FRAME_MS
        fo = first_observe if first_observe is not None else power_up_at + OBSERVE_MS
        add(t, ff, POWERING)
        add(max(t, ff), fo, WARMUP)
        t = max(t, fo)
    for a, b in offs:
        if b <= t:
            continue
        if a > t:
            add(t, min(a, end), ON)
        if a >= end:
            t = end
            break
        a = max(a, t)
        add(a, min(b, end), OFF)
        t = min(b, end)
        if t >= end:
            break
        add(t, min(t + FIRST_FRAME_MS, end), POWERING)
        add(min(t + FIRST_FRAME_MS, end), min(t + OBSERVE_MS, end), WARMUP)
        t = min(t + OBSERVE_MS, end)
    add(t, end, ON)
    return segs


class Timeline:
    """The run's radar timeline: epochs (boots), off intervals, and fault windows."""

    def __init__(self, cfg: dict, seed: int, horizon_days: int, faults: bool = False, extra_offs=()):
        offs = []
        for d in range(horizon_days + 1):
            offs.extend(W.night_window_off(d, cfg))
        offs.extend((int(a), int(b)) for a, b in extra_offs)           # P7 W5: the user's own empty-room switch-off
        merged = [(a * 1000, b * 1000) for a, b in W.union(offs)]
        self.offs = merged
        self.end = (horizon_days + 1) * DAY_MS
        ids = boot_ids(seed, 2)
        self.loss = None
        self.reboot = None
        if faults and horizon_days > FAULT_REBOOT[0]:
            r = (FAULT_REBOOT[0] * W.DAY + FAULT_REBOOT[1]) * 1000
            self.reboot = r
            e1 = Epoch(BOOT_SIM_MS, ids[0], _phase_segments(BOOT_SIM_MS, BOOT_SIM_MS, merged, r), r)
            restart = r + FAULT_REBOOT_DOWN_MS
            segs2 = _phase_segments(restart, restart, merged, self.end, first_frames=restart,
                                    first_observe=restart + OBSERVE_MS)
            e2 = Epoch(r, ids[1], segs2, None)
            self.epochs = [e1, e2]
        else:
            self.epochs = [Epoch(BOOT_SIM_MS, ids[0], _phase_segments(BOOT_SIM_MS, BOOT_SIM_MS, merged, self.end), None)]
        if faults and horizon_days > FAULT_LOSS[0]:
            a = (FAULT_LOSS[0] * W.DAY + FAULT_LOSS[1]) * 1000
            self.loss = (a, a + FAULT_LOSS[2] * 1000)

    def phase_at(self, t: int) -> Optional[str]:
        """The phase at simulated ms ``t``, or None while the node is down (reboot)."""
        for e in self.epochs:
            if e.stop_ms is not None and t >= e.stop_ms:
                continue
            if t < e.start:
                continue
            for s in e.segments:
                if s.start <= t < s.end:
                    return s.phase
        return None

    def observing(self, t: int) -> bool:
        if self.loss is not None and self.loss[0] <= t < self.loss[1]:
            return False
        return self.phase_at(t) == ON

    def off_seconds(self) -> list:
        """Radar-off intervals in simulated seconds (the night window), merged."""
        return [(a // 1000, b // 1000) for a, b in self.offs]


# ---------------------------------------------------------------- targets per frame (11.3.2)
class _TrackState:
    __slots__ = ("pts", "i", "noised", "serial")

    def __init__(self, pts, noised, serial):
        self.pts = pts
        self.i = 0
        self.noised = noised
        self.serial = serial


class TargetFrames:
    """Target frames of every track, in time order, materialised one simulated second at a time."""

    def __init__(self, seed: int, noise_m: float = NOISE_M, p_miss: float = P_MISS, rot=None):
        self.heap = []
        self.serial = 0
        self.noise_m = noise_m
        self.p_miss = p_miss
        # P7 W2: ``rot`` = (day, second_of_day, degrees, dx_m, dy_m): from that simulated second on every emitted point of every
        # kind is rotated about the origin (the radar) and shifted before the noise is added. No random number is drawn for it.
        self.rot = None
        if rot is not None:
            a = math.radians(rot[2])
            self.rot = (int(rot[0]) * W.DAY + int(rot[1]), math.cos(a), math.sin(a), float(rot[3]), float(rot[4]))
        self.rng_noise = _rng(seed, 5)
        self.rng_miss = _rng(seed, 7)
        self.buf = deque()           # (ms, [(serial, x_mm, y_mm, v_cms), ...]) with >= 1 target
        self.points = 0
        self.target_entries = 0

    def add_tracks(self, d: int, tracks: list) -> None:
        for k, tr in enumerate(tracks):
            pts = []
            last = None
            for p in tr['pts']:
                t = int(p[0])
                if t != last:                     # one point per track and second (the first after sort)
                    pts.append((t, p[1], p[2]))
                    last = t
            if not pts:
                continue
            self.serial += 1
            st = _TrackState(pts, tr['kind'] in NOISED, self.serial)
            heapq.heappush(self.heap, (pts[0][0], PRIO.get(tr['kind'], 1), pts[0][0], self.serial, st))

    def _materialise(self) -> bool:
        heap = self.heap
        if not heap:
            return False
        t = heap[0][0]
        rows = []
        miss = self.rng_miss.random
        gauss = self.rng_noise.gauss
        nm = self.noise_m
        pm = self.p_miss
        hyp = math.hypot
        rot = self.rot if self.rot is not None and t >= self.rot[0] else None
        while heap and heap[0][0] == t:
            item = heapq.heappop(heap)
            st = item[4]
            pts = st.pts
            i = st.i
            _, x, y = pts[i]
            nxt = pts[i + 1] if i + 1 < len(pts) and pts[i + 1][0] == t + 1 else None
            vals = [None] * 10
            self.points += 1
            if nxt is not None:
                dx = nxt[1] - x
                dy = nxt[2] - y
            for q in range(10):
                if miss() < pm:
                    continue
                if nxt is not None:
                    xi = x + dx * q / 10.0
                    yi = y + dy * q / 10.0
                    f1 = (q + 1) / 10.0
                    # radial speed of the noise-free position over the next 100 ms, cm/s
                    if rot is None:
                        v = int(round((hyp(x + dx * f1, y + dy * f1) - hyp(xi, yi)) * 1000.0))
                    else:                                  # the same speed of the transformed positions (a rotation keeps the range)
                        _, rc, rs, rx, ry = rot
                        xe, ye = x + dx * f1, y + dy * f1
                        v = int(round((hyp(rc * xe - rs * ye + rx, rs * xe + rc * ye + ry)
                                       - hyp(rc * xi - rs * yi + rx, rs * xi + rc * yi + ry)) * 1000.0))
                else:
                    xi, yi, v = x, y, 0
                if rot is not None:
                    _, rc, rs, rx, ry = rot
                    xi, yi = rc * xi - rs * yi + rx, rs * xi + rc * yi + ry
                if st.noised:
                    xi += gauss(0.0, nm)
                    yi += gauss(0.0, nm)
                xm = int(round(xi * 1000.0))
                ym = int(round(yi * 1000.0))
                xm = XY_MAX if xm > XY_MAX else (-XY_MAX if xm < -XY_MAX else xm)
                ym = XY_MAX if ym > XY_MAX else (-XY_MAX if ym < -XY_MAX else ym)
                vals[q] = (st.serial, xm, ym, max(-XY_MAX, min(XY_MAX, v)))
            rows.append(vals)
            st.i = i + 1
            if st.i < len(pts):
                heapq.heappush(heap, (pts[st.i][0], item[1], item[2], item[3], st))
        base = t * 1000
        for q in range(10):
            tg = [r[q] for r in rows if r[q] is not None]
            if tg:
                self.target_entries += min(3, len(tg))
                self.buf.append((base + q * FRAME_MS, tg[:3]))
        return True

    def next_frame(self, min_ms: int):
        """The first target frame with ms >= ``min_ms`` (earlier ones are discarded), or None."""
        buf = self.buf
        while True:
            while buf and buf[0][0] < min_ms:
                buf.popleft()
            if buf:
                return buf[0]
            if not self.heap:
                return None
            # skip whole seconds that end before min_ms without materialising them
            heap = self.heap
            while heap and (heap[0][0] + 1) * 1000 <= min_ms:
                item = heapq.heappop(heap)
                st = item[4]
                # consume this second's draws, so random streams do not depend on the timeline
                self._skip_second(st, item)
            if not heap:
                return None
            self._materialise()

    def _skip_second(self, st, item) -> None:
        miss = self.rng_miss.random
        gauss = self.rng_noise.gauss
        for _ in range(10):
            if miss() < self.p_miss:
                continue
            if st.noised:
                gauss(0.0, self.noise_m)
                gauss(0.0, self.noise_m)
        self.points += 1
        st.i += 1
        if st.i < len(st.pts):
            heapq.heappush(self.heap, (st.pts[st.i][0], item[1], item[2], item[3], st))

    def pop(self) -> None:
        self.buf.popleft()


class EpochFrames:
    """Frame source of one boot epoch for ``MqttLoop``: seqs, frame existence, empty runs, stable slots."""

    def __init__(self, targets: TargetFrames, epoch: Epoch):
        self.targets = targets
        self.iv = epoch.intervals
        self.i = 0
        self.cursor = self.iv[0][0] if self.iv else INF
        self.last_t_ms = None
        self.last_slots = {}
        self.frames = 0                 # frames drained (empty included)
        self.target_frames = 0

    def _assign(self, ms: int, tg: list) -> tuple:
        last = self.last_slots if self.last_t_ms == ms - FRAME_MS else {}
        slots = {}
        used = set()
        for e in tg:
            s = last.get(e[0])
            if s is not None:
                slots[e[0]] = s
                used.add(s)
        for e in tg:
            if e[0] not in slots:
                s = 0
                while s in used:
                    s += 1
                slots[e[0]] = s
                used.add(s)
        self.last_slots = slots
        self.last_t_ms = ms
        return tuple(sorted((slots[e[0]], e[1], e[2], e[3]) for e in tg))

    def drain(self, upto: int):
        iv = self.iv
        while self.i < len(iv):
            fs, fe, seq0 = iv[self.i]
            if self.cursor >= fe:
                self.i += 1
                if self.i < len(iv):
                    self.cursor = iv[self.i][0]
                continue
            if self.cursor > upto:
                return
            limit = min(upto, fe - 1)
            last = fs + ((limit - fs) // FRAME_MS) * FRAME_MS
            if last < self.cursor:
                return
            while True:
                nf = self.targets.next_frame(self.cursor)
                if nf is None or nf[0] > last:
                    break
                ms, tg = nf
                self.targets.pop()
                if ms > self.cursor:
                    cnt = (ms - self.cursor) // FRAME_MS
                    self.frames += cnt
                    yield ("e", seq0 + (self.cursor - fs) // FRAME_MS, cnt, self.cursor, ms - FRAME_MS)
                self.frames += 1
                self.target_frames += 1
                yield ("t", seq0 + (ms - fs) // FRAME_MS, ms, self._assign(ms, tg))
                self.cursor = ms + FRAME_MS
            if self.cursor <= last:
                cnt = (last - self.cursor) // FRAME_MS + 1
                self.frames += cnt
                yield ("e", seq0 + (self.cursor - fs) // FRAME_MS, cnt, self.cursor, last)
                self.cursor = last + FRAME_MS
            if self.cursor < fe:
                return

    def next_event_ms(self, after: int) -> int:
        best = INF
        for j in range(self.i, len(self.iv)):
            fs = self.iv[j][0]
            if fs > after:
                best = fs
                break
        nf = self.targets.next_frame(max(after + 1, self.cursor))
        if nf is not None and nf[0] < best:
            best = nf[0]
        return best


# ---------------------------------------------------------------- LD2412 OUT (11.3.3)
class Ld2412Out:
    """LD2412 OUT: 1 while occupied, except dropout runs; queried with non-decreasing times."""

    def __init__(self, seed: int, on_mean_s: float = LD_ON_MEAN_S, miss_mean_s: float = LD_MISS_MEAN_S,
                 start_s: float = BOOT_SIM_MS / 1000.0):
        self.rng = _rng(seed, 17)
        self.on_mean = on_mean_s
        self.miss_mean = miss_mean_s
        self.occ = []                 # merged [a, b) simulated seconds, not yet passed
        self.run_end = start_s + self.rng.expovariate(1.0 / on_mean_s)
        self.in_miss = False

    def add_occupancy(self, intervals) -> None:
        self.occ = [tuple(x) for x in W.union([tuple(x) for x in self.occ] + [(a, b) for a, b in intervals])]

    def _advance(self, s: float) -> None:
        while s >= self.run_end:
            self.in_miss = not self.in_miss
            mean = self.miss_mean if self.in_miss else self.on_mean
            self.run_end += self.rng.expovariate(1.0 / mean)

    def occupied(self, s: float) -> bool:
        occ = self.occ
        while occ and occ[0][1] <= s:
            occ.pop(0)
        return bool(occ) and occ[0][0] <= s

    def out(self, sim_ms: int) -> int:
        s = sim_ms / 1000.0
        self._advance(s)
        if self.in_miss:
            self.occupied(s)          # keep the occupancy pointer moving
            return 0
        return 1 if self.occupied(s) else 0


# ---------------------------------------------------------------- BH1750 lux (P4, OA 7.5)
class LuxModel:
    """The room's lux: lit (``LUX_BRIGHT``) except while the sleeper's lights are off.

    Dark runs from the true sleep onset (+ 0-4 min) to the next morning's wake (+ 0-5 min), also for onsets after
    00:00. Nothing is known before the first wake of day 0 (the run starts at 00:00 with the sleeper in bed), so
    no sample is made before it. Days are added in order (``add_day``); ``value`` is asked with growing times.

    An IDEAL model: the signal is derived from the truth (dark 0.5-2.5 lx exactly in those runs, 150 lx otherwise;
    no daytime dimming, no panel glow, no other light source), so what the replay measures with it (the lux onset
    errors of P4-R1, the start accuracy of P4-2) holds for an ideal sensor and says nothing about how the real
    BH1750 proxy tracks sleep: that is the P0 measurement (design 7.5), still open.
    """

    def __init__(self, seed: int):
        self.rng = _rng(seed, 31)
        self.val_rng = _rng(seed, 37)
        self.known_from = None          # sim ms of the first lights-on
        self.open_off = None
        self.intervals = deque()        # (off_ms, on_ms) of finished dark runs
        self.onsets = {}                # night (evening day) -> true onset, sim s (late ones included)
        self.wakes = {}                 # night -> true wake, sim s

    def add_day(self, rec) -> None:
        d = rec.d
        wake = onset = None
        for ag, place, mode, t0, t1, x, y in rec.sessions:
            if ag == "P1" and place == "bed" and mode == "sleep":
                if t0 == d * W.DAY:
                    wake = t1
                else:
                    onset = t0
        for ag, s0 in rec.late:
            if ag == "P1":
                onset = s0
        if wake is not None:
            self.wakes[d - 1] = wake
            on_ms = (wake + self.rng.randint(*LUX_ON_LEAD_S)) * 1000
            if self.open_off is not None:
                self.intervals.append((self.open_off, on_ms))
                self.open_off = None
            elif self.known_from is None:
                self.known_from = on_ms
        if onset is not None:
            self.onsets[d] = onset
            self.open_off = (onset + self.rng.randint(*LUX_OFF_LEAD_S)) * 1000

    def value(self, t_ms: int):
        """Lux at sim ms ``t_ms``, or None before the first known time."""
        if self.known_from is None or t_ms < self.known_from:
            return None
        iv = self.intervals
        while iv and iv[0][1] <= t_ms:
            iv.popleft()
        dark = (iv and iv[0][0] <= t_ms) or (not iv and self.open_off is not None and t_ms >= self.open_off)
        if dark:
            return round(self.val_rng.uniform(*LUX_DARK), 1)
        return round(LUX_BRIGHT * self.val_rng.uniform(0.9, 1.1), 1)


def _walk_points(rng: random.Random, p0, p1, t0: int) -> list:
    """The world's ``World.walk`` with an own random stream (the world's is not touched)."""
    v = rng.uniform(0.7, 1.2)
    d = math.dist(p0, p1)
    n = max(1, int(d / v))
    off = rng.uniform(-0.3, 0.3)
    nx, ny = (-(p1[1] - p0[1]) / (d or 1), (p1[0] - p0[0]) / (d or 1))
    pts = []
    for k in range(n + 1):
        f = k / n
        b = math.sin(math.pi * f) * off
        pts.append((t0 + k, p0[0] + (p1[0] - p0[0]) * f + nx * b + rng.gauss(0, 0.08),
                    p0[1] + (p1[1] - p0[1]) * f + ny * b + rng.gauss(0, 0.08)))
    return pts


def late_overlay(rec, onset_s: int, rng: random.Random) -> tuple:
    """Day ``rec`` after a night whose sleep onset ``onset_s`` (sim s) fell after 00:00 (the simulator dropped it):
    the sleeper is not in bed from 00:00 but walks in at the onset. Returns (tracks, occ): the day's tracks
    without the sleeper's points before the onset, plus the arrival walk and the settling points; the occupancy
    of the sleeper starts with the walk. ``rec`` is not changed. No random numbers of the world are used."""
    wake = pos = None
    for ag, place, mode, t0, t1, x, y in rec.sessions:
        if ag == "P1" and place == "bed" and mode == "sleep" and t0 == rec.d * W.DAY:
            wake, pos = t1, (x, y)
    if wake is None or onset_s >= wake:
        return rec.tracks, rec.occ
    tracks = []
    for tr in rec.tracks:
        if tr["agent"] == "P1" and tr["pts"] and tr["pts"][0][0] < onset_s:
            pts = [pt for pt in tr["pts"] if pt[0] >= onset_s]
            if pts:
                tracks.append(dict(tr, pts=pts))
        else:
            tracks.append(tr)
    src = rng.choice(list(W.DOORS.values()))
    start = onset_s - max(1, int(math.dist(src, pos) / 0.9))
    pts = _walk_points(rng, src, pos, start)
    settle = rng.randint(5, 20)
    pts += [(onset_s + j, pos[0] + rng.gauss(0, 0.07), pos[1] + rng.gauss(0, 0.07)) for j in range(1, settle)]
    tracks.append({"pts": sorted(pts), "kind": "human", "agent": "P1"})
    occ = []
    for a, b, ag in rec.occ:
        if ag == "P1" and a == rec.d * W.DAY:
            a = start
        occ.append((a, b, ag))
    return tracks, occ


def extra_tracks(rng: random.Random, extras) -> list:
    """Scripted people ``(agent, x, y, t0_s, t1_s)``: one point a second, 7 cm noise, always visible."""
    out = []
    for ag, x, y, t0, t1 in extras:
        out.append({"pts": [(t, x + rng.gauss(0, EXTRA_SIGMA_M), y + rng.gauss(0, EXTRA_SIGMA_M))
                            for t in range(int(t0), int(t1))], "kind": "human", "agent": ag})
    return out


# ---------------------------------------------------------------- P7 W5: the empty-room switch-off
EMPTY_OFF_LEAD_S = 600


def empty_off_intervals(seed: int, cfg: dict, horizon_days: int) -> list:
    """The radar-off intervals ``[a + 600 s, b)`` (simulated seconds) of every home-empty interval ``(a, b)`` of the world of
    ``(seed, cfg)`` over days 0..``horizon_days`` (P7 W5, DV4). The world is generated again from its own seed, day by day, and
    each day is dropped after its intervals were read, so this costs the world's generation time and no memory."""
    rw = W.RunWorld(seed, cfg)
    out = []
    for d in range(horizon_days + 1):
        rec = rw.day(d)
        for a, b in W.union([tuple(x) for x in rec.he]):
            if b - (a + EMPTY_OFF_LEAD_S) > 0:
                out.append((a + EMPTY_OFF_LEAD_S, b))
        rw.forget_before(d)
    return out


# ---------------------------------------------------------------- the run's event stream
class RunStream:
    """All non-interactive input events of one run, in reception order (frames, home_empty, node status).

    ``world`` is the run's ``RunWorld``; register further day consumers on ``world.on_day`` before
    iterating. ``events()`` yields ``(recv_ms, order, event)``.
    """

    def __init__(self, cfg: dict, seed: int, horizon_days: int, faults: bool = False,
                 noise_m: float = NOISE_M, p_miss: float = P_MISS, lux: bool = False, extras=(),
                 quiet_from_day: Optional[int] = None, quiet_keep=(), scripted=None, gaps=(), rot=None,
                 empty_off: bool = False):
        self.cfg = cfg
        self.seed = seed
        self.world = W.RunWorld(seed, cfg)
        # P7 W5: the radars are off from 10 minutes into every home-empty interval of the world to its end (the user's own
        # automation, DV4); the intervals come from a pre-pass over the same world (generated again, day by day, and dropped)
        self.empty_off = empty_off_intervals(seed, cfg, horizon_days) if empty_off else []
        self.timeline = Timeline(cfg, seed, horizon_days, faults, extra_offs=self.empty_off)
        self.targets = TargetFrames(seed, noise_m, p_miss, rot=rot)
        self.ld = Ld2412Out(seed)
        self.rng_lat = _rng(seed, 13)
        self.rng_fault = _rng(seed, 19)
        self.faults = faults
        self.horizon_days = horizon_days
        self.ctx_pending = []          # (recv_ms, ContextState)
        self.world.on_day.append(self._on_day)
        self.loops = [MqttLoop(e.segments, EpochFrames(self.targets, e), self._header, e.boot_id, e.boot, e.stop_ms)
                      for e in self.timeline.epochs]
        self.last_recv = BASE_MS + BOOT_SIM_MS
        self.last_pub_recv = None
        self.dup_at = {}
        self.stats = {"messages": 0, "delivered": 0, "lost_fault": 0, "duplicated": 0, "lost_window": 0,
                      "pre_base": 0}
        self.node_events = []          # (recv_ms, NodeStatus) from faults
        # P4: lux samples, the sleep onset after 00:00 that the simulator drops, scripted extra people
        self.lux = LuxModel(seed) if lux else None
        self.rng_lux = _rng(seed, 29)
        self.lux_phase = _rng(seed, 41).randrange(LUX_PERIOD_MS)
        self.rng_late = _rng(seed, 43)
        self.late_pending = None
        self.late_nights = []          # nights (evening day) whose onset was after 00:00
        self.extras = list(extras)
        self.rng_extra = _rng(seed, 47)
        self._extras_done = False
        # P6: quiet days, scripted people, scripted drop windows and what was dropped (all empty for a P3-P5 job)
        self.quiet_from_day = quiet_from_day
        self.quiet_keep = tuple(quiet_keep)
        self.scripted = scripted
        self.gap_windows = [(int(a) * 1000, (int(a) + int(d)) * 1000) for a, d in gaps]
        self.dropped = []              # (t_sim_ms, cause) of every dropped message: random, window, gap
        self.spans = []                # [a_ms, b_ms, causes, n]: runs of dropped messages with the delivered ones around them
        self._last_delivered = None
        self._open_span = None

    # day consumer: targets, LD2412 occupancy, home_empty edges
    def _on_day(self, rec) -> None:
        tracks, occ = rec.tracks, rec.occ
        if self.late_pending is not None:
            tracks, occ = late_overlay(rec, self.late_pending, self.rng_late)
            self.late_pending = None
        for ag, s0 in rec.late:
            if ag == "P1":
                self.late_pending = s0
                self.late_nights.append(rec.d)
        if self.lux is not None:
            self.lux.add_day(rec)
        if self.quiet_from_day is not None and rec.d >= self.quiet_from_day:     # P6: no world agents, only the reflector
            keep = ("refl",) + self.quiet_keep
            tracks = [t for t in tracks if t["agent"] in keep]
            occ = [o for o in occ if o[2] in self.quiet_keep]
        if self.extras and not self._extras_done:
            self._extras_done = True
            self.targets.add_tracks(rec.d, extra_tracks(self.rng_extra, self.extras))
            # an extra named Y... is not fed to the LD2412 model (P6-9: a label stayer at the reflector)
            self.ld.add_occupancy([(t0, t1) for ag, _, _, t0, t1 in self.extras if not ag.startswith("Y")])
        self.targets.add_tracks(rec.d, tracks)
        self.ld.add_occupancy([(a, b) for a, b, _ in occ])
        if self.scripted is not None:
            sc_tracks, sc_occ = self.scripted.day(rec.d, _rng(self.seed, 1000 + rec.d))
            if sc_tracks:
                self.targets.add_tracks(rec.d, sc_tracks)
            if sc_occ:
                self.ld.add_occupancy(sc_occ)
        for a, b in W.union([tuple(x) for x in rec.he]):
            self.ctx_pending.append((BASE_MS + a * 1000, ContextState("home_empty", True, BASE_MS + a * 1000)))
            self.ctx_pending.append((BASE_MS + b * 1000, ContextState("home_empty", False, BASE_MS + b * 1000)))
        if self.faults:
            self.dup_at[rec.d] = int((rec.d * W.DAY + self.rng_fault.uniform(0, W.DAY)) * 1000)

    def _header(self, phase: str, t: int) -> tuple:
        if phase == ON:
            return (1, self.ld.out(t), 3, "A")
        if phase == OFF:
            return (None, None, 0, "N")
        return (None, None, 0, "A")

    def _deliver(self, t_sim: int, payload: str, out: list) -> None:
        self.stats["messages"] += 1
        lat = self.rng_lat.randint(LAT_MIN_MS, LAT_MAX_MS)
        recv = BASE_MS + t_sim + lat
        if recv < self.last_recv:
            recv = self.last_recv
        self.last_recv = recv
        self.last_pub_recv = recv
        if self.faults:
            if self.rng_fault.random() < FAULT_LOSS_P:
                self.stats["lost_fault"] += 1
                self._dropped(t_sim, "random")
                return
            loss = self.timeline.loss
            if loss is not None and loss[0] <= t_sim < loss[1]:
                self.stats["lost_window"] += 1
                self._dropped(t_sim, "window")
                return
        for ga, gb in self.gap_windows:                # P6: a scripted drop window, applied like the loss window
            if ga <= t_sim < gb:
                self.stats["lost_gap"] = self.stats.get("lost_gap", 0) + 1
                self._dropped(t_sim, "gap")
                return
        if recv < BASE_MS:
            self.stats["pre_base"] += 1
            return
        self._delivered(t_sim)
        data = payload.encode("ascii")
        out.append((recv, 1, FramesMessage(data, recv)))
        self.stats["delivered"] += 1
        if self.faults:
            d = t_sim // DAY_MS
            at = self.dup_at.get(d)
            if at is not None and t_sim >= at:
                del self.dup_at[d]
                out.append((recv + 1, 1, FramesMessage(data, recv + 1)))
                self.stats["duplicated"] += 1

    def _dropped(self, t_sim: int, cause: str) -> None:
        """Records a dropped message (P6-4): the message itself and the run of drops it belongs to."""
        self.dropped.append((t_sim, cause))
        sp = self._open_span
        if sp is None:
            sp = self._open_span = [self._last_delivered, t_sim, {cause}, 0]
        sp[2].add(cause)
        sp[3] += 1

    def _delivered(self, t_sim: int) -> None:
        sp = self._open_span
        if sp is not None:                       # the first delivered message after a run of drops closes it
            self.spans.append([sp[0], t_sim, "+".join(sorted(sp[2])), sp[3]])
            self._open_span = None
        self._last_delivered = t_sim

    def _lux_events(self, a: int, b: int) -> list:
        """LuxSample events for the sim ms range [a, b) (the sensors/state publishes every 10 s)."""
        out = []
        per = LUX_PERIOD_MS
        k = max(0, -((self.lux_phase - a) // per))            # first k with phase + k * per >= a
        t = self.lux_phase + k * per
        while t < b:
            if t >= 0 and not (self.faults and self.timeline.phase_at(t) is None):
                v = self.lux.value(t)
                if v is not None:
                    recv = BASE_MS + t + self.rng_lux.randint(LAT_MIN_MS, LAT_MAX_MS)
                    out.append((recv, 0, LuxSample(v, recv)))
            t += per
        return out

    def events(self) -> Iterator[tuple]:
        carry = []
        chunk = BOOT_SIM_MS
        end = self.timeline.end
        loops = list(self.loops)
        reboot = self.timeline.reboot
        while chunk < end:
            c1 = min(end, (chunk // CHUNK_MS + 1) * CHUNK_MS)
            self.world.ensure(max(0, (c1 - 1) // DAY_MS) + 1)
            self.world.forget_before((c1 - 1) // DAY_MS - 1)
            out = carry
            carry = []
            for k, lp in enumerate(loops):
                if lp.stop_ms is not None and chunk >= lp.stop_ms:
                    continue
                if k > 0 and c1 <= self.timeline.epochs[k].start:
                    continue
                for t_sim, payload in lp.run(c1):
                    self._deliver(t_sim, payload, out)
                if reboot is not None and lp.stop_ms == reboot and chunk < reboot <= c1:
                    restart = reboot + FAULT_REBOOT_DOWN_MS
                    last = self.last_pub_recv if self.last_pub_recv is not None else BASE_MS + reboot
                    off_at = min(last + FAULT_WILL_MS, BASE_MS + restart)
                    self.node_events.append((off_at, NodeStatus(False, off_at)))
                    self.node_events.append((BASE_MS + restart, NodeStatus(True, BASE_MS + restart)))
            ctx, self.ctx_pending = self.ctx_pending, []
            for e in ctx:
                out.append((e[0], 0, e[1]))
            if self.lux is not None:
                out.extend(self._lux_events(chunk, c1))
            for e in self.node_events:
                out.append((e[0], 0, e[1]))
            self.node_events = []
            out.sort(key=lambda e: (e[0], e[1]))
            limit = BASE_MS + c1
            for e in out:
                if e[0] >= limit:
                    carry.append(e)
                else:
                    yield e
            chunk = c1
        carry.sort(key=lambda e: (e[0], e[1]))
        for e in carry:
            yield e
