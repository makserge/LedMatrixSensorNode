"""MQTT discovery payloads of the P7 entities (P7 spec 7.1): two configs, both on the learner device.

========================  ===========  ==================================  ============================================
entity                    component    ``unique_id``                       topic
========================  ===========  ==================================  ============================================
Export tuning data        button       ``radar_zones_learner_export_tuning``  command ``radar_zones/learner/export_tuning``
Tuning data               sensor       ``radar_zones_learner_tuning``         state ``radar_zones/learner/tuning``
========================  ===========  ==================================  ============================================

The button is a configuration entity (``entity_category: config``) and the sensor a diagnostic one. The sensor says ``none`` or
``<n> nights``: how many nightly count lines the add-on keeps locally, never a date, a size or a value. A press makes the learner
publish one pooled counts record on a topic that is not retained (``radar_zones/learner/tuning/export``, spec 4.8); nothing
else of the tuning data leaves the add-on. Both configs use the single learner availability topic, like every other entity of
the learner device. Discovery is published not retained, in the key order below, like every config of the app.

P7 adds no per-zone entity and changes no entity of P3 to P6.
"""
from __future__ import annotations

from .discovery import _device_learner, _dumps, _origin

EXPORT_TUNING_ID = "radar_zones_learner_export_tuning"
TUNING_ID = "radar_zones_learner_tuning"


def _tail(topics) -> dict:
    return {"availability_topic": topics.learner_status, "device": _device_learner(), "origin": _origin()}


def export_tuning_config(topics) -> dict:
    cfg = {"name": "Export tuning data", "unique_id": EXPORT_TUNING_ID,
           "default_entity_id": "button." + EXPORT_TUNING_ID, "command_topic": topics.learner_export_tuning,
           "payload_press": "PRESS", "qos": 1, "retain": False, "entity_category": "config",
           "icon": "mdi:database-export-outline"}
    cfg.update(_tail(topics))
    return cfg


def tuning_config(topics) -> dict:
    cfg = {"name": "Tuning data", "unique_id": TUNING_ID, "default_entity_id": "sensor." + TUNING_ID,
           "state_topic": topics.learner_tuning, "entity_category": "diagnostic", "icon": "mdi:chart-histogram"}
    cfg.update(_tail(topics))
    return cfg


def p7_discovery_messages(topics) -> list:
    """(topic, payload bytes) of the two P7 configs: the button, then the sensor."""
    return [(topics.discovery("button", EXPORT_TUNING_ID), _dumps(export_tuning_config(topics))),
            (topics.discovery("sensor", TUNING_ID), _dumps(tuning_config(topics)))]
