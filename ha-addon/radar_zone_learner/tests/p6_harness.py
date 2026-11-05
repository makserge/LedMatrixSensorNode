"""Shared setUp and helpers for the tests of the P6 layer.

Two harnesses:

- ``LayerRig``: the real ``P6Layer`` with the ``FakeLearner`` (or any learner) and ``FakeZoneHa``, with no service and no MQTT
  client logic: ``publish`` appends to a ``FakeMqttClient`` and the fake Home Assistant reads from it. The rig plays the
  service's part for the layer (``burst`` is ``Service._republish`` for the layer, ``feed`` is ``_execute``, ``send_event`` is
  ``_p4_events`` plus ``learner.handle``), so the layer tests stay fast and exact about order and time.
- ``P6Harness``: the real ``Service`` with ``MqttIO``, ``StateStore`` and the real ``P6Layer`` (the model is
  ``tests/p5_harness.py``); the learner is whatever ``make_learner`` returns (the ``FakeLearner`` or the real ``Learner``).
"""
from __future__ import annotations

import json
import queue
from dataclasses import replace
from pathlib import Path

from radar_zone_learner.core import Learner
from radar_zone_learner.core.params import DEFAULT_P6, DEFAULT_PARAMS
from radar_zone_learner.core.types import FixedOffsetTime
from radar_zone_learner.core.types import NodeStatus, StatusChanged, ZoneStates, ZoneView
from radar_zone_learner.shell import service as service_mod
from radar_zone_learner.shell.mqtt_io import MqttIO
from radar_zone_learner.shell.p4_types import RadarObservingMsg, RadarStateMsg
from radar_zone_learner.shell.p6 import P6Layer
from radar_zone_learner.shell.persistence import StateStore
from radar_zone_learner.shell.topics_p6 import build_topics_p6
from tests._tmp import tmpdir
from tests.fake_ha_zone import FakeZoneHa
from tests.fakes import FakeClientFactory, FakeClock, FakeLearner, FakeMqttClient, FakeNode, quiet_logs
from tests.p4_harness import FixedRandom, Stub
from tests.p5_harness import make_settings

DAY_MS = 86_400_000
FRAMES = "led_matrix_node/radar/ld2454/frames"
POLY = ((-120, 250), (-40, 250), (-40, 330), (-120, 330))
AVAIL = "radar_zones/zone/availability"


def zone_view(zid: str, name: str = "", kind: str = "dwell", rev: int = 1, poly=POLY, **kw) -> ZoneView:
    """A ``ZoneView`` as ``Learner.zones()`` makes it: ``z0001`` records and ``l0001`` labels."""
    values = dict(id=zid, name=name, kind=kind, src="mark" if zid.startswith("l") else "approve", ref=None, rev=rev,
                  cx_m=-0.8, cy_m=2.9, cells=frozenset({1000, 1001}), poly_cm=tuple(poly), created_s=0, night=7,
                  frozen=False)
    values.update(kw)
    return ZoneView(**values)


def occ_id(zid: str, epoch: int = 0) -> str:
    return "binary_sensor." + (f"radar_spot_{zid}" if not epoch else f"radar_spot_e{epoch}_{zid}")


def ent_id(component: str, zid: str, suffix: str = "", epoch: int = 0) -> str:
    base = f"radar_spot_{zid}" if not epoch else f"radar_spot_e{epoch}_{zid}"
    return f"{component}.{base}{suffix}"


