"""The two selects of the review device (P5 spec 2.6): 'Review item' and 'Advanced item'. Pure, no I/O.

``SelectModel`` turns the core's item list into the option list Home Assistant shows and back: a label is what the user
picks, an item id is what the core knows.

- **dynamic mode** (``SELECT_MODE``, the default): ``No item``, ``Night window``, ``Bed zone`` and one option per open
  tier-2 item, ``Place <tag>``, ``Extend <tag>`` or ``Moved <tag>``. The tags are the core's persistent tags (spec 2.3),
  so a label means the same place for as long as the item exists. The option list changes with the items, so the
  select's discovery config has to be published again whenever ``update`` returns True (the layer does that);
- **slot mode** (the fallback of spec 2.6 if Home Assistant does not apply a changed option list in place; the owner
  switches it with the one constant): the options never change, ``No item``, ``Night window``, ``Bed zone`` and
  ``Place 1`` .. ``Place 12``, slot ``k`` being the k-th tier-2 item in list order. The meaning of a slot label can change
  when the list shifts, so the state is published at every change and the user confirms with the map.

The first option is always ``No item``: Home Assistant turns a select state payload ``None`` into ``unknown`` (spec D8),
so the P4 option ``None`` could never be shown. ``resolve`` still accepts ``None``, ``none`` and the empty string.

``AdvancedModel`` is the same over the Advanced entries (spec 2.10): ``No entry`` plus one label per entry, at most 61.
The selected entry lives here, in RAM; it is acted on only by the button 'Apply advanced'.
"""
from __future__ import annotations

from typing import Callable, Optional

from .p4_types import ITEM_BED, ITEM_NIGHT_WINDOW
from .review_p5 import advanced_label

SELECT_MODE = "dynamic"                  # "dynamic" | "slots" (P5 spec 2.6): one constant switches the fallback
SELECT_RESEND_S = 2.0                    # the select state again this long after an option change (spec 2.6 step 7)
NO_ITEM = "No item"
NO_ENTRY = "No entry"
LABEL_NIGHT_WINDOW = "Night window"
LABEL_BED = "Bed zone"
MAX_TIER2 = 12                           # tier-2 items shown at once (P5Params.review_max_places)
MAX_ADVANCED = 60                        # Advanced entries (P5Params.advanced_max)
STATIC_LABELS = {ITEM_NIGHT_WINDOW: LABEL_NIGHT_WINDOW, ITEM_BED: LABEL_BED}
_TYPE_PREFIX = {"merge_into": "Extend", "modify_zone": "Moved"}      # every other tier-2 type is a place
_NONE_TEXTS = ("", "none", NO_ITEM.casefold())


def _tag(item) -> str:
    data = getattr(item, "data", None)
    tag = data.get("tag") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) and tag else item.id


def _unique(label: str, used: set) -> str:
    out, n = label, 1
    while out in used:
        n += 1
        out = "%s (%d)" % (label, n)
    used.add(out)
    return out


def slot_label(k: int) -> str:
    return "Place %d" % k


