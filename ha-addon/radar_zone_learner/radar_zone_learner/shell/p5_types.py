"""P5 hand-off between the shell and the core, as the shell sees it (P5 spec Appendix B.1).

The values themselves live in ``core/types.py`` (the core owns its interface); this module re-exports the new ones the
shell uses, so the rest of the P5 shell imports them from one place, and adds the events that only the shell knows.

Shell -> core (events the shell passes to ``Learner.handle``), new in P5:

- ``NightTimeEdit``     the two time entities 'Night window start/end (edit)'
- ``SettingSwitch``     the four persistent switches (``approve_sched``, ``guests_pause``, ``pets``, ``detail_text``)
- ``PushAck``           the blueprint's acknowledgement of a push request
- ``ReviewAnswer``      now with ``name`` and ``aid`` (core type, not re-exported here: ``shell.p4_types`` does)

Core -> shell (outputs of ``handle``/``tick``), new in P5:

- ``AnswerIgnored``     why an answer changed nothing (turned into the review event ``answer_ignored``)
- ``AdvancedChanged``   the entries of the 'Advanced' select
- ``PushRequest``       the push request the blueprint turns into a notification (no text field)

Read-only views the layer pulls through ``Learner`` accessors: ``ZoneView``, ``AdvancedEntry``, ``WhereView``.

Shell only (never handed to the core): ``ReviewSelectLabel``, ``AdvancedSelectLabel``, ``LiveSwitch``,
``ForgetConfirmText``, ``ForgetPress`` and ``CaptureSwitch``.

Do not confuse ``shell.p4_types.ANSWER_OPS`` (three ops, the P4 buttons) with the core's ``ANSWER_OPS`` (seven): the P5
parser uses the core's list.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.types import (ADV_ID_RE, ANSWER_IGNORED_REASONS, ITEM_ID_RE, PUSH_KINDS, PUSH_TARGET_RE, SETTING_NAMES,
                          AdvancedChanged, AdvancedEntry, AnswerIgnored, NightTimeEdit, PushAck, PushRequest,
                          SettingSwitch, WhereView, ZoneView)

__all__ = ["ADV_ID_RE", "ANSWER_IGNORED_REASONS", "ITEM_ID_RE", "PUSH_KINDS", "PUSH_TARGET_RE", "SETTING_NAMES",
           "AdvancedChanged", "AdvancedEntry", "AnswerIgnored", "NightTimeEdit", "PushAck", "PushRequest",
           "SettingSwitch", "WhereView", "ZoneView", "ReviewSelectLabel", "AdvancedSelectLabel", "LiveSwitch",
           "ForgetConfirmText", "ForgetPress", "CaptureSwitch"]


@dataclass(frozen=True, slots=True)
class ReviewSelectLabel:
    text: str                      # a select label that is not a static one (spec 8.4); 1 to 64 characters
    recv_ms: int


@dataclass(frozen=True, slots=True)
class AdvancedSelectLabel:
    text: str                      # an option label of the Advanced select (``radar_zones/review/advanced/set``)
    recv_ms: int


@dataclass(frozen=True, slots=True)
class LiveSwitch:
    on: bool                       # 'Show me live' (``radar_zones/review/live/set``)
    recv_ms: int


@dataclass(frozen=True, slots=True)
class ForgetConfirmText:
    text: str                      # the confirmation text of 'Forget all learned data'; at most 16 characters kept
    recv_ms: int


@dataclass(frozen=True, slots=True)
class ForgetPress:
    recv_ms: int                   # the button 'Forget all learned data' (``radar_zones/learner/forget_all``)


@dataclass(frozen=True, slots=True)
class CaptureSwitch:
    on: bool                       # the 'Debug capture' switch (spec 7.8)
    recv_ms: int
