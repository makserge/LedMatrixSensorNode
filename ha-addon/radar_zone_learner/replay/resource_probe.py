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

``--p5`` (check P5-R3, P5 spec 11.2, 0): the cost of the P5 additions on a synthetic full state (200 suggestions, 100 zone
records, 100 labels, 400 tags and snoozes, 200 suppressions with masks, 50 real-place markers; the learner is made to read
"learning" and two confirmed tracks are put in its tracker, because the live view needs them):

    python -I -B replay/resource_probe.py --p5 --out DIR

(a) a 1 s ``Learner.tick``: median <= 5 ms, maximum <= 20 ms (the tick that also computes the periodic status, whose label
precision and recall are P3 code, is timed apart: ``tick_with_status_ms``), and a second ``items()`` call in the same state
<= 0.2 ms;
(b) one live frame (``live_view`` + ``where_am_i`` + the markers over the cached base + ``encode_png``): <= 15 ms on average
over 100 frames, and one base rebuild (``review_map`` + ``render_review``) <= 40 ms; (c) ``export_state`` + an atomic save
(write to a temporary file, ``os.replace``) <= 80 ms; (d) the memory the P5 data adds to the core (tracemalloc: the full
state against the same state without the ``p5`` section and the zone extras; the whole state's growth over an empty core is
reported as ``memory_total_mb``) <= 2 MB. Results go to ``DIR/p5_probe.json``.
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


# ---------------------------------------------------------------- P5-R3: the cost of the P5 additions
P5_LIMITS = {"tick_median_ms": 5.0, "tick_max_ms": 20.0, "items_cached_ms": 0.2, "live_frame_avg_ms": 15.0,
             "base_rebuild_ms": 40.0, "export_save_ms": 80.0, "memory_growth_mb": 2.0}
P5_ZONE_EXTRAS = ("poly_cm", "rev", "upd", "srev", "also")


def _blob(i0: int, j0: int, w: int = 3, h: int = 3) -> list:
    """The cells (row-major index, 70 columns) of a w x h block with its corner at column i0, row j0."""
    return sorted((j0 + dj) * 70 + i0 + di for dj in range(h) for di in range(w))


