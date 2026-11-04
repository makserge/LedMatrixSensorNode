"""P5 review map data: `Learner.review_map` and the zones on `debug_map()` (spec 2.9, A.6, §11.3 `test_p5_map`)."""
import dataclasses
import unittest

from radar_zone_learner.core.types import DebugMapData

from .test_labels import BED, CHAIR
from .test_p5_answers import ans
from .test_p5_items import BED_ITEM, POS, evidence, places
from .test_p5_where import scene, sit
from .test_p5_zones import merge_scene, modify_scene

NEW_FIELDS = ("zones", "others", "highlight", "focus", "live")


def base_fields(d):
    return {f.name: getattr(d, f.name) for f in dataclasses.fields(d) if f.name not in NEW_FIELDS}


def cells_of(L, sid):
    return frozenset([s for s in L.ns.suggestions if s["id"] == sid][0]["cells"])


class DebugMapTest(unittest.TestCase):
    def test_debug_map_is_unchanged_except_for_the_zones(self):
        L = places(3)
        d = L.debug_map()
        self.assertIsInstance(d, DebugMapData)
        self.assertEqual((d.zones, d.others, d.highlight, d.focus, d.live), ((), (), frozenset(), None, ()))
        ans(L, "s0005-1", "approve", name="Bett")
        L.label_list.append({"id": "l0001", "created_s": 0, "x": 0.0, "y": 7.9, "name": "X", "kind": "manual",
                             "cells": frozenset([5, 6])})
        d2 = L.debug_map()
        self.assertEqual([b for _, b in d2.zones], [True, False])
        self.assertEqual(d2.zones[0][0], cells_of(L, "s0005-1"))
        self.assertEqual((d2.others, d2.highlight, d2.focus, d2.live), ((), frozenset(), None, ()))
        self.assertEqual(len(d2.dwell_rate), 2800)
        self.assertEqual(base_fields(d2)["fov_deg"], 60.0)

    def test_the_old_fields_are_the_same_in_the_review_map(self):
        L = places(3)
        self.assertEqual(base_fields(L.review_map()), base_fields(L.debug_map()))
        self.assertEqual(L.review_map().zones, L.debug_map().zones)