class LayerRig:
    """The real ``P6Layer`` bound to a learner, with a fake Home Assistant, driven step by step."""

    def __init__(self, test, learner=None, *, valve: int = 0, cap: int = 24, epoch: int = 0, connected: bool = True,
                 day_of=None, create_delay_s: float = 0.0) -> None:
        quiet_logs(test)
        self.clock = FakeClock()
        self.client = FakeMqttClient("rig")
        self.connected = connected
        self.settings = make_settings(Path("."), debug_map=False)
        self.topics = build_topics_p6(self.settings)
        self.p6 = replace(DEFAULT_P6, valve_after_episodes=valve, zone_entities_max=cap)
        self.layer = P6Layer(self.topics, self.publish, None, monotonic=self.clock.monotonic,
                             clock_ms=self.clock.clock_ms, debug_map=False, p6params=self.p6, day_of=day_of)
        self.learner = learner if learner is not None else FakeLearner()
        self.learner.p6_epoch = epoch
        self.ha = FakeZoneHa(self.client, self.clock, create_delay_s)
        self.second_at = None
        self.consumed: list = []
        self.stamps: list = []              # (wall_ms, topic, payload, retain) of every publish, in order

    # ------------------------------------------------------------ plumbing
    def publish(self, topic, payload, qos=1, retain=False) -> bool:
        if not self.connected:
            return False
        self.client.publish(topic, payload, qos, retain)
        self.stamps.append((self.clock.wall_ms, topic, payload if isinstance(payload, bytes) else payload.encode("utf-8"),
                            retain))
        return True

    @staticmethod
    def call(fn, *args):
        return fn(*args)

    @property
    def ms(self) -> int:
        return self.clock.clock_ms()

    def bind(self, learner=None) -> None:
        if learner is not None:
            self.learner = learner
        self.layer.bind(self.call, self.learner)

    def start(self) -> None:
        """What ``Service.start`` does for the layer, then a connect: ``bind``, ``start``, the learner's and the node's retained
        ``online`` and a discovery burst."""
        self.bind()
        self.layer.start(self.ms)
        self.publish(self.topics.learner_status, "online", 1, True)
        self.publish(self.topics.node_status, "online", 1, True)
        self.burst()

    def node(self, online: bool) -> None:
        """The node's will or its birth on ``sensors/status`` (retained), seen by Home Assistant and by the layer."""
        self.publish(self.topics.node_status, "online" if online else "offline", 1, True)
        self.layer.on_event(NodeStatus(online, self.ms), self.ms)
        self.ha.process()

    def learner_status(self, online: bool) -> None:
        """The learner's own will (``offline`` when the add-on is dead) or its ``online``: only Home Assistant sees it."""
        self.publish(self.topics.learner_status, "online" if online else "offline", 1, True)
        self.ha.process()

    def burst(self) -> None:
        """``Service._republish`` for the layer: the discovery messages, then the states; the second pass is due 10 s later."""
        for topic, payload in self.layer.discovery():
            self.publish(topic, payload, 1, False)
        self.layer.republish(self.ms)
        self.second_at = self.clock.monotonic() + 10.0
        self.ha.process()

    def step(self, seconds: float = 1.0, tick: bool = True) -> None:
        self.clock.advance(seconds)
        if self.second_at is not None and self.clock.monotonic() >= self.second_at:
            self.second_at = None
            self.layer.second_pass(self.ms)
        if tick:
            self.layer.tick(self.ms)
        self.ha.process()

    def run(self, seconds: float, step: float = 1.0) -> None:
        t = 0.0
        while t < seconds - 1e-9:
            self.step(step)
            t += step

    def feed(self, *outs) -> None:
        """``Service._execute`` for outputs of the learner: the layer sees every one."""
        for out in outs:
            self.consumed.append((type(out).__name__, self.layer.on_output(out, self.ms)))
        self.ha.process()

    def send_event(self, ev) -> list:
        """The path of one received event: the layer first, then the learner, then the learner's outputs."""
        given = self.layer.on_event(ev, self.ms)
        for e in given:
            self.feed(*self.learner.handle(e))
        self.ha.process()
        return given

    # ------------------------------------------------------------ the radar and the node
    def radar(self, state: str = "on", observing: bool = True, node: bool = True, core: str = "learning") -> None:
        self.layer.on_event(NodeStatus(node, self.ms), self.ms)
        self.layer.on_event(RadarStateMsg(state, self.ms), self.ms)
        self.layer.on_event(RadarObservingMsg(observing, self.ms), self.ms)
        self.feed(StatusChanged(core, {}))

    def zones(self, *views, **states) -> None:
        """Sets the zone views of the learner (``Learner.zones()``)."""
        self.learner.p5_zones = tuple(views)

    def push_states(self, valid=None) -> ZoneStates:
        if valid is not None:
            self.learner.p6_valid = valid
        snap = self.learner.p6_snapshot()
        self.feed(snap)
        return snap

    def ready(self, *views, valid: bool = True) -> None:
        """Zones known to the learner and the radars observed and valid: the layer is bound, connected and ``online``."""
        self.zones(*views)
        for v in views:
            if v.id not in self.learner.p6_zones:
                self.learner.p6_zone(v.id)
        self.start()
        self.radar()
        self.push_states(valid)
        self.run(11)

    # ------------------------------------------------------------ reading what was published
    def published(self, topic: str) -> list:
        return self.client.published_to(topic)

    def last(self, topic: str):
        pubs = self.published(topic)
        return pubs[-1][1].decode("utf-8") if pubs else None

    def cleared(self) -> None:
        self.client.published.clear()
        self.ha._cursor = 0

    def stamped(self, topic: str, day: int = None) -> list:
        """(wall_ms, payload text) of every publish to ``topic``, of one wall-clock day when ``day`` is given."""
        return [(ms, p.decode("utf-8")) for ms, t, p, _ in self.stamps
                if t == topic and (day is None or ms // DAY_MS == day)]


class P6Harness:
    """setUp and helpers for tests of the service with the P6 layer (mix in with unittest.TestCase). Subclasses define
    ``make_learner()`` and may set ``debug_map`` and ``valve``."""

    debug_map = False
    valve = 0

    def setUp(self):
        quiet_logs(self)
        self._tmp = tmpdir()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.settings = make_settings(self.dir, debug_map=self.debug_map)
        self.clock = FakeClock()
        self.events = queue.Queue(maxsize=2000)
        self.factory = FakeClientFactory()
        self.topics = build_topics_p6(self.settings)
        self.mqtt = MqttIO(self.settings, self.topics, self.events, client_factory=self.factory,
                           clock_ms=self.clock.clock_ms, monotonic=self.clock.monotonic)
        self.learner = self.make_learner()
        self.store = StateStore(self.dir, validate=lambda state: self.learner)
        self.p6 = replace(DEFAULT_P6, valve_after_episodes=self.valve)
        self.layer = P6Layer(self.topics, self.mqtt.publish, self.dir, monotonic=self.clock.monotonic,
                             clock_ms=self.clock.clock_ms, debug_map=self.debug_map, p6params=self.p6)
        self.layer.attach_mapper(self.mqtt.mapper)
        if self.layer.capture is not None:
            self.addCleanup(self.layer.capture.stop)
        self.svc = service_mod.Service(self.settings, lambda state: self.learner, self.mqtt, self.store, Stub(), Stub(),
                                       self.clock.clock_ms, self.clock.monotonic, topics=self.topics,
                                       events=self.events, rng=FixedRandom(), rss=lambda: 24.0, p4=self.layer)
        self.ha = None

    @property
    def client(self):
        return self.factory.last

    def make_ha(self) -> FakeZoneHa:
        self.ha = FakeZoneHa(self.client, self.clock)
        return self.ha

    def connect(self, node_online: bool = True, **node_kwargs):
        self.svc.start()
        self.client.fire_connect(0)
        node = FakeNode(self.client, **node_kwargs) if node_kwargs else None
        if node is not None:
            node.announce()
        if self.ha is None:
            self.make_ha()
        # the node's retained status is on the broker: Home Assistant hears it as well (it is an availability topic)
        self.ha.hear(self.topics.node_status, "online" if node_online else "offline", True)
        self.pump(node)
        return node

    def node_says(self, topic: str, payload: str, retain: bool = True) -> None:
        """A message of the node: the app and Home Assistant both receive it."""
        self.client.fire_message(topic, payload, retain=retain)
        if self.ha is not None:
            self.ha.hear(topic, payload, retain)

    def pump(self, node=None):
        for _ in range(60):
            busy = False
            while not self.events.empty():
                self.svc.step(0)
                busy = True
            if node is not None and node.process():
                busy = True
            if self.ha is not None and self.ha.process():
                busy = True
            if not busy:
                break
        self.svc.step(0)
        while not self.events.empty():
            self.svc.step(0)
            if node is not None:
                node.process()
        if self.ha is not None:
            self.ha.process()

    def run_for(self, seconds, node=None, step=1.0):
        t = 0.0
        while t < seconds - 1e-9:
            self.clock.advance(step)
            t += step
            self.svc.step(0)
            self.pump(node)

    def published(self, topic):
        return self.client.published_to(topic)

    def last(self, topic):
        pubs = self.published(topic)
        return pubs[-1][1].decode("utf-8") if pubs else None


# ---------------------------------------------------------------- the real core behind the service
def real_zone_learner(kind: str = "manual", name: str = "Desk"):
    """A real ``Learner`` with one confirmed zone, the label ``l0001`` (a 3 x 3 cell block at (-0.2..0.4, 2.8..3.4) m: the
    point (0.1, 3.1) is inside, (2.5, 3.1) is well outside). The label is put into the learner's state the way
    ``test_p6_learner`` does it; 'Mark spot here' would need a stay of 20 s and a bed or manual decision."""
    from tests.test_learner import BASE_MS, start
    from tests.test_zoneeval import block
    L = Learner(DEFAULT_PARAMS, FixedOffsetTime(0))
    start(L)
    L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": 0.1, "y": 3.1, "name": name, "kind": kind,
                         "cells": block(-0.1, 2.9)})
    L.review.touch()
    return L


