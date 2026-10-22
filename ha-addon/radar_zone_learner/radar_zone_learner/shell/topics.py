"""Topic names (spec 9.1) and the pure mapping of received messages to core input events (spec 6.1).

Nothing here does I/O or imports paho, so ``replay/capture_replay.py`` may use ``TopicMapper`` to turn a
captured message log into the same events the app produces.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..core.frames import MAX_PAYLOAD_BYTES
from ..core.types import ContextState, FramesMessage, MarkPress, NodeStatus, ScheduleState, SpotName

SPOT_NAME_MAX = 32          # spec 7.10 spot_name_max
MAX_SMALL_PAYLOAD_BYTES = 256   # every topic but the frames: a name is 32 characters, the rest are a few bytes
HA_BIRTH_TOPIC = "homeassistant/status"     # Home Assistant's default birth message topic; it does not follow the
                                            # discovery prefix (the two are separate settings of HA's MQTT)
HHMM_RE = re.compile(r"^([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?$")


@dataclass(frozen=True)
class Topics:
    # inputs (spec 6.1)
    frames: str
    node_status: str
    schedule_state: str
    night_start_state: str
    night_end_state: str
    context_home_empty: str
    review_mark: str
    review_name_set: str
    ha_status: str
    # outputs (spec 9.1)
    learner_status: str
    learner_state: str
    learner_attr: str
    learner_debug_map: str
    review_name: str
    discovery_prefix: str

    def discovery(self, component: str, object_id: str) -> str:
        return f"{self.discovery_prefix}/{component}/{object_id}/config"

    def subscriptions(self) -> list:
        """(topic, qos) pairs exactly as spec 6.1."""
        return [
            (self.frames, 0),
            (self.node_status, 1),
            (self.schedule_state, 1),
            (self.night_start_state, 1),
            (self.night_end_state, 1),
            (self.context_home_empty, 1),
            (self.review_mark, 1),
            (self.review_name_set, 1),
            (self.ha_status, 1),
        ]


def build_topics(settings) -> Topics:
    """Every topic of spec 9.1 from ``settings.node_prefix`` and ``settings.discovery_prefix``."""
    node = settings.node_prefix
    disc = settings.discovery_prefix
    return Topics(
        frames=f"{node}/radar/ld2454/frames",
        node_status=f"{node}/sensors/status",
        schedule_state=f"{node}/radar/schedule/state",
        night_start_state=f"{node}/radar/night_start/state",
        night_end_state=f"{node}/radar/night_end/state",
        context_home_empty="radar_zones/context/home_empty",
        review_mark="radar_zones/review/mark",
        review_name_set="radar_zones/review/name/set",
        ha_status=HA_BIRTH_TOPIC,
        learner_status="radar_zones/learner/status",
        learner_state="radar_zones/learner/state",
        learner_attr="radar_zones/learner/attr",
        learner_debug_map="radar_zones/learner/debug_map",
        review_name="radar_zones/review/name",
        discovery_prefix=disc,
    )


# ---------------------------------------------------------------- shell-internal markers and actions
@dataclass(frozen=True)
class Republish:
    """Publish discovery, availability, state, attributes, the name echo and the debug map now."""


@dataclass(frozen=True)
class HaBirth:
    """Home Assistant sent 'online' on <discovery_prefix>/status: republish after 0-10 s."""
    recv_ms: int


@dataclass(frozen=True)
class ClearRetained:
    """A retained message on one of the app's command topics: ignore it and delete it (D18)."""
    topic: str


# ---------------------------------------------------------------- payload parsers (pure)
def _text(payload) -> Optional[str]:
    if isinstance(payload, str):
        return payload
    try:
        return bytes(payload).decode("utf-8")
    except (UnicodeDecodeError, TypeError, ValueError):
        return None


def parse_online(payload) -> Optional[bool]:
    """'online' -> True, 'offline' -> False (case-insensitive), anything else None."""
    text = _text(payload)
    if text is None:
        return None
    text = text.strip().casefold()
    if text == "online":
        return True
    if text == "offline":
        return False
    return None


