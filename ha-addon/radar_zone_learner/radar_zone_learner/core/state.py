"""State dict codec, schema v4 (P3 spec 8.1 and the P4 additions; OA 7.7, 10).

Schema 4 adds the top-level sections ``zones``, ``suppressions`` and ``review`` and fills ``night`` (night_lux,
onsets, wakes, activity bins, proposal_rev, night_learning). A v3 file loads through ``migrate_3_to_4``.

P5 adds no schema version (D2): an optional top-level section ``p5`` after ``settings`` (written only when it differs
from the defaults, see ``core/p5state.py``) and optional keys in zone records (``poly_cm``, ``rev``, ``upd``, ``srev``,
``also``); a P4 file loads with the defaults, a P5 file loads in 0.4.0 (which keeps unknown sections).

export_state() builds a fresh JSON-serialisable dict (it never shares mutable objects with the
learner); load_state() validates a dict and loads it into a fresh learner, raising StateError.
Tracks, pending stays, fragment buffers, the 24 h fragment list, timelines and epochs are RAM only.
"""
from __future__ import annotations

import copy
import math
from datetime import date

from . import geometry
from . import nightlearn as nightlearn_mod
from . import night as night_mod
from . import rejections
from . import review as review_mod
from .covis import Covis
from .extraction import NightState
from .grids import GRID_NAMES, decode_array, encode_array, rle_decode, rle_encode
from .p5state import P5State
from .params import DEFAULT_PARAMS, params_digest
from .types import APP_VERSION, MIN_SCHEMA_VERSION, SCHEMA_VERSION, StateError

TOP_KEYS = (("schema", int), ("writer", str), ("phase", str), ("params", str), ("meta", dict),
            ("clock", dict), ("grids", dict), ("days_bits", dict), ("covis", dict), ("coverage", dict),
            ("stays", list), ("door_events", list), ("ghost_events", list), ("candidates", list),
            ("doors_prev", list), ("outline", dict), ("suggestions", list), ("labels", list),
            ("zones", list), ("suppressions", list), ("review", dict), ("night", dict), ("settings", dict))
OPTIONAL_KEYS = (("p5", dict),)                                 # present only when P5 data exists (spec 9.1)
TOP_NAMES = tuple(k for k, _ in TOP_KEYS) + tuple(k for k, _ in OPTIONAL_KEYS)

STAY_KEYS = (("t0", int), ("t1", int), ("dur", int), ("x", int), ("y", int), ("sd", int), ("nf", int),
             ("ld", (int, float)), ("he", (int, float)), ("exit", str), ("arr", str), ("day", str))
DOOR_KEYS = (("t", int), ("x", int), ("y", int), ("k", str))
GHOST_KEYS = (("t", int), ("x", int), ("y", int), ("dur", int), ("why", str), ("ld", (int, float)),
              ("arr", str))
SUGG_KEYS = (("id", str), ("rev", int), ("type", str), ("kind", str), ("state", str), ("first_night", int),
             ("last_night", int), ("cx", (int, float)), ("cy", (int, float)), ("cells", list),
             ("poly_cm", list), ("conf", (int, float)), ("evidence", dict), ("variants", list),
             ("variant_evidence_split", bool), ("variant_share_split", bool), ("variant_suppressed", bool),
             ("frozen", bool))
LABEL_KEYS = (("id", str), ("created_s", int), ("x", (int, float)), ("y", (int, float)), ("name", str),
              ("kind", str), ("cells", list))
CAND_EXTRA = ("cls", "n", "core_days", "minutes", "median_s", "ld_share", "ghost_share", "covis", "covis_src",
              "conf", "gates", "outcome")

# Value ranges of a state file that is valid JSON of the right types: a corrupt number (a time of 2**63, a
# position of 10**9) would load and then raise in every nightly run, so it is refused at load.
T_MIN_S = 1577836800                 # 2020-01-01
T_MAX_S = 4102444800                 # 2100-01-01
POS_MAX_CM = 3300                    # the radar reports up to 32.7 m
DUR_MAX_S = 31 * 86400
STAY_EXITS = ("moving", "still", "moved", "censored")
STAY_ARRS = ("walk", "bridge", "static")
GHOST_WHYS = ("identical", "ld_off", "home_empty")


