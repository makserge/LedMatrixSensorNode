"""MQTT discovery payloads of the P5 entities (P5 spec 8.2): nineteen configs, eleven on the review device and eight on the
learner device, with full key names and in the key order of the spec (three of them are pinned byte for byte).

Review device: the review map and the push map (images), the Advanced select, the buttons Rename and Apply advanced,
'Show me live', 'You are in', the two ``time`` entities of 'Edit times', the box 'Approve also switches schedule ON' and
the diagnostic sensor that carries the push request.
Learner device: 'Forget all learned data' with its confirmation text, 'Forget rejections', 'Re-suggest night window',
and the switches 'Guests present - pause learning', 'Pets in home', 'Detailed review text' and 'Debug capture'.

No zone entity and no occupancy entity: those come with P6 (shadow mode until then). With the option ``debug_map``
false the two image configs are empty payloads (they remove the entities), as the debug map's is (spec D33).
"""
from __future__ import annotations

import json

from .discovery import _device_learner, _device_review, _dumps, _origin
from .select_model import NO_ENTRY

MAP_ID = "radar_zones_review_map"
PUSH_MAP_ID = "radar_zones_review_push_map"
ADVANCED_ID = "radar_zones_review_advanced"
RENAME_ID = "radar_zones_review_rename"
APPLY_ADVANCED_ID = "radar_zones_review_apply_advanced"
LIVE_ID = "radar_zones_review_show_live"
YOU_ARE_IN_ID = "radar_zones_review_you_are_in"
NW_START_ID = "radar_zones_review_nw_start"
NW_END_ID = "radar_zones_review_nw_end"
APPROVE_SCHED_ID = "radar_zones_review_approve_sched"
PUSH_ID = "radar_zones_review_push"
FORGET_ALL_ID = "radar_zones_learner_forget_all"
FORGET_CONFIRM_ID = "radar_zones_learner_forget_confirm"
FORGET_REJECTIONS_ID = "radar_zones_learner_forget_rejections"
RESUGGEST_ID = "radar_zones_learner_resuggest_night_window"
GUESTS_PAUSE_ID = "radar_zones_learner_guests_pause"
PETS_ID = "radar_zones_learner_pets"
DETAIL_TEXT_ID = "radar_zones_learner_detail_text"
DEBUG_CAPTURE_ID = "radar_zones_learner_debug_capture"

EVENT_TYPES_P5 = ("mark_failed", "device_not_confirmed", "restore_failed", "answer_ignored", "forget_refused",
                  "forgotten")


def _press(op: str, item=None) -> str:
    obj = {"op": op} if item is None else {"op": op, "item": item}
    return json.dumps(obj, separators=(",", ":"))


def _tail(topics, device: dict) -> dict:
    return {"availability_topic": topics.learner_status, "device": device, "origin": _origin()}


def _image(topics, uid: str, name: str, topic: str, diagnostic: bool) -> dict:
    cfg = {"name": name, "unique_id": uid, "default_entity_id": "image." + uid, "image_topic": topic,
           "content_type": "image/png"}
    if diagnostic:
        cfg["entity_category"] = "diagnostic"
    cfg.update(_tail(topics, _device_review()))
    return cfg


def map_config(topics) -> dict:
    return _image(topics, MAP_ID, "Review map", topics.review_map, False)


def push_map_config(topics) -> dict:
    return _image(topics, PUSH_MAP_ID, "Review push map", topics.review_push_map, True)


def advanced_config(topics, options=None) -> dict:
    cfg = {"name": "Advanced item", "unique_id": ADVANCED_ID, "default_entity_id": "select." + ADVANCED_ID,
           "command_topic": topics.review_advanced_set, "state_topic": topics.review_advanced_state,
           "options": list([NO_ENTRY] if options is None else options), "optimistic": False, "qos": 1,
           "retain": False, "icon": "mdi:tools"}
    cfg.update(_tail(topics, _device_review()))
    return cfg


