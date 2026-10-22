import random
import unittest
from datetime import date

from radar_zone_learner.core.observation import Censor, Observation, TrackFrames, frame_time
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import FixedOffsetTime, FramesBatch, ListedFrame, Target

BASE_MS = 1767571200000
OFF = BASE_MS - 120000          # wall = device + OFF (device booted 120 s before BASE_MS)


def batch(s, n, t, a0=None, a1=None, frames=(), pw=3, sm="A", mt=1, p2=1, boot="0000aaaa"):
    if n > 0 and a0 is None:
        a0, a1 = 40 + 100 * (n - 1), 40
    return FramesBatch(boot, t, s, n, a0 if n else None, a1 if n else None, mt, p2, pw, sm, tuple(frames))


def tgt(x=1000, y=2000):
    return (Target(0, x, y, 0),)


class Feed:
    """Contiguous 10 Hz frames as 500 ms batches (5 frames), recv = t + OFF + latency."""

    def __init__(self, obs, t=200000, s=0, boot="0000aaaa", lat=10):
        self.obs = obs
        self.t = t
        self.s = s
        self.boot = boot
        self.lat = lat

    def next(self, n=5, pw=3, sm="A", mt=1, p2=1, listed=(), gap_ms=0, lat=None):
        self.t += gap_ms + 100 * n
        b = batch(self.s, n, self.t, pw=pw, sm=sm, mt=mt, p2=p2, frames=listed, boot=self.boot)
        self.s += n
        r = self.t + OFF + (self.lat if lat is None else lat)
        return self.obs.ingest(b, r, r)


