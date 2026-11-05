"""P6 zone evaluation through the real Learner (spec 4.2 wiring, 6, 8.5; checks P6-4, P6-11 at unit level). Owner: CORE."""
import json
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core.p6state import Zone6
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import (FixedOffsetTime, NodeStatus, SaveRequested, SettingSwitch, StateError,
                                           VariantRequest, ZoneEvent, ZoneForget, ZoneStates, ZoneTune)
from tests.test_learner import BASE_MS, Feeder, start, walk_fn
from tests.test_zoneeval import block

CELLS = block(-0.1, 2.9)
POS = (0.1, 3.1)
OUT = (2.5, 3.1)


def zone_learner(kind="manual"):
    L = Learner(P, FixedOffsetTime(0))
    start(L)
    L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": 0.1, "y": 3.1, "name": "Desk",
                         "kind": kind, "cells": CELLS})
    L.review.touch()
    return L


def sit(pos):
    return lambda t: [pos]


def events(f):
    return [o for o in f.outs if isinstance(o, ZoneEvent)]


def states(f):
    return [o for o in f.outs if isinstance(o, ZoneStates)]


class WiringTest(unittest.TestCase):
    def test_a_person_walks_in_sits_and_leaves(self):
        L = zone_learner()
        f = Feeder(L)
        f.idle_until(30_000)                                   # the radars are observed: zones become valid
        self.assertTrue(states(f)[-1].valid)
        f.targets_until(36_000, walk_fn(OUT, POS, 30_000, 36_000))
        f.targets_until(60_000, sit(POS))
        f.targets_until(66_000, walk_fn(POS, OUT, 60_000, 66_000))
        f.idle_until(70_000)
        ev = events(f)
        self.assertEqual([(e.kind, e.via) for e in ev], [("enter", ""), ("leave", "edge")])
        self.assertTrue(30_000 < ev[0].t_dev_ms - 120_000 < 38_000)     # Feeder device time = sim + 120 s
        self.assertGreater(ev[1].dwell_s, 20)
        last = states(f)[-1]
        self.assertEqual((last.valid, last.zones[0].occ, last.zones[0].count), (True, False, 0))

    def test_no_zone_no_zone_outputs(self):
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        f = Feeder(L)
        f.idle_until(30_000)
        f.targets_until(40_000, sit(POS))
        f.idle_until(50_000)
        self.assertEqual([o for o in f.outs if isinstance(o, (ZoneEvent, ZoneStates))], [])
        self.assertEqual(L.zone_states(), ZoneStates(True, L.zone_states().t_dev_ms, ()))
        self.assertEqual(L.counters().get("zones_skipped_kind", 0), 0)

    def test_the_vanished_person_is_held_and_cleared_by_the_tick(self):
        L = zone_learner("bed")                                 # bed: 600 s
        f = Feeder(L)
        f.idle_until(30_000)
        f.targets_until(60_000, sit(POS))
        f.idle_until(61_000)
        for sec in range(61, 700):
            f.idle_until(sec * 1000)
            if sec % 1 == 0:
                f.tick(sec * 1000)
            if any(e.kind == "leave" for e in events(f)):
                break
        ev = events(f)
        self.assertEqual([(e.kind, e.via) for e in ev], [("enter", ""), ("leave", "hold_expired")])
        last_det = 60_000 - 100 + 120_000
        self.assertEqual(ev[1].t_dev_ms, last_det + 600_000)
        self.assertLessEqual(sec * 1000, 60_000 + 600_000 + 3_000)

    def test_power_off_gives_no_event_and_invalid_zones(self):
        L = zone_learner()
        f = Feeder(L)
        f.idle_until(30_000)
        f.targets_until(45_000, sit(POS))
        n_before = len(events(f))
        # the node reports that the radar is off: a batch with pw bit 0 clear ends the observation
        import json as _json
        from radar_zone_learner.core.types import FramesMessage
        t_dev = 46_000 + f.dev_off
        obj = {"v": 1, "b": f.boot, "t": t_dev, "s": f.seq, "n": 0, "pw": 0, "sm": "X"}
        f.outs += L.handle(FramesMessage(_json.dumps(obj).encode(), BASE_MS + 46_000))
        self.assertEqual(len(events(f)), n_before)
        self.assertFalse(states(f)[-1].valid)
        self.assertEqual(states(f)[-1].zones[0].occ, False)

    def test_node_offline_is_power(self):
        L = zone_learner()
        f = Feeder(L)
        f.idle_until(30_000)
        f.targets_until(45_000, sit(POS))
        n_before = len(events(f))
        f.outs += L.handle(NodeStatus(False, BASE_MS + 46_000))
        self.assertEqual(len(events(f)), n_before)
        self.assertFalse(states(f)[-1].valid)

    def test_censor_class_table(self):
        L = Learner(P, FixedOffsetTime(0))
        from radar_zone_learner.core.observation import Censor, TrackFrames
        for reason in ("pw", "sm", "mt", "offline", "silence", "reboot", "something_new"):
            self.assertEqual(L.censor_class(Censor(0, 0, reason)), "POWER", reason)
        for reason in ("seq_gap", "seq_resync", "gap", "compressed", "clock_step"):
            self.assertEqual(L.censor_class(Censor(0, 0, reason)), "GAP", reason)
            near = TrackFrames([], 5_000, 4_000, 0)
            far = TrackFrames([], 25_000, 24_000, 0)
            self.assertEqual(L.censor_class(Censor(0, 0, reason), near), "GAP")
            self.assertEqual(L.censor_class(Censor(0, 0, reason), far), "POWER")      # POWER wins (D42)


