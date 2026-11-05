"""P6 zone evaluator (spec 4, 5; checks P6-1..P6-4, P6-14, P6-15 at unit level). Owner: CORE.

Most tests drive ``ZoneEval`` directly with fake track objects (``Tr``); the learner-level tests at the end feed real
frames through ``Learner``.
"""
import unittest

from radar_zone_learner.core.grids import GridGeom
from radar_zone_learner.core.p6state import Zone6
from radar_zone_learner.core.params import DEFAULT_P6, DEFAULT_PARAMS, P6Params
from radar_zone_learner.core.types import FixedOffsetTime, ZoneEvent, ZoneStates
from radar_zone_learner.core.zoneeval import ZoneEval

GEOM = GridGeom(DEFAULT_PARAMS)
NX = DEFAULT_PARAMS.nx


def block(x0, y0, w=3, h=3):
    """A cell mask of w x h cells whose lower-left cell contains (x0, y0)."""
    i0, j0 = GEOM.cell_of(x0, y0) % NX, GEOM.cell_of(x0, y0) // NX
    return frozenset((j0 + j) * NX + (i0 + i) for j in range(h) for i in range(w))


ZONE = block(-0.1, 2.9)           # x -0.2..0.4, y 2.8..3.4 (cells), area with the 25 cm margin: x -0.45..0.65
IN = (0.1, 3.1)                    # well inside
EDGE_OUT = (0.9, 3.1)              # outside the area (0.25 m beyond the 25 cm margin)
FAR = (3.0, 5.0)


class Tr:
    def __init__(self, tid, x, y, vx=0.0, vy=0.0, powerup=False, confirmed=True):
        self.id = tid
        self.rx = x
        self.ry = y
        self.vx = vx
        self.vy = vy
        self.last_dev = 0
        self.confirmed = confirmed
        self.powerup = powerup


def make(kind="dwell", cells=ZONE, z6=None, doors=(), p6=DEFAULT_P6, zid="z0001"):
    z6 = z6 or Zone6()
    ze = ZoneEval(p6, GEOM, FixedOffsetTime(0), z6)
    ze.sync([(zid, kind, cells)], list(doors))
    return ze


WALL0 = 1767571200000                 # Monday 2026-01-05 00:00 UTC


def resume(ze, t0=0, settle=True, wall=WALL0):
    """An observed batch starts at device time t0; with ``settle`` the zones become valid."""
    ze.begin_batch(t0, t0)
    ze.on_time(t0, wall + t0)
    if settle:
        ze.on_time(t0 + ze.settle_ms + 100, wall + t0 + ze.settle_ms + 100)


def det(ze, t, trs):
    """One frame at device time t with the given tracks detected."""
    for tr in trs:
        tr.last_dev = t
    ze.on_frame(t, trs)


def run(ze, tr, t0, n, pos, dt=100, vx=0.0, vy=0.0):
    """n detections of ``tr`` at ``pos`` from device time t0; returns the next time."""
    t = t0
    for _ in range(n):
        tr.rx, tr.ry = pos
        tr.vx, tr.vy = vx, vy
        det(ze, t, [tr])
        t += dt
    return t


def events(ze):
    return ze.drain()


