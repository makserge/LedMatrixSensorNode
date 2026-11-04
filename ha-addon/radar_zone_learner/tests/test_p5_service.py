"""The service with the P5 layer: Forget all (spec 7.2), saving, debug_map false, restarts, main's wiring, and the real core
behind the layer (what the Learner can already do)."""
import json
import unittest
from pathlib import Path
from unittest import mock

from radar_zone_learner.core.learner import Learner
from radar_zone_learner.core.params import DEFAULT_PARAMS
from radar_zone_learner.core.types import (ContextState, FixedOffsetTime, LuxSample, NameEcho, NodeStatus, PushRequest,
                                           ReviewAnswer, ReviewChanged, SaveRequested, ScheduleState, StatusChanged)
from radar_zone_learner.shell import service as service_mod
from radar_zone_learner.shell.p5 import P5Layer
from radar_zone_learner.shell.topics_p5 import TopicsP5
from tests.fakes import FakeLearner
from tests.p5_harness import P5Harness, bed_item, changed, place_item, window_item

FRAMES = "led_matrix_node/radar/ld2454/frames"
FORGET_ALL = "radar_zones/learner/forget_all"
FORGET_TEXT_SET = "radar_zones/learner/forget_confirm/set"
EVENT = "radar_zones/review/event"
ANSWER = "radar_zones/review/answer"
PENDING_ATTR = "radar_zones/review/pending/attr"
PUSH = "radar_zones/review/push"
MAP = "radar_zones/review/map"
PUSH_MAP = "radar_zones/review/push_map"


