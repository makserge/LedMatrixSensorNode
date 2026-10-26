"""P4 topics and the mapping of their messages to events (design 5.6, 6.1-6.3, 7.5, 7.6).

``build_topics_p4`` extends the P3 topics; ``TopicsP4.make_mapper`` makes the mapper that knows them. The P3
``build_topics``/``TopicMapper`` are untouched, so a P3 setup subscribes to the same 9 topics as before.

Inputs added here:

- ``<node>/sensors/state``      JSON with the BH1750 ``lux`` -> ``LuxSample`` (night-onset proxy)
- ``<node>/radar/state``        -> ``RadarStateMsg`` (RF sensors; shell only)
- ``<node>/radar/observing``    ``ON``/``OFF`` -> ``RadarObservingMsg`` (radar data valid; shell only)
- ``radar_zones/context/{lights_off,sleep_hint,guests}`` -> ``ContextState`` (the blueprint's retained topics)
- ``radar_zones/learner/night_learning/set``   -> ``NightLearningSwitch``
- ``radar_zones/review/select/set``            -> ``ReviewSelect``
- ``radar_zones/review/answer``                -> ``ReviewAnswer``

A retained message on one of the app's own command topics is ignored and deleted, as in P3 (D18).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields

from .p4_types import (ANSWER_OPS, ITEM_IDS, RADAR_STATES, SELECT_LABELS, SELECT_NONE, LuxSample,
                       NightLearningSwitch, RadarObservingMsg, RadarStateMsg, ReviewAnswer, ReviewSelect)
from .topics import (MAX_SMALL_PAYLOAD_BYTES, ClearRetained, TopicMapper, Topics, _text, build_topics,
                     parse_context, parse_schedule)
from ..core.types import ContextState

LUX_MAX = 200000.0


@dataclass(frozen=True)
class TopicsP4(Topics):
    # inputs from the node (P2 firmware for the radar topics; sensors/state exists since P1)
    sensors_state: str
    radar_state: str
    radar_observing: str
    # commands to the node (design 5.6 (b), 6.1 #2-#4); the echo is read from the *_state topics of Topics
    radar_schedule_cmd: str
    radar_night_start_cmd: str
    radar_night_end_cmd: str
    # inputs from Home Assistant (blueprint, retained) and from the user
    context_lights_off: str
    context_sleep_hint: str
    context_guests: str
    night_learning_set: str
    review_select_set: str
    review_answer: str
    # outputs
    night_learning_state: str
    review_select_state: str
    review_pending: str
    review_pending_attr: str
    review_item_text: str
    review_event: str
    radar_valid: str
    rf_on_24h: str
    rf_saving_7d: str
    coverage_7d: str

    def subscriptions(self) -> list:
        return super().subscriptions() + [
            (self.sensors_state, 1),
            (self.radar_state, 1),
            (self.radar_observing, 1),
            (self.context_lights_off, 1),
            (self.context_sleep_hint, 1),
            (self.context_guests, 1),
            (self.night_learning_set, 1),
            (self.review_select_set, 1),
            (self.review_answer, 1),
        ]

    def command_topics(self) -> tuple:
        """The app's own command topics: a retained message on one is ignored and deleted."""
        return (self.review_mark, self.review_name_set, self.night_learning_set, self.review_select_set,
                self.review_answer)

    def make_mapper(self) -> "TopicMapperP4":
        return TopicMapperP4(self)


def build_topics_p4(settings) -> TopicsP4:
    base = build_topics(settings)
    node = settings.node_prefix
    return TopicsP4(
        **{f.name: getattr(base, f.name) for f in fields(base)},
        sensors_state=f"{node}/sensors/state",
        radar_state=f"{node}/radar/state",
        radar_observing=f"{node}/radar/observing",
        radar_schedule_cmd=f"{node}/radar/schedule",
        radar_night_start_cmd=f"{node}/radar/night_start",
        radar_night_end_cmd=f"{node}/radar/night_end",
        context_lights_off="radar_zones/context/lights_off",
        context_sleep_hint="radar_zones/context/sleep_hint",
        context_guests="radar_zones/context/guests",
        night_learning_set="radar_zones/learner/night_learning/set",
        review_select_set="radar_zones/review/select/set",
        review_answer="radar_zones/review/answer",
        night_learning_state="radar_zones/learner/night_learning",
        review_select_state="radar_zones/review/select",
        review_pending="radar_zones/review/pending",
        review_pending_attr="radar_zones/review/pending/attr",
        review_item_text="radar_zones/review/item_text",
        review_event="radar_zones/review/event",
        radar_valid="radar_zones/radar/valid",
        rf_on_24h="radar_zones/scan/rf_on_24h",
        rf_saving_7d="radar_zones/scan/rf_saving_7d",
        coverage_7d="radar_zones/learner/coverage_7d",
    )


