"""Mutation-catching tests for the P6 core and shell (from the P6 review, scratchpad/p6/review/catch_tests*.py): rules
that the first suite left untested (two holds at once, spells after a fast or early clear, the 50-spell cap, tune over
learned, variant windows at the hour edges, the exact area edge, sync after a modify, the Learner wiring of LD2412
evidence, doors, gaps, power, status counters, the shell second pass while the zones are invalid). Owner: CORE (shell
case at the end)."""
import json
import unittest

from radar_zone_learner.core import Learner, zoneinfo
from radar_zone_learner.core.p6state import Zone6
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import (FixedOffsetTime, FramesMessage, ZoneEvent, ZoneForget, ZoneStates, ZoneTune)
from tests.p6_harness import LayerRig, zone_view
from tests.test_learner import BASE_MS, Feeder, start
from tests.test_p6_learner import CELLS, OUT, POS, events, sit, states, zone_learner
from tests.test_zoneeval import (EDGE_OUT, FAR, IN, WALL0, DoorTest, Tr, det, make, resume, run)
from tests.test_zoneeval import events as ze_events


class HoldCatch(unittest.TestCase):
    def two_vanished(self):
        ze = make("short_task")
        resume(ze, 0)
        a = Tr(1, *IN)
        b = Tr(2, 0.3, 3.2)
        t = 10_000
        for _ in range(4):
            det(ze, t, [a, b])
            t += 100
        last_a = t - 100
        for _ in range(100):                          # 10 s with only b: a vanishes
            det(ze, t, [b])
            t += 100
        last_b = t - 100
        ze_events(ze)
        return ze, last_a, last_b

    def test_second_hold_still_pending(self):          # Z6 (also Z43)
        ze, last_a, last_b = self.two_vanished()
        ze.on_time(last_a + 61_000, 0)                 # a's deadline passed, b's (10 s later) not yet
        self.assertEqual(ze_events(ze), [])
        self.assertTrue(ze.states().zones[0].occ)
        ze.on_time(last_b + 61_000, 0)
        self.assertEqual([(e.via, e.t_dev_ms) for e in ze_events(ze)], [("hold_expired", last_b + 60_000)])

    def test_leave_time_is_the_latest_deadline(self):  # Z7
        ze, last_a, last_b = self.two_vanished()
        ze.on_time(last_b + 61_000, 0)
        self.assertEqual([e.t_dev_ms for e in ze_events(ze)], [last_b + 60_000])

    def test_door_death_of_one_does_not_clear_a_sitter(self):   # spec 5.5 'other tracks keep theirs'
        door = DoorTest.DOOR
        ze = make("short_task", doors=[door])
        resume(ze, 0)
        sitter = Tr(2, 0.0, 3.0)
        walker = Tr(1, -0.3, 2.7)
        t = 10_000
        for k in range(26):
            walker.rx, walker.ry = -0.3 + 0.9 * k / 25, 2.7 + 0.6 * k / 25
            sitter.rx, sitter.ry = 0.0, 3.0
            det(ze, t, [walker, sitter])
            t += 100
        last_w = t - 100
        # the sitter is detected until the walker has vanished; then it vanishes too, 20 s later
        for _ in range(200):
            det(ze, t, [sitter])
            t += 100
        last_s = t - 100
        ze_events(ze)
        ze.on_time(last_w + 3_500, 0)                  # the walker's fast clear (3 s) is due, the sitter's hold is not
        self.assertEqual(ze_events(ze), [])
        self.assertTrue(ze.states().zones[0].occ)

    def test_first_of_two_leaves_at_the_edge_zone_stays(self):  # Z44, P6-14 (2)
        ze = make("short_task")
        resume(ze, 0)
        a = Tr(1, *IN)
        b = Tr(2, 0.3, 3.2)
        t = 10_000
        for _ in range(4):
            det(ze, t, [a, b])
            t += 100
        ze_events(ze)
        a.rx, a.ry = EDGE_OUT
        det(ze, t, [a, b])
        self.assertEqual(ze_events(ze), [])
        st = ze.states().zones[0]
        self.assertEqual((st.occ, st.count, st.held), (True, 1, False))
        t += 100
        b.rx, b.ry = EDGE_OUT
        det(ze, t, [b])
        self.assertEqual([(e.kind, e.via) for e in ze_events(ze)], [("leave", "edge")])