class ForgetAllTest(P5Harness, unittest.TestCase):
    def make_learner(self):
        self.fresh = FakeLearner()
        self.fresh.p5_push = PushRequest(7, 7, "none", 0, None, None, ())      # the kept seq and acked (spec 7.2 step 2)
        learner = FakeLearner()
        learner.p5_reset_result = self.fresh
        learner.p5_items = (window_item(), place_item())
        learner.p5_selected = "s0005-1"
        learner.p5_push = PushRequest(7, 7, "setup", 2, "s0005-1", 1, ("pa1b2c3d",))
        return learner

    def prepare(self):
        node = self.connect(sched=True, start="00:00:00", end="06:00:00")
        self.client.fire_message("radar_zones/context/guests", "on", retain=True)
        self.client.fire_message("radar_zones/context/home_empty", "off", retain=True)
        self.client.fire_message("led_matrix_node/sensors/state", '{"lux":3.5}')
        self.client.fire_message("radar_zones/learner/debug_capture/set", "ON")
        self.client.fire_message(FRAMES, '{"x":1}')
        self.pump(node)
        self.svc._save("periodic")
        self.store.snapshot({"schema": 4, "n": 1}, "2026-01-04")
        self.store.snapshot({"schema": 4, "n": 2}, "2026-01-05")
        (self.dir / "state.json.corrupt-1").write_text("{}")
        self.layer.save(self.clock.clock_ms())
        self.assertTrue((self.dir / "rf_stats.json").exists())
        self.assertTrue(list((self.dir / "capture").glob("*.jsonl")))
        self.client.published.clear()
        return node

    def forget(self, node=None):
        self.client.fire_message(FORGET_TEXT_SET, "FORGET")
        self.client.fire_message(FORGET_ALL, "PRESS")
        self.pump(node)

    def test_the_seven_steps_in_order(self):
        node = self.prepare()
        calls = []
        old = self.learner
        wrap = lambda name, obj, attr: setattr(obj, attr, self.recorder(calls, name, getattr(obj, attr)))
        wrap("reset", old, "reset")
        wrap("purge", self.layer, "purge_capture")
        wrap("wipe", self.store, "wipe")
        wrap("layer.reset", self.layer, "reset")
        wrap("bind", self.layer, "bind")
        wrap("save", self.svc, "_save")
        wrap("republish", self.svc, "_republish")
        wrap("event", self.layer, "notify_forgotten")
        wrap("feed", self.fresh, "handle")
        self.forget(node)
        order = [c for c in calls if c != "feed"]
        self.assertEqual(order, ["reset", "purge", "wipe", "layer.reset", "bind", "save", "republish", "event"])
        self.assertLess(calls.index("save"), calls.index("feed"))
        self.assertLess(calls.index("feed"), calls.index("republish"))
        self.assertEqual(old.resets, 1)
        self.assertIs(self.svc.learner, self.fresh)
        self.assertIs(self.layer.learner, self.fresh)

    @staticmethod
    def recorder(calls, name, fn):
        def wrapper(*a, **k):
            calls.append(name)
            return fn(*a, **k)
        return wrapper

    def test_the_files_are_deleted_and_the_fresh_state_is_saved_at_once(self):
        node = self.prepare()
        self.forget(node)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.is_file()),
                         ["rf_stats.json", "state.json"])                  # state, snapshots and quarantines are gone
        self.assertEqual(list((self.dir / "snapshots").glob("*")), [])
        self.assertTrue((self.dir / "snapshots").is_dir())
        self.assertEqual(list((self.dir / "capture").glob("*.jsonl")), [])
        saved = json.loads((self.dir / "state.json").read_text())
        self.assertEqual(saved["events"], 0)                               # written before the cached inputs were fed
        self.assertFalse(self.layer.capture.active)
        self.assertEqual(self.last("radar_zones/learner/debug_capture"), "OFF")

    def test_the_cached_inputs_go_to_the_new_learner_with_the_time_of_now(self):
        node = self.prepare()
        self.clock.advance(100)
        self.forget(node)
        fed = [e for e in self.fresh.events if isinstance(e, (ScheduleState, NodeStatus, ContextState, LuxSample))]
        self.assertEqual(sorted(type(e).__name__ for e in fed),
                         ["ContextState", "ContextState", "LuxSample", "NodeStatus", "ScheduleState"])
        self.assertTrue(all(e.recv_ms == self.clock.clock_ms() for e in fed))     # day 0 of the new learner is now
        self.assertEqual([e for e in fed if isinstance(e, LuxSample)][0].lux, 3.5)

    def test_every_retained_topic_is_overwritten_with_its_empty_version(self):
        node = self.prepare()
        self.forget(node)
        self.assertEqual(self.last(PENDING_ATTR), '{"selected":null,"items":[],"more":0}')
        self.assertEqual(self.last("radar_zones/review/pending"), "0")
        self.assertEqual(self.last("radar_zones/review/item_text"), "No item")
        self.assertEqual(self.last("radar_zones/review/select"), "No item")
        self.assertEqual(self.last("radar_zones/review/name"), "")
        self.assertEqual(self.last(PUSH), "7|7|none|0||||")                # count 0 and the kept seq: the blueprint clears
        attr = json.loads(self.last("radar_zones/learner/attr"))
        self.assertNotIn("suggestions", attr) if "suggestions" not in self.fresh.status().attributes else None
        self.assertEqual(self.last("radar_zones/learner/forget_confirm"), "")
        self.assertEqual(self.last("radar_zones/review/live"), "OFF")

    def test_the_event_forgotten_comes_last(self):
        node = self.prepare()
        self.forget(node)
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual(events, [{"event_type": "forgotten"}])
        topics = [p[0] for p in self.client.published]
        self.assertGreater(topics.index(EVENT), topics.index(PUSH))

    def test_after_it_the_maps_the_where_and_the_seeding_come_from_the_new_learner(self):
        node = self.prepare()
        old_calls = len(self.learner.map_calls)
        self.client.fire_message("radar_zones/review/live/set", "ON")
        self.pump(node)
        self.forget(node)
        self.assertFalse(self.layer.live.active)
        self.assertGreaterEqual(len(self.fresh.map_calls), 1)               # the review map and the push map
        n_old, n_fresh, w_old = len(self.learner.map_calls), len(self.fresh.map_calls), len(self.learner.where_calls)
        self.client.fire_message("radar_zones/review/live/set", "ON")
        self.pump(node)
        self.run_for(5, node)
        self.assertEqual(len(self.learner.map_calls), n_old)                # the discarded learner is never called again
        self.assertEqual(len(self.learner.where_calls), w_old)
        self.assertGreater(len(self.fresh.where_calls), 0)
        self.assertGreaterEqual(len(self.fresh.map_calls), n_fresh)         # the base is cached: no new render needed

    def test_a_lone_press_does_nothing(self):
        node = self.prepare()
        self.client.fire_message(FORGET_ALL, "PRESS")
        self.pump(node)
        self.assertEqual(self.learner.resets, 0)
        self.assertTrue((self.dir / "state.json").exists())
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual(events, [{"reason": "not_confirmed", "event_type": "forget_refused"}])

    def test_it_is_refused_during_night_learning_and_nothing_is_deleted(self):
        node = self.prepare()
        self.layer.nl_active = True
        self.forget(node)
        self.assertEqual(self.learner.resets, 0)
        self.assertTrue((self.dir / "state.json").exists())
        self.assertTrue(list((self.dir / "snapshots").glob("*.json")))

    def test_it_is_refused_while_a_night_window_waits_for_the_node(self):
        node = self.prepare()
        self.learner.tick_outputs = [StatusChanged("learning", {"review": {"more": 0, "command_pending": True}})]
        self.run_for(1, node)
        self.forget(node)
        self.assertEqual(self.learner.resets, 0)
        self.assertEqual([json.loads(p[1])["reason"] for p in self.published(EVENT)], ["command_pending"])

    def test_a_core_error_in_the_reset_changes_nothing(self):
        node = self.prepare()

        def boom():
            raise RuntimeError("core bug")
        self.learner.reset = boom
        self.forget(node)
        self.assertGreaterEqual(self.svc.core_errors, 1)
        self.assertTrue((self.dir / "state.json").exists())
        self.assertIsNot(self.svc.learner, self.fresh)

    def test_the_node_and_options_files_are_not_touched(self):
        node = self.prepare()
        (self.dir / "options.json").write_text("{}")
        self.forget(node)
        self.assertTrue((self.dir / "options.json").exists())
        self.assertTrue((self.dir / "rf_stats.json").exists())

    def test_the_layer_asks_for_the_press_only_once(self):
        node = self.prepare()
        self.forget(node)
        self.assertEqual(self.old_resets(), 1)
        self.run_for(5, node)
        self.assertEqual(self.old_resets(), 1)

    def old_resets(self):
        return self.svc.learner.resets + 1 if self.svc.learner is self.fresh else self.learner.resets


