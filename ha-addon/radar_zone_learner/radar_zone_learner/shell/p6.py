"""The P6 shell layer: zone occupancy, count, events, tuning and variant entities (P6 spec 5.7, 7.2, 8, Appendix B.1, B.2).

``P6Layer`` is a ``P5Layer`` and **the one layer object**: ``main`` passes it as the service's ``p4=`` argument, as in P5, so
there is still one ``NodeLink``, one ``RfStats`` and one event buffer, and no event is handled twice. It overrides what P6
changes (discovery, republish, the second pass, the event and output paths, tick, attributes, shutdown, reset, bind) and
inherits the rest unchanged; the service needs no new hook. Everything here runs on the worker thread. It decides nothing
about zones: the core evaluates the radar frames, learns the delays and owns the variants; this layer only transports and
displays them (``zonepub.ZonePublisher`` does the publishing).

What it does:

- publishes, for every confirmed zone of the evaluated kinds (at most ``zone_entities_max``), an occupancy binary sensor with
  its six attributes, a count sensor (disabled by default), an enter/leave event entity, the two tuning numbers
  ``clear_delay`` and ``margin`` and the ``forget`` button, and for a zone that has time variants one request switch per
  variant and, on request, an occupancy sensor per variant (spec 8.1, 8.4);
- keeps the zone entities **unavailable** while the radars are off, warming up or faulted and while the node or the learner is
  dead: the retained topic ``radar_zones/zone/availability`` is ``online`` only while the core's ``ZoneStates.valid`` and this
  layer's own radar validity (``P4Layer._valid``) are both true; the real state is published before ``online`` (spec 5.7, D19,
  D20);
- turns the three zone commands (``ZoneTune``, ``ZoneForget``, ``VariantRequest``) into core events after checking them
  against the zones that have entities; a command for a zone or variant without an entity, or outside its range, is dropped
  and counted (``zone_cmds_ignored``);
- never touches the review path, the push request or any P3-P5 topic: with no zone the layer publishes only
  ``radar_zones/zone/availability`` more than the P5 layer does.

A Python error in the zone code is contained and counted (``zone_shell_errors``) and never reaches the P5 behaviour; the
service's own containment (``S:service.py:_p4_call``) stays behind it.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from ..core.params import DEFAULT_P6
from ..core.types import (AdvancedChanged, NightlyDone, ReviewChanged, StatusChanged, ZONE_TUNE_FIELDS, ZoneEvent,
                          ZoneForget, ZoneStates, ZoneTune, VariantRequest)
from .p4_types import RadarStateMsg
from .p5 import P5Layer
from .p6_types import TUNE_RANGES
from .select_model import SELECT_MODE
from .topics_p6 import TopicMapperP6
from .zonepub import ZonePublisher, state_source

ZONES_POLL_S = 5.0                 # Learner.zones() is read at most this often when nothing says that a zone changed
STATES_POLL_S = 5.0                # Learner.zone_states() is read this often as a safety net (the core emits it on change)

log = logging.getLogger("radar_zone_learner.p6")


def read_epoch(learner) -> int:
    """The installation epoch of the entity ids from the learner's ``zone6`` container (``zone6.export()["set"]["epoch"]``):
    0 until the first Forget all, +1 at every Forget all (spec 6.3, D41). 0 for a learner that has no ``zone6``."""
    export = getattr(getattr(learner, "zone6", None), "export", None)
    if export is None:
        return 0
    d = export()
    st = d.get("set") if isinstance(d, dict) else None
    epoch = st.get("epoch") if isinstance(st, dict) else 0
    return epoch if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0 else 0


class P6Layer(P5Layer):
    def __init__(self, topics, publish: Callable[..., bool], data_dir=None, *,
                 monotonic: Callable[[], float] = time.monotonic, select_mode: str = SELECT_MODE,
                 debug_map: bool = True,
                 clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
                 p6params=DEFAULT_P6, day_of: Optional[Callable[[int], int]] = None) -> None:
        super().__init__(topics, publish, data_dir, monotonic=monotonic, select_mode=select_mode, debug_map=debug_map,
                         clock_ms=clock_ms)
        self.p6params = p6params
        self._clock_ms = clock_ms
        day = day_of if day_of is not None else (lambda ms: ms // 86_400_000)
        self.zpub = ZonePublisher(publish, topics, monotonic, p6=p6params, day_of=lambda: day(self._clock_ms()))
        self.mapper = None
        self.zone_cmds_ignored = 0
        self.zone_shell_errors = 0
        self._zviews: tuple = ()               # ZoneView of every confirmed zone, as the core said last
        self._zstates: Optional[ZoneStates] = None
        self._zones_at: Optional[float] = None
        self._states_at: Optional[float] = None
        self._in_republish = False

    def attach_mapper(self, mapper) -> None:
        """Gives the layer the topic mapper of the MQTT thread: the layer tells it which zones and variants have entities, and
        its drop counter is part of ``zone_cmds_ignored``."""
        self.mapper = mapper if isinstance(mapper, TopicMapperP6) else None
        self._tell_mapper()

    # ------------------------------------------------------------ containment
    def _zone_safe(self, fn, *args):
        try:
            return fn(*args)
        except Exception:
            self.zone_shell_errors += 1
            log.exception("error in the P6 zone code (%s)", getattr(fn, "__name__", "call"))
            return None

    # ------------------------------------------------------------ lifecycle
    def bind(self, core_call: Callable, learner) -> None:
        """Called by ``Service.start()`` before MQTT starts and again after a Forget all swap (after ``reset``). Seeds the zone
        mirrors from the learner; the zone availability is ``offline`` until the core has resumed (spec 5.7)."""
        super().bind(core_call, learner)
        self._zone_safe(self._bind_zones)

    def _bind_zones(self) -> None:
        epoch = self._core(read_epoch, self.learner)
        self.zpub.set_epoch(epoch if epoch is not None else self.zpub.epoch)
        self.zpub.offline()                           # before any state (spec 5.7): the core has not resumed yet
        self._pull_views()
        self._pull_states()
        self._zones_at = self._states_at = self._mono()
        self.zpub.update(self._zviews, self._zstates, self._valid(), self._source())
        self._tell_mapper()

    def reset(self) -> None:
        """Forget all (spec 6.3, 8.4): the P5 mirrors are cleared and every zone entity is removed; the service binds the new
        learner right after."""
        super().reset()
        self._zone_safe(self.zpub.reset)
        self._zviews = ()
        self._zstates = None
        self._zones_at = self._states_at = None
        self._tell_mapper()

    def shutdown(self, now_ms: int) -> None:
        super().shutdown(now_ms)
        self._zone_safe(self.zpub.shutdown)

    # ------------------------------------------------------------ discovery and states
    def discovery(self) -> list:
        """P5's thirty-one messages and the P6 configs of the zones as they are now (six for each, plus the variant entities)."""
        base = super().discovery()
        if self.learner is None:
            return base
        self._zone_safe(self._pull_all)
        self.zpub.take(self._zviews, self._zstates, self._valid(), self._source())
        if not self.zpub.online:
            # the first thing after a connect: a restart of the add-on must not leave the previous run's retained 'online'
            # standing while the learner's own 'online' (sent by the MQTT layer at the connect) is already out
            self._zone_safe(self.zpub.offline)
        msgs = self._zone_safe(self.zpub.discovery_messages) or []
        self._tell_mapper()
        return base + msgs

    def republish(self, now_ms: int) -> None:
        """All states again (after a connect or a Home Assistant birth): the P5 order of spec 8.8, then the zone states in the
        order of spec 5.7: tuning and request states, attributes, occupancy and count (valid only), and last the availability."""
        self._in_republish = True
        try:
            super().republish(now_ms)
        finally:
            self._in_republish = False
        if self.learner is not None:
            self.zpub.take(self._zviews, self._zstates, self._valid(), self._source())
            self._zone_safe(self.zpub.republish)

    def second_pass(self, now_ms: int) -> None:
        super().second_pass(now_ms)
        if self.learner is not None:
            self.zpub.take(self._zviews, self._zstates, self._valid(), self._source())
            self._zone_safe(self.zpub.second_pass)

    def attributes(self) -> dict:
        out = super().attributes()
        ignored = self.zone_cmds_ignored + (self.mapper.zone_cmds_ignored if self.mapper is not None else 0)
        out.update({"zone_entities": len(self.zpub.entries), "zone_cmds_ignored": ignored,
                    "zone_events_sent": self.zpub.events_published, "zone_events_dropped": self.zpub.events_dropped,
                    "zone_valve_zones": self.zpub.valved_zones, "zone_shell_errors": self.zone_shell_errors})
        return out

    # ------------------------------------------------------------ what the zones need from the core
    def _source(self) -> str:
        return state_source(self.radar_state, self.observing, self.node_online, self.core_state)

    def _pull_views(self) -> None:
        views = self._core(self.learner.zones) if self.learner is not None else None
        if views is not None:
            self._zviews = tuple(views)
        self._zones_at = self._mono()

    def _pull_states(self) -> None:
        zs = self._core(self.learner.zone_states) if self.learner is not None else None
        if zs is not None:
            self._zstates = zs
        self._states_at = self._mono()

    def _pull_all(self) -> None:
        self._pull_views()
        self._pull_states()

    def _refresh(self) -> None:
        """Publish what changed: the shell's radar validity, the core's zone states and the zone set as they are now."""
        if self.learner is None:
            return
        self.zpub.update(self._zviews, self._zstates, self._valid(), self._source())
        self._tell_mapper()

    def _tell_mapper(self) -> None:
        if self.mapper is not None:
            self.mapper.known = self.zpub.known()

    def _publish_valid(self) -> None:
        """The P4 'Radar data valid' follows the shell's mirror; the zone availability follows it at once (the fast path)."""
        super()._publish_valid()
        if not self._in_republish and self.learner is not None:
            self._zone_safe(self._refresh)

    # ------------------------------------------------------------ input events
    def on_event(self, ev, now_ms: int) -> list:
        if isinstance(ev, (ZoneTune, ZoneForget, VariantRequest)):
            return self._zone_command(ev)
        out = super().on_event(ev, now_ms)
        if isinstance(ev, RadarStateMsg):
            self._zone_safe(self._refresh)               # state_source follows 'radar/state'
        return out

    def _zone_command(self, ev) -> list:
        """A tuning command, a forget press or a variant request: handed to the core only when the zone has entities, the value
        is in range and (for a request) the variant is one of the zone's. Applied while the radars are off too (D38)."""
        entry = self.zpub.entries.get(ev.zid)
        ok = entry is not None
        if ok and isinstance(ev, ZoneTune):
            lo, hi = TUNE_RANGES.get(ev.field, (1, 0))
            ok = (ev.field in ZONE_TUNE_FIELDS and isinstance(ev.value, int) and not isinstance(ev.value, bool)
                  and lo <= ev.value <= hi)
        elif ok and isinstance(ev, VariantRequest):
            ok = ev.variant in entry.variants
        if not ok:
            self.zone_cmds_ignored += 1
            log.info("zone command %s for %s ignored", type(ev).__name__, ev.zid)
            return []
        self._states_at = None                           # read the new values back at the next tick
        return [ev]

    # ------------------------------------------------------------ core outputs
    def on_output(self, out, now_ms: int) -> bool:
        """True when the output was consumed here (the service must not handle it again)."""
        if isinstance(out, ZoneEvent):
            self._zone_safe(self._zone_event, out)
            return True
        if isinstance(out, ZoneStates):
            self._zone_safe(self._zone_states, out)
            return True
        consumed = super().on_output(out, now_ms)
        if isinstance(out, StatusChanged):
            self._zone_safe(self._refresh)               # the core's state changed: state_source
        elif isinstance(out, (ReviewChanged, AdvancedChanged)):
            self._zone_safe(self._zones_changed)
        elif isinstance(out, NightlyDone):
            self._zone_safe(self._nightly)
        return consumed

    def _zone_states(self, zs: ZoneStates) -> None:
        self._zstates = zs
        have = {v.id for v in self._zviews}
        if any(z.id not in have for z in zs.zones):
            self._pull_views()                           # a zone that is new to the shell
        self._refresh()

    def _zone_event(self, ev: ZoneEvent) -> None:
        if ev.id not in self.zpub.entries:
            self._pull_views()
            self._refresh()
        self.zpub.event(ev)

    def _zones_changed(self) -> None:
        self._pull_views()
        self._refresh()

    def _nightly(self) -> None:
        """The nightly run refreshes the zones' typical hours, confidence and variants (spec 5.8; the core emits the new
        ``ZoneStates`` itself, this is the safety net)."""
        self._pull_all()
        self._refresh()

    # ------------------------------------------------------------ time
    def tick(self, now_ms: int) -> None:
        super().tick(now_ms)
        if self.learner is not None:
            self._zone_safe(self._zone_tick)

    def _zone_tick(self) -> None:
        now = self._mono()
        pulled = False
        if self._zones_at is None or now - self._zones_at >= ZONES_POLL_S:
            self._pull_views()
            pulled = True
        if self._states_at is None or now - self._states_at >= STATES_POLL_S:
            self._pull_states()
            pulled = True
        if pulled:
            self._refresh()
        self.zpub.tick()
