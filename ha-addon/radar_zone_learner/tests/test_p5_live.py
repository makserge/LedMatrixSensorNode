"""'Show me live' and 'You are in' (spec 2.7, 2.8): the LiveView state machine and the layer, with a fake clock."""
import json
import unittest

from radar_zone_learner.core.types import WhereView
from radar_zone_learner.shell.p5 import LiveView
from tests.fakes import FakeLearner
from tests.p5_harness import P5Harness, changed, place_item, window_item

LIVE_SET = "radar_zones/review/live/set"
LIVE_STATE = "radar_zones/review/live"
MAP = "radar_zones/review/map"
YOU = "radar_zones/review/you_are_in"
YOU_ATTR = "radar_zones/review/you_are_in/attr"


class LiveViewTest(unittest.TestCase):
    def test_defaults(self):
        v = LiveView()
        self.assertEqual((v.period_s, v.duration_s, v.slow_s), (2.0, 300.0, 0.5))
        self.assertFalse(v.active)
        self.assertFalse(v.frame_due(100.0))
        self.assertFalse(v.expired(10_000.0))

    def test_start_gives_a_frame_at_once_then_every_two_seconds(self):
        v = LiveView()
        v.start(100.0)
        self.assertTrue(v.active)
        self.assertTrue(v.frame_due(100.0))
        v.frame_done(100.0, 0.01)
        self.assertFalse(v.frame_due(101.0))
        self.assertFalse(v.frame_due(101.99))
        self.assertTrue(v.frame_due(102.0))
        v.frame_done(102.0, 0.01)
        self.assertTrue(v.frame_due(104.0))

    def test_five_minutes_then_expired(self):
        v = LiveView()
        v.start(100.0)
        self.assertFalse(v.expired(399.9))
        self.assertTrue(v.expired(400.0))
        v.stop()
        self.assertFalse(v.active)
        self.assertFalse(v.expired(500.0))

    def test_a_second_start_extends_and_never_starts_a_second_loop(self):
        v = LiveView()
        v.start(100.0)
        v.frame_done(100.0, 0.01)
        v.start(150.0)                                  # the user toggled off and on in Home Assistant: ON again
        self.assertFalse(v.expired(399.0))
        self.assertFalse(v.expired(449.9))
        self.assertTrue(v.expired(450.0))               # five minutes from the second start
        self.assertEqual(v.next_frame, 102.0)           # the frame timer is not reset: one loop only
        self.assertFalse(v.frame_due(101.0))
        self.assertTrue(v.frame_due(102.0))

    def test_two_slow_frames_in_a_row_double_the_period(self):
        v = LiveView()
        v.start(0.0)
        v.frame_done(0.0, 0.6)
        self.assertEqual(v.period_s, 2.0)               # one slow frame: not yet
        self.assertEqual(v.slow_frames, 1)
        v.frame_done(2.0, 0.1)                          # a fast one resets the run
        v.frame_done(4.0, 0.6)
        v.frame_done(6.0, 0.7)
        self.assertEqual(v.period_s, 4.0)
        self.assertEqual(v.slow_frames, 3)
        self.assertFalse(v.frame_due(9.9))
        self.assertTrue(v.frame_due(10.0))
        v.frame_done(10.0, 0.01)                        # it stays doubled for the rest of the session
        self.assertEqual(v.period_s, 4.0)
        v.stop()
        v.start(100.0)                                  # a new session starts at 2 s again
        self.assertEqual(v.period_s, 2.0)
        self.assertEqual(v.slow_frames, 3)              # the counter is kept

    def test_exactly_the_limit_is_not_slow(self):
        v = LiveView()
        v.start(0.0)
        v.frame_done(0.0, 0.5)
        v.frame_done(2.0, 0.5)
        self.assertEqual(v.slow_frames, 0)


