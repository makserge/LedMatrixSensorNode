"""Diffs two dump folders (``replay/dump_run.py``) on the explicit field list of check P5-R1 (P5 spec 11.2), P6-R1 (P6 spec 11.2: the
same field list, the P5 jobs on the 0.5.0 baseline and on the P6 tree) and P7-R1 (P7 spec 9.1, 8.5 (7): the extended field list).

    python -I -B replay/compare_runs.py --a DIR_BASELINE --b DIR_NEW [--p7on]

Every ``<job>_s<seed>.json`` of the two folders is compared (a run that exists in one folder only is a FAIL). A file is a dump
(``{"result": ..., "final_state": ...}``) or a bare run result (a ``runs/<job>_s<seed>.json`` of ``run_replay.py``: ``final_state`` is then
compared only when both sides have it). Compared:

* from ``result``: ``nights[*].suggestions``, ``nights[*].candidates``, ``nights[*].labels``, ``nights[*].counters``,
  ``labels``, ``counters`` and ``status``;
* P7-R1: from every night record also the review items, the push request, the zone records, the Advanced ids, the suppressions, the
  status values and the ``p5`` section of the night (``NIGHT_PATHS``: ``p5.items``, ``p5.push_request``, ``p5.zones``, ``p5.advanced``,
  ``p5.suppressions``, ``p5.status``, ``p5.p5``; today these were exempt wholesale) and, for a P6 job, ``p6.zone6`` and ``p6.zones``
  (``NIGHT_PATHS_P6``); a path that neither side has is skipped;
* ``final_state`` as a whole, after the keys that P5 may differ in are removed from both sides (``EXEMPT``): the top-level
  keys ``p5``, ``phase`` and ``writer``, the zone-record extras ``poly_cm``, ``rev``, ``upd``, ``srev`` and ``also``, and the
  key ``he_share`` inside the ``evidence`` of ``ignore_zone`` suggestions (P5 spec 7.4).

The named exemptions of P7-R1 (spec 8.5 (7)): the status keys of spec 7.2 (``P7_STATUS_KEYS``), the counters ``zone7_pruned``,
``zone7_reset`` (every counter that starts with ``zone`` is ignored, P6-R1) and ``p7_errors``, and ``PushRequest.attention`` (the push
record of the replay has the seven fields of P5 only). With ``P7Params(enabled=False)`` they do not exist, so any other difference is a FAIL.

``--p7on`` (P7-R1 (b), ``compare_run_p7``): the B side ran with ``P7Params(enabled=True)`` (``dump_run.py --p7on``). The learning fields
(suggestions, candidates, labels, counters of every night, and the grids, stays, door and ghost events, covis, coverage of the final
state) must be identical **up to the first night with a modify, retire or radar-moved item** (after the first answer gate 7 withholds
old-spot candidates by design, DV9; later differences are listed as 'informational'); the review items and the push requests may
differ only in the named categories (the tier-2 order, a plain place withheld by probation or a moved-from mask, a modify, merge,
retire or radar-moved item, and the push request, the status counts and the tag counter that follow those, and the ``p5`` section that a
learner exports with its defaults as soon as ``zone7`` holds data, P7 spec 5.1), which the tool lists with the night. An unnamed difference
is a FAIL.

P6-R1: the ``counters`` of a run and of a night are compared without their keys that start with ``zone`` (the learner's
new zone counters, for example ``zone6_pruned``: ``Learner.counters()`` is ``dict(obs.counters)``, so a new key would
otherwise be 'only in B'); ``zone6`` sits inside ``p5``, which is exempt in the final state. The tracker's additive attributes and the zone
evaluator change no compared field.

Timing, stream statistics and call counts are not compared. PASS: identical in all runs. A difference is a change of
the learning that no spec section allows. Exit code 0 when every run passes, 1 on a difference or a missing run, 2 on a
usage error.
"""
import copy
import json
import sys
from pathlib import Path

RESULT_FIELDS = ("labels", "counters", "status")
NIGHT_FIELDS = ("suggestions", "candidates", "labels", "counters")
NIGHT_PATHS = (("p5", "items"), ("p5", "push_request"), ("p5", "zones"), ("p5", "advanced"), ("p5", "suppressions"), ("p5", "status"),
               ("p5", "p5"))                     # P7-R1: the review, the push, the zone records of a night
