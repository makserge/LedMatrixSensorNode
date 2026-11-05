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

P6 variants (P6 spec 11.6; ``replay.p6_scenarios``, ``replay.p6_people``): ``p6edge``, ``p6edge05``, ``p6edge08`` (the
walkers and the edge sweep at detection noise 0.02, 0.05 and 0.08 m: P6-1, P6-2, P6-15), ``p6hold`` (hidden spells:
P6-3, P6-7), ``p6pow`` (stayers across the radar-off window, the fault run and scripted drop windows: P6-4, P6-5,
P6-6), ``p6door`` (door passages and exits: P6-8), ``p6ld`` and ``p6ldghost`` (LD2412 evidence and early clear: P6-9),
``p6count`` (several people in one zone: P6-14), ``p6setup`` (the approval flow only, the world's agents stay: P6-10),
``p6guests`` and ``p6guestsc`` (the pause pair: P6-11). From day 8 on the world's own agents are dropped
(``quiet_from_day``) and scripted people take their place; every P6 job carries the P5 ``p5setup`` person X1.

P7 variants (P7 spec 8.2, 8.3; ``replay.p7_scenarios``): the sofa moved 1.1 m or 2.9 m on day 35 (``p7mv11``, ``p7mv29``; with the
user's empty-room switch-off ``p7mv11e``, ``p7mv29e``; away on day 28 and back on day 49 ``p7mvback``), the controls (``p7ctl``,
``p7ctle``, ``p7ctlnp`` with the probation rule off), the table retired on day 14 (``p7ret``), the three beds (``p7bed`` the label bed,
``p7bednl`` Night learning, ``p7bednf`` the label bed without the static freeze), the radar turned or shifted on day 28 at 08:00
(``p7rot`` 10 degrees, ``p7rot3``, ``p7rot6``, ``p7shift`` 0.8 m, ``p7shift5`` 0.5 m, ``p7reset`` with the Approve of the reset), the
one-week guest (``p7guest``, ``p7guestp`` with the pause switch, ``p7guestnp`` without the probation rule) and the tuning jobs
(``p7tune``, ``p7tuneh300``, ``p7tuneh900``: hidden-spell means 90, 300 and 900 s). The learner of a P7 job has ``P7Params(enabled=True)``;
the learner of every other job has the default (disabled), which is P6 byte for byte. The profiles are ``p7smoke``, ``p7move``,
``p7drift``, ``p7beds``, ``p7radar``, ``p7guests``, ``p7tune`` and ``p7opt`` (the last one is reported only).

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

# ---------------------------------------------------------------- P6 (spec 11.1, 11.6)
SCRIPT_FROM_DAY = 8                        # the first day of scripted people; the zones of the setup exist from night 7
P6_SPOTS = {"desk": W.SPOT["desk"], "table": W.SPOT["table"], "counter": W.SPOT["counter"],
            "sofaAB": (round((W.SPOT["sofaA"][0] + W.SPOT["sofaB"][0]) / 2, 3),
                       round((W.SPOT["sofaA"][1] + W.SPOT["sofaB"][1]) / 2, 3)),
            "bed": W.SPOT["bed"]}                                    # the world's spots of the zones under test
P6_SPOT_KIND = {"desk": "dwell", "table": "dwell", "sofaAB": "dwell", "counter": "short_task", "bed": "bed"}
P6_DEFAULT_DELAY_S = {"bed": 600, "dwell": 300, "short_task": 60}     # P6Params.delay_default_s (fixed at construction)
P6_SPEEDS = (0.7, 1.0, 1.2, 0.5)           # walker speeds m/s; 0.5 is reported only
P6_SPELL_FACTORS = (0.5, 0.9, 1.1, 1.5)    # hidden spells as multiples of the default delay of the spot's kind
P6_SPELL_MIN_S = 30
P6_GAP_S = 5                               # a scripted drop window (shorter than the 15 s POWER rule)
P6_GAP_TIMES_S = (10 * 3600 + 20, 11 * 3600 + 20)       # seconds of a day at which a scripted gap starts (days 9-20)
DOOR_SITE = (4.6, 2.6)                     # the scripted door of p6door: beyond the east wall, away from the world's own traffic
DOOR_PROBE = (4.35, 2.6)                   # the door-side label stayer of p6door (0.25 m inside the door)
REFLECTOR_LABEL_DAY = 8                    # p6ldghost: the label stayer at the reflector (day, start s, end s)
REFLECTOR_LABEL_S = (10 * 3600, 11 * 3600 + 1800)       # the stay of a label press: p6ldghost (Y1) and p6door (a scripted stayer)


def _spells() -> tuple:
    """The hidden-spell lengths of P6-3 and P6-7: ``(spot, seconds)`` for each of the 4 factors of the DEFAULT delay of
    the spot's kind (never the delay in force: that changes at the nightly run, after the day was generated)."""
    out = []
    for spot in ("desk", "table", "counter", "sofaAB", "bed"):
        d = P6_DEFAULT_DELAY_S[P6_SPOT_KIND[spot]]
        for f in P6_SPELL_FACTORS:
            out.append((spot, max(P6_SPELL_MIN_S, int(round(f * d)))))
    return tuple(out)


def _gaps() -> tuple:
    """The scripted drop windows of p6pow: ``(start_s, dur_s)`` twice a day on days 9-20 (the dropped messages are
    recorded by the stream)."""
    return tuple((d * W.DAY + t, P6_GAP_S) for d in range(9, 21) for t in P6_GAP_TIMES_S)


_P6 = dict(lux=True, labels=False, quiet_from_day=SCRIPT_FROM_DAY)
VARIANTS.update({
    "p6edge": dict(_P6, script="p6edge", noise_m=0.02, crossings=P6_SPEEDS, programs=("walkers", "sweep")),
    "p6edge05": dict(_P6, script="p6edge05", noise_m=0.05, crossings=P6_SPEEDS, programs=("walkers", "sweep")),
    "p6edge08": dict(_P6, script="p6edge08", noise_m=0.08, crossings=P6_SPEEDS, programs=("walkers", "sweep")),
    "p6hold": dict(_P6, script="p6hold", spells=_spells(), programs=("hold",)),
    "p6pow": dict(_P6, script="p6pow", faults=True, gaps=_gaps(), programs=("pow",)),
    "p6door": dict(_P6, script="p6door", programs=("door",)),
    "p6ld": dict(_P6, script="p6ld", programs=("ld",)),
    "p6ldghost": dict(_P6, script="p6ldghost", programs=()),
    "p6count": dict(_P6, script="p6count", programs=("count",)),
    "p6setup": dict(lux=True, labels=False, script="p6setup", quiet_from_day=None, programs=()),
    "p6guests": dict(_P6, script="p6guests", programs=("busy",)),
    "p6guestsc": dict(_P6, script="p6guestsc", programs=("busy",)),         # the control: no pause
})
# ---------------------------------------------------------------- P7 (spec 8.2, 8.3; ``replay.p7_scenarios``)
P7_MOVE_DAY = 35                           # the sofa of the move jobs moves on this day (W1)
P7_EVENT_DAY = 28                          # the radar jobs turn or shift the radar on this day at 08:00; p7mvback moves the sofa then
P7_ROT_AT_S = 8 * 3600
P7_BACK_DAY = 49                           # p7mvback: the sofa is back at its old place from this day
P7_GUEST_WEEK = (20, 7)                    # (first day, number of days) of the one-week guest (W4)
P7_RETIRE = ("table", 14)                  # p7ret: nobody uses the table from day 14 (W3)


def _rot(deg: float, dx: float = 0.0, dy: float = 0.0) -> tuple:
    """The ``JobSpec.rot`` of a radar job: ``(day, second of the day, degrees, dx m, dy m)`` (W2)."""
    return (P7_EVENT_DAY, P7_ROT_AT_S, deg, dx, dy)


_P7 = dict(lux=True, labels=False)
VARIANTS.update({
    "p7mv11": dict(_P7, script="p7mv11", move=1.1, move_day=P7_MOVE_DAY),
    "p7mv29": dict(_P7, script="p7mv29", move=2.9, move_day=P7_MOVE_DAY),
    "p7mv11e": dict(_P7, script="p7mv11e", move=1.1, move_day=P7_MOVE_DAY, empty_off=True),
    "p7mv29e": dict(_P7, script="p7mv29e", move=2.9, move_day=P7_MOVE_DAY, empty_off=True),
    "p7mvback": dict(_P7, script="p7mvback", move=1.1, move_day=P7_EVENT_DAY, move_back=P7_BACK_DAY),
    "p7ctl": dict(_P7, script="p7ctl"),
    "p7ctle": dict(_P7, script="p7ctle", empty_off=True),
    "p7ctlnp": dict(_P7, script="p7ctlnp", p7_span=0),
    "p7ret": dict(_P7, script="p7ret", retire=P7_RETIRE),
    "p7bed": dict(_P7, script="p7bed"),
    "p7bednl": dict(_P7, script="p7bednl", nl=7, p7_static_freeze=False),
    "p7bednf": dict(_P7, script="p7bednf", p7_static_freeze=False),
    "p7rot": dict(_P7, script="p7rot", rot=_rot(10.0)),
    "p7rot3": dict(_P7, script="p7rot3", rot=_rot(3.0)),
    "p7rot6": dict(_P7, script="p7rot6", rot=_rot(6.0)),
    "p7shift": dict(_P7, script="p7shift", rot=_rot(0.0, 0.8, 0.0)),
    "p7shift5": dict(_P7, script="p7shift5", rot=_rot(0.0, 0.5, 0.0)),
    "p7reset": dict(_P7, script="p7reset", rot=_rot(10.0)),
    "p7guest": dict(_P7, script="p7guest", guest_week=P7_GUEST_WEEK),
    "p7guestp": dict(_P7, script="p7guestp", guest_week=P7_GUEST_WEEK),
    "p7guestnp": dict(_P7, script="p7guestnp", guest_week=P7_GUEST_WEEK, p7_span=0),
    "p7tune": dict(_P7, script="p7tune"),
    "p7tuneh300": dict(_P7, script="p7tuneh300", hm=300),
    "p7tuneh900": dict(_P7, script="p7tuneh900", hm=900),
})
P4_VARIANTS = ("p4", "p4nl", "p4mark", "p4app", "p4rej", "p4names", "p4nop2")
P5_VARIANTS = ("p5setup", "p5ans", "p5push", "p5adv", "p5rej", "p5zone", "p5guests", "p5guestsc", "p5pets")
P6_VARIANTS = ("p6edge", "p6edge05", "p6edge08", "p6hold", "p6pow", "p6door", "p6ld", "p6ldghost", "p6count", "p6setup",
               "p6guests", "p6guestsc")
P7_VARIANTS = ("p7mv11", "p7mv29", "p7mv11e", "p7mv29e", "p7mvback", "p7ctl", "p7ctle", "p7ctlnp", "p7ret", "p7bed", "p7bednl",
               "p7bednf", "p7rot", "p7rot3", "p7rot6", "p7shift", "p7shift5", "p7reset", "p7guest", "p7guestp", "p7guestnp",
               "p7tune", "p7tuneh300", "p7tuneh900")
ALL_SEEDS = (1, 2, 3, 4, 5)
MAX_NIGHTS = 60                            # P7 (spec 8.2 W6): the move jobs run 55 nights, p7mvback 60 (the P3-P6 profiles run at most 28)
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
    # P6 (spec 11.6): scripted people, drop windows, detection noise. Defaults leave every P3-P5 job unchanged.
    spells: tuple = ()                 # ((spot, hidden length s), ...): the scripted hidden spells (P6-3, P6-7), fixed
    gaps: tuple = ()                   # ((start_s, dur_s), ...): scripted message drop windows of the stream (P6-4)
    noise_m: float = 0.02              # per-frame detection noise of the noised kinds (``RunStream.noise_m``)
    crossings: tuple = ()              # walker speeds m/s of the scripted passes through the zones (P6-1, P6-2, P6-15)
    quiet_from_day: Optional[int] = 8  # P6 only: from this day the world's own agents are dropped (None: never)
    programs: tuple = ()               # P6: the scripted programs of ``replay.p6_people`` the job runs
    # P7 (spec 8.2, 8.5): world changes, the radar's pose, the user's empty-room switch and the learner's P7 parameters. The
    # defaults leave every P3-P6 job byte for byte as it was (``make_cfg`` gets the world's own defaults).
    move: float = 0.0                  # W1: the sofa moves by this many metres (< 2.0: to the right; else to the other side, ~2.9 m)
    move_day: int = 35                 # W1: from this day on
    move_back: Optional[int] = None    # W1: the sofa is at its old place again from this day (p7mvback)
    retire: Optional[tuple] = None     # W3: ``(place, day)``: from that day nobody uses the place
    guest_week: Optional[tuple] = None  # W4: ``(first day, number of days)`` of a guest who comes every evening
    rot: Optional[tuple] = None        # W2: ``(day, second of the day, degrees, dx m, dy m)``: the radar's pose changes then
    empty_off: bool = False            # W5: the radars are off while the home is empty (the user's own automation, DV4)
    p7_static_freeze: bool = True      # P7Params.static_freeze (False: p7bednl and p7bednf, DV3)
    p7_span: Optional[int] = None      # P7Params.probation_min_span (0: the probation rule is off); None = the default

    def cfg(self) -> dict:
        """The simulator world config of this job."""
        return W.make_cfg(bed=1 if self.world == "bed" else 0, Vm=self.vm, Hm=self.hm, gw=self.gw, gs=self.gs,
                          gi=self.gi, pet=self.pet, nw=self.window or "", nl=self.nl, move=self.move, move_day=self.move_day,
                          move_back=self.move_back, retire=self.retire, guest_week=self.guest_week)

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
        if self.script == "p5setup" or self.is_p6 or (self.is_p7 and self.script != "p7bednl"):     # p7bednl learns the bed by itself
            out = [("X1", bx, by, d * W.DAY + 22 * 3600, d * W.DAY + 23 * 3600 + 58 * 60) for d in range(6, 10)]
            if self.script == "p6ldghost":
                # P6-9: the label stayer at the reflector (agent Y: not fed to the LD2412 model; the world's reflector
                # tracks then sit in the zone it makes). Day 8 is quiet: exactly one stay is in progress at the press.
                rx, ry = W.REFLECTOR
                a, b = REFLECTOR_LABEL_S
                out.append(("Y1", rx, ry, REFLECTOR_LABEL_DAY * W.DAY + a, REFLECTOR_LABEL_DAY * W.DAY + b))
            return out
        return []

    @property
    def is_p4(self) -> bool:
        return bool(self.script)

    @property
    def is_p5(self) -> bool:
        return self.script.startswith("p5")

    @property
    def is_p6(self) -> bool:
        return self.script.startswith("p6")

    @property
    def is_p7(self) -> bool:
        return self.script.startswith("p7")

    @property
    def tick_ms(self) -> int:
        """The replay's tick: 1 s for a P6 job (hold deadlines are judged to that resolution), 10 s otherwise (a P7 job too)."""
        return 1_000 if self.is_p6 else 10_000

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


def _p6(variant: str) -> str:
    return "bed_w0000_0600_" + variant


# P6 (spec 11.6a): about 800 simulated days. ``p6smoke`` is seed 1 of the three jobs that decide the evaluator's basics.
PROFILES["p6smoke"] = _jobs([_p6(v) for v in ("p6edge", "p6hold", "p6door")], 10, (1,))
PROFILES["p6"] = (_jobs([_p6(v) for v in ("p6edge", "p6edge05", "p6edge08")], 10, ALL_SEEDS)
                  + _jobs([_p6(v) for v in ("p6hold", "p6pow", "p6door", "p6ld", "p6ldghost")], 14, ALL_SEEDS)
                  + _jobs([_p6("p6count")], 10, ALL_SEEDS)
                  + _jobs([_p6("p6setup")], 10, (1, 2, 3))
                  + _jobs([_p6("p6guests"), _p6("p6guestsc")], 21, ALL_SEEDS))


# ---------------------------------------------------------------- P7 profiles (spec 8.3); about 5,400 simulated days in all
def _p7(variant: str, window: str = "w0000_0600") -> str:
    return "bed_%s_%s" % (window, variant)


PROFILES["p7smoke"] = (_jobs([_p7("p7mv11")], 46, (1,)) + _jobs([_p7("p7rot")], 34, (1,)) + _jobs([_p7("p7ctl")], 28, (1,)))
PROFILES["p7move"] = (_jobs([_p7(v, "none") for v in ("p7mv11", "p7mv29")], 55, ALL_SEEDS)
                      + _jobs([_p7(v) for v in ("p7mv11", "p7mv29")], 55, ALL_SEEDS)
                      + _jobs([_p7(v, "none") for v in ("p7mv11e", "p7mv29e")], 55, (1, 2, 3)))
PROFILES["p7drift"] = (_jobs([_p7("p7ctl")], 56, ALL_SEEDS)
                       + _jobs([_p7("p7ctl", "none"), _p7("p7ctle", "none")], 42, ALL_SEEDS)
                       + _jobs([_p7("p7ret")], 42, ALL_SEEDS))
PROFILES["p7beds"] = (_jobs([_p7("p7bed"), _p7("p7bednf")], 42, ALL_SEEDS) + _jobs([_p7("p7bednl")], 56, ALL_SEEDS))
PROFILES["p7radar"] = (_jobs([_p7(v) for v in ("p7rot", "p7rot3", "p7shift")], 34, ALL_SEEDS) + _jobs([_p7("p7reset")], 46, ALL_SEEDS))
PROFILES["p7guests"] = _jobs([_p7("p7guest"), _p7("p7guestp")], 42, ALL_SEEDS)
PROFILES["p7tune"] = _jobs([_p7(v) for v in ("p7tune", "p7tuneh300", "p7tuneh900")], 28, (1, 2, 3))
PROFILES["p7opt"] = (_jobs([_p7("p7guestnp")], 42, ALL_SEEDS) + _jobs([_p7("p7ctlnp")], 56, (1, 2, 3))
                     + _jobs([_p7("p7mvback")], 60, (1, 2, 3)) + _jobs([_p7("p7rot6"), _p7("p7shift5")], 34, ALL_SEEDS))
P7_PROFILES = ("p7smoke", "p7move", "p7drift", "p7beds", "p7radar", "p7guests", "p7tune", "p7opt")


def runs_of(jobs) -> list:
    """(job, seed) pairs of a job list."""
    return [(j, s) for j in jobs for s in j.seeds]


def estimate_days(jobs) -> float:
    return sum(j.sim_days() * len(j.seeds) for j in jobs)
