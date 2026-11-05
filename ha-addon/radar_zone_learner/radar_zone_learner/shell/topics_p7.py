"""P7 topics and the mapping of their one new message to an event (P7 spec 7.1, Appendix A).

``build_topics_p7`` extends the P6 topics; ``TopicsP7.make_mapper`` makes the mapper that knows the new command. The P5
``build_topics_p5``/``TopicMapperP5`` and the P6 ``build_topics_p6``/``TopicMapperP6`` are untouched, so a P5 setup subscribes
to the same 30 topics and a P6 setup to the same 34 as before.

Three topics are new, all on the learner device and none of them per zone or per night:

- ``radar_zones/learner/export_tuning``   ``PRESS``                    command, the button 'Export tuning data' (input)
- ``radar_zones/learner/tuning``          ``none`` or ``<n> nights``   retained, the diagnostic sensor 'Tuning data'
- ``radar_zones/learner/tuning/export``   one JSON document            NOT retained, the pooled counts record (spec 4.8)

Only the first is an input: 34 + 1 = 35 subscriptions (QoS 1). It is a command topic, so a retained message on it is ignored
and deleted like the other commands of the app (an export must never be started by a message that was left on the broker).
The export topic is published once per press and never retained: nothing of the tuning data stays on the broker.
"""
from __future__ import annotations

from dataclasses import dataclass, fields

from .topics_p6 import TopicMapperP6, TopicsP6, build_topics_p6, parse_press

__all__ = ["ExportTuningPress", "TopicsP7", "TopicMapperP7", "build_topics_p7", "LEARNER_BASE"]

LEARNER_BASE = "radar_zones/learner"


@dataclass(frozen=True)
class ExportTuningPress:
    """The button 'Export tuning data' was pressed (a shell event: it is answered by the layer, the core never sees it)."""
    recv_ms: int


@dataclass(frozen=True)
class TopicsP7(TopicsP6):
    learner_export_tuning: str              # input (command), not retained
    learner_tuning: str                     # output, retained: ``none`` or ``<n> nights``
    learner_tuning_export: str              # output, NOT retained: the pooled record

    # ------------------------------------------------------------ subscriptions
    def _p7_inputs(self) -> tuple:
        return (self.learner_export_tuning,)

    def subscriptions(self) -> list:
        """The 34 of P6, then the one command of P7, QoS 1: 35 topics."""
        return super().subscriptions() + [(topic, 1) for topic in self._p7_inputs()]

    def command_topics(self) -> tuple:
        """The 21 of P6 and the export command (22): a retained message on one is ignored and deleted."""
        return super().command_topics() + self._p7_inputs()

    def make_mapper(self) -> "TopicMapperP7":
        return TopicMapperP7(self)


def build_topics_p7(settings) -> TopicsP7:
    base = build_topics_p6(settings)
    return TopicsP7(**{f.name: getattr(base, f.name) for f in fields(base)},
                    learner_export_tuning=f"{LEARNER_BASE}/export_tuning",
                    learner_tuning=f"{LEARNER_BASE}/tuning",
                    learner_tuning_export=f"{LEARNER_BASE}/tuning/export")


class TopicMapperP7(TopicMapperP6):
    """The P6 mapper plus the export command; everything else is handled by the P6, P5, P4 and P3 code unchanged."""

    def __init__(self, topics: TopicsP7) -> None:
        super().__init__(topics)
        self._commands = frozenset(topics.command_topics())
        self._handlers[topics.learner_export_tuning] = self._export

    # ``map`` is inherited: the P6 mapper passes a topic that is not a zone topic to the P5 mapper, whose handler table does
    # the rest (the 256-byte cap, the retained-command rule, the echo of the app's own clear)

    def _export(self, payload, recv_ms):
        return [ExportTuningPress(recv_ms)] if parse_press(payload) else self._ignored()