class EnterTest(unittest.TestCase):
    def test_enter_on_the_third_counted_detection(self):
        ze = make()
        resume(ze, 0)
        events(ze)
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 2, IN)
        self.assertEqual(events(ze), [])
        run(ze, tr, t, 1, IN)
        ev = events(ze)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0].id, ev[0].kind, ev[0].via, ev[0].t_dev_ms, ev[0].track),
                         ("z0001", "enter", "", 10_200, 1))
        st = ze.states().zones[0]
        self.assertEqual((st.occ, st.count, st.held), (True, 1, False))

    def test_two_detections_are_not_enough(self):
        ze = make()
        resume(ze, 0)
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 2, IN)
        run(ze, tr, t, 5, FAR)
        self.assertEqual(events(ze), [])
        self.assertFalse(ze.states().zones[0].occ)

    def test_unconfirmed_track_is_not_evaluated(self):
        ze = make()
        resume(ze, 0)
        tr = Tr(1, *IN, confirmed=False)
        run(ze, tr, 10_000, 10, IN)
        self.assertEqual(events(ze), [])

    def test_three_of_ten_with_gaps(self):
        ze = make()
        resume(ze, 0)
        tr = Tr(1, *IN)
        t = 10_000
        for pos in (IN, FAR, IN, FAR, FAR, IN):      # outside detections keep a ring only while it has inside ones
            t = run(ze, tr, t, 1, pos)
        ev = events(ze)
        self.assertEqual([e.kind for e in ev], ["enter"])
        self.assertEqual(ev[0].t_dev_ms, 10_500)

    def test_margin_ring_counts(self):
        ze = make()
        resume(ze, 0)
        tr = Tr(1, 0.55, 3.1)                         # 15 cm outside the mask, inside the 25 cm margin
        run(ze, tr, 10_000, 3, (0.55, 3.1))
        self.assertEqual([e.kind for e in events(ze)], ["enter"])
        ze2 = make()
        resume(ze2, 0)
        tr = Tr(2, 0.75, 3.1)                         # 35 cm outside: beyond the margin
        run(ze2, tr, 10_000, 5, (0.75, 3.1))
        self.assertEqual(events(ze2), [])

    def test_negative_margin_shrinks_the_area(self):
        z6 = Zone6()
        z6.tune["z0001"] = {"mg": -10}
        ze = make(z6=z6)
        resume(ze, 0)
        tr = Tr(1, -0.15, 3.1)                        # inside the mask but 5 cm from its edge: out of the shrunk area
        run(ze, tr, 10_000, 5, (-0.15, 3.1))
        self.assertEqual(events(ze), [])
        run(ze, tr, 11_000, 3, IN)
        self.assertEqual([e.kind for e in events(ze)], ["enter"])

    def test_pass_speed_not_counted_in_dwell_but_counted_in_short_task(self):
        for kind, expect in (("dwell", []), ("bed", []), ("manual", []), ("short_task", ["enter"])):
            ze = make(kind)
            resume(ze, 0)
            tr = Tr(1, *IN)
            run(ze, tr, 10_000, 6, IN, vx=0.8)
            self.assertEqual([e.kind for e in events(ze)], expect, kind)
        ze = make("dwell")
        resume(ze, 0)
        tr = Tr(1, *IN)
        run(ze, tr, 10_000, 3, IN, vx=0.79)
        self.assertEqual([e.kind for e in events(ze)], ["enter"])

    def test_member_moving_fast_still_leaves_at_the_edge(self):
        ze = make("dwell")
        resume(ze, 0)
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 3, IN)
        events(ze)
        t = run(ze, tr, t, 2, IN, vx=1.2)
        run(ze, tr, t, 1, EDGE_OUT, vx=1.2)
        ev = events(ze)
        self.assertEqual([(e.kind, e.via) for e in ev], [("leave", "edge")])

    def test_kind_not_evaluated_is_skipped(self):
        ze = make("reflection")
        self.assertEqual(ze.zones, {})
        self.assertEqual(ze.counters["zones_skipped_kind"], 1)


class LeaveTest(unittest.TestCase):
    def test_leave_at_the_edge_is_immediate(self):
        ze = make()
        resume(ze, 0)
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 3, IN)
        events(ze)
        t = run(ze, tr, t, 3, IN)
        run(ze, tr, t, 1, EDGE_OUT)
        ev = events(ze)
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual((e.kind, e.via, e.t_dev_ms, e.track), ("leave", "edge", t, 1))
        self.assertEqual(e.dwell_s, (t - 10_200) // 1000)
        self.assertFalse(ze.states().zones[0].occ)

    def test_a_track_re_enters_through_the_same_rule(self):
        ze = make()
        resume(ze, 0)
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 3, IN)
        t = run(ze, tr, t, 1, EDGE_OUT)
        events(ze)
        t = run(ze, tr, t, 2, IN)
        self.assertEqual(events(ze), [])
        run(ze, tr, t, 1, IN)
        self.assertEqual([e.kind for e in events(ze)], ["enter"])


