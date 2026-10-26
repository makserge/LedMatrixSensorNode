#!/usr/bin/env python3
"""P2 host check of the Home Assistant discovery payloads (board revision 2, -D BOARD_REV2).

The firmware cannot run on the PC, so this file does what the P1 precedent (frames_batcher_model.py) does: Python
builders that mirror MqttManager::publishDiscovery() and the helpers publishConfig / publishSwitchConfig /
publishTimeConfig / publishBinaryConfig / publishEnumSensorConfig statement by statement, serialised the way
ArduinoJson 7 does (compact, insertion order, UTF-8), and tied to the C++ source mechanically so that a drift is
caught instead of trusted away:

  * the key order of every helper is read from src/mqtt_manager.cpp (doc["key"] occurrences, in order) and must
    equal the order of the Python builder; literal values (pl_on "1", ret false, ...) must match too;
  * every call in sendDiscovery() is parsed out of the C++ (helper, id, name, topics, icon, ...), resolved through
    include/radar_constants.h / constants.h, and compared with the design's call list; the payloads are then built
    from the parsed calls (so the check covers what the firmware really passes);
  * the default build (BOARD_REV2 undefined) publishes the four sensors only; REV2 adds exactly the ten radar entities;
  * the payloads are compared byte for byte with the design's literals (option-A design 5.6 c, 5.7 a; Appendix C of
    the P2 spec), against the sizes of design 5.7 b, against DISCOVERY_JSON_MAX (len >= 512 would be skipped), and
    against HA's abbreviated key sets per platform (a `time` entity carries no ptrn / min / max);
  * the four sensor configs equal tools/frames_checker.py EXPECTED_SENSOR_CONFIGS (byte-identical to P1).

Run: python -I -B tools/tests/discovery_payloads_check.py      (standard library only; exit code 0 = all passed)
"""
import ast
import importlib.util
import json
import os
import re
import sys

sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    out, stack = [], []          # stack entries: [parent_active, branch_active]
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


def c_unescape(s):
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), s)


def parse_calls(body, names):
    """[(function name, [raw argument text, ...]), ...] for every call of one of `names`, in source order."""
    pat = re.compile(r"\b(" + "|".join(names) + r")\s*\(")
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


def resolve(arg, env, options=None):
    """A C++ call argument: string literal(s) -> str, nullptr -> None, integer -> int, identifier -> its value."""
    arg = arg.strip()
    if arg.startswith('"'):
        parts = re.findall(r'"((?:[^"\\]|\\.)*)"', arg)
        return "".join(c_unescape(p) for p in parts)
    if arg == "nullptr":
        return None
    if re.fullmatch(r"\d+", arg):
        return int(arg)
    if options is not None and arg in options:
        return options[arg]
    return env[arg]


# ============================================================================ the Python mirror
DISCOVERY_JSON_MAX = None   # filled from constants.h


def ser(doc):
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


