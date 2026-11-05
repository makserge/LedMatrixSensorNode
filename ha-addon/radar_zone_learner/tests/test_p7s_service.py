"""The service with the P7 layer (P7 spec 12.1, 9.3 ``test_p7_service``, checks P7-11 and P7-12 shell part): one layer object,
the hooks the service already has, main's wiring (the P7 topics, one P7 layer, **the production learner factory builds a learner
with ``p7params.enabled`` True**), the manifest, a ``TuningLine`` written through the service, the button, Forget all deleting the
tuning folder, and the real core behind the service making a nightly line.

Home Assistant is the fake of ``tests/fake_ha_zone.py`` (HA[U1]..HA[U7]) and ``tests/p7s_harness.py`` (HA[U8], the 'Listen to a
topic' tool). Every one of them is an assumption of the P7 spec (14.2), not a fact."""
import json
import re
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from radar_zone_learner.core.learner import Learner
from radar_zone_learner.core.params import DEFAULT_P7, DEFAULT_PARAMS
from radar_zone_learner.core.types import FixedOffsetTime, SaveRequested, StatusChanged
from radar_zone_learner.shell import service as service_mod
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.p5 import P5Layer
from radar_zone_learner.shell.p6 import P6Layer
from radar_zone_learner.shell.p7 import P7Layer
from radar_zone_learner.shell.topics_p6 import TopicsP6
from radar_zone_learner.shell.topics_p7 import TopicMapperP7, TopicsP7
from tests.fakes import FakeClientFactory, FakeLearner, quiet_logs
from tests.p6_harness import zone_view
from tests.p7s_harness import BTN_ID, EXPORT_CMD, SENSOR_ID, TUNING_EXPORT, TUNING_STATE, P7Harness, tuning_line
from tests.test_learner import BASE_MS, start

ADDON = Path(__file__).resolve().parents[1]
NODE = "led_matrix_node/"


class FakeCoreBase(P7Harness):
    def make_learner(self):
        learner = FakeLearner()
        learner.p5_zones = (zone_view("z0001", "Desk"),)
        learner.p6_zone("z0001")
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
    def test_the_layer_is_the_one_layer_object_a_p6_layer_a_p5_layer(self):
        self.up()
        self.assertIs(self.svc.p4, self.layer)
        self.assertIs(self.layer.learner, self.learner)
        self.assertIsInstance(self.layer, P7Layer)
        self.assertIsInstance(self.layer, P6Layer)
        self.assertIsInstance(self.layer, P5Layer)
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.svc.shell_errors, 0)

    def test_the_service_has_no_new_hook_for_p7(self):
        text = Path(service_mod.__file__).read_text(encoding="utf-8")
        names = set(re.findall(r'getattr\((?:self\.p4|layer), "([a-z_0-9]+)"', text))
        self.assertEqual(names, {"bind", "take_forget_request", "second_pass", "cached_inputs", "purge_capture", "reset",
                                 "notify_forgotten"})

    def test_a_tuning_line_is_consumed_by_the_layer_written_and_never_called_unknown(self):
        self.up()
        with self.assertNoLogs("radar_zone_learner", level="WARNING"):
            self.learner.tick_outputs = [tuning_line(1), SaveRequested("nightly")]
            self.run_for(2)
        self.assertEqual(self.layer.tuning.count(), 1)
        self.assertEqual(self.last(TUNING_STATE), "1 nights")
        self.assertEqual(self.svc.shell_errors, 0)

    def test_the_status_attributes_carry_the_tuning_counts_and_nothing_else_of_it(self):
        self.up()
        self.svc._publish_status(force=True)
        attr = json.loads(self.last("radar_zones/learner/attr"))
        for key in ("tuning_exports", "tuning_export_refused", "tuning_write_errors", "p7_shell_errors"):
            self.assertEqual(attr[key], 0, key)
        self.assertNotIn("Desk", json.dumps(attr))

    def test_the_layers_mapper_is_the_mqtt_threads_mapper_and_the_client_subscribes_to_35(self):
        self.up()
        self.assertIs(self.layer.mapper, self.mqtt.mapper)
        self.assertIs(type(self.mqtt.mapper), TopicMapperP7)
        self.assertEqual(len(self.client.subscriptions), 35)
        self.assertIn((EXPORT_CMD, 1), self.client.subscriptions)

    def test_an_error_in_the_p7_code_is_contained_and_the_review_path_goes_on(self):
        self.up()
        self.layer.tuning.add = mock.Mock(side_effect=RuntimeError("tuning bug"))
        self.learner.tick_outputs = [tuning_line(1)]
        with self.assertLogs("radar_zone_learner.p7", level="ERROR"):
            self.run_for(2)
        self.assertEqual(self.layer.p7_shell_errors, 1)
        self.assertEqual(self.svc.shell_errors, 0)                    # contained below the service's own net
        self.assertEqual(self.svc.core_errors, 0)


