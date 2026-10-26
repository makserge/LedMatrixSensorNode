#!/usr/bin/env python3
"""P2 host-side model of the GPIO8 button ladder (board revision 2, -D BOARD_REV2): the decoder LadderButtons in
src/display_manager.cpp. Both buttons share one ADC pin: idle about 3.3 V, the mode button 0 V (both pressed also reads
0 V), the power / LED-off button about 1.65 V (through R8). The decoder reads analogReadMilliVolts() on every
DisplayTask poll (10-50 ms), classifies the reading into a band, confirms a press edge with a second read 3 ms later,
fires once per press and re-arms after BUTTON_DEBOUNCE_MS of idle.

The model mirrors LadderButtons::classify() and ::poll() line by line (docstrings name the C++), is driven by simulated
voltage signals (clean presses, contact bounce, a mode edge passing through the power band, both buttons, holds, a
button held at boot, release bounce, the UNSURE band, and 2000 random presses), and is tied to the C++ mechanically: the
band edges and times are parsed from include/radar_constants.h / constants.h, and the classify() and poll() statements
and the REV2 / default variants of the button code are read from src/display_manager.cpp and include/pins.h.

Run: python -I -B tools/tests/ladder_model.py        (standard library only; exit code 0 = all passed)
"""
import ast
import os
import random
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


def flat(text):
    return re.sub(r"\s+", " ", text)


def parse_constants(text, env):
    text = strip_comments(text)
    for m in re.finditer(r"static\s+const\s+[\w\s\*]+?\b(\w+)\s*=\s*([^;]+);", text):
        name, rhs = m.group(1), m.group(2).strip()
        try:
            env[name] = ast.literal_eval(re.sub(r"\b(\d+)[uUlL]+\b", r"\1", rhs))
        except Exception:
            pass
    return env


ENV = {}
parse_constants(read_src("include/constants.h"), ENV)
parse_constants(read_src("include/radar_constants.h"), ENV)

LADDER_MODE_MAX_MV = ENV["LADDER_MODE_MAX_MV"]
LADDER_POWER_MIN_MV = ENV["LADDER_POWER_MIN_MV"]
LADDER_POWER_MAX_MV = ENV["LADDER_POWER_MAX_MV"]
LADDER_IDLE_MIN_MV = ENV["LADDER_IDLE_MIN_MV"]
LADDER_CONFIRM_MS = ENV["LADDER_CONFIRM_MS"]
BUTTON_DEBOUNCE_MS = ENV["BUTTON_DEBOUNCE_MS"]

# ============================================================================ the model
IDLE, MODE, POWER, UNSURE = "IDLE", "MODE", "POWER", "UNSURE"
NONE, EV_MODE, EV_POWER = "NONE", "EV_MODE", "EV_POWER"


def classify(mv):
    """C++: LadderButtons::classify()."""
    if mv <= LADDER_MODE_MAX_MV:
        return MODE
    if LADDER_POWER_MIN_MV <= mv <= LADDER_POWER_MAX_MV:
        return POWER
    if mv >= LADDER_IDLE_MIN_MV:
        return IDLE
    return UNSURE


class LadderButtons:
    """C++: struct LadderButtons. read(t_ms) -> mV; the confirm read is read(t + LADDER_CONFIRM_MS)."""

    def __init__(self, read, t0=0):
        self.read = read
        self.armed = classify(read(t0)) == IDLE          # a button held during boot fires only after release
        self.releasing = False
        self.since = 0

    def poll(self, now):
        b = classify(self.read(now))
        if b in (MODE, POWER):
            self.releasing = False
            if not self.armed:
                return NONE                              # already fired; wait for the release
            confirm = classify(self.read(now + LADDER_CONFIRM_MS))      # vTaskDelay(3 ms) then a second read
            if confirm != b:
                return NONE                              # edge or bounce: decide next poll
            self.armed = False
            return EV_MODE if b == MODE else EV_POWER
        if b == IDLE and not self.armed:
            if not self.releasing:
                self.releasing = True
                self.since = now
            elif now - self.since >= BUTTON_DEBOUNCE_MS:
                self.armed = True
        elif b == UNSURE:
            self.releasing = False
        return NONE


def run_ladder(signal, until_ms, period=33):
    lb = LadderButtons(signal)
    ev = []
    t = 0
    while t < until_ms:
        e = lb.poll(t)
        if e != NONE:
            ev.append((t, e))
        t += period
    return ev


