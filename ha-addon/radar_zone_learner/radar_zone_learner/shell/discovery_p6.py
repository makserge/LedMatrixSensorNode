"""MQTT discovery payloads of the P6 entities (P6 spec 8.1, 8.4, Appendix B.1): six configs for each confirmed zone, a
request switch for each variant of a zone that has variants, and an occupancy sensor for each variant that was requested.

Every zone is one device ``radar_zone_<zid>`` (``via_device`` the node, ``Led_Matrix_Node``). The entities of one zone:

========================  ===========  ==============================  ==========================================
entity                    component    ``unique_id`` (``<base>`` below)  availability
========================  ===========  ==============================  ==========================================
occupancy                 binary_sensor  ``<base>``                      the three-entry list, mode ``all``
target count (disabled)   sensor         ``<base>_count``                the three-entry list, mode ``all``
enter/leave               event          ``<base>_event``                the three-entry list, mode ``all``
clear delay               number         ``<base>_clear_delay``          ``availability_topic`` (learner status)
margin                    number         ``<base>_margin``               ``availability_topic`` (learner status)
forget learned data       button         ``<base>_forget``               ``availability_topic`` (learner status)
variant occupancy         binary_sensor  ``<base>_v_<variant>``          the three-entry list, mode ``all``
variant request           switch         ``<base>_vreq_<variant>``       ``availability_topic`` (learner status)
========================  ===========  ==============================  ==========================================

``<base>`` is ``radar_spot_<zid>`` at installation epoch 0 and ``radar_spot_e<epoch>_<zid>`` after a Forget all (spec D41:
zone and label ids restart at ``z0001``/``l0001``, and a reused ``unique_id`` would inherit the registry entry of a removed
place); the device identifier follows the same rule. Topics keep the plain zone id.

The tuning entities, the Forget button and the request switches are configuration: they use the single learner availability
topic only, so they work while the radars are off. The zone state entities also depend on the node's status and on the zone
availability topic of ``P6Layer`` (spec 5.7).

Discovery is published not retained, like every config of the app. A removal is an empty, not retained payload on the config
topic (spec 7.1, D25). The zone name is part of every entity name and of the device name: the only place a zone name leaves the
add-on (README section 11).
"""
from __future__ import annotations

from ..core.types import APP_VERSION
from .discovery import _dumps, _origin
from .p6_types import ZONE_KINDS_EVALUATED, ZoneEntry

DEVICE_VIA = "Led_Matrix_Node"                      # the node's device identifier (F:include/constants.h)
ONLINE, OFFLINE = "online", "offline"
OCC_ON, OCC_OFF = "ON", "OFF"
EVENT_TYPES_P6 = ("enter", "leave")
SPOT_PREFIX = "Spot "


def base_uid(zid: str, epoch: int = 0) -> str:
    """The ``unique_id`` of the occupancy entity (and the base of the others)."""
    return f"radar_spot_{zid}" if not epoch else f"radar_spot_e{epoch}_{zid}"


def device_id(zid: str, epoch: int = 0) -> str:
    return f"radar_zone_{zid}" if not epoch else f"radar_zone_e{epoch}_{zid}"


def zone_title(name: str, zid: str) -> str:
    """The name of a zone's device and the stem of its entity names: the zone's own name, ``Spot <id>`` for an unnamed label."""
    name = (name or "").encode("utf-8", "replace").decode("utf-8").strip()     # a lone surrogate cannot be encoded
    return name if name else SPOT_PREFIX + zid


def entity_zones(views, cap: int) -> list:
    """The zones that get entities: of the kinds that are evaluated, in the order of ``Learner.zones()`` (records by id, then
    labels by id), at most ``cap`` of them (spec D27, D44)."""
    out = [v for v in views if getattr(v, "kind", None) in ZONE_KINDS_EVALUATED]
    return out[:max(0, cap)]


def _device(title: str, zid: str, epoch: int) -> dict:
    return {"identifiers": [device_id(zid, epoch)], "name": title, "model": "radar_zone_learner zone",
            "sw_version": APP_VERSION, "via_device": DEVICE_VIA}


def availability_all(topics) -> dict:
    """The three-entry availability of the zone state entities, ``availability_mode`` ``all`` (spec 5.7, 8.1): the learner's
    will, the node's will and the zone availability topic. Never combined with ``availability_topic`` in one config."""
    return {"availability": [
        {"topic": topics.learner_status, "payload_available": ONLINE, "payload_not_available": OFFLINE},
        {"topic": topics.node_status, "payload_available": ONLINE, "payload_not_available": OFFLINE},
        {"topic": topics.zone_availability, "payload_available": ONLINE, "payload_not_available": OFFLINE}],
        "availability_mode": "all"}