def _button(topics, uid: str, name: str, payload: str, icon: str, device: dict, config: bool) -> dict:
    cfg = {"name": name, "unique_id": uid, "default_entity_id": "button." + uid,
           "command_topic": topics.review_answer, "payload_press": payload, "qos": 1, "retain": False}
    if config:
        cfg["entity_category"] = "config"
    cfg["icon"] = icon
    cfg.update(_tail(topics, device))
    return cfg


def _switch(topics, uid: str, name: str, command: str, state: str, icon: str, device: dict, config: bool) -> dict:
    cfg = {"name": name, "unique_id": uid, "default_entity_id": "switch." + uid, "command_topic": command,
           "state_topic": state, "payload_on": "ON", "payload_off": "OFF", "state_on": "ON", "state_off": "OFF",
           "optimistic": False, "qos": 1, "retain": False}
    if config:
        cfg["entity_category"] = "config"
    cfg["icon"] = icon
    cfg.update(_tail(topics, device))
    return cfg


def live_config(topics) -> dict:
    return _switch(topics, LIVE_ID, "Show me live", topics.review_live_set, topics.review_live_state,
                   "mdi:eye-outline", _device_review(), False)


def you_are_in_config(topics) -> dict:
    cfg = {"name": "You are in", "unique_id": YOU_ARE_IN_ID, "default_entity_id": "sensor." + YOU_ARE_IN_ID,
           "state_topic": topics.review_you_are_in, "json_attributes_topic": topics.review_you_are_in_attr,
           "icon": "mdi:crosshairs-gps"}
    cfg.update(_tail(topics, _device_review()))
    return cfg


def _time(topics, uid: str, name: str, command: str, state: str, icon: str) -> dict:
    cfg = {"name": name, "unique_id": uid, "default_entity_id": "time." + uid, "command_topic": command,
           "state_topic": state, "optimistic": False, "qos": 1, "retain": False, "icon": icon}
    cfg.update(_tail(topics, _device_review()))
    return cfg


def nw_start_config(topics) -> dict:
    return _time(topics, NW_START_ID, "Night window start (edit)", topics.review_nw_start_set, topics.review_nw_start,
                 "mdi:clock-start")


def nw_end_config(topics) -> dict:
    return _time(topics, NW_END_ID, "Night window end (edit)", topics.review_nw_end_set, topics.review_nw_end,
                 "mdi:clock-end")


def approve_sched_config(topics) -> dict:
    return _switch(topics, APPROVE_SCHED_ID, "Approve also switches schedule ON", topics.review_approve_sched_set,
                   topics.review_approve_sched, "mdi:calendar-check", _device_review(), True)


def push_config(topics) -> dict:
    cfg = {"name": "Review push request", "unique_id": PUSH_ID, "default_entity_id": "sensor." + PUSH_ID,
           "state_topic": topics.review_push, "entity_category": "diagnostic", "icon": "mdi:bell-badge-outline"}
    cfg.update(_tail(topics, _device_review()))
    return cfg


def forget_all_config(topics) -> dict:
    return _button_with_topic(topics, FORGET_ALL_ID, "Forget all learned data", topics.learner_forget_all, "PRESS",
                              "mdi:delete-forever")


def _button_with_topic(topics, uid: str, name: str, command: str, payload: str, icon: str) -> dict:
    cfg = {"name": name, "unique_id": uid, "default_entity_id": "button." + uid, "command_topic": command,
           "payload_press": payload, "qos": 1, "retain": False, "entity_category": "config", "icon": icon}
    cfg.update(_tail(topics, _device_learner()))
    return cfg


def forget_confirm_config(topics) -> dict:
    cfg = {"name": "Forget all confirmation", "unique_id": FORGET_CONFIRM_ID,
           "default_entity_id": "text." + FORGET_CONFIRM_ID, "command_topic": topics.learner_forget_confirm_set,
           "state_topic": topics.learner_forget_confirm, "min": 0, "max": 16, "mode": "text", "qos": 1,
           "retain": False, "entity_category": "config", "icon": "mdi:shield-alert-outline"}
    cfg.update(_tail(topics, _device_learner()))
    return cfg


