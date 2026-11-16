"""0.7.2: 'Mark spot here' diagnostics: the press counter ``last_mark.n`` and the ``detail`` of an accepted press."""
import json
import random
import unittest

from radar_zone_learner.core import labels as LB
from radar_zone_learner.core.types import MarkPress, SpotName

from .test_labels import BED, CHAIR, press, setup
from .test_learner import BASE_MS, sit_fn

DETAIL_KEYS = {"source", "age_s", "dur_s", "live_confirmed_tracks", "recent_stays"}


class AcceptedDetailTest(unittest.TestCase):
    def test_live_accept(self):
        L, f = setup()
        f.targets_until(40000, sit_fn(random.Random(1), BED))
        out = press(L, f)[0]
        self.assertTrue(out.ok)
        d = out.detail
        self.assertEqual(set(d), DETAIL_KEYS)
        self.assertEqual((d["source"], d["age_s"], d["live_confirmed_tracks"]), ("live", 0.0, 1))
        self.assertTrue(20 <= d["dur_s"] <= 36)
        self.assertEqual(L.status().attributes["last_mark"]["detail"], d)
        json.dumps(d)

    def test_recent_accept(self):
        L, f = setup()
        f.targets_until(35000, sit_fn(random.Random(2), BED))
        f.idle_until(45000)                                          # the track is gone, the stay is pending
        self.assertEqual(L.tracker.live, [])
        out = press(L, f)[0]
        self.assertTrue(out.ok)
        d = out.detail
        self.assertEqual((d["source"], d["live_confirmed_tracks"], d["recent_stays"]), ("recent", 0, 1))
        self.assertTrue(8 <= d["age_s"] <= 12)
        self.assertTrue(20 <= d["dur_s"] <= 26)

    def test_no_position_in_the_detail(self):
        L, f = setup()
        f.targets_until(40000, sit_fn(random.Random(1), BED))
        text = json.dumps(press(L, f)[0].detail)
        for key in ("x", "y", "cx", "cy", "x_cm", "y_cm"):
            self.assertNotIn('"%s"' % key, text)

    def test_the_source_is_reported_by_the_candidate_function(self):
        L, f = setup()
        f.targets_until(40000, sit_fn(random.Random(1), BED))
        stays, detail, sources = LB.mark_candidate_sources(L, BASE_MS + f.next_ms + 50)
        self.assertEqual(sources, [("live", 0.0)])
        self.assertEqual(LB.mark_candidates(L, BASE_MS + f.next_ms + 50), (stays, detail))

    def test_live_wins_when_both_merge(self):
        L, f = setup()
        rng = random.Random(6)
        f.targets_until(25000, sit_fn(rng, BED))
        f.idle_until(35000)
        f.targets_until(50000, sit_fn(rng, BED))
        out = press(L, f)[0]
        self.assertTrue(out.ok)
        self.assertEqual(out.detail["source"], "live")

    def test_refusal_is_unchanged(self):
        L, f = setup()
        f.targets_until(22000, sit_fn(random.Random(7), BED))
        f.idle_until(23000)
        out = press(L, f)[0]
        self.assertEqual(out.reason, "no_stay")
        self.assertEqual(set(out.detail), {"live_confirmed_tracks", "best_run_s", "last_target_age_s", "recent_stays"})

    def test_several_stays_refusal_is_unchanged(self):
        L, f = setup()
        rng = random.Random(5)
        f.targets_until(35000, lambda t: sit_fn(rng, BED)(t) + sit_fn(rng, CHAIR)(t))
        out = press(L, f)[0]
        self.assertEqual(out.reason, "several_stays")
        self.assertEqual(out.detail["stays"], 2)
        self.assertNotIn("source", out.detail)


class CounterTest(unittest.TestCase):
    def test_n_counts_every_press_and_is_in_the_attribute(self):
        L, f = setup()
        self.assertIsNone(L.status().attributes["last_mark"])
        press(L, f)                                                  # no_stay
        self.assertEqual(L.status().attributes["last_mark"]["n"], 1)
        f.targets_until(40000, sit_fn(random.Random(1), BED))
        press(L, f)                                                  # ok
        self.assertEqual(L.status().attributes["last_mark"]["n"], 2)
        press(L, f)                                                  # too_soon
        lm = L.status().attributes["last_mark"]
        self.assertEqual((lm["n"], lm["reason"]), (3, "too_soon"))

    def test_identical_results_differ_by_n(self):
        L, f = setup()
        a = dict(press(L, f) and L.status().attributes["last_mark"])
        f.idle_until(f.next_ms + 30000)
        press(L, f)
        b = L.status().attributes["last_mark"]
        self.assertEqual((a["result"], a["reason"], a["at"]), (b["result"], b["reason"], b["at"]))
        self.assertNotEqual(a["n"], b["n"])

    def test_n_is_ram_only(self):
        L, f = setup()
        press(L, f)
        self.assertNotIn("last_mark", json.dumps(L.export_state()))
        self.assertNotIn("mark_presses", json.dumps(L.export_state()))


if __name__ == "__main__":
    unittest.main()