class BurstTest(FakeCoreBase, unittest.TestCase):
    def test_a_burst_is_p6s_plus_the_two_p7_configs(self):
        self.up()
        self.client.published.clear()
        self.svc._republish()
        configs = [p for p in self.client.published if p[0].startswith("homeassistant/") and p[0].endswith("/config")]
        self.assertEqual(len(configs), 35 + 2 + 6)                    # P5 and the service's four, the two of P7, one zone
        self.assertEqual(len({p[0] for p in configs}), 43)
        self.assertTrue(all(p[2:] == (1, False) for p in configs))    # QoS 1, not retained
        self.assertIn("homeassistant/button/radar_zones_learner_export_tuning/config", {p[0] for p in configs})
        self.assertIn("homeassistant/sensor/radar_zones_learner_tuning/config", {p[0] for p in configs})

    def test_the_sensor_state_is_published_retained_after_a_connect(self):
        self.up()
        pubs = self.published(TUNING_STATE)
        self.assertEqual(pubs[-1][1], b"none")
        self.assertEqual(pubs[-1][2:], (1, True))
        self.assertEqual(self.ha.state(SENSOR_ID), "none")
        self.assertIsNotNone(self.ha.entity(BTN_ID))

    def test_a_home_assistant_birth_brings_the_two_entities_back(self):
        self.up()
        self.ha.stop()
        self.ha.start(birth=False)
        self.client.published.clear()
        self.svc._republish()
        self.run_for(11)
        self.assertEqual(self.ha.state(SENSOR_ID), "none")
        self.assertIsNotNone(self.ha.entity(BTN_ID))


class ExportThroughTheServiceTest(FakeCoreBase, unittest.TestCase):
    def test_a_press_publishes_one_pooled_record_not_retained_and_a_retained_press_does_nothing(self):
        self.up()
        for n in range(1, 4):
            self.learner.tick_outputs = [tuning_line(n, sn=n)]
            self.run_for(1)
        listened = self.ha.listen(TUNING_EXPORT)
        self.client.published.clear()
        self.ha.press(BTN_ID)
        self.pump()
        got = self.published(TUNING_EXPORT)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][2:], (1, False))
        pooled = json.loads(got[0][1])
        self.assertEqual((pooled["v"], pooled["n"], pooled["sum"]["sn"]), (1, 3, 6))
        self.assertEqual(listened, [got[0][1]])
        self.assertNotIn(TUNING_EXPORT, self.ha.broker_retained)
        self.assertEqual(self.layer.tuning_exports, 1)
        self.client.published.clear()
        self.client.fire_message(EXPORT_CMD, "PRESS", retain=True)                    # a message left on the broker
        self.pump()
        self.assertEqual(self.published(TUNING_EXPORT), [])                           # never executed
        self.assertEqual([p[1:] for p in self.published(EXPORT_CMD)], [(b"", 1, True)])      # cleared

    def test_the_pooled_record_has_no_per_night_series_no_date_and_no_name(self):
        self.up()
        for n in range(1, 8):
            self.learner.tick_outputs = [tuning_line(n)]
            self.run_for(1)
        self.client.published.clear()
        self.ha.press(BTN_ID)
        self.pump()
        text = self.published(TUNING_EXPORT)[0][1].decode()
        self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}")
        self.assertNotIn("Desk", text)
        self.assertNotIn("z0001", text)
        self.assertEqual(set(json.loads(text)), {"v", "n", "sum", "last"})

    def test_a_press_with_nothing_kept_publishes_nothing(self):
        self.up()
        self.client.published.clear()
        self.ha.press(BTN_ID)
        self.pump()
        self.assertEqual(self.published(TUNING_EXPORT), [])
        self.assertEqual(self.layer.tuning_export_refused, 1)


