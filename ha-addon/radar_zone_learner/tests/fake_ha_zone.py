"""A generic fake Home Assistant for the zone entities of P6 (P6 spec 11.5): an entity registry built from the discovery
configs, availability handling, states and attributes, an ``event`` entity log, ``number``/``button``/``switch`` commands, a
stop and a start, and a recorder model with rows per entity per local day.

There is no Home Assistant on this PC. Everything below that is about Home Assistant's behaviour is an ASSUMPTION, each one
an ``HA[U<n>]`` of the spec (section 14.2) that acceptance steps A2, A8 and A9 settle on the real installation, and the tests
that use this model prove only that the add-on's messages produce the intended history *under these assumptions*:

- R-H3, R-H11, R-H12, R-H15 (verified in the P5 spec, 17.1): an MQTT ``event`` entity ignores retained messages and takes JSON
  with ``event_type``; Home Assistant's client has a clean session (a message published while it is down is lost, a retained
  one is replayed on subscribe); a retained state is replayed when an entity subscribes.
- HA[U1] A discovery config published not retained creates an entity that is dropped at the next restart until the config is
  published again (``stop`` drops every entity; ``start`` sends the birth message).
- HA[U2] An empty payload on the config topic removes the entity (its registry entry and its history stay).
- HA[U3] An MQTT entity keeps a payload that arrives while it is unavailable and shows it when it becomes available; two
  publishes of one client arrive in order (the messages are handled one after the other in the order they were published).
- HA[U4] An attribute-only change writes a recorder row; attributes are kept for an unavailable entity.
- HA[U5] ``availability`` as a list of ``{topic, payload_available, payload_not_available}`` with ``availability_mode: all``
  works for binary_sensor, sensor and event; ``availability_topic`` for number, button and switch. Until a message has been
  seen on an availability topic the entity is unavailable.
- HA[U6] An ``event`` entity accepts extra attributes (``track``, ``dwell_s``, ``via``, ``variant``) in its JSON payload.
- HA[U7] A new entity with a new ``unique_id`` does not inherit the registry entry (entity id, history) of a removed zone,
  while a reused ``unique_id`` does (the reason for the epoch id of DV6/D41).
- An entity with ``enabled_by_default: false`` is not created until ``enable`` (the zone count sensor).
- ``create_delay_s`` (default 0): Home Assistant creates an entity that many seconds after its config was read; a not retained
  state or attribute message that arrives before is lost (the reason for the app's second pass and its resend after a new
  config; the P5 fake has the same rule for images, H11).

The recorder model (spec 7.5): one row for every change of an entity's effective state (``unavailable`` when it is not
available) **or** of its attributes (HA[U4]); an ``event`` entity writes one row for every event. ``rows(entity_id, day)``
counts the rows of one local day; ``recorded`` entities can be restricted with ``exclude``.
"""
from __future__ import annotations

import json
from typing import Optional

BIRTH_TOPIC = "homeassistant/status"
DAY_MS = 86_400_000
UNAVAILABLE = "unavailable"


