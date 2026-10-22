import random
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core import labels as LB
from radar_zone_learner.core.extraction import _new_suggestion
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import (DebugMapChanged, FixedOffsetTime, MarkPress, MarkResult, SaveRequested,
                                           SpotName, StatusChanged)

from .test_learner import BASE_MS, Feeder, sit_fn, start

BED = (-2.3, 4.9)
CHAIR = (1.5, 2.0)


def setup():
    L = Learner(P, FixedOffsetTime(0))
    start(L)
    f = Feeder(L)
    f.present.append((0, 10 ** 9))
    f.idle_until(10000)
    return L, f


def press(L, f):
    return L.handle(MarkPress(BASE_MS + f.next_ms + 50))


class MarkTest(unittest.TestCase):
    def test_not_observing(self):
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        outs = L.handle(MarkPress(BASE_MS + 1000))
        self.assertEqual(outs[0], MarkResult(False, "not_observing", None, None))
        self.assertEqual(L.status().attributes["last_mark"]["reason"], "not_observing")
        self.assertEqual([type(o) for o in outs], [MarkResult, StatusChanged])

    def test_no_stay_with_pending_stay_and_no_track(self):
        L, f = setup()
        rng = random.Random(1)
        f.targets_until(40000, sit_fn(rng, BED))          # 30 s visible
        f.idle_until(50000)                                 # dropped by the radar (lying still)
        self.assertEqual(len(L.bh.pending), 1)
        self.assertEqual(L.tracker.live, [])
        outs = press(L, f)
        self.assertEqual(outs[0].reason, "no_stay")
        self.assertEqual(L.labels(), ())

    def test_several_stays(self):
        L, f = setup()
        rng = random.Random(2)
        f.targets_until(35000, lambda t: sit_fn(rng, BED)(t) + sit_fn(rng, CHAIR)(t))
        self.assertEqual(press(L, f)[0].reason, "several_stays")

    def test_ok_with_bridged_stay(self):
        L, f = setup()
        rng = random.Random(3)
        L.handle(SpotName(" Bed ", BASE_MS + 10000))
        f.targets_until(25000, sit_fn(rng, BED))           # 15 s visible -> a pending stay
        f.idle_until(55000)                                 # hidden 30 s, LD2412 present
        f.targets_until(67000, sit_fn(rng, BED))           # 12 s visible again: the run alone is < 20 s
        outs = press(L, f)
        self.assertEqual([type(o) for o in outs], [MarkResult, StatusChanged, DebugMapChanged, SaveRequested])
        self.assertEqual(outs[0], MarkResult(True, None, "l0001", "bed"))
        self.assertEqual((outs[2].reason, outs[3].reason), ("label", "label"))
        lb = L.labels()[0]
        self.assertAlmostEqual(lb.x_m, BED[0], delta=0.05)
        self.assertAlmostEqual(lb.y_m, BED[1], delta=0.05)
        self.assertEqual((lb.id, lb.name, lb.kind), ("l0001", "Bed", "bed"))
        self.assertEqual(lb.created_s % 300, 0)
        self.assertTrue(10 <= len(lb.cells) <= 16)
        self.assertIn(L.geom.cell_of(*BED), lb.cells)
        lm = outs[1].attributes["last_mark"]
        self.assertEqual((lm["result"], lm["label"], lm["kind"]), ("ok", "l0001", "bed"))
        self.assertEqual(outs[1].attributes["labels"], 1)

    def test_epp_reflector_is_not_a_stay(self):
        """A static reflector (3 mm jitter, OA 7.3 EPP rule) seen for >= 5 min is a ghost, not a stay in
        progress: a press while the person is hidden must not label it (replay liv seed 1)."""
        L, f = setup()
        rng = random.Random(5)
        refl = (2.0, 0.8)
        f.targets_until(330000, sit_fn(rng, refl, sd=0.003))      # 320 s of the reflector
        self.assertEqual(press(L, f)[0].reason, "no_stay")
        f.targets_until(360000, lambda t: sit_fn(rng, refl, sd=0.003)(t) + sit_fn(rng, CHAIR)(t))
        outs = press(L, f)                                          # the person, 30 s: the only stay
        self.assertEqual(outs[0].ok, True)
        lb = L.labels()[0]
        self.assertAlmostEqual(lb.x_m, CHAIR[0], delta=0.05)
        self.assertAlmostEqual(lb.y_m, CHAIR[1], delta=0.05)

    def test_run_alone_too_short(self):
        L, f = setup()
        rng = random.Random(4)
        f.targets_until(22000, sit_fn(rng, BED))           # 12 s visible, nothing pending
        self.assertEqual(press(L, f)[0].reason, "no_stay")
        f.targets_until(32000, sit_fn(rng, BED))           # 22 s
        outs = press(L, f)
        self.assertEqual(outs[0], MarkResult(True, None, "l0001", "manual"))
        self.assertEqual(L.labels()[0].name, "")

    def test_kind_of_name(self):
        for name, kind in (("bed", "bed"), (" Bed ", "bed"), ("BETT", "bed"), ("Desk", "manual"), ("", "manual"),
                           ("bedroom", "manual")):
            self.assertEqual(LB.kind_of_name(name, P.bed_label_names), kind, name)


