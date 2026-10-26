import dataclasses
import json
import logging
import queue
import threading
import unittest
from pathlib import Path

from radar_zone_learner.core.types import (SCHEMA_VERSION, DebugMapChanged, FramesMessage, MarkResult, NameEcho,
                                           NightlyDone, NightlyFailed, SaveRequested, StatusChanged)
from radar_zone_learner.shell import service as service_mod
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.persistence import StateStore
from radar_zone_learner.shell.topics import build_topics
from tests._tmp import tmpdir
from tests.fakes import FakeClientFactory, FakeClock, FakeLearner, quiet_logs

FRAMES = "led_matrix_node/radar/ld2454/frames"
STATUS = "radar_zones/learner/status"
STATE = "radar_zones/learner/state"
ATTR = "radar_zones/learner/attr"
MAP = "radar_zones/learner/debug_map"
NAME = "radar_zones/review/name"
DISCOVERY_SENSOR = "homeassistant/sensor/radar_zones_learner_status/config"


class FixedRandom:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def uniform(self, a, b):
        self.calls.append((a, b))
        return self.value


class Stub:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def beat(self):
        self.calls.append("beat")


class ServiceTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self._tmp = tmpdir()
        self.dir = Path(self._tmp.name)
        self.settings = Settings(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                                 debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                                 mqtt_password=None, mqtt_ssl=False, data_dir=self.dir)
        self.clock = FakeClock()
        self.events = queue.Queue(maxsize=2000)
        self.factory = FakeClientFactory()
        self.topics = build_topics(self.settings)
        self.mqtt = MqttIO(self.settings, self.topics, self.events, client_factory=self.factory,
                           clock_ms=self.clock.clock_ms, monotonic=self.clock.monotonic)
        self.learners = []
        self.store = StateStore(self.dir, validate=self.make_learner)
        self.health, self.watchdog = Stub(), Stub()
        self.rng = FixedRandom(4.2)
        self.svc = service_mod.Service(self.settings, self.make_learner, self.mqtt, self.store, self.health,
                                       self.watchdog, self.clock.clock_ms, self.clock.monotonic,
                                       topics=self.topics, events=self.events, rng=self.rng, rss=lambda: 24.12)

    def tearDown(self):
        self._tmp.cleanup()

    def make_learner(self, state):
        learner = FakeLearner(state)
        self.learners.append(learner)
        return learner

    @property
    def client(self):
        return self.factory.last

    def start_connected(self):
        self.svc.start()
        self.client.fire_connect(0)
        self.svc.step(0)                       # the Republish marker
        self.client.published.clear()

    def published(self, topic):
        return self.client.published_to(topic)

    # ------------------------------------------------------------ start and republish
    def test_start_publishes_status_after_connect(self):
        self.svc.start()
        self.assertEqual(self.health.calls, ["start"])
        self.assertEqual(self.watchdog.calls, ["start"])
        self.assertEqual(self.client.published, [])          # not connected yet: nothing sent
        self.client.fire_connect(0)
        self.svc.step(0)
        topics = [p[0] for p in self.client.published]
        self.assertEqual(topics[0], STATUS)                    # 'online' from on_connect
        self.assertIn(DISCOVERY_SENSOR, topics)
        self.assertLess(topics.index(DISCOVERY_SENSOR), topics.index(STATE))
        self.assertEqual(self.published(STATE), [(STATE, b"starting", 1, True)])
        attr = json.loads(self.published(ATTR)[0][1])
        self.assertEqual(attr["mode"], "shadow")
        self.assertEqual(attr["rss_mb"], 24.1)
        for key in ("cpu_pct_1h", "queue_dropped", "payload_oversize", "mqtt_reconnects", "state_saved",
                    "state_bytes", "core_errors"):
            self.assertIn(key, attr)
        png = self.published(MAP)
        self.assertEqual(len(png), 1)
        self.assertTrue(png[0][1].startswith(b"\x89PNG"))
        self.assertEqual(png[0][2:], (0, False))
        self.assertEqual(self.published(NAME), [(NAME, b"", 1, True)])
        discovery = [p for p in self.client.published if p[0].endswith("/config")]
        self.assertEqual(len(discovery), 4)
        self.assertTrue(all(p[3] is False for p in discovery))  # never retained
        self.assertEqual(self.watchdog.calls[-1], "beat")

    def test_debug_map_off(self):
        self.settings = dataclasses.replace(self.settings, debug_map=False)
        self.svc.settings = self.settings
        self.start_connected()
        self.svc._execute(DebugMapChanged("label"))
        self.assertEqual(self.published(MAP), [])

    def test_ha_birth_republishes_after_random_delay(self):
        self.start_connected()
        self.client.fire_message("homeassistant/status", "online")
        self.svc.step(0)
        self.assertEqual(self.rng.calls, [(0.0, 10.0)])
        self.clock.advance(4.1)
        self.svc.step(0)
        self.assertEqual(self.published(DISCOVERY_SENSOR), [])
        self.clock.advance(0.1)
        self.svc.step(0)
        self.assertEqual(len(self.published(DISCOVERY_SENSOR)), 1)
        self.assertEqual(len(self.published(STATE)), 1)       # state republished with the burst
        self.clock.advance(20)
        self.svc.step(0)
        self.assertEqual(len(self.published(DISCOVERY_SENSOR)), 1)

    # ------------------------------------------------------------ events and ticks
    def test_frames_reach_the_learner_and_ticks(self):
        self.start_connected()
        learner = self.learners[-1]
        self.client.fire_message(FRAMES, b'{"v":1}')
        self.svc.step(0)
        self.assertEqual(learner.events, [FramesMessage(b'{"v":1}', self.clock.wall_ms)])
        ticks = len(learner.ticks)
        self.clock.advance(0.5)
        self.svc.step(0)
        self.assertEqual(len(learner.ticks), ticks)            # < 1 s since the last tick
        self.clock.advance(0.5)
        self.svc.step(0)
        self.assertEqual(len(learner.ticks), ticks + 1)
        self.assertEqual(learner.ticks[-1], self.clock.wall_ms)

    def test_state_published_only_on_change_attr_always(self):
        self.start_connected()
        learner = self.learners[-1]
        learner.queued = [StatusChanged("learning", {"mode": "shadow"})]
        self.svc.events.put(FramesMessage(b"{}", 1))
        self.svc.step(0)
        learner.queued = [StatusChanged("learning", {"mode": "shadow", "nights": 1})]
        self.svc.events.put(FramesMessage(b"{}", 2))
        self.svc.step(0)
        self.assertEqual(self.published(STATE), [(STATE, b"learning", 1, True)])
        self.assertEqual(len(self.published(ATTR)), 2)
        self.assertEqual(json.loads(self.published(ATTR)[-1][1])["nights"], 1)

    def test_outputs_executed(self):
        self.start_connected()
        learner = self.learners[-1]
        learner.queued = [MarkResult(True, None, "l0001", "bed"), NameEcho("Bed"),
                          NightlyDone(3, "2026-01-08", 4, 1), StatusChanged("learning", {}),
                          DebugMapChanged("nightly"), SaveRequested("nightly")]
        learner.spot_name = "Bed"
        self.svc.events.put(FramesMessage(b"{}", 1))
        with self.assertLogs("radar_zone_learner", level="INFO") as logs:
            self.svc.step(0)
        text = "\n".join(logs.output)
        self.assertIn("mark ok l0001 bed", text)
        self.assertIn("nightly run: night 3 (2026-01-08), 4 candidates, 1 new suggestions", text)
        self.assertEqual(self.published(NAME), [(NAME, b"Bed", 1, True)])
        self.assertEqual(len(self.published(MAP)), 1)
        saved = json.loads((self.dir / "state.json").read_text())
        self.assertEqual(saved["settings"]["spot_name"], "Bed")
        self.assertTrue((self.dir / "snapshots" / "state-2026-01-08.json").exists())
        self.assertEqual(self.svc.state_bytes, len((self.dir / "state.json").read_bytes()))
        self.assertEqual(self.svc.state_saved_ms, self.clock.wall_ms)

    def test_save_requested_writes_file(self):
        self.start_connected()
        self.learners[-1].queued = [SaveRequested("label")]
        self.svc.events.put(FramesMessage(b"{}", 1))
        self.svc.step(0)
        self.assertEqual(json.loads((self.dir / "state.json").read_text())["events"], 1)
        self.assertFalse((self.dir / "snapshots").exists())

    def test_periodic_save(self):
        self.start_connected()
        self.clock.advance(1799)
        self.svc.step(0)
        self.assertFalse((self.dir / "state.json").exists())
        self.clock.advance(1)
        self.svc.step(0)
        self.assertTrue((self.dir / "state.json").exists())

    def test_mark_failed_logged(self):
        self.start_connected()
        self.learners[-1].queued = [MarkResult(False, "no_stay", None, None)]
        self.svc.events.put(FramesMessage(b"{}", 1))
        with self.assertLogs("radar_zone_learner", level="INFO") as logs:
            self.svc.step(0)
        self.assertIn("mark failed no_stay", "\n".join(logs.output))

    def test_debug_map_reaches_a_late_subscriber(self):
        """Home Assistant subscribes to the image topic only after it has created the entity from the config
        message (a fraction of a second to several seconds later); the PNG is not retained, so the copy sent
        with the burst is lost to it. The map is sent again 10 s after the burst."""
        log = []
        self.svc.start()
        original = self.client.publish

        def recording(topic, payload=None, qos=0, retain=False, **kw):
            log.append((self.clock.mono, topic))
            return original(topic, payload, qos, retain, **kw)

        self.client.publish = recording
        self.client.fire_connect(0)
        self.svc.step(0)
        for _ in range(30):
            self.clock.advance(1.0)
            self.svc.step(0)
        t_cfg = [t for t, topic in log if topic.endswith("radar_zones_learner_debug_map/config")][0]
        maps = [t for t, topic in log if topic == MAP]
        self.assertEqual(len(maps), 2)
        self.assertEqual(maps[0], t_cfg)
        self.assertTrue(maps[1] >= t_cfg + 5.0, maps)             # a subscriber that came 200 ms or 5 s after the config
        # and after a Home Assistant birth message
        self.client.fire_message("homeassistant/status", "online")
        for _ in range(30):
            self.clock.advance(1.0)
            self.svc.step(0)
        self.assertEqual(len([1 for _, topic in log if topic == MAP]), 4)

    def test_name_and_label_saves_are_not_written_every_time(self):
        self.start_connected()
        learner = self.learners[-1]
        writes = []
        real = self.store.save
        self.store.save = lambda state: (writes.append(1), real(state))[1]
        for i in range(20):
            learner.queued = [SaveRequested("name" if i % 2 else "label")]
            self.svc.events.put(FramesMessage(b"{}", i))
            self.svc.step(0)
            self.clock.advance(0.2)
        self.assertEqual(len(writes), 1)                       # the first at once, the others wait
        self.clock.advance(10)
        self.svc.step(0)
        self.assertEqual(len(writes), 2)                       # ... and are written together
        self.clock.advance(10)
        self.svc.step(0)
        self.assertEqual(len(writes), 2)                       # nothing pending any more
        learner.queued = [SaveRequested("nightly")]            # a nightly run is never deferred
        self.svc.events.put(FramesMessage(b"{}", 99))
        self.svc.step(0)
        self.assertEqual(len(writes), 3)
        learner.queued = [SaveRequested("name")]               # a pending one is covered by a later write
        self.svc.events.put(FramesMessage(b"{}", 100))
        self.svc.step(0)
        self.svc.events.put(FramesMessage(b"{}", 101))
        learner.queued = [SaveRequested("name")]
        self.svc.step(0)
        n = len(writes)
        self.svc.shutdown(0)
        self.assertEqual(len(writes), n + 1)                   # the final save always writes

    def test_refused_broker_exits_72_and_saves(self):
        self.start_connected()
        self.client.fire_connect(5)                            # refused: not authorized
        self.clock.advance(299)
        self.svc.step(0)
        self.assertIsNone(self.svc._exit_code)
        self.clock.advance(2)
        with self.assertLogs("radar_zone_learner", level="CRITICAL"):
            self.svc.step(0)
        self.assertEqual(self.svc._exit_code, 72)
        self.svc.shutdown(72)
        self.assertTrue((self.dir / "state.json").exists())     # nothing is wrong with the state

    def test_nightly_failed_is_logged(self):
        self.start_connected()
        self.learners[-1].queued = [NightlyFailed("2026-01-08", "ValueError at core.py:3 (f)")]
        self.svc.events.put(FramesMessage(b"{}", 1))
        with self.assertLogs("radar_zone_learner", level="ERROR") as logs:
            self.svc.step(0)
        self.assertIn("nightly run 2026-01-08 failed: ValueError at core.py:3 (f)", " ".join(logs.output))

    # ------------------------------------------------------------ errors
    def test_core_exception_counted_and_loop_continues(self):
        self.start_connected()
        learner = self.learners[-1]
        learner.raise_on_handle = RuntimeError("core bug")
        self.svc.events.put(FramesMessage(b"{}", 1))
        with self.assertLogs("radar_zone_learner", level="ERROR"):
            self.svc.step(0)
        self.assertEqual(self.svc.core_errors, 1)
        learner.raise_on_handle = None
        learner.queued = [SaveRequested("name")]
        self.svc.events.put(FramesMessage(b"{}", 2))
        self.svc.step(0)
        self.assertTrue((self.dir / "state.json").exists())
        self.assertIsNone(self.svc._exit_code)

    def test_repeated_core_errors_exit_71(self):
        self.start_connected()
        self.learners[-1].raise_on_handle = RuntimeError("core bug")
        with self.assertLogs("radar_zone_learner", level="ERROR"):
            for _ in range(101):
                self.svc.events.put(FramesMessage(b"{}", 1))
                self.svc.step(0)
                self.clock.advance(5)
        self.assertEqual(self.svc._exit_code, 71)

    def test_spread_out_errors_do_not_exit(self):
        self.start_connected()
        self.learners[-1].raise_on_handle = RuntimeError("core bug")
        with self.assertLogs("radar_zone_learner", level="ERROR"):
            for _ in range(150):
                self.svc.events.put(FramesMessage(b"{}", 1))
                self.svc.step(0)
                self.clock.advance(7)                         # 600 s hold about 86 errors
        self.assertIsNone(self.svc._exit_code)

    def test_save_failure_logged_not_raised(self):
        self.start_connected()

        def failing_save(state):
            raise OSError("read-only file system")

        self.store.save = failing_save
        self.learners[-1].queued = [SaveRequested("label")]
        self.svc.events.put(FramesMessage(b"{}", 1))
        with self.assertLogs("radar_zone_learner", level="ERROR"):
            self.svc.step(0)
        self.assertEqual(self.svc.save_failures, 1)

    # ------------------------------------------------------------ state across restarts
    def test_state_loaded_and_spot_name_echoed(self):
        self.store.save({"schema": 3, "settings": {"spot_name": "Desk"}})
        self.start_connected()
        self.assertEqual(self.learners[-1].state_in["settings"]["spot_name"], "Desk")
        self.assertEqual(len(self.learners), 1)                # validated learner reused, built once
        self.client.fire_connect(0)
        self.svc.step(0)
        self.assertEqual(self.published(NAME), [(NAME, b"Desk", 1, True)])

    def test_rejected_state_quarantined(self):
        (self.dir / "state.json").write_text('{"schema": 3}')

        def picky(state):
            if state is not None:
                raise ValueError("bad grids")
            return FakeLearner(None)

        self.store.validate = None
        self.svc.learner_factory = picky
        with self.assertLogs("radar_zone_learner", level="WARNING"):
            self.svc.start()
        self.assertIsInstance(self.svc.learner, FakeLearner)
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertTrue(any(n.startswith("state.json.corrupt-") for n in names), names)

    # ------------------------------------------------------------ stop
    def test_request_stop_saves_publishes_offline_returns_0(self):
        stop_event = threading.Event()
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("code", self.svc.run(stop_event)))
        thread.start()
        for _ in range(500):
            if self.factory.clients:
                break
            threading.Event().wait(0.01)
        self.client.fire_connect(0)
        self.svc.request_stop()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["code"], 0)
        self.assertTrue((self.dir / "state.json").exists())
        self.assertEqual(self.client.published[-1], (STATUS, b"offline", 1, True))
        self.assertTrue(self.client.disconnected and self.client.loop_stopped)
        self.assertEqual(self.health.calls, ["start", "stop"])
        self.assertEqual(self.watchdog.calls[-1], "stop")

    def test_stop_event_from_signal_path(self):
        stop_event = threading.Event()
        stop_event.set()
        self.assertEqual(self.svc.run(stop_event), 0)
        self.assertTrue((self.dir / "state.json").exists())

    def test_start_failure_returns_1(self):
        class BadHealth(Stub):
            def start(self):
                raise OSError("address in use")

        self.svc.health = BadHealth()
        with self.assertLogs("radar_zone_learner", level="ERROR"):
            self.assertEqual(self.svc.run(threading.Event()), 1)
        self.assertFalse((self.dir / "state.json").exists())


