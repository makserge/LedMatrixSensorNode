"""State file, snapshots and quarantine in /data (spec 8.2, D26).

- ``state.json`` is written atomically: a temporary file in the same folder, flush, fsync, ``os.replace``,
  then (POSIX only) an fsync of the folder.
- Load order: ``state.json``; if it is not JSON or fails validation it is renamed to
  ``state.json.corrupt-<epoch_s>`` and the snapshots are tried newest first; nothing valid -> ``None``
  (start fresh). A file with a schema newer than this app's is renamed to ``state.json.newer-<schema>``,
  never overwritten, and the app starts fresh. A file with an older schema (back to ``MIN_SCHEMA_VERSION``)
  is loaded: the core migrates it (P4: schema 3 -> 4).
- ``snapshots/state-YYYY-MM-DD.json``: one per date, the 7 newest by date are kept.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from ..core.types import SCHEMA_VERSION

try:
    from ..core.types import MIN_SCHEMA_VERSION      # P4: older files are migrated by the core on load
except ImportError:                                  # a core without migrations: only the current schema loads
    MIN_SCHEMA_VERSION = SCHEMA_VERSION

KEEP_CORRUPT = 3                  # quarantined state files kept
STATE_NAME = "state.json"
SNAPSHOT_DIR = "snapshots"
SNAPSHOT_RE = re.compile(r"^state-(\d{4}-\d{2}-\d{2})\.json$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

log = logging.getLogger("radar_zone_learner.persistence")


class _Corrupt(Exception):
    pass


class _Newer(Exception):
    def __init__(self, schema: int) -> None:
        super().__init__(schema)
        self.schema = schema


def encode_state(state: dict) -> bytes:
    return json.dumps(state, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> int:
    """Writes ``data`` to ``path`` atomically; on failure the previous file stays and the error is raised."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)
    return len(data)