class MarkLimitsTest(unittest.TestCase):
    """Anyone who can publish PRESS (no ACLs) or a stuck automation must not fill the state."""

    def test_flood_is_refused_merged_and_costs_nothing(self):
        L, f = setup()
        rng = random.Random(6)
        L.handle(SpotName("Bed", BASE_MS + 9000))
        f.targets_until(40000, sit_fn(rng, BED))
        first = press(L, f)
        self.assertEqual(first[0], MarkResult(True, None, "l0001", "bed"))
        self.assertEqual([type(o) for o in first if isinstance(o, SaveRequested)], [SaveRequested])
        f.targets_until(f.next_ms + 500, sit_fn(rng, BED))             # 0.5 s later: too soon
        outs = press(L, f)
        self.assertEqual(outs[0], MarkResult(False, "too_soon", None, None))
        self.assertEqual([o for o in outs if isinstance(o, (SaveRequested, DebugMapChanged))], [])
        self.assertEqual(L.status().attributes["last_mark"]["reason"], "too_soon")
        f.targets_until(f.next_ms + 10000, sit_fn(rng, BED))            # later, the same spot: nothing new
        outs = press(L, f)
        self.assertEqual(outs[0], MarkResult(True, None, "l0001", "bed"))
        self.assertEqual(len(L.labels()), 1)
        self.assertEqual([o for o in outs if isinstance(o, (SaveRequested, DebugMapChanged))], [])
        saves = 0
        for i in range(480):                                            # 4 presses a second for 2 minutes
            if i % 2 == 0:
                f.targets_until(f.next_ms + 500, sit_fn(rng, BED))
            saves += sum(1 for o in press(L, f) if isinstance(o, SaveRequested))
        self.assertEqual((len(L.labels()), saves), (1, 0))
        self.assertEqual(L.next_label_n, 2)                             # no id was burnt
        # another spot, or another name, is a new label
        L.handle(SpotName("Desk", BASE_MS + f.next_ms))
        f.targets_until(f.next_ms + 10000, sit_fn(rng, BED))
        outs = press(L, f)
        self.assertEqual(outs[0], MarkResult(True, None, "l0002", "manual"))
        self.assertEqual(len(L.labels()), 2)

    def test_cap_drops_the_oldest_manual_label(self):
        class Holder:
            label_list = []

        h = Holder()
        h.label_list = []
        old = LB.MAX_LABELS
        LB.MAX_LABELS = 3
        try:
            for i, kind in enumerate(("bed", "manual", "manual", "manual", "bed")):
                LB.add_label(h, {"id": "l%d" % i, "kind": kind})
            self.assertEqual([lb["id"] for lb in h.label_list], ["l0", "l3", "l4"])
            h.label_list = []
            for i in range(4):                                          # all 'bed': the oldest goes
                LB.add_label(h, {"id": "b%d" % i, "kind": "bed"})
            self.assertEqual([lb["id"] for lb in h.label_list], ["b1", "b2", "b3"])
        finally:
            LB.MAX_LABELS = old

    def test_find_same(self):
        labels = [{"id": "l1", "x": 1.0, "y": 2.0, "name": "Bed"}]
        self.assertIs(LB.find_same(labels, 1.3, 2.0, "Bed", 0.4), labels[0])
        self.assertIsNone(LB.find_same(labels, 1.5, 2.0, "Bed", 0.4))
        self.assertIsNone(LB.find_same(labels, 1.0, 2.0, "Desk", 0.4))


class LinkTest(unittest.TestCase):
    def learner_with_places(self):
        L = Learner(P, FixedOffsetTime(0))
        start(L)
        g = L.geom
        for k, pos in enumerate((BED, CHAIR, (-5.0, 7.0))):
            s = _new_suggestion("s0005-%d" % (k + 1), "dwell_zone", 5, g.cells_within(pos[0], pos[1], 0.3),
                                pos[0], pos[1])
            L.ns.suggestions.append(s)
        return L

    def label(self, L, name, pos):
        L.spot_name = name
        lb = LB.make_label(L, (0, 0, pos[0], pos[1], 10), BASE_MS)
        L.label_list.append(lb)
        return lb

    def test_linking(self):
        L = self.learner_with_places()
        self.label(L, "Reading", (BED[0] + 0.25, BED[1]))
        self.label(L, "Bett", (BED[0] - 0.1, BED[1]))
        self.label(L, "Desk", CHAIR)
        for s in L.ns.suggestions:
            L.link_labels_one(s)
        bed, chair, other = L.suggestions()
        self.assertEqual((bed.kind, bed.name_hint), ("bed", "Bett"))      # bed wins over manual
        self.assertEqual((chair.kind, chair.name_hint), ("dwell", "Desk"))
        self.assertEqual((other.kind, other.name_hint), ("dwell", None))

    def test_precision_recall(self):
        L = self.learner_with_places()
        self.assertEqual(L.status().attributes["label_recall"], None)
        self.assertEqual(L.status().attributes["label_precision"], 0.0)
        self.label(L, "Bed", BED)
        self.label(L, "Lost", (5.0, 1.0))
        a = L.status().attributes
        self.assertEqual(a["label_recall"], 0.5)
        self.assertEqual(a["label_precision"], round(1 / 3, 3))
        L.ns.suggestions[2]["state"] = "stale"             # only shadow places count
        self.assertEqual(L.status().attributes["label_precision"], 0.5)
        lb = L.label_list[0]
        s = L.ns.suggestions[0]
        self.assertTrue(LB.match(lb, s, L.geom, 0.6))
        far = dict(lb, x=BED[0] + 0.55, y=BED[1])           # outside the dilated cells, inside 0.6 m
        self.assertTrue(LB.match(far, s, L.geom, 0.6))
        self.assertFalse(LB.match(dict(lb, x=BED[0] + 0.9), s, L.geom, 0.6))


if __name__ == "__main__":
    unittest.main()