class SetupCore(FakeLearner):
    """A scripted core for the walk-through of spec 10.2: it keeps the open items, answers them, writes the night window
    through a NodeCommand, binds 'Mark spot here' to the bed item and lets the select fall to the first open item. It is a
    stand-in for the real core (CORE owns the real rules); what the test shows is the shell's path over MQTT."""

    def __init__(self, places=5):
        super().__init__()
        self.items = [window_item(rev=3), bed_item()] + [
            place_item("s%04d-1" % (5 + k), 1, "ABCDEFGH"[k]) for k in range(places)]
        self.chosen = None
        self.stay_ready = False
        self.zones_made = []
        self.pending_window = None
        self.p5_where = None

    def selected(self):
        ids = [i.id for i in self.items]
        return self.chosen if self.chosen in ids else (ids[0] if ids else None)

    def push(self, outs):
        outs.append(ReviewChanged(tuple(self.items), self.selected()))
        self.p5_items, self.p5_selected = tuple(self.items), self.selected()

    def handle(self, event):
        outs = list(super().handle(event))
        before = (tuple(i.id for i in self.items), self.selected())
        from radar_zone_learner.core.types import CommandResult, MarkPress, MarkResult, NodeCommand, ReviewSelect
        if isinstance(event, ReviewSelect):
            self.chosen = event.item
        elif isinstance(event, ReviewAnswer):
            item = next((i for i in self.items if i.id == event.item), None)
            if item is not None and event.op == "approve":
                if item.id == "night_window":
                    self.pending_window = "w1"
                    outs.append(NodeCommand("w1", "window", sched=event.sched or None, start_min=1410, end_min=405,
                                            reason="approved_window"))
                elif item.type == "dwell_zone":
                    self.zones_made.append(item.id)
                    self.items.remove(item)
        elif isinstance(event, CommandResult) and event.token == self.pending_window and event.ok:
            self.items = [i for i in self.items if i.id != "night_window"]
            self.pending_window = None
        elif isinstance(event, MarkPress):
            if self.selected() == "bed_needs_data" and self.stay_ready:
                outs.append(MarkResult(True, None, "l0001", "bed"))
                self.items = [i for i in self.items if i.id != "bed_needs_data"]
            else:
                outs.append(MarkResult(False, "no_stay", None, None))
        if before != (tuple(i.id for i in self.items), self.selected()):
            self.push(outs)
        return outs


