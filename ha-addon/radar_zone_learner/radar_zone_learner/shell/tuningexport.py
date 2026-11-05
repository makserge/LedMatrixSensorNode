"""The local nightly count lines and the pooled export of the tuning data (P7 spec 4.8, 7.1, 7.3). Shell only, standard library.

The core makes one JSON line a night (``TuningLine``: counts and fixed-length histograms, no date, no night index, no hour, no
position, no name, no id). This module

- keeps the newest ``agg_keep_nights`` (28) of them **locally** in ``<data_dir>/tuning/nights.jsonl``, written atomically, one line
  a night; a line that is not a clean count line (the core's own privacy scan, ``tuningagg.scan``) is refused and counted, and a
  line of a night that was already written is dropped (a nightly that ran twice after a restart must not count twice: the
  learner's night number is kept in the one-number file ``tuning/last_night``, so the file holds no night series);
- builds the **pooled export** (``tuningagg.pool``: sums and histograms over the kept nights, the newest value of the cumulative
  answer counters; no per-night list, no night index, no date) for the button 'Export tuning data' and checks it with the privacy
  scan and the size cap before it may be published; **no file is written for the export** (the add-on has no ``map:`` entry, so
  a file under ``/data`` would not be reachable by the owner): the layer publishes it once on a topic that is not retained;
- deletes the whole folder on 'Forget all learned data'.

The folder is excluded from Home Assistant backups (``config.yaml`` ``backup_exclude: tuning/*``): the per-night file is a
day-by-day series of counts (a night with no stays reads as 'nobody home'), the same class as the stay list in ``state.json`` but
coarser, and it stays on the add-on's disk. Nothing here logs a value: a count of lines is the most it says.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core import tuningagg
from ..core.params import DEFAULT_P7
from .persistence import atomic_write

TUNING_DIR = "tuning"
NIGHTS_NAME = "nights.jsonl"
LAST_NAME = "last_night"

STORED, DUPLICATE, REJECTED = "stored", "duplicate", "rejected"

log = logging.getLogger("radar_zone_learner.tuning")


@dataclass(frozen=True)
class ExportResult:
    """What a press of the export button gives: the bytes to publish, or ``None`` and the reason (``empty``, ``unclean``,
    ``too_large``, ``error``). ``n`` is the number of nightly lines pooled (the only figure that is logged)."""
    payload: Optional[bytes]
    n: int
    reason: str = ""


def state_text(n: int) -> str:
    """The state of the diagnostic sensor 'Tuning data': ``none`` or ``<n> nights``."""
    return "none" if n <= 0 else "%d nights" % n


class TuningStore:
    def __init__(self, data_dir, *, keep: int = DEFAULT_P7.agg_keep_nights, line_max: int = DEFAULT_P7.agg_night_bytes_max,
                 export_max: int = DEFAULT_P7.agg_export_bytes_max) -> None:
        self.dir = Path(data_dir) / TUNING_DIR
        self.path = self.dir / NIGHTS_NAME
        self.last_path = self.dir / LAST_NAME
        self.keep = keep
        self.line_max = line_max
        self.export_max = export_max
        self.rejected = 0                    # lines refused (not a clean count line)
        self.duplicates = 0                  # lines of a night that was already written
        self._last: Optional[int] = None     # the night number of the newest line written (RAM copy of ``last_night``)

    # ------------------------------------------------------------ reading
    @staticmethod
    def _clean(obj) -> bool:
        return isinstance(obj, dict) and not tuningagg.scan(obj)

    def lines(self) -> list:
        """The kept lines, oldest first (at most ``keep``): only the lines that parse and pass the privacy scan, so a damaged
        or hand-edited file can never reach the pool or the export."""
        try:
            raw = self.path.read_bytes().decode("utf-8", "replace")
        except OSError:
            return []
        out = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if self._clean(obj):
                out.append(line)
        return out[-self.keep:]

    def count(self) -> int:
        return len(self.lines())

    def _last_night(self) -> Optional[int]:
        if self._last is None:
            try:
                self._last = int(self.last_path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                self._last = None
        return self._last

    # ------------------------------------------------------------ writing
    def add(self, night: int, text: str) -> str:
        """Appends one nightly line; keeps the newest ``keep``. ``stored``, ``duplicate`` (that night was written already) or
        ``rejected`` (not a clean count line: not a JSON object, a newline inside, longer than ``line_max`` bytes, or a
        failure of the privacy scan). An ``OSError`` of the write is raised to the caller, which counts it."""
        if not isinstance(text, str) or "\n" in text or "\r" in text or len(text.encode("utf-8")) > self.line_max:
            self.rejected += 1
            return REJECTED
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if not self._clean(obj):
            self.rejected += 1
            return REJECTED
        if isinstance(night, int) and not isinstance(night, bool) and night == self._last_night():
            self.duplicates += 1
            return DUPLICATE
        kept = self.lines()
        kept.append(json.dumps(obj, separators=(",", ":")))
        kept = kept[-self.keep:]
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, ("\n".join(kept) + "\n").encode("utf-8"))
        if isinstance(night, int) and not isinstance(night, bool):
            atomic_write(self.last_path, str(night).encode("ascii"))
            self._last = night
        return STORED

    # ------------------------------------------------------------ the pooled export
    def export(self) -> ExportResult:
        """The pooled record for the button: ``tuningagg.pool`` over the kept lines as compact JSON, after the privacy scan and
        the size cap. Pure with respect to the file (nothing is written)."""
        lines = self.lines()
        if not lines:
            return ExportResult(None, 0, "empty")
        try:
            pooled = tuningagg.pool(lines)
        except Exception:                                  # a shape the pool cannot add: never publish a half result
            return ExportResult(None, len(lines), "error")
        if tuningagg.scan(pooled):
            return ExportResult(None, len(lines), "unclean")
        payload = json.dumps(pooled, separators=(",", ":")).encode("utf-8")
        if len(payload) > self.export_max:
            return ExportResult(None, len(lines), "too_large")
        return ExportResult(payload, len(lines))

    # ------------------------------------------------------------ Forget all
    def purge(self) -> int:
        """Deletes the whole folder (Forget all learned data, spec 7.1). Never raises: a file that cannot be deleted is logged
        (its class only) and left. Returns the number of files removed."""
        removed = 0
        try:
            names = os.listdir(self.dir)
        except OSError:
            names = []
        for name in names:
            try:
                (self.dir / name).unlink()
                removed += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.error("cannot delete a tuning file: %s", exc.__class__.__name__)
        try:
            os.rmdir(self.dir)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error("cannot delete the tuning folder: %s", exc.__class__.__name__)
        self._last = None
        return removed
