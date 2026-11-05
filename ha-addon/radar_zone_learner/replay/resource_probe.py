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

``--p6`` (check P6-R3, P6 spec 11.2): the cost of the zone evaluator with 100 zone records, 100 labels and their ``zone6`` (50 spells,
LD2412 evidence, variants) and 3 tracks at 10 Hz walking along rows of zones:

    python -I -B replay/resource_probe.py --p6 --out DIR

(a) the evaluator costs at most 8.3 microseconds per detection on this PC (the same 9,000 detections through the learner with the
100 + 100 zones and through one without zones; the minimum of three alternating repeats; gated for three people WALKING across the
zones, the stress in which a zone changes state every second or two; the figure for three people sitting in three zones is
reported next to it): 25 % of the P3 target (1 % of one CM4 core =
2.5 ms a second for 30 detections a second = 83 microseconds) divided by the CM4 factor 10 (the conservative end of the x5-10 of the
design); the report also gives the x5 value (16.6 microseconds); the 24 h measurement on the CM4 (A12) is the arbiter; (b) the peak RSS of
a child process that holds only the learner with the full state stays under 40 MB; (c) ``export_state`` + an atomic save with the full ``zone6`` is reported; (d) ``Learner.handle`` for a
20-frame batch with 3 tracks inside the zones is reported. Results go to ``DIR/p6_probe.json``.

``--p7`` (check P7-R3, P7 spec 9.1, DV14): the cost of the P7 additions on the P6 probe's state plus 14 covered dates of 300 stays (4,200
in all), 5,000 door events, 400 static-clutter events, the coverage of 42 dates and 50 moved-from masks:

    python -I -B replay/resource_probe.py --p7 --out DIR

(a) the four nightly P7 steps (drift, release, radar, tuning: the module functions the learner calls are timed) take at most 0.5 s together, at
most 0.5 s more than the whole nightly run of a P6 learner (P7 disabled) on the same state, and the radar-moved step at most 0.25 s (the minimum of
three alternating repeats); (b) the peak RSS of a child process that holds only the P7 learner with the full state, after a nightly run and
5 minutes of walking people, stays under 40 MB; (c) the per-frame cost of the P6 probe's walking people is unchanged with P7 enabled (the minimum of
five alternating repeats within 2 %: P7 does not touch the per-frame path); (d) ``export_state`` + an atomic save with 200 zone entries and
50 masks is reported. Results go to ``DIR/p7_probe.json``.
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


# ---------------------------------------------------------------- P6-R3 (P6 spec 11.2): the cost of the zone evaluator
P6_LIMITS = {"evaluator_us_per_detection": 8.3, "rss_mb": 40.0}
P6_CM4_ALLOWANCE_US = 83.0            # 25 % of the P3 target (1 % of one core) at 30 detections a second, on the CM4
P6_CM4_FACTORS = (5.0, 10.0)          # CD:ha_side 'CPU / RAM on the CM4': x5-10 slower than this PC (an inference there)


def zone6_state(state: dict) -> dict:
    """The ``zone6`` section of a full state: every zone record and label has 50 spells, LD2412 evidence, up to 3 hour ranges and
    6 variants; the first 24 have tuning and a variant request (P6 spec 6.4)."""
    ids = [z["zid"] for z in state["zones"]] + [lb["id"] for lb in state["labels"]]
    z6 = {"v": 1, "set": {"epoch": 0}, "tune": {}, "delay": {}, "ld": {}, "ldr": [], "info": {}, "ve": {}}
    for n, zid in enumerate(ids):
        z6["delay"][zid] = {"sp": [30 + (n * 7 + k * 11) % 500 for k in range(50)], "cd": 300, "upd": 9}
        z6["ld"][zid] = [5400 + n, 6000 + n]
        if n % 2 == 0:
            z6["ldr"].append(zid)
        var = [{"d": "wd" if k % 2 == 0 else "we", "s": 6 + k, "e": 8 + k, "n": 12} for k in range(6)]
        z6["info"][zid] = {"th": {"wd": ["09-12", "13-17", "20-23"], "we": ["10-12"]}, "var": var, "conf": 0.8, "upd": 9}
        if n < 24:
            z6["tune"][zid] = {"cd": 120, "mg": 25}
            z6["ve"][zid] = ["wd0608"]
    return z6


