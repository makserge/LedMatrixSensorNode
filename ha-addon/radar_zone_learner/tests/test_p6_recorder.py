"""P6-6 (ii): the recorder budget as a proxy (P6 spec 7.5, D22, DV8, Q13, Q20) on a synthetic stream of at most three simulated
days: the real ``P6Layer`` and a fake Home Assistant with the recorder model of ``tests/fake_ha_zone.py`` (HA[U4]: one row per
change of an entity's effective state or of its attributes; an availability flip is two rows).

What it checks:

- the layer publishes exactly what the model of spec 7.5 predicts (occupancy 2 per episode, event 2, count 2 for a one-person
  episode, availability 2 per radar-off cycle, the state source 3 attribute changes);
- occupancy and availability are never delayed or limited, the count is published at most once a second with a trailing
  publish of the latest value;
- a flapping zone with E = 120 episodes in a day: the rows are reported (default set 4E + 7 here, spec bound 4E + 12 = 492; all
  recorded 6E + 9, above 500: a fault, DV8), and with ``valve_after_episodes`` = 35 the all-recorded set stays below 500 (spec:
  6 x 35 + 52 + 2 x 85 = 432) while every occupancy change is still published at once.

The real row count on the real Home Assistant is acceptance A8; this is arithmetic on assumptions."""
import unittest

from radar_zone_learner.core.types import ZoneEvent
from tests.p6_harness import AVAIL, DAY_MS, LayerRig, zone_view

OCC = "radar_zones/zone/z0001/occupancy"
COUNT = "radar_zones/zone/z0001/count"
EVENT = "radar_zones/zone/z0001/event"
ATTR = "radar_zones/zone/z0001/attributes"
OCC_ID = "binary_sensor.radar_spot_z0001"
EV_ID = "event.radar_spot_z0001_event"
CNT_ID = "sensor.radar_spot_z0001_count"
H = 3600


class Stream:
    """A synthetic stream for one zone: radar off 00:00-06:00, episodes between 08:00 and 22:00 on the rig's wall clock."""

    def __init__(self, test, valve: int = 0, variants=(), count_recorded: bool = False) -> None:
        self.rig = LayerRig(test, valve=valve)
        self.test = test
        self.rig.zones(zone_view("z0001", "Desk"))
        self.rig.learner.p6_zone("z0001", variants=tuple(variants), requested=tuple(variants))
        self.rig.start()
        self.rig.radar()
        self.rig.push_states(True)
        self.rig.run(11)
        if count_recorded:
            self.assertTrue(self.rig.ha.enable(CNT_ID))
        self.day0 = self.rig.clock.wall_ms // DAY_MS
        self.rig.clock.wall_ms = self.day0 * DAY_MS              # the wall clock goes back to the midnight the stream starts at
        self.track = 100
        self.pushes: list = []             # (wall_ms, occ) of every occupancy change the learner made

    def assertTrue(self, v):
        self.test.assertTrue(v)

    # ------------------------------------------------------------ time
    def goto(self, day: int, sec: float) -> None:
        """Moves the shared clock to ``sec`` seconds into simulated day ``day`` and ticks the layer once."""
        target = (self.day0 + day) * DAY_MS + int(sec * 1000)
        delta = (target - self.rig.clock.wall_ms) / 1000.0
        self.test.assertGreaterEqual(delta, 0, (day, sec))
        if delta:
            self.rig.clock.advance(delta)
        self.rig.layer.tick(self.rig.ms)
        self.rig.ha.process()

    # ------------------------------------------------------------ the night
    def night_off(self, day: int) -> None:
        self.goto(day, 0)
        self.rig.radar(state="night_off", observing=False)
        self.rig.push_states(valid=False)

    def morning_on(self, day: int) -> None:
        self.goto(day, 5 * H + 3590)
        self.rig.radar(state="on", observing=False)                          # powered, warming up
        self.goto(day, 6 * H)
        self.rig.radar(state="on", observing=True)
        self.rig.push_states(valid=True)

    # ------------------------------------------------------------ one episode of one person
    def episode(self, day: int, sec: float, stay_s: float = 10.0, third=False) -> None:
        L = self.rig.learner
        self.goto(day, sec)
        self.track += 1
        self.rig.feed(ZoneEvent("z0001", "enter", "", 1000, self.track, 0, ""))
        L.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.pushes.append((self.rig.clock.wall_ms, True))
        if third:
            self.goto(day, sec + 3)
            L.p6_zone("z0001", count=2)
            self.rig.push_states()
            self.goto(day, sec + 6)
            L.p6_zone("z0001", count=1)
            self.rig.push_states()
        self.goto(day, sec + stay_s)
        self.rig.feed(ZoneEvent("z0001", "leave", "edge", 2000, self.track, int(stay_s), ""))
        L.p6_zone("z0001", occ=False, count=0)
        self.rig.push_states()
        self.pushes.append((self.rig.clock.wall_ms, False))

    def day(self, day: int, episodes: int, spacing_s: float = 600.0, first: float = 8 * H, **kw) -> None:
        self.night_off(day)
        self.morning_on(day)
        for k in range(episodes):
            self.episode(day, first + k * spacing_s, **kw)
        self.goto(day, 23 * H)