class ForgetAllThroughTheServiceTest(FakeCoreBase, unittest.TestCase):
    def forget(self):
        self.client.fire_message("radar_zones/learner/forget_confirm/set", "FORGET")
        self.client.fire_message("radar_zones/learner/forget_all", "PRESS")
        self.pump()

    def test_forget_all_deletes_the_tuning_folder_and_the_sensor_says_none(self):
        self.up()
        self.fresh = FakeLearner()
        self.fresh.p6_epoch = 1
        self.learner.p5_reset_result = self.fresh
        for n in range(1, 4):
            self.learner.tick_outputs = [tuning_line(n)]
            self.run_for(1)
        self.assertTrue((self.tuning_dir / "nights.jsonl").exists())
        self.assertEqual(self.ha.state(SENSOR_ID), "3 nights")
        self.forget()
        self.assertIs(self.svc.learner, self.fresh)
        self.assertFalse(self.tuning_dir.exists())
        self.assertEqual(self.ha.state(SENSOR_ID), "none")
        self.assertEqual(self.layer.tuning.count(), 0)
        self.assertEqual(self.svc.shell_errors, 0)
        # the new learner's first night is written, not dropped as a duplicate of the old night 3
        self.fresh.tick_outputs = [tuning_line(1)]
        self.run_for(2)
        self.assertEqual(self.layer.tuning.count(), 1)

    def test_forget_all_leaves_the_rf_numbers_and_the_options_alone(self):
        self.up()
        self.learner.p5_reset_result = FakeLearner()
        (self.dir / "rf_stats.json").write_text("{}")
        self.learner.tick_outputs = [tuning_line(1)]
        self.run_for(1)
        self.forget()
        self.assertTrue((self.dir / "rf_stats.json").exists())


class MainWiringTest(unittest.TestCase):
    def run_main(self):
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
        return seen, tmp

    def test_main_builds_the_p7_topics_and_one_p7_layer_and_attaches_the_mapper_of_the_mqtt_thread(self):
        seen, _ = self.run_main()
        kwargs = seen["kwargs"]
        layer, topics, mqtt = kwargs["p4"], kwargs["topics"], seen["mqtt"]
        self.assertIs(type(layer), P7Layer)
        self.assertIsInstance(layer, P6Layer)
        self.assertIsInstance(topics, TopicsP7)
        self.assertIsInstance(topics, TopicsP6)
        self.assertEqual(topics.frames, "node_b/radar/ld2454/frames")
        self.assertEqual(len(topics.subscriptions()), 35)
        self.assertIs(mqtt.topics, topics)
        self.assertIs(layer.mapper, mqtt.mapper)
        self.assertIs(type(mqtt.mapper), TopicMapperP7)
        self.assertEqual(layer.p6params.zone_entities_max, 24)
        self.assertEqual(layer.p6params.valve_after_episodes, 0)
        self.assertTrue(layer.p7params.enabled)
        self.assertIsNotNone(layer.tuning)

    def test_the_production_learner_factory_builds_a_learner_with_p7_enabled(self):
        seen, _ = self.run_main()
        factory = seen["args"][1]
        fresh = factory(None)
        self.assertIsInstance(fresh, Learner)
        self.assertTrue(fresh.p7params.enabled)
        self.assertTrue(fresh.p7on)
        self.assertEqual(replace(fresh.p7params, enabled=False), DEFAULT_P7)           # only the switch differs from the defaults
        # a saved state goes through the same factory (the store validates with it), and a reset keeps the switch
        again = factory(fresh.export_state())
        self.assertTrue(again.p7on)
        self.assertTrue(again.reset().p7on)
        self.assertEqual(seen["args"][2].__class__.__name__, "TestMqttIO")

    def test_the_default_learner_stays_a_p6_learner(self):
        # D4: the dataclass default is False, so every other user of the core is unchanged
        self.assertFalse(DEFAULT_P7.enabled)
        self.assertFalse(Learner(DEFAULT_PARAMS, FixedOffsetTime(0)).p7on)

    def test_the_layer_and_the_learner_use_the_same_p7_constants(self):
        seen, _ = self.run_main()
        self.assertEqual(seen["kwargs"]["p4"].p7params, seen["args"][1](None).p7params)


