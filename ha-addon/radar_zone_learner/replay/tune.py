"""The PC-side proposer of the threshold re-tuning (P7 spec 4.8, D14; standard library only).

    python -I -B replay/tune.py --aggregate POOLED.json [--json]

``POOLED.json`` is the pooled record that the add-on publishes on the button ``Export tuning data`` (``core.tuningagg.pool``):
``{"v": 1, "n": <nights pooled>, "sum": {...}, "last": {...}}``, counts and fixed-length histograms only. This script reads that record
and nothing else (no state, no position, no date, no per-night series exist in it) and prints one row per constant of the table of
spec 4.8: a **proposal** (a value for the owner, applied only by a release: nothing is tuned at runtime) or the **reason** there is
none. Class ``A`` = automatic at runtime (it exists already), ``P`` = a proposal printed here, ``N`` = never changed from data alone
(or not computed).

The rules (a PROPOSAL of the spec, where ``CD`` gives no fitting rule):

* ``Params.bridge_gap_s``: the upper edge of the bin of the hidden-spell histogram ``hs`` that holds the 95th percentile, rounded up to
  60 s, at least 300 s; no proposal below ``MIN_SPELLS`` (20) spells.
* ``P6Params.delay_default_s`` per kind: the upper edge of the bin of the learned clear delays ``dl[kind]`` that holds the median, if at
  least ``MIN_DELAY_ZONE_NIGHTS`` (3) zone-nights of that kind were pooled.
* ``Params.conf_min``, ``min_core_days``, ``dwell_min_stays`` (answers ``ac.gh``): **raise** one step when the bin just above the
  threshold holds at least ``RAISE_REJECT_SHARE`` (30 %) rejections over at least ``MIN_BIN_ANSWERS`` (10) answers; **lower** one step
  only on the label evidence: at least ``MIN_LABEL_NIGHTS`` (3) unmatched label-nights in the 14-day or older age bins (``la``) and this
  gate is the most frequent single failing gate (``nm``) of the pooled candidates. A threshold below the current one can never be
  observed in the answers (a candidate under it is never offered), which is why the lowering needs the labels.
* ``ghost_share_max``, ``covis_thr``, ``ld_share_min``: the bin edge that best separates the accepted and the rejected histograms, if each
  group holds at least ``MIN_GROUP_ANSWERS`` (5) answers; else none.
* ``margin_cm``: not computed (only the per-frame evaluator sees the flicker at the zone edge); the P7 ratios and the probation span:
  no automatic rule (the distributions are printed); the night window and ``night_lux``: learned by P4 (automatic).

Fewer than ``MIN_COVERED_DATES`` (14) covered dates (``last.cd``): no proposal at all, and the report says why (``CD:phases/P7``: 2-4
weeks). The bin edges below are the constants of ``core.tuningagg.BINS`` (``tests/test_tune.py`` checks that they agree).
"""
from __future__ import annotations

import json
import sys
from bisect import bisect_right
from pathlib import Path

MIN_COVERED_DATES = 14
MIN_SPELLS = 20
MIN_DELAY_ZONE_NIGHTS = 3
MIN_BIN_ANSWERS = 10
RAISE_REJECT_SHARE = 0.30
MIN_LABEL_NIGHTS = 3
MIN_GROUP_ANSWERS = 5
BRIDGE_MIN_S = 300
ROUND_S = 60
KINDS = ("dwell", "short_task", "bed", "manual")

# lower edges of the bins (the first bin is everything below the first edge): ``core.tuningagg.BINS``
BINS = {
    "hs": (10, 20, 40, 80, 160, 320, 640, 1280, 2560),
    "dl": (10, 20, 40, 80, 160),
    "n": (6, 9, 12, 18, 30),
    "cd": (2, 3, 4, 5, 7),
    "ld": (0.3, 0.5, 0.7, 0.85, 0.95),
    "gs": (0.05, 0.1, 0.2, 0.3, 0.5),
    "cv": (0.2, 0.4, 0.6, 0.8, 0.9),
    "cf": (0.3, 0.4, 0.5, 0.6, 0.8),
    "la": (7, 14, 28),
}
OPEN_BIN_FACTOR = {"hs": 2.0}                  # the open top bin of the hidden spells has no upper edge: twice the last edge
OPEN_BIN_VALUE = {"dl": 600}                   # the learned clear delay is clamped to 600 s (P6Params.tune_delay_max_s)
GATE_INDEX = {"dwell_min_stays": 0, "min_core_days": 1, "ghost_share_max": 2, "ld_share_min": 2, "covis_thr": 3, "conf_min": 7}
GATE_NAMES = ("g1_class", "g2_core_days", "g3_ld_ghost", "g4_covis", "g5_stable", "g6_move", "g7_suppression", "conf")