def steps(*segs):
    """segs: (from_ms, mv); the level holds until the next segment."""
    segs = sorted(segs)

    def f(t):
        v = segs[0][1]
        for a, mv in segs:
            if t >= a:
                v = mv
        return v
    return f


IDLE_MV = 3150


def test_bands():
    print("band edges")
    check((LADDER_MODE_MAX_MV, LADDER_POWER_MIN_MV, LADDER_POWER_MAX_MV, LADDER_IDLE_MIN_MV, LADDER_CONFIRM_MS, BUTTON_DEBOUNCE_MS)
          == (500, 1150, 2150, 2600, 3, 30), "constants: 500 / 1150-2150 / 2600 mV, 3 ms confirm, 30 ms debounce")
    for mv, want in ((0, MODE), (500, MODE), (501, UNSURE), (800, UNSURE), (1149, UNSURE), (1150, POWER), (1650, POWER),
                     (2150, POWER), (2151, UNSURE), (2599, UNSURE), (2600, IDLE), (3100, IDLE)):
        check(classify(mv) == want, "classify(%d) = %s" % (mv, classify(mv)))
    # worst-case levels of the design table (5% resistors, 3V3 +-3%, +-50 mV ADC error)
    check(classify(1520 - 50) == POWER and classify(1790 + 50) == POWER and classify(100 + 50) == MODE and classify(2850) == IDLE,
          "worst-case levels of the design table stay in their bands")


def test_presses():
    print("clean presses, taps, bounce, edges")
    idle = IDLE_MV
    ev = run_ladder(steps((0, idle), (100, 20), (300, idle)), 1000)
    check([e for _, e in ev] == [EV_MODE], "clean mode press: one event %s" % ev)
    ev = run_ladder(steps((0, idle), (100, 1650), (300, idle)), 1000)
    check([e for _, e in ev] == [EV_POWER], "clean power press: one event %s" % ev)
    ev = run_ladder(steps((0, idle), (100, 1650), (140, idle)), 1000, period=10)
    check([e for _, e in ev] == [EV_POWER], "40 ms power tap at a 10 ms poll")
    bounce = [(100, 10), (101, idle), (102, 10), (104, idle), (105, 10), (108, idle), (109, 10), (400, idle)]
    ev = run_ladder(steps((0, idle), *bounce), 1000, period=1)
    check([e for _, e in ev] == [EV_MODE], "mode contact bounce (1 ms poll): one event %s" % ev)
    ev = run_ladder(steps((0, idle), *bounce), 1000, period=10)
    check([e for _, e in ev] == [EV_MODE], "mode contact bounce (10 ms poll): one event %s" % ev)
    # the falling edge of the mode button passes through the power band: a single sample there must not fire POWER
    edge = steps((0, idle), (100, 1650), (101, 1650), (102, 10), (400, idle))
    lb = LadderButtons(edge)
    out = [lb.poll(t) for t in (100, 101)]
    check(out == [NONE, NONE], "power-band sample on a falling mode edge is rejected by the confirm read")
    check(lb.poll(133) == EV_MODE, "decided as MODE on the next poll")
    # the rising edge of the mode release passes the power band while not armed: nothing fires
    rel = steps((0, idle), (100, 20), (300, 1650), (301, 1650), (302, idle))
    ev = run_ladder(rel, 1000, period=1)
    check([e for _, e in ev] == [EV_MODE], "the release edge of the mode button through the power band fires nothing %s" % ev)
    # both buttons: 0 V, decodes as mode; the second button while the first is held does nothing
    ev = run_ladder(steps((0, idle), (100, 1650), (200, 20), (300, 1650), (400, idle)), 1000)
    check([e for _, e in ev] == [EV_POWER], "power held, then mode added: only the first press fires %s" % ev)
    ev = run_ladder(steps((0, idle), (100, 20), (200, 1650), (400, idle)), 1000)
    check([e for _, e in ev] == [EV_MODE], "mode held, then power added: only the first press fires %s" % ev)
    ev = run_ladder(steps((0, idle), (100, 20), (400, idle)), 1000)
    check([e for _, e in ev] == [EV_MODE], "both pressed together read 0 V: mode")
    # held for a long time: no repeat
    ev = run_ladder(steps((0, idle), (100, 20), (60000, idle)), 62000)
    check(len(ev) == 1, "holding does not repeat")
    # held during boot: fires only after release and a new press
    ev = run_ladder(steps((0, 20), (500, idle), (800, 20), (900, idle)), 2000)
    check(len(ev) == 1 and ev[0][0] >= 800, "button held at boot: no event until released and pressed again %s" % ev)
    ev = run_ladder(steps((0, 1650), (500, idle), (800, 1650), (900, idle)), 2000)
    check(len(ev) == 1 and ev[0][0] >= 800, "power held at boot: the same %s" % ev)
    # release bounce: low again within 30 ms of releasing does not fire twice
    ev = run_ladder(steps((0, idle), (100, 20), (300, idle), (310, 20), (320, idle), (330, 20), (340, idle)), 1000, period=10)
    check(len(ev) == 1, "release bounce: one event %s" % ev)
    ev = run_ladder(steps((0, idle), (100, 20), (300, idle), (360, 20), (500, idle)), 1000, period=10)
    check(len(ev) == 2, "a press 60 ms after the release fires again %s" % ev)
    # UNSURE readings (cable noise, half-pressed) never fire and never re-arm
    ev = run_ladder(steps((0, idle), (100, 20), (200, 800), (400, 20), (600, idle)), 1000)
    check(len(ev) == 1, "UNSURE band neither fires nor re-arms %s" % ev)
    ev = run_ladder(steps((0, idle), (100, 800), (300, idle)), 1000)
    check(len(ev) == 0, "a half-pressed level in the UNSURE band fires nothing")


