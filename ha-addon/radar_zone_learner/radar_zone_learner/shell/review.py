"""Text of the two P4 review items (design 7.6, 7.8). Pure functions; the shell renders, the core decides.

The night window item from the core may carry a ready-made ``text`` in its data, which wins; otherwise the text
is built from the item's data. The bed item is rendered from the node's night window as the shell last saw it.
"""
from __future__ import annotations

from typing import Optional

from .p4_types import ITEM_BED, ITEM_NIGHT_WINDOW

DEFAULT_START_MIN = 0           # 00:00-06:00 (decisions): shown until the node reports its own times
DEFAULT_END_MIN = 360
STATE_MAX_CHARS = 255           # a Home Assistant state is cut at 255 characters


def hhmm(minute: Optional[int]) -> str:
    if not isinstance(minute, int) or isinstance(minute, bool):
        return "?"
    return "%02d:%02d" % ((minute // 60) % 24, minute % 60)


def _int(v) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def bed_text(sched: Optional[bool], start_min: Optional[int], end_min: Optional[int]) -> str:
    """OA 7.6: the 'before {start} or after {end}' part is left out while the schedule is OFF (or unknown)."""
    when = ""
    if sched is True:
        s = start_min if start_min is not None else DEFAULT_START_MIN
        e = end_min if end_min is not None else DEFAULT_END_MIN
        when = ", before %s or after %s" % (hhmm(s), hhmm(e))
    return ("Bed zone: lie in bed and press Mark spot here once%s (recommended; no extra radar-on nights). "
            "Otherwise the learner finds it by itself, which can take 2 weeks or more while the night "
            "schedule runs." % when)


def night_window_text(data: dict) -> str:
    """OA 7.8: 'Switch both radars off between 23:30 and 06:45. Radar on-time -30%. No radar detection ...'"""
    start, end = _int(data.get("start_min")), _int(data.get("end_min"))
    parts = ["Switch both radars off between %s and %s." % (hhmm(start), hhmm(end))]
    saving = data.get("saving_pct")
    if isinstance(saving, (int, float)) and not isinstance(saving, bool):
        parts.append("Radar on-time −%d%%." % round(saving))
    parts.append("No radar detection during that time.")
    late = _int(data.get("late_nights"))
    if late:
        parts.append("Activity was still seen in the 30 min before your current start on %d of the last %d nights."
                     % (late, _int(data.get("late_of")) or 14))
    if data.get("from_lights_off"):
        parts.append("From lights-off times.")
    return " ".join(parts)


def item_text(item, sched: Optional[bool], start_min: Optional[int], end_min: Optional[int]) -> str:
    """The bed item is always rendered here, from the node's window as the shell sees it now: the core's item
    carries the window of the moment it was made and is not re-sent when only the schedule changes. The night
    window text is the core's ready-made ``text`` when it has one."""
    if item.id == ITEM_BED:
        return bed_text(sched, start_min, end_min)
    data = item.data if isinstance(item.data, dict) else {}
    ready = data.get("text")
    if isinstance(ready, str) and ready:
        return ready
    if item.id == ITEM_NIGHT_WINDOW:
        return night_window_text(data)
    return item.id


def state_text(text: str) -> str:
    return text if len(text) <= STATE_MAX_CHARS else text[:STATE_MAX_CHARS - 1] + "…"
