"""State compatibility in both directions (check P5-R2, P5 spec 11.2, D2).

    python -I -B replay/compat_check.py --base BASELINE_TREE --out DIR        (the check)
    python -I -B replay/compat_check.py --load-only --tree TREE --state FILE  (what the check runs in a baseline process)

Both trees contain a package named ``radar_zone_learner``, so each runs in its own process (``replay/dump_run.py`` and the
``--load-only`` mode of this script) and the processes exchange JSON files; this process imports the P5 tree only.

(a) **A 0.4.0 file loads in P5.** ``dump_run.py --tree BASELINE`` writes the final state of ``bed_w0000_0600_p4app``
(16 nights, seed 1). The P5 ``Learner(params, time_model, state)`` loads it without ``StateError``; ``suggestions()`` (ids,
types, cells, rev) equals the suggestions of the state; every zone record has the defaults of P5 spec 9.3 (``rev`` 0, ``upd``
the night, ``also`` empty, a polygon of at most 12 vertices, ``srev`` the rev of its suggestion); ``export_state()`` equals
the loaded state apart from the P5 additions (``compare_runs.normalise_state``); the learner survives three simulated days of
``tick`` calls (three nightly runs, no frames) and a ``rename`` answer on a zone with no error and unchanged suggestion ids.
(b) **A P5 file loads in 0.4.0.** ``dump_run.py --tree <this tree>`` writes the final state of ``bed_w0000_0600_p5zone``
(16 nights, seed 1); ``--load-only --tree BASELINE`` loads it with the baseline ``Learner`` and prints its suggestion ids,
label ids and zone records, which this process compares with the state: nothing is lost except the P5 additions (``p5``
and the zone-record extras).

Exit code 0 when both pass, 1 when one fails or could not be exercised, 2 on a usage error. Results go to ``DIR/compat.json``.
"""
import sys
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]
BASE_MS = 1767571200000                 # 2026-01-05 00:00 UTC (replay.framegen.BASE_MS)
DAY_MS = 86400 * 1000
TICK_MS = 60_000
IDLE_DAYS = 3
ZONE_P4_KEYS = ("zid", "name", "kind", "src", "ref", "cx", "cy", "cells", "night", "created_s")


