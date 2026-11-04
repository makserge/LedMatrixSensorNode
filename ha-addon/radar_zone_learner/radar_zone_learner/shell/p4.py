"""The P4 shell layer: radar valid, RF and coverage numbers, the review items, Night learning and the node link.

``Service`` calls into this object (see ``service.py``); without it the app is the P3 app. Everything here
runs on the worker thread.

What it does (design 6.3, 7.5-7.9, 4.2):

- publishes ``radar_zones/radar/valid`` (``online`` only while the learner observes, the node is online and
  its 'Radar observing' is not OFF), the three RF and coverage sensors, and the 'Night learning' switch state;
- shows the core's review items (night window, bed zone) on the review device, and hands the user's select
  and Approve / Reject / Later answers to the core (filling in the selected item and its revision, and
  dropping an answer for a stale revision);
- fires the review event ``mark_failed`` for a refused 'Mark spot here' press, ``device_not_confirmed``
  when the node's echo of a command does not come within 2 minutes, and ``restore_failed`` when the core gives
  up putting the node's schedule back (no confirmation after the tries, or the restore is more than a day
  overdue). An event that cannot be published because the broker link is down is kept (at most 20) and sent
  as soon as the link is back: the missing echo is most often caused by that very outage;
- executes the core's ``NodeCommand`` through ``NodeLink`` (nothing is sent before the P2 firmware's
  schedule echo has been seen) and gives the core the ``CommandResult``.

It decides nothing about learning: onsets, wakes, the proposal, the bed item and the Night learning
state machine belong to the core; this layer only transports and displays them.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional

from ..core.types import MarkResult, NodeStatus, ScheduleState, StatusChanged
from .discovery_p4 import p4_discovery_messages
from .nodelink import NodeLink
from .p4_types import (ITEM_IDS, RADAR_ON_STATES, SELECT_LABELS, SELECT_NONE, CommandResult,
                       NightLearningChanged, NodeCommand, RadarObservingMsg, RadarStateMsg, ReviewAnswer,
                       ReviewChanged, ReviewSelect)
from .review import DEFAULT_END_MIN, DEFAULT_START_MIN, item_text, state_text
from .rfstats import RfStats

RF_PUBLISH_PERIOD_S = 300.0       # the RF and coverage sensors are refreshed this often (and on a radar state change)
MAX_UNSENT_EVENTS = 20            # review events kept while the broker link is down
# the core's give-up events (``NightLearningChanged.event``) and the ``reason`` of the review event they raise
NL_FAILURES = {"restore_failed": "no_confirmation", "restore_overdue": "overdue"}

log = logging.getLogger("radar_zone_learner.p4")


def _on_off(flag: bool) -> str:
    return "ON" if flag else "OFF"


class P4Layer:
    def __init__(self, topics, publish: Callable[..., bool], data_dir: Optional[Path] = None) -> None:
        self.topics = topics
        self._publish = publish
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.node = NodeLink(topics, publish)
        self.rf = RfStats()
        # what the learner and the node said last
        self.core_state = "starting"
        self.observing: Optional[bool] = None          # <node>/radar/observing (P2)
        self.node_online: Optional[bool] = None
        self.radar_state: Optional[str] = None
        self.nl_active = False                         # Night learning: OFF by default (OA 7.6)
        self.nl_restoring = False                      # the learner's own OFF of the node's schedule is not put back yet
        self.nl_until: Optional[str] = None
        self.items: dict = {}                          # review items by id
        self.item_order: tuple = ()
        self.selected: Optional[str] = None
        self.dropped_answers = 0
        self._core_events: list = []
        self._unsent_events: list = []                 # review event payloads waiting for the broker link
        self._published: dict = {}                     # topic -> last payload that was sent
        self._rf_at: Optional[float] = None
        self._rf_dirty = True

    # ------------------------------------------------------------ lifecycle
    def start(self, now_ms: int) -> None:
        if self.data_dir is not None:
            self.rf.load_file(self.data_dir, now_ms / 1000.0)
        self.rf.set_valid(self._valid(), now_ms / 1000.0)

    def save(self, now_ms: int) -> None:
        if self.data_dir is None:
            return
        self.rf.advance(now_ms / 1000.0)
        try:
            self.rf.save_file(self.data_dir)
        except OSError as exc:
            log.error("saving the RF numbers failed: %s", exc.__class__.__name__)

    def shutdown(self, now_ms: int) -> None:
        """Best effort before the connection closes: the radar data are no longer valid."""
        self._pub(self.topics.radar_valid, "offline", force=True)
        self.rf.set_valid(False, now_ms / 1000.0)

    # ------------------------------------------------------------ discovery and states
    def discovery(self) -> list:
        return p4_discovery_messages(self.topics)

    def republish(self, now_ms: int) -> None:
        """All states again (after a connect or a Home Assistant birth); discovery is sent by the service."""
        self._published.clear()
        self._publish_valid()
        self._publish_night_learning()
        self._publish_review()
        self._publish_rf(now_ms / 1000.0, force=True)
        self._flush_events()

    def attributes(self) -> dict:
        n = self.node
        return {"node_schedule_seen": n.p2_seen, "node_cmds_sent": n.sent, "node_cmds_unconfirmed": n.unconfirmed,
                "node_cmds_refused": n.refused, "review_items": len(self.items)}

    def _pub(self, topic: str, payload, retain: bool = True, qos: int = 1, force: bool = False) -> None:
        if not force and self._published.get(topic) == payload:
            return
        if self._publish(topic, payload, qos=qos, retain=retain):
            self._published[topic] = payload

    # ------------------------------------------------------------ input events (worker thread)
    def on_event(self, ev, now_ms: int) -> list:
        """Sees every event before the core; returns the events to give the core (usually the event itself)."""
        if isinstance(ev, ScheduleState):
            self._results(self.node.on_schedule_state(ev, now_ms))
            self._publish_review()                       # the bed item shows the window
            return [ev]
        if isinstance(ev, NodeStatus):
            self.node.on_node_status(ev.online)
            self.node_online = ev.online
            if not ev.online:
                self.rf.set_radar(None, now_ms / 1000.0)
                self.radar_state = None
            self._update_valid(now_ms)
            return [ev]
        if isinstance(ev, RadarStateMsg):
            self.radar_state = ev.state
            self.rf.set_radar(ev.state in RADAR_ON_STATES, now_ms / 1000.0)
            self._rf_dirty = True
            return []
        if isinstance(ev, RadarObservingMsg):
            self.observing = ev.observing
            self._update_valid(now_ms)
            return []
        if isinstance(ev, ReviewAnswer):
            return self._answer(ev)
        if isinstance(ev, ReviewSelect):
            if ev.item is not None and ev.item not in ITEM_IDS:
                return []
            return [ev]
        return [ev]

    def _answer(self, ev: ReviewAnswer) -> list:
        item_id = ev.item or self.selected
        item = self.items.get(item_id) if item_id else None
        if item is None:
            self.dropped_answers += 1
            log.info("answer %s ignored: no such review item is open", ev.op)
            return []
        if ev.rev is not None and ev.rev != item.rev:
            self.dropped_answers += 1
            log.info("answer %s for %s ignored: stale revision %s (current %s)", ev.op, item.id, ev.rev, item.rev)
            return []
        return [ReviewAnswer(item.id, ev.op, item.rev, ev.sched, ev.recv_ms)]

    # ------------------------------------------------------------ core outputs
    def on_output(self, out, now_ms: int) -> bool:
        """True when the output was consumed here (the service must not handle it again)."""
        if isinstance(out, StatusChanged):
            self.core_state = out.state
            attrs = out.attributes if isinstance(out.attributes, dict) else {}
            nl = attrs.get("night_learning")
            if isinstance(nl, dict) and isinstance(nl.get("active"), bool):
                self._set_night_learning(nl["active"], nl.get("until") if isinstance(nl.get("until"), str) else None,
                                         nl.get("set_by_learner") is False)
            self._update_valid(now_ms)
            return False
        if isinstance(out, MarkResult):
            if not out.ok:
                self._event("mark_failed", reason=out.reason or "unknown")
            return False
        if isinstance(out, NodeCommand):
            self._results(self.node.send(out, now_ms))
            return True
        if isinstance(out, ReviewChanged):
            self._review_changed(out)
            return True
        if isinstance(out, NightLearningChanged):
            self._set_night_learning(out.active, out.until, out.set_by_learner is False)
            reason = NL_FAILURES.get(out.event) if out.event else None
            if reason is not None:
                log.warning("Night learning could not put the node's schedule back (%s): switch 'Radar night "
                            "schedule' ON yourself if it is OFF", out.event)
                self._event("restore_failed", reason=reason)
            return True
        return False

    def tick(self, now_ms: int) -> None:
        now_s = now_ms / 1000.0
        self._results(self.node.poll(now_ms))
        self.rf.advance(now_s)
        self._publish_rf(now_s, force=False)
        self._flush_events()

    def take_core_events(self) -> list:
        evs, self._core_events = self._core_events, []
        return evs

    # ------------------------------------------------------------ valid
    def _valid(self) -> bool:
        return self.core_state == "learning" and self.observing is not False and self.node_online is not False

    def _update_valid(self, now_ms: int) -> None:
        valid = self._valid()
        if valid != self.rf.valid:
            self.rf.set_valid(valid, now_ms / 1000.0)
        self._publish_valid()

    def _publish_valid(self) -> None:
        self._pub(self.topics.radar_valid, "online" if self._valid() else "offline")

    # ------------------------------------------------------------ night learning
    def _set_night_learning(self, active: bool, until: Optional[str], restoring: bool = False) -> None:
        """``restoring``: the learner wrote OFF to the node's schedule and has not seen it ON again (``set_by_learner``
        False). It outlives ``active``: the user switching Night learning off, or its 7th night ending, only starts
        the restore, which can wait days for an offline node."""
        if active != self.nl_active:
            log.info("Night learning %s", "started" if active else "ended")
        self.nl_active = active
        self.nl_restoring = restoring
        self.nl_until = until
        self._publish_night_learning()

    def _publish_night_learning(self) -> None:
        self._pub(self.topics.night_learning_state, _on_off(self.nl_active))

    # ------------------------------------------------------------ review items
    def _review_changed(self, out: ReviewChanged) -> None:
        items = [i for i in out.items if getattr(i, "id", None) in ITEM_IDS]
        self.items = {i.id: i for i in items}
        self.item_order = tuple(i.id for i in items)
        sel = out.selected if out.selected in self.items else None
        self.selected = sel
        self._publish_review()

    def _window(self) -> tuple:
        n = self.node
        return (n.sched, n.start_min if n.start_min is not None else DEFAULT_START_MIN,
                n.end_min if n.end_min is not None else DEFAULT_END_MIN)

    def _publish_review(self) -> None:
        t = self.topics
        sched, start, end = self._window()
        texts = {i: item_text(self.items[i], sched, start, end) for i in self.item_order}
        label = SELECT_LABELS.get(self.selected, SELECT_NONE) if self.selected else SELECT_NONE
        self._pub(t.review_select_state, label)
        self._pub(t.review_pending, str(len(self.items)))
        attr = {"selected": self.selected,
                "items": [{"id": i, "rev": self.items[i].rev, "type": self.items[i].type, "text": texts[i]}
                          for i in self.item_order]}
        self._pub(t.review_pending_attr, json.dumps(attr, separators=(",", ":"), ensure_ascii=True))
        self._pub(t.review_item_text, state_text(texts[self.selected]) if self.selected else "None")

    # ------------------------------------------------------------ events and command results
    def _event(self, event_type: str, **fields) -> None:
        payload = dict(fields)
        payload["event_type"] = event_type
        self._unsent_events.append(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
        del self._unsent_events[:-MAX_UNSENT_EVENTS]
        self._flush_events()

    def _flush_events(self) -> None:
        """Publishes the waiting review events in order; the first one that does not go (broker link down) stays."""
        while self._unsent_events:
            if not self._publish(self.topics.review_event, self._unsent_events[0], qos=1, retain=False):
                return
            self._unsent_events.pop(0)

    def _results(self, results: list) -> None:
        for r in results:
            if not isinstance(r, CommandResult):
                continue
            self._core_events.append(r)
            if not r.ok and r.reason == "no_echo":
                self._event("device_not_confirmed", kind=r.kind)

    # ------------------------------------------------------------ RF numbers
    def _publish_rf(self, now_s: float, force: bool) -> None:
        if not force and not self._rf_dirty and self._rf_at is not None and now_s - self._rf_at < RF_PUBLISH_PERIOD_S:
            return
        self._rf_at = now_s
        self._rf_dirty = False
        t = self.topics
        for topic, value in ((t.rf_on_24h, self.rf.rf_on_24h(now_s)), (t.rf_saving_7d, self.rf.rf_saving_7d(now_s)),
                             (t.coverage_7d, self.rf.coverage_7d(now_s))):
            # "None" is what Home Assistant's MQTT sensor reads as unknown
            self._pub(topic, "None" if value is None else ("%g" % value))