class FirstSetupFlowTest(P5Harness, unittest.TestCase):
    def make_learner(self):
        return SetupCore(places=5)

    def select_state(self):
        return self.last("radar_zones/review/select")

    def test_seven_taps_for_five_places_over_mqtt(self):
        node = self.connect(sched=True, start="00:00:00", end="06:00:00")
        self.learner.push([])
        self.learner.tick_outputs = [ReviewChanged(tuple(self.learner.items), "night_window")]
        self.run_for(2, node)
        self.assertEqual(self.select_state(), "Night window")
        self.assertEqual(self.last("radar_zones/review/pending"), "7")
        taps = 0
        # 1. Approve the night window
        self.client.fire_message(ANSWER, json.dumps({"op": "approve"}))
        taps += 1
        self.pump(node)
        self.assertEqual(node.received, [("led_matrix_node/radar/night_start", "23:30:00"),
                                         ("led_matrix_node/radar/night_end", "06:45:00")])
        self.assertEqual(self.select_state(), "Bed zone")                        # the select fell to the next item
        # 2. 'Mark spot here' in bed, the bed item selected (the stay is long enough this time)
        self.learner.stay_ready = True
        self.client.fire_message("radar_zones/review/mark", "PRESS")
        taps += 1
        self.pump(node)
        self.assertEqual(self.select_state(), "Place A")
        self.assertEqual(self.published(EVENT), [])
        # 3-7. Approve the five places, each time the select shows the next one
        for tag in "ABCDE":
            self.assertEqual(self.select_state(), "Place " + tag)
            self.client.fire_message(ANSWER, json.dumps({"op": "approve"}))
            taps += 1
            self.pump(node)
        self.assertEqual(taps, 7)
        self.assertEqual(self.learner.zones_made, ["s0005-1", "s0006-1", "s0007-1", "s0008-1", "s0009-1"])
        self.assertEqual(self.select_state(), "No item")
        self.assertEqual(self.last("radar_zones/review/pending"), "0")
        self.assertEqual(self.last("radar_zones/review/pending/attr"), '{"selected":null,"items":[],"more":0}')
        self.assertEqual(self.published(EVENT), [])                               # no refusal on the way
        self.assertEqual(self.svc.shell_errors, 0)
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.layer.answers_ignored, 0)

    def test_a_failed_press_is_a_tap_and_an_event(self):
        node = self.connect(sched=True)
        self.learner.tick_outputs = [ReviewChanged(tuple(self.learner.items), "bed_needs_data")]
        self.run_for(2, node)
        self.client.fire_message("radar_zones/review/mark", "PRESS")
        self.pump(node)
        self.assertEqual([json.loads(p[1]) for p in self.published(EVENT)],
                         [{"reason": "no_stay", "event_type": "mark_failed"}])
        self.assertEqual(self.select_state(), "Bed zone")                           # still open

    def test_the_select_options_follow_the_items_while_the_flow_runs(self):
        node = self.connect(sched=True)
        self.learner.tick_outputs = [ReviewChanged(tuple(self.learner.items), "night_window")]
        self.run_for(2, node)
        cfg = [json.loads(p[1]) for p in self.published("homeassistant/select/radar_zones_review_select/config")]
        self.assertEqual(cfg[-1]["options"], ["No item", "Night window", "Bed zone", "Place A", "Place B", "Place C",
                                              "Place D", "Place E"])
        self.client.fire_message("radar_zones/review/select/set", "Place C")
        self.pump(node)
        self.assertEqual(self.select_state(), "Place C")
        self.client.fire_message(ANSWER, json.dumps({"op": "approve"}))
        self.pump(node)
        cfg = [json.loads(p[1]) for p in self.published("homeassistant/select/radar_zones_review_select/config")]
        self.assertEqual(cfg[-1]["options"], ["No item", "Night window", "Bed zone", "Place A", "Place B", "Place D",
                                              "Place E"])
        self.assertEqual(self.select_state(), "Night window")                       # C was answered: the first item