def parse_schedule(payload) -> Optional[bool]:
    """'1'/'ON' -> True, '0'/'OFF' -> False (case-insensitive), anything else None."""
    text = _text(payload)
    if text is None:
        return None
    text = text.strip().casefold()
    if text in ("1", "on"):
        return True
    if text in ("0", "off"):
        return False
    return None


def parse_hhmm(payload) -> Optional[int]:
    """'HH:MM:SS' or 'HH:MM' -> minute of the day 0..1439 (seconds are dropped), else None."""
    text = _text(payload)
    if text is None:
        return None
    m = HHMM_RE.match(text.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    ss = int(m.group(3)) if m.group(3) is not None else 0
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return hh * 60 + mm


def parse_context(payload) -> Optional[bool]:
    """on/off, true/false, 1/0 (case-insensitive); empty or anything else -> None (cleared/unparseable)."""
    text = _text(payload)
    if text is None:
        return None
    text = text.strip().casefold()
    if text in ("on", "true", "1"):
        return True
    if text in ("off", "false", "0"):
        return False
    return None


def parse_spot_name(payload) -> Optional[str]:
    """UTF-8, stripped, cut to 32 characters; None when the payload is not UTF-8."""
    text = _text(payload)
    if text is None:
        return None
    return text.strip()[:SPOT_NAME_MAX].strip()


class TopicMapper:
    """Maps one received message to a list of items (spec 6.1): core events, ``HaBirth`` or ``ClearRetained``.

    Stateful only for the schedule (the last of the three schedule values, sent together) and for the
    empty messages that the app's own retained-clear publishes produce on its command topics.
    """

    def __init__(self, topics: Topics) -> None:
        self.topics = topics
        self.sched: Optional[bool] = None
        self.start_min: Optional[int] = None
        self.end_min: Optional[int] = None
        self.ignored = 0
        self.oversize = 0                 # messages dropped for their size, before anything was copied
        self._own_clears: set = set()

    def note_cleared(self, topic: str) -> None:
        """The app published an empty retained message to ``topic``; its echo is not a command."""
        self._own_clears.add(topic)

    def map(self, topic: str, payload: bytes, retain: bool, recv_ms: int) -> list:
        t = self.topics
        # The broker has no ACLs and MQTT allows 256 MiB: a payload that is larger than any real one of its
        # topic is dropped and counted here, in the paho thread, before it is copied or queued. A retained
        # message on a command topic is still cleared (D18), whatever its size.
        n = len(payload)
        if n > (MAX_PAYLOAD_BYTES if topic == t.frames else MAX_SMALL_PAYLOAD_BYTES) and \
                not (retain and topic in (t.review_mark, t.review_name_set)):
            self.oversize += 1
            return []
        if topic == t.frames:
            return [FramesMessage(bytes(payload), recv_ms)]
        if topic == t.node_status:
            online = parse_online(payload)
            return self._ignored() if online is None else [NodeStatus(online, recv_ms)]
        if topic in (t.schedule_state, t.night_start_state, t.night_end_state):
            if topic == t.schedule_state:
                value = parse_schedule(payload)
                if value is None:
                    return self._ignored()
                self.sched = value
            else:
                value = parse_hhmm(payload)
                if value is None:
                    return self._ignored()
                if topic == t.night_start_state:
                    self.start_min = value
                else:
                    self.end_min = value
            return [ScheduleState(self.sched, self.start_min, self.end_min, recv_ms)]
        if topic == t.context_home_empty:
            return [ContextState("home_empty", parse_context(payload), recv_ms)]
        if topic in (t.review_mark, t.review_name_set):
            if retain:
                if len(payload) == 0:
                    return []          # a retained empty message is already a deletion
                return [ClearRetained(topic)]
            if len(payload) == 0 and topic in self._own_clears:
                self._own_clears.discard(topic)
                return []
            if topic == t.review_mark:
                text = _text(payload)
                if text is None or text.strip().upper() != "PRESS":
                    return self._ignored()
                return [MarkPress(recv_ms)]
            name = parse_spot_name(payload)
            return self._ignored() if name is None else [SpotName(name, recv_ms)]
        if topic == t.ha_status:
            return [HaBirth(recv_ms)] if parse_online(payload) is True else []
        return self._ignored()

    def _ignored(self) -> list:
        self.ignored += 1
        return []
