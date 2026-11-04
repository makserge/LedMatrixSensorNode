"""P5Layer with the FakeLearner (spec 3.2, 2.6, 8.5, 8.8, 6.4, 7.2, 7.3-7.5): the pipeline, the publishing order, events,
republish, bind, the box, the time entities, the settings, the forget confirmation, one layer object."""
import json
import unittest

from radar_zone_learner.core.types import (AdvancedChanged, AdvancedEntry, AnswerIgnored, ContextState, LuxSample,
                                           MarkResult, NightLearningChanged, NightTimeEdit, NodeCommand, NodeStatus,
                                           PushRequest,
                                           ReviewAnswer, ReviewChanged, ReviewItem, ScheduleState, SettingSwitch,
                                           StatusChanged)
from radar_zone_learner.shell.p4 import P4Layer
from radar_zone_learner.shell.p5 import P5Layer
from tests.fakes import FakeLearner, FakeNode
from tests.p5_harness import P5Harness, bed_item, changed, place_item, window_item

ANSWER = "radar_zones/review/answer"
EVENT = "radar_zones/review/event"
SELECT_STATE = "radar_zones/review/select"
SELECT_CFG = "homeassistant/select/radar_zones_review_select/config"
PENDING = "radar_zones/review/pending"
PENDING_ATTR = "radar_zones/review/pending/attr"
ITEM_TEXT = "radar_zones/review/item_text"
PUSH = "radar_zones/review/push"
PUSH_MAP = "radar_zones/review/push_map"
MAP = "radar_zones/review/map"
NW_START = "radar_zones/review/nw_start"
NW_END = "radar_zones/review/nw_end"
FORGET_TEXT = "radar_zones/learner/forget_confirm"
FORGET_TEXT_SET = "radar_zones/learner/forget_confirm/set"
FORGET_ALL = "radar_zones/learner/forget_all"
SETTINGS = {"approve_sched": "radar_zones/review/approve_sched", "guests_pause": "radar_zones/learner/guests_pause",
            "pets": "radar_zones/learner/pets", "detail_text": "radar_zones/learner/detail_text"}


def answers(learner):
    return [e for e in learner.events if isinstance(e, ReviewAnswer)]


class LayerBase(P5Harness):
    def make_learner(self):
        return FakeLearner()

    def items(self, selected="s0005-1", extra=()):
        self.learner.tick_outputs = [changed([window_item(rev=3), bed_item(), place_item("s0005-1", 2, "A"),
                                              place_item("s0006-1", 1, "B"), *extra], selected)]
        self.run_for(1)

    def review_events(self):
        return [json.loads(p[1]) for p in self.published(EVENT)]

    def send(self, **obj):
        self.client.fire_message(ANSWER, json.dumps(obj))
        self.pump()


