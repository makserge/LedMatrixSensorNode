"""Diffs two dump folders (``replay/dump_run.py``) on the explicit field list of check P5-R1 (P5 spec 11.2).

    python -I -B replay/compare_runs.py --a DIR_BASELINE --b DIR_P5

Every ``<job>_s<seed>.json`` of the two folders is compared (a run that exists in one folder only is a FAIL). Compared:

* from ``result``: ``nights[*].suggestions``, ``nights[*].candidates``, ``nights[*].labels``, ``nights[*].counters``,
  ``labels``, ``counters`` and ``status``;
* ``final_state`` as a whole, after the keys that P5 may differ in are removed from both sides (``EXEMPT``): the top-level
  keys ``p5``, ``phase`` and ``writer``, the zone-record extras ``poly_cm``, ``rev``, ``upd``, ``srev`` and ``also``, and the
  key ``he_share`` inside the ``evidence`` of ``ignore_zone`` suggestions (P5 spec 7.4).

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
EXEMPT_TOP = ("p5", "phase", "writer")
EXEMPT_ZONE = ("poly_cm", "rev", "upd", "srev", "also")
MAX_DIFFS = 12


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


def compare_run(a: dict, b: dict) -> list:
    """The differences of one run: a list of 'field/path: ...' strings (empty: identical on the field list)."""
    out = []
    ra, rb = a.get("result") or {}, b.get("result") or {}
    for k in RESULT_FIELDS:
        out += ["result.%s%s" % (k, d) for d in diff(ra.get(k), rb.get(k), "")][:MAX_DIFFS]
    na = {n["night"]: n for n in ra.get("nights") or []}
    nb = {n["night"]: n for n in rb.get("nights") or []}
    if sorted(na) != sorted(nb):
        out.append("result.nights: the nights %s != %s" % (sorted(na)[:3] + ["..."], sorted(nb)[:3] + ["..."]))
    for night in sorted(set(na) & set(nb)):
        for k in NIGHT_FIELDS:
            for d in diff(na[night].get(k), nb[night].get(k), ""):
                out.append("result.nights[night %s].%s%s" % (night, k, d))
                if len(out) >= 4 * MAX_DIFFS:
                    return out
    sa, sb = normalise_state(a.get("final_state") or {}), normalise_state(b.get("final_state") or {})
    out += ["final_state%s" % d for d in diff(sa, sb, "")]
    return out


def compare_dirs(dir_a: Path, dir_b: Path) -> dict:
    """``{"runs": {name: {"status": PASS|FAIL, "diffs": [...]}}, "status": PASS|FAIL}`` for two dump folders."""
    fa = {p.name: p for p in Path(dir_a).glob("*_s*.json")}
    fb = {p.name: p for p in Path(dir_b).glob("*_s*.json")}
    runs = {}
    for name in sorted(set(fa) | set(fb)):
        if name not in fa or name not in fb:
            runs[name] = {"status": "FAIL", "diffs": ["the run exists in %s only" % ("A" if name in fa else "B")]}
            continue
        a = json.loads(fa[name].read_text(encoding="utf-8"))
        b = json.loads(fb[name].read_text(encoding="utf-8"))
        d = compare_run(a, b)
        runs[name] = {"status": "FAIL" if d else "PASS", "diffs": d[:2 * MAX_DIFFS]}
    status = "PASS" if runs and all(r["status"] == "PASS" for r in runs.values()) else "FAIL"
    return {"runs": runs, "status": status}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="compare_runs.py", allow_abbrev=False,
                                 description="Compare two dump folders on the field list of P5-R1.")
    ap.add_argument("--a", required=True, help="the baseline's dump folder")
    ap.add_argument("--b", required=True, help="the P5 code's dump folder")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if not Path(args.a).is_dir() or not Path(args.b).is_dir():
        print("usage error: --a and --b must be folders", file=sys.stderr)
        return 2
    res = compare_dirs(Path(args.a), Path(args.b))
    for name, r in res["runs"].items():
        print("%-4s %s" % (r["status"], name))
        for d in r["diffs"]:
            print("       " + d)
    print("P5-R1: %s (%d runs)" % (res["status"], len(res["runs"])))
    return 0 if res["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
