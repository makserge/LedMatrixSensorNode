"""Shared interface of the radar zone learner (P3). Pure data, stdlib only.

Inputs (shell or replay -> core), the decoded P1 frames batch, outputs (core -> shell), read-only
views and the time model. Every class here is an immutable value; the core never keeps references
to objects it hands out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Union

SCHEMA_VERSION = 3
APP_VERSION = "0.3.0"
STATUS_STATES = ("starting", "learning", "not_observing", "no_data")
SUGGESTION_TYPES = ("dwell_zone", "short_task_zone", "reflection", "ignore_zone", "room_outline", "entry_zone")
KINDS = ("dwell", "short_task", "bed", "reflection", "ignore", "entry", "outline")
MARK_FAIL_REASONS = ("not_observing", "no_stay", "several_stays", "too_soon")


# ---------------------------------------------------------------- inputs (shell or replay -> core)
@dataclass(frozen=True, slots=True)
class FramesMessage:
    payload: bytes            # raw payload of <node_prefix>/radar/ld2454/frames
    recv_ms: int              # wall clock (Unix epoch ms) at reception


@dataclass(frozen=True, slots=True)
class NodeStatus:
    online: bool              # <node_prefix>/sensors/status: "online" -> True, "offline" -> False
    recv_ms: int


@dataclass(frozen=True, slots=True)
class ScheduleState:
    sched: Optional[bool]     # <node_prefix>/radar/schedule/state "1"/"0"; None = never seen (pre-P2)
    start_min: Optional[int]  # <node_prefix>/radar/night_start/state "HH:MM:SS" -> minute of day 0..1439
    end_min: Optional[int]    # <node_prefix>/radar/night_end/state
    recv_ms: int


@dataclass(frozen=True, slots=True)
class ContextState:
    name: str                 # "home_empty" is the only context P3 uses; others are ignored
    value: Optional[bool]     # None = cleared or unparseable
    recv_ms: int


@dataclass(frozen=True, slots=True)
class MarkPress:
    recv_ms: int              # radar_zones/review/mark, payload "PRESS", not retained


@dataclass(frozen=True, slots=True)
class SpotName:
    text: str                 # radar_zones/review/name/set, UTF-8, stripped, <= 32 characters
    recv_ms: int


InputEvent = Union[FramesMessage, NodeStatus, ScheduleState, ContextState, MarkPress, SpotName]


# ---------------------------------------------------------------- decoded frames batch (core.frames)
@dataclass(frozen=True, slots=True)
class Target:
    slot: int                 # 0..2, not an identity
    x_mm: int
    y_mm: int
    v_cms: int


@dataclass(frozen=True, slots=True)
class ListedFrame:
    k: int                    # frame seq - batch.s
    age_ms: int               # batch.t_ms - parse time of this frame
    targets: tuple[Target, ...]


@dataclass(frozen=True, slots=True)
class FramesBatch:
    boot: str                 # 8 lowercase hex digits
    t_ms: int                 # device ms since boot at publish
    s: int
    n: int
    a0: Optional[int]         # present iff n > 0
    a1: Optional[int]
    mt: Optional[int]         # absent in P2 while pw bit0 = 0
    p2: Optional[int]
    pw: int                   # bit0 LD2454 observing, bit1 LD2412 observing
    sm: str                   # A N F X S
    frames: tuple[ListedFrame, ...]


class PayloadError(ValueError):
    """Raised by core.frames.parse_frames; str(exc) is a short reason without payload content."""


# ---------------------------------------------------------------- outputs (core -> shell)
@dataclass(frozen=True, slots=True)
class StatusChanged:
    state: str                # one of STATUS_STATES
    attributes: dict          # JSON-serialisable, keys in Section 9.3


@dataclass(frozen=True, slots=True)
class DebugMapChanged:
    reason: str               # "start" | "nightly" | "label"


@dataclass(frozen=True, slots=True)
class MarkResult:
    ok: bool
    reason: Optional[str]     # None when ok, else one of MARK_FAIL_REASONS
    label_id: Optional[str]
    kind: Optional[str]       # "bed" | "manual" when ok


@dataclass(frozen=True, slots=True)
class NameEcho:
    text: str                 # the accepted spot name, to publish on radar_zones/review/name


@dataclass(frozen=True, slots=True)
class NightlyDone:
    night: int                # local date of the run minus the state's creation date, in days
    date: str                 # ISO local date of the run
    candidates: int
    new_suggestions: int


@dataclass(frozen=True, slots=True)
class NightlyFailed:
    date: str                 # ISO local date of the run that raised
    error: str                # exception type and where it was raised; never a value (privacy)


@dataclass(frozen=True, slots=True)
class SaveRequested:
    reason: str               # "nightly" | "label" | "name"


Output = Union[StatusChanged, DebugMapChanged, MarkResult, NameEcho, NightlyDone, NightlyFailed, SaveRequested]


# ---------------------------------------------------------------- read-only views
@dataclass(frozen=True, slots=True)
class Suggestion:
    id: str                   # "s<night:04d>-<n>"
    rev: int
    type: str                 # one of SUGGESTION_TYPES
    kind: str                 # one of KINDS
    name_hint: Optional[str]  # "bed", or a label name
    state: str                # "shadow" | "stale"
    first_night: int
    last_night: int
    cx_m: float
    cy_m: float
    cells: frozenset          # 0.2 m cell indices (j * 70 + i)
    poly_cm: tuple            # ((x_cm, y_cm), ...) <= 12 vertices
    conf: float
    evidence: dict            # keys in Section 7.8.9
    variants: tuple           # ({"days": "wd"|"we", "start_h": int, "end_h": int, "stays": int, "days_n": int}, ...)
    variant_evidence_split: bool
    variant_share_split: bool # the simulator's older share rule, diagnostic only
    variant_suppressed: bool  # True when the OA §7.8 rule removed a split
    frozen: bool              # OA §7.4 predicate at the last night


@dataclass(frozen=True, slots=True)
class Label:
    id: str                   # "l<n:04d>"
    created_s: int            # wall epoch s rounded down to 300
    x_m: float
    y_m: float
    name: str
    kind: str                 # "bed" | "manual"
    cells: frozenset


@dataclass(frozen=True, slots=True)
class CandidateView:
    cid: str
    cls: str                  # "dwell" | "short"
    cells: frozenset
    cx_m: float
    cy_m: float
    n: int
    core_days: int
    minutes: float
    median_s: float
    ld_share: float
    ghost_share: float
    covis: float
    covis_src: Optional[str]
    streak: int
    conf: float
    gates: dict               # {"g1_class": bool, ..., "g7_suppression": bool, "conf": bool}
    outcome: str              # "suggested" | "reflection" | "failed"


@dataclass(frozen=True, slots=True)
class StayView:
    t0_s: int                 # rounded to 300 s
    t1_s: int
    dur_s: int                # rounded to 10 s
    x_m: float
    y_m: float
    ld: float
    he: float
    exit: str                 # "moving" | "still" | "moved" | "censored"
    arr: str                  # "walk" | "bridge" | "static"
    day: str                  # ISO local date of t0


@dataclass(frozen=True, slots=True)
class DebugMapData:
    nx: int                   # 70
    ny: int                   # 40
    cell_m: float             # 0.2
    x0_m: float               # -7.0
    y0_m: float               # 0.0
    dwell_rate: tuple         # blurred DWELL_SLOW rate per cell, row-major (j * nx + i)
    envelope: frozenset
    candidates: tuple         # CandidateView
    suggestions: tuple        # Suggestion
    labels: tuple             # Label
    doors: tuple              # ((x_m, y_m), ...)
    fov_deg: float            # 60.0: draws +-60 degree lines from (0, 0)


# ---------------------------------------------------------------- time
@dataclass(frozen=True, slots=True)
class LocalTime:
    date_ord: int             # datetime.date.toordinal() of the local date
    weekday: int              # 0 = Monday
    sec_of_day: int           # 0..86399 local wall-clock seconds


class TimeModel(Protocol):
    def local(self, epoch_ms: int) -> LocalTime: ...


@dataclass(frozen=True, slots=True)
class FixedOffsetTime:
    offset_s: int = 0

    def local(self, epoch_ms: int) -> LocalTime:
        s = epoch_ms // 1000 + self.offset_s
        days, sod = divmod(s, 86400)
        date_ord = 719163 + days          # date(1970, 1, 1).toordinal() == 719163
        return LocalTime(date_ord, (date_ord - 1) % 7, sod)   # toordinal 1 = Monday 0001-01-01


class StateError(ValueError):
    """Raised by Learner(state=...) for a state dict that fails validation (Section 8.1)."""
