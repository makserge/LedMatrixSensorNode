"""P6 hand-off between the shell and the core, as the shell sees it (P6 spec Appendix B.1).

The values themselves live in ``core/types.py`` (the core owns its interface); this module re-exports the ones the P6 shell
uses, so the rest of the P6 shell imports them from one place, and adds the one value that only the shell knows.

Shell -> core (events the shell passes to ``Learner.handle``), new in P6:

- ``ZoneTune``         the tuning numbers of a zone: ``field`` ``cd`` (clear delay, 3..900 s) or ``mg`` (margin, -50..100 cm)
- ``ZoneForget``       the 'Forget learned data' button of one zone (the evidence and the tuning, not the zone)
- ``VariantRequest``   the switch that asks for (or drops) the occupancy entity of one time variant of a zone

Core -> shell (outputs of ``handle``/``tick``), new in P6:

- ``ZoneEvent``        a real enter or leave of one zone (no zone name, no coordinate)
- ``ZoneStates``       the state of every evaluated zone with the radars-valid flag; emitted only when it changes

Read-only views the layer pulls through ``Learner`` accessors: ``ZoneView`` (P5), ``Learner.zone_states()`` (its zone states
carry ``typical_hours`` and ``confidence`` for the attributes) and the installation epoch of the entity ids
(``Learner.zone6.export()["set"]["epoch"]``).

Shell only: ``ZoneEntry``, one zone that has entities (what the discovery configs and the publisher are built from).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.types import (VARIANT_ID_RE, ZONE_EVENT_KINDS, ZONE_EVENT_VIA, ZONE_ID_RE, ZONE_KINDS_EVALUATED,
                          ZONE_TUNE_FIELDS, VariantRequest, ZoneEvent, ZoneForget, ZoneState, ZoneStates, ZoneTune)

__all__ = ["VARIANT_ID_RE", "ZONE_EVENT_KINDS", "ZONE_EVENT_VIA", "ZONE_ID_RE", "ZONE_KINDS_EVALUATED",
           "ZONE_TUNE_FIELDS", "VariantRequest", "ZoneEvent", "ZoneForget", "ZoneState", "ZoneStates", "ZoneTune",
           "ZoneEntry", "TUNE_RANGES"]

# the ranges of the two tuning numbers (CD:entities/Zone tuning): the same as the number entities' min and max
TUNE_RANGES = {"cd": (3, 900), "mg": (-50, 100)}


@dataclass(frozen=True, slots=True)
class ZoneEntry:
    """One zone that has entities: its id, the name of its device, the variant ids it has (one request switch each) and the
    variant ids whose occupancy sensor was requested."""
    zid: str
    name: str
    variants: tuple = ()
    requested: tuple = ()
