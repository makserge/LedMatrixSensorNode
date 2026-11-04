"""A fake Home Assistant for the push flow (P5 spec 11.5): the broker's retained messages, the entities that exist only
after their discovery config was seen, a restart, and a Python port of the blueprint ``radar_zone_review_push.yaml``.

There is no Home Assistant on this PC and the blueprint is YAML, so the delivery logic is tested against this reference
port; the blueprint text is checked against it by ``test_p5_blueprint`` (text checks) and the real blueprint is traced
by the owner (spec 11.9). The port keeps the order of the blueprint's steps:

1. trigger: every change of the push sensor's state, including the first one after the entity was created (a state
   trigger with no ``from``/``to``), starts a run (``mode: queued``, at most 10);
2. run-start check on the trigger's snapshot: eight fields, ``seq > 0``, kind ``setup`` or ``daily``, ``count > 0`` and
   this phone (``me``) not among the ``acks``; ``count`` 0 with ``seq > 0`` clears the notification instead;
3. a delay of 15 s; the re-check on the entity's current state (same ``seq``, still valid, ``me`` still not acked);
4. the notification (a second one with the same tag replaces the first), then the acknowledgement ``<seq>:<me>``.

``acked`` is never read: the test is per phone (spec D42).

Rules (spec 11.5): H1 retained messages are kept by the broker whether Home Assistant is up or not, a non-retained one
published while it is down is lost; H2 a discovery config seen while up creates its entity, which then receives the
retained payload of its state topic; H3 ``stop`` cancels every delayed run and drops the entities; H4 ``start`` sends the
birth message to the app; H5 triggers; H6 the run; H7 a tap on an action; H8 time moves with ``advance``/``run_due``; H9 two
phones are two instances that share the broker and the client; H10 ``start_offset_s`` delays the start of every run and
``start_enabled=False`` creates the automation later (``enable``); H11 ``image_delay_s`` delays the image entity: it
exists (and subscribes to its topic) only that many seconds after its discovery config was seen, so an unretained image
sent before that is lost, as in Home Assistant when the image platform is slow to start.
"""
from __future__ import annotations

import json
import re
from typing import Optional

PUSH_UID = "radar_zones_review_push"
PUSH_MAP_UID = "radar_zones_review_push_map"
ANSWER_TOPIC = "radar_zones/review/answer"
ACK_TOPIC = "radar_zones/review/push_ack"
BIRTH_TOPIC = "homeassistant/status"
TAG = "radar_zone_review"
DELAY_S = 15.0
MAX_RUNS = 10
ACTION_RE = re.compile(r"^RZ_(APPROVE|RENAME|REJECT)_[0-9]+_.+$")
GENERIC_SETUP = "Radar setup review is ready (%d items)"
GENERIC_DAILY = "Radar zone review: %d item(s) to look at"


def _int(text, default):
    try:
        return int(str(text).strip())
    except (ValueError, TypeError):
        return default


class _Run:
    def __init__(self, snap: str, at: float) -> None:
        self.snap = snap
        self.at = at
        self.phase = "queued"            # queued -> delay


