"""P5 'You are in': `Learner.where_am_i` and `Learner.live_view` (spec 2.7, A.5, §11.3 `test_p5_where`)."""
import random
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core import labels as labels_mod
from radar_zone_learner.core import where as WH
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import FixedOffsetTime, WhereView

from .test_labels import BED, CHAIR, setup
from .test_learner import BASE_MS, sit_fn, start
from .test_p5_answers import ans
from .test_p5_items import evidence
from .test_review import add_place


def scene(night=7):
    L, f = setup()
    L.night = night
    L.review.touch()
    return L, f


def sit(f, pos, secs=30, seed=1, extra=None):
    rng = random.Random(seed)
    if extra is None:
        f.targets_until(f.next_ms + secs * 1000, sit_fn(rng, pos))
    else:
        f.targets_until(f.next_ms + secs * 1000, lambda t: sit_fn(rng, pos)(t) + sit_fn(rng, extra)(t))


def place_at(L, pos, sid, conf=0.8):
    return add_place(L, pos, sid=sid, conf=conf, evidence=evidence())


class WhereTest(unittest.TestCase):
    def test_nobody_in_view(self):
        L, f = scene()
        self.assertEqual(L.where_am_i(), WhereView(True, 0, False, None, None))
        self.assertEqual(L.live_view(), ())

    def test_the_radars_do_not_observe(self):
        L = Learner(P, FixedOffsetTime(0))
        start(L)                                                       # no frames yet: 'starting', not 'learning'
        self.assertEqual(L.where_am_i(), WhereView(False, 0, False, None, None))
        self.assertEqual(L.live_view(), ())

    def test_one_person_in_an_open_place_item(self):
        L, f = scene()
        place_at(L, BED, "s0005-1")
        sit(f, BED)
        w = L.where_am_i()
        self.assertEqual((w.observing, w.people, w.stay_ready, w.item, w.item_kind), (True, 1, True, "s0005-1", "place"))

    def test_the_selected_item_comes_first_then_the_others_in_list_order(self):
        L, f = scene()
        place_at(L, BED, "s0005-1", conf=0.9)
        place_at(L, BED, "s0005-2", conf=0.8)
        sit(f, BED)
        self.assertEqual(L.where_am_i().item, "s0005-1")                # list order (the bed item is selected)
        self.assertEqual(L.where_am_i("s0005-2").item, "s0005-2")       # the selected item wins
        self.assertEqual(L.where_am_i("bed_needs_data").item, "s0005-1")
        self.assertEqual(L.where_am_i("z9999").item, "s0005-1")         # an unknown or non-place selection changes nothing

    def test_a_place_elsewhere_is_not_the_position(self):
        L, f = scene()
        place_at(L, BED, "s0005-1")
        sit(f, CHAIR)
        w = L.where_am_i()
        self.assertEqual((w.people, w.item, w.item_kind), (1, None, None))

    def test_the_dilated_cells_count(self):
        L, f = scene()
        s = place_at(L, (BED[0] + 0.45, BED[1]), "s0005-1")             # the person is one cell outside the 0.3 m disc
        sit(f, BED)
        self.assertNotIn(L.geom.cell_of(*BED), s["cells"])
        self.assertEqual(L.where_am_i().item, "s0005-1")
        place_at(L, (BED[0] + 1.5, BED[1]), "s0005-2")
        self.assertEqual(L.where_am_i("s0005-2").item, "s0005-1")

    def test_a_confirmed_zone_and_a_label(self):
        L, f = scene()
        place_at(L, BED, "s0005-1")
        ans(L, "s0005-1", "approve", name="Bed spot")
        sit(f, BED)
        w = L.where_am_i()
        self.assertEqual((w.item, w.item_kind), ("z0001", "zone"))
        L2, f2 = scene()
        L2.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": BED[0], "y": BED[1], "name": "",
                              "kind": "manual", "cells": L2.geom.cells_within(BED[0], BED[1], 0.4)})
        sit(f2, BED)
        w = L2.where_am_i()
        self.assertEqual((w.item, w.item_kind), ("l0001", "label"))      # a label with no name is still a label

    def test_open_items_before_zones_fewest_cells_first_then_lowest_id(self):
        L, f = scene()
        place_at(L, BED, "s0005-1", conf=0.9)
        ans(L, "s0005-1", "approve", name="A")
        place_at(L, BED, "s0005-2", conf=0.8)                           # an open item at the same place
        sit(f, BED)
        self.assertEqual(L.where_am_i().item, "s0005-2")                # an open item first
        ans(L, "s0005-2", "reject")
        self.assertEqual((L.where_am_i().item, L.where_am_i().item_kind), ("z0001", "zone"))
        L.label_list.append({"id": "l0001", "created_s": 0, "x": BED[0], "y": BED[1], "name": "L",
                             "kind": "manual", "cells": L.geom.cells_within(BED[0], BED[1], 0.6)})
        self.assertEqual(L.where_am_i().item, "z0001")                  # 9 cells beat 25
        L.review.zones[0]["cells"] = frozenset(L.review.zones[0]["cells"]) | L.geom.cells_within(BED[0], BED[1], 0.8)
        self.assertEqual((L.where_am_i().item, L.where_am_i().item_kind), ("l0001", "label"))   # now the label is smaller
        L.label_list.append({"id": "l0000", "created_s": 0, "x": BED[0], "y": BED[1], "name": "M", "kind": "manual",
                             "cells": L.label_list[0]["cells"]})
        self.assertEqual(L.where_am_i().item, "l0000")                  # same size: the lowest id

    def test_several_people(self):
        L, f = scene()
        place_at(L, BED, "s0005-1")
        sit(f, BED, extra=CHAIR)
        w = L.where_am_i()
        self.assertEqual((w.people, w.item, w.item_kind, w.stay_ready), (2, None, None, False))   # two stays: no press

    def test_stay_ready_means_a_press_would_succeed(self):
        L, f = scene()
        sit(f, BED, secs=10)
        self.assertFalse(L.where_am_i().stay_ready)                     # 10 s: no stay of 20 s yet
        sit(f, BED, secs=15)
        self.assertTrue(L.where_am_i().stay_ready)
        from .test_labels import press
        self.assertTrue(press(L, f)[0].ok)

    def test_the_stay_rule_equals_the_one_of_mark_spot_here(self):
        for k, (secs, extra) in enumerate(((10, None), (30, None), (30, CHAIR))):
            L, f = scene()
            sit(f, BED, secs=secs, seed=k + 1, extra=extra)
            mine = [t for _, t in WH.track_stays(L)]
            self.assertEqual(mine, labels_mod.stays_in_progress(L), k)
            self.assertEqual(len(mine), [0, 1, 2][k])

    def test_the_item_is_none_while_not_observing(self):
        L, f = scene()
        place_at(L, BED, "s0005-1")
        sit(f, BED)
        self.assertEqual(L.where_am_i().item, "s0005-1")
        L.tick(BASE_MS + f.next_ms + 120000)                            # the stream fell silent: not observing
        self.assertNotEqual(L.status().state, "learning")
        self.assertEqual(L.where_am_i(), WhereView(False, 0, False, None, None))


