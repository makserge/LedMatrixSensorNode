"""Multi-target tracker on 10 Hz frames (P3 spec 7.2, 7.3; OA 7.3 step 1).

- Association: exhaustive enumeration of the injective partial maps from the detections (payload
  slot order) to the live tracks (id order) or "none" (the 3x3 brute-force idea of
  PeterkoCZ91/HLK-LD2450-security, used as an idea only, OA 16). Lowest total cost wins, ties go to
  the first map enumerated.
- Filter: alpha-beta 0.35/0.08 (the idea of zomco/mmwave-engine, OA 16).
- Confirmed tracks emit 1 Hz samples (t_wall_s, x, y) for the behaviour pipeline (D13).
- Dead tracks wait in the dying list until the frontier has passed last_dev + censor_death_ms (then
  they are classified) or a censoring event ends them as censored.
"""
from __future__ import annotations

import math
from typing import Optional

from .params import Params


class Track:
    __slots__ = ("id", "x", "y", "vx", "vy", "hits", "last_dev", "born_dev", "confirmed",
                 "next_sample_dev", "bx", "by", "powerup", "bh", "rx", "ry")

    def __init__(self, tid: int, x: float, y: float, tf: int, powerup: bool) -> None:
        self.id = tid
        self.x = x
        self.y = y
        self.rx = x                 # P6: the last raw associated detection (the filter output lags it); read only
        self.ry = y
        self.vx = 0.0
        self.vy = 0.0
        self.hits = 1
        self.last_dev = tf
        self.born_dev = tf
        self.confirmed = False
        self.next_sample_dev = 0
        self.bx = x
        self.by = y
        self.powerup = powerup
        self.bh = None              # behaviour state (core.behaviours.TrackBehaviour), set on confirmation

    def __repr__(self) -> str:
        return "Track(%d, %.2f, %.2f, hits=%d)" % (self.id, self.x, self.y, self.hits)


class Tracker:
    def __init__(self, p: Params, next_id: int = 1) -> None:
        self.p = p
        self.live: list = []        # ascending id
        self.dying: list = []       # tracks waiting for their exit classification
        self.next_id = next_id

    # ------------------------------------------------------------ deaths
    def _kill(self, tr: Track) -> None:
        self.live.remove(tr)
        self.dying.append(tr)

    def expire(self, tf: int) -> None:
        ttl = self.p.ttl_ms
        if not self.live:
            return
        dead = [t for t in self.live if tf - t.last_dev > ttl]
        if dead:
            for t in dead:
                self._kill(t)

    def ready(self, frontier_dev: int) -> list:
        """Dead tracks whose classification time has come, ordered by (last_dev, id)."""
        if not self.dying:
            return []
        lim = frontier_dev - self.p.censor_death_ms
        out = [t for t in self.dying if t.last_dev <= lim]
        if out:
            self.dying = [t for t in self.dying if t.last_dev > lim]
            out.sort(key=lambda t: (t.last_dev, t.id))
        return out

    def censor_all(self) -> list:
        """End every live and dying track (censoring event); ascending id."""
        out = sorted(self.live + self.dying, key=lambda t: t.id)
        self.live = []
        self.dying = []
        return out

    # ------------------------------------------------------------ one frame
    def step(self, tf: int, dets: list, offset: int, observing_start: Optional[int], samples: list) -> None:
        """Process one listed frame at device time tf with detections [(x_m, y_m), ...] in slot
        order. Appends (track, t_wall_s, x, y) to samples for every 1 Hz sample emitted."""
        p = self.p
        self.expire(tf)
        live = self.live
        nd = len(dets)
        if nd == 0:
            return
        preds = []
        for tr in live:
            dt = (tf - tr.last_dev) / 1000.0
            if dt > p.dt_max_s:
                dt = p.dt_max_s
            px = tr.x + tr.vx * dt
            py = tr.y + tr.vy * dt
            lim = p.gate_m + math.hypot(tr.vx, tr.vy) * dt
            preds.append((px, py, lim, dt))
        assign = self._associate(dets, preds) if preds else [-1] * nd
        births = []
        for di in range(nd):
            ti = assign[di]
            zx, zy = dets[di]
            if ti < 0:
                births.append((zx, zy))
                continue
            tr = live[ti]
            tr.rx = zx
            tr.ry = zy
            px, py, _, dt = preds[ti]
            rx = zx - px
            ry = zy - py
            tr.x = px + p.alpha * rx
            tr.y = py + p.alpha * ry
            g = p.beta / max(dt, 0.05)
            tr.vx += g * rx
            tr.vy += g * ry
            tr.hits += 1
            tr.last_dev = tf
            self._samples(tr, tf, offset, samples)
        for zx, zy in births:
            if len(self.live) >= p.max_tracks:
                old = min(self.live, key=lambda t: (t.last_dev, t.id))
                self._kill(old)
            powerup = observing_start is not None and tf - observing_start <= p.powerup_birth_ms
            tr = Track(self.next_id, zx, zy, tf, powerup)
            self.next_id += 1
            self.live.append(tr)
            self._samples(tr, tf, offset, samples)

    def _samples(self, tr: Track, tf: int, offset: int, samples: list) -> None:
        p = self.p
        if not tr.confirmed:
            if tr.hits < p.confirm_hits:
                return
            tr.confirmed = True
            samples.append((tr, (tr.born_dev + offset) / 1000.0, tr.bx, tr.by))
            tr.next_sample_dev = tr.born_dev + p.sample_period_ms
            if tf == tr.born_dev:
                return
        if tf >= tr.next_sample_dev:
            samples.append((tr, (tf + offset) / 1000.0, tr.x, tr.y))
            tr.next_sample_dev = tf + p.sample_period_ms

    def _associate(self, dets: list, preds: list) -> list:
        """Lowest-cost injective partial map; ties go to the first map enumerated (depth first,
        tracks in id order before "none")."""
        un = self.p.unassigned_cost_m
        nd = len(dets)
        opts = []
        for zx, zy in dets:
            o = []
            for ti, (px, py, lim, _) in enumerate(preds):
                d = math.hypot(zx - px, zy - py)
                if d <= lim:
                    o.append((ti, d))
            opts.append(o)
        if nd == 1:
            o = opts[0]
            best_t, best_c = -1, un
            for ti, d in o:
                if d < best_c:
                    best_t, best_c = ti, d
            # an allowed pair enumerated before "none" wins a tie with it
            for ti, d in o:
                if d == un and best_t < 0:
                    best_t = ti
                    break
            return [best_t]
        best = [float("inf"), None]
        assign = [-1] * nd
        used = set()

        def rec(i: int, cost: float) -> None:
            if cost >= best[0]:
                return
            if i == nd:
                best[0] = cost
                best[1] = assign[:]
                return
            for ti, d in opts[i]:
                if ti not in used:
                    used.add(ti)
                    assign[i] = ti
                    rec(i + 1, cost + d)
                    used.discard(ti)
            assign[i] = -1
            rec(i + 1, cost + un)

        rec(0, 0.0)
        return best[1]
