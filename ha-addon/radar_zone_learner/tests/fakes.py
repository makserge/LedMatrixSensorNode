"""Test doubles for the shell: a paho-like MQTT client, its factory, a fake learner and fake clocks.

``FakeMqttClient`` implements the subset of paho-mqtt 2.x that ``shell.mqtt_io`` uses; tests call the
callbacks directly through the ``fire_*`` helpers.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from radar_zone_learner.core.types import (DebugMapData, DebugMapChanged, PushRequest, RadarMovedView, SaveRequested,
                                           StatusChanged, TuningLine, VariantRequest, WhereView, ZoneDrift, ZoneForget,
                                           ZoneState, ZoneStates, ZoneTune)


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


def clean_tuning_line(**changes) -> dict:
    """A zero-filled nightly count line of the shape of P7 spec 4.8 (every key of the whitelist, counts and fixed-length
    histograms only); ``changes`` replace top-level keys. It passes ``tuningagg.scan`` (a test checks that)."""
    zeros = lambda n: [0] * n        # noqa: E731
    line = {"v": 1, "ob": 0.0, "cd": 0, "sn": 0, "sd": zeros(6), "sl": zeros(5), "sf": zeros(4), "sg": zeros(5),
            "hs": zeros(10), "hn": 0, "dl": {k: zeros(6) for k in ("dwell", "short_task", "bed", "manual")}, "cn": 0,
            "gf": zeros(8), "nm": zeros(8), "ch": {k: zeros(6) for k in ("n", "cd", "ld", "gs", "cv", "cf")},
            "dr": zeros(6), "zs": zeros(7), "zf": 0, "zr": 0, "dg": 0, "lb": zeros(2), "la": zeros(4),
            "ac": {"op": {"place": zeros(4), "merge": zeros(4), "modify": zeros(4), "retire": zeros(3), "radar": zeros(3)},
                   "gh": {k: [zeros(6), zeros(6)] for k in ("cf", "cd", "n", "gs", "cv", "ld")},
                   "rh": {k: zeros(6) for k in ("mod_y", "mod_r", "ret_rm", "ret_kp")}}}
    line.update(changes)
    return line


def clean_tuning_text(**changes) -> str:
    return json.dumps(clean_tuning_line(**changes), separators=(",", ":"))


class FakeZone6:
    """The part of the core's ``Zone6`` container that the shell reads (P6 spec A.6): ``export()``, with the installation
    epoch under ``set``."""

    def __init__(self, owner: "FakeLearner") -> None:
        self.owner = owner

    def export(self) -> dict:
        return {"v": 1, "set": {"epoch": self.owner.p6_epoch}}


class FakeLearner:
    """Implements the ``Learner`` signatures of spec 5.3 and the P5 accessors (P5 spec A.8) with scripted outputs.

    The P5 accessors return what the test put into the ``p5_*`` attributes (empty by default) and record their
    calls, so that ``P5Layer`` tests can run without the real core.

    The P6 accessors (P6 spec A.4) return a scripted zone model: ``p6_zone`` makes or changes a ``ZoneState``,
    ``p6_valid`` is the ``valid`` flag of ``zone_states()``, ``p6_snapshot()`` is the ``ZoneStates`` a test pushes into
    ``tick_outputs`` to play the core's output, and ``handle`` applies ``ZoneTune``, ``ZoneForget`` and ``VariantRequest``
    the way the core does (the delay in force, the margin, the requested variants) and returns the changed ``ZoneStates`` and
    ``SaveRequested("zone")``. ``zone6.export()`` returns ``p6_epoch``. The zone views are ``p5_zones``."""

    def __init__(self, state: Optional[dict] = None) -> None:
        self.state_in = state
        self.events: list = []
        self.ticks: list = []
        self.queued: list = []              # outputs returned by the next handle()
        self.tick_outputs: list = []        # outputs returned by the next tick()
        self.raise_on_handle: Optional[Exception] = None
        self.state_name = "starting"
        self.spot_name = (state or {}).get("settings", {}).get("spot_name", "")
        self.responder = None               # optional callable(event) -> list of outputs (P4 tests)
        self.attributes: dict = {}          # extra keys of status().attributes
        # P5 accessors (spec A.8): scripted values and call records
        self.p5_items: tuple = ()           # review_state()[0]: ReviewItem, in review order
        self.p5_selected: Optional[str] = None   # review_state()[1]
        self.p5_advanced: tuple = ()        # advanced(): AdvancedEntry
        self.p5_zones: tuple = ()           # zones(): ZoneView
        self.p5_where = WhereView(False, 0, False, None, None)   # where_am_i()
        self.p5_live: tuple = ()            # live_view(): ((x_m, y_m, stay), ...)
        self.p5_push = PushRequest(0, 0, "none", 0, None, None, ())   # push_request()
        self.p5_paused_stays = 0            # paused_stays()
        self.p5_map = None                  # review_map(): a DebugMapData, else empty_debug_map()
        self.p5_reset_result = None         # reset(): the learner returned, else a new FakeLearner
        self.map_calls: list = []           # (selected, live) of every review_map() call
        self.where_calls: list = []         # selected of every where_am_i() call
        self.resets = 0                     # reset() calls
        # P6 accessors (spec A.4): the scripted zone model
        self.p6_valid = False               # ZoneStates.valid
        self.p6_zones: dict = {}            # zid -> ZoneState
        self.p6_t_dev = 0
        self.p6_epoch = 0                   # zone6.set.epoch
        self.p6_debug: tuple = ()           # zone_debug()
        self.p6_tune: dict = {}             # zid -> {"cd": seconds, "mg": cm}: the overrides the commands set (zone6.tune)
        self.zone6 = FakeZone6(self)
        # P7 accessors (spec A.3): the scripted values
        self.p7_enabled = False
        self.p7_drift: tuple = ()           # drift_view(): ZoneDrift
        self.p7_radar = RadarMovedView("none", 0, 0, (0, 0, 0), False)       # radar_moved_state()
        self.p7_moved_from = 0              # moved_from_count()
        self.p7_summary = {"grids_nonzero": 0, "candidates": 0, "stays": 0, "door_events": 0, "ghost_events": 0}
        self.p7_resets = 0                  # reset_maps() calls

    def handle(self, event) -> list:
        self.events.append(event)
        if self.raise_on_handle is not None:
            raise self.raise_on_handle
        if self.responder is not None:
            self.queued = self.queued + list(self.responder(event) or [])
        outs, self.queued = self.queued, []
        if isinstance(event, (ZoneTune, ZoneForget, VariantRequest)):
            outs = outs + self._p6_apply(event)
        return outs

    def tick(self, now_ms: int) -> list:
        self.ticks.append(now_ms)
        outs, self.tick_outputs = self.tick_outputs, []
        return outs

    def export_state(self) -> dict:
        return {"schema": 3, "writer": "radar_zone_learner 0.3.0", "phase": "p3",
                "events": len(self.events), "settings": {"spot_name": self.spot_name}}

    def status(self) -> StatusChanged:
        return StatusChanged(self.state_name, {"version": "0.3.0", "schema": 3, "mode": "shadow", **self.attributes})

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

    # --- P5 accessors (spec A.8)
    def review_map(self, selected: Optional[str] = None, live: bool = False) -> DebugMapData:
        self.map_calls.append((selected, live))
        return self.p5_map if self.p5_map is not None else empty_debug_map()

    def where_am_i(self, selected: Optional[str] = None) -> WhereView:
        self.where_calls.append(selected)
        return self.p5_where

    def zones(self) -> tuple:
        return tuple(self.p5_zones)

    def advanced(self) -> tuple:
        return tuple(self.p5_advanced)

    def live_view(self) -> tuple:
        return tuple(self.p5_live)

    def review_state(self) -> tuple:
        return tuple(self.p5_items), self.p5_selected

    def push_request(self) -> PushRequest:
        return self.p5_push

    def paused_stays(self) -> int:
        return self.p5_paused_stays

    def reset(self) -> "FakeLearner":
        self.resets += 1
        return self.p5_reset_result if self.p5_reset_result is not None else FakeLearner()

    # --- P7 accessors (spec A.3)
    @property
    def p7on(self) -> bool:
        return self.p7_enabled

    def drift_view(self) -> tuple:
        return tuple(self.p7_drift) if self.p7_enabled else ()

    def radar_moved_state(self) -> RadarMovedView:
        return self.p7_radar if self.p7_enabled else RadarMovedView("none", 0, 0, (0, 0, 0), False)

    def moved_from_count(self) -> int:
        return self.p7_moved_from

    def reset_maps(self) -> None:
        self.p7_resets += 1

    def reset_summary(self) -> dict:
        return dict(self.p7_summary)

    def p7_drift_row(self, zid: str, **fields) -> ZoneDrift:
        """A ``ZoneDrift`` (defaults: an unfrozen ``ok`` zone with ratio 1.0) appended to ``p7_drift``."""
        values = dict(id=zid, state="ok", ratio=1.0, frozen=False, lo=0, rt=0, lu=0, rc=False, held=False)
        values.update(fields)
        row = ZoneDrift(**values)
        self.p7_drift = tuple(self.p7_drift) + (row,)
        return row

    @staticmethod
    def p7_line(night: int = 1, text: str = None) -> TuningLine:
        """A ``TuningLine``: ``text`` as given, else a clean zero-filled line of the shape of P7 spec 4.8."""
        return TuningLine(night, text if text is not None else clean_tuning_text())

    # --- P6 accessors (spec A.4) and the scripted zone model
    def zone_states(self) -> ZoneStates:
        return self.p6_snapshot()

    def zone_tune(self, zid: str) -> dict:
        """The overrides in force for a zone (what ``zone6.tune[zid]`` holds in the core); empty when there are none."""
        return dict(self.p6_tune.get(zid, {}))

    def zone_snapshot(self) -> tuple:
        return tuple((z.id, z.occ, self.p6_valid) for z in self._p6_ordered())

    def zone_debug(self) -> tuple:
        return tuple(self.p6_debug)

    def _p6_ordered(self) -> list:
        return [self.p6_zones[k] for k in sorted(self.p6_zones)]

    def p6_snapshot(self) -> ZoneStates:
        return ZoneStates(self.p6_valid, self.p6_t_dev, tuple(self._p6_ordered()))

    def p6_zone(self, zid: str, **fields) -> ZoneState:
        """Makes the zone's state (defaults: empty, not held, delay 300 s by default, margin 25 cm, no variants, no typical
        hours, no confidence) or changes fields of it."""
        cur = self.p6_zones.get(zid)
        values = dict(id=zid, occ=False, count=0, held=False, variant="", ldr=False, delay_s=300, delay_src="default",
                      margin_cm=25, variants=(), requested=(), typical_hours="", confidence=None)
        if cur is not None:
            values.update({f: getattr(cur, f) for f in values})
        values.update(fields)
        self.p6_zones[zid] = ZoneState(**values)
        return self.p6_zones[zid]

    def _p6_apply(self, event) -> list:
        before = self.p6_snapshot()
        z = self.p6_zones.get(event.zid)
        if z is None:
            return []
        if isinstance(event, ZoneTune):
            self.p6_tune.setdefault(z.id, {})[event.field] = event.value
            if event.field == "cd":
                self.p6_zone(z.id, delay_s=event.value, delay_src="manual")
            else:
                self.p6_zone(z.id, margin_cm=event.value)
        elif isinstance(event, ZoneForget):
            self.p6_tune.pop(z.id, None)
            self.p6_zone(z.id, delay_s=300, delay_src="default", margin_cm=25, requested=(), ldr=False)
        else:
            want = [v for v in z.requested if v != event.variant]
            if event.on and event.variant in z.variants:
                want.append(event.variant)
            self.p6_zone(z.id, requested=tuple(sorted(want)))
        after = self.p6_snapshot()
        return [] if after == before else [after, SaveRequested("zone")]


class FakeNode:
    """A fake LED matrix node with the P2 radar topics, next to a ``FakeMqttClient``.

    ``announce`` delivers what the broker sends right after the app subscribes (the node's retained
    states); ``process`` reads what the app has published since the last call and, like the ESP, echoes
    the schedule and the night times on their retained state topics. ``p2=False`` is a P1 node: it has
    no schedule topics at all, so it never echoes. ``echo=False`` drops the echo (a node that does not
    confirm).
    """

    def __init__(self, client, prefix: str = "led_matrix_node", p2: bool = True, sched: bool = False,
                 start: str = "00:00:00", end: str = "06:00:00", echo: bool = True, online: bool = True) -> None:
        self.client = client
        self.prefix = prefix
        self.p2 = p2
        self.sched = sched
        self.start = start
        self.end = end
        self.echo = echo
        self.online = online
        self.received: list = []          # (topic, payload text) of every command that reached the node
        self._cursor = 0

    def announce(self) -> None:
        p = self.prefix
        self.client.fire_message(f"{p}/sensors/status", "online" if self.online else "offline", retain=True)
        if self.p2:
            self.client.fire_message(f"{p}/radar/schedule/state", "1" if self.sched else "0", retain=True)
            self.client.fire_message(f"{p}/radar/night_start/state", self.start, retain=True)
            self.client.fire_message(f"{p}/radar/night_end/state", self.end, retain=True)

    def process(self) -> int:
        """Takes the app's new publishes; returns how many commands reached the node."""
        pub = self.client.published
        if len(pub) < self._cursor:
            self._cursor = 0
        new = pub[self._cursor:]
        self._cursor = len(pub)
        p = self.prefix
        got = 0
        for topic, payload, _qos, retain in new:
            if retain or not topic.startswith(p + "/radar/") or topic.endswith("/state") or not self.p2:
                continue
            text = payload.decode("utf-8")
            self.received.append((topic, text))
            got += 1
            if topic == f"{p}/radar/schedule" and text in ("1", "0"):
                self.sched = text == "1"
                if self.echo:
                    self.client.fire_message(f"{p}/radar/schedule/state", text, retain=True)
            elif topic == f"{p}/radar/night_start":
                self.start = text
                if self.echo:
                    self.client.fire_message(f"{p}/radar/night_start/state", text, retain=True)
            elif topic == f"{p}/radar/night_end":
                self.end = text
                if self.echo:
                    self.client.fire_message(f"{p}/radar/night_end/state", text, retain=True)
        return got

    def user_sets_schedule(self, on: bool) -> None:
        """Somebody else (the user in Home Assistant) switches the schedule: the node echoes it."""
        self.sched = on
        self.client.fire_message(f"{self.prefix}/radar/schedule/state", "1" if on else "0", retain=True)


def quiet_logs(testcase) -> None:
    """Keeps expected warnings of the app's loggers out of the test output (assertLogs still works)."""
    logger = logging.getLogger("radar_zone_learner")
    handler = logging.NullHandler()
    logger.addHandler(handler)
    testcase.addCleanup(logger.removeHandler, handler)


__all__ = ["FakeMqttClient", "FakeClientFactory", "FakeMessage", "FakeMessageInfo", "FakeReasonCode",
           "FakeClock", "FakeLearner", "FakeZone6", "FakeNode", "empty_debug_map", "quiet_logs", "DebugMapChanged",
           "clean_tuning_line", "clean_tuning_text"]
