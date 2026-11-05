"""The service with the P6 layer (P6 spec B.2, 11.4, checks P6-5 and P6-12): the one layer object, the hooks the service already
has, main's wiring, the errors that stay inside the zone code, Forget all through the service, the zone entities in the discovery
burst, and the real core behind the layer with a fake Home Assistant (a person walks in, sits and leaves; a night with the radars
off). The fake Home Assistant encodes the assumptions HA[U1]..HA[U7] (``tests/fake_ha_zone.py``)."""
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from radar_zone_learner.core.learner import Learner
from radar_zone_learner.core.types import StatusChanged, ZoneEvent
from radar_zone_learner.shell import service as service_mod
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.p5 import P5Layer
from radar_zone_learner.shell.p6 import P6Layer
from radar_zone_learner.shell.topics_p6 import TopicMapperP6, TopicsP6
from tests.fakes import FakeClientFactory, FakeLearner, quiet_logs
from tests.p5_harness import bed_item, changed, place_item, window_item
from tests.p6_harness import AVAIL, MqttFeeder, P6Harness, real_zone_learner, zone_view
from tests.test_learner import BASE_MS, walk_fn

NODE = "led_matrix_node/"
OCC = "radar_zones/zone/z0001/occupancy"
OCC_ID = "binary_sensor.radar_spot_z0001"
LOCC_ID = "binary_sensor.radar_spot_l0001"
LEVENT = "radar_zones/zone/l0001/event"
POS = (0.1, 3.1)
OUT = (2.5, 3.1)


class FakeCoreBase(P6Harness):
    def make_learner(self):
        learner = FakeLearner()
        learner.p5_zones = (zone_view("z0001", "Desk"), zone_view("l0001", "Bed", "bed"))
        learner.p6_zone("z0001")
        learner.p6_zone("l0001")
        return learner

    def up(self):
        node = self.connect(sched=False)
        self.node_says(NODE + "sensors/status", "online")
        self.node_says(NODE + "radar/state", "on")
        self.node_says(NODE + "radar/observing", "ON")
        self.learner.p6_valid = True
        self.learner.tick_outputs = [StatusChanged("learning", {}), self.learner.p6_snapshot()]
        self.run_for(12)
        return node


class WiringTest(FakeCoreBase, unittest.TestCase):
    def test_the_layer_is_the_one_layer_object_and_gets_bound_once_to_the_services_learner(self):
        self.up()
        self.assertIs(self.svc.p4, self.layer)
        self.assertIs(self.layer.learner, self.learner)
        self.assertIsInstance(self.layer, P5Layer)
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.svc.shell_errors, 0)

    def test_the_service_has_no_new_hook_for_zones(self):
        text = (Path(service_mod.__file__)).read_text(encoding="utf-8")
        names = set(re.findall(r'getattr\((?:self\.p4|layer), "([a-z_0-9]+)"', text))
        self.assertEqual(names, {"bind", "take_forget_request", "second_pass", "cached_inputs", "purge_capture", "reset",
                                 "notify_forgotten"})

    def test_zone_outputs_are_consumed_by_the_layer_and_the_service_never_calls_them_unknown(self):
        self.up()
        with self.assertNoLogs("radar_zone_learner", level="WARNING"):
            self.learner.p6_zone("z0001", occ=True, count=1)
            self.learner.tick_outputs = [ZoneEvent("z0001", "enter", "", 5, 7, 0, ""), self.learner.p6_snapshot()]
            self.run_for(1)
        self.assertEqual(self.ha.state(OCC_ID), "on")
        self.assertEqual([json.loads(p[1]) for p in self.published("radar_zones/zone/z0001/event")],
                         [{"event_type": "enter", "track": "t7"}])

    def test_the_status_attributes_carry_the_zone_counts_and_no_name(self):
        self.up()
        self.svc._publish_status(force=True)
        attr = json.loads(self.last("radar_zones/learner/attr"))
        self.assertEqual(attr["zone_entities"], 2)
        for key in ("zone_cmds_ignored", "zone_events_sent", "zone_events_dropped", "zone_valve_zones", "zone_shell_errors"):
            self.assertIn(key, attr)
        self.assertNotIn("Desk", json.dumps(attr))

    def test_the_review_path_is_unchanged_next_to_the_zones(self):
        self.up()
        self.learner.tick_outputs = [changed([window_item(rev=3), bed_item(), place_item("s0005-1", 2, "A")], "s0005-1")]
        self.run_for(1)
        self.assertEqual(self.last("radar_zones/review/pending"), "3")
        self.assertEqual(self.last("radar_zones/review/select"), "Place A")
        self.client.fire_message("radar_zones/review/answer", json.dumps({"op": "later", "item": "s0005-1", "rev": 2}))
        self.pump()
        self.assertEqual([e.op for e in self.learner.events if hasattr(e, "op")], ["later"])

    def test_an_error_in_the_zone_code_is_contained_and_the_p5_layer_goes_on(self):
        self.up()
        self.layer.zpub.update = mock.Mock(side_effect=RuntimeError("zone bug"))
        self.learner.tick_outputs = [self.learner.p6_snapshot(), changed([window_item(), place_item()], "s0005-1")]
        self.run_for(2)
        self.assertGreaterEqual(self.layer.zone_shell_errors, 1)
        self.assertEqual(self.svc.shell_errors, 0)                                   # contained below the service's own net
        self.assertEqual(self.last("radar_zones/review/select"), "Place A")          # the review item still shows

    def test_the_layers_mapper_is_the_mqtt_threads_mapper_and_knows_the_zones(self):
        self.up()
        self.assertIs(self.layer.mapper, self.mqtt.mapper)
        self.assertIsInstance(self.mqtt.mapper, TopicMapperP6)
        self.assertEqual(self.mqtt.mapper.known[0], frozenset({"z0001", "l0001"}))
        self.assertEqual(len(self.client.subscriptions), 34)


