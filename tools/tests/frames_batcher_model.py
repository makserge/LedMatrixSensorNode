#!/usr/bin/env python3
"""Host-side model test of the P1 frames stream: the ring (src/ld2454_frames.cpp Ld2454FrameRing), the
batcher (Ld2454FrameBatcher), the payload format (formatLd2454Frames) and the MqttTask glue
(MqttManager::serviceFrameStream / sendFrameBatch), mirrored line by line from the C++, driven by a
simulated radar, LD2454Task and MqttTask. Every published payload also goes through
tools/frames_checker.py, so the checker is tested against the same stream.

Run: python -I tools/tests/frames_batcher_model.py      (no third-party modules; exit code 0 = all passed)
"""

import contextlib
import importlib.util
import io
import json
import os
import random
import sys

sys.dont_write_bytecode = True   # loading the checker must not leave tools/__pycache__ in the repo

# constants.h (P1)
LD2454_RING_FRAMES = 64
LD2454_STREAM_BATCH_MS = 500
LD2454_STREAM_IDLE_MS = 5000
LD2454_STREAM_MAX_FRAMES = 20
LD2454_RESTART_GAP_MS = 1000
LD2454_SENSOR_INTERVAL = 50
MQTT_LOOP_DELAY_MS = 40
FRAMES_DRAIN_CHUNK = 16
LD2454_FRAMES_JSON_MAX = 2048
U32 = 0xFFFFFFFF


