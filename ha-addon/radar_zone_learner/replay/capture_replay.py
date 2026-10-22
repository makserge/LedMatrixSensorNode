"""Replay of a real MQTT capture through the learner core (P3 spec 11.10; OA 12 P3 "the P0 capture replayed").

    python -I -B replay/capture_replay.py --in capture.txt.gz --utc-offset-min 60 --out DIR [--png]
                                          [--node-prefix led_matrix_node] [--discovery-prefix homeassistant]
                                          [--params-json FILE]

Input, one message per line (``.gz`` allowed):

* text as printed by ``mosquitto_sub -v -F '%U %t %p'`` on the broker host: ``<unix_ts> <topic> <payload>``,
  the Unix time with fractional seconds (check that the host's mosquitto_sub prints ``%U`` that way), or
* JSON lines ``{"recv_ms": int, "topic": str, "payload": str}``.

Topics are mapped exactly as the app does (``shell.topics.TopicMapper``, pure, no paho). Local time comes
from ``--utc-offset-min`` (``FixedOffsetTime``; Windows Python has no time-zone database without the
``tzdata`` package). The learner gets the captured reception times and a tick every 10 s of capture time.
Each nightly run is printed with the suggestions; at the end ``DIR/state.json`` is written and, with
``--png``, the debug map ``DIR/debug_map.png`` (``shell.render`` + ``shell.png``). Nothing else is written.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ADDON, so "radar_zone_learner" and "replay" import

import argparse  # noqa: E402
import gzip  # noqa: E402
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from radar_zone_learner.core.learner import Learner  # noqa: E402
from radar_zone_learner.core.params import DEFAULT_PARAMS  # noqa: E402
from radar_zone_learner.core.types import (ContextState, FixedOffsetTime, FramesMessage,  # noqa: E402
                                           MarkPress, MarkResult, NightlyDone, NodeStatus, ScheduleState,
                                           SpotName)

INPUT_EVENTS = (FramesMessage, NodeStatus, ScheduleState, ContextState, MarkPress, SpotName)
from radar_zone_learner.shell.topics import TopicMapper, build_topics  # noqa: E402

TICK_MS = 10_000


def read_messages(path: Path):
    """Yield (recv_ms, topic, payload bytes) from a capture file; malformed lines are counted and skipped."""
    opener = gzip.open if path.suffix == ".gz" else open
    bad = 0
    with opener(path, "rb") as f:
        for raw in f:
            line = raw.rstrip(b"\r\n")
            if not line.strip():
                continue
            try:
                if line.lstrip().startswith(b"{"):
                    obj = json.loads(line)
                    payload = obj["payload"]
                    payload = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
                    yield int(obj["recv_ms"]), str(obj["topic"]), payload
                else:
                    parts = line.split(b" ", 2)
                    ts = float(parts[0].decode("ascii"))
                    topic = parts[1].decode("utf-8")
                    payload = parts[2] if len(parts) > 2 else b""
                    yield int(round(ts * 1000.0)), topic, payload
            except (ValueError, KeyError, IndexError, UnicodeDecodeError, TypeError):
                bad += 1
    read_messages.bad = bad


read_messages.bad = 0


def summarize_suggestions(learner) -> list:
    out = []
    for s in learner.suggestions():
        out.append("  %s %-15s %-10s %-6s nights %d-%d  (%.2f, %.2f) m  conf %.2f%s" % (
            s.id, s.type, s.kind, s.state, s.first_night, s.last_night, s.cx_m, s.cy_m, s.conf,
            ("  name " + s.name_hint) if s.name_hint else ""))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="capture_replay.py", allow_abbrev=False,
                                 description="Replay an MQTT capture through the learner core.")
    ap.add_argument("--in", dest="inp", required=True, help="capture file (.txt, .jsonl, optionally .gz)")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--utc-offset-min", type=int, default=0, help="local time = UTC + this many minutes")
    ap.add_argument("--png", action="store_true", help="also write the debug map PNG")
    ap.add_argument("--node-prefix", default="led_matrix_node")
    ap.add_argument("--discovery-prefix", default="homeassistant")
    ap.add_argument("--params-json", default=None, help="Params field overrides (diagnosis only)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    src = Path(args.inp)
    if not src.is_file():
        print("usage error: no such file: %s" % src, file=sys.stderr)
        return 2
    params = DEFAULT_PARAMS
    if args.params_json:
        from replay.run_replay import load_params
        try:
            params = load_params(args.params_json)
        except (ValueError, OSError) as exc:
            print("usage error: %s" % exc, file=sys.stderr)
            return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    mapper = TopicMapper(build_topics(SimpleNamespace(node_prefix=args.node_prefix,
                                                      discovery_prefix=args.discovery_prefix)))
    learner = Learner(params, FixedOffsetTime(args.utc_offset_min * 60))
    counts = {"messages": 0, "events": 0, "ignored": 0, "ticks": 0}
    next_tick = None

    def process(outs):
        for o in outs or ():
            if isinstance(o, NightlyDone):
                print("night %d (%s): %d candidates, %d new suggestions" % (o.night, o.date, o.candidates,
                                                                           o.new_suggestions))
                for line in summarize_suggestions(learner):
                    print(line)
            elif isinstance(o, MarkResult):
                print("mark %s %s %s" % ("ok" if o.ok else "failed", o.label_id or o.reason, o.kind or ""))

    last = None
    for recv_ms, topic, payload in read_messages(src):
        counts["messages"] += 1
        if next_tick is None:
            next_tick = recv_ms
        while next_tick <= recv_ms:
            process(learner.tick(next_tick))
            counts["ticks"] += 1
            next_tick += TICK_MS
        items = mapper.map(topic, payload, False, recv_ms)
        evs = [e for e in items if isinstance(e, INPUT_EVENTS)]      # HaBirth / ClearRetained are shell-only
        if not evs:
            counts["ignored"] += 1
        for ev in evs:
            counts["events"] += 1
            process(learner.handle(ev))
        last = recv_ms
    if last is not None:
        process(learner.tick(last + 1))
    state = learner.export_state()
    (out / "state.json").write_text(json.dumps(state, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")
    st = learner.status()
    print("messages %d, events %d, ignored %d, bad lines %d, ticks %d" % (
        counts["messages"], counts["events"], counts["ignored"], read_messages.bad, counts["ticks"]))
    print("status %s; ingest %s" % (st.state, json.dumps(learner.counters(), sort_keys=True)))
    print("suggestions:")
    for line in summarize_suggestions(learner):
        print(line)
    print("state: %s" % (out / "state.json"))
    if args.png:
        from radar_zone_learner.shell.png import encode_png
        from radar_zone_learner.shell.render import render
        w, h, palette, pixels = render(learner.debug_map())
        (out / "debug_map.png").write_bytes(encode_png(w, h, palette, pixels))
        print("debug map: %s" % (out / "debug_map.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
