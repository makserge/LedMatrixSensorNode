#!/usr/bin/env python3
"""P2 host check of the BOARD_REV2 gate (P2 spec section 2): without -D BOARD_REV2 the firmware must behave exactly like
the committed P1 firmware, because on the current board GPIO9 (D10) is the power / LED-off button and must never be driven
as an output. This test preprocesses every source file for the two build variants (only #ifdef / #ifndef / #else / #endif
are used in the sources) and checks, on the text the compiler would see:

  * default variant: no P2 symbol is visible anywhere (no radar power, no RadarStatus, no radar topics or entities, no
    ladder, no tri-state, no /api/radar, no gpio_set_level), and the new headers and src/radar_power.cpp are empty;
  * REV2 variant: RADAR_PWR_EN_PIN appears only in pins.h and src/radar_power.cpp, gpio_set_level() appears only in
    src/radar_power.cpp, and the current board's button pins MODE_BUTTON_PIN / SWITCH_OFF_LED_PIN do not exist;
  * platformio.ini: the default environment is the current board, the rev2 environment is the only one with BOARD_REV2.

It complements the object-size comparison against a `git archive HEAD` baseline (spec 2.5), which proves the default image
is unchanged; this one runs in a second and names the offending file and line.

Run: python -I -B tools/tests/board_gate_check.py        (standard library only; exit code 0 = all passed)
"""
import glob
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


