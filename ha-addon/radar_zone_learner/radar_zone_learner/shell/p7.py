"""The P7 shell layer: the tuning data (P7 spec 4.8, 7.1, 12.1).

``P7Layer`` is a ``P6Layer`` and **the one layer object**: ``main`` passes it as the service's ``p4=`` argument, as in P5 and
P6, so there is still one ``NodeLink``, one ``RfStats`` and one event buffer and no event is handled twice. It overrides only
what P7 changes (discovery, republish, bind, the event and output paths, the attributes, the Forget all hook) and inherits the
rest unchanged; the service needs no new hook. Everything here runs on the worker thread.

The review items of P7 (a modify with its source, a merge, a retire, the radar-moved item) need nothing here: they are ordinary
``ReviewItem`` values, the P5 layer shows them (``review_p5`` writes their texts, ``select_model`` their labels) and the push
request carries the attention flag (``review_p5.push_state`` writes it). The layer decides nothing about learning.

What it does:

- takes each ``TuningLine`` the core makes at the end of a nightly run and keeps it **locally** (``tuningexport.TuningStore``:
  the newest 28 lines in ``/data/tuning/nights.jsonl``, excluded from Home Assistant backups, deleted by Forget all);
- answers the button 'Export tuning data' (``ExportTuningPress``) by publishing **one pooled record** (sums and histograms over
  the kept nights: no per-night series, no date) once on ``radar_zones/learner/tuning/export``, **not retained**; the record is
  scanned for privacy and size first, and a refusal is only counted. No file is written for the export (spec 4.8);
- shows ``none`` or ``<n> nights`` on the diagnostic sensor 'Tuning data' (retained), and the counters ``tuning_exports``,
  ``tuning_export_refused``, ``tuning_write_errors`` and ``p7_shell_errors`` in the status attributes (counts only).

HA[U8] (assumed, not verified in Home Assistant): a non-retained MQTT message of up to 64 KiB is delivered to the 'Listen to a
topic' tool of Home Assistant's MQTT integration (and to ``mosquitto_sub``) and can be copied out; acceptance step A7.
HA[U9] (assumed): the push request state with ``kind`` ``attention`` fires the blueprint's state trigger like ``setup`` and
``daily``; step A6. HA[U10] (assumed): a modify that changes only the ``poly_cm`` attribute of a zone leaves the entity
registry, the entity ids, the names and the history untouched; step A1 (the add-on keeps the zone id, hence the ``unique_id``).

A Python error in this code is contained and counted (``p7_shell_errors``) and never reaches the P6 behaviour; the service's own
containment (``Service._p4_call``) stays behind it.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from ..core.params import DEFAULT_P6, DEFAULT_P7
from ..core.types import TuningLine
from .discovery_p7 import p7_discovery_messages
from .p6 import P6Layer
from .select_model import SELECT_MODE
from .topics_p7 import ExportTuningPress, TopicsP7
from .tuningexport import REJECTED, STORED, TuningStore, state_text

log = logging.getLogger("radar_zone_learner.p7")


class P7Layer(P6Layer):
    def __init__(self, topics, publish: Callable[..., bool], data_dir=None, *,
                 monotonic: Callable[[], float] = time.monotonic, select_mode: str = SELECT_MODE,
                 debug_map: bool = True,
                 clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
                 p6params=DEFAULT_P6, day_of: Optional[Callable[[int], int]] = None, p7params=DEFAULT_P7) -> None:
        if not isinstance(topics, TopicsP7):
            raise TypeError("P7Layer needs the topics of build_topics_p7")
        super().__init__(topics, publish, data_dir, monotonic=monotonic, select_mode=select_mode, debug_map=debug_map,
                         clock_ms=clock_ms, p6params=p6params, day_of=day_of)
        self.p7params = p7params
        self.tuning: Optional[TuningStore] = (
            TuningStore(data_dir, keep=p7params.agg_keep_nights, line_max=p7params.agg_night_bytes_max,
                        export_max=p7params.agg_export_bytes_max) if data_dir is not None else None)
        self.tuning_exports = 0               # pooled records published
        self.tuning_export_refused = 0        # presses that published nothing (empty, unclean, too large, link down)
        self.tuning_write_errors = 0          # nightly lines the disk would not take
        self.p7_shell_errors = 0
        self._tuning_state = state_text(0)

    # ------------------------------------------------------------ containment
    def _p7_safe(self, fn, *args):
        try:
            return fn(*args)
        except Exception:
            self.p7_shell_errors += 1
            log.exception("error in the P7 code (%s)", getattr(fn, "__name__", "call"))
            return None

    # ------------------------------------------------------------ lifecycle
    def bind(self, core_call: Callable, learner) -> None:
        """Called by ``Service.start()`` and again after a Forget all swap: the sensor state is read from the local file."""
        super().bind(core_call, learner)
        self._p7_safe(self._tuning_refresh)

    def reset(self) -> None:
        """Forget all: the P6 mirrors are cleared; the tuning folder was deleted by ``purge_capture`` just before."""
        super().reset()
        self._p7_safe(self._tuning_refresh)

    def purge_capture(self) -> int:
        """The service's step 3 of Forget all: the raw-frame capture is deleted (P5) and so is the tuning folder (P7 spec 7.1:
        the per-night counts are learned data). Uses the existing hook, so the service gets no new one."""
        n = super().purge_capture()
        self._p7_safe(self._purge_tuning)
        return n

    def _purge_tuning(self) -> None:
        if self.tuning is not None:
            self.tuning.purge()
        self._tuning_refresh()

    # ------------------------------------------------------------ discovery and states
    def discovery(self) -> list:
        """P6's messages and the two P7 configs: the button 'Export tuning data' and the sensor 'Tuning data'."""
        return super().discovery() + p7_discovery_messages(self.topics)

    def republish(self, now_ms: int) -> None:
        super().republish(now_ms)
        self._p7_safe(self._tuning_refresh)
        self._pub(self.topics.learner_tuning, self._tuning_state)

    def attributes(self) -> dict:
        out = super().attributes()
        out.update({"tuning_exports": self.tuning_exports, "tuning_export_refused": self.tuning_export_refused,
                    "tuning_write_errors": self.tuning_write_errors, "p7_shell_errors": self.p7_shell_errors})
        return out

    # ------------------------------------------------------------ input events
    def on_event(self, ev, now_ms: int) -> list:
        if isinstance(ev, ExportTuningPress):
            self._p7_safe(self._export)
            return []
        return super().on_event(ev, now_ms)

    # ------------------------------------------------------------ core outputs
    def on_output(self, out, now_ms: int) -> bool:
        """True when the output was consumed here (the service must not handle it again)."""
        if isinstance(out, TuningLine):
            self._p7_safe(self._tuning_line, out)
            return True
        return super().on_output(out, now_ms)

    # ------------------------------------------------------------ the tuning data
    def _tuning_refresh(self) -> None:
        """The sensor state from the kept lines; published when it changed (``_pub`` skips an equal payload)."""
        n = self.tuning.count() if self.tuning is not None else 0
        self._tuning_state = state_text(n)
        self._pub(self.topics.learner_tuning, self._tuning_state)

    def _tuning_line(self, line: TuningLine) -> None:
        store = self.tuning
        if store is None:
            return
        try:
            res = store.add(line.night, line.text)
        except OSError as exc:
            self.tuning_write_errors += 1
            log.error("saving the nightly tuning line failed: %s", exc.__class__.__name__)
            return
        if res == STORED:
            self._tuning_refresh()
        elif res == REJECTED:
            log.warning("a nightly tuning line was refused: it is not a clean count line")

    def _export(self) -> None:
        """The button: one pooled record to a topic that is not retained. Nothing is written to a file, nothing is retried."""
        res = self.tuning.export() if self.tuning is not None else None
        if res is None or res.payload is None:
            self.tuning_export_refused += 1
            log.info("tuning export: nothing published (%s)", res.reason if res is not None else "empty")
            return
        if self._publish(self.topics.learner_tuning_export, res.payload, qos=1, retain=False):
            self.tuning_exports += 1
            log.info("tuning export: %d nights", res.n)
        else:
            self.tuning_export_refused += 1
            log.warning("tuning export: not sent, the broker link is down")
