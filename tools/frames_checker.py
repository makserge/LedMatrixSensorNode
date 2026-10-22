#!/usr/bin/env python3
"""Broker-side checker for the Led Matrix Node P1 verification (frames stream, legacy radar topics,
discovery configs). It only subscribes; it never publishes.

Checks, over --duration seconds (default 24 h):
  * led_matrix_node/radar/ld2454/frames: JSON validity and field types (P1 spec section 1.1), payload
    <= 2048 bytes, QoS 0 and not retained (every message: MQTT 5 retain-as-published), seq contiguity
    per boot id (s == previous s + n) with every gap logged, t not going back, message rates, and no
    silence while the node is online (at most 6 s between two messages by the node's t, and a message
    within 30 s by the checker's clock, which also catches a stream that stops for good);
  * led_matrix_node/radar/ld2412: '0' / '1', on change plus a 60 s heartbeat (silence <= 65 s between
    two messages, and a message within 90 s by the checker's clock);
  * led_matrix_node/radar/ld2454 (legacy 1 Hz JSON): silent in P1;
  * every discovery config of the device under homeassistant/+/+/config parses, and the four sensor
    configs are byte-identical to the pre-P1 firmware.
An OTA pause of the node shows as a frames silence, so do OTA tests outside a checker run.
It prints gaps and errors as they happen, an interim summary every --report-every seconds and a final
summary; the exit code is 0 if no check failed, 1 if one did, 2 on a usage or setup error.

Needs Python 3.8+, paho-mqtt 2.x (pip install "paho-mqtt>=2,<3") and an MQTT 5 broker (Mosquitto 1.6 or
later). The password is read from the environment variable named by --password-env (default
MQTT_PASSWORD), never from the command line; in bash, so that it stays out of the shell history:
  read -rs MQTT_PASSWORD; export MQTT_PASSWORD
  python3 tools/frames_checker.py --host 192.168.1.10 --user node_checker
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime

FRAMES_MAX_BYTES = 2048         # the node's static payload buffer (P1 spec 1.6)
DISCOVERY_MAX_BYTES = 511       # DISCOVERY_JSON_MAX 512 incl. the NUL; larger configs are never sent
MAX_LISTED = 20                 # LD2454_STREAM_MAX_FRAMES
HEARTBEAT_LD2412_S = 60.0       # LD2412_HEARTBEAT_MS
HEARTBEAT_SLACK_S = 5.0
FRAMES_SPACING_MAX_MS = 6000    # node t between two frames messages: LD2454_STREAM_IDLE_MS 5000 + 1 s slack
# Checker clock without any message while the node counts as online. When the node's link dies, its will
# ('offline') comes only after keepalive 15 s x 1.5 = 22.5 s, up to 5 s after its last frames message:
# shorter limits would fail every outage before the will explains it.
FRAMES_SILENCE_MAX_S = 30.0     # heartbeat 5 s
LD2412_SILENCE_MAX_S = 90.0     # heartbeat 60 s
CHECKER_OFFLINE_MAX_S = 60.0    # checker without a broker connection: nothing is being checked
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BUSY_RATE_RANGE = (0.5, 2.5)    # frames messages/s in a minute whose frames are mostly target frames
IDLE_RATE_MAX = 0.5             # frames messages/s in a minute without target frames (expected 0.2)
PEAK_10S_MAX = 30               # frames messages in any 10 s window (design peak: 21)
ONSET_MS = 500                  # first target frame parsed >= 500 ms after the previous target batch = onset
ONSET_AGE_P95_MS = 80           # its first target frame is published within 80 ms of being parsed
TODAY_UPLINK_PER_S = 2.1        # pre-P1 node uplink while occupied: 1 Hz LD2454 + 1 Hz LD2412 + 0.1 Hz state
UPLINK_SLACK = 1.05
SM_CODES = {"A", "N", "F", "X", "S"}
FRAME_KEYS = ("v", "b", "t", "s", "n", "a0", "a1", "mt", "p2", "pw", "sm", "f")
BOOT_ID_RE = re.compile(r"^[0-9a-f]{8}$")
U32 = 0xFFFFFFFF

# The four sensor discovery configs exactly as the pre-P1 firmware publishes them (P1 spec 3.3).
_SENSOR_TPL = ('{{"name":"{name}","stat_t":"led_matrix_node/sensors/state",'
               '"avty_t":"led_matrix_node/sensors/status","unit_of_meas":"{unit}","dev_cla":"{dev_cla}",'
               '"stat_cla":"measurement","exp_aft":30,'
               '"val_tpl":"{{{{ value_json.{id} if \'{id}\' in value_json else None }}}}",'
               '"uniq_id":"Led_Matrix_Node_{id}","dev":{{"ids":"Led_Matrix_Node","name":"Led Matrix Node",'
               '"mf":"","mdl":""}}}}')
EXPECTED_SENSOR_CONFIGS = {
    "homeassistant/sensor/Led_Matrix_Node_" + i: _SENSOR_TPL.format(id=i, name=n, unit=u, dev_cla=d).encode("utf-8")
    for i, n, u, d in (("co2", "CO2", "ppm", "carbon_dioxide"),
                       ("temp", "Temperature", "°C", "temperature"),
                       ("hum", "Humidity", "%", "humidity"),
                       ("lux", "Illuminance", "lx", "illuminance"))
}
EXPECTED_SENSOR_CONFIGS = {topic + "/config": payload for topic, payload in EXPECTED_SENSOR_CONFIGS.items()}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_frames(obj, phase="p1"):
    """Field and type checks of one frames payload (P1 spec 1.1). Returns a list of error strings."""
    errors = []
    if not isinstance(obj, dict):
        return ["payload is not a JSON object"]
    for key in obj:
        if key not in FRAME_KEYS:
            errors.append(f"unknown key {key!r}")
    for key in ("v", "b", "t", "s", "n", "pw", "sm"):
        if key not in obj:
            errors.append(f"missing {key!r}")
    if errors:
        return errors
    if obj["v"] != 1 or not _is_int(obj["v"]):
        errors.append(f"v must be 1, got {obj['v']!r}")
    if not isinstance(obj["b"], str) or not BOOT_ID_RE.match(obj["b"]):
        errors.append(f"b must be 8 lowercase hex digits, got {obj['b']!r}")
    if not _is_int(obj["t"]) or obj["t"] < 0:
        errors.append(f"t must be an integer >= 0, got {obj['t']!r}")
    for key in ("s", "n"):
        if not _is_int(obj[key]) or not 0 <= obj[key] <= U32:
            errors.append(f"{key} must be a uint32, got {obj[key]!r}")
    if not _is_int(obj["pw"]) or not 0 <= obj["pw"] <= 3:
        errors.append(f"pw must be 0..3, got {obj['pw']!r}")
    if not isinstance(obj["sm"], str) or obj["sm"] not in SM_CODES:
        errors.append(f"sm must be one of {sorted(SM_CODES)}, got {obj['sm']!r}")
    for key in ("mt", "p2"):
        if key in obj:
            if obj[key] not in (0, 1) or not _is_int(obj[key]):
                errors.append(f"{key} must be 0 or 1, got {obj[key]!r}")
        elif phase == "p1":
            errors.append(f"missing {key!r}")
    if phase == "p1" and (obj["pw"] != 3 or obj["sm"] != "A"):
        errors.append(f"P1 sends pw 3 and sm 'A', got pw {obj['pw']!r}, sm {obj['sm']!r}")
    if errors:
        return errors

    n = obj["n"]
    has_ages = "a0" in obj or "a1" in obj
    if n == 0:
        if has_ages:
            errors.append("a0/a1 present with n 0")
        if "f" in obj:
            errors.append("f present with n 0")
        return errors
    if not ("a0" in obj and "a1" in obj):
        return errors + ["a0 and a1 are required when n > 0"]
    a0, a1 = obj["a0"], obj["a1"]
    if not (_is_int(a0) and _is_int(a1) and 0 <= a1 <= a0 <= U32):
        return errors + [f"need integers 0 <= a1 <= a0, got a0 {a0!r}, a1 {a1!r}"]
    if n == 1 and a0 != a1:
        return errors + [f"n 1 covers one frame, so a0 must equal a1, got a0 {a0}, a1 {a1}"]
    if "f" not in obj:
        return errors
    f = obj["f"]
    if not isinstance(f, list) or not 1 <= len(f) <= MAX_LISTED:
        return errors + [f"f must be a list of 1..{MAX_LISTED} entries"]
    prev_k, prev_age = -1, None
    for i, entry in enumerate(f):
        where = f"f[{i}]"
        if not isinstance(entry, list) or len(entry) not in (6, 10, 14) or not all(_is_int(x) for x in entry):
            errors.append(f"{where} must be [k, age] + 1..3 x [slot, x, y, v], all integers")
            continue
        k, age = entry[0], entry[1]
        if not prev_k < k < n:
            errors.append(f"{where}: k {k} must increase and stay below n {n}")
        if not a1 <= age <= a0:
            errors.append(f"{where}: age {age} outside a1..a0 ({a1}..{a0})")
        if prev_age is not None and age > prev_age:
            errors.append(f"{where}: age {age} older than the previous entry's {prev_age}")
        if k == 0 and age != a0:
            errors.append(f"{where}: frame k 0 has age {age}, a0 is {a0}")
        if k == n - 1 and age != a1:
            errors.append(f"{where}: frame k n-1 has age {age}, a1 is {a1}")
        prev_k, prev_age = k, age
        prev_slot = -1
        for j in range(2, len(entry), 4):
            slot, x, y, v = entry[j:j + 4]
            if not prev_slot < slot <= 2:
                errors.append(f"{where}: slots must increase within 0..2, got {slot}")
            prev_slot = slot
            if not all(-32767 <= c <= 32767 for c in (x, y, v)):
                errors.append(f"{where}: slot {slot} x/y/v outside -32767..32767")
    return errors


class FrameStreamChecker:
    """The analysis, independent of paho: feed it every message with its receive times."""

    def __init__(self, node_prefix="led_matrix_node", device_id="Led_Matrix_Node",
                 discovery_prefix="homeassistant", phase="p1"):
        self.phase = phase
        self.device_id = device_id
        self.discovery_prefix = discovery_prefix
        self.t_frames = f"{node_prefix}/radar/ld2454/frames"
        self.t_ld2412 = f"{node_prefix}/radar/ld2412"
        self.t_ld2454 = f"{node_prefix}/radar/ld2454"
        self.t_status = f"{node_prefix}/sensors/status"
        self.t_state = f"{node_prefix}/sensors/state"
        self.uplink_topics = {self.t_frames, self.t_ld2412, self.t_ld2454, self.t_status, self.t_state}
        self.start_wall = None
        self.start_mono = None
        self.last_mono = None
        self.epoch = 0                    # +1 on every node status message and checker (re)connect
        self.checker_connects = 0
        self.failures = []                # (wall, text); counted in full, the first 200 kept
        self.failure_count = 0
        self.warnings = []
        self.warning_count = 0
        self.boots = {}                   # boot id -> stream state
        self.boot_order = []
        self.gaps = []
        self.minutes = {}                 # int(mono // 60) -> counters
        self.window10 = deque()
        self.peak10 = 0
        self.frames_msgs = 0
        self.frames_bytes_max = 0
        self.frames_bytes_sum = 0
        self.frames_covered = 0
        self.frames_listed = 0
        self.onset_ages = []              # f[0] age of onset batches: parse -> publish on the node, ms
        self.discovery = {}               # topic -> dict
        self.ld2412 = {"count": 0, "changes": 0, "last": None, "last_mono": None, "last_epoch": None,
                       "max_gap": 0.0, "retained_seen": None}
        self.ld2454_legacy = 0
        self.status = []                  # (wall, payload, retained)
        self.event_log = []               # JSON lines for --gap-log
        # silences: checked on every message and on tick(), so a stream that stops for good is caught too
        self.connected = False
        self.disconnected_mono = None     # when the checker lost the broker (None: never connected yet)
        self.offline_reported = False
        self.node_status = None           # last payload on the status topic, retained or live
        self.frames_ref_mono = None       # last frames message, checker connect or node status change
        self.frames_silence_open = False  # the current frames silence was already reported
        self.frames_silences = 0
        self.frames_silence_max = 0.0     # longest frames silence seen while the node was online, s
        self.frames_spacing_max = 0       # largest node t between two frames messages in one epoch, ms
        self.frames_spacing_fails = 0
        self.ld2412_ref_mono = None
        self.ld2412_silence_open = False
        self.ld2412_silences = 0

    # ---------------------------------------------------------------- helpers
    def _fail(self, wall, text):
        self.failure_count += 1
        if len(self.failures) < 200:
            self.failures.append((wall, text))
        self.event_log.append({"time": _iso(wall), "level": "FAIL", "text": text})
        return f"{_iso(wall)} FAIL {text}"

    def _warn(self, wall, text):
        self.warning_count += 1
        if len(self.warnings) < 200:
            self.warnings.append((wall, text))
        self.event_log.append({"time": _iso(wall), "level": "WARN", "text": text})
        return f"{_iso(wall)} WARN {text}"

    def _minute(self, mono):
        key = int(mono // 60)
        m = self.minutes.get(key)
        if m is None:
            m = self.minutes[key] = {"frames": 0, "target_msgs": 0, "covered": 0, "listed": 0, "uplink": 0}
        return m

    # ---------------------------------------------------------------- connection events
    def on_connect(self, wall, mono):
        if self.start_wall is None:
            self.start_wall, self.start_mono = wall, mono
        self.checker_connects += 1
        self.epoch += 1
        self.connected = True
        self.offline_reported = False
        self.frames_ref_mono = self.ld2412_ref_mono = mono   # silences count from the (re)connect

    def on_disconnect(self, wall, mono):
        self.epoch += 1
        if not self.connected:
            return []                     # a failed connect attempt: offline since disconnected_mono
        self.connected = False
        self.disconnected_mono = mono
        return [self._warn(wall, "checker lost its broker connection (frames meanwhile are not checked)")]

    def tick(self, wall, mono):
        """Call about once a second: finds silences even when no message arrives at all."""
        if self.start_mono is not None:
            self.last_mono = mono if self.last_mono is None else max(self.last_mono, mono)
        return self._check_silences(wall, mono)

    def _check_silences(self, wall, mono):
        out = []
        if not self.connected:
            if (self.disconnected_mono is not None and not self.offline_reported
                    and mono - self.disconnected_mono > CHECKER_OFFLINE_MAX_S):
                self.offline_reported = True
                out.append(self._fail(wall, f"checker without a broker connection for over "
                                            f"{CHECKER_OFFLINE_MAX_S:.0f} s: the node is not being checked"))
            return out
        if self.node_status == "offline":
            return out                    # the node is gone (LWT); its 'online' restarts the clocks
        silence = mono - self.frames_ref_mono
        self.frames_silence_max = max(self.frames_silence_max, silence)
        if silence > FRAMES_SILENCE_MAX_S and not self.frames_silence_open:
            self.frames_silence_open = True
            self.frames_silences += 1
            out.append(self._fail(wall, f"frames stream stopped: no frames message for {silence:.0f} s while the "
                                        f"node is online (heartbeat 5 s)"))
        silence = mono - self.ld2412_ref_mono
        if silence > LD2412_SILENCE_MAX_S and not self.ld2412_silence_open:
            self.ld2412_silence_open = True
            self.ld2412_silences += 1
            out.append(self._fail(wall, f"LD2412 silent for {silence:.0f} s while the node is online "
                                        f"(heartbeat {HEARTBEAT_LD2412_S:.0f} s)"))
        return out

    # ---------------------------------------------------------------- messages
    def on_message(self, topic, payload, retain, qos, wall, mono):
        if self.start_wall is None:
            self.start_wall, self.start_mono = wall, mono
        self.last_mono = mono if self.last_mono is None else max(self.last_mono, mono)
        out = self._check_silences(wall, mono)
        if topic in self.uplink_topics and not retain:
            self._minute(mono)["uplink"] += 1
        if topic == self.t_frames:
            return out + self._on_frames(payload, retain, qos, wall, mono)
        if topic == self.t_ld2412:
            return out + self._on_ld2412(payload, retain, wall, mono)
        if topic == self.t_ld2454:
            if retain:
                return out + [self._warn(wall, "a retained legacy LD2454 JSON is on the broker (stale, from old "
                                               "firmware)")]
            self.ld2454_legacy += 1
            if self.phase == "p1":
                return out + [self._fail(wall, "legacy led_matrix_node/radar/ld2454 JSON received; P1 does not "
                                               "send it")]
            return out
        if topic == self.t_status:
            text = payload.decode("utf-8", "replace")
            self.status.append((wall, text, retain))
            if not retain:
                self.epoch += 1
            # the node (re)connected or is gone: its silences count from here
            self.frames_ref_mono = self.ld2412_ref_mono = mono
            self.frames_silence_open = self.ld2412_silence_open = False
            self.node_status = text
            return out
        if topic.startswith(self.discovery_prefix + "/") and topic.endswith("/config"):
            return out + self._on_discovery(topic, payload, wall)
        return out

    def _on_frames(self, payload, retain, qos, wall, mono):
        out = []
        size = len(payload)
        if retain:
            return [self._fail(wall, "frames message is retained; it must be published with retain false")]
        reported_silence = self.frames_silence_open
        if reported_silence:
            out.append(f"{_iso(wall)} INFO frames stream resumed after {mono - self.frames_ref_mono:.0f} s")
        self.frames_ref_mono = mono
        self.frames_silence_open = False
        if qos != 0:
            out.append(self._fail(wall, f"frames message arrived with QoS {qos}; the node must publish QoS 0"))
        self.frames_msgs += 1
        self.frames_bytes_sum += size
        self.frames_bytes_max = max(self.frames_bytes_max, size)
        if size > FRAMES_MAX_BYTES:
            out.append(self._fail(wall, f"frames payload is {size} B, over {FRAMES_MAX_BYTES} B"))
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            out.append(self._fail(wall, f"frames payload is not valid JSON ({exc}): {payload[:80]!r}"))
            return out
        errors = validate_frames(obj, self.phase)
        if errors:
            out.append(self._fail(wall, "frames payload: " + "; ".join(errors[:5]) + f" -- {payload[:120]!r}"))
            return out

        listed = len(obj.get("f", []))
        m = self._minute(mono)
        m["frames"] += 1
        m["covered"] += obj["n"]
        m["listed"] += listed
        if listed:
            m["target_msgs"] += 1
        self.frames_covered += obj["n"]
        self.frames_listed += listed
        self.window10.append(mono)
        while self.window10 and mono - self.window10[0] > 10.0:
            self.window10.popleft()
        if len(self.window10) > self.peak10:
            self.peak10 = len(self.window10)
            if self.peak10 == PEAK_10S_MAX + 1:
                out.append(self._fail(wall, f"more than {PEAK_10S_MAX} frames messages within 10 s"))

        b = obj["b"]
        st = self.boots.get(b)
        if st is None:
            if self.boot_order:
                prev = self.boots[self.boot_order[-1]]
                prev["ended"] = True
                out.append(f"{_iso(wall)} INFO new boot id {b} (previous {self.boot_order[-1]}): node restarted")
            else:
                out.append(f"{_iso(wall)} INFO first frames message: boot id {b}, s {obj['s']}")
            st = self.boots[b] = {"first_wall": wall, "first_s": obj["s"], "msgs": 0, "covered": 0,
                                  "gaps": 0, "missing": 0, "ended": False, "last_target_t": None,
                                  "prev_listed": None}
            self.boot_order.append(b)
        else:
            if st["ended"]:
                out.append(self._fail(wall, f"boot id {b} came back after a newer boot id: two nodes, or a reused id"))
                st["ended"] = False
            expected = st["s"] + st["n"]
            if obj["s"] > expected:
                missing = obj["s"] - expected
                explained = st["epoch"] != self.epoch
                gap = {"time": _iso(wall), "boot": b, "expected_s": expected, "got_s": obj["s"],
                       "missing_frames": missing, "approx_s": round(missing / 10.0, 1),
                       "explained": explained,
                       "context": "node or checker reconnected in between" if explained
                                  else "no reconnect seen: ring overflow or failed publish on the node"}
                self.gaps.append(gap)
                self.event_log.append({"time": _iso(wall), "level": "GAP", **gap})
                st["gaps"] += 1
                st["missing"] += missing
                text = (f"GAP boot {b}: expected s {expected}, got {obj['s']} ({missing} frames, "
                        f"~{missing / 10.0:.1f} s); {gap['context']}")
                if explained:
                    out.append(f"{_iso(wall)} {text}")
                else:
                    out.append(self._fail(wall, text))
            elif obj["s"] < expected:
                out.append(self._fail(wall, f"seq went back in boot {b}: expected s {expected}, got {obj['s']}"))
            if obj["t"] < st["t"]:
                out.append(self._fail(wall, f"t went back in boot {b}: {st['t']} -> {obj['t']}"))
            # cadence by the node's own clock (no network jitter): a heartbeat at least every 5 s
            spacing = obj["t"] - st["t"]
            if st["epoch"] == self.epoch and spacing > 0:
                self.frames_spacing_max = max(self.frames_spacing_max, spacing)
                if spacing > FRAMES_SPACING_MAX_MS and not reported_silence:
                    self.frames_spacing_fails += 1
                    out.append(self._fail(wall, f"frames cadence: {spacing} ms between two frames messages on the "
                                                f"node (boot {b}, by t), limit {FRAMES_SPACING_MAX_MS} ms: MqttTask "
                                                f"blocked, or the node paused (OTA)"))
            # onset latency, only on an unbroken stream (no reconnect, no gap since the previous message)
            if listed and st["epoch"] == self.epoch and obj["s"] == expected:
                lt = st["last_target_t"]
                parsed_t = obj["t"] - obj["f"][0][1]      # when the node parsed the first target frame
                if (lt is not None and parsed_t - lt >= ONSET_MS) or (lt is None and st["prev_listed"] == 0):
                    self.onset_ages.append(obj["f"][0][1])
        if listed:
            st["last_target_t"] = obj["t"]
        st["prev_listed"] = listed
        st.update(s=obj["s"], n=obj["n"], t=obj["t"], epoch=self.epoch)
        st["msgs"] += 1
        st["covered"] += obj["n"]
        return out

    def _on_ld2412(self, payload, retain, wall, mono):
        st = self.ld2412
        text = payload.decode("utf-8", "replace")
        if retain:
            st["retained_seen"] = text
            return [] if text in ("0", "1") else [self._fail(wall, f"retained LD2412 payload {text!r}")]
        out = []
        reported_silence = self.ld2412_silence_open
        self.ld2412_ref_mono = mono
        self.ld2412_silence_open = False
        if text not in ("0", "1"):
            out.append(self._fail(wall, f"LD2412 payload must be '0' or '1', got {text!r}"))
        st["count"] += 1
        if st["last_mono"] is not None and st["last_epoch"] == self.epoch:
            gap = mono - st["last_mono"]
            st["max_gap"] = max(st["max_gap"], gap)
            if gap > HEARTBEAT_LD2412_S + HEARTBEAT_SLACK_S and not reported_silence:
                out.append(self._fail(wall, f"LD2412 silent for {gap:.0f} s (heartbeat {HEARTBEAT_LD2412_S:.0f} s)"))
            if text == st["last"] and gap < HEARTBEAT_LD2412_S - HEARTBEAT_SLACK_S:
                out.append(self._fail(wall, f"LD2412 repeated {text!r} after {gap:.1f} s: not on change only"))
        if st["last"] is not None and text != st["last"]:
            st["changes"] += 1
        st["last"], st["last_mono"], st["last_epoch"] = text, mono, self.epoch
        return out

    def _on_discovery(self, topic, payload, wall):
        parts = topic.split("/")
        if len(parts) != 4:
            return []
        object_id = parts[2]
        obj = None
        try:
            obj = json.loads(payload.decode("utf-8")) if payload else None
        except (UnicodeDecodeError, ValueError):
            obj = False
        ids = obj.get("dev", {}).get("ids") if isinstance(obj, dict) and isinstance(obj.get("dev"), dict) else None
        mine = object_id.startswith(self.device_id + "_") or ids == self.device_id or (
            isinstance(ids, list) and self.device_id in ids)
        if not mine:
            return []
        rec = self.discovery.setdefault(topic, {"count": 0, "ok": True, "size": 0, "note": ""})
        rec["count"] += 1
        rec["size"] = len(payload)
        out = []
        if not payload:
            rec["note"] = "removed (empty retained payload)"
            return [self._warn(wall, f"discovery {topic} is empty: entity removed")]
        problems = []
        if obj is False or not isinstance(obj, dict):
            problems.append("not a JSON object")
        else:
            if not isinstance(obj.get("uniq_id"), str):
                problems.append("no uniq_id")
            if not (isinstance(obj.get("dev"), dict) and obj["dev"].get("ids")):
                problems.append("no dev.ids")
            if "avty_t" not in obj and "avty" not in obj:
                out.append(self._warn(wall, f"discovery {topic} has no availability topic"))
        if len(payload) > DISCOVERY_MAX_BYTES:
            problems.append(f"{len(payload)} B, over {DISCOVERY_MAX_BYTES} B")
        expected = EXPECTED_SENSOR_CONFIGS.get(topic) if self.device_id == "Led_Matrix_Node" else None
        if expected is not None and payload != expected:
            problems.append("not byte-identical to the pre-P1 sensor config")
        if problems:
            rec["ok"] = False
            rec["note"] = "; ".join(problems)
            out.append(self._fail(wall, f"discovery {topic}: {rec['note']}"))
        else:
            rec["note"] = "byte-identical" if expected is not None else "parses"
        return out

    # ---------------------------------------------------------------- summary
    def report(self, final=True):
        lines = []
        fail = self.failure_count > 0
        now = self.last_mono if self.last_mono is not None else self.start_mono
        span = (now - self.start_mono) if (now is not None and self.start_mono is not None) else 0.0
        lines.append("=" * 72)
        lines.append(f"{'FINAL' if final else 'INTERIM'} frames checker summary, {span / 3600:.2f} h observed "
                     f"(since {_iso(self.start_wall)})")

        # frames: volume and sizes
        lines.append(f"frames messages: {self.frames_msgs}, frames covered: {self.frames_covered}, "
                     f"target frames listed: {self.frames_listed}")
        if self.frames_msgs:
            lines.append(f"payload size: mean {self.frames_bytes_sum / self.frames_msgs:.0f} B, "
                         f"max {self.frames_bytes_max} B (limit {FRAMES_MAX_BYTES} B)")
        else:
            lines.append("FAIL no frames message received")
            fail = True
        if span > 0:
            lines.append(f"mean rate: {self.frames_msgs / span:.2f} msg/s, "
                         f"{self.frames_covered / span:.2f} frames/s covered (radar: 10/s while it reports)")

        # seq
        explained = [g for g in self.gaps if g["explained"]]
        unexplained = [g for g in self.gaps if not g["explained"]]
        lines.append(f"boot ids: {', '.join(self.boot_order) or '-'}")
        for b in self.boot_order:
            st = self.boots[b]
            lines.append(f"  {b}: {st['msgs']} msgs, {st['covered']} frames, s {st['first_s']}..{st['s'] + st['n']}, "
                         f"{st['gaps']} gaps, {st['missing']} frames missing")
        verdict = "PASS" if not unexplained else "FAIL"
        lines.append(f"{verdict} seq contiguity: {len(explained)} gaps at reconnects "
                     f"({sum(g['missing_frames'] for g in explained)} frames), {len(unexplained)} gaps while "
                     f"connected ({sum(g['missing_frames'] for g in unexplained)} frames)")

        # rates per minute (complete minutes only)
        keys = sorted(self.minutes)[1:-1] if final else sorted(self.minutes)[1:]
        busy = [self.minutes[k] for k in keys if self.minutes[k]["covered"] >= 300
                and self.minutes[k]["listed"] >= 0.5 * self.minutes[k]["covered"]]
        idle = [self.minutes[k] for k in keys if self.minutes[k]["frames"] and self.minutes[k]["listed"] == 0]
        if busy:
            rates = sorted(m["frames"] / 60.0 for m in busy)
            mean = sum(rates) / len(rates)
            outliers = sum(1 for r in rates if not BUSY_RATE_RANGE[0] <= r <= BUSY_RATE_RANGE[1])
            ok = BUSY_RATE_RANGE[0] <= mean <= BUSY_RATE_RANGE[1]
            fail |= not ok
            lines.append(f"{'PASS' if ok else 'FAIL'} rate while occupied ({len(busy)} busy minutes): mean "
                         f"{mean:.2f} msg/s, min {rates[0]:.2f}, max {rates[-1]:.2f}, {outliers} minutes outside "
                         f"{BUSY_RATE_RANGE[0]}-{BUSY_RATE_RANGE[1]} msg/s")
            uplink = sum(m["uplink"] for m in busy) / (60.0 * len(busy))
            ok = uplink <= TODAY_UPLINK_PER_S * UPLINK_SLACK
            fail |= not ok
            lines.append(f"{'PASS' if ok else 'FAIL'} node uplink while occupied: {uplink:.2f} msg/s "
                         f"(pre-P1: {TODAY_UPLINK_PER_S} msg/s)")
        else:
            lines.append("INFO rate while occupied: no busy minute (>= 50% target frames) seen")
        if idle:
            rates = [m["frames"] / 60.0 for m in idle]
            mean = sum(rates) / len(rates)
            ok = max(rates) <= IDLE_RATE_MAX
            lines.append(f"{'PASS' if ok else 'WARN'} rate while empty ({len(idle)} minutes): mean {mean:.2f} msg/s "
                         f"(5 s heartbeat: 0.20), max {max(rates):.2f}")
        verdict = "PASS" if self.peak10 <= PEAK_10S_MAX else "FAIL"
        lines.append(f"{verdict} peak: {self.peak10} frames messages in 10 s (limit {PEAK_10S_MAX})")
        verdict = "PASS" if not (self.frames_silences or self.frames_spacing_fails) else "FAIL"
        lines.append(f"{verdict} frames cadence: largest spacing on the node {self.frames_spacing_max} ms (limit "
                     f"{FRAMES_SPACING_MAX_MS} ms); longest silence while the node was online "
                     f"{self.frames_silence_max:.0f} s, {self.frames_silences} over {FRAMES_SILENCE_MAX_S:.0f} s")
        if self.onset_ages:
            ages = sorted(self.onset_ages)
            p95 = ages[min(len(ages) - 1, int(len(ages) * 0.95))]
            lines.append(f"{'PASS' if p95 <= ONSET_AGE_P95_MS else 'WARN'} onset latency on the node (parse -> "
                         f"publish, from f[0] age): {len(ages)} onsets, median {ages[len(ages) // 2]} ms, "
                         f"p95 {p95} ms, max {ages[-1]} ms (target: <= {ONSET_AGE_P95_MS} ms)")

        # legacy topics
        st = self.ld2412
        ok412 = (st["count"] > 0 or span < HEARTBEAT_LD2412_S * 2) and not self.ld2412_silences
        lines.append(f"{'PASS' if ok412 else 'FAIL'} LD2412 legacy: {st['count']} messages, {st['changes']} changes, "
                     f"longest silence {st['max_gap']:.0f} s, {self.ld2412_silences} silences over "
                     f"{LD2412_SILENCE_MAX_S:.0f} s while online, retained value at start "
                     f"{st['retained_seen']!r}")
        fail |= not ok412
        if self.phase == "p1":
            lines.append(f"{'PASS' if self.ld2454_legacy == 0 else 'FAIL'} LD2454 legacy JSON: "
                         f"{self.ld2454_legacy} messages (P1: none)")

        # discovery
        mine = sorted(self.discovery.items())
        bad = [t for t, r in mine if not r["ok"]]
        missing = [t for t in EXPECTED_SENSOR_CONFIGS if t not in self.discovery] \
            if self.device_id == "Led_Matrix_Node" else []
        ok = not bad and not missing and bool(mine)
        fail |= not ok
        lines.append(f"{'PASS' if ok else 'FAIL'} discovery: {len(mine)} configs of {self.device_id}, "
                     f"{len(bad)} bad, {len(missing)} expected sensor configs missing")
        for topic, rec in mine:
            lines.append(f"  {topic}: {rec['size']} B, seen {rec['count']}x, {rec['note']}")
        for topic in missing:
            lines.append(f"  {topic}: MISSING")

        # node status
        offline = sum(1 for _, p, r in self.status if p == "offline" and not r)
        lines.append(f"node status messages: {len(self.status)} ({offline} live 'offline' = LWT), "
                     f"checker connects: {self.checker_connects}")
        lines.append(f"failures: {self.failure_count}, warnings: {self.warning_count}")
        for wall, text in self.failures[:20]:
            lines.append(f"  {_iso(wall)} {text}")
        lines.append(f"RESULT: {'FAIL' if (fail or self.failure_count) else 'PASS'}")
        lines.append("=" * 72)
        return "\n".join(lines), not (fail or self.failure_count)


def _iso(wall):
    return datetime.fromtimestamp(wall).isoformat(timespec="seconds") if wall is not None else "-"


def main(argv=None):
    # allow_abbrev=False: '--password <secret>' must not be taken as an abbreviation of --password-env
    ap = argparse.ArgumentParser(description="Checks the Led Matrix Node frames stream and discovery on the broker.",
                                 allow_abbrev=False)
    ap.add_argument("--host", required=True, help="broker host name or address")
    ap.add_argument("--port", type=int, default=1883, help="broker port (default 1883)")
    ap.add_argument("--user", help="broker user name; the password then comes from --password-env")
    ap.add_argument("--password-env", default="MQTT_PASSWORD",
                    help="environment variable that holds the password (default MQTT_PASSWORD)")
    ap.add_argument("--duration", type=float, default=86400.0, help="seconds to run (default 86400 = 24 h)")
    ap.add_argument("--report-every", type=float, default=3600.0, help="interim summary interval, s (0 = none)")
    ap.add_argument("--node-prefix", default="led_matrix_node")
    ap.add_argument("--device-id", default="Led_Matrix_Node")
    ap.add_argument("--discovery-prefix", default="homeassistant")
    ap.add_argument("--phase", choices=("p1", "any"), default="p1",
                    help="p1: also require pw 3, sm 'A' and a silent legacy LD2454 topic")
    ap.add_argument("--gap-log", help="append every gap, failure and warning to this file as JSON lines")
    args, unknown = ap.parse_known_args(argv)
    if unknown:
        # argparse would print them, and one may be a password typed on the command line
        print("error: unrecognized command-line arguments (not shown, in case one is a password); see --help. "
              "The password is read only from the environment variable named by --password-env.", file=sys.stderr)
        return 2
    if not ENV_NAME_RE.match(args.password_env):
        print("error: invalid --password-env value (not shown): it must be the name of an environment variable",
              file=sys.stderr)
        return 2

    password = None
    if args.user:
        password = os.environ.get(args.password_env)
        if password is None:
            name = "MQTT_PASSWORD" if args.password_env == "MQTT_PASSWORD" else "named by --password-env"
            print(f"error: --user given, but the environment variable {name} is not set", file=sys.stderr)
            return 2
    try:
        import paho.mqtt.client as mqtt
        from paho.mqtt.subscribeoptions import SubscribeOptions
    except ImportError:
        print('error: paho-mqtt 2.x is required: pip install "paho-mqtt>=2,<3"', file=sys.stderr)
        return 2
    if not hasattr(mqtt, "CallbackAPIVersion"):
        print("error: paho-mqtt 2.x is required (found 1.x)", file=sys.stderr)
        return 2

    checker = FrameStreamChecker(args.node_prefix, args.device_id, args.discovery_prefix, args.phase)
    lock = threading.Lock()
    gap_file = open(args.gap_log, "a", encoding="utf-8") if args.gap_log else None

    def emit(lines):
        for line in lines:
            print(line, flush=True)
        if gap_file and checker.event_log:
            for event in checker.event_log:
                gap_file.write(json.dumps(event) + "\n")
            gap_file.flush()
        checker.event_log.clear()

    def guarded(callback):
        # An exception in a callback would end paho's network thread and leave the checker running blind:
        # it is recorded as a failure instead.
        def wrapper(*cb_args):
            try:
                callback(*cb_args)
            except Exception:
                text = " | ".join(traceback.format_exc().strip().splitlines()[-3:])
                with lock:
                    emit([checker._fail(time.time(), f"checker error in {callback.__name__}: {text}")])
        return wrapper

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print(f"{_iso(time.time())} broker refused the connection: {reason_code}", flush=True)
            return
        with lock:
            checker.on_connect(time.time(), time.monotonic())
        print(f"{_iso(time.time())} connected to {args.host}:{args.port}", flush=True)
        # QoS 1: a message arrives with the QoS it was published with (0 or 1). Retain-as-published on the
        # frames topic only: every live frames message then keeps the node's retain flag, while the
        # retained status and LD2412 topics are told apart as usual (retain set = stored copy).
        p = args.node_prefix
        client.subscribe([(f"{p}/radar/ld2454/frames", SubscribeOptions(qos=1, retainAsPublished=True)),
                          (f"{p}/radar/ld2454", SubscribeOptions(qos=1)),
                          (f"{p}/radar/ld2412", SubscribeOptions(qos=1)),
                          (f"{p}/sensors/#", SubscribeOptions(qos=1)),
                          (f"{args.discovery_prefix}/+/+/config", SubscribeOptions(qos=0))])

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        with lock:
            lines = checker.on_disconnect(time.time(), time.monotonic())
            emit(lines)

    def on_message(client, userdata, msg):
        with lock:
            lines = checker.on_message(msg.topic, msg.payload, bool(msg.retain), msg.qos,
                                       time.time(), time.monotonic())
            emit(lines)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"frames-checker-{os.getpid()}",
                         protocol=mqtt.MQTTv5)
    if args.user:
        client.username_pw_set(args.user, password)
    client.on_connect = guarded(on_connect)
    client.on_disconnect = guarded(on_disconnect)
    client.on_message = guarded(on_message)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    checker.disconnected_mono = time.monotonic()   # not connected yet: counts as offline until on_connect
    client.connect_async(args.host, args.port, keepalive=30)
    client.loop_start()

    end = time.monotonic() + args.duration
    next_report = time.monotonic() + args.report_every if args.report_every > 0 else None
    try:
        while time.monotonic() < end:
            time.sleep(1.0)
            with lock:
                emit(checker.tick(time.time(), time.monotonic()))
            if next_report is not None and time.monotonic() >= next_report:
                next_report += args.report_every
                with lock:
                    print(checker.report(final=False)[0], flush=True)
    except KeyboardInterrupt:
        print("interrupted", flush=True)
    finally:
        client.loop_stop()
        client.disconnect()
    with lock:
        emit(checker.tick(time.time(), time.monotonic()))
        text, ok = checker.report(final=True)
    print(text, flush=True)
    if gap_file:
        gap_file.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
