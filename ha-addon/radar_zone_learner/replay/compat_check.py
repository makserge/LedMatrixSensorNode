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

``--p6`` (check P6-R2, P6 spec 11.2, D2): the same two directions between the 0.5.0 baseline (``git archive 91c05e9``) and this tree, and
two state checks in this process:

    python -I -B replay/compat_check.py --p6 --base BASELINE_TREE --out DIR

(a) the final state of ``bed_w0000_0600_p5zone`` (0.5.0, 16 nights, seed 1) loads in 0.6.0, has no ``zone6`` after the export, equals its
export after ``writer`` and ``phase`` (the ``p5`` section included), and the learner survives three idle days and a rename; (b) the
final state of ``bed_w0000_0600_p6hold`` (0.6.0, 14 nights, seed 1) loads in 0.5.0, which keeps ``zone6`` in the ``p5`` extra and writes it
back unchanged (``--load-only`` returns the exported ``p5['zone6']``); (c) corrupt ``zone6`` values give ``StateError`` (``CORRUPT_ZONE6``: a wrong type, a value out of range);
(d) ``zone6`` entries of zones that are not there are pruned and counted (``counters()['zone6_pruned']``). Results go to
``DIR/compat_p6.json``.

``--p7`` (check P7-R2, P7 spec 9.1, D2, D5, D43): the 0.7.0 tree against the older ones:

    python -I -B replay/compat_check.py --p7 --base BASELINE_TREE --out DIR