class SpellCatch(unittest.TestCase):
    def test_no_spell_after_a_fast_clear(self):         # Z8
        ze = make("short_task", doors=[DoorTest.DOOR])
        resume(ze, 0)
        tr = Tr(1, -0.3, 2.7)
        last = DoorTest.exit_walk(ze, tr, 10_000)
        ze_events(ze)
        ze.on_time(last + 3_100, 0)
        self.assertEqual([e.via for e in ze_events(ze)], ["door_death"])
        run(ze, Tr(2, *IN), last + 10_000, 3, IN)       # someone sits down 10 s later (inside 2 x 60 s)
        self.assertEqual(ze.z6.delay.get("z0001", {"sp": []})["sp"], [])

    def test_no_spell_after_an_early_clear(self):       # Z9
        from tests.test_zoneeval import LdrEarlyTest
        helper = LdrEarlyTest()
        ze, last = helper.setup_zone()
        t = last + 5_000
        ze.on_time(t, 0)
        ze.on_ld(t, 0, True)
        ze.on_ld(t + 61_000, 0, True)
        self.assertEqual([e.via for e in ze_events(ze)], ["early"])
        run(ze, Tr(2, *IN), t + 80_000, 3, IN)
        self.assertEqual(ze.z6.delay.get("z0001", {"sp": []})["sp"], [])

    def test_only_the_last_fifty_spells_are_kept(self):  # Z22
        ze = make("short_task")
        resume(ze, 0)
        ze.z6.delay["z0001"] = {"sp": list(range(1, 51))}
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 5, IN)
        last = t - 100
        ze.on_time(last + 20_000, 0)
        run(ze, Tr(2, *IN), last + 20_100, 3, IN)
        sp = ze.z6.delay["z0001"]["sp"]
        self.assertEqual((len(sp), sp[0], sp[-1]), (50, 2, 20))
        # and the state still loads (spec 6.2: sp longer than 50 is a StateError)
        Zone6.load(ze.z6.export())


class DelayCatch(unittest.TestCase):
    def test_tune_wins_over_learned(self):               # Z11, P6-7
        L = zone_learner()
        L.zone_states()
        L.zone6.delay["l0001"] = {"sp": [40] * 12}
        zoneinfo.nightly(L, 1)
        self.assertEqual(L.zone_states().zones[0].delay_s, 40)
        L.handle(ZoneTune("l0001", "cd", 77, BASE_MS))
        s = L.zone_states().zones[0]
        self.assertEqual((s.delay_s, s.delay_src), (77, "manual"))
        L.handle(ZoneForget("l0001", BASE_MS))
        s = L.zone_states().zones[0]
        self.assertEqual((s.delay_s, s.delay_src), (300, "default"))


class VariantCatch(unittest.TestCase):
    def ze(self, var):
        z6 = Zone6()
        z6.info["z0001"] = {"th": {}, "var": var, "conf": 0.8}
        return make(z6=z6)

    def at(self, ze, hours, extra_s=0):
        ze.on_tick(WALL0 + int(hours * 3600) * 1000 + extra_s * 1000)
        return ze.states().zones[0].variant

    def test_hour_boundaries(self):                      # Z12, Z12b (P6-10: 'correct at the hour boundaries')
        ze = self.ze([{"d": "wd", "s": 9, "e": 17, "n": 12}])
        self.assertEqual(self.at(ze, 9), "wd0917")            # start hour is inside
        self.assertEqual(self.at(ze, 17), "")                 # end hour is exclusive
        self.assertEqual(self.at(ze, 8.9999 + 0.0001, -1), "")  # 08:59:59 is outside (the minute changes)

    def test_weekday_weekend_change(self):               # Z13
        ze = self.ze([{"d": "wd", "s": 0, "e": 24, "n": 12}, {"d": "we", "s": 0, "e": 24, "n": 12}])
        fri = 4 * 24
        self.assertEqual(self.at(ze, fri + 23.9), "wd0024")
        self.assertEqual(self.at(ze, fri + 24.0), "we0024")   # Saturday 00:00

    def test_wrap_window(self):                          # Z14: profiles.modes() produces (22, 2) windows
        ze = self.ze([{"d": "wd", "s": 22, "e": 2, "n": 12}])
        self.assertEqual(self.at(ze, 23), "wd2202")
        self.assertEqual(self.at(ze, 1), "wd2202")
        self.assertEqual(self.at(ze, 12), "")