class StateTest(unittest.TestCase):
    def test_a_learner_without_zone_data_exports_no_zone6(self):
        L = zone_learner()
        st = L.export_state()
        self.assertNotIn("p5", st)
        self.assertEqual(st["writer"], "radar_zone_learner 0.7.0")

    def test_tuning_round_trips(self):
        L = zone_learner()
        L.handle(ZoneTune("l0001", "cd", 120, BASE_MS))
        L.handle(ZoneTune("l0001", "mg", 40, BASE_MS))
        st = json.loads(json.dumps(L.export_state()))
        self.assertEqual(st["p5"]["zone6"]["tune"], {"l0001": {"cd": 120, "mg": 40}})
        L2 = Learner(P, FixedOffsetTime(0), st)
        s = L2.zone_states().zones[0]
        self.assertEqual((s.id, s.delay_s, s.delay_src, s.margin_cm), ("l0001", 120, "manual", 40))

    def test_corrupt_zone6_raises_state_error(self):
        L = zone_learner()
        L.handle(ZoneTune("l0001", "cd", 120, BASE_MS))
        st = json.loads(json.dumps(L.export_state()))
        st["p5"]["zone6"]["tune"]["l0001"]["cd"] = 5000
        with self.assertRaises(StateError):
            Learner(P, FixedOffsetTime(0), st)

    def test_unknown_keys_inside_zone6_are_kept(self):
        L = zone_learner()
        L.handle(ZoneTune("l0001", "cd", 120, BASE_MS))
        st = json.loads(json.dumps(L.export_state()))
        st["p5"]["zone6"]["drift"] = {"a": 1}
        L2 = Learner(P, FixedOffsetTime(0), st)
        self.assertEqual(L2.export_state()["p5"]["zone6"]["drift"], {"a": 1})

    def test_orphans_are_pruned_and_counted(self):
        L = zone_learner()
        L.handle(ZoneTune("l0001", "cd", 120, BASE_MS))
        st = json.loads(json.dumps(L.export_state()))
        st["p5"]["zone6"]["tune"]["l0009"] = {"cd": 10}
        L2 = Learner(P, FixedOffsetTime(0), st)
        L2.zone_states()
        self.assertEqual(L2.counters()["zone6_pruned"], 1)
        self.assertEqual(set(L2.export_state()["p5"]["zone6"]["tune"]), {"l0001"})

    def test_zone6_without_any_zone_is_valid(self):
        L = Learner(P, FixedOffsetTime(0))
        L.zone6.tune["z0001"] = {"cd": 30}
        st = json.loads(json.dumps(L.export_state()))
        L2 = Learner(P, FixedOffsetTime(0), st)
        L2.zone_states()
        self.assertEqual(L2.export_state()["p5"]["zone6"]["tune"], {"z0001": {"cd": 30}})

    def test_forget_all_carries_the_epoch_plus_one(self):
        L = zone_learner()
        L.handle(ZoneTune("l0001", "cd", 120, BASE_MS))
        L2 = L.reset()
        self.assertEqual(L2.zone6.epoch, 1)
        self.assertEqual(L2.zone6.tune, {})
        self.assertEqual(L2.export_state()["p5"]["zone6"], {"v": 1, "set": {"epoch": 1}, "tune": {}, "delay": {},
                                                             "ld": {}, "ldr": [], "info": {}, "ve": {}})
        self.assertEqual(L2.reset().zone6.epoch, 2)