class SeqTest(unittest.TestCase):
    def setUp(self):
        self.o = Observation(P, FixedOffsetTime(0))
        self.f = Feed(self.o)

    def test_contiguous(self):
        a = self.f.next()
        b = self.f.next()
        self.assertEqual([type(x) for x in a + b], [TrackFrames, TrackFrames])
        c = self.o.counters
        self.assertEqual((c["batches"], c["frames"], c["seq_gaps"], c["boots"], c["censor_events"]), (2, 10, 0, 1, 0))
        self.assertTrue(self.o.observing)

    def test_duplicate(self):
        self.f.next()
        b = batch(0, 5, self.f.t)
        self.assertEqual(self.o.ingest(b, self.f.t + OFF, self.f.t + OFF), [])
        self.assertEqual(self.o.counters["duplicates"], 1)
        self.assertEqual(self.o.counters["batches"], 1)

    def test_overlap(self):
        self.f.next()                              # s 0..4, t = 200500
        t = self.f.t + 300
        fr = [ListedFrame(k, 440 - 100 * k, tgt()) for k in range(5)]
        b = batch(3, 5, t, frames=fr)               # frames 3..7: 3 and 4 seen before
        acts = self.o.ingest(b, t + OFF, t + OFF)
        self.assertEqual(self.o.counters["overlaps"], 1)
        self.assertEqual(self.o.counters["frames"], 8)
        tf = [a for a in acts if isinstance(a, TrackFrames)][0]
        self.assertEqual([d for d, _ in tf.frames], [t - 240, t - 140, t - 40])
        self.assertEqual(self.o.epoch.next_seq, 8)

    def test_seq_gap(self):
        self.f.next()
        self.f.s += 7
        acts = self.f.next()
        self.assertIsInstance(acts[0], Censor)
        self.assertEqual(acts[0].reason, "seq_gap")
        self.assertIsInstance(acts[1], TrackFrames)
        self.assertEqual(self.o.counters["frames_lost"], 7)
        self.assertEqual(self.o.counters["seq_gaps"], 1)
        # censor is at the frontier: the last frame of the first batch
        self.assertEqual(acts[0].at_dev, 200500 - 40)
        self.assertEqual(acts[0].at_wall, 200500 - 40 + OFF + 10)

    def test_new_boot_after_silence_and_retired(self):
        self.f.next()
        # a real reboot: the node was silent for silence_censor_ms (15 s) before its new id appeared
        g = Feed(self.o, t=200000 + 20000, s=0, boot="0000bbbb")
        acts = g.next()
        self.assertEqual(acts[0].reason, "reboot")
        self.assertEqual(self.o.counters["boots"], 2)
        self.assertEqual(self.o.retired, ["0000aaaa"])
        self.assertEqual(self.f.next(), [])         # a late batch of the old boot id is not taken
        self.assertEqual(self.o.counters["stale_boot"], 1)
        self.assertEqual(self.o.boot_id, "0000bbbb")

    def test_boot_history(self):
        for k in range(6):
            Feed(self.o, t=200000 + 20000 * k, boot="%08x" % (k + 1)).next()
        self.assertEqual(len(self.o.retired), 4)
        self.assertEqual(self.o.retired[0], "00000002")

    def test_foreign_boot_needs_silence_or_three_batches(self):
        self.f.next()
        self.f.next()
        c = self.o.counters
        # one forged batch with another boot id, 0.5 s after the real one: only counted
        g = Feed(self.o, t=self.f.t, s=77, boot="deadbeef")
        self.assertEqual(g.next(), [])
        self.assertEqual((c["foreign_boot"], c["boots"], self.o.boot_id), (1, 1, "0000aaaa"))
        self.assertIsInstance(self.f.next()[-1], TrackFrames)        # the real node goes on, unharmed
        # a real batch between them resets the run: three in a row are needed
        g.next()
        self.f.next()
        g.next()
        self.f.next()
        self.assertEqual((c["foreign_boot"], c["boots"]), (3, 1))
        g.next()
        g.next()
        self.assertEqual((c["foreign_boot"], c["boots"]), (5, 1))
        self.assertEqual([a.reason for a in g.next() if isinstance(a, Censor)], ["reboot"])
        self.assertEqual((c["boots"], self.o.boot_id), (2, "deadbeef"))

    def test_real_node_returns_after_a_takeover(self):
        self.f.next()
        g = Feed(self.o, t=self.f.t, s=0, boot="deadbeef")
        for _ in range(3):
            g.next()
        self.assertEqual(self.o.boot_id, "deadbeef")
        self.assertEqual(self.o.retired, ["0000aaaa"])
        # the forger is silent, the real node (retired) is back after 20 s and is taken again
        self.f.t += 20000
        acts = self.f.next()
        self.assertEqual(self.o.boot_id, "0000aaaa")
        self.assertIsInstance(acts[-1], TrackFrames)
        self.assertNotIn("0000aaaa", self.o.retired)

    def test_forged_seq_does_not_blind(self):
        self.f.next()
        c = self.o.counters
        # n = 0, s two billion ahead: well beyond what the node can have produced since the last batch
        b = batch(5 + 2000000000, 0, self.f.t + 100)
        self.assertEqual(self.o.ingest(b, self.f.t + 100 + OFF, self.f.t + 100 + OFF), [])
        self.assertEqual((c["bad_seq"], c["seq_gaps"], c["frames_lost"]), (1, 0, 0))
        b = batch(5 + 1000000, 5, self.f.t + 200)
        self.assertEqual(self.o.ingest(b, self.f.t + 200 + OFF, self.f.t + 200 + OFF), [])
        self.assertEqual(c["bad_seq"], 2)
        self.assertEqual(self.o.epoch.next_seq, 5)
        for _ in range(20):
            self.assertIsInstance(self.f.next()[-1], TrackFrames)      # every real batch counts
        self.assertEqual((c["batches"], c["frames"], c["duplicates"]), (21, 105, 0))

    def test_seq_jump_within_the_plausible_rate_is_a_gap(self):
        self.f.next()
        self.f.s += 900                              # 900 frames lost in 0.5 s: inside the 1000-frame slack
        acts = self.f.next()
        self.assertEqual(acts[0].reason, "seq_gap")
        self.assertEqual(self.o.counters["frames_lost"], 900)

    def test_stream_after_a_wrong_next_seq_resyncs(self):
        self.f.next()
        self.o.epoch.next_seq += 400                 # wrong, from one accepted forged batch
        c = self.o.counters
        for k in range(9):
            self.assertEqual(self.f.next(), [])      # nine duplicates, counted
        self.assertEqual(c["duplicates"], 9)
        acts = self.f.next()                         # the tenth continues the run: the stream is real
        self.assertEqual([type(a) for a in acts], [Censor, TrackFrames])
        self.assertEqual((acts[0].reason, c["seq_resyncs"]), ("seq_resync", 1))
        self.assertIsInstance(self.f.next()[-1], TrackFrames)
        self.assertEqual(c["duplicates"], 9)

    def test_a_single_duplicate_is_not_a_resync(self):
        self.f.next()
        b = batch(0, 5, self.f.t)
        for _ in range(3):
            self.f.next()
            self.assertEqual(self.o.ingest(b, self.f.t + OFF, self.f.t + OFF), [])
        self.assertEqual((self.o.counters["duplicates"], self.o.counters["seq_resyncs"]), (3, 0))

    def test_uint32_wrap(self):
        o = Observation(P, FixedOffsetTime(0))
        f = Feed(o, s=0xFFFFFFFF - 12)
        for _ in range(6):
            f.s &= 0xFFFFFFFF
            acts = f.next()
            self.assertIsInstance(acts[-1], TrackFrames)
        c = o.counters
        self.assertEqual((c["batches"], c["frames"], c["duplicates"], c["seq_gaps"]), (6, 30, 0, 0))
        self.assertEqual(o.epoch.next_seq, 30 - 13)