def read_src(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace").replace("\r\n", "\n")


def strip_comments(text):
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


def sources():
    files = sorted(glob.glob(os.path.join(ROOT, "src", "*.cpp")) + glob.glob(os.path.join(ROOT, "include", "*.h")))
    files = [f for f in files if os.path.basename(f) != "secrets.h"]     # local, git-ignored credentials: never read, and the count stays stable
    return [(os.path.relpath(f, ROOT).replace("\\", "/"), read_src(f)) for f in files]


# Symbols that exist only for board revision 2. The names the P1 firmware already has (RADAR_PRESENCE_PIN, RADAR_RX_PIN,
# RADAR_TX_PIN, RADAR_BAUD, MQTT_RADAR_PUBLISH_INTERVAL, MQTT_LD2412_TOPIC, publishLegacyRadar, ...) are not in this list.
P2_SYMBOLS = [
    r"\bradarPower\w*", r"\bradarSetHaState\b", r"\bradarStatusSnapshot\b", r"\bradarParse\w+", r"\bradarFormat\w+",
    r"\bradar(?:State|Reason|Fault|Ha|Phase)Name\b", r"\bradarWarmedUp\b", r"\bradarSwitchCommand\b", r"\bradarTimeCommand\b",
    r"\bRadar(?:Status|Phase|State|Reason|Fault|HaState|Cmd|Power)\b", r"\bRADAR_(?!PRESENCE_PIN\b|RX_PIN\b|TX_PIN\b|BAUD\b)\w+",
    r"\bg_(?:radarPhase|radarPowered|powerEpoch|poweredAtMs|parkRequest|parkAck|ld2454Ready|ld2454Fault|stateDirty|ladderMv)\b",
    r"\bBUTTON_LADDER_PIN\b", r"\bLadder\w*", r"\bLADDER_\w+", r"\bLd2412Presence\b", r"\bupdatePresence\b",
    r"\bHTML_RADAR\b", r"/api/radar", r"\bhandleApiRadar\b", r"\bisOtaPauseRequested\b", r"\bPARK_ACK_TIMEOUT_MS\b",
    r"\bLD2454_BOOT_TIMEOUT_MS\b", r"\bLD2454_WARMUP_FRAMES\b", r"\bLD2412_(?:OUT_IGNORE_MS|OUT_MIN_HIGH_MS|STATIC_WARMUP_MS)\b",
    r"\bLD2454_STREAM_OFF_IDLE_MS\b", r"\bLEGACY_HEARTBEAT_MS\b", r"\bMQTT_(?:HA_STATUS_TOPIC|PRESENCE_TOPIC|BIRTH_\w+)\b",
    r"\bMQTT_RADAR_(?!PUBLISH_INTERVAL\b)\w+", r"\bclearRetainedCommands\b", r"\bpublishRadarStates\b", r"\bserviceRadarStates\b",
    r"\bpublish(?:Switch|Time|Binary|EnumSensor)Config\b", r"\bonHaStatus\b", r"\bopenLink\b", r"\bparkLink\b", r"\bsyncReady\b",
    r"\bradar_power\.h\b", r"\bradar_constants\.h\b", r"\bgpio_set_level\b", r"\bBOARD_REV2\b", r"\besp_task_wdt_\w+",
]
P2_RE = re.compile("|".join(P2_SYMBOLS))
NEW_FILES = ("include/radar_constants.h", "include/radar_power.h", "src/radar_power.cpp")


def test_default_variant(srcs):
    print("default variant (BOARD_REV2 undefined): no P2 symbol is visible")
    for rel, text in srcs:
        if rel in NEW_FILES:
            continue                       # checked below: they must be empty (their include guard names look like P2 symbols)
        body = strip_comments(preprocess(text, set()))
        hits = []
        for ln, line in enumerate(body.split("\n"), 1):
            m = P2_RE.search(line)
            if m:
                hits.append((ln, m.group(0)))
        check(not hits, "%s: no P2 symbol in the default variant%s" % (rel, "" if not hits else " (line %d: %s)" % hits[0]))
    for rel in NEW_FILES:
        text = dict(srcs).get(rel)
        if text is None:
            continue
        body = strip_comments(preprocess(text, set()))
        body = re.sub(r"#\s*(?:ifndef|define|endif)\b.*", "", body)           # the include guard
        check(body.strip() == "", "%s is empty without BOARD_REV2" % rel)


def test_rev2_variant(srcs):
    print("REV2 variant: who may touch GPIO9, and the current board's button pins")
    pin_users, level_users = [], []
    for rel, text in srcs:
        body = strip_comments(preprocess(text, {"BOARD_REV2"}))
        if re.search(r"\bRADAR_PWR_EN_PIN\b", body):
            pin_users.append(rel)
        if re.search(r"\bgpio_set_level\b", body):
            level_users.append(rel)
        check(not re.search(r"\b(?:MODE_BUTTON_PIN|SWITCH_OFF_LED_PIN)\b", body),
              "%s: the current board's button pins do not exist under BOARD_REV2" % rel)
        check(not re.search(r"\bdigitalWrite\s*\(\s*(?:RADAR_PWR_EN_PIN|D10|9)\b", body),
              "%s: no digitalWrite() on GPIO9" % rel)
    check(sorted(pin_users) == ["include/pins.h", "src/radar_power.cpp"],
          "RADAR_PWR_EN_PIN is used only in pins.h and radar_power.cpp (found: %s)" % pin_users)
    check(level_users == ["src/radar_power.cpp"], "gpio_set_level() appears only in radar_power.cpp (found: %s)" % level_users)


def test_default_pins(srcs):
    print("default variant: GPIO9 stays the power / LED-off button input")
    by = dict(srcs)
    pins = strip_comments(preprocess(by["include/pins.h"], set()))
    check(re.search(r"#define\s+SWITCH_OFF_LED_PIN\s+D10\b", pins) and re.search(r"#define\s+MODE_BUTTON_PIN\s+D9\b", pins),
          "pins.h: MODE_BUTTON_PIN D9 and SWITCH_OFF_LED_PIN D10, as in P1")
    disp = strip_comments(preprocess(by["src/display_manager.cpp"], set()))
    check("pinMode(SWITCH_OFF_LED_PIN, INPUT_PULLUP)" in disp and "DebouncedButton powerButton(SWITCH_OFF_LED_PIN)" in disp,
          "display_manager.cpp: GPIO9 is read through the P1 DebouncedButton with INPUT_PULLUP")
    for rel, text in srcs:
        body = strip_comments(preprocess(text, set()))
        check(not re.search(r"\bpinMode\s*\(\s*(?:SWITCH_OFF_LED_PIN|D10)\s*,\s*OUTPUT", body), "%s: GPIO9 is never an output" % rel)


def test_ini():
    print("platformio.ini")
    ini = read_src(os.path.join(ROOT, "platformio.ini"))
    sections = re.split(r"^\[(.+?)\]\s*$", ini, flags=re.M)
    cfg = {sections[i]: sections[i + 1] for i in range(1, len(sections), 2)}
    check(set(cfg) == {"platformio", "env:seeed_xiao_esp32s3", "env:seeed_xiao_esp32s3_rev2"}, "three sections: platformio and the two environments (%s)" % sorted(cfg))
    check(re.search(r"^default_envs\s*=\s*seeed_xiao_esp32s3\s*$", cfg["platformio"], re.M) is not None,
          "default_envs is the current board: `pio run` and `pio run -t upload` never build or flash the rev2 image")
    check("BOARD_REV2" not in cfg["env:seeed_xiao_esp32s3"], "the current board's environment does not define BOARD_REV2")
    rev2 = cfg["env:seeed_xiao_esp32s3_rev2"]
    check("-D BOARD_REV2" in rev2 and "extends = env:seeed_xiao_esp32s3" in rev2 and "${env:seeed_xiao_esp32s3.build_flags}" in rev2,
          "the rev2 environment extends the current one, repeats its flags and adds -D BOARD_REV2")


def main():
    srcs = sources()
    check(len(srcs) >= 30, "%d source files scanned" % len(srcs))
    test_default_variant(srcs)
    test_rev2_variant(srcs)
    test_default_pins(srcs)
    test_ini()
    print("\n%d checks passed, %d failed" % (OKS[0], len(FAILS)))
    if FAILS:
        print("FAILED")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