def _head(component: str, uid: str, name: str) -> dict:
    return {"name": name, "unique_id": uid, "default_entity_id": f"{component}.{uid}"}


def _state_tail(topics, device: dict) -> dict:
    out = dict(availability_all(topics))
    out.update({"device": device, "origin": _origin()})
    return out


def _config_tail(topics, device: dict) -> dict:
    return {"availability_topic": topics.learner_status, "device": device, "origin": _origin()}


# ---------------------------------------------------------------- the six configs of a zone
def occupancy_config(topics, entry: ZoneEntry, epoch: int = 0) -> dict:
    title = zone_title(entry.name, entry.zid)
    cfg = _head("binary_sensor", base_uid(entry.zid, epoch), f"{title} occupancy")
    cfg.update({"state_topic": topics.zone_occupancy(entry.zid), "device_class": "occupancy", "payload_on": OCC_ON,
                "payload_off": OCC_OFF, "json_attributes_topic": topics.zone_attributes(entry.zid)})
    cfg.update(_state_tail(topics, _device(title, entry.zid, epoch)))
    return cfg


def count_config(topics, entry: ZoneEntry, epoch: int = 0) -> dict:
    title = zone_title(entry.name, entry.zid)
    cfg = _head("sensor", base_uid(entry.zid, epoch) + "_count", f"{title} target count")
    cfg.update({"state_topic": topics.zone_count(entry.zid), "state_class": "measurement",
                "entity_category": "diagnostic", "enabled_by_default": False, "icon": "mdi:account-multiple"})
    cfg.update(_state_tail(topics, _device(title, entry.zid, epoch)))
    return cfg


def event_config(topics, entry: ZoneEntry, epoch: int = 0) -> dict:
    title = zone_title(entry.name, entry.zid)
    cfg = _head("event", base_uid(entry.zid, epoch) + "_event", f"{title} enter/leave")
    cfg.update({"state_topic": topics.zone_event(entry.zid), "event_types": list(EVENT_TYPES_P6),
                "icon": "mdi:location-enter"})
    cfg.update(_state_tail(topics, _device(title, entry.zid, epoch)))
    return cfg


def _number(topics, entry: ZoneEntry, epoch: int, suffix: str, what: str, command: str, state: str, lo: int, hi: int,
            step: int, unit: str, icon: str) -> dict:
    title = zone_title(entry.name, entry.zid)
    cfg = _head("number", base_uid(entry.zid, epoch) + suffix, f"{title} {what}")
    cfg.update({"command_topic": command, "state_topic": state, "min": lo, "max": hi, "step": step,
                "unit_of_measurement": unit, "mode": "box", "optimistic": False, "qos": 1, "retain": False,
                "entity_category": "config", "icon": icon})
    cfg.update(_config_tail(topics, _device(title, entry.zid, epoch)))
    return cfg


def clear_delay_config(topics, entry: ZoneEntry, epoch: int = 0) -> dict:
    return _number(topics, entry, epoch, "_clear_delay", "clear delay", topics.zone_clear_delay_set(entry.zid),
                   topics.zone_clear_delay(entry.zid), 3, 900, 1, "s", "mdi:timer-sand")


def margin_config(topics, entry: ZoneEntry, epoch: int = 0) -> dict:
    return _number(topics, entry, epoch, "_margin", "margin", topics.zone_margin_set(entry.zid),
                   topics.zone_margin(entry.zid), -50, 100, 5, "cm", "mdi:arrow-expand-all")


def forget_config(topics, entry: ZoneEntry, epoch: int = 0) -> dict:
    title = zone_title(entry.name, entry.zid)
    cfg = _head("button", base_uid(entry.zid, epoch) + "_forget", f"{title} forget learned data")
    cfg.update({"command_topic": topics.zone_forget(entry.zid), "payload_press": "PRESS", "qos": 1, "retain": False,
                "entity_category": "config", "icon": "mdi:eraser"})
    cfg.update(_config_tail(topics, _device(title, entry.zid, epoch)))
    return cfg


# ---------------------------------------------------------------- variants
def variant_switch_config(topics, entry: ZoneEntry, variant: str, epoch: int = 0) -> dict:
    """The request switch of one variant: ON asks for the variant's occupancy sensor (spec 5.8, D23)."""
    title = zone_title(entry.name, entry.zid)
    cfg = _head("switch", f"{base_uid(entry.zid, epoch)}_vreq_{variant}", f"{title} - {variant} sensor")
    cfg.update({"command_topic": topics.zone_variant_entity_set(entry.zid, variant),
                "state_topic": topics.zone_variant_entity(entry.zid, variant), "payload_on": OCC_ON,
                "payload_off": OCC_OFF, "state_on": OCC_ON, "state_off": OCC_OFF, "optimistic": False, "qos": 1,
                "retain": False, "entity_category": "config", "icon": "mdi:clock-time-four-outline"})
    cfg.update(_config_tail(topics, _device(title, entry.zid, epoch)))
    return cfg


