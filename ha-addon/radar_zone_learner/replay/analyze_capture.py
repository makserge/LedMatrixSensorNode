"""Why does 'Mark spot here' say no_stay, or why does a zone turn on? Shows what the learner's tracker sees in a recording.

usage:  python -I -B replay/analyze_capture.py <file or folder> [more files]

Input (any mix): the JSON lines of the app's 'Debug capture' (capture-<UTC date-hour>.jsonl, one {"recv_ms", "topic", "payload"} per
line, also inside an 'Export debug data' folder), or one frames payload per line as written by `mosquitto_sub -t <node>/radar/ld2454/frames`.
A folder is read file by file (capture-*.jsonl, *.jsonl, *.txt) in name order.

For every confirmed track it prints the lifetime and the longest run that stays within frag_radius_m (0.5 m) of its start (the
'stay in progress' rule of 'Mark spot here': at least frag_min_s = 8 s and mark_min_stay_s = 20 s), plus the gaps of the detections
that are longer than the tracker's ttl (ttl_ms = 2000 ms), which end a track, and the walking speed of every track (m/s, from its 1 Hz
samples; the zone evaluator ignores a pass faster than 0.8 m/s). Read-only; nothing is sent anywhere.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_zone_learner.core.frames import parse_frames        # noqa: E402
from radar_zone_learner.core.params import DEFAULT_PARAMS      # noqa: E402
from radar_zone_learner.core.tracker import Tracker            # noqa: E402


def _payloads(paths) -> list:
    """The frames payloads (bytes) of the given files or folders, in order."""
    files = []
    for a in paths:
        q = Path(a)
        if q.is_dir():
            files += sorted(x for x in q.rglob("*") if x.is_file() and x.suffix in (".jsonl", ".txt", ".json"))
        else:
            files.append(q)
    out = []
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except ValueError:
                    obj = None
                if isinstance(obj, dict) and "payload" in obj:           # a Debug capture line
                    if "frames" not in str(obj.get("topic", "frames")):
                        continue
                    line = obj["payload"] if isinstance(obj["payload"], str) else json.dumps(obj["payload"])
            out.append(line.encode("utf-8"))
    return out


def main(paths) -> int:
    p = DEFAULT_PARAMS
    batches = []
    bad = 0
    for payload in _payloads(paths):
        try:
            batches.append(parse_frames(payload))
        except Exception:
            bad += 1
    if not batches:
        print("no valid frames payload in", ", ".join(paths), "(%d lines rejected)" % bad)
        return 1
    print("%d batches, %d lines rejected, boot ids %s" % (len(batches), bad, sorted({b.boot for b in batches})))
    print("pw (bit0 LD2454 observing, bit1 LD2412): %s   sm: %s" % (sorted({b.pw for b in batches}), sorted({b.sm for b in batches})))
    dets_t = []                                                  # (device ms, [(x_m, y_m)])
    for b in batches:
        for f in b.frames:
            dets_t.append((b.t_ms - f.age_ms, [(t.x_mm / 1000.0, t.y_mm / 1000.0) for t in f.targets]))
    dets_t.sort(key=lambda a: a[0])
    n_listed = len(dets_t)
    with_t = [d for d in dets_t if d[1]]
    print("listed frames: %d, with a target: %d (%.0f %%); frames in all batches: %d" % (
        n_listed, len(with_t), 100.0 * len(with_t) / max(1, n_listed), sum(b.n for b in batches)))
    if not with_t:
        print("the radar reports no target at all in this recording: nothing to track")
        return 0
    t0 = with_t[0][0]
    gaps = []
    for (a, _), (b, _) in zip(with_t, with_t[1:]):
        if b - a > p.ttl_ms:
            gaps.append((a - t0, b - a))
    print("detection gaps longer than ttl (%d ms), which end a track: %d" % (p.ttl_ms, len(gaps)))
    for at, g in gaps[:25]:
        print("   at +%.1f s: %.1f s without a target" % (at / 1000.0, g / 1000.0))
    tracker = Tracker(p)
    samples = []
    per_track = {}
    for dev, dets in dets_t:
        tracker.step(dev, dets, 0, None, samples)
        for tr, t, x, y in samples:
            per_track.setdefault(tr.id, []).append((t, x, y))
        samples.clear()
    print("confirmed tracks: %d" % len(per_track))
    best = 0.0
    for tid, s in sorted(per_track.items()):
        life = s[-1][0] - s[0][0]
        run = 0.0
        i = 0
        for j in range(len(s)):
            while math.hypot(s[j][1] - s[i][1], s[j][2] - s[i][2]) > p.frag_radius_m:
                i += 1
            run = max(run, s[j][0] - s[i][0])
        best = max(best, run)
        sp = [math.hypot(b[1] - a[1], b[2] - a[2]) / max(0.001, b[0] - a[0]) for a, b in zip(s, s[1:])]
        sp_txt = "max %.2f m/s" % max(sp) if sp else "n/a"
        print("   track %d: %d samples over %.0f s from +%.0f s, speed %s, longest run within %.1f m: %.0f s%s" % (
            tid, len(s), life, s[0][0] - t0 / 1000.0, sp_txt, p.frag_radius_m, run,
            "   <-- would be a stay (>= %d s)" % p.mark_min_stay_s if run >= p.mark_min_stay_s else ""))
    print()
    if best >= p.mark_min_stay_s:
        print("RESULT: a stay of %.0f s exists in this recording; 'Mark spot here' should have worked during it." % best)
    else:
        print("RESULT: the longest stay is %.0f s, under the %d s that 'Mark spot here' needs: the radar loses the person for"
              " longer than %d ms or the person moves more than %.1f m." % (best, p.mark_min_stay_s, p.ttl_ms, p.frag_radius_m))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) if len(sys.argv) > 1 else print(__doc__) or 2)
