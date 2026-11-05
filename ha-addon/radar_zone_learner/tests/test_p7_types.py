"""P7 types, parameters, version and the Output union (P7 spec A.1-A.4, 9.2 `test_p7_types`)."""
import dataclasses
import re
import unittest

from radar_zone_learner import __version__
from radar_zone_learner.core import learner as LM
from radar_zone_learner.core import params as PM
from radar_zone_learner.core import types as T


class ConstantsTest(unittest.TestCase):
    def test_version_and_schema(self):
        self.assertEqual((T.SCHEMA_VERSION, T.MIN_SCHEMA_VERSION, T.APP_VERSION), (4, 3, "0.7.0"))
        self.assertEqual(__version__, "0.7.0")

    def test_item_constants(self):
        self.assertEqual(T.ITEM_RADAR_MOVED, "radar_moved")
        self.assertEqual(T.ITEM_IDS, ("night_window", "bed_needs_data"))             # the two P4 ids: P4's persisted keys
        self.assertEqual(T.PUSH_KINDS, ("none", "setup", "daily"))                   # a 0.6.0 app must load a 0.7.0 file
        self.assertEqual(T.ANSWER_IGNORED_REASONS[:8], ("no_item", "no_such_item", "stale", "not_pending",
                                                        "not_applicable", "name_needed", "zone_limit", "times_invalid"))
        self.assertEqual(T.ANSWER_IGNORED_REASONS[8:], ("zone_busy",))

    def test_state_vocabularies(self):
        self.assertEqual(T.DRIFT_STATES, ("young", "blind", "frozen", "none", "collapsed", "ok", "falling"))
        self.assertEqual(T.MF_WHY, ("moved_from", "retired"))
        self.assertEqual(T.RADAR_STATES, ("none", "watching", "open", "ignored"))
        self.assertEqual(T.MOD_SRC, ("cand", "own"))

    def test_item_id_grammar_gains_two_alternatives(self):
        item = re.compile(T.ITEM_ID_RE)
        for ok in ("night_window", "bed_needs_data", "radar_moved", "s0005-1", "mrg-s0005-1", "mod-z0001",
                   "ret-z0001", "ret-z12345"):
            self.assertTrue(item.match(ok), ok)
        for bad in ("ret-z1", "ret-s0005-1", "ret-l0001", "radar_moved ", "Radar_moved", "radar_moved#2", "ret-"):
            self.assertFalse(item.match(bad), bad)


class ViewsAndOutputsTest(unittest.TestCase):
    def test_new_classes_are_frozen_and_slotted(self):
        for obj in (T.TuningLine(3, "{}"), T.ZoneDrift("z0001", "ok", 1.0, False, 0, 0, 0, False, False),
                    T.RadarMovedView("none", 0, 0, (0, 0, 0), False)):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(obj, dataclasses.fields(obj)[0].name, 1)
            self.assertFalse(hasattr(obj, "__dict__"))

    def test_push_request_attention_is_a_trailing_defaulted_field(self):
        names = [f.name for f in dataclasses.fields(T.PushRequest)]
        self.assertEqual(names, ["seq", "acked", "kind", "count", "head_id", "head_rev", "acks", "attention"])
        r = T.PushRequest(1, 0, "daily", 2, None, None)
        self.assertFalse(r.attention)
        self.assertEqual(r, T.PushRequest(1, 0, "daily", 2, None, None, (), False))
        self.assertNotEqual(r, dataclasses.replace(r, attention=True))

    def test_tuning_line_is_in_the_union_and_ordered_before_the_save(self):
        self.assertIn(T.TuningLine, T.Output.__args__)
        order = list(LM._OUTPUT_ORDER)
        self.assertEqual(order[-2:], [T.TuningLine, T.SaveRequested])
        self.assertLess(order.index(T.DebugMapChanged), order.index(T.TuningLine))
        p4 = [T.NodeCommand, T.ReviewChanged, T.StatusChanged, T.SaveRequested]
        self.assertEqual(sorted(order.index(c) for c in p4), [order.index(c) for c in p4])