class ModelTest(unittest.TestCase):
    """The layer publishes what the model of spec 7.5 predicts, three days of 30 episodes."""

    E = 30

    def setUp(self):
        self.s = Stream(self, count_recorded=True)
        for d in range(3):
            self.s.day(d, self.E)
        self.rig = self.s.rig
        self.ha = self.rig.ha
        self.day = self.s.day0 + 1                                           # a complete day: the first has the start-up

    def test_the_publishes_of_a_day_equal_the_model(self):
        E = self.E
        n = lambda topic: len(self.rig.stamped(topic, self.day))
        self.assertEqual(n(OCC), 2 * E + 1)               # two a episode and the resume's republish of the real state (D20)
        self.assertEqual(n(EVENT), 2 * E)                 # enter and leave: real transitions only
        self.assertEqual(n(COUNT), 2 * E + 1)             # 0 -> 1 and 1 -> 0 an episode, and the resume's republish
        self.assertEqual(n(AVAIL), 2)                     # one radar-off cycle a day: offline at 00:00, online at 06:00
        self.assertEqual(n(ATTR), 3)                      # the state source: radar_off, warmup, live

    def test_the_rows_of_the_default_set_are_inside_the_spec_bound(self):
        E = self.E
        default = (self.ha.rows(OCC_ID, self.day) + self.ha.rows(EV_ID, self.day))
        self.assertEqual(default, 4 * E + 7)              # occupancy 2E + 5 (availability 2, attributes 3), event 2E + 2
        self.assertLessEqual(default, 4 * E + 12)         # spec 7.5: rows = 4E + 12 for the default set
        self.assertLess(default, 500)

    def test_the_rows_with_everything_recorded_are_inside_the_spec_bound(self):
        E = self.E
        every = sum(self.ha.rows(e, self.day) for e in (OCC_ID, EV_ID, CNT_ID))
        self.assertEqual(every, 6 * E + 9)                # + the count entity: 2E and its two availability rows
        self.assertLessEqual(every, 6 * E + 52)           # spec 7.5: 52 rows of fixed overhead in the worst case
        self.assertLess(every, 500)

    def test_the_whole_zone_per_day_stays_under_the_line_for_every_complete_day(self):
        for d in (1, 2):
            self.assertLess(self.ha.rows_of_zone("z0001", self.s.day0 + d), 500, d)

    def test_the_night_cycle_gives_unavailable_then_the_state_every_day(self):
        states = self.ha.states(OCC_ID)
        self.assertEqual(states.count("unavailable"), 1 + 3)                  # the entity's first row and one a night
        for i, st in enumerate(states):
            if st == "unavailable" and i + 1 < len(states):
                self.assertIn(states[i + 1], ("on", "off"))
        self.assertNotIn("unknown", states[2:])

    def test_no_event_comes_from_the_nights(self):
        # every event of the stream is one of the scripted episodes: no enter or leave at 00:00 or 06:00
        for ms, payload in self.rig.stamped(EVENT):
            sec = (ms % DAY_MS) / 1000
            self.assertGreaterEqual(sec, 8 * H)
            self.assertLessEqual(sec, 23 * H)