# the constants as shipped (0.7.0) and the histogram that measures each; ``kind`` max: a value above it is rejected, min: below
CURRENT = {"bridge_gap_s": 900, "delay_default_s": {"bed": 600, "dwell": 300, "short_task": 60, "manual": None},
           "conf_min": 0.5, "min_core_days": 4, "dwell_min_stays": 6, "ghost_share_max": 0.2, "covis_thr": 0.8, "ld_share_min": 0.5}
GATE_VARS = {"conf_min": ("cf", "conf_min", "min"), "min_core_days": ("cd", "min_core_days", "min"),
             "dwell_min_stays": ("n", "dwell_min_stays", "min")}
SEPARATE_VARS = {"ghost_share_max": ("gs", "max"), "covis_thr": ("cv", "max"), "ld_share_min": ("ld", "min")}
LOWER_STEP = {"cf": 0.4, "cd": 3, "n": 5}
KEYS_NEEDED = ("hs", "hn", "dl", "nm", "la")


# ---------------------------------------------------------------- small helpers
def bin_of(key: str, value) -> int:
    return bisect_right(BINS[key], float(value))


def upper_edge(key: str, i: int) -> float:
    """The upper edge of bin ``i`` of the histogram ``key`` (the open top bin: see ``OPEN_BIN_FACTOR`` / ``OPEN_BIN_VALUE``)."""
    edges = BINS[key]
    if i < len(edges):
        return edges[i]
    if key in OPEN_BIN_VALUE:
        return OPEN_BIN_VALUE[key]
    return edges[-1] * OPEN_BIN_FACTOR.get(key, 2.0)


def quantile_bin(hist: list, q: float):
    """The index of the bin that holds the ``q`` quantile of a histogram (the smallest bin whose cumulative share reaches ``q``), or
    None for an empty one."""
    total = sum(hist)
    if total <= 0:
        return None
    acc = 0
    for i, c in enumerate(hist):
        acc += c
        if acc / total >= q - 1e-12:
            return i
    return len(hist) - 1


