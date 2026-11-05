"""``where_am_i`` and ``live_view``: the single confirmed track against the open items and zones (P5 spec 2.7, A.10).

Read-only: nothing here changes the learner. The positions are the tracker's filtered positions and they leave the core
only through ``live_view`` / ``Learner.review_map(live=True)``, which the shell calls while 'Show me live' runs.
"""
from __future__ import annotations

from typing import Optional

from .types import WhereView


def track_stays(L) -> list:
    """[(track, (t0, t1, cx, cy, n))]: one entry per live confirmed track whose stay in progress is at least
    ``mark_min_stay_s`` long. The same rule as ``labels.stays_in_progress`` (which 'Mark spot here' uses), with the track
    kept so that the live view can colour a marker as 'staying'; a unit test pins the two against each other."""
    p = L.p
    out = []
    for tr in L.tracker.live:
        tb = tr.bh
        if not tr.confirmed or tb is None or not tb.active:
            continue
        run = tb.run()
        if run is None:
            continue
        t0, t1, cx, cy, n, sd = run
        if t1 - t0 < p.frag_min_s:
            continue
        if sd < p.epp_sd_m and t1 - t0 >= p.epp_min_s:
            continue                                     # an EPP ghost is no stay in progress
        st = L.bh.bridge_target(t0, cx, cy)
        if st is not None:
            w = st.n + n
            t0 = st.t0
            cx = (st.cx * st.n + cx * n) / w
            cy = (st.cy * st.n + cy * n) / w
            n = w
        if t1 - t0 >= p.mark_min_stay_s:
            out.append((tr, (t0, t1, cx, cy, n)))
    return out


def _confirmed(L) -> list:
    return [tr for tr in L.tracker.live if tr.confirmed]


def live_view(L) -> tuple:
    """((x_m, y_m, stay), ...) of the confirmed live tracks while the learner is "learning", else ()."""
    if L._state_now() != "learning":
        return ()
    staying = {id(tr) for tr, _ in track_stays(L)}
    return tuple((round(tr.x, 3), round(tr.y, 3), id(tr) in staying) for tr in _confirmed(L))


def _near(geom, cells, cell: int) -> bool:
    """``cell in geom.dilate(cells)`` without building the dilation: the cell or one of its 8 neighbours is in cells."""
    if cell in cells:
        return True
    for n in geom.n8[cell]:
        if n in cells:
            return True
    return False


def item_cells(L, item) -> Optional[frozenset]:
    """The cells a tier-2 review item stands for: the suggestion's (a modify item's zone is drawn by the zones); P7: a
    retire item stands for its zone's own cells and a radar_moved item for nothing."""
    d = item.data
    if item.type == "retire_zone":
        z = next((x for x in L.review.zones if x["zid"] == d.get("zid")), None)
        return frozenset(z["cells"]) if z is not None else None
    if item.type == "radar_moved":
        return None
    sid = d.get("sid") if item.type in ("merge_into", "modify_zone") else item.id
    s = next((x for x in L.ns.suggestions if x["id"] == sid), None)
    return frozenset(s["cells"]) if s is not None else None


def where_am_i(L, selected: Optional[str] = None) -> WhereView:
    """spec 2.7. ``item`` is looked up only with exactly one confirmed track and the radars observing: (1) the selected
    item when it is a place, merge or modify item, (2) any other open such item (list order), (3) a confirmed zone or
    label (fewest cells first, then lowest id); each by ``cell in dilate(cells)``."""
    if L._state_now() != "learning":
        return WhereView(False, 0, False, None, None)
    tracks = _confirmed(L)
    people = len(tracks)
    stay_ready = len(track_stays(L)) == 1
    item = None
    kind = None
    if people == 1:
        tr = tracks[0]
        cell = L.geom.cell_of(tr.x, tr.y)
        if cell >= 0:
            item, kind = _lookup(L, cell, selected)
    return WhereView(True, people, stay_ready, item, kind)


def _lookup(L, cell: int, selected: Optional[str]) -> tuple:
    geom = L.geom
    sel = selected if selected is not None else L.review.selected(L)
    tier2 = [i for i in L.review.items(L) if i.tier == 2]
    ordered = [i for i in tier2 if i.id == sel] + [i for i in tier2 if i.id != sel]
    for it in ordered:
        cells = item_cells(L, it)
        if cells and _near(geom, cells, cell):
            return it.id, "place"
    best = None
    for z in L.review.zones:
        if _near(geom, z["cells"], cell):
            k = (len(z["cells"]), z["zid"])
            if best is None or k < best[0]:
                best = (k, z["zid"], "zone")
    for lb in L.label_list:
        if _near(geom, lb["cells"], cell):
            k = (len(lb["cells"]), lb["id"])
            if best is None or k < best[0]:
                best = (k, lb["id"], "label")
    if best is not None:
        return best[1], best[2]
    return None, None
