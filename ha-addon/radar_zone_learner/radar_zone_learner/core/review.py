"""Review items, the bed item and its answers, suppressions and confirmed zones (P4; OA 7.6, 7.8, 7.9).

Two review items exist before P5, in this order (OA 7.9: 'the night_window and bed_needs_data items first'):

- ``night_window`` (revision = ``NightModel.proposal_rev``): the proposal of 7.5. States: ``none`` (no proposal),
  ``proposed``, ``same`` (the proposal is within ``nw_same_min`` of both current edges: applied), ``snoozed``
  ([Later]) and ``suppressed`` ([Deny], for ``nw_deny_days`` days).
- ``bed_needs_data`` (revision ``bed_rev``): shown while no zone of kind ``bed`` exists. States: ``open``,
  ``snoozed`` ([Later]: until the next nightly run, the 'next daily review'), ``suppressed`` ([No bed in view],
  kept in ``suppressions``, undone by the ``undo`` op) and ``withdrawn`` (a zone of kind bed exists, or a place
  named 'bed' is suggested and not rejected).

The list of items exists once the setup review is ready (``review_min_nights`` nights, OA 7.9). The select shows the
first item until the user chooses with ``ReviewSelect`` (an item, or None = nothing: then no press is bound to an
item); 'Mark spot here' is bound to the bed item exactly when
the select shows it (OA 7.6): it then makes kind ``bed`` named 'Bed' (or the name text). Any other press keeps
the P3 name rule: kind ``manual``, except that a spot name that is the whole word bed or bett (trimmed, any
case) still gives kind ``bed``. This is a deliberate reading of OA 7.6 ('any other press creates a zone of kind
manual'): before the setup review no item exists and the P3 rule must hold (the P3 replay marks the bed that
way), and a user who typed 'Bed' says what the spot is, whichever item the select shows (the night-window item is
the first one while a proposal exists). The press does not need the bed item for that, and the bed item is
withdrawn afterwards like for any zone of kind bed.

Zones: in P4 a 'Mark spot here' label is the zone it makes (kind ``bed`` or ``manual``); approving or renaming a
place suggestion makes an entry in ``zones`` (P6 builds entities from both). A zone of kind ``bed`` is one of
either. Names are compared whole, trimmed and case-insensitive (``bed``, ``bett``).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from . import labels as labels_mod
from .night import circ_diff, window_len
from .params import Params
from .types import ITEM_BED, ITEM_IDS, ITEM_NIGHT_WINDOW, NodeCommand, ReviewChanged, ReviewItem, StateError

PLACE_TYPES = ("dwell_zone", "short_task_zone")
SUPPRESSION_TYPES = ("bed_needs_data", "night_window", "place")
KIND_BY_TYPE = {"dwell_zone": "dwell", "short_task_zone": "short_task"}
MAX_ZONES = 100
MAX_SUPPRESSIONS = 200
NAME_MAX = 32
NEVER = 1                    # date ordinal of 0001-01-01: 'no nightly run yet'


def hhmm(minute: int) -> str:
    return "%02d:%02d" % (minute // 60, minute % 60)


class ReviewModel:
    def __init__(self, p: Params, time_model) -> None:
        self.p = p
        self.tm = time_model
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
        self.pending_window: Optional[dict] = None       # {"token", "start", "end"} of an approved window
        self.last_event: Optional[str] = None
        self.stale_answers = 0

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
        for s in L.ns.suggestions:
            if s["type"] in PLACE_TYPES and s["state"] == "shadow" and not self._rejected_place(s["id"]) and \
                    (s["kind"] == "bed" or (s["name_hint"] or "").strip().casefold() in names):
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
        if circ_diff(raw["start_min"], w["start"]) <= self.p.nw_same_min and \
                circ_diff(raw["end_min"], w["end"]) <= self.p.nw_same_min:
            return "same"
        if self._snoozed(L, ITEM_NIGHT_WINDOW):
            return "snoozed"
        return "proposed"

    def ready(self, L) -> bool:
        return L.night >= self.p.review_min_nights

    # ------------------------------------------------------------ the item list
    def items(self, L) -> list:
        if not self.ready(L):
            return []
        out = []
        if self.nw_state(L) == "proposed":
            out.append(ReviewItem(ITEM_NIGHT_WINDOW, L.night_m.proposal_rev, ITEM_NIGHT_WINDOW,
                                  self.nw_data(L)))
        if self.bed_state(L)[0] == "open":
            out.append(ReviewItem(ITEM_BED, self.bed_rev, ITEM_BED, self.bed_data(L)))
        return out

    def selected(self, L, items: Optional[list] = None) -> Optional[str]:
        items = self.items(L) if items is None else items
        ids = [i.id for i in items]
        if self.select_chosen and (self.select is None or self.select in ids):
            return self.select
        return ids[0] if ids else None

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

    def rev_of(self, L, item: str) -> Optional[int]:
        if item == ITEM_NIGHT_WINDOW:
            return L.night_m.proposal_rev
        if item == ITEM_BED:
            return self.bed_rev
        for s in L.ns.suggestions:
            if s["id"] == item:
                return s["rev"]
        if any(z["zid"] == item for z in self.zones):
            return 0                                          # a zone has no revision of its own
        return None

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

    # ------------------------------------------------------------ answers
    def answer(self, L, ev, outs: list, now_ms: int) -> bool:
        """Applies a ReviewAnswer. True if something changed (the learner then requests a save)."""
        op = ev.op
        if not isinstance(op, str) or not (ev.item is None or isinstance(ev.item, str)):
            return False                                      # a malformed answer is ignored, never raised on
        if op == "forget":
            changed = bool(self.suppressions) or any(v is not None for v in self.later.values())
            self.suppressions = []
            self.later = {ITEM_NIGHT_WINDOW: None, ITEM_BED: None}
            if changed:
                self.bed_rev += 1
            return changed
        item = ev.item if ev.item is not None else self.selected(L)
        if item is None:
            return False
        if op == "undo":
            return self._undo(L, item)
        cur = self.rev_of(L, item)
        if cur is None:
            return False
        if ev.rev is not None and ev.rev != cur:
            self.stale_answers += 1
            return False
        if item == ITEM_NIGHT_WINDOW:
            return self._answer_window(L, ev, outs, now_ms)
        if item == ITEM_BED:
            return self._answer_bed(L, ev)
        if item.startswith("s"):
            return self._answer_place(L, ev, item, outs, now_ms)
        if item.startswith("z"):
            return self._answer_zone(L, ev, item)
        return False

    def _undo(self, L, item: str) -> bool:
        typ = {ITEM_BED: "bed_needs_data", ITEM_NIGHT_WINDOW: "night_window"}.get(item)
        if typ is None:
            return False
        n = len(self.suppressions)
        self.suppressions = [s for s in self.suppressions if s["type"] != typ]
        had_later = self.later.get(item) is not None
        self.later[item] = None
        if item == ITEM_BED and (n != len(self.suppressions) or had_later):
            self.bed_rev += 1
        return n != len(self.suppressions) or had_later

    def _add_supp(self, s: dict) -> None:
        self.suppressions.append(s)
        if len(self.suppressions) > MAX_SUPPRESSIONS:
            del self.suppressions[0]

    def _answer_window(self, L, ev, outs: list, now_ms: int) -> bool:
        state = self.nw_state(L)
        raw = L.night_m.raw
        if state != "proposed" or raw is None:
            return False
        if ev.op == "approve":
            token = L.next_token("nw")
            self.pending_window = {"token": token, "start": raw["start_min"], "end": raw["end_min"]}
            self.last_event = "window_sent"
            outs.append(NodeCommand(token, "window", True if ev.sched else None, raw["start_min"], raw["end_min"],
                                    None, "approved_window"))
            return True
        if ev.op == "reject":
            until = self._today(L) + self.p.nw_deny_days
            self.suppressions = [s for s in self.suppressions if s["type"] != "night_window"]
            self._add_supp({"type": "night_window", "until_ord": until, "night": L.night})
            return True
        if ev.op == "later":
            self.later[ITEM_NIGHT_WINDOW] = L.last_run_ord or NEVER
            return True
        return False

    def window_result(self, res) -> bool:
        """A CommandResult for an approved window. True when it was ours."""
        pw = self.pending_window
        if pw is None or res.token != pw["token"]:
            return False
        self.pending_window = None
        self.last_event = "window_applied" if res.ok else "window_not_confirmed"
        return True

    def _answer_bed(self, L, ev) -> bool:
        state = self.bed_state(L)[0]
        if state not in ("open", "snoozed"):
            return False
        if ev.op == "reject":                              # [No bed in view]
            self._add_supp({"type": "bed_needs_data", "night": L.night})
            self.later[ITEM_BED] = None
            self.bed_rev += 1
            return True
        if ev.op == "later" and state == "open":           # [Later]: shown again in the next daily review
            self.later[ITEM_BED] = L.last_run_ord or NEVER
            self.bed_rev += 1
            return True
        return False

    def on_nightly(self, L) -> None:
        """After a nightly run: snoozes end ('the next daily review'), expired suppressions go."""
        for item in ITEM_IDS:
            m = self.later.get(item)
            if m is not None and m != (L.last_run_ord or NEVER):
                self.later[item] = None
                if item == ITEM_BED:
                    self.bed_rev += 1
        today = self._today(L)
        self.suppressions = [s for s in self.suppressions if s["type"] != "night_window" or s["until_ord"] > today]

    # ------------------------------------------------------------ places and zones
    def _place_zone(self, sid: str) -> Optional[dict]:
        return next((z for z in self.zones if z["ref"] == sid), None)

    def _answer_place(self, L, ev, sid: str, outs: list, now_ms: int) -> bool:
        s = next((x for x in L.ns.suggestions if x["id"] == sid), None)
        if s is None or s["type"] not in PLACE_TYPES:
            return False
        if ev.op == "reject":
            if self._rejected_place(sid):
                return False
            self._add_supp({"type": "place", "ref": sid, "night": L.night})
            return True
        if ev.op not in ("approve", "rename"):
            return False
        name = L.spot_name.strip()[:NAME_MAX]
        z = self._place_zone(sid)
        if z is not None:                                  # already a zone: only a rename can change it
            return self._rename(L, z, name) if ev.op == "rename" and name else False
        if s["state"] != "shadow" or self._rejected_place(sid):
            return False
        if name:
            kind = "bed" if self.kind_of(name) == "bed" else KIND_BY_TYPE[s["type"]]
        else:
            kind = s["kind"] if s["kind"] in ("bed", "dwell", "short_task") else KIND_BY_TYPE[s["type"]]
            default = "Bed" if kind == "bed" else "Place %d" % self.next_zone_n
            name = (s["name_hint"] or "").strip()[:NAME_MAX] or default
        if len(self.zones) >= MAX_ZONES:
            return False
        zid = "z%04d" % self.next_zone_n
        self.next_zone_n += 1
        tr = self.p.time_round_s
        self.zones.append({"zid": zid, "name": name, "kind": kind, "src": "approve", "ref": sid,
                           "cx": s["cx"], "cy": s["cy"], "cells": frozenset(s["cells"]), "night": L.night,
                           "created_s": int(now_ms // 1000 // tr) * tr})
        if kind == "bed":
            s["kind"] = "bed"
            s["name_hint"] = name
        return True

    def _answer_zone(self, L, ev, zid: str) -> bool:
        z = next((x for x in self.zones if x["zid"] == zid), None)
        if z is None or ev.op != "rename":
            return False
        name = L.spot_name.strip()[:NAME_MAX]
        return self._rename(L, z, name) if name else False

    def _rename(self, L, z: dict, name: str) -> bool:
        if name == z["name"] and (self.kind_of(name) != "bed" or z["kind"] == "bed"):
            return False
        z["name"] = name
        if self.kind_of(name) == "bed":
            z["kind"] = "bed"
            s = next((x for x in L.ns.suggestions if x["id"] == z["ref"]), None)
            if s is not None:
                s["kind"] = "bed"
                s["name_hint"] = name
        return True

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
        }

    # ------------------------------------------------------------ persistence
    def load(self, zones: list, supp: list, review: dict) -> None:
        """Loads validated sections; ``zones`` already carry decoded ``cells``."""
        self.zones = [dict(z) for z in zones]
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

    def export_sections(self) -> tuple:
        from .grids import rle_encode
        zones = [{"zid": z["zid"], "name": z["name"], "kind": z["kind"], "src": z["src"], "ref": z["ref"],
                  "cx": z["cx"], "cy": z["cy"], "cells": rle_encode(z["cells"]), "night": z["night"],
                  "created_s": z["created_s"]} for z in self.zones]
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
    """Validates the P4 sections 'zones', 'suppressions' and 'review' of a v4 state (raises StateError)."""
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
        if not (z["ref"] is None or isinstance(z["ref"], str)):
            _bad("bad zone ref")
        for k in ("cx", "cy"):
            v = z.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not -33.0 <= v <= 33.0:
                _bad("bad zone " + k)
        if not _isint(z.get("night")) or z["night"] < 0 or not _isint(z.get("created_s")) \
                or not t_min <= z["created_s"] <= t_max:
            _bad("bad zone night or created_s")
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
