"""Review items, the bed item and its answers, suppressions and confirmed zones (P4; OA 7.6, 7.8, 7.9) and the P5
setup review: tiers, place items, merge and modify items, the answer matrix, edit times (P5 spec 2, 3).

The item list (tier 1, then tier 2; spec 2.1):

- ``night_window`` (tier 1; revision = ``NightModel.proposal_rev``): the proposal of 7.5. States: ``none`` (no
  proposal), ``proposed``, ``same`` (the proposal, or the window an approval wrote, is within ``nw_same_min`` of both
  current edges: applied), ``snoozed`` ([Later]) and ``suppressed`` ([Deny], for ``nw_deny_days`` days).
- ``bed_needs_data`` (tier 1; revision ``bed_rev``): shown while no zone of kind ``bed`` exists. States: ``open``,
  ``snoozed`` ([Later]: until the next nightly run, the 'next daily review'), ``suppressed`` ([No bed in view], kept in
  ``suppressions``, undone under 'Advanced') and ``withdrawn`` (a zone of kind bed exists, or a place named 'bed' is
  suggested and not rejected).
- places, ``mrg-<sid>`` and ``mod-<zid>`` items (tier 2): a shadow place suggestion that no zone or label covers, that
  is not rejected and not snoozed, ordered by confidence, at most ``review_max_places`` in all. Each has a persistent
  tag (``A``, ``B``, ...), the only identity the user sees.

The list exists once the setup review is ready (``review_min_nights`` nights, OA 7.9). The select shows the first item
until the user chooses with ``ReviewSelect`` (an item, or None = nothing: then no press is bound to an item); 'Mark spot
here' is bound to the bed item exactly when the select shows it (OA 7.6): it then makes kind ``bed`` named 'Bed' (or the
name text). Any other press keeps the P3 name rule: kind ``manual``, except that a spot name that is the whole word bed
or bett (trimmed, any case) still gives kind ``bed`` (a deliberate reading of OA 7.6, recorded in CHANGELOG 0.4.0).

Zones: a 'Mark spot here' label is the zone it makes (kind ``bed`` or ``manual``, D13); approving or renaming a place
suggestion makes a record in ``zones`` (``zones.py``; P6 builds entities from both). Names are compared whole, trimmed
and case-insensitive (``bed``, ``bett``).

Every answer that changes nothing says why (``AnswerIgnored``, spec 3.2, D17). The tier-2 list is cached by a change
counter (``ver``) and the local date, so ``items()`` costs almost nothing per second (spec 14 risk 8, D39): every
mutation made through this model bumps ``ver``; code that changes a suggestion, a label or a zone from outside calls
``touch()``.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Optional

from . import drift, moves, movedfrom, tuningagg
from . import labels as labels_mod
from . import rejections, zonework
from . import zones as zones_mod
from .advanced import advanced_entries
from .advanced import apply as advanced_apply
from .anchor import typical_hours
from .extraction import cid_key
from .night import circ_diff, window_len
from .p5state import P5State
from .params import DEFAULT_P5, P5Params, Params
from .types import (ANSWER_OPS, ITEM_BED, ITEM_IDS, ITEM_NIGHT_WINDOW, ITEM_RADAR_MOVED, AnswerIgnored,
                    DebugMapChanged, NodeCommand, ReviewChanged, ReviewItem, StateError)

PLACE_TYPES = ("dwell_zone", "short_task_zone")
SUPPRESSION_TYPES = ("bed_needs_data", "night_window", "place")
KIND_BY_TYPE = {"dwell_zone": "dwell", "short_task_zone": "short_task"}
MAX_ZONES = 100
MAX_SUPPRESSIONS = rejections.MAX_SUPPRESSIONS
NAME_MAX = zones_mod.NAME_MAX
NEVER = 1                    # date ordinal of 0001-01-01: 'no nightly run yet'


def hhmm(minute: int) -> str:
    return "%02d:%02d" % (minute // 60, minute % 60)


class ReviewModel:
    def __init__(self, p: Params, time_model, p5params: P5Params = DEFAULT_P5, p5: Optional[P5State] = None) -> None:
        self.p = p
        self.tm = time_model
        self.p5params = p5params
        self.p5 = p5 if p5 is not None else P5State()
        # persisted ('zones', 'suppressions', 'review')
        self.zones: list = []
        self.suppressions: list = []
        self.later: dict = {ITEM_NIGHT_WINDOW: None, ITEM_BED: None}   # date ordinal of the nightly run at the press
        self.bed_rev = 0
        self.next_zone_n = 1
        # RAM
        self.select: Optional[str] = None
        self.select_chosen = False                       # False: the select shows the first item
        self.rc_sig: tuple = ((), None)
        self.pending_window: Optional[dict] = None       # {"token", "start", "end", "rev"} of an approved window
        self.last_event: Optional[str] = None
        self.stale_answers = 0
        self.edit: Optional[dict] = None                 # {"rev", "start", "end"}: the 'Edit times' of the proposal
        self.ver = 0                                     # change counter of the tier-2 cache
        self._t2: Optional[tuple] = None                 # (key, tuple of ReviewItem)
        self._merge_by_sid: dict = {}
        self._mod_by_zid: dict = {}
        self._ret_by_zid: dict = {}                      # P7: the retire proposals by zone id (incl. snoozed ones)
        self.more = 0                                    # open tier-2 items that did not fit under the cap
        self.probation_held = 0                          # P7 RAM: places withheld by the probation rule (status count)
        self.recheck_held = 0                            # P7 RAM: places withheld next to a zone that needs re-check

    def touch(self) -> None:
        """Something the tier-2 list depends on changed outside this model: drop the cache."""
        self.ver += 1

    # ------------------------------------------------------------ names and kinds
    def kind_of(self, name: str) -> str:
        return labels_mod.kind_of_name(name, self.p.bed_label_names)

    # ------------------------------------------------------------ bed zone
    def bed_zones(self, L) -> list:
        """Labels and zones of kind bed, as dicts with x, y, cells."""
        out = [lb for lb in L.label_list if lb["kind"] == "bed"]
        out += [z for z in self.zones if z["kind"] == "bed"]
        return out

    def bed_zone_exists(self, L) -> bool:
        return any(lb["kind"] == "bed" for lb in L.label_list) or any(z["kind"] == "bed" for z in self.zones)

    def bed_cells(self, L) -> frozenset:
        """The bed region (cells of every bed label and zone, dilated once), empty while none exists."""
        cells = set()
        for z in self.bed_zones(L):
            cells |= z["cells"]
        return frozenset(L.geom.dilate(cells)) if cells else frozenset()

    def _rejected_place(self, sid: str) -> bool:
        return any(s["type"] == "place" and s["ref"] == sid for s in self.suppressions)

    def bed_suggested(self, L) -> bool:
        """A place named 'bed' is suggested (shadow, not rejected): the learner found the bed itself."""
        names = tuple(n.casefold() for n in self.p.bed_label_names)
        rejected = None
        for s in L.ns.suggestions:
            if s["type"] in PLACE_TYPES and s["state"] == "shadow" and \
                    (s["kind"] == "bed" or (s["name_hint"] or "").strip().casefold() in names):
                if rejected is None:
                    rejected = rejections.rejected_ids(L)
                if s["id"] not in rejected:
                    return True
        return False

    # ------------------------------------------------------------ suppressions and snoozes
    def _supp(self, typ: str) -> Optional[dict]:
        return next((s for s in self.suppressions if s["type"] == typ), None)

    def _snoozed(self, L, item: str) -> bool:
        marker = self.later.get(item)
        return marker is not None and marker == (L.last_run_ord or NEVER)

    def _today(self, L) -> int:
        return self.tm.local(L.now if L.now is not None else 0).date_ord

    def bed_state(self, L) -> tuple:
        """(state, reason) of the bed item."""
        if self.bed_zone_exists(L):
            return "withdrawn", "bed_zone"
        if self._supp("bed_needs_data") is not None:
            return "suppressed", "no_bed_in_view"
        if self.bed_suggested(L):
            return "withdrawn", "bed_suggested"
        if self._snoozed(L, ITEM_BED):
            return "snoozed", "later"
        return "open", None

    def nw_state(self, L) -> str:
        raw = L.night_m.raw
        if raw is None:
            return "none"
        if L.window["sched"] is None:
            return "no_p2"                                    # the node has no schedule to change (pre-P2)
        s = self._supp("night_window")
        if s is not None and self._today(L) < s["until_ord"]:
            return "suppressed"
        w = L.window
        same = self.p.nw_same_min
        if circ_diff(raw["start_min"], w["start"]) <= same and circ_diff(raw["end_min"], w["end"]) <= same:
            return "same"
        a = self.p5.nw_applied                                # the window an approval wrote (an edited one differs
        if a is not None and a["rev"] == L.night_m.proposal_rev and \
                circ_diff(a["start"], w["start"]) <= same and circ_diff(a["end"], w["end"]) <= same:
            return "same"                                     # from the proposal): it does not bring the item back
        if self._snoozed(L, ITEM_NIGHT_WINDOW):
            return "snoozed"
        return "proposed"

    def ready(self, L) -> bool:
        return L.night >= self.p.review_min_nights

    # ------------------------------------------------------------ tier 2: places, merge and modify items
    def pending_basic(self, L) -> list:
        """The suggestions that are open places before the merge rule (spec 2.2 rules 1 to 4), ordered by confidence:
        shadow place, not covered by a zone record or a label, not rejected (by id or by a rejection mask), not
        snoozed."""
        run = L.last_run_ord or NEVER
        covered = zones_mod.covered_ids(L)
        rejected = rejections.rejected_ids(L)
        later = self.p5.later
        out = []
        for s in L.ns.suggestions:
            if s["type"] not in PLACE_TYPES or s["state"] != "shadow":
                continue
            if later.get(s["id"]) == run:
                continue
            if s["id"] in covered or s["id"] in rejected or rejections.mask_match(L, s) or movedfrom.mask_match(L, s):
                continue
            if zones_mod.label_covers(L, s):
                continue
            out.append(s)
        out.sort(key=lambda s: (-s["conf"], cid_key(s["id"])))
        return out

    def _key2(self, L) -> tuple:
        return (self.ver, L.night, L.last_run_ord, self._today(L), len(L.ns.suggestions), len(self.zones),
                len(L.label_list), len(self.suppressions), len(self.p5.later), len(self.p5.masks))

    def tier2(self, L) -> tuple:
        """The open tier-2 items (cached): places, then merge items, then modify items, at most
        ``review_max_places`` in all. Allocates the tags of the items it lists (idempotent)."""
        key = self._key2(L)
        c = self._t2
        if c is not None and c[0] == key:
            return c[1]
        items = self._compute_tier2(L)
        self._t2 = (key, items)
        return items

    def _compute_tier2(self, L) -> tuple:
        p5 = self.p5
        cap = self.p5params.review_max_places
        run = L.last_run_ord or NEVER
        places = self.pending_basic(L)
        if L.p7on:
            return self._compute_tier2_p7(L, places, cap, run)
        merges, mods = zonework.proposals(L, places)
        self._merge_by_sid = {m["sid"]: m for m in merges}
        self._mod_by_zid = {m["zid"]: m for m in mods}
        merge_sids = set(self._merge_by_sid)
        plain = [s for s in places if s["id"] not in merge_sids]
        merges_open = [m for m in merges if p5.later.get("mrg-" + m["sid"]) != run]
        mods_open = [m for m in mods if p5.later.get("mod-" + m["zid"]) != run]
        combined = [("place", s) for s in plain[:cap]] + [("merge", m) for m in merges_open] + \
                   [("modify", m) for m in mods_open]
        shown = combined[:cap]
        self.more = len(plain) + len(merges_open) + len(mods_open) - len(shown)
        out = []
        for kind, x in shown:
            if kind == "place":
                out.append(self._place_item(L, x))
            elif kind == "merge":
                tag = p5.alloc_tag(x["sid"])
                out.append(ReviewItem("mrg-" + x["sid"], x["rev"], "merge_into",
                                      {"tag": tag, "sid": x["sid"], "zid": x["zid"], "zname": x["zname"],
                                       "ext_cm": x["ext_cm"], "dir": x["dir"], "r": x["r"]}, 2))
            else:
                tag = p5.alloc_tag(x["sid"])
                out.append(ReviewItem("mod-" + x["zid"], x["rev"], "modify_zone",
                                      {"tag": tag, "sid": x["sid"], "zid": x["zid"], "zname": x["zname"],
                                       "shift_cm": x["shift_cm"], "dir": x["dir"]}, 2))
        return tuple(out)

    def _compute_tier2_p7(self, L, places: list, cap: int, run: int) -> tuple:
        """The P7 path (spec 4.3.1, 6.1): ``moves.plan`` supplies the modify, merge and retire proposals and the ids a
        merge or modify consumes; probation and the 'needs re-check' hiding are applied only to the plain places left
        (a fresh modify candidate must not be hidden); the order is modify, places, merge, retire."""
        p5 = self.p5
        zone7 = L.zone7
        ctx = drift.context(L) if L.last_run_ord is not None else None
        merges, mods, retires, consumed = moves.plan(L, places, ctx)
        self._merge_by_sid = {m["sid"]: m for m in merges}
        self._mod_by_zid = {m["zid"]: m for m in mods}
        self._ret_by_zid = {r["zid"]: r for r in retires}
        plain = []
        prob = rech = 0
        for s in places:
            if s["id"] in consumed:
                continue
            if moves.rc_hidden(L, s):
                rech += 1
            elif ctx is not None and not moves.probation_ok(L, s, ctx):
                prob += 1
            else:
                plain.append(s)
        self.probation_held = prob
        self.recheck_held = rech
        merges_open = [m for m in merges if p5.later.get("mrg-" + m["sid"]) != run]
        mods_open = [m for m in mods if p5.later.get("mod-" + m["zid"]) != run]
        rets_open = [r for r in retires if not zone7.snoozed("ret-" + r["zid"], run)]
        combined = [("modify", m) for m in mods_open] + [("place", s) for s in plain] + \
                   [("merge", m) for m in merges_open] + [("retire", r) for r in rets_open]
        shown = combined[:cap]
        self.more = len(combined) - len(shown)
        out = []
        for kind, x in shown:
            if kind == "place":
                out.append(self._place_item(L, x))
            elif kind == "merge":
                tag = p5.alloc_tag(x["sid"])
                out.append(ReviewItem("mrg-" + x["sid"], x["rev"], "merge_into",
                                      {"tag": tag, "sid": x["sid"], "zid": x["zid"], "zname": x["zname"],
                                       "ext_cm": x["ext_cm"], "dir": x["dir"], "r": x["r"], "src": x["src"]}, 2))
            elif kind == "modify":
                tag = p5.alloc_tag(x["sid"])
                out.append(ReviewItem("mod-" + x["zid"], x["rev"], "modify_zone",
                                      {"tag": tag, "sid": x["sid"], "zid": x["zid"], "zname": x["zname"],
                                       "shift_cm": x["shift_cm"], "dir": x["dir"], "r": x["r"], "src": x["src"]}, 2))
            else:
                tag = p5.alloc_tag(x["zid"])
                out.append(ReviewItem("ret-" + x["zid"], x["rev"], "retire_zone",
                                      {"tag": tag, "zid": x["zid"], "zname": x["zname"], "weeks": x["weeks"]}, 2))
        return tuple(out)

    def _place_item(self, L, s: dict) -> ReviewItem:
        ev = s.get("evidence") or {}
        p5p = self.p5params
        data = {"tag": self.p5.alloc_tag(s["id"]), "kind": s["kind"], "conf": round(float(s["conf"]), 3),
                "x_cm": int(round(s["cx"] * 100)), "y_cm": int(round(s["cy"] * 100)),
                "name_hint": s["name_hint"] or "",
                "hrs_wd": typical_hours(ev.get("hours_wd"), p5p), "hrs_we": typical_hours(ev.get("hours_we"), p5p),
                "n": rejections.evidence_n(s), "frozen": bool(s["frozen"])}
        return ReviewItem(s["id"], s["rev"], s["type"], data, 2)

    def open_tier2(self, L, item_id) -> bool:
        """True when ``item_id`` is an open tier-2 item (the select accepts it, spec 2.5)."""
        return self.ready(L) and any(i.id == item_id for i in self.tier2(L))

    # ------------------------------------------------------------ the item list
    def items(self, L) -> list:
        if not self.ready(L):
            return []
        out = []
        if self.nw_state(L) == "proposed":
            out.append(ReviewItem(ITEM_NIGHT_WINDOW, L.night_m.proposal_rev, ITEM_NIGHT_WINDOW, self.nw_data(L)))
        if self.bed_state(L)[0] == "open":
            out.append(ReviewItem(ITEM_BED, self.bed_rev, ITEM_BED, self.bed_data(L)))
        rm = self.radar_item(L)
        if rm is not None:
            out.append(rm)
        out.extend(self.tier2(L))
        return out

    # ------------------------------------------------------------ the radar-moved item (P7 spec 6.1)
    def radar_item(self, L) -> Optional[ReviewItem]:
        """The tier-1 ``radar_moved`` item: open while the detector's latched item is open, the user did not ignore this
        revision and it is not snoozed. Never exists with P7 disabled."""
        if not L.p7on:
            return None
        rm = L.zone7.rm
        if not rm["open"] or rm["ign"] == rm["rev"] or L.zone7.snoozed(ITEM_RADAR_MOVED, L.last_run_ord or NEVER):
            return None
        dx, dy, tdeg = rm["tx"]
        return ReviewItem(ITEM_RADAR_MOVED, rm["rev"], "radar_moved",
                          {"shift_cm": int(round(math.hypot(dx, dy))), "deg": round(abs(tdeg) / 10.0, 1),
                           "streak": rm["st"]}, 1)

    def open_tier1(self, L, item_id) -> bool:
        """True when ``item_id`` is the open ``radar_moved`` item (the select accepts it, spec 6.3)."""
        return self.ready(L) and item_id == ITEM_RADAR_MOVED and self.radar_item(L) is not None

    def selected(self, L, items: Optional[list] = None) -> Optional[str]:
        items = self.items(L) if items is None else items
        ids = [i.id for i in items]
        if self.select_chosen and (self.select is None or self.select in ids):
            return self.select
        return ids[0] if ids else None

    def edit_now(self, L) -> Optional[dict]:
        """The 'Edit times' of the proposal being shown: only for the revision it was made on, only while proposed."""
        e = self.edit
        if e is None:
            return None
        if e["rev"] != L.night_m.proposal_rev or self.nw_state(L) != "proposed":
            self.edit = None
            return None
        return e

    def nw_data(self, L) -> dict:
        raw = L.night_m.raw
        w = L.window
        now = L.now if L.now is not None else 0
        created = (L.created_ord - 1) if L.created_ord is not None else None
        late, of = L.night_m.late_nights(w["start"], now, created)
        cur_len = window_len(w["start"], w["end"])
        d = {"start_min": raw["start_min"], "end_min": raw["end_min"], "saving_pct": raw["saving_pct"],
             "current_saving_pct": round(100.0 * cur_len / 1440.0, 1), "late_nights": late, "late_of": of,
             "from_lights_off": raw["from_lights_off"], "basis": raw["basis"]}
        text = "Switch both radars off between %s and %s. Radar on-time -%d%%. No radar detection during that time." \
            % (hhmm(raw["start_min"]), hhmm(raw["end_min"]), int(round(raw["saving_pct"])))
        if late:
            text += " Activity was still seen in the %d min before your current start on %d of the last %d nights." \
                % (self.p.nw_late_min, late, of)
        if raw["from_lights_off"]:
            text += " (from lights-off times)"
        d["text"] = text
        e = self.edit if (self.edit is not None and self.edit["rev"] == L.night_m.proposal_rev) else None
        d["edit_start"] = e["start"] if e is not None else None
        d["edit_end"] = e["end"] if e is not None else None
        d["sched_box"] = self.p5.setting("approve_sched")
        return d

    def bed_data(self, L) -> dict:
        w = L.window
        show = w["sched"] is True
        when = ", before %s or after %s" % (hhmm(w["start"]), hhmm(w["end"])) if show else ""
        text = ("Bed zone: lie in bed and press Mark spot here once%s (recommended; no extra radar-on nights). "
                "Otherwise the learner finds it by itself, which can take 2 weeks or more while the night "
                "schedule runs." % when)
        return {"start_min": w["start"], "end_min": w["end"], "sched": w["sched"], "show_window": show,
                "text": text}

    # ------------------------------------------------------------ resolving an item id
    def _suggestion(self, L, sid: str) -> Optional[dict]:
        return next((s for s in L.ns.suggestions if s["id"] == sid), None)

    def resolve(self, L, item: str) -> tuple:
        """(kind, object, revision) of an id an answer names, or (None, None, None). Kinds: ``nw``, ``bed``,
        ``merge`` (object: the proposal), ``modify`` (the proposal), ``place`` (the suggestion), ``zone`` (the record),
        ``label``. An item that is not open can still be resolved (the answer then says ``not_pending``)."""
        if item == ITEM_NIGHT_WINDOW:
            return "nw", None, L.night_m.proposal_rev
        if item == ITEM_BED:
            return "bed", None, self.bed_rev
        if item == ITEM_RADAR_MOVED:
            return ("radar", None, L.zone7.rm["rev"]) if L.p7on else (None, None, None)
        if item.startswith("mrg-") or item.startswith("mod-") or (L.p7on and item.startswith("ret-")):
            self.tier2(L)                                    # refreshes the proposals
            if item.startswith("mrg-"):
                m = self._merge_by_sid.get(item[4:])
                return ("merge", m, m["rev"]) if m is not None else (None, None, None)
            if item.startswith("ret-"):
                m = self._ret_by_zid.get(item[4:])
                return ("retire", m, m["rev"]) if m is not None else (None, None, None)
            m = self._mod_by_zid.get(item[4:])
            return ("modify", m, m["rev"]) if m is not None else (None, None, None)
        s = self._suggestion(L, item)
        if s is not None:
            return "place", s, s["rev"]
        z = zones_mod.find_record(L, item)
        if z is not None:
            return "zone", z, z["rev"]
        lb = zones_mod.find_label(L, item)
        if lb is not None:
            return "label", lb, 0
        return None, None, None

    def rev_of(self, L, item: str) -> Optional[int]:
        return self.resolve(L, item)[2]

    # ------------------------------------------------------------ output: ReviewChanged
    def check(self, L, outs: list) -> bool:
        """Appends a ReviewChanged when the items, their data (the text follows the schedule and the window) or the
        selected one changed. True if it did."""
        items = self.items(L)
        sel = self.selected(L, items)
        sig = (tuple((i.id, i.rev, tuple(sorted(i.data.items()))) for i in items), sel)
        if sig == self.rc_sig:
            return False
        self.rc_sig = sig
        outs.append(ReviewChanged(tuple(items), sel))
        return True

    # ------------------------------------------------------------ 'Mark spot here' binding (OA 7.6)
    def bound_to_bed(self, L) -> bool:
        return self.selected(L) == ITEM_BED

    # ------------------------------------------------------------ edit times (spec 3.4)
    def night_edit(self, L, ev, outs: list) -> None:
        """A ``NightTimeEdit``: stored in RAM for the proposal revision it was made on; without a proposed item it is
        refused (``AnswerIgnored(night_window, edit, no_such_item)``)."""
        f, m = ev.field, ev.minute
        if f not in ("start", "end") or isinstance(m, bool) or not isinstance(m, int) or not 0 <= m < 1440:
            return
        if self.nw_state(L) != "proposed":
            outs.append(AnswerIgnored(ITEM_NIGHT_WINDOW, "edit", "no_such_item"))
            return
        rev = L.night_m.proposal_rev
        if self.edit is None or self.edit["rev"] != rev:
            self.edit = {"rev": rev, "start": None, "end": None}
        self.edit[f] = m
        L.rv_dirty = True

    # ------------------------------------------------------------ answers
    def _ignore(self, outs: list, item, op: str, reason: str) -> bool:
        outs.append(AnswerIgnored(item, op, reason))
        return False

    def answer(self, L, ev, outs: list, now_ms: int) -> bool:
        """Applies a ReviewAnswer (spec 3.2, 3.3). True if something changed (the learner then requests a save); a
        refusal appends an ``AnswerIgnored`` and returns False. A malformed answer is ignored without any output."""
        op = ev.op
        if not isinstance(op, str) or not (ev.item is None or isinstance(ev.item, str)):
            return False                                      # a malformed answer is ignored, never raised on
        if op not in ANSWER_OPS:
            return False
        self.ver += 1
        try:
            return self._dispatch(L, ev, op, outs, now_ms)
        finally:
            self.ver += 1                                    # what the answer changed is read by the next items() call

    def _dispatch(self, L, ev, op: str, outs: list, now_ms: int) -> bool:
        if op == "forget":
            return self._forget(L)
        if op == "advanced":
            return advanced_apply(L, ev, outs, now_ms)
        item = ev.item if ev.item is not None else self.selected(L)
        if item is None:
            return self._ignore(outs, None, op, "no_item")
        if op == "undo":
            return self._undo(L, item, outs)
        if item.startswith("adv:"):
            return self._ignore(outs, item, op, "not_applicable")
        kind, obj, cur = self.resolve(L, item)
        if kind is None:
            return self._ignore(outs, item, op, "no_such_item")
        if ev.rev is not None and ev.rev != cur:
            self.stale_answers += 1
            return self._ignore(outs, item, op, "stale")
        if kind == "nw":
            return self._answer_window(L, ev, outs, now_ms)
        if kind == "bed":
            return self._answer_bed(L, ev, outs)
        if kind == "place":
            return self._answer_place(L, ev, item, obj, outs, now_ms)
        if kind == "merge":
            return self._answer_merge(L, ev, item, obj, outs, now_ms)
        if kind == "modify":
            return self._answer_modify(L, ev, item, obj, outs, now_ms)
        if kind == "retire":
            return self._answer_retire(L, ev, item, obj, outs)
        if kind == "radar":
            return self._answer_radar(L, ev, outs)
        return self._answer_record(L, ev, item, outs)         # a zone record or a label: only 'rename'

    def _forget(self, L) -> bool:
        """'Forget rejections': every suppression, snooze and mask goes. P7 (DV13): also the moved-from and retired masks,
        every Keep date, the P7 snoozes and the radar-moved Ignore mark."""
        changed = bool(self.suppressions) or any(v is not None for v in self.later.values()) or bool(self.p5.later)
        self.suppressions = []
        self.later = {ITEM_NIGHT_WINDOW: None, ITEM_BED: None}
        self.p5.later.clear()
        self.p5.masks.clear()
        z7 = L.zone7
        if L.p7on and (z7.mf or z7.snz or z7.rm["ign"] or z7.rm["qt"] or any(e["kp"] for e in z7.z.values())):
            changed = True
            movedfrom.clear(L)
            z7.snz.clear()
            z7.rm["ign"] = 0
            z7.rm["qt"] = 0                                    # review fix RM-5: the quiet time after an Ignore goes too
            for e in z7.z.values():
                e["kp"] = 0
        if changed:
            self.bed_rev += 1
        return changed

    def undo_item(self, L, item: str) -> bool:
        """Removes the suppression and the snooze of a tier-1 item; the bed item gets a new revision. True if
        anything changed."""
        typ = {ITEM_BED: "bed_needs_data", ITEM_NIGHT_WINDOW: "night_window"}.get(item)
        if typ is None:
            return False
        n = len(self.suppressions)
        self.suppressions = [s for s in self.suppressions if s["type"] != typ]
        had_later = self.later.get(item) is not None
        self.later[item] = None
        changed = n != len(self.suppressions) or had_later
        if item == ITEM_BED and changed:
            self.bed_rev += 1
        return changed

    def _undo(self, L, item: str, outs: list) -> bool:
        if item not in ITEM_IDS:
            return self._ignore(outs, item, "undo", "not_applicable")
        if not self.undo_item(L, item):
            return self._ignore(outs, item, "undo", "not_pending")
        return True

    def _add_supp(self, L, s: dict) -> None:
        self.suppressions.append(s)
        rejections.evict(L)

    # ------------------------------------------------------------ the night window (spec 3.4)
    def _answer_window(self, L, ev, outs: list, now_ms: int) -> bool:
        state = self.nw_state(L)
        raw = L.night_m.raw
        op = ev.op
        if state != "proposed" or raw is None:
            return self._ignore(outs, ITEM_NIGHT_WINDOW, op, "not_pending")
        rev = L.night_m.proposal_rev
        if op == "approve":
            pw = self.pending_window
            if pw is not None and pw["rev"] == rev:
                return self._ignore(outs, ITEM_NIGHT_WINDOW, op, "not_pending")   # the node's echo is awaited
            e = self.edit_now(L)
            start = e["start"] if e is not None and e["start"] is not None else raw["start_min"]
            end = e["end"] if e is not None and e["end"] is not None else raw["end_min"]
            if not self.p.nw_min_len_min <= window_len(start, end) <= self.p.nw_max_len_min:
                return self._ignore(outs, ITEM_NIGHT_WINDOW, op, "times_invalid")
            sched = bool(ev.sched) or self.p5.setting("approve_sched")
            token = L.next_token("nw")
            self.pending_window = {"token": token, "start": start, "end": end, "rev": rev}
            self.last_event = "window_sent"
            outs.append(NodeCommand(token, "window", True if sched else None, start, end, None, "approved_window"))
            if sched:
                self.p5.settings["approve_sched"] = False     # the box ticks once (spec 3.4)
            self.edit = None
            return True
        if op == "reject":
            until = self._today(L) + self.p.nw_deny_days
            self.suppressions = [s for s in self.suppressions if s["type"] != "night_window"]
            self._add_supp(L, {"type": "night_window", "until_ord": until, "night": L.night})
            self.edit = None
            return True
        if op == "later":
            self.later[ITEM_NIGHT_WINDOW] = L.last_run_ord or NEVER
            self.edit = None
            return True
        return self._ignore(outs, ITEM_NIGHT_WINDOW, op, "not_applicable")

    def window_result(self, res) -> bool:
        """A CommandResult for an approved window. True when it was ours. A confirmed one records the window that was
        written (``p5.nw_applied``), so an edited window does not bring the item back."""
        pw = self.pending_window
        if pw is None or res.token != pw["token"]:
            return False
        self.pending_window = None
        self.last_event = "window_applied" if res.ok else "window_not_confirmed"
        if res.ok:
            self.p5.nw_applied = {"rev": pw["rev"], "start": pw["start"], "end": pw["end"]}
        return True

    # ------------------------------------------------------------ the bed item
    def _answer_bed(self, L, ev, outs: list) -> bool:
        op = ev.op
        state = self.bed_state(L)[0]
        if state not in ("open", "snoozed"):
            return self._ignore(outs, ITEM_BED, op, "not_pending")
        if op == "reject":                                 # [No bed in view]
            self._add_supp(L, {"type": "bed_needs_data", "night": L.night})
            self.later[ITEM_BED] = None
            self.bed_rev += 1
            return True
        if op == "later":
            if state != "open":
                return self._ignore(outs, ITEM_BED, op, "not_pending")
            self.later[ITEM_BED] = L.last_run_ord or NEVER  # [Later]: shown again in the next daily review
            self.bed_rev += 1
            return True
        return self._ignore(outs, ITEM_BED, op, "not_applicable")   # use 'Mark spot here'

    # ------------------------------------------------------------ places (spec 3.5)
    def _place_applicable(self, L, s: dict) -> Optional[str]:
        """None when a place answer can apply, else the reason: the suggestion must be a shadow place, have no zone
        record and not be rejected by id (the P4 condition; not the setup review, a label, a snooze or the cap)."""
        if s["type"] not in PLACE_TYPES:
            return "not_applicable"
        if s["state"] != "shadow" or s["id"] in zones_mod.covered_ids(L) or self._rejected_place(s["id"]):
            return "not_pending"
        return None

    def _zone_name_kind(self, L, ev, s: dict, op: str, empty_is_error: bool) -> tuple:
        """(name, kind, from_text, reason) for a new zone from suggestion ``s``: the name of the answer, else the name
        text (only for an answer without an aid and not a leftover of a mark), else the auto name; the kind of a given
        name is bed for the whole words bed or bett, else the type's kind; a unique name (D12)."""
        name, from_text = zones_mod.requested_name(L, ev, leftover_filter=True)
        if not name and empty_is_error:
            return None, None, False, "name_needed"
        if name:
            kind = "bed" if self.kind_of(name) == "bed" else KIND_BY_TYPE[s["type"]]
        else:
            kind = s["kind"] if s["kind"] in ("bed", "dwell", "short_task") else KIND_BY_TYPE[s["type"]]
            default = "Bed" if kind == "bed" else "Place %d" % self.next_zone_n
            name = (s["name_hint"] or "").strip()[:NAME_MAX] or default
        return zones_mod.unique_name(L, name), kind, from_text, None

    def _new_zone(self, L, ev, s: dict, outs: list, now_ms: int, item: str, empty_is_error: bool) -> Optional[dict]:
        """Creates the zone for approve/rename; appends AnswerIgnored and returns None when refused."""
        op = ev.op
        if len(self.zones) >= MAX_ZONES:
            self._ignore(outs, item, op, "zone_limit")
            return None
        name, kind, from_text, reason = self._zone_name_kind(L, ev, s, op, empty_is_error)
        if reason is not None:
            self._ignore(outs, item, op, reason)
            return None
        rec = zones_mod.create_zone(L, s, name, kind, now_ms)
        if from_text:
            zones_mod.consume_text(L, outs)
        outs.append(DebugMapChanged("review"))
        return rec

    def _answer_place(self, L, ev, item: str, s: dict, outs: list, now_ms: int) -> bool:
        op = ev.op
        bad = self._place_applicable(L, s)
        if bad is not None:
            return self._ignore(outs, item, op, bad)
        if op == "reject":
            self._count(L, "place", "reject", s)
            rejections.add_rejection(L, s, L.night)
            return True
        if op == "later":
            self._count(L, "place", "later")
            self.p5.set_later(item, L.last_run_ord or NEVER)
            return True
        if op in ("approve", "rename"):
            made = self._new_zone(L, ev, s, outs, now_ms, item, empty_is_error=(op == "rename")) is not None
            if made:
                self._count(L, "place", op, s)
            return made
        return self._ignore(outs, item, op, "not_applicable")

    # ------------------------------------------------------------ P7 guards and answer counters (spec 6.2)
    def _count(self, L, kind: str, op: str, obj=None) -> None:
        if L.p7on:
            tuningagg.record_answer(L, kind, op, obj)

    @staticmethod
    def _zone_busy(L, zid: str) -> bool:
        return any(i == zid and occ and valid for i, occ, valid in L.zone_snapshot())

    def _approval_refusal(self, L, z: dict, guard: bool) -> Optional[str]:
        """The reason an approval of a merge, modify or retire is refused, or None (spec 6.2, preconditions 2 and 3): a
        bed or a frozen zone (static predicate or coverage freeze) is ``not_applicable``; with ``guard`` the busy guard
        refuses (``zone_busy``) while the zone is occupied and valid. Reject and Later are never refused."""
        if zones_mod.is_frozen(L, z["kind"], z.get("ref"), zid=z["zid"]) or z["kind"] == "bed":
            return "not_applicable"
        if guard and L.p7on and L.p7params.busy_guard and self._zone_busy(L, z["zid"]):
            return "zone_busy"
        return None

    # ------------------------------------------------------------ merge and modify items (spec 3.6)
    def _target_zone(self, L, zid: str) -> Optional[dict]:
        return zones_mod.find_record(L, zid)

    def _answer_merge(self, L, ev, item: str, prop: dict, outs: list, now_ms: int) -> bool:
        op = ev.op
        s = self._suggestion(L, prop["sid"])
        if s is None:
            return self._ignore(outs, item, op, "no_such_item")
        z = self._target_zone(L, prop["zid"])
        if op == "approve":
            if z is None:
                return self._ignore(outs, item, op, "no_such_item")
            bad = self._approval_refusal(L, z, False)
            if bad is not None:
                return self._ignore(outs, item, op, bad)       # frozen zones are skipped (OA 7.8)
            self._count(L, "merge", "approve")
            zones_mod.merge_into_zone(L, z["zid"], s)
            outs.append(DebugMapChanged("review"))
            return True
        if op == "rename":                                  # "No, it's a new place"
            made = self._new_zone(L, ev, s, outs, now_ms, item, empty_is_error=False) is not None
            if made:
                self._count(L, "merge", "rename")
            return made
        if op == "reject":
            self._count(L, "merge", "reject")
            rejections.add_rejection(L, s, L.night)
            return True
        if op == "later":
            self._count(L, "merge", "later")
            self.p5.set_later(item, L.last_run_ord or NEVER)
            return True
        return self._ignore(outs, item, op, "not_applicable")

    def _answer_modify(self, L, ev, item: str, prop: dict, outs: list, now_ms: int) -> bool:
        if prop.get("src") is not None:                     # a proposal of the P7 detector (a P5 one has no source)
            return self._answer_modify_p7(L, ev, item, prop, outs, now_ms)
        op = ev.op
        s = self._suggestion(L, prop["sid"])
        z = self._target_zone(L, prop["zid"])
        if s is None or z is None:
            return self._ignore(outs, item, op, "no_such_item")
        if op == "approve":
            if zones_mod.is_frozen(L, z["kind"], z.get("ref")) or z["kind"] == "bed":
                return self._ignore(outs, item, op, "not_applicable")
            zones_mod.update_zone(L, z["zid"], s)
            outs.append(DebugMapChanged("review"))
            return True
        if op == "rename":                                  # "No, it's a new place": the old zone keeps its record
            if self._new_zone(L, ev, s, outs, now_ms, item, empty_is_error=False) is None:
                return False
            z["ref"] = None
            z["srev"] = s["rev"]
            return True
        if op == "reject":
            z["srev"] = s["rev"]                            # asked again only when the suggestion's rev rises
            return True
        if op == "later":
            self.p5.set_later(item, L.last_run_ord or NEVER)
            return True
        return self._ignore(outs, item, op, "not_applicable")

    def _answer_modify_p7(self, L, ev, item: str, prop: dict, outs: list, now_ms: int) -> bool:
        """The modify answers by source (spec 6.2). ``cand``: the proposal's suggestion is a different pending candidate,
        the zone keeps its own ``ref``: 'No, it's a new place' only creates the new zone and Reject only rejects the
        candidate. ``own``: the zone's own suggestion moved: both behave as in P5."""
        op = ev.op
        src = prop["src"]
        s = self._suggestion(L, prop["sid"])
        z = self._target_zone(L, prop["zid"])
        if s is None or z is None:
            return self._ignore(outs, item, op, "no_such_item")
        if op == "approve":
            bad = self._approval_refusal(L, z, True)
            if bad is not None:
                return self._ignore(outs, item, op, bad)
            self._count(L, "modify", "approve", {"ratio": drift.zone_ratio(L, z["zid"])})
            zones_mod.move_zone(L, z["zid"], s, src)
            outs.append(DebugMapChanged("review"))
            return True
        e = L.zone7.entry(z["zid"], create=True)
        if op == "rename":                                  # "No, it's a new place"
            if self._new_zone(L, ev, s, outs, now_ms, item, empty_is_error=False) is None:
                return False
            if src == "own":                                # S passes to the new zone: the old one loses its ref (P5)
                z["ref"] = None
                z["srev"] = s["rev"]
            e["ms"] = ""
            self._count(L, "modify", "rename")
            return True
        if op == "reject":
            self._count(L, "modify", "reject", {"ratio": drift.zone_ratio(L, z["zid"])})
            if src == "cand":
                rejections.add_rejection(L, s, L.night)     # nothing else changes (srev means 'the ref suggestion's rev')
            else:
                z["srev"] = s["rev"]                        # asked again only when the suggestion's rev rises
            e["ms"] = ""
            return True
        if op == "later":
            self._count(L, "modify", "later")
            self.p5.set_later(item, L.last_run_ord or NEVER)
            return True
        return self._ignore(outs, item, op, "not_applicable")

    def _answer_retire(self, L, ev, item: str, prop: dict, outs: list) -> bool:
        """``ret-<zid>``: Approve removes the zone (and its entities) and leaves a ``retired`` mask, Reject is Keep for
        ``keep_days`` days, Later snoozes to the next nightly run (spec 4.5, 6.2)."""
        op = ev.op
        z = self._target_zone(L, prop["zid"])
        if z is None:
            return self._ignore(outs, item, op, "no_such_item")
        zid = z["zid"]
        if op == "approve":
            bad = self._approval_refusal(L, z, True)
            if bad is not None:
                return self._ignore(outs, item, op, bad)
            self._count(L, "retire", "approve", {"ratio": drift.zone_ratio(L, zid)})
            movedfrom.add(L, "retired", z)
            zones_mod.remove(L, zid)
            outs.append(DebugMapChanged("review"))
            return True
        if op == "reject":                                  # Keep
            e = L.zone7.entry(zid, create=True)
            self._count(L, "retire", "reject", {"ratio": drift.zone_ratio(L, zid)})
            e["kp"] = self._today(L) + L.p7params.keep_days
            e["rt"] = 0
            e["ro"] = False
            return True
        if op == "later":
            self._count(L, "retire", "later")
            L.zone7.snz["ret-" + zid] = L.last_run_ord or NEVER
            return True
        return self._ignore(outs, item, op, "not_applicable")

    def _answer_radar(self, L, ev, outs: list) -> bool:
        """``radar_moved``: Approve resets the learned maps (spec 4.7), Reject is Ignore, Later snoozes."""
        op = ev.op
        rm = L.zone7.rm
        if not rm["open"] or rm["ign"] == rm["rev"]:
            return self._ignore(outs, ITEM_RADAR_MOVED, op, "not_pending")
        if op == "approve":
            self._count(L, "radar", "approve")
            L.reset_maps()
            outs.append(DebugMapChanged("review"))
            return True
        if op == "reject":                                  # Ignore
            self._count(L, "radar", "reject")
            rm["ign"] = rm["rev"]
            rm["open"] = False
            rm["st"] = 0
            rm["d0"] = 0
            rm["qt"] = (L.last_run_ord or self._today(L)) + L.p7params.rm_quiet_days
            return True
        if op == "later":
            self._count(L, "radar", "later")
            L.zone7.snz[ITEM_RADAR_MOVED] = L.last_run_ord or NEVER
            return True
        return self._ignore(outs, ITEM_RADAR_MOVED, op, "not_applicable")

    # ------------------------------------------------------------ a zone record or a label as the answer's item
    def _answer_record(self, L, ev, item: str, outs: list) -> bool:
        """Direct targets that P4 accepts (the P4 tests and replay use them): ``rename`` of a zone record or a label
        by its id, exactly as ``adv:rename`` does. Every other op is ``not_applicable`` (removal and undo go through
        the Advanced entries)."""
        if ev.op != "rename":
            return self._ignore(outs, item, ev.op, "not_applicable")
        name, from_text = zones_mod.requested_name(L, ev, leftover_filter=False)
        if not name:
            return self._ignore(outs, item, ev.op, "name_needed")
        changed = zones_mod.rename(L, item, name)
        if from_text:
            zones_mod.consume_text(L, outs)
        if changed:
            outs.append(DebugMapChanged("review"))
        return changed or from_text

    # ------------------------------------------------------------ nightly
    def on_nightly(self, L) -> None:
        """After a nightly run: snoozes end ('the next daily review'), expired suppressions go, the P5 bookkeeping is
        tidied (spec 2.3, 5.1, 5.6)."""
        for item in ITEM_IDS:
            m = self.later.get(item)
            if m is not None and m != (L.last_run_ord or NEVER):
                self.later[item] = None
                if item == ITEM_BED:
                    self.bed_rev += 1
        today = self._today(L)
        self.suppressions = [s for s in self.suppressions if s["type"] != "night_window" or s["until_ord"] > today]
        # P5 bookkeeping
        run = L.last_run_ord or NEVER
        sids = {s["id"] for s in L.ns.suggestions}
        zids = {z["zid"] for z in self.zones}
        later = self.p5.later
        for k in [k for k, v in later.items() if v != run or not self._item_exists(L, k, sids, zids)]:
            del later[k]
        z7 = L.zone7
        if L.p7on and z7.snz:                                # P7 snoozes end at the next run, like p5.later
            for k in [k for k, v in z7.snz.items() if v != run or not self._item_exists(L, k, sids, zids)]:
                del z7.snz[k]
        rejections.fill_masks(L)
        masks = self.p5.masks
        self.suppressions = [s for s in self.suppressions if s["type"] != "place" or s["ref"] in masks]
        covered = zones_mod.covered_ids(L)
        self.p5.prune_tags(lambda k: k in sids or k in covered or k in zids)      # P7: a retire item is tagged by its zid
        L.planner.prune_sig(lambda k: self._item_exists(L, k, sids, zids))      # announced ids that no longer exist
        self.ver += 1

    def _item_exists(self, L, item: str, sids: set, zids: set) -> bool:
        """Does the item an announced signature or a snooze names still exist? P7 (spec 6.1): ``ret-<zid>`` while the
        record exists, ``radar_moved`` while the detector's item is open; without it the nightly ``prune_sig`` would drop
        the announced signature and the same unanswered item would be announced again every day."""
        if item.startswith("mrg-"):
            return item[4:] in sids
        if item.startswith("mod-") or item.startswith("ret-"):
            return item[4:] in zids
        if item == ITEM_RADAR_MOVED:
            return bool(L.zone7.rm["open"])
        return item in sids or item in ITEM_IDS

    # ------------------------------------------------------------ status
    def attributes(self, L) -> dict:
        items = self.items(L)
        bs, br = self.bed_state(L)
        frozen = L.window["sched"] is True
        bed = self.bed_zones(L)
        return {
            "ready": self.ready(L),
            "items": [i.id for i in items],
            "selected": self.selected(L, items),
            "night_window": {"state": self.nw_state(L), "rev": L.night_m.proposal_rev},
            "bed_item": {"state": bs, "reason": br, "rev": self.bed_rev},
            "bed": {"zones": len(bed), "frozen": bool(bed) and frozen},
            "zones": len(self.zones),
            "suppressions": len(self.suppressions),
            "stale_answers": self.stale_answers,
            "last_event": self.last_event,
            "tier2": sum(1 for i in items if i.tier == 2),
            "advanced": len(advanced_entries(L)),
            "more": self.more if self.ready(L) else 0,
            "command_pending": self.pending_window is not None,
        }

    # ------------------------------------------------------------ persistence
    def load(self, zones: list, supp: list, review: dict) -> None:
        """Loads validated sections; ``zones`` already carry decoded ``cells`` and the P5 keys (defaults filled)."""
        self.zones = [dict(z, also=list(z.get("also", ())), poly_cm=[list(v) for v in z.get("poly_cm", ())])
                      for z in zones]
        self.suppressions = []
        for s in supp:
            d = {"type": s["type"], "night": s["night"]}
            if s["type"] == "night_window":
                d["until_ord"] = date.fromisoformat(s["until"]).toordinal()
            if s["type"] == "place":
                d["ref"] = s["ref"]
            self.suppressions.append(d)
        self.later = {k: (date.fromisoformat(v).toordinal() if v is not None else None)
                      for k, v in review["later"].items()}
        self.bed_rev = review["bed_rev"]
        self.next_zone_n = review["next_zone_n"]
        self.ver += 1

    def export_sections(self) -> tuple:
        from .grids import rle_encode
        zones = [{"zid": z["zid"], "name": z["name"], "kind": z["kind"], "src": z["src"], "ref": z["ref"],
                  "cx": z["cx"], "cy": z["cy"], "cells": rle_encode(z["cells"]), "night": z["night"],
                  "created_s": z["created_s"], "poly_cm": [list(v) for v in z["poly_cm"]], "rev": z["rev"],
                  "upd": z["upd"], "srev": z["srev"], "also": list(z["also"])} for z in self.zones]
        supp = []
        for s in self.suppressions:
            d = {"type": s["type"], "night": s["night"]}
            if s["type"] == "night_window":
                d["until"] = date.fromordinal(s["until_ord"]).isoformat()
            if s["type"] == "place":
                d["ref"] = s["ref"]
            supp.append(d)
        review = {"later": {k: (date.fromordinal(v).isoformat() if v is not None else None)
                            for k, v in self.later.items()},
                  "bed_rev": self.bed_rev, "next_zone_n": self.next_zone_n}
        return zones, supp, review