def variant_sensor_config(topics, entry: ZoneEntry, variant: str, epoch: int = 0) -> dict:
    """The occupancy sensor of one requested variant: ON while the zone is occupied and the variant is the one in force."""
    title = zone_title(entry.name, entry.zid)
    cfg = _head("binary_sensor", f"{base_uid(entry.zid, epoch)}_v_{variant}", f"{title} - {variant} occupancy")
    cfg.update({"state_topic": topics.zone_variant(entry.zid, variant), "device_class": "occupancy",
                "payload_on": OCC_ON, "payload_off": OCC_OFF})
    cfg.update(_state_tail(topics, _device(title, entry.zid, epoch)))
    return cfg


# ---------------------------------------------------------------- the messages
def zone_config_topics(topics, zid: str, epoch: int = 0) -> list:
    """The six config topics of a zone, in the order of its messages."""
    base = base_uid(zid, epoch)
    return [topics.discovery("binary_sensor", base), topics.discovery("sensor", base + "_count"),
            topics.discovery("event", base + "_event"), topics.discovery("number", base + "_clear_delay"),
            topics.discovery("number", base + "_margin"), topics.discovery("button", base + "_forget")]


def variant_switch_topic(topics, zid: str, variant: str, epoch: int = 0) -> str:
    return topics.discovery("switch", f"{base_uid(zid, epoch)}_vreq_{variant}")


def variant_sensor_topic(topics, zid: str, variant: str, epoch: int = 0) -> str:
    return topics.discovery("binary_sensor", f"{base_uid(zid, epoch)}_v_{variant}")


def zone_messages(topics, entry: ZoneEntry, requested=None, epoch: int = 0) -> list:
    """(topic, payload bytes) of one zone: its six configs, a request switch for each of its variants and an occupancy
    sensor for each requested variant (a requested id that is not one of the zone's variants gets no sensor)."""
    want = tuple(entry.requested if requested is None else requested)
    base = base_uid(entry.zid, epoch)
    t = topics
    out = [(t.discovery("binary_sensor", base), _dumps(occupancy_config(t, entry, epoch))),
           (t.discovery("sensor", base + "_count"), _dumps(count_config(t, entry, epoch))),
           (t.discovery("event", base + "_event"), _dumps(event_config(t, entry, epoch))),
           (t.discovery("number", base + "_clear_delay"), _dumps(clear_delay_config(t, entry, epoch))),
           (t.discovery("number", base + "_margin"), _dumps(margin_config(t, entry, epoch))),
           (t.discovery("button", base + "_forget"), _dumps(forget_config(t, entry, epoch)))]
    for v in entry.variants:
        out.append((variant_switch_topic(t, entry.zid, v, epoch), _dumps(variant_switch_config(t, entry, v, epoch))))
    for v in entry.variants:
        if v in want:
            out.append((variant_sensor_topic(t, entry.zid, v, epoch),
                        _dumps(variant_sensor_config(t, entry, v, epoch))))
    return out


def p6_discovery_messages(topics, zones, requested=None, cap: int = 24, epoch: int = 0) -> list:
    """(topic, payload bytes) of every P6 config: for each of the first ``cap`` zones its six configs, a request switch for
    each variant and an occupancy sensor for each requested variant.

    ``zones`` are ``ZoneEntry`` values in the order of ``Learner.zones()``; ``requested`` is ``None`` (use each entry's own
    ``requested``) or a mapping zone id -> the requested variant ids. ``epoch`` is the installation epoch of the
    ``unique_id`` and the device identifier (0 until the first Forget all)."""
    out = []
    for entry in list(zones)[:max(0, cap)]:
        req = None if requested is None else requested.get(entry.zid, ())
        out.extend(zone_messages(topics, entry, req, epoch))
    return out


def p6_removal_messages(topics, zids=(), switches=(), sensors=(), epoch: int = 0) -> list:
    """Empty, not retained config payloads (spec 7.1, 8.4, D25, D43): the six configs of each whole zone in ``zids`` (their
    variant entities are listed in ``switches`` and ``sensors``), the request switch of each ``(zid, variant)`` in
    ``switches`` and the occupancy sensor of each ``(zid, variant)`` in ``sensors``."""
    out = []
    for zid in zids:
        out.extend((topic, b"") for topic in zone_config_topics(topics, zid, epoch))
    out.extend((variant_switch_topic(topics, z, v, epoch), b"") for z, v in switches)
    out.extend((variant_sensor_topic(topics, z, v, epoch), b"") for z, v in sensors)
    return out