(a) the final state of ``bed_w0000_0600_p6hold`` run on this tree with ``P7Params(enabled=False)`` (a 0.6.0 state: ``zone6``, no ``zone7``;
the P6 tree itself cannot be kept apart from the working tree, so a disabled learner is the 0.6.0 writer) loads in 0.7.0 with P7 enabled,
``zone7`` stays absent in the export, the export equals the file after ``writer`` and ``phase``, and the learner survives idle days and a
rename; (b) the final state of ``bed_w0000_0600_p7ctl`` (0.7.0, 14 nights, seed 1, with ``zone7``) loads in the 0.5.0 baseline (the
``--base`` tree: its ``P5State`` keeps every unknown key of ``p5`` in ``extra`` as 0.6.0's does), which keeps ``zone7`` and writes it back
unchanged (``--load-only`` returns ``p5['zone7']``), and in this tree's learner with P7 disabled (the 0.6.0 behaviour); (c) a ``zone7`` of the
wrong container type gives ``StateError``, a ``zone7`` with a value problem loads as the defaults and counts ``zone7_reset`` (``state.json``
stays loadable); (d) entries of zones that are not there are pruned and counted (``zone7_pruned``); (e) a 0.7.0 state with a ``ret-``
snooze, an ``attn`` push request and a modified zone (swapped ``ref``, ``also``) loads in the 0.5.0 baseline and in the disabled and the
enabled learner without ``StateError``, and ``p5.later`` and ``p5.push`` have no new key; (f) the export of the learner is accepted by its
own ``Zone7.validate``. Results go to ``DIR/compat_p7.json``.
"""
import sys
from pathlib import Path
from typing import Optional

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
            "p5_kept": "p5" in out, "writer": out.get("writer"), "p5_zone6": (out.get("p5") or {}).get("zone6"),
            "p5_zone7": (out.get("p5") or {}).get("zone7"), "p5_later": sorted((out.get("p5") or {}).get("later") or {}),
            "p5_push_kind": ((out.get("p5") or {}).get("push") or {}).get("kind")}


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


# ---------------------------------------------------------------- P6-R2 (P6 spec 11.2): 0.5.0 <-> 0.6.0
CORRUPT_ZONE6 = [
    ("a known key of the wrong type", {"tune": []}),
    ("set.epoch negative", {"set": {"epoch": -1}}),
    ("set.epoch not an int", {"set": {"epoch": "0"}}),
    ("tune.cd above 900", {"tune": {"z0001": {"cd": 1000}}}),
    ("tune.cd below 3", {"tune": {"z0001": {"cd": 2}}}),
    ("tune.mg above 100", {"tune": {"z0001": {"mg": 150}}}),
    ("tune.mg below -50", {"tune": {"z0001": {"mg": -60}}}),
    ("a spell of 0 s", {"delay": {"z0001": {"sp": [0]}}}),
    ("more than 50 spells", {"delay": {"z0001": {"sp": [10] * 51}}}),
    ("a learned delay below 5 s", {"delay": {"z0001": {"sp": [10], "cd": 3}}}),
    ("a learned delay above 600 s", {"delay": {"z0001": {"sp": [10], "cd": 601}}}),
    ("ld with on_s above known_s", {"ld": {"z0001": [10, 5]}}),
    ("ld with a negative number", {"ld": {"z0001": [-1, 5]}}),
    ("a variant with a bad day class", {"info": {"z0001": {"th": [], "var": [{"d": "xx", "s": 1, "e": 2, "n": 3}], "conf": 0.5}}}),
    ("a variant hour above 24", {"info": {"z0001": {"th": [], "var": [{"d": "wd", "s": 1, "e": 25, "n": 3}], "conf": 0.5}}}),
    ("more than 3 typical-hour ranges", {"info": {"z0001": {"th": {"wd": ["01-02", "03-04", "05-06", "07-08"]}, "var": [], "conf": 0.5}}}),
    ("typical hours that are not an object", {"info": {"z0001": {"th": [[9, 12]], "var": [], "conf": 0.5}}}),
    ("a typical-hour range that is not HH-HH", {"info": {"z0001": {"th": {"wd": ["9-12"]}, "var": [], "conf": 0.5}}}),
    ("a variant request that is not an id", {"ve": {"z0001": ["bad"]}}),
]
ORPHAN_ZONE6 = {"tune": {"cd": 120, "mg": 30}, "delay": {"sp": [20, 30, 40], "upd": 1}, "ld": [100, 120],
                "info": {"th": {"wd": ["09-12"]}, "var": [], "conf": 0.5, "upd": 1}}      # the entries of a zone z9998 that is not there


def _normalise_p6(state: dict) -> dict:
    """A copy of a state without the keys that differ by version alone: ``writer`` (it names the app version) and ``phase``."""
    import copy
    st = copy.deepcopy(state)
    for k in ("writer", "phase"):
        st.pop(k, None)
    return st


def check_p5_state_in_p6(state: dict, days: int = IDLE_DAYS, tick_ms: int = TICK_MS) -> dict:
    """P6-R2 (a): a 0.5.0 state loads in 0.6.0, ``zone6`` stays absent, ``export_state()`` equals the file after ``writer`` and
    ``phase`` (the ``p5`` section included), the learner survives idle days (nightly runs, no frames) and a rename."""
    import copy
    import json
    from radar_zone_learner.core.grids import rle_decode
    from radar_zone_learner.core.types import NightlyDone, NightlyFailed, ReviewAnswer
    from replay.compare_runs import diff
    problems, notes = [], {}
    state = copy.deepcopy(state)
    try:
        L = _p5_learner(copy.deepcopy(state))
    except Exception as exc:
        return {"status": "FAIL", "problems": ["0.6.0 refused the 0.5.0 file: %s: %s" % (type(exc).__name__, exc)], "notes": {}}
    want = sorted((s["id"], s["type"], s["rev"], tuple(sorted(rle_decode(s["cells"])))) for s in state["suggestions"])
    got = sorted((s.id, s.type, s.rev, tuple(sorted(s.cells))) for s in L.suggestions())
    if want != got:
        problems.append("suggestions differ: %d in the file, %d loaded" % (len(want), len(got)))
    exp = L.export_state()
    if "zone6" in (exp.get("p5") or {}):
        problems.append("the export has a zone6 key: a state without zone data must stay byte identical")
    d = diff(_normalise_p6(exp), _normalise_p6(state), "")
    if d:
        problems.append("export_state differs from the 0.5.0 file (writer and phase apart): %s" % d[:4])
    zones = exp.get("zones", [])
    notes["zones"] = len(zones)
    now = idle_start_ms(state)
    done = failed = 0
    errors = []
    for _ in range(days * 86400 * 1000 // tick_ms):
        now += tick_ms
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
    renamed = None
    if zones:
        z = zones[0]
        try:
            L.handle(ReviewAnswer(z["zid"], "rename", None, False, now + 1000, name="Renamed"))
            renamed = any(x["zid"] == z["zid"] and x["name"] == "Renamed" for x in L.export_state()["zones"])
            if not renamed:
                problems.append("the rename answer did not rename the zone")
            json.dumps(L.export_state())
        except Exception as exc:
            problems.append("the rename answer raised %s: %s" % (type(exc).__name__, exc))
    notes["renamed"] = renamed
    status = "FAIL" if problems else ("PASS" if zones else "INCOMPLETE")
    if status == "INCOMPLETE":
        problems.append("not exercised: the state has no zone record")
    return {"status": status, "problems": problems, "notes": notes}


def check_p6_state_in_baseline(state: dict, loaded: dict) -> dict:
    """P6-R2 (b): ``loaded`` is what ``load_only`` printed for the baseline (0.5.0) tree; ``state`` is the 0.6.0 file. The
    baseline loads it, keeps ``zone6`` in the ``p5`` extra and writes it back unchanged."""
    problems = []
    if not loaded.get("loaded"):
        return {"status": "FAIL", "problems": ["0.5.0 refused the 0.6.0 file: %s" % loaded.get("error")], "notes": {}}
    z6 = (state.get("p5") or {}).get("zone6")
    if sorted(loaded["suggestions"]) != sorted(s["id"] for s in state["suggestions"]):
        problems.append("suggestion ids differ")
    if sorted(loaded["labels"]) != sorted(lb["id"] for lb in state["labels"]):
        problems.append("label ids differ")
    if sorted(z["zid"] for z in loaded["zones"]) != sorted(z["zid"] for z in state["zones"]):
        problems.append("zone ids differ")
    if z6 is not None and loaded.get("p5_zone6") != z6:
        problems.append("0.5.0 did not write zone6 back unchanged")
    notes = {"zone6_in_file": z6 is not None, "zone6_keys": sorted(z6) if isinstance(z6, dict) else None,
             "baseline_kept_p5": loaded.get("p5_kept"), "zones": len(state["zones"])}
    status = "FAIL" if problems else ("PASS" if z6 else "INCOMPLETE")
    if status == "INCOMPLETE":
        problems.append("not exercised: the 0.6.0 state has no zone6 (no zone evidence was recorded)")
    return {"status": status, "problems": problems, "notes": notes}


def _base_state() -> dict:
    """A valid 0.6.0 state with no zone and no zone6 (a fresh learner's export)."""
    from radar_zone_learner.core.learner import Learner
    return Learner().export_state()


def check_corrupt_zone6() -> dict:
    """P6-R2 (c): every corrupt ``zone6`` makes the load fail with ``StateError`` (and no other exception)."""
    import copy
    from radar_zone_learner.core.types import StateError
    problems = []
    base = _base_state()
    for label, z6 in CORRUPT_ZONE6:
        st = copy.deepcopy(base)
        st["p5"] = dict(st.get("p5") or {})
        st["p5"]["zone6"] = z6
        try:
            _p5_learner(st)
        except StateError:
            continue
        except Exception as exc:
            problems.append("%s: %s instead of StateError (%s)" % (label, type(exc).__name__, exc))
            continue
        problems.append("%s: the state loaded" % label)
    return {"status": "FAIL" if problems else "PASS", "problems": problems, "notes": {"variants": len(CORRUPT_ZONE6)}}


def check_orphans(state: Optional[dict] = None) -> dict:
    """P6-R2 (d): ``zone6`` entries of zones that are not there are pruned silently, valid entries stay."""
    import copy
    problems, notes = [], {}
    if state is None:                          # a state with zones: entries are pruned against the zones the learner has
        from replay.resource_probe import heavy_state
        state = heavy_state()
    base = copy.deepcopy(state)
    base["p5"] = dict(base.get("p5") or {})
    z6 = copy.deepcopy(base["p5"].get("zone6") or {"v": 1, "set": {"epoch": 0}})
    for key, entry in ORPHAN_ZONE6.items():
        if not isinstance(z6.get(key), dict):
            z6[key] = {}
        z6[key]["z9998"] = copy.deepcopy(entry)
    base["p5"]["zone6"] = z6
    try:
        L = _p5_learner(base)
    except Exception as exc:
        return {"status": "FAIL", "problems": ["the state with orphan entries was refused: %s: %s" % (type(exc).__name__, exc)],
                "notes": {}}
    now = idle_start_ms(base)
    for k in (1, 2, 3):                        # the evaluator is given the zones (and the zone6 entries are pruned) on the next steps
        L.tick(now + 1000 * k)
    exp = (L.export_state().get("p5") or {}).get("zone6") or {}
    left = [k for k in ("tune", "delay", "ld", "info", "ve") if "z9998" in (exp.get(k) or {})]
    if left:
        problems.append("orphan entries were not pruned from %s" % left)
    pruned = (L.counters() or {}).get("zone6_pruned")
    notes["zone6_pruned"] = pruned
    if not pruned:
        problems.append("counters()['zone6_pruned'] is %r after pruning orphan entries" % (pruned,))
    return {"status": "FAIL" if problems else "PASS", "problems": problems, "notes": notes}


def p6_main(args) -> int:
    import json
    if not args.base or not args.out or not (Path(args.base) / "replay" / "driver.py").is_file():
        print("usage error: --p6 needs --base (a baseline add-on folder, git archive 91c05e9) and --out", file=sys.stderr)
        return 2
    sys.path.insert(0, str(ADDON))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dump = str(ADDON / "replay" / "dump_run.py")
    py = [sys.executable, "-I", "-B"]
    result = {}
    job_a = "bed_w0000_0600_p5zone"
    rc, so, se = _run(py + [dump, "--tree", args.base, "--jobs", job_a, "--seeds", "1", "--nights", "16", "--slim",
                            "--out", str(out / "a_base")])
    if rc != 0:
        result["a"] = {"status": "FAIL", "problems": ["the baseline run failed (exit %d): %s" % (rc, (se or so)[-400:])]}
    else:
        state = json.loads((out / "a_base" / ("%s_s1.json" % job_a)).read_text(encoding="utf-8"))["final_state"]
        result["a"] = check_p5_state_in_p6(state)
    job_b = "bed_w0000_0600_p6hold"
    rc, so, se = _run(py + [dump, "--tree", str(ADDON), "--jobs", job_b, "--seeds", "1", "--nights", "14", "--slim",
                            "--out", str(out / "b_p6")])
    state6 = None
    if rc != 0:
        result["b"] = {"status": "FAIL", "problems": ["the P6 run failed (exit %d): %s" % (rc, (se or so)[-400:])]}
    else:
        state6 = json.loads((out / "b_p6" / ("%s_s1.json" % job_b)).read_text(encoding="utf-8"))["final_state"]
        (out / "b_state.json").write_text(json.dumps(state6, separators=(",", ":")), encoding="utf-8")
        rc, so, se = _run(py + [str(Path(__file__).resolve()), "--load-only", "--tree", args.base,
                                "--state", str(out / "b_state.json")])
        if rc != 0:
            result["b"] = {"status": "FAIL", "problems": ["the baseline process failed (exit %d): %s" % (rc, (se or so)[-400:])]}
        else:
            result["b"] = check_p6_state_in_baseline(state6, json.loads(so.strip().splitlines()[-1]))
    result["c"] = check_corrupt_zone6()
    result["d"] = check_orphans(state6)
    sts = [result[k]["status"] for k in ("a", "b", "c", "d")]
    result["status"] = "PASS" if sts == ["PASS"] * 4 else ("FAIL" if "FAIL" in sts else "INCOMPLETE")
    (out / "compat_p6.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    labels = (("a", "(a) the 0.5.0 file in 0.6.0"), ("b", "(b) the 0.6.0 file (with zone6) in 0.5.0"),
              ("c", "(c) corrupt zone6 gives StateError"), ("d", "(d) orphan zone6 entries are pruned"))
    for k, label in labels:
        print("%-10s %s" % (result[k]["status"], label))
        for pr in result[k]["problems"]:
            print("           " + pr)
    print("P6-R2: %s" % result["status"])
    return 0 if result["status"] == "PASS" else 1


# ---------------------------------------------------------------- P7-R2 (P7 spec 9.1): 0.5.0 / 0.6.0 <-> 0.7.0
CORRUPT_ZONE7_CONTAINER = [
    ("zone7 is a list", []),
    ("zone7 is a string", "x"),
    ("z is a list", {"v": 1, "z": []}),
    ("rm is a list", {"v": 1, "rm": []}),
    ("snz is a list", {"v": 1, "snz": []}),
    ("ac is a list", {"v": 1, "ac": []}),
    ("mf is an object", {"v": 1, "mf": {}}),
]
_U_OK = [0.0] * 24
_U_OK[10] = 1.0
CORRUPT_ZONE7_VALUES = [
    ("v is not an int", {"v": "1"}),
    ("v is 0", {"v": 0}),
    ("a z key that is not a zone id", {"v": 1, "z": {"bad": {}}}),
    ("u of 5 numbers", {"v": 1, "z": {"z0001": {"u": [0.2] * 5}}}),
    ("u that does not sum to 1", {"v": 1, "z": {"z0001": {"u": [0.1] * 24}}}),
    ("u with a number above 1", {"v": 1, "z": {"z0001": {"u": [2.0] + [0.0] * 23}}}),
    ("lo above 99,999", {"v": 1, "z": {"z0001": {"lo": 100000}}}),
    ("rt negative", {"v": 1, "z": {"z0001": {"rt": -1}}}),
    ("mr not an int", {"v": 1, "z": {"z0001": {"mr": 1.5}}}),
    ("n negative", {"v": 1, "z": {"z0001": {"n": -1}}}),
    ("lu not an int", {"v": 1, "z": {"z0001": {"lu": "x"}}}),
    ("ms that is not an id", {"v": 1, "z": {"z0001": {"ms": "abc"}}}),
    ("vs that is not a state", {"v": 1, "z": {"z0001": {"vs": "maybe"}}}),
    ("ro not a bool", {"v": 1, "z": {"z0001": {"ro": 1}}}),
    ("a mask with a bad why", {"v": 1, "mf": [{"zid": "z0001", "cells": [[1, 2]], "cx": 1.0, "cy": 1.0, "day": 5, "n0": 0, "why": "x"}]}),
    ("a mask with a negative day", {"v": 1, "mf": [{"zid": "z0001", "cells": [[1, 2]], "cx": 1.0, "cy": 1.0, "day": -1, "n0": 0,
                                                    "why": "moved_from"}]}),
    ("a mask outside the room", {"v": 1, "mf": [{"zid": "z0001", "cells": [[1, 2]], "cx": 99.0, "cy": 1.0, "day": 5, "n0": 0,
                                                 "why": "retired"}]}),
    ("rm with a negative counter", {"v": 1, "rm": {"st": -1}}),
    ("rm.tx with two numbers", {"v": 1, "rm": {"tx": [1, 2]}}),
    ("snz with a key that is not an item", {"v": 1, "snz": {"bad": 5}}),
    ("snz with a negative date", {"v": 1, "snz": {"radar_moved": -3}}),
    ("ac with a non-int", {"v": 1, "ac": {"op": {"place": ["x", 0, 0, 0]}}}),
]
ORPHAN_ZONE7 = {"u": _U_OK, "lo": 1, "rt": 2, "n": 100, "lu": 99, "kp": 0, "mr": 0, "ms": "", "rr": 0, "ro": False, "rc": False,
                "rd": 0, "fz": False, "vs": "ok"}              # the entry of a zone z9998 that is not there


def _learner(state: dict, p7: bool = True):
    """A learner of this tree on ``state``: P7 enabled (0.7.0) or disabled (a 0.6.0 learner byte for byte)."""
    from radar_zone_learner.core.learner import Learner
    from radar_zone_learner.core.params import DEFAULT_P5, DEFAULT_P6, DEFAULT_PARAMS, P7Params
    from radar_zone_learner.core.types import FixedOffsetTime
    return Learner(DEFAULT_PARAMS, FixedOffsetTime(0), state, DEFAULT_P5, DEFAULT_P6, P7Params(enabled=bool(p7)))


def _idle(L, state: dict, days: int = IDLE_DAYS, tick_ms: int = TICK_MS) -> dict:
    """Idle ticks (nightly runs, no frames) over ``days`` days: ``{"done", "failed", "errors", "now"}``."""
    from radar_zone_learner.core.types import NightlyDone, NightlyFailed
    now = idle_start_ms(state)
    done = failed = 0
    errors = []
    for _ in range(days * 86400 * 1000 // tick_ms):
        now += tick_ms
        try:
            for o in L.tick(now):
                done += isinstance(o, NightlyDone)
                failed += isinstance(o, NightlyFailed)
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
            break
    return {"done": done, "failed": failed, "errors": errors, "now": now}


def check_p6_state_in_p7(state: dict, days: int = IDLE_DAYS, tick_ms: int = TICK_MS) -> dict:
    """P7-R2 (a): a 0.6.0 state (``zone6``, no ``zone7``) loads in 0.7.0, ``zone7`` stays absent in the export, the export equals the
    file after ``writer`` and ``phase``, the learner survives idle days (every nightly P7 step runs) and a rename."""
    import copy
    import json
    from radar_zone_learner.core.types import ReviewAnswer
    from replay.compare_runs import diff
    problems, notes = [], {}
    state = copy.deepcopy(state)
    if "zone7" in (state.get("p5") or {}):
        return {"status": "FAIL", "problems": ["the 0.6.0 file has a zone7 key: it was not written by a disabled learner"], "notes": {}}
    try:
        L = _learner(copy.deepcopy(state), True)
    except Exception as exc:
        return {"status": "FAIL", "problems": ["0.7.0 refused the 0.6.0 file: %s: %s" % (type(exc).__name__, exc)], "notes": {}}
    exp = L.export_state()
    if "zone7" in (exp.get("p5") or {}):
        problems.append("the export has a zone7 key: a state without P7 data must stay byte identical")
    d = diff(_normalise_p6(exp), _normalise_p6(state), "")
    if d:
        problems.append("export_state differs from the 0.6.0 file (writer and phase apart): %s" % d[:4])
    notes["has_zone6"] = bool((state.get("p5") or {}).get("zone6"))
    notes["zones"] = len(exp.get("zones", []))
    idle = _idle(L, state, days, tick_ms)
    notes.update(nightly_runs=idle["done"], nightly_failed=idle["failed"], p7_errors=(L.counters() or {}).get("p7_errors"))
    if idle["errors"] or idle["failed"]:
        problems.append("the idle ticks failed: %s" % (idle["errors"] or "%d nightly runs failed" % idle["failed"]))
    if idle["done"] != days:
        problems.append("%d nightly runs in %d idle days (expected %d)" % (idle["done"], days, days))
    renamed = None
    zones = exp.get("zones", [])
    if zones:
        z = zones[0]
        try:
            L.handle(ReviewAnswer(z["zid"], "rename", None, False, idle["now"] + 1000, name="Renamed"))
            renamed = any(x["zid"] == z["zid"] and x["name"] == "Renamed" for x in L.export_state()["zones"])
            if not renamed:
                problems.append("the rename answer did not rename the zone")
            json.dumps(L.export_state())
        except Exception as exc:
            problems.append("the rename answer raised %s: %s" % (type(exc).__name__, exc))
    notes["renamed"] = renamed
    status = "FAIL" if problems else ("PASS" if notes["has_zone6"] else "INCOMPLETE")
    if status == "INCOMPLETE":
        problems.append("not exercised: the 0.6.0 state has no zone6")
    return {"status": status, "problems": problems, "notes": notes}


def check_p7_state_in_older(state: dict, loaded: dict) -> dict:
    """P7-R2 (b), baseline half: ``loaded`` is what ``load_only`` printed for the baseline tree; ``state`` is the 0.7.0 file. The older
    tree loads it, keeps ``zone7`` in the ``p5`` extra and writes it back unchanged."""
    problems = []
    if not loaded.get("loaded"):
        return {"status": "FAIL", "problems": ["the baseline refused the 0.7.0 file: %s" % loaded.get("error")], "notes": {}}
    z7 = (state.get("p5") or {}).get("zone7")
    if sorted(loaded["suggestions"]) != sorted(s["id"] for s in state["suggestions"]):
        problems.append("suggestion ids differ")
    if sorted(loaded["labels"]) != sorted(lb["id"] for lb in state["labels"]):
        problems.append("label ids differ")
    if sorted(z["zid"] for z in loaded["zones"]) != sorted(z["zid"] for z in state["zones"]):
        problems.append("zone ids differ")
    if z7 is not None and loaded.get("p5_zone7") != z7:
        problems.append("the baseline did not write zone7 back unchanged")
    notes = {"zone7_in_file": z7 is not None, "zone7_keys": sorted(z7) if isinstance(z7, dict) else None,
             "zone7_zones": len((z7 or {}).get("z") or {}), "baseline_kept_p5": loaded.get("p5_kept"), "zones": len(state["zones"])}
    status = "FAIL" if problems else ("PASS" if z7 else "INCOMPLETE")
    if status == "INCOMPLETE":
        problems.append("not exercised: the 0.7.0 state has no zone7 (no nightly P7 step ran)")
    return {"status": status, "problems": problems, "notes": notes}


def check_p7_state_in_disabled(state: dict) -> dict:
    """P7-R2 (b), in-process half: this tree's learner with P7 disabled (the 0.6.0 behaviour) loads the 0.7.0 file; ``zone7`` is kept in
    the ``p5`` extra and written back unchanged, and nothing else of the export changes."""
    import copy
    from replay.compare_runs import diff
    problems, notes = [], {}
    try:
        L = _learner(copy.deepcopy(state), False)
    except Exception as exc:
        return {"status": "FAIL", "problems": ["a disabled learner refused the 0.7.0 file: %s: %s" % (type(exc).__name__, exc)], "notes": {}}
    exp = L.export_state()
    z7 = (state.get("p5") or {}).get("zone7")
    if z7 is not None and (exp.get("p5") or {}).get("zone7") != z7:
        problems.append("the disabled learner did not write zone7 back unchanged")
    d = diff(_normalise_p6(exp), _normalise_p6(state), "")
    if d:
        problems.append("export_state differs from the file: %s" % d[:4])
    notes["zone7_kept"] = (exp.get("p5") or {}).get("zone7") == z7
    return {"status": "FAIL" if problems else ("PASS" if z7 else "INCOMPLETE"), "problems": problems, "notes": notes}


def check_corrupt_zone7(base: Optional[dict] = None) -> dict:
    """P7-R2 (c): a ``zone7`` of the wrong container type makes the load fail with ``StateError`` (and no other exception); a ``zone7`` with
    a value problem loads as the defaults (``zone7`` absent in the export) and ``counters()['zone7_reset']`` counts it."""
    import copy
    import json
    from radar_zone_learner.core.types import StateError
    problems = []
    base = base if base is not None else _base_state()
    for label, z7 in CORRUPT_ZONE7_CONTAINER:
        st = copy.deepcopy(base)
        st["p5"] = dict(st.get("p5") or {})
        st["p5"]["zone7"] = z7
        try:
            _learner(st, True)
        except StateError:
            continue
        except Exception as exc:
            problems.append("%s: %s instead of StateError (%s)" % (label, type(exc).__name__, exc))
            continue
        problems.append("%s: the state loaded" % label)
    for label, z7 in CORRUPT_ZONE7_VALUES:
        st = copy.deepcopy(base)
        st["p5"] = dict(st.get("p5") or {})
        st["p5"]["zone7"] = z7
        try:
            L = _learner(st, True)
        except Exception as exc:
            problems.append("%s: the state was refused (%s: %s), a value problem must give the defaults" % (label, type(exc).__name__, exc))
            continue
        exp = L.export_state()
        if "zone7" in (exp.get("p5") or {}):
            problems.append("%s: zone7 is still in the export" % label)
        if not (L.counters() or {}).get("zone7_reset"):
            problems.append("%s: zone7_reset is %r" % (label, (L.counters() or {}).get("zone7_reset")))
        json.dumps(exp)
    return {"status": "FAIL" if problems else "PASS", "problems": problems,
            "notes": {"container_variants": len(CORRUPT_ZONE7_CONTAINER), "value_variants": len(CORRUPT_ZONE7_VALUES)}}


def check_orphans_p7(state: dict) -> dict:
    """P7-R2 (d): ``zone7`` entries of zones that are not there are pruned silently and counted; entries of zones that are there stay."""
    import copy
    problems, notes = [], {}
    base = copy.deepcopy(state)
    base["p5"] = dict(base.get("p5") or {})
    z7 = copy.deepcopy(base["p5"].get("zone7") or {"v": 1})
    z7.setdefault("z", {})
    here = {z["zid"] for z in state.get("zones", [])} | {lb["id"] for lb in state.get("labels", [])}
    keep = sorted(k for k in z7["z"] if k in here)
    z7["z"]["z9998"] = copy.deepcopy(ORPHAN_ZONE7)
    z7.setdefault("snz", {})["ret-z9998"] = 5
    base["p5"]["zone7"] = z7
    try:
        L = _learner(base, True)
    except Exception as exc:
        return {"status": "FAIL", "problems": ["the state with orphan entries was refused: %s: %s" % (type(exc).__name__, exc)], "notes": {}}
    now = idle_start_ms(base)
    for k in (1, 2, 3):
        L.tick(now + 1000 * k)
    got = (L.export_state().get("p5") or {}).get("zone7") or {}
    if "z9998" in (got.get("z") or {}) or "ret-z9998" in (got.get("snz") or {}):
        problems.append("an orphan entry was not pruned")
    for zid in keep:
        if zid not in (got.get("z") or {}):
            problems.append("the entry of the zone %s was pruned" % zid)
    pruned = (L.counters() or {}).get("zone7_pruned")
    notes["zone7_pruned"] = pruned
    if not pruned:
        problems.append("counters()['zone7_pruned'] is %r after pruning orphan entries" % (pruned,))
    return {"status": "FAIL" if problems else "PASS", "problems": problems, "notes": notes}


def modified_state(state: dict) -> Optional[dict]:
    """A copy of a 0.7.0 state with a ``ret-`` snooze, an ``attn`` push request and a modified zone (the ``ref`` swapped to another
    suggestion and the old one in ``also``); None when the state has no zone record and a second suggestion."""
    import copy
    st = copy.deepcopy(state)
    recs = [z for z in st.get("zones", []) if z.get("ref")]
    sugg = [s["id"] for s in st.get("suggestions", [])]
    if not recs:
        return None
    z = recs[0]
    other = next((i for i in sugg if i != z["ref"]), None)
    if other is None:
        return None
    z["also"] = [z["ref"]] + [a for a in z.get("also", []) if a != z["ref"]]
    z["ref"] = other
    z["rev"] = int(z.get("rev", 0)) + 1
    p5 = st.setdefault("p5", {})
    z7 = p5.setdefault("zone7", {"v": 1})
    z7.setdefault("snz", {})["ret-" + z["zid"]] = 40
    rm = z7.setdefault("rm", {"st": 0, "rev": 0, "open": False, "ign": 0, "tx": [0, 0, 0], "n": 0, "attn": False, "d0": 0, "qt": 0})
    rm.update(st=2, rev=1, open=True, attn=True, d0=30, n=31, tx=[0, 0, -75])
    z7.setdefault("z", {}).setdefault(z["zid"], dict(ORPHAN_ZONE7, vs="falling"))
    z7["z"][z["zid"]].update(mr=2, ms="%s#1" % other, ro=True, rr=1)
    push = dict(p5.get("push") or {})
    push.update(kind="daily")
    p5["push"] = push
    return st


def check_p7_modified_in_older(state: dict, loaded: dict) -> dict:
    """P7-R2 (e), baseline half: the modified 0.7.0 state loads in the baseline; ``p5.later`` and ``p5.push.kind`` have no new value."""
    problems = []
    if not loaded.get("loaded"):
        return {"status": "FAIL", "problems": ["the baseline refused the modified 0.7.0 file: %s" % loaded.get("error")], "notes": {}}
    later_in = sorted((state.get("p5") or {}).get("later") or {})
    if loaded.get("p5_later") != later_in:
        problems.append("p5.later keys differ: %s != %s" % (loaded.get("p5_later"), later_in))
    if any(str(k).startswith("ret-") or k == "radar_moved" for k in later_in):
        problems.append("p5.later has a P7 key")
    if loaded.get("p5_push_kind") not in ("none", "setup", "daily", None):
        problems.append("p5.push.kind is %r" % loaded.get("p5_push_kind"))
    if (state.get("p5") or {}).get("zone7") != loaded.get("p5_zone7"):
        problems.append("the baseline did not write zone7 back unchanged")
    return {"status": "FAIL" if problems else "PASS", "problems": problems,
            "notes": {"push_kind": loaded.get("p5_push_kind"), "later_keys": len(later_in)}}


def check_p7_modified_in_learner(state: dict, tick_ms: int = TICK_MS, days: int = 2) -> dict:
    """P7-R2 (e), in-process half: the disabled learner and the enabled one load the modified state; the enabled one survives idle days and
    the modified zone keeps its ``ref`` and ``also``."""
    import copy
    problems, notes = [], {}
    for label, on in (("a disabled learner", False), ("an enabled learner", True)):
        try:
            L = _learner(copy.deepcopy(state), on)
        except Exception as exc:
            problems.append("%s refused the modified file: %s: %s" % (label, type(exc).__name__, exc))
            continue
        exp = L.export_state()
        mod = next((z for z in state["zones"] if z.get("also")), None)
        got = next((z for z in exp["zones"] if mod and z["zid"] == mod["zid"]), None)
        if mod and (got is None or got.get("ref") != mod["ref"] or got.get("also") != mod["also"]):
            problems.append("%s changed the modified zone's ref or also" % label)
        if on:
            idle = _idle(L, state, days, tick_ms)
            if idle["errors"] or idle["failed"]:
                problems.append("%s failed on idle days: %s" % (label, idle["errors"] or "%d nightly runs failed" % idle["failed"]))
            notes["enabled_nightly_runs"] = idle["done"]
    return {"status": "FAIL" if problems else "PASS", "problems": problems, "notes": notes}


def check_export_validates(state: dict) -> dict:
    """P7-R2 (f): the ``zone7`` a learner exports is accepted by ``Zone7.validate`` (the export is validated before it is written)."""
    import copy
    from radar_zone_learner.core.p7state import Zone7
    z7 = (state.get("p5") or {}).get("zone7")
    if z7 is None:
        return {"status": "INCOMPLETE", "problems": ["not exercised: the state has no zone7"], "notes": {}}
    nx, ny = (state.get("grids") or {}).get("nx"), (state.get("grids") or {}).get("ny")
    if not isinstance(nx, int) or not isinstance(ny, int):
        return {"status": "FAIL", "problems": ["the state has no grid size"], "notes": {}}
    try:
        Zone7.validate(copy.deepcopy(z7), nx * ny)
    except Exception as exc:
        return {"status": "FAIL", "problems": ["Zone7.validate refused the export: %s: %s" % (type(exc).__name__, exc)], "notes": {}}
    L = _learner(copy.deepcopy(state), True)
    again = (L.export_state().get("p5") or {}).get("zone7")
    if again != z7:
        return {"status": "FAIL", "problems": ["the export after a load is not the loaded zone7"], "notes": {}}
    return {"status": "PASS", "problems": [], "notes": {"zones": len(z7.get("z") or {})}}


def _both(a: str, b: str) -> str:
    return "FAIL" if "FAIL" in (a, b) else ("INCOMPLETE" if "INCOMPLETE" in (a, b) else "PASS")


def p7_main(args) -> int:
    import json
    if not args.base or not args.out or not (Path(args.base) / "replay" / "driver.py").is_file():
        print("usage error: --p7 needs --base (a baseline add-on folder, git archive 91c05e9) and --out", file=sys.stderr)
        return 2
    sys.path.insert(0, str(ADDON))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dump = str(ADDON / "replay" / "dump_run.py")
    py = [sys.executable, "-I", "-B"]
    me = str(Path(__file__).resolve())
    result = {}
    # (a) a 0.6.0 state, written by this tree's disabled learner (a P6 job), loads in 0.7.0
    job_a = "bed_w0000_0600_p6hold"
    rc, so, se = _run(py + [dump, "--tree", str(ADDON), "--jobs", job_a, "--seeds", "1", "--nights", "14", "--slim", "--out", str(out / "a_p6")])
    if rc != 0:
        result["a"] = {"status": "FAIL", "problems": ["the P6 run failed (exit %d): %s" % (rc, (se or so)[-400:])]}
    else:
        state6 = json.loads((out / "a_p6" / ("%s_s1.json" % job_a)).read_text(encoding="utf-8"))["final_state"]
        result["a"] = check_p6_state_in_p7(state6)
    # (b), (d), (e), (f): a 0.7.0 state with zone7 in the older tree and in the disabled learner
    job_b = "bed_w0000_0600_p7ctl"
    rc, so, se = _run(py + [dump, "--tree", str(ADDON), "--jobs", job_b, "--seeds", "1", "--nights", "14", "--slim", "--out", str(out / "b_p7")])
    state7 = None
    if rc != 0:
        for k in ("b", "d", "e", "f"):
            result[k] = {"status": "FAIL", "problems": ["the P7 run failed (exit %d): %s" % (rc, (se or so)[-400:])]}
    else:
        state7 = json.loads((out / "b_p7" / ("%s_s1.json" % job_b)).read_text(encoding="utf-8"))["final_state"]
        (out / "b_state.json").write_text(json.dumps(state7, separators=(",", ":")), encoding="utf-8")
        rc, so, se = _run(py + [me, "--load-only", "--tree", args.base, "--state", str(out / "b_state.json")])
        if rc != 0:
            half = {"status": "FAIL", "problems": ["the baseline process failed (exit %d): %s" % (rc, (se or so)[-400:])], "notes": {}}
        else:
            half = check_p7_state_in_older(state7, json.loads(so.strip().splitlines()[-1]))
        dis = check_p7_state_in_disabled(state7)
        result["b"] = {"status": _both(half["status"], dis["status"]), "problems": half["problems"] + dis["problems"],
                       "notes": {"baseline": half.get("notes"), "disabled": dis.get("notes")}}
        result["d"] = check_orphans_p7(state7)
        mod = modified_state(state7)
        if mod is None:
            result["e"] = {"status": "INCOMPLETE", "problems": ["not exercised: the 0.7.0 state has no zone record with a suggestion to swap to"]}
        else:
            (out / "e_state.json").write_text(json.dumps(mod, separators=(",", ":")), encoding="utf-8")
            rc, so, se = _run(py + [me, "--load-only", "--tree", args.base, "--state", str(out / "e_state.json")])
            if rc != 0:
                half = {"status": "FAIL", "problems": ["the baseline process failed (exit %d): %s" % (rc, (se or so)[-400:])], "notes": {}}
            else:
                half = check_p7_modified_in_older(mod, json.loads(so.strip().splitlines()[-1]))
            dis = check_p7_modified_in_learner(mod)
            result["e"] = {"status": _both(half["status"], dis["status"]), "problems": half["problems"] + dis["problems"],
                           "notes": {"baseline": half.get("notes"), "learner": dis.get("notes")}}
        result["f"] = check_export_validates(state7)
    result["c"] = check_corrupt_zone7(state7)
    keys = ("a", "b", "c", "d", "e", "f")
    sts = [result[k]["status"] for k in keys]
    result["status"] = "PASS" if sts == ["PASS"] * 6 else ("FAIL" if "FAIL" in sts else "INCOMPLETE")
    result["deviation"] = ("the 0.6.0 tree could not be kept apart from the working tree: the 0.6.0 writer is this tree's learner with P7 "
                           "disabled, the older reader is the 0.5.0 baseline (its P5State keeps unknown keys of p5 in extra, as 0.6.0 does)")
    (out / "compat_p7.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    labels = (("a", "(a) the 0.6.0 file in 0.7.0"), ("b", "(b) the 0.7.0 file (with zone7) in 0.5.0 and in a disabled learner"),
              ("c", "(c) a wrong zone7 container gives StateError, a value problem the defaults and zone7_reset"),
              ("d", "(d) orphan zone7 entries are pruned and counted"),
              ("e", "(e) a ret- snooze, an attn push request and a modified zone load in the older readers"),
              ("f", "(f) the export is accepted by Zone7.validate"))
    for k, label in labels:
        print("%-10s %s" % (result[k]["status"], label))
        for pr in result[k]["problems"]:
            print("           " + pr)
    print("P7-R2: %s" % result["status"])
    return 0 if result["status"] == "PASS" else 1


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
    ap.add_argument("--p6", action="store_true", help="P6-R2: the 0.5.0 baseline against this tree (0.6.0, zone6)")
    ap.add_argument("--p7", action="store_true", help="P7-R2: the older trees against this tree (0.7.0, zone7)")
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
    if args.p6:
        return p6_main(args)
    if args.p7:
        return p7_main(args)
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