SIT_AT = ((-6.1, 3.9), (-5.5, 3.9), (-4.9, 3.9))       # the centres of three zones of ``heavy_state`` (records 101-103)


def synthetic_batches(n_batches: int, frames: int, boot_id: str = "00c0ffee", t0_dev: int = 100_000, tracks: int = 3,
                      mode: str = "walk") -> list:
    """Payload texts of ``n_batches`` batches of ``frames`` frames (10 Hz) with ``tracks`` people, pw 3, LD2412 present:
    ``walk`` (the stress: they walk along rows of zones at 0.6 m/s, back and forth, so zones change state all the time) or
    ``sit`` (each sits in one zone: the evaluator has work but no state changes). ``[(dev_ms_of_the_first_frame, payload)]``."""
    from replay.batcher import Batch, format_frames
    out = []
    seq = 0
    ms = t0_dev
    rows = (3.9, 4.5, 5.1)[:tracks]
    for _ in range(n_batches):
        b = Batch()
        b.s, b.n = seq, frames
        b.first_ms, b.last_ms = ms, ms + (frames - 1) * 100
        for f in range(frames):
            t = (ms + f * 100) / 1000.0
            tg = []
            for slot, y in enumerate(rows):
                if mode == "sit":
                    x, y = SIT_AT[slot]
                    x += 0.01 * ((f + slot) % 3 - 1)
                    tg.append((slot, int(round(x * 1000)), int(round(y * 1000)), 0))
                    continue
                ph = (t * 0.6 + slot * 3.0) % 22.0
                x = -6.0 + (ph if ph < 11.0 else 22.0 - ph)
                tg.append((slot, int(round(x * 1000)), int(round(y * 1000)), 60))
            b.f.append((seq + f, ms + f * 100, tuple(tg)))
        text = format_frames(b, b.last_ms + 40, boot_id, 1, 1, 3, "A")
        out.append((ms, text))
        seq += frames
        ms += frames * 100
    return out


def _drive(learner, batches: list, base_ms: int, time_calls: bool = True):
    """Feeds the batches (a tick every second of simulated time); returns the CPU seconds spent in the ``handle`` calls."""
    from radar_zone_learner.core.types import FramesMessage
    spent = 0.0
    pc = time.perf_counter
    next_tick = None
    for dev_ms, text in batches:
        recv = base_ms + dev_ms
        if next_tick is None:
            next_tick = recv
        while next_tick <= recv:
            learner.tick(next_tick)
            next_tick += 1000
        a = pc()
        learner.handle(FramesMessage(text.encode("ascii"), recv))
        spent += pc() - a
    return spent


def _build_p6(state: dict, base_ms: int):
    from radar_zone_learner.core.learner import Learner
    from radar_zone_learner.core.params import DEFAULT_P5, DEFAULT_P6, DEFAULT_PARAMS
    from radar_zone_learner.core.types import FixedOffsetTime, NodeStatus
    lr = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), json.loads(json.dumps(state)), DEFAULT_P5, DEFAULT_P6)
    lr.handle(NodeStatus(True, base_ms))
    return lr


def _p6_base_ms() -> int:
    from replay.framegen import BASE_MS
    return BASE_MS + 12 * 3600 * 1000 + 30 * 24 * 3600 * 1000            # noon of a day after the state's last night