class HoldTest(unittest.TestCase):
    def occupied(self, kind="dwell", **kw):
        ze = make(kind, **kw)
        resume(ze, 0)
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 5, IN)
        events(ze)
        return ze, tr, t - 100        # last inside detection time

    def test_vanish_inside_is_held_until_the_default_delay(self):
        ze, tr, last = self.occupied("dwell")                 # default 300 s
        ze.on_time(last + 2_500, 0)
        self.assertEqual(events(ze), [])
        st = ze.states().zones[0]
        self.assertEqual((st.occ, st.count, st.held), (True, 0, True))
        ze.on_time(last + 299_000, 0)
        self.assertEqual(events(ze), [])
        ze.on_time(last + 300_500, 0)
        ev = events(ze)
        self.assertEqual([(e.kind, e.via, e.t_dev_ms) for e in ev], [("leave", "hold_expired", last + 300_000)])
        self.assertFalse(ze.states().zones[0].occ)

    def test_tune_override_wins(self):
        z6 = Zone6()
        z6.tune["z0001"] = {"cd": 30}
        ze, tr, last = self.occupied(z6=z6)
        self.assertEqual(ze.states().zones[0].delay_src, "manual")
        ze.on_time(last + 29_900, 0)
        self.assertEqual(events(ze), [])
        ze.on_time(last + 30_100, 0)
        self.assertEqual([e.via for e in events(ze)], ["hold_expired"])

    def test_return_before_the_deadline_records_a_spell_and_no_event(self):
        ze, tr, last = self.occupied("short_task")           # 60 s
        ze.on_time(last + 20_000, 0)
        tr2 = Tr(2, *IN)
        run(ze, tr2, last + 20_100, 3, IN)
        self.assertEqual(events(ze), [])
        st = ze.states().zones[0]
        self.assertEqual((st.occ, st.count, st.held), (True, 1, False))
        sp = ze.z6.delay["z0001"]["sp"]
        self.assertEqual(sp, [20])

    def test_late_return_is_a_spell_but_still_an_enter(self):
        ze, tr, last = self.occupied("short_task")           # 60 s, late window 120 s
        ze.on_time(last + 61_000, 0)
        self.assertEqual([e.via for e in events(ze)], ["hold_expired"])
        tr2 = Tr(2, *IN)
        run(ze, tr2, last + 90_000, 3, IN)
        self.assertEqual([e.kind for e in events(ze)], ["enter"])
        self.assertEqual(ze.z6.delay["z0001"]["sp"], [90])

    def test_spell_beyond_twice_the_delay_is_not_recorded(self):
        ze, tr, last = self.occupied("short_task")
        ze.on_time(last + 61_000, 0)
        events(ze)
        tr2 = Tr(2, *IN)
        run(ze, tr2, last + 130_000, 3, IN)
        self.assertEqual(ze.z6.delay.get("z0001", {"sp": []})["sp"], [])

    def test_a_hold_deadline_is_judged_on_batch_time_not_on_ticks(self):
        """An idle room: a heartbeat batch every 5 s, a tick every second. A tick cannot tell a lost batch from a late
        one, so the leave comes with the first batch whose device time has passed the deadline (at most one heartbeat late)."""
        ze, tr, last = self.occupied("short_task")           # 60 s
        wall = 1_000_000
        dev = last + 100
        got = []
        for sec in range(1, 75):
            if sec % 5 == 3:                                 # batches at 3, 8, ..., 58, 63
                ze.on_time(dev + sec * 1000, wall + sec * 1000)
            ze.on_tick(wall + sec * 1000)
            ev = events(ze)
            if ev:
                got.append((sec, ev[0]))
        self.assertEqual(len(got), 1)
        sec, e = got[0]
        self.assertEqual((e.via, e.t_dev_ms), ("hold_expired", last + 60_000))     # t_dev is the computed deadline
        self.assertEqual(sec, 63)                                                  # the first batch after the deadline
        # ticks alone never expire a hold
        ze2, tr2, last2 = self.occupied("short_task")
        for sec in range(1, 14):
            ze2.on_tick(wall + sec * 1000)
        ze2.on_time(last2 + 100, wall)
        for sec in range(1, 14):
            ze2.on_tick(wall + sec * 1000)
        self.assertEqual(events(ze2), [])

    def test_a_tick_reclassifies_a_vanished_member_but_never_ends_a_hold(self):
        ze, tr, last = self.occupied("short_task")
        wall = 1_000_000
        ze.on_time(last + 100, wall)
        ze.on_tick(wall + 1_000)                              # 1.1 s since the last detection: still counted
        self.assertEqual(ze.states().zones[0].count, 1)
        ze.on_tick(wall + 3_000)                              # 3.1 s: vanished, the zone is held with count 0
        st = ze.states().zones[0]
        self.assertEqual((st.occ, st.count, st.held), (True, 0, True))
        for sec in range(4, 14):
            ze.on_tick(wall + sec * 1000)
        self.assertEqual(events(ze), [])

    def test_a_deadline_inside_a_lost_batch_does_not_fire(self):
        ze, tr, last = self.occupied("short_task")           # deadline last + 60 s
        wall = 2_000_000
        ze.on_time(last + 100, wall)
        # the batches covering last+55 .. last+65 are lost; the next one arrives 12 s later and shows the gap
        for sec in range(1, 12):
            ze.on_tick(wall + sec * 1000)
            self.assertEqual(events(ze), [])
        ze.on_censor("GAP", last + 55_000)
        ze.begin_batch(last + 66_000, last + 66_000)
        ze.on_time(last + 70_000, wall + 70_000)
        self.assertEqual(events(ze), [])                     # the deadline moved by the 11 s gap
        ze.on_time(last + 71_500, wall + 71_500)
        self.assertEqual([(e.via, e.t_dev_ms) for e in events(ze)], [("hold_expired", last + 60_000 + 11_000)])

    def test_a_leaving_track_does_not_clear_another_ones_hold(self):
        ze = make("short_task")
        resume(ze, 0)
        a = Tr(1, *IN)
        b = Tr(2, 0.3, 3.2)
        t = 10_000
        for _ in range(4):
            a.rx, a.ry = IN
            b.rx, b.ry = 0.3, 3.2
            det(ze, t, [a, b])
            t += 100
        events(ze)
        self.assertEqual(ze.states().zones[0].count, 2)
        last_a = t - 100
        # a vanishes; b stays until it walks out at the edge: the zone is still held for a
        for _ in range(30):
            b.rx, b.ry = 0.3, 3.2
            det(ze, t, [b])
            t += 100
        self.assertEqual(ze.states().zones[0].count, 1)
        b.rx, b.ry = EDGE_OUT
        det(ze, t, [b])
        self.assertEqual(events(ze), [])
        st = ze.states().zones[0]
        self.assertEqual((st.occ, st.held), (True, True))
        ze.on_time(last_a + 61_000, 0)
        self.assertEqual([e.via for e in events(ze)], ["hold_expired"])

    def test_vanish_of_one_while_another_stays_does_not_clear(self):
        ze = make("short_task")
        resume(ze, 0)
        a = Tr(1, *IN)
        b = Tr(2, 0.3, 3.2)
        t = 10_000
        for _ in range(4):
            det(ze, t, [a, b])
            t += 100
        events(ze)
        for _ in range(900):                       # 90 s: only b is detected
            det(ze, t, [b])
            t += 100
        self.assertEqual(events(ze), [])
        st = ze.states().zones[0]
        self.assertEqual((st.occ, st.count, st.held), (True, 1, False))


