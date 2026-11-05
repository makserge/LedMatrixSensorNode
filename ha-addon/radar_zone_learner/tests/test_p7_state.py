"""The `zone7` state key: defaults, validation, the droppable policy, orphans, unknown keys, byte identity, the sync into
`p5.extra`, a 0.6.0 load, the export validated (P7 spec 5, P7-R2, 9.2 `test_p7_state`)."""
import copy
import json
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core.p5state import P5State
from radar_zone_learner.core.p7state import FORMAT_VERSION, Zone7, default_ac, default_entry, default_rm
from radar_zone_learner.core.types import FixedOffsetTime, StateError

from .p7_harness import add_zone, make, p7on

NC = 2800


def u24(hot=(9, 10, 11)):
    u = [0.0] * 24
    for h in hot:
        u[h] = round(1.0 / len(hot), 3)
    return u


def good():
    return {
        "v": 1,
        "z": {"z0001": {"u": u24(), "lo": 1, "rt": 2, "n": 739500, "lu": 739498, "kp": 0, "mr": 2, "ms": "s0040-1#1",
                        "rr": 0, "ro": False, "rc": False, "rd": 0, "fz": False, "vs": "ok"}},
        "mf": [{"zid": "z0001", "cells": [[1234, 3], [1304, 4]], "cx": -2.5, "cy": 3.5, "day": 739490, "n0": 24,
                "why": "moved_from"}],
        "rm": {"st": 0, "rev": 0, "open": False, "ign": 0, "tx": [0, 0, 0], "n": 0, "attn": False, "d0": 0, "qt": 0},
        "snz": {"ret-z0002": 739501, "radar_moved": 739501},
        "ac": default_ac(),
    }


class DefaultsTest(unittest.TestCase):
    def test_a_new_object_is_default_and_exports_nothing(self):
        z = Zone7()
        self.assertTrue(z.is_default())
        self.assertEqual(z.export(), {})
        self.assertEqual((z.z, z.mf, z.snz, z.extra, z.reset_count), ({}, [], {}, {}, 0))
        self.assertEqual(z.rm, default_rm())
        self.assertEqual(z.ac, default_ac())
        self.assertEqual(default_entry(), {"u": [], "lo": 0, "rt": 0, "n": 0, "lu": 0, "kp": 0, "mr": 0, "ms": "",
                                           "rr": 0, "ro": False, "rc": False, "rd": 0, "fz": False, "vs": "young"})

    def test_the_answer_counter_shape(self):
        ac = default_ac()
        self.assertEqual({k: len(v) for k, v in ac["op"].items()},
                         {"place": 4, "merge": 4, "modify": 4, "retire": 3, "radar": 3})
        self.assertEqual(sorted(ac["gh"]), sorted(["cf", "cd", "n", "gs", "cv", "ld"]))
        self.assertTrue(all(len(p) == 2 and all(len(h) == 6 for h in p) for p in ac["gh"].values()))
        self.assertEqual(sorted(ac["rh"]), ["mod_r", "mod_y", "ret_kp", "ret_rm"])
        self.assertTrue(all(len(h) == 6 for h in ac["rh"].values()))

    def test_load_none_and_empty_object_give_the_defaults(self):
        for d in (None, {}):
            z = Zone7()
            z.z["z0001"] = default_entry()
            z.load(d, NC)
            self.assertTrue(z.is_default(), d)
            self.assertEqual(z.reset_count, 0)

    def test_any_change_makes_it_non_default(self):
        for mutate in (lambda z: z.entry("z0001", create=True), lambda z: z.snz.update({"radar_moved": 5}),
                       lambda z: z.rm.update(rev=1), lambda z: z.ac["op"]["place"].__setitem__(0, 1),
                       lambda z: z.ac["rh"]["mod_y"].__setitem__(5, 1), lambda z: z.extra.update(x=1),
                       lambda z: z.mf.append({"zid": "z0001", "cells": frozenset([1]), "cx": 0.0, "cy": 0.0, "day": 1,
                                              "n0": 0, "why": "retired"})):
            z = Zone7()
            mutate(z)
            self.assertFalse(z.is_default())
            self.assertNotEqual(z.export(), {})


