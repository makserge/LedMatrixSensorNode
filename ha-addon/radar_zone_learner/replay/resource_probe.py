"""CPU and memory estimate of the learner core on this PC (P3 spec 12.2).

    python -I -B replay/resource_probe.py --out DIR [--nights 18] [--tail-days 3]

1. Runs ``bed_w0000_0600`` seed 1 for 18 nights to a warm state (14 days of stays, COVIS filled) and writes it
   to ``DIR/warm_state.json``; writes the events of the following days (to the end of day 20) to
   ``DIR/events.jsonl.gz``, streamed (frames, home_empty, node status; ticks are re-made by the child).
2. Starts fresh child processes (``python -I -B resource_probe.py --child ...``) that load the warm state and
   feed the events to a ``Learner``:
   * ``cpu``: core CPU (``time.process_time()`` around the learner calls) per simulated day and the peak RSS
     (``shell.resources.peak_rss_mb``: VmHWM on Linux, PeakWorkingSetSize on Windows), without tracemalloc;
   * ``mem``: the ``tracemalloc`` peak (a separate child, because tracemalloc slows Python and adds memory).
3. Pass: core CPU <= 86.4 s per simulated day (1% of one CM4 core with the x10 factor), child peak RSS < 40 MB,
   tracemalloc peak < 15 MB. Prints the projected CM4 share ``cpu_s_per_day x 10 / 86,400``.

Windows CPython has another baseline than Python on Alpine; the measurement on the CM4 (P3 spec 12.3) is the
acceptance. Results go to ``DIR/probe.json``.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ADDON, so "radar_zone_learner" and "replay" import

import argparse  # noqa: E402
import gzip  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

TICK_MS = 10_000
CPU_LIMIT_S_PER_DAY = 86.4
RSS_LIMIT_MB = 40.0
TRACE_LIMIT_MB = 15.0
CM4_FACTOR = 10.0


def make_inputs(out: Path, nights: int, tail_days: int) -> dict:
    from radar_zone_learner.core.types import ContextState, FramesMessage, NodeStatus
    from replay import jobs as J
    from replay.driver import Run
    from replay.framegen import BASE_MS, DAY_MS

    end_day = nights + tail_days
    job = J.parse_job("bed_w0000_0600", nights=end_day)
    t0 = time.process_time()
    run = Run(job, 1, horizon_days=end_day + 2)
    status = run.run(stop_night=nights)
    if status != "ok":
        raise SystemExit("warm-up run ended with status %s" % status)
    state = run.learner.export_state()
    (out / "warm_state.json").write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    start_ms = run.now
    end_ms = BASE_MS + end_day * DAY_MS
    n = 0
    with gzip.open(out / "events.jsonl.gz", "wt", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"k": "start", "r": start_ms, "end": end_ms}) + "\n")
        for recv, _, ev in run.tail(end_ms):
            if isinstance(ev, FramesMessage):
                rec = {"k": "f", "r": recv, "p": ev.payload.decode("ascii")}
            elif isinstance(ev, ContextState):
                rec = {"k": "c", "r": recv, "n": ev.name, "v": ev.value}
            elif isinstance(ev, NodeStatus):
                rec = {"k": "n", "r": recv, "v": ev.online}
            else:
                continue
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            n += 1
    return {"warm_nights": nights, "events": n, "start_ms": start_ms, "end_ms": end_ms,
            "sim_days": (end_ms - start_ms) / DAY_MS, "prep_cpu_s": round(time.process_time() - t0, 1),
            "state_bytes": (out / "warm_state.json").stat().st_size}


def child(mode: str, state_path: Path, events_path: Path, result_path: Path) -> int:
    import tracemalloc
    if mode == "mem":
        tracemalloc.start()
    from radar_zone_learner.core.learner import Learner
    from radar_zone_learner.core.params import DEFAULT_PARAMS
    from radar_zone_learner.core.types import ContextState, FixedOffsetTime, FramesMessage, NodeStatus
    from radar_zone_learner.shell.resources import peak_rss_mb, rss_mb

    state = json.loads(state_path.read_text(encoding="utf-8"))
    learner = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), state=state)
    del state
    pt = time.process_time
    core = 0.0
    calls = 0
    t_all = pt()
    next_tick = None
    start = end = None
    with gzip.open(events_path, "rt", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            k = rec["k"]
            if k == "start":
                start, end = rec["r"], rec["end"]
                next_tick = (start // TICK_MS + 1) * TICK_MS
                continue
            r = rec["r"]
            while next_tick <= r:
                a = pt()
                learner.tick(next_tick)
                core += pt() - a
                calls += 1
                next_tick += TICK_MS
            if k == "f":
                ev = FramesMessage(rec["p"].encode("ascii"), r)
            elif k == "c":
                ev = ContextState(rec["n"], rec["v"], r)
            else:
                ev = NodeStatus(rec["v"], r)
            a = pt()
            learner.handle(ev)
            core += pt() - a
            calls += 1
    total = pt() - t_all
    days = (end - start) / 86_400_000 if start is not None else 0.0
    out = {"mode": mode, "sim_days": round(days, 3), "calls": calls, "core_cpu_s": round(core, 3),
           "core_cpu_s_per_day": round(core / days, 3) if days else None, "child_cpu_s": round(total, 3),
           "rss_mb": rss_mb(), "peak_rss_mb": peak_rss_mb(), "status": learner.status().state,
           "counters": learner.counters()}
    if mode == "mem":
        cur, peak = tracemalloc.get_traced_memory()
        out["tracemalloc_peak_mb"] = round(peak / 1048576.0, 2)
        out["tracemalloc_current_mb"] = round(cur / 1048576.0, 2)
        tracemalloc.stop()
    result_path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="resource_probe.py", allow_abbrev=False)
    ap.add_argument("--out", help="output folder (required in the parent)")
    ap.add_argument("--nights", type=int, default=18)
    ap.add_argument("--tail-days", type=int, default=3)
    ap.add_argument("--child", choices=("cpu", "mem"), default=None)
    ap.add_argument("--state")
    ap.add_argument("--events")
    ap.add_argument("--result")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.child:
        return child(args.child, Path(args.state), Path(args.events), Path(args.result))
    if not args.out or not 1 <= args.nights <= 28 or not 1 <= args.tail_days <= 7:
        print("usage error: --out is required; --nights 1..28, --tail-days 1..7", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print("warm-up: bed_w0000_0600 seed 1, %d nights, then %d days of events" % (args.nights, args.tail_days), flush=True)
    prep = make_inputs(out, args.nights, args.tail_days)
    print("warm state %d B, %d events over %.2f simulated days (prep CPU %.1f s)" % (
        prep["state_bytes"], prep["events"], prep["sim_days"], prep["prep_cpu_s"]), flush=True)
    results = {}
    for mode in ("cpu", "mem"):
        res_path = out / ("child_%s.json" % mode)
        cmd = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child", mode,
               "--state", str(out / "warm_state.json"), "--events", str(out / "events.jsonl.gz"),
               "--result", str(res_path)]
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            print("child %s failed with exit code %d" % (mode, rc), file=sys.stderr)
            return 1
        results[mode] = json.loads(res_path.read_text(encoding="utf-8"))
    cpu = results["cpu"]["core_cpu_s_per_day"] or 0.0
    rss = results["cpu"]["peak_rss_mb"]
    trace = results["mem"].get("tracemalloc_peak_mb")
    checks = {
        "core_cpu_s_per_day": {"value": cpu, "limit": CPU_LIMIT_S_PER_DAY, "pass": cpu <= CPU_LIMIT_S_PER_DAY},
        "child_peak_rss_mb": {"value": rss, "limit": RSS_LIMIT_MB, "pass": rss is not None and rss < RSS_LIMIT_MB},
        "tracemalloc_peak_mb": {"value": trace, "limit": TRACE_LIMIT_MB,
                                "pass": trace is not None and trace < TRACE_LIMIT_MB},
    }
    cm4 = cpu * CM4_FACTOR / 86400.0 * 100.0
    report = {"prep": prep, "children": results, "checks": checks, "projected_cm4_pct_of_one_core": round(cm4, 4),
              "pass": all(c["pass"] for c in checks.values())}
    (out / "probe.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for k, c in checks.items():
        print("%-22s %10s  limit %-6s %s" % (k, c["value"], c["limit"], "PASS" if c["pass"] else "FAIL"))
    print("projected CM4 share: %.4f %% of one core (x%.0f); margin %.0fx on the CPU limit" % (
        cm4, CM4_FACTOR, (CPU_LIMIT_S_PER_DAY / cpu) if cpu else float("inf")))
    print("probe: %s" % (out / "probe.json"))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
