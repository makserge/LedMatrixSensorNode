"""Rejection masks, matching, gate 7, the re-ask rule and real-place markers (P5 spec 5, A.10). Owner: CORE.

A place rejection stays in ``suppressions`` in exactly the P4 form (``{"type": "place", "ref", "night"}``). What a
rejection must also remember, its region and its evidence, lives in ``p5.masks`` keyed by the rejected suggestion's id.
A candidate region matches a mask by the overlap coefficient (``supp_overlap``, 0.5) or by its centroid cell lying in the
mask (OA 7.9: rejections are matched by region, not by id). A match blocks gate 7 until the re-ask rule releases it:
the candidate's evidence ``n`` has doubled (``reject_reask_factor``) and ``reject_reask_nights`` nights have passed.

Every read of a suggestion's ``evidence`` tolerates missing keys (P4 fixtures build suggestions with ``evidence {}``).
"""
from __future__ import annotations

from . import geometry

MAX_SUPPRESSIONS = 200


def evidence_n(s: dict) -> int:
    """The suggestion's evidence ``n`` (stays in the covered days), 0 when the key is absent (spec 2.4)."""
    ev = s.get("evidence") or {}
    n = ev.get("n", 0)
    if isinstance(n, bool) or not isinstance(n, (int, float)) or n < 0:
        return 0
    return int(n)


def mask_of(s: dict) -> dict:
    return {"cx": s["cx"], "cy": s["cy"], "cells": frozenset(s["cells"]), "n0": evidence_n(s)}


# ------------------------------------------------------------ records
def place_suppressions(L) -> dict:
    """Place suppressions by rejected suggestion id."""
    out = {}
    for sp in L.review.suppressions:
        if sp["type"] == "place":
            out.setdefault(sp["ref"], sp)
    return out


def rejected_ids(L) -> set:
    return {sp["ref"] for sp in L.review.suppressions if sp["type"] == "place"}


def matches_mask(L, cells, cx: float, cy: float, mask: dict) -> bool:
    """spec 5.2: overlap coefficient >= supp_overlap, or the centroid cell lies in the mask."""
    if geometry.overlap_coef(cells, mask["cells"]) >= L.p.supp_overlap:
        return True
    c = L.geom.cell_of(cx, cy)
    return c >= 0 and c in mask["cells"]


def mask_match(L, s: dict) -> bool:
    """True when the suggestion's region matches any rejection mask, for the open-item predicate (spec 2.2 rule 3): the
    rule of spec 5.2 (overlap coefficient, or the suggestion's centroid cell in the mask) and, the other way round, the
    mask's centroid cell inside the suggestion's cells (the check of P5-6: no open place item may contain the centre of
    a rejected region). Gate 7 itself uses ``matches_mask`` alone."""
    if not L.p5.masks:
        return False
    cells = s["cells"]
    for m in L.p5.masks.values():
        if matches_mask(L, cells, s["cx"], s["cy"], m):
            return True
        c = L.geom.cell_of(m["cx"], m["cy"])
        if c >= 0 and c in cells:
            return True
    return False


def is_rejected(L, s: dict) -> bool:
    """Rejected by id, or its region matches a rejection mask (the open-item predicate, spec 2.2 rule 3)."""
    if any(sp["type"] == "place" and sp["ref"] == s["id"] for sp in L.review.suppressions):
        return True
    return mask_match(L, s)


def add_rejection(L, s: dict, night: int) -> bool:
    """Records the rejection of a place suggestion: the suppression (P4 form) and the mask. False when the suggestion
    is rejected already (a second reject of a region whose suppression exists, spec 5.1)."""
    sid = s["id"]
    if sid in rejected_ids(L):
        return False
    L.p5.masks[sid] = mask_of(s)
    L.review.suppressions.append({"type": "place", "ref": sid, "night": night})
    L.p5.later.pop(sid, None)
    L.p5.later.pop("mrg-" + sid, None)
    evict(L)
    return True


