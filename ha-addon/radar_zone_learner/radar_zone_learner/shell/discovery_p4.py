"""MQTT discovery payloads of the P4 entities (design 6.3), in the style of the P3 ones (discovery.py).

Learner device: 'Radar data valid' (binary sensor), 'RF on time 24 h', 'RF saving 7 d', 'Coverage 7 d'
(diagnostic sensors) and the 'Night learning' switch. Review device: the item select, 'Review pending',
'Review item text', Approve / Reject / Later and the 'Review event' entity.

No zone, room occupancy or presence entity: those come with P6 (shadow mode until then). Discovery is
published not retained, like the P3 configs, and the message order here is the order of ``p4_discovery_messages``.
"""
from __future__ import annotations

import json

from .discovery import _device_learner, _device_review, _dumps, _origin
from .p4_types import EVENT_TYPES, SELECT_OPTIONS

VALID_ID = "radar_zones_radar_valid"
RF_ON_ID = "radar_zones_scan_rf_on_24h"
RF_SAVING_ID = "radar_zones_scan_rf_saving_7d"
COVERAGE_ID = "radar_zones_learner_coverage_7d"
NIGHT_LEARNING_ID = "radar_zones_learner_night_learning"
SELECT_ID = "radar_zones_review_select"
PENDING_ID = "radar_zones_review_pending"
ITEM_TEXT_ID = "radar_zones_review_item_text"
APPROVE_ID = "radar_zones_review_approve"
REJECT_ID = "radar_zones_review_reject"
LATER_ID = "radar_zones_review_later"
EVENT_ID = "radar_zones_review_event"


def _answer_payload(op: str) -> str:
    return json.dumps({"op": op}, separators=(",", ":"))


def valid_config(topics) -> dict:
    # 'online'/'offline' is also the availability vocabulary the zone entities of P6 will use as a second
    # availability topic (design 6.3 'Radar data valid').
    return {
        "name": "Radar data valid",
        "unique_id": VALID_ID,
        "default_entity_id": "binary_sensor." + VALID_ID,
        "state_topic": topics.radar_valid,
        "payload_on": "online",
        "payload_off": "offline",
        "entity_category": "diagnostic",
        "icon": "mdi:radar",
        "availability_topic": topics.learner_status,
        "device": _device_learner(),
        "origin": _origin(),
    }


def _sensor(topics, uid: str, name: str, topic: str, unit: str, extra: dict) -> dict:
    cfg = {
        "name": name,
        "unique_id": uid,
        "default_entity_id": "sensor." + uid,
        "state_topic": topic,
        "unit_of_measurement": unit,
        "state_class": "measurement",
    }
    cfg.update(extra)
    cfg.update({
        "entity_category": "diagnostic",
        "availability_topic": topics.learner_status,
        "device": _device_learner(),
        "origin": _origin(),
    })
    return cfg


def rf_on_config(topics) -> dict:
    return _sensor(topics, RF_ON_ID, "RF on time 24 h", topics.rf_on_24h, "h",
                   {"device_class": "duration", "suggested_display_precision": 1, "icon": "mdi:radar"})


def rf_saving_config(topics) -> dict:
    return _sensor(topics, RF_SAVING_ID, "RF saving 7 d", topics.rf_saving_7d, "%",
                   {"suggested_display_precision": 0, "icon": "mdi:radar"})


def coverage_config(topics) -> dict:
    return _sensor(topics, COVERAGE_ID, "Coverage 7 d", topics.coverage_7d, "%",
                   {"suggested_display_precision": 0, "icon": "mdi:radar"})


def night_learning_config(topics) -> dict:
    return {
        "name": "Night learning",
        "unique_id": NIGHT_LEARNING_ID,
        "default_entity_id": "switch." + NIGHT_LEARNING_ID,
        "command_topic": topics.night_learning_set,
        "state_topic": topics.night_learning_state,
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "optimistic": False,
        "qos": 1,
        "retain": False,
        "entity_category": "config",
        "icon": "mdi:weather-night",
        "availability_topic": topics.learner_status,
        "device": _device_learner(),
        "origin": _origin(),
    }