class LiveViewTest(unittest.TestCase):
    def test_positions_and_the_stay_flag(self):
        L, f = scene()
        sit(f, BED, secs=8)
        v = L.live_view()
        self.assertEqual(len(v), 1)
        x, y, stay = v[0]
        self.assertAlmostEqual(x, BED[0], delta=0.15)
        self.assertAlmostEqual(y, BED[1], delta=0.15)
        self.assertEqual((round(x, 3) == x, round(y, 3) == y, stay), (True, True, False))
        sit(f, BED, secs=25)
        self.assertEqual([s for _, _, s in L.live_view()], [True])

    def test_two_tracks(self):
        L, f = scene()
        sit(f, BED, secs=30, extra=CHAIR)
        v = L.live_view()
        self.assertEqual(len(v), 2)
        self.assertTrue(all(isinstance(s, bool) for _, _, s in v))

    def test_nothing_while_not_learning(self):
        L, f = scene()
        sit(f, BED)
        L.tick(BASE_MS + f.next_ms + 120000)
        self.assertEqual(L.live_view(), ())

    def test_the_live_view_changes_nothing(self):
        L, f = scene()
        sit(f, BED)
        before = L.export_state()
        for _ in range(3):
            L.live_view()
            L.where_am_i()
        self.assertEqual(L.export_state(), before)


if __name__ == "__main__":
    unittest.main()
