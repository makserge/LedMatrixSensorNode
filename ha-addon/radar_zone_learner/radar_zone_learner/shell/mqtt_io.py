"""MQTT I/O on paho-mqtt 2.x (spec 10.1).

paho is imported only inside ``_default_client_factory()``, which ``MqttIO.start()`` calls when no
factory was given; importing this module needs no paho. Tests pass ``tests.fakes.FakeClientFactory``.

The paho network thread only maps messages (``topics.TopicMapper``) and puts them in a bounded queue;
the worker thread (``service.Service``) does everything else. Every callback is wrapped so that an
exception is logged and counted and never ends paho's thread.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Callable, Optional

from .topics import ClearRetained, HaBirth, Republish, TopicMapper, Topics

__all__ = ["MqttIO", "Republish", "HaBirth", "make_client_id"]

log = logging.getLogger("radar_zone_learner.mqtt")


def make_client_id() -> str:
    return "radar_zone_learner-" + os.urandom(4).hex()


def _default_client_factory(client_id: str):
    import paho.mqtt.client as mqtt          # lazy: only the app image has paho
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                       protocol=mqtt.MQTTv311, clean_session=True)


def _is_failure(reason_code) -> bool:
    flag = getattr(reason_code, "is_failure", None)
    if flag is not None:
        return bool(flag)
    try:
        return int(reason_code) != 0
    except (TypeError, ValueError):
        return True


class MqttIO:
    def __init__(self, settings, topics: Topics, events: "queue.Queue", client_factory=None, *,
                 clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.topics = topics
        self.events = events
        self.client_factory = client_factory
        self.clock_ms = clock_ms
        self.monotonic = monotonic
        self._refused_since: Optional[float] = None
        make_mapper = getattr(topics, "make_mapper", None)
        self.mapper = make_mapper() if make_mapper is not None else TopicMapper(topics)
        self.client = None
        self.client_id = make_client_id()
        self.connected = False
        self.reconnects = 0
        self.publish_failures = 0
        self.publish_skipped = 0
        self.queue_dropped = 0
        self.callback_errors = 0
        self.retained_cleared = 0
        self._stopping = False
        self._republish_pending = threading.Event()
        self._connect_failed_logged = False
        self.connect_put_timeout_s = 1.0    # how long on_connect may wait for room for the Republish marker

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        factory = self.client_factory or _default_client_factory
        c = factory(self.client_id)
        self.client = c
        c.on_connect = self._guard(self._on_connect)
        c.on_disconnect = self._guard(self._on_disconnect)
        c.on_message = self._guard(self._on_message)
        c.on_connect_fail = self._guard(self._on_connect_fail)
        c.will_set(self.topics.learner_status, "offline", qos=1, retain=True)
        s = self.settings
        if s.mqtt_username:
            c.username_pw_set(s.mqtt_username, s.mqtt_password)
        if s.mqtt_ssl:
            c.tls_set()
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.connect_async(s.mqtt_host, s.mqtt_port, keepalive=30)
        c.loop_start()
        log.info("connecting to %s:%d as %s", s.mqtt_host, s.mqtt_port, self.client_id)

    def stop(self, timeout_s: float = 5.0) -> None:
        """Publishes 'offline' retained (waits up to 2 s for it), disconnects and stops paho's thread."""
        self._stopping = True
        c = self.client
        if c is None:
            return
        if self.connected:
            try:
                info = c.publish(self.topics.learner_status, "offline", qos=1, retain=True)
                wait = getattr(info, "wait_for_publish", None)
                if wait is not None:
                    wait(timeout=min(2.0, max(0.0, timeout_s)))
            except Exception as exc:          # not connected any more, or the wait failed
                log.warning("offline message not confirmed: %s", exc.__class__.__name__)
        try:
            c.disconnect()
        except Exception as exc:
            log.warning("disconnect failed: %s", exc.__class__.__name__)
        try:
            c.loop_stop()
        except Exception as exc:
            log.warning("stopping the MQTT thread failed: %s", exc.__class__.__name__)
        self.connected = False

    # ------------------------------------------------------------ publishing (worker thread)
    def publish(self, topic: str, payload, qos: int = 1, retain: bool = False) -> bool:
        """True when paho accepted the message; skipped (False) while disconnected."""
        c = self.client
        if c is None or not self.connected:
            self.publish_skipped += 1
            return False
        try:
            info = c.publish(topic, payload, qos=qos, retain=retain)
        except Exception as exc:
            self.publish_failures += 1
            log.warning("publish to %s failed: %s", topic, exc.__class__.__name__)
            return False
        rc = getattr(info, "rc", 0)
        if rc:
            self.publish_failures += 1
            log.debug("publish to %s returned rc %s", topic, rc)
            return False
        return True

    @property
    def oversize(self) -> int:
        """Messages dropped for their size (see TopicMapper.map)."""
        return self.mapper.oversize

    def refused_for_s(self) -> float:
        """Seconds the broker has refused every connection since the first refusal (0 when it does not)."""
        since = self._refused_since
        return 0.0 if since is None else max(0.0, self.monotonic() - since)

    def take_republish_request(self) -> bool:
        """True once after a connect whose Republish marker did not fit in the queue."""
        if self._republish_pending.is_set():
            self._republish_pending.clear()
            return True
        return False

    # ------------------------------------------------------------ callbacks (paho thread)
    def _guard(self, callback):
        def wrapper(*args, **kwargs):
            try:
                callback(*args, **kwargs)
            except Exception:
                self.callback_errors += 1
                log.exception("error in MQTT callback %s", callback.__name__)
        wrapper.__name__ = callback.__name__
        return wrapper

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if _is_failure(reason_code):
            if self._refused_since is None:
                self._refused_since = self.monotonic()
            log.error("broker refused the connection to %s:%d: %s", self.settings.mqtt_host,
                      self.settings.mqtt_port, reason_code)
            return
        self._refused_since = None
        self.connected = True
        self._connect_failed_logged = False
        log.info("connected to %s:%d", self.settings.mqtt_host, self.settings.mqtt_port)
        subs = self.topics.subscriptions()
        client.subscribe(subs)
        log.info("subscribed (%d topics)", len(subs))
        self.publish(self.topics.learner_status, "online", qos=1, retain=True)
        try:
            self.events.put(Republish(), timeout=self.connect_put_timeout_s)
        except queue.Full:
            self._republish_pending.set()

    def _on_connect_fail(self, client, userdata) -> None:
        if not self._connect_failed_logged:
            log.warning("cannot reach the broker at %s:%d; retrying", self.settings.mqtt_host,
                        self.settings.mqtt_port)
            self._connect_failed_logged = True

    def _on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None) -> None:
        was_connected = self.connected
        self.connected = False
        if self._stopping:
            return
        if was_connected:
            self.reconnects += 1
            log.warning("disconnected from the broker (%s); paho reconnects", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        recv_ms = self.clock_ms()
        payload = msg.payload if msg.payload is not None else b""
        for item in self.mapper.map(msg.topic, payload, bool(msg.retain), recv_ms):
            if isinstance(item, ClearRetained):
                log.info("retained message on %s ignored and cleared", item.topic)
                self.mapper.note_cleared(item.topic)
                if self.publish(item.topic, b"", qos=1, retain=True):
                    self.retained_cleared += 1
                continue
            try:
                self.events.put_nowait(item)
            except queue.Full:
                self.queue_dropped += 1