class Mirror:
    def __init__(self, env):
        self.env = env
        self.client = env["MQTT_CLIENT_ID"]
        self.portal = env["WIFI_PORTAL_NAME"]
        self.prefix = env["MQTT_DISCOVERY_PREFIX"]
        self.status_t = env["MQTT_STATUS_TOPIC"]
        self.state_t = env["MQTT_STATE_TOPIC"]
        self.expire = env["MQTT_EXPIRE_AFTER_S"]

    def finish(self, component, obj_id, doc):
        """publishDiscovery(): uniq_id, avty_t (unless an avty list exists), dev."""
        topic = "%s/%s/%s_%s/config" % (self.prefix, component, self.client, obj_id)
        doc["uniq_id"] = self.client + "_" + obj_id
        if not isinstance(doc.get("avty"), list):
            doc["avty_t"] = self.status_t
        doc["dev"] = {"ids": self.client, "name": self.portal, "mf": "", "mdl": ""}
        return topic, ser(doc)

    # the five builders return the document as it is before publishDiscovery() appends its keys
    def config_doc(self, id_, name, unit, dev_cla, val_tpl):
        d = {}
        d["name"] = name
        d["stat_t"] = self.state_t
        d["avty_t"] = self.status_t
        d["unit_of_meas"] = unit
        d["dev_cla"] = dev_cla
        d["stat_cla"] = "measurement"
        d["exp_aft"] = self.expire
        d["val_tpl"] = val_tpl
        return "sensor", d

    @staticmethod
    def switch_doc(id_, name, cmd_t, stat_t, icon, ent_cat):
        d = {}
        d["name"] = name
        d["cmd_t"] = cmd_t
        d["stat_t"] = stat_t
        d["pl_on"] = "1"
        d["pl_off"] = "0"
        d["stat_on"] = "1"
        d["stat_off"] = "0"
        d["opt"] = False
        d["ret"] = False
        if ent_cat:
            d["ent_cat"] = ent_cat
        d["ic"] = icon
        return "switch", d

    @staticmethod
    def time_doc(id_, name, cmd_t, stat_t, icon):
        d = {}
        d["name"] = name
        d["cmd_t"] = cmd_t
        d["stat_t"] = stat_t
        d["ret"] = False
        d["ent_cat"] = "config"
        d["ic"] = icon
        return "time", d

    def binary_doc(self, id_, name, stat_t, dev_cla, ent_cat, icon, avty2):
        d = {}
        d["name"] = name
        d["stat_t"] = stat_t
        if dev_cla:
            d["dev_cla"] = dev_cla
        if ent_cat:
            d["ent_cat"] = ent_cat
        if icon:
            d["ic"] = icon
        if avty2:
            d["avty"] = [{"t": self.status_t}, {"t": avty2, "pl_avail": "ON", "pl_not_avail": "OFF"}]
            d["avty_mode"] = "all"
        return "binary_sensor", d

    @staticmethod
    def enum_doc(id_, name, stat_t, options, attr_t, ent_cat, icon):
        d = {}
        d["name"] = name
        d["stat_t"] = stat_t
        d["dev_cla"] = "enum"
        d["ops"] = list(options)
        d["json_attr_t"] = attr_t
        d["ent_cat"] = ent_cat
        d["ic"] = icon
        return "sensor", d

    def build(self, helper, args):
        make = {"publishConfig": self.config_doc, "publishSwitchConfig": self.switch_doc,
                "publishTimeConfig": self.time_doc, "publishBinaryConfig": self.binary_doc,
                "publishEnumSensorConfig": self.enum_doc}[helper]
        component, doc = make(*args)
        return self.finish(component, args[0], doc)


# the C++ helper name -> (Python builder, the all-optionals-present argument list that exercises every key)
HELPERS = {
    "publishConfig": ("config_doc", ["i", "n", "u", "d", "v"]),
    "publishSwitchConfig": ("switch_doc", ["i", "n", "c", "s", "ic", "config"]),
    "publishTimeConfig": ("time_doc", ["i", "n", "c", "s", "ic"]),
    "publishBinaryConfig": ("binary_doc", ["i", "n", "s", "dc", "ec", "ic", "a2"]),
    "publishEnumSensorConfig": ("enum_doc", ["i", "n", "s", ["a"], "at", "ec", "ic"]),
}

# the design's call list (option-A design 5.6 c, 5.7 a; P2 spec 7.2), as (helper, id, name, ...) with topic NAMES
DESIGN_CALLS = [
    ("publishConfig", ["co2", "CO2", "ppm", "carbon_dioxide", "{{ value_json.co2 if 'co2' in value_json else None }}"]),
    ("publishConfig", ["temp", "Temperature", "\u00b0C", "temperature", "{{ value_json.temp if 'temp' in value_json else None }}"]),
    ("publishConfig", ["hum", "Humidity", "%", "humidity", "{{ value_json.hum if 'hum' in value_json else None }}"]),
    ("publishConfig", ["lux", "Illuminance", "lx", "illuminance", "{{ value_json.lux if 'lux' in value_json else None }}"]),
    ("publishSwitchConfig", ["radar_power", "Radars", "led_matrix_node/radar/power", "led_matrix_node/radar/power/state", "mdi:radar", None]),
    ("publishSwitchConfig", ["radar_schedule", "Radar night schedule", "led_matrix_node/radar/schedule", "led_matrix_node/radar/schedule/state", "mdi:weather-night", None]),
    ("publishBinaryConfig", ["radar_powered", "Radar power", "led_matrix_node/radar/powered", "power", "diagnostic", None, None]),
    ("publishBinaryConfig", ["radar_observing", "Radar observing", "led_matrix_node/radar/observing", None, "diagnostic", "mdi:eye", None]),
    ("publishEnumSensorConfig", ["radar_state", "Radar state", "led_matrix_node/radar/state", ["on", "night_off", "forced_on", "forced_off", "failsafe_on"], "led_matrix_node/radar/state/attr", "diagnostic", "mdi:radar"]),
    ("publishBinaryConfig", ["radar_presence", "Radar presence", "led_matrix_node/presence", "occupancy", None, None, "led_matrix_node/radar/observing"]),
    ("publishSwitchConfig", ["radar_sched_offline", "Radar schedule without HA", "led_matrix_node/radar/sched_offline", "led_matrix_node/radar/sched_offline/state", "mdi:lan-disconnect", "config"]),
    ("publishSwitchConfig", ["radar_legacy", "Legacy radar JSON", "led_matrix_node/radar/legacy", "led_matrix_node/radar/legacy/state", "mdi:code-json", "config"]),
    ("publishTimeConfig", ["radar_night_start", "Radar night start", "led_matrix_node/radar/night_start", "led_matrix_node/radar/night_start/state", "mdi:clock-start"]),
    ("publishTimeConfig", ["radar_night_end", "Radar night end", "led_matrix_node/radar/night_end", "led_matrix_node/radar/night_end/state", "mdi:clock-end"]),
]