class SavingTest(P5Harness, unittest.TestCase):
    def make_learner(self):
        return FakeLearner()

    def test_a_review_save_is_written_at_once_a_name_save_is_deferred(self):
        self.connect()
        self.svc._execute(SaveRequested("name"))
        first = self.svc.state_saved_ms
        self.assertIsNotNone(first)
        self.clock.advance(2)
        self.svc._execute(SaveRequested("name"))                           # within 10 s of the last name save: deferred
        self.assertEqual(self.svc.state_saved_ms, first)
        self.svc._execute(SaveRequested("review"))                         # an approval is never delayed
        self.assertGreater(self.svc.state_saved_ms, first)
        self.svc._execute(SaveRequested("push"))                           # nor a push request
        self.assertGreaterEqual(self.svc.state_saved_ms, first)

    def test_an_approve_with_a_typed_name_saves_at_once(self):
        def respond(ev):
            if isinstance(ev, ReviewAnswer) and ev.op == "approve":
                return [NameEcho(""), SaveRequested("review")]
            return []
        self.learner.responder = respond
        self.connect()
        self.learner.tick_outputs = [changed([window_item(), place_item()], "s0005-1")]
        self.run_for(1)
        self.client.fire_message("radar_zones/review/name/set", "Desk")
        self.pump()
        self.svc._save("periodic")
        before = self.svc.state_saved_ms
        self.clock.advance(1)
        self.client.fire_message(ANSWER, json.dumps({"op": "approve"}))
        self.pump()
        self.assertGreater(self.svc.state_saved_ms, before)
        self.assertEqual(self.last("radar_zones/review/name"), "")         # the name text was consumed

    def test_a_push_save_reason_is_handled(self):
        self.connect()
        before = self.svc.save_failures
        self.svc._execute(SaveRequested("push"))
        self.assertEqual(self.svc.save_failures, before)
        self.assertTrue((self.dir / "state.json").exists())


class DebugMapOffTest(P5Harness, unittest.TestCase):
    debug_map = False

    def make_learner(self):
        learner = FakeLearner()
        learner.p5_items = (window_item(), place_item())
        learner.p5_selected = "s0005-1"
        learner.p5_push = PushRequest(1, 0, "setup", 2, "s0005-1", 1, ())
        return learner

    def test_no_map_is_rendered_or_published_and_the_image_configs_are_empty(self):
        self.connect()
        self.run_for(15)
        self.assertEqual(self.published(MAP), [])
        self.assertEqual(self.published(PUSH_MAP), [])
        self.assertEqual(self.published("radar_zones/learner/debug_map"), [])
        self.assertEqual(self.learner.map_calls, [])
        empty = [p[0] for p in self.client.published if p[0].endswith("/config") and p[1] == b""]
        self.assertEqual(sorted(empty), sorted(["homeassistant/image/radar_zones_review_map/config",
                                                "homeassistant/image/radar_zones_review_push_map/config",
                                                "homeassistant/image/radar_zones_learner_debug_map/config"]))
        # everything else works: the select, the pending count, the push state
        self.assertEqual(self.last("radar_zones/review/select"), "Place A")
        self.assertEqual(self.last("radar_zones/review/pending"), "2")
        self.assertEqual(self.last(PUSH), "1|0|setup|2|1|s0005-1||")

    def test_a_new_push_request_publishes_no_push_map(self):
        self.connect()
        self.learner.tick_outputs = [PushRequest(2, 1, "daily", 2, "s0005-1", 1, ())]
        self.run_for(2)
        self.assertEqual(self.published(PUSH_MAP), [])
        self.assertEqual(self.last(PUSH), "2|1|daily|2|1|s0005-1||")