class BurstTest(FakeCoreBase, unittest.TestCase):
    def test_a_burst_is_thirty_five_messages_of_p5_and_six_for_each_zone(self):
        self.up()
        self.client.published.clear()
        self.svc._republish()
        configs = [p for p in self.client.published if p[0].startswith("homeassistant/") and p[0].endswith("/config")]
        self.assertEqual(len(configs), 35 + 2 * 6)
        self.assertEqual(len({p[0] for p in configs}), 47)
        self.assertTrue(all(p[2:] == (1, False) for p in configs))                    # QoS 1, not retained

    def test_a_home_assistant_birth_brings_everything_back_after_the_random_delay(self):
        self.up()
        self.learner.p6_zone("z0001", occ=True, count=1)
        self.learner.tick_outputs = [self.learner.p6_snapshot()]
        self.run_for(1)
        self.ha.stop()
        self.ha.start()
        self.run_for(14)
        self.assertEqual(self.ha.state(OCC_ID), "on")
        self.assertEqual(self.ha.state("number.radar_spot_z0001_clear_delay"), "300")
        self.assertEqual(self.ha.state("binary_sensor.radar_zones_radar_valid"), "on")

    def test_the_second_pass_ten_seconds_after_a_burst_repeats_the_zone_states(self):
        self.up()
        self.learner.p6_zone("z0001", occ=True, count=1)
        self.learner.tick_outputs = [self.learner.p6_snapshot()]
        self.run_for(1)
        self.client.published.clear()
        self.svc._republish()
        self.client.published.clear()
        self.run_for(11)
        self.assertEqual([p[1] for p in self.published(OCC)], [b"ON"])

    def test_a_connect_without_zones_publishes_only_the_availability_more_than_p5(self):
        self.learner.p5_zones = ()
        self.learner.p6_zones.clear()
        self.connect()
        self.run_for(12)
        self.assertEqual([p[1] for p in self.published(AVAIL)][-1:], [b"offline"])
        self.assertEqual([p for p in self.client.published if p[0].startswith("radar_zones/zone/") and p[0] != AVAIL], [])
        configs = [p for p in self.client.published if p[0].endswith("/config")]
        self.assertEqual(len(configs), 35)