class TimesTest(unittest.TestCase):
    def test_listed_and_interpolated(self):
        b = batch(100, 5, 81234567, 480, 40, frames=[ListedFrame(0, 480, tgt()), ListedFrame(4, 40, tgt())])
        self.assertEqual(frame_time(b, 0), 81234567 - 480)
        self.assertEqual(frame_time(b, 4), 81234567 - 40)
        self.assertEqual(frame_time(b, 2), 81234567 - 260)
        self.assertEqual(frame_time(batch(0, 1, 1000, 30, 30), 0), 970)
        o = Observation(P, FixedOffsetTime(0))
        acts = o.ingest(b, BASE_MS + 81234567, BASE_MS + 81234567)
        tf = acts[0]
        self.assertEqual([d for d, _ in tf.frames], [81234567 - 480, 81234567 - 40])
        self.assertEqual(tf.end_dev, 81234567 - 40)
        self.assertEqual(tf.start_dev, 81234567 - 480)

    def test_offset_min_with_jitter(self):
        o = Observation(P, FixedOffsetTime(0))
        f = Feed(o)
        rng = random.Random(3)
        lats = [rng.randint(5, 30) for _ in range(200)]
        for lat in lats:
            f.next(lat=lat)
        self.assertEqual(o.epoch.offset - OFF, min(lats))      # latency only adds: the minimum
        self.assertEqual(o.wall(1000), 1000 + o.epoch.offset)

    def test_offset_follows_drift(self):
        o = Observation(P, FixedOffsetTime(0))
        rng = random.Random(4)
        s = 0
        for k in range(14400):                    # 2 h of 500 ms batches; the device clock runs 50 ppm slow
            wall = BASE_MS + 500 * k
            dev = int(500 * k * (1 - 50e-6)) + 1000
            b = batch(s, 5, dev)
            s += 5
            r = wall + rng.randint(5, 30)
            o.ingest(b, r, r)
            if k == 1000:
                early = o.epoch.offset
        true_now = BASE_MS + 500 * 14399 - (int(500 * 14399 * (1 - 50e-6)) + 1000)
        self.assertGreater(o.epoch.offset, early + 300)        # the offset moved with the drift (360 ms)
        self.assertLessEqual(abs(o.epoch.offset - true_now), 5 + 31)


