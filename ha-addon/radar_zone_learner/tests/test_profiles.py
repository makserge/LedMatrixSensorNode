import unittest

from radar_zone_learner.core import profiles as PR
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import FixedOffsetTime

BASE_S = 1767571200          # Monday 2026-01-05 00:00 UTC


def prof_with(hours_minutes, count_per_hour=0, days=()):
    hp = PR.HourProfile()
    for h, m in hours_minutes.items():
        hp.minutes[h] = m
        hp.count[h] = count_per_hour
        hp.dates[h] = set(days)
    return hp


class ModesTest(unittest.TestCase):
    def test_wrap_around(self):
        prof = [0.0] * 24
        for h in (22, 23, 0, 1):
            prof[h] = 60.0
        m = PR.modes(prof)
        self.assertEqual(len(m), 1)
        self.assertEqual((m[0][0], m[0][1]), (21, 3))
        self.assertEqual(m[0][3], [21, 22, 23, 0, 1, 2])
        self.assertEqual(m[0][2], 240.0)

    def test_two_modes_and_edge_cases(self):
        prof = [0.0] * 24
        prof[10] = 100.0
        prof[20] = 30.0
        m = PR.modes(prof)
        self.assertEqual([(a, b) for a, b, _, _ in m], [(9, 12), (19, 22)])
        self.assertEqual(PR.modes([0.0] * 24), [])
        self.assertEqual(PR.modes([1.0] * 24)[0][:2], (0, 24))
        prof = [0.0] * 24
        prof[10] = 100.0
        prof[20] = 2.0                               # below 10 % of the smoothed peak
        self.assertEqual(len(PR.modes(prof)), 1)

    def test_build_profiles(self):
        stays = [{"t0": BASE_S + 23 * 3600 + 1800, "dur": 3600, "day": "2026-01-05"},      # Monday
                 {"t0": BASE_S + 5 * 86400 + 9 * 3600, "dur": 600, "day": "2026-01-10"}]   # Saturday
        wd, we = PR.build_profiles(stays, FixedOffsetTime(0))
        self.assertEqual(wd.minutes[23], 30.0)
        self.assertEqual(wd.minutes[0], 30.0)
        self.assertEqual(wd.count[23], 1)
        self.assertEqual(wd.count[0], 0)
        self.assertEqual(wd.dates[23], {"2026-01-05"})
        self.assertEqual(we.minutes[9], 10.0)
        self.assertEqual(sum(we.count), 1)

    def test_evidence_rule(self):
        days = ["2026-01-%02d" % d for d in (5, 6, 7, 8)]
        hp = prof_with({9: 100.0, 10: 100.0, 19: 50.0, 20: 50.0}, 5, days)
        m = PR.modes(hp.minutes)
        q = PR.qualifying(m, hp, 10, 4)
        self.assertEqual(len(q), 2)
        self.assertEqual(q[0][4], 10)                 # stays in the mode's hours (8..11 -> hours 9, 10)
        self.assertEqual(q[0][5], 4)
        hp3 = prof_with({9: 100.0, 10: 100.0, 19: 50.0, 20: 50.0}, 5, days[:3])
        self.assertEqual(PR.qualifying(PR.modes(hp3.minutes), hp3, 10, 4), [])
        hp4 = prof_with({9: 100.0, 10: 100.0, 19: 50.0, 20: 50.0}, 4, days)
        self.assertEqual(PR.qualifying(PR.modes(hp4.minutes), hp4, 10, 4), [])

    def test_share_rule(self):
        prof = [0.0] * 24
        prof[10] = 70.0
        prof[20] = 30.0
        self.assertTrue(PR.share_split(PR.modes(prof), 0.30))
        prof[20] = 20.0
        self.assertFalse(PR.share_split(PR.modes(prof), 0.30))
        self.assertFalse(PR.share_split(PR.modes([0.0] * 23 + [1.0]), 0.30))


