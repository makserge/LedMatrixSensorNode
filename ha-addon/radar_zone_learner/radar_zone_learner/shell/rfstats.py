"""RF and coverage numbers from the node's radar state transitions (design 6.3 'RF and coverage').

Time is counted in 15-minute buckets of UTC time, 8 days kept: seconds with the radars on, seconds with
them off (from ``<node>/radar/state``) and seconds with 'Radar data valid' online.

- ``rf_on_24h``     hours with the radars on in the last 24 h (``on``, ``forced_on``, ``failsafe_on``)
- ``rf_saving_7d``  share of the known time in the last 7 days with the radars off (``night_off``, ``forced_off``), %
- ``coverage_7d``   share of the last 7 days (168 h) in which the learner was observing, %

Time with no known radar state (node offline, no P2 firmware, app stopped) is counted as neither on nor off,
so a number is ``None`` until at least an hour of the window is known. A gap of more than 10 minutes between
two updates (the app was stopped or stalled) is not counted at all.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .persistence import atomic_write

BUCKET_S = 900
KEEP_DAYS = 8
MAX_GAP_S = 600
MIN_KNOWN_S = 3600
FILE_NAME = "rf_stats.json"
FORMAT = 1

log = logging.getLogger("radar_zone_learner.rfstats")


class RfStats:
    def __init__(self) -> None:
        self.buckets: dict = {}            # bucket index -> [on_s, off_s, valid_s]
        self.radar_on: Optional[bool] = None
        self.valid = False
        self._t: Optional[float] = None

    # ------------------------------------------------------------ accounting
    def advance(self, now_s: float) -> None:
        """Books the time since the last call to the current radar state and validity."""
        t = self._t
        self._t = now_s
        if t is None or now_s <= t or now_s - t > MAX_GAP_S:
            return
        if self.radar_on is None and not self.valid:
            return
        a = t
        while a < now_s:
            idx = int(a // BUCKET_S)
            end = min(now_s, (idx + 1) * BUCKET_S)
            rec = self.buckets.setdefault(idx, [0.0, 0.0, 0.0])
            d = end - a
            if self.radar_on is True:
                rec[0] += d
            elif self.radar_on is False:
                rec[1] += d
            if self.valid:
                rec[2] += d
            a = end
        self._prune(now_s)

    def set_radar(self, on: Optional[bool], now_s: float) -> None:
        self.advance(now_s)
        self.radar_on = on

    def set_valid(self, valid: bool, now_s: float) -> None:
        self.advance(now_s)
        self.valid = bool(valid)

    def _prune(self, now_s: float) -> None:
        old = int(now_s // BUCKET_S) - KEEP_DAYS * 86400 // BUCKET_S
        if len(self.buckets) > KEEP_DAYS * 86400 // BUCKET_S + 4:
            for idx in [i for i in self.buckets if i < old]:
                del self.buckets[idx]

    # ------------------------------------------------------------ the numbers
    def _sum(self, now_s: float, seconds: int) -> list:
        """[on, off, valid] seconds over the last ``seconds``; the oldest bucket counts by its share."""
        n = seconds // BUCKET_S
        now_idx = int(now_s // BUCKET_S)
        frac = (now_s % BUCKET_S) / BUCKET_S
        tot = [0.0, 0.0, 0.0]
        for idx in range(now_idx - n + 1, now_idx + 1):
            rec = self.buckets.get(idx)
            if rec:
                for i in range(3):
                    tot[i] += rec[i]
        rec = self.buckets.get(now_idx - n)
        if rec:
            for i in range(3):
                tot[i] += rec[i] * (1.0 - frac)
        return tot

    def rf_on_24h(self, now_s: float) -> Optional[float]:
        on, off, _ = self._sum(now_s, 86400)
        return None if on + off < MIN_KNOWN_S else round(on / 3600.0, 1)

    def rf_saving_7d(self, now_s: float) -> Optional[float]:
        on, off, _ = self._sum(now_s, 7 * 86400)
        return None if on + off < MIN_KNOWN_S else round(100.0 * off / (on + off), 0)

    def coverage_7d(self, now_s: float) -> float:
        return round(100.0 * self._sum(now_s, 7 * 86400)[2] / (7 * 86400), 0)

    # ------------------------------------------------------------ persistence
    def export(self) -> dict:
        return {"v": FORMAT, "bucket_s": BUCKET_S,
                "b": {str(i): [int(round(x)) for x in rec] for i, rec in sorted(self.buckets.items())}}

    def load(self, data, now_s: float) -> None:
        self.buckets = {}
        if not isinstance(data, dict) or data.get("v") != FORMAT or data.get("bucket_s") != BUCKET_S:
            return
        raw = data.get("b")
        if not isinstance(raw, dict):
            return
        oldest = int(now_s // BUCKET_S) - KEEP_DAYS * 86400 // BUCKET_S
        newest = int(now_s // BUCKET_S) + 1
        for key, rec in raw.items():
            try:
                idx = int(key)
                vals = [float(x) for x in rec]
            except (TypeError, ValueError):
                continue
            if len(vals) != 3 or not oldest <= idx <= newest or any(not 0 <= x <= BUCKET_S for x in vals):
                continue
            self.buckets[idx] = vals

    def save_file(self, data_dir: Path) -> int:
        return atomic_write(Path(data_dir) / FILE_NAME,
                            json.dumps(self.export(), separators=(",", ":")).encode("ascii"))

    def load_file(self, data_dir: Path, now_s: float) -> None:
        path = Path(data_dir) / FILE_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("%s is unusable: the RF numbers start again", FILE_NAME)
            return
        self.load(data, now_s)
