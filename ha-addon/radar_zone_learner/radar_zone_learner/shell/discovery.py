"""MQTT discovery payloads of the P3 entities (spec 9.2), with full key names and in the spec's key order.

Discovery is published not retained, after every connect and 0-10 s after each Home Assistant birth
message (spec 9.2). With the ``debug_map`` option off, the image entity is not announced; an empty
config payload is sent in its place so that an image entity announced earlier is removed from HA.
"""
from __future__ import annotations

import json

from ..core.types import APP_VERSION, STATUS_STATES

LEARNER_STATUS_ID = "radar_zones_learner_status"
DEBUG_MAP_ID = "radar_zones_learner_debug_map"
MARK_ID = "radar_zones_review_mark"
NAME_ID = "radar_zones_review_name"


def _device_learner() -> dict:
    return {"identifiers": ["radar_zones_learner"], "name": "Radar zone learner",
            "model": "radar_zone_learner app", "sw_version": APP_VERSION}


def _device_review() -> dict:
    return {"identifiers": ["radar_zones_review"], "name": "Radar zone review",
            "model": "radar_zone_learner app", "sw_version": APP_VERSION, "via_device": "radar_zones_learner"}


def _origin() -> dict:
    return {"name": "radar_zone_learner", "sw_version": APP_VERSION}


def _dumps(obj: dict) -> bytes:
    """The discovery payload as UTF-8 JSON. Not escaped (``ensure_ascii`` off): a name with an umlaut or a CJK character
    costs 2 or 3 bytes, not the 6 of a JSON unicode escape; a pure ASCII payload is byte-identical either way."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def learner_status_config(topics) -> dict:
    return {
        "name": "Learner status",
        "unique_id": LEARNER_STATUS_ID,
        "default_entity_id": "sensor." + LEARNER_STATUS_ID,
        "state_topic": topics.learner_state,
        "json_attributes_topic": topics.learner_attr,
        "device_class": "enum",
        "options": list(STATUS_STATES),
        "entity_category": "diagnostic",
        "icon": "mdi:radar",
        "availability_topic": topics.learner_status,
        "device": _device_learner(),
        "origin": _origin(),
    }


def debug_map_config(topics) -> dict:
    return {
        "name": "Learner debug map",
        "unique_id": DEBUG_MAP_ID,
        "default_entity_id": "image." + DEBUG_MAP_ID,
        "image_topic": topics.learner_debug_map,
        "content_type": "image/png",
        "entity_category": "diagnostic",
        "availability_topic": topics.learner_status,
        "device": _device_learner(),
        "origin": _origin(),
    }


def mark_config(topics) -> dict:
    return {
        "name": "Mark spot here",
        "unique_id": MARK_ID,
        "default_entity_id": "button." + MARK_ID,
        "command_topic": topics.review_mark,
        "payload_press": "PRESS",
        "qos": 1,
        "retain": False,
        "icon": "mdi:map-marker-plus",
        "availability_topic": topics.learner_status,
        "device": _device_review(),
        "origin": _origin(),
    }


def name_config(topics) -> dict:
    return {
        "name": "Spot name",
        "unique_id": NAME_ID,
        "default_entity_id": "text." + NAME_ID,
        "command_topic": topics.review_name_set,
        "state_topic": topics.review_name,
        "min": 0,
        "max": 32,
        "mode": "text",
        "qos": 1,
        "retain": False,
        "icon": "mdi:form-textbox",
        "availability_topic": topics.learner_status,
        "device": _device_review(),
        "origin": _origin(),
    }


def discovery_messages(settings, topics) -> list:
    """(topic, payload bytes) for every discovery config, in the order of spec 9.2.

    With ``settings.debug_map`` False the image config is an empty payload (removes the entity).
    """
    msgs = [(topics.discovery("sensor", LEARNER_STATUS_ID), _dumps(learner_status_config(topics)))]
    if settings.debug_map:
        msgs.append((topics.discovery("image", DEBUG_MAP_ID), _dumps(debug_map_config(topics))))
    else:
        msgs.append((topics.discovery("image", DEBUG_MAP_ID), b""))
    msgs.append((topics.discovery("button", MARK_ID), _dumps(mark_config(topics))))
    msgs.append((topics.discovery("text", NAME_ID), _dumps(name_config(topics))))
    return msgs