class ClockTest(unittest.TestCase):
    """The broker has no ACLs and the host clock may step: t and recv are cross-checked."""

    def setUp(self):
        self.o = Observation(P, FixedOffsetTime(0))
        self.f = Feed(self.o)
        for _ in range(10):
            self.f.next()
        self.c = self.o.counters

    def test_host_clock_not_set_is_rejected(self):
        o = Observation(P, FixedOffsetTime(0))
        b = batch(0, 5, 200000)
        self.assertEqual(o.ingest(b, 1000000, 1000000), [])      # recv - t is 1970
        self.assertEqual((o.counters["bad_time"], o.epoch), (1, None))

    def test_forged_t_is_rejected_without_a_trace(self):
        o, f, c = self.o, self.f, self.c
        ep = o.epoch
        before = (ep.offset, ep.frontier_dev, o.obs_s_since_extraction, ep.next_seq, dict(o.coverage))
        r = f.t + OFF + 10
        for t_forged in (f.t + 10 * 86400000, 10 ** 15, f.t + 15000):
            self.assertEqual(o.ingest(batch(f.s, 1, t_forged, 40, 40), r, r), [])
        self.assertEqual(c["bad_time"], 3)
        self.assertEqual((ep.offset, ep.frontier_dev, o.obs_s_since_extraction, ep.next_seq, dict(o.coverage)), before)
        self.assertIsInstance(f.next()[-1], TrackFrames)        # the real node is not affected

    def test_host_clock_step_back_is_followed_after_three_batches(self):
        o, f, c = self.o, self.f, self.c
        old_dates = set(o.coverage)
        frontier = o.epoch.frontier_dev
        f.lat = 10 - 2 * 86400000                              # recv - t falls by two days
        self.assertEqual(f.next(), [])
        self.assertEqual(f.next(), [])
        self.assertEqual(c["bad_time"], 2)
        acts = f.next()
        self.assertEqual([type(a) for a in acts], [Censor, TrackFrames])
        self.assertEqual(acts[0].reason, "clock_step")
        self.assertEqual(acts[0].at_wall, frontier + OFF + 10)  # the old offset
        self.assertEqual((c["clock_steps"], c["bad_time"]), (1, 2))
        self.assertEqual(o.epoch.offset, OFF + 10 - 2 * 86400000)
        self.assertEqual(len(o.coverage), 2)                   # frames after the step are booked two days earlier
        self.assertEqual(max(o.coverage) - min(o.coverage), 2)
        self.assertEqual(max(o.coverage), max(old_dates))
        self.assertIsInstance(f.next()[-1], TrackFrames)

    def test_two_batches_ahead_are_not_a_step(self):
        o, f, c = self.o, self.f, self.c
        f.lat = 10 - 40000
        f.next()
        f.next()
        f.lat = 10
        self.assertIsInstance(f.next()[-1], TrackFrames)
        f.lat = 10 - 40000
        f.next()
        self.assertEqual((c["bad_time"], c["clock_steps"]), (3, 0))

    def test_host_clock_step_forward_ends_the_chain(self):
        o, f, c = self.o, self.f, self.c
        f.lat = 10 + 3 * 3600000                                # the host clock jumps 3 h ahead
        acts = f.next()                                         # every sample of the window is older than 10 min now
        self.assertEqual([type(a) for a in acts], [Censor, TrackFrames])
        cen = acts[0]
        self.assertEqual(cen.reason, "clock_step")
        self.assertEqual(cen.at_wall - cen.at_dev, OFF + 10)     # the frontier keeps the offset it was booked with
        self.assertEqual(o.epoch.offset, OFF + 10 + 3 * 3600000)
        self.assertEqual(c["clock_steps"], 1)
        self.assertIsInstance(f.next()[-1], TrackFrames)

    def test_slow_latency_change_is_no_step(self):
        o, f, c = self.o, self.f, self.c
        for k in range(3000):                                   # 25 min, latency drifting up by 9 s
            f.lat = 10 + k * 3
            f.next()
        self.assertEqual((c["clock_steps"], c["bad_time"]), (0, 0))