class DoorTest(unittest.TestCase):
    DOOR = block(0.4, 3.1, 2, 2)           # x 0.4..0.8, y 3.0..3.4: right next to the zone

    @staticmethod
    def exit_walk(ze, tr, t0):
        """A moving death: 2.5 s along a line ending at (0.6, 3.3) in the door neighbourhood, 0.43 m/s."""
        t = t0
        for k in range(26):
            tr.rx, tr.ry = -0.3 + 0.9 * k / 25, 2.7 + 0.6 * k / 25
            det(ze, t, [tr])
            t += 100
        return t - 100

    def test_moving_death_in_a_door_clears_after_three_seconds(self):
        ze = make("short_task", doors=[self.DOOR])
        resume(ze, 0)
        tr = Tr(1, -0.3, 2.7)
        last = self.exit_walk(ze, tr, 10_000)
        self.assertEqual([e.kind for e in events(ze)], ["enter"])
        ze.on_time(last + 2_900, 0)
        self.assertEqual(events(ze), [])
        ze.on_time(last + 3_100, 0)
        ev = events(ze)
        self.assertEqual([(e.via, e.t_dev_ms) for e in ev], [("door_death", last + 3_000)])

    def test_a_moving_death_through_a_door_is_also_cleared_by_a_tick(self):
        """The person has left: a tick may end this hold (a lost batch changes nothing), unlike a stayer's hold."""
        ze = make("short_task", doors=[self.DOOR])
        resume(ze, 0)
        tr = Tr(1, -0.3, 2.7)
        last = self.exit_walk(ze, tr, 10_000)
        events(ze)
        wall = 5_000_000
        ze.on_time(last + 100, wall)
        ze.on_tick(wall + 2_000)
        self.assertEqual(events(ze), [])
        ze.on_tick(wall + 3_200)
        self.assertEqual([(e.via, e.t_dev_ms) for e in events(ze)], [("door_death", last + 3_000)])

    def test_without_a_door_there_is_no_fast_clear(self):
        ze = make("short_task", doors=[])
        resume(ze, 0)
        tr = Tr(1, -0.3, 2.7)
        last = self.exit_walk(ze, tr, 10_000)
        events(ze)
        ze.on_time(last + 3_100, 0)
        self.assertEqual(events(ze), [])
        ze.on_time(last + 60_500, 0)
        self.assertEqual([e.via for e in events(ze)], ["hold_expired"])

    def test_a_sitter_near_the_door_is_not_a_moving_death(self):
        ze = make("short_task", doors=[self.DOOR])
        resume(ze, 0)
        tr = Tr(1, 0.55, 3.15)
        t = run(ze, tr, 10_000, 40, (0.55, 3.15))
        last = t - 100
        events(ze)
        ze.on_time(last + 3_500, 0)
        self.assertEqual(events(ze), [])
        ze.on_time(last + 60_500, 0)
        self.assertEqual([e.via for e in events(ze)], ["hold_expired"])

    def test_a_dwell_default_delay_is_kept_for_a_walker_away_from_the_door(self):
        ze = make("short_task", doors=[block(2.0, 3.1, 2, 2)])
        resume(ze, 0)
        tr = Tr(1, -0.3, 2.7)
        last = self.exit_walk(ze, tr, 10_000)
        events(ze)
        ze.on_time(last + 3_500, 0)
        self.assertEqual(events(ze), [])


