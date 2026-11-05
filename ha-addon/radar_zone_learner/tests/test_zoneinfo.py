"""Nightly zone evidence: learned delay, ld2412_reliable, the copy of variants and hours (P6 spec 5.4, 5.6, 5.8; P6-7,
P6-10 at unit level). Owner: CORE."""
import unittest

from radar_zone_learner.core import Learner, zoneinfo
from radar_zone_learner.core.params import DEFAULT_P6, DEFAULT_PARAMS as P, P6Params
from radar_zone_learner.core.types import FixedOffsetTime
from tests.test_learner import BASE_MS
from tests.test_p6_learner import CELLS, zone_learner


class LearnedDelayTest(unittest.TestCase):
    def test_nearest_rank_values(self):
        self.assertEqual(zoneinfo.learned_delay(list(range(1, 21)) and [i * 10 for i in range(1, 21)], DEFAULT_P6), 190)   # 19th of 20
        self.assertEqual(zoneinfo.learned_delay([i * 10 for i in range(1, 11)], DEFAULT_P6), 100)                          # the 10th of 10
        self.assertEqual(zoneinfo.learned_delay([i * 5 for i in range(1, 51)], DEFAULT_P6), 240)                           # 48th of 50

    def test_below_the_minimum_there_is_no_learned_delay(self):
        self.assertIsNone(zoneinfo.learned_delay([100] * 9, DEFAULT_P6))
        self.assertIsNone(zoneinfo.learned_delay([], DEFAULT_P6))

    def test_clamped_to_5_600(self):
        self.assertEqual(zoneinfo.learned_delay([1] * 12, DEFAULT_P6), 5)
        self.assertEqual(zoneinfo.learned_delay([5000] * 12, DEFAULT_P6), 600)

    def test_order_does_not_matter(self):
        a = [30, 5, 90, 12, 200, 40, 41, 42, 7, 8, 9]
        self.assertEqual(zoneinfo.learned_delay(a, DEFAULT_P6), zoneinfo.learned_delay(sorted(a), DEFAULT_P6))


class ReliableTest(unittest.TestCase):
    def test_share_and_time(self):
        p = DEFAULT_P6
        self.assertTrue(zoneinfo.reliable([2900, 3600], p))          # 0.805
        self.assertFalse(zoneinfo.reliable([2800, 3600], p))         # 0.777
        self.assertFalse(zoneinfo.reliable([3000, 3599], p))         # not enough known time
        self.assertTrue(zoneinfo.reliable([2880, 3600], p))          # exactly 0.8
        self.assertFalse(zoneinfo.reliable(None, p))
        self.assertFalse(zoneinfo.reliable([0, 0], p))


class NightlyStepTest(unittest.TestCase):
    def test_ldr_is_set_and_cleared_by_the_evidence(self):
        L = zone_learner()
        L.zone_states()
        L.zone6.ld["l0001"] = [3000, 3700]
        zoneinfo.nightly(L, 1)
        self.assertTrue(L.zone6.is_ldr("l0001"))
        self.assertTrue(L.zone_states().zones[0].ldr)
        L.zone6.ld["l0001"] = [1000, 4000]
        zoneinfo.nightly(L, 2)
        self.assertFalse(L.zone6.is_ldr("l0001"))

    def test_the_spells_are_kept_across_the_run(self):
        L = zone_learner()
        L.zone_states()
        L.zone6.delay["l0001"] = {"sp": [40] * 12}
        zoneinfo.nightly(L, 1)
        self.assertEqual(L.zone6.delay["l0001"]["cd"], 40)
        self.assertEqual(len(L.zone6.delay["l0001"]["sp"]), 12)

    def test_info_is_copied_from_the_source_suggestion_and_survives_it(self):
        L = Learner(P, FixedOffsetTime(0))
        ev = {"hours_wd": [0.0] * 9 + [30.0, 40.0, 35.0] + [0.0] * 12,
              "hours_we": [0.0] * 20 + [20.0, 25.0, 22.0, 0.0], "hours_at_suggestion": None}
        L.ns.suggestions.append({"id": "s0001-1", "rev": 1, "type": "dwell_zone", "kind": "dwell", "name_hint": None,
                                 "state": "shadow", "first_night": 1, "last_night": 3, "cx": 0.1, "cy": 3.1,
                                 "cells": CELLS, "poly_cm": [], "conf": 0.8149, "evidence": ev,
                                 "variants": [{"days": "wd", "start_h": 9, "end_h": 17, "stays": 12, "days_n": 5}],
                                 "variant_evidence_split": True, "variant_share_split": False,
                                 "variant_suppressed": False, "frozen": False})
        L.review.zones.append({"zid": "z0001", "name": "Desk", "kind": "dwell", "src": "approve",
                               "ref": "s0001-1", "cx": 0.1, "cy": 3.1, "cells": CELLS, "night": 3,
                               "created_s": BASE_MS // 1000, "poly_cm": [], "rev": 1, "upd": 3, "srev": 1,
                               "also": []})
        L.review.touch()
        L.zone_states()
        zoneinfo.nightly(L, 4)
        info = L.zone6.info["z0001"]
        self.assertEqual(info["th"], {"wd": ["09-12"], "we": ["20-23"]})
        self.assertEqual(info["var"], [{"d": "wd", "s": 9, "e": 17, "n": 12}])
        self.assertEqual(info["conf"], 0.81)
        s = L.zone_states().zones[0]
        self.assertEqual((s.typical_hours, s.confidence, s.variants), ("wd 09-12; we 20-23", 0.81, ("wd0917",)))
        # the suggestion goes: the stored copy stays
        L.ns.suggestions.clear()
        zoneinfo.nightly(L, 5)
        self.assertEqual(L.zone6.info["z0001"]["conf"], 0.81)
        self.assertEqual(L.zone6.info["z0001"]["upd"], 4)

    def test_a_label_has_no_info(self):
        L = zone_learner()
        L.zone_states()
        zoneinfo.nightly(L, 1)
        self.assertNotIn("l0001", L.zone6.info)
        s = L.zone_states().zones[0]
        self.assertEqual((s.typical_hours, s.confidence, s.variants, s.variant), ("", None, (), ""))

    def test_a_variant_request_for_a_vanished_variant_is_dropped(self):
        L = zone_learner()
        L.zone_states()
        L.zone6.info["l0001"] = {"th": {}, "var": [{"d": "wd", "s": 9, "e": 17, "n": 1}], "conf": 0.5}
        L.zone6.ve["l0001"] = ["wd0917", "we2023"]
        zoneinfo.nightly(L, 1)
        self.assertEqual(L.zone6.ve["l0001"], ["wd0917"])
        L.zone6.info["l0001"]["var"] = []
        zoneinfo.nightly(L, 2)
        self.assertNotIn("l0001", L.zone6.ve)


if __name__ == "__main__":
    unittest.main()
