"""The real core behind the real service and P5Layer with FakeHa: the planner, the layer and the blueprint's protocol
together (the planner double of ``test_p5_push_flow`` replaced by the real ``Learner``; P5 spec 6.2, 6.5, 11.5).

What the two halves test on their own is not repeated: ``test_p5_notify`` (the planner) and ``test_p5_push_flow`` (the
shell and the blueprint against a double). Here: one setup notification for each of two phones, nothing repeated after an
add-on restart, a tap on a notification action reaching the real answer path, and a snoozed bed item that comes back
without a second push (D41).
"""
import json
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import FixedOffsetTime
from tests.fake_ha import TAG
from tests.p5_harness import P5Harness
from tests.test_extraction import BASE_MS, DAY
from tests.test_p5_items import BED_ITEM, places
from tests.test_p5_push_flow import ANSWER, PUSH, FlowBase

EVENT = "radar_zones/review/event"


def snapshot(self):
    """What ``FlowBase.restart_addon`` asks the learner for: its exported state, through a JSON round trip."""
    return json.loads(json.dumps(self.export_state()))


class _Real(Learner):
    snapshot = snapshot


def real_learner(saved=None):
    if saved is not None:
        return _Real(P, FixedOffsetTime(0), saved)
    L = places(3)                                                    # night 7: the bed item and three places
    L.__class__ = _Real
    return L


class RealPlannerFlowTest(FlowBase, unittest.TestCase):
    def make_learner(self, saved=None):
        return real_learner(saved)

    def setUp(self):
        super().setUp()
        self.clock.wall_ms = BASE_MS + 7 * DAY * 1000 + (7 * 60 + 59) * 60000 + 30000        # 07:59:30 local, night 7

    def at(self, day, hh, mm=0):
        return BASE_MS + day * DAY * 1000 + (hh * 60 + mm) * 60000

    def to(self, day, hh, mm=0):
        """The shared clock moves to that local time (a day or more at once is one long step, as after a long pause)."""
        self.clock.wall_ms = self.at(day, hh, mm)
        self.advance(5)

    def pushes(self):
        return [s for s in self.push_states() if s.split("|")[0] != "0"]

    def setup_push_for_two_phones(self):
        a = self.make_ha(phone="phoneA")
        b = self.make_ha(phone="phoneB", start_offset_s=3.0)
        self.start_world()
        self.advance(60)                                              # crosses 08:00 (at +30 s), the 15 s delay and the acks
        return a, b

    def test_one_setup_notification_for_each_phone_and_the_real_acks(self):
        a, b = self.setup_push_for_two_phones()
        for ha in (a, b):
            self.assertEqual(len(ha.sends), 1, ha.phone)
            self.assertEqual(ha.visible[TAG]["message"], "Radar setup review is ready (4 items)")
        p = self.learner.p5.push
        self.assertEqual((p["seq"], p["acked"], p["kind"]), (1, 1, "setup"))
        self.assertEqual(sorted(p["acks"]), sorted([a.me, b.me]))
        self.assertEqual(self.pushes()[-1].split("|")[:6], ["1", "1", "setup", "4", "1", self.learner.p5.push["head_id"]])

    def test_an_addon_restart_after_the_acks_sends_nothing_again(self):
        a, b = self.setup_push_for_two_phones()
        self.restart_addon()
        self.advance(40)
        self.assertEqual([len(a.sends), len(b.sends)], [1, 1])
        self.assertEqual(self.learner.p5.push["seq"], 1)
        self.assertTrue(self.learner.p5.push["setup_sent"])

    def test_a_tap_on_approve_reaches_the_real_answer_path_and_a_second_tap_is_ignored(self):
        a, b = self.setup_push_for_two_phones()
        head = self.learner.p5.push["head_id"]
        approve = a.visible[TAG]["data"]["actions"][0]["action"]
        self.assertTrue(approve.startswith("RZ_APPROVE_") and approve.endswith("_" + head))
        a.phone_tap(approve)
        self.advance(3)
        self.assertEqual(len(self.learner.zones()), 1)                # the place is a confirmed zone now
        self.assertNotIn(head, [i.id for i in self.learner.review.items(self.learner)])
        self.client.published.clear()
        a.phone_tap(approve)                                          # the same tap again, a new context id
        self.advance(3)
        self.assertEqual(len(self.learner.zones()), 1)
        events = [json.loads(p[1]) for p in self.client.published_to(EVENT)]
        self.assertEqual([e["event_type"] for e in events], ["answer_ignored"])
        self.assertIn(events[0]["reason"], ("no_such_item", "stale", "not_pending"))

    def test_a_snoozed_bed_item_comes_back_the_next_day_without_a_second_push(self):
        a, b = self.setup_push_for_two_phones()
        rev = [i.rev for i in self.learner.review.items(self.learner) if i.id == BED_ITEM][0]
        self.client.fire_message(ANSWER, json.dumps({"op": "later", "item": BED_ITEM, "rev": rev}))
        self.advance(3)
        self.assertNotIn(BED_ITEM, [i.id for i in self.learner.review.items(self.learner)])
        self.to(8, 9)                                                 # the next day: the nightly run ends the snooze
        self.assertIn(BED_ITEM, [i.id for i in self.learner.review.items(self.learner)])
        self.assertEqual(self.learner.p5.push["seq"], 1)              # listed again, never announced a second time
        self.assertEqual([len(a.sends), len(b.sends)], [1, 1])


if __name__ == "__main__":
    unittest.main()