class PowerAndGapTest(unittest.TestCase):
    def occupied(self):
        ze = make("short_task")
        resume(ze, 0)
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 5, IN)
        events(ze)
        return ze, tr, t

    def test_power_suspends_everything_without_an_event(self):
        ze, tr, t = self.occupied()
        ze.on_censor("POWER", t)
        self.assertEqual(events(ze), [])
        st = ze.states()
        self.assertFalse(st.valid)
        self.assertEqual((st.zones[0].occ, st.zones[0].count), (False, 0))
        ze.on_time(t + 600_000, 0)                          # a long time passes: nothing
        self.assertEqual(events(ze), [])

    def test_resume_is_silent_and_valid_after_the_settle_time(self):
        ze, tr, t = self.occupied()
        ze.on_censor("POWER", t)
        t0 = t + 3_600_000
        ze.begin_batch(t0, t0)
        self.assertFalse(ze.states().valid)
        p = Tr(7, *IN, powerup=True)
        t1 = run(ze, p, t0 + 100, 5, IN)
        self.assertEqual(events(ze), [])
        st = ze.states()
        self.assertEqual((st.zones[0].occ, st.zones[0].count), (True, 1))
        self.assertFalse(st.valid)
        ze.on_time(t0 + ze.settle_ms + 1, 0)
        self.assertTrue(ze.states().valid)
        ze.on_time(t1 + 1000, 0)
        self.assertEqual(events(ze), [])

    def test_a_walk_in_after_the_power_up_window_is_an_enter(self):
        ze, tr, t = self.occupied()
        ze.on_censor("POWER", t)
        t0 = t + 1000
        ze.begin_batch(t0, t0)
        q = Tr(8, *IN, powerup=False)
        run(ze, q, t0 + 5000, 4, IN)
        self.assertEqual([e.kind for e in events(ze)], ["enter"])

    def test_gap_keeps_the_occupancy_and_suspends_the_deadline(self):
        ze, tr, t = self.occupied()                          # short_task: 60 s
        last = t - 100
        ze.on_time(t, 0)
        ze.on_censor("GAP", t)
        self.assertEqual(events(ze), [])
        st = ze.states()
        self.assertTrue(st.valid)
        self.assertEqual((st.zones[0].occ, st.zones[0].count, st.zones[0].held), (True, 0, True))
        ze.on_tick(10_000)                                   # a tick does not advance a pending gap
        self.assertEqual(events(ze), [])
        self.assertTrue(ze.states().valid)
        ze.begin_batch(t + 10_000, t + 10_000)               # the chain restarts 10 s later (a GAP, below 15 s)
        ze.on_time(t + 10_000, 0)
        self.assertEqual(events(ze), [])
        # the deadline is last + 60 s + 10 s of gap
        ze.on_time(last + 69_000, 0)
        self.assertEqual(events(ze), [])
        ze.on_time(last + 71_000, 0)
        ev = events(ze)
        self.assertEqual([(e.via, e.t_dev_ms) for e in ev], [("hold_expired", last + 70_000)])

    def test_a_pending_gap_that_lasts_as_long_as_the_silence_rule_is_a_power_loss(self):
        ze, tr, t = self.occupied()
        ze.on_time(t, 0)
        ze.on_censor("GAP", t)
        ze.on_tick(14_000)
        self.assertTrue(ze.states().valid)
        ze.on_tick(15_500)                                   # D42: POWER wins, no event, zones invalid
        st = ze.states()
        self.assertEqual((st.valid, st.zones[0].occ), (False, False))
        self.assertEqual(events(ze), [])
        # the chain restarts long after: resumed as from POWER, silent for a track of the power-up window
        t0 = t + 7_200_000
        ze.begin_batch(t0, t0)
        self.assertFalse(ze.states().valid)

    def test_a_gap_longer_than_the_silence_rule_seen_at_resume_is_power(self):
        ze, tr, t = self.occupied()
        ze.on_censor("GAP", t)
        t0 = t + 20_000
        ze.begin_batch(t0, t0)
        st = ze.states()
        self.assertEqual((st.valid, st.zones[0].occ), (False, False))
        self.assertEqual(events(ze), [])

    def test_a_real_enter_after_a_gap_restart_is_an_event(self):
        """Review blocker: Track.powerup is a lifetime flag; only the power-up settle window may be silent."""
        ze = make("short_task")
        resume(ze, 0)                                        # valid
        ze.on_censor("GAP", 9_000)
        ze.begin_batch(10_000, 10_000)
        tr = Tr(5, *IN, powerup=True)                        # born within 3 s of the restarted chain
        run(ze, tr, 10_100, 4, IN)
        self.assertEqual([e.kind for e in events(ze)], ["enter"])

    def test_a_return_after_a_gap_records_no_hidden_spell(self):
        ze, tr, t = self.occupied()
        ze.on_censor("GAP", t)
        t0 = t + 1_200
        ze.begin_batch(t0, t0)
        p = Tr(9, *IN, powerup=True)
        run(ze, p, t0 + 100, 5, IN)
        self.assertEqual(ze.z6.delay.get("z0001", {"sp": []})["sp"], [])
        # a real vanish still records its spell
        last = t0 + 600
        ze.on_time(last + 20_000, 0)
        q = Tr(10, *IN)
        run(ze, q, last + 20_100, 3, IN)
        self.assertEqual(ze.z6.delay["z0001"]["sp"], [20])

    def test_gap_then_the_person_is_seen_again_is_silent(self):
        ze, tr, t = self.occupied()
        ze.on_censor("GAP", t)
        t0 = t + 5_000
        ze.begin_batch(t0, t0)
        p = Tr(9, *IN, powerup=True)
        run(ze, p, t0 + 100, 5, IN)
        self.assertEqual(events(ze), [])
        st = ze.states().zones[0]
        self.assertEqual((st.occ, st.count, st.held), (True, 1, False))

    def test_a_new_evaluator_starts_invalid(self):
        ze = make()
        self.assertFalse(ze.states().valid)
        ze.begin_batch(1000, 1000)
        self.assertFalse(ze.states().valid)