class ManifestTest(unittest.TestCase):
    def lines(self):
        return (ADDON / "config.yaml").read_text(encoding="utf-8").splitlines()

    def test_version_description_and_no_new_option(self):
        lines = self.lines()
        self.assertIn('version: "0.7.0"', lines)
        self.assertTrue(any(ln.startswith("description: ") and "(P7)" in ln for ln in lines))
        i = lines.index("options:")
        j = lines.index("schema:")
        self.assertEqual([ln.split(":")[0].strip() for ln in lines[i + 1:j]],
                         ["log_level", "node_prefix", "discovery_prefix", "debug_map", "night_lux"])      # every setting is an entity

    def test_backup_exclude_has_the_capture_and_the_tuning_entries(self):
        lines = self.lines()
        i = lines.index("backup_exclude:")
        self.assertEqual(lines[i + 1:i + 3], ['  - "capture/*"', '  - "tuning/*"'])
        self.assertTrue(lines[i + 3].startswith("options:"))

    def test_the_tuning_folder_the_layer_writes_is_the_one_the_pattern_names(self):
        from pathlib import PurePosixPath
        from radar_zone_learner.shell.tuningexport import NIGHTS_NAME, TUNING_DIR
        self.assertEqual(TUNING_DIR + "/*", "tuning/*")
        self.assertTrue((PurePosixPath("/data") / TUNING_DIR / NIGHTS_NAME).match("tuning/*"))


class RealCoreTest(P7Harness, unittest.TestCase):
    """The real Learner with P7 enabled behind the real service: a night passes and its count line is written."""

    def make_learner(self):
        L = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), None, p7params=replace(DEFAULT_P7, enabled=True))
        start(L)
        return L

    def up(self):
        self.clock.wall_ms = BASE_MS
        self.connect()
        self.node_says(NODE + "sensors/status", "online")
        self.node_says(NODE + "radar/state", "on")
        self.node_says(NODE + "radar/observing", "ON")

    def night(self):
        self.clock.advance(4 * 3600)             # past 03:30: the nightly run
        self.svc.step(0)
        self.pump()

    def test_a_nightly_run_writes_one_clean_line_and_the_sensor_follows(self):
        from radar_zone_learner.core import tuningagg
        self.up()
        self.assertTrue(self.learner.p7on)
        self.assertEqual(self.last(TUNING_STATE), "none")
        self.night()
        self.assertEqual(self.learner.night, 0)
        self.assertEqual(self.layer.tuning.count(), 1)
        self.assertEqual(self.last(TUNING_STATE), "1 nights")
        line = self.layer.tuning.lines()[0]
        self.assertEqual(tuningagg.scan(line), [])
        self.assertNotRegex(line, r"\d{4}-\d{2}-\d{2}")
        self.assertEqual(self.svc.core_errors, 0)
        self.assertEqual(self.svc.shell_errors, 0)

    def test_the_button_after_a_real_night_publishes_the_pooled_record(self):
        from radar_zone_learner.core import tuningagg
        self.up()
        self.night()
        self.ha.listen(TUNING_EXPORT)
        self.client.published.clear()
        self.ha.press(BTN_ID)
        self.pump()
        got = self.published(TUNING_EXPORT)
        self.assertEqual(len(got), 1)
        pooled = json.loads(got[0][1])
        self.assertEqual(pooled["n"], 1)
        self.assertEqual(tuningagg.scan(pooled), [])
        self.assertFalse(got[0][3])

    def test_the_state_saved_after_the_night_holds_no_tuning_line(self):
        self.up()
        self.night()
        saved = (self.dir / "state.json").read_text(encoding="utf-8") if (self.dir / "state.json").exists() else ""
        self.assertNotIn("nights.jsonl", saved)
        self.assertEqual(sorted(p.name for p in self.tuning_dir.iterdir()), ["last_night", "nights.jsonl"])


if __name__ == "__main__":
    unittest.main()