class MainWiringTest(unittest.TestCase):
    def test_main_builds_the_p5_topics_and_one_p5_layer_as_p4(self):
        from tests._tmp import tmpdir
        from tests.fakes import quiet_logs
        quiet_logs(self)
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        options = Path(tmp.name) / "options.json"
        options.write_text(json.dumps({"debug_map": False, "node_prefix": "node_b"}))
        seen = {}

        class FakeService:
            def __init__(self, *args, **kwargs):
                seen["args"], seen["kwargs"] = args, kwargs

            def run(self, stop_event):
                return 0

            def request_stop(self):
                pass
        with mock.patch.object(service_mod, "Service", FakeService), \
                mock.patch.object(service_mod, "MqttIO") as mqtt_cls, \
                mock.patch.object(service_mod, "HealthServer"), mock.patch.object(service_mod, "StallWatchdog"), \
                mock.patch.object(service_mod, "configure_logging"), mock.patch.object(service_mod.signal, "signal"):
            code = service_mod.main(["--options", str(options), "--health-port", "0"])
        self.assertEqual(code, 0)
        kwargs = seen["kwargs"]
        self.assertIsInstance(kwargs["p4"], P5Layer)
        self.assertIsInstance(kwargs["topics"], TopicsP5)
        self.assertEqual(kwargs["topics"].frames, "node_b/radar/ld2454/frames")
        self.assertFalse(kwargs["p4"].debug_map)
        self.assertFalse(kwargs["p4"].maps.enabled)
        self.assertEqual(len(kwargs["topics"].subscriptions()), 30)
        self.assertIs(mqtt_cls.call_args.args[1], kwargs["topics"])
        self.assertIsNotNone(kwargs["p4"].capture)


