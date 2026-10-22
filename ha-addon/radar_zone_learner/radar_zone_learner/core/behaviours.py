"""Behaviours of confirmed tracks (P3 spec 7.3-7.5; OA 7.3 step 2).

Per track: 1 Hz samples -> walking points, birth class, stay fragments (the online form of the
project simulator's offline loop, SIM:465-485, with identical results), EPP ghosts, exit class.
Across tracks: fragments -> COVIS, then stays with dropout bridging and commit (SIM:487-524 with the
design's LD2412 bridging rule), ghost and clutter evidence, door events, grid splats.

Times are wall seconds (float). Persisted records are rounded as OA 10 asks (spec 7.5).

Bounds for noisy input (a static ghost can live for days, a noisy sensor can fire all day): a fragment
buffer longer than FOLD_N samples is folded into running sums (the fragment is still emitted at its break
or death, with the same values up to rounding), and the persisted event lists keep at most MAX_STAYS,
MAX_DOOR_EVENTS and MAX_GHOST_EVENTS entries, the newest.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import date
from typing import Callable, Optional

from .params import Params


FOLD_N = 512                    # samples (1 Hz) after which a fragment buffer is folded into sums
MAX_STAYS = 20000
MAX_DOOR_EVENTS = 5000
MAX_GHOST_EVENTS = 5000


def rnd(v: float) -> int:
    """Round half up (deterministic, no banker's rounding)."""
    return int(math.floor(v + 0.5))


class Fragment:
    __slots__ = ("cx", "cy", "sd", "n", "t0", "t1", "start_index", "track_id", "end", "open_start",
                 "open_end", "death_class")

    def __init__(self, cx, cy, sd, n, t0, t1, start_index, track_id, end, open_start, open_end, death_class):
        self.cx = cx
        self.cy = cy
        self.sd = sd
        self.n = n
        self.t0 = t0
        self.t1 = t1
        self.start_index = start_index
        self.track_id = track_id
        self.end = end                  # "break" | "death" | "censor"
        self.open_start = open_start
        self.open_end = open_end
        self.death_class = death_class  # "moving" | "still" | None (only for end == "death")

    def __repr__(self) -> str:
        return "Fragment(%.2f,%.2f n=%d %.0f-%.0f %s os=%s oe=%s)" % (
            self.cx, self.cy, self.n, self.t0, self.t1, self.end, self.open_start, self.open_end)


class PendingStay:
    __slots__ = ("cx", "cy", "n", "t0", "t1", "frags", "last", "arr", "sd_sum")

    def __init__(self, f: Fragment) -> None:
        self.cx = f.cx
        self.cy = f.cy
        self.n = f.n
        self.t0 = f.t0
        self.t1 = f.t1
        self.frags = 1
        self.last = f
        self.arr = "static" if f.open_start else "walk"
        self.sd_sum = f.sd * f.n


class TrackBehaviour:
    """Behaviour state of one confirmed track (RAM only)."""

    __slots__ = ("track_id", "powerup", "n", "first", "last4", "buf", "active", "prev", "sp_b",
                 "birth_done", "B", "B_start", "checked", "F")

    def __init__(self, track_id: int, powerup: bool) -> None:
        self.track_id = track_id
        self.powerup = powerup
        self.n = 0                  # samples received
        self.first: list = []       # the first 4 samples
        self.last4: deque = deque(maxlen=4)
        self.buf: Optional[list] = []   # samples before min_track_samples
        self.active = False
        self.prev = None            # last processed sample
        self.sp_b: Optional[float] = None
        self.birth_done = False
        self.B: list = []           # fragment buffer since the anchor B[0]
        self.B_start = 0            # sample index of B[0]
        self.checked = 1            # B[1:checked] are known to lie within the radius of B[0]
        self.F: Optional[list] = None   # folded run [n, sum dx, sum dy, sum dx^2+dy^2, t_last] relative to B[0]; B == [B[0]]

    def fold_stats(self) -> tuple:
        """(n, cx, cy, sd, t0, t1) of the folded run."""
        n, sx, sy, s2, t_last = self.F
        a = self.B[0]
        mx, my = sx / n, sy / n
        return n, a[1] + mx, a[2] + my, math.sqrt(max(0.0, s2 / n - mx * mx - my * my)), a[0], t_last

    def run(self) -> Optional[tuple]:
        """(t0, t1, cx, cy, n, sd) of the current fragment buffer, for 'Mark spot here'."""
        if self.F is not None:
            n, cx, cy, sd, t0, t1 = self.fold_stats()
            return (t0, t1, cx, cy, n, sd)
        B = self.B if self.active else (self.buf or [])
        if len(B) < 2:
            return None
        n = len(B)
        cx = sum(s[1] for s in B) / n
        cy = sum(s[2] for s in B) / n
        sd = math.sqrt(sum((s[1] - cx) ** 2 + (s[2] - cy) ** 2 for s in B) / n)
        return (B[0][0], B[-1][0], cx, cy, n, sd)


class Behaviours:
    """Stays, events and grid feeding. Owns the persisted stay, door and ghost event lists."""

    def __init__(self, p: Params, grids, covis, obs, date_of: Callable[[float], int]) -> None:
        self.p = p
        self.grids = grids
        self.covis = covis
        self.obs = obs                  # Observation: ld_share, he_share, censor_between (wall ms)
        self.date_of = date_of          # wall s -> local date ordinal
        self.pending: list = []
        self.stays: list = []           # persisted dicts, spec 8.1
        self.door_events: list = []
        self.ghost_events: list = []
        self.walk_points_total = 0
        self.hold_times: list = []      # (birth wall s, x, y) of tracks that may still bridge, see flush()
        self.fold_n = FOLD_N
        self.counters = {"fragments": 0, "stays": 0, "ghost_stays": 0, "epp": 0, "short_stays": 0}

    # ------------------------------------------------------------ timelines in wall seconds
    def ld_share(self, a: float, b: float) -> float:
        return self.obs.ld_share(int(round(a * 1000)), int(round(b * 1000)))

    def ld_share_opt(self, a: float, b: float) -> Optional[float]:
        return self.obs.ld_share_opt(int(round(a * 1000)), int(round(b * 1000)))

    def he_share(self, a: float, b: float) -> float:
        return self.obs.he_share(int(round(a * 1000)), int(round(b * 1000)))

    # ------------------------------------------------------------ per track
    def on_sample(self, tr, t: float, x: float, y: float) -> None:
        tb = tr.bh
        if tb is None:
            tb = tr.bh = TrackBehaviour(tr.id, tr.powerup)
        s = (t, x, y)
        tb.n += 1
        if tb.n <= 4:
            tb.first.append(s)
        tb.last4.append(s)
        if not tb.active:
            tb.buf.append(s)
            if len(tb.buf) >= self.p.min_track_samples:
                tb.active = True
                buf = tb.buf
                tb.buf = None
                for q in buf:
                    self._process(tb, q)
            return
        self._process(tb, s)

    def _speed_birth(self, tb: TrackBehaviour) -> float:
        n = tb.n
        k3 = min(self.p.exit_window_samples, n - 1)
        a, b = tb.first[0], tb.first[k3]
        return math.hypot(b[1] - a[1], b[2] - a[2]) / max(1.0, b[0] - a[0])

    def _speed_death(self, tb: TrackBehaviour) -> float:
        n = tb.n
        k3 = min(self.p.exit_window_samples, n - 1)
        b = tb.last4[-1]
        a = tb.last4[-1 - k3]
        return math.hypot(b[1] - a[1], b[2] - a[2]) / max(1.0, b[0] - a[0])

    def _birth(self, tb: TrackBehaviour) -> None:
        tb.birth_done = True
        tb.sp_b = self._speed_birth(tb)
        if tb.sp_b > self.p.moving_mps and not tb.powerup:
            s0 = tb.first[0]
            self.grids.splat("birth_moving", s0[1], s0[2], 1.0)
            self._door_event(s0[0], s0[1], s0[2], "b")

    def _process(self, tb: TrackBehaviour, s: tuple) -> None:
        p = self.p
        prev = tb.prev
        if prev is not None and math.hypot(s[1] - prev[1], s[2] - prev[2]) > p.walk_step_m:
            self.grids.add_walk(s[1], s[2])
            self.walk_points_total += 1
        tb.prev = s
        if not tb.birth_done and tb.n >= p.exit_window_samples + 1:
            self._birth(tb)
        # fragments (online SIM:465-485)
        B = tb.B
        r = p.frag_radius_m
        fmin = p.frag_min_s
        F = tb.F
        if F is not None:
            # folded run: B == [anchor] and every folded sample lies within r of it
            dx = s[1] - B[0][1]
            dy = s[2] - B[0][2]
            if math.hypot(dx, dy) <= r:
                F[0] += 1
                F[1] += dx
                F[2] += dy
                F[3] += dx * dx + dy * dy
                F[4] = s[0]
                return
            n, cx, cy, sd, t0, t1 = tb.fold_stats()            # a break; the run spans >= fmin
            self._emit_frag(tb, n, cx, cy, sd, t0, t1, "break", None)
            tb.B_start += n
            tb.F = None
            tb.B = [s]
            tb.checked = 1
            return
        B.append(s)
        while True:
            x0, y0 = B[0][1], B[0][2]
            j = -1
            i = tb.checked
            nb = len(B)
            while i < nb:
                q = B[i]
                if math.hypot(q[1] - x0, q[2] - y0) > r:
                    j = i
                    break
                i += 1
            if j < 0:
                tb.checked = nb
                if nb >= self.fold_n and B[-1][0] - B[0][0] >= fmin:
                    self._fold(tb)
                return
            if B[j - 1][0] - B[0][0] >= fmin:
                self._emit(tb, B[:j], "break", None)
                tb.B_start += j
                B = tb.B = B[j:]
            else:
                tb.B_start += 1
                B = tb.B = B[1:]
            tb.checked = 1

    @staticmethod
    def _fold(tb: TrackBehaviour) -> None:
        """Replaces the samples of a long run (all within the radius of the anchor) by running sums."""
        B = tb.B
        a = B[0]
        sx = sy = s2 = 0.0
        for q in B:
            dx = q[1] - a[1]
            dy = q[2] - a[2]
            sx += dx
            sy += dy
            s2 += dx * dx + dy * dy
        tb.F = [len(B), sx, sy, s2, B[-1][0]]
        tb.B = [a]

    def finish(self, tr, censored: bool) -> None:
        """Classified death (censored = ended by a censoring event)."""
        tb = tr.bh
        if tb is None or not tb.active:
            return
        p = self.p
        if not tb.birth_done:
            self._birth(tb)
        death_class = None
        if not censored:
            sp_d = self._speed_death(tb)
            last = tb.last4[-1]
            if sp_d > p.moving_mps:
                death_class = "moving"
                self.grids.splat("death_moving", last[1], last[2], 1.0)
                self._door_event(last[0], last[1], last[2], "d")
            else:
                death_class = "still"
                self.grids.splat("death_still", last[1], last[2], 1.0)
        B = tb.B
        if tb.F is not None:
            n, cx, cy, sd, t0, t1 = tb.fold_stats()
            self._emit_frag(tb, n, cx, cy, sd, t0, t1, "censor" if censored else "death", death_class)
        elif B and B[-1][0] - B[0][0] >= p.frag_min_s:
            self._emit(tb, B, "censor" if censored else "death", death_class)
        tb.B = []
        tb.F = None

    def can_open(self, tr) -> bool:
        """True while the track may still emit an open_start fragment (bridging candidate)."""
        if tr.powerup:
            return False
        tb = tr.bh
        if tb is None:
            return True
        if tb.B_start != 0:
            return False
        return tb.sp_b is None or tb.sp_b <= self.p.moving_mps

    # ------------------------------------------------------------ fragments
    def _emit(self, tb: TrackBehaviour, seg: list, end: str, death_class: Optional[str]) -> None:
        n = len(seg)
        cx = sum(q[1] for q in seg) / n
        cy = sum(q[2] for q in seg) / n
        sd = math.sqrt(sum((q[1] - cx) ** 2 + (q[2] - cy) ** 2 for q in seg) / n)
        self._emit_frag(tb, n, cx, cy, sd, seg[0][0], seg[-1][0], end, death_class)

    def _emit_frag(self, tb: TrackBehaviour, n: int, cx: float, cy: float, sd: float, t0: float, t1: float,
                   end: str, death_class: Optional[str]) -> None:
        p = self.p
        if tb.sp_b is None:
            tb.sp_b = self._speed_birth(tb)
        open_start = tb.B_start == 0 and tb.sp_b <= p.moving_mps and not tb.powerup
        f = Fragment(cx, cy, sd, n, t0, t1, tb.B_start, tb.track_id, end, open_start,
                     end == "death", death_class)
        self.counters["fragments"] += 1
        self.covis.add(f)
        if sd < p.epp_sd_m and f.t1 - f.t0 >= p.epp_min_s:
            dur = f.t1 - f.t0
            self.counters["epp"] += 1
            self._ghost_event(f.t0, cx, cy, dur, "identical", self.ld_share(f.t0, f.t1),
                              "static" if open_start else "walk")
            self.grids.splat("ghost", cx, cy, dur / 60.0)
            return
        self.on_fragment(f)

    def on_fragment(self, f: Fragment) -> None:
        p = self.p
        self.flush(f.t0)
        if f.open_start:
            for st in self.pending:
                gap = f.t0 - st.t1
                if 0 <= gap <= p.bridge_gap_s and math.hypot(f.cx - st.cx, f.cy - st.cy) <= p.bridge_dist_m \
                        and self.ld_share(st.t1, f.t0) >= p.bridge_ld_share \
                        and not self.obs.censor_between(int(round(st.t1 * 1000)), int(round(f.t0 * 1000))):
                    w0, w1 = st.n, f.n
                    st.cx = (st.cx * w0 + f.cx * w1) / (w0 + w1)
                    st.cy = (st.cy * w0 + f.cy * w1) / (w0 + w1)
                    st.n += w1
                    st.t1 = f.t1
                    st.frags += 1
                    st.last = f
                    st.sd_sum += f.sd * f.n
                    if not f.open_end:
                        self.pending.remove(st)
                        self.commit(st)
                    return
        st = PendingStay(f)
        if f.open_end:
            self.pending.append(st)
        else:
            self.commit(st)

    def flush(self, now_s: float, ignore_holds: bool = False) -> None:
        """Commit every pending stay with now_s - t1 > bridge_gap_s (spec 7.5), unless a live track born
        inside its bridging window may still emit an open_start fragment that would bridge to it. The
        simulator processes whole tracks in birth order, so such a track's fragment always comes first
        there; holding the stay keeps the online result equal to it. Only a track that could bridge holds:
        its first sample must lie within bridge_dist_m + frag_radius_m of the stay (the fragment's centre
        is within frag_radius_m of its first sample); a track far away never holds. ignore_holds commits
        regardless (before a nightly run: a hold never outlives the night)."""
        if not self.pending:
            return
        lim = self.p.bridge_gap_s
        reach = self.p.bridge_dist_m + self.p.frag_radius_m
        holds = [] if ignore_holds else self.hold_times
        done = []
        for st in self.pending:
            if now_s - st.t1 > lim:
                if any(st.t1 <= h[0] <= st.t1 + lim and math.hypot(h[1] - st.cx, h[2] - st.cy) <= reach
                       for h in holds):
                    continue
                done.append(st)
        for st in done:
            self.pending.remove(st)
            self.commit(st)

    def flush_censored(self) -> None:
        """A censoring event commits every pending stay with exit 'censored'."""
        pend = self.pending
        self.pending = []
        for st in pend:
            self.commit(st, censored=True)

    # ------------------------------------------------------------ commit (SIM:511-524)
    def commit(self, st: PendingStay, censored: bool = False) -> None:
        p = self.p
        dur = st.t1 - st.t0
        if dur < p.stay_min_s:
            self.counters["short_stays"] += 1
            return
        ld_opt = self.ld_share_opt(st.t0, st.t1)         # None: the LD2412 was not observing (unknown)
        ld = 0.0 if ld_opt is None else ld_opt
        he = self.he_share(st.t0, st.t1)
        last = st.last
        if censored or last.end == "censor":
            exit_ = "censored"
        elif last.end == "death":
            exit_ = last.death_class or "still"
        else:
            exit_ = "moved"
        arr = "bridge" if st.frags > 1 else st.arr
        if ld_opt is not None and ld < p.stay_ld_min:
            # ghost evidence is a stay while the LD2412 is known to be absent; unknown time (pw bit1 = 0)
            # adds none (D3): the stay is kept with ld 0, which gate 3 counts against the place
            self.counters["ghost_stays"] += 1
            self._ghost_event(st.t0, st.cx, st.cy, dur, "ld_off", ld, arr)
            self.grids.splat("ghost", st.cx, st.cy, dur / 60.0)
            return
        if he > p.home_empty_share:
            self.counters["ghost_stays"] += 1
            self._ghost_event(st.t0, st.cx, st.cy, dur, "home_empty", ld, arr)
            self.grids.splat("ghost", st.cx, st.cy, dur / 60.0)
            self.grids.splat("clutter", st.cx, st.cy, dur / 60.0)
            return
        d_ord = self.date_of(st.t0)
        tr = p.time_round_s
        if len(self.stays) >= MAX_STAYS:
            del self.stays[:len(self.stays) - MAX_STAYS + 1]
        self.stays.append({
            "t0": int(st.t0 // tr) * tr,
            "t1": int(st.t1 // tr) * tr,
            "dur": rnd(dur / p.dur_round_s) * p.dur_round_s,
            "x": rnd(st.cx * 100),
            "y": rnd(st.cy * 100),
            "sd": rnd(st.sd_sum / st.n * 100) if st.n else 0,
            "nf": st.frags,
            "ld": round(ld, 2),
            "he": round(he, 2),
            "exit": exit_,
            "arr": arr,
            "day": date.fromordinal(d_ord).isoformat(),
        })
        self.counters["stays"] += 1
        g = self.grids
        g.splat("dwell_slow", st.cx, st.cy, dur / 60.0)
        g.splat("dwell_fast", st.cx, st.cy, dur / 60.0)
        g.splat("sessions", st.cx, st.cy, 1.0)
        g.days_mark(st.cx, st.cy, d_ord)

    # ------------------------------------------------------------ events
    def _door_event(self, t: float, x: float, y: float, k: str) -> None:
        tr = self.p.time_round_s
        if len(self.door_events) >= MAX_DOOR_EVENTS:
            del self.door_events[:len(self.door_events) - MAX_DOOR_EVENTS + 1]
        self.door_events.append({"t": int(t // tr) * tr, "x": rnd(x * 100), "y": rnd(y * 100), "k": k})

    def _ghost_event(self, t0: float, x: float, y: float, dur: float, why: str, ld: float, arr: str) -> None:
        p = self.p
        tr = p.time_round_s
        if len(self.ghost_events) >= MAX_GHOST_EVENTS:
            del self.ghost_events[:len(self.ghost_events) - MAX_GHOST_EVENTS + 1]
        self.ghost_events.append({"t": int(t0 // tr) * tr, "x": rnd(x * 100), "y": rnd(y * 100),
                                  "dur": rnd(dur / p.dur_round_s) * p.dur_round_s, "why": why,
                                  "ld": round(ld, 2), "arr": arr})

    # ------------------------------------------------------------ 'Mark spot here' (spec 7.10)
    def bridge_target(self, t0: float, cx: float, cy: float) -> Optional[PendingStay]:
        """The pending stay a fragment starting at t0 at (cx, cy) would bridge to, or None."""
        p = self.p
        for st in self.pending:
            gap = t0 - st.t1
            if 0 <= gap <= p.bridge_gap_s and math.hypot(cx - st.cx, cy - st.cy) <= p.bridge_dist_m \
                    and self.ld_share(st.t1, t0) >= p.bridge_ld_share \
                    and not self.obs.censor_between(int(round(st.t1 * 1000)), int(round(t0 * 1000))):
                return st
        return None