class LdCatch(unittest.TestCase):
    def test_evidence_is_only_for_zones_with_a_track_inside(self):   # Z24
        from radar_zone_learner.core.p6state import Zone6 as Z
        from radar_zone_learner.core.params import DEFAULT_P6
        from radar_zone_learner.core.grids import GridGeom
        from radar_zone_learner.core.zoneeval import ZoneEval
        from tests.test_zoneeval import GEOM, ZONE, block
        ze = ZoneEval(DEFAULT_P6, GEOM, FixedOffsetTime(0), Z())
        ze.sync([("z0001", "dwell", ZONE), ("z0002", "dwell", block(2.0, 3.0))], [])
        resume(ze, 0)
        tr = Tr(1, *IN)
        run(ze, tr, 10_000, 5, IN)
        for k in range(1, 21):
            ze.on_ld(10_000 + k * 500, 1, True)
        ze.flush_ld()
        self.assertIn("z0001", ze.z6.ld)
        self.assertNotIn("z0002", ze.z6.ld)

    def test_evidence_is_not_gathered_while_the_ld2412_is_off(self):
        ze = make("dwell")
        resume(ze, 0)
        run(ze, Tr(1, *IN), 10_000, 5, IN)
        for k in range(1, 21):
            ze.on_ld(10_000 + k * 500, 1, False)
        ze.flush_ld()
        self.assertNotIn("z0001", ze.z6.ld)


class LearnerLdCatch(unittest.TestCase):
    """The Learner -> evaluator LD2412 wiring (Learner.handle passes batch.p2 and pw bit 1)."""

    def run_it(self, pw_bit1=True):
        L = zone_learner("dwell")
        f = Feeder(L)
        f.idle_until(30_000)
        f.present.append((30_000, 400_000))
        f.targets_until(36_000, lambda t: [(0.1 + 0.0 * t, 3.1)])
        f.targets_until(300_000, lambda t: [(0.1, 3.1)])
        return L

    def test_ld_evidence_reaches_zone6(self):             # L2a/L2c
        L = self.run_it()
        z = json.loads(json.dumps(L.export_state()))["p5"]["zone6"]["ld"]
        self.assertGreater(z["l0001"][1], 200)
        self.assertEqual(z["l0001"][0], z["l0001"][1])


class LearnerDoorCatch(unittest.TestCase):
    def test_door_cells_come_from_shadow_entry_suggestions_only(self):   # L11, L12
        L = zone_learner("dwell")
        L.ns.suggestions.append({"id": "s0009-1", "rev": 1, "type": "entry_zone", "kind": "entry", "name_hint": None,
                                 "state": "shadow", "first_night": 1, "last_night": 3, "cx": 0.5, "cy": 3.2,
                                 "cells": CELLS, "poly_cm": [], "conf": 0.8, "evidence": {}, "variants": [],
                                 "variant_evidence_split": False, "variant_share_split": False,
                                 "variant_suppressed": False, "frozen": False})
        L.review.touch()
        L.zone_states()
        self.assertEqual(L.zone_eval._door_cells, frozenset(CELLS))
        L.ns.suggestions[0]["state"] = "stale"
        L.review.touch()
        L.zone_states()
        self.assertEqual(L.zone_eval._door_cells, frozenset())


class StatusCatch(unittest.TestCase):
    def test_status_counters(self):                       # L8, L9, L10, L14: spec 7.2
        L = zone_learner("manual")
        f = Feeder(L)
        f.idle_until(30_000)
        f.targets_until(36_000, lambda t: [(0.1, 3.1)])
        f.targets_until(40_000, lambda t: [(0.1, 3.1)])
        attrs = L.status().attributes
        self.assertEqual((attrs["zones_live"], attrs["zones_occupied"], attrs["zone_events_24h"]), (1, 1, 1))