def test_random_presses():
    print("2000 random presses (bench goal: 600 presses with 0 misdecodes)")
    for seed in (1, 2, 3, 4):
        rng = random.Random(seed)
        segs, intended, t = [(0, IDLE_MV)], [], 500
        for _ in range(500):
            kind = rng.choice((EV_MODE, EV_POWER))
            hold = rng.randint(60, 400)
            if kind == EV_MODE:
                # press edge passes through the power band for 1-2 ms, then 0 V with chatter in the first 8 ms
                segs += [(t, 1650), (t + rng.randint(1, 2), 20)]
                c = t + 4
                for _ in range(rng.randint(0, 3)):
                    segs += [(c, IDLE_MV), (c + 1, 20)]
                    c += rng.randint(2, 4)
                segs += [(t + hold, 1650), (t + hold + rng.randint(1, 2), IDLE_MV)]
            else:
                segs += [(t, 1650)]
                if rng.random() < 0.5:
                    segs += [(t + 3, IDLE_MV), (t + 4, 1650)]         # a 1 ms dropout inside the confirm window
                segs += [(t + hold, IDLE_MV)]
            intended.append(kind)
            t += hold + rng.randint(150, 500)
        period = rng.choice((10, 20, 33, 50))
        ev = run_ladder(steps(*sorted(segs, key=lambda s: s[0])), t + 1000, period=period)
        check([e for _, e in ev] == intended, "seed %d, poll %d ms: %d presses, %d events, all decoded in order"
              % (seed, period, len(intended), len(ev)))


