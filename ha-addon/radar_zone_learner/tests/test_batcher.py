"""Batcher port and MqttTask loop (P3 spec 11.4, 13.4).

The port's batches and payload strings are compared with the P1 model's ``Batcher`` and ``format_frames``
(``tools/tests/frames_batcher_model.py``, imported by path, never changed) driven at every 40 ms tick, and every
payload of a generated 2-day run is checked with ``validate_frames(obj, phase="any")`` from
``tools/frames_checker.py``.
"""
import importlib.util
import json
import random
import sys
import unittest
from pathlib import Path

from radar_zone_learner.core.types import FramesMessage
from replay import framegen as G
from replay import world as W
from replay.batcher import (OFF, ON, POWERING, WARMUP, Batch, Batcher, ListFrameSource, MqttLoop, Segment,
                            format_frames)

TOOLS = Path(__file__).resolve().parents[3] / "tools"
MODEL = TOOLS / "tests" / "frames_batcher_model.py"
CHECKER = TOOLS / "frames_checker.py"
BOOT = "0badc0de"


def _load(path, name):
    if not path.is_file():
        return None
    dwb = sys.dont_write_bytecode
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.dont_write_bytecode = dwb          # the model sets it; keep the caller's setting
    return mod


def p1_reference(model, frames, end_ms):
    """The P1 model's batcher driven by a strict 40 ms MqttTask loop; frames = [(ms, targets)], seq = index."""
    b = model.Batcher()
    out = []
    i = 0
    for t in range(0, end_ms, 40):
        while i < len(frames) and frames[i][0] <= t:
            ms, targets = frames[i]
            mask, xs, ys, vs = 0, [0, 0, 0], [0, 0, 0], [0, 0, 0]
            for slot, x, y, v in targets:
                mask |= 1 << slot
                xs[slot], ys[slot], vs[slot] = x, y, v
            closed = b.add(model.Frame(i, ms, mask, xs, ys, vs), t)
            if closed is not None:
                out.append((t, model.format_frames(closed, t, BOOT, True, False, 3, "A")))
            i += 1
        closed = b.poll(t)
        if closed is not None:
            out.append((t, model.format_frames(closed, t, BOOT, True, False, 3, "A")))
    return out


def port_run(frames, end_ms):
    loop = MqttLoop([Segment(0, end_ms, ON)], ListFrameSource(frames), lambda ph, t: (1, 0, 3, "A"), BOOT, 0)
    return loop.run(end_ms)