class GeometryCatch(unittest.TestCase):
    def enters(self, x, y, kind="dwell"):
        ze = make(kind)
        resume(ze, 0)
        run(ze, Tr(1, x, y), 10_000, 4, (x, y))
        return [e.kind for e in ze_events(ze)] == ["enter"]

    def test_margin_reaches_the_full_25_cm(self):          # Z39: mask edge x = 0.4, area edge x = 0.65
        self.assertTrue(self.enters(0.62, 3.1))             # 22 cm outside the mask: inside the 25 cm margin
        self.assertFalse(self.enters(0.68, 3.1))            # 28 cm: outside

    def test_margin_is_round_at_a_corner(self):             # Z53 (Chebyshev instead of Euclid)
        self.assertTrue(self.enters(0.55, 3.55))            # corner (0.4, 3.4): 21 cm away
        self.assertFalse(self.enters(0.60, 3.60))           # 28 cm away (20 cm in x and in y)

    def test_door_neighbourhood_is_dilated_by_the_door_radius(self):    # Z40
        door = DoorTest.DOOR
        far_door = type(door)(c + 3 for c in door)           # the same block moved 3 cells (0.6 m) to the right
        ze = make("short_task", doors=[far_door])
        resume(ze, 0)
        tr = Tr(1, -0.3, 2.7)
        t = 10_000
        for k in range(26):
            tr.rx, tr.ry = -0.3 + 0.93 * k / 25, 2.7 + 0.5 * k / 25     # ends at (0.63, 3.2): 0.37 m from the door mask
            det(ze, t, [tr])
            t += 100
        last = t - 100
        ze_events(ze)
        ze.on_time(last + 3_100, 0)
        self.assertEqual([e.via for e in ze_events(ze)], ["door_death"])


class SyncCatch(unittest.TestCase):
    def test_modify_keeps_the_runtime_state_and_takes_the_new_cells(self):    # Z55, Z56 (spec 6.3 'Modify or merge')
        from tests.test_zoneeval import block, GEOM, ZONE
        from radar_zone_learner.core.params import DEFAULT_P6
        ze = make("short_task")
        resume(ze, 0)
        tr = Tr(1, *IN)
        t = run(ze, tr, 10_000, 4, IN)
        ze_events(ze)
        bigger = block(-0.1, 2.9, 5, 3)                       # the zone is modified: two more cells to the right
        ze.sync([("z0001", "short_task", bigger)], [])
        st = ze.states().zones[0]
        self.assertEqual((st.occ, st.count), (True, 1))      # the person inside stays counted: no silent off
        run(ze, tr, t, 3, IN)
        self.assertEqual(ze_events(ze), [])                      # and no second enter
        ze2 = make("short_task")
        resume(ze2, 0)
        ze2.sync([("z0001", "short_task", bigger)], [])
        run(ze2, Tr(2, 0.9, 3.1), 10_000, 4, (0.9, 3.1))      # inside the new cells only (x 0.4..0.8 + margin)
        self.assertEqual([e.kind for e in ze_events(ze2)], ["enter"])


class LearnerPowerWinsCatch(unittest.TestCase):
    def test_a_twenty_second_hole_in_the_batches_is_power_not_gap(self):       # L4: POWER wins (D42)
        L = zone_learner("manual")
        f = Feeder(L)
        f.idle_until(30_000)
        f.targets_until(45_000, lambda t: [(0.1, 3.1)])
        f.next_ms += 20_000                                   # no batches for 20 s, no tick
        f.seq += 200
        f.targets_until(90_000, lambda t: [(0.1, 3.1)])
        sts = [o for o in f.outs if isinstance(o, ZoneStates)]
        self.assertTrue(any(not s.valid for s in sts[1:]))    # the zones went invalid (POWER), not held (GAP)
        self.assertEqual([e.kind for e in f.outs if isinstance(e, ZoneEvent)], ["enter"])


class LearnerLdOffCatch(unittest.TestCase):
    def test_no_ld_evidence_while_pw_bit1_is_clear(self):          # L2b: p2 is valid only while pw bit 1
        from radar_zone_learner.core.types import FramesMessage
        L = zone_learner("dwell")
        orig = L.handle

        def handle(ev):
            if isinstance(ev, FramesMessage):
                ev = FramesMessage(ev.payload.replace(b'"pw":3', b'"pw":1'), ev.recv_ms)
            return orig(ev)
        L.handle = handle
        f = Feeder(L)
        f.idle_until(30_000)
        f.present.append((30_000, 400_000))
        f.targets_until(300_000, lambda t: [(0.1, 3.1)])
        z = json.loads(json.dumps(L.export_state())).get("p5", {}).get("zone6", {}).get("ld", {})
        self.assertEqual(z, {})


