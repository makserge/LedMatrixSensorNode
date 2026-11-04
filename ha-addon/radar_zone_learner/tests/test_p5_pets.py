"""P5 'Pets in home': the kind `pet` for a silent ignore zone that was used mostly while the home was empty, and the
share that is stored in the evidence whatever the switch says (spec 7.4, D23, §11.3 `test_p5_pets`)."""
import json
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.params import P5Params
from radar_zone_learner.core.types import FixedOffsetTime, NodeStatus, SettingSwitch

from .test_extraction import BASE_MS, BASE_S, DAY, cover, make, night

SPOT = (2.0, 0.8)


def ghost(L, d, why, hour=12, dur=3600):
    L.bh.ghost_events.append({"t": BASE_S + d * DAY + hour * 3600, "x": 200, "y": 80, "dur": dur, "why": why,
                              "ld": 0.0, "arr": "static"})


def scene(pets, plan, nights=None):
    """plan: for each day, (number of home_empty events, number of other ghost events). Ghost evidence at SPOT."""
    L = make()
    if pets:
        L.handle(SettingSwitch("pets", True, BASE_MS + 1))
    for d, (he, other) in enumerate(plan):
        cover(L, d)
        for k in range(he):
            ghost(L, d, "home_empty", hour=8 + (k % 10))
        for k in range(other):
            ghost(L, d, "identical", hour=20 + (k % 3))
        L.grids.splat("ghost", SPOT[0], SPOT[1], 60.0)
        night(L, d + 1)
    return L


def ignore_zones(L):
    return [s for s in L.suggestions() if s.type == "ignore_zone"]


class PetKindTest(unittest.TestCase):
    def test_four_days_and_a_share_of_one_give_pet(self):
        L = scene(True, [(1, 0)] * 4)
        z = ignore_zones(L)
        self.assertEqual(len(z), 1)
        self.assertEqual(z[0].kind, "pet")
        self.assertEqual(z[0].type, "ignore_zone")                      # the type stays
        self.assertEqual(z[0].evidence["he_share"], 1.0)
        self.assertEqual(L.status().attributes["pet_spots"], 1)

    def test_three_days_do_not(self):
        L = scene(True, [(1, 0)] * 3)
        z = ignore_zones(L)
        self.assertEqual([s.kind for s in z], ["ignore"])
        self.assertEqual(z[0].evidence["days"], 3)
        self.assertEqual(L.status().attributes["pet_spots"], 0)

    def test_a_share_of_exactly_one_half_gives_pet_below_it_does_not(self):
        L = scene(True, [(1, 1)] * 4)                                   # 4 of 8 events: 0.5
        self.assertEqual([s.kind for s in ignore_zones(L)], ["pet"])
        self.assertEqual(ignore_zones(L)[0].evidence["he_share"], 0.5)
        plan = [(6, 7), (6, 6), (6, 6), (6, 6)]                         # 24 of 49 events: 0.49 (>= 4 days with them)
        L = scene(True, plan)
        z = ignore_zones(L)
        self.assertEqual([s.kind for s in z], ["ignore"])
        self.assertEqual(z[0].evidence["he_share"], 0.49)

    def test_the_switch_off_never_gives_pet_but_the_share_is_stored(self):
        L = scene(False, [(1, 0)] * 6)
        z = ignore_zones(L)
        self.assertEqual([s.kind for s in z], ["ignore"])
        self.assertEqual(z[0].evidence["he_share"], 1.0)                # stored whatever the switch says
        self.assertEqual(L.status().attributes["pet_spots"], 0)

    def test_switching_on_later_changes_the_kind_at_the_next_run_and_off_again_reverts_it(self):
        L = scene(False, [(1, 0)] * 4)
        self.assertEqual([s.kind for s in ignore_zones(L)], ["ignore"])
        L.handle(SettingSwitch("pets", True, BASE_MS + 4 * DAY * 1000 + 3600000))
        cover(L, 4)
        ghost(L, 4, "home_empty")
        L.grids.splat("ghost", SPOT[0], SPOT[1], 60.0)
        night(L, 5)
        self.assertEqual([s.kind for s in ignore_zones(L)], ["pet"])
        L.handle(SettingSwitch("pets", False, BASE_MS + 5 * DAY * 1000 + 3600000))
        cover(L, 5)
        ghost(L, 5, "home_empty")
        L.grids.splat("ghost", SPOT[0], SPOT[1], 60.0)
        night(L, 6)
        self.assertEqual([s.kind for s in ignore_zones(L)], ["ignore"])

    def test_the_days_are_the_days_with_home_empty_events(self):
        # five days of ghost events, but only three of them while the home was empty
        L = scene(True, [(1, 1), (1, 1), (1, 1), (0, 2), (0, 2)])
        self.assertEqual([s.kind for s in ignore_zones(L)], ["ignore"])
        L = scene(True, [(1, 1), (1, 1), (1, 1), (3, 0), (0, 0)])
        self.assertEqual([s.kind for s in ignore_zones(L)], ["pet"])

    def test_nothing_else_changes_no_place_no_review_item(self):
        L = scene(True, [(1, 0)] * 5)
        self.assertEqual([s for s in L.suggestions() if s.type in ("dwell_zone", "short_task_zone")], [])
        self.assertEqual(L.review.tier2(L), ())

    def test_the_kind_pet_survives_a_restart(self):
        L = scene(True, [(1, 0)] * 4)
        st = json.loads(json.dumps(L.export_state()))
        L2 = Learner(P, FixedOffsetTime(0), st)
        self.assertEqual([s.kind for s in ignore_zones(L2)], ["pet"])
        self.assertTrue(L2.p5.setting("pets"))
        self.assertEqual(L2.status().attributes["pet_spots"], 1)


