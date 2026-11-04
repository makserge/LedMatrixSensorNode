"""Zone records and labels as one set: create, rename, update, merge, remove, views, unique names (P5 spec 4, A.10).

A confirmed zone is a record in ``ReviewModel.zones`` (made by approving a place suggestion) or a label made by 'Mark
spot here' (D13: a mark stays a label). ``zone_views`` gives both to P6 as one read view. Zone ids are stable across
rename, modify and merge (OA 6.3: the unique_id stays stable). Nothing here publishes anything (shadow mode, D1).
"""
from __future__ import annotations

from typing import Optional

from . import geometry, profiles
from . import labels as labels_mod
from .extraction import PLACE_TYPES, _kind_of
from .types import NameEcho, ZoneView

NAME_MAX = 32


# ------------------------------------------------------------ lookups
def find_record(L, zid: str) -> Optional[dict]:
    return next((z for z in L.review.zones if z["zid"] == zid), None)


def find_label(L, lid: str) -> Optional[dict]:
    return next((lb for lb in L.label_list if lb["id"] == lid), None)


def suggestion_of(L, sid) -> Optional[dict]:
    if sid is None:
        return None
    return next((s for s in L.ns.suggestions if s["id"] == sid), None)


def label_covers(L, s: dict) -> bool:
    """A 'Mark spot here' label links to the place (labels.match: the label's cell in the dilated cells, or within
    label_match_m of the centre). One dilation per suggestion."""
    labels = L.label_list
    if not labels:
        return False
    geom = L.geom
    dil = None
    for lb in labels:
        c = geom.cell_of(lb["x"], lb["y"])
        if c >= 0:
            if dil is None:
                dil = geom.dilate(s["cells"])
            if c in dil:
                return True
        if ((lb["x"] - s["cx"]) ** 2 + (lb["y"] - s["cy"]) ** 2) ** 0.5 <= L.p.label_match_m:
            return True
    return False


def covered_ids(L) -> set:
    """Suggestion ids covered by a zone record: the ``ref`` and every id in ``also``."""
    out = set()
    for z in L.review.zones:
        if z.get("ref") is not None:
            out.add(z["ref"])
        out.update(z.get("also", ()))
    return out


def is_covered(L, s: dict, covered: Optional[set] = None) -> bool:
    """spec 2.2 rule 2: a zone record (ref or also) or a label covers the place."""
    cov = covered if covered is not None else covered_ids(L)
    return s["id"] in cov or label_covers(L, s)


# ------------------------------------------------------------ names
def _norm(name: str) -> str:
    return name.strip().casefold()


def unique_name(L, name: str, exclude_id: Optional[str] = None) -> str:
    """D12: a name equal (case-insensitive, trimmed) to the name of another zone record or label gets the suffix
    ' 2', ' 3', ...; the base is cut so that name plus suffix is at most NAME_MAX characters."""
    name = name.strip()[:NAME_MAX]
    used = {_norm(z["name"]) for z in L.review.zones if z["zid"] != exclude_id and z["name"].strip()}
    used |= {_norm(lb["name"]) for lb in L.label_list if lb["id"] != exclude_id and lb["name"].strip()}
    if _norm(name) not in used:
        return name
    k = 2
    while True:
        suffix = " %d" % k
        cand = name[:NAME_MAX - len(suffix)].rstrip() + suffix
        if _norm(cand) not in used:
            return cand
        k += 1


def _is_label_name(L, text: str) -> bool:
    t = _norm(text)
    return any(_norm(lb["name"]) == t for lb in L.label_list)


def requested_name(L, ev, leftover_filter: bool) -> tuple:
    """(name, from_text) of an answer (spec 3.5). ``ev.name`` wins when given. Otherwise the name text ``L.spot_name``
    is read only for an answer without an answer id (the buttons of the review device, the P4 direct calls) and, with
    ``leftover_filter``, only when it is not a leftover of a 'Mark spot here' (it does not equal the name of an
    existing label: a mark does not clear the text, P3 D23). An answer with an ``aid`` never reads the text."""
    given = ev.name if isinstance(getattr(ev, "name", None), str) else None
    if given is not None and given.strip():
        return given.strip()[:NAME_MAX], False
    if getattr(ev, "aid", None) is None:
        t = L.spot_name.strip()[:NAME_MAX]
        if t and not (leftover_filter and _is_label_name(L, t)):
            return t, True
    return "", False


