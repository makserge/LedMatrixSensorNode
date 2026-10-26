"""P4: night evidence (onsets, wakes, activity bins) and the night-window proposal (OA 7.5, 7.8)."""
import json
import random
import unittest
from datetime import date

from radar_zone_learner.core import Learner
from radar_zone_learner.core import night as N
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import (ContextState, FixedOffsetTime, LuxSample, NodeStatus, ScheduleState,
                                           StateError)

from .test_extraction import BASE_MS, BASE_S, DAY, make
from .test_learner import Feeder, sit_fn, start, walk_fn


D0 = date(2026, 1, 5).toordinal()
H = 3600000
M = 60000
BED = (-2.3, 4.9)
FAR = (1.0, 1.0)
CHAIR = (1.5, 2.0)


def at(day, h, m=0, s=0):
    return BASE_S + day * DAY + h * 3600 + m * 60 + s


def lux(L, day, h, m, v):
    L.handle(LuxSample(v, BASE_MS + day * DAY * 1000 + h * H + m * M))


def to_bed(f, rng, a_ms, pos=BED, src=FAR, lie_s=60, present_until=None):
    """Walk from src to pos (6 s, ending at a_ms), sit there lie_s seconds, then be hidden (a sleeper the radar
    drops). The LD2412 sees the person from the walk-in on."""
    f.present.append((a_ms - 6000, present_until if present_until is not None else a_ms + 10 * H))
    f.idle_until(a_ms - 6000)
    f.targets_until(a_ms, walk_fn(src, pos, a_ms - 6000, a_ms))
    f.targets_until(a_ms + lie_s * 1000, sit_fn(rng, pos))


def evening(day, h=22, m=50):
    return day * DAY * 1000 + h * H + m * M


class AxisTest(unittest.TestCase):
    def test_night_axis(self):
        tm = FixedOffsetTime(0)
        for (h, m, d, nm) in ((23, 30, 0, 690), (12, 0, 0, 0), (19, 59, 0, 479), (0, 7, -1, 727), (6, 10, -1, 1090),
                              (11, 59, -1, 1439)):
            night, got = N.night_axis(tm, BASE_MS + (h * 60 + m) * 60000)
            self.assertEqual((night - D0, got), (d, nm), (h, m))
        self.assertEqual(N.minute_of_day(690), 1410)
        self.assertEqual(N.minute_of_day(727), 7)
        self.assertEqual(N.minute_of_day(1090), 370)

    def test_percentile_linear(self):
        v = [10, 20, 30, 40, 50]
        self.assertEqual(N.percentile(v, 0.0), 10)
        self.assertEqual(N.percentile(v, 0.5), 30)
        self.assertEqual(N.percentile(v, 0.9), 46.0)
        self.assertEqual(N.percentile(v, 0.1), 14.0)
        self.assertEqual(N.percentile([7], 0.9), 7)

    def test_local_epoch_and_window_len(self):
        tm = FixedOffsetTime(3600)
        ms = N.local_epoch_ms(tm, D0 + 3, 6 * 60, BASE_MS)
        lt = tm.local(ms)
        self.assertEqual((lt.date_ord, lt.sec_of_day), (D0 + 3, 6 * 3600))
        self.assertEqual(N.window_len(0, 360), 360)
        self.assertEqual(N.window_len(1410, 405), 435)
        self.assertEqual(N.window_len(60, 60), 0)
        self.assertEqual(N.circ_diff(1430, 10), 20)


