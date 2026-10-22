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

from radar_zone_learner.core.types import ContextState, FramesMessage, NodeStatus

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

    def __init__(self, cfg: dict, seed: int, horizon_days: int, faults: bool = False):
        offs = []
        for d in range(horizon_days + 1):
            offs.extend(W.night_window_off(d, cfg))
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

    def __init__(self, seed: int, noise_m: float = NOISE_M, p_miss: float = P_MISS):
        self.heap = []
        self.serial = 0
        self.noise_m = noise_m
        self.p_miss = p_miss
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
                    v = int(round((hyp(x + dx * f1, y + dy * f1) - hyp(xi, yi)) * 1000.0))
                else:
                    xi, yi, v = x, y, 0
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


# ---------------------------------------------------------------- the run's event stream
class RunStream:
    """All non-interactive input events of one run, in reception order (frames, home_empty, node status).

    ``world`` is the run's ``RunWorld``; register further day consumers on ``world.on_day`` before
    iterating. ``events()`` yields ``(recv_ms, order, event)``.
    """

    def __init__(self, cfg: dict, seed: int, horizon_days: int, faults: bool = False,
                 noise_m: float = NOISE_M, p_miss: float = P_MISS):
        self.cfg = cfg
        self.seed = seed
        self.world = W.RunWorld(seed, cfg)
        self.timeline = Timeline(cfg, seed, horizon_days, faults)
        self.targets = TargetFrames(seed, noise_m, p_miss)
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

    # day consumer: targets, LD2412 occupancy, home_empty edges
    def _on_day(self, rec) -> None:
        self.targets.add_tracks(rec.d, rec.tracks)
        self.ld.add_occupancy([(a, b) for a, b, _ in rec.occ])
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
                return
            loss = self.timeline.loss
            if loss is not None and loss[0] <= t_sim < loss[1]:
                self.stats["lost_window"] += 1
                return
        if recv < BASE_MS:
            self.stats["pre_base"] += 1
            return
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