class Entity:
    def __init__(self, component: str, uid: str, cfg: dict, registry: dict) -> None:
        self.component = component
        self.uid = uid
        self.cfg = cfg
        reg = registry.get(uid)
        if reg is None:
            reg = registry[uid] = {"entity_id": cfg.get("default_entity_id") or f"{component}.{uid}", "history": [],
                                   "created": 0}
        reg["created"] += 1
        self.reg = reg
        self.entity_id = reg["entity_id"]
        self.state: Optional[str] = None        # the raw payload as the entity understands it (None = unknown)
        self.attrs: dict = {}
        self.avail: dict = {}                   # availability topic -> True/False (absent: no message seen yet)
        self.events: list = []                  # event entity: [(wall_ms, payload dict)]
        self.bad_payloads = 0
        self.set_config(cfg)

    def set_config(self, cfg: dict) -> None:
        self.cfg = cfg
        self.name = cfg.get("name")
        self.device = cfg.get("device") or {}
        entries = cfg.get("availability")
        self.avail_entries = []                 # (topic, payload_available, payload_not_available)
        self.avail_mode = cfg.get("availability_mode", "latest")
        if isinstance(entries, list):
            for e in entries:
                self.avail_entries.append((e["topic"], e.get("payload_available", "online"),
                                           e.get("payload_not_available", "offline")))
        elif cfg.get("availability_topic"):
            self.avail_entries.append((cfg["availability_topic"], cfg.get("payload_available", "online"),
                                       cfg.get("payload_not_available", "offline")))
        self.enabled = bool(cfg.get("enabled_by_default", True))

    # ------------------------------------------------------------ availability and state
    @property
    def available(self) -> bool:
        if not self.avail_entries:
            return True
        seen = [self.avail.get(t) for t, _, _ in self.avail_entries]
        if any(v is None for v in seen) and self.avail_mode == "all":
            return False
        if self.avail_mode == "all":
            return all(seen)
        if self.avail_mode == "any":
            return any(v for v in seen if v is not None)
        known = [v for v in seen if v is not None]                # 'latest' (one topic): the last value seen
        return bool(known) and known[-1]

    @property
    def effective(self) -> str:
        if not self.available:
            return UNAVAILABLE
        if self.state is None:
            return "unknown"
        return self.state

    def topics(self) -> set:
        out = {t for t, _, _ in self.avail_entries}
        for key in ("state_topic", "json_attributes_topic"):
            if self.cfg.get(key):
                out.add(self.cfg[key])
        return out


