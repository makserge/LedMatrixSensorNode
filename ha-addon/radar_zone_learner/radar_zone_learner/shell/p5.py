"""The P5 shell layer: the review device, push requests, privacy controls (P5 spec 2, 3.2, 6.4, 7, 8, Appendix B.1).

``P5Layer`` is a ``P4Layer`` and **the one layer object**: ``main`` passes it as the service's ``p4=`` argument, so there
is one ``NodeLink``, one ``RfStats`` and one event buffer, and no event is handled twice. It overrides what P5 changes
(discovery, republish, the event and output paths, the review publishing, tick, attributes, shutdown) and inherits the
rest unchanged. Everything here runs on the worker thread. It decides nothing about learning: the core owns the items,
the answers, the zones, the suppressions and the push planner; this layer only transports and displays them.

What it does:

- shows the core's review items on the two selects ('Review item', 'Advanced item'), the pending count and attributes,
  the item text, the two ``time`` entities of 'Edit times' and the push request sensor, in the order of spec 2.6;
- turns the user's answers into core events: an answer id ring drops a repeated tap, the item and its revision are filled
  in from what the select shows, a stale revision or a missing item never reaches the core and is reported as the review
  event ``answer_ignored`` (spec 3.2);
- runs 'Show me live' (a timer: a map frame and the 'You are in' text every 2 s for 5 minutes) and publishes the review
  map, the push map and the select option lists;
- runs the privacy controls that are shell business: the Forget all confirmation (the text ``FORGET`` within 60 s), the
  four persistent switches (published from the core's status attributes), the Debug capture switch;
- keeps the last ``ScheduleState``, ``NodeStatus``, contexts and lux so that Forget all can give them to the new learner.

``bind(core_call, learner)`` seeds the mirrors from the learner's accessors before the first publish: the core emits its
P5 outputs only at its first step, and the first ``republish`` runs on the first connect, so without the seeding it
would publish the empty defaults over the retained real push state (spec 6.4, D43).
"""
from __future__ import annotations

import dataclasses
import logging
import time
from collections import deque
from typing import Callable, Optional

from ..core.types import (AdvancedChanged, AnswerIgnored, ContextState, DebugMapChanged, FramesMessage, LuxSample,
                          NightTimeEdit, NodeStatus, PushAck, PushRequest, ScheduleState, SettingSwitch,
                          StatusChanged)
from .capture import DebugCapture
from .discovery import _dumps
from .discovery_p4 import SELECT_ID, p4_discovery_messages, select_config
from .discovery_p5 import EVENT_TYPES_P5, advanced_discovery_message, p5_discovery_messages
from .nodelink import hhmmss
from .p4 import P4Layer
from .p4_types import ITEM_NIGHT_WINDOW, ReviewAnswer, ReviewChanged, ReviewSelect
from .p5_types import (AdvancedSelectLabel, CaptureSwitch, ForgetConfirmText, ForgetPress, LiveSwitch,
                       ReviewSelectLabel, SETTING_NAMES)
from .review import DEFAULT_END_MIN, DEFAULT_START_MIN, state_text
from .review_p5 import (LIVE_OFF_TEXT, item_text_p5, pending_json, push_state, you_are_in_attr, you_are_in_text)
from .reviewmap import ReviewMapPublisher
from .select_model import (NO_ITEM, SELECT_MODE, SELECT_RESEND_S, AdvancedModel, SelectModel)

AID_RING = 64                      # answer ids remembered (spec 3.2 step 1)
FORGET_WINDOW_S = 60.0             # the confirmation text counts for this long (spec 7.2)
FORGET_WORD = "forget"
LIVE_PERIOD_S = 2.0
LIVE_DURATION_S = 300.0
LIVE_SLOW_S = 0.5
LIVE_FAIL_LIMIT = 3                # frames in a row without an answer from the core before the live view ends itself
EMPTY_REQUEST = PushRequest(0, 0, "none", 0, None, None, ())
_SETTING_TOPICS = (("approve_sched", "review_approve_sched"), ("guests_pause", "learner_guests_pause"),
                   ("pets", "learner_pets"), ("detail_text", "learner_detail_text"))