# --- expected literals (design 5.6 c, 5.7 a; P2 spec Appendix C) ---------------------------------------------------
DEV = '"dev":{"ids":"Led_Matrix_Node","name":"Led Matrix Node","mf":"","mdl":""}}'
EXPECTED = {
    "homeassistant/switch/Led_Matrix_Node_radar_power/config":
        '{"name":"Radars","cmd_t":"led_matrix_node/radar/power","stat_t":"led_matrix_node/radar/power/state",'
        '"pl_on":"1","pl_off":"0","stat_on":"1","stat_off":"0","opt":false,"ret":false,"ic":"mdi:radar",'
        '"uniq_id":"Led_Matrix_Node_radar_power","avty_t":"led_matrix_node/sensors/status",' + DEV,
    "homeassistant/switch/Led_Matrix_Node_radar_schedule/config":
        '{"name":"Radar night schedule","cmd_t":"led_matrix_node/radar/schedule",'
        '"stat_t":"led_matrix_node/radar/schedule/state","pl_on":"1","pl_off":"0","stat_on":"1","stat_off":"0",'
        '"opt":false,"ret":false,"ic":"mdi:weather-night","uniq_id":"Led_Matrix_Node_radar_schedule",'
        '"avty_t":"led_matrix_node/sensors/status",' + DEV,
    "homeassistant/binary_sensor/Led_Matrix_Node_radar_powered/config":
        '{"name":"Radar power","stat_t":"led_matrix_node/radar/powered","dev_cla":"power","ent_cat":"diagnostic",'
        '"uniq_id":"Led_Matrix_Node_radar_powered","avty_t":"led_matrix_node/sensors/status",' + DEV,
    "homeassistant/time/Led_Matrix_Node_radar_night_start/config":
        '{"name":"Radar night start","cmd_t":"led_matrix_node/radar/night_start",'
        '"stat_t":"led_matrix_node/radar/night_start/state","ret":false,"ent_cat":"config","ic":"mdi:clock-start",'
        '"uniq_id":"Led_Matrix_Node_radar_night_start","avty_t":"led_matrix_node/sensors/status",' + DEV,
    "homeassistant/time/Led_Matrix_Node_radar_night_end/config":
        '{"name":"Radar night end","cmd_t":"led_matrix_node/radar/night_end",'
        '"stat_t":"led_matrix_node/radar/night_end/state","ret":false,"ent_cat":"config","ic":"mdi:clock-end",'
        '"uniq_id":"Led_Matrix_Node_radar_night_end","avty_t":"led_matrix_node/sensors/status",' + DEV,
    "homeassistant/binary_sensor/Led_Matrix_Node_radar_observing/config":
        '{"name":"Radar observing","stat_t":"led_matrix_node/radar/observing","ent_cat":"diagnostic","ic":"mdi:eye",'
        '"uniq_id":"Led_Matrix_Node_radar_observing","avty_t":"led_matrix_node/sensors/status",' + DEV,
    "homeassistant/sensor/Led_Matrix_Node_radar_state/config":
        '{"name":"Radar state","stat_t":"led_matrix_node/radar/state","dev_cla":"enum",'
        '"ops":["on","night_off","forced_on","forced_off","failsafe_on"],'
        '"json_attr_t":"led_matrix_node/radar/state/attr","ent_cat":"diagnostic","ic":"mdi:radar",'
        '"uniq_id":"Led_Matrix_Node_radar_state","avty_t":"led_matrix_node/sensors/status",' + DEV,
    "homeassistant/binary_sensor/Led_Matrix_Node_radar_presence/config":
        '{"name":"Radar presence","stat_t":"led_matrix_node/presence","dev_cla":"occupancy",'
        '"avty":[{"t":"led_matrix_node/sensors/status"},{"t":"led_matrix_node/radar/observing","pl_avail":"ON",'
        '"pl_not_avail":"OFF"}],"avty_mode":"all","uniq_id":"Led_Matrix_Node_radar_presence",' + DEV,
    "homeassistant/switch/Led_Matrix_Node_radar_sched_offline/config":
        '{"name":"Radar schedule without HA","cmd_t":"led_matrix_node/radar/sched_offline",'
        '"stat_t":"led_matrix_node/radar/sched_offline/state","pl_on":"1","pl_off":"0","stat_on":"1","stat_off":"0",'
        '"opt":false,"ret":false,"ent_cat":"config","ic":"mdi:lan-disconnect",'
        '"uniq_id":"Led_Matrix_Node_radar_sched_offline","avty_t":"led_matrix_node/sensors/status",' + DEV,
    "homeassistant/switch/Led_Matrix_Node_radar_legacy/config":
        '{"name":"Legacy radar JSON","cmd_t":"led_matrix_node/radar/legacy","stat_t":"led_matrix_node/radar/legacy/state",'
        '"pl_on":"1","pl_off":"0","stat_on":"1","stat_off":"0","opt":false,"ret":false,"ent_cat":"config",'
        '"ic":"mdi:code-json","uniq_id":"Led_Matrix_Node_radar_legacy","avty_t":"led_matrix_node/sensors/status",' + DEV,
}