# ---------------------------------------------------------------- payload parsers (pure)
def parse_lux(payload):
    """``{"co2":..,"lux":12.5}`` -> 12.5; no ``lux`` key -> None (a sensor without a fresh value leaves it out);
    anything unusable -> False (counted as ignored)."""
    text = _text(payload)
    if text is None:
        return False
    try:
        obj = json.loads(text)
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    if "lux" not in obj:
        return None
    lux = obj["lux"]
    if isinstance(lux, bool) or not isinstance(lux, (int, float)) or not math.isfinite(lux):
        return False
    if not 0.0 <= lux <= LUX_MAX:
        return False
    return float(lux)


def parse_radar_state(payload):
    text = _text(payload)
    if text is None:
        return None
    text = text.strip().casefold()
    return text if text in RADAR_STATES else None


def parse_select(payload):
    """The select's option text or an item id -> (True, item id or None); anything else (False, None)."""
    text = _text(payload)
    if text is None:
        return False, None
    text = text.strip()
    if text == "" or text.casefold() == SELECT_NONE.casefold():
        return True, None
    for item, label in SELECT_LABELS.items():
        if text.casefold() in (label.casefold(), item):
            return True, item
    return False, None


def parse_answer(payload):
    """JSON ``{"op": "approve"|"reject"|"later", "item"?: str, "rev"?: int, "sched"?: bool}`` -> dict or None.

    ``op`` may also be written ``RZ_APPROVE`` / ``RZ_REJECT`` (the push-action names, OA 7.9).
    """
    text = _text(payload)
    if text is None:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    op = obj.get("op")
    if not isinstance(op, str):
        return None
    op = op.strip().casefold()
    if op.startswith("rz_"):
        op = op[3:]
    if op not in ANSWER_OPS:
        return None
    item = obj.get("item")
    if item is not None and (not isinstance(item, str) or item not in ITEM_IDS):
        return None
    rev = obj.get("rev")
    if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int)):
        return None
    sched = obj.get("sched", False)
    if not isinstance(sched, bool):
        return None
    return {"op": op, "item": item, "rev": rev, "sched": sched}


class TopicMapperP4(TopicMapper):
    """The P3 mapper plus the P4 topics; everything else is handled by the P3 code unchanged."""

    def __init__(self, topics: TopicsP4) -> None:
        super().__init__(topics)
        self._commands = frozenset(topics.command_topics())
        self._handlers = {
            topics.sensors_state: self._sensors_state,
            topics.radar_state: self._radar_state,
            topics.radar_observing: self._observing,
            topics.context_lights_off: lambda p, ms: [ContextState("lights_off", parse_context(p), ms)],
            topics.context_sleep_hint: lambda p, ms: [ContextState("sleep_hint", parse_context(p), ms)],
            topics.context_guests: lambda p, ms: [ContextState("guests", parse_context(p), ms)],
            topics.night_learning_set: self._night_learning,
            topics.review_select_set: self._select,
            topics.review_answer: self._answer,
        }

    def map(self, topic: str, payload: bytes, retain: bool, recv_ms: int) -> list:
        handler = self._handlers.get(topic)
        if handler is None:
            return super().map(topic, payload, retain, recv_ms)
        is_command = topic in self._commands
        if len(payload) > MAX_SMALL_PAYLOAD_BYTES and not (retain and is_command):
            self.oversize += 1
            return []
        if is_command:
            if retain:
                return [] if len(payload) == 0 else [ClearRetained(topic)]
            if len(payload) == 0 and topic in self._own_clears:
                self._own_clears.discard(topic)
                return []
        return handler(payload, recv_ms)

    # ------------------------------------------------------------ handlers
    def _sensors_state(self, payload, recv_ms):
        lux = parse_lux(payload)
        if lux is False:
            return self._ignored()
        return [] if lux is None else [LuxSample(lux, recv_ms)]

    def _radar_state(self, payload, recv_ms):
        state = parse_radar_state(payload)
        return self._ignored() if state is None else [RadarStateMsg(state, recv_ms)]

    def _observing(self, payload, recv_ms):
        value = parse_schedule(payload)           # ON/OFF/1/0
        return self._ignored() if value is None else [RadarObservingMsg(value, recv_ms)]

    def _night_learning(self, payload, recv_ms):
        value = parse_schedule(payload)
        return self._ignored() if value is None else [NightLearningSwitch(value, recv_ms)]

    def _select(self, payload, recv_ms):
        ok, item = parse_select(payload)
        return [ReviewSelect(item, recv_ms)] if ok else self._ignored()

    def _answer(self, payload, recv_ms):
        ans = parse_answer(payload)
        if ans is None:
            return self._ignored()
        return [ReviewAnswer(ans["item"], ans["op"], ans["rev"], ans["sched"], recv_ms)]
