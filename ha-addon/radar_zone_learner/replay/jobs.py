"""Replay jobs and profiles (P3 spec 11.8).

Job name: ``<world>_<window>[_<variant>]`` with world ``bed`` (bedroom/office, the primary case) or ``liv``
(living room), window ``none``, ``w0000_0600``, ``w2200_0600``, ``w2300_0700`` or ``w0000_0700``, and the
variants ``ghost`` (moderate seated-person ghosts, KJOBS ``k_ghost``: gs 0.15, gi 0.1), ``h300``, ``h900``,
``v3h300`` (dropout sweep, KJOBS ``k_H300``, ``k_H900``, ``c_V3H300``) and ``faults`` (11.3.4).
Night learning is always off in P3 (``nl`` 0).

P4 variants (OA 12 P4; ``replay.p4_scenarios``): ``p4`` (nothing is done for the bed: Night learning off, no
'Mark spot here'), ``p4nl`` (the user starts Night learning at the first minute; ``nl`` 7 makes the radar timeline
the one the learner's commands give), ``p4mark`` (scripted presses: failures, another item selected, then the bed
item), ``p4app`` (the learner's place at the bed is approved as 'Bed'), ``p4rej`` (the place the learner names
'bed' is rejected), ``p4names`` (names on approval) and ``p4nop2`` (Night learning started on a node without the
P2 firmware). All of them carry the BH1750 lux samples and none uses the P3 desk and bed labels.

Profiles. ``p3`` (G1-G6, 1,333 simulated days): bed and liv x the 5 windows, 21 nights, 5 seeds; ``bed_w0000_0600_ghost``
21 nights, 5 seeds; the dropout sweep ``h300``, ``h900``, ``v3h300`` and ``faults``, 14 nights, seeds 1-3.
``p4`` (P4-1 to P4-7, bedroom only, 47 runs, 707 simulated days) has NO run of the P3 jobs and no living-room job (the
P4 design is about the bedroom case): ``bed_w0000_0600_p4`` 21 nights x 5 seeds (P4-1, P4-7, P4-R1, P4-R2);
``bed_w2200_0600_p4`` and ``bed_w2300_0700_p4`` 14 nights x 5 seeds (P4-2); ``bed_<window>_p4nl`` for the 4 windows
14 nights x 5 seeds (P4-6); ``bed_w0000_0600_p4mark``, ``_p4app`` and ``_p4rej`` 16 nights x seeds 1-3 (P4-3, P4-4,
P4-7); ``bed_w0000_0600_p4names`` 14 nights x seeds 1-2 (P4-4); ``bed_none_p4nop2`` 3 nights x seed 1 (P4-5). The two
profiles together exceed the 1,500-day limit of one invocation, so G1-G6 and P4-1 to P4-7 are separate runs and a
p4 report says that G1-G6 were not run.
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
    "p4": dict(lux=True, script="base", labels=False),
    "p4nl": dict(lux=True, script="nl", labels=False, nl=7),
    "p4mark": dict(lux=True, script="mark", labels=False),
    "p4app": dict(lux=True, script="app", labels=False),
    "p4rej": dict(lux=True, script="rej", labels=False),
    "p4names": dict(lux=True, script="names", labels=False),
    "p4nop2": dict(lux=True, script="nop2", labels=False, p2=False),
    "p5setup": dict(lux=True, script="p5setup", labels=False),
    "p5ans": dict(lux=True, script="p5ans", labels=False),
    "p5push": dict(lux=True, script="p5push", labels=False),
    "p5adv": dict(lux=True, script="p5adv", labels=False),
    "p5rej": dict(lux=True, script="p5rej", labels=False),
    "p5zone": dict(lux=True, script="p5zone", labels=False, zone_change_items=True),
    "p5guests": dict(lux=True, script="p5guests", labels=False),
    "p5guestsc": dict(lux=True, script="p5guestsc", labels=False),        # the control: no pause
    "p5pets": dict(lux=True, script="p5pets", labels=False, pet=1),
}
P4_VARIANTS = ("p4", "p4nl", "p4mark", "p4app", "p4rej", "p4names", "p4nop2")
P5_VARIANTS = ("p5setup", "p5ans", "p5push", "p5adv", "p5rej", "p5zone", "p5guests", "p5guestsc", "p5pets")
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
    lux: bool = False          # P4: the stream carries the BH1750 lux samples (OA 7.5)
    script: str = ""           # P4 scenario (replay.p4_scenarios): base nl mark app rej names nop2
    labels: bool = True        # False: no P3 'Mark spot here' labels (desk, bed)
    p2: bool = True            # False: a node without the P2 firmware (no schedule topics)
    pet: int = 0               # P5: 1 = the world has a dog (``W.make_cfg(pet=1)``)
    zone_change_items: bool = False    # P5: ``P5Params.zone_change_items`` of the job's learner (merge and modify items)

    def cfg(self) -> dict:
        """The simulator world config of this job."""
        return W.make_cfg(bed=1 if self.world == "bed" else 0, Vm=self.vm, Hm=self.hm, gw=self.gw, gs=self.gs,
                          gi=self.gi, pet=self.pet, nw=self.window or "", nl=self.nl)

    def extras(self) -> list:
        """Scripted extra people ``(agent, x, y, t0_s, t1_s)`` for the stream.

        ``mark`` (OA 12 P4): the two people in bed, at 21:00-21:25 of day 8, before the 00:00 window start.
        ``p5setup`` (P5-4): one person awake in bed (OA 7.6: the user who presses 'Mark spot here' is awake in bed, which
        the simulator's sleeper is not: it is visible about 5 s in every 600 s, so a press almost never finds a stay), at
        22:00-23:58 of days 6 to 9, before the 00:00 window start and covering the evening stay of P1, which starts as
        early as about 22:40. The first setup of the replay is on day 7 in every seed."""
        bx, by = W.SPOT["bed"]
        if self.script == "mark":
            t0 = 8 * W.DAY + 21 * 3600
            return [("X1", bx, by, t0, t0 + 1500), ("X2", round(bx + 0.8, 3), by, t0, t0 + 1500)]
        if self.script == "p5setup":
            return [("X1", bx, by, d * W.DAY + 22 * 3600, d * W.DAY + 23 * 3600 + 58 * 60) for d in range(6, 10)]
        return []

    @property
    def is_p4(self) -> bool:
        return bool(self.script)

    @property
    def is_p5(self) -> bool:
        return self.script.startswith("p5")

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
    gated = wkey != "none" and variant in (None, "ghost")          # P4 variants: see replay.evaluate_p4
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
    "p4smoke": _jobs(["bed_w0000_0600_p4"], 9, (1,)),
    "p5smoke": _jobs(["bed_w0000_0600_p5setup"], 9, (1,)),
    "p4": (_jobs(["bed_w0000_0600_p4"], 21, ALL_SEEDS)
           + _jobs(["bed_w2200_0600_p4", "bed_w2300_0700_p4"], 14, ALL_SEEDS)
           + _jobs(["bed_%s_p4nl" % k for k in ("w0000_0600", "w2200_0600", "w2300_0700", "w0000_0700")],
                   14, ALL_SEEDS)
           + _jobs(["bed_w0000_0600_p4mark", "bed_w0000_0600_p4app", "bed_w0000_0600_p4rej"], 16, (1, 2, 3))
           + _jobs(["bed_w0000_0600_p4names"], 14, (1, 2))
           + _jobs(["bed_none_p4nop2"], 3, (1,))),
    "p5": (_jobs(["bed_w0000_0600_p5setup"], 14, ALL_SEEDS)
           + _jobs(["bed_w0000_0600_p5ans"], 14, (1, 2, 3))
           + _jobs(["bed_w0000_0600_p5push"], 16, (1, 2))
           + _jobs(["bed_w0000_0600_p5adv"], 16, (1, 2, 3))
           + _jobs(["bed_w0000_0600_p5rej"], 21, (1, 2))
           + _jobs(["bed_w0000_0600_p5zone"], 16, (1, 2))
           + _jobs(["bed_w0000_0600_p5guests", "bed_w0000_0600_p5guestsc"], 21, (1, 2))
           + _jobs(["bed_w0000_0600_p5pets"], 21, (1, 2))
           + _jobs(["bed_none_p5setup"], 9, (1,))
           + _jobs(["bed_w0000_0600_p4mark", "bed_w0000_0600_p4app", "bed_w0000_0600_p4rej"], 16, (1, 2, 3))),
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
