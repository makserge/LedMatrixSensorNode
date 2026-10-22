"""Ingest bookkeeping (P3 spec 6.3-6.7, 7.1): boot epochs, seq handling, frame times, the wall clock,
the two observation classes with censoring, coverage, and the LD2412 and home_empty timelines.

Observation is a pure state machine. ingest() judges a batch before it changes anything (boot id, seq,
time: see the constants below), so a rejected batch leaves no trace. ingest(), node_status() and tick()
return a list of actions, which the learner executes in order:
- Censor(at_dev, at_wall, reason): end every live track as censored and commit every pending stay;
- TrackFrames(frames, end_dev, start_dev, offset): give the listed frames to the tracker, then expire
  at end_dev (the batch's last frame); the frontier is then end_dev.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
from datetime import date
from typing import Optional

from .params import Params
from .types import FramesBatch, TimeModel

OBSERVING_SM = frozenset(("A", "F", "S"))
TIMELINE_KEEP_MS = 48 * 3600 * 1000

# Ingest hardening. The broker has no ACLs, so any client can publish a well-formed frames payload; a
# forged boot id, seq or t must not blind or derail the learner. Each limit is far outside what the P1
# node does (10 Hz frames, batches of at most 500 ms or a 5 s heartbeat, a reboot takes 10 s or more).
FOREIGN_BOOT_BATCHES = 3        # a boot id other than the epoch's needs this many batches in a row ...
                                # ... or an epoch silent for silence_censor_ms, to open a new epoch
SEQ_RATE_MAX_HZ = 20            # a forward seq jump may not exceed elapsed receive time x this ...
SEQ_JUMP_SLACK = 1000           # ... plus this many frames (a lost-message burst, a broker pause)
SEQ_RESYNC_BATCHES = 10         # rejected batches that continue one another: the stream is real, resync
TIME_TOL_MS = 10000             # recv - t below the window minimum by more than this: device ahead of host
TIME_RESYNC_BATCHES = 3         # ... that many such batches in a row: the host clock really stepped
MIN_BOOT_WALL_MS = 1577836800000    # 2020-01-01: no node booted earlier; also guards a host clock not yet set
U32 = 0xFFFFFFFF

COUNTER_KEYS = ("batches", "frames", "frames_lost", "seq_gaps", "duplicates", "overlaps", "parse_errors",
                "stale_boot", "boots", "compressed", "inconsistent_pw_sm", "censor_events",
                "foreign_boot", "bad_seq", "bad_time", "seq_resyncs", "clock_steps")


def seq_diff(a: int, b: int) -> int:
    """Signed distance a - b of two uint32 sequence numbers (positive: a is ahead of b)."""
    d = (a - b) & U32
    return d - 0x100000000 if d >= 0x80000000 else d


class Censor:
    __slots__ = ("at_dev", "at_wall", "reason")

    def __init__(self, at_dev: int, at_wall: int, reason: str) -> None:
        self.at_dev = at_dev
        self.at_wall = at_wall
        self.reason = reason

    def __repr__(self) -> str:
        return "Censor(%r, %r, %r)" % (self.at_dev, self.at_wall, self.reason)


class TrackFrames:
    __slots__ = ("frames", "end_dev", "start_dev", "offset")

    def __init__(self, frames: list, end_dev: int, start_dev: int, offset: int) -> None:
        self.frames = frames          # [(dev_ms, targets tuple)], listed frames in seq order
        self.end_dev = end_dev        # device time of the batch's last covered frame
        self.start_dev = start_dev    # observing_start_dev_ms of the current observed chain
        self.offset = offset          # wall = dev + offset

    def __repr__(self) -> str:
        return "TrackFrames(%d frames, end %r)" % (len(self.frames), self.end_dev)


class Epoch:
    """One boot id: seq bookkeeping, the frontier and the wall-clock offset window."""

    __slots__ = ("boot", "next_seq", "frontier_dev", "frontier_observed", "observing_start_dev",
                 "offsets", "prev_ld_t", "last_recv_ms", "rej", "low")

    def __init__(self, boot: str, next_seq: int) -> None:
        self.boot = boot
        self.next_seq = next_seq
        self.frontier_dev: Optional[int] = None
        self.frontier_observed = False
        self.observing_start_dev: Optional[int] = None
        self.offsets: deque = deque()     # monotonic deque of (recv_ms, recv - t) for the sliding minimum
        self.prev_ld_t: Optional[int] = None
        self.last_recv_ms: Optional[int] = None   # receive time of the last accepted batch
        self.rej: Optional[tuple] = None  # (next expected s, count) of the run of rejected seq batches
        self.low: Optional[tuple] = None  # (count, recv - t) of the run of batches with t ahead of the host

    def add_offset(self, recv_ms: int, value: int, window_ms: int) -> Optional[int]:
        """Adds a sample of the sliding minimum. Returns the previous offset when the minimum moved by
        more than TIME_TOL_MS (the host clock stepped forward and the old samples left the window)."""
        old = self.offset
        q = self.offsets
        while q and q[-1][1] >= value:
            q.pop()
        q.append((recv_ms, value))
        lo = recv_ms - window_ms
        while len(q) > 1 and q[0][0] < lo:
            q.popleft()
        new = q[0][1]
        return old if old is not None and abs(new - old) > TIME_TOL_MS else None

    @property
    def offset(self) -> Optional[int]:
        return self.offsets[0][1] if self.offsets else None


def frame_time(batch: FramesBatch, k: int) -> int:
    """Device time of frame k of the batch, listed or not (spec 6.4)."""
    if batch.n <= 1:
        return batch.t_ms - batch.a0
    return batch.t_ms - int(round(batch.a0 - (batch.a0 - batch.a1) * k / (batch.n - 1)))


class Timeline:
    """Merged runs (start_ms, end_ms, value) of a boolean signal; time outside the runs is unknown."""

    __slots__ = ("starts", "ends", "values")

    def __init__(self) -> None:
        self.starts: list = []
        self.ends: list = []
        self.values: list = []

    def add(self, a: int, b: int, value: bool) -> None:
        if self.ends and a < self.ends[-1]:
            a = self.ends[-1]
        if b <= a:
            return
        if self.ends and self.ends[-1] == a and self.values[-1] == value:
            self.ends[-1] = b
            return
        self.starts.append(a)
        self.ends.append(b)
        self.values.append(value)

    def prune(self, before_ms: int) -> None:
        k = 0
        while k < len(self.ends) and self.ends[k] < before_ms:
            k += 1
        if k:
            del self.starts[:k], self.ends[:k], self.values[:k]

    def seconds(self, a: int, b: int, open_end: Optional[tuple] = None) -> tuple:
        """(known ms, true ms) of [a, b]. open_end = (start_ms, value) of a run that is still open."""
        known = 0
        true = 0
        i = bisect_right(self.ends, a)
        n = len(self.ends)
        while i < n:
            s = self.starts[i]
            if s >= b:
                break
            ov = min(b, self.ends[i]) - max(a, s)
            if ov > 0:
                known += ov
                if self.values[i]:
                    true += ov
            i += 1
        if open_end is not None and open_end[1] is not None:
            s = open_end[0]
            ov = b - max(a, s)
            if ov > 0:
                known += ov
                if open_end[1]:
                    true += ov
        return known, true


class Observation:
    def __init__(self, p: Params, time_model: TimeModel) -> None:
        self.p = p
        self.tm = time_model
        self.counters = {k: 0 for k in COUNTER_KEYS}
        self.epoch: Optional[Epoch] = None
        self.retired: list = []
        self._foreign: Optional[tuple] = None         # (boot id, count) of the batches of a foreign boot id
        self.node_online: Optional[bool] = None
        self.last_frames_ms: Optional[int] = None    # core now at the last accepted frames message
        self.coverage: dict = {}                      # date ordinal -> 24 observed seconds
        self._hour: Optional[tuple] = None            # cache: (start_ms, end_ms, row, hour, date) of a local hour
        self.obs_s_since_extraction = 0.0
        self.ld = Timeline()
        self.he = Timeline()
        self.he_open: Optional[tuple] = None          # (start_ms, value) of the current home_empty run
        self.censor_walls: list = []                  # wall ms of recent censoring events, ascending

    # ------------------------------------------------------------ wall clock
    def wall(self, dev_ms: int) -> int:
        ep = self.epoch
        off = ep.offset if ep is not None else None
        return dev_ms + (off if off is not None else 0)

    @property
    def observing(self) -> bool:
        """True while the current observed chain is open (the latest frames were OBSERVED)."""
        return self.epoch is not None and self.epoch.frontier_observed

    @property
    def boot_id(self) -> Optional[str]:
        return self.epoch.boot if self.epoch is not None else None

    # ------------------------------------------------------------ censoring
    def _censor(self, reason: str, offset: Optional[int] = None) -> Censor:
        """Ends the observed chain at the frontier. ``offset``: the wall offset to use instead of the
        epoch's current one (the offset the frontier's wall time was booked with, after a clock step)."""
        ep = self.epoch
        at_dev = ep.frontier_dev
        at_wall = self.wall(at_dev) if offset is None else at_dev + offset
        ep.frontier_observed = False
        ep.prev_ld_t = None
        self.counters["censor_events"] += 1
        cw = self.censor_walls
        if not cw or at_wall >= cw[-1]:
            cw.append(at_wall)
        else:
            cw.insert(bisect_right(cw, at_wall), at_wall)
        return Censor(at_dev, at_wall, reason)

    def censor_between(self, a_ms: int, b_ms: int) -> bool:
        """True if a censoring event happened strictly inside (a, b) (wall ms)."""
        cw = self.censor_walls
        i = bisect_right(cw, a_ms)
        return i < len(cw) and cw[i] < b_ms

    # ------------------------------------------------------------ frames
    def _open_epoch(self, b: FramesBatch, recv_ms: int) -> bool:
        """True when a batch with a boot id other than the epoch's (or without an epoch) opens a new one.
        A real reboot leaves the epoch silent for silence_censor_ms or sends its new id on and on; a single
        forged batch does neither and is only counted."""
        ep = self.epoch
        if ep is None:
            return True
        f = self._foreign
        n = f[1] + 1 if f is not None and f[0] == b.boot else 1
        self._foreign = (b.boot, n)
        silent = ep.last_recv_ms is not None and recv_ms - ep.last_recv_ms >= self.p.silence_censor_ms
        if silent or n >= FOREIGN_BOOT_BATCHES:
            self._foreign = None
            return True
        self.counters["stale_boot" if b.boot in self.retired else "foreign_boot"] += 1
        return False

    def ingest(self, b: FramesBatch, recv_ms: int, now_ms: int) -> list:
        p = self.p
        c = self.counters
        acts = []
        lat = recv_ms - b.t_ms                # the node's boot time on the host clock, plus the latency
        if lat < MIN_BOOT_WALL_MS:
            c["bad_time"] += 1                # t from a future that cannot be, or a host clock not set yet
            return acts
        ep = self.epoch
        if ep is not None and b.boot == ep.boot:
            self._foreign = None
        else:
            if not self._open_epoch(b, recv_ms):
                return acts
            if ep is not None:
                if ep.frontier_observed:
                    acts.append(self._censor("reboot"))
                self.retired.append(ep.boot)
                if len(self.retired) > p.boot_history:
                    del self.retired[:len(self.retired) - p.boot_history]
            if b.boot in self.retired:
                self.retired.remove(b.boot)
            c["boots"] += 1
            ep = self.epoch = Epoch(b.boot, b.s)

        # Seq (6.3, modulo 2**32), judged without changing anything. A batch that is a duplicate or
        # jumps ahead by more frames than the node can have produced is rejected; a run of such batches
        # that continue one another is the real stream and ends the rejection (resync).
        d = seq_diff(b.s, ep.next_seq)        # > 0: ahead of the next expected frame, < 0: behind it
        bad = None
        if b.n > 0 and d + b.n <= 0:
            bad = "duplicates"
        elif d > 0 and ep.last_recv_ms is not None and \
                d > max(0, recv_ms - ep.last_recv_ms) * SEQ_RATE_MAX_HZ // 1000 + SEQ_JUMP_SLACK:
            bad = "bad_seq"
        resync = False
        if bad is not None:
            r = ep.rej
            cnt = r[1] + 1 if r is not None and r[0] == b.s else 1
            ep.rej = ((b.s + b.n) & U32, cnt)
            if cnt < SEQ_RESYNC_BATCHES:
                c[bad] += 1
                return acts
            resync = True
            d = 0

        # Time: the device clock cannot run ahead of the broker's arrival times. A batch whose recv - t is
        # far below the window minimum is rejected, unless several in a row agree (the host clock stepped).
        off = ep.offset
        step = False
        if off is not None and lat < off - TIME_TOL_MS:
            lo = ep.low
            cnt = lo[0] + 1 if lo is not None and abs(lat - lo[1]) <= TIME_TOL_MS else 1
            ep.low = (cnt, lat)
            if cnt < TIME_RESYNC_BATCHES:
                c["bad_time"] += 1
                return acts
            step = True
        else:
            ep.low = None

        # accepted: from here on the state changes
        ep.rej = None
        ep.low = None
        if step:
            c["clock_steps"] += 1
            if ep.frontier_observed:
                acts.append(self._censor("clock_step"))
            ep.offsets.clear()
        if resync:
            c["seq_resyncs"] += 1
            if ep.frontier_observed:
                acts.append(self._censor("seq_resync"))
            ep.next_seq = b.s
        k0 = 0
        end = d + b.n                         # the batch's end relative to the next expected frame
        if d < 0 < end:
            c["overlaps"] += 1
            k0 = -d
        elif d > 0:
            c["seq_gaps"] += 1
            c["frames_lost"] += d
            if ep.frontier_observed:
                acts.append(self._censor("seq_gap"))
        new_frames = b.n if d > 0 else max(0, end)
        if d > 0 or end > 0:
            ep.next_seq = (b.s + b.n) & U32
        c["batches"] += 1
        if new_frames > 0:
            c["frames"] += new_frames
        self.last_frames_ms = now_ms
        ep.last_recv_ms = recv_ms
        jumped = ep.add_offset(recv_ms, lat, p.offset_window_ms)
        if jumped is not None:
            # the window minimum moved: the host clock stepped forward some time ago; wall times before
            # this batch were booked with the old offset, so the chain ends there
            c["clock_steps"] += 1
            if ep.frontier_observed:
                acts.append(self._censor("clock_step", jumped))

        # observation class (OA 7.2, spec 6.5)
        reason = None
        compressed = b.n >= 5 and b.a0 - b.a1 < (b.n - 1) * p.compressed_spacing_ms
        if compressed:
            c["compressed"] += 1
        if (b.pw & 1) and b.sm not in OBSERVING_SM:
            c["inconsistent_pw_sm"] += 1
        if not b.pw & 1:
            reason = "pw"
        elif b.sm not in OBSERVING_SM:
            reason = "sm"
        elif b.mt is not None and b.mt != 1:
            reason = "mt"
        elif self.node_online is False:
            reason = "offline"
        elif compressed:
            reason = "compressed"
        if reason is not None and ep.frontier_observed:
            acts.append(self._censor(reason))

        # LD2412 timeline (spec 6.6)
        if (b.pw & 2) and b.p2 is not None:
            if ep.prev_ld_t is not None:
                a = self.wall(ep.prev_ld_t)
            else:
                a = self.wall(b.t_ms - b.a0) if b.n > 0 else self.wall(b.t_ms)
            self.ld.add(a, self.wall(b.t_ms), bool(b.p2))
            ep.prev_ld_t = b.t_ms
        else:
            ep.prev_ld_t = None

        if reason is not None:
            return acts
        if b.n == 0:
            if ep.frontier_observed and b.t_ms - ep.frontier_dev >= p.obs_gap_ms:
                acts.append(self._censor("gap"))
            return acts
        first = frame_time(b, k0)
        last = b.t_ms - b.a1
        span_from = first
        if ep.frontier_observed:
            if first - ep.frontier_dev >= p.obs_gap_ms:
                acts.append(self._censor("gap"))
            else:
                span_from = ep.frontier_dev          # contiguous: observed from the frontier on
        if not ep.frontier_observed:
            ep.observing_start_dev = first
        self._add_observed(span_from, last)
        if k0:
            frames = [(b.t_ms - lf.age_ms, lf.targets) for lf in b.frames if lf.k >= k0]
        else:
            frames = [(b.t_ms - lf.age_ms, lf.targets) for lf in b.frames]
        if ep.frontier_dev is None or last > ep.frontier_dev or not ep.frontier_observed:
            ep.frontier_dev = last
        ep.frontier_observed = True
        acts.append(TrackFrames(frames, ep.frontier_dev, ep.observing_start_dev, ep.offset))
        return acts

    def node_status(self, online: bool) -> list:
        self.node_online = online
        if not online and self.observing:
            return [self._censor("offline")]
        return []

    def tick(self, now_ms: int) -> list:
        acts = []
        if self.observing:
            if self.last_frames_ms is not None and now_ms - self.last_frames_ms >= self.p.silence_censor_ms:
                acts.append(self._censor("silence"))
            elif self.node_online is False:
                acts.append(self._censor("offline"))
        lo = now_ms - TIMELINE_KEEP_MS
        self.ld.prune(lo)
        self.he.prune(lo)
        cw = self.censor_walls
        if cw and cw[0] < lo:
            del cw[:bisect_right(cw, lo)]
        return acts

    # ------------------------------------------------------------ coverage (spec 7.1)
    def _add_observed(self, a_dev: int, b_dev: int) -> None:
        if b_dev <= a_dev:
            return
        self.add_coverage(self.wall(a_dev), self.wall(b_dev))

    def add_coverage(self, a: int, b: int) -> None:
        """Add the wall span [a, b] ms, split at local hour boundaries."""
        cov = self.coverage
        while a < b:
            hc = self._hour
            if hc is not None and hc[0] <= a < hc[1] and hc[2] is cov.get(hc[4]):
                lo, boundary, row, h = hc[0], hc[1], hc[2], hc[3]
            else:
                lt = self.tm.local(a)
                lo = a - (lt.sec_of_day % 3600) * 1000 - (a % 1000)
                boundary = lo + 3600000
                row = cov.get(lt.date_ord)
                if row is None:
                    row = cov[lt.date_ord] = [0.0] * 24
                    self._prune_coverage(lt.date_ord)
                h = lt.sec_of_day // 3600
                self._hour = (lo, boundary, row, h, lt.date_ord)
            e = min(b, boundary)
            sec = (e - a) / 1000.0
            row[h] += sec
            self.obs_s_since_extraction += sec
            a = e

    def _prune_coverage(self, newest: int) -> None:
        keep = self.p.coverage_days
        lo = max(self.coverage) - keep + 1 if self.coverage else newest
        for d in [d for d in self.coverage if d < lo]:
            del self.coverage[d]

    def covered_dates(self, before_ord: int) -> list:
        """Covered local dates (>= covered_min_s observed) strictly before before_ord, ascending."""
        m = self.p.covered_min_s
        return sorted(d for d, row in self.coverage.items() if d < before_ord and sum(row) >= m)

    def observed_s_last_24h(self, now_ms: int) -> float:
        lt = self.tm.local(now_ms)
        slot = lt.date_ord * 24 + lt.sec_of_day // 3600
        tot = 0.0
        for k in range(24):
            s = slot - k
            row = self.coverage.get(s // 24)
            if row is not None:
                tot += row[s % 24]
        return tot

    def o_obs(self) -> list:
        """O_obs[168]: observed seconds by (weekday, hour) over the last 28 dates (API for P4)."""
        out = [0.0] * 168
        if not self.coverage:
            return out
        newest = max(self.coverage)
        for d, row in self.coverage.items():
            if d > newest - 28:
                wd = (d - 1) % 7
                for h in range(24):
                    out[wd * 24 + h] += row[h]
        return out

    def coverage_export(self) -> dict:
        return {date.fromordinal(d).isoformat(): [round(v, 1) for v in self.coverage[d]]
                for d in sorted(self.coverage)}

    # ------------------------------------------------------------ timelines (spec 6.6)
    def ld_share_opt(self, a_ms: int, b_ms: int) -> Optional[float]:
        """Present share of the known LD2412 time in [a, b]; None when less than half of [a, b] is known
        (the LD2412 was not observing: unknown is neither present nor absent, spec 6.6, D3)."""
        if b_ms <= a_ms:
            return 1.0
        known, true = self.ld.seconds(a_ms, b_ms)
        if known <= 0 or known < 0.5 * (b_ms - a_ms):
            return None
        return true / known

    def ld_share(self, a_ms: int, b_ms: int) -> float:
        """ld_share_opt with unknown counted as 0 (never adds support: bridging, the stay's ld value)."""
        v = self.ld_share_opt(a_ms, b_ms)
        return 0.0 if v is None else v

    def set_context(self, value: Optional[bool], now_ms: int) -> None:
        if self.he_open is not None:
            s, v = self.he_open
            if v is not None:
                self.he.add(s, now_ms, v)
        self.he_open = (now_ms, value)

    def he_share(self, a_ms: int, b_ms: int) -> float:
        if b_ms <= a_ms:
            return 0.0
        _, true = self.he.seconds(a_ms, b_ms, self.he_open)
        return true / (b_ms - a_ms)
