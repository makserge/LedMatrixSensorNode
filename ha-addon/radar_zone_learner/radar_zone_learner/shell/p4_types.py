"""P4 hand-off between the shell and the core, as the shell sees it.

The values themselves live in ``core/types.py`` (the core owns its interface); this module re-exports the ones
the shell uses, so the rest of the shell imports them from one place, and adds what only the shell needs.

Shell -> core (events the shell passes to ``Learner.handle``):

- ``LuxSample``              BH1750 lux from ``<node_prefix>/sensors/state`` (the night-onset proxy, OA 7.5)
- ``ContextState``           also for ``lights_off``, ``sleep_hint`` and ``guests`` (the P3 type)
- ``ReviewSelect``           the user picked an item in the review select (``None`` = nothing)
- ``ReviewAnswer``           Approve / Reject / Later on an item (item and revision are filled in by the shell)
- ``NightLearningSwitch``    the user turned 'Night learning' on or off
- ``CommandResult``          what became of a ``NodeCommand`` (echo seen, not seen, refused)

Core -> shell (outputs of ``handle``/``tick``):

- ``NodeCommand``            publish to the node's schedule / night window topics (the shell checks the echo)
- ``ReviewChanged``          the open review items and the selected one
- ``NightLearningChanged``   the Night learning state (also read from the status attribute ``night_learning``)

Shell only (never handed to the core): ``RadarStateMsg`` (``radar/state``) and ``RadarObservingMsg``
(``radar/observing``); they feed the RF, coverage and 'Radar data valid' entities.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.types import (ITEM_BED, ITEM_IDS, ITEM_NIGHT_WINDOW, NODE_COMMAND_KINDS, CommandResult, LuxSample,
                          NightLearningChanged, NightLearningSwitch, NodeCommand, ReviewAnswer, ReviewChanged,
                          ReviewItem, ReviewSelect)

__all__ = ["ITEM_BED", "ITEM_IDS", "ITEM_NIGHT_WINDOW", "NODE_COMMAND_KINDS", "CommandResult", "LuxSample",
           "NightLearningChanged", "NightLearningSwitch", "NodeCommand", "ReviewAnswer", "ReviewChanged",
           "ReviewItem", "ReviewSelect", "SELECT_NONE", "SELECT_LABELS", "SELECT_OPTIONS", "ANSWER_OPS",
           "EVENT_TYPES", "RADAR_ON_STATES", "RADAR_OFF_STATES", "RADAR_STATES", "RadarStateMsg",
           "RadarObservingMsg"]

# the review select (fixed options before P5)
SELECT_NONE = "None"
SELECT_LABELS = {ITEM_NIGHT_WINDOW: "Night window", ITEM_BED: "Bed zone"}
SELECT_OPTIONS = (SELECT_NONE, SELECT_LABELS[ITEM_NIGHT_WINDOW], SELECT_LABELS[ITEM_BED])

# the answers the review buttons send (the core knows more: rename, undo, forget); reject on the bed item is
# [No bed in view] (OA 7.6)
ANSWER_OPS = ("approve", "reject", "later")
EVENT_TYPES = ("mark_failed", "device_not_confirmed", "restore_failed")

# 'Radar state' values of the node (OA 6.1 #8)
RADAR_ON_STATES = ("on", "forced_on", "failsafe_on")
RADAR_OFF_STATES = ("night_off", "forced_off")
RADAR_STATES = RADAR_ON_STATES + RADAR_OFF_STATES


@dataclass(frozen=True, slots=True)
class RadarStateMsg:
    state: str                     # one of RADAR_STATES
    recv_ms: int


@dataclass(frozen=True, slots=True)
class RadarObservingMsg:
    observing: bool
    recv_ms: int