def _iso(d) -> str:
    return date.fromordinal(d).isoformat() if d is not None else None


def migrate_3_to_4(d: dict) -> dict:
    """A schema 3 state as a schema 4 state: the new sections start empty, ``night`` gets its P4 defaults.
    Pure: the argument is not changed. Anything the v3 file holds is kept, unknown keys included."""
    if not isinstance(d, dict) or d.get("schema") != 3 or isinstance(d.get("schema"), bool):
        raise StateError("not a schema 3 state")
    out = {}
    for k, v in d.items():
        if k == "schema":
            out[k] = 4
        elif k == "phase":
            out[k] = "p4"
        else:
            out[k] = copy.deepcopy(v)
        if k == "labels":
            out["zones"] = []
            out["suppressions"] = []
            out["review"] = {"later": {"night_window": None, "bed_needs_data": None}, "bed_rev": 0,
                             "next_zone_n": 1}
    night = out.get("night")
    if isinstance(night, dict):
        aw = night.get("active_window")
        out["night"] = {"active_window": aw, "night_lux": DEFAULT_PARAMS.night_lux, "onsets": [], "wakes": [],
                        "act": {}, "proposal_rev": 0,
                        "night_learning": {"active": False, "until": None, "set_by_learner": None}}
        for k, v in night.items():                      # a v3 'night' with more keys keeps them
            if k not in out["night"]:
                out["night"][k] = copy.deepcopy(v)
    for k in ("zones", "suppressions", "review"):
        out.setdefault(k, [] if k != "review" else
                       {"later": {"night_window": None, "bed_needs_data": None}, "bed_rev": 0, "next_zone_n": 1})
    return out


# ---------------------------------------------------------------- export
def export_state(L) -> dict:
    G = L.grids
    ns = L.ns
    zones, supp, review = L.review.export_sections()
    night = {"active_window": {"start": L.window["start"], "end": L.window["end"], "sched": L.window["sched"]}}
    night.update(L.night_m.export())
    night["night_learning"] = L.nl.export()
    out = {
        "schema": SCHEMA_VERSION,
        "writer": "radar_zone_learner " + APP_VERSION,
        "phase": "p5",
        "params": params_digest(L.p),
        "meta": {"created_date": _iso(L.created_ord), "created_ms": L.created_ms,
                 "last_extraction_date": _iso(L.last_run_ord), "night": L.night,
                 "next_track_id": L.tracker.next_id, "next_label_n": L.next_label_n,
                 "last_boot_id": L.obs.boot_id if L.obs.boot_id is not None else L.last_boot_id,
                 "last_seq": L.obs.epoch.next_seq if L.obs.epoch is not None else L.last_seq,
                 "walk_points_total": L.bh.walk_points_total},
        "clock": {"eff_s": G.eff_s, "eff_f": G.eff_f, "obs_s_since_extraction": L.obs.obs_s_since_extraction},
        "grids": dict([("enc", "f32le-b64"), ("nx", L.geom.nx), ("ny", L.geom.ny)] +
                      [(name, encode_array(G[name])) for name in GRID_NAMES]),
        "days_bits": {"enc": "u16le-b64", "base": _iso(G.days_base), "data": encode_array(G.days_bits)},
        "covis": L.covis.export(),
        "coverage": L.obs.coverage_export(),
        "stays": [dict(st) for st in L.bh.stays],
        "door_events": [dict(e) for e in L.bh.door_events],
        "ghost_events": [dict(e) for e in L.bh.ghost_events],
        "candidates": [_cand_out(c) for c in ns.candidates],
        "doors_prev": [{"cells": rle_encode(d["cells"]), "cx": d["cx"], "cy": d["cy"], "streak": d["streak"]}
                       for d in ns.doors_prev],
        "outline": {"cells": rle_encode(ns.outline["cells"]), "streak": ns.outline["streak"],
                    "done": ns.outline["done"]},
        "suggestions": [_sugg_out(s) for s in ns.suggestions],
        "labels": [dict(lb, cells=rle_encode(lb["cells"])) for lb in L.label_list],
        "zones": zones,
        "suppressions": supp,
        "review": review,
        "night": night,
        "settings": {"spot_name": L.spot_name},
    }
    if not L.p5.is_default():                          # a fresh learner exports exactly what P4 exported
        out["p5"] = L.p5.export()
    for k, v in L.extra.items():
        out[k] = copy.deepcopy(v)
    return out