class SelectModel:
    """Label <-> item id for the 'Review item' select."""

    def __init__(self, mode: str = SELECT_MODE, max_tier2: int = MAX_TIER2) -> None:
        if mode not in ("dynamic", "slots"):
            raise ValueError("mode must be 'dynamic' or 'slots'")
        self.mode = mode
        self.max_tier2 = max_tier2
        self._entries: list = []         # (item id, label) in list order
        self._tier2: list = []           # the shown tier-2 item ids, in list order (slot k = _tier2[k - 1])
        self._selected: Optional[str] = None
        self._options: list = self._make_options()

    # ------------------------------------------------------------ building
    def update(self, items, selected: Optional[str]) -> bool:
        """Takes the core's items (``ReviewItem``, tier 1 then tier 2) and the selected id. True when the option list
        changed (never in slot mode)."""
        entries: list = []
        tier2: list = []
        used: set = set()
        for it in items:
            if it.id in STATIC_LABELS:
                entries.append((it.id, STATIC_LABELS[it.id]))
                used.add(STATIC_LABELS[it.id])
                continue
            if len(tier2) >= self.max_tier2:
                continue
            tier2.append(it.id)
            if self.mode == "slots":
                label = slot_label(len(tier2))
            else:
                label = _unique("%s %s" % (_TYPE_PREFIX.get(getattr(it, "type", ""), "Place"), _tag(it)), used)
            entries.append((it.id, label))
        self._entries, self._tier2 = entries, tier2
        ids = {i for i, _ in entries}
        self._selected = selected if selected in ids else None
        options = self._make_options()
        changed = options != self._options
        self._options = options
        return changed

    def _make_options(self) -> list:
        if self.mode == "slots":
            return [NO_ITEM, LABEL_NIGHT_WINDOW, LABEL_BED] + [slot_label(k) for k in range(1, self.max_tier2 + 1)]
        return [NO_ITEM] + [label for _, label in getattr(self, "_entries", [])]

    # ------------------------------------------------------------ reading
    @property
    def options(self) -> list:
        return list(self._options)

    @property
    def selected(self) -> Optional[str]:
        return self._selected

    def state(self) -> str:
        """The label of the selected item, else ``No item``; never the payload ``None``."""
        return self.label_of(self._selected) if self._selected else NO_ITEM

    def label_of(self, item_id: Optional[str]) -> str:
        """The select label of an open item (its slot label in slot mode), ``""`` when it is not in the list."""
        for i, label in self._entries:
            if i == item_id:
                return label
        return ""

    def place_label(self, item_id: str) -> str:
        """``Place <tag>`` for a place, merge or modify item (the 'You are in' text); the slot label in slot mode."""
        label = self.label_of(item_id)
        if self.mode == "slots" or not label:
            return label
        prefix = label.split(" ", 1)[0]
        return "Place " + label.split(" ", 1)[1] if prefix in _TYPE_PREFIX.values() else label

    def slot_labels(self) -> list:
        return [slot_label(k) for k in range(1, self.max_tier2 + 1)]

    def resolve(self, text: str) -> Optional[tuple]:
        """``(True, item id or None)`` for a label the select can send, ``None`` for an unknown one (an option that was
        removed meanwhile: the layer republishes the config)."""
        t = text.strip()
        cf = t.casefold()
        if cf in _NONE_TEXTS:
            return True, None
        for item_id, label in STATIC_LABELS.items():
            if cf in (label.casefold(), item_id):
                return True, item_id
        for item_id, label in self._entries:
            if cf == label.casefold():
                return True, item_id
        return None


class AdvancedModel:
    """Label <-> entry id for the 'Advanced item' select; the selection is kept here."""

    def __init__(self, max_entries: int = MAX_ADVANCED) -> None:
        self.max_entries = max_entries
        self._entries: list = []         # (entry id, label)
        self._by_id: dict = {}
        self._selected: Optional[str] = None
        self._options: list = [NO_ENTRY]

    def update(self, entries, tag_of: Optional[Callable[[str], Optional[str]]] = None) -> bool:
        """Takes the core's ``AdvancedEntry`` tuple. True when the option list changed. A selected entry that is gone
        is deselected."""
        used: set = set()
        rows: list = []
        by_id: dict = {}
        for e in list(entries)[:self.max_entries]:
            rows.append((e.id, _unique(advanced_label(e, tag_of), used)))
            by_id[e.id] = e
        self._entries, self._by_id = rows, by_id
        if self._selected not in by_id:
            self._selected = None
        options = [NO_ENTRY] + [label for _, label in rows]
        changed = options != self._options
        self._options = options
        return changed

    @property
    def options(self) -> list:
        return list(self._options)

    @property
    def selected(self) -> Optional[str]:
        return self._selected

    def entry(self, entry_id: Optional[str]):
        return self._by_id.get(entry_id) if entry_id else None

    def ids(self) -> set:
        return set(self._by_id)

    def label_of(self, entry_id: Optional[str]) -> str:
        for i, label in self._entries:
            if i == entry_id:
                return label
        return ""

    def state(self) -> str:
        return self.label_of(self._selected) if self._selected else NO_ENTRY

    def resolve(self, text: str) -> Optional[tuple]:
        t = text.strip()
        cf = t.casefold()
        if cf in ("", "none", NO_ENTRY.casefold()):
            return True, None
        for entry_id, label in self._entries:
            if cf == label.casefold():
                return True, entry_id
        return None

    def select(self, entry_id: Optional[str]) -> None:
        self._selected = entry_id if entry_id in self._by_id else None