NIGHT_PATHS_P6 = (("p6", "zone6"), ("p6", "zones"))          # P7-R1: for a P6 job, the zone evaluator's data and the zones it knew
EXEMPT_TOP = ("p5", "phase", "writer")
EXEMPT_ZONE = ("poly_cm", "rev", "upd", "srev", "also")
IGNORED_COUNTER_PREFIX = "zone"      # P6-R1: the zone counters of the P6 learner are new keys, not a change
IGNORED_COUNTERS = ("p7_errors",)    # P7-R1: the P7 learner's own counter (only present while enabled)
P7_STATUS_KEYS = ("zones_frozen", "zones_blind", "zones_falling", "zones_unused", "zones_recheck", "moved_from", "radar_moved",
                  "places_probation", "places_recheck_held")
MAX_DIFFS = 12

PLAIN_TYPES = ("dwell_zone", "short_task_zone")
P7_ITEM_TYPES = ("modify_zone", "merge_into", "retire_zone", "radar_moved")
CUTOFF_TYPES = ("modify_zone", "retire_zone", "radar_moved")      # the answers after which gate 7 withholds old-spot candidates
FINAL_LEARNING_KEYS = ("grids", "days_bits", "clock", "covis", "coverage", "stays", "door_events", "ghost_events")
LEARNING_NIGHT_FIELDS = ("suggestions", "candidates", "labels", "counters")
FOLLOWING_STATUS_KEYS = ("pending_places", "tier2", "more")        # status values that follow the item list
FOLLOWING_P5_KEYS = ("push", "tags_n")                              # the push bookkeeping and the tag counter follow the item list
EXPORT_P5_KEYS = ("push", "settings", "tags_n")                     # what the ``p5`` section holds when it is exported with defaults only
DEFAULT_PUSH = {"seq": 0, "acked": 0, "kind": "none", "setup_sent": False, "last_day": None, "sig": [], "acks": [], "head_id": None,
                "head_rev": None}
DEFAULT_SETTINGS = {"approve_sched": False, "guests_pause": False, "pets": False, "detail_text": False}


def normalise_state(state: dict) -> dict:
    """A copy of a final state without the keys the P5 code may differ in (see the module docstring)."""
    st = copy.deepcopy(state)
    for k in EXEMPT_TOP:
        st.pop(k, None)
    for z in st.get("zones") or []:
        if isinstance(z, dict):
            for k in EXEMPT_ZONE:
                z.pop(k, None)
    for s in st.get("suggestions") or []:
        if isinstance(s, dict) and s.get("type") == "ignore_zone" and isinstance(s.get("evidence"), dict):
            s["evidence"].pop("he_share", None)
    return st


def _counters(v):
    """A ``counters`` dict without the keys of ``IGNORED_COUNTER_PREFIX`` and ``IGNORED_COUNTERS`` (anything else is returned as it is)."""
    if isinstance(v, dict):
        return {k: x for k, x in v.items() if not str(k).startswith(IGNORED_COUNTER_PREFIX) and k not in IGNORED_COUNTERS}
    return v


def _status(v):
    """The status values of a night without the P7 keys of spec 7.2 (the named exemptions)."""
    if isinstance(v, dict):
        return {k: x for k, x in v.items() if k not in P7_STATUS_KEYS}
    return v


def p5_exported_for_zone7(qa, qb) -> bool:
    """True when the B side's ``p5`` section of a night is only the defaults that the learner exports because ``zone7`` holds data (P7 spec
    5.1: the ``p5`` section is exported while ``zone7`` is non-empty) and the A side had no ``p5`` data at all (push, settings and the
    tag counter ``None``). This is a named difference of P7-R1 (b), not a change of the learning."""
    qa, qb = qa or {}, qb or {}
    return (all(qa.get(k) is None for k in EXPORT_P5_KEYS) and qb.get("push") == DEFAULT_PUSH and qb.get("settings") == DEFAULT_SETTINGS
            and qb.get("tags_n") in (0, None))