class FakeZoneHa:
    def __init__(self, client, clock, create_delay_s: float = 0.0) -> None:
        self.client = client
        self.clock = clock
        self.create_delay_s = create_delay_s
        self.pending: dict = {}                 # (component, uid) -> (cfg, monotonic time of creation)
        self.up = True
        self.entities: dict = {}                # (component, uid) -> Entity (only while HA is up)
        self.registry: dict = {}                # uid -> {"entity_id", "history": [(wall_ms, state, attrs json)], "created"}
        self.broker_retained: dict = {}
        self.disabled: dict = {}                # (component, uid) -> cfg of an entity that is not enabled
        self.excluded: set = set()              # entity ids the recorder does not record
        self.commands: list = []                # (topic, payload) of every command sent
        self.removed: list = []                 # unique ids removed by an empty config
        self.messages_seen = 0
        self._cursor = len(client.published)

    # ------------------------------------------------------------ the broker side
    def process(self) -> int:
        """Reads what the app published since the last call, in order (HA[U3])."""
        self._create_due()
        pub = self.client.published
        if len(pub) < self._cursor:
            self._cursor = 0
        new = pub[self._cursor:]
        self._cursor = len(pub)
        for topic, payload, _qos, retain in new:
            self.hear(topic, payload, retain)
        self._create_due()
        return len(new)

    def hear(self, topic: str, payload, retain: bool = False) -> None:
        """One message on the broker, from the app or from another client (the node's will and birth on ``sensors/status``): the
        broker keeps a retained one whether Home Assistant is up or not, a not retained one is lost while it is down (R-H11)."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if retain:
            if payload == b"":
                self.broker_retained.pop(topic, None)
            else:
                self.broker_retained[topic] = payload
        if not self.up:
            return
        self.messages_seen += 1
        self._deliver(topic, payload, retain)

    def _create_due(self) -> None:
        now = self.clock.monotonic()
        for key, (cfg, at) in list(self.pending.items()):
            if now >= at:
                del self.pending[key]
                self._create(key[0], key[1], cfg)

    def _deliver(self, topic: str, payload: bytes, retained: bool, only: Optional[Entity] = None) -> None:
        if topic.startswith("homeassistant/") and topic.endswith("/config"):
            self._discovery(topic, payload)
            return
        for ent in list(self.entities.values()):
            if only is not None and ent is not only:
                continue
            if topic not in ent.topics():
                continue
            self._apply(ent, topic, payload, retained)

    def _discovery(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) != 4:
            return
        component, uid = parts[1], parts[2]
        key = (component, uid)
        if payload == b"":
            ent = self.entities.pop(key, None)
            self.pending.pop(key, None)
            self.disabled.pop(key, None)
            if ent is not None:
                self.removed.append(uid)                 # HA[U2]
            return
        try:
            cfg = json.loads(payload.decode("utf-8"))
        except ValueError:
            return
        ent = self.entities.get(key)
        if ent is not None:
            ent.set_config(cfg)
            return
        if key in self.disabled:
            self.disabled[key] = cfg
            return
        if key in self.pending:
            self.pending[key] = (cfg, self.pending[key][1])
            return
        if cfg.get("enabled_by_default") is False:
            self.disabled[key] = cfg                     # not created until ``enable``
            return
        if self.create_delay_s > 0:
            self.pending[key] = (cfg, self.clock.monotonic() + self.create_delay_s)
            return
        self._create(component, uid, cfg)

    def _create(self, component: str, uid: str, cfg: dict) -> Entity:
        ent = Entity(component, uid, cfg, self.registry)
        self.entities[(component, uid)] = ent
        for topic in sorted(ent.topics()):               # a new entity subscribes: the retained messages are replayed (R-H12)
            held = self.broker_retained.get(topic)
            if held is not None:
                self._apply(ent, topic, held, True)
        self._record(ent)
        return ent

    def enable(self, entity_id: str) -> bool:
        """The user enables a disabled entity (the count sensor): it is created now and subscribes."""
        for key, cfg in list(self.disabled.items()):
            if (cfg.get("default_entity_id") or f"{key[0]}.{key[1]}") == entity_id:
                del self.disabled[key]
                self._create(key[0], key[1], cfg)
                return True
        return False

    # ------------------------------------------------------------ one message for one entity
    def _apply(self, ent: Entity, topic: str, payload: bytes, retained: bool) -> None:
        text = payload.decode("utf-8", "replace")
        cfg = ent.cfg
        changed = False
        for t, pa, pn in ent.avail_entries:
            if t == topic:
                if text == pa:
                    ent.avail[t] = True
                elif text == pn:
                    ent.avail[t] = False
                changed = True
        if topic == cfg.get("json_attributes_topic"):
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                ent.attrs = obj                          # kept while unavailable (HA[U4])
                changed = True
        if topic == cfg.get("state_topic"):
            changed = self._state(ent, text, retained) or changed
        if changed:
            self._record(ent)

    def _state(self, ent: Entity, text: str, retained: bool) -> bool:
        comp = ent.component
        cfg = ent.cfg
        if comp == "event":
            if retained:
                return False                             # R-H3: retained messages are ignored by an event entity
            try:
                obj = json.loads(text)
            except ValueError:
                ent.bad_payloads += 1
                return False
            et = obj.get("event_type") if isinstance(obj, dict) else None
            if et not in cfg.get("event_types", ()):
                ent.bad_payloads += 1
                return False
            ent.events.append((self.clock.clock_ms(), dict(obj)))
            ent.state = "event#%d" % len(ent.events)
            ent.attrs = {k: v for k, v in obj.items() if k != "event_type"}
            ent.attrs["event_type"] = et                 # HA[U6]
            return True                                  # a new state ("event#N") and so a new row
        if comp == "binary_sensor":
            if text == cfg.get("payload_on", "ON"):
                new = "on"
            elif text == cfg.get("payload_off", "OFF"):
                new = "off"
            else:
                ent.bad_payloads += 1
                return False
        elif comp == "switch":
            if text == cfg.get("state_on", "ON"):
                new = "on"
            elif text == cfg.get("state_off", "OFF"):
                new = "off"
            else:
                ent.bad_payloads += 1
                return False
        elif comp == "number":
            try:
                value = float(text)
            except ValueError:
                ent.bad_payloads += 1
                return False
            if not (cfg.get("min", float("-inf")) <= value <= cfg.get("max", float("inf"))):
                ent.bad_payloads += 1
                return False
            new = str(int(value)) if value.is_integer() else str(value)
        else:
            if len(text) > 255:
                ent.bad_payloads += 1                    # R-H15
                return False
            new = text
        if comp != "button":
            ent.state = new
        return True

    # ------------------------------------------------------------ the recorder
    def _record(self, ent: Entity) -> None:
        """One row for every change of the effective state or of the attributes (HA[U4])."""
        if ent.entity_id in self.excluded:
            return
        hist = ent.reg["history"]
        row = (self.clock.clock_ms(), ent.effective, json.dumps(ent.attrs, sort_keys=True))
        if not hist or hist[-1][1:] != row[1:]:
            hist.append(row)

    # ------------------------------------------------------------ views
    def entity(self, entity_id: str) -> Optional[Entity]:
        for ent in self.entities.values():
            if ent.entity_id == entity_id:
                return ent
        return None

    def by_uid(self, uid: str) -> Optional[Entity]:
        for (comp, u), ent in self.entities.items():
            if u == uid:
                return ent
        return None

    def state(self, entity_id: str) -> Optional[str]:
        ent = self.entity(entity_id)
        return ent.effective if ent is not None else None

    def attrs(self, entity_id: str) -> dict:
        ent = self.entity(entity_id)
        return dict(ent.attrs) if ent is not None else {}

    def entity_ids(self) -> list:
        return sorted(e.entity_id for e in self.entities.values())

    def history_for(self, entity_id: str) -> list:
        for reg in self.registry.values():
            if reg["entity_id"] == entity_id:
                return list(reg["history"])
        return []

    def states(self, entity_id: str) -> list:
        """The effective states in the recorder, consecutive repeats (attribute-only rows) collapsed:
        ``["unavailable", "on", "unavailable", "off"]``."""
        out = []
        for _, eff, _ in self.history_for(entity_id):
            if not out or out[-1] != eff:
                out.append(eff)
        return out

    def history(self, entity_id: str) -> list:
        """``(state, available)`` transitions of an entity (spec 11.5): ``("unavailable", False)``, ``("on", True)``, ... with
        consecutive repeats collapsed, so a test can assert 'no ``off`` caused by the radars going off'."""
        return [(st, st != UNAVAILABLE) for st in self.states(entity_id)]

    def rows(self, entity_id: str, day: Optional[int] = None) -> int:
        """Recorder rows of one local day (the day number of ``wall_ms // 86,400,000``; all days when ``day`` is None)."""
        hist = self.history_for(entity_id)
        if day is None:
            return len(hist)
        return sum(1 for ms, _, _ in hist if ms // DAY_MS == day)

    def rows_of_zone(self, zid: str, day: Optional[int] = None, uid_base: Optional[str] = None) -> int:
        """Rows of every entity of the zone (the entity ids contain the zone's unique id base)."""
        base = uid_base or f"radar_spot_{zid}"
        return sum(self.rows(reg["entity_id"], day) for uid, reg in self.registry.items()
                   if uid == base or uid.startswith(base + "_"))

    def exclude(self, *entity_ids) -> None:
        self.excluded.update(entity_ids)

    def events(self, entity_id: str) -> list:
        ent = self.entity(entity_id)
        return [p for _, p in ent.events] if ent is not None else []

    # ------------------------------------------------------------ commands (the user)
    def _send(self, ent: Entity, payload: str) -> None:
        topic = ent.cfg["command_topic"]
        self.commands.append((topic, payload))
        self.client.fire_message(topic, payload, False, ent.cfg.get("qos", 0))

    def number_set(self, entity_id: str, value) -> None:
        ent = self.entity(entity_id)
        value = float(value)
        self._send(ent, str(int(value)) if value.is_integer() else str(value))

    def press(self, entity_id: str) -> None:
        ent = self.entity(entity_id)
        self._send(ent, ent.cfg.get("payload_press", "PRESS"))

    def switch_set(self, entity_id: str, on: bool) -> None:
        ent = self.entity(entity_id)
        self._send(ent, ent.cfg.get("payload_on", "ON") if on else ent.cfg.get("payload_off", "OFF"))

    # ------------------------------------------------------------ HA[U1]: stop and start
    def stop(self) -> None:
        self.up = False
        self.entities = {}
        self.disabled = {}
        self.pending = {}

    def start(self, birth: bool = True) -> None:
        """Home Assistant starts: no entity exists until its config arrives; the birth message goes to the app (a test that
        has no MQTT callbacks on its client passes ``birth=False`` and plays the app's answer itself)."""
        self.up = True
        self.process()
        if birth:
            self.client.fire_message(BIRTH_TOPIC, "online")
