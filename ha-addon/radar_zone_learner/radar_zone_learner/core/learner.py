"""Learner facade (P3 spec 5.3): the only entry point of the core for the shell and the replay.

Pure and deterministic: no clock, file, socket, thread, random or environment access. Time comes
only from event timestamps and tick(now_ms); "core now" is the largest of them.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Optional

from . import drift as drift_mod
from . import labels as labels_mod
from . import movedfrom as movedfrom_mod
from . import radarmoved as radarmoved_mod
from . import tuningagg as tuningagg_mod
from . import where as where_mod
from . import zones as zones_mod
from .advanced import advanced_entries
from .behaviours import Behaviours
from .covis import Covis
from .extraction import PLACE_TYPES, NightState, cid_key, refresh_places_now, run_nightly
from .frames import parse_frames
from .grids import GridGeom, Grids, rate
from .night import NightModel
from .nightlearn import NightLearning
from .notify import PushPlanner
from . import zoneinfo
from .observation import MIN_BOOT_WALL_MS, Censor, Observation, TrackFrames
from .p5state import P5State
from .p6state import Zone6, variant_id
from .p7state import Zone7
from .zoneeval import ZoneEval
from .params import DEFAULT_P5, DEFAULT_P6, DEFAULT_P7, DEFAULT_PARAMS, P5Params, P6Params, P7Params, Params
from .review import ReviewModel, hhmm
from .state import export_state, load_state
from .tracker import Tracker
from .types import (APP_VERSION, ITEM_IDS, ITEM_RADAR_MOVED, SCHEMA_VERSION, SETTING_NAMES, AdvancedChanged, AnswerIgnored,
                    CandidateView, CommandResult, ContextState, DebugMapChanged, DebugMapData, FixedOffsetTime,
                    FramesMessage, Label, LuxSample, MarkPress, MarkResult, NameEcho, NightLearningChanged,
                    NightLearningSwitch, NightlyDone, NightlyFailed, NightTimeEdit, NodeCommand, NodeStatus,
                    PayloadError, PushAck, PushRequest, ReviewAnswer, ReviewChanged, ReviewSelect, SaveRequested,
                    ScheduleState, SettingSwitch, SpotName, StatusChanged, StayView, Suggestion, TimeModel,
                    TuningLine, WhereView, ZoneDrift, ZoneEvent, ZoneForget, ZoneStates, ZoneTune, VariantRequest,
                    RadarMovedView, ZONE_TUNE_FIELDS)

# P5 (spec A.3): the only place a new output class can be lost (an unlisted class is silently dropped). P7 (spec A.2):
# TuningLine goes before SaveRequested, which keeps the P4 subsequence.
_OUTPUT_ORDER = (MarkResult, NameEcho, NightlyDone, NightlyFailed, NodeCommand, NightLearningChanged,
                 AnswerIgnored, ReviewChanged, AdvancedChanged, ZoneEvent, ZoneStates, PushRequest, StatusChanged,
                 DebugMapChanged, TuningLine, SaveRequested)
_REASON_PRIORITY = {"nightly": 0, "label": 1, "name": 2, "review": 3, "push": 3, "zone": 3, "night_learning": 4,
                    "start": 5}
P4_STEP_MS = 1000                # night evidence, Night learning timers and the review are looked at this often
MARK_MIN_INTERVAL_MS = 10000     # a second accepted press within this time is refused ('too_soon')


def _utc_min(ms: Optional[int], step_min: int = 1) -> Optional[str]:
    """UTC ISO time rounded down to step_min minutes, e.g. '2026-01-26T22:40Z'."""
    if ms is None:
        return None
    s = ms // 1000
    days, sod = divmod(s, 86400)
    m = sod // 60
    m -= m % step_min
    return "%sT%02d:%02dZ" % (date.fromordinal(719163 + days).isoformat(), m // 60, m % 60)


def _hhmm(minute: Optional[int]) -> Optional[str]:
    if minute is None:
        return None
    return "%02d:%02d" % (minute // 60, minute % 60)


class Learner:
    def __init__(self, params: Params = DEFAULT_PARAMS, time_model: TimeModel = FixedOffsetTime(0),
                 state: Optional[dict] = None, p5params: P5Params = DEFAULT_P5,
                 p6params: P6Params = DEFAULT_P6, p7params: P7Params = DEFAULT_P7) -> None:
        p = self.p = params
        self.p5params = p5params                            # P5 parameters (a separate dataclass, spec D4)
        self.p6params = p6params                            # P6 parameters (a separate dataclass, P6 spec D4)
        self.p7params = p7params                            # P7 parameters; ``enabled`` is False by default (P7 spec D4)
        self.tm = time_model
        self.geom = GridGeom(p)
        self.grids = Grids(p, self.geom)
        self.covis = Covis(p)
        self.obs = Observation(p, time_model)
        self.bh = Behaviours(p, self.grids, self.covis, self.obs, self._date_of_s)
        self.tracker = Tracker(p)
        self.ns = NightState()
        self.label_list: list = []
        self.window = {"start": p.default_window_min[0], "end": p.default_window_min[1], "sched": None}
        self.night_m = NightModel(p, time_model)           # P4: onsets, wakes, the night-window proposal
        self.p5 = P5State(p5params.later_max, p5params.tags_max)   # P5: the persisted `p5` section (spec 9.2)
        self.review = ReviewModel(p, time_model, p5params, self.p5)   # P4/P5: review items, suppressions, zones
        self.zone7 = Zone7()                               # P7: the zone7 key, filled in place after the load (spec 5.1)
        self.p7_errors = 0                                 # failed P7 nightly steps since start (RAM, spec 4.1)
        self.p7_own_seen: dict = {}                        # RAM: zid -> ((sid, rev), first run ordinal) of an own modify
        self.p7_requalified: set = set()                   # RAM: moved-from entry keys re-qualified in tonight's gate 7
        self.zone7_pruned = 0
        self.p7_radar_dbg: dict = {}                       # RAM: the last radar-moved verdict (hit rates, for the replay)
        self.p7_sp_prev: Optional[dict] = None             # RAM: the hidden-spell lists of the last tuning line
        self.planner = PushPlanner(self.p5, p5params,      # P5: when a push request is due (spec 6.2)
                                   self.zone7 if p7params.enabled else None)   # P7: the attention state (None = P5)
        self.guests_ctx: Optional[bool] = None             # the `guests` context (RAM, as before); spec 7.3
        self.nl = NightLearning(p, time_model)             # P4: Night learning (off by default)
        self.bh.on_walk = self._on_walk
        self.bh.on_death = self.night_m.note_death
        self.rv_dirty = True
        self.cmd_seq = 0
        self._p4_ms: Optional[int] = None
        self.spot_name = ""
        self.extra: dict = {}
        self.created_ord: Optional[int] = None
        self.created_ms: Optional[int] = None
        self.last_run_ord: Optional[int] = None
        self.night = 0
        self.next_label_n = 1
        self.last_boot_id: Optional[str] = None
        self.last_seq: Optional[int] = None
        # RAM only
        self.now: Optional[int] = None
        self.first_event_ms: Optional[int] = None
        self.state = "starting"
        self.state_since_ms: Optional[int] = None
        self.last_status_ms: Optional[int] = None
        self.last_mark: Optional[dict] = None
        self.last_mark_ms: Optional[int] = None      # core time of the last accepted press
        self.nightly_errors = 0                      # nightly runs that raised (RAM, since start)
        self._nl_next: Optional[int] = None          # next core time to check for the nightly run
        self._paused_on = False                      # the guests pause as applied to the evidence (spec 7.3)
        self._pause_init = False                     # the pause is applied at the first event (also after a load)
        self.adv_sig: tuple = ()                     # the AdvancedChanged signature last emitted (default: none)
        self._adv_cache: Optional[tuple] = None      # (key, entries) for the per-second Advanced check
        self.items_now: list = []                    # the review items of the last check (RAM)
        if state is not None:
            load_state(self, state)
        # P6: the zone6 key (validated here: P5State keeps unknown keys of the p5 section unchecked, spec 6.1) and the
        # evaluator. Both are built after the state load so a corrupt zone6 raises StateError.
        self.zone6 = Zone6.load(self.p5.extra.get("zone6"))
        self.zone7.load(self.p5.extra.get("zone7"), self.geom.nc)      # P7: a wrong container raises StateError (D43)
        cap7 = max(1, p7params.mf_max)                  # spec 5.2: a longer mask list is cut to the newest mf_max at load
        if p7params.enabled and len(self.zone7.mf) > cap7:
            del self.zone7.mf[:len(self.zone7.mf) - cap7]
        self.zone_eval = ZoneEval(p6params, self.geom, time_model, self.zone6, p.silence_censor_ms,
                                  p.powerup_birth_ms)
        self._zkey: Optional[tuple] = None           # what the evaluator's zone set was last synced from
        self._zones_seen = False                     # a sync has seen at least one zone (orphan pruning, spec 6.2)
        self.zone6_pruned = 0
        self.zone_cmds_rejected = 0

    # ------------------------------------------------------------ helpers
    def _date_of_s(self, t_s: float) -> int:
        return self.tm.local(int(t_s * 1000)).date_ord

    def _advance(self, t_ms: int) -> int:
        if self.now is None or t_ms > self.now:
            self.now = t_ms
        return self.now

    def link_labels_one(self, s: dict) -> None:
        labels_mod.link_one(s, self.label_list, self.geom)

    def _on_walk(self, t_s: float) -> None:
        self.night_m.note_walk(t_s, self.obs)

    def next_token(self, prefix: str) -> str:
        """A token for a NodeCommand, unique within this run."""
        self.cmd_seq += 1
        return "%s%d" % (prefix, self.cmd_seq)

    # ------------------------------------------------------------ public API
    def handle(self, event) -> list:
        outs: list = []
        p4_now = False
        now = self._advance(event.recv_ms)
        if self.first_event_ms is None:
            self.first_event_ms = now
            self.state_since_ms = now
        if self.created_ms is None and now >= MIN_BOOT_WALL_MS:
            # the learner's day 0: not before the host clock is plausible (a clock that is not set yet, or
            # a stale boot clock before the first NTP sync, would date everything from 1970)
            self.created_ms = now
            self.created_ord = self.tm.local(now).date_ord
        self._pause_sync(now)
        force = self._maybe_nightly(outs)
        if isinstance(event, FramesMessage):
            try:
                batch = parse_frames(event.payload)
            except PayloadError:
                self.obs.counters["parse_errors"] += 1
            else:
                acts = self.obs.ingest(batch, event.recv_ms, now)
                self._run(acts)
                if any(type(a) is TrackFrames for a in acts):      # an accepted, observed batch: its LD2412 bit
                    self.zone_eval.on_ld(batch.t_ms, batch.p2, bool(batch.pw & 2))
        elif isinstance(event, NodeStatus):
            self._run(self.obs.node_status(bool(event.online)))
            if event.online:
                self.nl.on_node_online(self, now, outs)     # a restore that waited for the node goes again
        elif isinstance(event, ScheduleState):
            if event.sched is not None:
                self.window["sched"] = bool(event.sched)
            for key, v in (("start", event.start_min), ("end", event.end_min)):
                if type(v) is int and 0 <= v < 1440:
                    self.window[key] = v
            self.nl.on_schedule(self, now, outs, event.sched is not None)
            self.review.touch()                              # zones freeze while the schedule is ON (the proposals read it)
            p4_now = True
        elif isinstance(event, ContextState):
            if event.name == "home_empty":
                self.obs.set_context(None if event.value is None else bool(event.value), now)
            elif event.name in ("lights_off", "sleep_hint", "guests"):
                val = None if event.value is None else bool(event.value)
                self.night_m.on_context(event.name, val, now)
                if event.name == "guests":
                    self.guests_ctx = val                      # P5: learning is paused while this or the switch is on
                    self._apply_pause(now)
        elif isinstance(event, LuxSample):
            self.night_m.on_lux(event.lux, now)
        elif isinstance(event, ReviewSelect):
            if event.item is None or event.item in ITEM_IDS or self.review.open_tier2(self, event.item) \
                    or self.review.open_tier1(self, event.item):
                if (event.item, True) != (self.review.select, self.review.select_chosen):
                    self.review.select = event.item
                    self.review.select_chosen = True
                    self.rv_dirty = True
            p4_now = True
        elif isinstance(event, ReviewAnswer):
            if self.review.answer(self, event, outs, now):
                refresh_places_now(self)
                self.review.touch()                        # the refresh changed suggestions the item list reads
                self.rv_dirty = True
                outs.append(SaveRequested("review"))
            force = p4_now = True
        elif isinstance(event, NightTimeEdit):
            self.review.night_edit(self, event, outs)
            p4_now = True
        elif isinstance(event, SettingSwitch):
            if event.name in SETTING_NAMES:
                if self.p5.set_setting(event.name, event.on):
                    self.review.touch()
                    outs.append(SaveRequested("review"))
                if event.name == "guests_pause":
                    self._apply_pause(now)
                self.rv_dirty = True                       # the night-window item shows the switch-on box
                force = p4_now = True                      # the status attributes carry the settings
        elif isinstance(event, PushAck):
            if self.planner.ack(event.seq, event.target):
                outs.append(SaveRequested("push"))
            p4_now = True
        elif isinstance(event, NightLearningSwitch):
            self.nl.switch(self, bool(event.on), now, outs)
            force = p4_now = True
        elif isinstance(event, CommandResult):
            if not self.nl.on_result(self, event, now, outs):
                if self.review.window_result(event):
                    self.rv_dirty = True
                    if event.ok:
                        outs.append(SaveRequested("review"))   # p5.nw_applied changed
            force = p4_now = True
        elif isinstance(event, MarkPress):
            self._mark(outs)
            force = True
        elif isinstance(event, SpotName):
            text = str(event.text).strip()[:self.p.spot_name_max]
            if text != self.spot_name:               # the same name again changes nothing: no echo, no write
                self.spot_name = text
                outs.append(NameEcho(text))
                outs.append(SaveRequested("name"))
        elif isinstance(event, (ZoneTune, ZoneForget, VariantRequest)):
            if self._zone_command(event):
                outs.append(SaveRequested("zone"))
        force = self._p4_step(outs, now, p4_now or force) or force
        self._status(outs, force, periodic=False)
        self._zone_outputs(outs)
        return self._order(outs)

    def tick(self, now_ms: int) -> list:
        now = self._advance(now_ms)
        if self.created_ms is None:
            return []                        # fresh learner: nothing before the first event
        outs: list = []
        self._pause_sync(now)
        force = self._maybe_nightly(outs)
        self._run(self.obs.tick(now))
        self._zone_sync_check()
        self.zone_eval.on_tick(now)
        force = self._p4_step(outs, now, False) or force
        self._status(outs, force, periodic=True)
        self._zone_outputs(outs)
        return self._order(outs)

    def _p4_step(self, outs: list, now: int, immediate: bool) -> bool:
        """Night evidence, Night learning timers and the review item list. True when the review changed."""
        if not immediate and self._p4_ms is not None and 0 <= now - self._p4_ms < P4_STEP_MS and not self.rv_dirty:
            return False
        self._p4_ms = now
        self.night_m.step(self, now)
        self.nl.tick(self, now, outs)
        self.rv_dirty = False
        changed = self.review.check(self, outs)
        items = self.items_now = self.review.items(self)
        adv = self._advanced_cached()
        if adv != self.adv_sig:
            self.adv_sig = adv
            outs.append(AdvancedChanged(adv))
            changed = True
        self.planner.step(self, items, now, outs)
        return changed

    def _advanced_cached(self) -> tuple:
        """The Advanced entries for the per-second check: cached by what they depend on (every change through the
        review model bumps ``review.ver``; a label made by 'Mark spot here' calls ``review.touch()``)."""
        rv = self.review
        key = (rv.ver, self.night, rv._today(self), len(self.label_list), len(rv.zones), len(rv.suppressions),
               len(self.ns.suggestions), len(self.p5.real))
        c = self._adv_cache
        if c is not None and c[0] == key:
            return c[1]
        adv = advanced_entries(self)
        self._adv_cache = (key, adv)
        return adv

    # ------------------------------------------------------------ the guests pause (spec 7.3)
    def _pause_sync(self, now_ms: int) -> None:
        if not self._pause_init:
            self._pause_init = True
            self._apply_pause(now_ms)

    def _apply_pause(self, now_ms: int) -> None:
        """Learning is paused while the switch 'Guests present - pause learning' or the `guests` context is on. The
        evidence guards judge the pause at the time of the evidence (``Behaviours.paused_in``), not at commit time."""
        on = bool(self.p5.setting("guests_pause")) or self.guests_ctx is True
        if on != self._paused_on:
            self._paused_on = on
            self.obs.set_paused(on)
            self.bh.set_paused(on, now_ms / 1000.0)

    def export_state(self) -> dict:
        self.zone_eval.flush_ld()                    # P6: the whole seconds of LD2412 evidence go into zone6 ...
        self.zone6.sync_into(self.p5)                # ... and zone6 into p5.extra (or out of it when default)
        self.zone7.sync_into(self.p5)                # P7: the same for zone7 (validated before it is written, D43)
        return export_state(self)

    def status(self) -> StatusChanged:
        return StatusChanged(self._state_now(), self._attributes())

    def debug_map(self) -> DebugMapData:
        g = self.geom
        dr = g.blur(rate(self.grids["dwell_slow"], self.grids.eff_s))
        shadow = [s for s in self.suggestions() if s.state == "shadow"]
        doors = tuple((s.cx_m, s.cy_m) for s in shadow if s.type == "entry_zone")
        return DebugMapData(g.nx, g.ny, g.cell, g.x0, g.y0, tuple(dr), frozenset(self.ns.envelope),
                            self.candidates(), tuple(shadow), self.labels(), doors, 60.0, self._map_zones())

    def _map_zones(self) -> tuple:
        """((cells, is_bed), ...) of the confirmed zone records and labels (spec A.6)."""
        out = [(frozenset(z["cells"]), z["kind"] == "bed") for z in sorted(self.review.zones, key=lambda r: r["zid"])]
        out += [(frozenset(lb["cells"]), lb["kind"] == "bed") for lb in sorted(self.label_list, key=lambda d: d["id"])]
        return tuple(out)

    # ------------------------------------------------------------ P5 interface (spec A.8)
    def zones(self) -> tuple:
        """ZoneView of every confirmed zone: the records (ordered by zid), then the labels (ordered by id)."""
        return zones_mod.zone_views(self)

    def advanced(self) -> tuple:
        """AdvancedEntry, in the order of spec 2.10; empty until the setup review is ready."""
        return advanced_entries(self)

    def review_map(self, selected: Optional[str] = None, live: bool = False) -> DebugMapData:
        """The debug map data with the five P5 fields (spec 2.9, A.6). ``selected`` is an item id, a zone or label id
        (the highlight is then that zone) or None (the learner's own selection); ``live`` adds the live markers."""
        base = self.debug_map()
        sel = selected if selected is not None else self.review.selected(self)
        items = [i for i in self.review.items(self) if i.tier == 2]
        hl = self._highlight_cells(sel)
        others = tuple(c for c, i in ((self._item_cells(i), i) for i in items) if c and i.id != sel)
        focus = None
        if hl:
            focus = zones_mod.cells_centroid(self, hl)
        return replace(base, others=others, highlight=hl if hl else frozenset(), focus=focus,
                       live=self.live_view() if live else ())

    def _item_cells(self, item) -> Optional[frozenset]:
        return where_mod.item_cells(self, item)

    def _highlight_cells(self, sel: Optional[str]) -> Optional[frozenset]:
        """The cells of the selected item: a place, merge or modify item shows its suggestion; a zone or a label its own
        cells; the night-window item, the bed item and None highlight nothing. A suggestion id that is not an open item
        any more (a push head that was answered) is still drawn."""
        if sel is None or sel in ITEM_IDS or sel == ITEM_RADAR_MOVED:
            return None
        if sel.startswith("mrg-"):
            s = zones_mod.suggestion_of(self, sel[4:])
        elif sel.startswith("ret-") and self.p7on:
            z = zones_mod.find_record(self, sel[4:])
            return frozenset(z["cells"]) if z is not None else None
        elif sel.startswith("mod-") and self.p7on:
            kind, prop, _ = self.review.resolve(self, sel)      # P7: the proposal's suggestion, not the zone's ref
            s = zones_mod.suggestion_of(self, prop["sid"]) if prop is not None else None
        elif sel.startswith("mod-"):
            z = zones_mod.find_record(self, sel[4:])
            s = zones_mod.suggestion_of(self, z["ref"]) if z is not None else None
        else:
            s = zones_mod.suggestion_of(self, sel)
            if s is None:
                z = zones_mod.find_record(self, sel)
                if z is not None:
                    return frozenset(z["cells"])
                lb = zones_mod.find_label(self, sel)
                return frozenset(lb["cells"]) if lb is not None else None
        return frozenset(s["cells"]) if s is not None else None

    def where_am_i(self, selected: Optional[str] = None) -> WhereView:
        """Where the single confirmed track is (spec 2.7)."""
        return where_mod.where_am_i(self, selected)

    def live_view(self) -> tuple:
        """((x_m, y_m, stay), ...) of the confirmed live tracks while "learning", else ()."""
        return where_mod.live_view(self)

    def reset(self) -> "Learner":
        """Forget all (spec 7.2 step 2, D40): a new learner that carries over exactly the node's window mirror, the four
        settings and the push counters ``seq`` and ``acked``; everything else is fresh."""
        fresh = Learner(self.p, self.tm, None, self.p5params, self.p6params, self.p7params)
        fresh.window = dict(self.window)
        kept = self.p5.reset_for_forget()
        fresh.p5.settings = kept.settings
        fresh.p5.push = kept.push
        fresh.zone6 = self.zone6.reset_for_forget()      # P6: all zone data is dropped, the epoch is carried plus one
        fresh.zone_eval.attach(fresh.zone6)
        fresh.review.touch()
        return fresh

    def review_state(self) -> tuple:
        """(items: tuple of ReviewItem, selected: Optional[str]) as of now, also before the first step."""
        items = self.review.items(self)
        return tuple(items), self.review.selected(self, items)

    def push_request(self) -> PushRequest:
        """The current push request (spec 6.2): the stored counters and head, ``count`` = the open items now."""
        return self.planner.request(self.review.items(self))

    def paused_stays(self) -> int:
        """Stays dropped by the guests pause (spec 7.3). ``counters()`` is not changed by it."""
        return self.bh.paused_stays

    # ------------------------------------------------------------ P7 interface (P7 spec A.3); read-only views
    @property
    def p7on(self) -> bool:
        return bool(self.p7params.enabled)

    def drift_view(self) -> tuple:
        """(ZoneDrift, ...) of every zone record, then every label (ordered like ``zones()``); () when P7 is off."""
        if not self.p7on:
            return ()
        return drift_mod.view(self)

    def radar_moved_state(self) -> RadarMovedView:
        """The radar-moved detector as it is now; ("none", 0, 0, (0, 0, 0), False) when P7 is off."""
        if not self.p7on:
            return RadarMovedView("none", 0, 0, (0, 0, 0), False)
        return radarmoved_mod.view(self)

    def moved_from_count(self) -> int:
        return len(self.zone7.mf)

    def reset_maps(self) -> None:
        """Spec 4.7, in place: clears the learned maps, closes the radar-moved item and marks every zone and label
        'needs re-check'. No output: the answer handler adds DebugMapChanged and SaveRequested."""
        radarmoved_mod.reset_maps(self)
        z7 = self.zone7
        rm = z7.rm
        rm["open"] = False
        rm["st"] = 0
        rm["d0"] = 0
        today = self.review._today(self)
        for z in drift_mod.zone_refs(self):
            e = z7.entry(z.zid, create=True)
            e["rc"] = True
            e["rd"] = today
        self.review.touch()

    def reset_summary(self) -> dict:
        """Read-only counts for the replay's check of the reset (spec A.3)."""
        return radarmoved_mod.summary(self)

    # ------------------------------------------------------------ P6 interface (P6 spec A.4); STUBS until CORE lands
    def zone_states(self) -> ZoneStates:
        """The current zone snapshot, also before the first output (seeding, like ``review_state``)."""
        self._zone_sync_check()
        return self.zone_eval.states()

    def zone_snapshot(self) -> tuple:
        """Read-only ((id, occ, valid), ...) for P7."""
        self._zone_sync_check()
        return self.zone_eval.snapshot()

    def zone_debug(self) -> tuple:
        """Read-only replay diagnostics (P6-1): per enter (zid, track, first_inside_t_dev_ms, decisive_t_dev_ms,
        frames_not_counted); not an output, not persisted, bounded to the last 1,000 enters."""
        return self.zone_eval.debug()

    # ------------------------------------------------------------ P6 wiring (spec 4.2)
    def _zone_key(self) -> tuple:
        rv = self.review
        return (rv.ver, len(rv.zones), len(self.label_list), len(self.ns.suggestions), self.last_run_ord)

    def _zone_sync_check(self, force: bool = False) -> None:
        """Gives the evaluator the confirmed zones and the shadow door cells when either changed (spec 4.2 step 5)."""
        key = self._zone_key()
        if not force and key == self._zkey:
            return
        self._zkey = key
        zones = [(z["zid"], z["kind"], z["cells"]) for z in self.review.zones]
        zones += [(lb["id"], lb["kind"], lb["cells"]) for lb in self.label_list]
        doors = [s["cells"] for s in self.ns.suggestions if s["type"] == "entry_zone" and s["state"] == "shadow"]
        ze = self.zone_eval
        ze.sync(zones, doors)
        if zones or self._zones_seen:                 # a state with zone6 data and no zone at all is valid (spec 6.2)
            n = self.zone6.prune([z[0] for z in zones])
            if n:
                self.zone6_pruned += n
                ze.refresh()
            n7 = self.zone7.prune([z[0] for z in zones])      # P7 (spec 5.2): the same orphan rule for zone7
            if n7:
                self.zone7_pruned += n7
        if zones:
            self._zones_seen = True

    def _zone_outputs(self, outs: list) -> None:
        ze = self.zone_eval
        outs.extend(ze.drain())
        st = ze.take_states()
        if st is not None:
            outs.append(st)

    def _zone_command(self, ev) -> bool:
        """ZoneTune, ZoneForget, VariantRequest. True when persisted data changed (the caller requests a save)."""
        self._zone_sync_check()
        ze = self.zone_eval
        p6 = self.p6params
        z6 = self.zone6
        zid = ev.zid
        if type(zid) is not str or zid not in ze.zones:
            self.zone_cmds_rejected += 1
            return False
        if isinstance(ev, ZoneTune):
            v = ev.value
            if ev.field not in ZONE_TUNE_FIELDS or type(v) is not int:
                self.zone_cmds_rejected += 1
                return False
            lo, hi = (p6.tune_delay_min_s, p6.tune_delay_max_s) if ev.field == "cd" else \
                (p6.margin_min_cm, p6.margin_max_cm)
            if not lo <= v <= hi:
                self.zone_cmds_rejected += 1
                return False
            t = z6.tune.setdefault(zid, {})
            if t.get(ev.field) == v:
                return False
            t[ev.field] = v
            ze.refresh()
            return True
        if isinstance(ev, ZoneForget):
            z6.forget_zone(zid)
            self.zone7.forget_zone(zid)               # P7 (H3, D44): the flag and retire runs of that zone only
            self.review.touch()                       # the withdrawn retire proposal leaves the cached tier-2 list
            ze.forget(zid)
            return True
        info = z6.info.get(zid)
        ids = {variant_id(x) for x in info["var"]} if info else set()
        if type(ev.variant) is not str or ev.variant not in ids:
            self.zone_cmds_rejected += 1
            return False
        cur = list(z6.ve.get(zid, ()))
        if ev.on and ev.variant not in cur:
            cur.append(ev.variant)
        elif not ev.on and ev.variant in cur:
            cur.remove(ev.variant)
        else:
            return False
        if cur:
            z6.ve[zid] = sorted(cur)
        else:
            z6.ve.pop(zid, None)
        ze.refresh()
        return True

    def suggestions(self) -> tuple:
        out = []
        for s in sorted(self.ns.suggestions, key=lambda d: cid_key(d["id"])):
            out.append(Suggestion(
                s["id"], s["rev"], s["type"], s["kind"], s["name_hint"], s["state"], s["first_night"],
                s["last_night"], float(s["cx"]), float(s["cy"]), frozenset(s["cells"]),
                tuple(tuple(v) for v in s["poly_cm"]), float(s["conf"]), _deep_copy(s["evidence"]),
                tuple(dict(v) for v in s["variants"]), s["variant_evidence_split"], s["variant_share_split"],
                s["variant_suppressed"], s["frozen"]))
        return tuple(out)

    def labels(self) -> tuple:
        return tuple(Label(lb["id"], lb["created_s"], float(lb["x"]), float(lb["y"]), lb["name"], lb["kind"],
                           frozenset(lb["cells"])) for lb in self.label_list)

    def candidates(self) -> tuple:
        out = []
        for c in sorted(self.ns.candidates, key=lambda d: cid_key(d["cid"])):
            gates = c.get("gates") or {}
            out.append(CandidateView(
                c["cid"], c.get("cls", "dwell"), frozenset(c["cells"]), float(c["cx"]), float(c["cy"]),
                int(c.get("n", 0)), int(c.get("core_days", 0)), float(c.get("minutes", 0.0)),
                float(c.get("median_s", 0.0)), float(c.get("ld_share", 0.0)), float(c.get("ghost_share", 0.0)),
                float(c.get("covis", 0.0)), c.get("covis_src"), int(c["streak"]), float(c.get("conf", 0.0)),
                dict(gates), c.get("outcome", "failed")))
        return tuple(out)

    def stays(self) -> tuple:
        out = []
        for st in sorted(self.bh.stays, key=lambda d: (d["t0"], d["t1"])):
            out.append(StayView(st["t0"], st["t1"], st["dur"], st["x"] / 100.0, st["y"] / 100.0, float(st["ld"]),
                                float(st["he"]), st["exit"], st["arr"], st["day"]))
        return tuple(out)

    def counters(self) -> dict:
        c = dict(self.obs.counters)
        # P6 counters appear only when they are not zero, so a learner without zones reports what P5 reported
        for k, v in (("zones_skipped_kind", self.zone_eval.counters["zones_skipped_kind"]),
                     ("zone6_pruned", self.zone6_pruned), ("zone_cmds_rejected", self.zone_cmds_rejected)):
            if v:
                c[k] = v
        if self.p7on:                                 # P7 (spec 7.2): only when enabled, so a P6 learner reports the same
            c["zone7_pruned"] = self.zone7_pruned
            c["zone7_reset"] = self.zone7.reset_count
            c["p7_errors"] = self.p7_errors
        return c

    # ------------------------------------------------------------ ingest actions
    def _run(self, acts: list) -> None:
        for k, a in enumerate(acts):
            if isinstance(a, Censor):
                self._censor(a, acts[k + 1] if k + 1 < len(acts) else None)
            else:
                self._track(a)

    # the reasons of ``Observation`` censors that end the radar observation (P6 spec 5.7: POWER); the interruptions
    # that can have an unobserved span of a few seconds (GAP) are the sequence, frame-gap, spacing and clock reasons.
    # A reason this table does not know is POWER (the safe side).
    _CENSOR_GAP = frozenset(("seq_gap", "seq_resync", "gap", "compressed", "clock_step"))

    def censor_class(self, c, nxt=None) -> str:
        """POWER or GAP for a ``Censor`` (spec 5.7, D28, D42). POWER wins: an interruption of silence_censor_ms or more
        is POWER whatever its reason."""
        if c.reason not in self._CENSOR_GAP:
            return "POWER"
        if type(nxt) is TrackFrames and nxt.start_dev - c.at_dev >= self.p.silence_censor_ms:
            return "POWER"
        return "GAP"

    def _censor(self, c=None, nxt=None) -> None:
        bh = self.bh
        for tr in self.tracker.censor_all():
            bh.finish(tr, censored=True)
        if c is not None:
            self.zone_eval.on_censor(self.censor_class(c, nxt), c.at_dev)
        bh.flush_censored()
        bh.hold_times = []

    def _track(self, a) -> None:
        tracker = self.tracker
        bh = self.bh
        ze = self.zone_eval
        off = a.offset
        samples: list = []
        self._zone_sync_check()
        ze.begin_batch(a.start_dev, a.end_dev)
        for dev, targets in a.frames:
            dets = [(t.x_mm / 1000.0, t.y_mm / 1000.0) for t in targets]
            tracker.step(dev, dets, off, a.start_dev, samples)
            ze.on_frame(dev, tracker.live)
            if samples:
                for tr, t, x, y in samples:
                    bh.on_sample(tr, t, x, y)
                samples.clear()
        tracker.expire(a.end_dev)
        ze.on_time(a.end_dev, self.now)
        for tr in tracker.ready(a.end_dev):
            bh.finish(tr, censored=False)
        holds = [((tr.born_dev + off) / 1000.0, tr.bx, tr.by) for tr in tracker.live if bh.can_open(tr)]
        holds += [((tr.born_dev + off) / 1000.0, tr.bx, tr.by) for tr in tracker.dying if bh.can_open(tr)]
        holds.sort()
        bh.hold_times = holds
        bh.flush((a.end_dev + off) / 1000.0)

    # ------------------------------------------------------------ nightly
    def _maybe_nightly(self, outs: list) -> bool:
        p = self.p
        now = self.now
        if self._nl_next is not None and now < self._nl_next:
            return False
        at = p.nightly_at_min * 60
        lt = self.tm.local(now)
        # next check: this date's (or the next date's) run time, re-checked at least every 60 s so a
        # DST change shifts nothing by more than a minute
        until = (at - lt.sec_of_day) if lt.sec_of_day < at else (86400 - lt.sec_of_day + at)
        self._nl_next = now - now % 1000 + min(until * 1000, 60000)
        if lt.sec_of_day < at:
            return False
        if self.last_run_ord is not None and lt.date_ord <= self.last_run_ord:
            return False
        if self.created_ord is None:
            return False
        if lt.date_ord == self.created_ord and self.created_ms is not None and \
                self.tm.local(self.created_ms).sec_of_day > at:
            return False                     # created after this date's run time: first run tomorrow
        night = lt.date_ord - self.created_ord
        try:
            self.bh.flush(now / 1000.0, ignore_holds=True)     # a hold never outlives the night
            n_cands, n_new = run_nightly(self, lt.date_ord, night)
            self.last_run_ord = lt.date_ord                    # (set again below) the P4 hooks read it
            self.night = night
            self.night_m.prune(now)
            self.review.on_nightly(self)
            self.rv_dirty = True
        except Exception as exc:
            # contained: the event that triggered the run is processed, the run is not retried every
            # minute, and the next night tries again; the shell logs where it failed
            self.review.touch()
            self.nightly_errors += 1
            self.last_run_ord = lt.date_ord
            self.night = night
            outs.append(NightlyFailed(date.fromordinal(lt.date_ord).isoformat(), _where(exc)))
            return True
        self.last_run_ord = lt.date_ord
        self.night = night
        try:
            self._zone_sync_check()
            zoneinfo.nightly(self, night)            # P6: learned delays, ld2412_reliable, variants; contained
        except Exception as exc:
            self.nightly_errors += 1
            outs.append(NightlyFailed(date.fromordinal(lt.date_ord).isoformat(), _where(exc)))
        if self.p7on:
            self._p7_nightly(night, date.fromordinal(lt.date_ord).isoformat(), outs)
        outs.append(NightlyDone(night, date.fromordinal(lt.date_ord).isoformat(), n_cands, n_new))
        outs.append(DebugMapChanged("nightly"))
        outs.append(SaveRequested("nightly"))
        return True

    def _p7_nightly(self, night: int, iso: str, outs: list) -> None:
        """P7 spec 4.1: four steps, each in its own ``try``, counted in ``p7_errors`` and reported as a
        ``NightlyFailed`` whose error starts with the step's tag; a failing step never stops the others, the review or
        the save. The tuning line, when the last step made one, is appended to the outputs."""
        line = None
        for tag, step in (("p7:drift", lambda: drift_mod.nightly(self, night)),
                          ("p7:release", lambda: movedfrom_mod.release(self)),
                          ("p7:radar", lambda: radarmoved_mod.nightly(self, night)),
                          ("p7:tuning", lambda: tuningagg_mod.nightly(self, night))):
            try:
                res = step()
                if tag == "p7:tuning":
                    line = res
            except Exception as exc:
                self.p7_errors += 1
                outs.append(NightlyFailed(iso, "%s %s" % (tag, _where(exc))))
        self.review.touch()
        if isinstance(line, TuningLine):
            outs.append(line)

    # ------------------------------------------------------------ 'Mark spot here'
    def _mark(self, outs: list) -> None:
        now = self.now
        lid = None
        kind = None
        reason = None
        new = False
        if self._state_now() != "learning":
            reason = "not_observing"
        elif self.last_mark_ms is not None and 0 <= now - self.last_mark_ms < MARK_MIN_INTERVAL_MS:
            reason = "too_soon"
        else:
            ips = labels_mod.stays_in_progress(self)
            if not ips:
                reason = "no_stay"
            elif len(ips) > 1:
                reason = "several_stays"
            else:
                # the review's bed item is selected: kind bed, named 'Bed' or the name text (OA 7.6)
                bound = self.review.bound_to_bed(self)
                name = (self.spot_name or "Bed") if bound else self.spot_name
                lb = labels_mod.find_same(self.label_list, ips[0][2], ips[0][3], name, self.p.label_radius_m)
                if lb is None:
                    lb = labels_mod.make_label(self, ips[0], now, name=name, kind="bed" if bound else None)
                    labels_mod.add_label(self, lb)
                    new = True
                    for s in self.ns.suggestions:
                        labels_mod.link_one(s, self.label_list, self.geom)
                    if lb["kind"] == "bed":
                        refresh_places_now(self)               # the OA 7.4 freeze applies at once
                    self.review.touch()
                    self.rv_dirty = True
                # else the spot is marked already: the press changes nothing
                self.last_mark_ms = now
                lid = lb["id"]
                kind = lb["kind"]
        ok = reason is None
        outs.append(MarkResult(ok, reason, lid, kind))
        self.last_mark = {"result": "ok" if ok else "failed", "reason": reason, "label": lid, "kind": kind,
                          "at": _utc_min(now, 5)}
        if new:
            outs.append(DebugMapChanged("label"))
            outs.append(SaveRequested("label"))

    # ------------------------------------------------------------ status (spec 7.11, 9.3)
    def _state_now(self) -> str:
        if self.first_event_ms is None:
            return "starting"
        p = self.p
        now = self.now
        lf = self.obs.last_frames_ms
        ref = lf if lf is not None else self.first_event_ms
        if now - ref >= p.no_data_ms:
            return "no_data"
        if lf is None:
            return "starting"
        if self.obs.observing and self.obs.node_online is not False and now - lf < p.silence_censor_ms:
            return "learning"
        return "not_observing"

    def _status(self, outs: list, force: bool, periodic: bool) -> None:
        st = self._state_now()
        now = self.now
        emit = force
        if st != self.state:
            self.state = st
            self.state_since_ms = now
            emit = True
        elif periodic and (self.last_status_ms is None or now - self.last_status_ms >= self.p.status_period_ms):
            emit = True
        if emit:
            self.last_status_ms = now
            outs.append(StatusChanged(st, self._attributes()))

    def _attributes(self) -> dict:
        obs = self.obs
        now = self.now if self.now is not None else 0
        shadow = [s for s in self.ns.suggestions if s["state"] == "shadow"]
        places = sorted([s for s in shadow if s["type"] in PLACE_TYPES],
                        key=lambda s: (-s["conf"], cid_key(s["id"])))[:20]
        recall, precision = labels_mod.precision_recall(self.label_list, self.ns.suggestions, self.geom,
                                                        self.p.label_match_m)
        today = self.tm.local(now).date_ord if self.now is not None else None
        covered = len(obs.covered_dates(today)[-self.p.retention_covered_days:]) if today is not None else 0
        rv = self.review.attributes(self)
        attrs = {
            "version": APP_VERSION, "schema": SCHEMA_VERSION, "mode": "shadow",
            "state_since": _utc_min(self.state_since_ms),
            "node_online": obs.node_online, "observing": obs.observing, "boot_id": obs.boot_id,
            "ingest": dict(obs.counters),
            "observed_h_24h": round(obs.observed_s_last_24h(now) / 3600.0, 1) if self.now is not None else 0.0,
            "covered_days": covered, "nights": self.night,
            "last_nightly": date.fromordinal(self.last_run_ord).isoformat() if self.last_run_ord else None,
            "schedule": {"sched": self.window["sched"], "start": _hhmm(self.window["start"]),
                         "end": _hhmm(self.window["end"])},
            "candidates": len(self.ns.candidates),
            "reflections": sum(1 for s in shadow if s["type"] == "reflection"),
            "doors": sum(1 for s in shadow if s["type"] == "entry_zone"),
            "outline": bool(self.ns.outline["done"]),
            "ignore_zones": sum(1 for s in shadow if s["type"] == "ignore_zone"),
            "suggestions": [{"id": s["id"], "type": s["type"], "kind": s["kind"], "name": s["name_hint"],
                             "night": s["first_night"], "conf": s["conf"], "x_cm": int(round(s["cx"] * 100)),
                             "y_cm": int(round(s["cy"] * 100)), "state": s["state"],
                             "variants": len(s["variants"])} for s in places],
            "labels": len(self.label_list), "label_recall": recall, "label_precision": precision,
            "last_mark": dict(self.last_mark) if self.last_mark else None,
            "nightly_errors": self.nightly_errors,
            "radar_valid": self._state_now() == "learning",
            "night": self._night_attributes(),
            "review": rv,
            "night_learning": {"active": self.nl.active, "set_by_learner": self.nl.set_by,
                               "until": date.fromordinal(self.nl.until_ord).isoformat()
                               if self.nl.until_ord is not None else None,
                               "phase": self.nl.phase, "last_event": self.nl.last_event},
            # P5 (spec A.9): counts and switches only, no name, no time, no coordinate
            "zones_total": len(self.review.zones) + len(self.label_list),
            "pending_places": rv["tier2"],
            "learning_paused": self._paused_on,
            "settings": {name: bool(self.p5.settings.get(name, False)) for name in SETTING_NAMES},
            "pet_spots": sum(1 for s in shadow if s["kind"] == "pet"),
            # P6 (spec 7.2): counts only, no name, no id, no coordinate
            "zones_live": len(self.zone_eval.zones) if self.zone_eval.valid and not self.zone_eval.suspended else 0,
            "zones_occupied": self.zone_eval.zones_occupied(),
            "zone_events_24h": self.zone_eval.events_24h(),
            "zones_over_budget": self.zone_eval.zones_over_budget(),
        }
        if self.p7on:
            attrs.update(self._p7_attributes())
        return attrs

    def _p7_attributes(self) -> dict:
        """P7 (spec 7.2): counts only, no name, no id, no coordinate; added only when P7 is enabled."""
        z7 = self.zone7
        ids = {z["zid"] for z in self.review.zones} | {lb["id"] for lb in self.label_list}
        ents = [e for zid, e in z7.z.items() if zid in ids]
        rstate = radarmoved_mod.state_name(z7.rm)
        return {"zones_frozen": sum(1 for v in zones_mod.zone_views(self) if v.frozen),
                "zones_blind": sum(1 for e in ents if e["vs"] == "blind"),
                "zones_falling": sum(1 for e in ents if e["vs"] == "falling"),
                "zones_unused": sum(1 for e in ents if e["lo"] >= self.p7params.flag_nights),
                "zones_recheck": sum(1 for e in ents if e["rc"]),
                "moved_from": len(z7.mf), "radar_moved": rstate,
                "places_probation": self.review.probation_held, "places_recheck_held": self.review.recheck_held}

    def _night_attributes(self) -> dict:
        nm = self.night_m
        raw = nm.raw
        prop = None
        if raw is not None:
            prop = dict(raw)
            d = self.review.nw_data(self)
            prop.update({"start": hhmm(raw["start_min"]), "end": hhmm(raw["end_min"]),
                         "current_saving_pct": d["current_saving_pct"], "late_nights": d["late_nights"],
                         "late_of": d["late_of"]})
        return {"onsets": {k: len(v) for k, v in nm.onsets.items()}, "wakes": len(nm.wakes),
                "night_lux": nm.night_lux, "proposal_rev": nm.proposal_rev, "proposal": prop}

    # ------------------------------------------------------------ output order (spec 5.3)
    @staticmethod
    def _order(outs: list) -> list:
        res = []
        for cls in _OUTPUT_ORDER:
            items = [o for o in outs if type(o) is cls]
            if not items:
                continue
            if cls is StatusChanged:
                res.append(items[-1])
            elif cls in (DebugMapChanged, SaveRequested):
                res.append(min(items, key=lambda o: _REASON_PRIORITY.get(o.reason, 9)))
            else:
                res.extend(items)
        return res


def _where(exc: BaseException) -> str:
    """Exception type and the innermost place it was raised in, without any value (privacy, spec 8.3)."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is None:
        return type(exc).__name__
    name = tb.tb_frame.f_code.co_filename.replace(chr(92), "/").rsplit("/", 1)[-1]
    return "%s at %s:%d (%s)" % (type(exc).__name__, name, tb.tb_lineno, tb.tb_frame.f_code.co_name)


def _deep_copy(v):
    if isinstance(v, dict):
        return {k: _deep_copy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_deep_copy(x) for x in v]
    return v