# ============================================================================ links to the C++
def test_cpp_links():
    print("C++ link: display_manager.cpp, pins.h, radar_constants.h (REV2 and default variants)")
    src = read_src("src/display_manager.cpp")
    rev2 = flat(strip_comments(preprocess(src, {"BOARD_REV2"})))
    default = flat(strip_comments(preprocess(src, set())))
    check("if (mv <= LADDER_MODE_MAX_MV) return BAND_MODE; if (mv >= LADDER_POWER_MIN_MV && mv <= LADDER_POWER_MAX_MV) return BAND_POWER; "
          "if (mv >= LADDER_IDLE_MIN_MV) return BAND_IDLE; return BAND_UNSURE;" in rev2, "LadderButtons::classify() has the model's three bands in this order")
    check("armed = (classify(read()) == BAND_IDLE);" in rev2, "the constructor arms only from IDLE (a button held at boot fires after release)")
    check("vTaskDelay(pdMS_TO_TICKS(LADDER_CONFIRM_MS)); if (classify(read()) != b) return LadderEvent::NONE; armed = false;" in rev2,
          "poll(): the confirm read after LADDER_CONFIRM_MS, then disarm")
    check("else if (nowMs - sinceMs >= BUTTON_DEBOUNCE_MS) armed = true;" in rev2, "poll(): re-armed after BUTTON_DEBOUNCE_MS of idle")
    check("if (!armed) return LadderEvent::NONE;" in rev2 and "return b == BAND_MODE ? LadderEvent::MODE : LadderEvent::POWER;" in rev2,
          "poll(): no repeat while held, mode band = MODE, power band = POWER")
    check("uint32_t mv = analogReadMilliVolts(pin); g_ladderMv = (uint16_t)(mv > 65535 ? 65535 : mv);" in rev2, "read(): analogReadMilliVolts() and g_ladderMv")
    check("LadderButtons ladder(BUTTON_LADDER_PIN);" in rev2 and "case LadderEvent::MODE: DisplayManager::nextView(); break; "
          "case LadderEvent::POWER: DisplayManager::toggleDisplayPower(); break;" in rev2, "displayTask(): mode -> nextView(), power -> toggleDisplayPower()")
    check("pinMode(BUTTON_LADDER_PIN" not in rev2 and "MODE_BUTTON_PIN" not in rev2 and "SWITCH_OFF_LED_PIN" not in rev2,
          "REV2: no pinMode on GPIO8 (INPUT_PULLUP would shift the 1.65 V level) and no use of the current board's pins")
    check("pinMode(MODE_BUTTON_PIN, INPUT_PULLUP); pinMode(SWITCH_OFF_LED_PIN, INPUT_PULLUP);" in default
          and "DebouncedButton modeButton(MODE_BUTTON_PIN); DebouncedButton powerButton(SWITCH_OFF_LED_PIN);" in default
          and "if (modeButton.poll(buttonMs)) { DisplayManager::nextView(); } if (powerButton.poll(buttonMs)) { DisplayManager::toggleDisplayPower(); }" in default,
          "default build: the two DebouncedButton pins and their handling are the P1 code")
    check("LadderButtons" not in default and "g_ladderMv" not in default and "BUTTON_LADDER_PIN" not in default and "LADDER_" not in default,
          "default build: no ladder symbol")
    pins = read_src("include/pins.h")
    p_rev2 = strip_comments(preprocess(pins, {"BOARD_REV2"}))
    p_def = strip_comments(preprocess(pins, set()))
    check("#define BUTTON_LADDER_PIN D9" in re.sub(r"\s+", " ", p_rev2) and "#define RADAR_PWR_EN_PIN D10" in re.sub(r"\s+", " ", p_rev2)
          and "MODE_BUTTON_PIN" not in p_rev2 and "SWITCH_OFF_LED_PIN" not in p_rev2, "pins.h REV2: GPIO8 (D9) is the ladder, GPIO9 (D10) the radar enable; the old names do not exist")
    check("#define MODE_BUTTON_PIN D9" in re.sub(r"\s+", " ", p_def) and "#define SWITCH_OFF_LED_PIN D10" in re.sub(r"\s+", " ", p_def)
          and "BUTTON_LADDER_PIN" not in p_def and "RADAR_PWR_EN_PIN" not in p_def, "pins.h default: the current board's button pins; no GPIO9 output name")
    hdr = read_src("include/display_manager.h")
    check("extern std::atomic<uint16_t> g_ladderMv;" in flat(strip_comments(preprocess(hdr, {"BOARD_REV2"})))
          and "g_ladderMv" not in preprocess(hdr, set()), "display_manager.h: g_ladderMv exists only under REV2")
    ini = read_src("platformio.ini")
    check("default_envs = seeed_xiao_esp32s3\n" in ini and "[env:seeed_xiao_esp32s3_rev2]" in ini and "-D BOARD_REV2" in ini,
          "platformio.ini: default_envs is the current board, the rev2 environment adds -D BOARD_REV2")
    rev2_env = ini[ini.index("[env:seeed_xiao_esp32s3_rev2]"):]
    check("extends = env:seeed_xiao_esp32s3" in rev2_env and "${env:seeed_xiao_esp32s3.build_flags}" in rev2_env,
          "the rev2 environment extends the current one and repeats its build flags")
    check("-D BOARD_REV2" not in ini[:ini.index("[env:seeed_xiao_esp32s3_rev2]")], "the default environment does not define BOARD_REV2")


def main():
    test_bands()
    test_presses()
    test_random_presses()
    test_cpp_links()
    print("\n%d checks passed, %d failed" % (OKS[0], len(FAILS)))
    if FAILS:
        print("FAILED")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
