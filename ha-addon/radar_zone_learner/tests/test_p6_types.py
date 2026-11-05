"""P6 interface: constants, inputs, outputs, parameters, output order (P6 spec Appendix A, P6-R1/R2 at unit level)."""
import dataclasses
import re
import unittest

from radar_zone_learner import __version__
from radar_zone_learner.core import learner as learner_mod
from radar_zone_learner.core import types as T
from radar_zone_learner.core.params import DEFAULT_P6, DEFAULT_PARAMS, P6Params, params_digest


class TypesTest(unittest.TestCase):
    def test_versions(self):
        self.assertEqual((T.SCHEMA_VERSION, T.MIN_SCHEMA_VERSION, T.APP_VERSION), (4, 3, "0.7.0"))
        self.assertEqual(__version__, "0.7.0")

    def test_constants(self):
        self.assertEqual(T.ZONE_EVENT_KINDS, ("enter", "leave"))
        self.assertEqual(T.ZONE_EVENT_VIA, ("edge", "hold_expired", "door_death", "early"))
        self.assertEqual(T.CENSOR_CLASSES, ("POWER", "GAP"))
        self.assertEqual(T.ZONE_KINDS_EVALUATED, ("dwell", "short_task", "bed", "manual"))
        self.assertEqual(T.ZONE_KINDS_DWELL, ("dwell", "bed", "manual"))
        self.assertEqual(T.ZONE_TUNE_FIELDS, ("cd", "mg"))
        zid = re.compile(T.ZONE_ID_RE)
        for ok in ("z0001", "l0001", "z12345"):
            self.assertTrue(zid.match(ok))
        for bad in ("z001", "x0001", "z0001 ", "Z0001"):
            self.assertFalse(zid.match(bad), bad)
        vid = re.compile(T.VARIANT_ID_RE)
        self.assertTrue(vid.match("wd0917"))
        self.assertTrue(vid.match("we2023"))
        self.assertFalse(vid.match("xx0917"))
        self.assertFalse(vid.match("wd917"))

    def test_values_are_frozen(self):
        for obj in (T.ZoneTune("z0001", "cd", 30, 1), T.ZoneForget("z0001", 1), T.VariantRequest("z0001", "wd0917", True, 1),
                    T.ZoneEvent("z0001", "enter", "", 1, 2, 0, ""),
                    T.ZoneState("z0001", False, 0, False, "", False, 300, "default", 25),
                    T.ZoneStates(False, 0, ())):
            name = "zid" if hasattr(obj, "zid") else "id"
            # a frozen dataclass with slots raises FrozenInstanceError, or TypeError on some Python 3.12 builds
            with self.assertRaises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
                setattr(obj, name, "x")

    def test_zone_state_defaults(self):
        s = T.ZoneState("z0001", False, 0, False, "", False, 300, "default", 25)
        self.assertEqual((s.variants, s.requested, s.typical_hours, s.confidence), ((), (), "", None))

    def test_inputs_are_in_the_union(self):
        for cls in (T.ZoneTune, T.ZoneForget, T.VariantRequest):
            self.assertIn(cls, T.InputEvent.__args__)
        for cls in (T.ZoneEvent, T.ZoneStates):
            self.assertIn(cls, T.Output.__args__)

    def test_output_order_keeps_the_new_classes_between_existing_ones(self):
        order = learner_mod._OUTPUT_ORDER
        self.assertIn(T.ZoneEvent, order)
        self.assertIn(T.ZoneStates, order)
        self.assertLess(order.index(T.ZoneEvent), order.index(T.ZoneStates))
        self.assertLess(order.index(T.AdvancedChanged), order.index(T.ZoneEvent))
        self.assertLess(order.index(T.ZoneStates), order.index(T.PushRequest))
        self.assertEqual(learner_mod._REASON_PRIORITY["zone"], 3)

    def test_order_passes_zone_outputs(self):
        e = T.ZoneEvent("z0001", "enter", "", 1, 2, 0, "")
        s = T.ZoneStates(True, 5, ())
        out = learner_mod.Learner._order([T.SaveRequested("zone"), s, e, T.SaveRequested("nightly")])
        self.assertEqual(out, [e, s, T.SaveRequested("nightly")])


class ParamsTest(unittest.TestCase):
    def test_defaults_are_the_spec_values(self):
        p = DEFAULT_P6
        self.assertEqual((p.enter_k, p.enter_n, p.exit_frames, p.margin_cm), (3, 10, 1, 25))
        self.assertEqual((p.margin_min_cm, p.margin_max_cm, p.pass_speed_ms, p.vanish_ms, p.count_max), (-50, 100, 0.8, 2000, 3))
        self.assertEqual(dict(p.delay_default_s), {"bed": 600, "dwell": 300, "short_task": 60, "manual": 300})
        self.assertEqual((p.delay_learn_min_s, p.delay_learn_max_s, p.tune_delay_min_s, p.tune_delay_max_s), (5, 600, 3, 900))
        self.assertEqual((p.spell_keep, p.spell_min_for_learn, p.learn_quantile, p.late_return_factor), (50, 10, 0.95, 2.0))
        self.assertEqual((p.door_radius_m, p.door_speed_ms, p.fast_clear_s), (0.4, 0.35, 3))
        self.assertEqual((p.ldr_min_known_s, p.ldr_min_share, p.early_absent_s), (3600, 0.8, 60))
        self.assertEqual((p.episode_budget_day, p.valve_after_episodes, p.zone_entities_max, p.variants_max), (74, 0, 24, 6))

    def test_params_and_the_state_digest_are_not_touched(self):
        self.assertIsInstance(DEFAULT_P6, P6Params)
        self.assertNotIn("enter_k", {f.name for f in dataclasses.fields(DEFAULT_PARAMS)})
        # the digest is a function of Params only (written into every state file)
        self.assertEqual(len(params_digest(DEFAULT_PARAMS)), 12)


if __name__ == "__main__":
    unittest.main()