class ForgetAllThroughTheServiceTest(FakeCoreBase, unittest.TestCase):
    def prepare(self):
        node = self.up()
        self.fresh = FakeLearner()
        self.fresh.p6_epoch = 1
        self.learner.p5_reset_result = self.fresh
        return node

    def forget(self):
        self.client.fire_message("radar_zones/learner/forget_confirm/set", "FORGET")
        self.client.fire_message("radar_zones/learner/forget_all", "PRESS")
        self.pump()

    def test_every_zone_entity_goes_and_the_layer_is_bound_to_the_new_learner(self):
        self.prepare()
        self.assertIsNotNone(self.ha.entity(OCC_ID))
        self.forget()
        self.assertIs(self.svc.learner, self.fresh)
        self.assertIs(self.layer.learner, self.fresh)
        self.assertEqual([e for e in self.ha.entity_ids() if "radar_spot" in e], [])
        self.assertEqual(self.last(AVAIL), "offline")
        self.assertEqual(self.layer.zpub.epoch, 1)
        self.assertEqual(self.svc.shell_errors, 0)

    def test_a_new_zone_with_a_reused_id_carries_the_epoch(self):
        self.prepare()
        self.forget()
        self.fresh.p5_zones = (zone_view("z0001", "New desk"),)
        self.fresh.p6_zone("z0001")
        self.fresh.p6_valid = True
        self.fresh.tick_outputs = [StatusChanged("learning", {}), self.fresh.p6_snapshot()]
        self.run_for(3)
        self.assertIsNotNone(self.ha.entity("binary_sensor.radar_spot_e1_z0001"))
        self.assertIsNone(self.ha.entity(OCC_ID))
        self.assertEqual(self.ha.state("binary_sensor.radar_spot_e1_z0001"), "off")

    def test_forget_all_is_still_refused_while_night_learning_runs(self):
        self.prepare()
        self.layer.nl_active = True
        self.forget()
        self.assertIs(self.svc.learner, self.learner)
        self.assertIsNotNone(self.ha.entity(OCC_ID))


class MainWiringTest(unittest.TestCase):
    def test_main_builds_the_p6_topics_one_p6_layer_and_attaches_the_mapper_of_the_mqtt_thread(self):
        # since P7 `main` passes the P7 layer and the P7 topics, which are the P6 ones plus the export command: the P6 part
        # asserted here is unchanged (the P7 wiring has its own test, tests/test_p7s_service.py)
        from tests._tmp import tmpdir
        quiet_logs(self)
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        options = Path(tmp.name) / "options.json"
        options.write_text(json.dumps({"debug_map": False, "node_prefix": "node_b"}))
        seen = {}
        factory = FakeClientFactory()

        class TestMqttIO(MqttIO):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, client_factory=factory, **kwargs)
                seen["mqtt"] = self

        class FakeService:
            def __init__(self, *args, **kwargs):
                seen["args"], seen["kwargs"] = args, kwargs

            def run(self, stop_event):
                return 0

            def request_stop(self):
                pass
        with mock.patch.object(service_mod, "Service", FakeService), \
                mock.patch.object(service_mod, "MqttIO", TestMqttIO), \
                mock.patch.object(service_mod, "HealthServer"), mock.patch.object(service_mod, "StallWatchdog"), \
                mock.patch.object(service_mod, "configure_logging"), mock.patch.object(service_mod.signal, "signal"):
            code = service_mod.main(["--options", str(options), "--health-port", "0"])
        self.assertEqual(code, 0)
        kwargs = seen["kwargs"]
        layer, topics, mqtt = kwargs["p4"], kwargs["topics"], seen["mqtt"]
        self.assertIsInstance(layer, P6Layer)                                       # P7: main passes the P7 layer, a P6 layer
        self.assertIsInstance(topics, TopicsP6)
        self.assertEqual(topics.frames, "node_b/radar/ld2454/frames")
        self.assertEqual(topics.node_status, "node_b/sensors/status")
        self.assertEqual(len(topics.subscriptions()), 34 + 1)                       # P7 adds the one export command
        self.assertEqual(topics.subscriptions()[:34], TopicsP6.subscriptions(topics))
        self.assertIs(mqtt.topics, topics)
        self.assertIs(layer.mapper, mqtt.mapper)
        self.assertIsInstance(mqtt.mapper, TopicMapperP6)
        self.assertEqual(layer.p6params.zone_entities_max, 24)
        self.assertEqual(layer.p6params.valve_after_episodes, 0)                    # the valve is off by default (Q13)
        self.assertIsInstance(layer.zpub._day_of(), int)                            # the valve's local day


