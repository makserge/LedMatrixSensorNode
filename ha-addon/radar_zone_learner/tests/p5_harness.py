"""Shared setUp and helpers for the tests of the service with the P5 layer (fake broker client, fake node, FakeLearner)."""
import queue
from pathlib import Path

from radar_zone_learner.core.types import ReviewChanged, ReviewItem
from radar_zone_learner.shell import service as service_mod
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.p5 import P5Layer
from radar_zone_learner.shell.persistence import StateStore
from radar_zone_learner.shell.topics_p5 import build_topics_p5
from tests._tmp import tmpdir
from tests.fakes import FakeClientFactory, FakeClock, FakeNode, quiet_logs
from tests.p4_harness import FixedRandom, Stub

NODE = "led_matrix_node/"


def make_settings(data_dir, **changes):
    values = dict(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant", debug_map=True,
                  mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None, mqtt_password=None,
                  mqtt_ssl=False, data_dir=data_dir)
    values.update(changes)
    return Settings(**values)


def place_item(sid="s0005-1", rev=1, tag="A", **data):
    """A tier-2 place item as the core builds it (spec 2.4)."""
    d = {"tag": tag, "kind": "dwell", "conf": 0.8, "x_cm": 255, "y_cm": 220, "name_hint": "", "hrs_wd": (),
         "hrs_we": (), "n": 12, "frozen": False}
    d.update(data)
    return ReviewItem(sid, rev, "dwell_zone", d, 2)


def window_item(rev=3, **data):
    d = {"start_min": 1410, "end_min": 405, "saving_pct": 30, "edit_start": None, "edit_end": None,
         "sched_box": False}
    d.update(data)
    return ReviewItem("night_window", rev, "night_window", d, 1)


def bed_item(rev=1):
    return ReviewItem("bed_needs_data", rev, "bed_needs_data", {"start_min": 0, "end_min": 360, "sched": True,
                                                                 "show_window": True}, 1)


def changed(items, selected=None):
    return ReviewChanged(tuple(items), selected)


class P5Harness:
    """setUp and helpers for tests of the service with the P5 layer (mix in with unittest.TestCase). Subclasses define
    ``make_learner()`` and may set ``debug_map``, ``select_mode`` and ``with_data_dir``."""

    debug_map = True
    select_mode = "dynamic"

    def setUp(self):
        quiet_logs(self)
        self._tmp = tmpdir()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.settings = make_settings(self.dir, debug_map=self.debug_map)
        self.clock = FakeClock()
        self.events = queue.Queue(maxsize=2000)
        self.factory = FakeClientFactory()
        self.topics = build_topics_p5(self.settings)
        self.mqtt = MqttIO(self.settings, self.topics, self.events, client_factory=self.factory,
                           clock_ms=self.clock.clock_ms, monotonic=self.clock.monotonic)
        self.learner = self.make_learner()
        self.store = StateStore(self.dir, validate=lambda state: self.learner)
        self.layer = P5Layer(self.topics, self.mqtt.publish, self.dir, monotonic=self.clock.monotonic,
                             clock_ms=self.clock.clock_ms, select_mode=self.select_mode, debug_map=self.debug_map)
        if self.layer.capture is not None:
            self.addCleanup(self.layer.capture.stop)          # an open file cannot be deleted on Windows
        self.svc = service_mod.Service(self.settings, lambda state: self.learner, self.mqtt, self.store, Stub(),
                                       Stub(), self.clock.clock_ms, self.clock.monotonic, topics=self.topics,
                                       events=self.events, rng=FixedRandom(), rss=lambda: 24.0, p4=self.layer)

    @property
    def client(self):
        return self.factory.last

    def connect(self, **node_kwargs):
        self.svc.start()
        self.client.fire_connect(0)
        node = FakeNode(self.client, **node_kwargs) if node_kwargs else None
        if node is not None:
            node.announce()
        self.pump(node)
        return node

    def pump(self, node=None):
        for _ in range(60):
            busy = False
            while not self.events.empty():
                self.svc.step(0)
                busy = True
            if node is not None and node.process():
                busy = True
            if not busy:
                break
        self.svc.step(0)
        while not self.events.empty():
            self.svc.step(0)
            if node is not None:
                node.process()

    def run_for(self, seconds, node=None, step=1.0):
        t = 0.0
        while t < seconds:
            self.clock.advance(step)
            t += step
            self.svc.step(0)
            self.pump(node)

    def published(self, topic):
        return self.client.published_to(topic)

    def last(self, topic):
        pubs = self.published(topic)
        return pubs[-1][1].decode("utf-8") if pubs else None

    def node_publishes(self):
        return [p for p in self.client.published if p[0].startswith(NODE)]
