"""Debug capture (P5 spec 7.8): the raw frames stream written to ``<data_dir>/capture`` for offline replay. Shell only.

A switch ('Debug capture', OFF at every start, never resumed) makes the layer hand every frames message to this
object. While it is ON one JSON line per message is appended to ``capture/capture-<UTC YYYYMMDD-HH>.jsonl``:

    {"recv_ms": <int>, "topic": "<the frames topic>", "payload": "<the payload as UTF-8 text>"}

which is the second input format of ``replay/capture_replay.py``. Only the frames topic is recorded: no status,
schedule, context, answer or name. A capture holds raw positions, so it is bounded and kept out of backups:

- it switches itself OFF 48 hours after it was switched ON (``CAPTURE_MAX_ON_S``) and when the folder holds more than
  100 MiB (``CAPTURE_MAX_BYTES``);
- it deletes every ``capture-*.jsonl`` whose last write is older than 72 hours (``CAPTURE_KEEP_S``), at start and every
  hour, also while OFF;
- a write error switches it OFF, logs the exception class only, and is counted;
- ``purge()`` (Forget all learned data) deletes the folder's files and switches it OFF;
- ``config.yaml`` excludes ``capture/*`` from Home Assistant backups (a pattern relative to the data folder: the
  Supervisor matches the entry against the file's full path, so an absolute ``/data/...`` pattern matches nothing).

Positions are never logged. The monotonic clock drives the limits, the wall clock names the hourly files.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

CAPTURE_MAX_ON_S = 172800            # 48 h
CAPTURE_MAX_BYTES = 100 * 1024 * 1024
CAPTURE_KEEP_S = 259200              # 72 h
FLUSH_PERIOD_S = 5.0
CLEAN_PERIOD_S = 3600.0
CAPTURE_DIR = "capture"
FILE_RE = re.compile(r"^capture-\d{8}-\d{2}\.jsonl$")

log = logging.getLogger("radar_zone_learner.capture")


class DebugCapture:
    def __init__(self, data_dir, clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
                 monotonic: Callable[[], float] = time.monotonic, *, topic: str = "led_matrix_node/radar/ld2454/frames",
                 max_on_s: float = CAPTURE_MAX_ON_S, max_bytes: int = CAPTURE_MAX_BYTES,
                 keep_s: float = CAPTURE_KEEP_S) -> None:
        self.dir = Path(data_dir) / CAPTURE_DIR
        self.clock_ms = clock_ms
        self.monotonic = monotonic
        self.topic = topic
        self.max_on_s = max_on_s
        self.max_bytes = max_bytes
        self.keep_s = keep_s
        self.active = False
        self.lines = 0                    # messages written since the last start
        self.skipped = 0                  # payloads that were not valid UTF-8
        self.write_errors = 0
        self.stop_reason: Optional[str] = None     # "limit_time", "limit_size", "error", "manual", "purge"
        self._file = None
        self._file_name: Optional[str] = None
        self._on_at: Optional[float] = None
        self._flushed_at: Optional[float] = None
        self._cleaned_at: Optional[float] = None
        self._bytes = 0                   # what the folder holds (kept up to date, no listing per message)
        self._files = 0
        self._unflushed = False

    # ------------------------------------------------------------ switch
    def start(self) -> bool:
        """Switches the capture ON (a no-op while it is). False when the folder cannot be made."""
        if self.active:
            return True
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.write_errors += 1
            log.error("debug capture cannot start: %s", exc.__class__.__name__)
            return False
        self._clean(self.monotonic())
        now = self.monotonic()
        self.active = True
        self.stop_reason = None
        self.lines = 0
        self._on_at = self._flushed_at = now
        log.info("debug capture ON (frames only; it switches itself off after 48 h)")
        return True

    def stop(self, reason: str = "manual") -> None:
        """Switches the capture OFF and closes the file (flushed)."""
        was = self.active
        self.active = False
        self._close()
        if was:
            self.stop_reason = reason
            log.info("debug capture OFF (%s)", reason)

    def purge(self) -> int:
        """Forget all (spec 7.2): switches OFF and deletes every ``capture-*.jsonl``. Returns the number deleted."""
        self.stop("purge")
        n = 0
        for path in self._capture_files():
            try:
                path.unlink()
                n += 1
            except OSError as exc:
                log.warning("cannot delete %s: %s", path.name, exc.__class__.__name__)
        self._rescan()
        return n

    # ------------------------------------------------------------ frames
    def on_frames(self, recv_ms: int, payload) -> None:
        """Appends one line for a frames message while the capture is ON; anything else is ignored."""
        if not self.active:
            return
        try:
            text = bytes(payload).decode("utf-8")
        except (UnicodeDecodeError, TypeError, ValueError):
            self.skipped += 1
            return
        line = (json.dumps({"recv_ms": int(recv_ms), "topic": self.topic, "payload": text}, separators=(",", ":"))
                + "\n").encode("utf-8")
        try:
            f = self._file_for(self.clock_ms())
            f.write(line)
        except OSError as exc:
            self.write_errors += 1
            log.error("debug capture write failed: %s; switching it OFF", exc.__class__.__name__)
            self.stop("error")
            return
        self._bytes += len(line)
        self.lines += 1
        self._unflushed = True

    def _file_for(self, wall_ms: int):
        s = wall_ms // 1000
        name = "capture-%s.jsonl" % time.strftime("%Y%m%d-%H", time.gmtime(s))
        if self._file is None or name != self._file_name:
            self._close()
            path = self.dir / name
            new = not path.exists()
            self._file = open(path, "ab")
            self._file_name = name
            if new:
                self._files += 1
        return self._file

    def _close(self) -> None:
        f, self._file, self._file_name = self._file, None, None
        self._unflushed = False
        if f is not None:
            try:
                f.flush()
                f.close()
            except OSError as exc:
                self.write_errors += 1
                log.error("debug capture close failed: %s", exc.__class__.__name__)

    # ------------------------------------------------------------ housekeeping (once per second from the layer)
    def tick(self) -> None:
        now = self.monotonic()
        if self.active:
            if self._on_at is not None and now - self._on_at >= self.max_on_s:
                self.stop("limit_time")
            elif self._bytes > self.max_bytes:
                self.stop("limit_size")
            elif self._unflushed and self._file is not None and now - (self._flushed_at or 0.0) >= FLUSH_PERIOD_S:
                try:
                    self._file.flush()
                except OSError as exc:
                    self.write_errors += 1
                    log.error("debug capture flush failed: %s; switching it OFF", exc.__class__.__name__)
                    self.stop("error")
                else:
                    self._unflushed = False
                    self._flushed_at = now
        if self._cleaned_at is None or now - self._cleaned_at >= CLEAN_PERIOD_S:
            self._clean(now)

    def _clean(self, now: float) -> None:
        """Deletes the files whose last write is older than ``keep_s`` (also while OFF)."""
        self._cleaned_at = now
        wall_s = self.clock_ms() / 1000.0
        for path in self._capture_files():
            if self._file_name == path.name:
                continue
            try:
                age = wall_s - path.stat().st_mtime
            except OSError:
                continue
            if age > self.keep_s:
                try:
                    path.unlink()
                except OSError as exc:
                    log.warning("cannot delete %s: %s", path.name, exc.__class__.__name__)
        self._rescan()

    def _capture_files(self) -> list:
        try:
            return sorted(p for p in self.dir.iterdir() if FILE_RE.match(p.name))
        except OSError:
            return []

    def _rescan(self) -> None:
        files = self._capture_files()
        total = 0
        for p in files:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        self._files, self._bytes = len(files), total

    def stats(self) -> dict:
        """Counts only, for the status attribute ``capture`` (no name, no position)."""
        return {"on": self.active, "files": self._files, "bytes": self._bytes}