def heavy_state(night: int = 30) -> dict:
    """A valid state with every P5 cap near full: 200 suggestions (88 rejected, 100 covered by zone records, 12 open places),
    100 zone records, 100 labels, 400 tags, 400 snoozes, 200 suppressions with masks (112 of them for suggestions that are
    gone) and 50 real-place markers. Built from a fresh learner's export so that the file passes the learner's validation."""
    import datetime

    from radar_zone_learner.core.grids import rle_encode
    from radar_zone_learner.core.learner import Learner
    from radar_zone_learner.core.p5state import tag_text
    from replay.framegen import BASE_MS

    st = Learner().export_state()
    day0 = datetime.date(2026, 1, 5)
    last = day0 + datetime.timedelta(days=night)
    st["meta"].update(created_date=day0.isoformat(), created_ms=BASE_MS, night=night,
                      last_extraction_date=last.isoformat(), next_label_n=101)

    def xy(i, j):
        return round(-7.0 + (i + 1.5) * 0.2, 3), round((j + 1.5) * 0.2, 3)

    def sid(k):
        return "s%04d-1" % (k + 1)

    sugg = []
    for k in range(200):
        i, j = 3 + 3 * (k % 20), 3 + 3 * (k // 20)
        cx, cy = xy(i, j)
        typ = "dwell_zone" if k < 160 else ("short_task_zone" if k < 180 else ("reflection" if k < 190 else "ignore_zone"))
        sugg.append({
            "id": sid(k), "rev": 1, "type": typ, "kind": "dwell" if typ == "dwell_zone" else "short_task"
            if typ == "short_task_zone" else ("reflection" if typ == "reflection" else "ignore"), "name_hint": None,
            "state": "shadow", "first_night": 4, "last_night": night, "cx": cx, "cy": cy,
            "cells": rle_encode(_blob(i, j)), "poly_cm": [[int(cx * 100) - 30, int(cy * 100) - 30],
                                                         [int(cx * 100) + 30, int(cy * 100) - 30],
                                                         [int(cx * 100) + 30, int(cy * 100) + 30],
                                                         [int(cx * 100) - 30, int(cy * 100) + 30]],
            "conf": round(0.9 - k * 0.003, 3),
            "evidence": {"n": 12, "hours_wd": [0.0] * 8 + [12.0, 20.0, 15.0, 6.0] + [0.0] * 12,
                         "hours_we": [0.0] * 24, "hours_at_suggestion": [0.0] * 24, "he_share": 0.0},
            "variants": [], "variant_evidence_split": False, "variant_share_split": False,
            "variant_suppressed": False, "frozen": False})
    st["suggestions"] = sugg
    st["labels"] = []
    for k in range(100):
        i, j = 2 * (k % 35), 32 + 2 * (k // 35)
        cx, cy = xy(i, j)
        st["labels"].append({"id": "l%04d" % (k + 1), "created_s": BASE_MS // 1000, "x": cx, "y": cy,
                             "name": "Spot %d" % k, "kind": "manual", "cells": rle_encode(_blob(i, j, 2, 2))})
    st["zones"] = []
    for k in range(100):
        s = sugg[100 + k]
        st["zones"].append({"zid": "z%04d" % (k + 1), "name": "Zone %d" % k, "kind": "dwell", "src": "approve",
                            "ref": s["id"], "cx": s["cx"], "cy": s["cy"], "cells": s["cells"], "night": 9,
                            "created_s": BASE_MS // 1000, "poly_cm": s["poly_cm"], "rev": 0, "upd": 9, "srev": 1,
                            "also": []})
    rejected = [sid(k) for k in range(88)] + [sid(k) for k in range(200, 312)]
    st["suppressions"] = [{"type": "place", "night": 12, "ref": r} for r in rejected]
    masks = {}
    for n, r in enumerate(rejected):
        k = int(r[1:5]) - 1
        i, j = 3 + 3 * (k % 20), 3 + 3 * (k // 20)
        j = min(j, 36)
        cx, cy = xy(i, j)
        masks[r] = {"cx": cx, "cy": cy, "cells": rle_encode(_blob(i, j)), "n0": 12}
    ids = [sid(k) for k in range(200)] + ["mrg-" + sid(k) for k in range(200)]
    st["p5"] = {
        "later": {k: "2026-01-01" for k in ids},
        "tags": {"by_id": {k: tag_text(n) for n, k in enumerate(ids)}, "n": len(ids)},
        "real": [{"ref": sid(180 + k % 10), "cx": sugg[180 + k % 10]["cx"], "cy": sugg[180 + k % 10]["cy"],
                  "cells": sugg[180 + k % 10]["cells"], "night": 10} for k in range(50)],
        "masks": masks, "nw_applied": None,
        "push": {"seq": 3, "acked": 3, "kind": "daily", "setup_sent": True, "last_day": last.isoformat(),
                 "sig": [], "acks": [], "head_id": None, "head_rev": None},
        "settings": {"approve_sched": False, "guests_pause": False, "pets": False, "detail_text": False}}
    return st


class _FakeTrack:
    """A confirmed live track for the live-view cost (the tracker's RAM objects are not part of any state file)."""
    confirmed = True
    bh = None

    def __init__(self, x: float, y: float):
        self.x, self.y = x, y


def _ms(fn, n: int = 1) -> list:
    pt = time.perf_counter
    out = []
    for _ in range(n):
        a = pt()
        fn()
        out.append((pt() - a) * 1000.0)
    return out


def p5_probe(out: Path) -> dict:
    """The measurements of P5-R3; writes ``out/p5_probe.json`` and returns the report."""
    import gc
    import os
    import statistics
    import tracemalloc

    from radar_zone_learner.core.learner import Learner
    from radar_zone_learner.core.params import DEFAULT_P5, DEFAULT_PARAMS
    from radar_zone_learner.core.types import FixedOffsetTime, StatusChanged
    from radar_zone_learner.shell.png import encode_png
    from radar_zone_learner.shell.render import draw_live, render_review
    from replay.framegen import BASE_MS, DAY_MS

    state = heavy_state()

    def loaded(st: dict):
        """(Learner loaded from ``st``, the bytes tracemalloc says it added); the state dict itself is dropped first."""
        gc.collect()
        tracemalloc.start()
        a = tracemalloc.get_traced_memory()[0]
        s = json.loads(json.dumps(st))
        lr = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), s, DEFAULT_P5)
        del s
        gc.collect()
        b = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        return lr, b - a

    # (d) memory: the core with the state of the P4 shape (no p5 section, no zone extras) and with every P5 cap full
    p4_shape = json.loads(json.dumps(state))
    p4_shape.pop("p5")
    for z in p4_shape["zones"]:
        for k in P5_ZONE_EXTRAS:
            z.pop(k, None)
    L4, m4 = loaded(p4_shape)
    del L4, p4_shape
    _, m0 = loaded(Learner().export_state())
    L, m1 = loaded(state)
    res = {"state_bytes": len(json.dumps(state, separators=(",", ":"))), "limits": dict(P5_LIMITS),
           "memory_growth_mb": round((m1 - m4) / 1048576.0, 2),
           "memory_total_mb": round((m1 - m0) / 1048576.0, 2), "memory_p4_shape_mb": round((m4 - m0) / 1048576.0, 2)}
    # (a) a 1 s tick: the review check, the Advanced list and the push planner on the full state. The periodic status
    # (every ``status_period_ms``) also computes the label precision and recall (P3 code, labels x suggestions): it is
    # timed apart and not part of the bound
    t = BASE_MS + 30 * DAY_MS + 12 * 3600 * 1000
    L.tick(t)
    ticks, status_ticks = [], []
    for k in range(1, 301):
        a = time.perf_counter()
        outs = L.tick(t + k * 1000)
        d = (time.perf_counter() - a) * 1000.0
        (status_ticks if any(isinstance(o, StatusChanged) for o in outs) else ticks).append(d)
    ticks = ticks[3:]
    res["tick_median_ms"] = round(statistics.median(ticks), 3)
    res["tick_max_ms"] = round(max(ticks), 3)
    res["tick_with_status_ms"] = round(max(status_ticks), 3) if status_ticks else None
    res["status_ticks"] = len(status_ticks)
    items1 = _ms(lambda: L.review.items(L), 1)[0]
    res["items_first_ms"] = round(items1, 3)
    res["items_cached_ms"] = round(min(_ms(lambda: L.review.items(L), 20)), 4)
    res["open_items"] = len(L.review.items(L))
    # (b) a live frame and a base rebuild
    L._state_now = lambda: "learning"                  # the probe has no frame stream: say "learning" so the view reads
    L.tracker.live = [_FakeTrack(1.0, 2.0), _FakeTrack(-2.3, 4.9)]
    sel = None
    data = L.review_map(sel)
    base = render_review(data)
    live = L.live_view()
    L.tracker.live = [_FakeTrack(-2.3, 4.9)]           # one person: where_am_i looks up the items and zones
    frames = []
    for _ in range(100):
        a = time.perf_counter()
        pts = L.live_view()
        L.where_am_i(sel)
        w, h, pal, px = draw_live(base, data, pts or live)
        encode_png(w, h, pal, px)
        frames.append((time.perf_counter() - a) * 1000.0)
    res["live_frame_avg_ms"] = round(sum(frames) / len(frames), 3)
    res["live_frame_max_ms"] = round(max(frames), 3)
    rebuild = _ms(lambda: render_review(L.review_map(sel)), 5)
    res["base_rebuild_ms"] = round(statistics.median(rebuild), 3)
    res["base_rebuild_max_ms"] = round(max(rebuild), 3)
    res["png_bytes"] = len(encode_png(*draw_live(base, data, live)))
    # (c) export + an atomic save
    path = out / "p5_state_probe.json"
    tmp = out / "p5_state_probe.json.tmp"

    def save():
        text = json.dumps(L.export_state(), separators=(",", ":"))
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    save()
    saves = _ms(save, 5)
    res["export_save_ms"] = round(statistics.median(saves), 3)
    res["export_save_max_ms"] = round(max(saves), 3)
    res["saved_bytes"] = path.stat().st_size
    checks = {k: {"value": res[k], "limit": P5_LIMITS[k], "pass": res[k] <= P5_LIMITS[k]} for k in P5_LIMITS}
    res["checks"] = checks
    res["pass"] = all(c["pass"] for c in checks.values())
    (out / "p5_probe.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="resource_probe.py", allow_abbrev=False)
    ap.add_argument("--p5", action="store_true", help="P5-R3: the cost of the P5 additions on a synthetic full state")
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
    if args.p5:
        if not args.out:
            print("usage error: --out is required", file=sys.stderr)
            return 2
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        res = p5_probe(out)
        print("P5-R3 on a full state of %d bytes (%d open items)" % (res["state_bytes"], res["open_items"]))
        for k, c in res["checks"].items():
            print("%-20s %10s  limit %-6s %s" % (k, c["value"], c["limit"], "PASS" if c["pass"] else "FAIL"))
        print("live frame max %s ms, base rebuild max %s ms, tick max %s ms, saved %d bytes, png %d bytes" % (
            res["live_frame_max_ms"], res["base_rebuild_max_ms"], res["tick_max_ms"], res["saved_bytes"],
            res["png_bytes"]))
        print("probe: %s" % (out / "p5_probe.json"))
        return 0 if res["pass"] else 1
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