class RoundTripTest(unittest.TestCase):
    def test_a_valid_value_round_trips_exactly(self):
        d = good()
        z = Zone7()
        z.load(copy.deepcopy(d), NC)
        self.assertEqual(z.reset_count, 0)
        out = z.export()
        self.assertEqual(json.dumps(out, sort_keys=True), json.dumps(d, sort_keys=True))
        z2 = Zone7()
        z2.load(json.loads(json.dumps(out)), NC)
        self.assertEqual(json.dumps(z2.export()), json.dumps(out))                    # byte identical, key order too

    def test_cells_are_frozensets_in_ram(self):
        z = Zone7()
        z.load(good(), NC)
        self.assertEqual(z.mf[0]["cells"], frozenset([1234, 1235, 1236, 1304, 1305, 1306, 1307]))
        self.assertIsInstance(z.mf[0]["cells"], frozenset)

    def test_unknown_keys_are_kept_at_both_levels(self):
        d = good()
        d["future"] = {"a": [1, 2]}
        d["z"]["z0001"]["newkey"] = "x"
        z = Zone7()
        z.load(d, NC)
        out = z.export()
        self.assertEqual(out["future"], {"a": [1, 2]})
        self.assertEqual(out["z"]["z0001"]["newkey"], "x")

    def test_missing_keys_take_their_defaults(self):
        z = Zone7()
        z.load({"v": 1, "z": {"z0003": {"lo": 4}}}, NC)
        self.assertEqual(z.z["z0003"], dict(default_entry(), lo=4))
        self.assertEqual(z.rm, default_rm())
        self.assertEqual(z.ac, default_ac())

    def test_a_u_of_the_right_shape_is_floats_summing_to_one(self):
        d = good()
        d["z"]["z0001"]["u"] = [0, 0, 0, 0, 0, 0, 0, 0, 0, 1] + [0] * 14               # ints are accepted
        z = Zone7()
        z.load(d, NC)
        self.assertEqual(z.reset_count, 0)
        self.assertTrue(all(isinstance(x, float) for x in z.z["z0001"]["u"]))