def forget_rejections_config(topics) -> dict:
    return _button(topics, FORGET_REJECTIONS_ID, "Forget rejections", _press("forget"), "mdi:thumb-down-outline",
                   _device_learner(), True)


def resuggest_config(topics) -> dict:
    return _button(topics, RESUGGEST_ID, "Re-suggest night window", _press("undo", "night_window"), "mdi:refresh",
                   _device_learner(), True)


def guests_pause_config(topics) -> dict:
    return _switch(topics, GUESTS_PAUSE_ID, "Guests present - pause learning", topics.learner_guests_pause_set,
                   topics.learner_guests_pause, "mdi:account-group", _device_learner(), True)


def pets_config(topics) -> dict:
    return _switch(topics, PETS_ID, "Pets in home", topics.learner_pets_set, topics.learner_pets, "mdi:paw",
                   _device_learner(), True)


def detail_text_config(topics) -> dict:
    return _switch(topics, DETAIL_TEXT_ID, "Detailed review text", topics.learner_detail_text_set,
                   topics.learner_detail_text, "mdi:text-box-search-outline", _device_learner(), True)


def debug_capture_config(topics) -> dict:
    return _switch(topics, DEBUG_CAPTURE_ID, "Debug capture", topics.learner_debug_capture_set,
                   topics.learner_debug_capture, "mdi:record-rec", _device_learner(), True)


def advanced_discovery_message(topics, options=None) -> tuple:
    """The Advanced select's config alone: published again when its option list changes (spec 2.6)."""
    return topics.discovery("select", ADVANCED_ID), _dumps(advanced_config(topics, options))


def p5_discovery_messages(topics, debug_map: bool = True, advanced_options=None) -> list:
    """(topic, payload bytes) of the nineteen P5 configs, review device first. With ``debug_map`` False the two image
    configs are empty payloads (they remove the entities)."""
    image = (lambda cfg: _dumps(cfg)) if debug_map else (lambda cfg: b"")
    return [
        (topics.discovery("image", MAP_ID), image(map_config(topics))),
        (topics.discovery("image", PUSH_MAP_ID), image(push_map_config(topics))),
        advanced_discovery_message(topics, advanced_options),
        (topics.discovery("button", RENAME_ID),
         _dumps(_button(topics, RENAME_ID, "Rename", _press("rename"), "mdi:rename-box", _device_review(), False))),
        (topics.discovery("button", APPLY_ADVANCED_ID),
         _dumps(_button(topics, APPLY_ADVANCED_ID, "Apply advanced", _press("advanced"), "mdi:check-all",
                        _device_review(), False))),
        (topics.discovery("switch", LIVE_ID), _dumps(live_config(topics))),
        (topics.discovery("sensor", YOU_ARE_IN_ID), _dumps(you_are_in_config(topics))),
        (topics.discovery("time", NW_START_ID), _dumps(nw_start_config(topics))),
        (topics.discovery("time", NW_END_ID), _dumps(nw_end_config(topics))),
        (topics.discovery("switch", APPROVE_SCHED_ID), _dumps(approve_sched_config(topics))),
        (topics.discovery("sensor", PUSH_ID), _dumps(push_config(topics))),
        (topics.discovery("button", FORGET_ALL_ID), _dumps(forget_all_config(topics))),
        (topics.discovery("text", FORGET_CONFIRM_ID), _dumps(forget_confirm_config(topics))),
        (topics.discovery("button", FORGET_REJECTIONS_ID), _dumps(forget_rejections_config(topics))),
        (topics.discovery("button", RESUGGEST_ID), _dumps(resuggest_config(topics))),
        (topics.discovery("switch", GUESTS_PAUSE_ID), _dumps(guests_pause_config(topics))),
        (topics.discovery("switch", PETS_ID), _dumps(pets_config(topics))),
        (topics.discovery("switch", DETAIL_TEXT_ID), _dumps(detail_text_config(topics))),
        (topics.discovery("switch", DEBUG_CAPTURE_ID), _dumps(debug_capture_config(topics))),
    ]