def select_config(topics) -> dict:
    return {
        "name": "Review item",
        "unique_id": SELECT_ID,
        "default_entity_id": "select." + SELECT_ID,
        "command_topic": topics.review_select_set,
        "state_topic": topics.review_select_state,
        "options": list(SELECT_OPTIONS),
        "optimistic": False,
        "qos": 1,
        "retain": False,
        "icon": "mdi:format-list-checks",
        "availability_topic": topics.learner_status,
        "device": _device_review(),
        "origin": _origin(),
    }


def pending_config(topics) -> dict:
    return {
        "name": "Review pending",
        "unique_id": PENDING_ID,
        "default_entity_id": "sensor." + PENDING_ID,
        "state_topic": topics.review_pending,
        "json_attributes_topic": topics.review_pending_attr,
        "icon": "mdi:clipboard-list-outline",
        "availability_topic": topics.learner_status,
        "device": _device_review(),
        "origin": _origin(),
    }


def item_text_config(topics) -> dict:
    return {
        "name": "Review item text",
        "unique_id": ITEM_TEXT_ID,
        "default_entity_id": "sensor." + ITEM_TEXT_ID,
        "state_topic": topics.review_item_text,
        "icon": "mdi:text-box-outline",
        "availability_topic": topics.learner_status,
        "device": _device_review(),
        "origin": _origin(),
    }


def _answer_button(topics, uid: str, name: str, op: str, icon: str) -> dict:
    return {
        "name": name,
        "unique_id": uid,
        "default_entity_id": "button." + uid,
        "command_topic": topics.review_answer,
        "payload_press": _answer_payload(op),
        "qos": 1,
        "retain": False,
        "icon": icon,
        "availability_topic": topics.learner_status,
        "device": _device_review(),
        "origin": _origin(),
    }


def event_config(topics) -> dict:
    return {
        "name": "Review event",
        "unique_id": EVENT_ID,
        "default_entity_id": "event." + EVENT_ID,
        "state_topic": topics.review_event,
        "event_types": list(EVENT_TYPES),
        "icon": "mdi:alert-circle-outline",
        "availability_topic": topics.learner_status,
        "device": _device_review(),
        "origin": _origin(),
    }


def p4_discovery_messages(topics) -> list:
    """(topic, payload bytes) of every P4 config, learner device first."""
    return [
        (topics.discovery("binary_sensor", VALID_ID), _dumps(valid_config(topics))),
        (topics.discovery("sensor", RF_ON_ID), _dumps(rf_on_config(topics))),
        (topics.discovery("sensor", RF_SAVING_ID), _dumps(rf_saving_config(topics))),
        (topics.discovery("sensor", COVERAGE_ID), _dumps(coverage_config(topics))),
        (topics.discovery("switch", NIGHT_LEARNING_ID), _dumps(night_learning_config(topics))),
        (topics.discovery("select", SELECT_ID), _dumps(select_config(topics))),
        (topics.discovery("sensor", PENDING_ID), _dumps(pending_config(topics))),
        (topics.discovery("sensor", ITEM_TEXT_ID), _dumps(item_text_config(topics))),
        (topics.discovery("button", APPROVE_ID), _dumps(_answer_button(topics, APPROVE_ID, "Approve", "approve",
                                                                         "mdi:check"))),
        (topics.discovery("button", REJECT_ID), _dumps(_answer_button(topics, REJECT_ID, "Reject", "reject",
                                                                        "mdi:close"))),
        (topics.discovery("button", LATER_ID), _dumps(_answer_button(topics, LATER_ID, "Later", "later",
                                                                       "mdi:clock-outline"))),
        (topics.discovery("event", EVENT_ID), _dumps(event_config(topics))),
    ]
