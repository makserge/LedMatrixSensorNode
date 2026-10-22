"""Test doubles for the shell: a paho-like MQTT client, its factory, a fake learner and fake clocks.

``FakeMqttClient`` implements the subset of paho-mqtt 2.x that ``shell.mqtt_io`` uses; tests call the
callbacks directly through the ``fire_*`` helpers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from radar_zone_learner.core.types import (DebugMapData, DebugMapChanged, StatusChanged)


@dataclass
class FakeReasonCode:
    value: int = 0

    @property
    def is_failure(self) -> bool:
        return self.value != 0

    def __str__(self) -> str:
        return "Success" if self.value == 0 else f"Failure({self.value})"


@dataclass
class FakeMessage:
    topic: str
    payload: bytes
    retain: bool = False
    qos: int = 0


class FakeMessageInfo:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.waited: Optional[float] = None

    def wait_for_publish(self, timeout: Optional[float] = None) -> None:
        self.waited = timeout


class FakeMqttClient:
    def __init__(self, client_id: str = "") -> None:
        self.client_id = client_id
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.on_connect_fail = None
        self.will = None
        self.credentials = None
        self.tls = False
        self.reconnect_delay = None
        self.connect_args = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.subscriptions: list = []
        self.published: list = []          # (topic, payload, qos, retain)
        self.publish_rc = 0
        self.publish_raises: Optional[Exception] = None
        self._connected = False
        self.last_info: Optional[FakeMessageInfo] = None

    # --- the paho subset used by shell.mqtt_io
    def username_pw_set(self, username, password=None) -> None:
        self.credentials = (username, password)

    def will_set(self, topic, payload=None, qos=0, retain=False) -> None:
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay=1, max_delay=120) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    def tls_set(self, *args, **kwargs) -> None:
        self.tls = True

    def connect_async(self, host, port=1883, keepalive=60, **kwargs) -> None:
        self.connect_args = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self, *args, **kwargs) -> None:
        self.disconnected = True
        self._connected = False

    def subscribe(self, topic, qos=0, **kwargs):
        if isinstance(topic, list):
            self.subscriptions.extend(topic)
        else:
            self.subscriptions.append((topic, qos))
        return (0, 1)

    def publish(self, topic, payload=None, qos=0, retain=False, **kwargs) -> FakeMessageInfo:
        if self.publish_raises is not None:
            raise self.publish_raises
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.published.append((topic, payload if payload is not None else b"", qos, retain))
        self.last_info = FakeMessageInfo(self.publish_rc)
        return self.last_info

    def is_connected(self) -> bool:
        return self._connected

    # --- test helpers: call the callbacks as paho's thread would
    def fire_connect(self, rc: int = 0) -> None:
        self._connected = rc == 0
        self.on_connect(self, None, {}, FakeReasonCode(rc), None)

    def fire_disconnect(self, rc: int = 7) -> None:
        self._connected = False
        self.on_disconnect(self, None, {}, FakeReasonCode(rc), None)

    def fire_message(self, topic: str, payload, retain: bool = False, qos: int = 0) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.on_message(self, None, FakeMessage(topic, payload, retain, qos))

    def published_to(self, topic: str) -> list:
        return [p for p in self.published if p[0] == topic]


class FakeClientFactory:
    """``client_factory`` for ``MqttIO``: records every client it makes."""

    def __init__(self) -> None:
        self.clients: list = []

    def __call__(self, client_id: str) -> FakeMqttClient:
        client = FakeMqttClient(client_id)
        self.clients.append(client)
        return client

    @property
    def last(self) -> FakeMqttClient:
        return self.clients[-1]


class FakeClock:
    """Monotonic seconds and wall ms that only move when the test says so."""

    def __init__(self, mono: float = 1000.0, wall_ms: int = 1_767_571_200_000) -> None:
        self.mono = mono
        self.wall_ms = wall_ms

    def monotonic(self) -> float:
        return self.mono

    def clock_ms(self) -> int:
        return self.wall_ms

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall_ms += int(round(seconds * 1000))


def empty_debug_map(**changes) -> DebugMapData:
    values = dict(nx=70, ny=40, cell_m=0.2, x0_m=-7.0, y0_m=0.0, dwell_rate=tuple([0.0] * 2800),
                  envelope=frozenset(), candidates=(), suggestions=(), labels=(), doors=(), fov_deg=60.0)
    values.update(changes)
    return DebugMapData(**values)


class FakeLearner:
    """Implements the ``Learner`` signatures of spec 5.3 with scripted outputs."""

    def __init__(self, state: Optional[dict] = None) -> None:
        self.state_in = state
        self.events: list = []
        self.ticks: list = []
        self.queued: list = []              # outputs returned by the next handle()
        self.tick_outputs: list = []        # outputs returned by the next tick()
        self.raise_on_handle: Optional[Exception] = None
        self.state_name = "starting"
        self.spot_name = (state or {}).get("settings", {}).get("spot_name", "")

    def handle(self, event) -> list:
        self.events.append(event)
        if self.raise_on_handle is not None:
            raise self.raise_on_handle
        outs, self.queued = self.queued, []
        return outs

    def tick(self, now_ms: int) -> list:
        self.ticks.append(now_ms)
        outs, self.tick_outputs = self.tick_outputs, []
        return outs

    def export_state(self) -> dict:
        return {"schema": 3, "writer": "radar_zone_learner 0.3.0", "phase": "p3",
                "events": len(self.events), "settings": {"spot_name": self.spot_name}}

    def status(self) -> StatusChanged:
        return StatusChanged(self.state_name, {"version": "0.3.0", "schema": 3, "mode": "shadow"})

    def debug_map(self) -> DebugMapData:
        return empty_debug_map()

    def suggestions(self) -> tuple:
        return ()

    def labels(self) -> tuple:
        return ()

    def candidates(self) -> tuple:
        return ()

    def stays(self) -> tuple:
        return ()

    def counters(self) -> dict:
        return {}


def quiet_logs(testcase) -> None:
    """Keeps expected warnings of the app's loggers out of the test output (assertLogs still works)."""
    logger = logging.getLogger("radar_zone_learner")
    handler = logging.NullHandler()
    logger.addHandler(handler)
    testcase.addCleanup(logger.removeHandler, handler)


__all__ = ["FakeMqttClient", "FakeClientFactory", "FakeMessage", "FakeMessageInfo", "FakeReasonCode",
           "FakeClock", "FakeLearner", "empty_debug_map", "quiet_logs", "DebugMapChanged"]