class CountTest(unittest.TestCase):
    def test_count_up_to_three(self):
        ze = make("bed")
        resume(ze, 0)
        trs = [Tr(i, 0.0 + 0.1 * i, 3.1) for i in range(1, 5)]
        t = 10_000
        for _ in range(4):
            det(ze, t, trs)
            t += 100
        self.assertEqual(ze.states().zones[0].count, 3)
        events(ze)
        self.assertTrue(ze.states().zones[0].occ)


class LdrEarlyTest(unittest.TestCase):
    def setup_zone(self, ldr=True):
        z6 = Zone6()
        if ldr:
            z6.set_ldr(["z0001"])
        ze = make("dwell", z6=z6)
        resume(ze, 0)
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 5, IN)
        events(ze)
        return ze, t - 100

    def test_early_clear_after_sixty_seconds_of_absence(self):
        ze, last = self.setup_zone()
        t = last + 5_000
        ze.on_ld(t, 0, True)
        ze.on_time(t, 0)
        self.assertEqual(events(ze), [])
        ze.on_ld(t + 30_000, 0, True)
        self.assertEqual(events(ze), [])
        ze.on_ld(t + 61_000, 0, True)
        ev = events(ze)
        self.assertEqual([(e.via, e.t_dev_ms) for e in ev], [("early", t + 60_000)])

    def test_a_present_ld2412_resets_the_window_and_never_lengthens(self):
        ze, last = self.setup_zone()
        t = last + 5_000
        ze.on_time(t, 0)
        ze.on_ld(t, 0, True)
        ze.on_ld(t + 40_000, 1, True)
        ze.on_ld(t + 45_000, 0, True)
        ze.on_ld(t + 100_000, 0, True)
        self.assertEqual(events(ze), [])
        ze.on_ld(t + 106_000, 0, True)
        self.assertEqual([e.via for e in events(ze)], ["early"])
        # a PRESENT LD2412 does not extend the delay: the hold still ends at last + 300 s
        ze2, last2 = self.setup_zone()
        for k in range(1, 70):
            ze2.on_time(last2 + k * 5_000, 0)
            ze2.on_ld(last2 + k * 5_000, 1, True)
        ze2.on_time(last2 + 300_500, 0)
        self.assertEqual([e.via for e in events(ze2)], ["hold_expired"])

    def test_not_reliable_is_never_early_cleared(self):
        ze, last = self.setup_zone(ldr=False)
        for k in range(1, 55):
            ze.on_time(last + k * 5_000, 0)
            ze.on_ld(last + k * 5_000, 0, True)
        self.assertEqual(events(ze), [])

    def test_a_track_anywhere_blocks_the_early_clear(self):
        ze, last = self.setup_zone()
        t = last + 5_000
        other = Tr(5, *FAR)
        for k in range(0, 80):
            det(ze, t + k * 1000, [other])
            ze.on_time(t + k * 1000, 0)
            ze.on_ld(t + k * 1000, 0, True)
        self.assertEqual(events(ze), [])

    def test_ld2412_off_gives_no_early_clear(self):
        ze, last = self.setup_zone()
        for k in range(1, 55):
            ze.on_time(last + k * 5_000, 0)
            ze.on_ld(last + k * 5_000, 0, False)
        self.assertEqual(events(ze), [])

    def test_evidence_accumulates_while_a_track_is_inside(self):
        ze = make("dwell")
        resume(ze, 0)
        tr = Tr(1, *IN)
        run(ze, tr, 10_000, 5, IN)
        t = 10_000
        for k in range(1, 21):
            ze.on_ld(t + k * 500, 1 if k % 5 else 0, True)
        ze.flush_ld()
        on_s, known_s = ze.z6.ld["z0001"]
        self.assertEqual(known_s, 9)                         # 19 intervals of 0.5 s = 9.5 s, 9 whole seconds
        self.assertLessEqual(on_s, known_s)
        self.assertGreater(on_s, 5)