class RealCoreTest(P5Harness, unittest.TestCase):
    """The real Learner behind the layer: what has to work whatever the core's P5 state is."""

    def make_learner(self):
        self.made = []

        def make(state):
            learner = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), state)
            self.made.append(learner)
            return learner
        self.make = make
        return make(None)

    def test_start_and_republish_with_no_errors(self):
        node = self.connect(sched=True)
        self.run_for(30, node)
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.svc.shell_errors, 0)
        configs = [p for p in self.client.published if p[0].endswith("/config")]
        self.assertEqual(len(configs), 35)
        self.assertEqual(self.last("radar_zones/review/select"), "No item")
        self.assertEqual(self.last("radar_zones/review/pending"), "0")
        self.assertEqual(self.last("radar_zones/review/item_text"), "No item")
        self.assertEqual(self.last(PUSH), "0|0|none|0||||")
        self.assertEqual(self.last("radar_zones/review/live"), "OFF")
        self.assertEqual(self.last("radar_zones/review/you_are_in"), "Live view off")
        self.assertTrue(self.published(MAP)[0][1].startswith(b"\x89PNG"))
        self.assertTrue(self.published(PUSH_MAP)[0][1].startswith(b"\x89PNG"))
        attr = json.loads(self.last("radar_zones/learner/attr"))
        self.assertEqual(attr["capture"], {"on": False, "files": 0, "bytes": 0})
        self.assertEqual(attr["live"], False)

    def test_the_live_view_runs_on_the_real_core(self):
        node = self.connect(sched=True)
        self.client.published.clear()
        self.client.fire_message("radar_zones/review/live/set", "ON")
        self.pump(node)
        self.run_for(6, node)
        self.assertEqual(self.last("radar_zones/review/live"), "ON")
        self.assertIn(self.last("radar_zones/review/you_are_in"),
                      ("Radar not observing", "No one in view", "In no known place"))
        self.client.fire_message("radar_zones/review/live/set", "OFF")
        self.pump(node)
        self.assertEqual(self.last("radar_zones/review/you_are_in"), "Live view off")
        self.assertEqual(self.svc.core_errors, 0)

    def test_the_four_settings_round_trip_through_the_core_status(self):
        node = self.connect(sched=True)
        for name, topic in (("pets", "radar_zones/learner/pets"), ("guests_pause", "radar_zones/learner/guests_pause"),
                            ("detail_text", "radar_zones/learner/detail_text"),
                            ("approve_sched", "radar_zones/review/approve_sched")):
            self.assertEqual(self.last(topic), "OFF")
            self.client.fire_message(topic + "/set", "ON")
            self.pump(node)
            self.assertEqual(self.last(topic), "ON", name)             # the state follows the core's status, not the command
        attr = json.loads(self.last("radar_zones/learner/attr"))
        self.assertEqual(attr["settings"], {"approve_sched": True, "guests_pause": True, "pets": True, "detail_text": True})
        self.assertEqual(self.svc.core_errors, 0)
        self.client.fire_message("radar_zones/learner/pets/set", "OFF")
        self.pump(node)
        self.assertEqual(self.last("radar_zones/learner/pets"), "OFF")

    def test_an_edit_of_the_night_window_without_a_proposal_is_a_visible_refusal(self):
        node = self.connect(sched=True)
        self.client.fire_message("radar_zones/review/nw_start/set", "23:45:00")
        self.pump(node)
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual(events, [{"reason": "no_such_item", "op": "edit", "item": "night_window",
                                   "event_type": "answer_ignored"}])

    def test_a_shell_refusal_needs_no_core(self):
        node = self.connect(sched=True)
        self.client.fire_message(ANSWER, json.dumps({"op": "approve", "item": "s0005-1", "rev": 1}))
        self.pump(node)
        events = [json.loads(p[1]) for p in self.published(EVENT)]
        self.assertEqual(events, [{"reason": "no_such_item", "op": "approve", "item": "s0005-1",
                                   "event_type": "answer_ignored"}])

    def test_forget_all_with_the_real_learner(self):
        node = self.connect(sched=True, start="22:00:00", end="07:00:00")
        old = self.learner
        self.client.fire_message(FORGET_TEXT_SET, "FORGET")
        self.client.fire_message(FORGET_ALL, "PRESS")
        self.pump(node)
        self.assertIsNot(self.svc.learner, old)
        self.assertIs(self.layer.learner, self.svc.learner)
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.svc.shell_errors, 0)
        self.assertEqual([json.loads(p[1]) for p in self.published(EVENT)], [{"event_type": "forgotten"}])
        self.assertEqual(self.svc.learner.window["start"], 1320)           # the node's window was handed over again
        self.assertEqual(self.last("radar_zones/review/pending"), "0")
        self.assertTrue((self.dir / "state.json").exists())
        # the app goes on with the new learner
        self.run_for(30, node)
        self.assertEqual(self.svc.core_errors, 0)

    def test_a_restart_reloads_the_state_and_republishes(self):
        node = self.connect(sched=True)
        self.svc._save("periodic")
        state = json.loads((self.dir / "state.json").read_text())
        self.svc.shutdown(0)
        again = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), state)             # what the next start loads
        self.assertEqual(again.export_state()["schema"], state["schema"])
        self.assertEqual(again.review_state(), ((), None))
        layer = P5Layer(self.topics, lambda *a, **k: True, None)
        layer.bind(lambda fn, *a: fn(*a), again)
        self.assertEqual(layer.sel.options, ["No item"])
        self.assertEqual(layer.push.seq, 0)


if __name__ == "__main__":
    unittest.main()