class LuxAndContextTest(unittest.TestCase):
    def setUp(self):
        self.L = make()

    def step(self, day, h, m):
        self.L.night_m.step(self.L, BASE_MS + day * DAY * 1000 + h * H + m * M)

    def test_lights_off_after_15_min_dark(self):
        L = self.L
        lux(L, 0, 21, 10, 40.0)
        lux(L, 0, 22, 0, 2.0)                           # dark from 22:00
        self.step(0, 22, 14)
        self.assertEqual(L.night_m.onsets["lux"], {})
        self.step(0, 22, 15)
        self.assertEqual(L.night_m.onsets["lux"], {D0: 600})                           # 22:00
        lux(L, 0, 22, 30, 1.0)                           # still dark: nothing new
        self.step(0, 23, 0)
        self.assertEqual(L.night_m.counters["onsets_lux"], 1)

    def test_before_20_00_counts_from_20_00(self):
        L = self.L
        lux(L, 0, 18, 30, 0.5)                           # dark winter evening, no lights on yet
        self.step(0, 19, 59)
        self.assertEqual(L.night_m.onsets["lux"], {})
        self.step(0, 20, 14)
        self.assertEqual(L.night_m.onsets["lux"], {})
        self.step(0, 20, 15)
        self.assertEqual(L.night_m.onsets["lux"], {D0: 480})      # 20:00

    def test_only_the_first_time_per_night_and_bright_resets(self):
        L = self.L
        lux(L, 0, 20, 30, 1.0)
        self.step(0, 20, 50)                             # 20:30 is the first lights-off
        lux(L, 0, 21, 0, 80.0)                           # lights on again
        lux(L, 0, 23, 0, 1.0)
        self.step(0, 23, 20)
        self.assertEqual(L.night_m.onsets["lux"], {D0: 510})
        self.assertEqual(L.night_m.counters["onsets_lux"], 1)

    def test_after_midnight_belongs_to_the_evening_before(self):
        L = self.L
        lux(L, 1, 0, 7, 0.5)                             # lights off at 00:07 on day 1
        self.step(1, 0, 30)
        self.assertEqual(L.night_m.onsets["lux"], {D0: 725})                 # night D0, 00:05

    def test_daytime_and_early_morning_dark_is_no_onset(self):
        L = self.L
        lux(L, 1, 9, 0, 0.5)
        self.step(1, 9, 30)
        lux(L, 1, 5, 0, 0.5)
        self.step(1, 5, 30)
        self.assertEqual(L.night_m.onsets["lux"], {})

    def test_bad_lux_ignored(self):
        nm = self.L.night_m
        for v in (float("nan"), -1.0, True, "x"):
            nm.on_lux(v, BASE_MS)
        self.assertIsNone(nm.lux)
        self.assertIsNone(nm.dark_since_ms)

    def test_context_edge_gives_an_onset_first_only(self):
        L = self.L
        L.handle(ContextState("lights_off", False, BASE_MS + 21 * H))
        L.handle(ContextState("lights_off", True, BASE_MS + 23 * H))
        L.handle(ContextState("sleep_hint", False, BASE_MS + 23 * H + M))
        L.handle(ContextState("sleep_hint", True, BASE_MS + 23 * H + 5 * M))
        self.assertEqual(L.night_m.onsets["context"], {D0: 23 * 60 - 720})       # lights_off first: 23:00
        self.assertEqual(L.night_m.counters["onsets_context"], 1)

    def test_first_message_is_a_baseline_not_an_edge(self):
        L = self.L
        L.handle(ContextState("lights_off", True, BASE_MS + 23 * H))             # a retained value after a restart
        self.assertEqual(L.night_m.onsets["context"], {})
        L.handle(ContextState("lights_off", None, BASE_MS + 23 * H + M))
        L.handle(ContextState("lights_off", True, BASE_MS + 23 * H + 2 * M))
        self.assertEqual(L.night_m.onsets["context"], {})                         # None -> True: no known 'was off'
        L.handle(ContextState("lights_off", False, BASE_MS + 23 * H + 3 * M))
        L.handle(ContextState("lights_off", True, BASE_MS + 23 * H + 4 * M))
        self.assertEqual(L.night_m.onsets["context"], {D0: 23 * 60 - 720})

    def test_guests_context_is_stored_and_changes_nothing(self):
        L = self.L
        L.handle(ContextState("guests", True, BASE_MS + 1000))
        self.assertIs(L.night_m.ctx["guests"], True)
        self.assertEqual(L.night_m.onsets, {"radar": {}, "context": {}, "lux": {}})