class MainTest(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        self._saved = (list(root.handlers), root.level)

    def tearDown(self):
        root = logging.getLogger()
        root.handlers[:] = self._saved[0]
        root.setLevel(self._saved[1])

    def test_invalid_options_exit_1(self):
        with tmpdir() as d:
            path = Path(d) / "options.json"
            path.write_text('{"log_level": "loud"}', encoding="utf-8")
            with self.assertLogs("radar_zone_learner", level="ERROR"):
                self.assertEqual(service_mod.main(["--options", str(path)]), 1)


class TimeModelTest(unittest.TestCase):
    def test_utc_matches_fixed_offset(self):
        from radar_zone_learner.core.types import FixedOffsetTime
        from radar_zone_learner.shell.timemodel import ZoneInfoTime
        tm = ZoneInfoTime("UTC")
        for ms in (0, 1_767_571_200_000, 1_767_571_200_000 + 3_599_999, 1_769_330_700_123):
            self.assertEqual(tm.local(ms), FixedOffsetTime(0).local(ms))

    def test_unknown_zone_falls_back_to_utc(self):
        from radar_zone_learner.shell.timemodel import ZoneInfoTime
        with self.assertLogs("radar_zone_learner.time", level="WARNING"):
            tm = ZoneInfoTime("Mars/Olympus_Mons")
        self.assertEqual(tm.name, "UTC")
        self.assertEqual(tm.local(1_767_571_200_000).sec_of_day, 0)

    def test_dst_zone_when_available(self):
        import zoneinfo
        from radar_zone_learner.shell.timemodel import ZoneInfoTime
        try:
            zoneinfo.ZoneInfo("Europe/Berlin")
        except Exception:
            self.skipTest("no time zone database on this system")
        tm = ZoneInfoTime("Europe/Berlin")
        winter = tm.local(1_767_571_200_000)              # 2026-01-05 00:00 UTC = 01:00 CET
        self.assertEqual((winter.weekday, winter.sec_of_day), (0, 3600))
        summer = tm.local(1_782_864_000_000)              # 2026-07-01 00:00 UTC = 02:00 CEST
        self.assertEqual(summer.sec_of_day, 7200)


class MainWiringTest(unittest.TestCase):
    """main() end to end with a fake MQTT client, once CORE's Learner works."""

    def test_main_starts_and_stops(self):
        import signal as signal_mod
        from unittest import mock
        from radar_zone_learner.core.learner import Learner
        from radar_zone_learner.core.params import DEFAULT_PARAMS
        from radar_zone_learner.core.types import FixedOffsetTime
        try:
            Learner(DEFAULT_PARAMS, FixedOffsetTime(0), None)
        except NotImplementedError:
            self.skipTest("core Learner not implemented yet")
        factory = FakeClientFactory()

        class TestMqttIO(MqttIO):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, client_factory=factory, **kwargs)

        class OneStepService(service_mod.Service):
            def run(self, stop_event):
                stop_event.set()
                return super().run(stop_event)

        root = logging.getLogger()
        saved = (list(root.handlers), root.level)
        handlers = {s: signal_mod.getsignal(s) for s in (signal_mod.SIGTERM, signal_mod.SIGINT)}
        try:
            with tmpdir() as d, mock.patch.object(service_mod, "MqttIO", TestMqttIO),                     mock.patch.object(service_mod, "Service", OneStepService):
                (Path(d) / "options.json").write_text('{"log_level": "warning"}', encoding="utf-8")
                code = service_mod.main(["--options", str(Path(d) / "options.json"), "--health-port", "0"])
                self.assertEqual(code, 0)
                self.assertTrue((Path(d) / "state.json").exists())
                self.assertTrue(factory.last.loop_started and factory.last.loop_stopped)
        finally:
            for s, h in handlers.items():
                signal_mod.signal(s, h)
            root.handlers[:] = saved[0]
            root.setLevel(saved[1])


