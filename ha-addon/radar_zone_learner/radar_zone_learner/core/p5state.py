"""The ``p5`` state section: container, defaults, export rule, validation (P5 spec 9, A.10). Owner: CORE.

``P5State`` holds what P5 adds to the persisted state without a schema bump (D2): the snoozed tier-2 items, the
persistent tags, the permanent 'real place' markers, the rejection masks, the applied night-window marker, the push
planner's state and the four settings. In RAM dates are date ordinals and cell sets are frozensets; the JSON form is the
one of spec 9.2, in that key order (so a state that has the section round-trips byte for byte). The section is exported
only when something differs from the defaults (a fresh learner exports exactly what P4 exported).
"""
from __future__ import annotations

import copy
import math
import re
from datetime import date

from .grids import rle_decode, rle_encode
from .types import ITEM_ID_RE, PUSH_KINDS, PUSH_TARGET_RE, SETTING_NAMES, StateError

LATER_MAX = 400
TAGS_MAX = 400
REAL_MAX = 50
MASKS_MAX = 200
SIG_MAX = 40
ACKS_MAX = 8

_ITEM_ID = re.compile(ITEM_ID_RE)
_TAG = re.compile(r"^[A-Z]{1,3}$")
_TARGET = re.compile(PUSH_TARGET_RE)
_SID = re.compile(r"^s[0-9]{4,}-[0-9]+$")


def default_push() -> dict:
    return {"seq": 0, "acked": 0, "kind": "none", "setup_sent": False, "last_day": None, "sig": [], "acks": [],
            "head_id": None, "head_rev": None}


def default_settings() -> dict:
    return {name: False for name in SETTING_NAMES}


def tag_text(n: int) -> str:
    """The tag of the n-th allocation: A..Z, AA, AB, ... (at most three letters; the counter wraps after 18278)."""
    n = n % 18278
    out = ""
    k = n + 1
    while k > 0:
        k, r = divmod(k - 1, 26)
        out = chr(65 + r) + out
    return out


def _bad(msg: str):
    raise StateError(msg)