class SettingTest(unittest.TestCase):
    def test_the_switch_forces_a_status_and_a_save(self):
        from radar_zone_learner.core.types import SaveRequested, StatusChanged
        L = make()
        outs = L.handle(SettingSwitch("pets", True, BASE_MS + 5))
        self.assertEqual([o.reason for o in outs if isinstance(o, SaveRequested)], ["review"])
        st = [o for o in outs if isinstance(o, StatusChanged)][-1]
        self.assertTrue(st.attributes["settings"]["pets"])
        outs = L.handle(SettingSwitch("pets", True, BASE_MS + 6))        # unchanged: no save, the status still says it
        self.assertEqual([o for o in outs if isinstance(o, SaveRequested)], [])
        self.assertTrue([o for o in outs if isinstance(o, StatusChanged)][-1].attributes["settings"]["pets"])
        outs = L.handle(SettingSwitch("nonsense", True, BASE_MS + 7))
        self.assertEqual([o for o in outs if isinstance(o, (SaveRequested, StatusChanged))], [])

    def test_default_off(self):
        L = Learner(P, FixedOffsetTime(0))
        self.assertEqual(L.p5.settings, {"approve_sched": False, "guests_pause": False, "pets": False,
                                         "detail_text": False})
        L.handle(NodeStatus(True, BASE_MS))
        self.assertEqual(L.status().attributes["settings"], L.p5.settings)

    def test_p5params_thresholds_are_used(self):
        L = Learner(P, FixedOffsetTime(0), None, P5Params(pet_min_days=3))
        L.handle(NodeStatus(True, BASE_MS))
        L.handle(SettingSwitch("pets", True, BASE_MS + 1))
        for d in range(3):
            cover(L, d)
            ghost(L, d, "home_empty")
            L.grids.splat("ghost", SPOT[0], SPOT[1], 60.0)
            night(L, d + 1)
        self.assertEqual([s.kind for s in ignore_zones(L)], ["pet"])


if __name__ == "__main__":
    unittest.main()