class ValidationTest(unittest.TestCase):
    def check_value_problem(self, mutate):
        d = good()
        mutate(d)
        z = Zone7()
        z.load(copy.deepcopy(d), NC)
        self.assertTrue(z.is_default(), d)
        self.assertEqual(z.reset_count, 1)
        with self.assertRaises(StateError):
            Zone7.validate(d, NC)

    def test_a_wrong_container_raises_state_error(self):
        for d in ("x", [], 5, True):
            with self.assertRaises(StateError):
                Zone7().load(d, NC)
        for key in ("z", "rm", "snz", "ac"):
            for bad in ([], "x", 3):
                d = good()
                d[key] = bad
                with self.assertRaises(StateError, msg=(key, bad)):
                    Zone7().load(d, NC)
        for bad in ({}, "x", 3):
            d = good()
            d["mf"] = bad
            with self.assertRaises(StateError):
                Zone7().load(d, NC)

    def test_a_wrong_container_is_not_counted_and_leaves_the_object_alone(self):
        z = Zone7()
        z.load(good(), NC)
        with self.assertRaises(StateError):
            z.load([], NC)
        self.assertEqual(z.reset_count, 0)

    def test_value_problems_reset_to_defaults_and_count(self):
        cases = [
            lambda d: d.update(v=0), lambda d: d.update(v="1"), lambda d: d.update(v=True),
            lambda d: d["z"].update({"bad": {}}), lambda d: d["z"].update({"x0001": {}}),
            lambda d: d["z"]["z0001"].update(u=[0.5] * 5),
            lambda d: d["z"]["z0001"].update(u=[0.5] * 24),                              # sum 12
            lambda d: d["z"]["z0001"].update(u=[0.0] * 24),                              # sum 0 but not []
            lambda d: d["z"]["z0001"].update(u=[-0.1, 1.1] + [0.0] * 22),
            lambda d: d["z"]["z0001"].update(u=[float("nan")] * 24),
            lambda d: d["z"]["z0001"].update(lo=-1), lambda d: d["z"]["z0001"].update(rt=100000),
            lambda d: d["z"]["z0001"].update(mr=1.5), lambda d: d["z"]["z0001"].update(rr=True),
            lambda d: d["z"]["z0001"].update(n=-1), lambda d: d["z"]["z0001"].update(lu="x"),
            lambda d: d["z"]["z0001"].update(kp=-5), lambda d: d["z"]["z0001"].update(rd=-1),
            lambda d: d["z"]["z0001"].update(ms="s0040#1"), lambda d: d["z"]["z0001"].update(ms="x"),
            lambda d: d["z"]["z0001"].update(vs="gone"), lambda d: d["z"]["z0001"].update(ro=1),
            lambda d: d["z"]["z0001"].update(fz="no"), lambda d: d["z"].update({"z0002": 5}),
            lambda d: d["mf"][0].update(cells=[[2799, 5]]), lambda d: d["mf"][0].update(cells=[[0, 0]]),
            lambda d: d["mf"][0].update(cells="x"), lambda d: d["mf"][0].update(cx=40.0),
            lambda d: d["mf"][0].update(cy=float("inf")), lambda d: d["mf"][0].update(why="gone"),
            lambda d: d["mf"][0].update(day=-1), lambda d: d["mf"][0].update(zid="x"),
            lambda d: d["mf"].append(5),
            lambda d: d["rm"].update(st=-1), lambda d: d["rm"].update(tx=[1, 2]), lambda d: d["rm"].update(tx=[1, 2, "x"]),
            lambda d: d["rm"].update(d0=-1), lambda d: d["rm"].update(qt="x"), lambda d: d["rm"].update(open=0),
            lambda d: d["rm"].update(attn="y"),
            lambda d: d["snz"].update({"ret-s0001-1": 3}), lambda d: d["snz"].update({"radar_moved": -1}),
            lambda d: d["snz"].update({"mod-z0001": 3}), lambda d: d["snz"].update({"ret-z0001": 1.5}),
            lambda d: d["ac"].update(op={"place": [1, 2, 3]}),                            # wrong length
            lambda d: d["ac"].update(op={"retire": [1, 2, 3, 4]}),
            lambda d: d["ac"].update(op={"place": [1, 2, 3, "x"]}),
            lambda d: d["ac"].update(gh={"cf": [[0] * 6]}), lambda d: d["ac"].update(gh={"cf": [[0] * 5, [0] * 6]}),
            lambda d: d["ac"].update(rh={"mod_y": [0] * 7}), lambda d: d["ac"].update(rh=[]),
            lambda d: d["ac"].update(op=[]),
        ]
        for mutate in cases:
            self.check_value_problem(mutate)

    def test_a_longer_mask_list_than_the_cap_is_not_a_problem_here(self):
        # the cut to mf_max is the learner's: the container accepts any number of valid entries
        d = good()
        d["mf"] = [dict(d["mf"][0], day=739490 + i) for i in range(60)]
        z = Zone7()
        z.load(d, NC)
        self.assertEqual((len(z.mf), z.reset_count), (60, 0))


class SyncTest(unittest.TestCase):
    def test_default_removes_the_key_and_a_value_writes_it(self):
        p5 = P5State()
        p5.extra["zone7"] = {"v": 1}
        z = Zone7()
        z.sync_into(p5)
        self.assertNotIn("zone7", p5.extra)
        z.entry("z0001", create=True)["lo"] = 2
        z.sync_into(p5)
        self.assertEqual(p5.extra["zone7"]["z"]["z0001"]["lo"], 2)
        self.assertFalse(p5.is_default())                                             # the p5 section is exported then

    def test_the_export_is_validated_before_it_is_written(self):
        p5 = P5State()
        z = Zone7()
        e = z.entry("z0001", create=True)
        e["lo"] = -5                                                                  # a bug writes an out-of-range value
        z.sync_into(p5)
        self.assertNotIn("zone7", p5.extra)                                           # the defaults are written ...
        self.assertTrue(z.is_default())
        self.assertEqual(z.reset_count, 1)                                            # ... and counted

    def test_sync_keeps_unrelated_extra_keys(self):
        p5 = P5State()
        p5.extra["zone6"] = {"v": 1}
        p5.extra["other"] = 5
        z = Zone7()
        z.entry("z0001", create=True)
        z.sync_into(p5)
        self.assertEqual((p5.extra["zone6"], p5.extra["other"]), ({"v": 1}, 5))