def round_up(x: float, step: int) -> int:
    return int(-(-x // step) * step)


def _row(key: str, cls: str, current, measurement: str, proposal=None, reason: str = "", note: str = "") -> dict:
    return {"key": key, "class": cls, "current": current, "proposal": proposal, "reason": reason, "measurement": measurement,
            "note": note}


def _get(d, *path, default=None):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


# ---------------------------------------------------------------- the rows
def row_bridge(sums: dict) -> dict:
    hs, hn = sums.get("hs"), sums.get("hn")
    meas = "hs, hn"
    if not isinstance(hs, list) or hn is None:
        return _row("bridge_gap_s", "P", CURRENT["bridge_gap_s"], meas, reason="the pooled record has no hidden-spell histogram")
    n = sum(hs)
    if n < MIN_SPELLS:
        return _row("bridge_gap_s", "P", CURRENT["bridge_gap_s"], meas, reason="%d spells pooled, fewer than %d" % (n, MIN_SPELLS))
    i = quantile_bin(hs, 0.95)
    edge = upper_edge("hs", i)
    value = max(BRIDGE_MIN_S, round_up(edge, ROUND_S))
    note = "the q95 is in bin %d (upper edge %s s%s) of %d spells" % (i, int(edge), ", the open top bin" if i >= len(BINS["hs"]) else "", n)
    return _row("bridge_gap_s", "P", CURRENT["bridge_gap_s"], meas, proposal=value, note=note)


def row_delay(sums: dict) -> dict:
    dl = sums.get("dl")
    meas = "dl[kind]"
    if not isinstance(dl, dict):
        return _row("delay_default_s", "P", CURRENT["delay_default_s"], meas, reason="the pooled record has no learned-delay histogram")
    prop, why = {}, []
    for kind in KINDS:
        h = dl.get(kind)
        n = sum(h) if isinstance(h, list) else 0
        if n < MIN_DELAY_ZONE_NIGHTS:
            why.append("%s: %d zone-nights, fewer than %d" % (kind, n, MIN_DELAY_ZONE_NIGHTS))
            continue
        prop[kind] = int(upper_edge("dl", quantile_bin(h, 0.5)))
    return _row("delay_default_s", "P", CURRENT["delay_default_s"], meas, proposal=prop or None, reason="; ".join(why))


def _gate_rows(sums: dict, last: dict) -> list:
    gh = _get(last, "ac", "gh", default={})
    nm = sums.get("nm") or [0] * 8
    la = sums.get("la") or [0] * 4
    out = []
    for key, (var, cur_key, _) in GATE_VARS.items():
        cur = CURRENT[cur_key]
        meas = "ac.gh[%s], la, nm" % var
        h = gh.get(var)
        if not (isinstance(h, list) and len(h) == 2):
            out.append(_row(key, "P", cur, meas, reason="the pooled record has no answer histogram for %s" % var))
            continue
        acc, rej = h
        b = bin_of(var, cur)                         # the bin that starts at the threshold: just above it
        a_n, r_n = acc[b], rej[b]
        n = a_n + r_n
        share = r_n / n if n else 0.0
        edges = BINS[var]
        up = edges[b] if b < len(edges) else None    # the next edge above the threshold's bin
        if n >= MIN_BIN_ANSWERS and share >= RAISE_REJECT_SHARE and up is not None:
            out.append(_row(key, "P", cur, meas, proposal=up,
                            note="the bin just above the threshold holds %d of %d answers rejected (%.0f %%)" % (r_n, n, 100 * share)))
            continue
        gi = GATE_INDEX[key]
        unmatched_old = sum(la[2:]) if len(la) >= 4 else 0
        top = max(nm) if nm else 0
        most_frequent = top > 0 and nm[gi] == top and nm.count(top) == 1
        if unmatched_old >= MIN_LABEL_NIGHTS and most_frequent:
            out.append(_row(key, "P", cur, meas, proposal=LOWER_STEP[var],
                            note="%d unmatched label-nights of 14 days or more and %s is the most frequent single failing gate"
                                 % (unmatched_old, GATE_NAMES[gi])))
            continue
        why = []
        if n < MIN_BIN_ANSWERS:
            why.append("the bin just above the threshold holds %d answers, fewer than %d" % (n, MIN_BIN_ANSWERS))
        elif share < RAISE_REJECT_SHARE:
            why.append("%.0f %% of the %d answers in the bin just above the threshold were rejected, under %.0f %%"
                       % (100 * share, n, 100 * RAISE_REJECT_SHARE))
        else:
            why.append("no higher bin edge")
        why.append("lowering needs label evidence (%d unmatched label-nights of 14 days or more, %s %s)" % (
            unmatched_old, GATE_NAMES[gi], "is the most frequent single failing gate" if most_frequent else "is not the most frequent single failing gate"))
        out.append(_row(key, "P", cur, meas, reason="; ".join(why)))
    return out


def _separating_edge(var: str, side: str, acc: list, rej: list):
    """(edge, errors, total) of the bin edge that splits the accepted from the rejected best, or None. ``side`` ``max``: values at or
    above the edge are predicted rejected; ``min``: values below it."""
    best = None
    total = sum(acc) + sum(rej)
    for k, e in enumerate(BINS[var]):
        i = k + 1                                    # the bins from index i up are 'at or above the edge e'
        if side == "max":
            err = sum(acc[i:]) + sum(rej[:i])
        else:
            err = sum(acc[:i]) + sum(rej[i:])
        if best is None or err < best[1]:
            best = (e, err, total)
    return best


def _separate_rows(last: dict) -> list:
    gh = _get(last, "ac", "gh", default={})
    out = []
    for key, (var, side) in SEPARATE_VARS.items():
        cur = CURRENT[key]
        meas = "ac.gh[%s]" % var
        h = gh.get(var)
        if not (isinstance(h, list) and len(h) == 2):
            out.append(_row(key, "P", cur, meas, reason="the pooled record has no answer histogram for %s" % var))
            continue
        acc, rej = h
        if sum(acc) < MIN_GROUP_ANSWERS or sum(rej) < MIN_GROUP_ANSWERS:
            out.append(_row(key, "P", cur, meas, reason="accepted %d and rejected %d answers, each group needs %d"
                                                         % (sum(acc), sum(rej), MIN_GROUP_ANSWERS)))
            continue
        e, err, total = _separating_edge(var, side, acc, rej)
        if err >= min(sum(acc), sum(rej)):
            out.append(_row(key, "P", cur, meas, reason="no bin edge separates the groups better than assigning all to one (%d errors of %d)"
                                                       % (err, total)))
        else:
            out.append(_row(key, "P", cur, meas, proposal=e, note="%d of %d answers on the wrong side of the edge" % (err, total)))
    return out


def propose(pooled: dict) -> dict:
    """The rows of the table of spec 4.8 for a pooled record: ``{"nights", "covered_dates", "ok", "reason", "rows": [...]}``. With
    fewer than 14 covered dates every proposal is withheld (its ``reason`` says why)."""
    if not isinstance(pooled, dict) or not isinstance(pooled.get("sum"), dict) or not isinstance(pooled.get("last"), dict):
        raise ValueError("not a pooled record: it needs the keys sum and last")
    sums, last = pooled["sum"], pooled["last"]
    nights = pooled.get("n")
    cd = last.get("cd")
    rows = [_row("delay_cd", "A", "per zone", "zone6 (P6)", reason="automatic at runtime: each zone's own q95 of its hidden spells (P6)"),
            row_bridge(sums), row_delay(sums),
            _row("margin_cm", "N", 25, "-", reason="not computed: the flicker rate at the zone edge is seen only by the per-frame evaluator "
                                                    "(P6 reports it offline, acceptance A4 fixes the margin on the real edges)")]
    rows += _gate_rows(sums, last)
    rows += _separate_rows(last)
    dist = {"dr": sums.get("dr"), "zs": sums.get("zs"), "zf": sums.get("zf"), "zr": sums.get("zr"),
            "op": _get(last, "ac", "op"), "rh": _get(last, "ac", "rh")}
    rows.append(_row("p7_ratios", "N", "0.6, 0.25, 0.1; probation 10", "dr, zs, ac.rh, ac.op", reason="no automatic rule: the owner reads "
                     "the distributions", note=json.dumps(dist, sort_keys=True, separators=(",", ":"))))
    rows.append(_row("night_window", "A", "P4", "-", reason="learned by P4 (the night window and night_lux): unchanged"))
    ok = isinstance(cd, int) and cd >= MIN_COVERED_DATES
    reason = ""
    if not ok:
        reason = ("%s covered dates in the pooled record, fewer than %d: no proposal is printed (CD:phases/P7: 2-4 weeks of aggregates)"
                  % (cd if cd is not None else "no", MIN_COVERED_DATES))
        for r in rows:
            if r["proposal"] is not None:
                r["withheld"] = r["proposal"]
                r["proposal"] = None
                r["reason"] = reason
    return {"nights": nights, "covered_dates": cd, "ok": ok, "reason": reason, "rows": rows}


# ---------------------------------------------------------------- output
def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, dict):
        return ", ".join("%s %s" % (k, _fmt(x)) for k, x in v.items())
    return str(v)


def render(rep: dict) -> str:
    L = ["Threshold re-tuning proposals (P7 spec 4.8): %s nights pooled, %s covered dates" % (rep["nights"], rep["covered_dates"]), ""]
    if not rep["ok"]:
        L += ["NO PROPOSAL: " + rep["reason"], ""]
    L += ["%-18s %-2s %-26s %-26s %s" % ("constant", "C", "current", "proposal", "reason / note")]
    for r in rep["rows"]:
        L.append("%-18s %-2s %-26s %-26s %s" % (r["key"], r["class"], _fmt(r["current"])[:26], _fmt(r["proposal"])[:26],
                                                  r["reason"] or r["note"]))
        if r["proposal"] is not None and r["note"]:
            L.append("%-18s    %s" % ("", r["note"]))
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="tune.py", allow_abbrev=False,
                                 description="P7 spec 4.8: proposals for the learner's thresholds from the pooled tuning export.")
    ap.add_argument("--aggregate", required=True, help="the pooled record (JSON) that 'Export tuning data' publishes")
    ap.add_argument("--json", action="store_true", help="print the rows as JSON")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        text = Path(args.aggregate).read_text(encoding="utf-8").strip()
        if not text.startswith("{") and "{" in text:             # a copy from mosquitto_sub or the HA tool may carry a topic prefix
            text = text[text.index("{"):]
        pooled = json.loads(text)
        rep = propose(pooled)
    except (OSError, ValueError) as exc:
        print("usage error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=1, sort_keys=True) if args.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
