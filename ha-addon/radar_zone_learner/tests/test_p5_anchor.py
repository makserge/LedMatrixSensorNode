"""P5 time anchor: `typical_hours` and `direction` (spec 2.11, §11.3 `test_p5_anchor`)."""
import unittest

from radar_zone_learner.core.anchor import direction, typical_hours
from radar_zone_learner.core.params import DEFAULT_P5, P5Params


def prof(**hours):
    """24 bins; keyword h09=30 sets hour 9 to 30 minutes."""
    v = [0.0] * 24
    for k, m in hours.items():
        v[int(k[1:])] = float(m)
    return v


class TypicalHoursTest(unittest.TestCase):
    def test_nothing_below_the_minimum(self):
        self.assertEqual(typical_hours([0.0] * 24, DEFAULT_P5), ())
        self.assertEqual(typical_hours(prof(h09=4.9), DEFAULT_P5), ())
        self.assertEqual(typical_hours([], DEFAULT_P5), ())
        self.assertEqual(typical_hours(None, DEFAULT_P5), ())
        self.assertEqual(typical_hours(prof(h09=5.0), DEFAULT_P5), ("09-10",))

    def test_threshold_is_a_quarter_of_the_peak_and_at_least_the_minimum(self):
        # peak 40: threshold 10; 9.9 is off, 10.0 is on
        self.assertEqual(typical_hours(prof(h09=40, h10=10.0, h11=9.9), DEFAULT_P5), ("09-11",))
        # peak 12: the quarter is 3, so the 5-minute minimum rules
        self.assertEqual(typical_hours(prof(h09=12, h10=5.0, h11=4.9), DEFAULT_P5), ("09-11",))

    def test_consecutive_hours_form_a_range_and_the_end_is_exclusive(self):
        self.assertEqual(typical_hours(prof(h09=30, h10=30, h11=30, h13=30, h14=30, h15=30, h16=30), DEFAULT_P5),
                         ("09-12", "13-17"))
        self.assertEqual(typical_hours(prof(h23=30), DEFAULT_P5), ("23-24",))          # 24 is allowed
        self.assertEqual(typical_hours(prof(h00=30), DEFAULT_P5), ("00-01",))

    def test_ranges_do_not_wrap_over_midnight(self):
        self.assertEqual(typical_hours(prof(h22=30, h23=30, h00=30, h01=30), DEFAULT_P5), ("00-02", "22-24"))

    def test_the_three_biggest_ranges_in_clock_order(self):
        v = prof(h02=20, h05=50, h08=40, h11=30, h14=60)
        # five single-hour ranges, sums 20 50 40 30 60: the three biggest are 14, 05, 08 -> clock order 05 08 14
        self.assertEqual(typical_hours(v, P5Params(anchor_frac=0.0)), ("05-06", "08-09", "14-15"))
        self.assertEqual(typical_hours(v, P5Params(anchor_frac=0.0, anchor_max_ranges=1)), ("14-15",))
        self.assertEqual(typical_hours(v, P5Params(anchor_frac=0.0, anchor_max_ranges=5))[0], "02-03")

    def test_ties_keep_the_earlier_range(self):
        v = prof(h02=30, h05=30, h08=30, h11=30)
        self.assertEqual(typical_hours(v, DEFAULT_P5), ("02-03", "05-06", "08-09"))

    def test_the_result_is_plain_strings(self):
        r = typical_hours(prof(h09=30, h10=30), DEFAULT_P5)
        self.assertIsInstance(r, tuple)
        self.assertTrue(all(isinstance(x, str) and len(x) == 5 for x in r))


class DirectionTest(unittest.TestCase):
    def test_words(self):
        self.assertEqual(direction(0.5, 0.1), "to the right")
        self.assertEqual(direction(-0.5, 0.1), "to the left")
        self.assertEqual(direction(0.1, 0.5), "away from the radar")
        self.assertEqual(direction(0.1, -0.5), "towards the radar")

    def test_ties_and_zero(self):
        self.assertEqual(direction(0.5, 0.5), "to the right")                  # |dx| >= |dy|: horizontal wins
        self.assertEqual(direction(-0.5, 0.5), "to the left")
        self.assertEqual(direction(0.0, 0.0), "to the right")
        self.assertEqual(direction(0.0, -0.2), "towards the radar")


if __name__ == "__main__":
    unittest.main()
