#!/usr/bin/env python3
"""P2 host-side model of the MqttTask side of the radar control (board revision 2, -D BOARD_REV2).

Mirrors, statement by statement: MqttManager::update() (the lost-connection check before the Wi-Fi early return, the
reconnect backoff, the connect path order), clearRetainedCommands(), subscribeTopics(), onHaStatus() with the birth
resend, serviceRadarStates() (the dirty-flag protocol, presence on change), publishRadarStates() (order),
publishLegacyRadar() (the LD2412 topic with None, the legacy JSON cadence), the frames idle selection and the mt / p2
omission, and the radar command branches of the MQTT callback. Each model method names its C++ counterpart in a docstring.

The connect burst (about 45 packets, more than the lwIP send buffer holds) and the HA-birth resend stop at the first
refused publish / subscribe: abortBurst() closes the connection, the backoff repeats the whole burst. The model's socket
refuses every packet from the Nth on (a stalled link) and counts the ~10 s each refused write blocks MqttTask.

RadarPower (owner A, include/radar_power.h) is replaced by a stub of its interface: request(), set_ha_state(),
snapshot(), the atomics g_stateDirty / haState. Its own behaviour is tested in tools/tests/radar_power_model.py.

The model is tied to the C++ mechanically, so a drift fails this test instead of passing silently: the constants and
topics are parsed from include/radar_constants.h / constants.h, and the order of the subscriptions, of the connect path,
of the retained states, the command dispatch table, the status JSON format, the frames format strings and the
no-publish-from-the-callback rule are read from src/mqtt_manager.cpp and src/ld2454_frames.cpp (the BOARD_REV2 variant).

Run: python -I -B tools/tests/mqtt_radar_model.py        (standard library only; exit code 0 = all passed)
"""
import ast
import os
import re
import sys

sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
U32 = 0xFFFFFFFF
FAILS = []
OKS = [0]


def check(cond, text):
    if cond:
        OKS[0] += 1
    else:
        FAILS.append(text)
        print("  FAIL:", text)


# ============================================================================ reading the C++ sources
def read_src(rel):
    with open(os.path.join(ROOT, rel), "rb") as fh:
        return fh.read().decode("utf-8").replace("\r\n", "\n")


def strip_comments(text):
    """Removes // and /* */ comments, leaving string literals alone."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


_PP = re.compile(r"^\s*#\s*(ifdef|ifndef|else|endif|if|elif)\b\s*(\w*)")


def preprocess(text, defines):
    """Keeps the lines of one build variant: handles #ifdef / #ifndef / #else / #endif (nothing else is used)."""
    out, stack = [], []
    active = True
    for line in text.split("\n"):
        m = _PP.match(line)
        if m:
            kind, name = m.group(1), m.group(2)
            if kind in ("ifdef", "ifndef"):
                cond = (name in defines) == (kind == "ifdef")
                stack.append([active, cond])
                active = active and cond
            elif kind == "else":
                parent, cond = stack[-1]
                stack[-1][1] = not cond
                active = parent and not cond
            elif kind == "endif":
                parent, _ = stack.pop()
                active = parent
            else:
                raise ValueError("unsupported preprocessor line: " + line)
            out.append("")
            continue
        out.append(line if active else "")
    assert not stack, "unbalanced #if"
    return "\n".join(out)


def extract_function(text, signature):
    """From the line that starts with `signature` to the closing brace at column 0."""
    start = text.find(signature)
    assert start >= 0, "function not found: " + signature
    end = text.find("\n}", start)
    assert end >= 0, "no closing brace: " + signature
    return text[start:end + 2]


def flat(text):
    """Whitespace collapsed, for robust substring checks."""
    return re.sub(r"\s+", " ", text)


def c_unescape(s):
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), s)


def safe_eval(expr, env):
    expr = re.sub(r"\((?:u?int\d+_t|size_t|unsigned long|unsigned|int)\)", "", expr)
    expr = re.sub(r"\b(\d+)[uUlL]+\b", r"\1", expr)

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b,
                   ast.Div: lambda: a // b, ast.FloorDiv: lambda: a // b}
            return ops[type(node.op)]()
        raise ValueError("unsupported expression")
    return ev(ast.parse(expr.strip(), mode="eval"))


def parse_constants(text, env=None):
    """static const <type> NAME = <value>;  ->  {NAME: int | str}"""
    env = {} if env is None else env
    text = strip_comments(text)
    for m in re.finditer(r"static\s+const\s+[\w\s\*]+?\b(\w+)\s*=\s*([^;]+);", text):
        name, rhs = m.group(1), m.group(2).strip()
        if rhs.startswith('"'):
            env[name] = c_unescape(rhs[1:-1])
            continue
        try:
            env[name] = safe_eval(rhs, env)
        except Exception:
            pass
    return env


def unused_results(body, names):
    """The calls of `names` in `body` whose result is not used: not directly after return, &&, ||, !, = or a parenthesis."""
    bad = []
    for m in re.finditer(r"(?<![\w.>])(" + "|".join(re.escape(n) for n in names) + r")\s*\(", body):
        if not re.search(r"(?:\breturn|&&|\|\||!|=|\()$", body[:m.start()].rstrip()):
            bad.append(m.group(1))
    return bad


def parse_calls(body, names):
    """[(function name, [raw argument text, ...]), ...] for every call of one of `names`, in source order."""
    pat = re.compile(r"(?<![\w.>])(" + "|".join(re.escape(n) for n in names) + r")\s*\(")
    calls, pos = [], 0
    while True:
        m = pat.search(body, pos)
        if not m:
            return calls
        j, depth, args, cur, in_str = m.end(), 1, [], "", False
        while depth:
            c = body[j]
            if in_str:
                cur += c
                if c == "\\":
                    cur += body[j + 1]
                    j += 1
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
                cur += c
            elif c == "(":
                depth += 1
                cur += c
            elif c == ")":
                depth -= 1
                if depth:
                    cur += c
            elif c == "," and depth == 1:
                args.append(cur.strip())
                cur = ""
            else:
                cur += c
            j += 1
        args.append(cur.strip())
        calls.append((m.group(1), args))
        pos = j


ENV = {}
ENV = parse_constants(read_src("include/constants.h"), ENV)
ENV = parse_constants(read_src("include/radar_constants.h"), ENV)


# ============================================================================ constants and topics (from the C++)
MQTT_BIRTH_RESEND_MAX_DELAY_MS = ENV["MQTT_BIRTH_RESEND_MAX_DELAY_MS"]
MQTT_BIRTH_SKIP_MS = ENV["MQTT_BIRTH_SKIP_MS"]
LD2412_HEARTBEAT_MS = ENV["LD2412_HEARTBEAT_MS"]
LEGACY_HEARTBEAT_MS = ENV["LEGACY_HEARTBEAT_MS"]
MQTT_RADAR_PUBLISH_INTERVAL = ENV["MQTT_RADAR_PUBLISH_INTERVAL"]
LD2454_STREAM_IDLE_MS = ENV["LD2454_STREAM_IDLE_MS"]
LD2454_STREAM_OFF_IDLE_MS = ENV["LD2454_STREAM_OFF_IDLE_MS"]
RADAR_ATTR_JSON_MAX = ENV["RADAR_ATTR_JSON_MAX"]
RADAR_PRESENCE_HOLD_MS = ENV["RADAR_PRESENCE_HOLD_MS"]
MQTT_RECONNECT_MIN_MS = ENV["MQTT_RECONNECT_MIN_MS"]
MQTT_RECONNECT_MAX_MS = ENV["MQTT_RECONNECT_MAX_MS"]

# NetworkClient::write() on a socket that takes no data: 10 retries of a 1 s select (Arduino-ESP32 3.2), then it gives up
# and PubSubClient::publish() returns false. The send buffer (CONFIG_LWIP_TCP_SND_BUF_DEFAULT = 5760 B) holds the whole
# P1 burst (11 packets, 1.9 KB) but not the REV2 one (45 packets, 6.8 KB).
WRITE_TIMEOUT_MS = 10 * 1000

# The labels abortBurst() logs, in the order of the steps (the C++ link in test_cpp_links() compares them with the source).
STEP_DISCOVERY, STEP_CLEAR, STEP_SUBSCRIBE, STEP_STATES, STEP_ONLINE = ("discovery", "command clear", "subscribe", "states", "online")
STEP_BIRTH_DISCOVERY, STEP_BIRTH_STATES = ("birth discovery", "birth states")

T = {
    "power_cmd": ENV["MQTT_RADAR_POWER_CMD"], "power_state": ENV["MQTT_RADAR_POWER_STATE"],
    "sched_cmd": ENV["MQTT_RADAR_SCHEDULE_CMD"], "sched_state": ENV["MQTT_RADAR_SCHEDULE_STATE"],
    "ns_cmd": ENV["MQTT_RADAR_NIGHT_START_CMD"], "ns_state": ENV["MQTT_RADAR_NIGHT_START_STATE"],
    "ne_cmd": ENV["MQTT_RADAR_NIGHT_END_CMD"], "ne_state": ENV["MQTT_RADAR_NIGHT_END_STATE"],
    "offl_cmd": ENV["MQTT_RADAR_SCHED_OFFLINE_CMD"], "offl_state": ENV["MQTT_RADAR_SCHED_OFFLINE_STATE"],
    "legacy_cmd": ENV["MQTT_RADAR_LEGACY_CMD"], "legacy_state": ENV["MQTT_RADAR_LEGACY_STATE"],
    "powered": ENV["MQTT_RADAR_POWERED_TOPIC"], "observing": ENV["MQTT_RADAR_OBSERVING_TOPIC"],
    "state": ENV["MQTT_RADAR_STATE_TOPIC"], "attr": ENV["MQTT_RADAR_STATE_ATTR_TOPIC"],
    "presence": ENV["MQTT_PRESENCE_TOPIC"], "ha": ENV["MQTT_HA_STATUS_TOPIC"], "ld2412": ENV["MQTT_LD2412_TOPIC"],
    "status": ENV["MQTT_STATUS_TOPIC"], "frames": ENV["MQTT_LD2454_FRAMES_TOPIC"],
}
DISPLAY_TOPICS = [ENV["MQTT_DISPLAY_MODE_TOPIC"], ENV["MQTT_DISPLAY_MSG_TOPIC"], ENV["MQTT_DISPLAY_POWER_TOPIC"],
                  ENV["MQTT_DISPLAY_COLOR_TOPIC"], ENV["MQTT_BEEPER_TOPIC"], ENV["MQTT_DISPLAY_ICON_TOPIC"]]