class ClassesTest(unittest.TestCase):
    def setUp(self):
        self.o = Observation(P, FixedOffsetTime(0))
        self.f = Feed(self.o)
        self.f.next()

    def reason(self, acts):
        cs = [a for a in acts if isinstance(a, Censor)]
        return cs[0].reason if cs else None

    def test_pw(self):
        acts = self.f.next(pw=2)
        self.assertEqual(self.reason(acts), "pw")
        self.assertFalse(self.o.observing)
        self.assertEqual(self.f.next(pw=0, n=0), [])        # once per transition
        self.assertEqual(self.o.counters["censor_events"], 1)

    def test_sm(self):
        self.assertEqual(self.reason(self.f.next(pw=0, sm="N", n=0, mt=None, p2=None)), "pw")
        self.f.next()
        self.assertEqual(self.reason(self.f.next(pw=0, sm="X", n=0)), "pw")
        self.f.next()
        self.assertEqual(self.reason(self.f.next(pw=1, sm="N")), "sm")
        self.assertEqual(self.o.counters["inconsistent_pw_sm"], 1)
        self.f.next()
        self.assertEqual(self.reason(self.f.next(pw=3, sm="X")), "sm")
        self.assertEqual(self.o.counters["inconsistent_pw_sm"], 2)
        for sm in ("A", "F", "S"):
            acts = self.f.next(sm=sm)
            self.assertIsInstance(acts[-1], TrackFrames)

    def test_mt(self):
        self.assertEqual(self.reason(self.f.next(mt=0)), "mt")
        acts = self.f.next(mt=None)
        self.assertIsInstance(acts[-1], TrackFrames)

    def test_offline(self):
        acts = self.o.node_status(False)
        self.assertEqual(acts[0].reason, "offline")
        self.assertEqual(self.f.next(), [])               # censored while offline, no second event
        self.o.node_status(True)
        self.assertIsInstance(self.f.next()[-1], TrackFrames)

    def test_compressed(self):
        self.f.t += 100
        t = self.f.t + 500
        b = batch(self.f.s, 10, t, 140, 40)                # 10 frames squeezed into 100 ms
        acts = self.o.ingest(b, t + OFF, t + OFF)
        self.assertEqual(self.reason(acts), "compressed")
        self.assertEqual(self.o.counters["compressed"], 1)

    def test_gap_rule(self):
        # last frame at t - 40; next batch's first frame 999 ms later: still observed
        last = self.f.t - 40
        before = self.o.obs_s_since_extraction
        acts = self.f.next(gap_ms=999 - 100)
        self.assertEqual(self.reason(acts), None)
        self.assertAlmostEqual(self.o.obs_s_since_extraction - before, 0.999 + 0.4, places=6)
        self.assertEqual(self.f.t - 40 - 400 - last, 999)
        acts = self.f.next(gap_ms=1000 - 100)
        self.assertEqual(self.reason(acts), "gap")
        self.assertIsInstance(acts[-1], TrackFrames)
        self.assertEqual(acts[-1].start_dev, self.f.t - 440)  # a new chain: observing starts again

    def test_silent_heartbeat(self):
        self.f.t += 5000
        b = batch(self.f.s, 0, self.f.t)
        acts = self.o.ingest(b, self.f.t + OFF, self.f.t + OFF)
        self.assertEqual(self.reason(acts), "gap")
        o2 = Observation(P, FixedOffsetTime(0))
        f2 = Feed(o2)
        f2.next()
        b = batch(f2.s, 0, f2.t + 500)
        self.assertEqual(o2.ingest(b, f2.t + 500 + OFF, f2.t + 500 + OFF), [])
        self.assertEqual(o2.obs_s_since_extraction, 0.4)

    def test_silence_tick(self):
        now = self.f.t + OFF
        self.assertEqual(self.o.tick(now + 14000), [])
        acts = self.o.tick(now + 15010)
        self.assertEqual(acts[0].reason, "silence")
        self.assertEqual(self.o.tick(now + 30000), [])


