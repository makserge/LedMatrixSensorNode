"""Shared setUp and helpers for the tests of the service with the P4 layer (fake broker client, fake node)."""
import queue
from pathlib import Path

from radar_zone_learner.shell import service as service_mod
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.options import Settings
from radar_zone_learner.shell.p4 import P4Layer
from radar_zone_learner.shell.persistence import StateStore
from radar_zone_learner.shell.topics_p4 import build_topics_p4
from tests._tmp import tmpdir
from tests.fakes import FakeClientFactory, FakeClock, FakeNode, quiet_logs

NODE = "led_matrix_node/"


class Stub:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def beat(self):
        self.calls.append("beat")


class FixedRandom:
    def uniform(self, a, b):
        return 1.0


class P4Harness:
    """setUp and helpers for tests of the service with the P4 layer (mix in with unittest.TestCase)."""

    def setUp(self):
        quiet_logs(self)
        self._tmp = tmpdir()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.settings = Settings(log_level="info", node_prefix="led_matrix_node", discovery_prefix="homeassistant",
                                 debug_map=True, mqtt_host="core-mosquitto", mqtt_port=1883, mqtt_username=None,
                                 mqtt_password=None, mqtt_ssl=False, data_dir=self.dir)
        self.clock = FakeClock()
        self.events = queue.Queue(maxsize=2000)
        self.factory = FakeClientFactory()
        self.topics = build_topics_p4(self.settings)
        self.mqtt = MqttIO(self.settings, self.topics, self.events, client_factory=self.factory,
                           clock_ms=self.clock.clock_ms, monotonic=self.clock.monotonic)
        self.learner = self.make_learner()
        self.store = StateStore(self.dir, validate=lambda state: self.learner)
        self.p4 = P4Layer(self.topics, self.mqtt.publish, self.dir)
        self.svc = service_mod.Service(self.settings, lambda state: self.learner, self.mqtt, self.store, Stub(),
                                       Stub(), self.clock.clock_ms, self.clock.monotonic, topics=self.topics,
                                       events=self.events, rng=FixedRandom(), rss=lambda: 24.0, p4=self.p4)

    @property
    def client(self):
        return self.factory.last

    def connect(self, **node_kwargs):
        """Starts the service, connects, lets a fake node (when given keywords) announce its retained states."""
        self.svc.start()
        self.client.fire_connect(0)
        node = FakeNode(self.client, **node_kwargs) if node_kwargs else None
        if node is not None:
            node.announce()
        self.pump(node)
        return node

    def pump(self, node=None):
        """Handles every queued event, the node's answers to what the app published, and a tick."""
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

    def run_for(self, seconds, node=None, step=30.0):
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
