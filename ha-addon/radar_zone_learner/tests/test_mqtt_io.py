import queue
import subprocess
import sys
import unittest
from pathlib import Path

from radar_zone_learner.core.types import (ContextState, FramesMessage, MarkPress, NodeStatus, ScheduleState,
                                           SpotName)
from radar_zone_learner.shell.mqtt_io import HaBirth, MqttIO, Republish
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.topics import build_topics, parse_hhmm, parse_spot_name
from tests.fakes import FakeClientFactory, quiet_logs

ADDON = Path(__file__).resolve().parents[1]
SHELL_MODULES = ("options", "topics", "mqtt_io", "discovery", "persistence", "timemodel", "health", "png",
                 "render", "resources", "service", "p4", "p4_types", "topics_p4", "discovery_p4", "review", "nodelink",
                 "rfstats", "p5", "p5_types", "topics_p5", "discovery_p5", "select_model", "review_p5", "reviewmap",
                 "capture")

# Spec 6.1, in order, with QoS.
EXPECTED_SUBSCRIPTIONS = [
    ("led_matrix_node/radar/ld2454/frames", 0),
    ("led_matrix_node/sensors/status", 1),
    ("led_matrix_node/radar/schedule/state", 1),
    ("led_matrix_node/radar/night_start/state", 1),
    ("led_matrix_node/radar/night_end/state", 1),
    ("radar_zones/context/home_empty", 1),
    ("radar_zones/review/mark", 1),
    ("radar_zones/review/name/set", 1),
    ("homeassistant/status", 1),
]


def settings(**changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                  debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username="addons",
                  mqtt_password="pw", mqtt_ssl=False, data_dir=Path("."))
    values.update(changes)
    return Settings(**values)


class LazyImportTest(unittest.TestCase):
    def test_shell_modules_import_without_paho(self):
        # A fresh interpreter whose import system refuses and records any attempt to import paho.
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(ADDON)!r})\n"
            "attempts = []\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'paho' or name.startswith('paho.'):\n"
            "            attempts.append(name)\n"
            "            raise ImportError('blocked')\n"
            "        return None\n"
            "sys.meta_path.insert(0, Block())\n"
            "import importlib\n"
            f"for m in {SHELL_MODULES!r}:\n"
            "    importlib.import_module('radar_zone_learner.shell.' + m)\n"
            "print(repr((attempts, 'paho' in sys.modules)))\n"
        )
        out = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "([], False)")


class MqttIOTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.events = queue.Queue(maxsize=20)
        self.factory = FakeClientFactory()
        self.now = [1_767_571_200_123]
        self.s = settings()
        self.topics = build_topics(self.s)
        self.io = MqttIO(self.s, self.topics, self.events, client_factory=self.factory,
                         clock_ms=lambda: self.now[0])
        self.io.start()
        self.c = self.factory.last

    def drain(self):
        items = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                return items

    def connect(self):
        self.c.fire_connect(0)
        self.assertIsInstance(self.drain()[-1], Republish)
        self.c.published.clear()

    def test_client_setup(self):
        c = self.c
        self.assertRegex(c.client_id, r"^radar_zone_learner-[0-9a-f]{8}$")
        self.assertEqual(c.will, ("radar_zones/learner/status", "offline", 1, True))
        self.assertEqual(c.credentials, ("addons", "pw"))
        self.assertFalse(c.tls)
        self.assertEqual(c.reconnect_delay, (1, 30))
        self.assertEqual(c.connect_args, ("core-mosquitto", 1883, 30))
        self.assertTrue(c.loop_started)

    def test_no_credentials_and_tls(self):
        factory = FakeClientFactory()
        io = MqttIO(settings(mqtt_username=None, mqtt_password=None, mqtt_ssl=True), self.topics,
                    queue.Queue(), client_factory=factory)
        io.start()
        self.assertIsNone(factory.last.credentials)
        self.assertTrue(factory.last.tls)

    def test_connect_subscribes_and_announces(self):
        self.assertFalse(self.io.connected)
        self.c.fire_connect(0)
        self.assertTrue(self.io.connected)
        self.assertEqual(self.c.subscriptions, EXPECTED_SUBSCRIPTIONS)
        self.assertEqual(self.c.published, [("radar_zones/learner/status", b"online", 1, True)])
        self.assertEqual(self.drain(), [Republish()])

    def test_refused_connection(self):
        self.c.fire_connect(5)
        self.assertFalse(self.io.connected)
        self.assertEqual(self.c.subscriptions, [])
        self.assertEqual(self.drain(), [])

    def test_republish_marker_when_queue_full(self):
        for _ in range(20):
            self.events.put_nowait(object())
        self.io.connect_put_timeout_s = 0.01
        self.c.fire_connect(0)
        self.assertTrue(self.io.take_republish_request())
        self.assertFalse(self.io.take_republish_request())

    def test_topic_mapping(self):
        self.connect()
        t = self.now[0]
        c = self.c
        c.fire_message("led_matrix_node/radar/ld2454/frames", b'{"v":1}')
        c.fire_message("led_matrix_node/sensors/status", "Online", retain=True)
        c.fire_message("led_matrix_node/sensors/status", "offline")
        c.fire_message("led_matrix_node/sensors/status", "rebooting")          # ignored
        c.fire_message("led_matrix_node/radar/schedule/state", "ON")
        c.fire_message("led_matrix_node/radar/night_start/state", "00:00:00")
        c.fire_message("led_matrix_node/radar/night_end/state", "06:30")
        c.fire_message("led_matrix_node/radar/schedule/state", "0")
        c.fire_message("led_matrix_node/radar/night_end/state", "25:00")        # ignored
        c.fire_message("radar_zones/context/home_empty", "ON")
        c.fire_message("radar_zones/context/home_empty", "false")
        c.fire_message("radar_zones/context/home_empty", "")
        c.fire_message("radar_zones/review/mark", "PRESS")
        c.fire_message("radar_zones/review/mark", "other")                      # ignored
        c.fire_message("radar_zones/review/name/set", "  Bed  ")
        c.fire_message("homeassistant/status", "online")
        c.fire_message("homeassistant/status", "offline")                       # nothing
        self.assertEqual(self.drain(), [
            FramesMessage(b'{"v":1}', t),
            NodeStatus(True, t),
            NodeStatus(False, t),
            ScheduleState(True, None, None, t),
            ScheduleState(True, 0, None, t),
            ScheduleState(True, 0, 390, t),
            ScheduleState(False, 0, 390, t),
            ContextState("home_empty", True, t),
            ContextState("home_empty", False, t),
            ContextState("home_empty", None, t),
            MarkPress(t),
            SpotName("Bed", t),
            HaBirth(t),
        ])

    def test_parsers(self):
        self.assertEqual(parse_hhmm("23:59:59"), 1439)
        self.assertEqual(parse_hhmm("7:05"), 425)
        self.assertIsNone(parse_hhmm("12:60"))
        self.assertIsNone(parse_hhmm("noon"))
        self.assertEqual(parse_spot_name(("x" * 40).encode()), "x" * 32)
        self.assertEqual(parse_spot_name("Schlafzimmer Bett".encode("utf-8")), "Schlafzimmer Bett")
        self.assertIsNone(parse_spot_name(b"\xff\xfe"))

    def test_retained_command_ignored_and_cleared(self):
        self.connect()
        self.c.fire_message("radar_zones/review/mark", "PRESS", retain=True)
        self.c.fire_message("radar_zones/review/name/set", "Bed", retain=True)
        self.assertEqual(self.drain(), [])
        self.assertEqual(self.c.published, [("radar_zones/review/mark", b"", 1, True),
                                            ("radar_zones/review/name/set", b"", 1, True)])
        self.assertEqual(self.io.retained_cleared, 2)
        # the broker echoes the clearing message (empty, not retained): it is not a command
        self.c.fire_message("radar_zones/review/name/set", b"")
        self.assertEqual(self.drain(), [])
        # a later empty name typed by the user is a command
        self.c.fire_message("radar_zones/review/name/set", b"")
        self.assertEqual(self.drain(), [SpotName("", self.now[0])])

    def test_oversize_payloads_are_dropped_before_queueing(self):
        # the broker has no ACLs and MQTT allows 256 MiB per message
        self.connect()
        c = self.c
        frames = "led_matrix_node/radar/ld2454/frames"
        c.fire_message(frames, bytes(8 * 1024 * 1024))
        c.fire_message(frames, b"x" * 4097)
        c.fire_message("radar_zones/review/name/set", b"x" * 300)
        c.fire_message("led_matrix_node/sensors/status", b"online" + b" " * 300)
        c.fire_message("homeassistant/status", b"y" * 1000)
        self.assertEqual(self.drain(), [])
        self.assertEqual(self.io.oversize, 5)
        c.fire_message(frames, b"x" * 4096)                    # the limits themselves pass (the core rejects it)
        c.fire_message("radar_zones/review/name/set", ("x" * 256).encode())
        self.assertEqual([type(e) for e in self.drain()], [FramesMessage, SpotName])
        self.assertEqual(self.io.oversize, 5)
        # a retained command of any size is still cleared (D18)
        c.fire_message("radar_zones/review/mark", b"P" * 1000, retain=True)
        self.assertEqual(c.published, [("radar_zones/review/mark", b"", 1, True)])
        self.assertEqual((self.io.oversize, self.io.retained_cleared), (5, 1))

    def test_refused_for_s_counts_from_the_first_refusal(self):
        mono = [100.0]
        factory = FakeClientFactory()
        io = MqttIO(self.s, self.topics, queue.Queue(), client_factory=factory, monotonic=lambda: mono[0])
        io.start()
        c = factory.last
        self.assertEqual(io.refused_for_s(), 0.0)
        c.fire_connect(5)
        mono[0] = 130.0
        c.fire_connect(5)
        mono[0] = 160.0
        self.assertEqual(io.refused_for_s(), 60.0)
        c.fire_connect(0)                                      # accepted: the count starts over
        self.assertEqual(io.refused_for_s(), 0.0)

    def test_queue_full_counted(self):
        self.connect()
        for _ in range(22):
            self.c.fire_message("led_matrix_node/radar/ld2454/frames", b"{}")
        self.assertEqual(self.io.queue_dropped, 2)
        self.assertEqual(len(self.drain()), 20)

    def test_callback_exception_does_not_escape(self):
        self.connect()

        def boom(*args, **kwargs):
            raise RuntimeError("mapper bug")

        self.io.mapper.map = boom
        self.c.fire_message("led_matrix_node/radar/ld2454/frames", b"{}")      # must not raise
        self.assertEqual(self.io.callback_errors, 1)

    def test_publish_failure_counted(self):
        self.connect()
        self.c.publish_rc = 4
        self.assertFalse(self.io.publish("radar_zones/learner/state", "learning"))
        self.assertEqual(self.io.publish_failures, 1)
        self.c.publish_rc = 0
        self.c.publish_raises = ValueError("payload too large")
        self.assertFalse(self.io.publish("radar_zones/learner/state", "learning"))
        self.assertEqual(self.io.publish_failures, 2)

    def test_publish_skipped_while_disconnected(self):
        self.assertFalse(self.io.publish("radar_zones/learner/state", "learning"))
        self.assertEqual(self.c.published, [])
        self.assertEqual(self.io.publish_failures, 0)

    def test_disconnect_counts_reconnects(self):
        self.connect()
        self.c.fire_disconnect()
        self.assertFalse(self.io.connected)
        self.assertEqual(self.io.reconnects, 1)
        self.c.fire_connect(0)
        self.assertTrue(self.io.connected)

    def test_stop_publishes_offline(self):
        self.connect()
        self.io.stop()
        self.assertEqual(self.c.published, [("radar_zones/learner/status", b"offline", 1, True)])
        self.assertEqual(self.c.last_info.waited, 2.0)
        self.assertTrue(self.c.disconnected)
        self.assertTrue(self.c.loop_stopped)
        self.assertEqual(self.io.reconnects, 0)

    def test_password_never_logged(self):
        with self.assertLogs("radar_zone_learner.mqtt", level="DEBUG") as logs:
            factory = FakeClientFactory()
            io = MqttIO(settings(mqtt_password="TopSecret!"), self.topics, queue.Queue(), client_factory=factory)
            io.start()
            factory.last.fire_connect(5)
            factory.last.fire_connect(0)
        self.assertFalse(any("TopSecret!" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