# design 5.7 b sizes (payload bytes)
DESIGN_SIZES = {
    "radar_power": 351, "radar_schedule": 382, "radar_sched_offline": 422, "radar_night_start": 339,
    "radar_night_end": 329, "radar_powered": 262, "radar_observing": 267, "radar_state": 387,
    "radar_presence": 343, "radar_legacy": 388,
}

# Keys HA's MQTT discovery accepts, per platform, in abbreviated form (homeassistant/components/mqtt/abbreviations.py).
COMMON = {"name", "uniq_id", "avty", "avty_t", "avty_mode", "avty_tpl", "pl_avail", "pl_not_avail", "ent_cat",
          "ic", "dev", "json_attr_t", "json_attr_tpl", "en", "obj_id", "o", "ent_pic", "enabled_by_default"}
PLATFORM_KEYS = {
    "switch": COMMON | {"cmd_t", "stat_t", "pl_on", "pl_off", "stat_on", "stat_off", "opt", "ret", "dev_cla",
                        "val_tpl", "cmd_tpl", "qos"},
    "time": COMMON | {"cmd_t", "stat_t", "ret", "opt", "cmd_tpl", "val_tpl", "qos"},
    "binary_sensor": COMMON | {"stat_t", "dev_cla", "pl_on", "pl_off", "exp_aft", "frc_upd", "off_dly", "on_dly",
                               "val_tpl", "qos"},
    "sensor": COMMON | {"stat_t", "dev_cla", "unit_of_meas", "stat_cla", "exp_aft", "val_tpl", "ops", "frc_upd",
                        "sug_dsp_prc", "last_reset_val_tpl", "qos"},
}
FORBIDDEN = {"time": {"ptrn", "min", "max", "mode", "pl_on", "pl_off", "stat_on", "stat_off"}}

LITERAL = r'("(?:[^"\\]|\\.)*"|true|false)'


def literal_value(text):
    if text == "true":
        return True
    if text == "false":
        return False
    return c_unescape(text[1:-1])


# ============================================================================ checks
def load_env():
    env = {}
    parse_constants(read_src("include/constants.h"), env)
    parse_constants(read_src("include/radar_constants.h"), env)
    return env