class LiveLayerTest(P5Harness, unittest.TestCase):
    def make_learner(self):
        learner = FakeLearner()
        learner.p5_live = ((1.0, 3.0, False),)
        learner.p5_where = WhereView(True, 1, False, None, None)
        return learner

    def start(self):
        self.connect()
        self.learner.tick_outputs = [changed([window_item(), place_item("s0005-1", tag="A")], "s0005-1")]
        self.run_for(12)                                  # past the second pass 10 s after the burst
        self.client.published.clear()

    def switch(self, text):
        self.client.fire_message(LIVE_SET, text)
        self.pump()

    # ------------------------------------------------------------ the state machine through MQTT
    def test_off_at_every_start(self):
        self.connect()
        self.assertEqual(self.published(LIVE_STATE)[0][1:], (b"OFF", 1, True))
        self.assertEqual(self.last(YOU), "Live view off")
        self.assertEqual(self.published(YOU)[0][2:], (1, False))              # not retained
        self.assertEqual(self.last(YOU_ATTR), '{"people":0,"stay_ready":false}')

    def test_on_gives_a_frame_at_once_and_the_text(self):
        self.start()
        self.switch("ON")
        self.assertEqual(self.last(LIVE_STATE), "ON")
        self.assertEqual(self.published(LIVE_STATE)[-1][2:], (1, True))
        self.assertEqual(len(self.published(MAP)), 1)                            # the first frame at once
        self.assertEqual(self.last(YOU), "In no known place")
        self.assertEqual(json.loads(self.last(YOU_ATTR)), {"people": 1, "stay_ready": False})
        self.assertEqual(self.learner.where_calls[-1], "s0005-1")                # for the selected item
        self.assertEqual(self.learner.map_calls[-1][1], False)                   # the base is made without markers

    def test_frames_every_two_seconds_and_only_when_something_changed(self):
        self.start()
        self.switch("ON")
        n = len(self.published(MAP))
        self.run_for(1)
        self.assertEqual(len(self.published(MAP)), n)                            # 1 s: no frame
        self.learner.p5_live = ((1.5, 3.0, False),)
        self.run_for(1)                                                          # 2 s: a frame, the markers moved
        self.assertEqual(len(self.published(MAP)), n + 1)
        self.run_for(2)                                                          # 4 s: the same markers: nothing sent
        self.assertEqual(len(self.published(MAP)), n + 1)
        self.run_for(2)
        self.learner.p5_where = WhereView(True, 1, True, "s0005-1", "place")
        self.run_for(2)
        self.assertEqual(self.last(YOU), "Place A")
        self.assertEqual(json.loads(self.last(YOU_ATTR)), {"people": 1, "stay_ready": True})

    def test_the_text_cases(self):
        self.start()
        self.switch("ON")
        for where, text in ((WhereView(False, 0, False, None, None), "Radar not observing"),
                            (WhereView(True, 0, False, None, None), "No one in view"),
                            (WhereView(True, 2, False, None, None), "Several people in view"),
                            (WhereView(True, 1, False, "z0001", "zone"), "In no known place"),
                            (WhereView(True, 1, False, "s0005-1", "place"), "Place A")):
            with self.subTest(text=text):
                self.learner.p5_where = where
                self.run_for(2)
                self.assertEqual(self.last(YOU), text)

    def test_zone_and_label_names(self):
        from radar_zone_learner.core.types import ZoneView
        self.learner.p5_zones = (ZoneView("z0001", "Desk", "dwell", "approve", "s0003-1", 0, 1.0, 2.0, frozenset({1}), (),
                                          0, 5, False),
                                 ZoneView("l0001", "", "manual", "mark", None, 0, 1.0, 2.0, frozenset({2}), (), 0, 5,
                                          False))
        self.start()
        self.switch("ON")
        self.learner.p5_where = WhereView(True, 1, False, "z0001", "zone")
        self.run_for(2)
        self.assertEqual(self.last(YOU), "Desk")
        self.learner.p5_where = WhereView(True, 1, False, "l0001", "label")
        self.run_for(2)
        self.assertEqual(self.last(YOU), "Marked spot")

    def test_auto_off_after_five_minutes_with_one_final_static_map(self):
        self.start()
        self.switch("ON")
        self.run_for(298)
        self.assertEqual(self.last(LIVE_STATE), "ON")
        self.client.published.clear()
        self.run_for(3)                                                          # 301 s
        self.assertEqual(self.last(LIVE_STATE), "OFF")
        self.assertEqual(len(self.published(MAP)), 1)                            # one final map, no markers
        self.assertEqual(self.last(YOU), "Live view off")
        self.assertEqual(self.last(YOU_ATTR), '{"people":0,"stay_ready":false}')
        self.assertFalse(self.layer.live.active)
        n = len(self.published(MAP))
        self.run_for(10)
        self.assertEqual(len(self.published(MAP)), n)                            # nothing more

    def test_off_stops_at_once(self):
        self.start()
        self.switch("ON")
        self.run_for(5)
        self.client.published.clear()
        self.switch("OFF")
        self.assertEqual(self.last(LIVE_STATE), "OFF")
        self.assertEqual(self.last(YOU), "Live view off")
        self.assertEqual(len(self.published(MAP)), 1)
        self.client.published.clear()
        self.run_for(6)
        self.assertEqual(self.published(MAP), [])
        self.assertEqual(self.published(YOU), [])

    def test_a_second_on_extends_to_five_minutes_from_that_moment(self):
        self.start()
        self.switch("ON")
        self.run_for(200)
        self.switch("ON")
        self.run_for(250)                                                        # 450 s after the first ON
        self.assertEqual(self.last(LIVE_STATE), "ON")
        self.run_for(60)                                                         # 510 s: 310 s after the second ON
        self.assertEqual(self.last(LIVE_STATE), "OFF")

    def test_the_frame_count_is_bounded(self):
        self.start()
        self.learner.p5_live = ()
        self.switch("ON")
        calls = len(self.learner.where_calls)
        self.run_for(300)
        self.assertLessEqual(len(self.learner.where_calls) - calls, 151)         # 300 s / 2 s

    def test_live_while_the_radars_are_off_has_no_markers_and_says_so(self):
        self.learner.p5_live = ()
        self.learner.p5_where = WhereView(False, 0, False, None, None)
        self.start()
        self.switch("ON")
        self.assertEqual(self.last(YOU), "Radar not observing")
        self.assertEqual(self.last(LIVE_STATE), "ON")

    # ------------------------------------------------------------ restarts
    def test_ha_birth_publishes_the_switch_the_map_and_the_text_again(self):
        self.start()
        self.switch("ON")
        self.learner.p5_where = WhereView(True, 1, False, "s0005-1", "place")
        self.run_for(2)
        self.client.published.clear()
        self.client.fire_message("homeassistant/status", "online")
        self.pump()
        self.run_for(3)
        self.assertEqual(self.last(LIVE_STATE), "ON")
        self.assertTrue(self.published(MAP))
        self.assertEqual(self.last(YOU), "Place A")
        self.assertTrue(self.layer.live.active)                                  # the loop kept running

    def test_the_second_pass_sends_the_maps_and_the_text_once_more(self):
        self.start()
        self.client.published.clear()
        self.layer.second_pass(self.clock.clock_ms())
        self.assertEqual(self.last(YOU), "Live view off")
        self.assertEqual(self.last(YOU_ATTR), '{"people":0,"stay_ready":false}')
        self.assertEqual(len(self.published(MAP)), 1)
        self.assertEqual(len(self.published("radar_zones/review/push_map")), 1)

    def test_the_service_calls_the_second_pass_ten_seconds_after_a_burst(self):
        self.connect()
        self.client.published.clear()
        self.run_for(9)
        self.assertEqual(self.published(YOU), [])
        self.run_for(2)
        self.assertEqual(self.last(YOU), "Live view off")                         # the second pass
        self.assertTrue(self.published(MAP))

    def test_an_add_on_restart_starts_off_and_overwrites_a_stale_retained_on(self):
        self.start()
        self.switch("ON")
        self.layer.shutdown(self.clock.clock_ms())
        self.assertEqual(self.last(LIVE_STATE), "OFF")                            # best effort at shutdown
        # a new process: the layer is new, the broker still holds the old retained ON
        from radar_zone_learner.shell.p5 import P5Layer
        fresh = P5Layer(self.topics, self.mqtt.publish, self.dir, monotonic=self.clock.monotonic,
                        clock_ms=self.clock.clock_ms)
        fresh.bind(lambda fn, *a: fn(*a), FakeLearner())
        self.client.published.clear()
        fresh.republish(self.clock.clock_ms())
        self.assertEqual(self.published(LIVE_STATE)[-1][1:], (b"OFF", 1, True))
        self.assertFalse(fresh.live.active)

    def test_shutdown_ends_the_live_view_with_the_final_map_and_the_text(self):
        self.start()
        self.switch("ON")
        self.run_for(4)
        self.client.published.clear()
        self.layer.shutdown(self.clock.clock_ms())
        self.assertFalse(self.layer.live.active)
        self.assertEqual(self.last(LIVE_STATE), "OFF")
        self.assertEqual(self.last(YOU), "Live view off")
        self.assertEqual(len(self.published(MAP)), 1)                            # one static map, no markers

    def test_a_core_that_keeps_failing_ends_the_live_view_instead_of_flooding_the_error_count(self):
        self.start()

        def boom(selected=None):
            raise RuntimeError("core bug")
        self.learner.where_am_i = boom
        self.switch("ON")
        self.run_for(10)
        self.assertFalse(self.layer.live.active)                                 # three frames in a row without an answer
        self.assertEqual(self.last(LIVE_STATE), "OFF")
        self.assertEqual(self.published(YOU), [])                                # it never left 'Live view off'
        self.assertLessEqual(self.svc.core_errors, 3)
        self.assertEqual(self.svc.shell_errors, 0)

    def test_a_frame_that_fails_once_does_not_end_the_live_view(self):
        self.start()
        real = self.learner.where_am_i
        calls = []

        def flaky(selected=None):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("once")
            return real(selected)
        self.learner.where_am_i = flaky
        self.switch("ON")
        self.run_for(8)
        self.assertTrue(self.layer.live.active)
        self.assertEqual(self.svc.core_errors, 1)

    def test_a_down_broker_skips_frames_but_the_timer_runs(self):
        self.start()
        self.switch("ON")
        self.client.fire_disconnect()
        self.run_for(4)
        self.assertTrue(self.layer.live.active)
        self.assertGreater(self.layer.live.frames, 1)

    def test_slow_frames_double_the_period(self):
        self.start()
        real_frame = self.layer.maps._frame

        def slow(base, data, points):
            self.clock.advance(0.6)
            return real_frame(base, data, points)
        self.layer.maps._frame = slow
        self.switch("ON")
        self.run_for(2)
        self.run_for(2)
        self.assertEqual(self.layer.live.period_s, 4.0)
        self.assertGreaterEqual(self.layer.live.slow_frames, 2)
        self.assertEqual(self.layer.attributes()["live_slow"], self.layer.live.slow_frames)
        self.assertTrue(self.layer.attributes()["live"])

    def test_no_coordinate_is_published_or_logged(self):
        self.start()
        self.switch("ON")
        self.run_for(10)
        for topic in (YOU, YOU_ATTR, LIVE_STATE):
            for _, payload, _, _ in self.published(topic):
                self.assertNotRegex(payload.decode(), r"\d\.\d")


class LiveWithoutImagesTest(P5Harness, unittest.TestCase):
    debug_map = False

    def make_learner(self):
        learner = FakeLearner()
        learner.p5_where = WhereView(True, 1, False, None, None)
        return learner

    def test_the_text_still_works_with_debug_map_false(self):
        self.connect()
        self.client.published.clear()
        self.client.fire_message(LIVE_SET, "ON")
        self.pump()
        self.assertEqual(self.last(YOU), "In no known place")
        self.assertEqual(self.published(MAP), [])
        self.assertEqual(self.learner.map_calls, [])


if __name__ == "__main__":
    unittest.main()