class RealCoreTest(P6Harness, unittest.TestCase):
    """The real Learner behind the layer: what the shell does with what the core really sends."""

    def make_learner(self):
        return real_zone_learner()

    def up(self):
        self.clock.wall_ms = BASE_MS
        self.connect()
        self.node_says(NODE + "sensors/status", "online")
        self.node_says(NODE + "radar/state", "on")
        self.node_says(NODE + "radar/observing", "ON")
        self.feeder = MqttFeeder(self)
        self.feeder.seconds(0, 60_000)                                               # an empty room, the radars observed

    def zone_events(self):
        return [json.loads(p[1]) for p in self.published(LEVENT)]

    def test_a_person_walks_in_sits_and_leaves(self):
        self.up()
        self.assertEqual(self.last(AVAIL), "online")
        self.assertEqual(self.ha.state(LOCC_ID), "off")
        f = self.feeder
        f.seconds(60_000, 66_000, walk_fn(OUT, POS, 60_000, 66_000))
        f.seconds(66_000, 100_000, lambda t: [POS])
        self.assertEqual(self.ha.state(LOCC_ID), "on")
        self.assertEqual(self.zone_events(), [{"event_type": "enter", "track": "t1"}])
        f.seconds(100_000, 106_000, walk_fn(POS, OUT, 100_000, 106_000))
        f.seconds(106_000, 112_000)
        self.assertEqual(self.ha.state(LOCC_ID), "off")
        ev = self.zone_events()
        self.assertEqual([e["event_type"] for e in ev], ["enter", "leave"])
        self.assertEqual(ev[1]["via"], "edge")
        self.assertGreater(ev[1]["dwell_s"], 20)
        self.assertNotIn("Desk", json.dumps(ev))
        self.assertEqual(self.ha.states(LOCC_ID), ["unavailable", "off", "on", "off"])
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.svc.shell_errors, 0)
        self.assertEqual(self.layer.zone_shell_errors, 0)
        attrs = self.ha.attrs(LOCC_ID)
        self.assertEqual((attrs["kind"], attrs["state_source"], attrs["variant_now"]), ("manual", "live", ""))
        self.assertIsNone(attrs["confidence"])                                       # a label has no suggestion

    def test_the_tuning_entities_show_the_defaults_of_the_core_and_a_command_changes_the_delay(self):
        self.up()
        self.assertEqual(self.ha.state("number.radar_spot_l0001_clear_delay"), "300")     # manual zone: 300 s
        self.assertEqual(self.ha.state("number.radar_spot_l0001_margin"), "25")
        self.ha.number_set("number.radar_spot_l0001_clear_delay", 120)
        self.pump()
        self.assertEqual(self.ha.state("number.radar_spot_l0001_clear_delay"), "120")
        self.assertEqual(self.learner.zone6.tune["l0001"]["cd"], 120)
        self.ha.number_set("number.radar_spot_l0001_margin", -10)
        self.pump()
        self.assertEqual(self.ha.state("number.radar_spot_l0001_margin"), "-10")
        self.ha.press("button.radar_spot_l0001_forget")
        self.pump()
        self.assertEqual(self.ha.state("number.radar_spot_l0001_clear_delay"), "300")
        self.assertEqual(self.ha.state("number.radar_spot_l0001_margin"), "25")
        self.assertNotIn("l0001", self.learner.zone6.tune)
        self.assertEqual(self.svc.core_errors, 0)

    def test_a_night_with_the_radars_off_gives_unavailable_then_on_and_no_second_enter(self):
        self.up()
        f = self.feeder
        f.seconds(60_000, 66_000, walk_fn(OUT, POS, 60_000, 66_000))
        f.seconds(66_000, 100_000, lambda t: [POS])
        self.assertEqual(self.ha.state(LOCC_ID), "on")
        # 00:00: the node switches the radars off (the retained observing and state, and a batch with pw 0 and sm N)
        self.node_says(NODE + "radar/state", "night_off")
        self.node_says(NODE + "radar/observing", "OFF")
        f.pw, f.sm = 0, "N"
        f.seconds(100_000, 140_000)
        self.assertEqual(self.ha.state(LOCC_ID), "unavailable")
        self.assertEqual(self.last(AVAIL), "offline")
        self.assertEqual(self.ha.attrs(LOCC_ID)["state_source"], "radar_off")
        self.assertEqual(self.ha.state("number.radar_spot_l0001_clear_delay"), "300")      # the tuning entity stays
        # 06:00: the radars come back; the person has been sitting there all night and is seen again
        self.node_says(NODE + "radar/state", "on")
        self.node_says(NODE + "radar/observing", "ON")
        f.pw, f.sm = 3, "A"
        f.seconds(140_000, 200_000, lambda t: [POS])
        self.assertEqual(self.ha.state(LOCC_ID), "on")
        states = self.ha.states(LOCC_ID)
        self.assertEqual(states[-3:], ["on", "unavailable", "on"])                         # no off row in between
        self.assertEqual([e["event_type"] for e in self.zone_events()], ["enter"])              # no event from the power cycle
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.svc.shell_errors, 0)

    def test_the_zone_is_unavailable_from_the_start_until_the_core_has_resumed(self):
        self.clock.wall_ms = BASE_MS
        self.connect()
        self.assertEqual(self.last(AVAIL), "offline")
        self.assertEqual(self.ha.state(LOCC_ID), "unavailable")
        self.node_says(NODE + "radar/state", "on")
        self.node_says(NODE + "radar/observing", "ON")
        self.assertEqual(self.ha.state(LOCC_ID), "unavailable")                            # the shell's validity is not enough
        self.assertEqual(self.zone_events(), [])

    def test_a_forget_all_with_the_real_core_removes_the_zone_and_moves_the_epoch(self):
        self.up()
        self.assertIsNotNone(self.ha.entity(LOCC_ID))
        self.client.fire_message("radar_zones/learner/forget_confirm/set", "FORGET")
        self.client.fire_message("radar_zones/learner/forget_all", "PRESS")
        self.pump()
        self.assertEqual([e for e in self.ha.entity_ids() if "radar_spot" in e], [])
        self.assertEqual(self.layer.zpub.epoch, 1)
        self.assertEqual(self.svc.learner.zone6.epoch, 1)
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.svc.shell_errors, 0)

    def test_a_variant_request_goes_to_the_real_core_and_back_as_the_request_state_and_a_sensor(self):
        self.learner.zone6.info["l0001"] = {"th": {}, "var": [{"d": "wd", "s": 9, "e": 17, "n": 12}], "conf": None, "upd": 1}
        self.learner.review.touch()
        self.up()
        sw = "switch.radar_spot_l0001_vreq_wd0917"
        self.assertEqual(self.ha.state(sw), "off")
        self.assertIsNone(self.ha.entity("binary_sensor.radar_spot_l0001_v_wd0917"))
        self.ha.switch_set(sw, True)
        self.pump()
        self.assertEqual(self.learner.zone6.ve["l0001"], ["wd0917"])
        self.assertEqual(self.ha.state(sw), "on")
        sensor = self.ha.entity("binary_sensor.radar_spot_l0001_v_wd0917")
        self.assertIsNotNone(sensor)
        self.assertEqual(self.ha.state("binary_sensor.radar_spot_l0001_v_wd0917"), "off")      # 00:00: not in 09-17
        self.ha.switch_set(sw, False)
        self.pump()
        self.assertEqual(self.learner.zone6.ve, {})
        self.assertIsNone(self.ha.entity("binary_sensor.radar_spot_l0001_v_wd0917"))
        self.assertEqual(self.svc.core_errors, 0)

    def test_the_save_with_zone_data_round_trips_through_the_state_store(self):
        self.up()
        self.ha.number_set("number.radar_spot_l0001_clear_delay", 150)
        self.pump()
        self.assertTrue((self.dir / "state.json").exists())
        saved = json.loads((self.dir / "state.json").read_text())
        self.assertEqual(saved["p5"]["zone6"]["tune"]["l0001"]["cd"], 150)