def test_cpp_key_order(src_rev2, env):
    print("C++ link: key order and literal values of the helpers")
    mirror = Mirror(env)
    assign = re.compile(r'\bdoc\["(\w+)"\]\s*(?:=(?!=)|\.to<)')
    lit_assign = re.compile(r'\bdoc\["(\w+)"\]\s*=\s*' + LITERAL + r'\s*;')
    for helper, (builder, probe) in HELPERS.items():
        body = strip_comments(extract_function(src_rev2, "MqttManager::" + helper + "("))
        cpp_keys = []
        for k in assign.findall(body):
            if k not in cpp_keys:
                cpp_keys.append(k)
        doc = getattr(mirror, builder)(*probe)[1]
        py_keys = list(doc)
        check(cpp_keys == py_keys, "%s: C++ key order %s equals the Python builder's %s" % (helper, cpp_keys, py_keys))
        for k, lit in lit_assign.findall(body):
            want = literal_value(lit)
            check(doc.get(k) == want and type(doc.get(k)) is type(want),
                  "%s: literal doc[%r] = %r in the C++ equals the builder's %r" % (helper, k, want, doc.get(k)))
    # the avty array of the binary sensor: element keys and the second availability's payloads
    body = strip_comments(extract_function(src_rev2, "MqttManager::publishBinaryConfig("))
    a2 = re.findall(r'\ba2\["(\w+)"\]\s*=\s*([^;]+);', body)
    check([k for k, _ in a2] == ["t", "pl_avail", "pl_not_avail"] and a2[1][1].strip() == '"ON"' and a2[2][1].strip() == '"OFF"',
          "publishBinaryConfig: the second availability carries t, pl_avail ON, pl_not_avail OFF (HA's default would not match OFF)")
    check('doc["avty_mode"] = "all"' in body, "publishBinaryConfig: avty_mode all")
    check(re.search(r'avty\.add<JsonObject>\(\)\["t"\]\s*=\s*MQTT_STATUS_TOPIC', body) is not None,
          "publishBinaryConfig: the first availability is the node status topic")
    # publishDiscovery(): uniq_id, avty_t unless an avty list exists, dev, the size guard
    body = strip_comments(extract_function(src_rev2, "bool MqttManager::publishDiscovery("))
    keys = []
    for k in assign.findall(body):
        if k not in keys:
            keys.append(k)
    check(keys == ["uniq_id", "avty_t", "dev"], "publishDiscovery appends uniq_id, avty_t, dev in this order: %s" % keys)
    check(re.search(r'if\s*\(\s*!doc\["avty"\]\.is<JsonArray>\(\)\s*\)\s*doc\["avty_t"\]\s*=\s*MQTT_STATUS_TOPIC', body) is not None,
          "publishDiscovery: avty_t only without an avty list")
    dev = re.findall(r'\bdev\["(\w+)"\]\s*=\s*([^;]+);', body)
    check([k for k, _ in dev] == ["ids", "name", "mf", "mdl"], "publishDiscovery: dev block keys ids, name, mf, mdl")
    check(dict(dev).get("ids") == "MQTT_CLIENT_ID" and dict(dev).get("name") == "WIFI_PORTAL_NAME"
          and dict(dev).get("mf") == '""' and dict(dev).get("mdl") == '""', "publishDiscovery: dev values")
    check("char buffer[DISCOVERY_JSON_MAX]" in body and "len >= sizeof(buffer)" in body,
          "publishDiscovery: a config of DISCOVERY_JSON_MAX bytes or more is skipped, not truncated")