def _isint(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num33(v, what: str) -> None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not -33.0 <= v <= 33.0:
        _bad("bad number in " + what)


def _iso_ord(v, what: str) -> int:
    if not isinstance(v, str):
        _bad("bad date in " + what)
    try:
        return date.fromisoformat(v).toordinal()
    except ValueError:
        _bad("bad date in " + what)


def _cells(runs, nc: int, what: str) -> frozenset:
    if not isinstance(runs, list):
        _bad("bad cells in " + what)
    for r in runs:
        if not (isinstance(r, list) and len(r) == 2 and _isint(r[0]) and _isint(r[1]) and r[0] >= 0 and r[1] >= 1
                and r[0] + r[1] <= nc):
            _bad("bad cells in " + what)
    return rle_decode(runs)


def _parse(d, nc: int) -> dict:
    """Validates a ``p5`` section and returns its parts in RAM form. Raises StateError (never another exception)."""
    if not isinstance(d, dict):
        _bad("p5 is not an object")
    out = {"later": {}, "tags": {}, "tag_n": 0, "real": [], "masks": {}, "nw_applied": None, "push": default_push(),
           "settings": default_settings(), "extra": {}}
    known = ("later", "tags", "real", "masks", "nw_applied", "push", "settings")
    for k, v in d.items():
        if k not in known:
            out["extra"][k] = copy.deepcopy(v)

    later = d.get("later", {})
    if not isinstance(later, dict) or len(later) > LATER_MAX:
        _bad("bad p5.later")
    for k, v in later.items():
        if not isinstance(k, str) or not _ITEM_ID.match(k):
            _bad("bad p5.later key")
        out["later"][k] = _iso_ord(v, "p5.later")

    tags = d.get("tags", {"by_id": {}, "n": 0})
    if not isinstance(tags, dict):
        _bad("bad p5.tags")
    by_id = tags.get("by_id", {})
    n = tags.get("n", 0)
    if not isinstance(by_id, dict) or len(by_id) > TAGS_MAX or not _isint(n) or n < 0:
        _bad("bad p5.tags")
    seen = set()
    for k, t in by_id.items():
        if not isinstance(k, str) or not isinstance(t, str) or not _TAG.match(t) or t in seen:
            _bad("bad p5.tags entry")
        seen.add(t)
    if n < len(by_id):
        _bad("bad p5.tags.n")
    out["tags"] = dict(by_id)
    out["tag_n"] = n

    real = d.get("real", [])
    if not isinstance(real, list) or len(real) > REAL_MAX:
        _bad("bad p5.real")
    for r in real:
        if not isinstance(r, dict) or not isinstance(r.get("ref"), str):
            _bad("bad p5.real entry")
        _num33(r.get("cx"), "p5.real")
        _num33(r.get("cy"), "p5.real")
        if not _isint(r.get("night")) or r["night"] < 0:
            _bad("bad p5.real night")
        out["real"].append({"ref": r["ref"], "cx": r["cx"], "cy": r["cy"],
                            "cells": _cells(r.get("cells"), nc, "p5.real"), "night": r["night"]})

    masks = d.get("masks", {})
    if not isinstance(masks, dict) or len(masks) > MASKS_MAX:
        _bad("bad p5.masks")
    for k, m in masks.items():
        if not isinstance(k, str) or not _SID.match(k) or not isinstance(m, dict):
            _bad("bad p5.masks entry")
        _num33(m.get("cx"), "p5.masks")
        _num33(m.get("cy"), "p5.masks")
        if not _isint(m.get("n0")) or m["n0"] < 0:
            _bad("bad p5.masks n0")
        out["masks"][k] = {"cx": m["cx"], "cy": m["cy"], "cells": _cells(m.get("cells"), nc, "p5.masks"),
                           "n0": m["n0"]}

    nwa = d.get("nw_applied")
    if nwa is not None:
        if not isinstance(nwa, dict) or not _isint(nwa.get("rev")) or nwa["rev"] < 0:
            _bad("bad p5.nw_applied")
        for k in ("start", "end"):
            if not _isint(nwa.get(k)) or not 0 <= nwa[k] <= 1439:
                _bad("bad p5.nw_applied")
        out["nw_applied"] = {"rev": nwa["rev"], "start": nwa["start"], "end": nwa["end"]}

    push = d.get("push", None)
    if push is not None:
        if not isinstance(push, dict):
            _bad("bad p5.push")
        ps = default_push()
        for k in ("seq", "acked"):
            v = push.get(k, 0)
            if not _isint(v) or v < 0:
                _bad("bad p5.push " + k)
            ps[k] = v
        if ps["acked"] > ps["seq"]:
            _bad("bad p5.push acked")
        kind = push.get("kind", "none")
        if kind not in PUSH_KINDS:
            _bad("bad p5.push kind")
        ps["kind"] = kind
        sent = push.get("setup_sent", False)
        if not isinstance(sent, bool):
            _bad("bad p5.push setup_sent")
        ps["setup_sent"] = sent
        ld = push.get("last_day")
        if ld is not None:
            _iso_ord(ld, "p5.push.last_day")
        ps["last_day"] = ld
        sig = push.get("sig", [])
        if not isinstance(sig, list) or len(sig) > SIG_MAX or \
                not all(isinstance(x, str) and len(x) <= 40 for x in sig):
            _bad("bad p5.push sig")
        ps["sig"] = list(sig)
        acks = push.get("acks", [])
        if not isinstance(acks, list) or len(acks) > ACKS_MAX or \
                not all(isinstance(x, str) and _TARGET.match(x) for x in acks):
            _bad("bad p5.push acks")
        ps["acks"] = list(acks)
        hid = push.get("head_id")
        hrev = push.get("head_rev")
        if not (hid is None or isinstance(hid, str)) or not (hrev is None or (_isint(hrev) and hrev >= 0)) \
                or (hid is None) != (hrev is None):
            _bad("bad p5.push head")
        ps["head_id"] = hid
        ps["head_rev"] = hrev
        out["push"] = ps

    st = d.get("settings", None)
    if st is not None:
        if not isinstance(st, dict):
            _bad("bad p5.settings")
        for k, v in st.items():
            if k in SETTING_NAMES:
                if not isinstance(v, bool):
                    _bad("bad p5.settings " + k)
                out["settings"][k] = v
            # an unknown setting is dropped on the next save: settings are written from the four known names
    return out


class P5State:
    """The ``p5`` section in RAM (spec 9.2)."""

    def __init__(self, later_max: int = LATER_MAX, tags_max: int = TAGS_MAX) -> None:
        self.later_max = later_max        # P5Params.later_max / tags_max (the caps of spec 9.3)
        self.tags_max = tags_max
        self.later: dict = {}             # item id -> date ordinal of the nightly run at the [Later] press
        self.tags: dict = {}              # suggestion id -> tag ('A', 'B', ...)
        self.tag_n = 0                    # next tag number, never decreases (reset only by Forget all)
        self.real: list = []              # {"ref", "cx", "cy", "cells" (frozenset), "night"}
        self.masks: dict = {}             # rejected suggestion id -> {"cx", "cy", "cells" (frozenset), "n0"}
        self.nw_applied = None            # {"rev", "start", "end"} of the last window the node confirmed
        self.push: dict = default_push()
        self.settings: dict = default_settings()
        self.extra: dict = {}             # unknown keys of a newer writer: kept and written back

    # ------------------------------------------------------------ defaults and the export rule
    def is_default(self) -> bool:
        return (not self.later and not self.tags and self.tag_n == 0 and not self.real and not self.masks
                and self.nw_applied is None and self.push == default_push()
                and self.settings == default_settings() and not self.extra)

    def export(self) -> dict:
        out = {
            "later": {k: date.fromordinal(v).isoformat() for k, v in self.later.items()},
            "tags": {"by_id": dict(self.tags), "n": self.tag_n},
            "real": [{"ref": r["ref"], "cx": r["cx"], "cy": r["cy"], "cells": rle_encode(r["cells"]),
                      "night": r["night"]} for r in self.real],
            "masks": {k: {"cx": m["cx"], "cy": m["cy"], "cells": rle_encode(m["cells"]), "n0": m["n0"]}
                      for k, m in self.masks.items()},
            "nw_applied": dict(self.nw_applied) if self.nw_applied is not None else None,
            "push": {"seq": self.push["seq"], "acked": self.push["acked"], "kind": self.push["kind"],
                     "setup_sent": self.push["setup_sent"], "last_day": self.push["last_day"],
                     "sig": list(self.push["sig"]), "acks": list(self.push["acks"]),
                     "head_id": self.push["head_id"], "head_rev": self.push["head_rev"]},
            "settings": {name: bool(self.settings.get(name, False)) for name in SETTING_NAMES},
        }
        for k, v in self.extra.items():
            out[k] = copy.deepcopy(v)
        return out

    # ------------------------------------------------------------ load
    @staticmethod
    def validate(d, nc: int) -> None:
        """Raises StateError when the section is not valid (spec 9.3)."""
        _parse(d, nc)

    def load(self, d, nc: int) -> None:
        """Validates and loads a ``p5`` section (``nc`` = number of grid cells). Raises StateError."""
        parts = _parse(d, nc)
        self.later = parts["later"]
        self.tags = parts["tags"]
        self.tag_n = parts["tag_n"]
        self.real = parts["real"]
        self.masks = parts["masks"]
        self.nw_applied = parts["nw_applied"]
        self.push = parts["push"]
        self.settings = parts["settings"]
        self.extra = parts["extra"]

    # ------------------------------------------------------------ Forget all (spec 7.2 step 2, D40)
    def reset_for_forget(self) -> "P5State":
        """A fresh section that keeps exactly the four settings and the push counters ``seq`` and ``acked``."""
        fresh = P5State(self.later_max, self.tags_max)
        fresh.settings = dict(self.settings)
        fresh.push["seq"] = self.push["seq"]
        fresh.push["acked"] = self.push["acked"]
        return fresh

    # ------------------------------------------------------------ settings
    def setting(self, name: str) -> bool:
        return bool(self.settings.get(name, False))

    def set_setting(self, name: str, on: bool) -> bool:
        """Sets one of the four switches. True when it changed. An unknown name changes nothing."""
        if name not in SETTING_NAMES:
            return False
        on = bool(on)
        if self.settings.get(name, False) == on:
            return False
        self.settings[name] = on
        return True

    # ------------------------------------------------------------ tags (spec 2.3)
    def tag_of(self, key: str) -> str:
        return self.tags.get(key, "")

    def alloc_tag(self, key: str) -> str:
        """The tag of a suggestion id, allocated on first use (A, B, ..., Z, AA, ...). The counter never decreases."""
        t = self.tags.get(key)
        if t is not None:
            return t
        used = set(self.tags.values())
        for _ in range(18278 + 1):
            t = tag_text(self.tag_n)
            self.tag_n += 1
            if t not in used:
                break
        self.tags[key] = t
        while len(self.tags) > self.tags_max:
            del self.tags[next(iter(self.tags))]
        return t

    def prune_tags(self, keep) -> bool:
        """Drops the tags whose id ``keep(id)`` says no longer exists. True when something was dropped."""
        gone = [k for k in self.tags if not keep(k)]
        for k in gone:
            del self.tags[k]
        return bool(gone)

    # ------------------------------------------------------------ [Later] markers (spec 2.3)
    def set_later(self, item: str, ord_: int) -> None:
        self.later.pop(item, None)
        self.later[item] = ord_
        while len(self.later) > self.later_max:
            oldest = min(self.later, key=lambda k: self.later[k])      # the oldest date goes first (ties: first set)
            del self.later[oldest]