def _cand_out(c: dict) -> dict:
    d = {"cid": c["cid"], "cells": rle_encode(c["cells"]), "cx": c["cx"], "cy": c["cy"], "streak": c["streak"]}
    for k in CAND_EXTRA:
        if k in c:
            d[k] = copy.deepcopy(c[k])
    return d


def _sugg_out(s: dict) -> dict:
    return {"id": s["id"], "rev": s["rev"], "type": s["type"], "kind": s["kind"], "name_hint": s["name_hint"],
            "state": s["state"], "first_night": s["first_night"], "last_night": s["last_night"],
            "cx": s["cx"], "cy": s["cy"], "cells": rle_encode(s["cells"]),
            "poly_cm": [list(v) for v in s["poly_cm"]], "conf": s["conf"],
            "evidence": copy.deepcopy(s["evidence"]), "variants": copy.deepcopy(s["variants"]),
            "variant_evidence_split": s["variant_evidence_split"],
            "variant_share_split": s["variant_share_split"], "variant_suppressed": s["variant_suppressed"],
            "frozen": s["frozen"]}


# ---------------------------------------------------------------- validation helpers
def _fail(msg: str):
    raise StateError(msg)


def _is_type(v, t) -> bool:
    if t is int or t == (int, float):
        if isinstance(v, bool):
            return False
    if t == (int, float):
        return isinstance(v, (int, float)) and math.isfinite(v)
    return isinstance(v, t)


def _check_keys(d, keys, what: str) -> None:
    if not isinstance(d, dict):
        _fail(what + " is not an object")
    for k, t in keys:
        if k not in d:
            _fail("%s: missing %s" % (what, k))
        if not _is_type(d[k], t):
            _fail("%s: bad %s" % (what, k))


def _date(v, what: str, nullable: bool = False):
    if v is None and nullable:
        return None
    if not isinstance(v, str):
        _fail("bad date in " + what)
    try:
        return date.fromisoformat(v).toordinal()
    except ValueError:
        _fail("bad date in " + what)


def _cells(runs, nc: int, what: str) -> frozenset:
    if not isinstance(runs, list):
        _fail("bad cells in " + what)
    for r in runs:
        if not (isinstance(r, list) and len(r) == 2 and _is_type(r[0], int) and _is_type(r[1], int)
                and r[0] >= 0 and r[1] >= 1 and r[0] + r[1] <= nc):
            _fail("bad cells in " + what)
    return rle_decode(runs)


def _in(v, lo, hi, what: str) -> None:
    if not lo <= v <= hi:
        _fail("out of range: " + what)


def _num(v, what: str) -> float:
    if not _is_type(v, (int, float)):
        _fail("bad number in " + what)
    return v


