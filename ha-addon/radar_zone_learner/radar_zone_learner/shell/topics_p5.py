"""P5 topics and the mapping of their messages to events (P5 spec 3.1, 6.4, 8.1, 8.4).

``build_topics_p5`` extends the P4 topics; ``TopicsP5.make_mapper`` makes the mapper that knows them. The P4
``build_topics_p4``/``TopicMapperP4`` are untouched, so a P4 setup subscribes to the same 18 topics as before.

Inputs added here (12, all commands, so a retained message on one is ignored and deleted):

- ``radar_zones/review/advanced/set``       the Advanced select's label            -> ``AdvancedSelectLabel`` (shell)
- ``radar_zones/review/live/set``           ON / OFF, 'Show me live'              -> ``LiveSwitch`` (shell)
- ``radar_zones/review/nw_start/set``, ``.../nw_end/set``  ``HH:MM:SS``           -> ``NightTimeEdit``
- ``radar_zones/review/approve_sched/set``, ``radar_zones/learner/{guests_pause,pets,detail_text}/set``
                                                                                    -> ``SettingSwitch``
- ``radar_zones/review/push_ack``           ``<seq>:<target>`` or ``<seq>``       -> ``PushAck``
- ``radar_zones/learner/debug_capture/set`` ON / OFF                              -> ``CaptureSwitch`` (shell)
- ``radar_zones/learner/forget_confirm/set`` the confirmation text                 -> ``ForgetConfirmText`` (shell)
- ``radar_zones/learner/forget_all``        ``PRESS``                             -> ``ForgetPress`` (shell)

and two P4 topics are mapped differently: ``radar_zones/review/select/set`` (a label of the dynamic select, spec 2.6) and
``radar_zones/review/answer`` (all ops, ``name`` and ``aid``, up to 512 bytes: a 32-character name with escaped
non-ASCII characters alone can exceed the 256 bytes of every other command topic, spec D22).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from typing import Optional

from ..core.types import ADV_ID_RE, ANSWER_OPS, ITEM_ID_RE, PUSH_TARGET_RE, SETTING_NAMES
from .p4_types import ITEM_BED, ITEM_NIGHT_WINDOW, ReviewAnswer, ReviewSelect
from .p5_types import (AdvancedSelectLabel, CaptureSwitch, ForgetConfirmText, ForgetPress, LiveSwitch, NightTimeEdit,
                       PushAck, ReviewSelectLabel, SettingSwitch)
from .topics import (MAX_SMALL_PAYLOAD_BYTES, ClearRetained, TopicMapper, _text, parse_hhmm, parse_schedule)
from .topics_p4 import TopicMapperP4, TopicsP4, build_topics_p4

ANSWER_MAX_BYTES = 512                   # spec D22: only the answer topic
NAME_MAX = 32                            # the name of a zone (core NAME_MAX)
LABEL_MAX = 64                           # an option label received from a select
FORGET_TEXT_MAX = 16                     # the 'Forget all confirmation' text (spec 7.2)
PUSH_SEQ_MAX_DIGITS = 10
_ITEM_RE = re.compile(ITEM_ID_RE)
_ADV_RE = re.compile(ADV_ID_RE)
_TARGET_RE = re.compile(PUSH_TARGET_RE)
_AID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SEQ_RE = re.compile(r"^[0-9]{1,%d}$" % PUSH_SEQ_MAX_DIGITS)
_SETTING_TOPIC_FIELDS = {"approve_sched": "review_approve_sched_set", "guests_pause": "learner_guests_pause_set",
                         "pets": "learner_pets_set", "detail_text": "learner_detail_text_set"}
assert tuple(_SETTING_TOPIC_FIELDS) == SETTING_NAMES


@dataclass(frozen=True)
class TopicsP5(TopicsP4):
    # outputs
    review_map: str
    review_push_map: str
    review_advanced_state: str
    review_live_state: str
    review_you_are_in: str
    review_you_are_in_attr: str
    review_nw_start: str
    review_nw_end: str
    review_approve_sched: str
    review_push: str
    learner_guests_pause: str
    learner_pets: str
    learner_detail_text: str
    learner_debug_capture: str
    learner_forget_confirm: str
    # inputs (commands)
    review_advanced_set: str
    review_live_set: str
    review_nw_start_set: str
    review_nw_end_set: str
    review_approve_sched_set: str
    review_push_ack: str
    learner_guests_pause_set: str
    learner_pets_set: str
    learner_detail_text_set: str
    learner_debug_capture_set: str
    learner_forget_confirm_set: str
    learner_forget_all: str

    def _p5_inputs(self) -> tuple:
        return (self.review_advanced_set, self.review_live_set, self.review_nw_start_set, self.review_nw_end_set,
                self.review_approve_sched_set, self.review_push_ack, self.learner_guests_pause_set,
                self.learner_pets_set, self.learner_detail_text_set, self.learner_debug_capture_set,
                self.learner_forget_confirm_set, self.learner_forget_all)

    def subscriptions(self) -> list:
        """The 18 of P4, then the 12 inputs of P5, all QoS 1: 30 topics."""
        return super().subscriptions() + [(topic, 1) for topic in self._p5_inputs()]

    def command_topics(self) -> tuple:
        """The P4 five and the twelve inputs of P5: a retained message on one is ignored and deleted (17)."""
        return super().command_topics() + self._p5_inputs()

    def make_mapper(self) -> "TopicMapperP5":
        return TopicMapperP5(self)


def build_topics_p5(settings) -> TopicsP5:
    base = build_topics_p4(settings)
    return TopicsP5(
        **{f.name: getattr(base, f.name) for f in fields(base)},
        review_map="radar_zones/review/map",
        review_push_map="radar_zones/review/push_map",
        review_advanced_state="radar_zones/review/advanced",
        review_live_state="radar_zones/review/live",
        review_you_are_in="radar_zones/review/you_are_in",
        review_you_are_in_attr="radar_zones/review/you_are_in/attr",
        review_nw_start="radar_zones/review/nw_start",
        review_nw_end="radar_zones/review/nw_end",
        review_approve_sched="radar_zones/review/approve_sched",
        review_push="radar_zones/review/push",
        learner_guests_pause="radar_zones/learner/guests_pause",
        learner_pets="radar_zones/learner/pets",
        learner_detail_text="radar_zones/learner/detail_text",
        learner_debug_capture="radar_zones/learner/debug_capture",
        learner_forget_confirm="radar_zones/learner/forget_confirm",
        review_advanced_set="radar_zones/review/advanced/set",
        review_live_set="radar_zones/review/live/set",
        review_nw_start_set="radar_zones/review/nw_start/set",
        review_nw_end_set="radar_zones/review/nw_end/set",
        review_approve_sched_set="radar_zones/review/approve_sched/set",
        review_push_ack="radar_zones/review/push_ack",
        learner_guests_pause_set="radar_zones/learner/guests_pause/set",
        learner_pets_set="radar_zones/learner/pets/set",
        learner_detail_text_set="radar_zones/learner/detail_text/set",
        learner_debug_capture_set="radar_zones/learner/debug_capture/set",
        learner_forget_confirm_set="radar_zones/learner/forget_confirm/set",
        learner_forget_all="radar_zones/learner/forget_all",
    )


# ---------------------------------------------------------------- payload parsers (pure)
def parse_select_p5(payload):
    """The 'Review item' select's text -> ``("item", id or None)`` for a static label, ``No item``/``None``/empty or an item
    id; ``("label", text)`` for any other text of 1 to 64 characters (a label of the dynamic select, resolved by the
    layer); ``None`` for the rest (not UTF-8, too long)."""
    text = _text(payload)
    if text is None:
        return None
    text = text.strip()
    cf = text.casefold()
    if cf in ("", "none", "no item"):
        return "item", None
    if cf in ("night window", ITEM_NIGHT_WINDOW):
        return "item", ITEM_NIGHT_WINDOW
    if cf in ("bed zone", ITEM_BED):
        return "item", ITEM_BED
    if _ITEM_RE.match(text):
        return "item", text
    if 1 <= len(text) <= LABEL_MAX:
        return "label", text
    return None


def parse_answer_p5(payload) -> Optional[dict]:
    """JSON ``{"op", "item"?, "rev"?, "sched"?, "name"?, "aid"?}`` -> a dict with exactly these keys, or ``None`` when it
    is refused (P5 spec 3.1).

    ``op`` is trimmed, case-insensitive, an optional ``rz_`` prefix is stripped, and is one of the seven core ops;
    ``item`` is an item id or an Advanced entry id; ``rev`` an int (not a bool); ``sched`` a bool; ``name`` a string of 1
    to 32 characters after stripping and cutting with no character below U+0020 (an int, float or bool is converted
    with ``str()`` first: a template in Home Assistant turns a typed ``2`` into a number); ``aid`` ``[A-Za-z0-9_.-]``, at
    most 64 characters. Unknown keys are ignored."""
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
    if item is not None and (not isinstance(item, str) or not (_ITEM_RE.match(item) or _ADV_RE.match(item))):
        return None
    rev = obj.get("rev")
    if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int)):
        return None
    sched = obj.get("sched", False)
    if not isinstance(sched, bool):
        return None
    name = obj.get("name")
    if name is not None:
        if isinstance(name, (bool, int, float)):
            name = str(name)
        if not isinstance(name, str):
            return None
        name = name.strip()[:NAME_MAX].strip()
        if not name or any(ord(ch) < 0x20 for ch in name):
            return None
    aid = obj.get("aid")
    if aid is not None and (not isinstance(aid, str) or not _AID_RE.match(aid)):
        return None
    return {"op": op, "item": item, "rev": rev, "sched": sched, "name": name, "aid": aid}


def parse_push_ack(payload) -> Optional[tuple]:
    """``<seq>:<target>`` or a plain ``<seq>`` -> ``(seq, target)`` (target ``""`` when absent), else ``None``."""
    text = _text(payload)
    if text is None:
        return None
    text = text.strip()
    seq_text, sep, target = text.partition(":")
    if not _SEQ_RE.match(seq_text):
        return None
    if sep and not _TARGET_RE.match(target):
        return None
    return int(seq_text), target if sep else ""


def parse_forget_confirm(payload) -> Optional[str]:
    """The confirmation text, cut to 16 characters; ``None`` when the payload is not UTF-8. Not stripped: the layer
    echoes it as typed."""
    text = _text(payload)
    if text is None:
        return None
    return text[:FORGET_TEXT_MAX]


class TopicMapperP5(TopicMapperP4):
    """The P4 mapper plus the P5 topics; everything else is handled by the P4 and P3 code unchanged."""

    def __init__(self, topics: TopicsP5) -> None:
        super().__init__(topics)
        self._commands = frozenset(topics.command_topics())
        self._handlers.update({
            topics.review_select_set: self._select5,
            topics.review_answer: self._answer5,
            topics.review_advanced_set: self._advanced,
            topics.review_live_set: self._live,
            topics.review_nw_start_set: lambda p, ms: self._time_edit("start", p, ms),
            topics.review_nw_end_set: lambda p, ms: self._time_edit("end", p, ms),
            topics.review_push_ack: self._push_ack,
            topics.learner_debug_capture_set: self._capture,
            topics.learner_forget_confirm_set: self._forget_confirm,
            topics.learner_forget_all: self._forget_all,
        })
        for name, field in _SETTING_TOPIC_FIELDS.items():
            self._handlers[getattr(topics, field)] = (lambda p, ms, n=name: self._setting(n, p, ms))

    def map(self, topic: str, payload: bytes, retain: bool, recv_ms: int) -> list:
        handler = self._handlers.get(topic)
        if handler is None:
            return TopicMapper.map(self, topic, payload, retain, recv_ms)
        is_command = topic in self._commands
        cap = ANSWER_MAX_BYTES if topic == self.topics.review_answer else MAX_SMALL_PAYLOAD_BYTES
        if len(payload) > cap and not (retain and is_command):
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
    def _select5(self, payload, recv_ms):
        parsed = parse_select_p5(payload)
        if parsed is None:
            return self._ignored()
        kind, value = parsed
        return [ReviewSelect(value, recv_ms)] if kind == "item" else [ReviewSelectLabel(value, recv_ms)]

    def _answer5(self, payload, recv_ms):
        ans = parse_answer_p5(payload)
        if ans is None:
            return self._ignored()
        return [ReviewAnswer(ans["item"], ans["op"], ans["rev"], ans["sched"], recv_ms, name=ans["name"],
                             aid=ans["aid"])]

    def _advanced(self, payload, recv_ms):
        text = _text(payload)
        text = text.strip() if text is not None else ""
        if not 1 <= len(text) <= LABEL_MAX:
            return self._ignored()
        return [AdvancedSelectLabel(text, recv_ms)]

    def _live(self, payload, recv_ms):
        value = parse_schedule(payload)
        return self._ignored() if value is None else [LiveSwitch(value, recv_ms)]

    def _time_edit(self, field: str, payload, recv_ms):
        minute = parse_hhmm(payload)
        return self._ignored() if minute is None else [NightTimeEdit(field, minute, recv_ms)]

    def _setting(self, name: str, payload, recv_ms):
        value = parse_schedule(payload)
        return self._ignored() if value is None else [SettingSwitch(name, value, recv_ms)]

    def _push_ack(self, payload, recv_ms):
        parsed = parse_push_ack(payload)
        if parsed is None:
            return self._ignored()
        return [PushAck(parsed[0], recv_ms, parsed[1])]

    def _capture(self, payload, recv_ms):
        value = parse_schedule(payload)
        return self._ignored() if value is None else [CaptureSwitch(value, recv_ms)]

    def _forget_confirm(self, payload, recv_ms):
        text = parse_forget_confirm(payload)
        return self._ignored() if text is None else [ForgetConfirmText(text, recv_ms)]

    def _forget_all(self, payload, recv_ms):
        text = _text(payload)
        if text is None or text.strip().upper() != "PRESS":
            return self._ignored()
        return [ForgetPress(recv_ms)]