def consume_text(L, outs: list) -> None:
    """D11: the name text is used up by an applied answer: cleared and echoed. No ``SaveRequested("name")`` follows (the
    answer's own ``SaveRequested("review")`` writes ``settings.spot_name``; a ``name`` save would outrank it)."""
    L.spot_name = ""
    outs.append(NameEcho(""))


# ------------------------------------------------------------ geometry helpers
def polygon(L, cells) -> list:
    return geometry.polygon_cm(cells, L.geom, 0.1, L.p.poly_max_vertices) if cells else []


def cells_centroid(L, cells) -> tuple:
    """Mean of the cell centres in metres, rounded to 3 decimals."""
    cs = sorted(cells)
    if not cs:
        return 0.0, 0.0
    xs = [L.geom.cell_xy(c) for c in cs]
    return round(sum(x for x, _ in xs) / len(xs), 3), round(sum(y for _, y in xs) / len(xs), 3)


# ------------------------------------------------------------ records
def create_zone(L, s: dict, name: str, kind: str, now_ms: int) -> dict:
    """A new zone record from a place suggestion (approve, or 'No, it's a new place'). ``name`` is final (unique, at
    most NAME_MAX characters); a kind ``bed`` makes the suggestion a bed (kind and name_hint), as in P4."""
    rv = L.review
    zid = "z%04d" % rv.next_zone_n
    rv.next_zone_n += 1
    tr = L.p.time_round_s
    cells = frozenset(s["cells"])
    rec = {"zid": zid, "name": name, "kind": kind, "src": "approve", "ref": s["id"], "cx": s["cx"], "cy": s["cy"],
           "cells": cells, "night": L.night, "created_s": int(now_ms // 1000 // tr) * tr,
           "poly_cm": polygon(L, cells), "rev": 0, "upd": L.night, "srev": s["rev"], "also": []}
    rv.zones.append(rec)
    if kind == "bed":
        s["kind"] = "bed"
        s["name_hint"] = name
    return rec


def merge_into_zone(L, zid: str, s: dict) -> Optional[dict]:
    """spec 3.6: the zone's cells become the union of its cells and the suggestion's, the centre the mean of the union's
    cell centres, the suggestion id joins ``also`` (at most zones_also_max, the oldest dropped), rev + 1."""
    z = find_record(L, zid)
    if z is None:
        return None
    z["cells"] = frozenset(z["cells"]) | frozenset(s["cells"])
    z["cx"], z["cy"] = cells_centroid(L, z["cells"])
    z["poly_cm"] = polygon(L, z["cells"])
    also = [x for x in z.get("also", []) if x != s["id"]]
    also.append(s["id"])
    cap = L.p5params.zones_also_max
    z["also"] = also[-cap:] if cap > 0 else []
    z["rev"] += 1
    z["upd"] = L.night
    return z


def update_zone(L, zid: str, s: dict) -> Optional[dict]:
    """spec 3.6 (modify): cells, centre and polygon are taken from the suggestion, srev = its rev, rev + 1."""
    z = find_record(L, zid)
    if z is None:
        return None
    z["cells"] = frozenset(s["cells"])
    z["cx"], z["cy"] = s["cx"], s["cy"]
    z["poly_cm"] = polygon(L, z["cells"])
    z["srev"] = s["rev"]
    z["rev"] += 1
    z["upd"] = L.night
    return z


def rename(L, target_id: str, name: str) -> bool:
    """Renames a zone record or a label (spec 3.7 ``adv:rename``): a unique name, kind up to ``bed`` (a bed is never
    downgraded), rev + 1 for a record. True when something changed."""
    z = find_record(L, target_id)
    lb = find_label(L, target_id) if z is None else None
    rec = z if z is not None else lb
    if rec is None:
        return False
    name = unique_name(L, name, exclude_id=target_id)
    bed = L.review.kind_of(name) == "bed"
    if name == rec["name"] and (not bed or rec["kind"] == "bed"):
        return False
    rec["name"] = name
    if bed:
        rec["kind"] = "bed"
        if z is not None:
            s = suggestion_of(L, z["ref"])
            if s is not None:
                s["kind"] = "bed"
                s["name_hint"] = name
    if z is not None:
        z["rev"] += 1
        z["upd"] = L.night
    return True


def remove(L, target_id: str) -> bool:
    """Deletes a zone record or a label (spec 3.7 ``adv:remove``). A removed label un-links its suggestions: a
    suggestion that only that label linked gets name_hint None and the kind of its type. True if something went."""
    z = find_record(L, target_id)
    if z is not None:
        L.review.zones.remove(z)
        s = suggestion_of(L, z.get("ref"))
        if s is not None and z["kind"] == "bed" and s["kind"] == "bed" and (s["name_hint"] or "") == z["name"] \
                and s["type"] in PLACE_TYPES:
            # the approval (or a rename to bed) made the suggestion a bed: give it back its own kind, so that the bed
            # item returns when no zone or label of kind bed remains (spec 3.7); the learner's own bed naming applies
            # again at the next nightly run
            s["kind"] = _kind_of(s["type"])
            s["name_hint"] = None
        return True
    lb = find_label(L, target_id)
    if lb is None:
        return False
    L.label_list.remove(lb)
    for s in L.ns.suggestions:
        if s["type"] not in PLACE_TYPES:
            continue
        if not labels_mod.match(lb, s, L.geom, L.p.label_match_m):
            continue
        if any(labels_mod.match(other, s, L.geom, L.p.label_match_m) for other in L.label_list):
            continue
        s["name_hint"] = None
        s["kind"] = _kind_of(s["type"])
    return True


# ------------------------------------------------------------ views
def window_hours(L) -> frozenset:
    return profiles.window_hours(L.window["start"], L.window["end"])


def is_frozen(L, kind: str, ref: Optional[str], win: Optional[frozenset] = None) -> bool:
    """The OA 7.4 predicate (spec 4.2): ``profiles.frozen`` with the confirmation-time hour profile of the ``ref``
    suggestion; a label, or a zone whose suggestion is gone, has none (a bed is frozen while the schedule is ON)."""
    s = suggestion_of(L, ref)
    hours_at = None
    if s is not None:
        hours_at = (s.get("evidence") or {}).get("hours_at_suggestion")
    return profiles.frozen(L.window["sched"], kind, hours_at, win if win is not None else window_hours(L), L.p)


def frozen_view(L, view: ZoneView) -> bool:
    return is_frozen(L, view.kind, view.ref)


def _label_night(L, lb: dict) -> int:
    if L.created_ord is None:
        return 0
    return max(0, L.tm.local(lb["created_s"] * 1000).date_ord - L.created_ord)


def zone_views(L) -> tuple:
    """ZoneView of every confirmed zone: the records (ordered by zid) and then the labels (ordered by id)."""
    win = window_hours(L)
    out = []
    for z in sorted(L.review.zones, key=lambda r: r["zid"]):
        out.append(ZoneView(z["zid"], z["name"], z["kind"], "approve", z.get("ref"), z["rev"], float(z["cx"]),
                            float(z["cy"]), frozenset(z["cells"]), tuple(tuple(v) for v in z["poly_cm"]),
                            z["created_s"], z["night"], is_frozen(L, z["kind"], z.get("ref"), win)))
    for lb in sorted(L.label_list, key=lambda d: d["id"]):
        out.append(ZoneView(lb["id"], lb["name"], lb["kind"], "mark", None, 0, float(lb["x"]), float(lb["y"]),
                            frozenset(lb["cells"]), tuple(tuple(v) for v in polygon(L, lb["cells"])),
                            lb["created_s"], _label_night(L, lb), is_frozen(L, lb["kind"], None, win)))
    return tuple(out)
