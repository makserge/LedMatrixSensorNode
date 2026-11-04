"""The silent 'Advanced' entries and their verbs (P5 spec 2.10, 3.7, A.10). Owner: CORE.

'Advanced' is a list that is never counted in ``Review pending``, never pushed and never auto-selected. In P5 it holds
what a user answered (rejections, the night-window denial, 'No bed in view', zones and marks) and the reflections that
could be 'a real place' (D38). Like the review list it exists only once the setup review is ready.
"""
from __future__ import annotations

import re
from datetime import date

from . import rejections, zones
from .extraction import cid_key
from .types import ITEM_BED, ITEM_NIGHT_WINDOW, ADV_ID_RE, AdvancedEntry, AnswerIgnored, DebugMapChanged

_ADV = re.compile(ADV_ID_RE)


def _entry(verb: str, kind: str, target: str, name: str = "", tag: str = "", until: str = "", rev: int = 0):
    return AdvancedEntry("adv:%s:%s" % (verb, target), verb, kind, target, name, tag, until, rev)


def advanced_entries(L) -> tuple:
    """The entries in the order of spec 2.10: suppressions (newest first), unanswered reflections, zone records, labels.
    At most ``advanced_max`` (the first that fit). ``()`` before the setup review is ready."""
    rv = L.review
    if not rv.ready(L):
        return ()
    p5p = L.p5params
    out = []
    seen = set()

    def add(e) -> None:
        if e.id not in seen and _ADV.match(e.id):
            seen.add(e.id)
            out.append(e)

    today = rv._today(L)
    n_place = 0
    for sp in reversed(rv.suppressions):
        t = sp["type"]
        if t == "bed_needs_data":
            add(_entry("undo", "bed", "bed"))
        elif t == "night_window":
            if today < sp["until_ord"]:
                add(_entry("undo", "night_window", "nw", until=date.fromordinal(sp["until_ord"]).isoformat()))
        elif t == "place":
            if n_place < p5p.advanced_max_rejections:
                n_place += 1
                add(_entry("undo", "place", sp["ref"], tag=L.p5.tag_of(sp["ref"])))
    n_refl = 0
    for s in sorted(L.ns.suggestions, key=lambda d: cid_key(d["id"])):
        if n_refl >= p5p.advanced_max_reflections:
            break
        if s["type"] != "reflection" or s["state"] != "shadow":
            continue
        if rejections.real_matches(L, s["cells"], s["cx"], s["cy"]):
            continue
        n_refl += 1
        add(_entry("real", "reflection", s["id"], tag=L.p5.alloc_tag(s["id"])))
    for z in sorted(rv.zones, key=lambda r: r["zid"]):
        add(_entry("rename", "zone", z["zid"], name=z["name"], rev=z["rev"]))
        add(_entry("remove", "zone", z["zid"], name=z["name"], rev=z["rev"]))
    for lb in sorted(L.label_list, key=lambda d: d["id"]):
        add(_entry("rename", "label", lb["id"], name=lb["name"]))
        add(_entry("remove", "label", lb["id"], name=lb["name"]))
    return tuple(out[:p5p.advanced_max])


def _ign(outs: list, item, reason: str) -> bool:
    outs.append(AnswerIgnored(item, "advanced", reason))
    return False


def apply(L, ev, outs: list, now_ms: int) -> bool:
    """Applies the op ``advanced`` to the entry id in ``ev.item`` (spec 3.7). True when something changed (the learner
    then saves). A refusal is an ``AnswerIgnored``: ``no_item``, ``no_such_item`` (the target does not exist),
    ``not_pending`` (it exists but its precondition does not hold), ``name_needed``, ``stale``."""
    rv = L.review
    item = ev.item
    if not isinstance(item, str) or not item:
        return _ign(outs, None, "no_item")
    if not _ADV.match(item):
        return _ign(outs, item, "no_such_item")
    _, verb, target = item.split(":", 2)

    if verb == "undo":
        if target == "bed":
            if not any(sp["type"] == "bed_needs_data" for sp in rv.suppressions):
                return _ign(outs, item, "not_pending")
            return rv.undo_item(L, ITEM_BED)
        if target == "nw":
            if not any(sp["type"] == "night_window" for sp in rv.suppressions):
                return _ign(outs, item, "not_pending")
            return rv.undo_item(L, ITEM_NIGHT_WINDOW)
        if target not in rejections.rejected_ids(L):
            return _ign(outs, item, "no_such_item")
        return rejections.remove_place_rejections(L, target)

    if verb == "real":
        s = zones.suggestion_of(L, target)
        if s is None or s["type"] != "reflection":
            return _ign(outs, item, "no_such_item")
        if s["state"] != "shadow" or rejections.real_matches(L, s["cells"], s["cx"], s["cy"]):
            return _ign(outs, item, "not_pending")
        rejections.add_real(L, s, L.night)
        return True

    # rename / remove: a zone record or a label
    z = zones.find_record(L, target)
    lb = zones.find_label(L, target) if z is None else None
    if z is None and lb is None:
        return _ign(outs, item, "no_such_item")
    if ev.rev is not None and z is not None and ev.rev != z["rev"]:
        rv.stale_answers += 1
        return _ign(outs, item, "stale")
    if verb == "remove":
        zones.remove(L, target)
        outs.append(DebugMapChanged("review"))
        return True
    if verb == "rename":
        name, from_text = zones.requested_name(L, ev, leftover_filter=False)
        if not name:
            return _ign(outs, item, "name_needed")
        changed = zones.rename(L, target, name)
        if from_text:
            zones.consume_text(L, outs)
        if changed:
            outs.append(DebugMapChanged("review"))
        return changed or from_text
    return _ign(outs, item, "no_such_item")