def p6_child(state_path: Path, result_path: Path) -> int:
    """Child process of the P6 probe: loads the full state (100 zone records, 100 labels, zone6), feeds the walking and the sitting
    people and reports this process's peak RSS (the learner alone, nothing of the probe's own bookkeeping)."""
    from radar_zone_learner.shell.resources import peak_rss_mb, rss_mb
    base_ms = _p6_base_ms()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    lr = _build_p6(state, base_ms)
    del state
    _drive(lr, synthetic_batches(60, 5), base_ms)
    _drive(lr, synthetic_batches(600, 5, t0_dev=130_000), base_ms)
    _drive(lr, synthetic_batches(600, 5, t0_dev=130_000 + 600 * 500, mode="sit"), base_ms)
    result_path.write_text(json.dumps({"peak_rss_mb": peak_rss_mb(), "rss_mb": rss_mb()}), encoding="utf-8")
    return 0


def p6_probe(out: Path) -> dict:
    """The measurements of P6-R3; writes ``out/p6_probe.json`` and returns the report."""
    import copy
    import gc
    import os
    import statistics

    from radar_zone_learner.core.types import FramesMessage

    state = heavy_state()
    state["p5"]["zone6"] = zone6_state(state)
    bare = copy.deepcopy(state)
    bare["zones"], bare["labels"] = [], []
    bare["p5"].pop("zone6", None)
    base_ms = _p6_base_ms()
    res = {"zones": len(state["zones"]) + len(state["labels"]), "limits": dict(P6_LIMITS)}
    warm = synthetic_batches(60, 5)                                       # 30 s: the 12 s warm-up and the first frames
    detections = 600 * 5 * 3
    # (a) the evaluator's cost per detection: the same frames through the learner with 100 zones and 100 labels and through one
    # with none (the minimum of three alternating repeats), for people who walk across the zones (the stress: a zone changes
    # state every second or two) and for people who sit in three zones (no state change)
    cost = {}
    for mode in ("walk", "sit"):
        measure = synthetic_batches(600, 5, t0_dev=130_000, mode=mode)    # 5 simulated minutes, 3 tracks, 10 Hz
        with_z, without = [], []
        for _ in range(3):
            b = _build_p6(bare, base_ms)
            _drive(b, warm, base_ms)
            without.append(_drive(b, measure, base_ms))
            del b
            a = _build_p6(state, base_ms)
            _drive(a, warm, base_ms)
            with_z.append(_drive(a, measure, base_ms))
            zs = a.zone_states()
            res["zones_valid_" + mode] = bool(getattr(zs, "valid", False))
            res["zones_occupied_" + mode] = sum(1 for z in zs.zones if z.occ) if hasattr(zs, "zones") else 0
            del a
            gc.collect()
        cost[mode] = (min(with_z) - min(without)) / detections * 1e6
        res["with_zones_s_" + mode] = round(min(with_z), 4)
        res["without_zones_s_" + mode] = round(min(without), 4)
    res.update(detections=detections, evaluator_us_per_detection=round(cost["walk"], 2),
               evaluator_us_per_detection_sit=round(cost["sit"], 2), cm4_us_at_x5=round(cost["walk"] * P6_CM4_FACTORS[0], 1),
               cm4_us_at_x10=round(cost["walk"] * P6_CM4_FACTORS[1], 1), cm4_allowance_us=P6_CM4_ALLOWANCE_US)
    # (d) one 20-frame batch with 3 tracks inside the zones, through the learner with the zones
    a = _build_p6(state, base_ms)
    _drive(a, warm, base_ms)
    big = synthetic_batches(100, 20, t0_dev=130_000 + 600 * 500)
    times = []
    for dev_ms, text in big:
        t = time.perf_counter()
        a.handle(FramesMessage(text.encode("ascii"), base_ms + dev_ms))
        times.append((time.perf_counter() - t) * 1000.0)
    res["batch20_median_ms"] = round(statistics.median(times), 3)
    res["batch20_max_ms"] = round(max(times), 3)
    # (c) the state save with the full zone6 (export, JSON, atomic write)
    path = out / "p6_state_probe.json"
    tmp = out / "p6_state_probe.json.tmp"

    def save():
        text = json.dumps(a.export_state(), separators=(",", ":"))
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    save()
    saves = _ms(save, 5)
    res["export_save_ms"] = round(statistics.median(saves), 3)
    res["export_save_max_ms"] = round(max(saves), 3)
    res["saved_bytes"] = path.stat().st_size
    res["zone6_bytes"] = len(json.dumps(state["p5"]["zone6"], separators=(",", ":")))
    res["zone6_kept"] = bool((a.export_state().get("p5") or {}).get("zone6"))
    # (b) the peak RSS of a child that holds only the learner with the full state
    (out / "p6_full_state.json").write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    rpath = out / "p6_child.json"
    rc = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child", "p6", "--state",
                         str(out / "p6_full_state.json"), "--result", str(rpath)]).returncode
    rss = json.loads(rpath.read_text(encoding="utf-8")).get("peak_rss_mb") if rc == 0 and rpath.is_file() else None
    res["rss_mb"] = round(rss, 1) if rss is not None else None
    checks = {"evaluator_us_per_detection": {"value": res["evaluator_us_per_detection"], "limit": P6_LIMITS["evaluator_us_per_detection"],
                                             "pass": res["evaluator_us_per_detection"] <= P6_LIMITS["evaluator_us_per_detection"]},
              "rss_mb": {"value": res["rss_mb"], "limit": P6_LIMITS["rss_mb"],
                         "pass": res["rss_mb"] is not None and res["rss_mb"] < P6_LIMITS["rss_mb"]}}
    res["checks"] = checks
    res["pass"] = all(c["pass"] for c in checks.values())
    (out / "p6_probe.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


# ---------------------------------------------------------------- P7-R3 (P7 spec 9.1): the cost of the nightly P7 steps
P7_LIMITS = {"p7_nightly_s": 0.5, "p7_over_p6_s": 0.5, "radar_search_s": 0.25, "rss_mb": 40.0, "frame_cost_ratio": 1.02}
P7_STEP_NAMES = ("drift", "release", "radar", "tuning")


def p7_state(night: int = 30) -> dict:
    """``heavy_state`` (100 zone records, 100 labels, the P5 caps) plus ``zone6``, 14 covered dates of 300 stays each (4,200 stays at the
    centres of the 200 zones and labels), 5,000 door events, 400 static-clutter events, the coverage of the newest 42 dates under a
    night-off schedule (the radars off 00:00-06:00) and 50 moved-from masks in ``zone7``: the state of P7-R3."""
    import datetime

    from radar_zone_learner.core.grids import rle_encode

    st = heavy_state(night)
    st["p5"]["zone6"] = zone6_state(st)
    last = datetime.date(2026, 1, 5) + datetime.timedelta(days=night)
    ord0 = last.toordinal()

    def epoch(d_ord: int, sec: int) -> int:
        return (d_ord - 719163) * 86400 + sec

    cov = {}
    for d in range(ord0 - 42, ord0):
        cov[datetime.date.fromordinal(d).isoformat()] = [0.0] * 6 + [3600.0] * 18
    st["coverage"] = cov
    centres = [(z["cx"], z["cy"]) for z in st["zones"]] + [(lb["x"], lb["y"]) for lb in st["labels"]]
    stays = []
    for di, d in enumerate(range(ord0 - 14, ord0)):
        iso = datetime.date.fromordinal(d).isoformat()
        for k in range(300):
            cx, cy = centres[(k * 7 + di) % len(centres)]
            t0 = epoch(d, (7 + (k * 3) % 16) * 3600 + (k % 12) * 300)
            dur = 120 + (k % 10) * 60
            stays.append({"t0": t0, "t1": t0 + dur // 300 * 300 + 300, "dur": dur, "x": int(round(cx * 100)) + (k % 5) - 2,
                          "y": int(round(cy * 100)) + (k % 3) - 1, "sd": 8 + k % 6, "nf": 1 + k % 4, "ld": round(0.5 + (k % 5) / 10.0, 2),
                          "he": 0.0, "exit": "moving", "arr": "walk", "day": iso})
    st["stays"] = stays
    doors = []
    sites = ((4.4, 2.6), (-6.0, 1.5), (0.5, 7.4))
    for di, d in enumerate(range(ord0 - 14, ord0)):
        for k in range(358):
            sx, sy = sites[k % 3]
            doors.append({"t": epoch(d, 6 * 3600 + k * 150), "x": int(round(sx * 100)) + (k % 7) * 4 - 12,
                          "y": int(round(sy * 100)) + (k % 5) * 4 - 8, "k": "b" if k % 2 == 0 else "d"})
    st["door_events"] = doors[:5000]
    st["ghost_events"] = [{"t": epoch(ord0 - 14 + k // 30, 8 * 3600 + (k % 30) * 600), "x": 200 + (k % 4), "y": 80 + (k % 3), "dur": 900,
                           "why": "identical", "ld": 0.0, "arr": "static"} for k in range(400)]
    masks = []
    for k in range(50):
        i, j = 3 + 3 * (k % 20), 3 + 3 * (k // 20)
        masks.append({"zid": "z%04d" % (k + 1), "cells": rle_encode(_blob(i, j)), "cx": round(-7.0 + (i + 1.5) * 0.2, 3),
                      "cy": round((j + 1.5) * 0.2, 3), "day": ord0 - 3 - k % 20, "n0": 12, "why": "moved_from" if k % 2 == 0 else "retired"})
    st["p5"]["zone7"] = {"v": 1, "mf": masks}
    return st


def _build_p7(state: dict, base_ms: int, on: bool):
    from radar_zone_learner.core.learner import Learner
    from radar_zone_learner.core.params import DEFAULT_P5, DEFAULT_P6, DEFAULT_PARAMS, P7Params
    from radar_zone_learner.core.types import FixedOffsetTime, NodeStatus
    lr = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), json.loads(json.dumps(state)), DEFAULT_P5, DEFAULT_P6, P7Params(enabled=on))
    lr.handle(NodeStatus(True, base_ms))
    return lr


NIGHTLY_TICK_MS = 600_000                 # a tick every 10 minutes is enough to cross 03:30 (a tick costs milliseconds, 1,440 a day)


def _to_nightly(lr, state: dict):
    """Ticks ``lr`` every 10 minutes from 03:31 of the state's last run until the first ``NightlyDone``; returns ``(now, outs, seconds the
    nightly tick took)`` or ``(now, None, None)`` when no nightly run happened within 26 hours."""
    from radar_zone_learner.core.types import NightlyDone
    from replay.compat_check import idle_start_ms
    now = idle_start_ms(state)
    for _ in range(26 * 3600 * 1000 // NIGHTLY_TICK_MS):
        now += NIGHTLY_TICK_MS
        t = time.perf_counter()
        outs = lr.tick(now)
        dt = time.perf_counter() - t
        if any(isinstance(o, NightlyDone) for o in outs):
            return now, outs, dt
    return now, None, None


def _nightly_cost(state: dict, on: bool) -> dict:
    """Ticks a learner built on ``state`` (every 10 minutes from 03:31 of the state's last run) until the first ``NightlyDone``; returns the
    seconds the nightly tick took and, for an enabled learner, the seconds of each of the four P7 steps (the module functions the
    learner calls are wrapped for the duration)."""
    from radar_zone_learner.core import drift, movedfrom, radarmoved, tuningagg
    from radar_zone_learner.core.types import NightlyFailed
    from replay.compat_check import idle_start_ms

    base = idle_start_ms(state)
    lr = _build_p7(state, base - 3600 * 1000, on)
    steps = dict.fromkeys(P7_STEP_NAMES, 0.0)
    targets = (("drift", drift, "nightly"), ("release", movedfrom, "release"), ("radar", radarmoved, "nightly"),
               ("tuning", tuningagg, "nightly"))
    saved = []
    for name, mod, attr in targets:
        orig = getattr(mod, attr)
        saved.append((mod, attr, orig))

        def wrapper(*a, _o=orig, _n=name, **kw):
            t = time.perf_counter()
            try:
                return _o(*a, **kw)
            finally:
                steps[_n] += time.perf_counter() - t
        setattr(mod, attr, wrapper)
    spent, failed = None, 0
    try:
        _, outs, spent = _to_nightly(lr, state)
        if outs is not None:
            failed = sum(isinstance(o, NightlyFailed) for o in outs)
    finally:
        for mod, attr, orig in saved:
            setattr(mod, attr, orig)
    return {"nightly_s": spent, "steps_s": steps, "failed": failed, "p7_errors": (lr.counters() or {}).get("p7_errors")}


def p7_child(state_path: Path, result_path: Path, on: bool = True) -> int:
    """Child process of the P7 probe: loads the full P7 state with P7 enabled, runs one nightly and feeds the walking people; reports
    this process's peak RSS (the learner alone)."""
    from radar_zone_learner.shell.resources import peak_rss_mb, rss_mb
    from replay.compat_check import idle_start_ms
    state = json.loads(state_path.read_text(encoding="utf-8"))
    base_ms = idle_start_ms(state)
    lr = _build_p7(state, base_ms - 3600 * 1000, on)
    now, _, _ = _to_nightly(lr, state)
    del state
    base2 = now + 12 * 3600 * 1000
    _drive(lr, synthetic_batches(60, 5), base2)
    _drive(lr, synthetic_batches(600, 5, t0_dev=130_000), base2)
    result_path.write_text(json.dumps({"peak_rss_mb": peak_rss_mb(), "rss_mb": rss_mb()}), encoding="utf-8")
    return 0


def p7_probe(out: Path) -> dict:
    """The measurements of P7-R3; writes ``out/p7_probe.json`` and returns the report."""
    import gc
    import os
    import statistics

    state = p7_state()
    res = {"zones": len(state["zones"]) + len(state["labels"]), "stays": len(state["stays"]), "door_events": len(state["door_events"]),
           "ghost_events": len(state["ghost_events"]), "masks": len(state["p5"]["zone7"]["mf"]), "limits": dict(P7_LIMITS)}
    # (a) the nightly run of a P6 learner (P7 disabled) and of a P7 learner on the same state: the minimum of three alternating repeats
    p6s, p7s, steps = [], [], []
    failed = 0
    for _ in range(3):
        a = _nightly_cost(state, False)
        b = _nightly_cost(state, True)
        gc.collect()
        p6s.append(a["nightly_s"])
        p7s.append(b["nightly_s"])
        steps.append(b["steps_s"])
        failed += (a["failed"] or 0) + (b["failed"] or 0) + (b["p7_errors"] or 0)
    if None in p6s or None in p7s:
        res.update(error="no nightly run happened", checks={}, **{"pass": False})
        (out / "p7_probe.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        return res
    best = {k: min(s[k] for s in steps) for k in P7_STEP_NAMES}
    p7_total = min(sum(s.values()) for s in steps)
    res.update(nightly_p6_s=round(min(p6s), 3), nightly_p7_s=round(min(p7s), 3), p7_steps_s={k: round(v, 4) for k, v in best.items()},
               p7_steps_total_s=round(p7_total, 4), nightly_failed_or_errors=failed,
               p7_other_s=round(min(p7s) - min(p6s) - p7_total, 4))      # what else the P7 learner's nightly tick does (the item plan)
    # (c) the per-frame cost is unchanged: the P6 probe's walking people through the P6 full state, P7 disabled and enabled
    s6 = heavy_state()
    s6["p5"]["zone6"] = zone6_state(s6)
    base_ms = _p6_base_ms()
    warm = synthetic_batches(60, 5)
    measure = synthetic_batches(600, 5, t0_dev=130_000)
    off, on = [], []
    for _ in range(5):
        for flag, acc in ((False, off), (True, on)):
            lr = _build_p7(s6, base_ms, flag)
            _drive(lr, warm, base_ms)
            acc.append(_drive(lr, measure, base_ms))
            del lr
            gc.collect()
    detections = 600 * 5 * 3
    res.update(frame_off_s=round(min(off), 4), frame_on_s=round(min(on), 4), frame_cost_ratio=round(min(on) / min(off), 4),
               frame_delta_us_per_detection=round((min(on) - min(off)) / detections * 1e6, 3))
    # (d) the state save with 100 zones, 100 labels and 50 masks (export, JSON, atomic write) after a nightly run
    from replay.compat_check import idle_start_ms
    lr = _build_p7(state, idle_start_ms(state) - 3600 * 1000, True)
    _to_nightly(lr, state)
    path = out / "p7_state_probe.json"
    tmp = out / "p7_state_probe.json.tmp"

    def save():
        text = json.dumps(lr.export_state(), separators=(",", ":"))
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    save()
    saves = _ms(save, 5)
    z7 = (lr.export_state().get("p5") or {}).get("zone7") or {}
    res.update(export_save_ms=round(statistics.median(saves), 3), export_save_max_ms=round(max(saves), 3), saved_bytes=path.stat().st_size,
               zone7_bytes=len(json.dumps(z7, separators=(",", ":"))), zone7_entries=len(z7.get("z") or {}), zone7_masks=len(z7.get("mf") or []))
    # (b) the peak RSS of a child process that holds only the learner with the full state
    (out / "p7_full_state.json").write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    rss = {}
    for child in ("p7", "p7off"):                     # the P7 learner and, on the same state, the P6 learner (P7 disabled)
        rpath = out / ("%s_child.json" % child)
        rc = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child", child, "--state",
                             str(out / "p7_full_state.json"), "--result", str(rpath)]).returncode
        rss[child] = json.loads(rpath.read_text(encoding="utf-8")).get("peak_rss_mb") if rc == 0 and rpath.is_file() else None
    res["rss_mb"] = round(rss["p7"], 1) if rss["p7"] is not None else None
    res["rss_mb_p6"] = round(rss["p7off"], 1) if rss["p7off"] is not None else None
    res["rss_p7_increment_mb"] = round(rss["p7"] - rss["p7off"], 1) if rss["p7"] is not None and rss["p7off"] is not None else None
    checks = {
        "p7_nightly_s": {"value": res["p7_steps_total_s"], "limit": P7_LIMITS["p7_nightly_s"], "pass": res["p7_steps_total_s"] <= P7_LIMITS["p7_nightly_s"]},
        "p7_over_p6_s": {"value": round(res["nightly_p7_s"] - res["nightly_p6_s"], 4), "limit": P7_LIMITS["p7_over_p6_s"],
                         "pass": res["nightly_p7_s"] <= res["nightly_p6_s"] + P7_LIMITS["p7_over_p6_s"]},
        "radar_search_s": {"value": res["p7_steps_s"]["radar"], "limit": P7_LIMITS["radar_search_s"],
                           "pass": res["p7_steps_s"]["radar"] <= P7_LIMITS["radar_search_s"]},
        "rss_mb": {"value": res["rss_mb"], "limit": P7_LIMITS["rss_mb"], "pass": res["rss_mb"] is not None and res["rss_mb"] < P7_LIMITS["rss_mb"]},
        "frame_cost_ratio": {"value": res["frame_cost_ratio"], "limit": P7_LIMITS["frame_cost_ratio"],
                             "pass": res["frame_cost_ratio"] <= P7_LIMITS["frame_cost_ratio"]},
        "no_failed_step": {"value": failed, "limit": 0, "pass": failed == 0},
    }
    res["checks"] = checks
    res["pass"] = all(c["pass"] for c in checks.values())
    (out / "p7_probe.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="resource_probe.py", allow_abbrev=False)
    ap.add_argument("--p5", action="store_true", help="P5-R3: the cost of the P5 additions on a synthetic full state")
    ap.add_argument("--p6", action="store_true", help="P6-R3: the cost of the zone evaluator with 100 zones and 3 tracks at 10 Hz")
    ap.add_argument("--p7", action="store_true", help="P7-R3: the cost of the nightly P7 steps with 100 zones, 4,200 stays and 5,000 door events")
    ap.add_argument("--out", help="output folder (required in the parent)")
    ap.add_argument("--nights", type=int, default=18)
    ap.add_argument("--tail-days", type=int, default=3)
    ap.add_argument("--child", choices=("cpu", "mem", "p6", "p7", "p7off"), default=None)
    ap.add_argument("--state")
    ap.add_argument("--events")
    ap.add_argument("--result")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.child == "p6":
        return p6_child(Path(args.state), Path(args.result))
    if args.child in ("p7", "p7off"):
        return p7_child(Path(args.state), Path(args.result), on=args.child == "p7")
    if args.p7:
        if not args.out:
            print("usage error: --out is required", file=sys.stderr)
            return 2
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        res = p7_probe(out)
        if res.get("error"):
            print("P7-R3: %s" % res["error"], file=sys.stderr)
            return 1
        print("P7-R3 with %d zones, %d stays, %d door events, %d masks: nightly of a P6 learner %.3f s, of a P7 learner %.3f s; the P7 steps %s "
              "(total %.4f s)" % (res["zones"], res["stays"], res["door_events"], res["masks"], res["nightly_p6_s"], res["nightly_p7_s"],
                                  res["p7_steps_s"], res["p7_steps_total_s"]))
        for k, c in res["checks"].items():
            print("%-20s %10s  limit %-6s %s" % (k, c["value"], c["limit"], "PASS" if c["pass"] else "FAIL"))
        print("per detection: %s microseconds more with P7 enabled; save median %s ms, %d bytes (zone7 %d bytes, %d entries, %d masks)" % (
            res["frame_delta_us_per_detection"], res["export_save_ms"], res["saved_bytes"], res["zone7_bytes"], res["zone7_entries"],
            res["zone7_masks"]))
        print("peak RSS of the child: %s MB with P7, %s MB with P7 disabled on the same state (P7 adds %s MB; Windows CPython has a larger "
              "baseline than the CM4's Alpine Python, the 24 h run on the CM4 is the arbiter)" % (
                  res["rss_mb"], res["rss_mb_p6"], res["rss_p7_increment_mb"]))
        print("probe: %s" % (out / "p7_probe.json"))
        return 0 if res["pass"] else 1
    if args.child:
        return child(args.child, Path(args.state), Path(args.events), Path(args.result))
    if args.p6:
        if not args.out:
            print("usage error: --out is required", file=sys.stderr)
            return 2
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        res = p6_probe(out)
        print("P6-R3 with %d zones: %d detections, evaluator %.2f microseconds per detection walking across the zones, %.2f sitting "
              "(CM4 x5 %.1f, x10 %.1f microseconds for the walking case; allowance %.0f)" % (
                  res["zones"], res["detections"], res["evaluator_us_per_detection"], res["evaluator_us_per_detection_sit"],
                  res["cm4_us_at_x5"], res["cm4_us_at_x10"], res["cm4_allowance_us"]))
        for k, c in res["checks"].items():
            print("%-30s %10s  limit %-6s %s" % (k, c["value"], c["limit"], "PASS" if c["pass"] else "FAIL"))
        print("20-frame batch median %s ms, max %s ms; save median %s ms, %d bytes (zone6 %d bytes, kept: %s)" % (
            res["batch20_median_ms"], res["batch20_max_ms"], res["export_save_ms"], res["saved_bytes"], res["zone6_bytes"],
            res["zone6_kept"]))
        print("probe: %s" % (out / "p6_probe.json"))
        return 0 if res["pass"] else 1
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