class StateStore:
    """Owns ``<data_dir>/state.json`` and ``<data_dir>/snapshots``.

    ``validate`` (optional) is called with a decoded state dict and raises ``ValueError`` (the core's
    ``StateError`` is one) when the dict is not a valid state; the service passes the learner factory.
    """

    def __init__(self, data_dir: Path, keep_snapshots: int = 7, *,
                 validate: Optional[Callable[[dict], object]] = None,
                 clock_s: Callable[[], float] = time.time) -> None:
        self.data_dir = Path(data_dir)
        self.keep_snapshots = keep_snapshots
        self.validate = validate
        self.clock_s = clock_s
        self.state_path = self.data_dir / STATE_NAME
        self.snapshot_dir = self.data_dir / SNAPSHOT_DIR
        self.loaded_from: Optional[str] = None      # "state", "snapshot <date>" or None (fresh)
        self.validated: object = None               # the validator's return value for the loaded dict

    # ------------------------------------------------------------ load
    def load(self) -> Optional[dict]:
        """Spec 8.2 order: state.json, newest valid snapshot, None."""
        self.loaded_from = None
        self.validated = None
        if self.state_path.exists():
            try:
                state, built = self._read(self.state_path)
            except _Newer as exc:
                dest = self._quarantine(self.state_path, f"newer-{exc.schema}")
                log.error("state file has schema %d, newer than this app's %d: kept as %s; starting fresh",
                          exc.schema, SCHEMA_VERSION, dest.name if dest else "(rename failed)")
                return None
            except _Corrupt as exc:
                dest = self._quarantine(self.state_path, f"corrupt-{int(self.clock_s())}")
                log.warning("state file unusable (%s): kept as %s; trying the snapshots",
                            exc, dest.name if dest else "(rename failed)")
            else:
                self.loaded_from = "state"
                self.validated = built
                return state
        for date_iso, path in self._snapshots(newest_first=True):
            try:
                state, built = self._read(path)
            except (_Corrupt, _Newer) as exc:
                log.warning("snapshot %s skipped (%s)", path.name,
                            f"schema {exc.schema}" if isinstance(exc, _Newer) else exc)
                continue
            log.warning("state restored from snapshot %s", path.name)
            self.loaded_from = f"snapshot {date_iso}"
            self.validated = built
            return state
        if self.state_path.exists() or any(True for _ in self._snapshots()):
            log.warning("no valid state file or snapshot: starting fresh")
        return None

    def _read(self, path: Path):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise _Corrupt(f"read error {exc.__class__.__name__}") from None
        try:
            state = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _Corrupt("not JSON") from None
        if not isinstance(state, dict):
            raise _Corrupt("not a JSON object")
        schema = state.get("schema")
        if isinstance(schema, int) and not isinstance(schema, bool) and schema > SCHEMA_VERSION:
            raise _Newer(schema)
        if isinstance(schema, bool) or not isinstance(schema, int) or schema < MIN_SCHEMA_VERSION:
            raise _Corrupt(f"schema {schema!r}")
        built = None
        if self.validate is not None:
            try:
                built = self.validate(state)
            except ValueError as exc:
                raise _Corrupt(f"invalid: {str(exc)[:200]}") from None
            except Exception as exc:
                # Not a validation result but a failure while loading it: quarantining the file keeps
                # the app starting (the Supervisor would otherwise restart it in a loop).
                log.error("loading %s failed with %s", path.name, exc.__class__.__name__, exc_info=True)
                raise _Corrupt(f"load error {exc.__class__.__name__}") from None
        return state, built

    def quarantine_state(self) -> Optional[Path]:
        """Renames ``state.json`` to ``state.json.corrupt-<epoch_s>`` (a file the learner rejected)."""
        if not self.state_path.exists():
            return None
        return self._quarantine(self.state_path, f"corrupt-{int(self.clock_s())}")

    def _quarantine(self, path: Path, suffix: str) -> Optional[Path]:
        dest = path.with_name(f"{path.name}.{suffix}")
        n = 1
        while dest.exists():
            dest = path.with_name(f"{path.name}.{suffix}-{n}")
            n += 1
        try:
            os.replace(path, dest)
        except OSError as exc:
            log.error("cannot rename %s: %s", path.name, exc.__class__.__name__)
            return None
        if suffix.startswith("corrupt-"):
            self._prune_corrupt()
        return dest

    def _prune_corrupt(self) -> None:
        """Keeps the newest KEEP_CORRUPT ``state.json.corrupt-*`` files (a file that keeps failing must not
        fill the disk); ``state.json.newer-*`` files are never deleted (D26)."""
        try:
            found = [(p.stat().st_mtime, p.name, p) for p in self.data_dir.glob(STATE_NAME + ".corrupt-*")]
        except OSError:
            return
        found.sort()
        for _, _, p in found[:max(0, len(found) - KEEP_CORRUPT)]:
            try:
                p.unlink()
            except OSError as exc:
                log.warning("cannot delete old %s: %s", p.name, exc.__class__.__name__)

    # ------------------------------------------------------------ save
    def save(self, state: dict) -> int:
        """Atomic write of ``state.json``; returns the bytes written. Raises on failure (old file kept)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return atomic_write(self.state_path, encode_state(state))

    def snapshot(self, state: dict, date_iso: str) -> None:
        """Writes ``snapshots/state-<date_iso>.json`` atomically and keeps the newest ``keep_snapshots``."""
        if not DATE_RE.match(date_iso or ""):
            raise ValueError("snapshot date must be YYYY-MM-DD")
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.snapshot_dir / f"state-{date_iso}.json", encode_state(state))
        for _, path in self._snapshots(newest_first=True)[self.keep_snapshots:]:
            try:
                path.unlink()
            except OSError as exc:
                log.warning("cannot delete old snapshot %s: %s", path.name, exc.__class__.__name__)

    def _snapshots(self, newest_first: bool = True) -> list:
        try:
            names = os.listdir(self.snapshot_dir)
        except OSError:
            return []
        found = []
        for name in names:
            m = SNAPSHOT_RE.match(name)
            if m:
                found.append((m.group(1), self.snapshot_dir / name))
        found.sort(reverse=newest_first)
        return found