# ---------------------------------------------------------------- load
def load_state(L, d: dict) -> None:
    """Validate d and load it into the (fresh) learner L. Raises StateError."""
    if not isinstance(d, dict):
        _fail("state is not an object")
    sch = d.get("schema")
    if isinstance(sch, bool) or not isinstance(sch, int) or not MIN_SCHEMA_VERSION <= sch <= SCHEMA_VERSION:
        _fail("unsupported schema")
    if sch == 3:
        d = migrate_3_to_4(d)
    for k, t in TOP_KEYS:
        if k not in d:
            _fail("missing " + k)
        if not _is_type(d[k], t):
            _fail("bad type of " + k)
    for k, t in OPTIONAL_KEYS:
        if k in d and not _is_type(d[k], t):
            _fail("bad type of " + k)
    nc = L.geom.nc
    G = L.grids

    meta = d["meta"]
    _check_keys(meta, (("night", int), ("next_track_id", int), ("next_label_n", int),
                       ("walk_points_total", int)), "meta")
    created_ord = _date(meta.get("created_date"), "meta", nullable=True)
    created_ms = meta.get("created_ms")
    if created_ms is not None and not _is_type(created_ms, int):
        _fail("bad created_ms")
    if created_ms is not None:
        _in(created_ms, T_MIN_S * 1000, T_MAX_S * 1000, "created_ms")
    last_run = _date(meta.get("last_extraction_date"), "meta", nullable=True)
    if meta["night"] < 0 or meta["next_track_id"] < 1 or meta["next_label_n"] < 1 or meta["walk_points_total"] < 0:
        _fail("bad meta values")
    lb = meta.get("last_boot_id")
    ls = meta.get("last_seq")
    if lb is not None and not isinstance(lb, str):
        _fail("bad last_boot_id")
    if ls is not None and not _is_type(ls, int):
        _fail("bad last_seq")

    clock = d["clock"]
    _check_keys(clock, (("eff_s", (int, float)), ("eff_f", (int, float)),
                        ("obs_s_since_extraction", (int, float))), "clock")
    for k in ("eff_s", "eff_f", "obs_s_since_extraction"):
        _in(clock[k], 0.0, 1e9, "clock " + k)

    grids = d["grids"]
    if grids.get("enc") != "f32le-b64" or grids.get("nx") != L.geom.nx or grids.get("ny") != L.geom.ny:
        _fail("bad grids header")
    arrays = {}
    for name in GRID_NAMES:
        if not isinstance(grids.get(name), str):
            _fail("missing grid " + name)
        try:
            a = decode_array("f", grids[name], nc)
        except (ValueError, TypeError):
            _fail("bad grid " + name)
        if not all(math.isfinite(v) for v in a):
            _fail("grid not finite: " + name)
        arrays[name] = a

    db = d["days_bits"]
    if db.get("enc") != "u16le-b64" or not isinstance(db.get("data"), str):
        _fail("bad days_bits")
    try:
        bits = decode_array("H", db["data"], nc)
    except (ValueError, TypeError):
        _fail("bad days_bits data")
    days_base = _date(db.get("base"), "days_bits", nullable=True)

    cv = d["covis"]
    if cv.get("enc") != "pairs-u32le-f32le-b64" or not isinstance(cv.get("fragsec"), str) \
            or not isinstance(cv.get("pairs"), str):
        _fail("bad covis")
    covis = Covis(L.p)
    try:
        covis.load(cv)
    except (ValueError, TypeError):
        _fail("bad covis data")

    coverage = {}
    for k, row in d["coverage"].items():
        do = _date(k, "coverage")
        if not (isinstance(row, list) and len(row) == 24):
            _fail("bad coverage row")
        coverage[do] = [float(_num(v, "coverage")) for v in row]

    stays = []
    for st in d["stays"]:
        _check_keys(st, STAY_KEYS, "stay")
        _date(st["day"], "stay")
        _in(st["t0"], T_MIN_S, T_MAX_S, "stay t0")
        _in(st["t1"], st["t0"], T_MAX_S, "stay t1")
        _in(st["dur"], 0, DUR_MAX_S, "stay dur")
        _in(st["x"], -POS_MAX_CM, POS_MAX_CM, "stay x")
        _in(st["y"], -POS_MAX_CM, POS_MAX_CM, "stay y")
        _in(st["sd"], 0, POS_MAX_CM, "stay sd")
        _in(st["nf"], 1, 10 ** 7, "stay nf")
        _in(st["ld"], 0.0, 1.0, "stay ld")
        _in(st["he"], 0.0, 1.0, "stay he")
        if st["exit"] not in STAY_EXITS or st["arr"] not in STAY_ARRS:
            _fail("bad stay exit or arr")
        stays.append(dict(st))
    doors = []
    for e in d["door_events"]:
        _check_keys(e, DOOR_KEYS, "door event")
        _in(e["t"], T_MIN_S, T_MAX_S, "door t")
        _in(e["x"], -POS_MAX_CM, POS_MAX_CM, "door x")
        _in(e["y"], -POS_MAX_CM, POS_MAX_CM, "door y")
        if e["k"] not in ("b", "d"):
            _fail("bad door kind")
        doors.append(dict(e))
    ghosts = []
    for e in d["ghost_events"]:
        _check_keys(e, GHOST_KEYS, "ghost event")
        _in(e["t"], T_MIN_S, T_MAX_S, "ghost t")
        _in(e["x"], -POS_MAX_CM, POS_MAX_CM, "ghost x")
        _in(e["y"], -POS_MAX_CM, POS_MAX_CM, "ghost y")
        _in(e["dur"], 0, 365 * 86400, "ghost dur")
        _in(e["ld"], 0.0, 1.0, "ghost ld")
        if e["why"] not in GHOST_WHYS or e["arr"] not in STAY_ARRS:
            _fail("bad ghost why or arr")
        ghosts.append(dict(e))

    cands = []
    for c in d["candidates"]:
        _check_keys(c, (("cid", str), ("cells", list), ("cx", (int, float)), ("cy", (int, float)),
                        ("streak", int)), "candidate")
        _in(c["cx"], -33.0, 33.0, "candidate cx")
        _in(c["cy"], -33.0, 33.0, "candidate cy")
        rec = {"cid": c["cid"], "cells": _cells(c["cells"], nc, "candidate"), "cx": c["cx"], "cy": c["cy"],
               "streak": c["streak"]}
        for k in CAND_EXTRA:
            if k in c:
                rec[k] = copy.deepcopy(c[k])
        cands.append(rec)
    doors_prev = []
    for c in d["doors_prev"]:
        _check_keys(c, (("cells", list), ("cx", (int, float)), ("cy", (int, float)), ("streak", int)), "door")
        doors_prev.append({"cells": _cells(c["cells"], nc, "door"), "cx": c["cx"], "cy": c["cy"],
                           "streak": c["streak"]})
    o = d["outline"]
    _check_keys(o, (("cells", list), ("streak", int), ("done", bool)), "outline")
    outline = {"cells": _cells(o["cells"], nc, "outline"), "streak": o["streak"], "done": o["done"]}

    suggestions = []
    for s in d["suggestions"]:
        _check_keys(s, SUGG_KEYS, "suggestion")
        _in(s["cx"], -33.0, 33.0, "suggestion cx")
        _in(s["cy"], -33.0, 33.0, "suggestion cy")
        if "name_hint" not in s or not (s["name_hint"] is None or isinstance(s["name_hint"], str)):
            _fail("suggestion: bad name_hint")
        rec = {k: copy.deepcopy(s[k]) for k, _ in SUGG_KEYS}
        rec["name_hint"] = s["name_hint"]
        rec["cells"] = _cells(s["cells"], nc, "suggestion")
        for v in s["poly_cm"]:
            if not (isinstance(v, list) and len(v) == 2 and all(_is_type(x, int) for x in v)):
                _fail("suggestion: bad poly_cm")
        rec = {k: rec[k] for k in ("id", "rev", "type", "kind", "name_hint", "state", "first_night", "last_night",
                                   "cx", "cy", "cells", "poly_cm", "conf", "evidence", "variants",
                                   "variant_evidence_split", "variant_share_split", "variant_suppressed",
                                   "frozen")}
        suggestions.append(rec)

    labels = []
    for lbl in d["labels"]:
        _check_keys(lbl, LABEL_KEYS, "label")
        _in(lbl["created_s"], T_MIN_S, T_MAX_S, "label created_s")
        _in(lbl["x"], -33.0, 33.0, "label x")
        _in(lbl["y"], -33.0, 33.0, "label y")
        labels.append({"id": lbl["id"], "created_s": lbl["created_s"], "x": lbl["x"], "y": lbl["y"],
                       "name": lbl["name"], "kind": lbl["kind"], "cells": _cells(lbl["cells"], nc, "label")})

    night = d["night"]
    aw = night.get("active_window")
    if not isinstance(aw, dict):
        _fail("bad night.active_window")
    for k in ("start", "end"):
        v = aw.get(k)
        if not (_is_type(v, int) and 0 <= v < 1440):
            _fail("bad active_window")
    if not (aw.get("sched") is None or isinstance(aw.get("sched"), bool)):
        _fail("bad active_window.sched")
    settings = d["settings"]
    if not isinstance(settings.get("spot_name"), str):
        _fail("bad settings.spot_name")
    night_mod.validate(night, L.p)
    nightlearn_mod.validate(night.get("night_learning"), L.p)
    review_mod.validate_sections(d["zones"], d["suppressions"], d["review"], nc, T_MIN_S, T_MAX_S)
    if "p5" in d:
        P5State.validate(d["p5"], nc)
    # P4 zone records get the P5 defaults (spec 9.3): rev 0, upd = night, no also, the polygon of the cells, and srev =
    # the current rev of the ref suggestion, so that upgrading never makes a modify proposal appear
    sug_rev = {s["id"]: s["rev"] for s in suggestions}
    zones = []
    for z in d["zones"]:
        rec = dict(z)
        rec["cells"] = _cells(z["cells"], nc, "zone")
        rec.setdefault("rev", 0)
        rec.setdefault("upd", rec["night"])
        rec.setdefault("also", [])
        if "poly_cm" not in rec:
            rec["poly_cm"] = geometry.polygon_cm(rec["cells"], L.geom, 0.1, L.p.poly_max_vertices) \
                if rec["cells"] else []
        rec.setdefault("srev", sug_rev.get(rec["ref"], 0))
        zones.append(rec)

    # all checks passed: load
    L.created_ord = created_ord
    L.created_ms = created_ms
    L.last_run_ord = last_run
    L.night = meta["night"]
    L.tracker.next_id = meta["next_track_id"]
    L.next_label_n = meta["next_label_n"]
    L.last_boot_id = lb
    L.last_seq = ls
    L.bh.walk_points_total = meta["walk_points_total"]
    G.eff_s = float(clock["eff_s"])
    G.eff_f = float(clock["eff_f"])
    L.obs.obs_s_since_extraction = float(clock["obs_s_since_extraction"])
    for name in GRID_NAMES:
        G[name][:] = arrays[name]
    G.days_bits[:] = bits
    G.days_base = days_base
    L.covis = covis
    L.bh.covis = covis
    L.obs.coverage = coverage
    L.bh.stays = stays
    L.bh.door_events = doors
    L.bh.ghost_events = ghosts
    ns = NightState()
    ns.candidates = cands
    ns.doors_prev = doors_prev
    ns.outline = outline
    ns.suggestions = suggestions
    ns.envelope = outline["cells"]
    L.ns = ns
    L.label_list = labels
    L.window = {"start": aw["start"], "end": aw["end"], "sched": aw.get("sched")}
    L.spot_name = settings["spot_name"]
    L.night_m.load(night)
    L.nl.load(night["night_learning"])
    L.review.load(zones, d["suppressions"], d["review"])
    if "p5" in d:
        L.p5.load(d["p5"], nc)
    rejections.fill_masks(L)                           # a P4 place suppression gets the mask of its suggestion
    L.review.touch()
    L.extra = {k: copy.deepcopy(v) for k, v in d.items() if k not in TOP_NAMES}