def test_send_discovery(src, env):
    print("C++ link: sendDiscovery() per build variant")
    names = list(HELPERS)
    default = preprocess(src, set())
    rev2 = preprocess(src, {"BOARD_REV2"})
    options = {}
    m = re.search(r"RADAR_STATE_OPTIONS\[\]\s*=\s*\{([^}]*)\}", strip_comments(rev2))
    check(m is not None, "RADAR_STATE_OPTIONS array found in the REV2 variant")
    options["RADAR_STATE_OPTIONS"] = re.findall(r'"([^"]*)"', m.group(1)) if m else []
    check("RADAR_STATE_OPTIONS" not in strip_comments(default), "the default build has no RADAR_STATE_OPTIONS")
    calls_default = parse_calls(strip_comments(extract_function(default, "void MqttManager::sendDiscovery(")), names)
    calls_rev2 = parse_calls(strip_comments(extract_function(rev2, "bool MqttManager::sendDiscovery(")), names)
    check([c[0] for c in calls_default] == ["publishConfig"] * 4,
          "default build: sendDiscovery() publishes the four sensors only (%d calls)" % len(calls_default))
    check(len(calls_rev2) == 14 and [c[0] for c in calls_rev2[:4]] == ["publishConfig"] * 4,
          "REV2: the four sensors, then ten radar entities (%d calls)" % len(calls_rev2))
    check(calls_default == calls_rev2[:4], "the four sensor calls are identical in both variants")

    resolved = []
    for helper, args in calls_rev2:
        try:
            resolved.append((helper, [resolve(a, env, options) for a in args]))
        except KeyError as exc:
            check(False, "unresolved identifier %s in %s(%s)" % (exc, helper, args))
            resolved.append((helper, []))
    # the options argument of the enum sensor is followed by its size
    for helper, args in resolved:
        if helper == "publishEnumSensorConfig" and args:
            n_options = args.pop(4)
            check(n_options == len(args[3]), "publishEnumSensorConfig is told %d options, the array has %d" % (n_options, len(args[3])))
    check(resolved == DESIGN_CALLS, "every call of sendDiscovery() equals the design's list (ids, names, topics, icons, categories)")
    if resolved != DESIGN_CALLS:
        for a, b in zip(resolved, DESIGN_CALLS):
            if a != b:
                print("   got", a, "\n   exp", b)
    return resolved


def test_topics(env):
    print("topics in radar_constants.h")
    want = {
        "MQTT_RADAR_POWER_CMD": "led_matrix_node/radar/power", "MQTT_RADAR_POWER_STATE": "led_matrix_node/radar/power/state",
        "MQTT_RADAR_SCHEDULE_CMD": "led_matrix_node/radar/schedule", "MQTT_RADAR_SCHEDULE_STATE": "led_matrix_node/radar/schedule/state",
        "MQTT_RADAR_NIGHT_START_CMD": "led_matrix_node/radar/night_start", "MQTT_RADAR_NIGHT_START_STATE": "led_matrix_node/radar/night_start/state",
        "MQTT_RADAR_NIGHT_END_CMD": "led_matrix_node/radar/night_end", "MQTT_RADAR_NIGHT_END_STATE": "led_matrix_node/radar/night_end/state",
        "MQTT_RADAR_SCHED_OFFLINE_CMD": "led_matrix_node/radar/sched_offline", "MQTT_RADAR_SCHED_OFFLINE_STATE": "led_matrix_node/radar/sched_offline/state",
        "MQTT_RADAR_LEGACY_CMD": "led_matrix_node/radar/legacy", "MQTT_RADAR_LEGACY_STATE": "led_matrix_node/radar/legacy/state",
        "MQTT_RADAR_POWERED_TOPIC": "led_matrix_node/radar/powered", "MQTT_RADAR_OBSERVING_TOPIC": "led_matrix_node/radar/observing",
        "MQTT_RADAR_STATE_TOPIC": "led_matrix_node/radar/state", "MQTT_RADAR_STATE_ATTR_TOPIC": "led_matrix_node/radar/state/attr",
        "MQTT_PRESENCE_TOPIC": "led_matrix_node/presence", "MQTT_HA_STATUS_TOPIC": "homeassistant/status",
    }
    for name, topic in want.items():
        check(env.get(name) == topic, "%s = %r" % (name, topic))
    for name in want:
        if name.endswith("_STATE") and name != "MQTT_RADAR_STATE_TOPIC":
            cmd = name[:-len("_STATE")] + "_CMD"
            check(env[name] == env[cmd] + "/state", "%s is its command topic + /state" % name)