OCC = "radar_zones/zone/z0001/occupancy"
COUNT = "radar_zones/zone/z0001/count"


class LearnerGapCatch(unittest.TestCase):
    def test_gap_with_the_person_gone_keeps_the_hold_and_shifts_the_deadline(self):     # L4/L5, spec 5.7 GAP, P6-4
        L = zone_learner("manual")
        L.handle(ZoneTune("l0001", "cd", 30, BASE_MS))
        f = Feeder(L)
        f.idle_until(30_000)
        f.targets_until(40_000, sit(POS))
        last_det = 39_900 + 120_000
        f.idle_until(45_000)
        # a 10 s hole in the stream (a lost batch burst: a GAP, shorter than the 15 s POWER rule)
        f.next_ms += 10_000
        f.seq += 100
        f.idle_until(60_000)
        for sec in range(60, 90):
            f.idle_until(sec * 1000)
            f.tick(sec * 1000)
        ev = events(f)
        self.assertEqual([(e.kind, e.via) for e in ev], [("enter", ""), ("leave", "hold_expired")])
        self.assertTrue(states(f)[-1].valid)
        # deadline = last detection + 30 s + the unobserved span (about 10 s)
        self.assertTrue(last_det + 38_000 <= ev[1].t_dev_ms <= last_det + 42_000, ev[1].t_dev_ms - last_det)

    def test_gap_with_the_person_still_there_is_silent_and_records_no_gap_sized_spell(self):
        L = zone_learner("manual")
        f = Feeder(L)
        f.idle_until(30_000)
        f.targets_until(36_000, sit(POS))
        f.targets_until(45_000, sit(POS))
        f.next_ms += 10_000
        f.seq += 100
        f.targets_until(80_000, sit(POS))
        f.idle_until(81_000)
        self.assertEqual([(e.kind, e.via) for e in events(f)], [("enter", "")])
        self.assertEqual(L.zone6.delay.get("l0001", {"sp": []})["sp"], [])


class LearnerTickCatch(unittest.TestCase):
    def test_hold_expiry_is_judged_on_batch_time_between_five_second_heartbeats(self):    # spec 5.7 D42, P6-3
        L = zone_learner("manual")
        L.handle(ZoneTune("l0001", "cd", 31, BASE_MS))
        f = Feeder(L)
        f.idle_until(30_000)
        f.targets_until(40_000, sit(POS))                  # last detection at 39.9 s
        deadline_sim = 39_900 + 31_000                     # 70.9 s
        got = None
        for sec in range(41, 90):
            if sec % 5 == 0:
                f.idle_until(sec * 1000)                   # the node's 5 s heartbeat
            n = len(events(f))
            f.tick(sec * 1000)
            if len(events(f)) > n:
                got = sec * 1000
                break
            n = len(events(f))
        ev = events(f)
        self.assertEqual([(e.kind, e.via) for e in ev], [("enter", ""), ("leave", "hold_expired")])
        self.assertEqual(ev[1].t_dev_ms - 120_000, deadline_sim)                    # t_dev is the computed deadline
        emitted = [o for o in f.outs if isinstance(o, ZoneEvent) and o.kind == "leave"]
        self.assertEqual(len(emitted), 1)
        self.assertIsNone(got)                             # a tick alone never emitted it: the heartbeat batch did


class ShellSecondPassCatch(unittest.TestCase):
    def test_nothing_but_offline_is_published_by_the_second_pass_while_the_zones_are_invalid(self):   # S3
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"))
        rig.push_states(valid=False)
        rig.burst()                                          # an HA birth while the radars are off
        rig.cleared()
        rig.run(12)                                          # the second pass, 10 s later
        self.assertEqual(rig.published(OCC), [])
        self.assertEqual(rig.published(COUNT), [])


if __name__ == "__main__":
    unittest.main()