def _load_checker():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frames_checker.py")
    spec = importlib.util.spec_from_file_location("frames_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Frame:
    __slots__ = ("seq", "ms", "mask", "x", "y", "v")

    def __init__(self, seq, ms, mask, x, y, v):
        self.seq, self.ms, self.mask, self.x, self.y, self.v = seq, ms, mask, x, y, v


class Ring:
    """Ld2454FrameRing: drop-oldest, seq = frames pushed before this one."""

    def __init__(self):
        self.frames = [None] * LD2454_RING_FRAMES
        self.head = 0
        self.tail = 0
        self.overwritten = 0

    def push(self, targets, ms):   # targets: 3 x (valid, x, y, v)
        mask, xs, ys, vs = 0, [0, 0, 0], [0, 0, 0], [0, 0, 0]
        for i, (valid, x, y, v) in enumerate(targets):
            if valid:
                mask |= 1 << i
                xs[i], ys[i], vs[i] = x, y, v
        frame = Frame(self.head, ms & U32, mask, xs, ys, vs)
        if (self.head - self.tail) & U32 == LD2454_RING_FRAMES:
            self.tail = (self.tail + 1) & U32
            self.overwritten += 1
        self.frames[self.head % LD2454_RING_FRAMES] = frame
        self.head = (self.head + 1) & U32

    def pop(self, max_frames):
        out = []
        while len(out) < max_frames and self.tail != self.head:
            out.append(self.frames[self.tail % LD2454_RING_FRAMES])
            self.tail = (self.tail + 1) & U32
        return out


class Batch:
    def __init__(self):
        self.s = 0
        self.n = 0
        self.first_ms = 0
        self.last_ms = 0
        self.f = []

    def copy(self):
        b = Batch()
        b.s, b.n, b.first_ms, b.last_ms, b.f = self.s, self.n, self.first_ms, self.last_ms, list(self.f)
        return b


class Batcher:
    """Ld2454FrameBatcher."""

    def __init__(self):
        self.pending = Batch()
        self.prev_had_targets = False
        self.target_gate_open = True
        self.last_close_ms = 0
        self.last_target_close_ms = 0
        self.seq_gap_frames = 0

    def add(self, frame, now_ms):
        closed = None
        expected = (self.pending.s + self.pending.n) & U32
        if frame.seq != expected:
            self.seq_gap_frames += (frame.seq - expected) & U32
            if self.pending.n > 0:
                closed = self.close(now_ms)
            self.pending.s = frame.seq
        elif self.pending.n > 0 and ((frame.ms - self.pending.last_ms) & U32) >= LD2454_RESTART_GAP_MS:
            closed = self.close(now_ms)
        if self.pending.n == 0:
            self.pending.first_ms = frame.ms
        self.pending.last_ms = frame.ms
        self.pending.n += 1
        if frame.mask != 0:
            self.pending.f.append(frame)
            if len(self.pending.f) == LD2454_STREAM_MAX_FRAMES:
                assert closed is None
                closed = self.close(now_ms)
        return closed

    def poll(self, now_ms):
        if not self.target_gate_open and ((now_ms - self.last_target_close_ms) & U32) >= LD2454_STREAM_BATCH_MS:
            self.target_gate_open = True
        if self.pending.f and self.target_gate_open:
            return self.close(now_ms)
        if self.prev_had_targets and ((now_ms - self.last_close_ms) & U32) >= LD2454_STREAM_BATCH_MS:
            return self.close(now_ms)
        if ((now_ms - self.last_close_ms) & U32) >= LD2454_STREAM_IDLE_MS:
            return self.close(now_ms)
        return None

    def close(self, now_ms):
        closed = self.pending.copy()
        self.prev_had_targets = len(closed.f) > 0
        if self.prev_had_targets:
            self.last_target_close_ms = now_ms
            self.target_gate_open = False
        self.last_close_ms = now_ms
        self.pending.s = (closed.s + closed.n) & U32
        self.pending.n = 0
        self.pending.f = []
        return closed


def format_frames(batch, t_ms, boot_id, mt, p2, pw, sm):
    """formatLd2454Frames: the same text, character for character."""
    t32 = t_ms & U32
    out = '{"v":1,"b":"%s","t":%d,"s":%d,"n":%d' % (boot_id, t_ms, batch.s, batch.n)
    if batch.n > 0:
        out += ',"a0":%d,"a1":%d' % ((t32 - batch.first_ms) & U32, (t32 - batch.last_ms) & U32)
    out += ',"mt":%d,"p2":%d,"pw":%d,"sm":"%s"' % (1 if mt else 0, 1 if p2 else 0, pw, sm)
    if batch.f:
        out += ',"f":['
        for i, fr in enumerate(batch.f):
            out += '%s[%d,%d' % ("," if i else "", (fr.seq - batch.s) & U32, (t32 - fr.ms) & U32)
            for slot in range(3):
                if fr.mask & (1 << slot):
                    out += ',%d,%d,%d,%d' % (slot, fr.x[slot], fr.y[slot], fr.v[slot])
            out += ']'
        out += ']'
    out += '}'
    if len(out) + 1 > LD2454_FRAMES_JSON_MAX:
        return None
    return out


# ------------------------------------------------------------------ simulation
class Sim:
    """Radar at 10 frames/s -> UART -> LD2454Task (50 ms poll) -> ring -> MqttTask (40 ms loop)."""

    def __init__(self, seed, presence, duration_ms, mqtt_down=(), mqtt_stalls=(), radar_silences=(),
                 ota_pauses=(), publish_fail_ms=(), start_ms=7000):
        self.rng = random.Random(seed)
        self.presence = presence            # function(ms) -> list of (x, y, v) targets
        self.duration = duration_ms
        self.mqtt_down = mqtt_down          # [(start, end)]
        self.mqtt_stalls = mqtt_stalls      # [(start, length)]: MqttTask blocked
        self.radar_silences = radar_silences
        self.ota_pauses = ota_pauses        # [(start, length)]: both tasks parked, the UART keeps buffering
        self.publish_fail_ms = set(publish_fail_ms)
        self.start = start_ms
        self.ring = Ring()
        self.batcher = Batcher()
        self.boot_id = "%08x" % self.rng.getrandbits(32)
        self.published = []                 # (time, payload, closed batch)
        self.status_events = []             # (time, 'online'/'offline')
        self.dropped_offline = 0
        self.dropped_publish = 0
        self.skipped_after_fail = 0         # batches dropped unsent after a failed publish in the same call
        self.pushed = []                    # (seq, push time, has targets)
        self.uart = []                      # frames waiting in the UART buffer: (emit time, targets)
        self.ota_stale = []                 # per OTA resume: (resume poll time, frames buffered meanwhile)
        self.ota_muted = 0                  # frames parsed in the muted first update() after a resume

    def _in(self, ms, ranges):
        return any(a <= ms < b for a, b in ranges)

    def _ota(self, ms):
        return any(a <= ms < a + d for a, d in self.ota_pauses)

    def connected(self, ms):
        return not self._in(ms, self.mqtt_down) and not self._ota_long(ms)

    def _ota_long(self, ms):
        # the broker drops the node about 22 s into a pause (no keepalive); it reconnects after
        return any(a + 22000 <= ms < a + d + 2000 for a, d in self.ota_pauses if d > 22000)

    def run(self):
        events = []
        t = self.start
        while t < self.start + self.duration:            # radar frames
            jitter = self.rng.randint(-3, 3)
            if not self._in(t, self.radar_silences):
                events.append((t + jitter, 0, "frame"))
            t += 100
        t = self.start + self.rng.randint(0, 49)
        while t < self.start + self.duration + 6000:      # LD2454Task polls
            events.append((t, 1, "poll"))
            t += LD2454_SENSOR_INTERVAL
        t = self.start + self.rng.randint(0, 39)
        mqtt_times = []
        while t < self.start + self.duration + 12000:     # MqttTask loops
            mqtt_times.append(t)
            stall = next((d for a, d in self.mqtt_stalls if a <= t < a + MQTT_LOOP_DELAY_MS + 10), 0)
            t += MQTT_LOOP_DELAY_MS + self.rng.randint(0, 6) + stall
        for m in mqtt_times:
            events.append((m, 2, "mqtt"))
        events.sort(key=lambda e: (e[0], e[1]))

        was_connected = True
        was_ota = False
        for now, _, kind in events:
            ota = self._ota(now)
            if kind == "frame":
                if len(self.uart) < 34:                   # 1024-byte RX buffer = 34 frames
                    self.uart.append((now, self.presence(now)))
            elif kind == "poll":
                if ota:
                    was_ota = True
                    continue
                muted = False
                if was_ota:                               # the driver parses what the pause buffered, now,
                    self.ota_stale.append((now, len(self.uart)))   # with the ring muted for this update()
                    was_ota = False
                    muted = True
                for emit_ms, targets in self.uart:
                    if muted:
                        self.ota_muted += 1
                        continue
                    slots = [(True, x, y, v) for (x, y, v) in targets[:3]]
                    slots += [(False, 0, 0, 0)] * (3 - len(slots))
                    self.ring.push(slots, now)
                    self.pushed.append((self.ring.head - 1, now, bool(targets)))
                self.uart.clear()
            else:
                if ota:
                    continue
                conn = self.connected(now)
                if conn and not was_connected:
                    self.status_events.append((now, "online"))
                if not conn and was_connected:
                    # the broker publishes the will ~22 s into a long OTA pause, else the outage starts now
                    ota_start = next((a for a, d in self.ota_pauses if d > 22000 and a + 22000 <= now < a + d + 2000),
                                     None)
                    self.status_events.append((now if ota_start is None else ota_start + 22000, "offline"))
                was_connected = conn
                self.service(now, conn)
        return self

    def service(self, now, conn):
        state = {"publish_failed": False}             # serviceFrameStream()'s publishFailed
        drained = 0
        while drained < LD2454_RING_FRAMES:
            got = self.ring.pop(FRAMES_DRAIN_CHUNK)
            if not got:
                break
            drained += len(got)
            for fr in got:
                closed = self.batcher.add(fr, now)
                if closed is not None:
                    self.send(closed, now, conn, state)
        closed = self.batcher.poll(now)
        if closed is not None:
            self.send(closed, now, conn, state)

    def send(self, batch, now, conn, state):
        if not conn:
            self.dropped_offline += batch.n
            return
        if state["publish_failed"]:                   # a publish of this call already failed: drop unsent
            self.dropped_publish += batch.n
            self.skipped_after_fail += 1
            return
        payload = format_frames(batch, now, self.boot_id, True, False, 3, "A")
        if payload is None:
            self.dropped_publish += batch.n
            return
        if now in self.publish_fail_ms:
            self.dropped_publish += batch.n
            state["publish_failed"] = True
            return
        self.published.append((now, payload, batch))


def person(x0, y0, vx):
    def targets(ms):
        return [(x0 + int(vx * ms / 1000.0) % 2000, y0, int(vx / 10))]
    return targets


def run_checker(checker_mod, sim, extra_messages=()):
    chk = checker_mod.FrameStreamChecker()
    chk.on_connect(0.0, 0.0)
    msgs = [(t, sim_topic(chk), p.encode(), False) for t, p, _ in sim.published]
    msgs += [(t, chk.t_status, s.encode(), False) for t, s in sim.status_events]
    # what the node also sends: retained discovery at the start, LD2412 every 60 s (no change here)
    msgs += [(sim.start - 1, topic, payload, True) for topic, payload in checker_mod.EXPECTED_SENSOR_CONFIGS.items()]
    last = max([t for t, _, _ in sim.published] + [sim.start + sim.duration])   # MqttTask runs on past it
    msgs += [(t, chk.t_ld2412, b"0", False) for t in range(sim.start, last + 1, 60000) if sim.connected(t)]
    msgs += list(extra_messages)
    msgs.sort(key=lambda m: m[0])
    lines = []
    for t, topic, payload, retain in msgs:
        lines += chk.on_message(topic, payload, retain, 0, 1.7e9 + t / 1000.0, t / 1000.0)
    end = msgs[-1][0] + 1000
    lines += chk.tick(1.7e9 + end / 1000.0, end / 1000.0)
    return chk, lines


def sim_topic(chk):
    return chk.t_frames


# ------------------------------------------------------------------ checks
FAILS = []


def check(cond, text):
    print(("  ok    " if cond else "  FAIL  ") + text)
    if not cond:
        FAILS.append(text)


def common_invariants(name, sim):
    pubs = sim.published
    check(len(pubs) > 0, f"{name}: messages published ({len(pubs)})")
    sizes = [len(p) for _, p, _ in pubs]
    check(max(sizes) + 1 <= LD2454_FRAMES_JSON_MAX, f"{name}: largest payload {max(sizes)} B + NUL fits 2048 B")
    check(all(len(b.f) <= LD2454_STREAM_MAX_FRAMES for _, _, b in pubs), f"{name}: <= 20 listed frames per batch")
    # no batch spans a radar pause: consecutive covered frames < 1000 ms apart
    pushed_ms = {seq: ms for seq, ms, _ in sim.pushed}
    span_ok = True
    for _, _, b in pubs:
        times = [pushed_ms[s] for s in range(b.s, b.s + b.n) if s in pushed_ms]
        if any(t2 - t1 >= LD2454_RESTART_GAP_MS for t1, t2 in zip(times, times[1:])):
            span_ok = False
    check(span_ok, f"{name}: no batch spans a pause of >= {LD2454_RESTART_GAP_MS} ms")
    # accounting: published + dropped + overwritten + still pending = every pushed frame
    covered = sum(b.n for _, _, b in pubs)
    total = covered + sim.dropped_offline + sim.dropped_publish + sim.ring.overwritten + sim.batcher.pending.n \
        + ((sim.ring.head - sim.ring.tail) & U32)
    check(total == len(sim.pushed), f"{name}: frame accounting {total} == {len(sim.pushed)} pushed")
    check(sim.batcher.seq_gap_frames == sim.ring.overwritten,
          f"{name}: seq gaps seen by the batcher ({sim.batcher.seq_gap_frames}) == ring overflow "
          f"({sim.ring.overwritten})")


def test_worst_case():
    print("worst-case payload")
    t_ms = (2 ** 63 - 1) // 1000            # widest t: int64 microseconds / 1000, 16 digits
    old_ms = ((t_ms & U32) + 1) & U32       # every age = t32 - old_ms = 4294967295, 10 digits
    b = Batch()
    b.s, b.n, b.first_ms, b.last_ms = U32, U32, old_ms, old_ms
    for _ in range(LD2454_STREAM_MAX_FRAMES):   # seq - s = 4294967295 (k, 10 digits)
        b.f.append(Frame((U32 + U32) & U32, old_ms, 0b111, [-32767] * 3, [-32767] * 3, [-32767] * 3))
    payload = format_frames(b, t_ms, "ffffffff", True, True, 3, "A")
    check(payload is not None and len(payload) == 2001,
          f"worst case is {len(payload) if payload else None} B (2001 expected, the static_assert's figure)")


def test_example_format(checker_mod):
    print("format")
    b = Batch()
    b.s, b.n = 1234560, 5
    b.first_ms, b.last_ms = 81234567 - 480, 81234567 - 40
    b.f = [Frame(1234560, 81234567 - 480, 1, [-15, 0, 0], [1193, 0, 0], [55, 0, 0]),
           Frame(1234564, 81234567 - 40, 1, [-761, 0, 0], [5446, 0, 0], [58, 0, 0])]
    p = format_frames(b, 81234567, "9f3c21aa", True, True, 3, "A")
    expected = ('{"v":1,"b":"9f3c21aa","t":81234567,"s":1234560,"n":5,"a0":480,"a1":40,"mt":1,"p2":1,'
                '"pw":3,"sm":"A","f":[[0,480,0,-15,1193,55],[4,40,0,-761,5446,58]]}')
    check(p == expected, "the design's example payload is reproduced exactly (with sm 'A')")
    check(checker_mod.validate_frames(json.loads(p)) == [], "the checker accepts it")
    e = Batch()
    e.s = 1234565
    p0 = format_frames(e, 81264567, "9f3c21aa", True, False, 3, "A")
    check(p0 == '{"v":1,"b":"9f3c21aa","t":81264567,"s":1234565,"n":0,"mt":1,"p2":0,"pw":3,"sm":"A"}',
          "an n 0 heartbeat has no a0, a1 or f")
    check(checker_mod.validate_frames(json.loads(p0)) == [], "the checker accepts it")
    bad = json.loads(expected)
    bad["f"][1][1] = 500
    check(checker_mod.validate_frames(bad) != [], "the checker rejects an age outside a1..a0")


def test_continuous(checker_mod):
    print("continuous presence, 10 min")
    sim = Sim(1, person(-500, 1500, 400), 600000).run()
    common_invariants("continuous", sim)
    t = [m[0] for m in sim.published]
    target_t = [m[0] for m in sim.published if m[2].f]
    spacing = [b - a for a, b in zip(target_t, target_t[1:])]
    check(min(spacing) >= LD2454_STREAM_BATCH_MS, f"target batches >= 500 ms apart (min {min(spacing)} ms)")
    check(max(spacing) <= LD2454_STREAM_BATCH_MS + 60, f"... and <= 560 ms apart (max {max(spacing)} ms)")
    rate = len(t) / ((t[-1] - t[0]) / 1000.0)
    check(1.8 <= rate <= 2.05, f"rate {rate:.2f} msg/s (about 2)")
    first = sim.published[0]
    chk, lines = run_checker(checker_mod, sim)
    text, ok = chk.report()
    check(ok, "checker: PASS on the continuous stream")
    if not ok:
        print(text)


def test_intermittent_and_onset(checker_mod):
    print("intermittent presence (onset latency, trailing batch, heartbeat), 30 min")
    rng = random.Random(5)
    spans = []
    t = 10000
    while t < 1800000:
        quiet = rng.randint(2000, 60000)
        busy = rng.randint(300, 20000)
        spans.append((t + quiet, t + quiet + busy))
        t += quiet + busy

    def targets(ms):
        for a, b in spans:
            if a <= ms < b:
                return [] if rng.random() < 0.25 else [(300, 2000, 30), (-900, 3500, -12)][:rng.choice([1, 1, 2])]
        return []
    sim = Sim(2, targets, 1800000).run()
    common_invariants("intermittent", sim)
    # onset: first target frame after no target batch for >= 500 ms -> published at the next MqttTask loop
    pushed_ms = {seq: ms for seq, ms, _ in sim.pushed}
    last_target_close = -10 ** 9
    lat = []
    for now, _, b in sim.published:
        if b.f:
            first_target_push = pushed_ms[b.f[0].seq]
            if first_target_push - last_target_close >= LD2454_STREAM_BATCH_MS:
                lat.append(now - first_target_push)
            last_target_close = now
    lat.sort()
    check(lat and lat[-1] <= 80, f"onset published <= 80 ms after the frame reached the ring "
                                 f"({len(lat)} onsets, median {lat[len(lat) // 2]} ms, max {lat[-1]} ms)")
    # after the last target batch exactly one trailing batch follows ~500 ms later, then 5 s heartbeats
    ok_trailing = True
    ok_heartbeat = True
    pubs = sim.published
    for i in range(1, len(pubs) - 1):
        prev, cur = pubs[i - 1], pubs[i]
        if prev[2].f and not cur[2].f:
            if not LD2454_STREAM_BATCH_MS <= cur[0] - prev[0] <= LD2454_STREAM_BATCH_MS + 60:
                ok_trailing = False
        if not prev[2].f and not cur[2].f and i >= 2 and not pubs[i - 2][2].f:
            if not LD2454_STREAM_IDLE_MS <= cur[0] - prev[0] <= LD2454_STREAM_IDLE_MS + 60:
                ok_heartbeat = False
    check(ok_trailing, "a batch without targets follows the last target batch after 500-560 ms")
    check(ok_heartbeat, "heartbeats 5000-5060 ms apart while empty")
    chk, lines = run_checker(checker_mod, sim)
    text, ok = chk.report()
    check(ok, "checker: PASS on the intermittent stream")
    check(sorted(chk.onset_ages) == lat, f"checker: onset ages from the payloads equal the model's latencies "
                                         f"({len(chk.onset_ages)} onsets)")
    if not ok:
        print(text)


def test_outages(checker_mod):
    print("MQTT down 20 s, radar silent 3 s, MqttTask stalled 8 s, one failed publish, OTA pause 30 s")
    sim = Sim(3, person(200, 2500, -300), 240000,
              mqtt_down=[(60000, 80000)],
              radar_silences=[(100000, 103000)],
              mqtt_stalls=[(130000, 8000)],
              ota_pauses=[(170000, 30000)]).run()
    # one failed publish: re-run with a publish failure in the first MqttTask loop after the stall, which
    # closes several batches (ring overflow, 20 listed frames): the ones after the failure are not tried
    fail_at = next(t for t, _, b in sim.published if t > 138000)
    sim = Sim(3, person(200, 2500, -300), 240000,
              mqtt_down=[(60000, 80000)],
              radar_silences=[(100000, 103000)],
              mqtt_stalls=[(130000, 8000)],
              ota_pauses=[(170000, 30000)],
              publish_fail_ms=[fail_at]).run()
    common_invariants("outages", sim)
    check(sim.dropped_offline > 150, f"MQTT down: {sim.dropped_offline} frames dropped, not queued")
    check(sim.ring.overwritten > 0, f"stall of 8 s: ring overflow drops the oldest ({sim.ring.overwritten} frames)")
    check(sim.dropped_publish > 0, f"failed publish: {sim.dropped_publish} frames dropped")
    check(sim.skipped_after_fail > 0 and not any(t == fail_at for t, _, _ in sim.published),
          f"failed publish: the {sim.skipped_after_fail} later batches of the same call are dropped unsent")
    resume, buffered = sim.ota_stale[0]
    stale = [seq for seq, ms, _ in sim.pushed if ms == resume]
    check(buffered > 0 and not stale and sim.ota_muted == buffered,
          f"OTA pause: the {buffered} frames the UART buffered are kept out of the stream (muted, no seq)")
    check(any(ms > resume for _, ms, _ in sim.pushed),
          "OTA pause: fresh frames after the resume are streamed again")
    chk, lines = run_checker(checker_mod, sim)
    explained = [g for g in chk.gaps if g["explained"]]
    unexplained = [g for g in chk.gaps if not g["explained"]]
    check(sum(g["missing_frames"] for g in explained) == sim.dropped_offline,
          f"checker: gaps at reconnects = MQTT-down drops ({sim.dropped_offline} frames)")
    check(sum(g["missing_frames"] for g in unexplained) == sim.ring.overwritten + sim.dropped_publish,
          f"checker: gaps while connected = ring overflow + failed publish "
          f"({sim.ring.overwritten} + {sim.dropped_publish} frames), flagged FAIL")
    text, ok = chk.report()
    check(not ok, "checker: FAIL, because of the gaps while connected")


def test_checker_discovery_and_legacy(checker_mod):
    print("checker: discovery, legacy topics")
    chk = checker_mod.FrameStreamChecker()
    chk.on_connect(1.7e9, 0.0)
    for topic, payload in checker_mod.EXPECTED_SENSOR_CONFIGS.items():
        chk.on_message(topic, payload, True, 0, 1.7e9, 0.0)
    chk.on_message("homeassistant/sensor/Other_Device_x/config", b"{bad", True, 0, 1.7e9, 0.0)
    check(all(r["ok"] for r in chk.discovery.values()) and len(chk.discovery) == 4,
          "the four expected sensor configs pass; another device's broken config is ignored")
    sha = __import__("hashlib").sha256(checker_mod.EXPECTED_SENSOR_CONFIGS[
        "homeassistant/sensor/Led_Matrix_Node_temp/config"]).hexdigest()
    check(sha == "94a22aa1cba654329b64c83180ebb5a5d865146f295d3bcc8dd24d1d8ec0c1e2",
          "embedded temp config matches the bytes computed from the firmware (sha256)")
    altered = checker_mod.EXPECTED_SENSOR_CONFIGS["homeassistant/sensor/Led_Matrix_Node_co2/config"].replace(
        b'"uniq_id"', b'"avty_t":"x","uniq_id"')
    lines = chk.on_message("homeassistant/sensor/Led_Matrix_Node_co2/config", altered, False, 0, 1.7e9, 1.0)
    check(any("byte-identical" in l for l in lines), "a changed sensor config is reported as not byte-identical")
    lines = chk.on_message("led_matrix_node/radar/ld2454", b'{"targets":[]}', False, 0, 1.7e9, 2.0)
    check(any("FAIL" in l for l in lines), "a legacy LD2454 JSON fails in P1")
    chk2 = checker_mod.FrameStreamChecker()
    chk2.on_connect(1.7e9, 0.0)
    out = []
    for t, p in ((0, b"1"), (10, b"0"), (70, b"0"), (80, b"0"), (200, b"0")):
        out += chk2.on_message("led_matrix_node/radar/ld2412", p, False, 0, 1.7e9 + t, float(t))
    check(sum("repeated" in l for l in out) == 1 and sum("silent" in l for l in out) == 1,
          "LD2412: a repeat within 55 s and a 120 s silence are both reported")


def _frames_msg(boot, t_ms, s, n=50, **extra):
    obj = {"v": 1, "b": boot, "t": t_ms, "s": s, "n": n}
    if n:
        obj.update(a0=4990, a1=40)
    obj.update(mt=1, p2=0, pw=3, sm="A")
    obj.update(extra)
    return json.dumps(obj, separators=(",", ":")).encode()


def test_checker_silences_and_cadence(checker_mod):
    print("checker: silences, cadence, field edge cases, command line")
    F = "led_matrix_node/radar/ld2454/frames"
    L = "led_matrix_node/radar/ld2412"
    S = "led_matrix_node/sensors/state"
    ST = "led_matrix_node/sensors/status"

    def feed(chk, t, topic, payload, retain=False):
        return chk.on_message(topic, payload, retain, 0, 1.7e9 + t, float(t))

    # the frames stream and the LD2412 heartbeat stop after 2 min; only sensors/state goes on for 10 h
    chk = checker_mod.FrameStreamChecker()
    chk.on_connect(1.7e9, 0.0)
    for topic, payload in checker_mod.EXPECTED_SENSOR_CONFIGS.items():
        feed(chk, 0, topic, payload, True)
    out = feed(chk, 0, L, b"0")
    for i in range(24):
        out += feed(chk, 5 * (i + 1), F, _frames_msg("9f3c21aa", 5000 * (i + 1), 50 * i))
    for t in range(130, 36000, 10):
        out += feed(chk, t, S, b"{}")
    text, ok = chk.report()
    check(not ok and chk.frames_silences == 1 and chk.ld2412_silences == 1
          and sum("frames stream stopped" in l for l in out) == 1 and sum("LD2412 silent" in l for l in out) == 1,
          "a frames stream and an LD2412 heartbeat that stop for good are each reported once, and FAIL")

    # the same with no message at all: tick() finds it
    chk = checker_mod.FrameStreamChecker()
    chk.on_connect(1.7e9, 0.0)
    out = feed(chk, 0, F, _frames_msg("9f3c21aa", 5000, 0))
    for t in range(1, 300):
        out += chk.tick(1.7e9 + t, float(t))
    check(chk.frames_silences == 1 and chk.ld2412_silences == 1 and chk.failure_count == 2,
          "tick(): silences found without any message arriving")

    # node offline (LWT): no silence while it is gone; 'online' restarts the clocks
    chk = checker_mod.FrameStreamChecker()
    chk.on_connect(1.7e9, 0.0)
    feed(chk, 0, L, b"0")
    feed(chk, 5, F, _frames_msg("9f3c21aa", 5000, 0))
    feed(chk, 8, ST, b"offline")
    for t in range(9, 600):
        chk.tick(1.7e9 + t, float(t))
    feed(chk, 600, ST, b"online")
    feed(chk, 600, L, b"0")
    feed(chk, 603, F, _frames_msg("9f3c21aa", 603000, 6000))
    for t in range(604, 610):
        chk.tick(1.7e9 + t, float(t))
    check(chk.failure_count == 0, "no silence is reported while the node is offline (its will)")

    # a link loss: the will comes 22.5 s after the node's last packet, up to 27.5 s after its last frames
    # message; the silence before it is no failure, and the seq gap across the outage is explained
    chk = checker_mod.FrameStreamChecker()
    chk.on_connect(1.7e9, 0.0)
    feed(chk, 0, L, b"0")
    feed(chk, 5, F, _frames_msg("9f3c21aa", 5000, 0))
    out = []
    for t in range(6, 33):
        out += chk.tick(1.7e9 + t, float(t))
    out += feed(chk, 32.5, ST, b"offline")
    for t in range(33, 80):
        out += chk.tick(1.7e9 + t, float(t))
    out += feed(chk, 80, ST, b"online")
    out += feed(chk, 80, L, b"0")
    out += feed(chk, 81, F, _frames_msg("9f3c21aa", 81000, 760))
    check(chk.failure_count == 0 and len(chk.gaps) == 1 and chk.gaps[0]["explained"],
          "a link loss: the silence until the will is no failure, the seq gap is explained")

    # cadence by the node's t: 8 s between two messages in one epoch fails; across a reconnect it does not
    chk = checker_mod.FrameStreamChecker()
    chk.on_connect(1.7e9, 0.0)
    out = feed(chk, 5, F, _frames_msg("9f3c21aa", 5000, 0))
    out += feed(chk, 10, F, _frames_msg("9f3c21aa", 10000, 50))
    out += feed(chk, 18, F, _frames_msg("9f3c21aa", 18000, 100))
    out += feed(chk, 19, ST, b"online")
    out += feed(chk, 27, F, _frames_msg("9f3c21aa", 27000, 150))
    check(chk.frames_spacing_fails == 1 and chk.frames_spacing_max == 8000 and chk.frames_silences == 0,
          "a missed heartbeat (8 s by t) fails once; the same spacing across a node reconnect does not")

    # field edge cases
    check(checker_mod.validate_frames(json.loads(_frames_msg("9f3c21aa", 1000, 7, n=1, a0=480, a1=40))) != [],
          "n 1 with a0 != a1 is rejected")
    check(checker_mod.validate_frames(json.loads(_frames_msg("9f3c21aa", 1000, 7, n=1, a0=30, a1=30))) == [],
          "n 1 with a0 == a1 is accepted")
    errors = []
    for bad_sm in (["A"], {"x": 1}):
        try:
            errors.append(checker_mod.validate_frames(json.loads(_frames_msg("9f3c21aa", 1000, 7, sm=bad_sm))))
        except TypeError:
            errors.append(None)
    check(all(e for e in errors), "sm as a list or an object is an error, not an exception")
    chk = checker_mod.FrameStreamChecker()
    chk.on_connect(1.7e9, 0.0)
    out = feed(chk, 1, F, _frames_msg("9f3c21aa", 1000, 0), retain=True)
    check(chk.failure_count == 1 and "retained" in out[0], "a retained frames message fails")

    # command line: a password typed as an option is never echoed
    secret = "NOT_A_REAL_SECRET_123"
    for argv in (["--host", "127.0.0.1", "--user", "u", "--password", secret],
                 ["--host", "127.0.0.1", "--user", "u", "--pass=" + secret],
                 ["--host", "127.0.0.1", "--user", "u", "--password-env", secret + "!"],
                 ["--host", "127.0.0.1", "--user", "u", "--password-env", secret]):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = checker_mod.main(argv)
        shown = " ".join(a.replace(secret, "<value>") for a in argv[4:])
        check(rc == 2 and secret not in err.getvalue(), f"exit 2, value not echoed: {shown}")


def main():
    checker_mod = _load_checker()
    test_worst_case()
    test_example_format(checker_mod)
    test_continuous(checker_mod)
    test_intermittent_and_onset(checker_mod)
    test_outages(checker_mod)
    test_checker_discovery_and_legacy(checker_mod)
    test_checker_silences_and_cadence(checker_mod)
    print()
    print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