# ---------------------------------------------------------------- the baseline process
def load_only(tree: Path, state_path: Path) -> dict:
    """Loads a state with the ``Learner`` of ``tree`` (a baseline tree) and returns what it holds."""
    import json
    sys.path.insert(0, str(tree))
    from radar_zone_learner.core.learner import Learner
    from radar_zone_learner.core.params import DEFAULT_PARAMS
    from radar_zone_learner.core.types import FixedOffsetTime
    state = json.loads(state_path.read_text(encoding="utf-8"))
    try:
        L = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), state=state)
    except Exception as exc:                      # the baseline refusing the file is the finding
        return {"loaded": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    out = L.export_state()
    return {"loaded": True, "suggestions": [s.id for s in L.suggestions()], "labels": [lb.id for lb in L.labels()],
            "zones": [{k: z.get(k) for k in ZONE_P4_KEYS} for z in out.get("zones", [])],
            "p5_kept": "p5" in out, "writer": out.get("writer")}


# ---------------------------------------------------------------- the P5 process
def _p5_learner(state: dict):
    from radar_zone_learner.core.learner import Learner
    from radar_zone_learner.core.params import DEFAULT_PARAMS
    from radar_zone_learner.core.types import FixedOffsetTime
    return Learner(DEFAULT_PARAMS, FixedOffsetTime(0), state=state)


def _sug_key(s: dict) -> tuple:
    return (s["id"], s["type"], s["rev"], tuple(map(tuple, s["cells"])))


def idle_start_ms(state: dict) -> int:
    """03:31 of the date of the state's last nightly run (UTC: the replay's time model), in epoch ms."""
    import datetime
    d = datetime.date.fromisoformat(state["meta"]["last_extraction_date"])
    return (d.toordinal() - 719163) * DAY_MS + (3 * 3600 + 31 * 60) * 1000


def check_p4_state_in_p5(state: dict, days: int = IDLE_DAYS) -> dict:
    """Part (a) on a loaded dict: returns ``{"status": PASS|FAIL|INCOMPLETE, "problems": [...], ...}``."""
    import copy
    import json
    from radar_zone_learner.core.types import NightlyDone, NightlyFailed, ReviewAnswer
    from replay.compare_runs import diff, normalise_state
    problems, notes = [], {}
    state = copy.deepcopy(state)
    try:
        L = _p5_learner(copy.deepcopy(state))
    except Exception as exc:
        return {"status": "FAIL", "problems": ["P5 refused the file: %s: %s" % (type(exc).__name__, exc)], "notes": {}}
    # suggestions equal
    from radar_zone_learner.core.grids import rle_decode
    want = sorted((s["id"], s["type"], s["rev"], tuple(sorted(rle_decode(s["cells"])))) for s in state["suggestions"])
    got = sorted((s.id, s.type, s.rev, tuple(sorted(s.cells))) for s in L.suggestions())
    if want != got:
        problems.append("suggestions differ: %d in the file, %d loaded" % (len(want), len(got)))
    # zone defaults (spec 9.3)
    exp = L.export_state()
    by_id = {s["id"]: s for s in exp["suggestions"]}
    zones = exp.get("zones", [])
    for z in zones:
        sug = by_id.get(z["ref"]) if z.get("ref") else None
        want_srev = sug["rev"] if sug is not None else 0
        if z.get("rev") != 0 or z.get("upd") != z.get("night") or z.get("also") != [] or z.get("srev") != want_srev \
                or not isinstance(z.get("poly_cm"), list) or not 0 < len(z["poly_cm"]) <= 12:
            problems.append("zone %s has not the defaults of spec 9.3: %s" % (z.get("zid"), {
                k: z.get(k) for k in ("rev", "upd", "night", "also", "srev")} | {"poly": len(z.get("poly_cm") or [])}))
    notes["zones"] = len(zones)
    # the export equals the file apart from the P5 additions
    d = diff(normalise_state(exp), normalise_state(state), "")
    if d:
        problems.append("export_state differs from the loaded state: %s" % d[:4])
    # three idle days and a rename
    now = idle_start_ms(state)
    done = failed = 0
    errors = []
    for k in range(days * 86400 * 1000 // TICK_MS):
        now += TICK_MS
        try:
            for o in L.tick(now):
                done += isinstance(o, NightlyDone)
                failed += isinstance(o, NightlyFailed)
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
            break
    notes.update(nightly_runs=done, nightly_failed=failed)
    if errors or failed:
        problems.append("the idle ticks failed: %s" % (errors or "%d nightly runs failed" % failed))
    if done != days:
        problems.append("%d nightly runs in %d idle days (expected %d)" % (done, days, days))
    if sorted(s.id for s in L.suggestions()) != sorted(s["id"] for s in state["suggestions"]):
        problems.append("the suggestion ids changed during the idle days")
    renamed = None
    if zones:
        z = zones[0]
        try:
            outs = L.handle(ReviewAnswer(z["zid"], "rename", None, False, now + 1000, name="Renamed"))
            now_zone = next((x for x in L.export_state()["zones"] if x["zid"] == z["zid"]), None)
            renamed = bool(now_zone and now_zone["name"] == "Renamed" and now_zone["rev"] == 1)
            if not renamed:
                problems.append("the rename answer did not rename the zone: %s" % [type(o).__name__ for o in outs])
            json.dumps(L.export_state())
        except Exception as exc:
            problems.append("the rename answer raised %s: %s" % (type(exc).__name__, exc))
    notes["renamed"] = renamed
    status = "FAIL" if problems else ("PASS" if zones else "INCOMPLETE")
    if status == "INCOMPLETE":
        problems.append("not exercised: the state has no zone record (the rename was not tried)")
    return {"status": status, "problems": problems, "notes": notes}


def check_p5_state_in_baseline(state: dict, loaded: dict) -> dict:
    """Part (b): ``loaded`` is what ``load_only`` printed for the baseline; ``state`` is the P5 file."""
    from radar_zone_learner.core.grids import rle_decode
    problems = []
    if not loaded.get("loaded"):
        return {"status": "FAIL", "problems": ["the baseline refused the P5 file: %s" % loaded.get("error")], "notes": {}}
    if sorted(loaded["suggestions"]) != sorted(s["id"] for s in state["suggestions"]):
        problems.append("suggestion ids differ")
    if sorted(loaded["labels"]) != sorted(lb["id"] for lb in state["labels"]):
        problems.append("label ids differ")
    got = {z["zid"]: z for z in loaded["zones"]}
    if sorted(got) != sorted(z["zid"] for z in state["zones"]):
        problems.append("zone ids differ: %s against %s" % (sorted(got), sorted(z["zid"] for z in state["zones"])))
    for z in state["zones"]:
        g = got.get(z["zid"])
        if g is None:
            continue
        for k in ZONE_P4_KEYS:
            a, b = z.get(k), g.get(k)
            if k == "cells":
                a, b = sorted(rle_decode(a)), sorted(rle_decode(b))
            if a != b:
                problems.append("zone %s: %s differs" % (z["zid"], k))
    notes = {"zones": len(state["zones"]), "labels": len(state["labels"]), "suggestions": len(state["suggestions"]),
             "p5_in_file": "p5" in state, "baseline_kept_p5": loaded.get("p5_kept")}
    status = "FAIL" if problems else ("PASS" if state["zones"] else "INCOMPLETE")
    if status == "INCOMPLETE":
        problems.append("not exercised: the P5 state has no zone record")
    return {"status": status, "problems": problems, "notes": notes}


def _run(cmd: list) -> tuple:
    import subprocess
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="compat_check.py", allow_abbrev=False,
                                 description="P5-R2: state compatibility between the baseline and the P5 code.")
    ap.add_argument("--base", help="the baseline add-on folder (git archive 58804fa ha-addon, extracted)")
    ap.add_argument("--out", help="output folder")
    ap.add_argument("--load-only", action="store_true", help="child mode: load --state with the --tree's Learner")
    ap.add_argument("--tree")
    ap.add_argument("--state")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.load_only:
        if not args.tree or not args.state:
            print("usage error: --load-only needs --tree and --state", file=sys.stderr)
            return 2
        print(json.dumps(load_only(Path(args.tree), Path(args.state)), separators=(",", ":")))
        return 0
    if not args.base or not args.out or not (Path(args.base) / "replay" / "driver.py").is_file():
        print("usage error: --base (a baseline add-on folder) and --out are required", file=sys.stderr)
        return 2
    sys.path.insert(0, str(ADDON))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dump = str(ADDON / "replay" / "dump_run.py")
    py = [sys.executable, "-I", "-B"]
    result = {}
    # (a) the baseline's state in P5
    job_a = "bed_w0000_0600_p4app"
    rc, so, se = _run(py + [dump, "--tree", args.base, "--jobs", job_a, "--seeds", "1", "--nights", "16",
                            "--out", str(out / "a_base")])
    if rc != 0:
        result["a"] = {"status": "FAIL", "problems": ["the baseline run failed (exit %d): %s" % (rc, (se or so)[-400:])]}
    else:
        state = json.loads((out / "a_base" / ("%s_s1.json" % job_a)).read_text(encoding="utf-8"))["final_state"]
        result["a"] = check_p4_state_in_p5(state)
    # (b) the P5 state in the baseline
    job_b = "bed_w0000_0600_p5zone"
    rc, so, se = _run(py + [dump, "--tree", str(ADDON), "--jobs", job_b, "--seeds", "1", "--nights", "16",
                            "--out", str(out / "b_p5")])
    if rc != 0:
        result["b"] = {"status": "FAIL", "problems": ["the P5 run failed (exit %d): %s" % (rc, (se or so)[-400:])]}
    else:
        path = out / "b_p5" / ("%s_s1.json" % job_b)
        state = json.loads(path.read_text(encoding="utf-8"))["final_state"]
        (out / "b_state.json").write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        rc, so, se = _run(py + [str(Path(__file__).resolve()), "--load-only", "--tree", args.base,
                                "--state", str(out / "b_state.json")])
        if rc != 0:
            result["b"] = {"status": "FAIL", "problems": ["the baseline process failed (exit %d): %s" % (rc, (se or so)[-400:])]}
        else:
            result["b"] = check_p5_state_in_baseline(state, json.loads(so.strip().splitlines()[-1]))
    sts = [result[k]["status"] for k in ("a", "b")]
    result["status"] = "PASS" if sts == ["PASS", "PASS"] else ("FAIL" if "FAIL" in sts else "INCOMPLETE")
    (out / "compat.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    for k, label in (("a", "(a) the 0.4.0 file in P5"), ("b", "(b) the P5 file in 0.4.0")):
        print("%-10s %s" % (result[k]["status"], label))
        for pr in result[k]["problems"]:
            print("           " + pr)
    print("P5-R2: %s" % result["status"])
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