class ParamsTest(unittest.TestCase):
    def test_enabled_defaults_to_false(self):
        self.assertFalse(PM.P7Params().enabled)
        self.assertFalse(PM.DEFAULT_P7.enabled)
        self.assertIsInstance(PM.DEFAULT_P7, PM.P7Params)

    def test_the_spec_values(self):
        p = PM.DEFAULT_P7
        self.assertEqual((p.fast_dates, p.slow_dates, p.fast_hl, p.slow_hl), (3, 14, 3.0, 14.0))
        self.assertEqual((p.cov_min_frac, p.frozen_share, p.static_freeze), (0.5, 0.5, True))
        self.assertEqual((p.min_zone_age_nights, p.min_slow_stays), (7, 4))
        self.assertEqual((p.ratio_falling, p.ratio_flag, p.flag_nights, p.ratio_retire, p.retire_days, p.retire_clear),
                         (0.6, 0.25, 3, 0.1, 21, 0.1))
        self.assertEqual((p.retire_usual_cov, p.keep_days), (0.5, 90))
        self.assertEqual((p.usual_alpha, p.usual_full_cov, p.lastuse_min_dwell_s), (0.2, 0.9, 300))
        self.assertEqual((p.move_min_m, p.move_max_m, p.move_min_r, p.succ_far_m, p.succ_max_share),
                         (0.5, 4.0, 0.6, 1.5, 0.25))
        self.assertEqual((p.merge_touch_m, p.merge_iou, p.merge_min_r), (0.4, 0.2, 0.6))
        self.assertEqual((p.mf_max, p.recheck_days, p.rc_overlap), (50, 28, 0.5))
        self.assertEqual((p.probation_from_night, p.probation_min_span), (14, 10))
        self.assertEqual((p.rm_nights, p.rm_shift_min_m, p.rm_rot_min_deg, p.rm_corr_min, p.rm_gain_min),
                         (2, 0.4, 5.0, 0.7, 0.2))
        self.assertEqual((p.rm_search_m, p.rm_search_step_m, p.rm_rot_max_deg, p.rm_rot_step_deg),
                         (1.0, 0.2, 10.0, 2.5))
        self.assertEqual((p.rm_recent_max_dates, p.rm_min_recent, p.rm_min_ref, p.rm_tol_rings, p.rm_max_points),
                         (3, 10, 30, 1, 200))
        self.assertEqual((p.rm_hold_days, p.rm_quiet_days, p.busy_guard), (14, 14, True))
        self.assertEqual((p.agg_keep_nights, p.agg_night_bytes_max, p.agg_export_bytes_max), (28, 2048, 16384))

    def test_frozen_and_slotted(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            PM.DEFAULT_P7.enabled = True
        self.assertFalse(hasattr(PM.DEFAULT_P7, "__dict__"))

    def test_the_earlier_parameter_classes_are_untouched(self):
        self.assertNotIn("enabled", {f.name for f in dataclasses.fields(PM.Params)})
        self.assertFalse(PM.DEFAULT_P5.zone_change_items)                             # pinned by test_p5_types
        self.assertEqual(len(dataclasses.fields(PM.P5Params)), 21)
        self.assertNotIn("p7", PM.params_digest(PM.DEFAULT_PARAMS))
        self.assertEqual(PM.params_digest(PM.DEFAULT_PARAMS), PM.params_digest(PM.Params()))


class LearnerSurfaceTest(unittest.TestCase):
    def test_a_default_learner_is_not_enabled_and_every_accessor_is_empty(self):
        from radar_zone_learner.core import Learner
        from radar_zone_learner.core.types import FixedOffsetTime
        L = Learner(PM.DEFAULT_PARAMS, FixedOffsetTime(0))
        self.assertFalse(L.p7on)
        self.assertEqual(L.p7params, PM.DEFAULT_P7)
        self.assertEqual(L.drift_view(), ())
        self.assertEqual(L.radar_moved_state(), T.RadarMovedView("none", 0, 0, (0, 0, 0), False))
        self.assertEqual(L.moved_from_count(), 0)
        self.assertEqual((L.p7_errors, L.p7_requalified), (0, set()))
        self.assertTrue(L.zone7.is_default())
        self.assertEqual(L.reset_summary(), {"grids_nonzero": 0, "candidates": 0, "stays": 0, "door_events": 0,
                                             "ghost_events": 0})
        for k in ("zone7_pruned", "zone7_reset", "p7_errors"):
            self.assertNotIn(k, L.counters())                                       # only when enabled

    def test_reset_passes_all_three_parameter_objects(self):
        from tests.p7_harness import make, p7on
        p7 = p7on(keep_days=77)
        L = make(p7)
        F = L.reset()
        self.assertIs(F.p7params, p7)
        self.assertIs(F.p5params, L.p5params)
        self.assertIs(F.p6params, L.p6params)
        self.assertIsNot(F.zone7, L.zone7)


if __name__ == "__main__":
    unittest.main()
