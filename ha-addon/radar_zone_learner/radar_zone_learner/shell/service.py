"""Main loop of the app (spec 10.2, 10.3): wiring, timers, signals, publishing of the core's outputs.

One worker thread (the main thread) owns the learner: it takes events from the queue that the paho
thread fills, calls ``learner.handle``/``learner.tick``, and executes every output (publish, render,
save). The core is never touched from another thread.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import queue
import random
import signal
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from ..core.types import (APP_VERSION, DebugMapChanged, FixedOffsetTime, MarkResult, NameEcho, NightlyDone,
                          NightlyFailed, SaveRequested, StatusChanged)
from .discovery import discovery_messages
from .health import HealthServer, StallWatchdog
from .mqtt_io import MqttIO
from .options import SettingsError, configure_logging, load_settings
from .p5 import P5Layer
from .persistence import StateStore
from .png import encode_png
from .render import render
from .resources import CpuMeter, rss_mb
from .timemodel import ZoneInfoTime
from .topics import HaBirth, Republish, build_topics
from .topics_p5 import build_topics_p5

QUEUE_SIZE = 2000                 # D28
TICK_PERIOD_S = 1.0               # spec 4.2: at least once a second
SAVE_PERIOD_S = 1800.0            # OA 10: every 30 min
CPU_SAMPLE_PERIOD_S = 60.0        # spec 10.2
REPUBLISH_DELAY_MAX_S = 10.0      # spec 9.2: 0-10 s after the HA birth message
SAVE_MIN_INTERVAL_S = 10.0        # label and name saves: at most one write in this time (the next one waits)
MAP_REPUBLISH_DELAY_S = 10.0      # the debug map again after a discovery burst: Home Assistant subscribes to
                                  # the image topic only after it has created the entity from the config
REFUSED_EXIT_S = 300.0            # the broker refused every connection for this long: exit 72 (fresh credentials)
ERROR_LIMIT = 100                 # D28: more than 100 errors in 10 min -> exit 71
ERROR_WINDOW_S = 600.0
EXIT_OK, EXIT_SETUP, EXIT_ERRORS, EXIT_REFUSED = 0, 1, 71, 72

log = logging.getLogger("radar_zone_learner")


def _utc_minute(epoch_ms: Optional[int]) -> Optional[str]:
    if epoch_ms is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(epoch_ms // 1000))


class Service:
    """The worker loop; testable without paho (takes MqttIO-like, StateStore-like and learner objects)."""

    def __init__(self, settings, learner_factory: Callable, mqtt, store, health, watchdog,
                 clock_ms: Callable[[], int], monotonic: Callable[[], float], *,
                 topics=None, events: Optional[queue.Queue] = None, rng: Optional[random.Random] = None,
                 cpu: Optional[CpuMeter] = None, rss: Callable[[], Optional[float]] = rss_mb,
                 time_model=None, p4=None) -> None:
        self.settings = settings
        self.learner_factory = learner_factory
        self.mqtt = mqtt
        self.store = store
        self.health = health
        self.watchdog = watchdog
        self.clock_ms = clock_ms
        self.monotonic = monotonic
        self.topics = topics if topics is not None else build_topics(settings)
        self.events = events if events is not None else mqtt.events
        self.rng = rng if rng is not None else random.Random()
        self.cpu = cpu if cpu is not None else CpuMeter()
        self.rss = rss
        self.time_model = time_model if time_model is not None else FixedOffsetTime(0)
        self.p4 = p4                     # shell.p4.P4Layer (P5: shell.p5.P5Layer, the one layer object), or None:
                                         # then the app is the P3 app. The P5 hooks are looked up with getattr, so a
                                         # plain P4Layer and None keep working.

        self.learner = None
        self.core_errors = 0
        self.shell_errors = 0
        self.save_failures = 0
        self.state_saved_ms: Optional[int] = None
        self.state_bytes: Optional[int] = None
        self._errors: deque = deque()
        self._exit_code: Optional[int] = None
        self._stop = threading.Event()
        self._status: Optional[StatusChanged] = None
        self._published_state: Optional[str] = None
        self._logged_state: Optional[str] = None
        self._spot_name: Optional[str] = None
        self._png: Optional[bytes] = None
        self._map_dirty = True
        self._republish_at: Optional[float] = None
        self._map_at: Optional[float] = None
        self._last_req_save: Optional[float] = None
        self._save_pending = False
        self._last_nightly_date: Optional[str] = None
        self._last_call_s = 0.0
        self._last_tick = self._last_save = self._last_cpu = 0.0

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Loads the state, builds the learner, starts health, watchdog and MQTT, publishes the start state."""
        self.learner = self._load_learner()
        bind = getattr(self.p4, "bind", None)
        if bind is not None:
            self._p4_call(bind, self._core, self.learner)    # the layer's mirrors are seeded before the first publish
        self.health.start()
        self.watchdog.start()
        self.mqtt.start()
        if self.p4 is not None:
            self._p4_call(self.p4.start, self.clock_ms())
        now = self.monotonic()
        self._last_tick = self._last_save = self._last_cpu = now
        self.cpu.sample()
        status = self._core(self.learner.status)
        if status is not None:
            self._execute(status)
        self._execute(DebugMapChanged("start"))

    def _load_learner(self):
        state = self.store.load()
        learner = getattr(self.store, "validated", None) if state is not None else None
        if learner is not None and hasattr(learner, "handle"):
            log.info("state loaded (%s)", getattr(self.store, "loaded_from", None) or "state.json")
        else:
            learner = None
            if state is not None:
                try:
                    learner = self.learner_factory(state)
                    log.info("state loaded")
                except ValueError as exc:            # StateError
                    log.warning("saved state rejected (%s): starting fresh", str(exc)[:200])
                    quarantine = getattr(self.store, "quarantine_state", None)
                    if quarantine is not None:
                        quarantine()
                    state = None
            if learner is None:
                log.info("no saved state: starting fresh")
                learner = self.learner_factory(None)
        if state is not None:
            settings = state.get("settings")
            if isinstance(settings, dict) and isinstance(settings.get("spot_name"), str):
                self._spot_name = settings["spot_name"]
        return learner

    def request_stop(self) -> None:
        self._stop.set()

    def run(self, stop_event: threading.Event) -> int:
        try:
            self.start()
        except Exception:
            log.exception("start failed")
            self._teardown()
            return EXIT_SETUP
        log.info("learning in shadow mode (radar_zone_learner %s)", APP_VERSION)
        code = EXIT_OK
        try:
            while not (stop_event.is_set() or self._stop.is_set()):
                try:
                    self.step(1.0)
                except Exception:
                    self.shell_errors += 1
                    log.exception("error in the worker loop")
                    self._count_error()
                if self._exit_code is not None:
                    code = self._exit_code
                    break
        finally:
            self.shutdown(code)
        return code

    def shutdown(self, code: int = EXIT_OK) -> None:
        """Final save (not after repeated errors), 'offline', disconnect, stop health and watchdog."""
        if self.learner is not None and code in (EXIT_OK, EXIT_REFUSED):
            self._save("shutdown")
        if self.p4 is not None:
            self._p4_call(self.p4.shutdown, self.clock_ms())
        self._teardown()
        log.info("stopped (exit %d)", code)

    def _teardown(self) -> None:
        for name, fn in (("mqtt", getattr(self.mqtt, "stop", None)), ("health", getattr(self.health, "stop", None)),
                         ("watchdog", getattr(self.watchdog, "stop", None))):
            if fn is None:
                continue
            try:
                fn()
            except Exception:
                log.exception("stopping %s failed", name)

    # ------------------------------------------------------------ one loop iteration
    def step(self, timeout: float = 1.0) -> None:
        try:
            ev = self.events.get(timeout=timeout) if timeout > 0 else self.events.get_nowait()
        except queue.Empty:
            ev = None
        outs = []
        if isinstance(ev, Republish):
            self._republish()
        elif isinstance(ev, HaBirth):
            delay = self.rng.uniform(0.0, REPUBLISH_DELAY_MAX_S)
            due = self.monotonic() + delay
            self._republish_at = due if self._republish_at is None else min(self._republish_at, due)
        elif ev is not None:
            for e in self._p4_events(ev):
                outs.extend(self._core(self.learner.handle, e) or ())
        now = self.monotonic()
        if now - self._last_tick >= TICK_PERIOD_S:
            self._last_tick = now
            outs.extend(self._core(self.learner.tick, self.clock_ms()) or ())
            if self.p4 is not None:
                self._p4_call(self.p4.tick, self.clock_ms())
        for out in outs:
            self._execute(out)
        self._drain_p4()
        take_forget = getattr(self.p4, "take_forget_request", None)
        if take_forget is not None and self._p4_call(take_forget):
            self.forget_all()
        take = getattr(self.mqtt, "take_republish_request", None)
        if take is not None and take():
            self._republish()
        if self._republish_at is not None and now >= self._republish_at:
            self._republish_at = None
            self._republish()
        if self._save_pending and (self._last_req_save is None or now - self._last_req_save >= SAVE_MIN_INTERVAL_S):
            self._save("deferred")
        if now - self._last_save >= SAVE_PERIOD_S:
            self._save("periodic")
        if self._map_at is not None and now >= self._map_at:
            self._map_at = None
            self._publish_map()
            second = getattr(self.p4, "second_pass", None)
            if second is not None:
                self._p4_call(second, self.clock_ms())
        refused = getattr(self.mqtt, "refused_for_s", None)
        if refused is not None and self._exit_code is None and refused() >= REFUSED_EXIT_S:
            log.critical("the broker has refused every connection for %d s: exiting with %d so that the "
                         "Supervisor restarts the app with fresh credentials", int(REFUSED_EXIT_S), EXIT_REFUSED)
            self._exit_code = EXIT_REFUSED
        if now - self._last_cpu >= CPU_SAMPLE_PERIOD_S:
            self._last_cpu = now
            self.cpu.sample()
        self.watchdog.beat()

    # ------------------------------------------------------------ P4 layer
    def _p4_call(self, fn, *args):
        """Calls the P4 layer; an exception is logged, counted, and gives None (the app goes on as the P3 app)."""
        try:
            return fn(*args)
        except Exception:
            self.shell_errors += 1
            log.exception("error in the P4 layer (%s)", getattr(fn, "__name__", "call"))
            self._count_error()
            return None

    def _p4_events(self, ev) -> list:
        """The events to give the core for one received event: the event itself without the P4 layer."""
        if self.p4 is None:
            return [ev]
        evs = self._p4_call(self.p4.on_event, ev, self.clock_ms())
        return [ev] if evs is None else evs

    def _drain_p4(self) -> None:
        """Gives the core the events the P4 layer produced (command results); their outputs may produce more."""
        if self.p4 is None:
            return
        for _ in range(8):
            evs = self._p4_call(self.p4.take_core_events) or ()
            if not evs:
                return
            outs = []
            for e in evs:
                outs.extend(self._core(self.learner.handle, e) or ())
            for out in outs:
                self._execute(out)

    # ------------------------------------------------------------ core calls and errors
    def _core(self, fn, *args):
        """Calls the core; an exception is logged with its traceback, counted, and gives None."""
        t0 = time.perf_counter()
        try:
            result = fn(*args)
        except Exception:
            self.core_errors += 1
            log.exception("core error in %s", getattr(fn, "__name__", "call"))
            self._count_error()
            return None
        self._last_call_s = time.perf_counter() - t0
        return result

    def _count_error(self) -> None:
        now = self.monotonic()
        self._errors.append(now)
        while self._errors and self._errors[0] < now - ERROR_WINDOW_S:
            self._errors.popleft()
        if len(self._errors) > ERROR_LIMIT and self._exit_code is None:
            log.critical("more than %d errors in %d s: exiting with %d", ERROR_LIMIT, int(ERROR_WINDOW_S),
                         EXIT_ERRORS)
            self._exit_code = EXIT_ERRORS

    # ------------------------------------------------------------ outputs
    def _execute(self, out) -> None:
        if self.p4 is not None and self._p4_call(self.p4.on_output, out, self.clock_ms()):
            return                                   # a P4 output (node command, review items, Night learning)
        if isinstance(out, StatusChanged):
            self._status = out
            self._publish_status(force=False)
        elif isinstance(out, DebugMapChanged):
            self._map_dirty = True
            self._publish_map()
        elif isinstance(out, MarkResult):
            if out.ok:
                log.info("mark ok %s %s", out.label_id, out.kind)
            else:
                log.info("mark failed %s", out.reason)
        elif isinstance(out, NameEcho):
            self._spot_name = out.text
            self.mqtt.publish(self.topics.review_name, out.text, qos=1, retain=True)
        elif isinstance(out, NightlyDone):
            self._last_nightly_date = out.date
            log.info("nightly run: night %d (%s), %d candidates, %d new suggestions, %.2f s",
                     out.night, out.date, out.candidates, out.new_suggestions, self._last_call_s)
        elif isinstance(out, NightlyFailed):
            log.error("nightly run %s failed: %s", out.date, out.error)
        elif isinstance(out, SaveRequested):
            self._save(out.reason)
        else:
            log.warning("unknown core output %s ignored", type(out).__name__)

    def attributes(self, status: StatusChanged) -> dict:
        """Core attributes plus the shell's keys (spec 9.3)."""
        attrs = dict(status.attributes) if isinstance(status.attributes, dict) else {}
        rss = None
        try:
            rss = self.rss()
        except Exception:
            pass
        cpu = self.cpu.percent_1h()
        attrs.update({
            "rss_mb": round(rss, 1) if rss is not None else None,
            "cpu_pct_1h": round(cpu, 3) if cpu is not None else None,
            "queue_dropped": getattr(self.mqtt, "queue_dropped", 0),
            "payload_oversize": getattr(self.mqtt, "oversize", 0),
            "mqtt_reconnects": getattr(self.mqtt, "reconnects", 0),
            "state_saved": _utc_minute(self.state_saved_ms),
            "state_bytes": self.state_bytes,
            "core_errors": self.core_errors,
        })
        if self.p4 is not None:
            attrs.update(self.p4.attributes())
        return attrs

    def _publish_status(self, force: bool) -> None:
        st = self._status
        if st is None:
            return
        if st.state != self._logged_state:
            log.info("status %s", st.state)
            self._logged_state = st.state
        if force or st.state != self._published_state:
            if self.mqtt.publish(self.topics.learner_state, st.state, qos=1, retain=True):
                self._published_state = st.state
        payload = json.dumps(self.attributes(st), separators=(",", ":"), default=str)
        self.mqtt.publish(self.topics.learner_attr, payload, qos=1, retain=True)

    def _publish_map(self) -> None:
        if not self.settings.debug_map:
            return
        if self._png is None or self._map_dirty:
            data = self._core(self.learner.debug_map)
            if data is None:
                return
            try:
                width, height, palette, pixels = render(data)
                self._png = encode_png(width, height, palette, pixels)
            except Exception:
                self.shell_errors += 1
                log.exception("rendering the debug map failed")
                self._count_error()
                return
            self._map_dirty = False
        if self.mqtt.publish(self.topics.learner_debug_map, self._png, qos=0, retain=False):
            log.debug("debug map published (%d bytes)", len(self._png))

    def _republish(self) -> None:
        """Discovery burst, then availability, state, attributes, the name echo and the debug map (9.2)."""
        for topic, payload in discovery_messages(self.settings, self.topics):
            self.mqtt.publish(topic, payload, qos=1, retain=False)
        if self.p4 is not None:
            for topic, payload in self.p4.discovery():
                self.mqtt.publish(topic, payload, qos=1, retain=False)
        self.mqtt.publish(self.topics.learner_status, "online", qos=1, retain=True)
        if self._status is None and self.learner is not None:
            self._status = self._core(self.learner.status)
        self._publish_status(force=True)
        self.mqtt.publish(self.topics.review_name, self._spot_name or "", qos=1, retain=True)
        if self.p4 is not None:
            self._p4_call(self.p4.republish, self.clock_ms())
        self._publish_map()
        self._map_at = self.monotonic() + MAP_REPUBLISH_DELAY_S      # the first copy may be lost, see above

    # ------------------------------------------------------------ Forget all learned data (P5 spec 7.2)
    def forget_all(self) -> None:
        """Resets the learner and everything it stored, in one worker step: the cached inputs are kept, the core makes a
        new learner (``Learner.reset``), the state files and the raw-frame capture are deleted, the fresh state is saved
        at once, the layer's mirrors are cleared and bound to the new learner, the cached inputs are fed to it, every
        state is published again (the retained learner and review topics are overwritten by their empty versions) and
        the review event ``forgotten`` is raised. Not deleted: ``rf_stats.json`` and ``options.json``."""
        layer = self.p4
        if layer is None or self.learner is None:
            return
        cached = self._p4_call(getattr(layer, "cached_inputs", list)) or []
        fresh = self._core(self.learner.reset)
        if fresh is None:
            log.error("Forget all: the learner could not be reset, nothing was changed")
            return
        purge = getattr(layer, "purge_capture", None)
        if purge is not None:
            self._p4_call(purge)
        wipe = getattr(self.store, "wipe", None)
        removed = 0
        if wipe is not None:
            try:
                removed = wipe()
            except Exception:
                self.shell_errors += 1
                log.exception("Forget all: deleting the state files failed")
        self.learner = fresh
        self._status = None
        self._published_state = None
        self._png = None
        self._map_dirty = True
        self._spot_name = ""
        self._last_nightly_date = None
        reset = getattr(layer, "reset", None)
        if reset is not None:
            self._p4_call(reset)
        bind = getattr(layer, "bind", None)
        if bind is not None:
            self._p4_call(bind, self._core, fresh)
        self._save("forget")
        now = self.clock_ms()
        for ev in cached:
            # the cached events keep their meaning but not their time: day 0 of the new learner is now
            outs = self._core(fresh.handle, replace(ev, recv_ms=now)) or ()
            for out in outs:
                self._execute(out)
        log.info("all learned data forgotten (%d state files deleted)", removed)
        self._republish()
        notify = getattr(layer, "notify_forgotten", None)
        if notify is not None:
            self._p4_call(notify)

    # ------------------------------------------------------------ persistence
    def _save(self, reason: str) -> None:
        now = self.monotonic()
        if reason in ("label", "name"):
            # a flood of presses or names must not become a flood of fsync'd writes of 150-250 KB each:
            # one write now, the next ones wait for the interval and are written together
            if self._last_req_save is not None and now - self._last_req_save < SAVE_MIN_INTERVAL_S:
                self._save_pending = True
                return
        if reason in ("label", "name", "deferred"):
            self._last_req_save = now
        self._save_pending = False
        self._last_save = now
        state = self._core(self.learner.export_state)
        if state is None:
            return
        t0 = time.perf_counter()
        try:
            written = self.store.save(state)
        except Exception as exc:
            self.save_failures += 1
            log.error("state save failed (%s): %s: %s", reason, exc.__class__.__name__, exc)
            return
        self.state_saved_ms = self.clock_ms()
        self.state_bytes = written
        if self.p4 is not None and reason in ("periodic", "nightly", "shutdown"):
            self._p4_call(self.p4.save, self.clock_ms())
        level = logging.DEBUG if reason == "periodic" else logging.INFO
        log.log(level, "state saved (%s, %d bytes, %.0f ms)", reason, written, (time.perf_counter() - t0) * 1000)
        if reason == "nightly":
            date_iso = self._last_nightly_date or self._local_date_iso()
            try:
                self.store.snapshot(state, date_iso)
            except Exception as exc:
                log.error("snapshot %s failed: %s: %s", date_iso, exc.__class__.__name__, exc)

    def _local_date_iso(self) -> str:
        return _dt.date.fromordinal(self.time_model.local(self.clock_ms()).date_ord).isoformat()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="radar_zone_learner",
                                     description="Radar zone learner app (shadow mode).")
    parser.add_argument("--options", default="/data/options.json",
                        help="options file; its folder holds the state (default /data/options.json)")
    parser.add_argument("--health-port", type=int, default=8099, help="TCP health port (default 8099)")
    args = parser.parse_args(argv)
    configure_logging("info")
    try:
        settings = load_settings(Path(args.options))
    except SettingsError as exc:
        log.error("invalid options: %s", exc)
        return EXIT_SETUP
    configure_logging(settings.log_level)
    log.info("radar_zone_learner %s starting (shadow mode)", APP_VERSION)

    from ..core.learner import Learner
    from ..core.params import DEFAULT_PARAMS

    params = replace(DEFAULT_PARAMS, night_lux=settings.night_lux)
    if params.night_lux != DEFAULT_PARAMS.night_lux:
        log.info("night_lux %g lx (option)", params.night_lux)
    time_model = ZoneInfoTime(os.environ.get("TZ", "UTC"))
    log.info("time zone %s", time_model.name)

    def learner_factory(state):
        return Learner(params, time_model, state)

    topics = build_topics_p5(settings)
    events: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
    mqtt = MqttIO(settings, topics, events)
    store = StateStore(settings.data_dir, validate=learner_factory)
    p4 = P5Layer(topics, mqtt.publish, settings.data_dir, debug_map=settings.debug_map)   # the one layer object
    service = Service(settings, learner_factory, mqtt, store, HealthServer(args.health_port), StallWatchdog(),
                      clock_ms=lambda: time.time_ns() // 1_000_000, monotonic=time.monotonic,
                      topics=topics, events=events, time_model=time_model, p4=p4)
    stop_event = threading.Event()

    def on_signal(signum, frame):
        log.info("signal %d: stopping", signum)
        service.request_stop()
        stop_event.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    return service.run(stop_event)