assert tuple(n for n, _ in _SETTING_TOPICS) == SETTING_NAMES

log = logging.getLogger("radar_zone_learner.p5")


def _on_off(flag: bool) -> str:
    return "ON" if flag else "OFF"


class LiveView:
    """'Show me live' (spec 2.8): a pure state machine, the time is passed in.

    ``start`` begins a session (a first frame is due at once, the end is 300 s later) or, while one runs, only extends it;
    ``frame_due`` is true every ``period_s``; ``frame_done`` counts a slow frame and, after two in a row, doubles the period
    for the rest of the session (a guard for a slow host: 40-80 ms are expected); ``expired`` is true at the end."""

    def __init__(self, period_s: float = LIVE_PERIOD_S, duration_s: float = LIVE_DURATION_S,
                 slow_s: float = LIVE_SLOW_S) -> None:
        self.base_period_s = period_s
        self.period_s = period_s
        self.duration_s = duration_s
        self.slow_s = slow_s
        self.active = False
        self.until = 0.0
        self.next_frame = 0.0
        self.frames = 0
        self.slow_frames = 0              # since the app started
        self._slow_run = 0

    def start(self, now: float) -> None:
        if not self.active:
            self.active = True
            self.next_frame = now
            self.period_s = self.base_period_s
            self._slow_run = 0
        self.until = now + self.duration_s

    def stop(self) -> None:
        self.active = False

    def frame_due(self, now: float) -> bool:
        return self.active and now >= self.next_frame

    def frame_done(self, now: float, took_s: float) -> None:
        self.frames += 1
        if took_s > self.slow_s:
            self.slow_frames += 1
            self._slow_run += 1
            if self._slow_run >= 2:
                self.period_s = self.base_period_s * 2
        else:
            self._slow_run = 0
        self.next_frame = now + self.period_s

    def expired(self, now: float) -> bool:
        return self.active and now >= self.until