COMMAND_TOPICS = [T["power_cmd"], T["sched_cmd"], T["ns_cmd"], T["ne_cmd"], T["offl_cmd"], T["legacy_cmd"]]
SUBSCRIPTIONS_RADAR = COMMAND_TOPICS + [T["ha"]]
STATE_ORDER = [T["power_state"], T["sched_state"], T["ns_state"], T["ne_state"], T["offl_state"], T["legacy_state"],
               T["powered"], T["presence"], T["observing"], T["state"], T["attr"]]
DISCOVERY_IDS = ["co2", "temp", "hum", "lux", "radar_power", "radar_schedule", "radar_powered", "radar_observing",
                 "radar_state", "radar_presence", "radar_sched_offline", "radar_legacy", "radar_night_start",
                 "radar_night_end"]

# the status JSON template: C++ snprintf format after unescaping (see test_cpp_links); %lu / %u are printed as %d here
ATTR_FORMAT = ('{"sw":%d,"since":%lu,"next":"%s","reason":"%s","ha":"%s","ups24":%u,"on24":%u.%u,"thr":%d,'
               '"fault":"%s","b":"%s","seq":%lu}')


# ============================================================================ stub of the RadarPower interface
# radar_power.h: enums
ON, PARKING, OFF_SETTLING, OFF, POWERING = range(5)
ST_ON, ST_NIGHT_OFF, ST_FORCED_ON, ST_FORCED_OFF, ST_FAILSAFE_ON = range(5)
STATE_NAMES = ("on", "night_off", "forced_on", "forced_off", "failsafe_on")
REASON_NAMES = ("schedule", "manual", "failsafe", "ha_unknown", "ota", "fault", "throttle", "pending")
FAULT_NAMES = ("", "park_timeout", "ld2454_no_frames")
HA_UNKNOWN, HA_ONLINE, HA_OFFLINE = range(3)
HA_NAMES = ("unknown", "online", "offline")
CMD_ENABLE, CMD_SCHEDULE, CMD_NIGHT_START, CMD_NIGHT_END, CMD_SCHED_OFFLINE, CMD_LEGACY = range(6)
CMD_COUNT = 6
CMD_KEYS = ("en", "sched", "ns", "ne", "offl", "legacy")
RADAR_STATIC_WARMUP_MS = ENV["LD2412_STATIC_WARMUP_MS"]
RADAR_ON_SETTLE_MS = ENV["RADAR_ON_SETTLE_MS"]


def parse_on_off(text):
    """C++: radar_power.cpp radarParseOnOff() (tested in radar_power_model.py)."""
    if text in ("1", "0"):
        return int(text)
    t = text.lower()
    return 1 if t == "on" else (0 if t == "off" else -1)


def parse_time_of_day(text):
    """C++: radar_power.cpp radarParseTimeOfDay(): H:MM, HH:MM, HH:MM:SS -> minutes, else None."""
    m = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?", text)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return h * 60 + mi if h <= 23 and mi <= 59 and s <= 59 else None