def fixed_sequence():
    frames = []
    t = 0
    for _ in range(100):                                   # 10 s empty
        frames.append((t, ()))
        t += 100
    for k in range(30):                                    # 3 s one moving target
        frames.append((t, ((0, -500 + 10 * k, 2000, 25),)))
        t += 100
    for _ in range(60):                                    # 6 s empty
        frames.append((t, ()))
        t += 100
    for k in range(60):                                    # targets on/off every 300 ms
        frames.append((t, ((1, 300, 1500, -3),) if (k // 3) % 2 == 0 else ()))
        t += 100
    t += 1500                                              # a 1.5 s radar pause (no frames)
    for k in range(25):                                    # 25 consecutive target frames, 3 targets
        frames.append((t, ((0, 1, 2, 3), (1, -4000, 5000, 0), (2, 32767, -32767, -32767))))
        t += 100
    for _ in range(120):                                   # 12 s empty: trailing batch and heartbeats
        frames.append((t, ()))
        t += 100
    return frames, t + 7000


def random_sequence(seed):
    rng = random.Random(seed)
    frames = []
    t = rng.randint(0, 99)
    present = False
    end = 90_000
    while t < end:
        r = rng.random()
        if r < 0.01:
            t += rng.randint(1000, 4000)                   # radar pause >= 1 s
        elif r < 0.03:
            t += rng.randint(150, 900)                     # short gap
        if rng.random() < 0.05:
            present = not present
        if present and rng.random() > 0.1:
            n = rng.randint(1, 3)
            slots = sorted(rng.sample(range(3), n))
            frames.append((t, tuple((s, rng.randint(-3000, 3000), rng.randint(0, 6000), rng.randint(-200, 200))
                                    for s in slots)))
        else:
            frames.append((t, ()))
        t += rng.randint(90, 110)
    return frames, t + 6000


class P1EquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = _load(MODEL, "rzl_test_frames_batcher_model")

    def setUp(self):
        if self.model is None:
            self.skipTest("tools/tests/frames_batcher_model.py not found next to the add-on")

    def test_fixed_sequence(self):
        frames, end = fixed_sequence()
        ref = p1_reference(self.model, frames, end)
        got = port_run(frames, end)
        self.assertGreater(len(ref), 10)
        self.assertEqual(got, ref)
        # no batch spans the 1.5 s radar pause (A2): consecutive covered frames < 1000 ms apart
        for t, p in got:
            m = json.loads(p)
            if m["n"] > 1:
                self.assertLess(m["a0"] - m["a1"], 1000 + 100 * (m["n"] - 1))

    def test_random_sequences(self):
        for seed in range(12):
            frames, end = random_sequence(seed)
            self.assertEqual(port_run(frames, end), p1_reference(self.model, frames, end), seed)

    def test_formatter_strings(self):
        rng = random.Random(5)
        for _ in range(200):
            b = Batch()
            b.s = rng.randint(0, 10 ** 6)
            b.n = rng.randint(0, 60)
            t = rng.randint(10 ** 5, 10 ** 8)
            b.first_ms = t - rng.randint(0, 5000)
            b.last_ms = t - rng.randint(0, 40)
            mb = self.model.Batch()
            mb.s, mb.n, mb.first_ms, mb.last_ms = b.s, b.n, b.first_ms, b.last_ms
            for k in sorted(rng.sample(range(max(1, b.n)), min(b.n, rng.randint(0, 5)))):
                slots = sorted(rng.sample(range(3), rng.randint(1, 3)))
                tg = tuple((s, rng.randint(-32767, 32767), rng.randint(-32767, 32767), rng.randint(-99, 99)) for s in slots)
                ms = t - rng.randint(40, 4000)
                b.f.append((b.s + k, ms, tg))
                mask, xs, ys, vs = 0, [0] * 3, [0] * 3, [0] * 3
                for s, x, y, v in tg:
                    mask |= 1 << s
                    xs[s], ys[s], vs[s] = x, y, v
                mb.f.append(self.model.Frame(b.s + k, ms, mask, xs, ys, vs))
            self.assertEqual(format_frames(b, t, BOOT, 1, 0, 3, "A"),
                             self.model.format_frames(mb, t, BOOT, True, False, 3, "A"))


class BatcherUnitTest(unittest.TestCase):
    def test_empty_run_equals_single_adds(self):
        a, b = Batcher(), Batcher()
        a.add(0, 0, ((0, 1, 2, 3),), 0)
        b.add(0, 0, ((0, 1, 2, 3),), 0)
        ca = a.add_empty_run(1, 30, 100, 3000, 3020)
        cb = None
        for k in range(30):
            c = b.add(1 + k, 100 + 100 * k, (), 3020)
            cb = cb or c
        self.assertIsNone(ca)
        self.assertIsNone(cb)
        self.assertEqual((a.pending.s, a.pending.n, a.pending.first_ms, a.pending.last_ms, a.pending.f),
                         (b.pending.s, b.pending.n, b.pending.first_ms, b.pending.last_ms, b.pending.f))
        # after a pause of >= 1 s the run closes the pending batch first (A2), as single adds do
        ca = a.add_empty_run(31, 5, 4500, 4900, 5000)
        cb = b.add(31, 4500, (), 5000)
        self.assertEqual((ca.s, ca.n), (cb.s, cb.n))

    def test_format_omits_mt_and_p2(self):
        b = Batch()
        b.s, b.n = 7, 0
        self.assertEqual(format_frames(b, 1000, BOOT, None, None, 0, "N"),
                         '{"v":1,"b":"0badc0de","t":1000,"s":7,"n":0,"pw":0,"sm":"N"}')
        self.assertEqual(format_frames(b, 1000, BOOT, 1, 1, 3, "A"),
                         '{"v":1,"b":"0badc0de","t":1000,"s":7,"n":0,"mt":1,"p2":1,"pw":3,"sm":"A"}')


class OffPhaseTest(unittest.TestCase):
    def test_flush_off_heartbeats_power_up(self):
        segs = [Segment(0, 60_000, ON), Segment(60_000, 200_000, OFF), Segment(200_000, 203_000, POWERING),
                Segment(203_000, 212_000, WARMUP), Segment(212_000, 300_000, ON)]
        frames = [(t, ((0, 100, 2000, 0),) if 59_000 <= t < 60_000 else ()) for t in range(0, 60_000, 100)]
        frames += [(t, ((1, 200, 3000, 0),) if 230_000 <= t < 231_000 else ()) for t in range(203_000, 300_000, 100)]

        def header(ph, t):
            return (1, 1, 3, "A") if ph == ON else ((None, None, 0, "N") if ph == OFF else (None, None, 0, "A"))
        loop = MqttLoop(segs, ListFrameSource(frames), header, BOOT, 0)
        out = loop.run(300_000)
        msgs = [(t, json.loads(p)) for t, p in out]
        off = [(t, m) for t, m in msgs if m["sm"] == "N"]
        self.assertEqual([t for t, _ in off], [60_000, 90_000, 120_000, 150_000, 180_000])
        self.assertTrue(all(m["n"] == 0 and m["pw"] == 0 and "mt" not in m for _, m in off))
        before = [m for t, m in msgs if t <= 60_000 and m["sm"] == "A"]
        self.assertEqual(before[-1]["pw"], 3)
        self.assertEqual(sum(m["n"] for m in before), 600)           # every frame before the window is published
        up = [(t, m) for t, m in msgs if 200_000 <= t < 212_000]
        self.assertEqual(up[0][0], 200_000)                           # 5 s heartbeat rule after the off phase
        self.assertTrue(all(m["pw"] == 0 and m["sm"] == "A" and "mt" not in m and "p2" not in m for _, m in up))
        later = [m for t, m in msgs if t >= 212_000]
        self.assertTrue(all(m["pw"] == 3 and m["mt"] == 1 for m in later))
        self.assertTrue(any("f" in m for m in later))
        s = 0
        for t, m in msgs:
            self.assertEqual(m["s"], s)
            s += m["n"]
        undrained = len(frames) - loop.source.i                       # after the last processed tick
        self.assertEqual(s + loop.batcher.pending.n + undrained, len(frames))


class GeneratedPayloadsTest(unittest.TestCase):
    def test_two_day_run_passes_the_p1_validator(self):
        checker = _load(CHECKER, "rzl_test_frames_checker_batcher")
        if checker is None:
            self.skipTest("tools/frames_checker.py not found next to the add-on")
        rs = G.RunStream(W.make_cfg(bed=1, nw="00:00-06:00"), 2, horizon_days=3)
        n = 0
        prev = None
        end = G.BASE_MS + 2 * W.DAY * 1000
        for recv, _, ev in rs.events():
            if recv >= end:
                break
            if not isinstance(ev, FramesMessage):
                continue
            obj = json.loads(ev.payload)
            self.assertEqual(checker.validate_frames(obj, phase="any"), [], ev.payload[:120])
            self.assertLessEqual(len(ev.payload), 2047)
            if prev is not None:
                self.assertEqual(obj["s"], prev["s"] + prev["n"])
                self.assertGreaterEqual(obj["t"], prev["t"])
                self.assertEqual(obj["b"], prev["b"])
            prev = obj
            n += 1
        self.assertGreater(n, 40_000)


if __name__ == "__main__":
    unittest.main()
