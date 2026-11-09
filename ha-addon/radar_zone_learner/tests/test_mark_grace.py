"""0.7.1: 'Mark spot here' counts a recent pending stay whose track was lost (mark_grace_s) and a refusal carries numbers."""
import json
import random
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import FixedOffsetTime, MarkResult, SpotName

from .test_labels import BED, CHAIR, press, setup
from .test_learner import BASE_MS, sit_fn


class MarkGraceTest(unittest.TestCase):
    def test_default(self):
        self.assertEqual((P.mark_grace_s, P.mark_min_stay_s), (60, 20))

    def test_live_stay_unchanged(self):
        L, f = setup()
        f.targets_until(40000, sit_fn(random.Random(1), BED))
        outs = press(L, f)
        self.assertEqual(outs[0], MarkResult(True, None, "l0001", "manual"))
        self.assertIsNone(outs[0].detail)
        self.assertNotIn("detail", L.status().attributes["last_mark"])

    def test_track_died_10s_ago_with_25s_pending_stay_is_ok(self):
        L, f = setup()
        f.targets_until(35000, sit_fn(random.Random(2), BED))      # 25 s visible
        f.idle_until(45000)                                          # lost for 10 s
        self.assertEqual(L.tracker.live, [])
        self.assertEqual(len(L.bh.pending), 1)
        outs = press(L, f)
        self.assertTrue(outs[0].ok)
        lb = L.labels()[0]
        self.assertAlmostEqual(lb.x_m, BED[0], delta=0.05)
        self.assertAlmostEqual(lb.y_m, BED[1], delta=0.05)

    def test_pending_stay_older_than_grace_is_no_stay(self):
        L, f = setup()
        f.targets_until(35000, sit_fn(random.Random(3), BED))
        f.idle_until(35000 + 62000)
        outs = press(L, f)
        self.assertEqual(outs[0].reason, "no_stay")
        self.assertEqual(L.labels(), ())

    def test_short_pending_stay_is_no_stay_and_counted(self):
        L, f = setup()
        f.targets_until(24000, sit_fn(random.Random(4), BED))      # 14 s: a fragment, but < 20 s
        f.idle_until(34000)
        outs = press(L, f)
        self.assertEqual(outs[0].reason, "no_stay")
        self.assertEqual(outs[0].detail["recent_stays"], 1)

    def test_two_distinct_recent_stays_are_several(self):
        L, f = setup()
        rng = random.Random(5)
        f.targets_until(35000, lambda t: sit_fn(rng, BED)(t) + sit_fn(rng, CHAIR)(t))
        f.idle_until(45000)
        outs = press(L, f)
        self.assertEqual(outs[0].reason, "several_stays")
        self.assertEqual(outs[0].detail["stays"], 2)
        self.assertEqual(L.labels(), ())

    def test_live_and_its_pending_stay_merge(self):
        L, f = setup()
        rng = random.Random(6)
        f.targets_until(25000, sit_fn(rng, BED))
        f.idle_until(35000)
        f.targets_until(50000, sit_fn(rng, BED))                    # the same spot again, live
        outs = press(L, f)
        self.assertTrue(outs[0].ok)
        self.assertEqual(len(L.labels()), 1)

    def test_refusal_numbers(self):
        L, f = setup()
        f.targets_until(22000, sit_fn(random.Random(7), BED))      # 12 s run, live
        f.idle_until(23000)
        outs = press(L, f)
        d = outs[0].detail
        self.assertEqual(outs[0].reason, "no_stay")
        self.assertEqual(set(d), {"live_confirmed_tracks", "best_run_s", "last_target_age_s", "recent_stays"})
        self.assertEqual(d["live_confirmed_tracks"], 1)
        self.assertTrue(10 <= d["best_run_s"] <= 13)
        self.assertTrue(0.5 <= d["last_target_age_s"] <= 2.5)
        self.assertEqual(L.status().attributes["last_mark"]["detail"], d)
        json.dumps(d)

    def test_refusal_without_any_detection(self):
        L, f = setup()
        d = press(L, f)[0].detail
        self.assertEqual((d["live_confirmed_tracks"], d["best_run_s"], d["last_target_age_s"], d["recent_stays"]),
                         (0, 0.0, None, 0))

    def test_not_observing_and_too_soon_have_no_detail(self):
        L = Learner(P, FixedOffsetTime(0))
        self.assertIsNone(L.handle(__import__("radar_zone_learner.core.types", fromlist=["MarkPress"]).MarkPress(BASE_MS + 1))[0].detail)
        L, f = setup()
        L.handle(SpotName("Bed", BASE_MS + 9000))
        f.targets_until(40000, sit_fn(random.Random(8), BED))
        press(L, f)
        f.targets_until(f.next_ms + 500, sit_fn(random.Random(8), BED))
        self.assertIsNone(press(L, f)[0].detail)


if __name__ == "__main__":
    unittest.main()
