"""Replay jobs and profiles (P3 spec 11.8).

Job name: ``<world>_<window>[_<variant>]`` with world ``bed`` (bedroom/office, the primary case) or ``liv``
(living room), window ``none``, ``w0000_0600``, ``w2200_0600``, ``w2300_0700`` or ``w0000_0700``, and the
variants ``ghost`` (moderate seated-person ghosts, KJOBS ``k_ghost``: gs 0.15, gi 0.1), ``h300``, ``h900``,
``v3h300`` (dropout sweep, KJOBS ``k_H300``, ``k_H900``, ``c_V3H300``) and ``faults`` (11.3.4).
Night learning is always off in P3 (``nl`` 0).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Optional

from . import world as W

WINDOWS = {"none": None, "w0000_0600": "00:00-06:00", "w2200_0600": "22:00-06:00",
           "w2300_0700": "23:00-07:00", "w0000_0700": "00:00-07:00"}
MAIN_WINDOWS = ("none", "w0000_0600", "w2200_0600", "w2300_0700", "w0000_0700")
VARIANTS = {
    "ghost": dict(gs=0.15, gi=0.1),
    "h300": dict(hm=300),
    "h900": dict(hm=900),
    "v3h300": dict(vm=3, hm=300),
    "faults": dict(faults=True),
}
ALL_SEEDS = (1, 2, 3, 4, 5)
MAX_NIGHTS = 28
NIGHTLY_DAY_FRACTION = 3.5 / 24.0          # the night-N run happens at 03:30 of day N


@dataclass(frozen=True)
class JobSpec:
    name: str                  # e.g. "bed_w0000_0600", "liv_none", "bed_w0000_0600_h300"
    world: str                 # "bed" | "liv"
    window: Optional[str]      # "HH:MM-HH:MM" or None
    nl: int = 0                # Night learning nights (P3: always 0)
    vm: int = 15
    hm: int = 90               # visible / hidden spell means for 'sit' (SIM:84)
    gw: float = 0.25
    gs: float = 0.0
    gi: float = 0.0            # ghosts (SIM:84; KJOBS)
    faults: bool = False       # Section 11.3.4
    nights: int = 21
    gated: bool = True         # False: reported only
    seeds: tuple = field(default=ALL_SEEDS)     # the profile's seeds for this job (P3 spec 11.8)

    def cfg(self) -> dict:
        """The simulator world config of this job."""
        return W.make_cfg(bed=1 if self.world == "bed" else 0, Vm=self.vm, Hm=self.hm, gw=self.gw, gs=self.gs,
                          gi=self.gi, nw=self.window or "", nl=self.nl)

    @property
    def window_key(self) -> str:
        return window_key(self.window)

    @property
    def variant(self) -> str:
        parts = self.name.split("_")
        base = 2 if parts[1] == "none" else 3
        return "_".join(parts[base:])

    def sim_days(self, nights: Optional[int] = None) -> float:
        return (self.nights if nights is None else nights) + NIGHTLY_DAY_FRACTION


def window_key(window: Optional[str]) -> str:
    if window is None:
        return "none"
    a, b = window.split("-")
    return "w" + a.replace(":", "") + "_" + b.replace(":", "")


_NAME_RE = re.compile(r"^(bed|liv)_(none|w\d{4}_\d{4})(?:_([a-z0-9]+))?$")


def parse_job(name: str, nights: Optional[int] = None, seeds: Optional[tuple] = None) -> JobSpec:
    """Build a JobSpec from its name (ValueError for an unknown world, window or variant)."""
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError("bad job name %r" % name)
    world, wkey, variant = m.group(1), m.group(2), m.group(3)
    if wkey not in WINDOWS:
        raise ValueError("unknown window %r" % wkey)
    kw = dict(name=name, world=world, window=WINDOWS[wkey])
    if variant is not None:
        if variant not in VARIANTS:
            raise ValueError("unknown variant %r" % variant)
        kw.update(VARIANTS[variant])
    job = JobSpec(**kw)
    # reported-only jobs: the no-window baseline (R1), the dropout sweep (R4) and the fault run (R6)
    gated = wkey != "none" and variant in (None, "ghost")
    job = replace(job, gated=gated)
    if nights is not None:
        job = replace(job, nights=nights)
    if seeds is not None:
        job = replace(job, seeds=tuple(seeds))
    return job


def _jobs(names, nights, seeds) -> list:
    return [parse_job(n, nights=nights, seeds=seeds) for n in names]


PROFILES: dict = {
    "smoke": _jobs(["bed_w0000_0600"], 8, (1,)),
    "quick": _jobs(["bed_w0000_0600", "liv_w0000_0600"], 14, (1, 2, 3)),
    "p3": (_jobs(["%s_%s" % (w, k) for w in ("bed", "liv") for k in MAIN_WINDOWS], 21, ALL_SEEDS)
           + _jobs(["bed_w0000_0600_ghost"], 21, ALL_SEEDS)
           + _jobs(["bed_w0000_0600_h300", "bed_w0000_0600_h900", "bed_w0000_0600_v3h300"], 14, (1, 2, 3))
           + _jobs(["bed_w0000_0600_faults"], 14, (1, 2, 3))),
}


def runs_of(jobs) -> list:
    """(job, seed) pairs of a job list."""
    return [(j, s) for j in jobs for s in j.seeds]


def estimate_days(jobs) -> float:
    return sum(j.sim_days() * len(j.seeds) for j in jobs)