class SignatureTest(unittest.TestCase):
    def test_states_are_emitted_only_when_they_change(self):
        ze = make()
        first = ze.take_states()
        self.assertIsInstance(first, ZoneStates)
        self.assertFalse(first.valid)
        self.assertIsNone(ze.take_states())
        resume(ze, 0)
        st = ze.take_states()
        self.assertTrue(st.valid)
        self.assertIsNone(ze.take_states())
        ze.on_time(20_000, 0)
        self.assertIsNone(ze.take_states())

    def test_no_zone_never_emits(self):
        ze = ZoneEval(DEFAULT_P6, GEOM, FixedOffsetTime(0), Zone6())
        ze.sync([], [])
        self.assertIsNone(ze.take_states())
        ze.begin_batch(0, 0)
        ze.on_time(10_000, 0)
        self.assertIsNone(ze.take_states())
        self.assertEqual(ze.drain(), [])

    def test_removing_the_last_zone_emits_an_empty_state_once(self):
        ze = make()
        ze.take_states()
        ze.sync([], [])
        st = ze.take_states()
        self.assertEqual(st.zones, ())
        self.assertIsNone(ze.take_states())

    def test_variant_now_follows_local_time(self):
        z6 = Zone6()
        z6.info["z0001"] = {"th": {"wd": ["09-17"]}, "var": [{"d": "wd", "s": 9, "e": 17, "n": 12},
                                                              {"d": "we", "s": 20, "e": 23, "n": 8}], "conf": 0.8}
        ze = make(z6=z6)
        mon_10 = 1767571200000 + 10 * 3600_000             # Monday 2026-01-05 10:00 UTC
        ze.on_tick(mon_10)
        st = ze.states().zones[0]
        self.assertEqual(st.variant, "wd0917")
        self.assertEqual(st.variants, ("wd0917", "we2023"))
        self.assertEqual((st.typical_hours, st.confidence), ("wd 09-17", 0.8))
        ze.on_tick(1767571200000 + 18 * 3600_000)          # Monday 18:00: no variant
        self.assertEqual(ze.states().zones[0].variant, "")
        sat_21 = 1767571200000 + (5 * 24 + 21) * 3600_000
        ze.on_tick(sat_21)
        self.assertEqual(ze.states().zones[0].variant, "we2023")

    def test_events_carry_the_variant(self):
        z6 = Zone6()
        z6.info["z0001"] = {"th": {}, "var": [{"d": "wd", "s": 9, "e": 17, "n": 12}], "conf": 0.5}
        ze = make(z6=z6)
        resume(ze, 0, wall=WALL0 + 10 * 3600_000)           # Monday 10:00
        tr = Tr(1, *IN)
        run(ze, tr, 10_000, 3, IN)
        ev = events(ze)
        self.assertEqual((ev[0].kind, ev[0].variant), ("enter", "wd0917"))


class DebugTest(unittest.TestCase):
    def test_enter_debug_records_frames_not_counted(self):
        ze = make("dwell")
        resume(ze, 0)
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 1, IN)
        t = run(ze, tr, t, 1, IN, vx=1.0)                    # not counted (pass speed)
        t = run(ze, tr, t, 1, IN)
        run(ze, tr, t, 1, IN)
        d = ze.debug()
        self.assertEqual(len(d), 1)
        zid, track, first, decisive, k = d[0]
        self.assertEqual((zid, track, first, decisive, k), ("z0001", 1, 10_000, 10_300, 1))


if __name__ == "__main__":
    unittest.main()
