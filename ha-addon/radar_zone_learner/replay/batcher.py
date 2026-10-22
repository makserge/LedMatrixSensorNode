"""P1 frames batcher and payload formatter, ported, plus the P2 off phase (P3 spec 11.4).

``Batcher`` and ``format_frames`` are ports of ``Batcher`` and ``format_frames()`` in
``tools/tests/frames_batcher_model.py`` (the project's line-by-line model of ``src/ld2454_frames.cpp``):
P1 rules A1-A4 and B1-B3 with ``LD2454_STREAM_BATCH_MS`` 500, ``LD2454_STREAM_IDLE_MS`` 5000,
``LD2454_STREAM_MAX_FRAMES`` 20 and ``LD2454_RESTART_GAP_MS`` 1000. Two additions:

* ``Batcher.add_empty_run`` adds a run of consecutive empty frames in O(1), with exactly the effect of
  adding them one by one (empty frames are counted, never materialised);
* ``format_frames`` omits ``mt`` and ``p2`` when they are ``None`` (P2: while ``pw`` bit0 = 0, and in the
  OFF heartbeat), as P1 spec 1.1 says.

``MqttLoop`` emulates MqttTask: a loop every ``MQTT_LOOP_DELAY_MS`` (40 ms) that drains the parsed frames
into the batcher, then polls it. It is event driven for speed: while the pending batch lists no target frame
and the last closed batch had none, it jumps to the earliest of the next target frame, the next frame after
a radar pause, the next heartbeat deadline and the next timeline boundary, on the 40 ms grid. That gives
the same batches as stepping every 40 ms (``tests/test_batcher.py`` checks it against the P1 model).

P2 off phase (OA 5.7 c): on entering OFF the pending batch is flushed with the header of the phase its
frames came from, then an OFF heartbeat (``n`` 0, ``pw`` 0, ``sm`` "N", no ``mt``/``p2``) goes out at
once and every 30 s. Times inside the loop are simulated ms; the batcher works in device ms
(simulated ms minus the boot time), so payloads carry the node's ``t``.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

LD2454_STREAM_BATCH_MS = 500
LD2454_STREAM_IDLE_MS = 5000
LD2454_STREAM_OFF_IDLE_MS = 30000
LD2454_STREAM_MAX_FRAMES = 20
LD2454_RESTART_GAP_MS = 1000
LD2454_FRAMES_JSON_MAX = 2048
MQTT_LOOP_DELAY_MS = 40
U32 = 0xFFFFFFFF

POWERING = "powering"     # radar powered, no frames yet; pw 0, sm A, 5 s heartbeats
WARMUP = "warmup"         # frames, pw 0 (LD2412 warm-up), sm A
ON = "on"                 # frames, pw 3, sm A
OFF = "off"               # radar off (night window); pw 0, sm N, 30 s heartbeats
FRAME_PHASES = (WARMUP, ON)


class Batch:
    __slots__ = ("s", "n", "first_ms", "last_ms", "f")

    def __init__(self):
        self.s = 0
        self.n = 0
        self.first_ms = 0
        self.last_ms = 0
        self.f = []             # listed target frames: (seq, ms, targets); targets = ((slot, x, y, v), ...)

    def copy(self):
        b = Batch()
        b.s, b.n, b.first_ms, b.last_ms, b.f = self.s, self.n, self.first_ms, self.last_ms, list(self.f)
        return b


class Batcher:
    """Ld2454FrameBatcher (port of the P1 model's ``Batcher``)."""

    def __init__(self, start_ms: int = 0):
        self.pending = Batch()
        self.prev_had_targets = False
        self.target_gate_open = True
        self.last_close_ms = start_ms
        self.last_target_close_ms = start_ms
        self.seq_gap_frames = 0

    def _before_add(self, seq: int, ms: int, now_ms: int):
        closed = None
        expected = (self.pending.s + self.pending.n) & U32
        if seq != expected:                                            # A1
            self.seq_gap_frames += (seq - expected) & U32
            if self.pending.n > 0:
                closed = self.close(now_ms)
            self.pending.s = seq
        elif self.pending.n > 0 and ms - self.pending.last_ms >= LD2454_RESTART_GAP_MS:   # A2
            closed = self.close(now_ms)
        return closed

    def add(self, seq: int, ms: int, targets: tuple, now_ms: int):
        """Add one parsed frame (``targets`` empty for an empty frame). Returns a closed batch or None."""
        closed = self._before_add(seq, ms, now_ms)
        p = self.pending
        if p.n == 0:
            p.first_ms = ms
        p.last_ms = ms
        p.n += 1                                                       # A3
        if targets:
            p.f.append((seq, ms, targets))
            if len(p.f) == LD2454_STREAM_MAX_FRAMES:                   # A4
                assert closed is None
                closed = self.close(now_ms)
        return closed

    def add_empty_run(self, seq0: int, count: int, ms_first: int, ms_last: int, now_ms: int):
        """Add ``count`` consecutive empty frames (seq ``seq0``.., spaced < 1000 ms) as ``add`` would."""
        if count <= 0:
            return None
        closed = self._before_add(seq0, ms_first, now_ms)
        p = self.pending
        if p.n == 0:
            p.first_ms = ms_first
        p.last_ms = ms_last
        p.n += count
        return closed

    def poll(self, now_ms: int):
        if not self.target_gate_open and now_ms - self.last_target_close_ms >= LD2454_STREAM_BATCH_MS:
            self.target_gate_open = True
        if self.pending.f and self.target_gate_open:                   # B1
            return self.close(now_ms)
        if self.prev_had_targets and now_ms - self.last_close_ms >= LD2454_STREAM_BATCH_MS:   # B2
            return self.close(now_ms)
        if now_ms - self.last_close_ms >= LD2454_STREAM_IDLE_MS:      # B3
            return self.close(now_ms)
        return None

    def close(self, now_ms: int):
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


def format_frames(batch: Batch, t_ms: int, boot_id: str, mt, p2, pw: int, sm: str) -> Optional[str]:
    """formatLd2454Frames: the P1 text, character for character; ``mt``/``p2`` None = omitted (P2)."""
    t32 = t_ms & U32
    out = '{"v":1,"b":"%s","t":%d,"s":%d,"n":%d' % (boot_id, t_ms, batch.s, batch.n)
    if batch.n > 0:
        out += ',"a0":%d,"a1":%d' % ((t32 - batch.first_ms) & U32, (t32 - batch.last_ms) & U32)
    if mt is not None:
        out += ',"mt":%d' % (1 if mt else 0)
    if p2 is not None:
        out += ',"p2":%d' % (1 if p2 else 0)
    out += ',"pw":%d,"sm":"%s"' % (pw, sm)
    if batch.f:
        parts = []
        for seq, ms, targets in batch.f:
            e = '[%d,%d' % ((seq - batch.s) & U32, (t32 - ms) & U32)
            for slot, x, y, v in targets:
                e += ',%d,%d,%d,%d' % (slot, x, y, v)
            parts.append(e + ']')
        out += ',"f":[' + ','.join(parts) + ']'
    out += '}'
    if len(out) + 1 > LD2454_FRAMES_JSON_MAX:
        return None
    return out


class Segment:
    """One phase of the radar timeline, ``[start_ms, end_ms)`` in simulated ms."""
    __slots__ = ("start", "end", "phase")

    def __init__(self, start: int, end: int, phase: str):
        self.start = start
        self.end = end
        self.phase = phase

    def __repr__(self):
        return "Segment(%d, %d, %s)" % (self.start, self.end, self.phase)


def align_up(ms: int, step: int = MQTT_LOOP_DELAY_MS) -> int:
    return -((-ms) // step) * step


class MqttLoop:
    """MqttTask emulation for one boot epoch: yields ``(tick_sim_ms, payload_str)`` in publish order.

    ``segments``: contiguous ``Segment`` list from the epoch's first message time (its end may be
    ``stop_ms``, a reboot: the pending batch is then lost). ``source``: frames of this epoch, with
    ``drain(upto_ms)`` yielding ``("e", seq0, count, ms_first, ms_last)`` and ``("t", seq, ms, targets)``
    in order (simulated ms) and ``next_event_ms(after_ms)``. ``header(phase, tick_ms)`` gives
    ``(mt, p2, pw, sm)`` for a payload built at that tick.
    """

    def __init__(self, segments: list, source, header: Callable, boot_id: str, boot_sim_ms: int,
                 stop_ms: Optional[int] = None):
        self.segments = segments
        self.source = source
        self.header = header
        self.boot_id = boot_id
        self.boot = boot_sim_ms
        self.stop_ms = stop_ms
        self.batcher = Batcher(start_ms=0)       # device ms: 0 = boot
        self.seg_i = 0
        self.tick = align_up(segments[0].start)
        self.in_off = False
        self.prev_phase = None
        self.published = 0
        self.dropped_too_long = 0

    def _segment(self, t: int) -> Optional[Segment]:
        segs = self.segments
        while self.seg_i < len(segs) and segs[self.seg_i].end <= t:
            self.seg_i += 1
        return segs[self.seg_i] if self.seg_i < len(segs) else None

    def _emit(self, out: list, batch: Batch, t_sim: int, phase: str) -> None:
        mt, p2, pw, sm = self.header(phase, t_sim)      # built when published, as the node does
        payload = format_frames(batch, t_sim - self.boot, self.boot_id, mt, p2, pw, sm)
        if payload is None:
            self.dropped_too_long += 1
            return
        self.published += 1
        out.append((t_sim, payload))

    def _drain(self, out: list, t: int, phase: str) -> None:
        b = self.batcher
        boot = self.boot
        now = t - boot
        for item in self.source.drain(t):
            if item[0] == "e":
                closed = b.add_empty_run(item[1], item[2], item[3] - boot, item[4] - boot, now)
            else:
                closed = b.add(item[1], item[2] - boot, item[3], now)
            if closed is not None:
                self._emit(out, closed, t, phase)

    def run(self, until_ms: int) -> list:
        """Process every loop tick before ``until_ms``; returns the payloads published meanwhile."""
        out = []
        b = self.batcher
        boot = self.boot
        while self.tick < until_ms:
            t = self.tick
            if self.stop_ms is not None and t >= self.stop_ms:
                self.tick = until_ms if until_ms > t else t
                break
            seg = self._segment(t)
            if seg is None:
                break
            now = t - boot
            if seg.phase == OFF:
                if not self.in_off:
                    prev = self.prev_phase if self.prev_phase is not None else ON
                    self._drain(out, t, prev)
                    if b.pending.n > 0:
                        self._emit(out, b.close(now), t, prev)
                    self._emit(out, b.close(now), t, OFF)
                    self.in_off = True
                elif now - b.last_close_ms >= LD2454_STREAM_OFF_IDLE_MS:
                    self._emit(out, b.close(now), t, OFF)
                self.prev_phase = OFF
                nxt = min(b.last_close_ms + boot + LD2454_STREAM_OFF_IDLE_MS, seg.end)
                self.tick = max(align_up(nxt), t + MQTT_LOOP_DELAY_MS)
                continue
            self.in_off = False
            self.prev_phase = seg.phase
            self._drain(out, t, seg.phase)
            closed = b.poll(now)
            if closed is not None:
                self._emit(out, closed, t, seg.phase)
            if b.pending.f or b.prev_had_targets:
                self.tick = t + MQTT_LOOP_DELAY_MS
            else:
                nxt = min(self.source.next_event_ms(t), b.last_close_ms + boot + LD2454_STREAM_IDLE_MS, seg.end)
                if self.stop_ms is not None:
                    nxt = min(nxt, self.stop_ms)
                self.tick = max(align_up(nxt), t + MQTT_LOOP_DELAY_MS)
        return out


class ListFrameSource:
    """A frame source over an explicit list of ``(ms, targets)`` frames with consecutive seqs from ``seq0``.

    For tests and small cases (every frame materialised): radar pauses are simply missing frames.
    """

    def __init__(self, frames: Iterable, seq0: int = 0):
        self.frames = list(frames)
        self.seq0 = seq0
        self.i = 0

    def drain(self, upto_ms: int):
        fr = self.frames
        while self.i < len(fr) and fr[self.i][0] <= upto_ms:
            ms, targets = fr[self.i]
            yield ("t", self.seq0 + self.i, ms, tuple(targets)) if targets else ("e", self.seq0 + self.i, 1, ms, ms)
            self.i += 1

    def next_event_ms(self, after_ms: int) -> int:
        fr = self.frames
        prev = fr[self.i - 1][0] if self.i > 0 else None
        for j in range(self.i, len(fr)):
            ms, targets = fr[j]
            if ms > after_ms and (targets or (prev is not None and ms - prev >= LD2454_RESTART_GAP_MS)):
                return ms
            prev = ms
        return 1 << 62
