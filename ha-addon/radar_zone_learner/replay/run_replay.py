"""Replay CLI: run jobs in parallel (at most 3 workers), evaluate, write the report (P3 spec 11.9).

    python -I -B replay/run_replay.py --out DIR [--profile smoke|quick|p3] [--jobs NAME,...] [--seeds 1-5]
                                      [--nights N] [--workers N] [--cpu-budget-per-day S] [--params-json FILE]

Output (nothing is written outside ``--out``): ``DIR/runs/<job>_s<seed>.json``, ``DIR/report.json``,
``DIR/report.md``, ``DIR/timing.json``, ``DIR/log.txt``.

Bounds: ``--workers`` is clamped to ``min(3, cpu_count - 1)``; ``--nights`` <= 28; seeds within 1..5; more than
1,500 simulated days in one invocation is refused; each run stops when its CPU exceeds
``--cpu-budget-per-day`` (default 6 s) x simulated days + 30 s. Exit code: 0 when no gated criterion fails and
every run finished, 1 when a gated criterion fails or a run is over budget or failed, 2 on a usage error or
a budget refusal. With a profile that lacks runs of a criterion (smoke, quick) the gate reads INCOMPLETE.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ADDON, so "radar_zone_learner" and "replay" import

import argparse  # noqa: E402
import dataclasses  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import multiprocessing  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

from radar_zone_learner.core.params import DEFAULT_PARAMS, Params, params_digest  # noqa: E402
from replay import jobs as J  # noqa: E402
from replay.driver import run_task  # noqa: E402
from replay.evaluate import evaluate, write_report  # noqa: E402

MAX_WORKERS = 3
MAX_SIM_DAYS = 1500
EST_CPU_PER_DAY = 2.5          # s per simulated day on the i3 (P3 spec 11.9; timing.json has the measured value)


class Log:
    def __init__(self, path: Path):
        self.f = open(path, "a", encoding="utf-8", newline="\n")

    def __call__(self, text: str = "") -> None:
        print(text, flush=True)
        self.f.write(text + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def parse_seeds(text: str) -> tuple:
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
    if not seeds or any(s not in J.ALL_SEEDS for s in seeds):
        raise ValueError("seeds must lie within 1..5")
    return seeds


def load_params(path: str) -> Params:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("--params-json must hold a JSON object of Params fields")
    fields = {f.name: f for f in dataclasses.fields(Params)}
    kw = {}
    for k, v in data.items():
        if k not in fields:
            raise ValueError("unknown Params field %r" % k)
        default = getattr(DEFAULT_PARAMS, k)
        if isinstance(default, tuple) and isinstance(v, list):
            v = tuple(v)
        kw[k] = v
    return dataclasses.replace(DEFAULT_PARAMS, **kw)


def build_jobs(args) -> list:
    if args.jobs:
        catalogue = {j.name: j for j in J.PROFILES[args.profile or "p3"]}
        jobs = []
        for name in [n.strip() for n in args.jobs.split(",") if n.strip()]:
            jobs.append(catalogue[name] if name in catalogue else J.parse_job(name))
    else:
        jobs = list(J.PROFILES[args.profile or "smoke"])
    if args.nights is not None:
        jobs = [dataclasses.replace(j, nights=args.nights) for j in jobs]
    if args.seeds is not None:
        jobs = [dataclasses.replace(j, seeds=args.seeds) for j in jobs]
    for j in jobs:
        if not 1 <= j.nights <= J.MAX_NIGHTS:
            raise ValueError("nights must be 1..%d" % J.MAX_NIGHTS)
    return jobs


def run_g5b_unit_test():
    """The G5b unit test of the core (tests/test_profiles.py, tests whose name contains 'g5b'); None if absent."""
    import unittest
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_profiles")
    except Exception:
        return None
    picked = unittest.TestSuite()

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            elif "g5b" in t.id().lower():
                picked.addTest(t)
    walk(suite)
    if picked.countTestCases() == 0:
        return None
    res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(picked)
    return res.wasSuccessful()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_replay.py", allow_abbrev=False,
                                 description="P3 replay of the simulator world through the learner core.")
    ap.add_argument("--out", required=True, help="output folder (required; nothing is written elsewhere)")
    ap.add_argument("--profile", choices=sorted(J.PROFILES), default=None)
    ap.add_argument("--jobs", default=None, help="comma-separated job names, e.g. bed_w0000_0600,liv_none")
    ap.add_argument("--seeds", default=None, help="e.g. 1-5 or 1,3")
    ap.add_argument("--nights", type=int, default=None)
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--cpu-budget-per-day", type=float, default=6.0)
    ap.add_argument("--params-json", default=None)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        args.seeds = parse_seeds(args.seeds) if args.seeds is not None else None
        jobs = build_jobs(args)
        params = load_params(args.params_json) if args.params_json else DEFAULT_PARAMS
        if args.cpu_budget_per_day <= 0:
            raise ValueError("--cpu-budget-per-day must be > 0")
    except (ValueError, KeyError, OSError) as exc:
        print("usage error: %s" % exc, file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = Log(out / "log.txt")
    workers = max(1, min(args.workers, MAX_WORKERS, (os.cpu_count() or 2) - 1))
    runs = J.runs_of(jobs)
    days = J.estimate_days(jobs)
    log("P3 replay  %s  params %s  workers %d (asked %d)  budget %.1f s/day" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), params_digest(params), workers, args.workers, args.cpu_budget_per_day))
    log("job matrix:")
    for j in jobs:
        log("  %-26s world %s window %-11s nights %2d seeds %s%s" % (
            j.name, j.world, j.window or "none", j.nights, ",".join(map(str, j.seeds)), "" if j.gated else "  (reported)"))
    est_cpu = days * EST_CPU_PER_DAY
    log("runs %d, simulated days %.1f, estimated CPU %.0f s (%.1f min), wall about %.1f min on %d workers" % (
        len(runs), days, est_cpu, est_cpu / 60, est_cpu / 60 / max(1, min(workers, len(runs))), workers))
    if days > MAX_SIM_DAYS:
        log("refused: %.0f simulated days exceed the %d-day limit of one invocation" % (days, MAX_SIM_DAYS))
        log.close()
        return 2

    order = sorted(runs, key=lambda js: (-js[0].sim_days(), js[0].name, js[1]))
    tasks = [(j, s, params, args.cpu_budget_per_day, str(out)) for j, s in order]
    t0 = time.perf_counter()
    summaries = []
    ctx = multiprocessing.get_context("spawn")
    pool = ctx.Pool(workers, maxtasksperchild=1)
    try:
        for k, summ in enumerate(pool.imap_unordered(run_task, tasks), 1):
            summaries.append(summ)
            tm = summ.get("timing") or {}
            log("[%d/%d] %s s%d %s  cpu %s s  %s s/day  wall %s s" % (
                k, len(tasks), summ["job"], summ["seed"], summ["status"], tm.get("cpu_s"), tm.get("cpu_s_per_day"),
                tm.get("wall_s")))
        pool.close()
    except KeyboardInterrupt:
        pool.terminate()
        log("interrupted")
        log.close()
        return 2
    finally:
        pool.join()
    wall = time.perf_counter() - t0

    results = []
    for summ in sorted(summaries, key=lambda s: (s["job"], s["seed"])):
        results.append(json.loads(Path(summ["path"]).read_text(encoding="utf-8")))
    unit = run_g5b_unit_test()
    report = evaluate(results, unit_g5b=unit)
    report["profile"] = args.profile
    report["wall_s"] = round(wall, 1)
    write_report(report, out)
    cpu_total = sum((r.get("timing") or {}).get("cpu_s") or 0 for r in results)
    sim_total = sum((r.get("timing") or {}).get("sim_days") or 0 for r in results)
    timing = {"workers": workers, "wall_s": round(wall, 1), "cpu_s": round(cpu_total, 1),
              "sim_days": round(sim_total, 2),
              "cpu_s_per_day": round(cpu_total / sim_total, 3) if sim_total else None,
              "estimate": {"cpu_s_per_day": EST_CPU_PER_DAY, "cpu_s": round(est_cpu, 1)},
              "over_2x_estimate": bool(sim_total and cpu_total / sim_total > 2 * EST_CPU_PER_DAY),
              "runs": [{"job": r["job"], "seed": r["seed"], "status": r["status"], **(r.get("timing") or {})}
                       for r in results]}
    (out / "timing.json").write_text(json.dumps(timing, indent=1), encoding="utf-8")
    log("")
    for c in report["criteria"]:
        log("%-4s %-10s %s" % (c["id"], c["status"], c["title"]))
    log("gate: %s%s; CPU %.0f s for %.1f simulated days (%.2f s/day); wall %.0f s" % (
        report["gate"], (" (failed %s)" % ",".join(report["failed"])) if report["failed"] else "",
        cpu_total, sim_total, timing["cpu_s_per_day"] or 0, wall))
    if report["bad_runs"]:
        log("runs not ok: %s" % ", ".join("%s s%s %s" % (b["job"], b["seed"], b["status"]) for b in report["bad_runs"]))
    log("report: %s" % (out / "report.md"))
    log.close()
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