class MqttFeeder:
    """``tests.test_learner.Feeder`` with the frames going over MQTT into the service instead of into ``Learner.handle``: the
    feeder's learner is a proxy whose ``handle`` moves the shared clock to the message's reception time, fires the message on
    the fake client and lets the service work, and whose ``tick`` moves the clock and lets the service tick. ``pw`` and ``sm``
    are the power bits and the scan mode of every batch (3 and ``A``: both radars observing)."""

    def __init__(self, harness) -> None:
        from tests.test_learner import Feeder
        self.h = harness
        self.feeder = Feeder(self)
        self.feeder._send = self._send
        self.pw = 3
        self.sm = "A"

    # the proxy for Feeder
    def handle(self, event):
        h = self.h
        delta = (event.recv_ms - h.clock.wall_ms) / 1000.0
        if delta > 0:
            h.clock.advance(delta)
        h.client.fire_message(FRAMES, event.payload)
        h.pump()
        return []

    def tick(self, now_ms: int):
        h = self.h
        delta = (now_ms - h.clock.wall_ms) / 1000.0
        if delta > 0:
            h.clock.advance(delta)
        h.svc.step(0)
        h.pump()
        return []

    def _send(self, n, first, last, listed):
        f = self.feeder
        from tests.test_learner import BASE_MS
        from radar_zone_learner.core.types import FramesMessage
        t_dev = last + f.dev_off + 40
        obj = {"v": 1, "b": f.boot, "t": t_dev, "s": f.seq, "n": n, "a0": t_dev - (first + f.dev_off), "a1": 40, "mt": 1,
               "p2": f._p2(last), "pw": self.pw, "sm": self.sm}
        if listed:
            fr = []
            for k, t, tg in listed:
                e = [k, t_dev - (t + f.dev_off)]
                for slot, (x, y) in enumerate(tg[:3]):
                    e += [slot, int(round(x * 1000)), int(round(y * 1000)), 0]
                fr.append(e)
            obj["f"] = fr
        f.seq += n
        payload = json.dumps(obj, separators=(",", ":")).encode()
        self.handle(FramesMessage(payload, BASE_MS + last + 40 + f.lat))

    # the Feeder's own calls, for the test's convenience
    def idle_until(self, t_ms):
        self.feeder.idle_until(t_ms)

    def targets_until(self, t_ms, fn):
        self.feeder.targets_until(t_ms, fn)

    def seconds(self, t_from_ms, t_to_ms, fn=None):
        """One tick a second from ``t_from_ms`` to ``t_to_ms`` with idle frames, or the targets of ``fn`` (None: no target)."""
        from tests.test_learner import BASE_MS
        t = t_from_ms
        while t < t_to_ms:
            t += 1000
            if fn is None:
                self.feeder.idle_until(t)
            else:
                self.feeder.targets_until(t, fn)
            self.tick(BASE_MS + t)