class RealLearnerTest(unittest.TestCase):
    """The service with CORE's Learner, once CORE has implemented it."""

    def test_service_with_real_learner(self):
        from radar_zone_learner.core.learner import Learner
        from radar_zone_learner.core.params import DEFAULT_PARAMS
        from radar_zone_learner.core.types import FixedOffsetTime
        try:
            Learner(DEFAULT_PARAMS, FixedOffsetTime(0), None)
        except NotImplementedError:
            self.skipTest("core Learner not implemented yet")
        with tmpdir() as d:
            settings = Settings(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                                debug_map=True, mqtt_host="h", mqtt_port=1883, mqtt_username=None,
                                mqtt_password=None, mqtt_ssl=False, data_dir=Path(d))
            clock = FakeClock()
            events = queue.Queue()
            factory = FakeClientFactory()
            topics = build_topics(settings)
            mqtt = MqttIO(settings, topics, events, client_factory=factory, clock_ms=clock.clock_ms)

            def make(state):
                return Learner(DEFAULT_PARAMS, FixedOffsetTime(0), state)

            svc = service_mod.Service(settings, make, mqtt, StateStore(Path(d), validate=make), Stub(), Stub(),
                                      clock.clock_ms, clock.monotonic, topics=topics, events=events)
            svc.start()
            factory.last.fire_connect(0)
            svc.step(0)
            payload = (b'{"v":1,"b":"9f3c21aa","t":81234567,"s":1234560,"n":5,"a0":480,"a1":40,"mt":1,'
                       b'"p2":1,"pw":3,"sm":"A","f":[[0,480,0,1200,2300,0]]}')
            factory.last.fire_message(FRAMES, payload)
            factory.last.fire_message(FRAMES, b"not json")
            for _ in range(3):
                clock.advance(1.0)
                svc.step(0)
            self.assertEqual(svc.core_errors, 0)
            self.assertTrue(factory.last.published_to(STATE))
            png = factory.last.published_to(MAP)
            self.assertTrue(png and png[-1][1].startswith(b"\x89PNG"))
            svc.shutdown(0)
            saved = json.loads((Path(d) / "state.json").read_text())
            self.assertEqual(saved["schema"], SCHEMA_VERSION)
            svc2 = service_mod.Service(settings, make, mqtt, StateStore(Path(d), validate=make), Stub(), Stub(),
                                       clock.clock_ms, clock.monotonic, topics=topics, events=events)
            self.assertIsInstance(svc2._load_learner(), Learner)


if __name__ == "__main__":
    unittest.main()
