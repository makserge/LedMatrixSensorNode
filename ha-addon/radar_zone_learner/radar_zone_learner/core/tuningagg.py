"""The privacy-safe nightly count line, the pooled record and the privacy scan (P7 spec 4.8). Owner: CORE.

``nightly`` makes one JSON line per night with whitelisted keys only: counts and fixed-length histograms, no date, no night
index, no hour of day, no position, no name, no id. The shell keeps the last ``agg_keep_nights`` lines locally; the export
is **one pooled record** (``pool``: sums and the newest value of the cumulative counters, no per-night series). ``scan`` is
the privacy scan the tests run over every line and over the pooled record. Pure: the line is a deterministic function of
the learner state (the one RAM snapshot of the hidden-spell lists only decides which spells are new since the last line).
"""
from __future__ import annotations

import copy
import json
import re
from bisect import bisect_right
from typing import Optional

from . import drift, labels as labels_mod
from .extraction import GATE_KEYS, PLACE_TYPES
from .types import DRIFT_STATES, TuningLine

FORMAT_VERSION = 1

# lower edges of the bins (the first bin is everything below the first edge); bin index = bisect_right(edges, value)
BINS = {
    "sd": (1, 2, 5, 15, 60),                 # stay duration, minutes (6 bins)
    "sl": (0.2, 0.4, 0.6, 0.8),              # LD2412 share of a stay (5)
    "sf": (2, 4, 8),                         # fragments per stay (4)
    "sg": (10, 20, 30, 50),                  # spread of a stay, cm (5)
    "hs": (10, 20, 40, 80, 160, 320, 640, 1280, 2560),    # hidden spell, seconds (10)
    "dl": (10, 20, 40, 80, 160),             # learned clear delay, seconds (6)
    "n": (6, 9, 12, 18, 30),                 # candidate evidence n (6)
    "cd": (2, 3, 4, 5, 7),                   # core days (6)
    "ld": (0.3, 0.5, 0.7, 0.85, 0.95),       # LD2412 share of a candidate (6)
    "gs": (0.05, 0.1, 0.2, 0.3, 0.5),        # ghost share (6)
    "cv": (0.2, 0.4, 0.6, 0.8, 0.9),         # covis (6)
    "cf": (0.3, 0.4, 0.5, 0.6, 0.8),         # confidence (6)
    "dr": (0.1, 0.25, 0.6, 0.9, 1.2),        # drift ratio (6)
    "la": (7, 14, 28),                       # label age, days (4)
}
SUM_KEYS = ("ob", "sn", "sd", "sl", "sf", "sg", "hs", "hn", "dl", "cn", "gf", "nm", "ch", "dr", "zs", "zf", "zr", "dg",
            "lb", "la")
LAST_KEYS = ("cd", "ac")
KINDS = ("dwell", "short_task", "bed", "manual")
CH_KEYS = ("n", "cd", "ld", "gs", "cv", "cf")
OPS3 = ("approve", "reject", "later")
OPS4 = ("approve", "reject", "rename", "later")
PLACE_VARS = ("cf", "cd", "n", "gs", "cv", "ld")
RH_KEYS = ("mod_y", "mod_r", "ret_rm", "ret_kp")
OP_KINDS = {"place": OPS4, "merge": OPS4, "modify": OPS4, "retire": OPS3, "radar": OPS3}
_PLACE_VAR_SRC = {"cf": None, "cd": "core_days", "n": "n", "gs": "ghost_share", "cv": "covis", "ld": "ld_share"}
_CAP = 10 ** 9

# the fixed shape of every key: an int (None), a list of ints of that length, or a dict whose values have a shape
SHAPE = {
    "v": None, "ob": None, "cd": None, "sn": None, "hn": None, "cn": None, "zf": None, "zr": None, "dg": None,
    "sd": 6, "sl": 5, "sf": 4, "sg": 5, "hs": 10, "gf": 8, "nm": 8, "dr": 6, "zs": 7, "lb": 2, "la": 4,
    "dl": {k: 6 for k in KINDS},
    "ch": {k: 6 for k in CH_KEYS},
    "ac": {"op": {k: len(v) for k, v in OP_KINDS.items()},
           "gh": {k: (2, 6) for k in PLACE_VARS},
           "rh": {k: 6 for k in RH_KEYS}},
}


