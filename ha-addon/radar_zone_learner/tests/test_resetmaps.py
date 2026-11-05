"""P7 reset of the learned maps and 'needs re-check' (spec 4.7, check P7-6 (U) `reset_maps` parts)."""
import copy
import json
import unittest

from radar_zone_learner.core import Learner, movedfrom, rejections
from radar_zone_learner.core.grids import GRID_NAMES
from radar_zone_learner.core.types import FixedOffsetTime, ITEM_BED

from .p7_harness import D0, add_label, add_stay, add_suggestion, add_zone, cover, make, set_run, use_days


def loaded_learner():
    L = make()
    for name in GRID_NAMES:
        L.grids.splat(name, 2.0, 4.0, 5.0)
    L.grids.eff_s = 120.0
    L.grids.eff_f = 40.0
    L.grids.days_mark(2.0, 4.0, D0 + 3)
    L.obs.obs_s_since_extraction = 3600.0
    L.covis.fragsec[7] = 3.0
    L.covis.pairs[1 * 4096 + 2] = 5.0
    for d in range(10):
        cover(L, d)
        add_stay(L, d, 10, 3000, 2.0, 4.0)
    L.bh.door_events.append({"t": 1767571200 + 5, "x": 100, "y": 200, "k": "b"})
    L.bh.ghost_events.append({"t": 1767571200 + 9, "x": 100, "y": 200, "dur": 30, "why": "identical", "ld": 1.0,
                              "arr": "static"})
    L.ns.candidates = [{"cid": "c0010-1", "cells": frozenset([100, 101]), "cx": 2.0, "cy": 4.0, "streak": 2}]
    L.ns.doors_prev = [{"cells": frozenset([5]), "cx": 1.0, "cy": 1.0, "streak": 1}]
    L.ns.outline = {"cells": frozenset([1, 2, 3]), "streak": 4, "done": True}
    L.ns.envelope = frozenset([1, 2, 3])
    zrec = add_zone(L, (2.0, 4.0), name="Desk", sid="s0005-1")
    lab = add_label(L, (5.0, 2.0), name="Plant", kind="manual", lid="l0001")
    s2 = add_suggestion(L, (5.0, 6.0), sid="s0006-1")
    s3 = add_suggestion(L, (6.0, 6.0), sid="s0007-1")
    set_run(L, 12)
    rejections.add_rejection(L, s3, 4)
    rejections.add_real(L, s2, 4)
    L.p5.set_later("s0006-1", D0 + 12)
    L.p5.alloc_tag("s0005-1")
    L.p5.set_setting("pets", True)
    L.p5.push.update(seq=3, acked=2, kind="daily")
    L.review.suppressions.append({"type": "night_window", "until_ord": D0 + 100, "night": 3})
    L.review.suppressions.append({"type": "bed_needs_data", "night": 3})
    L.review.later[ITEM_BED] = D0 + 12
    L.zone6.tune["z0001"] = {"cd": 30}
    movedfrom.add(L, "moved_from", zrec)
    L.zone7.entry("z0001", create=True)["u"] = [0.0] * 9 + [1.0] + [0.0] * 14
    L.zone7.rm.update(st=2, open=True, rev=1, d0=D0 + 10)
    return L


