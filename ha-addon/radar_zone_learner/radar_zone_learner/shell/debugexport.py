"""The debug-data export (0.7.2): a copy of the raw capture frames and of ``state.json`` into the Home Assistant ``share`` folder.

Why: the capture (``/data/capture``) and the state live in the app's private ``/data``, which nobody can reach from the Samba
share or the Terminal & SSH app, so a failing run could not be handed to the developer. ``config.yaml`` maps ``share:rw``; the app
sees it as ``/share``. A press of the diagnostic button 'Export debug data' (and only that: nothing is exported by itself) makes

    /share/radar_zone_learner/export-<UTC YYYYMMDD-HHMMSS>/
        capture-*.jsonl     every current capture file (frames only), newest first until the budget is used
        state.json          a copy of the state file (the learned map, zones, stays)
        README.txt          what the folder holds

The copy holds **raw positions and the learned map**. It is for the owner and the developer; delete it when done. Bounds: at most
``MAX_BYTES`` (100 MiB) are copied (the state first, then the newest capture files that still fit), only the newest
``KEEP`` (3) ``export-*`` folders are kept (older ones are deleted, only inside ``<share>/radar_zone_learner`` and only a real
folder whose name matches ``export-<8 digits>-<6 digits>``), a symbolic link is never followed (a link is skipped, and so is a
base folder that is a link), the folder is written under a temporary name and renamed (an export is complete or absent), and
nothing is exported when there is no capture file and no state. Standard library only; nothing here logs a path or a value.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SHARE_ROOT = "/share"                    # the Home Assistant ``share`` folder as the app sees it (config.yaml ``map:``)
SHARE_SUBDIR = "radar_zone_learner"
MAX_BYTES = 100 * 1024 * 1024
KEEP = 3
EXPORT_RE = re.compile(r"^export-\d{8}-\d{6}$")
TMP_PREFIX = ".tmp-export-"
CAPTURE_FILE_RE = re.compile(r"^capture-\d{8}-\d{2}\.jsonl$")
STATE_NAME = "state.json"
CHUNK = 1024 * 1024

log = logging.getLogger("radar_zone_learner.debugexport")

README = """Radar Zone Learner: debug data export

This folder is a COPY made when you pressed 'Export debug data'. It holds:

- capture-*.jsonl  the raw radar frames of the Debug capture switch (positions of people), one file per UTC hour,
                   {files} file(s), {capture_bytes} bytes{left_out}
- state.json       the learned map: zones, stays, labels and counts{state_note}

Both contain raw positions and where you live in the room. They are for you and the developer only.
Delete this folder when you are done (the app keeps only the newest {keep} export folders, and never exports by itself).
Nothing here is sent anywhere by the app.
"""


@dataclass(frozen=True)
class DebugExportResult:
    """``ok`` with the folder name, file count and bytes; or not ok with ``reason``: ``empty``, ``too_large``, ``exists`` or
    ``error`` (``error`` covers a missing or unwritable share folder, a link, a full disk)."""
    ok: bool
    reason: str = ""
    folder: str = ""
    files: int = 0
    bytes: int = 0
    left_out: int = 0
    pruned: int = 0


def _regular(path: Path) -> Optional[int]:
    """The size of a regular file that is not a link, else None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_size if stat.S_ISREG(st.st_mode) else None


def _copy(src: Path, dst: Path, size: int, trim_lines: bool) -> int:
    """Copies at most ``size`` bytes (the size when it was listed: a file that grows meanwhile is cut there), never through a
    link; a capture file is cut after its last complete line. Returns the bytes written."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(src, flags)
    written = 0
    last_nl = 0
    with os.fdopen(fd, "rb") as fin, open(dst, "xb") as fout:
        left = size
        while left > 0:
            chunk = fin.read(min(CHUNK, left))
            if not chunk:
                break
            fout.write(chunk)
            if trim_lines:
                i = chunk.rfind(b"\n")
                if i >= 0:
                    last_nl = written + i + 1
            written += len(chunk)
            left -= len(chunk)
        if trim_lines and last_nl != written:
            fout.truncate(last_nl)
            written = last_nl
        fout.flush()
        os.fsync(fout.fileno())
    return written


def export_debug(data_dir, share_root=SHARE_ROOT, now_s: Optional[float] = None, *, max_bytes: int = MAX_BYTES,
                 keep: int = KEEP) -> DebugExportResult:
    """One export. Never raises: a failure is a result with ``ok`` False."""
    base = Path(share_root) / SHARE_SUBDIR
    tmp: Optional[Path] = None
    try:
        data = Path(data_dir)
        cap_dir = data / "capture"
        state_size = _regular(data / STATE_NAME)
        try:
            names = sorted((p for p in cap_dir.iterdir() if CAPTURE_FILE_RE.match(p.name)), reverse=True)
        except OSError:
            names = []
        caps = [(p, s) for p in names for s in (_regular(p),) if s is not None and s > 0]
        if not caps and state_size is None:
            return DebugExportResult(False, "empty")
        if state_size is not None and state_size > max_bytes:
            return DebugExportResult(False, "too_large")
        if not Path(share_root).is_dir():
            return DebugExportResult(False, "error")
        if base.is_symlink():
            return DebugExportResult(False, "error")
        base.mkdir(exist_ok=True)
        _remove_stale_tmp(base)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() if now_s is None else now_s))
        final = base / ("export-" + stamp)
        if final.exists() or final.is_symlink():
            return DebugExportResult(False, "exists")
        tmp = base / (TMP_PREFIX + stamp)
        tmp.mkdir()
        budget = max_bytes - (state_size or 0)
        files = left_out = cap_bytes = 0
        total = 0
        if state_size is not None:
            total += _copy(data / STATE_NAME, tmp / STATE_NAME, state_size, False)
            files += 1
        for path, size in caps:                       # newest first: the newest hours matter most
            if size > budget:
                left_out += 1
                continue
            got = _copy(path, tmp / path.name, size, True)
            budget -= size
            cap_bytes += got
            total += got
            files += 1
        n_caps = files - (1 if state_size is not None else 0)
        text = README.format(files=n_caps, capture_bytes=cap_bytes, keep=keep,
                             left_out=(" (%d older file(s) left out: the 100 MiB limit)" % left_out) if left_out else "",
                             state_note="" if state_size is not None else " (there was no state file)")
        (tmp / "README.txt").write_text(text, encoding="ascii", newline="\n")
        os.rename(tmp, final)
        tmp = None
        pruned = _prune(base, keep)
        return DebugExportResult(True, "", final.name, files + 1, total, left_out, pruned)
    except Exception as exc:                          # a full disk, a missing folder, a permission error: counted, not raised
        log.error("debug export failed: %s", exc.__class__.__name__)
        return DebugExportResult(False, "error")
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _remove_stale_tmp(base: Path) -> None:
    try:
        for p in base.iterdir():
            if p.name.startswith(TMP_PREFIX) and p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _prune(base: Path, keep: int) -> int:
    """Deletes all but the newest ``keep`` ``export-*`` folders inside ``base``. Returns how many were deleted."""
    try:
        found = sorted(p for p in base.iterdir() if EXPORT_RE.match(p.name) and p.is_dir() and not p.is_symlink())
    except OSError:
        return 0
    n = 0
    for p in found[:max(0, len(found) - keep)]:
        shutil.rmtree(p, ignore_errors=True)
        n += 1
    return n