class NeverDelayedTest(unittest.TestCase):
    def test_every_occupancy_change_is_published_in_the_same_instant_and_availability_too(self):
        s = Stream(self)
        s.day(0, 25, spacing_s=7.0, first=8 * H, stay_s=3.0)                   # episodes 7 s apart, each 3 s long
        stamps = s.rig.stamped(OCC)
        pushed = s.pushes
        # the first publish of the stream is the start-up value; then one publish per change at the instant of the change
        published = [(ms, v == "ON") for ms, v in stamps if (ms % DAY_MS) >= 8 * H * 1000]
        self.assertEqual(published, pushed)

    def test_the_off_of_the_availability_is_at_the_instant_the_shell_sees_the_radar_go(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"))
        t0 = rig.clock.wall_ms
        rig.radar(state="night_off", observing=False)
        offs = [ms for ms, v in rig.stamped(AVAIL) if v == "offline"]
        self.assertEqual(offs[-1], t0)

    def test_the_count_is_published_at_most_once_a_second_whatever_the_core_does(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"))
        L = rig.learner
        L.p6_zone("z0001", occ=True)
        for k in range(100):                                                 # the count flaps every 0.3 s for 30 s
            rig.clock.advance(0.3)
            L.p6_zone("z0001", count=1 + (k % 3))
            rig.push_states()
            rig.layer.tick(rig.ms)
        rig.run(3)
        times = [ms for ms, _ in rig.stamped(COUNT)]
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertGreater(len(times), 20)
        self.assertGreaterEqual(min(gaps), 1000)
        self.assertEqual(rig.stamped(COUNT)[-1][1], str(1 + (99 % 3)))       # the trailing publish is the latest value


class FlappingZoneTest(unittest.TestCase):
    """E = 120 episodes in a day: a ghost or a flapping zone. The spec's line is conditional on the episode rate (DV8)."""

    E = 120

    def run_stream(self, valve):
        s = Stream(self, valve=valve, count_recorded=True)
        s.day(0, 0)
        s.day(1, self.E, spacing_s=60.0)
        return s

    def rows(self, s, day_offset=1):
        day = s.day0 + day_offset
        ha = s.rig.ha
        return {"default": ha.rows(OCC_ID, day) + ha.rows(EV_ID, day),
                "all": sum(ha.rows(e, day) for e in (OCC_ID, EV_ID, CNT_ID))}

    def test_without_the_valve_the_default_set_is_inside_the_line_and_the_all_recorded_set_is_not(self):
        s = self.run_stream(valve=0)
        rows = self.rows(s)
        E = self.E
        self.assertEqual(rows["default"], 4 * E + 7)
        self.assertLessEqual(rows["default"], 4 * E + 12)                      # spec 7.5: 492 predicted
        self.assertLess(rows["default"], 500)
        self.assertEqual(rows["all"], 6 * E + 9)                               # 729: above 500, a fault to look at (DV8)
        self.assertGreater(rows["all"], 500)
        self.assertEqual(s.rig.layer.attributes()["zone_valve_zones"], 0)      # no valve: nothing is held back
        self.assertEqual(len(s.rig.stamped(OCC, s.day0 + 1)), 2 * E + 1)
        self.assertEqual(len(s.rig.stamped(EVENT, s.day0 + 1)), 2 * E)

    def test_with_the_valve_at_35_the_all_recorded_set_stays_below_the_line(self):
        s = self.run_stream(valve=35)
        rows = self.rows(s)
        E = self.E
        self.assertLess(rows["all"], 500)
        self.assertLessEqual(rows["all"], 6 * 35 + 52 + 2 * (E - 35))          # spec 7.5: 432
        self.assertEqual(rows["all"], 2 * E + 2 * 35 + 2 * 35 + 9)             # occupancy all, event and count 35 episodes
        self.assertEqual(s.rig.layer.attributes()["zone_valve_zones"], 1)

    def test_with_the_valve_every_occupancy_change_is_still_published_at_once(self):
        s = self.run_stream(valve=35)
        E = self.E
        day_ms = (s.day0 + 1) * DAY_MS
        published = [(ms, v == "ON") for ms, v in s.rig.stamped(OCC) if ms >= day_ms + 8 * H * 1000]
        self.assertEqual(published, s.pushes[-2 * E:])
        self.assertEqual(len(s.rig.stamped(EVENT, s.day0 + 1)), 2 * 35)       # the 36th episode's events are held back
        attrs = s.rig.layer.attributes()
        self.assertEqual(attrs["zone_events_dropped"], 2 * (E - 35))
        self.assertEqual(attrs["zone_events_sent"], 2 * 35)

    def test_the_availability_is_never_held_back_by_the_valve(self):
        s = self.run_stream(valve=35)
        self.assertEqual(len(s.rig.stamped(AVAIL, s.day0 + 1)), 2)
        s.rig.radar(state="night_off", observing=False)
        self.assertEqual(s.rig.stamped(AVAIL)[-1][1], "offline")

    def test_the_valve_ends_with_the_local_day_and_the_latest_values_follow(self):
        s = Stream(self, valve=3, count_recorded=True)
        s.night_off(0)
        s.morning_on(0)
        for k in range(5):
            s.episode(0, 8 * H + k * 60)                                       # episodes 4 and 5 are held back
        L = s.rig.learner
        self.assertEqual(s.rig.layer.attributes()["zone_valve_zones"], 1)
        s.goto(0, 12 * H)
        L.p6_zone("z0001", variant="wd0917", count=2)
        s.rig.push_states()
        n_attr = len(s.rig.stamped(ATTR))
        n_count = len(s.rig.stamped(COUNT))
        s.rig.run(5)
        self.assertEqual((len(s.rig.stamped(ATTR)), len(s.rig.stamped(COUNT))), (n_attr, n_count))   # held back all day
        self.assertEqual(s.rig.ha.attrs(OCC_ID)["variant_now"], "")
        s.goto(1, 1)                                                           # the next local day
        self.assertEqual(s.rig.layer.attributes()["zone_valve_zones"], 0)
        s.rig.run(2)
        self.assertEqual(s.rig.ha.attrs(OCC_ID)["variant_now"], "wd0917")      # one trailing publish of the latest values
        self.assertEqual(s.rig.stamped(ATTR)[-1][0] // DAY_MS, s.day0 + 1)

    def test_a_valved_episode_count_starts_again_the_next_day(self):
        s = Stream(self, valve=2)
        s.day(0, 4)
        sent_day0 = len(s.rig.stamped(EVENT, s.day0))
        self.assertEqual(sent_day0, 4)                                         # 2 episodes of 2 events
        s.day(1, 4)
        self.assertEqual(len(s.rig.stamped(EVENT, s.day0 + 1)), 4)


class VariantRowsTest(unittest.TestCase):
    def test_a_requested_variant_sensor_costs_rows_only_while_it_is_the_variant_in_force(self):
        s = Stream(self, variants=("wd0917", "we1022"))
        L = s.rig.learner
        s.night_off(0)
        s.morning_on(0)
        L.p6_zone("z0001", variant="wd0917")
        s.rig.push_states()
        for k in range(5):
            s.episode(0, 8 * H + k * 60)
        a = s.rig.ha.rows("binary_sensor.radar_spot_z0001_v_wd0917", s.day0)
        b = s.rig.ha.rows("binary_sensor.radar_spot_z0001_v_we1022", s.day0)
        self.assertGreater(a, b)
        self.assertLessEqual(b, 4)                                             # only availability and the initial value
        self.assertEqual(len(s.rig.stamped("radar_zones/zone/z0001/we1022/occupancy")), 3)   # start-up, second pass, resume


if __name__ == "__main__":
    unittest.main()