class CoverageTest(unittest.TestCase):
    def test_split_hour_and_date(self):
        o = Observation(P, FixedOffsetTime(0))
        d0 = date(2026, 1, 5).toordinal()
        a = BASE_MS + 23 * 3600000 + 30 * 60000
        o.add_coverage(a, a + 3600000)
        self.assertEqual(o.coverage[d0][23], 1800.0)
        self.assertEqual(o.coverage[d0 + 1][0], 1800.0)
        self.assertEqual(o.obs_s_since_extraction, 3600.0)
        o.add_coverage(BASE_MS + 10 * 3600000 + 500, BASE_MS + 12 * 3600000 + 250)
        self.assertAlmostEqual(o.coverage[d0][10], 3599.5)
        self.assertEqual(o.coverage[d0][11], 3600.0)
        self.assertAlmostEqual(o.coverage[d0][12], 0.25)
        self.assertEqual(o.covered_dates(d0 + 2), [d0])
        self.assertEqual(o.covered_dates(d0), [])
        self.assertEqual(o.coverage_export()["2026-01-05"][23], 1800.0)

    def test_offset_time_zone(self):
        o = Observation(P, FixedOffsetTime(3600))
        o.add_coverage(BASE_MS - 1800000, BASE_MS + 1800000)       # 23:30-00:30 UTC = 00:30-01:30 local
        d0 = date(2026, 1, 5).toordinal()
        self.assertEqual(o.coverage[d0][0], 1800.0)
        self.assertEqual(o.coverage[d0][1], 1800.0)

    def test_batches_cover_time_and_keep_42_dates(self):
        o = Observation(P, FixedOffsetTime(0))
        f = Feed(o)
        for _ in range(20):
            f.next()
        self.assertAlmostEqual(o.obs_s_since_extraction, 9.9, places=6)
        for d in range(50):
            o.add_coverage(BASE_MS + d * 86400000, BASE_MS + d * 86400000 + 1000)
        self.assertEqual(len(o.coverage), 42)
        self.assertEqual(len(o.o_obs()), 168)


class TimelineTest(unittest.TestCase):
    def test_ld_share(self):
        o = Observation(P, FixedOffsetTime(0))
        f = Feed(o)
        f.next(p2=1)                     # first batch of the epoch: [t - a0, t]
        a = f.t + OFF + 10 - 440
        for _ in range(9):
            f.next(p2=1)
        for _ in range(10):
            f.next(p2=0)
        b = f.t + OFF + 10
        self.assertAlmostEqual(o.ld_share(a, b), 0.5, places=2)
        self.assertEqual(o.ld_share(b, b), 1.0)
        # unknown time (pw bit1 = 0) adds no evidence; < 50 % known -> 0
        for _ in range(30):
            f.next(pw=1, p2=None)
        c = f.t + OFF + 10
        self.assertAlmostEqual(o.ld_share(a, c), 0.0)
        self.assertIsNone(o.ld_share_opt(a, c))                      # unknown is not 'absent'
        self.assertAlmostEqual(o.ld_share_opt(a, b), 0.5, places=2)
        self.assertEqual(o.ld_share_opt(b, b), 1.0)
        for _ in range(5):
            f.next(p2=1)
        end = f.t + OFF + 10
        self.assertAlmostEqual(o.ld_share(c - 2000, end), 1.0)       # 2440 of 4500 ms known, all present
        self.assertEqual(o.ld_share(c - 3000, end), 0.0)             # 2440 of 5500 ms known: < 50 %

    def test_ld_unknown_after_censor(self):
        o = Observation(P, FixedOffsetTime(0))
        f = Feed(o)
        f.next(p2=1)
        f.next(p2=1)
        f.s += 3                          # seq gap: censor
        f.next(p2=1, gap_ms=300)
        # the batch after the censor claims only its own frames, not the gap
        known, _ = o.ld.seconds(0, 10 ** 15)
        self.assertEqual(known, 440 + 500 + 440)

    def test_he_share(self):
        o = Observation(P, FixedOffsetTime(0))
        o.set_context(False, 0)
        o.set_context(True, 1000)
        o.set_context(None, 3000)
        o.set_context(True, 4000)
        self.assertAlmostEqual(o.he_share(0, 4000), 0.5)
        self.assertAlmostEqual(o.he_share(3000, 5000), 0.5)
        self.assertAlmostEqual(o.he_share(4000, 9000), 1.0)
        self.assertEqual(o.he_share(5, 5), 0.0)
        o2 = Observation(P, FixedOffsetTime(0))
        self.assertEqual(o2.he_share(0, 1000), 0.0)


if __name__ == "__main__":
    unittest.main()