class AccessorsTest(unittest.TestCase):
    def test_entry_frozen_forget_drop_prune(self):
        z = Zone7()
        self.assertIsNone(z.entry("z0001"))
        self.assertFalse(z.frozen("z0001"))
        e = z.entry("z0001", create=True)
        self.assertIs(z.entry("z0001"), e)
        self.assertIs(z.entry("z0001", create=True), e)
        e.update(fz=True, lo=4, rt=9, kp=77, lu=5, u=u24(), rc=True, mr=3)
        self.assertTrue(z.frozen("z0001"))
        z.forget_zone("z0001")                                                        # D44: only the two runs
        self.assertEqual((e["lo"], e["rt"], e["kp"], e["lu"], e["rc"], e["mr"], e["u"]), (0, 0, 77, 5, True, 3, u24()))
        z.forget_zone("z0009")                                                        # no entry: nothing happens
        z.entry("z0002", create=True)
        z.snz.update({"ret-z0001": 5, "ret-z0002": 5, "radar_moved": 5})
        z.drop("z0001")
        self.assertEqual((sorted(z.z), sorted(z.snz)), (["z0002"], ["radar_moved", "ret-z0002"]))
        z.entry("l0001", create=True)
        z.snz["ret-z0003"] = 6
        self.assertEqual(z.prune(["z0002"]), 2)                                       # l0001 and the orphan snooze of z0003
        self.assertEqual((sorted(z.z), sorted(z.snz)), (["z0002"], ["radar_moved", "ret-z0002"]))
        self.assertEqual(z.prune(["z0002", "z0003"]), 0)

    def test_snoozed_holds_for_the_run_that_set_it_only(self):
        z = Zone7()
        z.snz["radar_moved"] = 10
        self.assertTrue(z.snoozed("radar_moved", 10))
        self.assertFalse(z.snoozed("radar_moved", 11))
        self.assertFalse(z.snoozed("ret-z0001", 10))