class RadarOnsetTest(unittest.TestCase):
    def setup(self, dark=True):
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        f = Feeder(L)
        if dark:
            lux(L, 0, 22, 40, 1.0)
        return L, f

    def test_onset_when_still_and_dark_for_15_min(self):
        L, f = self.setup()
        rng = random.Random(1)
        to_bed(f, rng, evening(0))
        f.idle_until(evening(0) + 14 * M)
        self.assertEqual(L.night_m.onsets["radar"], {})                       # 15 quiet minutes are not over yet
        f.idle_until(evening(0) + 16 * M)
        self.assertEqual(L.night_m.onsets["radar"], {D0: 22 * 60 + 50 - 720})  # 22:50
        self.assertEqual(L.night_m.counters["onsets_radar"], 1)

    def test_not_while_lux_is_bright(self):
        L, f = self.setup(dark=False)
        lux(L, 0, 22, 0, 120.0)
        rng = random.Random(2)
        to_bed(f, rng, evening(0))
        f.idle_until(evening(0) + 20 * M)
        self.assertEqual(L.night_m.onsets["radar"], {})

    def test_lights_off_context_replaces_lux(self):
        L, f = self.setup(dark=False)
        L.handle(ContextState("lights_off", False, BASE_MS + 22 * H))
        L.handle(ContextState("lights_off", True, BASE_MS + 22 * H + 45 * M))
        rng = random.Random(3)
        to_bed(f, rng, evening(0))
        f.idle_until(evening(0) + 20 * M)
        self.assertEqual(len(L.night_m.onsets["radar"]), 1)

    def test_not_when_the_ld2412_does_not_see_anyone(self):
        L, f = self.setup()
        rng = random.Random(4)
        to_bed(f, rng, evening(0), present_until=evening(0) + 2 * M)         # the person leaves the room
        f.idle_until(evening(0) + 20 * M)
        self.assertEqual(L.night_m.onsets["radar"], {})

    def test_not_when_the_last_track_died_moving(self):
        L, f = self.setup()
        a = evening(0)
        f.present.append((a - 6000, a + 10 * H))
        f.idle_until(a - 6000)
        f.targets_until(a + 6000, walk_fn(BED, FAR, a - 6000, a + 6000))    # walks out through the door
        f.idle_until(a + 20 * M)
        self.assertEqual(L.night_m.last_death[3], "moving")
        self.assertEqual(L.night_m.onsets["radar"], {})

    def test_with_a_bed_zone_the_rest_position_must_be_in_it(self):
        L, f = self.setup()
        L.label_list.append({"id": "l0001", "created_s": BASE_S, "x": BED[0], "y": BED[1], "name": "Bed",
                             "kind": "bed", "cells": L.geom.cells_within(BED[0], BED[1], 0.4)})
        rng = random.Random(5)
        to_bed(f, rng, evening(0), pos=CHAIR, src=FAR)                       # still, but on the chair
        f.idle_until(evening(0) + 20 * M)
        self.assertEqual(L.night_m.onsets["radar"], {})
        to_bed(f, rng, evening(0) + 30 * M, pos=BED, src=CHAIR)             # now in the bed
        f.idle_until(evening(0) + 55 * M)
        self.assertEqual(list(L.night_m.onsets["radar"].values()), [22 * 60 + 80 - 720])   # 23:20

    def test_a_later_quiet_start_replaces_the_earlier_one(self):
        L, f = self.setup()
        rng = random.Random(6)
        to_bed(f, rng, evening(0))
        f.idle_until(evening(0) + 20 * M)
        self.assertEqual(list(L.night_m.onsets["radar"].values()), [650])
        to_bed(f, rng, evening(0) + 40 * M, src=FAR)                         # up and back to bed at 23:30
        f.idle_until(evening(0) + 60 * M)
        self.assertEqual(list(L.night_m.onsets["radar"].values()), [22 * 60 + 90 - 720])
        self.assertEqual(L.night_m.counters["onsets_radar"], 2)

    def test_a_trip_in_the_night_does_not_move_the_bedtime(self):
        L, f = self.setup()
        rng = random.Random(9)
        to_bed(f, rng, evening(0))                                           # in bed 22:50
        f.idle_until(evening(0) + 20 * M)
        self.assertEqual(list(L.night_m.onsets["radar"].values()), [650])
        a = evening(0) + 4 * H + 10 * M                                      # up at 03:00 (a bathroom trip) ...
        to_bed(f, rng, a, src=FAR)                                           # ... and back in bed
        f.idle_until(a + 20 * M)
        self.assertEqual(list(L.night_m.onsets["radar"].values()), [650])    # still 22:50, not 03:00
        self.assertEqual(L.night_m.counters["onsets_radar"], 1)

    def test_a_quiet_start_more_than_60_min_after_the_first_does_not_replace_it(self):
        L, f = self.setup()
        rng = random.Random(10)
        to_bed(f, rng, evening(0))                                           # 22:50
        f.idle_until(evening(0) + 20 * M)
        a = evening(0) + 70 * M                                              # back in bed at 00:00: 70 min later
        to_bed(f, rng, a, src=FAR)
        f.idle_until(a + 20 * M)
        self.assertEqual(list(L.night_m.onsets["radar"].values()), [650])
        b = evening(1)                                                       # the next night: 22:50, then 23:40 (50 min)
        to_bed(f, rng, b, src=FAR)
        f.idle_until(b + 20 * M)
        to_bed(f, rng, b + 50 * M, src=FAR)
        f.idle_until(b + 70 * M)
        self.assertEqual(sorted(L.night_m.onsets["radar"].values()), [650, 700])

    def test_several_trips_do_not_chain_away_from_the_first_onset(self):
        L, f = self.setup()
        rng = random.Random(11)
        to_bed(f, rng, evening(0))                                           # 22:50
        f.idle_until(evening(0) + 20 * M)
        for k in (1, 2, 3):                                                  # back in bed after 50, 100 and 150 min
            a = evening(0) + 50 * k * M
            to_bed(f, rng, a, src=FAR)
            f.idle_until(a + 20 * M)
        self.assertEqual(list(L.night_m.onsets["radar"].values()), [700])    # 23:40: the 50 min one; not 01:20

    def test_not_outside_the_evening_hours(self):
        L, f = self.setup()
        lux(L, 0, 13, 0, 0.5)
        rng = random.Random(7)
        to_bed(f, rng, 13 * H + 10 * M)                                      # a nap at 13:10 in a dark room
        f.idle_until(13 * H + 40 * M)
        self.assertEqual(L.night_m.onsets["radar"], {})

    def test_censoring_inside_the_quiet_time_blocks_it(self):
        L, f = self.setup()
        rng = random.Random(8)
        to_bed(f, rng, evening(0))
        f.idle_until(evening(0) + 8 * M)
        L.handle(NodeStatus(False, BASE_MS + evening(0) + 8 * M + 500))
        L.handle(NodeStatus(True, BASE_MS + evening(0) + 8 * M + 900))
        f.idle_until(evening(0) + 20 * M)
        self.assertEqual(L.night_m.onsets["radar"], {})


