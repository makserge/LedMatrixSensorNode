"""Shared interface of the radar zone learner (P3). Pure data, stdlib only.

Inputs (shell or replay -> core), the decoded P1 frames batch, outputs (core -> shell), read-only
views and the time model. Every class here is an immutable value; the core never keeps references
to objects it hands out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Union

SCHEMA_VERSION = 4
MIN_SCHEMA_VERSION = 3            # older files are migrated on load (state.migrate_3_to_4)
APP_VERSION = "0.7.0"
STATUS_STATES = ("starting", "learning", "not_observing", "no_data")
SUGGESTION_TYPES = ("dwell_zone", "short_task_zone", "reflection", "ignore_zone", "room_outline", "entry_zone")
KINDS = ("dwell", "short_task", "bed", "reflection", "ignore", "entry", "outline", "pet")   # documentation only
MARK_FAIL_REASONS = ("not_observing", "no_stay", "several_stays", "too_soon")

# P4 review items (OA 7.8): the two that exist before P5 (tier 1), and the ops of ReviewAnswer
ITEM_NIGHT_WINDOW = "night_window"
ITEM_BED = "bed_needs_data"
ITEM_IDS = (ITEM_NIGHT_WINDOW, ITEM_BED)
ANSWER_OPS = ("approve", "reject", "later", "rename", "undo", "forget", "advanced")   # P4's six + "advanced" (P5)
NODE_COMMAND_KINDS = ("schedule", "window")

# P5 (spec Appendix A.1): why an answer changed nothing, push request kinds, persistent settings, zone sources and the
# id grammars (the shell's parser and the core use these same expressions)
ANSWER_IGNORED_REASONS = ("no_item", "no_such_item", "stale", "not_pending", "not_applicable", "name_needed",
                          "zone_limit", "times_invalid", "zone_busy")   # the eight of P5 first; zone_busy is P7 (spec 6.2)
PUSH_KINDS = ("none", "setup", "daily")
SETTING_NAMES = ("approve_sched", "guests_pause", "pets", "detail_text")
ZONE_SRC = ("approve", "mark")           # a record made by approval (merge and modify included) / a label
ITEM_RADAR_MOVED = "radar_moved"         # P7 (spec A.1): the tier-1 item after the two P4 ones; ITEM_IDS stays the P4 pair
ITEM_ID_RE = r"^(night_window|bed_needs_data|radar_moved|s[0-9]{4,}-[0-9]+|mrg-s[0-9]{4,}-[0-9]+|mod-z[0-9]{4,}|ret-z[0-9]{4,})$"
ADV_ID_RE = r"^adv:(undo|real|rename|remove):[A-Za-z0-9_-]{1,24}$"
PUSH_TARGET_RE = r"^[A-Za-z0-9]{1,12}$"  # the blueprint's phone id in the push ack and in PushRequest.acks

# P6 (spec Appendix A.1): confirmed-zone evaluation. The shell's parser and the core use these same expressions.
ZONE_EVENT_KINDS = ("enter", "leave")
ZONE_EVENT_VIA = ("edge", "hold_expired", "door_death", "early")   # first three: the design; early: spec proposal
CENSOR_CLASSES = ("POWER", "GAP")
ZONE_ID_RE = r"^[zl][0-9]{4,}$"
VARIANT_ID_RE = r"^(wd|we)[0-9]{4}$"
ZONE_KINDS_EVALUATED = ("dwell", "short_task", "bed", "manual")
ZONE_KINDS_DWELL = ("dwell", "bed", "manual")      # the pass-speed rule applies (design: 'dwell zones')
ZONE_TUNE_FIELDS = ("cd", "mg")                    # clear delay in seconds, margin in centimetres
DELAY_SRC = ("default", "learned", "manual")

# P7 (spec Appendix A.1): drift verdicts, moved-from masks, radar-moved states, the source of a modify proposal
DRIFT_STATES = ("young", "blind", "frozen", "none", "collapsed", "ok", "falling")
MF_WHY = ("moved_from", "retired")
RADAR_STATES = ("none", "watching", "open", "ignored")
MOD_SRC = ("cand", "own")                # 'cand': a different pending candidate (4.3.3); 'own': the zone's own suggestion moved


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


@dataclass(frozen=True, slots=True)
class LuxSample:
    lux: float                # BH1750 lux from <node_prefix>/sensors/state (the night-onset proxy, OA 7.5)
    recv_ms: int


@dataclass(frozen=True, slots=True)
class ReviewSelect:
    item: Optional[str]       # ITEM_IDS member, or None = nothing selected (the default: the first item)
    recv_ms: int


@dataclass(frozen=True, slots=True)
class ReviewAnswer:
    item: Optional[str]       # an item id (ITEM_ID_RE), an Advanced entry id (ADV_ID_RE), a zone id ("z0001") or a
                              # label id; None = the selected item
    op: str                   # one of ANSWER_OPS
    rev: Optional[int]        # the item's revision the answer was made on; None = not checked
    sched: bool               # night_window approve: also switch the schedule ON ('the box ticked')
    recv_ms: int
    name: Optional[str] = None   # P5: a name given with the answer (the push's RZ_RENAME, a hand-written message)
    aid: Optional[str] = None    # P5: answer id; the shell's ring drops a repeat; the core reads only whether it is
                                 # present (an answer with an aid never uses the name text)


@dataclass(frozen=True, slots=True)
class NightLearningSwitch:
    on: bool                  # the user turned 'Night learning' on or off
    recv_ms: int


@dataclass(frozen=True, slots=True)
class CommandResult:
    token: str                # the NodeCommand's token
    kind: str                 # "schedule" | "window"
    ok: bool                  # True: the echo matched (or the node already had the value)
    reason: Optional[str]     # None, "unchanged", or why not: no_p2, node_offline, changed_by_user,
                              # not_connected, no_echo (2 min), superseded, bad_command
    recv_ms: int


@dataclass(frozen=True, slots=True)
class NightTimeEdit:
    field: str                # "start" | "end": the two time entities of the review device (P5 spec 3.4)
    minute: int               # 0..1439
    recv_ms: int


@dataclass(frozen=True, slots=True)
class SettingSwitch:
    name: str                 # one of SETTING_NAMES: the four persistent switches (P5 spec 3.4, 7.3-7.5)
    on: bool
    recv_ms: int


@dataclass(frozen=True, slots=True)
class PushAck:
    seq: int                  # the blueprint's acknowledgement of a push request (P5 spec 6.4)
    recv_ms: int
    target: str = ""          # the phone's id (PUSH_TARGET_RE); "" for a plain "<seq>"


@dataclass(frozen=True, slots=True)
class ZoneTune:
    zid: str                  # ZONE_ID_RE
    field: str                # one of ZONE_TUNE_FIELDS: cd 3..900 seconds, mg -50..100 centimetres
    value: int
    recv_ms: int


@dataclass(frozen=True, slots=True)
class ZoneForget:
    zid: str                  # discards the learned evidence and the tuning of one zone, not the zone (spec D15)
    recv_ms: int


@dataclass(frozen=True, slots=True)
class VariantRequest:
    zid: str
    variant: str              # VARIANT_ID_RE: asks for (on) or drops (off) the variant's occupancy entity
    on: bool
    recv_ms: int


InputEvent = Union[FramesMessage, NodeStatus, ScheduleState, ContextState, MarkPress, SpotName, LuxSample,
                   ReviewSelect, ReviewAnswer, NightLearningSwitch, CommandResult, NightTimeEdit, SettingSwitch,
                   PushAck, ZoneTune, ZoneForget, VariantRequest]


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
    reason: str               # "start" | "nightly" | "label" | "review"


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
    reason: str               # "nightly" | "label" | "name" | "review" | "push" | "night_learning" | "zone"


@dataclass(frozen=True, slots=True)
class NodeCommand:
    token: str                # chosen by the core; comes back in the CommandResult
    kind: str                 # "schedule": set the night schedule switch; "window": times (and the switch)
    sched: Optional[bool] = None   # schedule: the value; window: also ON/OFF when not None (the ticked box)
    start_min: Optional[int] = None
    end_min: Optional[int] = None
    expect_current: Optional[bool] = None   # the schedule value the learner believes the node has; if the
                                            # node's echo differs somebody else changed it: nothing is sent
    reason: str = ""          # for the log only: "night_learning_start", "night_learning_end", "approved_window"


@dataclass(frozen=True, slots=True)
class ReviewItem:
    id: str                   # an item id (ITEM_ID_RE): night_window, bed_needs_data, "s0005-1", "mrg-s0005-1", ...
    rev: int
    type: str                 # night_window | bed_needs_data | dwell_zone | short_task_zone | merge_into | modify_zone
                              # | retire_zone | radar_moved (P7: radar_moved is tier 1, retire_zone tier 2)
    data: dict               # flat, hashable values only (P5 spec 2.4). night_window: start_min, end_min, saving_pct,
                              # current_saving_pct, late_nights, late_of, from_lights_off, basis, text (P5 adds
                              # edit_start, edit_end, sched_box); bed_needs_data: start_min, end_min, sched,
                              # show_window, text
    tier: int = 1             # P5: 1 = night_window, bed_needs_data; 2 = place, merge, modify


@dataclass(frozen=True, slots=True)
class ReviewChanged:
    items: tuple              # ReviewItem, in review order
    selected: Optional[str]   # the item the select shows (the first item unless the user chose)


@dataclass(frozen=True, slots=True)
class NightLearningChanged:
    active: bool
    until: Optional[str] = None             # ISO date the 7 nights end
    set_by_learner: Optional[bool] = None   # the schedule value the learner last wrote (OA 4.2); None = none
    event: Optional[str] = None             # what happened (``NightLearning.last_event``); the shell raises the
                                            # review event ``restore_failed`` for restore_failed / restore_overdue


@dataclass(frozen=True, slots=True)
class AnswerIgnored:
    item: Optional[str]       # P5: why an answer changed nothing; the shell turns it into the review event
    op: str
    reason: str               # one of ANSWER_IGNORED_REASONS


@dataclass(frozen=True, slots=True)
class AdvancedChanged:
    entries: tuple            # AdvancedEntry, in the order of P5 spec 2.10


@dataclass(frozen=True, slots=True)
class PushRequest:            # P5 spec 6.2; no text field by design
    seq: int
    acked: int
    kind: str                 # one of PUSH_KINDS
    count: int                # open items, tier 1 and 2
    head_id: Optional[str]    # the first tier-2 item when the request was created, fixed per seq, or None
    head_rev: Optional[int]
    acks: tuple = ()          # the targets that acknowledged this seq (at most 8)
    attention: bool = False   # P7 (spec 6.5): the current request is a radar-moved request; the shell writes kind "attention"


@dataclass(frozen=True, slots=True)
class ZoneEvent:              # P6 spec A.3: a real transition of one zone; no zone name, no coordinate
    id: str                   # zone id
    kind: str                 # one of ZONE_EVENT_KINDS
    via: str                  # leave: one of ZONE_EVENT_VIA; enter: ""
    t_dev_ms: int             # device time of the decisive frame (enter, edge leave) or the computed deadline
    track: int                # persistent track id, published as "t<id>"; the last track to leave or vanish for a leave
    dwell_s: int              # leave only: seconds since the zone became occupied; 0 for an enter
    variant: str              # variant_now at the event, "" when none


@dataclass(frozen=True, slots=True)
class ZoneState:
    id: str
    occ: bool
    count: int                # confirmed tracks counted inside, 0..3
    held: bool                # occupied with nobody inside now: a delay, an early or a fast clear is pending
    variant: str              # variant_now, "" when none
    ldr: bool                 # ld2412_reliable
    delay_s: int              # the clear delay in force
    delay_src: str            # one of DELAY_SRC
    margin_cm: int            # the margin in force
    variants: tuple = ()      # ids of the zone's variants (for the optional entities)
    requested: tuple = ()     # ids whose occupancy entity was requested (zone6.ve)
    # trailing fields the occupancy attributes need (spec 8.3): the shell has no other source for them
    typical_hours: str = ""   # "wd 09-12,13-17; we 10-22" (day class, whole-hour ranges); "" when unknown
    confidence: Optional[float] = None   # Suggestion.conf rounded to two decimals; None for a label or a manual zone


@dataclass(frozen=True, slots=True)
class ZoneStates:
    valid: bool               # False: every zone is unavailable (radars off, warming up, faulted, learner not resumed)
    t_dev_ms: int             # not part of the emission signature
    zones: tuple              # ZoneState, ordered by id


@dataclass(frozen=True, slots=True)
class TuningLine:             # P7 spec A.2: one nightly count line, emitted once per nightly run when P7 is enabled
    night: int                # the learner's night counter (the shell may drop a re-delivery by it; not part of `text`)
    text: str                 # one JSON line, at most P7Params.agg_night_bytes_max bytes, whitelisted keys only (4.8)


Output = Union[StatusChanged, DebugMapChanged, MarkResult, NameEcho, NightlyDone, NightlyFailed, SaveRequested,
               NodeCommand, ReviewChanged, NightLearningChanged, AnswerIgnored, AdvancedChanged, PushRequest,
               ZoneEvent, ZoneStates, TuningLine]


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
class ZoneView:               # Learner.zones(): records (ordered by zid) then labels (ordered by id)
    id: str                   # "z0001" or "l0001"; stable
    name: str                 # a label with no name: ""
    kind: str                 # "bed" | "dwell" | "short_task" | "manual"
    src: str                  # one of ZONE_SRC: "approve" (a record) | "mark" (a label)
    ref: Optional[str]        # source suggestion id of a record, else None
    rev: int                  # record revision; 0 for a label
    cx_m: float
    cy_m: float
    cells: frozenset
    poly_cm: tuple            # ((x_cm, y_cm), ...), at most 12 vertices; computed for a label
    created_s: int
    night: int
    frozen: bool              # the OA 7.4 predicate


@dataclass(frozen=True, slots=True)
class ZoneDrift:              # P7 spec A.3: Learner.drift_view(): one per zone record, then one per label
    id: str
    state: str                # one of DRIFT_STATES: the last night's verdict (the stored `vs`)
    ratio: float              # 3 decimals; 0.0 for collapsed; -1.0 when not computed (young, blind, none)
    frozen: bool              # ZoneView.frozen: the static predicate OR the coverage freeze
    lo: int                   # flag run
    rt: int                   # retire run
    lu: int                   # date ordinal of the last use, 0 = none
    rc: bool                  # needs re-check
    held: bool                # the last night was held (rc or radar hold)


@dataclass(frozen=True, slots=True)
class RadarMovedView:         # P7 spec A.3: Learner.radar_moved_state()
    state: str                # one of RADAR_STATES
    rev: int                  # rm.rev
    streak: int               # rm.st
    tx: tuple                 # (dx_cm, dy_cm, tenths of a degree) of the last moved night
    hold: bool                # the radar hold is in force (spec 4.6)


@dataclass(frozen=True, slots=True)
class AdvancedEntry:
    id: str                   # "adv:<verb>:<target>" (ADV_ID_RE)
    verb: str                 # "undo" | "real" | "rename" | "remove"
    kind: str                 # "bed" | "night_window" | "place" | "reflection" | "zone" | "label"
    target: str               # "bed", "nw", a suggestion id, a zid or a label id
    name: str                 # zone or label name, else ""
    tag: str                  # tier-2 tag of the suggestion (kind place or reflection), else ""
    until: str                # ISO date for night_window, else ""
    rev: int                  # zone record revision, else 0


@dataclass(frozen=True, slots=True)
class WhereView:              # Learner.where_am_i() (P5 spec 2.7)
    observing: bool           # the learner state is "learning"
    people: int               # confirmed live tracks
    stay_ready: bool          # exactly one stay of >= mark_min_stay_s is in progress (a press would succeed)
    item: Optional[str]       # an open tier-2 item id, a zone id or a label id, else None
    item_kind: Optional[str]  # "place" | "zone" | "label" | None


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
    # P5 (spec A.6): trailing defaulted fields. Learner.debug_map() fills only `zones`, Learner.review_map() all five.
    zones: tuple = ()         # ((cells: frozenset, is_bed: bool), ...) of the confirmed zones and labels
    others: tuple = ()        # (cells: frozenset, ...) of the open tier-2 items that are not selected
    highlight: frozenset = frozenset()   # cells of the selected item
    focus: Optional[tuple] = None        # (x_m, y_m) centroid of the highlight
    live: tuple = ()          # ((x_m, y_m, stay: bool), ...), only with live=True and the radars observing


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