class PipelineTest(LayerBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.items()
        self.client.published.clear()

    def test_a_current_answer_reaches_the_core_with_the_mirrors_revision(self):
        self.send(op="approve", item="s0005-1", rev=2)
        got = answers(self.learner)
        self.assertEqual([(a.item, a.op, a.rev, a.sched, a.name, a.aid) for a in got],
                         [("s0005-1", "approve", 2, False, None, None)])
        self.assertEqual(self.review_events(), [])

    def test_the_buttons_fill_the_selected_item_and_its_revision(self):
        self.send(op="reject")
        self.send(op="rename")
        self.send(op="later")
        self.assertEqual([(a.item, a.op, a.rev) for a in answers(self.learner)],
                         [("s0005-1", "reject", 2), ("s0005-1", "rename", 2), ("s0005-1", "later", 2)])

    def test_name_and_aid_travel_to_the_core(self):
        self.send(op="rename", item="s0006-1", rev=1, name="Desk", aid="ctx1")
        a = answers(self.learner)[0]
        self.assertEqual((a.item, a.rev, a.name, a.aid), ("s0006-1", 1, "Desk", "ctx1"))

    def test_a_stale_revision_never_reaches_the_core_and_is_an_event(self):
        self.send(op="approve", item="s0005-1", rev=1)
        self.assertEqual(answers(self.learner), [])
        self.assertEqual(self.review_events(), [{"reason": "stale", "op": "approve", "item": "s0005-1",
                                          "event_type": "answer_ignored"}])
        self.assertEqual(self.published(EVENT)[0][2:], (1, False))                   # not retained
        self.assertEqual(self.layer.answers_ignored, 1)
        self.assertEqual(self.layer.dropped_answers, 1)

    def test_an_item_that_is_not_open_is_an_event_not_silence(self):
        self.send(op="approve", item="s0099-1", rev=1)
        self.send(op="later", item="mrg-s0005-1")
        self.assertEqual(answers(self.learner), [])
        self.assertEqual([(e["reason"], e["item"]) for e in self.review_events()],
                         [("no_such_item", "s0099-1"), ("no_such_item", "mrg-s0005-1")])

    def test_no_item_selected_and_none_given_is_no_such_item_with_an_empty_item(self):
        self.learner.tick_outputs = [changed([], None)]
        self.run_for(1)
        self.client.published.clear()
        self.send(op="approve")
        self.assertEqual(self.review_events(), [{"reason": "no_such_item", "op": "approve", "item": "",
                                          "event_type": "answer_ignored"}])

    def test_the_answer_id_ring_drops_a_repeat_silently(self):
        self.send(op="approve", item="s0005-1", rev=2, aid="ctx1")
        self.send(op="approve", item="s0005-1", rev=2, aid="ctx1")                   # a QoS-1 re-delivery
        self.assertEqual(len(answers(self.learner)), 1)
        self.assertEqual(self.review_events(), [])
        self.assertEqual(self.layer.attributes()["answer_ids"], 1)

    def test_the_ring_holds_64_ids(self):
        for n in range(70):
            self.send(op="later", item="s0005-1", rev=2, aid="a%d" % n)
        self.assertEqual(len(answers(self.learner)), 70)
        self.assertEqual(self.layer.attributes()["answer_ids"], 64)
        self.send(op="later", item="s0005-1", rev=2, aid="a69")                      # still remembered
        self.send(op="later", item="s0005-1", rev=2, aid="a0")                       # forgotten: handled again
        self.assertEqual(len(answers(self.learner)), 71)

    def test_an_answer_without_an_id_is_never_dropped_as_a_repeat(self):
        self.send(op="later", item="s0005-1", rev=2)
        self.send(op="later", item="s0005-1", rev=2)
        self.assertEqual(len(answers(self.learner)), 2)

    def test_forget_is_passed_without_an_item_check(self):
        self.send(op="forget")
        self.assertEqual([(a.op, a.item) for a in answers(self.learner)], [("forget", None)])

    def test_undo_is_passed_without_an_open_item_but_needs_its_item(self):
        self.send(op="undo", item="night_window")
        self.send(op="undo")
        self.assertEqual([(a.op, a.item) for a in answers(self.learner)], [("undo", "night_window")])
        self.assertEqual([(e["reason"], e["op"]) for e in self.review_events()], [("no_item", "undo")])

    def test_advanced_needs_an_entry_in_the_mirror(self):
        self.learner.tick_outputs = [AdvancedChanged((AdvancedEntry("adv:undo:bed", "undo", "bed", "bed", "", "", "", 0),))]
        self.run_for(1)
        self.client.published.clear()
        self.send(op="advanced")                                                       # nothing selected
        self.send(op="advanced", item="adv:remove:z0009")                              # not in the mirror
        self.assertEqual(answers(self.learner), [])
        self.assertEqual([e["reason"] for e in self.review_events()], ["no_such_item", "no_such_item"])
        self.send(op="advanced", item="adv:undo:bed")
        self.assertEqual([(a.op, a.item) for a in answers(self.learner)], [("advanced", "adv:undo:bed")])

    def test_advanced_uses_the_selected_entry(self):
        self.learner.tick_outputs = [AdvancedChanged((AdvancedEntry("adv:undo:bed", "undo", "bed", "bed", "", "", "", 0),))]
        self.run_for(1)
        self.client.fire_message("radar_zones/review/advanced/set", "Undo: No bed in view")
        self.send(op="advanced", name="x")
        a = answers(self.learner)[0]
        self.assertEqual((a.op, a.item, a.name), ("advanced", "adv:undo:bed", "x"))

    def test_the_core_has_the_last_word_a_core_refusal_is_the_same_event(self):
        self.learner.tick_outputs = [AnswerIgnored("s0005-1", "approve", "zone_limit")]
        self.run_for(1)
        self.assertEqual(self.review_events(), [{"reason": "zone_limit", "op": "approve", "item": "s0005-1",
                                          "event_type": "answer_ignored"}])
        self.learner.tick_outputs = [AnswerIgnored(None, "edit", "no_such_item")]
        self.run_for(1)
        self.assertEqual(self.review_events()[-1], {"reason": "no_such_item", "op": "edit", "item": "",
                                             "event_type": "answer_ignored"})

    def test_the_selected_item_follows_the_core_not_the_select_command(self):
        self.client.fire_message("radar_zones/review/select/set", "Place B")
        self.pump()
        self.assertEqual(self.layer.selected, "s0005-1")                               # still: the core has not said so
        self.send(op="approve")
        self.assertEqual(answers(self.learner)[0].item, "s0005-1")
        self.items(selected="s0006-1")
        self.send(op="approve")
        self.assertEqual(answers(self.learner)[-1].item, "s0006-1")

    def test_the_approve_box_is_ored_into_sched_for_the_night_window_only(self):
        self.learner.tick_outputs = [StatusChanged("learning", {"settings": {"approve_sched": True, "guests_pause": False,
                                                                             "pets": False, "detail_text": False}})]
        self.run_for(1)
        self.assertEqual(self.last(SETTINGS["approve_sched"]), "ON")
        self.send(op="approve", item="night_window", rev=3)
        self.send(op="approve", item="s0005-1", rev=2)
        self.send(op="approve", item="night_window", rev=3, sched=True)
        self.assertEqual([(a.item, a.sched) for a in answers(self.learner)],
                         [("night_window", True), ("s0005-1", False), ("night_window", True)])

    def test_the_box_ticks_once_the_core_publishes_off_after_the_approve(self):
        on = {"approve_sched": True, "guests_pause": False, "pets": False, "detail_text": False}
        off = dict(on, approve_sched=False)
        self.learner.tick_outputs = [StatusChanged("learning", {"settings": on})]
        self.run_for(1)

        def respond(ev):
            if isinstance(ev, ReviewAnswer) and ev.op == "approve" and ev.item == "night_window" and ev.sched:
                return [StatusChanged("learning", {"settings": off})]
            return []
        self.learner.responder = respond
        self.send(op="approve", item="night_window", rev=3)
        self.assertEqual(self.last(SETTINGS["approve_sched"]), "OFF")
        self.send(op="approve", item="night_window", rev=3)                            # the box is off now
        self.assertFalse(answers(self.learner)[-1].sched)


class PublishingOrderTest(LayerBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.run_for(12)
        self.client.published.clear()
        self._n = 0

    def vary_map(self):
        """The FakeLearner's map ignores the selected item: make each one a different image."""
        from tests.fakes import empty_debug_map
        self._n += 1
        self.learner.p5_map = empty_debug_map(highlight=frozenset(range(100 * self._n, 100 * self._n + 9)))

    def order(self, wanted):
        topics = [p[0] for p in self.client.published]
        idx = [topics.index(t) for t in wanted]
        self.assertEqual(idx, sorted(idx), topics)

    def test_the_order_of_a_review_change(self):
        self.vary_map()
        self.learner.tick_outputs = [changed([window_item(), place_item()], "s0005-1"),
                                    PushRequest(1, 0, "setup", 2, "s0005-1", 1, ())]
        self.run_for(1)
        self.order([MAP, SELECT_CFG, SELECT_STATE, PENDING_ATTR, PENDING, ITEM_TEXT, NW_START, PUSH_MAP, PUSH])
        self.assertEqual(self.last(SELECT_STATE), "Place A")
        self.assertEqual(self.last(PENDING), "2")
        self.assertEqual(self.last(PUSH), "1|0|setup|2|1|s0005-1||")

    def test_the_select_config_only_when_the_options_changed(self):
        self.learner.tick_outputs = [changed([window_item(), place_item()], "s0005-1")]
        self.run_for(1)
        self.client.published.clear()
        self.learner.tick_outputs = [changed([window_item(), place_item(conf=0.9)], "s0005-1")]
        self.run_for(1)
        self.assertEqual(self.published(SELECT_CFG), [])
        self.assertEqual(len(self.published(PENDING_ATTR)), 0)               # nothing visible changed (conf is not shown)

    def test_the_map_waits_one_second_but_the_states_do_not(self):
        self.vary_map()
        self.learner.tick_outputs = [changed([window_item()], "night_window")]
        self.run_for(1)
        self.assertEqual(len(self.published(MAP)), 1)
        self.client.published.clear()
        self.clock.advance(0.2)
        self.vary_map()
        self.layer._review_changed(ReviewChanged((window_item(), place_item()), "s0005-1"))
        self.assertEqual(self.published(MAP), [])                            # debounced: less than 1 s since the last
        self.assertEqual(self.last(SELECT_STATE), "Place A")                 # the states are not
        self.assertEqual(self.last(PENDING), "2")
        self.clock.advance(0.9)
        self.layer.tick(self.clock.clock_ms())
        self.assertEqual(len(self.published(MAP)), 1)
        self.layer.tick(self.clock.clock_ms())
        self.assertEqual(len(self.published(MAP)), 1)                        # once

    def test_a_debug_map_change_marks_the_base_dirty(self):
        from radar_zone_learner.core.types import DebugMapChanged
        self.client.published.clear()
        self.learner.tick_outputs = [DebugMapChanged("review")]
        self.run_for(1)
        self.assertEqual(len(self.published(MAP)), 0)                        # same bytes: not published twice
        self.assertEqual(self.learner.map_calls[-1], (None, False))
        self.assertEqual(len(self.published("radar_zones/learner/debug_map")), 1)    # the service handled it too

    def test_the_push_map_comes_before_the_push_state_and_only_for_a_new_seq(self):
        self.learner.tick_outputs = [changed([window_item(), place_item()], "s0005-1"),
                                    PushRequest(1, 0, "setup", 2, "s0005-1", 1, ())]
        self.run_for(1)
        self.assertEqual(self.learner.map_calls[-1], ("s0005-1", False))     # the head item, no markers
        n = len(self.published(PUSH_MAP))
        self.learner.tick_outputs = [PushRequest(1, 1, "setup", 2, "s0005-1", 1, ("pa1b2c3d",))]
        self.run_for(1)
        self.assertEqual(len(self.published(PUSH_MAP)), n)                   # acked: no new map
        self.assertEqual(self.last(PUSH), "1|1|setup|2|1|s0005-1|pa1b2c3d|")
        self.learner.tick_outputs = [PushRequest(1, 1, "setup", 1, "s0005-1", 1, ("pa1b2c3d",))]
        self.run_for(1)
        self.assertEqual(len(self.published(PUSH_MAP)), n)                   # the count fell: no new map
        self.assertEqual(self.last(PUSH), "1|1|setup|1|1|s0005-1|pa1b2c3d|")

    def test_a_request_without_a_head_shows_the_map_without_a_highlight(self):
        from radar_zone_learner.shell.png import encode_png
        from radar_zone_learner.shell.render import render_review
        from tests.fakes import empty_debug_map
        self.learner.p5_map = empty_debug_map(highlight=frozenset({10, 11}), focus=(1.0, 2.0))
        self.learner.tick_outputs = [PushRequest(1, 0, "daily", 2, None, None, ())]
        self.run_for(1)
        self.assertEqual(self.learner.map_calls[-1], (None, False))
        self.assertEqual(self.layer.maps.push_png, encode_png(*render_review(empty_debug_map())))   # no highlight, no marker
        plain = self.layer.maps.push_png
        self.learner.tick_outputs = [PushRequest(2, 0, "daily", 2, "s0005-1", 1, ())]
        self.run_for(1)
        self.assertNotEqual(self.layer.maps.push_png, plain)               # with a head the highlight is drawn
        self.assertEqual(self.published(PUSH_MAP)[-1][1], self.layer.maps.push_png)
        self.assertEqual(self.last(PUSH), "2|0|daily|2|1|s0005-1||")

    def test_the_push_map_carries_no_dwell_heat_but_the_review_map_does(self):
        # the image leaves the home network on its way to the phone: geometry and the highlighted head only (spec 6.7)
        from radar_zone_learner.shell.png import encode_png
        from radar_zone_learner.shell.render import render_review
        from tests.fakes import empty_debug_map
        hot = tuple([0.0] * 700 + [5.0] * 40 + [0.0] * 2060)
        self.learner.p5_map = empty_debug_map(dwell_rate=hot, highlight=frozenset({1500, 1501}), focus=(0.1, 3.0))
        self.learner.tick_outputs = [PushRequest(1, 0, "setup", 2, "s0005-1", 1, ())]
        self.run_for(1)
        cold = empty_debug_map(highlight=frozenset({1500, 1501}), focus=(0.1, 3.0))
        self.assertEqual(self.layer.maps.push_png, encode_png(*render_review(cold)))
        self.assertNotEqual(self.layer.maps.push_png, encode_png(*render_review(self.learner.p5_map)))
        self.assertEqual(self.published(PUSH_MAP)[-1][1], self.layer.maps.push_png)

    def test_the_pending_count_is_not_retained_wrongly(self):
        self.learner.tick_outputs = [changed([window_item(), place_item()], "s0005-1")]
        self.run_for(1)
        for topic in (SELECT_STATE, PENDING, PENDING_ATTR, ITEM_TEXT, PUSH):
            for p in self.published(topic):
                self.assertEqual(p[2:], (1, True), topic)
        for p in self.published(MAP) + self.published(PUSH_MAP):
            self.assertEqual(p[2:], (0, False))


class PendingAttributesTest(LayerBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connect()

    def attr(self):
        return json.loads(self.last(PENDING_ATTR))

    def test_rows_labels_tiers_and_more(self):
        self.learner.tick_outputs = [StatusChanged("learning", {"review": {"more": 3}}),
                                    changed([window_item(rev=3), bed_item(), place_item("s0005-1", 2, "A")], "s0005-1")]
        self.run_for(1)
        attr = self.attr()
        self.assertEqual(attr["selected"], "s0005-1")
        self.assertEqual(attr["more"], 3)
        self.assertEqual([(i["id"], i["rev"], i["type"], i["tier"], i["label"]) for i in attr["items"]],
                         [("night_window", 3, "night_window", 1, "Night window"),
                          ("bed_needs_data", 1, "bed_needs_data", 1, "Bed zone"),
                          ("s0005-1", 2, "dwell_zone", 2, "Place A")])
        self.assertTrue(attr["items"][2]["text"].startswith("New place A:"))
        self.assertEqual(self.last(ITEM_TEXT), attr["items"][2]["text"])

    def test_more_is_republished_when_the_status_changes_it(self):
        self.items()
        self.learner.tick_outputs = [StatusChanged("learning", {"review": {"more": 2}})]
        self.run_for(1)
        self.assertEqual(self.attr()["more"], 2)

    def test_at_most_fifteen_items_and_8000_bytes(self):
        long_text = "−" * 255
        many = [ReviewItem("s%04d-1" % k, 1, "dwell_zone", {"tag": "T%d" % k, "kind": "dwell", "x_cm": 1, "y_cm": 1}, 2)
                for k in range(1, 15)]
        nw = ReviewItem("night_window", 1, "night_window", {"text": long_text}, 1)
        self.learner.tick_outputs = [changed([nw, bed_item()] + many, "night_window")]
        self.run_for(1)
        payload = self.published(PENDING_ATTR)[-1][1]
        self.assertLessEqual(len(payload), 8000)
        self.assertEqual(len(self.attr()["items"]), 15)
        self.assertEqual(self.last(PENDING), "16")                                   # all open items are counted
        self.assertLessEqual(len(self.last(ITEM_TEXT)), 255)

    def test_item_text_is_cut_at_255(self):
        nw = ReviewItem("night_window", 1, "night_window", {"text": "x" * 400}, 1)
        self.learner.tick_outputs = [changed([nw], "night_window")]
        self.run_for(1)
        self.assertEqual(len(self.last(ITEM_TEXT)), 255)
        self.assertTrue(self.last(ITEM_TEXT).endswith("…"))

    def test_the_empty_state(self):
        self.assertEqual(self.last(SELECT_STATE), "No item")
        self.assertEqual(self.last(PENDING), "0")
        self.assertEqual(self.last(ITEM_TEXT), "No item")
        self.assertEqual(self.last(PENDING_ATTR), '{"selected":null,"items":[],"more":0}')
        self.assertEqual(self.last(PUSH), "0|0|none|0||||")
        for topic in (SELECT_STATE, ITEM_TEXT):
            self.assertNotEqual(self.last(topic), "None")                           # HA reads None as unknown (D8)

    def test_selecting_no_item_gives_the_text_no_item(self):
        self.items()
        self.items(selected=None)
        self.assertEqual(self.last(SELECT_STATE), "No item")
        self.assertEqual(self.last(ITEM_TEXT), "No item")
        self.assertEqual(self.attr()["selected"], None)

    def test_a_selected_id_that_is_not_listed_is_no_item(self):
        self.items(selected="s9999-9")
        self.assertEqual(self.last(SELECT_STATE), "No item")

    def test_the_bed_text_follows_the_nodes_window(self):
        node = self.connect_node = FakeNode(self.client, sched=True, start="22:00:00", end="07:00:00")
        node.announce()
        self.pump(node)
        self.learner.tick_outputs = [changed([bed_item()], "bed_needs_data")]
        self.run_for(1)
        self.assertIn("before 22:00 or after 07:00", self.last(ITEM_TEXT))
        node.user_sets_schedule(False)
        self.pump(node)
        self.assertNotIn("before", self.last(ITEM_TEXT))
        self.assertNotIn("before", self.attr()["items"][0]["text"])


class EventsTest(LayerBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connect()

    def test_the_p4_events_still_work(self):
        self.svc._execute(MarkResult(False, "several_stays", None, None))
        self.assertEqual(self.review_events(), [{"reason": "several_stays", "event_type": "mark_failed"}])

    def test_forgotten_and_forget_refused(self):
        self.layer.notify_forgotten()
        self.assertEqual(self.review_events()[-1], {"event_type": "forgotten"})

    def test_the_event_types_of_the_discovery_config(self):
        cfg = [json.loads(p[1]) for p in self.published("homeassistant/event/radar_zones_review_event/config")][-1]
        self.assertEqual(cfg["event_types"], ["mark_failed", "device_not_confirmed", "restore_failed",
                                              "answer_ignored", "forget_refused", "forgotten"])

    def test_events_wait_for_the_broker_link_and_keep_their_order(self):
        self.client.fire_disconnect()
        self.send(op="approve", item="s0099-1")
        self.svc._execute(MarkResult(False, "no_stay", None, None))
        self.assertEqual(self.published(EVENT), [])
        self.client.fire_connect(0)
        self.pump()
        self.assertEqual([e["event_type"] for e in self.review_events()], ["answer_ignored", "mark_failed"])


class RepublishTest(LayerBase, unittest.TestCase):
    def test_the_order_of_spec_8_8(self):
        self.learner.p5_items = (window_item(), place_item())
        self.learner.p5_selected = "s0005-1"
        self.connect()
        topics = [p[0] for p in self.client.published if not p[0].endswith("/config")]
        wanted = ["radar_zones/radar/valid", "radar_zones/learner/night_learning", SETTINGS["approve_sched"],
                  SETTINGS["guests_pause"], SETTINGS["pets"], SETTINGS["detail_text"],
                  "radar_zones/learner/debug_capture", "radar_zones/review/live", FORGET_TEXT, MAP, SELECT_STATE,
                  "radar_zones/review/advanced", NW_START, NW_END, PENDING_ATTR, PENDING, ITEM_TEXT, PUSH_MAP, PUSH,
                  "radar_zones/review/you_are_in", "radar_zones/review/you_are_in/attr", "radar_zones/scan/rf_on_24h"]
        idx = [topics.index(t) for t in wanted]
        self.assertEqual(idx, sorted(idx), [(t, topics.index(t)) for t in wanted])
        # the burst first, then the status, then the layer's states
        order = [p[0] for p in self.client.published]
        self.assertLess(max(i for i, t in enumerate(order) if t.endswith("/config")), order.index("radar_zones/radar/valid"))

    def test_the_bind_seeds_the_first_republish_so_the_real_state_goes_out_not_the_defaults(self):
        self.learner.p5_items = (window_item(rev=3), place_item("s0005-1", 2, "A"))
        self.learner.p5_selected = "s0005-1"
        self.learner.p5_advanced = (AdvancedEntry("adv:undo:bed", "undo", "bed", "bed", "", "", "", 0),)
        self.learner.p5_push = PushRequest(7, 7, "setup", 2, "s0005-1", 2, ("pa1b2c3d",))
        self.svc.start()
        self.client.fire_connect(0)
        self.svc.step(0)                                               # the first republish, before any core step
        self.assertEqual(self.last(SELECT_STATE), "Place A")
        self.assertEqual(self.last(PENDING), "2")
        self.assertEqual(self.last(PUSH), "7|7|setup|2|2|s0005-1|pa1b2c3d|")
        self.assertEqual([p[1] for p in self.published(PUSH)], [b"7|7|setup|2|2|s0005-1|pa1b2c3d|"])   # never the default
        cfg = [json.loads(p[1]) for p in self.published(SELECT_CFG)]
        self.assertEqual(cfg[-1]["options"], ["No item", "Night window", "Place A"])
        adv = [json.loads(p[1]) for p in self.published("homeassistant/select/radar_zones_review_advanced/config")]
        self.assertEqual(adv[-1]["options"], ["No entry", "Undo: No bed in view"])
        # the core's own first emissions equal the mirrors and publish nothing new
        self.client.published.clear()
        self.learner.tick_outputs = [ReviewChanged(self.learner.p5_items, "s0005-1"),
                                    AdvancedChanged(self.learner.p5_advanced), self.learner.p5_push]
        self.run_for(1)
        for topic in (SELECT_STATE, PENDING, PENDING_ATTR, PUSH, PUSH_MAP, SELECT_CFG):
            self.assertEqual(self.published(topic), [], topic)

    def test_an_ha_birth_republishes_everything_and_the_burst_again(self):
        self.connect()
        self.items()
        self.client.published.clear()
        self.client.fire_message("homeassistant/status", "online")
        self.pump()
        self.run_for(3)
        configs = [p[0] for p in self.client.published if p[0].endswith("/config")]
        self.assertEqual(len(configs), 35)
        self.assertEqual(self.last(SELECT_STATE), "Place A")
        self.assertEqual(self.last(PENDING), "4")
        self.assertEqual(json.loads(self.published(SELECT_CFG)[-1][1])["options"],
                         ["No item", "Night window", "Bed zone", "Place A", "Place B"])
        self.assertEqual(self.last(PUSH), "0|0|none|0||||")

    def test_the_second_pass_ten_seconds_after_a_burst(self):
        self.connect()
        self.items()
        self.client.published.clear()
        self.layer.second_pass(self.clock.clock_ms())
        topics = [p[0] for p in self.client.published]
        self.assertEqual(topics, [MAP, PUSH_MAP, SELECT_STATE, "radar_zones/review/advanced",
                                  "radar_zones/review/you_are_in", "radar_zones/review/you_are_in/attr", FORGET_TEXT])
        self.assertEqual(self.last(SELECT_STATE), "Place A")

    def test_every_not_retained_state_is_sent_again_in_the_second_pass(self):
        self.connect()
        self.client.published.clear()
        self.run_for(11)                                              # the service's own timer
        self.assertIn(MAP, [p[0] for p in self.client.published])
        self.assertIn("radar_zones/review/you_are_in", [p[0] for p in self.client.published])

    def test_the_layer_is_the_only_one_and_a_node_command_is_sent_once(self):
        def respond(ev):
            if isinstance(ev, ReviewAnswer) and ev.op == "approve" and ev.item == "night_window":
                return [NodeCommand("w1", "window", sched=None, start_min=1410, end_min=405, reason="approved_window")]
            return []
        self.learner.responder = respond
        node = self.connect(sched=True)
        self.learner.tick_outputs = [changed([window_item(rev=3)], "night_window")]
        self.run_for(1, node)
        self.client.fire_message(ANSWER, json.dumps({"op": "approve"}))
        self.pump(node)
        self.assertEqual(node.received, [("led_matrix_node/radar/night_start", "23:30:00"),
                                         ("led_matrix_node/radar/night_end", "06:45:00")])
        self.assertEqual(self.layer.node.sent, 1)
        self.assertEqual(len(answers(self.learner)), 1)
        self.assertIsInstance(self.layer, P4Layer)
        self.assertIs(self.svc.p4, self.layer)
        # a schedule echo is handled once
        sched = [e for e in self.learner.events if isinstance(e, ScheduleState)]
        self.assertEqual(len(sched), len(set((e.sched, e.start_min, e.end_min, e.recv_ms) for e in sched)))


class TimeEntitiesTest(LayerBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.node = self.connect(sched=True, start="00:00:00", end="06:00:00")

    def test_the_proposal_when_nothing_is_edited(self):
        self.learner.tick_outputs = [changed([window_item(start_min=1410, end_min=405)], "night_window")]
        self.run_for(1, self.node)
        self.assertEqual((self.last(NW_START), self.last(NW_END)), ("23:30:00", "06:45:00"))

    def test_the_edited_value_wins(self):
        self.learner.tick_outputs = [changed([window_item(start_min=1410, end_min=405, edit_start=1425)], "night_window")]
        self.run_for(1, self.node)
        self.assertEqual((self.last(NW_START), self.last(NW_END)), ("23:45:00", "06:45:00"))

    def test_without_a_proposal_the_nodes_window(self):
        self.assertEqual((self.last(NW_START), self.last(NW_END)), ("00:00:00", "06:00:00"))
        self.node.start, self.node.end = "22:15:00", "07:05:00"
        self.client.fire_message("led_matrix_node/radar/night_start/state", "22:15:00", retain=True)
        self.client.fire_message("led_matrix_node/radar/night_end/state", "07:05:00", retain=True)
        self.pump(self.node)
        self.assertEqual((self.last(NW_START), self.last(NW_END)), ("22:15:00", "07:05:00"))

    def test_the_state_is_always_a_valid_time(self):
        fresh = P5Layer(self.topics, lambda *a, **k: True, None)
        sent = {}
        fresh._publish = lambda topic, payload, qos=1, retain=False: sent.setdefault(topic, payload) or True
        fresh._publish_times()
        self.assertEqual((sent[NW_START], sent[NW_END]), ("00:00:00", "06:00:00"))        # before any node state: the default

    def test_a_time_command_reaches_the_core_as_an_edit(self):
        self.client.fire_message("radar_zones/review/nw_start/set", "23:45:00")
        self.client.fire_message("radar_zones/review/nw_end/set", "06:15:00")
        self.client.fire_message("radar_zones/review/nw_end/set", "25:00:00")
        self.pump(self.node)
        self.assertEqual([(e.field, e.minute) for e in self.learner.events if isinstance(e, NightTimeEdit)],
                         [("start", 1425), ("end", 375)])

    def test_the_entities_follow_a_core_edit(self):
        self.learner.tick_outputs = [changed([window_item(start_min=1410, end_min=405)], "night_window")]
        self.run_for(1, self.node)
        self.learner.tick_outputs = [changed([window_item(start_min=1410, end_min=405, edit_start=1425, edit_end=390)],
                                            "night_window")]
        self.run_for(1, self.node)
        self.assertEqual((self.last(NW_START), self.last(NW_END)), ("23:45:00", "06:30:00"))
        self.learner.tick_outputs = [changed([], None)]                                  # approved: the proposal is gone
        self.run_for(1, self.node)
        self.assertEqual((self.last(NW_START), self.last(NW_END)), ("00:00:00", "06:00:00"))


class SettingsTest(LayerBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connect()

    def status(self, **on):
        s = {"approve_sched": False, "guests_pause": False, "pets": False, "detail_text": False}
        s.update(on)
        return StatusChanged("learning", {"settings": s, "review": {"more": 0, "command_pending": False}})

    def test_off_at_the_start(self):
        for topic in SETTINGS.values():
            self.assertEqual(self.published(topic)[0][1:], (b"OFF", 1, True))

    def test_a_command_reaches_the_core_and_the_state_follows_the_status(self):
        for name, topic in (("guests_pause", "radar_zones/learner/guests_pause"), ("pets", "radar_zones/learner/pets"),
                            ("detail_text", "radar_zones/learner/detail_text"),
                            ("approve_sched", "radar_zones/review/approve_sched")):
            with self.subTest(name=name):
                self.client.fire_message(topic + "/set", "ON")
                self.pump()
                self.assertEqual(self.learner.events[-1], SettingSwitch(name, True, self.clock.clock_ms()))
                self.assertEqual(self.last(topic), "OFF")                 # not optimistic: the core's status says
                self.learner.tick_outputs = [self.status(**{name: True})]
                self.run_for(1)
                self.assertEqual(self.last(topic), "ON")
                self.assertEqual(self.published(topic)[-1][1:], (b"ON", 1, True))

    def test_the_settings_are_published_once_per_change(self):
        self.learner.tick_outputs = [self.status(pets=True)]
        self.run_for(1)
        n = len(self.published(SETTINGS["pets"]))
        self.learner.tick_outputs = [self.status(pets=True)]
        self.run_for(1)
        self.assertEqual(len(self.published(SETTINGS["pets"])), n)

    def test_detail_off_has_no_hours_anywhere(self):
        self.learner.tick_outputs = [changed([place_item(hrs_wd=("09-12", "13-17"), hrs_we=("10-11",))], "s0005-1"),
                                    PushRequest(1, 0, "setup", 1, "s0005-1", 1, ())]
        self.run_for(1)
        for topic in (PENDING_ATTR, ITEM_TEXT, PUSH):
            self.assertNotIn("Used mostly", self.last(topic))
            self.assertNotRegex(self.last(topic), r"\d\d-\d\d")
        self.assertEqual(self.last(PUSH), "1|0|setup|1|1|s0005-1||")

    def test_detail_on_adds_the_hours_to_the_texts_and_the_push_state(self):
        self.learner.tick_outputs = [changed([place_item(hrs_wd=("09-12", "13-17"), hrs_we=("09-12", "13-17"))],
                                            "s0005-1"), PushRequest(1, 0, "setup", 1, "s0005-1", 1, ())]
        self.run_for(1)
        self.learner.tick_outputs = [self.status(detail_text=True)]
        self.run_for(1)
        self.assertIn("Used mostly 09-12 h and 13-17 h.", self.last(ITEM_TEXT))
        self.assertIn("Used mostly 09-12 h and 13-17 h.", json.loads(self.last(PENDING_ATTR))["items"][0]["text"])
        push = self.last(PUSH).split("|")
        self.assertEqual(len(push), 8)
        self.assertTrue(push[7].startswith("New place A:"))
        self.assertIn("Used mostly", push[7])
        self.learner.tick_outputs = [self.status(detail_text=False)]
        self.run_for(1)
        self.assertEqual(self.last(PUSH), "1|0|setup|1|1|s0005-1||")
        self.assertNotIn("Used mostly", self.last(ITEM_TEXT))

    def test_the_push_text_is_that_of_the_head_when_the_request_was_made(self):
        self.learner.tick_outputs = [self.status(detail_text=True)]
        self.run_for(1)
        self.learner.tick_outputs = [changed([place_item("s0005-1", 1, "A", hrs_wd=("09-12",)),
                                              place_item("s0006-1", 1, "B")], "s0005-1"),
                                    PushRequest(1, 0, "setup", 2, "s0005-1", 1, ())]
        self.run_for(1)
        self.assertIn("New place A", self.last(PUSH))
        self.learner.tick_outputs = [changed([place_item("s0006-1", 1, "B")], "s0006-1"),
                                    PushRequest(1, 0, "setup", 1, "s0005-1", 1, ())]     # the head was answered meanwhile
        self.run_for(1)
        self.assertIn("New place A", self.last(PUSH))                                      # fixed per seq


class ForgetAfterTheRealNightLearningTest(LayerBase, unittest.TestCase):
    """The guard follows the real core outputs: the learner's own OFF of the node's schedule counts as not put back from
    the user's stop until the echo of ON, also when the status says ``active`` False."""
    def setUp(self):
        super().setUp()
        self.connect()
        self.client.published.clear()

    def feed(self, outs, now_ms):
        for o in outs:
            if isinstance(o, (StatusChanged, NightLearningChanged)):
                self.layer.on_output(o, now_ms)

    def test_a_reset_waits_until_the_schedule_is_back_on(self):
        from radar_zone_learner.core import Learner
        from radar_zone_learner.core.params import DEFAULT_PARAMS
        from radar_zone_learner.core.types import (CommandResult, FixedOffsetTime, NightLearningSwitch)
        t0 = 1_767_571_200_000
        L = Learner(DEFAULT_PARAMS, FixedOffsetTime(0))
        L.handle(NodeStatus(True, t0))
        L.handle(ScheduleState(True, 0, 360, t0 + 10))
        outs = L.handle(NightLearningSwitch(True, t0 + 1000))
        cmd = [o for o in outs if isinstance(o, NodeCommand)][0]
        self.feed(outs, t0 + 1000)
        self.assertTrue(self.layer.nl_active)
        self.feed(L.handle(CommandResult(cmd.token, "schedule", True, None, t0 + 2000)), t0 + 2000)
        self.feed(L.handle(ScheduleState(False, 0, 360, t0 + 2500)), t0 + 2500)         # the echo of OFF
        outs = L.handle(NightLearningSwitch(False, t0 + 5000))                          # the user ends it early
        self.assertFalse(L.nl.active)
        self.assertIs(L.nl.set_by, False)                                               # the OFF is still the learner's
        self.feed(outs, t0 + 5000)
        self.assertFalse(self.layer.nl_active)
        self.assertTrue(self.layer.nl_restoring)
        self.type_text("FORGET")
        self.press()
        self.assertEqual(self.review_events(), [{"reason": "night_learning_active", "event_type": "forget_refused"}])
        self.assertFalse(self.layer.take_forget_request())
        self.feed(L.handle(ScheduleState(True, 0, 360, t0 + 9000)), t0 + 9000)         # the node's schedule is back ON
        self.assertIsNone(L.nl.set_by)
        self.assertFalse(self.layer.nl_restoring)
        self.type_text("FORGET")
        self.press()
        self.assertTrue(self.layer.take_forget_request())

    def type_text(self, text):
        self.client.fire_message("radar_zones/learner/forget_confirm/set", text)
        self.pump()

    def press(self):
        from radar_zone_learner.shell.p5_types import ForgetPress
        self.layer.on_event(ForgetPress(self.clock.clock_ms()), self.clock.clock_ms())

    def review_events(self):
        return [json.loads(p[1]) for p in self.published(EVENT)]


class ForgetConfirmationTest(LayerBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.client.published.clear()

    def type_text(self, text):
        self.client.fire_message(FORGET_TEXT_SET, text)
        self.pump()

    def press(self):
        """The press as the layer sees it (through MQTT the running service would take the request at once)."""
        from radar_zone_learner.shell.p5_types import ForgetPress
        self.layer.on_event(ForgetPress(self.clock.clock_ms()), self.clock.clock_ms())

    def test_the_accepted_text_is_echoed_as_typed(self):
        self.type_text("FORGET")
        self.assertEqual(self.last(FORGET_TEXT), "FORGET")
        self.assertEqual(self.published(FORGET_TEXT)[-1][1:], (b"FORGET", 1, True))
        self.client.published.clear()
        self.type_text("x" * 30)
        self.assertEqual(self.last(FORGET_TEXT), "x" * 16)

    def test_a_lone_press_is_refused_with_an_event(self):
        self.press()
        self.assertFalse(self.layer.take_forget_request())
        self.assertEqual(self.review_events(), [{"reason": "not_confirmed", "event_type": "forget_refused"}])

    def test_the_text_and_a_press_within_60_seconds_start_the_reset_once(self):
        self.type_text("forget")                                                        # any case
        self.run_for(30)
        self.press()
        self.assertEqual(self.review_events(), [])
        self.assertTrue(self.layer.take_forget_request())
        self.assertFalse(self.layer.take_forget_request())                               # once

    def test_every_press_accepted_or_refused_clears_the_text(self):
        self.type_text("FORGET")
        self.press()
        self.assertEqual(self.last(FORGET_TEXT), "")
        self.assertTrue(self.layer.take_forget_request())
        self.press()                                                                    # a second press: no text left
        self.assertEqual([e["reason"] for e in self.review_events()], ["not_confirmed"])
        self.assertFalse(self.layer.take_forget_request())
        self.type_text("nope")
        self.press()
        self.assertEqual(self.last(FORGET_TEXT), "")
        self.assertEqual(len(self.review_events()), 2)

    def test_the_text_expires_after_60_seconds_and_is_cleared_on_the_topic(self):
        self.type_text("FORGET")
        self.run_for(59)
        self.assertEqual(self.last(FORGET_TEXT), "FORGET")
        self.run_for(2)
        self.assertEqual(self.last(FORGET_TEXT), "")
        self.press()
        self.assertEqual(self.review_events()[-1]["reason"], "not_confirmed")
        self.assertFalse(self.layer.take_forget_request())

    def test_a_press_after_the_window_is_refused_even_without_a_tick(self):
        self.type_text("FORGET")
        self.clock.advance(61)
        self.press()
        self.assertEqual(self.review_events()[-1]["reason"], "not_confirmed")

    def test_through_mqtt_the_service_takes_the_request_and_runs_forget_all(self):
        self.type_text("FORGET")
        self.client.fire_message(FORGET_ALL, "PRESS")
        self.pump()
        self.assertEqual(self.learner.resets, 1)                                        # the reset ran in that worker step
        self.assertFalse(self.layer.take_forget_request())
        self.assertEqual(self.review_events()[-1], {"event_type": "forgotten"})

    def test_refused_during_night_learning(self):
        self.layer.nl_active = True
        self.type_text("FORGET")
        self.press()
        self.assertEqual(self.review_events(), [{"reason": "night_learning_active", "event_type": "forget_refused"}])
        self.assertFalse(self.layer.take_forget_request())
        self.assertEqual(self.last(FORGET_TEXT), "")

    def night_learning_status(self, active, set_by):
        return StatusChanged("learning", {"night_learning": {"active": active, "set_by_learner": set_by, "until": None,
                                                            "phase": "restoring", "last_event": "stopped"},
                                          "review": {"more": 0, "command_pending": False}})

    def test_refused_while_night_learning_is_still_putting_the_schedule_back(self):
        # the switch is off (or the 7th night is over) but the learner's own OFF is not undone: a reset would drop
        # that bookkeeping and leave the node's night schedule OFF
        self.learner.tick_outputs = [self.night_learning_status(False, False)]
        self.run_for(1)
        self.assertFalse(self.layer.nl_active)
        self.assertTrue(self.layer.nl_restoring)
        self.type_text("FORGET")
        self.press()
        self.assertEqual(self.review_events(), [{"reason": "night_learning_active", "event_type": "forget_refused"}])
        self.assertFalse(self.layer.take_forget_request())
        self.assertEqual(self.last(FORGET_TEXT), "")

    def test_the_restore_is_known_at_once_from_the_core_output_not_only_from_the_status(self):
        self.learner.tick_outputs = [NightLearningChanged(False, None, False, "stopped")]
        self.run_for(1)
        self.type_text("FORGET")
        self.press()
        self.assertEqual(self.review_events()[-1]["reason"], "night_learning_active")
        self.assertFalse(self.layer.take_forget_request())
        self.learner.tick_outputs = [NightLearningChanged(False, None, None, "finished")]    # the echo ON came in
        self.run_for(1)
        self.assertFalse(self.layer.nl_restoring)
        self.type_text("FORGET")
        self.press()
        self.assertTrue(self.layer.take_forget_request())

    def test_a_status_without_a_pending_restore_does_not_refuse(self):
        self.learner.tick_outputs = [self.night_learning_status(False, None)]
        self.run_for(1)
        self.assertFalse(self.layer.nl_restoring)
        self.type_text("FORGET")
        self.press()
        self.assertTrue(self.layer.take_forget_request())

    def test_refused_while_a_night_window_waits_for_the_echo(self):
        self.learner.tick_outputs = [StatusChanged("learning", {"review": {"more": 0, "command_pending": True}})]
        self.run_for(1)
        self.type_text("FORGET")
        self.press()
        self.assertEqual(self.review_events(), [{"reason": "command_pending", "event_type": "forget_refused"}])
        self.learner.tick_outputs = [StatusChanged("learning", {"review": {"more": 0, "command_pending": False}})]
        self.run_for(1)
        self.type_text("FORGET")
        self.press()
        self.assertTrue(self.layer.take_forget_request())

    def test_retained_commands_are_cleared_and_never_arm_it(self):
        self.client.fire_message(FORGET_TEXT_SET, "FORGET", retain=True)
        self.client.fire_message(FORGET_ALL, "PRESS", retain=True)
        self.pump()
        cleared = {p[0] for p in self.client.published if p[1] == b"" and p[3]}
        self.assertEqual(cleared, {FORGET_TEXT_SET, FORGET_ALL})
        self.assertFalse(self.layer.take_forget_request())
        self.assertIsNone(self.layer.forget_text)

    def test_the_text_is_empty_at_every_start(self):
        self.assertEqual(self.layer.forget_text, None)
        self.client.published.clear()
        self.svc._republish()
        self.assertEqual(self.last(FORGET_TEXT), "")


class CacheTest(LayerBase, unittest.TestCase):
    def test_the_inputs_forget_all_hands_on_are_cached(self):
        self.connect()
        self.client.fire_message("led_matrix_node/sensors/status", "online", retain=True)
        self.client.fire_message("led_matrix_node/radar/schedule/state", "1", retain=True)
        self.client.fire_message("led_matrix_node/radar/night_start/state", "23:00:00", retain=True)
        self.client.fire_message("radar_zones/context/guests", "on", retain=True)
        self.client.fire_message("radar_zones/context/home_empty", "off", retain=True)
        self.client.fire_message("led_matrix_node/sensors/state", '{"lux":3.5}')
        self.pump()
        cached = self.layer.cached_inputs()
        kinds = sorted(type(e).__name__ for e in cached)
        self.assertEqual(kinds, ["ContextState", "ContextState", "LuxSample", "NodeStatus", "ScheduleState"])
        sched = [e for e in cached if isinstance(e, ScheduleState)][0]
        self.assertEqual((sched.sched, sched.start_min), (True, 1380))
        self.assertEqual({e.name for e in cached if isinstance(e, ContextState)}, {"guests", "home_empty"})

    def test_the_latest_one_of_each_wins(self):
        self.connect()
        self.client.fire_message("radar_zones/context/guests", "on", retain=True)
        self.client.fire_message("radar_zones/context/guests", "off", retain=True)
        self.pump()
        ctx = [e for e in self.layer.cached_inputs() if isinstance(e, ContextState)]
        self.assertEqual([(e.name, e.value) for e in ctx], [("guests", False)])

    def test_nothing_cached_nothing_returned(self):
        self.assertEqual(self.layer.cached_inputs(), [])


class ResetTest(LayerBase, unittest.TestCase):
    def test_reset_clears_the_mirrors_and_the_kept_maps(self):
        self.connect()
        self.items()
        self.learner.tick_outputs = [PushRequest(3, 3, "setup", 2, "s0005-1", 2, ("pa1b2c3d",))]
        self.run_for(1)
        self.client.fire_message("radar_zones/review/live/set", "ON")
        self.pump()
        self.layer.reset()
        self.assertEqual(self.layer.items, {})
        self.assertIsNone(self.layer.selected)
        self.assertEqual(self.layer.sel.options, ["No item"])
        self.assertEqual(self.layer.adv.options, ["No entry"])
        self.assertFalse(self.layer.live.active)
        self.assertIsNone(self.layer.maps.push_png)
        self.assertEqual(self.layer.push.seq, 0)
        self.assertIsNone(self.layer._push_head)

    def test_bind_after_reset_seeds_from_the_new_learner(self):
        self.connect()
        self.items()
        self.layer.reset()
        fresh = FakeLearner()
        fresh.p5_push = PushRequest(3, 3, "none", 0, None, None, ())
        self.layer.bind(lambda fn, *a: fn(*a), fresh)
        self.assertIs(self.layer.learner, fresh)
        self.assertEqual(self.layer.push.seq, 3)
        self.client.published.clear()
        self.layer.republish(self.clock.clock_ms())
        self.assertEqual(self.last(PUSH), "3|3|none|0||||")
        self.assertEqual(self.last(SELECT_STATE), "No item")
        self.assertEqual(self.last(PENDING_ATTR), '{"selected":null,"items":[],"more":0}')
        self.assertEqual(fresh.map_calls[-1][1], False)                      # the maps come from the new learner


class RobustnessTest(LayerBase, unittest.TestCase):
    def test_an_error_in_the_layer_does_not_stop_the_core(self):
        self.connect()

        def boom(ev, now_ms):
            raise RuntimeError("p5 bug")
        self.layer.on_event = boom
        self.client.fire_message("radar_zones/context/guests", "on", retain=True)
        self.pump()
        self.assertEqual(len(self.learner.events), 1)
        self.assertEqual(self.svc.shell_errors, 1)

    def test_a_core_error_in_a_map_call_is_contained(self):
        self.connect()

        def boom(selected=None, live=False):
            raise RuntimeError("core bug")
        self.learner.review_map = boom
        self.layer.maps.mark_dirty()
        self.run_for(6)
        self.assertEqual(self.svc.core_errors, 1)                      # not retried every second until the app gives up
        self.assertFalse(self.layer.maps.dirty)
        self.layer.maps.mark_dirty()                                   # the next change tries again
        self.run_for(2)
        self.assertEqual(self.svc.core_errors, 2)

    def test_a_plain_p4_layer_still_works_with_the_service_hooks(self):
        from tests.p4_harness import P4Harness

        class Plain(P4Harness, unittest.TestCase):
            def make_learner(self):
                return FakeLearner()

            def runTest(self):
                self.connect()
                self.assertEqual(self.last("radar_zones/review/select"), "None")
        case = Plain()
        case.setUp()
        try:
            case.runTest()
        finally:
            case._tmp.cleanup()

    def test_the_layer_without_a_learner_does_not_raise(self):
        layer = P5Layer(self.topics, lambda *a, **k: True, None)
        layer.republish(1000)
        layer.second_pass(1000)
        layer.tick(2000)
        layer.on_event(__import__("radar_zone_learner.shell.p5_types", fromlist=["LiveSwitch"]).LiveSwitch(True, 1), 1)
        layer.shutdown(3000)

    def test_unknown_events_pass_through(self):
        from radar_zone_learner.core.types import FramesMessage
        layer = P5Layer(self.topics, lambda *a, **k: True, None)
        ev = FramesMessage(b"{}", 1)
        self.assertEqual(layer.on_event(ev, 1), [ev])
        lux = LuxSample(2.0, 1)
        self.assertEqual(layer.on_event(lux, 1), [lux])
        st = NodeStatus(True, 1)
        self.assertEqual(layer.on_event(st, 1), [st])


if __name__ == "__main__":
    unittest.main()