class LearnerStateTest(unittest.TestCase):
    def test_a_learner_that_never_used_p7_exports_what_p6_exported(self):
        L = make()
        st = L.export_state()
        self.assertNotIn("p5", st)
        L2 = make(p7params=None)
        L2.zone7.entry("z0001", create=True)
        st2 = L2.export_state()
        self.assertIn("zone7", st2["p5"])

    def test_zone7_round_trips_through_the_learner_state(self):
        L = make()
        L.zone7.load(good(), NC)
        st = json.loads(json.dumps(L.export_state()))
        self.assertEqual(st["schema"], 4)
        self.assertEqual(st["phase"], "p5")
        L2 = Learner(L.p, FixedOffsetTime(0), st, L.p5params, L.p6params, L.p7params)
        self.assertEqual(json.dumps(L2.zone7.export(), sort_keys=True), json.dumps(good(), sort_keys=True))
        st2 = json.loads(json.dumps(L2.export_state()))
        self.assertEqual(json.dumps(st2, sort_keys=True), json.dumps(st, sort_keys=True))

    def test_a_disabled_learner_keeps_zone7_through_a_save_and_load(self):
        L = make()
        L.zone7.load(good(), NC)
        st = json.loads(json.dumps(L.export_state()))
        D = Learner(L.p, FixedOffsetTime(0), st)                                      # P7 disabled (the default)
        self.assertFalse(D.p7on)
        st2 = json.loads(json.dumps(D.export_state()))
        self.assertEqual(st2["p5"]["zone7"], st["p5"]["zone7"])

    def test_a_0_6_0_state_without_zone7_loads_and_exports_without_it(self):
        L = make()
        L.p5.set_setting("pets", True)                                                # a non-default p5 section
        st = json.loads(json.dumps(L.export_state()))
        self.assertNotIn("zone7", st["p5"])
        L2 = Learner(L.p, FixedOffsetTime(0), st, L.p5params, L.p6params, p7on())
        self.assertTrue(L2.zone7.is_default())
        self.assertEqual(json.dumps(L2.export_state(), sort_keys=True), json.dumps(st, sort_keys=True))

    def test_the_0_6_0_loader_keeps_zone7_in_extra(self):
        # P5State is the code a 0.6.0 and a 0.5.0 app share: an unknown key of p5 is kept and written back unchanged
        L = make()
        L.zone7.load(good(), NC)
        p5d = L.export_state()["p5"]
        p5 = P5State()
        p5.load(p5d, NC)
        self.assertEqual(p5.extra["zone7"], p5d["zone7"])
        self.assertEqual(p5.export()["zone7"], p5d["zone7"])

    def test_a_p7_0_7_0_state_with_p7_snoozes_and_attention_loads_in_p5_code(self):
        # no new key in p5.later or p5.push (D5): the P5 validators accept a state that carries P7 data
        L = make()
        d = good()
        d["rm"].update(open=True, rev=1, attn=True)
        L.zone7.load(d, NC)
        L.p5.push.update(seq=1, kind="daily", head_id="mod-z0001", head_rev=1, sig=["radar_moved#1", "ret-z0001#2"])
        p5d = json.loads(json.dumps(L.export_state()["p5"]))
        P5State().load(p5d, NC)                                                       # does not raise

    def test_a_value_problem_in_a_saved_state_loads_as_defaults_and_counts(self):
        L = make()
        L.zone7.load(good(), NC)
        st = json.loads(json.dumps(L.export_state()))
        st["p5"]["zone7"]["z"]["z0001"]["lo"] = -3
        L2 = Learner(L.p, FixedOffsetTime(0), st, L.p5params, L.p6params, p7on())
        self.assertTrue(L2.zone7.is_default())
        self.assertEqual(L2.counters()["zone7_reset"], 1)
        self.assertNotIn("zone7", L2.export_state().get("p5", {}))

    def test_a_wrong_container_in_a_saved_state_fails_the_construction(self):
        L = make()
        L.zone7.load(good(), NC)
        st = json.loads(json.dumps(L.export_state()))
        st["p5"]["zone7"] = ["not", "an", "object"]
        with self.assertRaises(StateError):
            Learner(L.p, FixedOffsetTime(0), st, L.p5params, L.p6params, p7on())

    def test_orphans_are_pruned_and_counted_by_the_zone_sync(self):
        L = make()
        add_zone(L, (2.0, 4.0))
        L.zone7.entry("z0001", create=True)
        L.zone7.entry("z0009", create=True)
        L.zone7.snz["ret-z0009"] = 3
        L._zone_sync_check(force=True)
        self.assertEqual(sorted(L.zone7.z), ["z0001"])
        self.assertNotIn("ret-z0009", L.zone7.snz)
        self.assertEqual(L.counters()["zone7_pruned"], 1)

    def test_entries_with_no_zones_at_all_are_valid_and_kept(self):
        L = make()
        L.zone7.entry("z0001", create=True)
        L._zone_sync_check(force=True)
        self.assertEqual(sorted(L.zone7.z), ["z0001"])
        self.assertEqual(L.counters()["zone7_pruned"], 0)

    def test_zone_forget_restarts_only_the_two_runs(self):
        from radar_zone_learner.core.types import ZoneForget
        from .p7_harness import BASE_MS
        L = make()
        add_zone(L, (2.0, 4.0))
        e = L.zone7.entry("z0001", create=True)
        e.update(lo=3, rt=8, kp=99, lu=5, u=u24())
        L.handle(ZoneForget("z0001", BASE_MS + 5))
        self.assertEqual((e["lo"], e["rt"], e["kp"], e["lu"], e["u"]), (0, 0, 99, 5, u24()))

    def test_forget_all_drops_zone7_and_keeps_the_parameters(self):
        L = make(p7on(keep_days=5))
        L.zone7.load(good(), NC)
        F = L.reset()
        self.assertTrue(F.zone7.is_default())
        self.assertEqual(F.p7params.keep_days, 5)


if __name__ == "__main__":
    unittest.main()