class ReviewMapTest(unittest.TestCase):
    def test_a_place_item_selected(self):
        L = places(3)
        m = L.review_map("s0005-2")
        s = cells_of(L, "s0005-2")
        self.assertEqual(m.highlight, s)
        self.assertEqual(m.others, (cells_of(L, "s0005-1"), cells_of(L, "s0005-3")))   # open, not selected, list order
        self.assertAlmostEqual(m.focus[0], POS[1][0], delta=0.05)
        self.assertAlmostEqual(m.focus[1], POS[1][1], delta=0.05)
        self.assertEqual(m.live, ())
        self.assertEqual(m.zones, ())

    def test_focus_is_the_centroid_of_the_highlight(self):
        L = places(2)
        m = L.review_map("s0005-1")
        xs = [L.geom.cell_xy(c) for c in m.highlight]
        self.assertAlmostEqual(m.focus[0], sum(x for x, _ in xs) / len(xs), delta=0.001)
        self.assertAlmostEqual(m.focus[1], sum(y for _, y in xs) / len(xs), delta=0.001)

    def test_the_learners_own_selection_when_none_is_given(self):
        L = places(2)
        m = L.review_map()
        self.assertEqual((m.highlight, m.focus), (frozenset(), None))      # the bed item is selected: nothing to draw
        self.assertEqual(len(m.others), 2)
        from radar_zone_learner.core.types import ReviewSelect
        L.handle(ReviewSelect("s0005-1", 1))
        self.assertEqual(L.review_map().highlight, cells_of(L, "s0005-1"))
        self.assertEqual(L.review_map(None).others, (cells_of(L, "s0005-2"),))

    def test_tier_one_items_and_none_highlight_nothing(self):
        L = places(2)
        for sel in (BED_ITEM, "night_window", "no-such-id", "adv:undo:bed"):
            m = L.review_map(sel)
            self.assertEqual((m.highlight, m.focus), (frozenset(), None), sel)
            self.assertEqual(len(m.others), 2, sel)

    def test_zones_are_records_then_labels(self):
        L = places(3)
        ans(L, "s0005-3", "approve", name="Bett")
        ans(L, "s0005-1", "approve", name="Desk")
        L.label_list.append({"id": "l0001", "created_s": 0, "x": 0.0, "y": 7.9, "name": "Plant", "kind": "bed",
                             "cells": frozenset([7])})
        m = L.review_map("s0005-2")
        self.assertEqual([(len(c) > 0, b) for c, b in m.zones], [(True, True), (True, False), (True, True)])
        self.assertEqual(m.zones[0][0], cells_of(L, "s0005-3"))             # z0001 is the first approved
        self.assertEqual(m.zones[2][0], frozenset([7]))
        self.assertEqual(m.others, ())                                      # the others are no longer open

    def test_a_zone_or_label_id_highlights_that_zone(self):
        L = places(2)
        ans(L, "s0005-1", "approve", name="Desk")
        L.label_list.append({"id": "l0001", "created_s": 0, "x": 0.0, "y": 7.9, "name": "Plant", "kind": "manual",
                             "cells": frozenset([7, 8])})
        self.assertEqual(L.review_map("z0001").highlight, cells_of(L, "s0005-1"))
        m = L.review_map("l0001")
        self.assertEqual(m.highlight, frozenset([7, 8]))
        self.assertEqual(m.others, (cells_of(L, "s0005-2"),))

    def test_a_suggestion_that_is_no_longer_open_is_still_drawn_for_the_push_map(self):
        L = places(2)
        ans(L, "s0005-1", "approve", name="Desk")                           # the head of the push was answered meanwhile
        m = L.review_map("s0005-1")
        self.assertEqual(m.highlight, cells_of(L, "s0005-1"))
        self.assertEqual(m.others, (cells_of(L, "s0005-2"),))
        self.assertNotEqual(L.review_map("s9999-1").highlight, cells_of(L, "s0005-1"))
        self.assertEqual(L.review_map("s9999-1").highlight, frozenset())

    def test_merge_and_modify_items_show_their_suggestion_and_the_zone_is_in_the_zones(self):
        L = merge_scene()
        m = L.review_map("mrg-s0006-1")
        self.assertEqual(m.highlight, cells_of(L, "s0006-1"))
        self.assertEqual(len(m.zones), 1)
        self.assertEqual(m.others, ())
        L = modify_scene()
        m = L.review_map("mod-z0001")
        self.assertEqual(m.highlight, cells_of(L, "s0005-1"))               # the zone's source suggestion, the new region
        self.assertEqual(m.zones[0][0], L.review.zones[0]["cells"])         # the old region
        self.assertEqual(m.others, ())

    def test_live_markers_only_with_live_and_observing(self):
        L, f = scene()
        sit(f, BED, secs=30)
        self.assertEqual(L.review_map("bed_needs_data", live=False).live, ())
        m = L.review_map("bed_needs_data", live=True)
        self.assertEqual(len(m.live), 1)
        x, y, stay = m.live[0]
        self.assertAlmostEqual(x, BED[0], delta=0.15)
        self.assertTrue(stay)
        self.assertEqual(m.live, L.live_view())
        L.tick(L.now + 120000)
        self.assertEqual(L.review_map("bed_needs_data", live=True).live, ())    # not observing: no markers

    def test_deterministic_and_read_only(self):
        L = places(3)
        L.review.items(L)                                                   # listing allocates the tags: its only effect
        before = L.export_state()
        a = L.review_map("s0005-1", live=True)
        b = L.review_map("s0005-1", live=True)
        self.assertEqual(a, b)
        self.assertEqual(L.export_state(), before)


if __name__ == "__main__":
    unittest.main()
