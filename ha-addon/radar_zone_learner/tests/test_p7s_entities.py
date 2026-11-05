"""P7-12 (shell part) and P7-11 (shell part): zone entities and the automations bound to them survive a modify, a retire removes
the entities, and the new review items travel through the layer, the select, the text entities and the push sensor.

Home Assistant is the fake of ``tests/fake_ha_zone.py`` (HA[U1]..HA[U7] of the P6 spec) and, for what P7 adds, the assumptions
HA[U9] (a push request of the kind ``attention`` fires the blueprint like the others) and HA[U10] (a change of the ``poly_cm``
attribute alone leaves the entity registry, the entity ids, the names and the history untouched). Nothing here proves that Home
Assistant behaves so; acceptance steps A1 and A6 settle it. What is proved is that the add-on makes exactly the messages those
assumptions need: the same unique ids, the same topics, no second config, no removal, one changed attribute.

The first class runs the REAL core (a learner that has a zone and a moved candidate, as ``tests.test_moves.sofa_scene`` builds
it) behind the real layer: the tap on 'Approve' goes through the layer's answer path, the core's modify answer and the layer's
output path, and the entities of the zone are looked at before and after."""
import json
import unittest

from radar_zone_learner.core.types import PushRequest, ReviewChanged, ReviewItem
from radar_zone_learner.shell.p4_types import ReviewAnswer
from tests.fakes import FakeLearner
from tests.p7s_harness import LayerRig7
from tests.test_moves import sofa_scene
from tests.test_retire import retire_scene

ANSWER = "radar_zones/review/answer"
SELECT_SET = "radar_zones/review/select/set"
OCC_ID = "binary_sensor.radar_spot_z0001"
# the zone's entities that exist in Home Assistant: the target count sensor is disabled by default and is created only when
# the user enables it, so it has no registry entry here (its config is the sixth of the zone)
ZONE_UIDS = ("radar_spot_z0001", "radar_spot_z0001_event", "radar_spot_z0001_clear_delay", "radar_spot_z0001_margin",
             "radar_spot_z0001_forget")


def zone_configs(rig, zid="z0001"):
    return [p for p in rig.client.published if p[0].startswith("homeassistant/") and "radar_spot_" + zid in p[0]]


def start_real(test, L):
    rig = LayerRig7(test, learner=L)
    rig.start()
    rig.run(11)
    return rig


def tap(rig, item, op, rev):
    """The user's tap: the answer topic, through the mapper, the layer (checks the item and its revision) and the core."""
    rig.client.fire_message(ANSWER, json.dumps({"op": op, "item": item, "rev": rev}))
    rig.ha.process()


class ModifyKeepsTheEntitiesTest(unittest.TestCase):
    """P7-12: a modify keeps the zone id, hence the unique ids, the device and the entity ids."""

    def setUp(self):
        L, rec, s = sofa_scene()
        L.review.touch()
        self.L = L
        self.rig = start_real(self, L)
        items, selected = L.review_state()
        self.rig.feed(ReviewChanged(tuple(items), selected))
        self.ha = self.rig.ha

    def test_the_zone_has_its_entities_and_the_modify_item_is_open(self):
        for uid in ZONE_UIDS:
            self.assertIn(uid, self.ha.registry, uid)
        self.assertIn("mod-z0001", self.rig.layer.items)
        self.assertEqual(self.rig.layer.items["mod-z0001"].type, "modify_zone")
        self.assertEqual(self.rig.layer.sel.label_of("mod-z0001").split(" ")[0], "Moved")

    def test_approve_updates_the_zone_and_changes_no_entity(self):
        old_poly = self.ha.attrs(OCC_ID)["poly_cm"]
        old_entity_ids = sorted(self.ha.entity_ids())
        created = {uid: self.ha.registry[uid]["created"] for uid in ZONE_UIDS}
        old_name = self.ha.entity(OCC_ID).name
        old_cfg = {uid: self.ha.registry[uid]["entity_id"] for uid in ZONE_UIDS}
        rev = self.rig.layer.items["mod-z0001"].rev
        self.rig.cleared()
        tap(self.rig, "mod-z0001", "approve", rev)
        self.rig.run(6)
        # the core made the change: the same zone, new outline
        z = [v for v in self.L.zones() if v.id == "z0001"][0]
        self.assertEqual((z.id, z.name, z.kind), ("z0001", "Desk", "dwell"))
        # the entities: the same ids, no removal, no second creation, no config published again, one attribute changed
        self.assertEqual(sorted(self.ha.entity_ids()), old_entity_ids)
        self.assertEqual(self.ha.removed, [])
        self.assertEqual({uid: self.ha.registry[uid]["created"] for uid in ZONE_UIDS}, created)
        self.assertEqual({uid: self.ha.registry[uid]["entity_id"] for uid in ZONE_UIDS}, old_cfg)
        self.assertEqual(zone_configs(self.rig), [])
        self.assertEqual(self.ha.entity(OCC_ID).name, old_name)
        self.assertNotEqual(self.ha.attrs(OCC_ID)["poly_cm"], old_poly)                   # HA[U10]: only the attribute moves

    def test_an_automation_bound_to_the_entity_id_still_sees_the_zone_after_the_modify(self):
        """The model of an automation: it watches an entity id. The id is the same, the entity is there, and an occupancy
        message after the modify reaches the same entity (HA[U1]-HA[U3], HA[U10])."""
        seen = []
        before = self.ha.entity(OCC_ID)
        self.assertIsNotNone(before)
        rev = self.rig.layer.items["mod-z0001"].rev
        tap(self.rig, "mod-z0001", "approve", rev)
        self.rig.run(6)
        after = self.ha.entity(OCC_ID)
        self.assertIs(after, before)                                                    # the very entity object, never rebuilt
        self.rig.client.publish("radar_zones/zone/z0001/occupancy", "ON", 1, False)
        self.ha.process()
        seen.append(self.ha.entity(OCC_ID).state)
        self.assertEqual(seen, ["on"])

    def test_the_item_is_gone_after_the_answer_and_a_second_tap_is_refused(self):
        rev = self.rig.layer.items["mod-z0001"].rev
        tap(self.rig, "mod-z0001", "approve", rev)
        self.rig.run(6)
        self.assertNotIn("mod-z0001", self.rig.layer.items)
        before = self.rig.layer.answers_ignored
        tap(self.rig, "mod-z0001", "approve", rev)
        self.assertEqual(self.rig.layer.answers_ignored, before + 1)                    # no such item: refused by the layer


