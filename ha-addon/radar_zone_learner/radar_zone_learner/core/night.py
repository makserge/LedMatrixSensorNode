"""Night evidence and the night-window proposal (P4; OA 7.5 and the night_window row of 7.8).

Everything here is about the radar's sleep window and when people fall asleep and get up:

- **Onsets** (when someone goes to sleep), from three sources, each with its own list:
  ``radar`` (LD2412 present, no moving target for 15 min, dark or the ``lights_off`` context, the last track
  died still in the bed region), ``context`` (the HA ``lights_off`` / ``sleep_hint`` contexts) and ``lux``
  (the BH1750 'lights-off' proxy: the first time after 20:00 that lux stays below ``night_lux`` for 15 min).
  The proxy and the contexts work while the radars are off.
- **Wakes** (the radar only; visible only after the window end): the first activity of the morning (a walking
  point after >= 30 min without one, or after unobserved time) that is followed by more than 10 min of activity,
  which is read as 'the room is not at rest again': within the next 10 min there is a walking point, or the
  LD2412 does not show someone present (the person left). Someone who gets up and lies down again (still, LD2412
  present, no walking) is a trip, not the wake. A walking point is a confirmed track that moved more than
  ``walk_step_m`` in a second.
- **Activity bins**, 5 min per night, from walking points, kept for the last 15 nights only: they answer
  'activity was still seen in the 30 min before your current start on N of the last 14 nights'.
- **The proposal**: start = P90(onset) rounded up to 15 min, end = P10(wake) rounded down to 15 min, from >= 7
  nights with each edge and an IQR < 90 min. One onset is taken per night (radar, else context, else lux) before
  the percentiles, so a night on which the radar was blind (someone fell asleep after the window start) still
  counts through its lights-off time. The proposal is labelled 'from lights-off times' when the radar onsets
  alone would not give a start (fewer than 7 nights) or one more than ``nw_same_min`` (15 min) away.
- **One radar onset per night**: a later quiet start replaces it only within ``onset_replace_max_min`` of the
  night's first one (someone who got up and lay down again soon); a bathroom trip at 03:00, or several trips one
  after the other, never move the bedtime further than that.

Night axis: ``night_axis(tm, ms)`` -> (night, nm). ``night`` is the local date ordinal of the evening the night
starts on and ``nm`` the minutes after 12:00 local of that date (0..1439), so 23:30 is 690, 00:07 on the next
date is 727 and 06:10 is 1090. Percentiles over ``nm`` therefore need no wrap-around at midnight.

Pure and deterministic like the rest of the core: no clock, only the timestamps handed in.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import date
from typing import Optional

from .params import Params
from .types import StateError

SOURCES = ("radar", "context", "lux")
WAKE_SOURCES = ("radar",)
BINS = 288                       # 5 min bins of a night (0..1439 minutes after noon)
WAKE_SKIP_S = 30                 # the first seconds after the first activity (getting out of bed) are not judged
ACT_HEX = 72                     # hex digits of one night's bitmap
MAX_ENTRIES = 400                # bound on the loaded onset/wake/activity lists


def night_axis(tm, epoch_ms: int) -> tuple:
    """(night, nm): see the module docstring."""
    lt = tm.local(epoch_ms)
    m = lt.sec_of_day // 60
    if m >= 720:
        return lt.date_ord, m - 720
    return lt.date_ord - 1, m + 720


def minute_of_day(nm: int) -> int:
    return (nm + 720) % 1440


def local_epoch_ms(tm, date_ord: int, minute: int, near_ms: int) -> int:
    """Epoch ms of local minute ``minute`` on local date ``date_ord``, found from ``near_ms`` by correcting with
    the time model (so a DST change between the two is handled)."""
    cand = near_ms
    for _ in range(4):
        lt = tm.local(cand)
        diff = (date_ord - lt.date_ord) * 86400 + minute * 60 - lt.sec_of_day
        if diff == 0:
            return cand - cand % 1000
        cand += diff * 1000
    return cand


def percentile(sorted_vals: list, q: float) -> float:
    """Linear interpolation between order statistics (the numpy default)."""
    n = len(sorted_vals)
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def circ_diff(a: int, b: int) -> int:
    """Distance of two minutes of the day on the 24 h circle."""
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


def window_len(start_min: int, end_min: int) -> int:
    """Minutes of the window [start, end) on the circle; 0 when start == end (a window that never happens)."""
    return (end_min - start_min) % 1440


class NightModel:
    def __init__(self, p: Params, time_model) -> None:
        self.p = p
        self.tm = time_model
        # persisted (state 'night')
        self.night_lux: float = p.night_lux
        self.onsets: dict = {s: {} for s in SOURCES}     # source -> {night: nm}
        self._first: dict = {}                           # RAM: (source, night) -> the first onset of that night
        self.wakes: dict = {}                            # night -> nm
        self.act: dict = {}                              # night -> int bitmap of 5 min bins
        self.proposal_rev = 0
        # RAM
        self.lux: Optional[float] = None
        self.dark_since_ms: Optional[int] = None
        self.lux_done = False                            # this dark run already gave its lights-off time
        self.ctx: dict = {}                              # context name -> Optional[bool]
        self.last_move_s: Optional[float] = None
        self.eval_done = True
        self.last_death: Optional[tuple] = None          # (t_s, x, y, class)
        self.walks: deque = deque()                      # wall s of the walking points of the last 20 min
        self.prev_walk_s: Optional[float] = None
        self.wake_cand: Optional[float] = None           # the first activity that waits for its 10 min
        self.raw: Optional[dict] = None                  # the proposal now, without the 'current window' parts
        self._sig: Optional[tuple] = None
        self.dirty = True
        self.last_step_ms: Optional[int] = None
        self.counters = {"onsets_radar": 0, "onsets_lux": 0, "onsets_context": 0, "wakes": 0}

    # ------------------------------------------------------------ inputs
    def on_lux(self, lux: float, now_ms: int) -> None:
        if not isinstance(lux, (int, float)) or isinstance(lux, bool) or not math.isfinite(lux) or lux < 0:
            return
        self.lux = float(lux)
        if lux < self.night_lux:
            if self.dark_since_ms is None:
                self.dark_since_ms = now_ms
                self.lux_done = False
        else:
            self.dark_since_ms = None
            self.lux_done = False

    def on_context(self, name: str, value: Optional[bool], now_ms: int) -> None:
        prev = self.ctx.get(name)
        self.ctx[name] = value
        if name in ("lights_off", "sleep_hint") and value is True and prev is not None and prev is not True:
            night, nm = night_axis(self.tm, now_ms)
            if self._in_onset_range(nm):
                self._put_onset("context", night, nm, first_only=True)

    def note_walk(self, t_s: float, obs) -> None:
        """A walking point (a confirmed track moved more than walk_step_m in a second) at wall second t_s."""
        p = self.p
        t_ms = int(t_s * 1000)
        night, nm = night_axis(self.tm, t_ms)
        self.act[night] = self.act.get(night, 0) | (1 << min(BINS - 1, nm // p.night_res_min))
        if self.last_move_s is None or t_s > self.last_move_s:
            self.last_move_s = t_s
            self.eval_done = False
        # the first activity of the morning: after >= wake_rest_s without a walking point, or after unobserved time
        prev = self.prev_walk_s
        if self.wake_cand is None and night not in self.wakes and \
                p.wake_earliest_min <= minute_of_day(nm) < p.wake_latest_min and \
                (prev is None or t_s - prev >= p.wake_rest_s or obs.censor_between(int(prev * 1000), t_ms)):
            self.wake_cand = t_s
        self.walks.append(t_s)
        while self.walks and self.walks[0] < t_s - p.wake_rest_s:
            self.walks.popleft()
        if prev is None or t_s > prev:
            self.prev_walk_s = t_s

    def note_death(self, t_s: float, x: float, y: float, cls: str) -> None:
        self.last_death = (t_s, x, y, cls)

    # ------------------------------------------------------------ evaluation (called often, cheap)
    def step(self, L, now_ms: int) -> None:
        p = self.p
        self.last_step_ms = now_ms
        # lights-off time from the lux proxy (works while the radars are off)
        if self.dark_since_ms is not None and not self.lux_done:
            night, nm = night_axis(self.tm, now_ms)
            dn, dnm = night_axis(self.tm, self.dark_since_ms)
            if dn == night:
                t_nm = max(dnm, p.evening_start_min - 720)
                if self._in_onset_range(t_nm) and (nm - t_nm) * 60 >= p.lux_dark_min_s:
                    self.lux_done = True
                    self._put_onset("lux", night, t_nm, first_only=True)
        # a wake: the first activity, 10 min later, if the room is not at rest again
        w = self.wake_cand
        if w is not None and now_ms / 1000.0 - w >= p.wake_run_s:
            self.wake_cand = None
            self._wake(L, w)
        # radar onset: 15 min after the last moving sample
        if not self.eval_done and self.last_move_s is not None and \
                now_ms / 1000.0 - self.last_move_s >= p.onset_quiet_s:
            self.eval_done = True
            self._radar_onset(L, now_ms)
        if self.dirty:
            self.dirty = False
            self.refresh(L)

    def _wake(self, L, w: float) -> None:
        p = self.p
        a = int((w + WAKE_SKIP_S) * 1000)
        b = int((w + p.wake_run_s) * 1000)
        if L.obs.censor_between(int(w * 1000), b):
            return                                    # the 10 min were not observed: no verdict
        later = [t for t in self.walks if w + WAKE_SKIP_S < t <= w + p.wake_run_s]
        share = L.obs.ld_share_opt(a, b)
        if not later and share is not None and share >= p.onset_ld_min:
            return                                    # back in bed: lying still, the LD2412 sees someone
        night, nm = night_axis(self.tm, int(w * 1000))
        if night not in self.wakes:
            self.wakes[night] = nm - nm % p.night_res_min
            self.counters["wakes"] += 1
            self.dirty = True

    def _in_onset_range(self, nm: int) -> bool:
        a = self.p.evening_start_min - 720
        b = self.p.night_onset_end_min + 720
        return a <= nm < b

    def _put_onset(self, src: str, night: int, nm: int, first_only: bool = False,
                   max_replace_min: Optional[int] = None) -> None:
        nm -= nm % self.p.night_res_min
        d = self.onsets[src]
        old = d.get(night)
        first = self._first.setdefault((src, night), nm if old is None else old)   # the night's first onset (RAM)
        if old is not None:
            if first_only:
                return
            if max_replace_min is not None and nm - first > max_replace_min:
                return                                # a later quiet start (a trip in the night) keeps the bedtime
        if old != nm:
            d[night] = nm
            self.counters["onsets_" + src] += 1
            self.dirty = True

    def _radar_onset(self, L, now_ms: int) -> None:
        p = self.p
        t = self.last_move_s
        t_ms = int(t * 1000)
        night, nm = night_axis(self.tm, t_ms)
        if not self._in_onset_range(nm):
            return
        if L._state_now() != "learning" or L.obs.censor_between(t_ms, now_ms):
            return                                    # the quiet 15 min must have been observed
        share = L.obs.ld_share_opt(t_ms, t_ms + p.onset_quiet_s * 1000)
        if share is None or share < p.onset_ld_min:
            return                                    # LD2412 present
        dark = self.dark_since_ms is not None and now_ms - self.dark_since_ms >= p.onset_dark_s * 1000
        if not (dark or self.ctx.get("lights_off") is True):
            return                                    # lux below night_lux, or lights_off
        pos = self._rest_position(L, t)
        if pos is None:
            return                                    # the last track did not die still (or nothing is known)
        bed = L.review.bed_cells(L)
        if bed and L.geom.cell_of(pos[0], pos[1]) not in bed:
            return                                    # ... in the bed zone, once there is one
        self._put_onset("radar", night, nm, max_replace_min=p.onset_replace_max_min)

    def _rest_position(self, L, t_move: float) -> Optional[tuple]:
        """Where the last track came to rest: a live confirmed track's position, else the position of the last
        track death if it was classified still. None: it left (a moving death) or nothing is known."""
        live = [tr for tr in L.tracker.live if tr.confirmed]
        if live:
            tr = live[-1]
            return tr.x, tr.y
        d = self.last_death
        if d is not None and d[3] == "still" and d[0] >= t_move - 5.0:
            return d[1], d[2]
        return None

    # ------------------------------------------------------------ retention
    def prune(self, now_ms: int) -> None:
        night, _ = night_axis(self.tm, now_ms)
        lo = night - self.p.night_keep_nights + 1
        for d in list(self.onsets.values()) + [self.wakes]:
            for k in [k for k in d if k < lo]:
                del d[k]
        for k in [k for k in self._first if k[1] < lo]:
            del self._first[k]
        lo = night - self.p.nw_late_nights - 1                # activity bins: only the nights the item asks about
        for k in [k for k in self.act if k < lo]:
            del self.act[k]
        self.dirty = True

    # ------------------------------------------------------------ the proposal
    def refresh(self, L) -> None:
        """Recomputes the proposal from the stored evidence; bumps ``proposal_rev`` when its content changes."""
        raw = self._compute()
        sig = self._signature(raw)
        if sig != self._sig:
            self._sig = sig
            if sig is not None:
                self.proposal_rev += 1
        self.raw = raw
        L.rv_dirty = True

    @staticmethod
    def _signature(raw: Optional[dict]) -> Optional[tuple]:
        """What makes two proposals the same one for the review: start, end and basis."""
        return None if raw is None else (raw["start_min"], raw["end_min"], raw["basis"])

    def _merged_onsets(self) -> list:
        """One onset per night, as (nm, source): the radar's if it saw one, else the context's, else the lux
        proxy's. A night on which the radar was blind still counts through its lights-off time."""
        nights = set()
        for src in SOURCES:
            nights.update(self.onsets[src])
        out = []
        for n in nights:
            for src in SOURCES:
                v = self.onsets[src].get(n)
                if v is not None:
                    out.append((v, src))
                    break
        out.sort()
        return out

    def _start_nm(self, onsets: list) -> int:
        r = self.p.nw_round_min
        return int(math.ceil(percentile(onsets, 0.90) / r - 1e-9)) * r

    def _compute(self) -> Optional[dict]:
        p = self.p
        merged = self._merged_onsets()
        onsets = [v for v, _ in merged]
        wakes = sorted(self.wakes.values())
        if len(onsets) < p.nw_min_nights or len(wakes) < p.nw_min_nights:
            return None
        iqr_on = percentile(onsets, 0.75) - percentile(onsets, 0.25)
        iqr_wk = percentile(wakes, 0.75) - percentile(wakes, 0.25)
        if iqr_on >= p.nw_iqr_max_min or iqr_wk >= p.nw_iqr_max_min:
            return None
        r = p.nw_round_min
        start_nm = self._start_nm(onsets)
        end_nm = int(math.floor(percentile(wakes, 0.10) / r + 1e-9)) * r
        start_min = minute_of_day(start_nm)
        end_min = minute_of_day(end_nm)
        length = window_len(start_min, end_min)
        if not p.nw_min_len_min <= length <= p.nw_max_len_min:
            return None
        # 'from lights-off times': the radar's own onsets would not give this start (fewer than 7 nights, or a start
        # that is more than nw_same_min away: the nights it was blind move the proposal)
        radar = [v for v, s in merged if s == "radar"]
        from_lights_off = len(radar) < p.nw_min_nights or abs(self._start_nm(radar) - start_nm) > p.nw_same_min
        basis = "radar"
        if from_lights_off:
            cnt = {s: sum(1 for _, x in merged if x == s) for s in ("context", "lux")}
            basis = "context" if cnt["context"] >= cnt["lux"] and cnt["context"] > 0 else "lux"
        return {"start_min": start_min, "end_min": end_min, "basis": basis, "from_lights_off": from_lights_off,
                "onsets_n": len(onsets), "wakes_n": len(wakes), "iqr_onset_min": round(iqr_on, 1),
                "iqr_wake_min": round(iqr_wk, 1), "len_min": length,
                "saving_pct": round(100.0 * length / 1440.0, 1)}

    def late_nights(self, start_min: int, now_ms: int, created_night: Optional[int]) -> tuple:
        """(N, of): on N of the last ``nw_late_nights`` nights, activity was seen in the ``nw_late_min`` minutes
        before ``start_min`` (a night counts once that time of it has passed)."""
        p = self.p
        today_night, now_nm = night_axis(self.tm, now_ms)
        s_nm = (start_min + 720) % 1440
        last = today_night if now_nm >= s_nm else today_night - 1
        lo_bin = max(0, (s_nm - p.nw_late_min) // p.night_res_min)
        hi_bin = s_nm // p.night_res_min
        mask = 0
        for b in range(lo_bin, hi_bin):
            mask |= 1 << b
        first = last - p.nw_late_nights + 1
        if created_night is not None:
            first = max(first, created_night)
        n = 0
        of = 0
        for night in range(first, last + 1):
            of += 1
            if self.act.get(night, 0) & mask:
                n += 1
        return n, of

    # ------------------------------------------------------------ persistence
    def export(self) -> dict:
        iso = lambda n: date.fromordinal(n).isoformat()      # noqa: E731
        ons = []
        for src in SOURCES:
            for n in sorted(self.onsets[src]):
                ons.append({"n": iso(n), "m": self.onsets[src][n], "s": src})
        return {
            "night_lux": self.night_lux,
            "onsets": ons,
            "wakes": [{"n": iso(n), "m": self.wakes[n], "s": "radar"} for n in sorted(self.wakes)],
            "act": {iso(n): format(self.act[n], "0%dx" % ACT_HEX) for n in sorted(self.act) if self.act[n]},
            "proposal_rev": self.proposal_rev,
        }

    def load(self, d: dict) -> None:
        """Loads the validated ``night`` section (see ``validate``)."""
        o = {s: {} for s in SOURCES}
        for e in d["onsets"]:
            o[e["s"]][date.fromisoformat(e["n"]).toordinal()] = e["m"]
        # night_lux is NOT read: ``Params.night_lux`` is the one source of the threshold (the P0 measurement becomes
        # that constant) and the stored value is only the record of what was in use (tests/test_state.py:
        # test_the_night_lux_of_the_options_wins_over_the_file). Reading it would freeze the default in every
        # existing state, and a later change of the constant would never reach an installed learner.
        self.onsets = o
        self.wakes = {date.fromisoformat(e["n"]).toordinal(): e["m"] for e in d["wakes"]}
        self.act = {date.fromisoformat(k).toordinal(): int(v, 16) for k, v in d["act"].items()}
        self.proposal_rev = d["proposal_rev"]
        # the stored revision belongs to the stored proposal: the first refresh after a restart must not count the
        # unchanged proposal as a new one (answers and notifications carry the revision)
        self._sig = self._signature(self._compute())
        self.dirty = True


def _bad(msg: str):
    raise StateError(msg)


def _isint(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _iso_ok(v, what: str) -> None:
    if not isinstance(v, str):
        _bad("bad date in " + what)
    try:
        date.fromisoformat(v)
    except ValueError:
        _bad("bad date in " + what)


def validate(d, p: Params) -> None:
    """Validates the P4 part of the ``night`` section (raises StateError)."""
    if not isinstance(d, dict):
        _bad("night is not an object")
    lux = d.get("night_lux")
    if isinstance(lux, bool) or not isinstance(lux, (int, float)) or not math.isfinite(lux) or not 0 <= lux <= 1e5:
        _bad("bad night.night_lux")
    for key, srcs in (("onsets", SOURCES), ("wakes", WAKE_SOURCES)):
        lst = d.get(key)
        if not isinstance(lst, list) or len(lst) > MAX_ENTRIES:
            _bad("bad night." + key)
        for e in lst:
            if not isinstance(e, dict) or not _isint(e.get("m")) or not 0 <= e["m"] < 1440 or e.get("s") not in srcs:
                _bad("bad night." + key + " entry")
            _iso_ok(e.get("n"), "night." + key)
    act = d.get("act")
    if not isinstance(act, dict) or len(act) > MAX_ENTRIES:
        _bad("bad night.act")
    for k, v in act.items():
        _iso_ok(k, "night.act")
        if not isinstance(v, str) or len(v) != ACT_HEX:
            _bad("bad night.act bitmap")
        try:
            int(v, 16)
        except ValueError:
            _bad("bad night.act bitmap")
    if not _isint(d.get("proposal_rev")) or d["proposal_rev"] < 0:
        _bad("bad night.proposal_rev")
