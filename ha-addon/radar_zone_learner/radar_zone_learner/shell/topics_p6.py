"""P6 topics and the mapping of their messages to events (P6 spec 8.1, 8.2, Appendix B.1).

``build_topics_p6`` extends the P5 topics; ``TopicsP6.make_mapper`` makes the mapper that knows them. The P5
``build_topics_p5``/``TopicMapperP5`` are untouched, so a P5 setup subscribes to the same 30 topics as before.

Topics of one zone (``<zid>`` is a zone id ``z0001`` or a label id ``l0001``; zone names never appear in a topic):

- ``radar_zones/zone/<zid>/occupancy``                  ``ON``/``OFF``      not retained
- ``radar_zones/zone/<zid>/count``                      ``0``..``3``        not retained
- ``radar_zones/zone/<zid>/event``                      JSON, enter/leave   not retained
- ``radar_zones/zone/<zid>/attributes``                 JSON, six keys      not retained
- ``radar_zones/zone/<zid>/clear_delay``, ``.../margin``  the value in force   retained; ``.../set`` is the command
- ``radar_zones/zone/<zid>/forget``                     ``PRESS``           command
- ``radar_zones/zone/<zid>/<variant>/occupancy``        ``ON``/``OFF``      not retained (only while requested)
- ``radar_zones/zone/<zid>/<variant>/entity``           ``ON``/``OFF``      retained; ``.../set`` is the command
- ``radar_zones/zone/availability``                     ``online``/``offline``  retained, owned by ``P6Layer`` (spec 5.7)

Inputs added here (4 subscription patterns, all commands, so a retained message on one is ignored and deleted; QoS 1):
``radar_zones/zone/+/clear_delay/set``, ``radar_zones/zone/+/margin/set``, ``radar_zones/zone/+/forget`` and
``radar_zones/zone/+/+/entity/set``. 30 + 4 = 34 subscriptions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Optional

from ..core.types import VARIANT_ID_RE, ZONE_ID_RE, VariantRequest, ZoneForget, ZoneTune
from .p6_types import TUNE_RANGES
from .topics import MAX_SMALL_PAYLOAD_BYTES, ClearRetained, _text
from .topics_p5 import TopicMapperP5, TopicsP5, build_topics_p5

ZONE_BASE = "radar_zones/zone"
_ZID_RE = re.compile(ZONE_ID_RE)
_VID_RE = re.compile(VARIANT_ID_RE)
_NUMBER_RE = re.compile(r"^[+-]?[0-9]{1,5}(?:\.0{1,3})?$")      # a decimal integer, '120' or '120.0' (never '1e2', 'nan')
_NUMBER_FIELDS = {"clear_delay": "cd", "margin": "mg"}


@dataclass(frozen=True)
class TopicsP6(TopicsP5):
    zone_availability: str                  # output only, retained: the zone entities' own availability topic

    # ------------------------------------------------------------ builders (spec Appendix B.1)
    def zone_occupancy(self, zid: str) -> str:
        return f"{ZONE_BASE}/{zid}/occupancy"

    def zone_count(self, zid: str) -> str:
        return f"{ZONE_BASE}/{zid}/count"

    def zone_event(self, zid: str) -> str:
        return f"{ZONE_BASE}/{zid}/event"

    def zone_attributes(self, zid: str) -> str:
        return f"{ZONE_BASE}/{zid}/attributes"

    def zone_clear_delay(self, zid: str) -> str:
        return f"{ZONE_BASE}/{zid}/clear_delay"

    def zone_clear_delay_set(self, zid: str) -> str:
        return f"{ZONE_BASE}/{zid}/clear_delay/set"

    def zone_margin(self, zid: str) -> str:
        return f"{ZONE_BASE}/{zid}/margin"

    def zone_margin_set(self, zid: str) -> str:
        return f"{ZONE_BASE}/{zid}/margin/set"

    def zone_forget(self, zid: str) -> str:
        return f"{ZONE_BASE}/{zid}/forget"

    def zone_variant(self, zid: str, variant: str) -> str:
        """The occupancy of one variant (only while its request switch is ON)."""
        return f"{ZONE_BASE}/{zid}/{variant}/occupancy"

    def zone_variant_entity(self, zid: str, variant: str) -> str:
        """The state of the request switch of one variant."""
        return f"{ZONE_BASE}/{zid}/{variant}/entity"

    def zone_variant_entity_set(self, zid: str, variant: str) -> str:
        return f"{ZONE_BASE}/{zid}/{variant}/entity/set"

    # ------------------------------------------------------------ subscriptions
    def _p6_inputs(self) -> tuple:
        return (f"{ZONE_BASE}/+/clear_delay/set", f"{ZONE_BASE}/+/margin/set", f"{ZONE_BASE}/+/forget",
                f"{ZONE_BASE}/+/+/entity/set")

    def subscriptions(self) -> list:
        """The 30 of P5, then the 4 patterns of P6, all QoS 1: 34 topics."""
        return super().subscriptions() + [(topic, 1) for topic in self._p6_inputs()]

    def command_topics(self) -> tuple:
        """The 17 of P5 and the 4 patterns of P6 (the mapper matches the patterns itself)."""
        return super().command_topics() + self._p6_inputs()

    def make_mapper(self) -> "TopicMapperP6":
        return TopicMapperP6(self)


def build_topics_p6(settings) -> TopicsP6:
    base = build_topics_p5(settings)
    return TopicsP6(**{f.name: getattr(base, f.name) for f in fields(base)},
                    zone_availability=f"{ZONE_BASE}/availability")


# ---------------------------------------------------------------- payload parsers (pure)
def parse_zone_number(payload, field: str) -> Optional[int]:
    """A number payload (``clear_delay`` or ``margin``) as a decimal integer within the range of its field (``cd`` 3..900,
    ``mg`` -50..100), else ``None``. A leading ``+`` and a ``.0`` are accepted; an exponent, ``nan``, a bool or a fraction
    are not."""
    text = _text(payload)
    if text is None:
        return None
    text = text.strip()
    if not _NUMBER_RE.match(text):
        return None
    value = int(float(text))
    lo, hi = TUNE_RANGES[field]
    return value if lo <= value <= hi else None


def parse_on_off(payload) -> Optional[bool]:
    """``ON``/``OFF`` (case-insensitive) only: the request switch has no other vocabulary."""
    text = _text(payload)
    if text is None:
        return None
    text = text.strip().casefold()
    if text == "on":
        return True
    if text == "off":
        return False
    return None


def parse_press(payload) -> bool:
    text = _text(payload)
    return text is not None and text.strip().upper() == "PRESS"


def classify_zone_topic(topic: str) -> Optional[tuple]:
    """A zone command topic by its shape: ``("number", zid, field)``, ``("forget", zid, None)`` or ``("request", zid,
    variant)``; ``None`` for any other topic. The ids are not checked here (a bad id on a command topic is still a command
    topic: a retained message there is cleared)."""
    if not topic.startswith(ZONE_BASE + "/"):
        return None
    parts = topic.split("/")
    n = len(parts)
    if n == 5 and parts[4] == "set" and parts[3] in _NUMBER_FIELDS:
        return "number", parts[2], _NUMBER_FIELDS[parts[3]]
    if n == 4 and parts[3] == "forget":
        return "forget", parts[2], None
    if n == 6 and parts[4] == "entity" and parts[5] == "set":
        return "request", parts[2], parts[3]
    return None


class TopicMapperP6(TopicMapperP5):
    """The P5 mapper plus the zone command topics; everything else is handled by the P5, P4 and P3 code unchanged.

    ``known`` (set by ``P6Layer``) is ``None`` or ``(frozenset of zone ids that have entities, {zid: frozenset of the
    variant ids of the zone})``: with it a command for a zone or a variant that has no entity is dropped here, in the paho
    thread, before it reaches the queue (the layer checks again on the worker thread, where the set may have moved on).
    The attribute is replaced as a whole, never changed in place."""

    def __init__(self, topics: TopicsP6) -> None:
        super().__init__(topics)
        self._commands = frozenset(topics.command_topics())
        self.zone_cmds_ignored = 0
        self.known: Optional[tuple] = None

    def map(self, topic: str, payload: bytes, retain: bool, recv_ms: int) -> list:
        shape = classify_zone_topic(topic)
        if shape is None:
            return super().map(topic, payload, retain, recv_ms)
        if len(payload) > MAX_SMALL_PAYLOAD_BYTES and not retain:
            self.oversize += 1
            return []
        if retain:
            return [] if len(payload) == 0 else [ClearRetained(topic)]
        if len(payload) == 0 and topic in self._own_clears:
            self._own_clears.discard(topic)
            return []
        kind, zid, extra = shape
        if not _ZID_RE.fullmatch(zid):
            return self._zone_ignored()
        known = self.known
        if known is not None and zid not in known[0]:
            return self._zone_ignored()
        if kind == "number":
            value = parse_zone_number(payload, extra)
            return self._zone_ignored() if value is None else [ZoneTune(zid, extra, value, recv_ms)]
        if kind == "forget":
            return [ZoneForget(zid, recv_ms)] if parse_press(payload) else self._zone_ignored()
        if not _VID_RE.fullmatch(extra):
            return self._zone_ignored()
        if known is not None and extra not in known[1].get(zid, ()):
            return self._zone_ignored()
        on = parse_on_off(payload)
        return self._zone_ignored() if on is None else [VariantRequest(zid, extra, on, recv_ms)]

    def _zone_ignored(self) -> list:
        self.zone_cmds_ignored += 1
        return self._ignored()
