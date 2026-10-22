"""Self-measurement for the P3 resource check (spec 12): RSS and CPU share of this process.

- ``rss_mb()``: resident set size now (Linux ``/proc/self/status`` VmRSS; Windows ``WorkingSetSize`` from
  ``K32GetProcessMemoryInfo``); ``None`` elsewhere.
- ``peak_rss_mb()``: the peak (Linux VmHWM; Windows ``PeakWorkingSetSize``); used by the replay probe.
- ``CpuMeter``: process CPU time (all threads, ``time.process_time()``) over a sliding 1 h window.
MB here is MiB (1,048,576 bytes).
"""
from __future__ import annotations

import os
import time
from collections import deque
from typing import Callable, Optional

WINDOW_S = 3600.0
_MIB = 1024.0 * 1024.0


def _proc_status_kb(key: str, path: str = "/proc/self/status") -> Optional[float]:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith(key + ":"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        unit = parts[2].lower() if len(parts) >= 3 else "kb"
                        value = float(parts[1])
                        return value if unit == "kb" else value / 1024.0
                    return None
    except OSError:
        return None
    return None


def _windows_memory() -> Optional[tuple]:
    """(WorkingSetSize, PeakWorkingSetSize) in bytes for this process, or None."""
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                                                     wintypes.DWORD]
        kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if not kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters),
                                                counters.cb):
            return None
        return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)
    except Exception:
        return None


def rss_mb() -> Optional[float]:
    if os.name == "nt":
        mem = _windows_memory()
        return mem[0] / _MIB if mem else None
    kb = _proc_status_kb("VmRSS")
    return kb / 1024.0 if kb is not None else None


def peak_rss_mb() -> Optional[float]:
    if os.name == "nt":
        mem = _windows_memory()
        return mem[1] / _MIB if mem else None
    kb = _proc_status_kb("VmHWM")
    return kb / 1024.0 if kb is not None else None


class CpuMeter:
    """CPU share of this process in percent of one core over the last hour (or since the first sample)."""

    def __init__(self, process_time: Callable[[], float] = time.process_time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._process_time = process_time
        self._monotonic = monotonic
        self._samples: deque = deque()

    def sample(self) -> None:
        now = self._monotonic()
        self._samples.append((now, self._process_time()))
        # keep the newest sample at or before now - 1 h, so the window spans a full hour once it can
        while len(self._samples) >= 2 and self._samples[1][0] <= now - WINDOW_S:
            self._samples.popleft()

    def percent_1h(self) -> Optional[float]:
        if len(self._samples) < 2:
            return None
        (m0, c0), (m1, c1) = self._samples[0], self._samples[-1]
        if m1 - m0 <= 0:
            return None
        return max(0.0, (c1 - c0) / (m1 - m0) * 100.0)