class CommandTest(unittest.TestCase):
    def test_tune_ranges_and_unknown_zone(self):
        L = zone_learner()
        L.tick(BASE_MS + 1000)                                  # the zone is synced and its first state emitted
        for ev in (ZoneTune("l0001", "cd", 2, BASE_MS), ZoneTune("l0001", "cd", 901, BASE_MS),
                   ZoneTune("l0001", "mg", -51, BASE_MS), ZoneTune("l0001", "mg", 101, BASE_MS),
                   ZoneTune("l0001", "zz", 5, BASE_MS), ZoneTune("z0009", "cd", 30, BASE_MS)):
            self.assertEqual(L.handle(ev), [], ev)
        self.assertEqual(L.counters()["zone_cmds_rejected"], 6)
        for ev in (ZoneTune("l0001", "cd", 3, BASE_MS), ZoneTune("l0001", "cd", 900, BASE_MS),
                   ZoneTune("l0001", "mg", -50, BASE_MS), ZoneTune("l0001", "mg", 100, BASE_MS)):
            outs = L.handle(ev)
            self.assertIn(SaveRequested("zone"), outs)

    def test_a_tune_changes_the_states_and_is_idempotent(self):
        L = zone_learner()
        L.handle(ZoneTune("l0001", "cd", 77, BASE_MS))
        s = L.zone_states().zones[0]
        self.assertEqual((s.delay_s, s.delay_src), (77, "manual"))
        self.assertEqual(L.handle(ZoneTune("l0001", "cd", 77, BASE_MS)), [])
        L.handle(ZoneForget("l0001", BASE_MS))
        s = L.zone_states().zones[0]
        self.assertEqual((s.delay_s, s.delay_src), (300, "default"))

    def test_the_tune_command_emits_a_changed_zone_state(self):
        L = zone_learner()
        outs = L.handle(ZoneTune("l0001", "mg", 60, BASE_MS))
        zs = [o for o in outs if isinstance(o, ZoneStates)]
        self.assertEqual(zs[0].zones[0].margin_cm, 60)

    def test_variant_requests(self):
        L = zone_learner()
        L.zone6.info["l0001"] = {"th": {}, "var": [{"d": "wd", "s": 9, "e": 17, "n": 12}], "conf": 0.5}
        L.zone_eval.refresh()
        self.assertEqual(L.zone_states().zones[0].variants, ("wd0917",))
        outs = L.handle(VariantRequest("l0001", "wd0917", True, BASE_MS))
        self.assertIn(SaveRequested("zone"), outs)
        self.assertEqual(L.zone_states().zones[0].requested, ("wd0917",))
        self.assertEqual(L.handle(VariantRequest("l0001", "wd0917", True, BASE_MS)), [])
        self.assertEqual(L.handle(VariantRequest("l0001", "we2023", True, BASE_MS)), [])      # not a variant
        L.handle(VariantRequest("l0001", "wd0917", False, BASE_MS))
        self.assertEqual(L.zone_states().zones[0].requested, ())

    def test_a_command_is_applied_while_the_radars_are_off(self):
        L = zone_learner()
        self.assertFalse(L.zone_states().valid)
        L.handle(ZoneTune("l0001", "cd", 10, BASE_MS))
        self.assertEqual(L.zone_states().zones[0].delay_s, 10)


class NightlyTest(unittest.TestCase):
    def test_spells_become_the_learned_delay_at_the_nightly_run(self):
        from radar_zone_learner.core import zoneinfo
        L = zone_learner()
        L.zone_states()
        L.zone6.delay["l0001"] = {"sp": [30, 60, 90, 120, 150, 180, 210, 240, 270, 300]}
        L.zone_eval.refresh()
        self.assertEqual(L.zone_states().zones[0].delay_src, "default")          # the delay changes only nightly
        zoneinfo.nightly(L, 3)
        s = L.zone_states().zones[0]
        self.assertEqual((s.delay_s, s.delay_src), (300, "learned"))
        self.assertEqual(L.zone6.delay["l0001"]["upd"], 3)

    def test_the_whole_nightly_run_survives_a_zoneinfo_failure(self):
        from radar_zone_learner.core import learner as learner_mod
        L = zone_learner()
        f = Feeder(L)
        f.idle_until(30_000)
        orig = learner_mod.zoneinfo.nightly
        learner_mod.zoneinfo.nightly = lambda *a, **k: 1 / 0
        try:
            f.idle_until(4 * 3600_000)             # past 03:30 of day 0
        finally:
            learner_mod.zoneinfo.nightly = orig
        from radar_zone_learner.core.types import NightlyDone, NightlyFailed
        self.assertTrue(any(isinstance(o, NightlyDone) for o in f.outs))
        failed = [o for o in f.outs if isinstance(o, NightlyFailed)]
        self.assertEqual(len(failed), 1)
        self.assertIn("ZeroDivisionError", failed[0].error)


class PauseTest(unittest.TestCase):
    def run_once(self, pause):
        L = zone_learner()
        if pause:
            L.handle(SettingSwitch("guests_pause", True, BASE_MS))
        f = Feeder(L)
        f.idle_until(30_000)
        f.targets_until(36_000, walk_fn(OUT, POS, 30_000, 36_000))
        f.targets_until(60_000, sit(POS))
        f.targets_until(66_000, walk_fn(POS, OUT, 60_000, 66_000))
        f.idle_until(70_000)
        return [(type(o).__name__, getattr(o, "kind", None), getattr(o, "via", None), getattr(o, "t_dev_ms", None))
                for o in f.outs if isinstance(o, (ZoneEvent,))]

    def test_zone_evaluation_ignores_the_guests_pause(self):
        self.assertEqual(self.run_once(False), self.run_once(True))
        self.assertEqual(len(self.run_once(True)), 2)

    def test_the_evaluator_does_not_read_the_pause(self):
        import inspect
        from radar_zone_learner.core import zoneeval, zoneinfo
        for mod in (zoneeval, zoneinfo):
            src = inspect.getsource(mod)
            self.assertNotIn("paused", src)


if __name__ == "__main__":
    unittest.main()