def evict(L) -> None:
    """At most MAX_SUPPRESSIONS records: the oldest place record goes first, only when none is left the oldest of any
    type (P4 evicted the oldest of any type, so 200 later rejections could remove 'No bed in view', spec 5.6). A
    place record takes its mask with it; at most MAX_SUPPRESSIONS masks exist."""
    sup = L.review.suppressions
    while len(sup) > MAX_SUPPRESSIONS:
        # the record that was added last is the one being kept: it is never the one that goes
        k = next((i for i, sp in enumerate(sup[:-1]) if sp["type"] == "place"), 0)
        gone = sup.pop(k)
        if gone["type"] == "place":
            L.p5.masks.pop(gone["ref"], None)
    masks = L.p5.masks
    while len(masks) > MAX_SUPPRESSIONS:
        del masks[next(iter(masks))]


def remove_place_rejections(L, sid: str) -> bool:
    """Removes every place suppression with ref == sid and its mask ('Undo: Place rejected'). True if any."""
    sup = L.review.suppressions
    keep = [sp for sp in sup if not (sp["type"] == "place" and sp["ref"] == sid)]
    changed = len(keep) != len(sup)
    if changed:
        sup[:] = keep
    if L.p5.masks.pop(sid, None) is not None:
        changed = True
    return changed


def fill_masks(L, suggestion_by_id=None) -> bool:
    """A P4 place suppression has no mask: it gets the mask of its suggestion when that suggestion still exists, else
    it stays without one (it blocks by id only). A mask whose suppression is gone is deleted. True if anything
    changed. Called at load and after each nightly run (spec 5.1)."""
    by_id = suggestion_by_id if suggestion_by_id is not None else {s["id"]: s for s in L.ns.suggestions}
    changed = False
    refs = rejected_ids(L)
    for ref in refs:
        if ref not in L.p5.masks and ref in by_id:
            L.p5.masks[ref] = mask_of(by_id[ref])
            changed = True
    for ref in [k for k in L.p5.masks if k not in refs]:
        del L.p5.masks[ref]
        changed = True
    return changed


# ------------------------------------------------------------ gate 7 and the re-ask rule
def _hits(L, c) -> list:
    """[(suppression, mask)] of the place suppressions whose mask the candidate matches."""
    masks = L.p5.masks
    if not masks:
        return []
    out = []
    for ref, sp in place_suppressions(L).items():
        m = masks.get(ref)
        if m is not None and matches_mask(L, c.cells, c.cx, c.cy, m):
            out.append((sp, m))
    return out


def _released(L, c, sp: dict, m: dict, night: int) -> bool:
    return c.n >= L.p5params.reject_reask_factor * max(1, m["n0"]) and night - sp["night"] >= \
        L.p5params.reject_reask_nights


def blocks_candidate(L, c, night: int) -> bool:
    """Gate 7 fails (spec 5.2): the candidate matches a rejection mask that the re-ask rule has not released."""
    hits = _hits(L, c)
    return any(not _released(L, c, sp, m, night) for sp, m in hits)


def release_due(L, cands, night: int) -> set:
    """The suggestion ids whose rejection the re-ask rule releases in this nightly run: a candidate that matches only
    released masks (so it is not blocked) frees them. The run deletes them after step 9 with ``apply_release``."""
    out = set()
    for c in cands:
        hits = _hits(L, c)
        if hits and all(_released(L, c, sp, m, night) for sp, m in hits):
            out.update(sp["ref"] for sp, _ in hits)
    return out


def apply_release(L, refs) -> bool:
    """Deletes the released suppressions and their masks. True if anything was deleted."""
    changed = False
    for ref in refs:
        if remove_place_rejections(L, ref):
            changed = True
    return changed


# ------------------------------------------------------------ 'It's a real place' (spec 5.4)
def add_real(L, s: dict, night: int, real_max: int = None) -> None:
    """Stores a permanent marker for a reflection suggestion (at most ``real_place_max``, the oldest dropped)."""
    cap = real_max if real_max is not None else L.p5params.real_place_max
    real = L.p5.real
    real.append({"ref": s["id"], "cx": s["cx"], "cy": s["cy"], "cells": frozenset(s["cells"]), "night": night})
    while len(real) > cap:
        del real[0]


def real_matches(L, cells, cx: float, cy: float) -> bool:
    """True when the region matches a 'real place' marker by the rule of spec 5.2."""
    for m in L.p5.real:
        if matches_mask(L, cells, cx, cy, m):
            return True
    return False