class ResetTest(unittest.TestCase):
    def test_the_maps_and_lists_are_cleared_in_place(self):
        L = loaded_learner()
        grids_obj, covis_old = L.grids, L.covis
        before = L.reset_summary()
        self.assertGreater(before["grids_nonzero"], 0)
        L.reset_maps()
        self.assertIs(L.grids, grids_obj)                                   # in place: the learner object is kept
        self.assertIsNot(L.covis, covis_old)
        self.assertIs(L.bh.covis, L.covis)
        self.assertEqual(L.reset_summary(), {"grids_nonzero": 0, "candidates": 0, "stays": 0, "door_events": 0,
                                             "ghost_events": 0})
        self.assertEqual((L.grids.eff_s, L.grids.eff_f, L.grids.days_base), (0.0, 0.0, None))
        self.assertEqual(sum(L.grids.days_bits), 0)
        self.assertEqual(L.obs.obs_s_since_extraction, 0.0)
        self.assertEqual((L.covis.pairs, sum(L.covis.fragsec)), ({}, 0.0))
        self.assertEqual((L.ns.doors_prev, L.ns.envelope), ([], frozenset()))
        self.assertEqual(L.ns.outline, {"cells": frozenset(), "streak": 0, "done": False})
        self.assertTrue(all(s["state"] == "stale" for s in L.ns.suggestions))
        self.assertEqual(len(L.ns.suggestions), 3)                          # marked stale, not deleted

    def test_masks_real_later_and_only_the_place_suppressions_go(self):
        L = loaded_learner()
        self.assertTrue(L.p5.masks and L.p5.real and L.p5.later)
        self.assertEqual(sorted(s["type"] for s in L.review.suppressions), ["bed_needs_data", "night_window", "place"])
        L.reset_maps()
        self.assertEqual((L.p5.masks, L.p5.real, L.p5.later), ({}, [], {}))
        self.assertEqual(sorted(s["type"] for s in L.review.suppressions), ["bed_needs_data", "night_window"])
        self.assertEqual(L.review.later[ITEM_BED], D0 + 12)                 # review.later survives (the user's answers)
        self.assertEqual(L.zone7.mf, [])

    def test_what_has_nothing_to_do_with_the_radar_pose_is_kept(self):
        L = loaded_learner()
        before = L.export_state()
        L.reset_maps()
        after = L.export_state()
        for key in ("coverage", "night", "settings", "zones", "labels", "review", "meta"):
            self.assertEqual(json.dumps(after[key], sort_keys=True), json.dumps(before[key], sort_keys=True), key)
        self.assertEqual(after["p5"]["push"], before["p5"]["push"])
        self.assertEqual(after["p5"]["tags"], before["p5"]["tags"])
        self.assertEqual(after["p5"]["settings"], before["p5"]["settings"])
        self.assertEqual(after["p5"]["zone6"], before["p5"]["zone6"])
        self.assertEqual(after["stays"], [])
        self.assertEqual(after["door_events"], [])
        self.assertEqual(after["ghost_events"], [])
        self.assertEqual(after["candidates"], [])

    def test_every_zone_and_label_needs_a_recheck_and_the_item_is_closed(self):
        L = loaded_learner()
        L.reset_maps()
        z7 = L.zone7
        for zid in ("z0001", "l0001"):
            e = z7.entry(zid)
            self.assertTrue(e["rc"], zid)
            self.assertEqual(e["rd"], L.review._today(L))
        self.assertEqual(z7.entry("z0001")["u"], [0.0] * 9 + [1.0] + [0.0] * 14)        # u is kept
        self.assertEqual((z7.rm["open"], z7.rm["st"], z7.rm["d0"]), (False, 0, 0))
        self.assertEqual(z7.rm["rev"], 1)                                                # the revision never goes back
        self.assertEqual([d.rc for d in L.drift_view()], [True, True])
        self.assertEqual(L.status().attributes["zones_recheck"], 2)

    def test_the_zones_are_kept_and_no_zone_was_removed(self):
        L = loaded_learner()
        L.reset_maps()
        self.assertEqual([z.id for z in L.zones()], ["z0001", "l0001"])
        self.assertEqual(L.zone6.tune, {"z0001": {"cd": 30}})

    def test_the_detector_is_insufficient_after_the_reset(self):
        from radar_zone_learner.core import radarmoved
        L = loaded_learner()
        L.reset_maps()
        set_run(L, 13)
        cover(L, 12)
        radarmoved.nightly(L, 13)
        self.assertEqual(L.zone7.rm["st"], 0)
        self.assertEqual(L.p7_radar_dbg["verdict"], "insufficient")

    def test_an_export_after_a_reset_loads_again(self):
        L = loaded_learner()
        L.reset_maps()
        st = json.loads(json.dumps(L.export_state()))
        L2 = Learner(L.p, FixedOffsetTime(0), st, L.p5params, L.p6params, L.p7params)
        self.assertEqual(json.dumps(L2.export_state(), sort_keys=True), json.dumps(st, sort_keys=True))
        self.assertTrue(L2.zone7.entry("z0001")["rc"])

    def test_a_reset_of_a_fresh_learner_does_not_raise(self):
        L = make()
        L.reset_maps()
        self.assertEqual(L.reset_summary()["grids_nonzero"], 0)

    def test_the_reset_is_idempotent(self):
        L = loaded_learner()
        L.reset_maps()
        a = json.dumps(L.export_state(), sort_keys=True)
        L.reset_maps()
        self.assertEqual(json.dumps(L.export_state(), sort_keys=True), a)

    def test_a_nightly_run_after_the_reset_works(self):
        from radar_zone_learner.core.types import NightlyDone, NightlyFailed
        L = loaded_learner()
        L.reset_maps()
        from .p7_harness import BASE_MS, DAY
        outs = L.tick(BASE_MS + 13 * DAY * 1000 + 210 * 60000)
        self.assertTrue(any(isinstance(o, NightlyDone) for o in outs))
        self.assertFalse(any(isinstance(o, NightlyFailed) for o in outs), [o for o in outs if isinstance(o, NightlyFailed)])


if __name__ == "__main__":
    unittest.main()