class WindowFreezeTest(unittest.TestCase):
    def test_window_hours(self):
        self.assertEqual(PR.window_hours(0, 360), frozenset(range(6)))
        self.assertEqual(PR.window_hours(1320, 360), frozenset([22, 23, 0, 1, 2, 3, 4, 5]))
        self.assertEqual(PR.window_hours(1410, 375), frozenset([23, 0, 1, 2, 3, 4, 5, 6]))
        self.assertEqual(PR.window_hours(60, 60), frozenset())
        self.assertEqual(PR.window_hours(None, 360), frozenset())

    def test_freeze_truth_table(self):
        win = PR.window_hours(0, 360)
        night = [0.0] * 24
        night[1] = 10.0                                   # 100 % inside the window
        day = [0.0] * 24
        day[12] = 10.0
        half = [0.0] * 24
        half[2] = 5.0
        half[14] = 5.0
        for sched in (None, False, True):
            for kind in ("bed", "dwell"):
                for hours, share_ok in ((night, True), (day, False), (half, True), (None, False)):
                    want = sched is True and (kind == "bed" or share_ok)
                    self.assertEqual(PR.frozen(sched, kind, hours, win, P), want, (sched, kind, hours))

    def test_g5b_suppressed_for_frozen_bed(self):
        days = ["2026-01-%02d" % d for d in (5, 6, 7, 8, 9)]
        wd = prof_with({22: 60.0, 23: 60.0, 6: 60.0, 7: 60.0}, 5, days)     # modes 21-01 h and 05-09 h
        we = PR.HourProfile()
        modes = [(a, b) for a, b, _, _ in PR.modes(wd.minutes)]
        self.assertEqual(modes, [(5, 9), (21, 1)])
        win = PR.window_hours(0, 360)
        self.assertTrue(PR.suppressed(PR.qualifying(PR.modes(wd.minutes), wd, 10, 4), win))
        v = PR.variants_for(wd, we, False, True, True, win, P)
        self.assertTrue(v["evidence_split"])
        self.assertTrue(v["suppressed"])
        self.assertEqual(v["variants"], [])
        # not frozen: the split is kept as variants
        v = PR.variants_for(wd, we, False, True, False, win, P)
        self.assertFalse(v["suppressed"])
        self.assertEqual([(x["start_h"], x["end_h"], x["days"]) for x in v["variants"]], [(5, 9, "wd"), (21, 1, "wd")])
        self.assertEqual(v["variants"][0]["stays"], 10)
        self.assertEqual(v["variants"][0]["days_n"], 5)
        # frozen, but the gap between the modes is not inside the window: kept
        v = PR.variants_for(wd, we, False, True, True, PR.window_hours(120, 180), P)
        self.assertFalse(v["suppressed"])
        self.assertEqual(len(v["variants"]), 2)
        # the freeze needs a known schedule ON for the bed (D20)
        self.assertFalse(PR.frozen(None, "bed", None, win, P))
        self.assertTrue(PR.frozen(True, "bed", None, win, P))

    def test_weekend_needs_14_days(self):
        days = ["2026-01-%02d" % d for d in (10, 11, 17, 18)]
        we = prof_with({9: 60.0, 10: 60.0, 19: 60.0, 20: 60.0}, 5, days)
        wd = PR.HourProfile()
        self.assertFalse(PR.variants_for(wd, we, False, None, False, frozenset(), P)["evidence_split"])
        v = PR.variants_for(wd, we, True, None, False, frozenset(), P)
        self.assertTrue(v["evidence_split"])
        self.assertEqual({x["days"] for x in v["variants"]}, {"we"})

    def test_gap_hours(self):
        self.assertEqual(PR.gap_hours(1, 5), [1, 2, 3, 4])
        self.assertEqual(PR.gap_hours(22, 2), [22, 23, 0, 1])

    def test_bed_naming(self):
        night = [0.0] * 24
        for h in (22, 23, 0, 1, 7, 8):
            night[h] = 60.0
        zero = [0.0] * 24
        self.assertTrue(PR.bed_named(night, zero, 7200.0, P))
        self.assertFalse(PR.bed_named(night, zero, 7199.0, P))      # stays too short
        day = list(night)
        for h in range(9, 21):
            day[h] = 60.0
        self.assertFalse(PR.bed_named(day, zero, 9000.0, P))       # < 60 % at night
        self.assertFalse(PR.bed_named(zero, zero, 9000.0, P))


if __name__ == "__main__":
    unittest.main()