def _path(rec: dict, path: tuple):
    v = rec
    for k in path:
        if not isinstance(v, dict) or k not in v:
            return None
        v = v[k]
    return v


def diff(a, b, path: str = "", out=None, limit: int = MAX_DIFFS) -> list:
    """The paths where two JSON values differ (at most ``limit``, the first ones)."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))
                                       and not isinstance(a, bool) and not isinstance(b, bool)):
        out.append("%s: %s != %s" % (path or "/", _short(a), _short(b)))
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                out.append("%s/%s: only in B" % (path, k))
            elif k not in b:
                out.append("%s/%s: only in A" % (path, k))
            else:
                diff(a[k], b[k], "%s/%s" % (path, k), out, limit)
            if len(out) >= limit:
                break
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append("%s: length %d != %d" % (path or "/", len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, "%s[%d]" % (path, i), out, limit)
            if len(out) >= limit:
                break
    elif a != b:
        out.append("%s: %s != %s" % (path or "/", _short(a), _short(b)))
    return out


def _short(v, n: int = 60) -> str:
    t = json.dumps(v, sort_keys=True, separators=(",", ":")) if isinstance(v, (dict, list)) else repr(v)
    return t if len(t) <= n else t[:n] + "..."


def _split(d: dict) -> tuple:
    """(result, final_state or None) of a dump or of a bare run result."""
    if isinstance(d.get("result"), dict):
        return d["result"], d.get("final_state")
    return d, None


def _is_p6(res: dict) -> bool:
    return str((res.get("job_spec") or {}).get("script") or "").startswith("p6") or any(
        isinstance(n, dict) and "p6" in n for n in (res.get("nights") or [])[:1])


def compare_run(a: dict, b: dict, p7on: bool = False) -> list:
    """The differences of one run: a list of 'field/path: ...' strings (empty: identical on the field list). ``p7on``: see
    ``compare_run_p7`` (the B side ran with P7 enabled; the lists of named differences are dropped here)."""
    if p7on:
        return compare_run_p7(a, b)[0]
    out = []
    ra, fa = _split(a)
    rb, fb = _split(b)
    for k in RESULT_FIELDS:
        va, vb = ra.get(k), rb.get(k)
        if k == "counters":
            va, vb = _counters(va), _counters(vb)
        out += ["result.%s%s" % (k, d) for d in diff(va, vb, "")][:MAX_DIFFS]
    na = {n["night"]: n for n in ra.get("nights") or []}
    nb = {n["night"]: n for n in rb.get("nights") or []}
    if sorted(na) != sorted(nb):
        out.append("result.nights: the nights %s != %s" % (sorted(na)[:3] + ["..."], sorted(nb)[:3] + ["..."]))
    p6job = _is_p6(ra) or _is_p6(rb)
    paths = NIGHT_PATHS + (NIGHT_PATHS_P6 if p6job else ())
    for night in sorted(set(na) & set(nb)):
        for k in NIGHT_FIELDS:
            va, vb = na[night].get(k), nb[night].get(k)
            if k == "counters":
                va, vb = _counters(va), _counters(vb)
            for d in diff(va, vb, ""):
                out.append("result.nights[night %s].%s%s" % (night, k, d))
                if len(out) >= 4 * MAX_DIFFS:
                    return out
        for p in paths:
            va, vb = _path(na[night], p), _path(nb[night], p)
            if va is None and vb is None:
                continue
            if p == ("p5", "status"):
                va, vb = _status(va), _status(vb)
            for d in diff(va, vb, ""):
                out.append("result.nights[night %s].%s%s" % (night, ".".join(p), d))
                if len(out) >= 4 * MAX_DIFFS:
                    return out
    if fa is not None and fb is not None:
        sa, sb = normalise_state(fa), normalise_state(fb)
        out += ["final_state%s" % d for d in diff(sa, sb, "")]
    return out


# ---------------------------------------------------------------- P7-R1 (b): the B side has P7 enabled
def _items_by_id(items) -> dict:
    return {i["id"]: i for i in (items or []) if isinstance(i, dict) and "id" in i}


def classify_items(a_items, b_items) -> tuple:
    """(named, unnamed) differences between two item lists (the A side is the baseline, the B side has P7 enabled): the named categories
    are the tier-2 order, a plain place only in A (withheld by probation, a moved-from mask or consumed by a modify or merge), and a
    modify, merge, retire or radar-moved item only in B; every other difference is unnamed."""
    named, unnamed = [], []
    ia, ib = _items_by_id(a_items), _items_by_id(b_items)
    for k in sorted(set(ia) - set(ib)):
        if ia[k].get("type") in PLAIN_TYPES:
            named.append("a plain place only in A (withheld or consumed): %s" % k)
        else:
            unnamed.append("item %s (%s) only in A" % (k, ia[k].get("type")))
    for k in sorted(set(ib) - set(ia)):
        if ib[k].get("type") in P7_ITEM_TYPES:
            named.append("a %s item only in B: %s" % (ib[k]["type"], k))
        else:
            unnamed.append("item %s (%s) only in B" % (k, ib[k].get("type")))
    for k in sorted(set(ia) & set(ib)):
        if ia[k] != ib[k]:
            unnamed.append("item %s differs: %s" % (k, diff(ia[k], ib[k], "")[:2]))
    common_a = [i["id"] for i in (a_items or []) if i.get("id") in ib]
    common_b = [i["id"] for i in (b_items or []) if i.get("id") in ia]
    if common_a != common_b:
        named.append("the order of the common items differs")
    return named, unnamed


def compare_run_p7(a: dict, b: dict) -> tuple:
    """P7-R1 (b): ``(diffs, named)``. ``diffs`` are the failures (unnamed differences, and a difference of the learning fields up to the
    cut-off), ``named`` the named differences of the item lists and what follows them, each with its night, and the informational
    differences of the learning fields after the first modify, retire or reset."""
    diffs, named = [], []
    ra, fa = _split(a)
    rb, fb = _split(b)
    na = {n["night"]: n for n in ra.get("nights") or []}
    nb = {n["night"]: n for n in rb.get("nights") or []}
    if sorted(na) != sorted(nb):
        diffs.append("result.nights: the nights %s != %s" % (sorted(na)[:3] + ["..."], sorted(nb)[:3] + ["..."]))
    if ra.get("status") != rb.get("status"):
        diffs.append("result.status: %r != %r" % (ra.get("status"), rb.get("status")))
    nights = sorted(set(na) & set(nb))
    cutoff = next((n for n in nights if any(isinstance(i, dict) and i.get("type") in CUTOFF_TYPES
                                            for i in (_path(nb[n], ("p5", "items")) or []))), None)
    seen = False
    for night in nights:
        late = cutoff is not None and night > cutoff
        for k in LEARNING_NIGHT_FIELDS:
            va, vb = na[night].get(k), nb[night].get(k)
            if k == "counters":
                va, vb = _counters(va), _counters(vb)
            d = diff(va, vb, "")
            if d:
                (named if late else diffs).append(("informational (after the first answer): " if late else "") +
                                                  "night %s %s%s" % (night, k, d[0]))
        ia, ib = _path(na[night], ("p5", "items")), _path(nb[night], ("p5", "items"))
        if ia is not None or ib is not None:
            nm, un = classify_items(ia, ib)
            seen = seen or bool(nm)
            named += ["night %s: %s" % (night, t) for t in nm]
            diffs += ["night %s: %s" % (night, t) for t in un]
        pa, pb = _path(na[night], ("p5", "push_request")), _path(nb[night], ("p5", "push_request"))
        if pa != pb:
            if seen:
                named.append("night %s: the push request follows the item list" % night)
            else:
                diffs.append("night %s: push_request %s" % (night, diff(pa, pb, "")[:2]))
        sa, sb = _status(_path(na[night], ("p5", "status"))), _status(_path(nb[night], ("p5", "status")))
        if sa != sb:
            keys = [k for k in set(sa or {}) | set(sb or {}) if (sa or {}).get(k) != (sb or {}).get(k)]
            bad = [k for k in keys if k not in FOLLOWING_STATUS_KEYS or not seen]
            if bad:
                diffs.append("night %s: status %s" % (night, [(k, (sa or {}).get(k), (sb or {}).get(k)) for k in bad][:3]))
        qa, qb = _path(na[night], ("p5", "p5")), _path(nb[night], ("p5", "p5"))
        if qa != qb:
            keys = [k for k in set(qa or {}) | set(qb or {}) if (qa or {}).get(k) != (qb or {}).get(k)]
            export_only = p5_exported_for_zone7(qa, qb)
            if export_only and not any("because zone7 holds data" in t for t in named):
                named.append("night %s: the p5 section is exported with its defaults because zone7 holds data (P7 spec 5.1)" % night)
            bad = [k for k in keys if not (export_only and k in EXPORT_P5_KEYS) and (k not in FOLLOWING_P5_KEYS or not seen)]
            if bad:
                diffs.append("night %s: the p5 section differs in %s" % (night, bad[:4]))
        for p in (("p5", "zones"), ("p5", "advanced"), ("p5", "suppressions")) + (NIGHT_PATHS_P6 if _is_p6(ra) else ()):
            va, vb = _path(na[night], p), _path(nb[night], p)
            if (va is not None or vb is not None) and diff(va, vb, ""):
                if late and p[0] == "p5" and p[1] == "suppressions":
                    named.append("informational (after the first answer): night %s suppressions" % night)
                else:
                    diffs.append("night %s: %s %s" % (night, ".".join(p), diff(va, vb, "")[:2]))
    if fa is not None and fb is not None:
        for k in FINAL_LEARNING_KEYS:
            if k in fa or k in fb:
                d = diff(fa.get(k), fb.get(k), "")
                if d:
                    diffs.append("final_state/%s%s" % (k, d[0]))
    return diffs[:4 * MAX_DIFFS], named[:6 * MAX_DIFFS]


def compare_dirs(dir_a: Path, dir_b: Path, p7on: bool = False) -> dict:
    """``{"runs": {name: {"status": PASS|FAIL, "diffs": [...]}}, "status": PASS|FAIL}`` for two dump folders (``p7on``: the B side ran
    with P7 enabled; each run then also has the ``named`` differences)."""
    fa = {p.name: p for p in Path(dir_a).glob("*_s*.json")}
    fb = {p.name: p for p in Path(dir_b).glob("*_s*.json")}
    runs = {}
    for name in sorted(set(fa) | set(fb)):
        if name not in fa or name not in fb:
            runs[name] = {"status": "FAIL", "diffs": ["the run exists in %s only" % ("A" if name in fa else "B")]}
            continue
        a = json.loads(fa[name].read_text(encoding="utf-8"))
        b = json.loads(fb[name].read_text(encoding="utf-8"))
        if p7on:
            d, nm = compare_run_p7(a, b)
            runs[name] = {"status": "FAIL" if d else "PASS", "diffs": d[:2 * MAX_DIFFS], "named": nm}
        else:
            d = compare_run(a, b)
            runs[name] = {"status": "FAIL" if d else "PASS", "diffs": d[:2 * MAX_DIFFS]}
    status = "PASS" if runs and all(r["status"] == "PASS" for r in runs.values()) else "FAIL"
    return {"runs": runs, "status": status}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="compare_runs.py", allow_abbrev=False,
                                 description="Compare two dump folders on the field list of P5-R1 / P6-R1 / P7-R1.")
    ap.add_argument("--a", required=True, help="the baseline's dump folder")
    ap.add_argument("--b", required=True, help="the new code's dump folder (P5, P6 or P7 tree)")
    ap.add_argument("--p7on", action="store_true", help="P7-R1 (b): the B side ran with P7Params(enabled=True) (dump_run.py --p7on)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if not Path(args.a).is_dir() or not Path(args.b).is_dir():
        print("usage error: --a and --b must be folders", file=sys.stderr)
        return 2
    res = compare_dirs(Path(args.a), Path(args.b), p7on=args.p7on)
    for name, r in res["runs"].items():
        print("%-4s %s" % (r["status"], name))
        for d in r["diffs"]:
            print("       " + d)
        for d in r.get("named", []):
            print("       (named) " + d)
    print("%s: %s (%d runs)" % ("P7-R1 (b)" if args.p7on else "P5-R1", res["status"], len(res["runs"])))   # the same list serves P6-R1, P7-R1 (a)
    return 0 if res["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
