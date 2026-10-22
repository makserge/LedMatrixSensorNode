import json
import math
import random
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import (ContextState, DebugMapChanged, DebugMapData, FixedOffsetTime,
                                           FramesMessage, MarkPress, MarkResult, NameEcho, NightlyDone,
                                           NightlyFailed, NodeStatus, SaveRequested, ScheduleState, SpotName,
                                           StatusChanged)

BASE_MS = 1767571200000          # Monday 2026-01-05 00:00 UTC
DAY_MS = 86400000
DOOR = (-2.0, 5.0)
DESK = (1.0, 3.0)


class Feeder:
    """P1-like frames batches for a learner: 10 Hz frames on a 100 ms grid (sim ms from BASE_MS),
    500 ms batches while targets are listed, empty frames as heartbeats of up to 60 s."""

    def __init__(self, learner, boot="0badc0de", lat_ms=20, dev_off_ms=120000):
        self.L = learner
        self.boot = boot
        self.seq = 0
        self.lat = lat_ms
        self.dev_off = dev_off_ms
        self.next_ms = 0
        self.present = []                 # [(a_ms, b_ms)] LD2412 OUT = 1
        self.outs = []

    def _p2(self, t):
        return 1 if any(a <= t < b for a, b in self.present) else 0

    def _send(self, n, first, last, listed):
        t_dev = last + self.dev_off + 40
        obj = {"v": 1, "b": self.boot, "t": t_dev, "s": self.seq, "n": n,
               "a0": t_dev - (first + self.dev_off), "a1": 40, "mt": 1, "p2": self._p2(last), "pw": 3, "sm": "A"}
        if listed:
            f = []
            for k, t, tg in listed:
                e = [k, t_dev - (t + self.dev_off)]
                for slot, (x, y) in enumerate(tg[:3]):
                    e += [slot, int(round(x * 1000)), int(round(y * 1000)), 0]
                f.append(e)
            obj["f"] = f
        self.seq += n
        payload = json.dumps(obj, separators=(",", ":")).encode()
        self.outs += self.L.handle(FramesMessage(payload, BASE_MS + last + 40 + self.lat))

    def idle_until(self, t_ms):
        while self.next_ms < t_ms:
            n = min(600, (t_ms - self.next_ms + 99) // 100)
            self._send(n, self.next_ms, self.next_ms + 100 * (n - 1), [])
            self.next_ms += 100 * n

    def targets_until(self, t_ms, fn):
        while self.next_ms < t_ms:
            n = min(5, (t_ms - self.next_ms + 99) // 100)
            listed = []
            for k in range(n):
                t = self.next_ms + 100 * k
                tg = fn(t)
                if tg:
                    listed.append((k, t, tg))
            self._send(n, self.next_ms, self.next_ms + 100 * (n - 1), listed)
            self.next_ms += 100 * n

    def tick(self, t_ms):
        self.outs += self.L.tick(BASE_MS + t_ms)


def walk_fn(p0, p1, a, b):
    def fn(t):
        f = min(1.0, max(0.0, (t - a) / (b - a)))
        return [(p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f)]
    return fn


def sit_fn(rng, pos, sd=0.03):
    return lambda t: [(pos[0] + rng.gauss(0, sd), pos[1] + rng.gauss(0, sd))]


def seated_session(f, rng, a, b, pos=DESK, door=DOOR):
    """Walk in from the door (6 s), visible and hidden spells at pos, walk out (6 s). LD2412 sees the
    person from the walk-in to the walk-out."""
    f.present.append((a - 6000, b + 6000))
    f.idle_until(a - 6000)
    f.targets_until(a, walk_fn(door, pos, a - 6000, a))
    t = a
    while t < b:
        v = int(1000 * (5 + rng.expovariate(1 / 15.0)))
        f.targets_until(min(b, t + v), sit_fn(rng, pos))
        t = min(b, t + v) + int(1000 * (1 + rng.expovariate(1 / 90.0)))
        t = (t // 100) * 100
        if t < b:
            f.idle_until(t)
    f.idle_until(b)
    f.targets_until(b + 6000, walk_fn(pos, door, b, b + 6000))


def desk_days(f, rng, days):
    for d in days:
        a = d * DAY_MS + 9 * 3600000 + rng.randint(-600, 600) * 1000
        seated_session(f, rng, a, a + 3 * 3600000)
        f.idle_until((d + 1) * DAY_MS)


def start(L):
    L.handle(NodeStatus(True, BASE_MS))
    L.handle(ScheduleState(None, None, None, BASE_MS))
    L.handle(ContextState("home_empty", False, BASE_MS))


class LearnerTest(unittest.TestCase):
    def test_seated_person_gives_dwell_zone(self):
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        f = Feeder(L)
        rng = random.Random(11)
        desk_days(f, rng, range(10))
        nights = [o for o in f.outs if isinstance(o, NightlyDone)]
        self.assertEqual([n.night for n in nights], list(range(10)))
        s = [x for x in L.suggestions() if x.type == "dwell_zone"]
        self.assertEqual(len(s), 1)
        self.assertLessEqual(s[0].first_night, 7)
        self.assertAlmostEqual(s[0].cx_m, DESK[0], delta=0.15)
        self.assertAlmostEqual(s[0].cy_m, DESK[1], delta=0.15)
        self.assertEqual([x for x in L.suggestions() if x.type in ("dwell_zone", "short_task_zone")
                          and math.hypot(x.cx_m - DESK[0], x.cy_m - DESK[1]) > 0.6], [])
        st = L.stays()
        self.assertGreater(len(st), 9)
        self.assertTrue(all(x.t0_s % 300 == 0 and x.dur_s % 10 == 0 for x in st))
        self.assertTrue({x.exit for x in st} <= {"moving", "still", "moved", "censored"})
        self.assertIn("bridge", {x.arr for x in st})      # hidden spells are bridged
        c = L.counters()
        self.assertEqual((c["seq_gaps"], c["parse_errors"], c["duplicates"]), (0, 0, 0))
        dm = L.debug_map()
        self.assertIsInstance(dm, DebugMapData)
        self.assertEqual(len(dm.dwell_rate), 2800)
        a = L.status().attributes
        self.assertEqual(a["suggestions"][0]["id"], s[0].id)
        self.assertEqual(a["mode"], "shadow")
        json.dumps(a)
        json.dumps(L.export_state())

    def run_twice(self):
        res = []
        for _ in range(2):
            L = Learner(P, FixedOffsetTime(0))
            start(L)
            f = Feeder(L)
            rng = random.Random(5)
            desk_days(f, rng, range(2))
            f.tick(2 * DAY_MS + 4 * 3600000)
            res.append((f.outs, json.dumps(L.export_state(), sort_keys=False, separators=(",", ":")),
                        L.suggestions(), L.candidates(), L.stays(), L.counters()))
        return res

    def test_determinism(self):
        a, b = self.run_twice()
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[1], b[1])
        self.assertEqual(a[2:], b[2:])
        self.assertGreater(len(a[4]), 0)

    def test_output_order(self):
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        L.handle(SpotName("  Bed  ", BASE_MS + 1000))
        outs = L.handle(MarkPress(BASE_MS + DAY_MS + 4 * 3600000))       # after 03:30 on day 1: nightly runs too
        self.assertEqual([type(o) for o in outs],
                         [MarkResult, NightlyDone, StatusChanged, DebugMapChanged, SaveRequested])
        self.assertEqual(outs[0].reason, "not_observing")
        self.assertEqual(outs[3].reason, "nightly")
        self.assertEqual(outs[4].reason, "nightly")
        outs = L.handle(SpotName("Desk", BASE_MS + DAY_MS + 4 * 3600000 + 1))
        self.assertEqual([type(o) for o in outs], [NameEcho, SaveRequested])
        self.assertEqual((outs[0].text, outs[1].reason), ("Desk", "name"))
        outs = L.handle(SpotName("x" * 40, BASE_MS + DAY_MS + 4 * 3600000 + 2))
        self.assertEqual(outs[0].text, "x" * 32)

    def test_older_event_uses_core_now(self):
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        f = Feeder(L)
        f.idle_until(10000)
        L.tick(BASE_MS + 60000)
        f.next_ms = 10000
        f.idle_until(12000)                                # recv about BASE + 11.9 s < core now
        self.assertEqual(L.now, BASE_MS + 60000)
        self.assertEqual(L.obs.last_frames_ms, BASE_MS + 60000)
        self.assertEqual(L.status().state, "learning")
        outs = L.handle(MarkPress(BASE_MS + 30000))
        self.assertEqual(outs[0].reason, "no_stay")
        self.assertEqual(L.status().attributes["last_mark"]["at"], "2026-01-05T00:00Z")

    def test_status_transitions(self):
        L = Learner(P, FixedOffsetTime(0))
        self.assertEqual(L.status().state, "starting")
        self.assertEqual(L.tick(BASE_MS), [])
        f = Feeder(L)
        f.idle_until(5000)
        st = [o.state for o in f.outs if isinstance(o, StatusChanged)]
        self.assertEqual(st, ["learning"])
        outs = L.tick(BASE_MS + 5000 + 14000)
        self.assertEqual([o for o in outs if isinstance(o, StatusChanged)], [])
        outs = L.tick(BASE_MS + 5000 + 16000)
        self.assertEqual([o.state for o in outs if isinstance(o, StatusChanged)], ["not_observing"])
        self.assertEqual(L.counters()["censor_events"], 1)
        outs = L.tick(BASE_MS + 5000 + 76000)
        self.assertEqual([o.state for o in outs if isinstance(o, StatusChanged)], ["no_data"])
        a = L.status().attributes
        self.assertEqual(a["state_since"], "2026-01-05T00:01Z")
        # periodic attributes every 5 min, otherwise only on change
        outs = L.tick(BASE_MS + 5000 + 76000 + 299000)
        self.assertEqual(outs, [])
        outs = L.tick(BASE_MS + 5000 + 76000 + 300000)
        self.assertEqual([o.state for o in outs], ["no_data"])

    def test_not_observing_with_censored_frames(self):
        L = Learner(P, FixedOffsetTime(0))
        f = Feeder(L)
        f.idle_until(5000)
        obj = {"v": 1, "b": "0badc0de", "t": 5000 + 120000 + 30000, "s": f.seq, "n": 0, "pw": 0, "sm": "N"}
        outs = L.handle(FramesMessage(json.dumps(obj).encode(), BASE_MS + 35000))
        self.assertEqual([o.state for o in outs if isinstance(o, StatusChanged)], ["not_observing"])
        outs = L.handle(FramesMessage(b"garbage", BASE_MS + 36000))
        self.assertEqual(L.counters()["parse_errors"], 1)

    def test_state_restart_keeps_suggestions(self):
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        f = Feeder(L)
        rng = random.Random(3)
        desk_days(f, rng, range(2))
        st = json.loads(json.dumps(L.export_state()))
        L2 = Learner(P, FixedOffsetTime(0), st)
        self.assertEqual(L2.suggestions(), L.suggestions())
        self.assertEqual(L2.stays(), L.stays())
        self.assertEqual(L2.status().state, "starting")
        self.assertEqual(L2.tracker.live, [])
        self.assertEqual(L2.bh.pending, [])


class HardeningTest(unittest.TestCase):
    def test_nightly_exception_is_contained(self):
        """A run that raises must not abort the event that triggered it, be retried every minute, or leak
        values into the message: one NightlyFailed per night, the learner goes on."""
        import radar_zone_learner.core.learner as LM
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        calls = []

        def boom(learner, run_ord, night):
            calls.append(night)
            raise ValueError("position 3.14 m")

        old = LM.run_nightly
        LM.run_nightly = boom
        try:
            t = BASE_MS + 3 * 3600000 + 31 * 60000
            f = Feeder(L)
            f.next_ms = t - BASE_MS - 2000
            f.idle_until(t - BASE_MS + 1000)                       # a frames batch triggers the run
            fails = [o for o in f.outs if isinstance(o, NightlyFailed)]
            self.assertEqual(len(fails), 1)
            self.assertTrue(fails[0].error.startswith("ValueError at test_learner.py:"), fails[0].error)
            self.assertNotIn("3.14", fails[0].error)
            self.assertEqual(fails[0].date, "2026-01-05")
            self.assertEqual([o for o in f.outs if isinstance(o, NightlyDone)], [])
            for k in range(1, 11):                                 # not retried every minute
                L.tick(t + 60000 * k)
            self.assertEqual(calls, [0])
            self.assertEqual((L.nightly_errors, L.status().attributes["nightly_errors"]), (1, 1))
            self.assertEqual(L.status().state, "learning" if L.obs.observing else L.status().state)
            outs = L.tick(t + DAY_MS)                              # tomorrow it tries again
            self.assertEqual(calls, [0, 1])
            self.assertEqual(len([o for o in outs if isinstance(o, NightlyFailed)]), 1)
        finally:
            LM.run_nightly = old

    def test_day_zero_waits_for_a_plausible_clock(self):
        L = Learner(P, FixedOffsetTime(0))
        L.handle(NodeStatus(True, 5000))                     # the host clock is not set yet: 1970
        self.assertIsNone(L.created_ms)
        self.assertEqual(L.tick(10000), [])
        L.handle(NodeStatus(True, BASE_MS))
        self.assertEqual((L.created_ms, L.created_ord), (BASE_MS, 739621))      # 2026-01-05
        self.assertEqual(L.export_state()["meta"]["created_date"], "2026-01-05")

    def test_same_spot_name_is_silent(self):
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        outs = L.handle(SpotName("Bed", BASE_MS + 1000))
        self.assertEqual([type(o) for o in outs], [NameEcho, SaveRequested])
        for text in ("Bed", "  Bed ", "Bed"):
            outs = L.handle(SpotName(text, BASE_MS + 2000))
            self.assertEqual([o for o in outs if isinstance(o, (NameEcho, SaveRequested))], [])
        outs = L.handle(SpotName("bed", BASE_MS + 3000))             # another text: echoed and saved
        self.assertEqual([type(o) for o in outs if isinstance(o, (NameEcho, SaveRequested))],
                         [NameEcho, SaveRequested])

    def test_pending_stay_is_committed_at_the_nightly_run_although_a_track_holds_it(self):
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        f = Feeder(L)
        f.present.append((0, 10 ** 9))
        f.idle_until(5000)
        rng = random.Random(9)
        f.targets_until(65000, sit_fn(rng, (1.0, 3.0)))             # a 60 s stay at (1, 3), then the radar loses it
        f.idle_until(110000)
        f.targets_until(3 * 3600000 + 20 * 60000, sit_fn(rng, (1.5, 3.2)))    # a track 0.5 m away sits for hours
        holds = L.bh.hold_times
        self.assertEqual(len(holds), 1)
        self.assertEqual(len(L.bh.pending), 1)                      # held: the sitting track may still bridge
        self.assertEqual(L.bh.stays, [])
        f.targets_until(3 * 3600000 + 31 * 60000, sit_fn(rng, (1.5, 3.2)))    # the nightly run at 03:30
        self.assertEqual([n.night for n in f.outs if isinstance(n, NightlyDone)], [0])
        self.assertEqual(L.bh.pending, [])                           # a hold never outlives the night
        self.assertEqual(len(L.bh.stays), 1)


if __name__ == "__main__":
    unittest.main()