def _bad(msg: str):
    raise StateError(msg)


def _isint(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_sections(zones, supp, review, nc: int, t_min: int, t_max: int) -> None:
    """Validates the P4 sections 'zones', 'suppressions' and 'review' of a v4 state and the P5 keys of a zone record
    (raises StateError; a key a P4 file lacks is optional, one that is present must be valid)."""
    import math
    if not isinstance(zones, list) or len(zones) > MAX_ZONES:
        _bad("bad zones")
    for z in zones:
        if not isinstance(z, dict):
            _bad("bad zone")
        for k, t in (("zid", str), ("name", str), ("kind", str), ("src", str), ("cells", list)):
            if not isinstance(z.get(k), t):
                _bad("bad zone " + k)
        if not z["zid"].startswith("z") or len(z["name"]) > 64 or z["src"] not in ("approve",):
            _bad("bad zone values")
        if z["kind"] not in ("bed", "dwell", "short_task"):
            _bad("bad zone kind")
        if "ref" not in z or not (z["ref"] is None or isinstance(z["ref"], str)):
            _bad("bad zone ref")
        for k in ("cx", "cy"):
            v = z.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not -33.0 <= v <= 33.0:
                _bad("bad zone " + k)
        if not _isint(z.get("night")) or z["night"] < 0 or not _isint(z.get("created_s")) \
                or not t_min <= z["created_s"] <= t_max:
            _bad("bad zone night or created_s")
        for k in ("rev", "upd", "srev"):
            if k in z and (not _isint(z[k]) or z[k] < 0):
                _bad("bad zone " + k)
        if "poly_cm" in z:
            pc = z["poly_cm"]
            if not isinstance(pc, list) or len(pc) > 12 or \
                    not all(isinstance(v, list) and len(v) == 2 and all(_isint(x) for x in v) for v in pc):
                _bad("bad zone poly_cm")
        if "also" in z:
            al = z["also"]
            if not isinstance(al, list) or len(al) > 8 or not all(isinstance(x, str) for x in al):
                _bad("bad zone also")
    if not isinstance(supp, list) or len(supp) > MAX_SUPPRESSIONS:
        _bad("bad suppressions")
    for s in supp:
        if not isinstance(s, dict) or s.get("type") not in SUPPRESSION_TYPES or not _isint(s.get("night")) \
                or s["night"] < 0:
            _bad("bad suppression")
        if s["type"] == "night_window":
            _iso(s.get("until"), "suppression until")
        if s["type"] == "place" and not isinstance(s.get("ref"), str):
            _bad("bad suppression ref")
    if not isinstance(review, dict):
        _bad("bad review")
    later = review.get("later")
    if not isinstance(later, dict) or set(later) != set(ITEM_IDS):
        _bad("bad review.later")
    for v in later.values():
        if v is not None:
            _iso(v, "review.later")
    if not _isint(review.get("bed_rev")) or review["bed_rev"] < 0 or not _isint(review.get("next_zone_n")) \
            or review["next_zone_n"] < 1:
        _bad("bad review counters")


def _iso(v, what: str) -> None:
    if not isinstance(v, str):
        _bad("bad date in " + what)
    try:
        date.fromisoformat(v)
    except ValueError:
        _bad("bad date in " + what)