def test_payloads(resolved, env):
    print("payloads built from the parsed C++ calls")
    mirror = Mirror(env)
    limit = env["DISCOVERY_JSON_MAX"]
    check(limit == 512, "DISCOVERY_JSON_MAX is 512 (constants.h)")
    results = [mirror.build(h, a) for h, a in resolved]
    by_topic = {}
    print("  %-9s %-24s %7s %7s  %s" % ("platform", "object id", "payload", "packet", "fits"))
    for topic, payload in results:
        n = len(payload.encode("utf-8"))
        parts = topic.split("/")
        platform, obj = parts[1], parts[2][len(mirror.client) + 1:]
        rem = 2 + len(topic.encode()) + n               # MQTT 3.1.1 PUBLISH, QoS 0
        packet = 1 + (1 if rem < 128 else 2) + rem
        fits = n < limit
        print("  %-9s %-24s %7d %7d  %s" % (platform, obj, n, packet, "yes" if fits else "NO (skipped)"))
        by_topic[topic] = payload
        check(fits, "%s: %d B is below DISCOVERY_JSON_MAX (%d)" % (obj, n, limit))
        doc = json.loads(payload)
        if obj.startswith("radar_"):
            bad = [k for k in doc if k not in PLATFORM_KEYS[platform]]
            check(not bad, "%s: only keys HA knows for %s (%s)" % (obj, platform, bad))
            for k in FORBIDDEN.get(platform, ()):
                check(k not in doc, "%s: no %s on a %s entity" % (obj, k, platform))
        check(doc.get("uniq_id") == mirror.client + "_" + obj, "%s: uniq_id" % obj)
        check(not ("avty" in doc and "avty_t" in doc), "%s: not both avty and avty_t" % obj)
        check("avty" in doc or doc.get("avty_t") == mirror.status_t, "%s: availability is the node status topic" % obj)
        check(doc["dev"] == {"ids": mirror.client, "name": mirror.portal, "mf": "", "mdl": ""}, "%s: dev block" % obj)
        keys = list(doc)
        check(keys[-1] == "dev" and keys[-2] in ("avty_t", "uniq_id"), "%s: tail key order %s" % (obj, keys[-3:]))
        if obj in DESIGN_SIZES:
            check(n == DESIGN_SIZES[obj], "%s: %d B equals the design's %d B" % (obj, n, DESIGN_SIZES[obj]))
    for topic, lit in EXPECTED.items():
        check(by_topic.get(topic) == lit, "byte-identical to the design literal: %s" % topic)
        if by_topic.get(topic) != lit:
            print("   got", by_topic.get(topic), "\n   exp", lit)
    check(len([t for t in by_topic if "Led_Matrix_Node_radar_" in t]) == 10, "ten radar discovery topics")
    check(len(by_topic) == 14, "fourteen distinct discovery topics in all")
    largest = max(len(p.encode("utf-8")) for p in by_topic.values())
    check(largest == 422, "the largest payload is 422 B (radar_sched_offline), margin %d B" % (limit - 1 - largest))
    return by_topic


def test_sensors_unchanged(by_topic):
    print("the four sensor configs stay byte-identical (tools/frames_checker.py EXPECTED_SENSOR_CONFIGS)")
    path = os.path.join(ROOT, "tools", "frames_checker.py")
    spec = importlib.util.spec_from_file_location("frames_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for topic, expected in module.EXPECTED_SENSOR_CONFIGS.items():
        check(by_topic.get(topic) == expected.decode("utf-8"), "unchanged: %s" % topic)
    check(len(module.EXPECTED_SENSOR_CONFIGS) == 4, "four sensor configs in the checker")


def test_default_build_has_no_radar_entities(src):
    print("default build: nothing radar-related is discovered, subscribed or cleared")
    default = strip_comments(preprocess(src, set()))
    for needle in ("publishSwitchConfig(", "publishTimeConfig(", "publishBinaryConfig(", "publishEnumSensorConfig(",
                   "clearRetainedCommands", "MQTT_HA_STATUS_TOPIC", "MQTT_RADAR_POWER_CMD", "radarStatusSnapshot",
                   "radarPowerRequest", "radarSetHaState", "homeassistant/status"):
        # the declarations of the helper functions are inside #ifdef BOARD_REV2 as well, so no match at all
        check(needle not in default, "default variant of mqtt_manager.cpp has no %s" % needle)


def main():
    global DISCOVERY_JSON_MAX
    env = load_env()
    DISCOVERY_JSON_MAX = env["DISCOVERY_JSON_MAX"]
    src = read_src("src/mqtt_manager.cpp")
    src_rev2 = preprocess(src, {"BOARD_REV2"})
    test_topics(env)
    test_cpp_key_order(src_rev2, env)
    resolved = test_send_discovery(src, env)
    by_topic = test_payloads(resolved, env)
    test_sensors_unchanged(by_topic)
    test_default_build_has_no_radar_entities(src)
    print("\n%d checks passed, %d failed" % (OKS[0], len(FAILS)))
    if FAILS:
        print("FAILED")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
