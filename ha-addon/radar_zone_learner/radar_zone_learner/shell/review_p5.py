"""Texts of the P5 review (spec 2.7, 2.10, 2.11, 6.3, 8.6). Pure functions; the shell renders, the core decides.

- the tier-1 texts (night window, bed zone) stay ``shell/review.py:item_text``; this module delegates to it;
- the tier-2 texts (place, merge, modify) are built here from the flat ``ReviewItem.data`` (spec 2.4), with the typical
  hours of a place only when ``detail`` (the switch 'Detailed review text') is on, so that no hour range reaches any
  retained topic or the recorder by default (spec 2.11, 7.5);
- ``push_state`` builds the eight-field state of the push request sensor (spec 6.3);
- ``you_are_in_text`` and ``advanced_label`` are the texts of the 'You are in' sensor and the Advanced select.

Nothing here reads a clock or a file, and no coordinate or stay time is ever put into a text: a place is described by
its centre rounded to 0.1 m, which the status attributes already carry (README section 6).
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from .p4_types import ITEM_BED, ITEM_NIGHT_WINDOW
from .review import DEFAULT_END_MIN, DEFAULT_START_MIN, STATE_MAX_CHARS, item_text, state_text

__all__ = ["item_text_p5", "item_generic_text", "kind_phrase", "position_phrase", "hours_phrase", "push_state",
           "you_are_in_text", "you_are_in_attr", "advanced_label", "pending_json", "PENDING_ATTR_MAX_BYTES",
           "NAME_LABEL_MAX", "LIVE_OFF_TEXT", "MARKED_SPOT_TEXT", "ADVANCED_LABEL_MAX", "state_text"]

NAME_LABEL_MAX = 24                  # a zone or label name in an Advanced label (spec 2.10)
ADVANCED_LABEL_MAX = 44              # a whole Advanced label (the spec sizes 40; the longest verb text is 42)
PENDING_ATTR_MAX_BYTES = 8000        # attribute JSON of 'Review pending' (spec 8.6)
LIVE_OFF_TEXT = "Live view off"
MARKED_SPOT_TEXT = "Marked spot"
_KIND_PHRASE = {"dwell": "a place where you stay", "short_task": "a short-task spot", "bed": "a bed"}


# ---------------------------------------------------------------- small helpers
def _data(item) -> dict:
    data = getattr(item, "data", None)
    return data if isinstance(data, dict) else {}


def _str(v, default: str = "") -> str:
    return v if isinstance(v, str) else default


def _int(v, default: int = 0) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else default


def _num(v, default: float = 0.0) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return float(v)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"


def _ranges(v) -> tuple:
    if not isinstance(v, (tuple, list)):
        return ()
    return tuple(r for r in v if isinstance(r, str) and r)


# ---------------------------------------------------------------- item texts (spec 2.11)
def kind_phrase(kind: str) -> str:
    return _KIND_PHRASE.get(kind, "a place")


def position_phrase(x_cm: int, y_cm: int) -> str:
    """'2.5 m to the right, 2.2 m from the radar'; 'straight ahead' when the centre is within 5 cm of the axis."""
    if abs(x_cm) < 5:
        side = "straight ahead"
    elif x_cm > 0:
        side = "%.1f m to the right" % (x_cm / 100.0)
    else:
        side = "%.1f m to the left" % (abs(x_cm) / 100.0)
    return "%s, %.1f m from the radar" % (side, y_cm / 100.0)


def _join_hours(ranges: tuple) -> str:
    parts = ["%s h" % r for r in ranges]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def hours_phrase(hrs_wd, hrs_we) -> str:
    """'Used mostly 09-12 h and 13-17 h.' (the typical hours of a place), '' when there is nothing to say."""
    wd, we = _ranges(hrs_wd), _ranges(hrs_we)
    if not wd and not we:
        return ""
    if not wd:
        return "Used mostly %s at weekends." % _join_hours(we)
    if not we or we == wd:
        return "Used mostly %s." % _join_hours(wd)
    return "Used mostly %s on weekdays; at weekends %s." % (_join_hours(wd), _join_hours(we))


def _place_text(data: dict, detail: bool) -> str:
    text = "New place %s: %s, %s. Look at the map, then Approve, Rename or Reject." % (
        _str(data.get("tag"), "?"), kind_phrase(_str(data.get("kind"))),
        position_phrase(_int(data.get("x_cm")), _int(data.get("y_cm"))))
    if detail:
        hours = hours_phrase(data.get("hrs_wd"), data.get("hrs_we"))
        if hours:
            text += " " + hours
    return text


def _merge_text(data: dict) -> str:
    return ("Extend %s? Place %s touches it (%d cm %s) and is used at similar hours. Approve to extend it, Rename "
            "to keep it as a new place, Reject to ignore it." % (
                _str(data.get("zname"), "the zone"), _str(data.get("tag"), "?"), _int(data.get("ext_cm")),
                _str(data.get("dir"))))


def _modify_text(data: dict) -> str:
    return ("Has %s moved? Its place changed by about %d cm %s. Approve to update it, Rename to keep the old one "
            "and add the new place, Reject to leave it." % (
                _str(data.get("zname"), "the zone"), _int(data.get("shift_cm")), _str(data.get("dir"))))


def item_text_p5(item, window: Optional[tuple], detail: bool) -> str:
    """The text of any review item. ``window`` is ``(sched, start_min, end_min)`` as the shell last saw the node's
    night window (used by the bed item); ``detail`` is the persisted switch 'Detailed review text'."""
    if item.id in (ITEM_NIGHT_WINDOW, ITEM_BED):
        sched, start, end = window if window is not None else (None, DEFAULT_START_MIN, DEFAULT_END_MIN)
        return item_text(item, sched, start, end)
    data = _data(item)
    typ = getattr(item, "type", "")
    if typ == "merge_into":
        return _merge_text(data)
    if typ == "modify_zone":
        return _modify_text(data)
    if typ in ("dwell_zone", "short_task_zone"):
        return _place_text(data, detail)
    return item.id


def item_generic_text(item, window: Optional[tuple] = None) -> str:
    """The text without any hour range (``detail`` off): what a default installation publishes."""
    return item_text_p5(item, window, False)


# ---------------------------------------------------------------- Review pending attributes (spec 8.6)
def pending_json(selected: Optional[str], rows: list, more: int) -> str:
    """JSON of ``radar_zones/review/pending/attr``: ``{"selected", "items": [{id, rev, type, tier, label, text}], "more"}``.

    ``rows`` are dicts with those keys. At most 15 rows; the texts are cut to 120 and then to 60 characters when the
    JSON would exceed 8,000 bytes (Home Assistant drops all attributes of a state above 16 KiB)."""
    rows = rows[:15]
    for limit in (None, 120, 60, 20):
        out = []
        for r in rows:
            row = dict(r)
            if limit is not None:
                row["text"] = _cut(_str(row.get("text")), limit)
            out.append({"id": row["id"], "rev": row["rev"], "type": row["type"], "tier": row["tier"],
                        "label": row["label"], "text": row["text"]})
        payload = json.dumps({"selected": selected, "items": out, "more": more}, separators=(",", ":"),
                             ensure_ascii=True)
        if len(payload.encode("ascii")) <= PENDING_ATTR_MAX_BYTES:
            return payload
    return payload


# ---------------------------------------------------------------- push request state (spec 6.3)
def push_state(req, head_text: Optional[str], detail: bool) -> str:
    """``<seq>|<acked>|<kind>|<count>|<rev>|<item>|<acks>|<text>``: eight fields, at most 255 characters.

    ``text`` is empty unless ``detail`` (the switch 'Detailed review text') is on; every pipe in it becomes ``/`` and it
    is cut so that the whole state fits (it is last, so a long text cannot shift another field)."""
    rev = "" if req.head_rev is None else str(int(req.head_rev))
    prefix = "%d|%d|%s|%d|%s|%s|%s|" % (int(req.seq), int(req.acked), req.kind, int(req.count), rev,
                                        req.head_id or "", ",".join(req.acks))
    text = ""
    if detail and head_text:
        text = " ".join(head_text.replace("|", "/").split())
        room = STATE_MAX_CHARS - len(prefix)
        text = _cut(text, room) if room > 0 else ""
    return prefix + text


# ---------------------------------------------------------------- You are in (spec 2.7)
def you_are_in_text(live: bool, where, label_of: Callable[[str], Optional[str]]) -> str:
    """The state of 'You are in'. ``label_of(item_id)`` gives the text for an open place, merge or modify item
    (``Place <tag>``) and the name of a zone or label; ``None`` when it is not known."""
    if not live:
        return LIVE_OFF_TEXT
    if not where.observing:
        return "Radar not observing"
    if where.people <= 0:
        return "No one in view"
    if where.people >= 2:
        return "Several people in view"
    if where.item is None:
        return "In no known place"
    text = label_of(where.item)
    if where.item_kind == "label":
        return text or MARKED_SPOT_TEXT
    return text or "In no known place"


def you_are_in_attr(people: int, stay_ready: bool) -> str:
    """Attributes of 'You are in': counts only, no coordinates."""
    return json.dumps({"people": int(people), "stay_ready": bool(stay_ready)}, separators=(",", ":"))


# ---------------------------------------------------------------- Advanced labels (spec 2.10)
def advanced_label(entry, tag_of: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """The select label of one Advanced entry; names are cut at 24 characters. A repeated label gets its
    ``(2)``, ``(3)`` from the model, which sees the whole list."""
    name = _cut(entry.name, NAME_LABEL_MAX) if entry.name else ""
    tag = entry.tag or ((tag_of(entry.target) if tag_of is not None else None) or "")
    kind, verb = entry.kind, entry.verb
    if verb == "undo" and kind == "bed":
        label = "Undo: No bed in view"
    elif verb == "undo" and kind == "night_window":
        label = "Undo: Night window denied until %s" % (entry.until or "?")
    elif verb == "undo":
        label = "Undo: Place %s rejected" % (tag or "?")
    elif verb == "real":
        label = "Real place: Reflection %s" % (tag or "?")
    elif verb in ("rename", "remove"):
        what = "zone" if kind == "zone" else "mark"
        label = "%s %s: %s" % ("Rename" if verb == "rename" else "Remove", what, name or MARKED_SPOT_TEXT)
    else:
        label = entry.id
    return _cut(label, ADVANCED_LABEL_MAX)
