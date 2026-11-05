"""Runs replay jobs on ONE given code tree and writes each run's result and final state (checks P5-R1 and P5-R2,
P5 spec 11.2; P6-R1 and P6-R2, P6 spec 11.2).

    python -I -B replay/dump_run.py --tree TREE --jobs NAME,... [--seeds 1-3] [--nights N] --out DIR

``TREE`` is an add-on folder (the one that holds ``radar_zone_learner/`` and ``replay/``): this tree's own ``replay.driver``
and ``radar_zone_learner`` package are imported, nothing of the tree this script lives in. That is the point: the P5 tree
and the baseline (``git archive 58804fa ha-addon`` extracted somewhere) both contain a package named ``radar_zone_learner``
and cannot be imported into one process, so every tree is dumped by its own process of this one script. Each (job, seed)
runs in sequence (one process, no pool) and writes ``DIR/<job>_s<seed>.json`` =
``{"result": Run.result(), "final_state": Run.learner.export_state()}``.

It works on any tree that has ``replay/driver.py`` (``Run``) and ``replay/jobs.py`` (``parse_job``) with the job names.
P6 (P6-R1, P6-R2): the P5 jobs are dumped on the 0.5.0 baseline (``git archive 91c05e9``, read only) and on this tree, and
``compare_runs.py`` compares them; a P6 job (``bed_w0000_0600_p6hold``) dumped from this tree gives the 0.6.0 state with its
``zone6`` for ``compat_check.py``. ``--slim`` leaves the run's bulky ``p6`` record (events, states, people) out of the dump
(the final state is always written).
P7 (P7-R1, P7-R2; P7 spec 9.1): the P5 and P6 jobs are dumped on the 0.5.0 baseline and on this tree (the learner of a non-P7 job has
``P7Params(enabled=False)``, so this tree must give the same fields), a P7 job (``bed_w0000_0600_p7ctl``) dumped from this tree gives
the 0.7.0 state with its ``zone7`` for ``compat_check.py --p7``, and ``--p7on`` runs a P5 or P6 job with ``P7Params(enabled=True)`` (the
learner the production factory builds) so that ``compare_runs.py --p7on`` can show where P7 differs from the baseline.
``--nights`` applies to every job (default: the job's own); ``--seeds`` default 1. Exit code 0 when every run finished
with status ok and no core error, 1 otherwise, 2 on a usage error.
"""
import sys
from pathlib import Path


def _parse_seeds(text: str) -> tuple:
    out = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    seeds = tuple(sorted(out))
    if not seeds or any(not 1 <= s <= 5 for s in seeds):
        raise ValueError("seeds must lie within 1..5")
    return seeds


def dump(tree: Path, jobs: list, seeds: tuple, nights, out: Path, log=print, slim: bool = False, p7on: bool = False) -> list:
    """Runs the jobs on ``tree`` and writes the dumps; returns the list of (job, seed, status, path, core_errors). ``p7on``: the learner
    of the run has ``P7Params(enabled=True)`` whatever the job says (P7-R1 (b); only a tree that has P7)."""
    import json
    import time
    sys.path.insert(0, str(tree))                       # the tree first: its own packages are the ones imported
    from replay import driver as D
    from replay import jobs as J
    out.mkdir(parents=True, exist_ok=True)
    done = []
    for name in jobs:
        for seed in seeds:
            job = J.parse_job(name, nights=nights)
            t0 = time.perf_counter()
            run = D.Run(job, seed)
            if p7on:
                run.factory.p7_enabled = True
                run.learner = run._new_learner(None)
            run.run()
            res = run.result()
            if slim:
                res.pop("p6", None)
            state = run.learner.export_state()
            path = out / ("%s_s%d.json" % (name, seed))
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"result": res, "final_state": state}, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
            done.append((name, seed, res["status"], str(path), res.get("core_errors", 0)))
            log("%s s%d %s  nights %s  wall %.0f s" % (name, seed, res["status"], res["nights"][-1]["night"]
                                                       if res["nights"] else 0, time.perf_counter() - t0))
    return done


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="dump_run.py", allow_abbrev=False,
                                 description="Run jobs on one code tree; write result and final state per run.")
    ap.add_argument("--tree", required=True, help="the add-on folder whose replay/ and radar_zone_learner/ are used")
    ap.add_argument("--jobs", required=True, help="comma-separated job names, e.g. bed_w0000_0600,liv_w0000_0600")
    ap.add_argument("--seeds", default="1", help="e.g. 1-3 or 1,3 (default 1)")
    ap.add_argument("--nights", type=int, default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--slim", action="store_true", help="leave the run's bulky p6 record out of the dump")
    ap.add_argument("--p7on", action="store_true",
                    help="P7-R1 (b): the learner has P7Params(enabled=True) whatever the job says (only a tree that has P7)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        seeds = _parse_seeds(args.seeds)
        names = [n.strip() for n in args.jobs.split(",") if n.strip()]
        tree = Path(args.tree).resolve()
        if not names or not (tree / "replay" / "driver.py").is_file() or not (tree / "radar_zone_learner").is_dir():
            raise ValueError("--tree must hold replay/driver.py and radar_zone_learner/, and --jobs names jobs")
        if args.nights is not None and not 1 <= args.nights <= 60:
            raise ValueError("nights must be 1..60")
    except ValueError as exc:
        print("usage error: %s" % exc, file=sys.stderr)
        return 2
    done = dump(tree, names, seeds, args.nights, Path(args.out), log=lambda t: print(t, flush=True), slim=args.slim,
                p7on=args.p7on)
    return 0 if all(st == "ok" and not err for _, _, st, _, err in done) else 1


if __name__ == "__main__":
    sys.exit(main())