class RetireRemovesTheEntitiesTest(unittest.TestCase):
    """A retire approval removes the zone: its six configs are emptied (HA[U2]), nothing else is touched."""

    def test_remove(self):
        L, rec = retire_scene(unused_until=48)
        L.review.touch()
        rig = start_real(self, L)
        items, selected = L.review_state()
        self.assertIn("ret-z0001", [i.id for i in items])
        rig.feed(ReviewChanged(tuple(items), selected))
        for uid in ZONE_UIDS:
            self.assertIn(uid, rig.ha.registry)
        self.assertTrue(rig.layer.sel.label_of("ret-z0001").startswith("Retire "))
        rev = rig.layer.items["ret-z0001"].rev
        rig.cleared()
        tap(rig, "ret-z0001", "approve", rev)
        rig.run(6)
        self.assertEqual([z.id for z in L.zones()], [])
        self.assertEqual(sorted(rig.ha.removed), sorted(ZONE_UIDS))               # the disabled count sensor never existed
        self.assertEqual(rig.ha.entity(OCC_ID), None)
        empties = [p for p in zone_configs(rig) if p[1] == b""]
        self.assertEqual(len(empties), 6)
        self.assertTrue(all(p[2:] == (1, False) for p in empties))


class ReviewItemsThroughTheLayerTest(unittest.TestCase):
    """The new items on the review device with the scripted core: the select, the pending attributes, the text entity, the
    answers, the stale and the unknown, and the push request."""

    def setUp(self):
        self.rig = LayerRig7(self)
        self.rig.start()
        self.rig.radar()
        self.rig.run(11)
        self.t = self.rig.topics
        self.mod = ReviewItem("mod-z0001", 2, "modify_zone", {"tag": "C", "sid": "s0020-1", "zid": "z0001", "zname": "Desk",
                                                               "shift_cm": 110, "dir": "to the right", "r": 0.9, "src": "cand"}, 2)
        self.ret = ReviewItem("ret-z0002", 1, "retire_zone", {"tag": "D", "zid": "z0002", "zname": "Armchair", "weeks": 4}, 2)
        self.radar = ReviewItem("radar_moved", 3, "radar_moved", {"shift_cm": 80, "deg": 10.0, "streak": 2}, 1)

    def show(self, items, selected=None):
        self.rig.feed(ReviewChanged(tuple(items), selected))
        self.rig.run(3)

    def test_the_select_pending_attributes_and_text_show_the_new_items_with_no_name_by_default(self):
        self.show([self.radar, self.mod, self.ret], "radar_moved")
        opts = json.loads(self.rig.published("homeassistant/select/radar_zones_review_select/config")[-1][1])["options"]
        self.assertEqual(opts, ["No item", "Radar moved", "Moved C", "Retire D"])
        self.assertEqual(self.rig.last(self.t.review_select_state), "Radar moved")
        self.assertEqual(self.rig.last(self.t.review_pending), "3")
        self.assertIn("The radar seems to have been moved or turned (about 80 cm, 10.0 degrees)",
                      self.rig.last(self.t.review_item_text))
        attr = json.loads(self.rig.last(self.t.review_pending_attr))
        self.assertEqual([r["id"] for r in attr["items"]], ["radar_moved", "mod-z0001", "ret-z0002"])
        self.assertEqual([r["tier"] for r in attr["items"]], [1, 2, 2])
        for row in attr["items"]:
            self.assertNotIn("Desk", row["text"])
            self.assertNotIn("Armchair", row["text"])

    def test_with_the_detail_switch_on_the_names_appear_in_the_texts(self):
        self.rig.layer.setting["detail_text"] = True
        self.show([self.mod, self.ret], "mod-z0001")
        self.assertTrue(self.rig.last(self.t.review_item_text).startswith("Has Desk moved here?"))
        attr = json.loads(self.rig.last(self.t.review_pending_attr))
        self.assertIn("Remove Armchair? It has not been used for 4 weeks.", attr["items"][1]["text"])

    def test_choosing_radar_moved_by_label_reaches_the_core_as_the_item(self):
        self.show([self.radar, self.mod], None)
        self.rig.client.fire_message(SELECT_SET, "Radar moved")
        self.rig.client.fire_message(SELECT_SET, "Retire D")                  # not open: the label is unknown, the select is repaired
        self.rig.ha.process()
        from radar_zone_learner.shell.p4_types import ReviewSelect
        sel = [e for e in self.rig.learner.events if isinstance(e, ReviewSelect)]
        self.assertEqual([e.item for e in sel], ["radar_moved"])
        self.assertEqual(self.rig.layer.unknown_labels, 1)

    def test_an_answer_for_radar_moved_carries_the_items_revision_and_a_stale_one_never_reaches_the_core(self):
        self.show([self.radar], "radar_moved")
        tap(self.rig, "radar_moved", "approve", 3)
        tap(self.rig, "radar_moved", "reject", 2)                              # an older notification
        ans = [e for e in self.rig.learner.events if isinstance(e, ReviewAnswer)]
        self.assertEqual([(e.item, e.op, e.rev) for e in ans], [("radar_moved", "approve", 3)])
        self.assertEqual(self.rig.layer.answers_ignored, 1)
        evs = [json.loads(p[1]) for p in self.rig.published(self.t.review_event)]
        self.assertEqual([(e["event_type"], e["reason"], e["item"]) for e in evs], [("answer_ignored", "stale", "radar_moved")])

    def test_the_selected_item_is_answered_when_the_answer_names_none(self):
        self.show([self.ret], "ret-z0002")
        self.rig.client.fire_message(ANSWER, json.dumps({"op": "reject"}))
        ans = [e for e in self.rig.learner.events if isinstance(e, ReviewAnswer)]
        self.assertEqual([(e.item, e.op, e.rev) for e in ans], [("ret-z0002", "reject", 1)])

    def test_a_core_refusal_zone_busy_is_a_review_event_with_its_reason(self):
        from radar_zone_learner.core.types import AnswerIgnored
        self.show([self.ret], "ret-z0002")
        self.rig.feed(AnswerIgnored("ret-z0002", "approve", "zone_busy"))
        evs = [json.loads(p[1]) for p in self.rig.published(self.t.review_event)]
        self.assertEqual(evs[-1], {"reason": "zone_busy", "op": "approve", "item": "ret-z0002", "event_type": "answer_ignored"})

    def test_an_attention_request_is_published_as_the_kind_attention_with_no_text(self):
        self.show([self.radar], "radar_moved")
        self.rig.feed(PushRequest(4, 3, "daily", 1, None, None, (), True))
        self.rig.run(2)
        self.assertEqual(self.rig.last(self.t.review_push), "4|3|attention|1||||")
        self.rig.feed(PushRequest(5, 4, "daily", 2, "mod-z0001", 2, (), False))
        self.assertEqual(self.rig.last(self.t.review_push), "5|4|daily|2|2|mod-z0001||")

    def test_a_retire_item_is_never_the_head_the_core_decides_and_the_shell_writes_what_it_is_given(self):
        # the planner is the core's; the shell must not alter a head it was given
        self.show([self.ret, self.mod], None)
        self.rig.feed(PushRequest(2, 1, "daily", 2, "mod-z0001", 2, (), False))
        self.assertIn("|mod-z0001|", self.rig.last(self.t.review_push))

    def test_every_published_text_of_the_new_items_fits_a_state(self):
        self.show([self.radar, self.mod, self.ret], None)
        self.assertLessEqual(len(self.rig.last(self.t.review_item_text)), 255)
        self.assertLessEqual(len(self.rig.last(self.t.review_pending_attr).encode("ascii")), 8000)


class FakeLearnerSurfaceTest(unittest.TestCase):
    """The fake has the P7 surface of the interface (A.3) so that a test can hand it to code that calls it."""

    def test_the_accessors(self):
        L = FakeLearner()
        self.assertFalse(L.p7on)
        self.assertEqual((L.drift_view(), L.moved_from_count()), ((), 0))
        self.assertEqual(L.radar_moved_state().state, "none")
        L.p7_enabled = True
        L.p7_drift_row("z0001", state="falling", ratio=0.4, lo=2)
        self.assertEqual([(d.id, d.state, d.ratio, d.lo) for d in L.drift_view()], [("z0001", "falling", 0.4, 2)])
        L.reset_maps()
        self.assertEqual(L.p7_resets, 1)
        self.assertEqual(set(L.reset_summary()), {"grids_nonzero", "candidates", "stays", "door_events", "ghost_events"})


if __name__ == "__main__":
    unittest.main()
