"""The zone publisher of the P6 layer (P6 spec 5.7, 7.1, 7.5, 8.1-8.6, D19, D20, D22, Appendix B.1).

``ZonePublisher`` turns what the core says about the confirmed zones (``ZoneEvent``, ``ZoneStates``, the zone views and the
copy of typical hours and confidence) and what the shell knows about the radar into MQTT messages. It decides nothing about
occupancy: it only transports and displays, in this order of rules:

- **Diffed publishing.** A topic is published only when its value differs from what was last published (the recorder lever of
  spec 7.5). Occupancy and availability are never delayed or limited: an automation must see every ``on`` and ``off`` at once,
  and Home Assistant must never hold a stale occupancy (D22). The count is published at most once a second per zone, with a
  trailing publish of the latest value (``CD:entities/Zone target count``).
- **Availability (D19, spec 5.7).** The new retained topic ``radar_zones/zone/availability`` is ``online`` only while the core's
  ``ZoneStates.valid`` and the shell's own radar validity are both true, and ``offline`` at once when either turns false.
  While it is ``offline`` nothing is published except the configs, the retained tuning and request states and the attributes
  (so that ``state_source`` is visible while the radars are off).
- **Order at resume (D20).** There is no reset to OFF. Going from unavailable to available the publisher sends the real
  state of every zone (occupancy, count, variant sensors) first and ``online`` last, so Home Assistant sees ``unavailable ->
  on`` or ``unavailable -> off``, never ``unavailable -> off -> on`` and never the evening's stale ``on``. This relies on
  HA[U3] (a payload that arrives while an entity is unavailable is kept and shown when it becomes available, and two publishes
  of one client arrive in order): acceptance A2.
- **Entities follow the zone set (spec 8.4).** The discovery configs are kept by diff: a new zone gets its configs, a rename
  republishes them (same ``unique_id``), a removed zone, a removed variant or a request switch that goes OFF gets an empty not
  retained config once and an empty retained publish on its retained state topics (D25, D43).
- **Optional valve (D22, default OFF).** After ``valve_after_episodes`` episodes of a zone in a local day its event, count,
  variant and attribute publishes stop until the local day ends, then one trailing publish of the latest values follows;
  occupancy and availability continue.

Everything runs on the worker thread; the publish function is ``MqttIO.publish`` (returns ``False`` while disconnected, and
then nothing is remembered as published, so the next update sends it again).
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from ..core.params import DEFAULT_P6
from .discovery_p6 import OCC_OFF, OCC_ON, ONLINE, OFFLINE, entity_zones, p6_discovery_messages
from .p6_types import ZoneEntry, ZoneEvent, ZoneState, ZoneStates

MIN_COUNT_INTERVAL_S = 1.0          # CD:entities/Zone target count: on change, at most once a second
DOWN_BACKOFF_S = 1.5                # after a publish the broker link refused, no new attempt for this long (no retry storm)
RESEND_S = 10.0                     # a new zone's not retained states again after this: HA creates the entity after the config
COUNT_MAX = 3
SOURCES = ("live", "failsafe", "radar_off", "warmup", "fault")


# ---------------------------------------------------------------- pure helpers
def state_source(radar_state: Optional[str], observing: Optional[bool], node_online: Optional[bool],
                 core_state: str = "learning") -> str:
    """The ``state_source`` attribute from the shell's mirror of the node's ``radar/state`` and ``radar/observing`` (spec 5.7):
    ``live`` (on, forced_on; also while the P2 topics are unknown), ``failsafe``, ``radar_off`` (night_off, forced_off),
    ``warmup`` (powered, not yet observing) and ``fault`` (the node is offline, or frames stopped while it is powered). The
    values beyond ``live`` are a PROPOSAL (``CD`` gives ``live | held_empty_off | held_night`` for the room entity only)."""
    if node_online is False:
        return "fault"
    if radar_state in ("night_off", "forced_off"):
        return "radar_off"
    if observing is False:
        return "warmup"
    if radar_state == "failsafe_on":
        return "failsafe"
    if core_state == "no_data":
        return "fault"
    if radar_state is None and core_state == "not_observing":
        return "radar_off"
    return "live"


def attribute_payload(view, zs: Optional[ZoneState], source: str) -> str:
    """The six attributes of ``CD:entities/Zone attributes``, always all six (spec 8.3): ``kind``, ``state_source``,
    ``variant_now`` (``""`` when none), ``typical_hours`` (``wd 09-12,13-17; we 10-22``, ``""`` when none), ``confidence``
    (the stored suggestion's ``conf`` rounded to two decimals, ``null`` for a label, a manual zone or a zone that never had a
    suggestion) and ``poly_cm`` (at most 12 pairs). The last two of the zone state's fields come from the core's copy
    (``ZoneState.typical_hours`` and ``ZoneState.confidence``). Not published: the clear delay, its source,
    ``ld2412_reliable``, ``frozen``."""
    th = getattr(zs, "typical_hours", "") if zs is not None else ""
    conf = getattr(zs, "confidence", None) if zs is not None else None
    conf = round(float(conf), 2) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None
    poly = [[int(round(x)), int(round(y))] for x, y in tuple(view.poly_cm)[:12]]
    obj = {"kind": view.kind, "state_source": source, "variant_now": zs.variant if zs is not None else "",
           "typical_hours": th if isinstance(th, str) else "", "confidence": conf, "poly_cm": poly}
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def event_payload(ev: ZoneEvent) -> str:
    """The event payload (spec 8.1): ``event_type``, ``track`` (``t`` + the track id) and the variant name; a leave also has
    ``dwell_s`` and ``via``. No zone name, no coordinate; ``variant`` is left out when there is none."""
    obj = {"event_type": ev.kind, "track": "t%d" % ev.track}
    if ev.kind == "leave":
        obj["dwell_s"] = int(ev.dwell_s)
        obj["via"] = ev.via
    if ev.variant:
        obj["variant"] = ev.variant
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------- the publisher
class ZonePublisher:
    def __init__(self, publish: Callable[..., bool], topics, monotonic: Callable[[], float], *, p6=DEFAULT_P6,
                 day_of: Optional[Callable[[], int]] = None) -> None:
        self._publish = publish
        self.topics = topics
        self._mono = monotonic
        self.p6 = p6
        self._day_of = day_of
        self.epoch = 0
        # what the core and the shell said last
        self._views: tuple = ()
        self._zs: dict = {}                  # zid -> ZoneState (the last known: a ZoneStates with no zones keeps them)
        self._core_valid = False
        self._shell_valid = False
        self._source = "live"
        self.entries: dict = {}              # zid -> ZoneEntry of the zones that have entities
        self._view_of: dict = {}
        # what was published
        self._cfg: dict = {}                 # config topic -> payload
        self._static: dict = {}              # attributes, tuning and request states: topic -> payload
        self._dyn: dict = {}                 # occupancy, variant sensors: topic -> payload (forgotten while unavailable)
        self._retained: set = set()          # the retained state topics this publisher wrote
        self._avail: Optional[bool] = None
        self._count_pub: dict = {}           # zid -> the count last published
        self._count_at: dict = {}            # zid -> monotonic time of that publish
        self._count_want: dict = {}          # zid -> the latest count to publish
        self._retry = False                  # something could not be published: the next tick tries again
        self._down_until = 0.0               # monotonic time before which the publisher does not try again (link down)
        self._cfg_sig: Optional[tuple] = None    # the entries and the epoch the configs were last synchronised for
        self._resend_at: Optional[float] = None
        # the valve
        self._valve_day: Optional[int] = None
        self._episodes: dict = {}
        self._valved: set = set()
        # counters
        self.events_published = 0
        self.events_dropped = 0

    # ------------------------------------------------------------ small things
    def set_epoch(self, epoch: int) -> None:
        self.epoch = int(epoch)

    def known(self) -> tuple:
        """``(frozenset of the zone ids that have entities, {zid: frozenset of its variant ids})``: what a command may name."""
        return (frozenset(self.entries), {z: frozenset(e.variants) for z, e in self.entries.items()})

    def variants_of(self, zid: str) -> tuple:
        e = self.entries.get(zid)
        return e.variants if e is not None else ()

    @property
    def online(self) -> bool:
        return self._core_valid and self._shell_valid

    @property
    def valved_zones(self) -> int:
        return len(self._valved)

    def _send(self, topic: str, payload, retain: bool) -> bool:
        now = self._mono()
        if now < self._down_until:
            self._retry = True
            return False
        ok = bool(self._publish(topic, payload, qos=1, retain=retain))
        if not ok:
            self._retry = True
            self._down_until = now + DOWN_BACKOFF_S
        return ok

    def _set(self, store: dict, topic: str, payload, retain: bool, force: bool = False) -> bool:
        if not force and store.get(topic) == payload:
            return False
        if self._send(topic, payload, retain):
            store[topic] = payload
            if retain:
                self._retained.add(topic)
            return True
        return False

    def _clear_retained(self, topic: str) -> None:
        if self._send(topic, b"", True):
            self._retained.discard(topic)
            self._static.pop(topic, None)

    def _send_avail(self, online: bool) -> bool:
        if self._send(self.topics.zone_availability, ONLINE if online else OFFLINE, True):
            self._avail = online
            return True
        return False

    # ------------------------------------------------------------ updates
    def take(self, zones_view, zone_states: Optional[ZoneStates], shell_valid: bool, state_source_: str) -> None:
        """Takes what the layer knows now without publishing (the layer's ``discovery`` and ``republish`` publish in their own
        order). ``None`` for a part leaves what was known."""
        self._shell_valid = bool(shell_valid)
        self._source = state_source_
        if zone_states is not None:
            self._take_states(zone_states)
        if zones_view is not None:
            self._views = tuple(zones_view)

    def update(self, zones_view, zone_states: Optional[ZoneStates], shell_valid: bool, state_source_: str) -> None:
        """One step: take what the layer knows now and publish what changed, in the order of spec 5.7 (the configs, the
        retained and attribute states, then occupancy and count, and last ``online``; ``offline`` at once)."""
        self.take(zones_view, zone_states, shell_valid, state_source_)
        self._roll_day()
        self._publish_all()

    def _take_states(self, zs: ZoneStates) -> None:
        self._core_valid = bool(zs.valid)
        if zs.zones or zs.valid:
            self._zs = {z.id: z for z in zs.zones}
        else:
            # an invalid snapshot with no zones is 'not known': keep the last tuning and variants so they stay visible
            pass

    def event(self, ev: ZoneEvent) -> bool:
        """Publishes one enter or leave on the zone's event topic (not retained). A zone that has no entity, an event the
        valve holds back and an event that the broker link could not take are dropped and counted."""
        if ev.id not in self.entries:
            self.events_dropped += 1
            return False
        self._roll_day()
        if ev.kind == "enter":
            n = self._episodes.get(ev.id, 0) + 1
            self._episodes[ev.id] = n
            limit = self.p6.valve_after_episodes
            if limit > 0 and n > limit:
                self._valved.add(ev.id)
        if ev.id in self._valved:
            self.events_dropped += 1
            return False
        ok = bool(self._publish(self.topics.zone_event(ev.id), event_payload(ev), qos=1, retain=False))
        if ok:
            self.events_published += 1
        else:
            self.events_dropped += 1
        return ok

    # ------------------------------------------------------------ the three groups
    def _publish_all(self, force: bool = False) -> None:
        self._sync_configs(force)
        self._publish_static(force)
        self._publish_dynamic(force)

    def _build_entries(self) -> dict:
        out = {}
        views = entity_zones(self._views, self.p6.zone_entities_max)
        self._view_of = {v.id: v for v in views}
        for v in views:
            zs = self._zs.get(v.id)
            variants = tuple(zs.variants) if zs is not None else ()
            requested = tuple(r for r in zs.requested if r in variants) if zs is not None else ()
            out[v.id] = ZoneEntry(v.id, v.name, variants, requested)
        return out

    def _sync_configs(self, force: bool = False) -> None:
        """The discovery configs by diff (spec 8.4): new and changed ones are published, the ones that are no longer wanted
        get an empty payload once (D25, D43)."""
        entries = self._build_entries()
        self.entries = entries
        sig = (tuple(entries.values()), self.epoch)
        if sig == self._cfg_sig and not force:
            return
        desired = dict(p6_discovery_messages(self.topics, list(entries.values()), None, self.p6.zone_entities_max,
                                             self.epoch))
        fresh = False
        complete = True
        for topic, payload in desired.items():
            if not force and self._cfg.get(topic) == payload:
                continue
            if self._send(topic, payload, False):
                self._cfg[topic] = payload
                fresh = True
            else:
                complete = False
        for topic in [t for t in self._cfg if t not in desired]:
            if self._send(topic, b"", False):
                del self._cfg[topic]
            else:
                complete = False
        self._cfg_sig = sig if complete else None
        if fresh:
            self._resend_at = self._mono() + RESEND_S

    def _wanted_retained(self) -> dict:
        out = {}
        t = self.topics
        for zid, e in self.entries.items():
            zs = self._zs.get(zid)
            if zs is None:
                continue
            out[t.zone_clear_delay(zid)] = str(int(zs.delay_s))
            out[t.zone_margin(zid)] = str(int(zs.margin_cm))
            for v in e.variants:
                out[t.zone_variant_entity(zid, v)] = OCC_ON if v in zs.requested else OCC_OFF
        return out

    def _publish_static(self, force: bool = False) -> None:
        """The retained tuning and request states and the attributes: also while the zones are unavailable."""
        wanted = self._wanted_retained()
        for topic, payload in wanted.items():
            self._set(self._static, topic, payload, True, force)
        # the retained states of a removed zone, a removed variant: an empty retained publish once (spec 7.1)
        stale_prefixes = ("clear_delay", "margin", "entity")
        for topic in sorted(self._retained):
            if topic not in wanted and topic.rsplit("/", 1)[-1] in stale_prefixes:
                self._clear_retained(topic)
        for zid in self.entries:
            if zid in self._valved:
                continue
            view = self._view_of.get(zid)
            if view is None:
                continue
            payload = attribute_payload(view, self._zs.get(zid), self._source)
            self._set(self._static, self.topics.zone_attributes(zid), payload, False, force)

    def _publish_dynamic(self, force: bool = False) -> None:
        if not self.online:
            if self._avail is not False:
                self._send_avail(False)
            # the real state is published again at the next resume, before 'online' (D20)
            self._dyn.clear()
            self._count_pub.clear()
            self._count_at.clear()
            self._count_want.clear()
            return
        self._publish_states(force)
        if self._avail is not True:
            self._send_avail(True)

    def _publish_states(self, force: bool = False) -> None:
        t = self.topics
        now = self._mono()
        for zid, entry in self.entries.items():
            zs = self._zs.get(zid)
            if zs is None:
                continue
            occ = bool(zs.occ)
            self._set(self._dyn, t.zone_occupancy(zid), OCC_ON if occ else OCC_OFF, False, force)
            if zid in self._valved:
                continue
            self._count_want[zid] = max(0, min(COUNT_MAX, int(zs.count)))
            self._publish_count(zid, now, force)
            for v in entry.requested:
                self._set(self._dyn, t.zone_variant(zid, v), OCC_ON if occ and zs.variant == v else OCC_OFF, False, force)

    def _publish_count(self, zid: str, now: float, force: bool = False) -> None:
        want = self._count_want.get(zid)
        if want is None:
            return
        pub = self._count_pub.get(zid)
        if want == pub and not force:
            return
        last = self._count_at.get(zid)
        if not force and pub is not None and last is not None and now - last < MIN_COUNT_INTERVAL_S:
            return                                  # the trailing publish follows in ``tick``
        if self._send(self.topics.zone_count(zid), str(want), False):
            self._count_pub[zid] = want
            self._count_at[zid] = now

    # ------------------------------------------------------------ the valve
    def _day(self) -> Optional[int]:
        return self._day_of() if self._day_of is not None else None

    def _roll_day(self) -> bool:
        """A new local day: the episode counts and the valve start again; the latest values follow in the next publish."""
        day = self._day()
        if day is None or day == self._valve_day:
            return False
        first = self._valve_day is None
        self._valve_day = day
        if first:
            return False
        self._episodes.clear()
        had = bool(self._valved)
        self._valved.clear()
        if had:
            self._retry = True                      # the trailing publish of the latest values (the next tick or update)
        return had

    # ------------------------------------------------------------ the layer's calls
    def tick(self) -> None:
        """Once a second: the trailing count, the day roll of the valve, the resend after a new config, a retry."""
        self._roll_day()
        now = self._mono()
        if self.online:
            for zid in list(self._count_want):
                if zid in self.entries and zid not in self._valved:
                    self._publish_count(zid, now)
        if self._retry:
            self._retry = False
            self._publish_all()
        if self._resend_at is not None and now >= self._resend_at:
            self._resend_at = None
            self.second_pass()

    def republish(self) -> None:
        """After a connect or a Home Assistant birth (spec 8.6): the configs are sent by the layer's ``discovery``; here the
        states, in the order of spec 5.7: retained tuning and request states, attributes, then (only while the zones are
        valid) occupancy and count, and last the availability."""
        self._static.clear()
        self._dyn.clear()
        self._count_pub.clear()
        self._count_at.clear()
        self._avail = None
        self._retry = False
        self._down_until = 0.0                       # a connect (or a birth) is why this runs: try at once
        self._roll_day()
        self._publish_all()

    def second_pass(self) -> None:
        """10 s after a burst (spec 8.6): Home Assistant creates an entity only after its config and drops earlier states, so
        the attributes, the occupancy, the count and the variant sensors are sent once more."""
        self._publish_static(False)
        for zid in self.entries:
            if zid not in self._valved:
                topic = self.topics.zone_attributes(zid)
                view = self._view_of.get(zid)
                if view is not None:
                    self._set(self._static, topic, attribute_payload(view, self._zs.get(zid), self._source), False, True)
        if self.online:
            self._publish_states(True)

    def discovery_messages(self) -> list:
        """The full burst of the P6 configs for the current zones (the service publishes them all, not retained)."""
        entries = self._build_entries()
        self.entries = entries
        msgs = p6_discovery_messages(self.topics, list(entries.values()), None, self.p6.zone_entities_max, self.epoch)
        new = dict(msgs)
        pending = [t for t in self._cfg if t not in new]      # configs of removed zones whose empty publish was refused
        self._cfg = new
        self._cfg_sig = (tuple(entries.values()), self.epoch)
        self._down_until = 0.0
        self._resend_at = None                       # the service's own second pass follows a burst
        return list(msgs) + [(t, b"") for t in pending]

    def offline(self) -> None:
        """At ``bind`` (spec 5.7, Appendix B.1): the zone availability is ``offline`` before any state is published, so a
        restart of the add-on never shows the previous run's occupancy as available. While the link is down this fails and
        the next tick (or the burst after the connect) sends it. It never waits for a refusal backoff: the refused publish at
        ``bind`` (MQTT not started yet) must not delay the ``offline`` that the first connect sends."""
        self._down_until = 0.0
        self._send_avail(False)

    def shutdown(self) -> None:
        """Best effort before the connection closes: every zone is unavailable."""
        self._send_avail(False)

    def reset(self) -> None:
        """Forget all (spec 6.3, 8.4): every entity of every zone is removed (empty configs, empty retained states), the
        availability is ``offline`` and everything published is forgotten. The new learner is bound right after."""
        for topic in list(self._cfg):
            if self._send(topic, b"", False):
                del self._cfg[topic]                 # a refused removal stays known: the next burst sends it (review 13)
        for topic in sorted(self._retained):
            if self._send(topic, b"", True):
                self._retained.discard(topic)
        self._send_avail(False)
        self._cfg_sig = None
        self._static.clear()
        self._dyn.clear()
        self._count_pub.clear()
        self._count_at.clear()
        self._count_want.clear()
        self._episodes.clear()
        self._valved.clear()
        self._valve_day = None
        self.entries = {}
        self._view_of = {}
        self._views = ()
        self._zs = {}
        self._core_valid = False
        self._resend_at = None
        self._retry = False