class FakeHa:
    def __init__(self, client, clock, detailed_text: bool = False, phone: str = "phoneA", start_offset_s: float = 0.0,
                 notify_fails: bool = False, start_enabled: bool = True, image_delay_s: float = 0.0) -> None:
        self.client = client
        self.clock = clock
        self.detailed_text = detailed_text
        self.phone = phone
        self.me = "p" + phone[:7]
        self.start_offset_s = start_offset_s
        self.notify_fails = notify_fails
        self.image_delay_s = image_delay_s
        self._image_pending: Optional[dict] = None   # the image entity waiting for its platform (H11)
        self.enabled = start_enabled
        self.up = True
        self.drop_acks = False                   # the app is down when the ack is published: the test decides
        self.entities: dict = {}                 # "push" / "push_map" -> {"topic": str, "state"/"bytes": ...}
        self.broker_retained: dict = {}
        self.visible: dict = {}                  # tag -> {"title", "message", "data", "image"}
        self.sends: list = []                    # every send, in order
        self.clears: list = []
        self.acks: list = []                     # every ack published
        self.failures = 0                        # runs that stopped at a failing notify action
        self.taps_lost = 0
        self.dropped_runs = 0
        self._cursor = len(client.published)
        self._active: Optional[_Run] = None
        self._pending: list = []
        self._ctx = 0

    # ------------------------------------------------------------ the broker side
    def process(self) -> int:
        """Reads what the app published since the last call (H1, H2). Returns how many messages it saw."""
        self._image_ready()
        pub = self.client.published
        if len(pub) < self._cursor:
            self._cursor = 0
        new = pub[self._cursor:]
        self._cursor = len(pub)
        for topic, payload, _qos, retain in new:
            if retain:
                if payload == b"":
                    self.broker_retained.pop(topic, None)
                else:
                    self.broker_retained[topic] = payload
            if not self.up:
                continue                         # a non-retained message published while HA is down is lost
            self._deliver(topic, payload)
        self.run_due()                           # a run that was just triggered starts at once
        return len(new)

    def _deliver(self, topic: str, payload: bytes) -> None:
        if topic.startswith("homeassistant/") and topic.endswith("/config"):
            self._discovery(topic, payload)
            return
        for name, ent in list(self.entities.items()):
            if ent["topic"] == topic:
                if name == "push":
                    self._set_state(payload.decode("utf-8", "replace"))
                else:
                    ent["bytes"] = payload

    def _discovery(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) != 4 or payload == b"":
            return
        component, uid = parts[1], parts[2]
        try:
            cfg = json.loads(payload)
        except ValueError:
            return
        if component == "sensor" and uid == PUSH_UID and "push" not in self.entities:
            self.entities["push"] = {"topic": cfg["state_topic"], "state": None}
            retained = self.broker_retained.get(cfg["state_topic"])
            if retained is not None:
                self._set_state(retained.decode("utf-8", "replace"))       # H2: the retained payload at once
        elif component == "image" and uid == PUSH_MAP_UID and "push_map" not in self.entities:
            if self.image_delay_s > 0:
                if self._image_pending is None:
                    self._image_pending = {"topic": cfg["image_topic"], "at": self.clock.monotonic() + self.image_delay_s}
            else:
                self.entities["push_map"] = {"topic": cfg["image_topic"], "bytes": None}

    def _image_ready(self) -> None:
        """H11: the delayed image entity comes into being and subscribes; nothing sent before is replayed."""
        pend = self._image_pending
        if pend is not None and self.up and self.clock.monotonic() >= pend["at"]:
            self._image_pending = None
            self.entities.setdefault("push_map", {"topic": pend["topic"], "bytes": None})

    @property
    def push_state(self) -> Optional[str]:
        ent = self.entities.get("push")
        return ent["state"] if ent else None

    @property
    def push_map_bytes(self) -> Optional[bytes]:
        ent = self.entities.get("push_map")
        return ent["bytes"] if ent else None

    # ------------------------------------------------------------ H3, H4: stop and start
    def stop(self) -> None:
        self.up = False
        self._active = None
        self._pending = []
        self._image_pending = None
        self.entities = {}                       # unavailable until discovered again

    def start(self) -> None:
        self.up = True
        self.client.fire_message(BIRTH_TOPIC, "online")

    def enable(self) -> None:
        """The automation is created now (H10): nothing fires until the state changes."""
        self.enabled = True

    # ------------------------------------------------------------ H5: trigger
    def _set_state(self, text: str) -> None:
        ent = self.entities["push"]
        old = ent["state"]
        ent["state"] = text
        if text != old and self.enabled:         # no from/to/not_from/not_to: also unknown or unavailable to a value
            self._trigger(text)

    def _trigger(self, snapshot: str) -> None:
        if len(self._pending) + (1 if self._active else 0) >= MAX_RUNS:
            self.dropped_runs += 1               # max_exceeded: silent
            return
        self._pending.append(_Run(snapshot, self.clock.monotonic() + self.start_offset_s))

    # ------------------------------------------------------------ H6, H8: the run
    def run_due(self) -> int:
        """Runs every step whose time has come (queued runs one after the other). Returns the number of steps."""
        steps = 0
        self._image_ready()
        now = self.clock.monotonic()
        progressed = True
        while progressed:
            progressed = False
            if self._active is not None:
                if self._active.at <= now:
                    run, self._active = self._active, None
                    self._finish(run)
                    steps += 1
                    progressed = True
            elif self._pending and self._pending[0].at <= now:
                run = self._pending.pop(0)
                if self._begin(run, now):
                    run.at = now + DELAY_S
                    run.phase = "delay"
                    self._active = run
                steps += 1
                progressed = True
        return steps

    def advance(self, seconds: float) -> None:
        """H8: moves the shared clock and runs what is due (the test steps the app in between where it matters)."""
        self.clock.advance(seconds)
        self.process()
        self.run_due()

    def _fields(self, text: Optional[str]) -> list:
        return (text if text is not None else "unknown").split("|")

    def _begin(self, run: _Run, now: float) -> bool:
        """The run-start check on the snapshot. True: wait 15 s, then re-check and send."""
        snap = self._fields(run.snap)
        if not (len(snap) == 8 and _int(snap[0], -1) >= 0 and _int(snap[1], -1) >= 0 and _int(snap[3], -1) >= 0):
            return False
        seq, count = _int(snap[0], 0), _int(snap[3], 0)
        if count == 0 and seq > 0:
            self.visible.pop(TAG, None)
            self.clears.append(seq)
            return False
        return seq > 0 and snap[2] in ("setup", "daily") and count > 0 and self.me not in snap[6].split(",")

    def _finish(self, run: _Run) -> None:
        snap = self._fields(run.snap)
        cur = self._fields(self.push_state)
        if not (len(cur) == 8 and cur[0] == snap[0] and cur[2] in ("setup", "daily") and _int(cur[3], 0) > 0
                and self.me not in cur[6].split(",")):
            return
        seq, kind, count, rev, item = _int(cur[0], 0), cur[2], _int(cur[3], 0), cur[4], cur[5]
        generic = GENERIC_SETUP % count if kind == "setup" else GENERIC_DAILY % count
        message = cur[7] if self.detailed_text and len(cur[7]) > 0 else generic
        if self.notify_fails:
            self.failures += 1                   # the device action raises: the run stops before the acknowledgement
            return
        data = {"tag": TAG, "group": TAG, "alert_once": True, "clickAction": "/config/devices/dashboard",
                "url": "/config/devices/dashboard", "image": "/api/image_proxy/image.radar_zones_review_push_map"}
        if item != "" and rev != "":
            data["actions"] = [
                {"action": "RZ_APPROVE_%s_%s" % (rev, item), "title": "Approve"},
                {"action": "RZ_RENAME_%s_%s" % (rev, item), "title": "Rename", "behavior": "textInput"},
                {"action": "RZ_REJECT_%s_%s" % (rev, item), "title": "Reject"}]
        note = {"title": "Radar zone review", "message": message, "data": data, "seq": seq,
                "image": self.push_map_bytes}      # the image as it is in the entity at this moment
        self.visible[TAG] = note                   # the same tag replaces the first copy
        self.sends.append(note)
        ack = "%d:%s" % (seq, self.me)
        self.acks.append(ack)
        if not self.drop_acks:
            self.client.fire_message(ACK_TOPIC, ack)

    # ------------------------------------------------------------ H7: a tap on a notification action
    def phone_tap(self, action_id: str, reply_text=None, context_id: Optional[str] = None) -> Optional[dict]:
        """Delivers the answer of the blueprint's action handler to the app. While HA is down the tap is lost."""
        if not self.up:
            self.taps_lost += 1
            return None
        if not ACTION_RE.match(action_id):
            return None
        _, op, rev, item = action_id.split("_", 3)
        self._ctx += 1
        body = {"op": op.lower(), "item": item, "rev": int(rev), "aid": context_id or ("ctx%s%d" % (self.me, self._ctx))}
        name = ("" if reply_text is None else str(reply_text)).strip()[:32]
        if body["op"] == "rename" and name != "":
            body["name"] = name
        self.client.fire_message(ANSWER_TOPIC, json.dumps(body))
        return body

    def tap_notification(self, button: str, reply_text=None, context_id: Optional[str] = None) -> Optional[dict]:
        """Taps ``Approve``, ``Rename`` or ``Reject`` on the visible notification."""
        note = self.visible.get(TAG)
        if note is None:
            return None
        for a in note["data"].get("actions", []):
            if a["title"] == button:
                return self.phone_tap(a["action"], reply_text, context_id)
        return None