class RealCoreNoZoneTest(P6Harness, unittest.TestCase):
    def make_learner(self):
        from radar_zone_learner.core.params import DEFAULT_PARAMS
        from radar_zone_learner.core.types import FixedOffsetTime
        return Learner(DEFAULT_PARAMS, FixedOffsetTime(0))

    def test_a_learner_with_no_zone_adds_nothing_but_the_availability_to_the_p5_app(self):
        self.clock.wall_ms = BASE_MS
        self.connect()
        self.node_says(NODE + "radar/state", "on")
        self.node_says(NODE + "radar/observing", "ON")
        feeder = MqttFeeder(self)
        feeder.seconds(0, 60_000)
        self.assertEqual([p for p in self.client.published
                          if p[0].startswith("radar_zones/zone/") and p[0] != AVAIL], [])
        self.assertEqual(len([p for p in self.client.published if p[0].endswith("/config")]), 35)
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.svc.shell_errors, 0)
        attr = json.loads(self.last("radar_zones/learner/attr"))
        self.assertEqual(attr["zone_entities"], 0)
        self.assertEqual(attr["zones_live"], 0)                                      # the core's own counters are there too


class SizeOfTheDiscoveryBurstTest(FakeCoreBase, unittest.TestCase):
    def test_a_full_set_of_zones_fits_the_queue_and_every_payload_is_below_its_limit(self):
        self.learner.p5_zones = tuple(zone_view("z%04d" % k, "Z" * 30 + str(k)) for k in range(1, 31))
        for v in self.learner.p5_zones:
            self.learner.p6_zone(v.id)
        self.up()
        configs = [p for p in self.client.published if p[0].endswith("/config") and "radar_spot" in p[0]]
        self.assertEqual(len({p[0] for p in configs}), 24 * 6)
        for topic, payload, _, _ in configs:
            self.assertLess(len(payload), 1300, topic)
        self.assertLess(self.events.qsize(), 2000)


if __name__ == "__main__":
    unittest.main()