class P5Layer(P4Layer):
    def __init__(self, topics, publish: Callable[..., bool], data_dir=None, *,
                 monotonic: Callable[[], float] = time.monotonic, select_mode: str = SELECT_MODE,
                 debug_map: bool = True,
                 clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000) -> None:
        super().__init__(topics, publish, data_dir)
        self._mono = monotonic
        self.debug_map = debug_map
        self.sel = SelectModel(select_mode)
        self.adv = AdvancedModel()
        self.maps = ReviewMapPublisher(publish, topics, monotonic=monotonic, enabled=debug_map)
        self.live = LiveView()
        self.capture: Optional[DebugCapture] = (
            DebugCapture(data_dir, clock_ms, monotonic, topic=topics.frames) if data_dir is not None else None)
        self.core_call: Optional[Callable] = None
        self.learner = None
        # what the core said last (mirrors)
        self.adv_entries: dict = {}
        self.setting = {name: False for name in SETTING_NAMES}
        self.more = 0                              # open tier-2 items that did not fit under the cap
        self.command_pending = False               # an approved night window waits for the node's echo
        self.push: PushRequest = EMPTY_REQUEST
        self._push_head = None                     # the head item as it was when the request was made (spec 6.2)
        # answers, selects, privacy
        self._aids: deque = deque(maxlen=AID_RING)
        self.answers_ignored = 0
        self.unknown_labels = 0
        self.forget_text: Optional[str] = None
        self._forget_at = 0.0
        self.forget_requested = False
        self._select_cfg: Optional[list] = None    # the option lists last announced in a discovery config
        self._adv_cfg: Optional[list] = None
        self._resend_at: Optional[float] = None
        self._capture_on_published: Optional[bool] = None
        self._live_fail = 0
        # what Forget all hands to the new learner
        self._c_sched: Optional[ScheduleState] = None
        self._c_node: Optional[NodeStatus] = None
        self._c_ctx: dict = {}
        self._c_lux: Optional[LuxSample] = None

    # ------------------------------------------------------------ lifecycle
    def bind(self, core_call: Callable, learner) -> None:
        """Called by ``Service.start()`` before MQTT starts and again after a Forget all swap. Seeds the mirrors."""
        self.core_call = core_call
        self.learner = learner
        rs = core_call(learner.review_state)
        if rs is not None:
            self._set_items(rs[0], rs[1])
        adv = core_call(learner.advanced)
        if adv is not None:
            self._set_advanced(adv)
        req = core_call(learner.push_request)
        if req is not None:
            self.push = req
            self._push_head = self._head_item_for(req)
        st = core_call(learner.status)
        if st is not None:
            self._apply_status(st.attributes if isinstance(st.attributes, dict) else {})
        self.maps.mark_dirty()

    def start(self, now_ms: int) -> None:
        super().start(now_ms)
        if self.capture is not None:
            self.capture.tick()                    # deletes what is older than 72 h, also while OFF

    def shutdown(self, now_ms: int) -> None:
        super().shutdown(now_ms)
        if self.live.active:
            self._stop_live()                       # OFF, one final static map, 'Live view off' (spec 2.8)
        self._pub(self.topics.review_live_state, "OFF", force=True)
        if self.capture is not None:
            self.capture.stop("manual")
            self._pub(self.topics.learner_debug_capture, "OFF", force=True)

    def reset(self) -> None:
        """Forget all, step 4 of spec 7.2 (the service binds the new learner right after): the mirrors, the push cache,
        the kept maps and the live view are cleared."""
        self.live.stop()
        self.maps.forget()
        self.items = {}
        self.item_order = ()
        self.selected = None
        self.sel.update((), None)
        self.adv.update(())
        self.adv_entries = {}
        self.more = 0
        self.command_pending = False
        self.push = EMPTY_REQUEST
        self._push_head = None
        self._aids.clear()
        self._core_events = []
        self._resend_at = None
        self.forget_text = None
        self.forget_requested = False
        self._pub(self.topics.learner_forget_confirm, "", force=True)

    def purge_capture(self) -> int:
        """Forget all: deletes the raw-frame capture and switches it OFF (spec 7.2 step 3)."""
        if self.capture is None:
            return 0
        n = self.capture.purge()
        self._publish_capture_state()
        return n

    def notify_forgotten(self) -> None:
        self._event("forgotten")

    def cached_inputs(self) -> list:
        """The last schedule state, node status, contexts and lux seen: what the new learner needs to know at once."""
        out: list = [self._c_sched, self._c_node]
        out += list(self._c_ctx.values())
        out.append(self._c_lux)
        return [ev for ev in out if ev is not None]

    def take_forget_request(self) -> bool:
        """True once after an accepted press of 'Forget all learned data'; polled by ``Service.step``."""
        flag, self.forget_requested = self.forget_requested, False
        return flag

    # ------------------------------------------------------------ discovery and states
    def discovery(self) -> list:
        """P4's twelve (select options and event types of the moment) and the nineteen of P5: 31 messages."""
        self._select_cfg = self.sel.options
        self._adv_cfg = self.adv.options
        return (p4_discovery_messages(self.topics, self._select_cfg, list(EVENT_TYPES_P5))
                + p5_discovery_messages(self.topics, self.debug_map, self._adv_cfg))

    def republish(self, now_ms: int) -> None:
        """All states again (after a connect or a Home Assistant birth), in the order of spec 8.8."""
        self._published.clear()
        self._publish_valid()
        self._publish_night_learning()
        self._publish_settings()
        self._publish_capture_state()
        self._publish_live_state()
        self._publish_forget_text()
        self._ensure_maps()
        self.maps.republish_selected(live=self.live.active)
        self._pub(self.topics.review_select_state, self.sel.state())
        self._pub(self.topics.review_advanced_state, self.adv.state())
        self._publish_times()
        self._publish_pending()
        self.maps.republish_push()
        self._publish_push()
        self._publish_you_are_in(force=True)
        self._publish_rf(now_ms / 1000.0, force=True)
        self._flush_events()

    def second_pass(self, now_ms: int) -> None:
        """The service's 10 s timer after a discovery burst (spec 8.8): Home Assistant creates its entities only after the
        burst, so every image and every state that is not retained is sent once more."""
        self._ensure_maps()
        self.maps.republish(live=self.live.active)
        self._pub(self.topics.review_select_state, self.sel.state(), force=True)
        self._pub(self.topics.review_advanced_state, self.adv.state(), force=True)
        self._publish_you_are_in(force=True)
        self._publish_forget_text(force=True)

    def attributes(self) -> dict:
        out = super().attributes()
        out.update({"live": self.live.active, "live_slow": self.live.slow_frames,
                    "answers_ignored": self.answers_ignored, "answer_ids": len(self._aids),
                    "capture": self.capture.stats() if self.capture is not None
                    else {"on": False, "files": 0, "bytes": 0}})
        return out

    # ------------------------------------------------------------ core access
    def _core(self, fn, *args):
        if self.core_call is None:
            return None
        return self.core_call(fn, *args)

    # ------------------------------------------------------------ input events
    def on_event(self, ev, now_ms: int) -> list:
        """Sees every event before the core; returns the events to give the core (usually the event itself)."""
        if isinstance(ev, FramesMessage):
            if self.capture is not None and self.capture.active:
                self.capture.on_frames(ev.recv_ms, ev.payload)
            return [ev]
        if isinstance(ev, ScheduleState):
            self._c_sched = ev
        elif isinstance(ev, NodeStatus):
            self._c_node = ev
        elif isinstance(ev, ContextState):
            self._c_ctx[ev.name] = ev
            return [ev]
        elif isinstance(ev, LuxSample):
            self._c_lux = ev
            return [ev]
        elif isinstance(ev, ReviewAnswer):
            return self._answer(ev)
        elif isinstance(ev, ReviewSelect):
            return [ev]
        elif isinstance(ev, ReviewSelectLabel):
            return self._select_label(ev)
        elif isinstance(ev, AdvancedSelectLabel):
            self._advanced_label(ev)
            return []
        elif isinstance(ev, LiveSwitch):
            self._live_switch(ev.on)
            return []
        elif isinstance(ev, (NightTimeEdit, SettingSwitch, PushAck)):
            return [ev]
        elif isinstance(ev, ForgetConfirmText):
            self._forget_confirm(ev.text)
            return []
        elif isinstance(ev, ForgetPress):
            self._forget_press()
            return []
        elif isinstance(ev, CaptureSwitch):
            self._capture_switch(ev.on)
            return []
        return super().on_event(ev, now_ms)

    # ------------------------------------------------------------ answers (spec 3.2)
    def _answer(self, ev: ReviewAnswer) -> list:
        if ev.aid is not None:
            if ev.aid in self._aids:
                return []                                 # a QoS-1 re-delivery or a double tap: dropped silently
            self._aids.append(ev.aid)
        op = ev.op
        if op in ("forget", "undo"):
            if op == "undo" and not ev.item:
                return self._refuse(None, op, "no_item")
            return [ReviewAnswer(ev.item, op, ev.rev, ev.sched, ev.recv_ms, name=ev.name, aid=ev.aid)]
        if op == "advanced":
            entry_id = ev.item or self.adv.selected
            if not entry_id or entry_id not in self.adv.ids():
                return self._refuse(entry_id, op, "no_such_item")
            return [ReviewAnswer(entry_id, op, ev.rev, ev.sched, ev.recv_ms, name=ev.name, aid=ev.aid)]
        item_id = ev.item or self.selected
        item = self.items.get(item_id) if item_id else None
        if item is None:
            return self._refuse(item_id, op, "no_such_item")
        if ev.rev is not None and ev.rev != item.rev:
            return self._refuse(item.id, op, "stale")
        sched = ev.sched or (op == "approve" and item.id == ITEM_NIGHT_WINDOW and self.setting["approve_sched"])
        return [ReviewAnswer(item.id, op, item.rev, sched, ev.recv_ms, name=ev.name, aid=ev.aid)]

    def _refuse(self, item_id: Optional[str], op: str, reason: str) -> list:
        """An answer the shell does not hand to the core: counted, logged with ids only, shown as a review event."""
        self.dropped_answers += 1
        self.answers_ignored += 1
        log.info("answer %s for %s ignored: %s", op, item_id or "-", reason)
        self._event("answer_ignored", reason=reason, op=op, item=item_id or "")
        return []

    # ------------------------------------------------------------ the selects
    def _select_label(self, ev: ReviewSelectLabel) -> list:
        res = self.sel.resolve(ev.text)
        if res is None:
            # an option that was removed meanwhile: Home Assistant's list is stale, so the config goes out again
            # (a repeated state alone would not repair that: a state for an unknown option is dropped)
            self.unknown_labels += 1
            log.info("select label not known: the option list is published again")
            self._publish_select_config(force=True)
            self._pub(self.topics.review_select_state, self.sel.state(), force=True)
            return []
        return [ReviewSelect(res[1], ev.recv_ms)]

    def _advanced_label(self, ev: AdvancedSelectLabel) -> None:
        res = self.adv.resolve(ev.text)
        if res is None:
            self.unknown_labels += 1
            log.info("Advanced label not known: the option list is published again")
            self._publish_advanced_config(force=True)
        else:
            self.adv.select(res[1])
        self._pub(self.topics.review_advanced_state, self.adv.state(), force=True)

    def _publish_select_config(self, force: bool = False) -> None:
        options = self.sel.options
        if not force and options == self._select_cfg:
            return
        topic = self.topics.discovery("select", SELECT_ID)
        payload = _dumps(select_config(self.topics, options))
        if self._publish(topic, payload, qos=1, retain=False):
            self._select_cfg = options
            self._schedule_resend()

    def _publish_advanced_config(self, force: bool = False) -> None:
        options = self.adv.options
        if not force and options == self._adv_cfg:
            return
        topic, payload = advanced_discovery_message(self.topics, options)
        if self._publish(topic, payload, qos=1, retain=False):
            self._adv_cfg = options
            self._schedule_resend()

    def _schedule_resend(self) -> None:
        """Home Assistant applies a changed option list in a task and drops a state for an option it does not know yet:
        the state goes out again shortly after the config (spec 2.6 step 7)."""
        self._resend_at = self._mono() + SELECT_RESEND_S

    # ------------------------------------------------------------ core outputs
    def on_output(self, out, now_ms: int) -> bool:
        """True when the output was consumed here (the service must not handle it again)."""
        if isinstance(out, StatusChanged):
            self._status_changed(out, now_ms)
            return False
        if isinstance(out, DebugMapChanged):
            self.maps.mark_dirty()
            return False
        if isinstance(out, AdvancedChanged):
            self._set_advanced(out.entries)
            self.maps.mark_dirty()
            self._publish_advanced()
            self._publish_map_if_due()
            return True
        if isinstance(out, PushRequest):
            self._push_request(out)
            return True
        if isinstance(out, AnswerIgnored):
            self.answers_ignored += 1
            log.info("answer %s for %s ignored by the core: %s", out.op, out.item or "-", out.reason)
            self._event("answer_ignored", reason=out.reason, op=out.op, item=out.item or "")
            return True
        return super().on_output(out, now_ms)

    def _status_changed(self, out: StatusChanged, now_ms: int) -> None:
        super().on_output(out, now_ms)             # core_state, Night learning, radar valid (it returns False)
        attrs = out.attributes if isinstance(out.attributes, dict) else {}
        before = (dict(self.setting), self.more)
        self._apply_status(attrs)
        if self.setting != before[0]:
            self._publish_settings()
        if self.setting["detail_text"] != before[0]["detail_text"] or self.more != before[1]:
            self._publish_review()

    def _apply_status(self, attrs: dict) -> None:
        s = attrs.get("settings")
        if isinstance(s, dict):
            for name in SETTING_NAMES:
                if isinstance(s.get(name), bool):
                    self.setting[name] = s[name]
        r = attrs.get("review")
        if isinstance(r, dict):
            more = r.get("more")
            if isinstance(more, int) and not isinstance(more, bool):
                self.more = more
            if isinstance(r.get("command_pending"), bool):
                self.command_pending = r["command_pending"]

    # ------------------------------------------------------------ review items
    def _set_items(self, items, selected: Optional[str]) -> None:
        items = list(items)
        self.items = {i.id: i for i in items}
        self.item_order = tuple(i.id for i in items)
        self.selected = selected if selected in self.items else None
        if self.sel.update(items, self.selected):
            self._schedule_resend()

    def _set_advanced(self, entries) -> None:
        entries = list(entries)
        self.adv_entries = {e.id: e for e in entries}
        if self.adv.update(entries):
            self._schedule_resend()

    def _review_changed(self, out: ReviewChanged) -> None:
        self._set_items(out.items, out.selected)
        self.maps.mark_dirty()
        self._publish_review()

    def _texts(self) -> dict:
        window = self._window()
        detail = self.setting["detail_text"]
        return {i: item_text_p5(self.items[i], window, detail) for i in self.item_order}

    def _publish_review(self) -> None:
        """The review items on the device, in the order of spec 2.6: the map first (when it is due), then the select's
        config (when its options changed), the select state, the pending attributes and state, the item text, the time
        entities and the push state."""
        self._publish_map_if_due()
        self._publish_select_config()
        self._pub(self.topics.review_select_state, self.sel.state())
        self._publish_pending()
        self._publish_times()
        self._publish_push()

    def _publish_pending(self) -> None:
        t = self.topics
        texts = self._texts()
        rows = [{"id": i, "rev": self.items[i].rev, "type": self.items[i].type,
                 "tier": getattr(self.items[i], "tier", 1), "label": self.sel.label_of(i), "text": texts[i]}
                for i in self.item_order]
        self._pub(t.review_pending_attr, pending_json(self.selected, rows, self.more))
        self._pub(t.review_pending, str(len(self.items)))
        self._pub(t.review_item_text, state_text(texts[self.selected]) if self.selected in texts else NO_ITEM)

    def _publish_advanced(self) -> None:
        self._publish_advanced_config()
        self._pub(self.topics.review_advanced_state, self.adv.state())

    def _publish_times(self) -> None:
        """'Edit times' (spec 3.4): the edited value, else the proposal's, else the node's current window."""
        nw = self.items.get(ITEM_NIGHT_WINDOW)
        data = nw.data if nw is not None and isinstance(nw.data, dict) else {}
        node = self.node
        defaults = (node.start_min if node.start_min is not None else DEFAULT_START_MIN,
                    node.end_min if node.end_min is not None else DEFAULT_END_MIN)
        out = []
        for key, proposal, fallback in (("edit_start", "start_min", defaults[0]), ("edit_end", "end_min", defaults[1])):
            value = None
            for k in (key, proposal):
                v = data.get(k)
                if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1440:
                    value = v
                    break
            out.append(hhmmss(value if value is not None else fallback))
        self._pub(self.topics.review_nw_start, out[0])
        self._pub(self.topics.review_nw_end, out[1])

    def _publish_settings(self) -> None:
        for name, field in _SETTING_TOPICS:
            self._pub(getattr(self.topics, field), _on_off(self.setting[name]))

    # ------------------------------------------------------------ the push request (spec 6.3, 6.4)
    def _head_item_for(self, req: PushRequest):
        item = self.items.get(req.head_id) if req.head_id else None
        return item if item is not None and item.rev == req.head_rev else None

    def _push_request(self, req: PushRequest) -> None:
        new_seq = req.seq != self.push.seq
        self.push = req
        if new_seq:
            self._push_head = self._head_item_for(req)
            self._publish_push_map(req)
        self._publish_push()

    def _push_map_data(self, req: PushRequest):
        """The map data of the push image: geometry only (spec 6.7). The phone fetches the image from Home Assistant and it
        may travel outside the home network, so it carries neither the dwell heat (where the household spends its time)
        nor the outlines of failed candidates: the room, the outlines of the places and the highlighted head."""
        data = self._core(self.learner.review_map, req.head_id, False) if self.learner is not None else None
        if data is None:
            return None
        data = dataclasses.replace(data, dwell_rate=(0.0,) * len(data.dwell_rate), candidates=())
        if req.head_id is None:
            data = dataclasses.replace(data, highlight=frozenset(), focus=None)
        return data

    def _publish_push_map(self, req: PushRequest) -> None:
        if not self.maps.enabled:
            return
        data = self._push_map_data(req)
        if data is not None:
            self.maps.publish_push(data)

    def _publish_push(self) -> None:
        detail = self.setting["detail_text"]
        head_text = ""
        if detail and self._push_head is not None:
            head_text = item_text_p5(self._push_head, self._window(), True)
        self._pub(self.topics.review_push, push_state(self.push, head_text, detail))

    # ------------------------------------------------------------ maps
    def _ensure_maps(self) -> None:
        """A review map and a push map to publish, made now when the layer has none yet (a start, a Forget all)."""
        if not self.maps.enabled or self.learner is None:
            return
        if not self.maps.has_base or self.maps.dirty:
            data = self._map_data()
            if data is not None:
                self.maps.set_base(data)
        if self.maps.push_png is None:
            data = self._push_map_data(self.push)
            if data is not None:
                self.maps.keep_push(data)

    def _publish_map_if_due(self) -> None:
        if self.maps.enabled and not self.live.active and self.maps.resend_due(self._mono()):
            self._publish_map_now()

    def _map_data(self):
        """The learner's map for the selected item. A core error (``None``) is not retried every second: the base stays
        out of date until the next change makes it dirty again."""
        data = self._core(self.learner.review_map, self.selected, False)
        if data is None:
            self.maps.dirty = False
        return data

    def _publish_map_now(self, force: bool = False) -> None:
        if self.learner is None:
            return
        data = self._map_data()
        if data is not None:
            self.maps.publish_selected(data, self._mono(), force)

    # ------------------------------------------------------------ 'Show me live' and 'You are in' (spec 2.7, 2.8)
    def _live_switch(self, on: bool) -> None:
        now = self._mono()
        if on:
            self.live.start(now)
            self._live_fail = 0
            self._publish_live_state()
            if self.learner is not None:
                self._live_frame(now)               # the first frame at once
        elif self.live.active:
            self._stop_live()
        else:
            self._publish_live_state()

    def _stop_live(self) -> None:
        """OFF (the switch, the end of the 5 minutes): one final static map without markers and 'Live view off'."""
        self.live.stop()
        self._publish_live_state()
        if self.maps.enabled and self.learner is not None:
            self._publish_map_now()
        self._publish_you_are_in()

    def _publish_live_state(self) -> None:
        self._pub(self.topics.review_live_state, _on_off(self.live.active))

    def _live_frame(self, now: float) -> None:
        t0 = self._mono()
        points = self._core(self.learner.live_view)
        where = self._core(self.learner.where_am_i, self.selected)
        if points is not None and where is not None:
            self._live_fail = 0
            if self.maps.enabled:
                if self.maps.dirty or not self.maps.has_base:
                    data = self._map_data()
                    if data is not None:
                        self.maps.set_base(data)
                self.maps.publish_frame(points, now)
            self._publish_you_are_in(where)
            self.live.frame_done(now, self._mono() - t0)
            return
        self._live_fail += 1
        self.live.frame_done(now, self._mono() - t0)
        if self._live_fail >= LIVE_FAIL_LIMIT:
            log.warning("the live view ended: the learner gave no answer in %d frames in a row", self._live_fail)
            self._stop_live()

    def _where_label(self, item_id: str) -> Optional[str]:
        if item_id in self.items:
            return self.sel.place_label(item_id)
        for z in self._core(self.learner.zones) or ():
            if z.id == item_id:
                return z.name
        return None

    def _publish_you_are_in(self, where=None, force: bool = False) -> None:
        """The sensor says ``Live view off`` unless the live view runs (spec D27); it never carries a coordinate."""
        t = self.topics
        if self.live.active and self.learner is not None:
            if where is None:
                where = self._core(self.learner.where_am_i, self.selected)
            if where is not None:
                self._pub(t.review_you_are_in, you_are_in_text(True, where, self._where_label), retain=False,
                          force=force)
                self._pub(t.review_you_are_in_attr, you_are_in_attr(where.people, where.stay_ready), retain=False,
                          force=force)
                return
        self._pub(t.review_you_are_in, LIVE_OFF_TEXT, retain=False, force=force)
        self._pub(t.review_you_are_in_attr, you_are_in_attr(0, False), retain=False, force=force)

    # ------------------------------------------------------------ Forget all (spec 7.2)
    def _forget_confirm(self, text: str) -> None:
        self.forget_text = text
        self._forget_at = self._mono()
        self._publish_forget_text()

    def _clear_forget_text(self) -> None:
        self.forget_text = None
        self._publish_forget_text()

    def _publish_forget_text(self, force: bool = False) -> None:
        self._pub(self.topics.learner_forget_confirm, self.forget_text or "", force=force)

    def _forget_press(self) -> None:
        """Every press clears the text; only ``FORGET`` set within the last 60 s starts the reset, and not while Night
        learning runs or is still putting the node's schedule back (a reset would drop that bookkeeping and strand the
        node's night schedule OFF), nor while an approved night window waits for the node's echo."""
        armed = (self.forget_text is not None and self.forget_text.strip().casefold() == FORGET_WORD
                 and self._mono() - self._forget_at <= FORGET_WINDOW_S)
        self._clear_forget_text()
        if not armed:
            self._event("forget_refused", reason="not_confirmed")
        elif self.nl_active or self.nl_restoring:
            self._event("forget_refused", reason="night_learning_active")
        elif self.command_pending:
            self._event("forget_refused", reason="command_pending")
        else:
            self.forget_requested = True

    # ------------------------------------------------------------ Debug capture (spec 7.8)
    def _capture_switch(self, on: bool) -> None:
        if self.capture is not None:
            if on:
                self.capture.start()
            else:
                self.capture.stop("manual")
        self._publish_capture_state()

    def _publish_capture_state(self) -> None:
        on = self.capture is not None and self.capture.active
        self._pub(self.topics.learner_debug_capture, _on_off(on))
        self._capture_on_published = on

    # ------------------------------------------------------------ time
    def tick(self, now_ms: int) -> None:
        super().tick(now_ms)
        now = self._mono()
        if self.forget_text is not None and now - self._forget_at >= FORGET_WINDOW_S:
            self._clear_forget_text()
        if self.live.active:
            if self.live.expired(now):
                self._stop_live()
            elif self.live.frame_due(now) and self.learner is not None:
                self._live_frame(now)
        else:
            self._publish_map_if_due()
        if self._resend_at is not None and now >= self._resend_at:
            self._resend_at = None
            self._pub(self.topics.review_select_state, self.sel.state(), force=True)
            self._pub(self.topics.review_advanced_state, self.adv.state(), force=True)
        if self.capture is not None:
            self.capture.tick()
            if self.capture.active != self._capture_on_published:
                self._publish_capture_state()
