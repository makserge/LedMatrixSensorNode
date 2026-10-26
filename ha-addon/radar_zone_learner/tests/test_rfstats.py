import json
import unittest
from pathlib import Path

from radar_zone_learner.shell.rfstats import BUCKET_S, FILE_NAME, MAX_GAP_S, RfStats
from tests._tmp import tmpdir
from tests.fakes import quiet_logs

T0 = 1_767_571_200.0          # 2026-01-05 00:00:00 UTC, on a bucket edge
H = 3600.0


def run(rf, t, seconds, step=60.0):
    """Advances ``seconds`` in steps of ``step`` (the service ticks every second; a minute is the same here)."""
    end = t + seconds
    while t < end:
        t = min(t + step, end)
        rf.advance(t)
    return t


class RfStatsTest(unittest.TestCase):
    def test_no_state_no_numbers(self):
        rf = RfStats()
        t = run(rf, T0, 10 * H)
        self.assertIsNone(rf.rf_on_24h(t))
        self.assertIsNone(rf.rf_saving_7d(t))
        self.assertEqual(rf.coverage_7d(t), 0.0)

    def test_on_all_day(self):
        rf = RfStats()
        rf.set_radar(True, T0)
        t = run(rf, T0, 24 * H)
        self.assertEqual(rf.rf_on_24h(t), 24.0)
        self.assertEqual(rf.rf_saving_7d(t), 0.0)

    def test_night_window_saving(self):
        rf = RfStats()
        rf.set_radar(True, T0)
        rf.set_valid(True, T0)
        t = run(rf, T0, 24 * H)                              # day 1: 24 h on (00:00-24:00 before the schedule)
        for _ in range(2):                                   # nights 2 and 3: off 00:00-06:00, on 06:00-24:00
            rf.set_radar(False, t)
            rf.set_valid(False, t)
            t = run(rf, t, 6 * H)
            rf.set_radar(True, t)
            rf.set_valid(True, t)
            t = run(rf, t, 18 * H)
        self.assertEqual(rf.rf_on_24h(t), 18.0)              # the last 24 h: 6 h off + 18 h on
        self.assertAlmostEqual(rf.rf_saving_7d(t), 100.0 * 12 / 72, places=0)
        # coverage: 24 + 18 + 18 valid hours of the 168 h week
        self.assertEqual(rf.coverage_7d(t), round(100.0 * 60 / 168))

    def test_week_window_drops_old_days(self):
        rf = RfStats()
        rf.set_radar(False, T0)
        t = run(rf, T0, 24 * H)                              # one off day
        rf.set_radar(True, t)
        t = run(rf, t, 7 * 24 * H)                           # then a week on
        self.assertEqual(rf.rf_saving_7d(t), 0.0)
        self.assertEqual(rf.rf_on_24h(t), 24.0)

    def test_unknown_time_is_neither_on_nor_off(self):
        rf = RfStats()
        rf.set_radar(True, T0)
        t = run(rf, T0, 2 * H)
        rf.set_radar(None, t)                                # node offline
        t = run(rf, t, 5 * H)
        rf.set_radar(False, t)
        t = run(rf, t, 3 * H)
        self.assertEqual(rf.rf_on_24h(t), 2.0)
        self.assertEqual(rf.rf_saving_7d(t), 60.0)           # 3 h off of 5 h known

    def test_a_long_gap_is_not_counted(self):
        rf = RfStats()
        rf.set_radar(True, T0)
        rf.advance(T0 + 60)
        rf.advance(T0 + 60 + MAX_GAP_S + 1)                  # the app was stopped
        rf.advance(T0 + 60 + MAX_GAP_S + 31)
        total = sum(r[0] for r in rf.buckets.values())
        self.assertAlmostEqual(total, 60 + 30, delta=0.01)   # the gap itself is not booked
        self.assertIsNone(rf.rf_on_24h(T0 + 2 * H))          # under an hour known

    def test_time_is_split_over_bucket_edges(self):
        rf = RfStats()
        rf.set_radar(True, T0 + BUCKET_S - 10)
        rf.advance(T0 + BUCKET_S + 20)
        self.assertEqual(sorted(rf.buckets), [int(T0 // BUCKET_S), int(T0 // BUCKET_S) + 1])
        self.assertAlmostEqual(rf.buckets[int(T0 // BUCKET_S)][0], 10.0)
        self.assertAlmostEqual(rf.buckets[int(T0 // BUCKET_S) + 1][0], 20.0)

    def test_export_and_load(self):
        rf = RfStats()
        rf.set_radar(True, T0)
        rf.set_valid(True, T0)
        t = run(rf, T0, 5 * H)
        data = json.loads(json.dumps(rf.export()))
        rf2 = RfStats()
        rf2.load(data, t)
        self.assertEqual(rf2.rf_on_24h(t), rf.rf_on_24h(t))
        self.assertEqual(rf2.coverage_7d(t), rf.coverage_7d(t))

    def test_load_ignores_garbage(self):
        rf = RfStats()
        for data in (None, [], {"v": 2}, {"v": 1, "bucket_s": 900, "b": []},
                     {"v": 1, "bucket_s": 900, "b": {"x": [1, 2, 3], "5": [1, 2], "6": "no",
                                                       str(int(T0 // BUCKET_S)): [0, 5000, 0],
                                                       "1": [1, 1, 1]}}):
            rf.load(data, T0)
            self.assertEqual(rf.buckets, {})

    def test_file_roundtrip_and_bad_file(self):
        quiet_logs(self)
        with tmpdir() as d:
            rf = RfStats()
            rf.set_radar(False, T0)
            t = run(rf, T0, 3 * H)
            rf.save_file(Path(d))
            self.assertTrue((Path(d) / FILE_NAME).exists())
            rf2 = RfStats()
            rf2.load_file(Path(d), t)
            self.assertEqual(rf2.rf_saving_7d(t), 100.0)
            (Path(d) / FILE_NAME).write_text("{oops", encoding="utf-8")
            rf3 = RfStats()
            rf3.load_file(Path(d), t)
            self.assertEqual(rf3.buckets, {})
            rf4 = RfStats()
            rf4.load_file(Path(d) / "missing", t)
            self.assertEqual(rf4.buckets, {})

    def test_old_buckets_are_pruned(self):
        rf = RfStats()
        rf.set_radar(True, T0)
        t = run(rf, T0, 10 * 24 * H, step=600.0)
        self.assertLessEqual(len(rf.buckets), 8 * 96 + 5)
        self.assertEqual(rf.rf_on_24h(t), 24.0)


if __name__ == "__main__":
    unittest.main()