def bin_of(key: str, value) -> int:
    """The bin of ``value`` for the edges ``BINS[key]``; 0 for a value that is not a finite number."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if v != v:
        return 0
    return bisect_right(BINS[key], v)


def _bump(lst: list, i: int) -> None:
    if lst[i] < _CAP:
        lst[i] += 1


def _paths(shape, prefix: str = "") -> list:
    out = []
    for k, v in shape.items():
        p = prefix + k
        out.append(p)
        if isinstance(v, dict):
            out.extend(_paths(v, p + "."))
    return out


_WHITELIST = frozenset(_paths(SHAPE))


def whitelist() -> frozenset:
    """Every allowed key path of a nightly line (``ac.op.place``, ``ch.n``, ...)."""
    return _WHITELIST


# ---------------------------------------------------------------- the nightly line (4.8)
def _hist(key: str, values) -> list:
    out = [0] * (len(BINS[key]) + 1)
    for v in values:
        _bump(out, bin_of(key, v))
    return out


def _new_spells(prev: list, cur: list) -> list:
    """The spells appended since ``prev`` (the lists are bounded: older ones are dropped at the front): the longest suffix
    of ``prev`` that is a prefix of ``cur`` is the part both hold."""
    n = min(len(prev), len(cur))
    for k in range(n, -1, -1):
        if prev[len(prev) - k:] == cur[:k]:
            return cur[k:]
    return list(cur)


def nightly(L, night: int) -> Optional[TuningLine]:
    """P7 step 4: the count line of the run (deterministic given the learner state and the RAM spell snapshot)."""
    run = L.last_run_ord
    if run is None:
        return None
    p = L.p7params
    d14 = L.obs.covered_dates(run)[-p.slow_dates:]
    d_last = d14[-1] if d14 else None
    iso = None
    if d_last is not None:
        from datetime import date as _date
        iso = _date.fromordinal(d_last).isoformat()
    line: dict = {"v": FORMAT_VERSION}
    # observed hours and stays of the newest covered date
    row = L.obs.coverage.get(d_last) if d_last is not None else None
    line["ob"] = round(sum(row) / 3600.0, 1) if row else 0.0
    line["cd"] = len(d14)
    stays = [st for st in L.bh.stays if iso is not None and st.get("day") == iso]
    line["sn"] = len(stays)
    line["sd"] = _hist("sd", (st["dur"] / 60.0 for st in stays))
    line["sl"] = _hist("sl", (st["ld"] for st in stays))
    line["sf"] = _hist("sf", (st.get("nf", 1) for st in stays))
    line["sg"] = _hist("sg", (st.get("sd", 0) for st in stays))
    # hidden spells recorded since the last line (P6 zone6.delay.sp), pooled over zones
    cur = {zid: list(e.get("sp", [])) for zid, e in L.zone6.delay.items()}
    prev = L.p7_sp_prev
    new = []
    if prev is not None:
        for zid in sorted(cur):
            new.extend(_new_spells(prev.get(zid, []), cur[zid]))
    L.p7_sp_prev = cur
    line["hs"] = _hist("hs", new)
    line["hn"] = len(new)
    # learned clear delays per kind, once per night
    kind_of = {z["zid"]: z["kind"] for z in L.review.zones}
    kind_of.update({lb["id"]: lb["kind"] for lb in L.label_list})
    dl = {k: [0] * 6 for k in KINDS}
    for zid, e in sorted(L.zone6.delay.items()):
        k = kind_of.get(zid)
        if k in dl and e.get("cd") is not None:
            _bump(dl[k], bin_of("dl", e["cd"]))
    line["dl"] = dl
    # the extraction's candidates and gates
    cands = L.ns.candidates
    line["cn"] = len(cands)
    gf = [0] * 8
    nm = [0] * 8
    for c in cands:
        gates = c.get("gates") or {}
        fails = [i for i, k in enumerate(GATE_KEYS) if not gates.get(k, True)]
        for i in fails:
            _bump(gf, i)
        if len(fails) == 1:
            _bump(nm, fails[0])
    line["gf"] = gf
    line["nm"] = nm
    line["ch"] = {"n": _hist("n", (c.get("n", 0) for c in cands)),
                  "cd": _hist("cd", (c.get("core_days", 0) for c in cands)),
                  "ld": _hist("ld", (c.get("ld_share", 0.0) for c in cands)),
                  "gs": _hist("gs", (c.get("ghost_share", 0.0) for c in cands)),
                  "cv": _hist("cv", (c.get("covis", 0.0) for c in cands)),
                  "cf": _hist("cf", (c.get("conf", 0.0) for c in cands))}
    # the drift verdicts
    views = L.drift_view()
    line["dr"] = _hist("dr", (v.ratio for v in views if v.state in ("ok", "falling", "collapsed") and not v.frozen
                              and v.ratio >= 0.0))
    zs = [0] * len(DRIFT_STATES)
    zs_order = ("ok", "falling", "frozen", "blind", "young", "none", "collapsed")
    for v in views:
        _bump(zs, zs_order.index(v.state))
    line["zs"] = zs
    line["zf"] = sum(1 for v in views if v.lo >= p.flag_nights)
    line["zr"] = sum(1 for v in views if v.rt >= p.retire_days)
    # door events of that date
    local = L.tm.local
    line["dg"] = sum(1 for e in L.bh.door_events if d_last is not None and local(e["t"] * 1000).date_ord == d_last)
    # 'Mark spot here' labels: matched or not by a place suggestion, unmatched ones by age (counts only)
    places = [s for s in L.ns.suggestions if s["type"] in PLACE_TYPES]
    lb = [0, 0]
    la = [0] * 4
    for lab in L.label_list:
        if any(labels_mod.match(lab, s, L.geom, L.p.label_match_m) for s in places):
            lb[0] += 1
        else:
            lb[1] += 1
            age = run - local(lab["created_s"] * 1000).date_ord
            _bump(la, bin_of("la", max(0, age)))
    line["lb"] = lb
    line["la"] = la
    line["ac"] = copy.deepcopy(L.zone7.ac)
    text = _dumps(line)
    if len(text.encode("utf-8")) > p.agg_night_bytes_max:                      # a safety net: cap the cumulative counters
        line["ac"] = _capped(line["ac"], 9999)
        text = _dumps(line)
    if len(text.encode("utf-8")) > p.agg_night_bytes_max:
        line["ac"]["gh"] = {k: [[0] * 6, [0] * 6] for k in PLACE_VARS}
        text = _dumps(line)
    return TuningLine(night, text)


def _dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _capped(v, cap: int):
    if isinstance(v, list):
        return [_capped(x, cap) for x in v]
    if isinstance(v, dict):
        return {k: _capped(x, cap) for k, x in v.items()}
    return min(v, cap)


# ---------------------------------------------------------------- the pooled record (4.8)
def _add(a, b):
    if isinstance(a, list):
        return [_add(x, y) for x, y in zip(a, b)]
    if isinstance(a, dict):
        return {k: _add(a[k], b[k]) for k in a}
    return a + b


def _line(x) -> dict:
    return json.loads(x) if isinstance(x, (str, bytes)) else x


def pool(lines: list) -> dict:
    """``{"v": 1, "n": <lines pooled>, "sum": {...}, "last": {...}}``: for every ``sum`` key the element-wise sum over the
    lines, for every ``last`` key the newest line's value (the last in the list). Nothing else: no per-night list, no
    night index, no date. ``lines`` are dicts or their JSON text, oldest first."""
    ls = [_line(x) for x in lines]
    out: dict = {"v": FORMAT_VERSION, "n": len(ls), "sum": {}, "last": {}}
    if not ls:
        return out
    for k in SUM_KEYS:
        have = [ln[k] for ln in ls if k in ln]
        if not have:
            continue
        acc = have[0]
        for x in have[1:]:
            acc = _add(acc, x)
        out["sum"][k] = round(acc, 1) if isinstance(acc, float) else acc
    newest = ls[-1]
    for k in LAST_KEYS:
        if k in newest:
            out["last"][k] = copy.deepcopy(newest[k])
    return out


# ---------------------------------------------------------------- the privacy scan (4.8, P7-9 (a))
_FORBIDDEN_KEY = re.compile(r"(^|[_.])(x|y|cx|cy|xy|id|zid|sid|lid|uid|t|t0|t1|ts)($|[_.])|name|date|time|hour|night|"
                            r"day|pos|coord|stamp|clock|minute|hhmm|label_id|zone", re.I)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HHMM = re.compile(r"(^|\D)([01]?\d|2[0-3]):[0-5]\d")
_ALLOWED_TOP = frozenset(SHAPE) | {"v"}


def _shape_ok(v, shape) -> bool:
    if shape is None:
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if isinstance(shape, int):
        return isinstance(v, list) and len(v) == shape and all(isinstance(x, int) and not isinstance(x, bool) for x in v)
    if isinstance(shape, tuple):
        return isinstance(v, list) and len(v) == shape[0] and all(_shape_ok(x, shape[1]) for x in v)
    return isinstance(v, dict) and set(v) == set(shape) and all(_shape_ok(v[k], shape[k]) for k in shape)


def _scan_line(obj, out: list, where: str, positions) -> None:
    if not isinstance(obj, dict):
        out.append(where + ": not an object")
        return
    for k, v in obj.items():
        if not isinstance(k, str) or k not in SHAPE:
            out.append("%s: key %r is not whitelisted" % (where, k))
            continue
        if _FORBIDDEN_KEY.search(k) and k not in ("cd", "dg", "dl"):
            out.append("%s: key %r looks like a coordinate, name, id, date or time" % (where, k))
        if not _shape_ok(v, SHAPE[k]):
            out.append("%s: key %r has the wrong shape" % (where, k))
    _scan_values(obj, out, where, positions)


def _scan_values(v, out: list, where: str, positions) -> None:
    if isinstance(v, str):
        out.append("%s: a string value %r" % (where, v[:20]))
        if _ISO_DATE.search(v) or _HHMM.search(v):
            out.append("%s: a date or time string" % where)
    elif isinstance(v, dict):
        for x in v.values():
            _scan_values(x, out, where, positions)
    elif isinstance(v, list):
        if positions and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v) \
                and (v[0], v[1]) in positions:
            out.append("%s: a pair of numbers that equals a stay position" % where)
        for x in v:
            _scan_values(x, out, where, positions)


def scan(obj_or_text, positions=()) -> list:
    """The privacy scan: [] = clean, else violation strings. A nightly line or a pooled record (the dict with the keys
    ``v``, ``n``, ``sum``, ``last``) is accepted. Every key must be on the whitelist with its fixed shape, there is no
    string value (so no date, time, name or id), no key named like a coordinate, name, id, date, time, hour or night
    index, no list that is not a whitelisted histogram (so no per-night list) and, when ``positions`` (a set of (x, y)
    pairs) is given, no pair of numbers equal to a stay position."""
    out: list = []
    try:
        obj = json.loads(obj_or_text) if isinstance(obj_or_text, (str, bytes)) else obj_or_text
    except ValueError:
        return ["not JSON"]
    pos = set(positions) if positions else ()
    if isinstance(obj, dict) and set(obj) == {"v", "n", "sum", "last"}:
        if not isinstance(obj["n"], int) or isinstance(obj["n"], bool):
            out.append("pooled: n is not an int")
        for part, keys in (("sum", SUM_KEYS), ("last", LAST_KEYS)):
            sec = obj[part]
            if not isinstance(sec, dict):
                out.append("pooled: %s is not an object" % part)
                continue
            for k in sec:
                if k not in keys:
                    out.append("pooled: key %r is not a %s key" % (k, part))
            _scan_line(sec, out, "pooled." + part, pos)
        return out
    _scan_line(obj, out, "line", pos)
    return out


# ---------------------------------------------------------------- the answer counters (zone7.ac)
def record_answer(L, kind: str, op: str, obj: Optional[dict] = None) -> None:
    """Increments ``zone7.ac`` for an answered P7-relevant item: ``kind`` ``place``, ``merge``, ``modify`` (counts
    approve, reject, rename, later), ``retire``, ``radar`` (approve, reject, later). For ``place`` ``obj`` is the
    suggestion: its six variables go into the accepted (approve, rename) or rejected (reject) histogram of ``gh``. For
    ``modify`` and ``retire`` ``obj`` may carry the ``ratio`` at the moment of the answer: an approved modify, a rejected
    modify, a removed retire and a kept retire each have a histogram in ``rh``."""
    ac = L.zone7.ac
    names = OP_KINDS.get(kind)
    if names is None or op not in names:
        return
    _bump(ac["op"][kind], names.index(op))
    if kind == "place" and obj is not None and op in ("approve", "reject", "rename"):
        side = 1 if op == "reject" else 0
        ev = obj.get("evidence") or {}
        for var, src in _PLACE_VAR_SRC.items():
            v = obj.get("conf") if src is None else ev.get(src)
            if v is None:
                continue
            _bump(ac["gh"][var][side], bin_of(var, v))
    elif kind in ("modify", "retire") and obj is not None and obj.get("ratio") is not None:
        key = {("modify", "approve"): "mod_y", ("modify", "reject"): "mod_r", ("retire", "approve"): "ret_rm",
               ("retire", "reject"): "ret_kp"}.get((kind, op))
        if key is not None:
            _bump(ac["rh"][key], bin_of("dr", obj["ratio"]))
