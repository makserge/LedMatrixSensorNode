#!/usr/bin/env python3
"""P2 host-side model of Ld2412Sensor::presence(), the tri-state next to the unchanged isPresent() (board revision 2,
-D BOARD_REV2): src/ld2412_sensor.cpp, include/ld2412_sensor.h.

  * isPresent() is the raw OUT level, set by update() every 500 ms exactly as in P1 (the spectrum view reads it): it must
    equal the OUT level at every poll in every phase (inside the OUT mask, in the warm-up, for pulses shorter than 2 s,
    while the radars are off).
  * presence() is UNKNOWN from the moment a park is requested until 3 s after the next power-up (LD2412_OUT_IGNORE_MS), a
    LOW OUT is UNKNOWN, not ABSENT, until 12 s after the power-up (LD2412_STATIC_WARMUP_MS), PRESENT only after OUT has been
    HIGH for 2 s (LD2412_OUT_MIN_HIGH_MS), and falls to ABSENT as soon as OUT falls. It is evaluated at call time for the
    park request and the power state, so a poll that lags cannot publish a stale PRESENT.
  * While the radars are still powered, a park request keeps the value UNKNOWN but keeps TIMING the OUT level, so a
    withdrawn (or timed-out) request does not restart the 2 s debounce: no false ABSENT for static presence. Only a real
    power-off clears the timer.
  * A power-up stamp a few ms AHEAD of the sampled now (RadarPower stored it after update() sampled millis()) counts as
    'just powered up' (elapsed 0): UNKNOWN, never ABSENT.

The model mirrors update() / updatePresence() / presence() line by line, is driven by simulated OUT signals and
RadarPower flags, and is tied to the C++ mechanically (constants from include/radar_constants.h; the statements of the
BOARD_REV2 variant and the unchanged P1 text of the default variant are read from the sources).

Run: python -I -B tools/tests/ld2412_presence_model.py        (standard library only; exit code 0 = all passed)
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
LD2412_OUT_IGNORE_MS = ENV["LD2412_OUT_IGNORE_MS"]
LD2412_OUT_MIN_HIGH_MS = ENV["LD2412_OUT_MIN_HIGH_MS"]
LD2412_STATIC_WARMUP_MS = ENV["LD2412_STATIC_WARMUP_MS"]
LD2412_SENSOR_INTERVAL = ENV["LD2412_SENSOR_INTERVAL"]
LD2412_HEARTBEAT_MS = ENV["LD2412_HEARTBEAT_MS"]
PARK_ACK_TIMEOUT_MS = ENV["PARK_ACK_TIMEOUT_MS"]
RADAR_STAMP_MAX_AGE_MS = ENV["RADAR_STAMP_MAX_AGE_MS"]

UNKNOWN, ABSENT, PRESENT = "UNKNOWN", "ABSENT", "PRESENT"


class Ld2412:
    """C++: Ld2412Sensor. update() runs every 500 ms (LD2412Task)."""

    def __init__(self, shared):
        self.g = shared
        self._presence = False
        self._tri = UNKNOWN
        self._high_since = 0
        self._high_since_valid = False

    def update(self, now, out):
        """C++: Ld2412Sensor::update()."""
        new = bool(out)
        if new != self._presence:
            self._presence = new                          # exactly today's code
        self.update_presence(now, new)

    def _time_out(self, now, out):
        """C++: the two-line OUT timer (start on a rising OUT, clear on a low OUT); it stands in the park branch and after the mask."""
        if out:
            if not self._high_since_valid:
                self._high_since, self._high_since_valid = now, True
        else:
            self._high_since_valid = False

    def update_presence(self, now, out):
        """C++: Ld2412Sensor::updatePresence()."""
        g = self.g
        powered = g["powered"]                            # read once
        if not powered:
            self._tri, self._high_since_valid = UNKNOWN, False
            return
        if g["park_request"]:                             # radars still powered: UNKNOWN, but OUT keeps being timed
            self._tri = UNKNOWN
            self._time_out(now, out)
            return
        since = (now - g["powered_at"]) & U32
        if since >= 0x80000000:                           # (int32_t)since < 0: the stamp is a few ms ahead of the sampled now
            since = 0
        if since < LD2412_OUT_IGNORE_MS:
            self._tri, self._high_since_valid = UNKNOWN, False
            return
        self._time_out(now, out)
        if out and ((now - self._high_since) & U32) >= LD2412_OUT_MIN_HIGH_MS:
            self._tri = PRESENT
        elif since < LD2412_STATIC_WARMUP_MS:
            self._tri = UNKNOWN
        else:
            self._tri = ABSENT

    def isPresent(self):
        """C++: bool isPresent() const { return _presence; } (unchanged)."""
        return self._presence

    def presence(self):
        """C++: Ld2412Sensor::presence(): the park request and the power state are read at call time."""
        if self.g["park_request"] or not self.g["powered"]:
            return UNKNOWN
        return self._tri


def drive(sim_out, g, until_ms, events=(), dev=None):
    """sim_out(t) -> OUT level. events: (t_ms, fn(g)). Returns [(t, raw, tri, out)] at every 500 ms poll.
    dev: an Ld2412 on g, when the caller wants to look at its state afterwards."""
    d = dev if dev is not None else Ld2412(g)
    rows = []
    ev = sorted(events, key=lambda e: e[0])
    t = 0
    while t <= until_ms:
        while ev and ev[0][0] <= t:
            ev.pop(0)[1](g)
        out = sim_out(t) if g["powered"] else 0           # an unpowered module drives nothing; INPUT_PULLDOWN reads LOW
        d.update(t, out)
        rows.append((t, d.isPresent(), d.presence(), out))
        t += LD2412_SENSOR_INTERVAL
    return rows


def test_presence():
    print("LD2412 presence() and the unchanged isPresent()")
    check((LD2412_OUT_IGNORE_MS, LD2412_OUT_MIN_HIGH_MS, LD2412_STATIC_WARMUP_MS, LD2412_SENSOR_INTERVAL)
          == (3000, 2000, 12000, 500), "constants: 3 s mask, 2 s minimum high, 12 s warm-up, 500 ms poll")
    g = {"powered": True, "park_request": False, "powered_at": 0}

    def out_seq(t):
        if 1000 <= t < 1500:
            return 1        # a 0.5 s spurious pulse during the OUT mask
        if 5000 <= t < 6000:
            return 1        # a 1 s pulse in the warm-up (shorter than 2 s)
        if 7000 <= t < 10000:
            return 1        # a 3 s presence in the warm-up: PRESENT before 12 s
        if 20000 <= t < 20800:
            return 1        # a 0.8 s pulse in steady state
        if 30000 <= t < 40000:
            return 1        # a 10 s presence
        return 0
    rows = drive(out_seq, g, 50000)
    by = {t: (raw, tri, out) for t, raw, tri, out in rows}
    check(all(raw == bool(out) for t, raw, tri, out in rows), "isPresent() equals the raw OUT level at every poll (all phases)")
    check(all(by[t][1] == UNKNOWN for t in range(0, 3000, 500)), "UNKNOWN during the 3 s OUT mask, also for the 0.5 s pulse")
    check(by[5000][1] == UNKNOWN and by[5500][1] == UNKNOWN and by[6000][1] == UNKNOWN, "a 1 s pulse in the warm-up stays UNKNOWN")
    check(by[9000][1] == PRESENT, "OUT high for 2 s in the warm-up is PRESENT before 12 s (rose at 7.0 s, PRESENT at 9.0 s)")
    check(by[8500][1] == UNKNOWN, "... and not earlier than 2 s after it rose")
    check(by[10500][1] == UNKNOWN and by[11500][1] == UNKNOWN, "low OUT in the warm-up is UNKNOWN, not ABSENT")
    check(by[12000][1] == ABSENT and by[15000][1] == ABSENT, "LOW OUT after the 12 s warm-up is ABSENT")
    check(by[20000][1] == ABSENT and by[20500][1] == ABSENT, "a 0.8 s pulse in steady state never makes PRESENT")
    check(by[31500][1] != PRESENT, "(before the 2 s mark it is not PRESENT)")
    check(by[32000][1] == PRESENT and by[39500][1] == PRESENT, "PRESENT once OUT has been HIGH for 2 s, by day too")
    check(by[40000][1] == ABSENT, "back to ABSENT as soon as OUT falls (no hold)")
    # park request: UNKNOWN from that poll; isPresent() still follows OUT while the module is powered
    g = {"powered": True, "park_request": False, "powered_at": 0}
    rows = drive(lambda t: 1 if t >= 25000 else 0, g, 40000,
                 events=[(30000, lambda g: g.update(park_request=True)), (31000, lambda g: g.update(powered=False))])
    by = {t: (raw, tri, out) for t, raw, tri, out in rows}
    check(by[29500][1] == PRESENT and by[30000][1] == UNKNOWN, "UNKNOWN from the park request on")
    check(by[30500][0] is True and by[30500][1] == UNKNOWN, "isPresent() keeps reading OUT while the module is still powered")
    check(by[31000][0] is False and by[35000][0] is False and by[35000][1] == UNKNOWN, "powered off: OUT low, isPresent() false, presence() UNKNOWN")
    # next power-up: the mask and the warm-up start again
    g = {"powered": False, "park_request": True, "powered_at": 0}
    rows = drive(lambda t: 1 if t >= 1000 else 0, g, 30000,
                 events=[(2000, lambda g: g.update(powered=True, park_request=False, powered_at=2000))])
    by = {t: (raw, tri, out) for t, raw, tri, out in rows}
    check(all(by[t][1] == UNKNOWN for t in (0, 1500, 2000, 4500)), "UNKNOWN while off and for the mask after the power-up (t < 5 s)")
    check(by[5000][1] == UNKNOWN and by[7000][1] == PRESENT, "after the mask: PRESENT once OUT was high for 2 s")
    # frames p2 and the legacy topic follow presence(): 1 only while PRESENT; None while UNKNOWN
    p2 = lambda tri: 1 if tri == PRESENT else 0
    legacy = lambda tri: {PRESENT: "1", ABSENT: "0", UNKNOWN: "None"}[tri]
    check(p2(UNKNOWN) == 0 and p2(ABSENT) == 0 and p2(PRESENT) == 1, "p2 is 1 only while PRESENT")
    check(legacy(UNKNOWN) == "None" and legacy(PRESENT) == "1" and legacy(ABSENT) == "0", "legacy topic payloads")


def test_call_time_rule():
    print("presence() reads the park request and the power state at call time")
    g = {"powered": True, "park_request": False, "powered_at": 0}
    d = Ld2412(g)
    for t in range(0, 30000, LD2412_SENSOR_INTERVAL):
        d.update(t, 1 if t >= 5000 else 0)
    check(d.presence() == PRESENT, "PRESENT after a long presence")
    g["park_request"] = True                         # RadarPower raises the request between two polls
    check(d.presence() == UNKNOWN, "UNKNOWN at once, before the next 500 ms poll (a stale 1 would reach the legacy topic and p2)")
    check(d.isPresent() is True, "isPresent() still returns the raw OUT level")
    g["park_request"] = False                        # withdrawn before any poll saw it: the stored tri-state is untouched
    check(d.presence() == PRESENT, "a withdrawn request makes the last evaluated value visible again")
    # (a poll that DID see the request keeps the timer running, so the value after the withdrawal is the timed one:
    #  test_park_withdrawn() below)
    d.update(30000, 1)
    g["powered"] = False
    check(d.presence() == UNKNOWN, "UNKNOWN as soon as the radars are not powered")


def test_park_withdrawn():
    print("a park request while the radars stay powered keeps timing OUT: no false ABSENT after a withdrawal or a park timeout")
    HOUR = 3600 * 1000
    T = 3 * HOUR                                         # OUT has been HIGH for hours (static presence)

    def request(g):
        g["park_request"] = True

    def withdraw(g):
        g["park_request"] = False

    # (a) a withdrawn request (schedule switched off, HA state change, manual OFF then ON)
    g = {"powered": True, "park_request": False, "powered_at": 0}
    d = Ld2412(g)
    rows = drive(lambda t: 1 if t >= 5000 else 0, g, T + 8000, events=[(T, request), (T + 3000, withdraw)], dev=d)
    by = {t: (raw, tri, out) for t, raw, tri, out in rows}
    check(by[T - 500][1] == PRESENT, "a: PRESENT before the request (OUT high for hours)")
    check(all(by[t][1] == UNKNOWN for t in range(T, T + 3000, 500)), "a: UNKNOWN at every poll during the request")
    check(all(by[t][0] is True for t in range(T, T + 3000, 500)), "a: isPresent() keeps reading the raw OUT during the request")
    check(by[T + 3000][1] == PRESENT, "a: PRESENT again at the very poll after the withdrawal (the timer survived the request)")
    check(all(tri != ABSENT for t, raw, tri, out in rows if T - 500 <= t <= T + 8000), "a: never ABSENT around the request (OUT never dropped)")
    # (a2) the same with a request shorter than one poll interval seen by a single poll
    g = {"powered": True, "park_request": False, "powered_at": 0}
    rows = drive(lambda t: 1 if t >= 5000 else 0, g, T + 3000, events=[(T, request), (T + 500, withdraw)])
    by = {t: tri for t, raw, tri, out in rows}
    check(by[T] == UNKNOWN and by[T + 500] == PRESENT, "a2: a request seen by one single poll: UNKNOWN, then PRESENT at once")
    # (b) a park timeout: RadarPower keeps the request set past PARK_ACK_TIMEOUT_MS (the radars stay ON), then it goes away
    g = {"powered": True, "park_request": False, "powered_at": 0}
    t_end = T + PARK_ACK_TIMEOUT_MS + 4000
    rows = drive(lambda t: 1 if t >= 5000 else 0, g, t_end + 5000, events=[(T, request), (t_end, withdraw)])
    by = {t: tri for t, raw, tri, out in rows}
    check(all(by[t] == UNKNOWN for t in range(T, t_end, 500)), "b: UNKNOWN through the whole request, also after PARK_ACK_TIMEOUT_MS")
    check(by[t_end] == PRESENT, "b: PRESENT at once when the timed-out request goes away")
    check(all(tri != ABSENT for t, raw, tri, out in rows if t >= T), "b: never ABSENT after a park timeout")
    # (c) OUT drops during the request and rises again before the withdrawal: the 2 s debounce starts again from the rise
    g = {"powered": True, "park_request": False, "powered_at": 0}
    d = Ld2412(g)
    rows = drive(lambda t: 0 if T + 1000 <= t < T + 2500 else (1 if t >= 5000 else 0), g, T + 8000,
                 events=[(T, request), (T + 3000, withdraw)], dev=d)
    by = {t: (raw, tri, out) for t, raw, tri, out in rows}
    check(all(by[t][1] == UNKNOWN for t in range(T, T + 3000, 500)), "c: UNKNOWN during the request, also while OUT is low")
    check(by[T + 3000][1] == ABSENT and by[T + 3500][1] == ABSENT and by[T + 4000][1] == ABSENT,
          "c: after the withdrawal OUT has been HIGH for less than 2 s: not PRESENT (ABSENT, as in the normal path)")
    check(by[T + 4500][1] == PRESENT, "c: PRESENT once OUT has been HIGH for LD2412_OUT_MIN_HIGH_MS again (rose at +2.5 s, PRESENT at +4.5 s)")
    # (c2) OUT still low at the withdrawal: ABSENT at once (the radars are past the warm-up)
    g = {"powered": True, "park_request": False, "powered_at": 0}
    rows = drive(lambda t: 0 if T + 1000 <= t < T + 4000 else (1 if t >= 5000 else 0), g, T + 6000,
                 events=[(T, request), (T + 3000, withdraw)])
    by = {t: tri for t, raw, tri, out in rows}
    check(by[T + 3000] == ABSENT and by[T + 3500] == ABSENT, "c2: OUT low at the withdrawal: ABSENT at once")
    # (d) the timer runs while the request is set, and a low OUT clears it there too
    g = {"powered": True, "park_request": False, "powered_at": 0}
    d = Ld2412(g)
    d.update(10000, 1)
    d.update(10500, 1)
    g["park_request"] = True
    d.update(11000, 1)
    check(d._high_since_valid and d._high_since == 10000, "d: a request does not restart the timer of an OUT that stays HIGH")
    d.update(11500, 0)
    check(not d._high_since_valid and d.presence() == UNKNOWN, "d: a low OUT clears the timer during the request")
    d.update(12000, 1)
    check(d._high_since_valid and d._high_since == 12000, "d: a rising OUT starts it during the request")
    # (e) a real power-off clears it; the next power-up starts the mask and the warm-up again (as before)
    g = {"powered": True, "park_request": False, "powered_at": 0}
    d = Ld2412(g)
    pu = T + 20000
    rows = drive(lambda t: 1 if t >= 5000 else 0, g, pu + 20000,
                 events=[(T, request), (T + 2000, lambda g: g.update(powered=False)),
                         (pu, lambda g: g.update(powered=True, park_request=False, powered_at=pu))], dev=d)
    by = {t: (raw, tri, out) for t, raw, tri, out in rows}
    check(by[T + 2000][1] == UNKNOWN and by[T + 2000][0] is False, "e: powered off: UNKNOWN, OUT low, isPresent() false")
    check(all(by[t][1] == UNKNOWN for t in range(T, pu + LD2412_OUT_IGNORE_MS, 500)), "e: UNKNOWN from the request through the 3 s mask after the power-up")
    check(by[pu + 3000][1] == UNKNOWN and by[pu + 4500][1] == UNKNOWN, "e: after the mask OUT must be HIGH for 2 s again (the old timer is gone)")
    check(by[pu + 5000][1] == PRESENT, "e: PRESENT 2 s after the mask ended")
    g = {"powered": True, "park_request": False, "powered_at": 0}
    d = Ld2412(g)
    d.update(T, 1)
    g["park_request"] = True
    d.update(T + 500, 1)
    g["powered"] = False
    d.update(T + 1000, 0)
    check(not d._high_since_valid and d.presence() == UNKNOWN, "e: the power-off clears the timer")
    # (f) the power-up order of RadarPower: request cleared first, then the stamp, then 'powered'
    g = {"powered": False, "park_request": True, "powered_at": 0}
    d = Ld2412(g)
    d.update(1000, 1)
    g["park_request"] = False                                   # RadarPower: request cleared first ...
    d.update(1500, 1)
    check(d.presence() == UNKNOWN, "f: request cleared, not yet powered: still UNKNOWN")
    g["powered_at"], g["powered"] = 2000, True                  # ... then the stamp, then 'powered'
    d.update(2000, 1)
    check(d.presence() == UNKNOWN and not d._high_since_valid, "f: just powered up: the mask holds, no timer")


def test_powered_at_ahead():
    print("B1: the power-up stamp a few ms ahead of the sampled now")
    for lead in (1, 3, 5, 20):
        for out in (0, 1):
            g = {"powered": True, "park_request": False, "powered_at": 100000 + lead}
            d = Ld2412(g)
            d.update(100000, out)                      # now sampled first, RadarPower stored the stamp, then the loads
            check(d.presence() == UNKNOWN and not d._high_since_valid,
                  "stamp %d ms ahead, OUT=%d: UNKNOWN (inside the mask), not ABSENT" % (lead, out))
    g = {"powered": True, "park_request": False, "powered_at": 3}
    d = Ld2412(g)
    d.update(0xFFFFFFFE, 0)                            # the same across the millis() wrap
    check(d.presence() == UNKNOWN, "the same across the millis() wrap")
    # the polls after it are the normal ones, counted from the stamp
    g = {"powered": True, "park_request": False, "powered_at": 100003}
    d = Ld2412(g)
    seen = {}
    for t in range(100000, 116000, LD2412_SENSOR_INTERVAL):
        d.update(t, 0)
        seen[t] = d.presence()
    check(seen[100000] == UNKNOWN and seen[112000] == UNKNOWN and seen[112500] == ABSENT,
          "LOW OUT: UNKNOWN until 12 s after the stamp, ABSENT from the first poll past it")
    g = {"powered": True, "park_request": False, "powered_at": 100003}
    d = Ld2412(g)
    seen = {}
    for t in range(100000, 116000, LD2412_SENSOR_INTERVAL):
        d.update(t, 1)
        seen[t] = d.presence()
    check(seen[100000] == UNKNOWN and seen[103000] == UNKNOWN and seen[105000] == UNKNOWN and seen[105500] == PRESENT,
          "HIGH OUT: PRESENT 2 s after the mask (poll 103.5 s starts the timer, 105.5 s is PRESENT)")
    # the clamp only catches an AHEAD stamp: a long uptime (keepOld() keeps the stamp younger than 2^31) is still ABSENT
    for age in (LD2412_STATIC_WARMUP_MS, RADAR_STAMP_MAX_AGE_MS, 0x7FFFFFFF):
        g = {"powered": True, "park_request": False, "powered_at": 5}
        d = Ld2412(g)
        d.update((5 + age) & U32, 0)
        check(d.presence() == ABSENT, "a stamp %d ms old, LOW OUT: ABSENT (the clamp does not touch real elapsed time)" % age)


def test_wrap():
    print("the epoch wrap: poweredAt just below 2^32")
    g = {"powered": True, "park_request": False, "powered_at": 0xFFFFFFFF - 5000}
    d = Ld2412(g)
    seen = []
    for k in range(40):
        t = (0xFFFFFFFF - 5000 + k * 500) & U32
        d.update(t, 0)
        seen.append(d.presence())
    check(seen[0] == UNKNOWN and seen[23] == UNKNOWN and seen[24] == ABSENT, "the 12 s warm-up survives the millis() wrap")
    g = {"powered": True, "park_request": False, "powered_at": 0xFFFFFFFF - 5000}
    d = Ld2412(g)
    seen = []
    for k in range(40):
        t = (0xFFFFFFFF - 5000 + k * 500) & U32
        d.update(t, 1 if k >= 8 else 0)            # OUT rises at +4 s, i.e. before the wrap, and stays high across it
        seen.append(d.presence())
    check(seen[8] == UNKNOWN and seen[12] == PRESENT and seen[39] == PRESENT, "the 2 s minimum-high timer survives the wrap")


def test_cpp_links():
    print("C++ link: ld2412_sensor.h / .cpp (BOARD_REV2 and default variants)")
    hdr = read_src("include/ld2412_sensor.h")
    cpp = read_src("src/ld2412_sensor.cpp")
    h_rev2, h_def = flat(strip_comments(preprocess(hdr, {"BOARD_REV2"}))), flat(strip_comments(preprocess(hdr, set())))
    c_rev2, c_def = flat(strip_comments(preprocess(cpp, {"BOARD_REV2"}))), flat(strip_comments(preprocess(cpp, set())))
    check("bool isPresent() const { return _presence; }" in h_rev2 and "bool isPresent() const { return _presence; }" in h_def,
          "isPresent() is the raw level in both builds")
    check("Ld2412Presence presence() const;" in h_rev2 and "enum class Ld2412Presence : uint8_t { UNKNOWN = 0, ABSENT = 1, PRESENT = 2 };" in h_rev2,
          "REV2: the tri-state enum and the getter")
    check("Ld2412Presence" not in h_def and "presence()" not in h_def and "atomic" not in h_def, "default build: no tri-state in the header")
    p1_update = ('void Ld2412Sensor::update() { bool newPresence = (digitalRead(RADAR_PRESENCE_PIN) == HIGH); '
                 'if (newPresence != _presence) { _presence = newPresence; '
                 'Serial.printf("LD2412 presence: %s\\n", _presence ? "DETECTED" : "CLEAR"); } }')
    check(p1_update in c_def, "default build: update() is the P1 text, token for token")
    check("Serial.printf(\"LD2412 presence: %s\\n\", _presence ? \"DETECTED\" : \"CLEAR\"); } updatePresence(millis(), newPresence); }" in c_rev2,
          "REV2: update() keeps the three P1 lines, then calls updatePresence(millis(), newPresence)")
    check("updatePresence" not in c_def and "g_parkRequest" not in c_def and "radar_power.h" not in c_def, "default build: nothing of the tri-state")
    check("if (g_parkRequest.load() || !g_radarPowered.load()) return Ld2412Presence::UNKNOWN; return (Ld2412Presence)_tri.load();" in c_rev2,
          "presence(): park request or unpowered -> UNKNOWN at call time, else the stored value")
    body = c_rev2[c_rev2.index("void Ld2412Sensor::updatePresence("):]
    timer = "if (out) { if (!_highSinceValid) { _highSinceMs = nowMs; _highSinceValid = true; } } else { _highSinceValid = false; }"
    for needle in ("const bool powered = g_radarPowered.load();",
                   "if (!powered) { _tri = (uint8_t)Ld2412Presence::UNKNOWN; _highSinceValid = false; return; }",
                   "if (g_parkRequest.load()) { _tri = (uint8_t)Ld2412Presence::UNKNOWN; " + timer + " return; }",
                   "return; } uint32_t since = nowMs - g_poweredAtMs.load();",
                   "if ((int32_t)since < 0) since = 0;",
                   "if (since < LD2412_OUT_IGNORE_MS) { _tri = (uint8_t)Ld2412Presence::UNKNOWN; _highSinceValid = false; return; }",
                   "} " + timer + " if (out && (uint32_t)(nowMs - _highSinceMs) >= LD2412_OUT_MIN_HIGH_MS) _tri = (uint8_t)Ld2412Presence::PRESENT;",
                   "else if (since < LD2412_STATIC_WARMUP_MS) _tri = (uint8_t)Ld2412Presence::UNKNOWN;",
                   "else _tri = (uint8_t)Ld2412Presence::ABSENT;"):
        check(needle in body, "updatePresence(): %s" % needle[:90])
    check(body.count(timer) == 2, "updatePresence(): the OUT timer stands twice, in the park branch and after the mask")
    check("const uint32_t since" not in body, "updatePresence(): 'since' is not const any more (the clamp assigns it)")
    check(body.count("g_radarPowered") == 1 and body.count("g_parkRequest") == 1 and body.count("g_poweredAtMs") == 1,
          "updatePresence(): the power flag, the request and the stamp are each loaded once (no torn pair)")
    check(body.index("g_radarPowered") < body.index("g_parkRequest") < body.index("g_poweredAtMs") < body.index("(int32_t)since")
          < body.index("LD2412_OUT_IGNORE_MS") < body.index("LD2412_OUT_MIN_HIGH_MS") < body.index("LD2412_STATIC_WARMUP_MS"),
          "updatePresence(): power, park (before the stamp is read), the clamp, the mask, the 2 s timer, the 12 s warm-up (the model's order)")
    spectrum = strip_comments(read_src("src/spectrum_analyzer.cpp"))
    check("ld2412Sensor.isPresent()" in spectrum and "presence()" not in spectrum.replace("isPresent()", ""), "the spectrum view still reads the raw isPresent()")


def main():
    test_presence()
    test_call_time_rule()
    test_park_withdrawn()
    test_powered_at_ahead()
    test_wrap()
    test_cpp_links()
    print("\n%d checks passed, %d failed" % (OKS[0], len(FAILS)))
    if FAILS:
        print("FAILED")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