class WakeTest(unittest.TestCase):
    """A wake is the first activity of the morning that is followed by more than 10 min of activity: the room is
    not at rest again within 10 min (a walking point, or the LD2412 shows nobody)."""

    def setUp(self):
        self.L = make()
        self.nm = self.L.night_m

    def walk(self, *times):
        for t in times:
            self.nm.note_walk(t, self.L.obs)

    def ld(self, a, b, present):
        self.L.obs.ld.add(int(a * 1000), int(b * 1000), present)

    def judge(self, t):
        self.nm.step(self.L, int(t * 1000))

    def test_the_person_gets_up_and_leaves(self):
        self.ld(at(1, 6, 0), at(1, 6, 52, 20), True)                    # lying in bed since the window ended
        self.ld(at(1, 6, 52, 20), at(1, 7, 30), False)                  # then the room is empty
        self.walk(at(1, 6, 52))
        self.judge(at(1, 7, 1))
        self.assertEqual(self.nm.wakes, {})                             # 10 min have not passed yet
        self.judge(at(1, 7, 2))
        self.assertEqual(self.nm.wakes, {D0: 6 * 60 + 50 + 720})        # the first activity 06:52, floored to 06:50
        self.assertEqual(self.nm.counters["wakes"], 1)
        self.judge(at(1, 7, 20))
        self.assertEqual(self.nm.counters["wakes"], 1)

    def test_still_moving_about_after_10_min(self):
        self.ld(at(1, 6, 0), at(1, 8, 0), True)                         # somebody is in the room all the time
        self.walk(at(1, 6, 52), at(1, 6, 55), at(1, 6, 59))
        self.judge(at(1, 7, 3))
        self.assertEqual(self.nm.wakes, {D0: 1130})

    def test_back_in_bed_is_a_trip_not_a_wake(self):
        self.ld(at(1, 6, 0), at(1, 8, 0, 20), True)
        self.ld(at(1, 8, 0, 20), at(1, 9, 0), False)
        self.walk(at(1, 6, 20))                                         # to the bathroom and back, lying still
        self.judge(at(1, 6, 31))
        self.assertEqual(self.nm.wakes, {})
        self.walk(at(1, 8, 0))                                          # 100 min later: up for good (room empties)
        self.judge(at(1, 8, 11))
        self.assertEqual(self.nm.wakes, {D0: 8 * 60 + 720})

    def test_a_walking_point_soon_after_another_is_no_new_first_activity(self):
        self.ld(at(1, 6, 0), at(1, 7, 10), True)
        self.walk(at(1, 6, 20))
        self.judge(at(1, 6, 31))                                        # a trip
        self.walk(at(1, 6, 40))                                         # only 20 min after the last one: not a first
        self.ld(at(1, 6, 40, 20), at(1, 7, 10), False)
        self.judge(at(1, 6, 52))
        self.assertEqual(self.nm.wakes, {})
        self.walk(at(1, 7, 15))                                         # 35 min after: a first activity again
        self.ld(at(1, 7, 15, 20), at(1, 8, 0), False)
        self.judge(at(1, 7, 26))
        self.assertEqual(self.nm.wakes, {D0: 7 * 60 + 15 + 720})

    def test_unobserved_time_counts_as_rest(self):
        self.ld(at(1, 6, 0), at(1, 6, 20, 20), True)
        self.ld(at(1, 6, 20, 20), at(1, 7, 0), False)
        self.walk(at(1, 6, 0))
        self.judge(at(1, 6, 11))                                        # lying still afterwards: a trip
        self.assertEqual(self.nm.wakes, {})
        self.L.obs.censor_walls.append(at(1, 6, 10) * 1000)             # the radar was off for a while
        self.walk(at(1, 6, 20))                                         # 20 min later, but not observed in between
        self.judge(at(1, 6, 31))
        self.assertEqual(self.nm.wakes, {D0: 6 * 60 + 20 + 720})

    def test_censoring_inside_the_10_min_gives_no_verdict(self):
        self.ld(at(1, 6, 52, 20), at(1, 8, 30), False)
        self.walk(at(1, 6, 52))
        self.L.obs.censor_walls.append(at(1, 6, 57) * 1000)
        self.judge(at(1, 7, 3))
        self.assertEqual(self.nm.wakes, {})
        self.walk(at(1, 8, 0))                                          # a first activity again, after a censoring
        self.judge(at(1, 8, 11))
        self.assertEqual(self.nm.wakes, {D0: 8 * 60 + 720})

    def test_an_unknown_ld2412_is_not_rest(self):
        self.walk(at(1, 6, 52))
        self.judge(at(1, 7, 3))
        self.assertEqual(self.nm.wakes, {D0: 1130})

    def test_only_the_first_wake_of_a_night_and_each_night_on_its_own(self):
        self.walk(at(1, 6, 50))
        self.judge(at(1, 7, 1))
        self.walk(at(1, 9, 0))
        self.judge(at(1, 9, 11))
        self.assertEqual(self.nm.wakes, {D0: 1130})
        self.walk(at(2, 6, 30))
        self.judge(at(2, 6, 41))
        self.assertEqual(self.nm.wakes, {D0: 1130, D0 + 1: 6 * 60 + 30 + 720})

    def test_first_activity_before_03_00_or_after_noon_is_no_candidate(self):
        self.walk(at(1, 2, 0))
        self.judge(at(1, 2, 11))
        self.walk(at(1, 12, 30))
        self.judge(at(1, 12, 41))
        self.assertEqual(self.nm.wakes, {})
        self.assertIsNone(self.nm.wake_cand)

    def test_out_of_order_samples_are_tolerated(self):
        self.walk(at(1, 6, 52), at(1, 6, 58), at(1, 6, 57))
        self.judge(at(1, 7, 3))
        self.assertEqual(self.nm.wakes, {D0: 1130})

    def test_activity_bins(self):
        self.walk(at(0, 23, 47, 10))
        self.assertEqual(self.nm.act[D0], 1 << ((23 * 60 + 47 - 720) // 5))
        self.walk(at(1, 0, 3))                                             # after midnight: the same night
        self.assertEqual(self.nm.act[D0].bit_count(), 2)


def fill(L, onsets=None, wakes=None, src="radar"):
    nm = L.night_m
    nm.onsets[src] = {D0 + i: v for i, v in enumerate(onsets or [])}
    nm.wakes = {D0 + i: v for i, v in enumerate(wakes or [])}
    nm.refresh(L)
    return nm.raw


class ProposalTest(unittest.TestCase):
    ON = [690, 700, 675, 710, 705, 695, 685, 700]          # about 23:15 .. 23:50
    WK = [1100, 1120, 1090, 1130, 1110, 1100, 1115, 1105]    # about 06:10 .. 06:50

    def test_start_p90_up_and_end_p10_down_to_15_min(self):
        L = make()
        raw = fill(L, self.ON, self.WK)
        on = sorted(self.ON)
        wk = sorted(self.WK)
        p90 = N.percentile(on, 0.9)
        p10 = N.percentile(wk, 0.1)
        self.assertEqual(p90, 706.5)
        self.assertEqual(p10, 1097.0)
        self.assertEqual(raw["start_min"], 0)               # 706.5 rounded up to 720 = 00:00
        self.assertEqual(raw["end_min"], 375)               # 1097 rounded down to 1095 = 06:15
        self.assertEqual((raw["basis"], raw["from_lights_off"]), ("radar", False))
        self.assertEqual((raw["onsets_n"], raw["wakes_n"]), (8, 8))
        self.assertEqual(raw["len_min"], 375)
        self.assertEqual(raw["saving_pct"], 26.0)
        self.assertEqual(L.night_m.proposal_rev, 1)

    def test_a_window_that_starts_before_midnight(self):
        L = make()
        raw = fill(L, [660, 665, 670, 675, 650, 655, 660, 670], [1115, 1120, 1125, 1130, 1135, 1140, 1145, 1150])
        # P90 of the onsets 671.5 -> 675 = 23:15; P10 of the wakes 1118.5 -> 1110 = 06:30
        self.assertEqual((raw["start_min"], raw["end_min"]), (23 * 60 + 15, 6 * 60 + 30))
        self.assertEqual((raw["len_min"], raw["saving_pct"]), (435, 30.2))
        self.assertEqual((raw["iqr_onset_min"], raw["iqr_wake_min"]), (11.2, 17.5))

    def test_needs_7_nights_with_each_edge(self):
        L = make()
        self.assertIsNone(fill(L, self.ON[:6], self.WK))
        self.assertIsNone(fill(L, self.ON, self.WK[:6]))
        self.assertIsNotNone(fill(L, self.ON[:7], self.WK[:7]))

    def test_iqr_must_be_below_90_min(self):
        L = make()
        wide = [600, 600, 600, 700, 700, 800, 800, 800]                      # IQR 125
        self.assertIsNone(fill(L, wide, self.WK))
        self.assertIsNone(fill(L, self.ON, [900, 900, 1000, 1100, 1200, 1300, 1300, 1400]))
        edge = [650, 650, 650, 650, 740, 740, 740, 740]                     # IQR exactly 90: refused
        self.assertIsNone(fill(L, edge, self.WK))
        self.assertIsNotNone(fill(L, [650, 650, 650, 660, 720, 730, 740, 740], self.WK))     # IQR 82.5

    def test_lights_off_times_are_the_fallback_and_are_labelled(self):
        L = make()
        L.night_m.onsets["radar"] = {D0 + i: v for i, v in enumerate(self.ON[:6])}      # too few radar onsets
        raw = fill(L, self.ON, self.WK, src="lux")
        self.assertEqual((raw["basis"], raw["from_lights_off"]), ("lux", True))
        L.night_m.onsets["context"] = {D0 + i: v for i, v in enumerate(self.ON)}
        L.night_m.refresh(L)
        self.assertEqual(L.night_m.raw["basis"], "context")                  # context before lux
        L.night_m.onsets["radar"] = {D0 + i: v for i, v in enumerate(self.ON)}
        L.night_m.refresh(L)
        self.assertEqual(L.night_m.raw["basis"], "radar")                    # radar before both

    def test_a_night_the_radar_was_blind_counts_through_its_lights_off_time(self):
        L = make()
        nm = L.night_m
        for k in range(14):
            late = k % 7 in (2, 5)                                           # 4 nights: asleep after 00:45, radar off
            nm.onsets["lux"][D0 + k] = 765 if late else 690
            if not late:
                nm.onsets["radar"][D0 + k] = 690
            nm.wakes[D0 + k] = 1100
        nm.refresh(L)
        raw = nm.raw
        self.assertEqual((raw["onsets_n"], raw["start_min"], raw["end_min"]), (14, 45, 375))   # 00:45, not 23:30
        self.assertEqual((raw["basis"], raw["from_lights_off"]), ("lux", True))

    def test_lights_off_nights_move_the_label_only_by_more_than_15_min(self):
        L = make()
        nm = L.night_m
        for k in range(12):
            nm.onsets["radar" if k < 10 else "lux"][D0 + k] = 690
            nm.wakes[D0 + k] = 1100
        nm.refresh(L)
        self.assertEqual((nm.raw["basis"], nm.raw["from_lights_off"], nm.raw["onsets_n"]), ("radar", False, 12))
        self.assertEqual(nm.raw["start_min"], 23 * 60 + 30)
        for v, label in ((700, False), (715, True)):                         # the 2 lux-only nights: start 23:45 / 00:00
            nm.onsets["lux"] = {D0 + 10: v, D0 + 11: v}
            nm.refresh(L)
            self.assertEqual(nm.raw["from_lights_off"], label, v)
        self.assertEqual((nm.raw["start_min"], nm.raw["basis"]), (0, "lux"))

    def test_the_7_nights_are_counted_on_the_merged_series(self):
        L = make()
        nm = L.night_m
        for k in range(7):
            nm.onsets["radar" if k < 4 else "context"][D0 + k] = 690 + k
            nm.wakes[D0 + k] = 1100
        nm.refresh(L)
        self.assertEqual((nm.raw["basis"], nm.raw["from_lights_off"], nm.raw["onsets_n"]), ("context", True, 7))
        del nm.onsets["context"][D0 + 6]
        nm.refresh(L)
        self.assertIsNone(nm.raw)                                            # 6 nights with an onset

    def test_a_window_that_is_too_short_or_too_long_is_no_proposal(self):
        L = make()
        self.assertIsNone(fill(L, [690] * 8, [720] * 8))                     # 23:30-00:00: 30 min
        self.assertIsNone(fill(L, [300] * 8, [1300] * 8))                    # 17:00-09:40: 16 h 40 min
        self.assertIsNotNone(fill(L, [690] * 8, [750] * 8))                  # 23:30-00:30: 60 min is allowed

    def test_rev_changes_with_the_content_only(self):
        L = make()
        fill(L, self.ON, self.WK)
        self.assertEqual(L.night_m.proposal_rev, 1)
        L.night_m.refresh(L)
        self.assertEqual(L.night_m.proposal_rev, 1)
        fill(L, self.ON, [1200] * 8)
        self.assertEqual(L.night_m.proposal_rev, 2)
        fill(L, self.ON[:3], self.WK)                                         # no proposal now
        self.assertIsNone(L.night_m.raw)
        self.assertEqual(L.night_m.proposal_rev, 2)
        fill(L, self.ON, [1200] * 8)                                          # the same one comes back: new revision
        self.assertEqual(L.night_m.proposal_rev, 3)

    def test_an_unchanged_proposal_keeps_its_revision_over_restarts(self):
        """The revision means 'the content changed': a restart must not bump it (answers and notifications carry it)."""
        L = make()
        fill(L, self.ON, self.WK)
        fill(L, self.ON, [1200] * 8)
        self.assertEqual(L.night_m.proposal_rev, 2)
        st = json.loads(json.dumps(L.export_state()))
        for _ in range(3):
            L = Learner(P, FixedOffsetTime(0), st)
            L.night_m.refresh(L)
            L.night_m.step(L, BASE_MS + 20 * DAY * 1000)
            self.assertEqual(L.night_m.proposal_rev, 2)
            st = json.loads(json.dumps(L.export_state()))
            self.assertEqual(st["night"]["proposal_rev"], 2)
        fill(L, self.ON, [1250] * 8)                                          # a real change still counts
        self.assertEqual(L.night_m.proposal_rev, 3)

    def test_retention_is_28_nights(self):
        L = make()
        nm = L.night_m
        nm.onsets["lux"] = {D0 + i: 700 for i in range(40)}
        nm.wakes = {D0 + i: 1100 for i in range(40)}
        nm.act = {D0 + i: 1 for i in range(40)}
        nm.prune(BASE_MS + 40 * DAY * 1000 + 13 * H)                        # night D0 + 40 starts at noon of day 40
        self.assertEqual((min(nm.onsets["lux"]), max(nm.onsets["lux"])), (D0 + 13, D0 + 39))
        self.assertEqual(len(nm.wakes), 27)

    def test_activity_bins_are_kept_for_15_nights(self):
        L = make()
        nm = L.night_m
        nm.act = {D0 + i: 1 for i in range(40)}
        nm.prune(BASE_MS + 40 * DAY * 1000 + 13 * H)                        # the night D0 + 40 has just started
        self.assertEqual((min(nm.act), max(nm.act)), (D0 + 25, D0 + 39))
        self.assertEqual(len(nm.act), 15)

    def test_late_nights(self):
        L = make()
        nm = L.night_m
        # window start 00:00: the bins of 23:30-23:55 of the night
        mask_bin = (23 * 60 + 40 - 720) // 5
        for i in (1, 3, 5):
            nm.act[D0 + i] = 1 << mask_bin
        nm.act[D0 + 4] = 1 << ((23 * 60 + 10 - 720) // 5)                    # too early: not in the last 30 min
        now = BASE_MS + 10 * DAY * 1000 + 8 * H                              # morning of day 10: nights up to D0 + 9
        n, of = nm.late_nights(0, now, None)
        self.assertEqual((n, of), (3, 14))
        n, of = nm.late_nights(0, now, D0 + 2)                              # a learner created on night D0 + 2
        self.assertEqual((n, of), (2, 8))

    def test_export_load_roundtrip_and_validation(self):
        L = make()
        fill(L, self.ON, self.WK)
        L.night_m.act[D0] = (1 << 140) | 1
        d = json.loads(json.dumps(L.night_m.export()))
        L2 = make()
        N.validate(d, P)
        L2.night_m.load(d)
        self.assertEqual(L2.night_m.onsets, L.night_m.onsets)
        self.assertEqual(L2.night_m.wakes, L.night_m.wakes)
        self.assertEqual(L2.night_m.act, L.night_m.act)
        self.assertEqual(L2.night_m.proposal_rev, 1)
        for mut in (lambda x: x.update(night_lux=-1),
                    lambda x: x["onsets"].append({"n": "2026-01-05", "m": 5, "s": "x"}),
                    lambda x: x["wakes"].append({"n": "2026-01-05", "m": 5, "s": "lux"}),
                    lambda x: x["onsets"].append({"n": "nope", "m": 5, "s": "lux"}),
                    lambda x: x["onsets"].append({"n": "2026-01-05", "m": 1440, "s": "lux"}),
                    lambda x: x["act"].update({"2026-01-05": "abc"}), lambda x: x.update(proposal_rev=-1),
                    lambda x: x["act"].update({"2026-01-05": "z" * 72})):
            bad = json.loads(json.dumps(d))
            mut(bad)
            with self.assertRaises(StateError):
                N.validate(bad, P)


DAY_MS = DAY * 1000
ONSETS = [1385, 1390, 1395, 1400, 1388, 1392, 1398, 1386, 1391]      # minutes after midnight: 23:05-23:20
WAKES = [380, 385, 390, 395, 383, 388, 392, 381, 386]                # 06:20-06:35 on the next morning


class NineNightsTest(unittest.TestCase):
    """Radar frames on a schedule of 00:00-06:00 (no frames inside it): onsets and wakes are seen, the proposal
    comes, the review shows it, and the learner never sends anything to the node."""

    LEAVES = False                 # True: the person walks out of the room a few seconds after waking

    @classmethod
    def setUpClass(cls):
        from radar_zone_learner.core.types import NodeCommand, ReviewChanged
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        L.handle(ScheduleState(True, 0, 360, BASE_MS + 1))
        f = Feeder(L)
        rng = random.Random(21)
        cls.review_changes = []
        for d in range(9):
            t0 = d * DAY_MS
            L.handle(LuxSample(80.0, BASE_MS + t0 + 20 * H))                          # the lights are on
            L.handle(LuxSample(1.0, BASE_MS + t0 + 22 * H + 30 * M))                  # dark from 22:30
            to_bed(f, rng, t0 + ONSETS[d] * M, present_until=(t0 + 24 * H) if cls.LEAVES else None)
            f.idle_until(t0 + 23 * H + 50 * M)
            f.next_ms = (d + 1) * DAY_MS + 6 * H                                      # the radars are off until 06:00
            w = (d + 1) * DAY_MS + WAKES[d] * M
            f.idle_until(w)
            if cls.LEAVES:
                f.present.append(((d + 1) * DAY_MS + 6 * H, w + 10000))             # asleep until it ends
                f.targets_until(w + 6000, walk_fn(BED, FAR, w, w + 6000))              # gets up, leaves at once
            else:
                f.present.append((w - 6000, w + 25 * M))
                for k in range(4):                                                     # 15 min of moving about
                    a = w + k * 5 * M
                    f.targets_until(a + 6000, walk_fn(BED if k % 2 == 0 else CHAIR, CHAIR if k % 2 == 0 else BED,
                                                      a, a + 6000))
                    f.idle_until(a + 5 * M)
            f.idle_until((d + 1) * DAY_MS + 8 * H)
        cls.L = L
        cls.outs = f.outs
        cls.NodeCommand = NodeCommand
        cls.review_changes = [o for o in f.outs if isinstance(o, ReviewChanged)]

    def test_onsets_and_wakes_are_seen_on_every_night(self):
        nm = self.L.night_m
        self.assertEqual(len(nm.onsets["radar"]), 9)
        self.assertEqual(len(nm.wakes), 9)
        self.assertEqual(len(nm.onsets["lux"]), 9)
        for i in range(9):
            night = D0 + i
            self.assertLess(abs(nm.onsets["radar"][night] - (ONSETS[i] - 720 + 1440) % 1440), 6, i)
            self.assertLess(abs(nm.wakes[night] - (WAKES[i] + 720)), 6, i)
            self.assertEqual(nm.onsets["lux"][night], 22 * 60 + 30 - 720)

    def test_the_proposal_and_the_item(self):
        raw = self.L.night_m.raw
        self.assertEqual((raw["basis"], raw["onsets_n"], raw["wakes_n"]), ("radar", 9, 9))
        self.assertTrue(23 * 60 + 15 <= raw["start_min"] <= 23 * 60 + 45, raw)
        self.assertTrue(6 * 60 <= raw["end_min"] <= 6 * 60 + 30, raw)
        self.assertEqual(self.L.review.nw_state(self.L), "proposed")
        self.assertTrue(self.review_changes)
        last = self.review_changes[-1]
        self.assertEqual([i.id for i in last.items], ["night_window", "bed_needs_data"])
        d = last.items[0].data
        self.assertFalse(d["from_lights_off"])
        self.assertEqual(d["current_saving_pct"], 25.0)

    def test_nothing_is_sent_to_the_node(self):
        self.assertEqual([o for o in self.outs if isinstance(o, self.NodeCommand)], [])
        self.assertEqual(self.L.status().attributes["night_learning"]["active"], False)


class NineNightsLeavingTest(NineNightsTest):
    """The same, with the simulator's kind of morning: the person walks out of the room at once. The LD2412 then
    shows nobody, so the first activity counts as the wake."""

    LEAVES = True


if __name__ == "__main__":
    unittest.main()
