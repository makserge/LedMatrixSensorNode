"""Shared setUp and helpers for the tests of the P7 shell layer (``test_p7s_*``).

- ``LayerRig7``: the real ``P7Layer`` (with a data folder, so the tuning lines are really written) with the ``FakeLearner``
  and the fake Home Assistant ``FakeP7Ha``; ``tests.p6_harness.LayerRig`` with the P7 topics and layer in place of the P6 ones.
- ``P7Harness``: the real ``Service`` with ``MqttIO``, ``StateStore`` and the real ``P7Layer`` (``P6Harness`` with the same
  replacement).
- ``FakeP7Ha``: ``tests.fake_ha_zone.FakeZoneHa`` plus the one thing P7 adds, the 'Listen to a topic' tool of the MQTT
  integration. **That tool is an ASSUMPTION, HA[U8] of the P7 spec (14.2), settled by acceptance step A7**: a not retained
  message published while the tool listens is delivered to it and can be copied out; a message that was published before it
  started listening is not replayed (it was not retained), and the broker keeps nothing of an export (``broker_retained``).
  Everything else Home Assistant does in these tests is the P6 fake's HA[U1]..HA[U7] and the P7 spec's HA[U9] (the push kind
  ``attention``) and HA[U10] (a change of the ``poly_cm`` attribute alone leaves the entity untouched).
- ``tuning_line(night, **changes)``: a ``TuningLine`` of a clean zero-filled count line.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from radar_zone_learner.core.params import DEFAULT_P6
from radar_zone_learner.core.types import TuningLine
from radar_zone_learner.shell import service as service_mod
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.p7 import P7Layer
from radar_zone_learner.shell.persistence import StateStore
from radar_zone_learner.shell.topics import ClearRetained
from radar_zone_learner.shell.topics_p7 import build_topics_p7
from tests._tmp import tmpdir
from tests.fake_ha_zone import FakeZoneHa
from tests.fakes import clean_tuning_text
from tests.p4_harness import FixedRandom, Stub
from tests.p6_harness import LayerRig, P6Harness

EXPORT_CMD = "radar_zones/learner/export_tuning"
TUNING_STATE = "radar_zones/learner/tuning"
TUNING_EXPORT = "radar_zones/learner/tuning/export"
BTN_ID = "button.radar_zones_learner_export_tuning"
SENSOR_ID = "sensor.radar_zones_learner_tuning"


def tuning_line(night: int = 1, **changes) -> TuningLine:
    return TuningLine(night, clean_tuning_text(**changes))


class FakeP7Ha(FakeZoneHa):
    """The fake Home Assistant with a model of the 'Listen to a topic' tool (HA[U8], an assumption)."""

    def __init__(self, client, clock, create_delay_s: float = 0.0) -> None:
        super().__init__(client, clock, create_delay_s)
        self.listening: dict = {}               # topic -> [payload bytes, ...] seen since ``listen``

    def listen(self, topic: str) -> list:
        """The user starts the tool on ``topic``: from now on every message on it is recorded; a retained one is replayed."""
        got = self.listening.setdefault(topic, [])
        held = self.broker_retained.get(topic)
        if held is not None:
            got.append(held)
        return got

    def hear(self, topic: str, payload, retain: bool = False) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if self.up and topic in self.listening:
            self.listening[topic].append(payload)
        super().hear(topic, payload, retain)


class LayerRig7(LayerRig):
    """``LayerRig`` with the P7 layer and the P7 fake Home Assistant. ``data_dir`` is the add-on's data folder (a temporary
    one unless given)."""

    def __init__(self, test, learner=None, *, data_dir=None, **kw) -> None:
        super().__init__(test, learner, **kw)
        if data_dir is None:
            tmp = tmpdir()
            test.addCleanup(tmp.cleanup)
            data_dir = Path(tmp.name)
        self.data_dir = Path(data_dir)
        self.topics = build_topics_p7(self.settings)
        self.layer = P7Layer(self.topics, self.publish, self.data_dir, monotonic=self.clock.monotonic,
                             clock_ms=self.clock.clock_ms, debug_map=False, p6params=self.p6, day_of=kw.get("day_of"))
        self.ha = FakeP7Ha(self.client, self.clock, kw.get("create_delay_s", 0.0))
        # what a command of Home Assistant does: the mapper of the MQTT thread, then the layer, then the learner (the rig plays
        # the service's part, as ``send_event`` says)
        self.mapper = self.topics.make_mapper()
        self.layer.attach_mapper(self.mapper)
        self.client.on_message = self._on_message
        self.cleared_topics: list = []                # topics of retained commands the mapper asked to clear

    def _on_message(self, client, userdata, msg) -> None:
        for item in self.mapper.map(msg.topic, msg.payload, bool(msg.retain), self.ms):
            if isinstance(item, ClearRetained):
                self.cleared_topics.append(item.topic)
                continue
            self.send_event(item)


class P7Harness(P6Harness):
    """setUp and helpers for tests of the service with the P7 layer (mix in with unittest.TestCase). Subclasses define
    ``make_learner()``."""

    def setUp(self):
        super().setUp()                          # the P6 objects are replaced below; nothing of them was started
        self.topics = build_topics_p7(self.settings)
        self.mqtt = MqttIO(self.settings, self.topics, self.events, client_factory=self.factory,
                           clock_ms=self.clock.clock_ms, monotonic=self.clock.monotonic)
        self.store = StateStore(self.dir, validate=lambda state: self.learner)
        self.p6 = replace(DEFAULT_P6, valve_after_episodes=self.valve)
        self.layer = P7Layer(self.topics, self.mqtt.publish, self.dir, monotonic=self.clock.monotonic,
                             clock_ms=self.clock.clock_ms, debug_map=self.debug_map, p6params=self.p6)
        self.layer.attach_mapper(self.mqtt.mapper)
        if self.layer.capture is not None:
            self.addCleanup(self.layer.capture.stop)
        self.svc = service_mod.Service(self.settings, lambda state: self.learner, self.mqtt, self.store, Stub(), Stub(),
                                       self.clock.clock_ms, self.clock.monotonic, topics=self.topics,
                                       events=self.events, rng=FixedRandom(), rss=lambda: 24.0, p4=self.layer)

    def make_ha(self) -> FakeP7Ha:
        self.ha = FakeP7Ha(self.client, self.clock)
        return self.ha

    @property
    def tuning_dir(self) -> Path:
        return self.dir / "tuning"