def format_hms(minutes):
    return "%02d:%02d:00" % (minutes // 60, minutes % 60)


def format_hm(minutes):
    return "%02d:%02d" % (minutes // 60, minutes % 60)


class Nvs:
    DEFAULTS = {"en": 1, "sched": 0, "ns": 0, "ne": 360, "offl": 0, "legacy": 0}

    def __init__(self):
        self.data = {}
        self.writes = []

    def get(self, key):
        return self.data.get(key, self.DEFAULTS[key])

    def put(self, key, value):
        self.data[key] = value
        self.writes.append((key, value))


class Shared:
    """The atomics of radar_power.h that MqttTask touches."""

    def __init__(self):
        self.state_dirty = False
        self.ha_state = HA_UNKNOWN
        self.ha_lost_ms = 0
        self.ld2454_ready = False
        self.phase = ON


class Status:
    def __init__(self):
        self.since_ms = 0
        self.ns, self.ne, self.next_min, self.ups24, self.on24x10 = 0, 360, -1, 1, 1000
        self.en = self.sched = self.offl = self.legacy = 0
        self.state, self.reason, self.fault, self.ha = ST_ON, 0, 0, HA_UNKNOWN
        self.powered = self.observing = self.obs412 = self.thr = False

    @property
    def pw(self):
        return (1 if self.observing else 0) | (2 if self.obs412 else 0)

    @property
    def sm(self):
        return "ANFXS"[self.state]

    def key(self):
        return tuple(v for k, v in sorted(vars(self).items()) if k not in ("since_ms", "on24x10"))


class RadarStub:
    """A stand-in for RadarPower: just enough to make 'a change is waiting' (g_stateDirty), a snapshot and the
    observing condition (12 s after the power-up and LD2454 ready). Not a model of the real task."""

    def __init__(self, g, nvs, boot_ms):
        self.g, self.nvs = g, nvs
        self.set = {k: nvs.get(k) for k in CMD_KEYS}
        self.pending = [None] * CMD_COUNT
        self.on_at, self.powered_at = boot_ms & U32, 0
        self.phase = POWERING
        self.status = Status()
        g.phase, g.ha_state, g.ha_lost_ms, g.state_dirty = POWERING, HA_UNKNOWN, boot_ms & U32, True
        self._update(boot_ms)

    def request(self, cmd, value):
        """C++: radarPowerRequest(): validate, one slot per command, last wins; no NVS, no blocking."""
        ok = (0 <= value <= 1) if cmd in (CMD_ENABLE, CMD_SCHEDULE, CMD_SCHED_OFFLINE, CMD_LEGACY) else (0 <= value <= 1439)
        if ok:
            self.pending[cmd] = value

    def set_ha_state(self, state, now):
        """C++: radarSetHaState(): leaving ONLINE starts the grace; a no-op when unchanged."""
        if state != self.g.ha_state:
            if self.g.ha_state == HA_ONLINE:
                self.g.ha_lost_ms = now & U32
            self.g.ha_state = state

    def pass_(self, now):
        for cmd in range(CMD_COUNT):                         # applyPending(): NVS only on change, the echo always
            v, self.pending[cmd] = self.pending[cmd], None
            if v is None:
                continue
            key = CMD_KEYS[cmd]
            if self.set[key] != v:
                self.nvs.put(key, v)
                self.set[key] = v
            self.g.state_dirty = True
        if self.phase == POWERING and ((now - self.on_at) & U32) >= RADAR_ON_SETTLE_MS:
            self.phase, self.powered_at = ON, now & U32
            self.g.phase = ON
        self._update(now)

    def _update(self, now):
        s = Status()
        s.en, s.sched, s.ns, s.ne, s.offl, s.legacy = (self.set[k] for k in CMD_KEYS)
        s.state = ST_FORCED_OFF if not s.en else (ST_ON if s.sched else ST_FORCED_ON)
        s.powered = self.phase == ON
        warm = self.phase == ON and ((now - self.powered_at) & U32) >= RADAR_STATIC_WARMUP_MS
        s.obs412, s.observing = warm, warm and self.g.ld2454_ready
        s.ha = self.g.ha_state
        s.since_ms = self.status.since_ms if s.state == self.status.state and s.powered == self.status.powered else now
        if s.key() != self.status.key():
            self.g.state_dirty = True
        self.status = s

    def snapshot(self):
        return self.status

    @staticmethod
    def attr_json(st, boot_id, seq):
        """The status JSON as MqttTask builds it with snprintf (RADAR_ATTR_JSON_MAX)."""
        nxt = format_hm(st.next_min) if st.next_min >= 0 else ""
        fmt = ATTR_FORMAT.replace("%lu", "%d").replace("%u", "%d")
        return fmt % (1 if st.powered else 0, (st.since_ms & U32) // 1000, nxt, REASON_NAMES[st.reason], HA_NAMES[st.ha],
                      st.ups24, st.on24x10 // 10, st.on24x10 % 10, 1 if st.thr else 0, FAULT_NAMES[st.fault], boot_id, seq)


# ============================================================================ the MqttTask model
UNKNOWN, ABSENT, PRESENT = "UNKNOWN", "ABSENT", "PRESENT"


class Broker:
    def __init__(self):
        self.ops = []
        self.closes = 0             # connections the node closed itself (_espClient.stop(), no DISCONNECT)

    def publish(self, topic, payload, retained):
        self.ops.append(("pub", topic, payload, retained))
        return True

    def subscribe(self, topic):
        self.ops.append(("sub", topic, None, None))


class Mqtt:
    """MqttManager under BOARD_REV2."""

    def __init__(self, rp, broker, bootid="9f3c21aa"):
        self.rp, self.b, self.bootid = rp, broker, bootid
        self.connected = False
        self.birth_pending = False
        self.birth_at = self.birth_delay = 0
        self.last_discovery = 0
        self.presence_published = -1
        self.ld2412_published = -1
        self.ld2412_published_ms = 0
        self.legacy_ms = 0
        self.legacy_sent = 0
        self.live = {"presence": ABSENT, "target": False}
        self.clock = 0                      # millis() as of the last call into the model
        self.last_target = 0                # _lastTargetMs
        self.target_held = False            # _targetHeld
        self.rng = lambda n: 0
        self.fail_publish_after = None      # packet number from which every PUBLISH / SUBSCRIBE is refused (a stalled link)
        self.npub = 0                       # packets attempted on the socket, refused ones included
        self.stall_ms = 0                   # time MqttTask spent blocked in refused writes (WRITE_TIMEOUT_MS each)
        self.last_attempt = 0               # _lastConnectAttemptMs
        self.reconnect_delay = 0            # _reconnectDelayMs
        self.log = []                       # the Serial lines of abortBurst()

    def send(self, kind, topic, payload, retained):
        """One packet into the socket, C++: PubSubClient::publish() / subscribe(). False when the write fails: the send
        buffer of a stalled link is full and NetworkClient::write() gave up after WRITE_TIMEOUT_MS."""
        self.npub += 1
        if self.fail_publish_after is not None and self.npub >= self.fail_publish_after:
            self.stall_ms += WRITE_TIMEOUT_MS
            return False
        if kind == "sub":
            self.b.subscribe(topic)
        else:
            self.b.publish(topic, payload, retained)
        return True

    def pub(self, topic, payload, retained):
        return self.send("pub", topic, payload, retained)

    def update(self, now, wifi_ok, sock_ok, broker_accepts=True):
        """C++: MqttManager::update()."""
        self.clock = now & U32
        client_connected = self.connected and sock_ok
        if (not wifi_ok) or (not client_connected):
            self.rp.set_ha_state(HA_UNKNOWN, now)                  # BEFORE the Wi-Fi early return
        if not wifi_ok:
            self.connected = False
            return
        if not client_connected:
            if ((now - self.last_attempt) & U32) < self.reconnect_delay:   # the backoff between attempts
                return
            self.last_attempt = now & U32
            if not broker_accepts:
                self.connected = False
                self.reconnect_delay = min(max(self.reconnect_delay * 2, MQTT_RECONNECT_MIN_MS), MQTT_RECONNECT_MAX_MS)
                return
            self.connected = True
            self.reconnect_delay = MQTT_RECONNECT_MIN_MS
            self.ld2412_published = -1
            self.rp.set_ha_state(HA_UNKNOWN, now)                  # before the subscription
            self.presence_published = -1
            self.birth_pending = False
            # the first refused step ends the burst: the connection is closed, the backoff retries the whole burst
            if not self.send_discovery():
                return self.abort_burst(STEP_DISCOVERY)
            if not self.clear_retained_commands():
                return self.abort_burst(STEP_CLEAR)
            if not self.subscribe_topics():
                return self.abort_burst(STEP_SUBSCRIBE)
            self.rp.g.state_dirty = False
            if not self.publish_radar_states(self.rp.snapshot()):
                return self.abort_burst(STEP_STATES)
            self.last_discovery = now & U32
            if not self.pub(T["status"], "online", True):
                return self.abort_burst(STEP_ONLINE)

    def abort_burst(self, step):
        """C++: MqttManager::abortBurst(): one log line, the socket closed without a DISCONNECT (the broker publishes the
        will), the dirty flag set again. last_discovery and birth_pending are left alone."""
        self.log.append("[MQTT] %s refused, closing the connection" % step)
        self.connected = False
        self.b.closes += 1
        self.rp.g.state_dirty = True

    def send_discovery(self):
        """C++: MqttManager::sendDiscovery(): false at the first refused publish, the rest is not sent."""
        for id_ in DISCOVERY_IDS:
            if not self.pub("homeassistant/x/Led_Matrix_Node_%s/config" % id_, "{}", True):
                return False
        return True

    def clear_retained_commands(self):
        """C++: MqttManager::clearRetainedCommands(): empty retained publishes; false at the first refused one."""
        for t in COMMAND_TOPICS:
            if not self.pub(t, "", True):
                return False
        return True

    def subscribe_topics(self):
        """C++: MqttManager::subscribeTopics() under REV2: false at the first refused SUBSCRIBE."""
        for t in DISPLAY_TOPICS + SUBSCRIPTIONS_RADAR:
            if not self.send("sub", t, None, None):
                return False
        return True

    def task_loop(self, now, wifi_ok=True):
        """C++: mqttTask(), one pass: update(), then only while connected publishLegacyRadar() and serviceRadarStates()."""
        self.update(now, wifi_ok, True)
        if self.connected and wifi_ok:
            self.publish_legacy(now)
            self.service_radar_states(now)

    def on_message(self, topic, payload, now):
        """C++: onMqttMessageReceived(), the radar branches. Never publishes."""
        n0 = len(self.b.ops)
        switch_cmds = {T["power_cmd"]: CMD_ENABLE, T["sched_cmd"]: CMD_SCHEDULE, T["offl_cmd"]: CMD_SCHED_OFFLINE,
                       T["legacy_cmd"]: CMD_LEGACY}
        time_cmds = {T["ns_cmd"]: CMD_NIGHT_START, T["ne_cmd"]: CMD_NIGHT_END}
        if topic in switch_cmds:
            v = parse_on_off(payload)
            if v >= 0:
                self.rp.request(switch_cmds[topic], v)
        elif topic in time_cmds:
            m = parse_time_of_day(payload)
            if m is not None:
                self.rp.request(time_cmds[topic], m)
        elif topic == T["ha"]:
            if payload == "online":
                self.on_ha_status(True, now)
            elif payload == "offline":
                self.on_ha_status(False, now)
        assert len(self.b.ops) == n0, "the callback must not publish"

    def on_ha_status(self, online, now):
        """C++: MqttManager::onHaStatus()."""
        self.rp.set_ha_state(HA_ONLINE if online else HA_OFFLINE, now)
        if online and ((now - self.last_discovery) & U32) >= MQTT_BIRTH_SKIP_MS:
            self.birth_at = now & U32
            self.birth_delay = self.rng(MQTT_BIRTH_RESEND_MAX_DELAY_MS)
            self.birth_pending = True

    def presence_now(self, s):
        """C++: MqttManager::presenceNow(): an LD2454 target holds the presence ON for RADAR_PRESENCE_HOLD_MS (frame flicker)."""
        if not s.observing:
            self.target_held = False
            return False
        now = self.clock
        if self.live["target"]:
            self.last_target = now
            self.target_held = True
        elif self.target_held and ((now - self.last_target) & U32) >= RADAR_PRESENCE_HOLD_MS:
            self.target_held = False
        return self.live["presence"] == PRESENT or self.target_held

    def publish_radar_states(self, s):
        """C++: MqttManager::publishRadarStates(): all retained; False if any publish failed."""
        ok = True

        def pub(topic, payload):
            nonlocal ok
            if ok:
                ok = self.pub(topic, payload, True)
        pub(T["power_state"], "1" if s.en else "0")
        pub(T["sched_state"], "1" if s.sched else "0")
        pub(T["ns_state"], format_hms(s.ns))
        pub(T["ne_state"], format_hms(s.ne))
        pub(T["offl_state"], "1" if s.offl else "0")
        pub(T["legacy_state"], "1" if s.legacy else "0")
        pub(T["powered"], "ON" if s.powered else "OFF")
        p = self.presence_now(s)
        self.presence_published = 1 if p else 0
        pub(T["presence"], "ON" if p else "OFF")
        pub(T["observing"], "ON" if s.observing else "OFF")
        pub(T["state"], STATE_NAMES[s.state])
        attr = RadarStub.attr_json(s, self.bootid, 0)
        assert len(attr) < RADAR_ATTR_JSON_MAX
        pub(T["attr"], attr)
        return ok

    def service_radar_states(self, now):
        """C++: MqttManager::serviceRadarStates()."""
        self.clock = now & U32
        if self.birth_pending and ((now - self.birth_at) & U32) >= self.birth_delay:
            self.birth_pending = False
            if not self.send_discovery():                          # the reconnect repeats the whole burst
                return self.abort_burst(STEP_BIRTH_DISCOVERY)
            self.rp.g.state_dirty = False
            if not self.publish_radar_states(self.rp.snapshot()):
                return self.abort_burst(STEP_BIRTH_STATES)
            self.last_discovery = now & U32
            return
        dirty = self.rp.g.state_dirty
        self.rp.g.state_dirty = False                              # exchange(false) FIRST
        s = self.rp.snapshot()
        if dirty:
            if not self.publish_radar_states(s):
                self.rp.g.state_dirty = True
            return
        p = 1 if self.presence_now(s) else 0
        if p != self.presence_published and self.pub(T["presence"], "ON" if p else "OFF", True):
            self.presence_published = p

    def publish_legacy(self, now):
        """C++: MqttManager::publishLegacyRadar()."""
        self.clock = now & U32
        pr = self.live["presence"]
        code = 1 if pr == PRESENT else (0 if pr == ABSENT else 2)
        changed = code != self.ld2412_published
        heartbeat = code != 2 and ((now - self.ld2412_published_ms) & U32) >= LD2412_HEARTBEAT_MS
        if changed or heartbeat:
            if self.pub(T["ld2412"], "None" if code == 2 else ("1" if code else "0"), True):
                self.ld2412_published, self.ld2412_published_ms = code, now & U32
        s = self.rp.snapshot()
        if s.legacy and s.observing:
            every = MQTT_RADAR_PUBLISH_INTERVAL if self.live["target"] else LEGACY_HEARTBEAT_MS
            if ((now - self.legacy_ms) & U32) >= every:
                self.legacy_ms = now & U32
                self.legacy_sent += 1


def frames_fields(pw, sm, mt=1, p2=1):
    """C++: formatLd2454Frames() tail under REV2."""
    out = ""
    if pw & 1:
        out += ',"mt":%d' % mt
    if pw & 2:
        out += ',"p2":%d' % p2
    return out + ',"pw":%d,"sm":"%s"' % (pw, sm)


def make(boot=1000, ready=False):
    g = Shared()
    g.ld2454_ready = ready
    nvs = Nvs()
    nvs.data["sched"] = 1
    rp = RadarStub(g, nvs, boot)
    rp.nvs = nvs
    b = Broker()
    return rp, b, Mqtt(rp, b)


# ============================================================================ behaviour tests
def test_connect_order():
    print("connect path order")
    rp, b, m = make()
    m.update(2000, wifi_ok=True, sock_ok=False)
    ops = b.ops
    kinds = [o[0] for o in ops]
    first_sub = kinds.index("sub")
    before = [o for o in ops[:first_sub] if o[0] == "pub"]
    disc = [o for o in before if "/config" in o[1]]
    clears = [o for o in before if o[1] in COMMAND_TOPICS]
    check(len(disc) == 14 and all(o[3] for o in disc), "14 retained discovery publishes (4 sensors + 10 radar) before the subscriptions")
    check([o[1].split("Led_Matrix_Node_")[1].split("/")[0] for o in disc] == DISCOVERY_IDS, "discovery order")
    check(len(clears) == 6 and all(o[2] == "" and o[3] is True for o in clears), "six empty retained publishes to the command topics, before the subscribe")
    check(all(ops.index(c) < first_sub for c in clears), "clearRetainedCommands() before the SUBSCRIBE")
    check(max(ops.index(d) for d in disc) < min(ops.index(c) for c in clears), "the discovery burst comes before clearRetainedCommands()")
    subs = [o[1] for o in ops if o[0] == "sub"]
    check(subs == DISPLAY_TOPICS + SUBSCRIPTIONS_RADAR and len(subs) == 13, "13 subscriptions: 6 display + 6 radar + homeassistant/status")
    states = [o[1] for o in ops if o[0] == "pub" and o[1] in STATE_ORDER]
    check(states == STATE_ORDER, "the 11 retained states in the documented order (presence before observing)")
    check(all(o[3] for o in ops if o[0] == "pub" and o[1] in STATE_ORDER), "states retained")
    check(ops.index(("pub", T["status"], "online", True)) == len(ops) - 1, "'online' last")
    check(rp.g.ha_state == HA_UNKNOWN, "haState UNKNOWN at connect, before any status")
    check(m.last_discovery == 2000, "discovery time recorded")
    check(all(o[1] not in COMMAND_TOPICS or o[2] == "" for o in ops if o[0] == "pub"), "no radar command is ever published with a payload")


def burst_steps(ops):
    """The step of every packet of a connect burst: discovery, command clear, subscribe, states or online."""
    steps = []
    for o in ops:
        if o[0] == "sub":
            steps.append(STEP_SUBSCRIBE)
        elif "/config" in o[1]:
            steps.append(STEP_DISCOVERY)
        elif o[1] in COMMAND_TOPICS:
            steps.append(STEP_CLEAR)
        elif o[1] == T["status"]:
            steps.append(STEP_ONLINE)
        else:
            steps.append(STEP_STATES)
    return steps


def clean_connect():
    """The packets of one normal connect burst: a fresh model, connected at 2000 ms."""
    rp, b, m = make()
    m.update(2000, wifi_ok=True, sock_ok=False)
    return list(b.ops)


def refused(step):
    return ["[MQTT] %s refused, closing the connection" % step]


def test_connect_burst_refused():
    print("connect burst: a refused packet ends it, the connection is closed, the backoff repeats the whole burst")
    clean = clean_connect()
    steps = burst_steps(clean)
    total = len(clean)
    golden = ([("pub", "homeassistant/x/Led_Matrix_Node_%s/config" % i, "{}", True) for i in DISCOVERY_IDS]
              + [("pub", t, "", True) for t in COMMAND_TOPICS]
              + [("sub", t, None, None) for t in DISPLAY_TOPICS + SUBSCRIPTIONS_RADAR])
    check(clean[:len(golden)] == golden, "a normal burst: discovery, command clears and subscriptions, same packets in the same order")
    check([o[1] for o in clean[len(golden):]] == STATE_ORDER + [T["status"]] and all(o[3] for o in clean[len(golden):]),
          "... then the 11 retained states, 'online' last")
    check(total == 45 and [steps.count(s) for s in (STEP_DISCOVERY, STEP_CLEAR, STEP_SUBSCRIBE, STEP_STATES, STEP_ONLINE)] == [14, 6, 13, 11, 1],
          "a normal burst is 45 packets: 14 discovery, 6 clears, 13 subscribes, 11 states, 'online'")
    rp, b, m = make()
    m.update(2000, True, False)
    check(m.npub == total and m.stall_ms == 0 and b.closes == 0 and m.connected and m.log == [], "a normal burst is not aborted and blocks nothing")

    bad = {k: [] for k in ("attempts", "ops", "online", "closed", "log", "stamp", "dirty", "stall", "backoff", "ha", "retry", "state")}
    for accepted in range(total):                          # `accepted` packets go out, the next one is refused
        rp, b, m = make()
        m.fail_publish_after = accepted + 1
        m.update(2000, wifi_ok=True, sock_ok=False)
        want_stamp = 2000 if steps[accepted] == STEP_ONLINE else 0       # set just before 'online'
        if m.npub != accepted + 1:
            bad["attempts"].append(accepted)               # nothing is attempted after the first refusal
        if b.ops != clean[:accepted]:
            bad["ops"].append(accepted)
        if any(o[1] == T["status"] for o in b.ops):
            bad["online"].append(accepted)
        if m.connected or b.closes != 1:
            bad["closed"].append(accepted)
        if m.log != refused(steps[accepted]):
            bad["log"].append(accepted)
        if m.last_discovery != want_stamp or m.birth_pending:
            bad["stamp"].append(accepted)
        if not rp.g.state_dirty:
            bad["dirty"].append(accepted)
        if m.stall_ms != WRITE_TIMEOUT_MS:
            bad["stall"].append(accepted)                  # one refused write, ~10 s: not one per remaining packet
        m.task_loop(2040)
        m.update(2000 + MQTT_RECONNECT_MIN_MS - 1, wifi_ok=True, sock_ok=True)
        if m.npub != accepted + 1 or len(b.ops) != accepted or b.closes != 1 or m.connected:
            bad["backoff"].append(accepted)                # no attempt before the backoff is over
        if rp.g.ha_state != HA_UNKNOWN:
            bad["ha"].append(accepted)                     # the loss check of update() is reached
        m.fail_publish_after = None
        m.update(2000 + MQTT_RECONNECT_MIN_MS, wifi_ok=True, sock_ok=True)
        if b.ops[accepted:] != clean or not m.connected or b.closes != 1:
            bad["retry"].append(accepted)                  # the whole burst again, identical to a normal one
        if m.last_discovery != 2000 + MQTT_RECONNECT_MIN_MS or rp.g.state_dirty or m.log != refused(steps[accepted]):
            bad["state"].append(accepted)
    check(not bad["attempts"], "no publish or subscribe is attempted after the first refused one (refused after %s packets)" % bad["attempts"])
    check(not bad["ops"], "the packets before the refusal are the normal burst's, in order (refused after %s)" % bad["ops"])
    check(not bad["online"], "'online' is never published by a burst that was refused (refused after %s)" % bad["online"])
    check(not bad["closed"], "the connection is closed exactly once, the model is not connected (refused after %s)" % bad["closed"])
    check(not bad["log"], "one log line naming the step (refused after %s)" % bad["log"])
    check(not bad["stamp"], "the discovery timestamp is not set (the birth skip window), no birth is pending (refused after %s)" % bad["stamp"])
    check(not bad["dirty"], "the retained-state dirty flag is set (refused after %s)" % bad["dirty"])
    check(not bad["stall"], "MqttTask blocks for one refused write only, not for every remaining packet (refused after %s)" % bad["stall"])
    check(not bad["backoff"], "nothing is sent before MQTT_RECONNECT_MIN_MS is over (refused after %s)" % bad["backoff"])
    check(not bad["ha"], "the loop reaches the loss check: HA state UNKNOWN (refused after %s)" % bad["ha"])
    check(not bad["retry"], "the next attempt after the backoff sends the whole burst again, identical to a normal one (refused after %s)" % bad["retry"])
    check(not bad["state"], "... and records the discovery time, clears the dirty flag (refused after %s)" % bad["state"])
    # the case of the review: the send buffer is full after the 13th discovery packet of the burst
    rp, b, m = make()
    m.fail_publish_after = 14
    m.update(2000, True, False)
    check(m.npub == 14 and len(b.ops) == 13 and m.stall_ms == WRITE_TIMEOUT_MS and m.log == refused(STEP_DISCOVERY),
          "a link that stalls after 13 discovery packets: 1 refused write (10 s) instead of %d" % (total - 13))

    # a refused connect during the retry keeps the backoff rules of P1
    rp, b, m = make()
    m.fail_publish_after = 21                              # the first SUBSCRIBE is refused
    m.update(2000, True, False)
    m.fail_publish_after = None
    m.update(4000, True, True, broker_accepts=False)
    check(not m.connected and m.reconnect_delay == 2 * MQTT_RECONNECT_MIN_MS, "a refused connect after an aborted burst doubles the backoff as before")
    m.update(4000 + 2 * MQTT_RECONNECT_MIN_MS - 1, True, True)
    check(not m.connected, "... and the next attempt waits for it")
    m.update(4000 + 2 * MQTT_RECONNECT_MIN_MS, True, True)
    check(m.connected and b.ops[20:] == clean, "... then the whole burst goes out")


def test_birth_resend_refused():
    print("HA-birth resend: a refused packet ends it the same way")

    def live_birth():
        rp, b, m = make()
        m.update(2000, wifi_ok=True, sock_ok=False)
        m.rng = lambda n: 1234
        m.on_message(T["ha"], "online", 70000)             # a live birth, long after the skip window
        return rp, b, m
    clean = clean_connect()
    rp, b, m = live_birth()
    n0 = len(b.ops)
    m.service_radar_states(70000 + 1234)
    resend = b.ops[n0:]
    steps = [STEP_BIRTH_DISCOVERY] * 14 + [STEP_BIRTH_STATES] * 11
    check(len(resend) == 25 and [o[1] for o in resend[14:]] == STATE_ORDER and m.last_discovery == 71234 and m.connected and b.closes == 0 and m.log == [],
          "a normal resend is 14 discovery configs, then the 11 states; nothing is aborted")
    bad = {k: [] for k in ("attempts", "ops", "closed", "log", "stamp", "dirty", "stall", "ha", "retry", "skip")}
    for accepted in range(len(resend)):
        rp, b, m = live_birth()
        n0, p0 = len(b.ops), m.npub
        m.fail_publish_after = p0 + accepted + 1
        m.service_radar_states(71234)
        if m.npub - p0 != accepted + 1:
            bad["attempts"].append(accepted)
        if b.ops[n0:] != resend[:accepted]:
            bad["ops"].append(accepted)
        if m.connected or b.closes != 1:
            bad["closed"].append(accepted)
        if m.log != refused(steps[accepted]):
            bad["log"].append(accepted)
        if m.last_discovery != 2000 or m.birth_pending:
            bad["stamp"].append(accepted)                  # the connect's stamp stays; the resend is not retried by itself
        if not rp.g.state_dirty:
            bad["dirty"].append(accepted)
        if m.stall_ms != WRITE_TIMEOUT_MS:
            bad["stall"].append(accepted)
        m.fail_publish_after = None                        # the link is back for the next MqttTask pass
        n1 = len(b.ops)
        m.update(71274, wifi_ok=True, sock_ok=True)        # the loss check, then the reconnect: the backoff of the connect 70 s ago is over
        if rp.g.ha_lost_ms != 71274 or rp.g.ha_state != HA_UNKNOWN:
            bad["ha"].append(accepted)                     # the loss check runs in the very next pass: the grace counts from there
        if n1 != n0 + accepted or b.ops[n1:] != clean or not m.connected or b.closes != 1 or m.last_discovery != 71274:
            bad["retry"].append(accepted)                  # the whole connect burst, not only the resend
        m.on_message(T["ha"], "online", 71274 + 5)
        if m.birth_pending or rp.g.ha_state != HA_ONLINE:
            bad["skip"].append(accepted)                   # the retained 'online' after the new subscribe triggers no second resend
    check(not bad["attempts"], "no publish is attempted after the first refused one (refused after %s packets)" % bad["attempts"])
    check(not bad["ops"], "the packets before the refusal are the normal resend's (refused after %s)" % bad["ops"])
    check(not bad["closed"], "the connection is closed exactly once (refused after %s)" % bad["closed"])
    check(not bad["log"], "one log line naming the step (refused after %s)" % bad["log"])
    check(not bad["stamp"], "the discovery timestamp is not refreshed, no resend is pending (refused after %s)" % bad["stamp"])
    check(not bad["dirty"], "the retained-state dirty flag is set (refused after %s)" % bad["dirty"])
    check(not bad["stall"], "MqttTask blocks for one refused write only (refused after %s)" % bad["stall"])
    check(not bad["ha"], "the next pass reaches the loss check: HA state UNKNOWN, the grace counts from that moment (refused after %s)" % bad["ha"])
    check(not bad["retry"], "the reconnect sends the whole connect burst, not only the resend (refused after %s)" % bad["retry"])
    check(not bad["skip"], "the retained 'online' after that reconnect is inside the skip window: no second resend (refused after %s)" % bad["skip"])
    # the link is still stalled at the next passes: one refused packet per attempt, the attempts a backoff apart
    rp, b, m = live_birth()
    m.fail_publish_after = m.npub + 6                      # refused after 5 discovery configs
    m.service_radar_states(71234)
    seen, attempts = m.npub, []
    for t in range(71274, 71274 + 20000, 40):
        m.task_loop(t)
        if m.npub != seen:
            attempts.append(t)
            seen = m.npub
    gaps = [y - x for x, y in zip(attempts, attempts[1:])]
    check(attempts[:1] == [71274] and gaps and min(gaps) >= MQTT_RECONNECT_MIN_MS and len(attempts) == 10,
          "a stalled link after the birth abort: the first retry is the next pass, then one attempt per backoff (%s)" % attempts[:3])
    check(m.stall_ms == (1 + len(attempts)) * WRITE_TIMEOUT_MS and m.birth_pending is False, "... one refused write each")


def test_stalled_link_loop():
    print("a link that stays stalled: one refused packet per attempt, a backoff apart, the loop keeps running")
    rp, b, m = make()
    m.fail_publish_after = 1
    attempts, seen = [], 0
    for t in range(2000, 62000, 40):                       # one MqttTask pass per 40 ms
        m.task_loop(t)
        if m.npub != seen:
            attempts.append(t)
            seen = m.npub
    gaps = [y - x for x, y in zip(attempts, attempts[1:])]
    check(len(attempts) == 30 and gaps and min(gaps) >= MQTT_RECONNECT_MIN_MS, "30 attempts in 60 s, at least MQTT_RECONNECT_MIN_MS apart (%d)" % len(attempts))
    check(m.npub == len(attempts) and m.stall_ms == len(attempts) * WRITE_TIMEOUT_MS, "each attempt costs one refused packet, one write timeout")
    check(b.ops == [] and b.closes == len(attempts), "nothing reached the broker, no 'online'; every attempt closed its connection")
    check(rp.g.ha_state == HA_UNKNOWN and not m.connected, "HA stays UNKNOWN, the node is not connected")
    m.fail_publish_after = None                            # the link recovers
    m.update(62000, wifi_ok=True, sock_ok=True)
    check(m.connected and b.ops == clean_connect() and b.ops[-1] == ("pub", T["status"], "online", True),
          "when the link recovers the next attempt sends the whole burst")


def test_ha_state_and_birth():
    print("HA liveness, lost connection, birth resend")
    rp, b, m = make(boot=1000)
    rp.set_ha_state(HA_ONLINE, 1500)
    check(rp.g.ha_state == HA_ONLINE, "ONLINE")
    m.update(5000, wifi_ok=False, sock_ok=False)
    check(rp.g.ha_state == HA_UNKNOWN and rp.g.ha_lost_ms == 5000, "Wi-Fi lost -> UNKNOWN at once, haLostMs = the moment of the loss")
    m.update(6000, wifi_ok=False, sock_ok=False)
    check(rp.g.ha_lost_ms == 5000, "repeated calls keep haLostMs")
    # a dead socket with Wi-Fi up (broker down): UNKNOWN as well, and a reconnect that is refused keeps it
    rp.set_ha_state(HA_ONLINE, 7000)
    m.update(8000, wifi_ok=True, sock_ok=False, broker_accepts=False)
    check(rp.g.ha_state == HA_UNKNOWN and rp.g.ha_lost_ms == 8000, "a lost MQTT connection counts as HA down too (the grace starts at the loss)")
    # reconnect, retained 'online' right after the subscribe: no resend
    m.update(60000, wifi_ok=True, sock_ok=False)
    m.on_message(T["ha"], "online", 60005)
    check(rp.g.ha_state == HA_ONLINE and not m.birth_pending, "the retained online right after the connect triggers no second discovery")
    m.on_message(T["ha"], "offline", 61000)
    check(rp.g.ha_state == HA_OFFLINE, "offline")
    m.on_message(T["ha"], "online", 62000)
    check(not m.birth_pending, "an online within 5 s of the discovery burst is skipped")
    m.on_message(T["ha"], "garbage", 62500)
    check(rp.g.ha_state == HA_ONLINE, "an unknown status payload changes nothing")
    # a live birth later, with haState already ONLINE: one resend after a random 0-2 s, from the loop
    rp.set_ha_state(HA_ONLINE, 70000)
    m.rng = lambda n: 1234
    ops_before = len(b.ops)
    m.on_message(T["ha"], "online", 70000)
    check(m.birth_pending and len(b.ops) == ops_before, "live online while ONLINE: scheduled, nothing published from the callback")
    m.service_radar_states(70000 + 1233)
    check(len(b.ops) == ops_before, "not before the random delay")
    m.service_radar_states(70000 + 1234)
    disc = [o for o in b.ops[ops_before:] if "/config" in o[1]]
    check(len(disc) == 14 and not m.birth_pending, "after the delay: discovery (14) and states from the loop")
    check([o[1] for o in b.ops[ops_before + 14:] if o[1] in STATE_ORDER] == STATE_ORDER, "... followed by the 11 states")
    # the random delay stays inside 0..MQTT_BIRTH_RESEND_MAX_DELAY_MS (esp_random() % MAX)
    check(all(0 <= (r % MQTT_BIRTH_RESEND_MAX_DELAY_MS) < MQTT_BIRTH_RESEND_MAX_DELAY_MS for r in (0, 1, 2 ** 32 - 1)), "delay range")
    # millis() wrap: connect just before the wrap, birth after it
    rp2, b2, m2 = make(boot=(1 << 32) - 20000)
    m2.update((1 << 32) - 10000, wifi_ok=True, sock_ok=False)
    m2.on_message(T["ha"], "online", ((1 << 32) - 10000 + 6000) & U32)
    check(m2.birth_pending, "wrap: 6 s after the burst the online triggers a resend")
    m2.birth_at = ((1 << 32) - 4000) & U32
    m2.birth_delay = 5000
    m2.service_radar_states(1000)
    check(not m2.birth_pending, "wrap: the elapsed-time comparison fires across the wrap")
    rp3, b3, m3 = make()
    m3.update(2000, wifi_ok=True, sock_ok=False)
    m3.service_radar_states(3000)
    check(rp3.g.ha_state == HA_UNKNOWN, "no status after the connect: stays UNKNOWN (counts as down after the grace)")


def test_dirty_protocol():
    print("dirty flag: exchange first, retry after a failed publish, presence on change")
    rp, b, m = make()
    m.update(2000, wifi_ok=True, sock_ok=False)
    m.service_radar_states(2100)
    n = len(b.ops)
    m.service_radar_states(2140)
    check(len(b.ops) == n, "no dirty flag, no publish")
    rp.request(CMD_ENABLE, 0)
    rp.pass_(2200)
    check(rp.g.state_dirty, "RadarPower set the flag")
    m.service_radar_states(2240)
    check(not rp.g.state_dirty and [o[1] for o in b.ops[n:] if o[1] in STATE_ORDER] == STATE_ORDER, "published once, flag cleared")
    rp.request(CMD_SCHEDULE, 0)
    rp.pass_(2300)
    m.fail_publish_after = m.npub + 3
    ops_before = len(b.ops)
    m.service_radar_states(2340)
    check(rp.g.state_dirty and len(b.ops) - ops_before == 2, "the 3rd publish fails: the rest is skipped, the flag is set again")
    check(m.connected and b.closes == 0, "a refused steady-state publish keeps the connection: only the connect burst and the birth resend close it")
    m.fail_publish_after = None
    m.service_radar_states(2380)
    check(not rp.g.state_dirty, "the next loop publishes everything")
    # a change that lands while the snapshot is being published (flag cleared first) goes out one loop later
    rp.request(CMD_LEGACY, 1)
    rp.pass_(2400)
    snapshot_flag = rp.g.state_dirty
    rp.g.state_dirty = False
    rp.request(CMD_LEGACY, 0)                         # RadarPower runs during the publish ...
    rp.pass_(2410)
    check(snapshot_flag and rp.g.state_dirty, "a change during the publish sets the flag again")
    n = len(b.ops)
    m.service_radar_states(2440)
    check(any(o[1] == T["legacy_state"] and o[2] == "0" for o in b.ops[n:]), "... and the broker ends with the latest state")
    # presence on change, only while observing
    rp2, b2, m2 = make(ready=True)
    m2.update(2000, True, False)
    rp2.pass_(2010)
    rp2.pass_(14100)
    m2.service_radar_states(14140)
    check(rp2.snapshot().observing, "observing")
    n = len(b2.ops)
    m2.live["presence"] = PRESENT
    m2.service_radar_states(14180)
    check(b2.ops[n:] == [("pub", T["presence"], "ON", True)], "presence ON published on change")
    m2.service_radar_states(14220)
    check(len(b2.ops) == n + 1, "no republish without a change (no heartbeat)")
    m2.live["presence"] = ABSENT
    m2.live["target"] = False
    m2.service_radar_states(14260)
    check(b2.ops[-1] == ("pub", T["presence"], "OFF", True), "presence OFF on change")
    check(STATE_ORDER.index(T["presence"]) < STATE_ORDER.index(T["observing"]), "presence precedes observing in publishRadarStates()")
    rp3, b3, m3 = make()
    m3.live["target"] = True
    m3.update(2000, True, False)
    pres = [o for o in b3.ops if o[1] == T["presence"]]
    check(pres and pres[-1][2] == "OFF", "presence OFF while not observing, even with a target")
    # an LD2454 target alone (LD2412 ABSENT) is enough while observing
    m2.live["target"] = True
    m2.service_radar_states(14300)
    check(b2.ops[-1] == ("pub", T["presence"], "ON", True), "an LD2454 target makes presence ON")
    # the hold: a target that flickers out and back within RADAR_PRESENCE_HOLD_MS never turns the presence OFF
    n = len(b2.ops)
    m2.live["target"] = False
    for tt in range(14340, 14300 + RADAR_PRESENCE_HOLD_MS - 40, 40):
        m2.service_radar_states(tt)
    check(len(b2.ops) == n, "a target gone for less than the hold: no OFF is published")
    m2.live["target"] = True
    m2.service_radar_states(14300 + RADAR_PRESENCE_HOLD_MS - 20)
    m2.live["target"] = False
    check(len(b2.ops) == n, "the flicker back: still ON, nothing published")
    last = 14300 + RADAR_PRESENCE_HOLD_MS - 20
    m2.service_radar_states(last + RADAR_PRESENCE_HOLD_MS - 40)
    check(len(b2.ops) == n, "the hold counts from the last target, not from the first")
    m2.service_radar_states(last + RADAR_PRESENCE_HOLD_MS)
    check(b2.ops[-1] == ("pub", T["presence"], "OFF", True), "OFF once RADAR_PRESENCE_HOLD_MS passed without a target")
    # an LD2412 PRESENT is not held (it has its own 2 s rule): it falls with the pin
    m2.live["presence"] = PRESENT
    m2.service_radar_states(last + RADAR_PRESENCE_HOLD_MS + 40)
    check(b2.ops[-1] == ("pub", T["presence"], "ON", True), "LD2412 PRESENT: ON")
    m2.live["presence"] = ABSENT
    m2.service_radar_states(last + RADAR_PRESENCE_HOLD_MS + 80)
    check(b2.ops[-1] == ("pub", T["presence"], "OFF", True), "LD2412 ABSENT: OFF at once (no hold)")
    # losing 'observing' drops the hold: a target seen just before a park does not come back as presence
    m2.live["target"] = True
    m2.service_radar_states(last + RADAR_PRESENCE_HOLD_MS + 120)
    check(b2.ops[-1] == ("pub", T["presence"], "ON", True), "target again: ON")
    m2.live["target"] = False
    check(m2.presence_now(type("S", (), {"observing": False})()) is False and not m2.target_held,
          "not observing: presence OFF and the hold is cleared")
    # the millis() wrap inside a hold
    m4 = Mqtt(rp2, Broker())
    m4.clock = 0xFFFFFFF0
    s_obs = type("S", (), {"observing": True})()
    m4.live["target"] = True
    check(m4.presence_now(s_obs), "wrap: target seen")
    m4.live["target"] = False
    m4.clock = (0xFFFFFFF0 + RADAR_PRESENCE_HOLD_MS - 1) & 0xFFFFFFFF
    check(m4.presence_now(s_obs), "wrap: still held 1 ms before the end")
    m4.clock = (0xFFFFFFF0 + RADAR_PRESENCE_HOLD_MS) & 0xFFFFFFFF
    check(not m4.presence_now(s_obs), "wrap: released at the end of the hold")


def test_legacy_topics():
    print("LD2412 legacy topic, legacy JSON")
    rp, b, m = make()
    m.update(2000, True, False)
    pubs = lambda: [o[2] for o in b.ops if o[1] == T["ld2412"]]
    m.live["presence"] = UNKNOWN
    m.publish_legacy(2100)
    check(pubs() == ["None"], "None once on entering UNKNOWN (retained)")
    check(all(o[3] for o in b.ops if o[1] == T["ld2412"]), "retained")
    for t in range(2200, 2200 + 600000, 1000):
        m.publish_legacy(t)
    check(pubs() == ["None"], "no heartbeat while UNKNOWN (10 min)")
    m.live["presence"] = ABSENT
    m.publish_legacy(700000)
    check(pubs() == ["None", "0"], "0 on the change to ABSENT")
    for t in range(700000 + 1000, 700000 + 130000, 1000):
        m.publish_legacy(t)
    check(pubs() == ["None", "0", "0", "0"], "60 s heartbeat while ABSENT")
    m.live["presence"] = PRESENT
    m.publish_legacy(900000)
    check(pubs()[-1] == "1", "1 on PRESENT")
    m.live["presence"] = UNKNOWN
    m.publish_legacy(901000)
    check(pubs()[-1] == "None", "None again on the next UNKNOWN (a park)")
    m.ld2412_published = -1
    m.publish_legacy(902000)
    check(pubs()[-2:] == ["None", "None"], "re-sent after a reconnect")
    rp2, b2, m2 = make(ready=True)
    m2.update(2000, True, False)
    rp2.pass_(2010)
    rp2.pass_(14100)
    s = rp2.snapshot()
    check(s.observing and not s.legacy, "observing, legacy off")
    m2.live["target"] = True
    for t in range(14100, 24000, 40):
        m2.publish_legacy(t)
    check(m2.legacy_sent == 0, "legacy JSON never sent while the switch is off (default)")
    rp2.request(CMD_LEGACY, 1)
    rp2.pass_(24000)
    for t in range(24000, 34000, 40):
        m2.publish_legacy(t)
    check(9 <= m2.legacy_sent <= 11, "with targets: 1 Hz (%d in 10 s)" % m2.legacy_sent)
    m2.live["target"] = False
    sent = m2.legacy_sent
    for t in range(34000, 94000, 40):
        m2.publish_legacy(t)
    check(1 <= m2.legacy_sent - sent <= 3, "no targets: 30 s heartbeat (%d in 60 s)" % (m2.legacy_sent - sent))
    # not observing: the legacy JSON stays silent although the switch is on
    rp3, b3, m3 = make(ready=False)
    m3.update(2000, True, False)
    rp3.request(CMD_LEGACY, 1)
    rp3.pass_(2010)
    rp3.pass_(14100)
    m3.live["target"] = True
    for t in range(14100, 34000, 40):
        m3.publish_legacy(t)
    check(m3.legacy_sent == 0, "legacy JSON stays silent while the radars do not observe")


def test_frames_rules():
    print("frames: idle by phase, field omission, the design's examples")
    idle = lambda phase: LD2454_STREAM_OFF_IDLE_MS if phase == OFF else LD2454_STREAM_IDLE_MS
    check([idle(p) for p in (ON, PARKING, OFF_SETTLING, OFF, POWERING)] == [5000, 5000, 5000, 30000, 5000],
          "30 s heartbeat only in phase OFF")
    off_hb = '{"v":1,"b":"9f3c21aa","t":81244567,"s":1234615,"n":0' + frames_fields(0, "X") + '}'
    check(off_hb == '{"v":1,"b":"9f3c21aa","t":81244567,"s":1234615,"n":0,"pw":0,"sm":"X"}', "the OFF heartbeat has no mt / p2")
    check(frames_fields(3, "A") == ',"mt":1,"p2":1,"pw":3,"sm":"A"', "pw 3: mt and p2")
    check(frames_fields(1, "S") == ',"mt":1,"pw":1,"sm":"S"', "pw 1: mt only")
    check(frames_fields(2, "A") == ',"p2":1,"pw":2,"sm":"A"', "pw 2: p2 only")
    check(frames_fields(0, "N") == ',"pw":0,"sm":"N"', "night heartbeat")
    rp, b, m = make()
    s = rp.snapshot()
    check(s.sm in "ANFXS" and 0 <= s.pw <= 3, "snapshot sm / pw valid")
    check(s.pw == 0, "pw is 0 until the radars have warmed up")


def test_commands():
    print("callback: payload handling, no publish, no NVS")
    rp, b, m = make()
    writes = len(rp.nvs.writes)
    for payload, expect in (("1", 1), ("0", 0), ("ON", 1), ("off", 0), ("On", 1), ("auto", None), ("2", None), ("", None), ("1 ", None)):
        rp.pending = [None] * CMD_COUNT
        m.on_message(T["power_cmd"], payload, 1000)
        check(rp.pending[CMD_ENABLE] == expect, "power payload %r -> %r" % (payload, rp.pending[CMD_ENABLE]))
    check(len(rp.nvs.writes) == writes, "the callback never writes NVS")
    for payload, expect in (("23:00:00", 1380), ("06:00", 360), ("0:00", 0), ("24:00:00", None), ("None", None), ("", None), ("12:0", None)):
        rp.pending = [None] * CMD_COUNT
        m.on_message(T["ns_cmd"], payload, 1000)
        check(rp.pending[CMD_NIGHT_START] == expect, "night_start %r -> %r" % (payload, rp.pending[CMD_NIGHT_START]))
    rp.pending = [None] * CMD_COUNT
    m.on_message(T["legacy_cmd"], "1", 1000)
    m.on_message(T["offl_cmd"], "0", 1000)
    m.on_message(T["sched_cmd"], "ON", 1000)
    m.on_message(T["ne_cmd"], "07:30:00", 1000)
    check(rp.pending[CMD_LEGACY] == 1 and rp.pending[CMD_SCHED_OFFLINE] == 0 and rp.pending[CMD_SCHEDULE] == 1
          and rp.pending[CMD_NIGHT_END] == 450, "the other command topics")
    # the echo of an unchanged command: no NVS write, the flag is set
    rp.pending = [None] * CMD_COUNT
    rp.request(CMD_ENABLE, 1)
    rp.pass_(1100)
    rp.g.state_dirty = False
    w = len(rp.nvs.writes)
    rp.request(CMD_ENABLE, 1)
    rp.pass_(1200)
    check(len(rp.nvs.writes) == w and rp.g.state_dirty, "a repeated command writes nothing and is echoed")
    # the retained-command clearing delivers an empty payload if the broker still sent one: ignored
    rp.pending = [None] * CMD_COUNT
    m.on_message(T["power_cmd"], "", 1300)
    check(rp.pending == [None] * CMD_COUNT, "an empty (cleared retained) command is ignored")


# ============================================================================ links to the C++
def cpp_variant(rel, defines):
    return strip_comments(preprocess(read_src(rel), defines))


def test_cpp_links():
    print("C++ link: mqtt_manager.cpp and ld2454_frames.cpp (BOARD_REV2 variant), constants")
    rev2 = cpp_variant("src/mqtt_manager.cpp", {"BOARD_REV2"})
    default = cpp_variant("src/mqtt_manager.cpp", set())

    # constants
    check(MQTT_BIRTH_RESEND_MAX_DELAY_MS == 2000 and MQTT_BIRTH_SKIP_MS == 5000, "birth constants 2000 / 5000 ms")
    check(LD2412_HEARTBEAT_MS == 60000 and LEGACY_HEARTBEAT_MS == 30000 and LD2454_STREAM_OFF_IDLE_MS == 30000
          and LD2454_STREAM_IDLE_MS == 5000 and MQTT_RADAR_PUBLISH_INTERVAL == 1000, "heartbeat constants")
    check(RADAR_ATTR_JSON_MAX == 256, "RADAR_ATTR_JSON_MAX 256")
    check(ENV["RADAR_MQTT_EXTRA_STACK"] == 2048 and ENV["MQTT_TASK_STACK_SIZE"] == 8192, "MqttTask stack 8192 + 2048 under REV2")
    check("MQTT_TASK_STACK_SIZE + RADAR_MQTT_EXTRA_STACK" in flat(rev2), "initMqttTask() adds RADAR_MQTT_EXTRA_STACK under REV2")
    check("RADAR_MQTT_EXTRA_STACK" not in default, "the default build keeps the P1 stack")

    # subscriptions: REV2 returns false at the first refused SUBSCRIBE, the default build is the P1 text
    body = extract_function(rev2, "bool MqttManager::subscribeTopics(")
    subs = [ENV[a[0]] for n, a in parse_calls(body, ["_client.subscribe"])]
    check(subs == DISPLAY_TOPICS + SUBSCRIPTIONS_RADAR, "subscribeTopics(): six display topics, then the six radar commands and homeassistant/status")
    check(not unused_results(body, ["_client.subscribe"]) and flat(body).startswith("bool MqttManager::subscribeTopics() { return _client.subscribe("),
          "subscribeTopics(): every SUBSCRIBE result is used (return / &&): the first refusal skips the rest")
    body_d = extract_function(default, "void MqttManager::subscribeTopics(")
    check([ENV[a[0]] for n, a in parse_calls(body_d, ["_client.subscribe"])] == DISPLAY_TOPICS, "default build: the six display topics only")
    check(len(unused_results(body_d, ["_client.subscribe"])) == 6 and "return" not in body_d, "default build: subscribeTopics() is the P1 text (void, results ignored)")

    # clearRetainedCommands
    body = extract_function(rev2, "bool MqttManager::clearRetainedCommands(")
    arr = re.search(r"cmdTopics\[\]\s*=\s*\{([^}]*)\}", body)
    names = re.findall(r"\w+", arr.group(1)) if arr else []
    check([ENV[n] for n in names] == COMMAND_TOPICS, "clearRetainedCommands(): the six command topics, nothing else")
    check('_client.publish(t, (const uint8_t*)"", 0, true)' in flat(body), "clearRetainedCommands(): empty payload, retained")
    check("if (!_client.publish(t, (const uint8_t*)\"\", 0, true)) return false;" in flat(body) and flat(body).endswith("return true; }"),
          "clearRetainedCommands(): false at the first refused publish, the rest is not sent")

    # update(): order
    upd = extract_function(rev2, "void MqttManager::update(")
    f = flat(upd)
    i_loss = f.find("radarSetHaState(RADAR_HA_UNKNOWN)")
    i_wifi = f.find("if (WiFi.status() != WL_CONNECTED) {")
    check(0 <= i_loss < i_wifi and "WiFi.status() != WL_CONNECTED || !_client.connected()" in f[:i_wifi],
          "update(): the lost-connection check comes before the Wi-Fi early return")
    chain = ["_client.connect(", "Serial.println(\"MQTT connected\")", "_ld2412Published = -1;", "radarSetHaState(RADAR_HA_UNKNOWN)",
             "_presencePublished = -1;", "if (!sendDiscovery())", "if (!clearRetainedCommands())", "if (!subscribeTopics())",
             "g_stateDirty = false;", "if (!publishRadarStates(radarStatusSnapshot()))", "_lastDiscoveryMs = millis();",
             "if (!_client.publish(MQTT_STATUS_TOPIC, \"online\", true))"]
    pos = [f.find(c, i_wifi) for c in chain]
    check(all(p >= 0 for p in pos) and pos == sorted(pos),
          "update(): connect path order connect, HA UNKNOWN, discovery, clear retained, subscribe, states, 'online' last (%s)" % pos)
    steps = re.findall(r'if \(!([^;{}]+?)\) \{ abortBurst\("([^"]+)"\); return; \}', f)
    check(steps == [("sendDiscovery()", STEP_DISCOVERY), ("clearRetainedCommands()", STEP_CLEAR), ("subscribeTopics()", STEP_SUBSCRIBE),
                    ("publishRadarStates(radarStatusSnapshot())", STEP_STATES), ('_client.publish(MQTT_STATUS_TOPIC, "online", true)', STEP_ONLINE)],
          "update(): each burst step is checked, the first refusal calls abortBurst(<step>) and returns (%s)" % [s[1] for s in steps])
    after = f[f.find('Serial.println("MQTT connected")'):f.find("} else {", f.find('Serial.println("MQTT connected")'))]
    check(f.count("abortBurst(") == 5 and after.count("return;") == 5 and "g_stateDirty = true" not in after,
          "update(): the connect branch returns only through the five abortBurst() calls (a refusal skips the retained 'online' and _client.loop())")
    check("g_stateDirty = false; if (!publishRadarStates(radarStatusSnapshot())) { abortBurst(" in f,
          "update(): the dirty flag is cleared before the states; a refused state publish aborts instead of being retried on this socket")
    check("if (nowMs - _lastConnectAttemptMs < _reconnectDelayMs) { return; } _lastConnectAttemptMs = nowMs;" in f
          and "_reconnectDelayMs = MQTT_RECONNECT_MIN_MS;" in f
          and "_reconnectDelayMs = constrain(_reconnectDelayMs * 2, MQTT_RECONNECT_MIN_MS, MQTT_RECONNECT_MAX_MS);" in f,
          "update(): the reconnect backoff the model uses (min gap from the last attempt, doubled by a refused connect)")
    # abortBurst(): close the socket, no DISCONNECT, nothing is published
    ab = flat(extract_function(rev2, "void MqttManager::abortBurst("))
    check('Serial.printf("[MQTT] %s refused, closing the connection\\n", step);' in ab and "_espClient.stop();" in ab and "g_stateDirty = true;" in ab,
          "abortBurst(): one log line, the socket closed, the dirty flag set again")
    check(".publish(" not in ab and "disconnect(" not in ab and "delay(" not in ab and "_lastDiscoveryMs" not in ab and "_birthPending" not in ab,
          "abortBurst(): no DISCONNECT (it would block and discard the will), no packet, no delay, the timestamp and _birthPending are left alone")
    # sendDiscovery() and the helpers: every result is used, the first refusal skips the rest
    sd = extract_function(rev2, "bool MqttManager::sendDiscovery(")
    helpers = ["publishConfig", "publishSwitchConfig", "publishTimeConfig", "publishBinaryConfig", "publishEnumSensorConfig"]
    check(len(parse_calls(sd, helpers)) == 14 and not unused_results(sd, helpers) and flat(sd).startswith("bool MqttManager::sendDiscovery() { return publishConfig("),
          "sendDiscovery(): the 14 configs are one && chain: the first refused publish skips the rest")
    for fn in helpers:
        hb = flat(extract_function(rev2, "bool MqttManager::%s(" % fn))
        check(re.search(r"return publishDiscovery\([^;]*\); }$", hb) is not None, "%s(): returns the result of publishDiscovery()" % fn)
    sd_d = extract_function(default, "void MqttManager::sendDiscovery(")
    check(len(unused_results(sd_d, helpers)) == 4 and "return" not in sd_d and "void MqttManager::publishConfig(" in default
          and "return publishDiscovery" not in default, "default build: sendDiscovery() / publishConfig() are the P1 text (void, results ignored)")
    check("abortBurst" not in default and "sendDiscovery(); subscribeTopics(); _client.publish(MQTT_STATUS_TOPIC, \"online\", true);" in flat(default),
          "default build: update() sends the P1 burst unchanged, nothing is checked or closed")

    # the retained states, in order
    body = extract_function(rev2, "bool MqttManager::publishRadarStates(")
    pubs = [a[0] for n, a in parse_calls(body, ["pub"])]
    check([ENV[p] for p in pubs] == STATE_ORDER, "publishRadarStates(): topics and order equal the model (presence before observing)")
    m = re.search(r'snprintf\(attr, sizeof\(attr\),\s*((?:"(?:[^"\\]|\\.)*"\s*)+),', body)
    fmt = "".join(c_unescape(p) for p in re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))) if m else None
    check(fmt == ATTR_FORMAT, "publishRadarStates(): the status JSON format equals the model's\n    C++   %r\n    model %r" % (fmt, ATTR_FORMAT))
    check("pub(MQTT_RADAR_STATE_ATTR_TOPIC, attr)" in flat(body) and "char attr[RADAR_ATTR_JSON_MAX]" in flat(body),
          "publishRadarStates(): the attribute JSON goes to its topic from a RADAR_ATTR_JSON_MAX buffer")
    check("auto pub = [&](const char* topic, const char* payload) { if (ok) ok = _client.publish(topic, payload, true); }" in flat(body),
          "publishRadarStates(): retained, and the rest is skipped after the first failure")

    # callback dispatch table
    cb = extract_function(rev2, "static void onMqttMessageReceived(")
    table = re.findall(r"strcasecmp\(topic, (MQTT_\w+)\) == 0\)\s*(radar(?:Switch|Time)Command)\((RADAR_CMD_\w+),", cb)
    got = {ENV[t]: (fn, cmd) for t, fn, cmd in table}
    want = {T["power_cmd"]: ("radarSwitchCommand", "RADAR_CMD_ENABLE"), T["sched_cmd"]: ("radarSwitchCommand", "RADAR_CMD_SCHEDULE"),
            T["offl_cmd"]: ("radarSwitchCommand", "RADAR_CMD_SCHED_OFFLINE"), T["legacy_cmd"]: ("radarSwitchCommand", "RADAR_CMD_LEGACY"),
            T["ns_cmd"]: ("radarTimeCommand", "RADAR_CMD_NIGHT_START"), T["ne_cmd"]: ("radarTimeCommand", "RADAR_CMD_NIGHT_END")}
    check(got == want, "the callback's command table equals the model's (topic -> parser -> command)")
    check("strcasecmp(topic, MQTT_HA_STATUS_TOPIC) == 0" in flat(cb) and 'strcmp(payloadBuf, "online") == 0) mqttManager.onHaStatus(true)' in flat(cb)
          and 'strcmp(payloadBuf, "offline") == 0) mqttManager.onHaStatus(false)' in flat(cb), "the callback maps online / offline to onHaStatus()")
    cmdfn = extract_function(rev2, "static void radarSwitchCommand(") + extract_function(rev2, "static void radarTimeCommand(")
    check("radarParseOnOff(payload)" in cmdfn and "radarParseTimeOfDay(payload, &minutes)" in cmdfn
          and "radarPowerRequest(cmd, v)" in cmdfn and "radarPowerRequest(cmd, minutes)" in cmdfn, "the command helpers parse with A's parsers and call radarPowerRequest()")
    # nothing in the callback path publishes or touches NVS
    ha = extract_function(rev2, "void MqttManager::onHaStatus(")
    for name, text in (("the callback", cb), ("the command helpers", cmdfn), ("onHaStatus()", ha)):
        check(".publish(" not in text and "Preferences" not in text and "nvs_" not in text and "delay(" not in text and "vTaskDelay" not in text,
              "%s never publishes, writes NVS or blocks" % name)
    check("(uint32_t)(now - _lastDiscoveryMs) >= MQTT_BIRTH_SKIP_MS" in flat(ha) and "esp_random() % MQTT_BIRTH_RESEND_MAX_DELAY_MS" in flat(ha)
          and "_birthPending = true" in flat(ha), "onHaStatus(): the 5 s skip window and the random 0..2 s delay, resend deferred")

    # serviceRadarStates
    svc = flat(extract_function(rev2, "void MqttManager::serviceRadarStates("))
    check(svc.find("g_stateDirty.exchange(false)") < svc.find("radarStatusSnapshot()", svc.find("g_stateDirty.exchange(false)")),
          "serviceRadarStates(): the dirty flag is cleared first, then the snapshot is taken")
    check("if (!publishRadarStates(s)) g_stateDirty = true;" in svc and "(uint32_t)(nowMs - _birthAtMs) >= _birthDelayMs" in svc,
          "serviceRadarStates(): a failed publish sets the flag again; the birth delay is an elapsed-time comparison")
    check("_birthPending = false; if (!sendDiscovery()) { abortBurst(\"%s\"); return; } g_stateDirty = false; "
          "if (!publishRadarStates(radarStatusSnapshot())) { abortBurst(\"%s\"); return; } _lastDiscoveryMs = millis(); return; }"
          % (STEP_BIRTH_DISCOVERY, STEP_BIRTH_STATES) in svc,
          "serviceRadarStates(): the birth resend stops at the first refused packet (abortBurst), the timestamp is set only after a complete resend")
    check("presenceNow(s) ? 1 : 0" in svc and "presenceOn != _presencePublished" in svc, "serviceRadarStates(): presence on change")
    pn = flat(extract_function(rev2, "bool MqttManager::presenceNow("))
    check("if (!s.observing) { _targetHeld = false; return false; }" in pn
          and "if (ld2454Sensor.hasAnyTarget()) { _lastTargetMs = now; _targetHeld = true; }" in pn
          and "else if (_targetHeld && (uint32_t)(now - _lastTargetMs) >= RADAR_PRESENCE_HOLD_MS) { _targetHeld = false; }" in pn
          and "return ld2412Sensor.presence() == Ld2412Presence::PRESENT || _targetHeld;" in pn,
          "presenceNow(): observing and (LD2412 PRESENT or an LD2454 target held for RADAR_PRESENCE_HOLD_MS)")
    task = flat(extract_function(rev2, "void mqttTask("))
    check(task.find("mqttManager.publishLegacyRadar(now);") < task.find("mqttManager.serviceRadarStates(now);") < task.find("vTaskDelay"),
          "mqttTask(): serviceRadarStates() runs in the connected block, after publishLegacyRadar()")

    # legacy topics
    leg = flat(extract_function(rev2, "void MqttManager::publishLegacyRadar("))
    for needle in ('const int8_t code = p == Ld2412Presence::PRESENT ? 1 : (p == Ld2412Presence::ABSENT ? 0 : 2);',
                   'const bool heartbeat = code != 2 && nowMs - _ld2412PublishedMs >= LD2412_HEARTBEAT_MS;',
                   'code == 2 ? "None" : (code ? "1" : "0"), true)',
                   "if (s.legacy && s.observing) {",
                   "ld2454Sensor.hasAnyTarget() ? MQTT_RADAR_PUBLISH_INTERVAL : LEGACY_HEARTBEAT_MS"):
        check(needle in leg, "publishLegacyRadar(): %s" % needle)

    # frames
    sfs = flat(extract_function(rev2, "void MqttManager::serviceFrameStream("))
    check("g_radarPhase.load() == RADAR_PHASE_OFF ? LD2454_STREAM_OFF_IDLE_MS : LD2454_STREAM_IDLE_MS" in sfs
          and "_frameBatcher.poll(millis(), idleMs)" in sfs, "serviceFrameStream(): 30 s heartbeat only in phase OFF")
    sfb = flat(extract_function(rev2, "void MqttManager::sendFrameBatch("))
    check("const RadarStatus rs = radarStatusSnapshot();" in sfb and "ld2412Sensor.presence() == Ld2412Presence::PRESENT" in sfb
          and "rs.pw(), rs.sm()" in sfb, "sendFrameBatch(): pw / sm from one snapshot, p2 from presence()")
    fr = cpp_variant("src/ld2454_frames.cpp", {"BOARD_REV2"})
    ff = flat(extract_function(fr, "size_t formatLd2454Frames("))
    check('if (observingMask & 1u) o.add(",\\"mt\\":%u", multiTarget ? 1u : 0u);' in ff
          and 'if (observingMask & 2u) o.add(",\\"p2\\":%u", ld2412Present ? 1u : 0u);' in ff
          and 'o.add(",\\"pw\\":%u,\\"sm\\":\\"%c\\"", (unsigned)observingMask, stateCode);' in ff,
          "formatLd2454Frames(): mt only with pw bit0, p2 only with pw bit1, then pw and sm")
    check("static const uint32_t idleMs" not in fr and "nowMs - _lastCloseMs >= idleMs" in flat(fr), "Ld2454FrameBatcher::poll() uses the idleMs argument under REV2")
    frd = cpp_variant("src/ld2454_frames.cpp", set())
    check('o.add(",\\"mt\\":%u,\\"p2\\":%u,\\"pw\\":%u,\\"sm\\":\\"%c\\""' in flat(frd) and "LD2454_STREAM_IDLE_MS) return close" in flat(frd),
          "default build: the P1 frames format and the 5 s heartbeat are unchanged")
    check("FRAMES_PW_P1" in default and "FRAMES_PW_P1" not in rev2, "the P1 pw / sm constants exist only in the default build")


def main():
    for fn in (test_connect_order, test_connect_burst_refused, test_birth_resend_refused, test_stalled_link_loop,
               test_ha_state_and_birth, test_dirty_protocol, test_legacy_topics, test_frames_rules, test_commands,
               test_cpp_links):
        fn()
    print("\n%d checks passed, %d failed" % (OKS[0], len(FAILS)))
    if FAILS:
        print("FAILED")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
