#!/usr/bin/env python3
"""Host-side model test for src/radar_power.cpp: schedule maths and the RadarPower state machine (P2 spec 12.1).

The firmware cannot run on the PC (Arduino-ESP32), so this file is a Python model that mirrors src/radar_power.cpp
statement by statement (every method names its C++ counterpart in its docstring), driven by simulated tasks:
RadarPower (core 0), LD2454Task (the park handshake), MqttTask (the g_stateDirty protocol) and a clock with a local
time of day, DST jumps and a millis() wrap. It follows the precedent of tools/tests/frames_batcher_model.py.

Mechanical links to the C++ (so a drift fails the test instead of passing silently): every timing constant is read
from include/radar_constants.h; the enum order of include/radar_power.h, the name tables, the NVS keys and defaults of
src/radar_power.cpp are parsed and compared with the model; radarParseOnOff() and radarParseTimeOfDay() are
transliterated character by character and compared with the model on random strings; no source other than
radar_power.cpp may write GPIO9.

Scenarios (OA 12 P2 'Native tests'): window maths, defaults and boot, three schedule days, settle and UART rules, no valid
clock, offline grace, manual OFF across a reboot, rapid toggling, throttle, the five states, the snapshot hand-over,
park cases, OTA pause, the ld2454_no_frames power-cycle, DST nights and the millis() wrap, the 49.7-day wrap of the long-lived
stamps (keepOld), the store order of POWERING -> ON, the status JSON size; the HA-lost stamp stored by another task a moment after
`now` was sampled (A1), a failed NVS write (A2: D32, except 'Radars' ON), the per-key NVS rate limit (A3) and the settle time
of an automatic OFF after a setting change (A4). Each of the four has a sensitivity check: the old behaviour (a class flag of
RadarPower) makes the same scenario fail.

Run: python -I -B tools/tests/radar_power_model.py        exit code 0 = all passed, last line ALL PASSED
"""
import heapq
import os
import random
import re
import sys

sys.dont_write_bytecode = True

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def read_source(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as f:
        return f.read()


def parse_constants(text):
    """`static const <int type> NAME = <number>;` -> {NAME: int}. Strings and expressions are skipped."""
    out = {}
    for m in re.finditer(r"static const\s+[A-Za-z0-9_]+\s+([A-Za-z0-9_]+)\s*=\s*([0-9]+)\s*;", text):
        out[m.group(1)] = int(m.group(2))
    return out


C = parse_constants(read_source("include/radar_constants.h"))


U32 = 0xFFFFFFFF


def elapsed(now, start):
    """C++: src/radar_power.cpp: elapsed(). uint32 elapsed time; the firmware's rule for the 49.7-day millis() wrap."""
    return (now - start) & U32


def keep_old(now, stamp):
    """C++: src/radar_power.cpp: keepOld(). A 32-bit stamp older than STAMP_MAX_AGE_MS moves forward to exactly that age
    (the 49.7-day wrap would make it look new); a stamp a moment ahead of `now` (another task stored it after `now` was
    sampled) is not old."""
    age = elapsed(now, stamp)
    return (now - STAMP_MAX_AGE_MS) & U32 if STAMP_MAX_AGE_MS < age < 0x80000000 else stamp


# ---- constants: read from include/radar_constants.h (a changed value changes the model and the checks that pin it) ------
RADAR_TICK_MS = C["RADAR_TICK_MS"]
RADAR_OFFLINE_GRACE_MS = C["RADAR_OFFLINE_GRACE_MS"]
RADAR_MIN_ON_MS = C["RADAR_MIN_ON_MS"]
RADAR_MIN_OFF_MS = C["RADAR_MIN_OFF_MS"]
RADAR_MAX_POWERUPS_PER_H = C["RADAR_MAX_POWERUPS_PER_H"]
RADAR_ON_SETTLE_MS = C["RADAR_ON_SETTLE_MS"]
RADAR_OFF_SETTLE_MS = C["RADAR_OFF_SETTLE_MS"]
PARK_ACK_TIMEOUT_MS = C["PARK_ACK_TIMEOUT_MS"]
PARK_POLL_MS = C["RADAR_PARK_POLL_MS"]
LD2412_STATIC_WARMUP_MS = C["LD2412_STATIC_WARMUP_MS"]
RADAR_FAULT_CYCLE_MIN_MS = C["RADAR_FAULT_CYCLE_MIN_MS"]
RADAR_NIGHT_START_DEFAULT_MIN = C["RADAR_NIGHT_START_DEFAULT_MIN"]
RADAR_NIGHT_END_DEFAULT_MIN = C["RADAR_NIGHT_END_DEFAULT_MIN"]
RADAR_HISTORY_MAX = C["RADAR_HISTORY_MAX"]
HOUR_MS = C["RADAR_THROTTLE_WINDOW_MS"]
DAY_MS = C["RADAR_STATS_WINDOW_MS"]
STAMP_MAX_AGE_MS = C["RADAR_STAMP_MAX_AGE_MS"]
LD2412_OUT_IGNORE_MS = C["LD2412_OUT_IGNORE_MS"]
RADAR_NVS_MIN_INTERVAL_MS = C["RADAR_NVS_MIN_INTERVAL_MS"]
RADAR_SETTING_SETTLE_MS = C["RADAR_SETTING_SETTLE_MS"]

# ---- enums ---------------------------------------------------------------------------------------------------------
ON, PARKING, OFF_SETTLING, OFF, POWERING = range(5)
PHASE_NAMES = ("ON", "PARKING", "OFF_SETTLING", "OFF", "POWERING")

ST_ON, ST_NIGHT_OFF, ST_FORCED_ON, ST_FORCED_OFF, ST_FAILSAFE_ON = range(5)
STATE_NAMES = ("on", "night_off", "forced_on", "forced_off", "failsafe_on")
SM_CODES = "ANFXS"

RS_SCHEDULE, RS_MANUAL, RS_FAILSAFE, RS_HA_UNKNOWN, RS_OTA, RS_FAULT, RS_THROTTLE, RS_PENDING = range(8)
REASON_NAMES = ("schedule", "manual", "failsafe", "ha_unknown", "ota", "fault", "throttle", "pending")

FAULT_NONE, FAULT_PARK_TIMEOUT, FAULT_LD2454_NO_FRAMES = range(3)
FAULT_NAMES = ("", "park_timeout", "ld2454_no_frames")

HA_UNKNOWN, HA_ONLINE, HA_OFFLINE = range(3)
HA_NAMES = ("unknown", "online", "offline")

CMD_ENABLE, CMD_SCHEDULE, CMD_NIGHT_START, CMD_NIGHT_END, CMD_SCHED_OFFLINE, CMD_LEGACY = range(6)
CMD_COUNT = 6
CMD_KEYS = ("en", "sched", "ns", "ne", "offl", "legacy")


# ---- pure helpers (radar_power.cpp, no state) -----------------------------------------------------------------------
def in_window(now_min, start, end):
    """C++: src/radar_power.cpp: inWindow(). start == end: never; start < end: start <= t < end; start > end (crosses midnight): t >= start or t < end."""
    if start == end:
        return False
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def parse_on_off(text):
    """C++: src/radar_power.cpp: radarParseOnOff(). '1' / '0', or on / off in any case; -1 otherwise (empty, 'auto', '2', ...)."""
    if text == "1":
        return 1
    if text == "0":
        return 0
    t = text.lower()
    if t == "on":
        return 1
    if t == "off":
        return 0
    return -1


def parse_time_of_day(text):
    """C++: src/radar_power.cpp: radarParseTimeOfDay(). 'H:MM', 'HH:MM' or 'HH:MM:SS' -> minutes after midnight (0..1439); None if invalid. The seconds are
    validated (00-59) and dropped. No sign, no spaces, no trailing text; ASCII digits only."""
    parts = text.split(":")
    if len(parts) not in (2, 3):
        return None
    h, m = parts[0], parts[1]
    s = parts[2] if len(parts) == 3 else "00"
    digits = "0123456789"
    if not (1 <= len(h) <= 2 and len(m) == 2 and len(s) == 2):
        return None
    if not all(c in digits for c in h + m + s):
        return None
    hh, mm, ss = int(h), int(m), int(s)
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return hh * 60 + mm


def format_hms(minutes):
    """C++: src/radar_power.cpp: radarFormatHms()"""
    return "%02d:%02d:00" % (minutes // 60, minutes % 60)


def format_hm(minutes):
    """C++: src/radar_power.cpp: radarFormatHm()"""
    return "%02d:%02d" % (minutes // 60, minutes % 60)


# ---- state ---------------------------------------------------------------------------------------------------------
class Settings:
    def __init__(self, en=1, sched=0, ns=RADAR_NIGHT_START_DEFAULT_MIN, ne=RADAR_NIGHT_END_DEFAULT_MIN, offl=0,
                 legacy=0):
        self.en, self.sched, self.ns, self.ne, self.offl, self.legacy = en, sched, ns, ne, offl, legacy


class Nvs:
    """The 'radar' namespace. An absent key reads as its default. Every put() is one flash write.
    `fail` injects a broken NVS: put() then changes nothing and returns False (C++: storeSetting() returns written != 0)."""
    DEFAULTS = {"en": 1, "sched": 0, "ns": RADAR_NIGHT_START_DEFAULT_MIN, "ne": RADAR_NIGHT_END_DEFAULT_MIN,
                "offl": 0, "legacy": 0}

    def __init__(self):
        self.data = {}
        self.writes = []
        self.fail = False
        self.failed = []                # the refused writes (key, value)

    def get(self, key):
        return self.data.get(key, self.DEFAULTS[key])

    def put(self, key, value):
        if self.fail:
            self.failed.append((key, value))
            return False
        self.data[key] = value
        self.writes.append((key, value))
        return True

    def load(self):
        return Settings(*(self.get(k) for k in CMD_KEYS))


class Shared:
    """The atomics of radar_power.h."""

    def __init__(self):
        self.phase = ON
        self.radar_powered = False
        self.power_epoch = 0
        self.powered_at_ms = 0
        self.park_request = False
        self.park_ack = False
        self.ld2454_ready = False       # written by LD2454Task
        self.ld2454_fault = FAULT_NONE  # written by LD2454Task
        self.state_dirty = False        # cleared by MqttTask
        self.ha_state = HA_UNKNOWN
        self.ha_lost_ms = 0


class Status:
    """RadarStatus, the snapshot MqttTask reads. on24x10 is the 24 h on-share in tenths of a percent."""
    FIELDS = ("en", "sched", "ns", "ne", "offl", "legacy", "phase", "powered", "observing", "obs412", "state",
              "reason", "next_min", "ups24", "on24x10", "thr", "fault", "park_timeouts", "since_ms", "ha")

    def __init__(self):
        for f in self.FIELDS:
            setattr(self, f, 0)
        self.next_min = -1

    def copy(self):
        c = Status()
        for f in self.FIELDS:
            setattr(c, f, getattr(self, f))
        return c

    def key(self):
        """The fields whose change sets g_stateDirty: all but the continuously moving on24x10."""
        return tuple(getattr(self, f) for f in self.FIELDS if f != "on24x10")

    @property
    def pw(self):
        return (1 if self.observing else 0) | (2 if self.obs412 else 0)

    @property
    def sm(self):
        return SM_CODES[self.state]


class Decision:
    def __init__(self, off, state, reason, automatic):
        self.off, self.state, self.reason, self.automatic = off, state, reason, automatic


class RadarPower:
    restamp = True          # the 49.7-day stamp refresh (keepOld()); the wrap test switches it off to prove that it is needed
    clamp_ha_stamp = True   # pass(): an HA-lost stamp a few ms ahead of 'now' reads as 'now'; the race test switches it off
    rate_limit = True       # applyPending(): per key at most one NVS store per RADAR_NVS_MIN_INTERVAL_MS; the flapping test switches it off
    en1_ram_on_fail = True  # applyPending(): a failed write of en = 1 still applies to RAM (the one exception to D32)
    settle_rule = True      # stepPhase(): an automatic OFF starts no park within RADAR_SETTING_SETTLE_MS of an applied setting change

    def __init__(self, io, shared, nvs, boot_ms):
        """radarPowerEarlyInit() + radarPhaseAfterBoot(). io provides gpio_set_level(), pin_mode_output(), kick(),
        kick_ld2454() and clock() -> (valid, minute_of_day)."""
        self.io, self.g, self.nvs = io, shared, nvs
        self.s = nvs.load()
        en = self.s.en
        io.gpio_set_level(1 if en else 0)           # latch the level first ...
        io.pin_mode_output()                        # ... then enable the output driver
        g = self.g
        self.pending = [None] * CMD_COUNT
        self.hist = []                              # [up_ms, down_ms or None], 64-bit uptime
        self.boot_ms = boot_ms
        self.park_timeouts = 0
        self.park_timeout_flag = False
        self.cycle_active = False
        self.last_fault_cycle_ms = 0
        self.have_fault_cycle = False
        self.last_on_state = ST_ON
        self.last_off_state = ST_FORCED_OFF
        self.thr = False
        self.park_start_ms = self.off_at_ms = self.off_since_ms = self.on_at_ms = 0
        self.last_store = [boot_ms - RADAR_NVS_MIN_INTERVAL_MS] * CMD_COUNT   # 64-bit uptime of the last store attempt per key: none yet
        self.last_change = boot_ms - RADAR_SETTING_SETTLE_MS                  # 64-bit uptime of the last applied setting change: none yet
        self.store_log = []             # (uptime, key, value, stored) of every NVS store attempt
        self.serial = []                # (uptime, line) of the [RADAR] log lines the tests look at
        self.before_ha_read = None      # test hooks: another task runs at this point of the pass / of applyPending()
        self.before_putback = None
        g.ha_state, g.ha_lost_ms = HA_UNKNOWN, boot_ms
        g.power_epoch = 0
        if en:
            g.phase = POWERING
            g.park_request = g.park_ack = False
            g.radar_powered = False
            self.on_at_ms = boot_ms
        else:
            g.phase = OFF_SETTLING
            g.park_request = g.park_ack = True       # Serial1 was never opened: the park is already done
            g.radar_powered = True                   # until the settle ends
            self.off_at_ms = boot_ms
            self.last_off_state = ST_FORCED_OFF
        self.cur = Status()
        self.cur.since_ms = boot_ms
        self.cur.state = ST_FORCED_ON if en else ST_FORCED_OFF
        self.cur.reason = RS_MANUAL
        self.cur.phase = g.phase
        self.cur.powered = g.radar_powered
        self.cur.ha = HA_UNKNOWN
        s0 = self.s
        self.cur.en, self.cur.sched, self.cur.ns, self.cur.ne = s0.en, s0.sched, s0.ns, s0.ne
        self.cur.offl, self.cur.legacy = s0.offl, s0.legacy
        g.state_dirty = True                        # g_stateDirty = true at boot
        self.dirty_edges = 0

    # radarPowerRequest(): called from MqttTask / the web handler; never blocks, never writes NVS
    def request(self, cmd, value):
        """C++: src/radar_power.cpp: radarPowerRequest()"""
        self.pending[cmd] = value
        self.io.kick()

    # radarSetHaState(): called from MqttTask on every loop; a no-op when nothing changed
    def set_ha_state(self, new_state, now):
        """C++: src/radar_power.cpp: radarSetHaState()"""
        g = self.g
        if g.ha_state == new_state:
            return
        if g.ha_state == HA_ONLINE:
            g.ha_lost_ms = now & U32                 # leaving ONLINE starts the grace; UNKNOWN <-> OFFLINE keeps it
        g.ha_state = new_state
        self.io.kick()

    # called by the task loop right after waitWhileOtaPaused() returned true: the upload does not count towards
    # PARK_ACK_TIMEOUT_MS (design 5.1)
    def after_ota_pause(self, now):
        """C++: src/radar_power.cpp: RadarPower::afterOtaPause()"""
        if self.g.phase == PARKING:
            self.park_start_ms = now & U32

    # ---- one pass of the task loop; returns the wait in ms until the next pass ---------------------------------------
    def pass_(self, now):
        """C++: src/radar_power.cpp: RadarPower::pass()"""
        store_wait = self.apply_pending(now)
        self.refresh_stamps(now)
        valid, now_min, now_sec = self.io.clock()
        if self.before_ha_read:                                  # test hook: MqttTask runs after `now` was sampled
            self.before_ha_read(now)
        if self.restamp and self.g.ha_state != HA_ONLINE:        # the critical section of pass(): no HA for 49.7 days is not 'just lost'
            self.g.ha_lost_ms = keep_old(now, self.g.ha_lost_ms)
        ha_lost = self.g.ha_lost_ms
        if self.clamp_ha_stamp and ((now - ha_lost) & U32) >= 0x80000000:   # (int32_t)(now - haLostMs) < 0: stored after `now` was sampled
            ha_lost = now & U32
        dec = self.decide(valid, now_min, now, ha_lost)
        self.prune_history(now)
        wait = self.step_phase(dec, now)
        self.update_status(dec, valid, now_min, now_sec, now)
        return min(wait, self.next_wait(dec, now), store_wait)

    def refresh_stamps(self, now):
        """C++: src/radar_power.cpp: RadarPower::refreshStamps(). The stamps that outlive 49.7 days, each only while its state lasts."""
        if not self.restamp:
            return
        g = self.g
        if g.phase in (ON, PARKING):
            g.powered_at_ms = keep_old(now, g.powered_at_ms)
        elif g.phase == OFF:
            self.off_since_ms = keep_old(now, self.off_since_ms)
        if self.have_fault_cycle:
            self.last_fault_cycle_ms = keep_old(now, self.last_fault_cycle_ms)

    def apply_pending(self, now):
        """C++: src/radar_power.cpp: RadarPower::applyPending(). Returns the ms until a deferred store is due (0xFFFFFFFF = nothing waits)."""
        pend, self.pending = self.pending, [None] * CMD_COUNT
        s = self.s
        any_cmd = False
        deferred = []
        retry = U32
        for cmd, v in enumerate(pend):
            if v is None:
                continue
            any_cmd = True                           # an echo is due even when nothing changes
            key = CMD_KEYS[cmd]
            if v == getattr(s, key):
                continue                             # unchanged: echoed, nothing written
            since_store = now - self.last_store[cmd]
            if self.rate_limit and since_store < RADAR_NVS_MIN_INTERVAL_MS:     # too soon after the last store of this setting
                deferred.append((cmd, v))
                left = RADAR_NVS_MIN_INTERVAL_MS - since_store
                retry = min(retry, left)
                continue
            self.last_store[cmd] = now
            stored = self.nvs.put(key, v)            # written only on change
            self.store_log.append((now, key, v, stored))
            if stored:
                setattr(s, key, v)                   # RAM follows only what was stored
                self.last_change = now
                self.serial.append((now, "[RADAR] %s = %d" % (key, v)))
            elif self.en1_ram_on_fail and cmd == CMD_ENABLE and v == 1:
                setattr(s, key, 1)                   # the one exception to D32, in the safe direction
                self.last_change = now
                self.serial.append((now, "[RADAR] NVS write failed: %s (applied to RAM only)" % key))
            else:
                self.serial.append((now, "[RADAR] NVS write failed: %s" % key))      # the echo shows the stored value (D32)
        if deferred:
            if self.before_putback:                  # test hook: a newer command arrives while the slots are being worked off
                self.before_putback(now)
            for cmd, v in deferred:
                if self.pending[cmd] is None:        # a newer command for this setting is already waiting: it wins
                    self.pending[cmd] = v
        if any_cmd:
            self.g.state_dirty = True
        return retry

    def decide(self, valid, now_min, now, ha_lost):
        """C++: src/radar_power.cpp: decide()"""
        s, g = self.s, self.g
        if not s.en:
            return Decision(True, ST_FORCED_OFF, RS_MANUAL, False)
        if not s.sched:
            return Decision(False, ST_FORCED_ON, RS_MANUAL, False)
        if not valid:
            return Decision(False, ST_FAILSAFE_ON, RS_FAILSAFE, False)
        if not in_window(now_min, s.ns, s.ne):
            return Decision(False, ST_ON, RS_SCHEDULE, False)
        offline = g.ha_state != HA_ONLINE and elapsed(now, ha_lost) >= RADAR_OFFLINE_GRACE_MS
        if offline and not s.offl:
            return Decision(False, ST_FAILSAFE_ON, RS_HA_UNKNOWN if g.ha_state == HA_UNKNOWN else RS_FAILSAFE, False)
        return Decision(True, ST_NIGHT_OFF, RS_SCHEDULE, True)

    # ---- history of power-ups: the throttle, ups24 and on24 -------------------------------------------------------------
    def prune_history(self, now):
        """C++: src/radar_power.cpp: RadarPower::pruneHistory()"""
        while self.hist and self.hist[0][1] is not None and now - self.hist[0][1] > DAY_MS:
            self.hist.pop(0)

    def powerups_last_hour(self, now):
        """C++: src/radar_power.cpp: RadarPower::powerupsLastHour()"""
        return sum(1 for up, _ in self.hist if now - up < HOUR_MS)

    def oldest_powerup_in_hour(self, now):
        """C++: src/radar_power.cpp: RadarPower::oldestPowerupInHour()"""
        ups = [up for up, _ in self.hist if now - up < HOUR_MS]
        return ups[0] if ups else None

    def record_power_up(self, now):
        """C++: src/radar_power.cpp: RadarPower::recordPowerUp()"""
        if len(self.hist) == RADAR_HISTORY_MAX:
            self.hist.pop(0)
        self.hist.append([now, None])

    def record_power_down(self, now):
        """C++: src/radar_power.cpp: RadarPower::recordPowerDown()"""
        if self.hist and self.hist[-1][1] is None:
            self.hist[-1][1] = now

    def on_share_x10(self, now):
        """C++: src/radar_power.cpp: RadarPower::onShareX10()"""
        window = min(DAY_MS, now - self.boot_ms)
        if window <= 0:
            return 1000
        lo = now - window
        total = 0
        for up, down in self.hist:
            end = now if down is None else down
            a, b = max(up, lo), min(end, now)
            if b > a:
                total += b - a
        return min(1000, (total * 1000 + window // 2) // window)

    def warm_done(self, now):
        """C++: include/radar_power.h: radarWarmedUp()"""
        g = self.g
        return g.phase == ON and not g.park_request and elapsed(now, g.powered_at_ms) >= LD2412_STATIC_WARMUP_MS

    # ---- the phase machine (design 5.2) ---------------------------------------------------------------------------------
    def step_phase(self, dec, now):
        """C++: src/radar_power.cpp: RadarPower::stepPhase()"""
        g = self.g
        wait = RADAR_TICK_MS
        self.thr = False
        # a fault power-cycle is an automatic OFF wanted on top of the decision
        if (g.phase == ON and not self.cycle_active and not dec.off
                and g.ld2454_fault == FAULT_LD2454_NO_FRAMES
                and (not self.have_fault_cycle or elapsed(now, self.last_fault_cycle_ms) >= RADAR_FAULT_CYCLE_MIN_MS)
                and elapsed(now, g.powered_at_ms) >= RADAR_MIN_ON_MS
                and self.powerups_last_hour(now) < RADAR_MAX_POWERUPS_PER_H):
            self.cycle_active = True
            self.have_fault_cycle = True
            self.last_fault_cycle_ms = now & U32
        wants_off = dec.off or self.cycle_active
        automatic = dec.automatic or (self.cycle_active and not dec.off)
        if g.phase == ON:
            if wants_off:
                if elapsed(now, g.powered_at_ms) < RADAR_MIN_ON_MS:
                    pass                                           # deferred, not dropped
                elif automatic and self.powerups_last_hour(now) >= RADAR_MAX_POWERUPS_PER_H:
                    self.thr = True                                # an automatic OFF is refused (never a manual one)
                elif self.settle_rule and dec.automatic and now - self.last_change < RADAR_SETTING_SETTLE_MS:
                    # a setting changed a moment ago: the rest of a multi-command update may still withdraw this OFF
                    wait = RADAR_SETTING_SETTLE_MS - (now - self.last_change)      # >= 1: no spinning
                else:
                    g.park_ack = False                             # clear the ack first ...
                    g.park_request = True                          # ... then raise the request
                    self.park_start_ms = now & U32
                    self.park_timeout_flag = False
                    g.phase = PARKING
                    self.serial.append((now, "[RADAR] park requested"))
                    self.io.kick_ld2454()
                    wait = PARK_POLL_MS
        elif g.phase == PARKING:
            if not wants_off:                                      # the OFF was withdrawn: back to ON, same epoch
                g.park_request = False
                g.park_ack = False
                self.park_timeout_flag = False
                g.phase = ON
                self.serial.append((now, "[RADAR] park withdrawn"))
            elif g.park_ack:                                       # parked: now, and only now, GPIO9 goes LOW
                self.park_timeout_flag = False
                self.io.gpio_set_level(0)
                self.off_at_ms = now & U32
                g.phase = OFF_SETTLING
                wait = RADAR_OFF_SETTLE_MS
            elif elapsed(now, self.park_start_ms) >= PARK_ACK_TIMEOUT_MS:
                if not self.park_timeout_flag:
                    self.park_timeout_flag = True
                    self.park_timeouts += 1
                wait = RADAR_TICK_MS                               # stays ON, keeps polling once per second
            else:
                wait = PARK_POLL_MS
        elif g.phase == OFF_SETTLING:
            if elapsed(now, self.off_at_ms) >= RADAR_OFF_SETTLE_MS:
                g.radar_powered = False
                self.record_power_down(now)
                self.off_since_ms = now & U32
                g.phase = OFF
            else:
                wait = max(1, RADAR_OFF_SETTLE_MS - elapsed(now, self.off_at_ms))
        elif g.phase == OFF:
            if not dec.off and elapsed(now, self.off_since_ms) >= RADAR_MIN_OFF_MS:    # a fault cycle ends here too
                self.last_on_state = dec.state                     # the ON state that is published from here on
                self.io.gpio_set_level(1)
                self.on_at_ms = now & U32
                g.phase = POWERING
                wait = RADAR_ON_SETTLE_MS
        elif g.phase == POWERING:
            if elapsed(now, self.on_at_ms) >= RADAR_ON_SETTLE_MS:
                g.park_request = False
                g.park_ack = False
                g.power_epoch += 1
                g.powered_at_ms = now & U32
                g.radar_powered = True                             # last of the three: a reader never pairs 'powered' with the old stamp
                self.record_power_up(now)
                self.cycle_active = False
                self.park_timeout_flag = False
                g.phase = ON
            else:
                wait = max(1, RADAR_ON_SETTLE_MS - elapsed(now, self.on_at_ms))
        return wait

    def next_wait(self, dec, now):
        """C++: src/radar_power.cpp: RadarPower::nextWait()"""
        g = self.g
        wants_off = dec.off or self.cycle_active
        w = RADAR_TICK_MS
        if g.phase == ON:
            if wants_off and elapsed(now, g.powered_at_ms) < RADAR_MIN_ON_MS:
                w = min(w, max(1, RADAR_MIN_ON_MS - elapsed(now, g.powered_at_ms)))
            if elapsed(now, g.powered_at_ms) < LD2412_STATIC_WARMUP_MS:
                w = min(w, max(1, LD2412_STATIC_WARMUP_MS - elapsed(now, g.powered_at_ms)))
        elif g.phase == OFF and not dec.off and elapsed(now, self.off_since_ms) < RADAR_MIN_OFF_MS:
            w = min(w, max(1, RADAR_MIN_OFF_MS - elapsed(now, self.off_since_ms)))
        return w

    # ---- the published status (design 5.6 b: only the five states, reason pending while a change waits) ------------
    def next_boundary_min(self, valid, now_min):
        s = self.s
        if not (s.en and s.sched and valid) or s.ns == s.ne:
            return -1
        return s.ne if in_window(now_min, s.ns, s.ne) else s.ns

    @staticmethod
    def local_after(now_min, now_sec, delta_ms):
        """C++: src/radar_power.cpp: localAfter(). Minute of day (HH:MM) of the moment `delta_ms` from now: whole seconds, then floor to the minute."""
        return ((now_min * 60 + now_sec + (delta_ms + 999) // 1000) // 60) % 1440

    def update_status(self, dec, valid, now_min, now_sec, now):
        """C++: src/radar_power.cpp: RadarPower::updateStatus()"""
        g, s = self.g, self.s
        wants_off = dec.off or self.cycle_active
        st, rs, nxt = dec.state, dec.reason, self.next_boundary_min(valid, now_min)
        if g.phase in (ON, PARKING, POWERING):                     # the radars are (about to be) powered
            if g.phase == PARKING and self.park_timeout_flag:
                st, rs = ST_FAILSAFE_ON, RS_FAULT
                nxt = now_min if valid else -1
            elif self.thr:
                st, rs = ST_FAILSAFE_ON, RS_THROTTLE
                old = self.oldest_powerup_in_hour(now)
                nxt = self.local_after(now_min, now_sec, HOUR_MS - (now - old)) if (valid and old is not None) else -1
            elif wants_off:
                st, rs = self.last_on_state, RS_PENDING            # keeps the previous ON state
                if g.phase == PARKING:
                    nxt = now_min if valid else -1
                elif valid:
                    nxt = self.local_after(now_min, now_sec, max(0, RADAR_MIN_ON_MS - elapsed(now, g.powered_at_ms)))
                else:
                    nxt = -1
            else:
                self.last_on_state = st
        else:                                                      # OFF_SETTLING, OFF: GPIO9 is LOW
            if self.cycle_active and not dec.off:                  # a fault power-cycle: the decision wants ON anyway
                rs = RS_FAULT
                nxt = self.local_after(now_min, now_sec, max(0, RADAR_MIN_OFF_MS - elapsed(now, self.off_since_ms))) \
                    if (valid and g.phase == OFF) else -1
            elif dec.off:
                self.last_off_state = st
            else:                                                  # an ON waits for the 10 s minimum OFF time
                st, rs = self.last_off_state, RS_PENDING
                nxt = self.local_after(now_min, now_sec, max(0, RADAR_MIN_OFF_MS - elapsed(now, self.off_since_ms))) \
                    if (valid and g.phase == OFF) else -1
        new = self.cur.copy()
        new.en, new.sched, new.ns, new.ne, new.offl, new.legacy = s.en, s.sched, s.ns, s.ne, s.offl, s.legacy
        new.phase = g.phase
        new.powered = g.radar_powered
        warm = self.warm_done(now)
        new.obs412 = warm
        new.observing = warm and g.ld2454_ready
        new.state, new.reason, new.next_min, new.thr = st, rs, nxt, 1 if self.thr else 0
        new.fault = FAULT_PARK_TIMEOUT if self.park_timeout_flag else g.ld2454_fault
        new.park_timeouts = self.park_timeouts
        new.ha = g.ha_state
        new.ups24 = sum(1 for up, _ in self.hist if now - up <= DAY_MS)
        new.on24x10 = self.on_share_x10(now)
        if (new.state, new.powered) != (self.cur.state, self.cur.powered):
            new.since_ms = now
        if new.key() != self.cur.key():
            g.state_dirty = True
            self.dirty_edges += 1
        self.cur = new

    def snapshot(self):
        """C++: src/radar_power.cpp: radarStatusSnapshot()"""
        return self.cur.copy()

    @staticmethod
    def attr_json(st, boot_id, seq):
        """C++: src/mqtt_manager.cpp: MqttManager::publishRadarStates() (the snprintf of the radar/state/attr JSON). The status JSON as MqttTask builds it with snprintf (RADAR_ATTR_JSON_MAX = 256)."""
        nxt = format_hm(st.next_min) if st.next_min >= 0 else ""
        return ('{"sw":%d,"since":%d,"next":"%s","reason":"%s","ha":"%s","ups24":%d,"on24":%d.%d,"thr":%d,'
                '"fault":"%s","b":"%s","seq":%d}') % (
            1 if st.powered else 0, (st.since_ms & U32) // 1000, nxt, REASON_NAMES[st.reason], HA_NAMES[st.ha],
            st.ups24, st.on24x10 // 10, st.on24x10 % 10, st.thr, FAULT_NAMES[st.fault], boot_id, seq)



# =================================================================== the world ==============================================
FAILS = []
PASSES = [0]


def check(cond, text):
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(text)
        print("  FAIL:", text)


def mm(h, m=0):
    return h * 60 + m


S = 1000


class LD2454Sim:
    """LD2454Task: 50 ms loop, one UART rule (open only in phase ON with no park request), the level-based park
    ack, a radar that boots after power-up, first-frame configuration, 5 warm-up frames, the 8 s no-frames fault."""

    def __init__(self, w, radar_boot_ms=2500, config_ms=300, silent=False, stuck=False):
        self.w = w
        self.radar_boot_ms, self.config_ms, self.silent, self.stuck = radar_boot_ms, config_ms, silent, stuck
        self.link_open = False
        self.parked = False
        self.busy_until = 0
        self.await_first = False
        self.first_seen = False
        self.opened_at = 0
        self.multi = False
        self.frames_since = 0
        self.frame_t = None
        self.no_frames_fault = False
        self.opens = []          # (t, phase, park_request)  every Serial1.begin
        self.parks = []          # t of every park
        self.retry_on_frame = False

    def radar_frames_from(self):
        return None if self.silent else self.w.radar_on_since + self.radar_boot_ms

    def tick(self, t):
        g = self.w.g
        if t < self.busy_until:
            return
        if self.stuck:
            return
        if g.park_request:
            if not self.parked:
                self.link_open = False
                self.parked = True
                self.multi = False
                self.await_first = False
                self.first_seen = False
                self.no_frames_fault = False
                self.frames_since = 0
                g.ld2454_ready = False
                g.ld2454_fault = FAULT_NONE
                self.parks.append(t)
            if not g.park_ack:
                g.park_ack = True                 # level-based ack, set only after the pins are parked
                self.w.kick()
        elif not self.link_open and g.phase == ON:
            self.link_open = True
            self.parked = False
            self.opens.append((t, g.phase, g.park_request))
            self.opened_at = t
            self.await_first = True
            self.first_seen = False
            self.multi = False
            self.frames_since = 0
            self.no_frames_fault = False
            g.ld2454_fault = FAULT_NONE
            g.ld2454_ready = False
            ff = self.radar_frames_from()
            self.frame_t = None if ff is None else max(ff, t) + 100
        if self.link_open:
            self.service(t)

    def service(self, t):
        g = self.w.g
        # frames
        while self.frame_t is not None and self.frame_t <= t and self.w.radar_powered_now(self.frame_t):
            ft = self.frame_t
            self.frame_t += 100
            if self.no_frames_fault:
                self.no_frames_fault = False
                g.ld2454_fault = FAULT_NONE
                self.w.kick()
            if self.await_first:
                self.first_seen = True
            elif self.multi:
                self.frames_since += 1
                if self.frames_since >= 5 and not g.ld2454_ready:
                    g.ld2454_ready = True
                    self.w.kick()
            elif self.retry_on_frame:
                self.retry_on_frame = False
                self.busy_until = t + self.config_ms
                self.w.at(self.busy_until, self.finish_config)
                return
        if self.await_first:
            if self.first_seen:
                self.await_first = False
                self.busy_until = t + self.config_ms
                self.config_end_at = self.busy_until
                self.multi_after = True
                self.w.at(self.busy_until, self.finish_config)
            elif t - self.opened_at >= 8000:
                self.await_first = False
                self.no_frames_fault = True
                g.ld2454_fault = FAULT_LD2454_NO_FRAMES
                self.w.kick()
                self.busy_until = t + 3050           # a silent radar: ENABLE_CONFIG and END_CONFIG time out
                self.retry_on_frame = True
                self.w.at(self.busy_until, self.finish_config_failed)

    def finish_config(self, t):
        self.multi = True
        self.frames_since = 0
        self.w.g.ld2454_ready = False

    def finish_config_failed(self, t):
        self.multi = False
        self.frames_since = 0


class World:
    def __init__(self, nvs=None, boot_ms=1000, tod_min=mm(12), time_valid_at=0, dst=(), ld=None,
                 publish_ms=0, mqtt_connected=True, rp_start_delay=20):
        self.now = boot_ms
        self.q = []
        self.n = 0
        self.nvs = nvs if nvs is not None else Nvs()
        self.g = Shared()
        self.gpio = []                      # (t, level)
        self.gpio_pre_output = []           # gpio_set_level calls made before pin_mode_output
        self.output_enabled = False
        self.tod_min, self.tod_t0 = tod_min, boot_ms
        self.time_valid_at = boot_ms + time_valid_at
        self.dst = list(dst)                # (t_ms, delta_minutes)
        self.ota = []                       # [(from, to)]
        self.radar_on_since = boot_ms       # GPIO9 HIGH since (the radars were on through the reset)
        self.gpio_level = 1 if self.nvs.get("en") else 0
        self.rp = RadarPower(self, self.g, self.nvs, boot_ms)
        self.ld = LD2454Sim(self, **(ld or {}))
        self.rp_token = 0
        self.rp_due = None
        self.log = []
        self.passes = []                    # (t, wait) of every RadarPower pass
        self.published = []                 # (t_done, snapshot)
        self.retained = None
        self.mq_connected = mqtt_connected
        self.publish_ms = publish_ms
        self.safety_violations = []
        self.sched_rp(boot_ms + rp_start_delay)
        self.at(boot_ms + 30, self.ld_tick)
        self.at(boot_ms + 40, self.mq_tick)

    # ---- environment used by RadarPower (the io object) ---------------------------------------------------------------
    def gpio_set_level(self, level):
        if not self.output_enabled:
            self.gpio_pre_output.append((self.now, level))
        else:
            if level == 0 and self.g.park_ack is False and not (self.g.park_request and self.g.park_ack):
                self.safety_violations.append((self.now, "GPIO9 LOW without a park ack"))
            if level == 0 and self.ld.link_open:
                self.safety_violations.append((self.now, "GPIO9 LOW while Serial1 is open"))
            if level == 1 and self.gpio_level == 0:
                self.radar_on_since = self.now
        self.gpio_level = level
        self.gpio.append((self.now, level))

    def pin_mode_output(self):
        self.output_enabled = True

    def kick(self):
        self.sched_rp(self.now)

    def kick_ld2454(self):
        pass                                # LD2454Task polls every 50 ms

    def clock(self):
        valid = self.now >= self.time_valid_at
        minute = (self.tod_min + (self.now - self.tod_t0) // 60000
                  + sum(d for t, d in self.dst if t <= self.now)) % 1440
        return valid, minute, ((self.now - self.tod_t0) // 1000) % 60

    def radar_powered_now(self, t):
        return True

    # ---- scheduler ------------------------------------------------------------------------------------------------------
    def at(self, t, fn):
        self.n += 1
        heapq.heappush(self.q, (t, self.n, "fn", fn, 0))

    def sched_rp(self, t):
        if self.rp_due is not None and self.rp_due <= t and self.rp_due >= self.now:
            return
        self.rp_token += 1
        self.rp_due = t
        self.n += 1
        heapq.heappush(self.q, (t, self.n, "rp", None, self.rp_token))

    def ota_active(self, t):
        for a, b in self.ota:
            if a <= t < b:
                return b
        return None

    def ld_tick(self, t):
        self.ld.tick(t)
        self.at(t + 50, self.ld_tick)

    def mq_tick(self, t):
        if self.mq_connected and self.g.state_dirty:
            self.g.state_dirty = False
            snap = self.rp.snapshot()
            self.published.append((t + self.publish_ms, snap))
            self.at(t + self.publish_ms, lambda tt, s=snap: setattr(self, "retained", s))
        self.at(t + 40, self.mq_tick)

    def run_until(self, t_end):
        while self.q and self.q[0][0] <= t_end:
            t, _, kind, fn, token = heapq.heappop(self.q)
            self.now = t
            if kind == "rp":
                if token != self.rp_token:
                    continue
                self.rp_due = None
                end = self.ota_active(t)
                if end is not None:                 # waitWhileOtaPaused(): nothing runs, GPIO9 does not move
                    self.sched_rp_after_ota(end)
                    continue
                wait = self.rp.pass_(t)
                self.passes.append((t, wait))
                self.sched_rp(t + wait)
            else:
                end = self.ota_active(t)
                if end is not None and fn in (self.ld_tick, self.mq_tick):
                    self.at(end, fn)                # LD2454Task and MqttTask wait as well
                    continue
                fn(t)
        self.now = t_end

    def sched_rp_after_ota(self, end):
        def resume(tt):
            self.rp.after_ota_pause(tt)
            self.sched_rp(tt)
        self.at(end, resume)

    # ---- helpers for the scenarios --------------------------------------------------------------------------------------
    def cmd(self, t, cmd, value):
        self.at(t, lambda tt: self.rp.request(cmd, value))

    def ha(self, t, state):
        self.at(t, lambda tt: self.rp.set_ha_state(state, tt))

    def gpio_edges(self, lo=0, hi=1 << 62):
        return [(t, lv) for t, lv in self.gpio if lo <= t < hi]

    def states(self):
        return [(t, STATE_NAMES[s.state], REASON_NAMES[s.reason]) for t, s in self.published]


def settle(w, t_ms):
    w.run_until(t_ms)


# =================================================================== scenarios ============================================
def test_window_and_parsers():
    print("window maths and parsers")
    check(in_window(0, 0, 360) and in_window(359, 0, 360) and not in_window(360, 0, 360), "default 00:00-06:00 edges")
    check(not in_window(1439, 0, 360) and not in_window(720, 0, 360), "default window outside")
    check(in_window(mm(23), mm(22), mm(6)) and in_window(mm(5, 59), mm(22), mm(6)) and not in_window(mm(6), mm(22), mm(6))
          and in_window(mm(22), mm(22), mm(6)) and not in_window(mm(21, 59), mm(22), mm(6)), "crossing midnight")
    check(not any(in_window(t, mm(5), mm(5)) for t in range(1440)), "start == end never")
    check(parse_on_off("1") == 1 and parse_on_off("0") == 0 and parse_on_off("ON") == 1 and parse_on_off("off") == 0
          and parse_on_off("On") == 1, "on/off payloads")
    check(parse_on_off("auto") == -1 and parse_on_off("2") == -1 and parse_on_off("") == -1 and parse_on_off("11") == -1
          and parse_on_off("true") == -1 and parse_on_off(" 1") == -1, "refused payloads")
    check(parse_time_of_day("23:00:00") == mm(23) and parse_time_of_day("06:00") == 360 and parse_time_of_day("0:00") == 0
          and parse_time_of_day("7:05:59") == mm(7, 5), "time payloads accepted")
    check(all(parse_time_of_day(x) is None for x in ("24:00:00", "12:60", "12:00:60", "", "None", "12", "12:0", "12:00:0",
                                                      "-1:00", "12:00:00:00", "12:00 ", "1\uff12:00", "unknown")),
          "time payloads refused")
    check(format_hms(0) == "00:00:00" and format_hms(360) == "06:00:00" and format_hms(1439) == "23:59:00",
          "HH:MM:SS state payloads")
    check(format_hm(360) == "06:00" and format_hm(0) == "00:00", "HH:MM next")


def test_defaults_and_boot():
    print("defaults and boot (en = 1)")
    w = World()
    s = w.nvs.load()
    check((s.en, s.sched, s.ns, s.ne, s.offl, s.legacy) == (1, 0, 0, 360, 0, 0), "fresh NVS defaults")
    check(w.nvs.writes == [], "no NVS write at boot")
    check(w.gpio_pre_output == [(1000, 1)] and w.output_enabled, "early init latches GPIO9 HIGH before pinMode(OUTPUT)")
    check(w.g.phase == POWERING and not w.g.radar_powered and w.g.power_epoch == 0, "boot phase POWERING")
    w.run_until(1000 + 100)
    check(w.g.phase == ON and w.g.radar_powered and w.g.power_epoch == 1, "ON after the 5 ms settle, epoch 1")
    snap = w.rp.snapshot()
    check(STATE_NAMES[snap.state] == "forced_on" and snap.sm == "F", "schedule OFF on a fresh flash: forced_on")
    check(w.ld.opens and w.ld.opens[0][1] == ON and not w.ld.opens[0][2], "Serial1 opened in phase ON only")
    w.run_until(1000 + 12000 + 500)
    snap = w.rp.snapshot()
    check(snap.observing and snap.pw == 3, "observing after the warm-up, pw 3")
    # observing is never earlier than 12 s after power-up
    first_obs = next((t for t, s in w.published if s.observing), None)
    check(first_obs is not None and first_obs - w.g.powered_at_ms >= LD2412_STATIC_WARMUP_MS,
          "observing no earlier than 12 s after power-up (%s)" % (None if first_obs is None else first_obs - w.g.powered_at_ms))
    check(w.safety_violations == [], "no safety violation: %s" % w.safety_violations)


def run_days(sched=1, days=3, start_tod=mm(22), ha_online=True, **kw):
    nvs = Nvs()
    nvs.data["sched"] = sched
    w = World(nvs=nvs, tod_min=start_tod, **kw)
    if ha_online:
        w.ha(1100, HA_ONLINE)
    w.run_until(1000 + days * DAY_MS)
    return w


def test_schedule_days():
    print("three days with the default window")
    w = run_days(days=3, start_tod=mm(22))
    edges = [(t, lv) for t, lv in w.gpio if t > 2000]
    lows = [t for t, lv in edges if lv == 0]
    highs = [t for t, lv in edges if lv == 1]
    check(len(lows) == 3 and len(highs) >= 2, "one OFF per night (lows %d highs %d)" % (len(lows), len(highs)))
    # 22:00 at boot + 2 h = 00:00
    first_low = lows[0] - 1000
    check(7200 * S <= first_low <= 7200 * S + 1300, "first OFF at 00:00:00 (+%d ms)" % (first_low - 7200 * S))
    first_high = highs[0] - 1000
    check(7200 * S + 6 * 3600 * S <= first_high <= 7200 * S + 6 * 3600 * S + 1300,
          "first ON at 06:00:00 (+%d ms)" % (first_high - (7200 * S + 6 * 3600 * S)))
    seq = []
    for t, s in w.published:
        n = STATE_NAMES[s.state]
        if not seq or seq[-1] != n:
            seq.append(n)
    check(seq[:5] == ["forced_on", "on", "night_off", "on", "night_off"] or seq[:4] == ["on", "night_off", "on", "night_off"]
          or seq[:5] == ["failsafe_on", "on", "night_off", "on", "night_off"],
          "state sequence %s" % seq[:6])
    check(all(s.reason != RS_OTA for _, s in w.published), "reason ota is never emitted")
    check(w.safety_violations == [], "safety %s" % w.safety_violations)
    ups = w.rp.snapshot().ups24
    check(ups in (1, 2), "ups24 after 3 days %d" % ups)
    on24 = w.rp.snapshot().on24x10
    check(745 <= on24 <= 755, "on24 about 75 %% (%d / 10)" % on24)
    # the physical order GPIO9 LOW -> radar_powered false 50 ms later
    pw_false = [t for t, s in w.published if not s.powered and s.phase == OFF]
    check(pw_false and all(any(0 <= tp - tl <= 400 for tl in lows) for tp in pw_false[:3]),
          "'Radar power' OFF follows GPIO9 LOW (settle 50 ms)")


def test_settle_and_uart_rules():
    print("settle times and the UART rule")
    w = run_days(days=1, start_tod=mm(23, 58))
    lows = [t for t, lv in w.gpio if lv == 0 and t > 2000]
    highs = [t for t, lv in w.gpio if lv == 1 and t > 2000]
    # powered false only >= 50 ms after GPIO9 low
    off_states = [(t, s) for t, s in w.published if not s.powered]
    check(off_states and off_states[0][0] - lows[0] >= RADAR_OFF_SETTLE_MS - 41,
          "'Radar power' OFF published %d ms after GPIO9 LOW" % (off_states[0][0] - lows[0]))
    # Serial1 re-opened only after GPIO9 HIGH + 5 ms
    reopen = [t for t, ph, pr in w.ld.opens if t > highs[0] - 1]
    check(reopen and reopen[0] - highs[0] >= RADAR_ON_SETTLE_MS, "Serial1 opens %d ms after GPIO9 HIGH" % (reopen[0] - highs[0]))
    check(all(ph == ON and not pr for _, ph, pr in w.ld.opens), "every Serial1.begin in phase ON with no request")
    # the radar mode is re-applied after each power-up: every open is followed by a configuration
    check(w.ld.multi, "multi-target confirmed again after the power-up")
    check(w.safety_violations == [], "safety %s" % w.safety_violations)


def test_ntp_invalid():
    print("no valid clock")
    nvs = Nvs(); nvs.data["sched"] = 1
    w = World(nvs=nvs, tod_min=mm(2), time_valid_at=120 * S)
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 110 * S)
    s = w.rp.snapshot()
    check(STATE_NAMES[s.state] == "failsafe_on" and REASON_NAMES[s.reason] == "failsafe" and w.g.radar_powered,
          "inside the window without NTP: failsafe_on, radars on")
    w.run_until(1000 + 121 * S)
    low = [t for t, lv in w.gpio if lv == 0]
    check(low and 119 * S <= low[0] - 1000 <= 123 * S, "OFF right after NTP (60 s minimum ON time long over): %s" % [(t - 1000) for t in low])
    # NTP valid at 30 s: the 60 s minimum ON time delays the OFF
    nvs_b = Nvs(); nvs_b.data["sched"] = 1
    w2 = World(nvs=nvs_b, tod_min=mm(2), time_valid_at=30 * S)
    w2.ha(1100, HA_ONLINE)
    w2.run_until(1000 + 100 * S)
    low = [t for t, lv in w2.gpio if lv == 0]
    check(low and 60 * S <= low[0] - 1000 - 25 <= 62 * S, "OFF deferred to boot + 60 s: %s" % [(t - 1000) for t in low])
    pend = [(t, s) for t, s in w2.published if s.reason == RS_PENDING]
    check(pend and STATE_NAMES[pend[0][1].state] == "failsafe_on", "state keeps failsafe_on with reason pending meanwhile")
    check(w.safety_violations == [] and w2.safety_violations == [], "safety")


def test_offline_grace():
    print("HA offline grace")
    # radars off at night; Wi-Fi lost at +10 min; grace 5 min counts from the loss
    nvs = Nvs(); nvs.data["sched"] = 1
    w = World(nvs=nvs, tod_min=mm(0, 10))
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 130 * S)
    check(not w.g.radar_powered and w.g.phase == OFF, "night_off reached")
    t_loss = w.now + 10 * 60 * S
    w.ha(t_loss, HA_UNKNOWN)
    # a reconnect after 100 s that gets no retained status: UNKNOWN again, the grace is not restarted
    w.ha(t_loss + 100 * S, HA_UNKNOWN)
    w.run_until(t_loss + 299 * S)
    check(not w.g.radar_powered, "still off 299 s after the loss")
    w.run_until(t_loss + 302 * S)
    highs = [t for t, lv in w.gpio if lv == 1 and t > t_loss]
    check(highs and 299 * S <= highs[0] - t_loss <= 301 * S + 100, "radars ON 5 min after the loss: +%s ms" % ((highs[0] - t_loss) if highs else None))
    s = w.rp.snapshot()
    check(STATE_NAMES[s.state] == "failsafe_on" and REASON_NAMES[s.reason] == "ha_unknown", "failsafe_on, reason ha_unknown")
    # a live online restores night_off (after the minimum ON time)
    w.ha(w.now + 5 * S, HA_ONLINE)
    w.run_until(w.now + 80 * S)
    check(STATE_NAMES[w.rp.snapshot().state] == "night_off", "ONLINE again: night_off")
    # 'Radar schedule without HA' ON bypasses it
    nvs2 = Nvs(); nvs2.data["sched"] = 1; nvs2.data["offl"] = 1
    w2 = World(nvs=nvs2, tod_min=mm(0, 10))
    w2.ha(1100, HA_ONLINE); w2.ha(130 * S, HA_UNKNOWN)
    w2.run_until(1000 + 3600 * S)
    check(not w2.g.radar_powered, "offl = 1: the radars stay off with HA down")
    # offline (retained) at boot inside the window: night_off once the minimum ON time is over, failsafe at boot + 5 min
    nvs3 = Nvs(); nvs3.data["sched"] = 1
    w3 = World(nvs=nvs3, tod_min=mm(1))
    w3.ha(1000 + 2 * S, HA_OFFLINE)
    w3.run_until(1000 + 400 * S)
    lows = [t - 1000 for t, lv in w3.gpio if lv == 0]
    highs = [t - 1000 for t, lv in w3.gpio if lv == 1 and t > 2000]
    check(lows and 60 * S <= lows[0] <= 62 * S, "after a reset with HA down: OFF at boot + 60 s %s" % lows)
    check(highs and 299 * S <= highs[0] <= 302 * S, "... then failsafe ON at boot + 5 min %s" % highs)
    check(w.safety_violations == [] and w3.safety_violations == [], "safety")
    # defensive fallback: retain off, no status ever arrives: UNKNOWN for 5 min -> failsafe_on / ha_unknown
    nvs4 = Nvs(); nvs4.data["sched"] = 1
    w4 = World(nvs=nvs4, tod_min=mm(1))
    w4.run_until(1000 + 400 * S)
    s = w4.rp.snapshot()
    check(STATE_NAMES[s.state] == "failsafe_on" and REASON_NAMES[s.reason] == "ha_unknown", "fallback: ha_unknown")
    w4.ha(w4.now + S, HA_ONLINE)
    w4.run_until(w4.now + 80 * S)
    check(STATE_NAMES[w4.rp.snapshot().state] == "night_off", "a live online after UNKNOWN restores night_off")
    # reaching 'online' within the grace keeps night_off for good (normal path)
    nvs5 = Nvs(); nvs5.data["sched"] = 1
    w5 = World(nvs=nvs5, tod_min=mm(1))
    w5.ha(1000 + 3 * S, HA_ONLINE)
    w5.run_until(1000 + 3000 * S)
    check(not w5.g.radar_powered and w5.g.power_epoch == 1, "normal path: night_off, no extra power-up")


def test_manual_off_persistence():
    print("manual OFF persists across a reboot")
    w = World(tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    w.cmd(1000 + 70 * S, CMD_ENABLE, 0)
    w.run_until(1000 + 80 * S)
    check(w.nvs.writes == [("en", 0)], "one NVS write for the change: %s" % w.nvs.writes)
    lows = [t for t, lv in w.gpio if lv == 0]
    check(lows and w.ld.parks and w.ld.parks[-1] <= lows[0], "TX parked before GPIO9 falls")
    s = w.rp.snapshot()
    check(STATE_NAMES[s.state] == "forced_off" and s.en == 0 and not s.powered and REASON_NAMES[s.reason] == "manual",
          "forced_off / manual / powered 0")
    # repeating 0 writes nothing but is echoed (dirty)
    w.g.state_dirty = False
    w.cmd(w.now + 1 * S, CMD_ENABLE, 0)
    w.run_until(w.now + 2 * S)
    check(w.nvs.writes == [("en", 0)] and (w.retained is not None) and len(w.published) >= 2, "a repeated 0 writes nothing")
    # reboot (software restart: GPIO9 stays LOW): early init with en = 0
    w2 = World(nvs=w.nvs, boot_ms=500, tod_min=mm(12))
    w2.gpio_level = 0
    check(w2.gpio_pre_output == [(500, 0)], "early init: GPIO9 latched LOW before the output is enabled")
    check(w2.g.phase == OFF_SETTLING and w2.g.park_request and w2.g.park_ack and w2.g.radar_powered, "boot phase OFF_SETTLING, request = ack = true")
    w2.run_until(500 + 60 * S)
    check(w2.ld.opens == [], "Serial1 is never opened while the manual OFF lasts")
    check(not w2.g.radar_powered and w2.g.phase == OFF, "OFF after the 50 ms settle")
    check(w2.gpio_edges(lo=501) == [], "GPIO9 not touched after boot")
    # 'Radars' ON 3 s after boot: deferred by the 10 s minimum OFF time, which started at boot
    w2.cmd(500 + 3 * S, CMD_ENABLE, 1)
    w2.run_until(500 + 80 * S)
    highs = [t - 500 for t, lv in w2.gpio_edges(lo=501) if lv == 1]
    check(highs and 10000 <= highs[0] <= 10200, "ON after the minimum OFF time: +%s" % highs)
    check(w2.ld.opens and w2.ld.opens[0][0] - 500 >= highs[0] + RADAR_ON_SETTLE_MS - 10, "Serial1 opens only in phase ON")
    check(w.safety_violations == [] and w2.safety_violations == [], "safety %s %s" % (w.safety_violations, w2.safety_violations))


def test_rapid_toggling():
    print("rapid toggling: deferral, last command wins")
    nvs = Nvs()
    w = World(nvs=nvs, tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    t0 = 1000 + 100 * S
    for i in range(10):
        w.cmd(t0 + i * S, CMD_ENABLE, 0 if i % 2 == 0 else 1)     # 0 1 0 1 0 1 0 1 0 1  (last is 1)
    w.run_until(t0 + 40 * S)
    edges = w.gpio_edges(lo=t0)
    check([lv for _, lv in edges] == [0, 1], "GPIO9 LOW once and HIGH once: %s" % [(t - t0, lv) for t, lv in edges])
    gap = edges[1][0] - edges[0][0]
    check(10000 <= gap <= 10600, "HIGH about 10 s after LOW: %d ms" % gap)
    check(w.rp.snapshot().en == 1, "stored setting ends at 1")
    pend = [s for t, s in w.published if s.reason == RS_PENDING and STATE_NAMES[s.state] == "forced_off"]
    check(pend, "while the ON waits: forced_off with reason pending")
    writes = [v for k, v in nvs.writes]
    check(writes == [0, 1, 0, 1, 0, 1, 0, 1, 0, 1][: len(writes)] and len(writes) <= 10, "NVS written only on change: %s" % nvs.writes)
    check(w.safety_violations == [], "safety")
    # a command inside the 60 s minimum ON time is deferred, not dropped
    w2 = World(nvs=Nvs(), tod_min=mm(12))
    w2.ha(1100, HA_ONLINE)
    w2.cmd(1000 + 10 * S, CMD_ENABLE, 0)
    w2.run_until(1000 + 59 * S)
    check(w2.g.radar_powered and w2.g.phase == ON, "OFF at +10 s waits for the 60 s minimum")
    pend = [s for t, s in w2.published if s.reason == RS_PENDING]
    check(pend and STATE_NAMES[pend[0].state] == "forced_on", "forced_on / pending meanwhile")
    w2.run_until(1000 + 63 * S)
    check(not w2.g.radar_powered, "applied after the minute")


def test_throttle():
    print("throttle: 12 power-ups per hour for automatic OFFs")
    nvs = Nvs(); nvs.data["sched"] = 1
    w = World(nvs=nvs, tod_min=mm(0, 30))
    w.ha(1100, HA_ONLINE)
    t = 1000
    # cycle the schedule switch: OFF for 62 s (radars on), ON for 12 s (radars off): one power-up per ~75 s
    ups = 1
    thr_seen = None
    for i in range(40):
        t += 75 * S if i else 70 * S
        w.cmd(t, CMD_SCHEDULE, 0)
        w.cmd(t + 62 * S, CMD_SCHEDULE, 1)
        w.run_until(t + 74 * S)
        snap = w.rp.snapshot()
        if snap.thr and thr_seen is None:
            thr_seen = (i, w.rp.powerups_last_hour(w.now), snap)
            break
    check(thr_seen is not None, "the throttle engaged")
    if thr_seen:
        i, n, snap = thr_seen
        check(n == RADAR_MAX_POWERUPS_PER_H, "12 power-ups in the last hour (%d)" % n)
        check(STATE_NAMES[snap.state] == "failsafe_on" and REASON_NAMES[snap.reason] == "throttle" and snap.thr == 1,
              "failsafe_on / throttle / thr 1")
        check(w.g.radar_powered, "radars stay on while throttled")
        a = format_hm(snap.next_min) if snap.next_min >= 0 else ""
        check(a != "", "next shows when the oldest power-up is an hour old: %s" % a)
        # a manual OFF is never throttled
        w.cmd(w.now + 1 * S, CMD_ENABLE, 0)
        w.run_until(w.now + 5 * S)
        check(not w.g.radar_powered and STATE_NAMES[w.rp.snapshot().state] == "forced_off", "manual OFF still applies")
        w.cmd(w.now + 1 * S, CMD_ENABLE, 1)                 # back to the schedule: throttled automatic OFF again
        w.run_until(w.now + 30 * S)
        s2 = w.rp.snapshot()
        check(STATE_NAMES[s2.state] in ("failsafe_on",) and s2.thr == 1 or not w.g.radar_powered,
              "after 'Radars' ON the schedule is throttled again (or already off) %s" % STATE_NAMES[s2.state])
    # the throttle ends when the oldest power-up is an hour old
    w.run_until(w.now + 3700 * S)
    check(not w.g.radar_powered and STATE_NAMES[w.rp.snapshot().state] == "night_off", "night_off once the hour has passed")
    check(w.safety_violations == [], "safety")


def test_states_domain():
    print("only five states, pending, and the frames sm code")
    nvs = Nvs(); nvs.data["sched"] = 1
    w = World(nvs=nvs, tod_min=mm(23, 59))
    w.ha(1100, HA_ONLINE)
    for k in range(6):
        w.cmd(1000 + 20 * S + k * 70 * S, CMD_ENABLE, k % 2)
        w.cmd(1000 + 50 * S + k * 70 * S, CMD_SCHEDULE, (k + 1) % 2)
    w.run_until(1000 + 500 * S)
    ok = all(0 <= s.state < 5 and s.sm in "ANFXS" for _, s in w.published)
    check(ok, "published states are among the five; sm codes valid")
    reasons = {REASON_NAMES[s.reason] for _, s in w.published}
    check("pending" in reasons, "pending was published: %s" % sorted(reasons))
    # the sm code equals the code of the published state
    check(all(s.sm == "ANFXS"[s.state] for _, s in w.published), "sm = code of the published state")
    # pw is 0 whenever the radars are not observing
    check(all((s.pw == 0) or (s.phase == ON and s.powered) for _, s in w.published), "pw 0 outside phase ON")
    check(w.safety_violations == [], "safety")


def test_handover():
    print("state hand-over: a change during a publish goes out in the next loop")
    nvs = Nvs(); nvs.data["sched"] = 1
    w = World(nvs=nvs, tod_min=mm(0, 0), publish_ms=30)
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 90 * S)
    check(w.retained is not None and not w.retained.powered and w.retained.phase == OFF,
          "retained 'Radar power' ends OFF (phase %s)" % (None if w.retained is None else PHASE_NAMES[w.retained.phase]))
    # fewer publishes than phase changes may happen (coalescing), but the last one is the latest state
    check(w.g.state_dirty is False, "dirty flag settled")
    # a failed publish keeps the flag set: model the disconnected case
    w2 = World(nvs=Nvs(), tod_min=mm(12), mqtt_connected=False)
    w2.run_until(1000 + 20 * S)
    check(w2.g.state_dirty and w2.published == [], "while disconnected the flag stays set")


def test_park_cases():
    print("park handshake: mid-config, timeout, withdrawn")
    # (a) LD2454Task is inside enableMultiTargetTracking() when the request comes: up to ~4.6 s
    nvs = Nvs()
    w = World(nvs=nvs, tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 70 * S)
    w.ld.busy_until = w.now + 4600
    t_cmd = w.now + 10
    w.cmd(t_cmd, CMD_ENABLE, 0)
    w.run_until(t_cmd + 5500)
    lows = [t for t, lv in w.gpio if lv == 0 and t >= t_cmd]
    check(lows and 4600 <= lows[0] - t_cmd <= 4900, "GPIO9 LOW after the park, +%s ms" % ([t - t_cmd for t in lows]))
    check(w.rp.park_timeouts == 0, "no park_timeout below 6 s")
    snap_pending = [s for t, s in w.published if s.phase == PARKING]
    check(snap_pending and STATE_NAMES[snap_pending[0].state] == "forced_on" and REASON_NAMES[snap_pending[0].reason] == "pending",
          "while parking: the ON state with reason pending")
    check(snap_pending and snap_pending[0].pw == 0, "observing false from the park request")
    check(w.safety_violations == [], "safety %s" % w.safety_violations)
    # (b) no ack: the radars stay ON, fault park_timeout after 6 s, OFF as soon as the ack comes
    w2 = World(nvs=Nvs(), tod_min=mm(12), ld=dict(stuck=True))
    w2.ha(1100, HA_ONLINE)
    w2.run_until(1000 + 70 * S)
    t_cmd = w2.now + 10
    w2.cmd(t_cmd, CMD_ENABLE, 0)
    w2.run_until(t_cmd + 7000)
    check(w2.g.radar_powered and w2.g.phase == PARKING, "no ack: still ON")
    s = w2.rp.snapshot()
    check(STATE_NAMES[s.state] == "failsafe_on" and REASON_NAMES[s.reason] == "fault" and FAULT_NAMES[s.fault] == "park_timeout"
          and w2.rp.park_timeouts == 1, "failsafe_on / fault / park_timeout, counter 1")
    check([lv for _, lv in w2.gpio if _ >= t_cmd] == [], "GPIO9 untouched")
    w2.run_until(t_cmd + 20000)
    check(w2.rp.park_timeouts == 1, "counted once")
    w2.ld.stuck = False
    w2.run_until(t_cmd + 22000)
    lows = [t for t, lv in w2.gpio if lv == 0 and t >= t_cmd]
    check(lows and lows[0] - (t_cmd + 20000) <= 1100, "OFF within 1 s of the late ack (+%s)" % [t - t_cmd - 20000 for t in lows])
    check(w2.safety_violations == [], "safety %s" % w2.safety_violations)
    # (c) withdrawn mid-park: LD2454Task parks late and acks after the request is gone; the UART is re-opened,
    #     the radars stay ON and the next park is not fooled by a stale ack
    w3 = World(nvs=Nvs(), tod_min=mm(12))
    w3.ha(1100, HA_ONLINE)
    w3.run_until(1000 + 70 * S)
    w3.ld.busy_until = w3.now + 3000
    t_cmd = w3.now + 10
    w3.cmd(t_cmd, CMD_ENABLE, 0)
    w3.cmd(t_cmd + 1500, CMD_ENABLE, 1)
    w3.run_until(t_cmd + 8000)
    check(w3.g.radar_powered and w3.g.phase == ON, "withdrawn: back to ON")
    check(w3.gpio_edges(lo=t_cmd) == [], "GPIO9 never moved")
    check(w3.ld.link_open and len(w3.ld.opens) == 1 and not w3.g.park_request, "request gone, no park happened (busy task never saw it)")
    # (d) the request lands, LD2454Task parks, then the request is withdrawn -> reopen under the one rule
    w4 = World(nvs=Nvs(), tod_min=mm(12))
    w4.ha(1100, HA_ONLINE)
    w4.run_until(1000 + 70 * S)
    t_cmd = w4.now + 10
    w4.cmd(t_cmd, CMD_ENABLE, 0)
    w4.run_until(t_cmd + 60)             # parked, ack seen, GPIO9 may or may not be low yet
    low_already = w4.g.phase in (OFF_SETTLING, OFF)
    if not low_already:
        w4.cmd(w4.now + 1, CMD_ENABLE, 1)
        w4.run_until(w4.now + 3000)
        check(w4.g.phase == ON and w4.ld.link_open, "withdrawn after the park: Serial1 re-opened, radars ON")
        check(all(ph == ON and not pr for _, ph, pr in w4.ld.opens), "re-open only in phase ON, request clear")
        t2 = w4.now + 70 * S
        w4.run_until(t2)
        w4.cmd(w4.now + 10, CMD_ENABLE, 0)
        w4.run_until(w4.now + 3000)
        check(not w4.g.radar_powered, "a later park works (no stale ack): GPIO9 low only after the new ack")
    check(w4.safety_violations == [], "safety %s" % w4.safety_violations)


def test_ota():
    print("OTA pause")
    nvs = Nvs(); nvs.data["sched"] = 1
    w = World(nvs=nvs, tod_min=mm(23, 59))
    w.ha(1100, HA_ONLINE)
    # upload from 23:59:30 to 00:02:00 (boot is 23:59:00): the window starts at 00:00 inside the upload
    w.ota.append((1000 + 30 * S, 1000 + 180 * S))
    w.run_until(1000 + 179 * S)
    check(w.gpio_edges(lo=1000 + 30 * S) == [], "GPIO9 does not change during the upload")
    w.run_until(1000 + 190 * S)
    lows = [t - 1000 for t, lv in w.gpio if lv == 0]
    check(lows and 180 * S <= lows[0] <= 183 * S, "the window start inside the upload is applied afterwards: %s" % lows)
    # a park pending when the upload starts: no park_timeout afterwards
    nvs2 = Nvs()
    w2 = World(nvs=nvs2, tod_min=mm(12), ld=dict(stuck=True))
    w2.ha(1100, HA_ONLINE)
    w2.run_until(1000 + 70 * S)
    t_cmd = w2.now + 10
    w2.cmd(t_cmd, CMD_ENABLE, 0)
    w2.ota.append((t_cmd + 100, t_cmd + 100 + 30 * S))
    w2.run_until(t_cmd + 100 + 30 * S + 500)
    check(w2.rp.park_timeouts == 0 and w2.g.phase == PARKING, "no park_timeout right after the upload (counter %d)" % w2.rp.park_timeouts)
    w2.run_until(t_cmd + 100 + 30 * S + 7000)
    check(w2.rp.park_timeouts == 1, "the 6 s count restarted after the upload")


def test_fault_cycle():
    print("ld2454_no_frames: a power-cycle at most once per hour")
    nvs = Nvs()
    w = World(nvs=nvs, tod_min=mm(12), ld=dict(silent=True))
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 30 * S)
    s = w.rp.snapshot()
    check(FAULT_NAMES[s.fault] == "ld2454_no_frames" and STATE_NAMES[s.state] == "forced_on", "fault attribute, state unchanged")
    w.run_until(1000 + 100 * S)
    lows = [t - 1000 for t, lv in w.gpio if lv == 0]
    highs = [t - 1000 for t, lv in w.gpio if lv == 1 and t > 2000]
    check(lows and 60 * S <= lows[0] <= 66 * S, "cycle starts after the minimum ON time: %s" % lows)
    check(highs and highs[0] - lows[0] >= 10000, "OFF for at least 10 s: %s" % highs)
    w.run_until(1000 + 3 * 3600 * S)
    lows = [t - 1000 for t, lv in w.gpio if lv == 0]
    check(1 <= len(lows) <= 3, "at most one cycle per hour: %d in 3 h" % len(lows))
    check(all(b - a >= 3600 * S for a, b in zip(lows, lows[1:])), "cycles at least an hour apart %s" % lows)
    check(w.safety_violations == [], "safety %s" % w.safety_violations)
    # a radar that answers again after the fault clears it
    w2 = World(nvs=Nvs(), tod_min=mm(12), ld=dict(silent=False, radar_boot_ms=12000))
    w2.ha(1100, HA_ONLINE)
    w2.run_until(1000 + 9 * S)
    check(FAULT_NAMES[w2.rp.snapshot().fault] == "ld2454_no_frames", "slow radar: fault after 8 s")
    w2.run_until(1000 + 30 * S)
    check(w2.rp.snapshot().fault == FAULT_NONE and w2.rp.snapshot().observing, "fault cleared by the first frame; observing")


def test_dst_and_wrap():
    print("DST nights and the millis() wrap")
    nvs = Nvs(); nvs.data["sched"] = 1
    # spring forward at 02:00 -> 03:00: local minutes skip 120..179; window 00:00-06:00
    w = World(nvs=nvs, tod_min=mm(22), dst=[(1000 + 4 * 3600 * S, +60)])
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 12 * 3600 * S)
    lows = [t - 1000 for t, lv in w.gpio if lv == 0]
    highs = [t - 1000 for t, lv in w.gpio if lv == 1 and t > 2000]
    check(lows and highs, "edges present")
    if lows and highs:
        dur = highs[0] - lows[0]
        check(abs(dur - 5 * 3600 * S) < 2500, "spring night is 1 h shorter: OFF for %.2f h" % (dur / 3600000))
    nvs2 = Nvs(); nvs2.data["sched"] = 1
    w2 = World(nvs=nvs2, tod_min=mm(22), dst=[(1000 + 4 * 3600 * S, -60)])   # fall back: 03:00 -> 02:00
    w2.ha(1100, HA_ONLINE)
    w2.run_until(1000 + 12 * 3600 * S)
    lows = [t - 1000 for t, lv in w2.gpio if lv == 0]
    highs = [t - 1000 for t, lv in w2.gpio if lv == 1 and t > 2000]
    if lows and highs:
        dur = highs[0] - lows[0]
        check(abs(dur - 7 * 3600 * S) < 2500, "fall night is 1 h longer: OFF for %.2f h" % (dur / 3600000))
    # the 49.7-day wrap: boot 100 s before the uint32 millis() wraps; run a manual OFF/ON across it
    nvs3 = Nvs()
    boot = (1 << 32) - 100 * S
    w3 = World(nvs=nvs3, boot_ms=boot, tod_min=mm(12))
    w3.ha(boot + 100, HA_ONLINE)
    w3.cmd(boot + 70 * S, CMD_ENABLE, 0)       # OFF before the wrap
    w3.cmd(boot + 85 * S, CMD_ENABLE, 1)       # ON deferred by the 10 s minimum: applies at about boot + 80 s
    w3.cmd(boot + 150 * S, CMD_ENABLE, 0)      # after the wrap, the 60 s minimum ON time counts across it
    w3.run_until(boot + 400 * S)
    edges = [(t - boot, lv) for t, lv in w3.gpio if t > boot + 1000]
    check([lv for _, lv in edges] == [0, 1, 0], "edges across the wrap %s" % edges)
    check(w3.safety_violations == [], "safety")
    # the 64-bit history is not confused by the wrap
    check(w3.rp.snapshot().ups24 == 2, "ups24 across the wrap: %d" % w3.rp.snapshot().ups24)


def test_attr_json():
    print("status JSON size")
    st = Status()
    st.powered, st.since_ms, st.next_min, st.reason, st.ha, st.ups24, st.on24x10, st.thr = 0, 81234000, 360, RS_MANUAL, HA_ONLINE, 1, 750, 0
    j = RadarPower.attr_json(st, "9f3c21aa", 1234565)
    exp = '{"sw":0,"since":81234,"next":"06:00","reason":"manual","ha":"online","ups24":1,"on24":75.0,"thr":0,"fault":"","b":"9f3c21aa","seq":1234565}'
    check(j == exp and len(j) == 139, "the design's example, 139 B (%d)" % len(j))
    worst = Status()
    worst.powered, worst.since_ms, worst.next_min, worst.reason, worst.ha = 1, 0xFFFFFFFF, 1439, RS_HA_UNKNOWN, HA_OFFLINE
    worst.ups24, worst.on24x10, worst.thr, worst.fault = RADAR_HISTORY_MAX, 1000, 1, FAULT_LD2454_NO_FRAMES
    jw = RadarPower.attr_json(worst, "ffffffff", 0xFFFFFFFF)
    check(len(jw) == 167 and len(jw) + 1 <= 256, "worst case %d B fits RADAR_ATTR_JSON_MAX 256" % len(jw))



# ---- the 49.7-day wrap: stamps that stay unchanged while their state lasts (src/radar_power.cpp keepOld()) -----------------
WRAP = 1 << 32


def fast_forward(w, t_end, step=HOUR_MS):
    """pass_() directly at `step` intervals, with the level-based park ack of LD2454Task. The real task passes every <= 1 s;
    the stamps only see their ages, and keepOld() acts at 2^30 ms, so hourly passes carry a run of 49.7 days."""
    t = w.now
    while t < t_end:
        t = min(t + step, t_end)
        w.now = t
        w.rp.pass_(t)
        if w.g.park_request and not w.g.park_ack:
            w.g.park_ack = True


def one_second_passes(w, t_end, each=None):
    t = w.now
    while t < t_end:
        t += S
        w.now = t
        w.rp.pass_(t)
        if w.g.park_request and not w.g.park_ack:
            w.g.park_ack = True
        if each:
            each(t)


def wrap_always_on():
    """The default configuration (schedule off): radars ON for 2^32 ms, g_poweredAtMs is still the boot-time power-up. After the
    wrap 'observing' must not drop for 12 s and presence() must not fall back to UNKNOWN."""
    w = World(boot_ms=1000, tod_min=mm(12))
    w.run_until(1000 + 120 * S)
    if not (w.rp.snapshot().observing and w.rp.snapshot().phase == ON):
        return ["not observing before the run"]
    t_wrap = w.g.powered_at_ms + WRAP
    fast_forward(w, t_wrap - 30 * S)
    w.g.ld2454_ready = True                                   # the LD2454 simulation does not run during the fast-forward
    bad = []

    def look(t):
        s = w.rp.snapshot()
        if not (s.observing and s.obs412 and s.phase == ON and w.rp.warm_done(t)):
            bad.append(t - t_wrap)
    one_second_passes(w, t_wrap + 60 * S, look)
    return bad


def wrap_ha_never_online():
    """No HA since the boot (a node used without HA), schedule on, inside the window at the wrap: failsafe_on all along. Without
    the refresh s_haLostMs wraps to 'just lost' and the decision flips to night_off for 5 minutes."""
    nvs = Nvs(); nvs.data["sched"] = 1
    t_wrap = 1000 + WRAP                                      # begin() stamps s_haLostMs with the boot time
    tod = (mm(3) - (WRAP + 30 * S) // 60000) % 1440           # local 03:00 at t_wrap + 30 s, inside 00:00-06:00
    w = World(nvs=nvs, boot_ms=1000, tod_min=tod)
    w.run_until(1000 + 120 * S)
    fast_forward(w, t_wrap - 60 * S)
    one_second_passes(w, t_wrap + 6 * 60 * S)
    st = w.rp.snapshot()
    n_low = len([1 for t, lv in w.gpio if lv == 0])
    if n_low == 0 and st.phase == ON and (STATE_NAMES[st.state], REASON_NAMES[st.reason]) == ("failsafe_on", "ha_unknown"):
        return []
    return ["%d LOW edges, state %s/%s" % (n_low, STATE_NAMES[st.state], REASON_NAMES[st.reason])]


def wrap_manual_off_then_on():
    """A manual OFF that lasts 2^32 ms, then 'Radars' ON 2 s after the stamp wrapped: the 10 s minimum OFF time is long over."""
    nvs = Nvs(); nvs.data["en"] = 0
    w = World(nvs=nvs, boot_ms=1000)
    w.run_until(1000 + 120 * S)
    if w.rp.snapshot().phase != OFF:
        return ["not OFF before the run"]
    t_wrap = w.rp.off_since_ms + WRAP
    fast_forward(w, t_wrap - S)
    one_second_passes(w, t_wrap + 2 * S)
    t_req = w.now
    w.rp.request(CMD_ENABLE, 1)
    one_second_passes(w, t_req + 3 * S)
    return [] if w.gpio and w.gpio[-1][1] == 1 and w.gpio[-1][0] > t_req else ["GPIO9 stayed LOW after the ON request"]


def test_stamp_wrap():
    print("the 49.7-day wrap of the long-lived 32-bit stamps (keepOld)")
    # the helper itself
    now = 5_000_000_000 & U32
    at_limit = (now - STAMP_MAX_AGE_MS) & U32
    check(keep_old(now, at_limit) == at_limit, "an age of exactly the limit is not moved")
    check(keep_old(now, (at_limit - 1) & U32) == at_limit, "an older stamp moves forward to the limit")
    check(keep_old(now, (now - 5000) & U32) == (now - 5000) & U32, "a young stamp stays")
    check(keep_old(now, (now + 2) & U32) == (now + 2) & U32, "a stamp 2 ms AHEAD of now (stored by another task) is not old")
    near = WRAP - 500                                         # 'now' just before the uint32 wrap
    check(keep_old(near, (near - STAMP_MAX_AGE_MS - 7) & U32) == (near - STAMP_MAX_AGE_MS) & U32, "the helper works across the wrap")
    check(STAMP_MAX_AGE_MS > 24 * 3600 * 1000 and STAMP_MAX_AGE_MS < 0x80000000, "the limit is far above every compared duration and below 2^31")
    # the three runs, with the refresh and without it (the model of the old code must fail: the test is sensitive)
    runs = (("radars ON around the clock", wrap_always_on), ("no HA ever, inside the window", wrap_ha_never_online),
            ("manual OFF, then ON", wrap_manual_off_then_on))
    for name, fn in runs:
        bad = fn()
        check(not bad, "after 49.7 days: %s (%s)" % (name, bad[:3]))
    RadarPower.restamp = False
    try:
        for name, fn in runs:
            bad = fn()
            check(bool(bad), "sensitivity: without the refresh the run '%s' fails" % name)
    finally:
        RadarPower.restamp = True


class Ld2412Reader:
    """C++: src/ld2412_sensor.cpp: presence() / updatePresence() for a LOW OUT pin, reading the atomics of radar_power.h."""

    @staticmethod
    def read(g, now):
        if g.park_request or not g.radar_powered:
            return "UNKNOWN"
        since = elapsed(now, g.powered_at_ms)
        if since < LD2412_OUT_IGNORE_MS or since < LD2412_STATIC_WARMUP_MS:
            return "UNKNOWN"
        return "ABSENT"


def powering_store_sequence(cpp_src):
    """The stores of the POWERING -> ON branch of stepPhase(), in source order."""
    code = strip_comments(cpp_src)
    i = code.index("case RADAR_PHASE_POWERING:")
    blk = code[i:code.index("default:", i)]
    return [re.sub(r"\s+", " ", m).strip() for m in re.findall(r"g_\w+(?:\.fetch_add\(1\)|\s*=\s*[^;]+);", blk)]


def run_store_prefixes(statements):
    """Apply the stores one at a time to the state of a radar that was OFF; return the presence() a reader would see after each."""
    g = Shared()
    g.phase, g.park_request, g.park_ack, g.radar_powered = POWERING, True, True, False
    g.powered_at_ms = 5000                                    # the previous power-up, ten minutes ago
    now = 5000 + 10 * 60 * S
    effect = {
        "g_parkRequest = false;": lambda: setattr(g, "park_request", False),
        "g_parkAck = false;": lambda: setattr(g, "park_ack", False),
        "g_powerEpoch.fetch_add(1);": lambda: setattr(g, "power_epoch", g.power_epoch + 1),
        "g_poweredAtMs = now;": lambda: setattr(g, "powered_at_ms", now),
        "g_radarPowered = true;": lambda: setattr(g, "radar_powered", True),
        "g_radarPhase = RADAR_PHASE_ON;": lambda: setattr(g, "phase", ON),
    }
    seen = [Ld2412Reader.read(g, now + 1)]
    for st in statements:
        if st not in effect:
            return None
        effect[st]()
        seen.append(Ld2412Reader.read(g, now + 1))
    return seen


def test_powering_store_order():
    print("POWERING -> ON: a reader between any two stores never sees 'powered' with the old timestamp")
    seq = powering_store_sequence(read_source("src/radar_power.cpp"))
    check(sorted(seq) == sorted(["g_parkRequest = false;", "g_parkAck = false;", "g_powerEpoch.fetch_add(1);", "g_poweredAtMs = now;",
                                 "g_radarPowered = true;", "g_radarPhase = RADAR_PHASE_ON;"]),
          "the six stores of the branch are the ones the model knows: %s" % seq)
    seen = run_store_prefixes(seq)
    check(seen is not None and all(v == "UNKNOWN" for v in seen), "presence() is UNKNOWN after every prefix of the stores: %s" % seen)
    check(seq and seq[-1] == "g_radarPhase = RADAR_PHASE_ON;" and seq.index("g_powerEpoch.fetch_add(1);") < seq.index("g_radarPhase = RADAR_PHASE_ON;"),
          "the phase is the last store and the epoch precedes it")
    check(seq.index("g_poweredAtMs = now;") < seq.index("g_radarPowered = true;"), "the timestamp is stored before g_radarPowered")
    old = ["g_parkRequest = false;", "g_parkAck = false;", "g_radarPowered = true;", "g_powerEpoch.fetch_add(1);", "g_poweredAtMs = now;",
           "g_radarPhase = RADAR_PHASE_ON;"]
    seen_old = run_store_prefixes(old)
    check(seen_old is not None and "ABSENT" in seen_old, "sensitivity: the previous order (powered first) shows ABSENT to a reader in between")


# ---- A1: an HA-lost stamp a few ms ahead of `now` (src/radar_power.cpp pass()) ----------------------------------------------
def ha_race_world(clamp):
    """Radars OFF at night (window 00:00-06:00, local 01:00), HA online. At a pass 2 s later, MqttTask runs between the sampling of
    `now` and the critical section and stores s_haLostMs = now + 2 (HA goes UNKNOWN). Returns (world, time of that pass)."""
    RadarPower.clamp_ha_stamp = clamp
    nvs = Nvs(); nvs.data["sched"] = 1
    w = World(nvs=nvs, tod_min=mm(1))
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 130 * S)
    t_from = w.now + 2 * S
    fired = []

    def hook(now):
        if not fired and now >= t_from:
            fired.append(now)
            w.rp.set_ha_state(HA_UNKNOWN, now + 2)            # the stamp is 2 ms AHEAD of the `now` this pass sampled

    w.rp.before_ha_read = hook
    w.run_until(t_from + 1500)
    return w, (fired[0] if fired else None)


def test_ha_stamp_race():
    print("A1: an HA-lost stamp stored 2 ms after the pass sampled `now` does not skip the 5-minute grace")
    try:
        w, t_race = ha_race_world(True)
        check(t_race is not None and w.g.ha_state == HA_UNKNOWN and w.g.ha_lost_ms == t_race + 2,
              "the race happened: HA is UNKNOWN, its stamp is 2 ms ahead of the pass that sampled `now`")
        snap = w.rp.snapshot()
        check(STATE_NAMES[snap.state] == "night_off" and REASON_NAMES[snap.reason] == "schedule",
              "the decision stays night_off / schedule: %s / %s" % (STATE_NAMES[snap.state], REASON_NAMES[snap.reason]))
        check(not w.g.radar_powered and w.g.phase == OFF and w.gpio_edges(lo=t_race) == [],
              "the radars are NOT switched on: %s" % w.gpio_edges(lo=t_race))
        check(not any(STATE_NAMES[s.state] == "failsafe_on" for t, s in w.published if t >= t_race), "failsafe_on is never published")
        # the grace itself still ends 5 minutes after the loss (the clamp does not postpone or skip it)
        w.run_until(t_race + RADAR_OFFLINE_GRACE_MS - 2 * S)
        check(not w.g.radar_powered, "still off 2 s before the grace is over")
        w.run_until(t_race + RADAR_OFFLINE_GRACE_MS + 3 * S)
        highs = [t for t, lv in w.gpio_edges(lo=t_race) if lv == 1]
        check(highs and RADAR_OFFLINE_GRACE_MS - 2 * S <= highs[0] - t_race <= RADAR_OFFLINE_GRACE_MS + 2 * S,
              "failsafe_on 5 min after the loss: +%s ms" % ([t - t_race for t in highs]))
        check(w.safety_violations == [], "safety %s" % w.safety_violations)
        # sensitivity: without the clamp (the old code) the stamp reads as 4.29e9 ms old and the radars come on at once
        w_old, t_old = ha_race_world(False)
        highs_old = [t for t, lv in w_old.gpio_edges(lo=t_old) if lv == 1]
        check(highs_old and highs_old[0] - t_old <= 5 and w_old.g.radar_powered,
              "sensitivity: without the clamp the radars come on in the same pass: %s" % ([t - t_old for t in highs_old]))
        check(any((STATE_NAMES[s.state], REASON_NAMES[s.reason]) == ("failsafe_on", "ha_unknown") for t, s in w_old.published if t >= t_old),
              "sensitivity: ... and failsafe_on / ha_unknown was published")
    finally:
        RadarPower.clamp_ha_stamp = True


# ---- A2: a failed NVS write (src/radar_power.cpp applyPending(); spec D32 with one exception) ----------------------------------
def test_nvs_write_failure():
    print("A2: NVS write failure: RAM unchanged (D32) except 'Radars' ON, which applies to RAM")
    # en = 1 with a stored en = 0: applied to RAM, the radars come on
    try:
        nvs = Nvs(); nvs.data["en"] = 0
        w = World(nvs=nvs, tod_min=mm(12))
        w.ha(1100, HA_ONLINE)
        w.run_until(1000 + 3 * S)
        nvs.fail = True
        w.cmd(w.now + 10, CMD_ENABLE, 1)
        w.run_until(1000 + 40 * S)
        check(w.rp.s.en == 1 and nvs.get("en") == 0 and nvs.writes == [] and nvs.failed == [("en", 1)],
              "en = 1: RAM 1, NVS still 0, one refused write: RAM %d NVS %d %s" % (w.rp.s.en, nvs.get("en"), nvs.failed))
        check(w.g.radar_powered and w.g.phase == ON and [lv for _, lv in w.gpio_edges(lo=2000)] == [1],
              "the radars come on after the minimum OFF time (phase %s)" % PHASE_NAMES[w.g.phase])
        check(any(line == "[RADAR] NVS write failed: en (applied to RAM only)" for _, line in w.rp.serial),
              "the distinct log line: %s" % [l for _, l in w.rp.serial if "failed" in l])
        check(w.retained is not None and w.retained.en == 1 and STATE_NAMES[w.rp.snapshot().state] == "forced_on",
              "the echo shows en 1 / forced_on")
        w2 = World(nvs=nvs, boot_ms=500, tod_min=mm(12))
        check(w2.rp.s.en == 0, "a reboot reloads the stored 0 (the RAM-only value is lost)")
        check(w.safety_violations == [], "safety %s" % w.safety_violations)
        # sensitivity: D32 without the exception leaves the radars off for good
        RadarPower.en1_ram_on_fail = False
        nvs_o = Nvs(); nvs_o.data["en"] = 0
        w_o = World(nvs=nvs_o, tod_min=mm(12))
        w_o.ha(1100, HA_ONLINE)
        w_o.run_until(1000 + 3 * S)
        nvs_o.fail = True
        w_o.cmd(w_o.now + 10, CMD_ENABLE, 1)
        w_o.cmd(w_o.now + 20 * S, CMD_ENABLE, 1)
        w_o.run_until(1000 + 40 * S)
        check(w_o.rp.s.en == 0 and not w_o.g.radar_powered and w_o.g.phase == OFF and len(nvs_o.failed) == 2,
              "sensitivity: without the exception every retry fails and the radars stay off (en %d)" % w_o.rp.s.en)
    finally:
        RadarPower.en1_ram_on_fail = True
    # en = 0 with a broken NVS: RAM unchanged, the radars stay on
    nvs = Nvs()
    w = World(nvs=nvs, tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 100 * S)
    nvs.fail = True
    w.cmd(w.now + 10, CMD_ENABLE, 0)
    w.run_until(w.now + 30 * S)
    check(w.rp.s.en == 1 and nvs.failed == [("en", 0)] and w.g.radar_powered and w.g.phase == ON
          and w.gpio_edges(lo=2000) == [] and w.rp.snapshot().en == 1,
          "en = 0: RAM unchanged, the radars stay on, the echo shows 1")
    check(any(line == "[RADAR] NVS write failed: en" for _, line in w.rp.serial)
          and not any("applied to RAM only" in line for _, line in w.rp.serial), "en = 0: the plain failure line")
    # every other command keeps D32
    for key, cmd, value in (("ns", CMD_NIGHT_START, 120), ("ne", CMD_NIGHT_END, 400), ("sched", CMD_SCHEDULE, 1),
                            ("offl", CMD_SCHED_OFFLINE, 1), ("legacy", CMD_LEGACY, 1)):
        nvs = Nvs()
        w = World(nvs=nvs, tod_min=mm(12))
        w.ha(1100, HA_ONLINE)
        w.run_until(1000 + 3 * S)
        nvs.fail = True
        w.cmd(w.now + 10, cmd, value)
        w.run_until(w.now + 3 * S)
        check(getattr(w.rp.s, key) == Nvs.DEFAULTS[key] and nvs.failed == [(key, value)] and nvs.writes == [],
              "%s = %d refused: RAM unchanged (%s)" % (key, value, getattr(w.rp.s, key)))
        check(getattr(w.rp.snapshot(), key) == Nvs.DEFAULTS[key] and w.retained is not None
              and getattr(w.retained, key) == Nvs.DEFAULTS[key], "%s: the echo shows the stored value" % key)
    # a failed write counts as a store attempt: a broken NVS is not hammered
    nvs = Nvs()
    w = World(nvs=nvs, tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 100 * S)
    nvs.fail = True
    t0 = w.now + 10
    w.cmd(t0, CMD_ENABLE, 0)
    w.cmd(t0 + 100, CMD_ENABLE, 0)
    w.run_until(t0 + 3 * S)
    check([t for t, k, v, ok in w.rp.store_log if k == "en"] == [t0, t0 + RADAR_NVS_MIN_INTERVAL_MS],
          "a refused write is retried at most once per interval: %s" % [t - t0 for t, k, v, ok in w.rp.store_log])


# ---- A3: at most one NVS store per key per RADAR_NVS_MIN_INTERVAL_MS (src/radar_power.cpp applyPending()) ------------------------
def store_times(w, key):
    return [t for t, k, v, ok in w.rp.store_log if k == key]


def flapping_world():
    """A script alternates radar/power 0 / 1, 24 messages in 943 ms (41 ms apart, the last one is 1)."""
    nvs = Nvs()
    w = World(nvs=nvs, tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    t0 = 1000 + 100 * S
    for i in range(24):
        w.cmd(t0 + i * 41, CMD_ENABLE, 0 if i % 2 == 0 else 1)
    w.run_until(t0 + 40 * S)
    return w, nvs, t0


def test_nvs_rate_limit():
    print("A3: NVS stores are limited to one per key per RADAR_NVS_MIN_INTERVAL_MS")
    check(RADAR_NVS_MIN_INTERVAL_MS == 1000, "the interval is 1000 ms")
    # (a) a flapping client
    w, nvs, t0 = flapping_world()
    en_stores = [(t - t0, k, v) for t, k, v, ok in w.rp.store_log if k == "en"]
    check(en_stores == [(0, "en", 0), (RADAR_NVS_MIN_INTERVAL_MS, "en", 1)],
          "24 alternating commands in 1 s: 2 stores, the first at once, the last value 1000 ms later: %s" % en_stores)
    check(nvs.writes == [("en", 0), ("en", 1)] and nvs.get("en") == 1 and w.rp.s.en == 1, "the final stored value is the last command")
    check(w.retained is not None and w.retained.en == 1 and w.g.state_dirty is False, "the echo converged to the last command")
    check(w.g.radar_powered and w.g.phase == ON and [lv for _, lv in w.gpio_edges(lo=t0)] == [0, 1],
          "the radars went off once and came back on: %s" % [lv for _, lv in w.gpio_edges(lo=t0)])
    check(w.safety_violations == [], "safety %s" % w.safety_violations)
    waits = [wt for t, wt in w.passes]
    check(waits and min(waits) >= 1, "every wake hint is >= 1 ms (min %s)" % (min(waits) if waits else None))
    busy = [t for t, wt in w.passes if t0 <= t <= t0 + 1100]
    check(len(busy) <= 24 + 6, "the loop does not spin: %d passes in the flapping second" % len(busy))
    # a deferred value is visible in the echo as the STORED one until it is applied
    check(all(s.en == 0 for t, s in w.published if t0 + 100 <= t < t0 + RADAR_NVS_MIN_INTERVAL_MS) and
          any(t0 + 100 <= t < t0 + RADAR_NVS_MIN_INTERVAL_MS for t, s in w.published), "while deferred the echo shows the stored 0")
    # sensitivity: without the limit every changed message is one NVS commit
    try:
        RadarPower.rate_limit = False
        w_old, nvs_old, t0_old = flapping_world()
        check(len([1 for k, v in nvs_old.writes if k == "en"]) == 24,
              "sensitivity: without the limit 24 commands write 24 times (%d)" % len(nvs_old.writes))
    finally:
        RadarPower.rate_limit = True
    # (b) the first command after an idle period is applied at once
    w = World(nvs=Nvs(), tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    t0 = 1000 + 20 * S
    w.cmd(t0, CMD_SCHEDULE, 1)
    w.cmd(t0 + 5 * S, CMD_SCHEDULE, 0)
    w.run_until(t0 + 8 * S)
    check(store_times(w, "sched") == [t0, t0 + 5 * S], "a command after 5 s idle is stored with no delay: %s" % [t - t0 for t in store_times(w, "sched")])
    # ... also right after the boot (uptime below the interval)
    w = World(nvs=Nvs(), boot_ms=50, tod_min=mm(12))
    w.cmd(60, CMD_LEGACY, 1)
    w.run_until(3 * S)
    check(store_times(w, "legacy") == [60], "the first command 10 ms after the boot is not deferred: %s" % store_times(w, "legacy"))
    # (c) different keys inside the same second: all stored at once, a store of one key does not delay another
    w = World(nvs=Nvs(), tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    t0 = 1000 + 20 * S
    plan = ((0, CMD_NIGHT_START, 60, "ns"), (10, CMD_NIGHT_END, 300, "ne"), (20, CMD_SCHEDULE, 1, "sched"),
            (30, CMD_SCHED_OFFLINE, 1, "offl"), (40, CMD_LEGACY, 1, "legacy"), (50, CMD_ENABLE, 0, "en"))
    for dt, cmd, v, key in plan:
        w.cmd(t0 + dt, cmd, v)
    w.cmd(t0 + 60, CMD_NIGHT_START, 90)                       # a second ns change inside the second: waits for ns only
    w.cmd(t0 + 70, CMD_NIGHT_END, 310)                        # ... and ne's, for ne only
    w.run_until(t0 + 5 * S)
    check(all(store_times(w, key)[:1] == [t0 + dt] for dt, cmd, v, key in plan),
          "ns, ne, sched, offl, legacy, en inside one second: each stored at its own command time: %s"
          % {key: [t - t0 for t in store_times(w, key)] for dt, cmd, v, key in plan})
    check(store_times(w, "ns") == [t0, t0 + RADAR_NVS_MIN_INTERVAL_MS] and store_times(w, "ne") == [t0 + 10, t0 + 10 + RADAR_NVS_MIN_INTERVAL_MS],
          "a second change of ns / ne is deferred to 1000 ms after that key's own store, independent of the other key")
    check((w.rp.s.ns, w.rp.s.ne) == (90, 310), "ns and ne end at the newest values")
    # (d) an unchanged command writes nothing and is echoed
    nvs = Nvs()
    w = World(nvs=nvs, tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    t0 = 1000 + 20 * S
    n_before = len(w.published)
    for i in range(5):
        w.cmd(t0 + i * 100, CMD_ENABLE, 1)
        w.cmd(t0 + i * 100 + 10, CMD_NIGHT_START, RADAR_NIGHT_START_DEFAULT_MIN)
    w.run_until(t0 + 3 * S)
    check(nvs.writes == [] and w.rp.store_log == [], "unchanged commands write nothing")
    check(len(w.published) > n_before, "... but are echoed")
    check(w.rp.pending == [None] * CMD_COUNT, "... and leave nothing waiting")
    w.cmd(w.now + 10, CMD_NIGHT_START, 60)                    # an echo of an unchanged ns did not start the interval
    t_chg = w.now + 10
    w.run_until(w.now + 3 * S)
    check(store_times(w, "ns") == [t_chg], "an unchanged command does not delay the next change of that key")
    # (e) a deferred command is applied about 1000 ms after the previous store of its key, and the echo shows the stored value until then
    w = World(nvs=Nvs(), tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    t0 = 1000 + 20 * S
    w.cmd(t0, CMD_SCHEDULE, 1)
    w.cmd(t0 + 100, CMD_SCHEDULE, 0)
    w.run_until(t0 + 500)
    check(w.rp.s.sched == 1 and w.rp.snapshot().sched == 1, "deferred: the stored value (1) is still the one in force and echoed")
    w.run_until(t0 + 3 * S)
    check(store_times(w, "sched") == [t0, t0 + RADAR_NVS_MIN_INTERVAL_MS] and w.rp.s.sched == 0 and w.nvs.get("sched") == 0
          and w.retained.sched == 0, "applied 1000 ms after the previous store: %s" % [t - t0 for t in store_times(w, "sched")])
    # the boundary: 999 ms after the store is deferred by exactly 1 ms (a wake hint of 1, never 0), 1000 ms is stored at once
    w = World(nvs=Nvs(), tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    w.cmd(t0, CMD_LEGACY, 1)
    w.cmd(t0 + RADAR_NVS_MIN_INTERVAL_MS - 1, CMD_LEGACY, 0)
    w.cmd(t0 + 3 * S, CMD_LEGACY, 1)
    w.cmd(t0 + 3 * S + RADAR_NVS_MIN_INTERVAL_MS, CMD_LEGACY, 0)
    w.run_until(t0 + 6 * S)
    check((t0 + RADAR_NVS_MIN_INTERVAL_MS - 1, 1) in w.passes, "999 ms after the store: the wake hint is exactly 1 ms")
    check(store_times(w, "legacy") == [t0, t0 + RADAR_NVS_MIN_INTERVAL_MS, t0 + 3 * S, t0 + 3 * S + RADAR_NVS_MIN_INTERVAL_MS],
          "stores at 0, 1000, 3000, 4000 ms: %s" % [t - t0 for t in store_times(w, "legacy")])
    check(min(wt for t, wt in w.passes) >= 1, "no pass ever returns 0")
    # (f) the newest value wins, an equal-to-stored newest value cancels the deferral
    w = World(nvs=Nvs(), tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    t0 = 1000 + 20 * S
    w.cmd(t0, CMD_NIGHT_START, 60)
    w.cmd(t0 + 100, CMD_NIGHT_START, 120)
    w.cmd(t0 + 200, CMD_NIGHT_START, 180)
    w.cmd(t0, CMD_ENABLE, 0)
    w.cmd(t0 + 100, CMD_ENABLE, 1)
    w.cmd(t0 + 200, CMD_ENABLE, 0)
    w.run_until(t0 + 5 * S)
    check([v for k, v in w.nvs.writes if k == "ns"] == [60, 180], "ns: 60, then only the newest deferred value 180 (never 120): %s" % w.nvs.writes)
    check([v for k, v in w.nvs.writes if k == "en"] == [0] and w.rp.s.en == 0,
          "en: a newer command equal to the stored value cancels the deferred one")
    # (g) a command that arrives while the deferred slots are put back is not overwritten
    w = World(nvs=Nvs(), tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    w.cmd(t0, CMD_NIGHT_START, 60)
    w.cmd(t0 + 100, CMD_NIGHT_START, 120)
    race = []

    def inject(now):
        if now == t0 + 100 and not race:
            race.append(now)
            w.rp.request(CMD_NIGHT_START, 180)                # MqttTask stores a newer value after the slots were taken

    w.rp.before_putback = inject
    w.run_until(t0 + 5 * S)
    check(race and [v for k, v in w.nvs.writes if k == "ns"] == [60, 180] and w.rp.s.ns == 180,
          "put-back does not clobber a newer slot: %s" % w.nvs.writes)


# ---- A4: an automatic OFF waits for the settings to settle (src/radar_power.cpp stepPhase()) -------------------------------------
def learner_world(settle):
    """Node window 22:00-00:30 (local 05:30 is outside), schedule on, radars ON for more than 60 s, HA online. The HA add-on learner
    approves 01:00-05:00 and sends night_start, night_end, schedule 41 ms apart: after night_start alone the window is 01:00-00:30
    (23.5 h), which contains 05:30."""
    RadarPower.settle_rule = settle
    nvs = Nvs()
    nvs.data.update(sched=1, ns=mm(22), ne=mm(0, 30))
    w = World(nvs=nvs, tod_min=mm(5, 30))
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 100 * S)
    t = w.now + 10
    w.cmd(t, CMD_NIGHT_START, mm(1))
    w.cmd(t + 41, CMD_NIGHT_END, mm(5))
    w.cmd(t + 82, CMD_SCHEDULE, 1)
    w.run_until(t + 6 * S)
    return w, t


def window_start_world(change_ms_before_start, kick_ms_after_start, settle=True):
    """Schedule on, window 00:00-06:00, local 23:55, radars ON for 5 minutes. The clock reaches 00:00 at t_start. A setting (legacy)
    changes `change_ms_before_start` before it (None = no change). A pass is forced `kick_ms_after_start` after it."""
    RadarPower.settle_rule = settle
    nvs = Nvs(); nvs.data["sched"] = 1
    w = World(nvs=nvs, tod_min=mm(23, 55))
    w.ha(1100, HA_ONLINE)
    t_start = 1000 + 5 * 60 * S
    if change_ms_before_start is not None:
        w.cmd(t_start - change_ms_before_start, CMD_LEGACY, 1)
    w.at(t_start + kick_ms_after_start, lambda tt: w.kick())
    w.run_until(t_start + 3 * S)
    return w, t_start


def parked_at(w):
    return [t for t, line in w.rp.serial if line == "[RADAR] park requested"]


def test_settle_rule():
    print("A4: an automatic OFF starts no park within RADAR_SETTING_SETTLE_MS of an applied setting change")
    check(RADAR_SETTING_SETTLE_MS == 500, "the settle time is 500 ms")
    try:
        # (1) the learner's three messages 41 ms apart
        w, t = learner_world(True)
        snap = w.rp.snapshot()
        check(parked_at(w) == [] and not any("park withdrawn" in line for _, line in w.rp.serial),
              "night_start, night_end, schedule 41 ms apart: no park is requested, none withdrawn: %s" % w.rp.serial)
        check(w.g.phase == ON and w.g.radar_powered and not w.g.park_request and w.ld.parks == [] and w.gpio_edges(lo=t) == [],
              "the radars stay ON, the LD2454 link is never parked")
        check((w.rp.s.ns, w.rp.s.ne, w.rp.s.sched) == (mm(1), mm(5), 1) and STATE_NAMES[snap.state] == "on" and REASON_NAMES[snap.reason] == "schedule",
              "the new window is in force: 01:00-05:00, state on / schedule")
        check(not any(STATE_NAMES[s.state] == "night_off" for tt, s in w.published if tt >= t) and w.safety_violations == [],
              "night_off is never published")
        # sensitivity: without the rule the first message requests a park and the second withdraws it
        w_old, t_old = learner_world(False)
        check(parked_at(w_old) == [t_old] and w_old.ld.parks and any(lv == 0 for _, lv in w_old.gpio_edges(lo=t_old)),
              "sensitivity: the old behaviour requests a park at night_start, the LD2454 link parks and the radars go off: %s" % w_old.rp.serial)
        # (2) a window that really starts, no recent setting change: no delay (the first pass at or after the start)
        w, t_start = window_start_world(None, 100)
        first = min(t for t, _ in w.passes if t >= t_start)
        check(parked_at(w) == [first] and first - t_start < RADAR_TICK_MS,
              "no recent setting change: the park is requested at the first pass of the window, no settle delay: %s"
              % [t - t_start for t in parked_at(w)])
        # (3) a setting change 200 ms before the window start: the park waits until 500 ms after the change
        w, t_start = window_start_world(200, 100)
        check(parked_at(w) == [t_start - 200 + RADAR_SETTING_SETTLE_MS],
              "setting change 200 ms before the start, pass 100 ms after it: the park is requested 500 ms after the change: %s"
              % [t - t_start for t in parked_at(w)])
        check((t_start + 100, RADAR_SETTING_SETTLE_MS - 300) in w.passes, "the wake hint of the waiting pass is the remaining settle time (200 ms)")
        check(w.g.phase in (OFF_SETTLING, OFF) and STATE_NAMES[w.rp.snapshot().state] == "night_off", "and the night OFF then happens")
        w_old, t_start = window_start_world(200, 100, settle=False)
        check(parked_at(w_old) == [t_start + 100], "sensitivity: the old behaviour parks at the first pass, 100 ms after the start")
        # the status keeps the ON state with reason pending while the park waits
        w, t_start = window_start_world(200, 100)
        pend = [s for tt, s in w.published if t_start + 100 <= tt < t_start + 300]
        check(pend and all(REASON_NAMES[s.reason] == "pending" and STATE_NAMES[s.state] == "on" for s in pend[-1:]),
              "while the park waits: on / pending")
    finally:
        RadarPower.settle_rule = True
    # (4) a manual OFF right after a setting change is not delayed
    nvs = Nvs()
    w = World(nvs=nvs, tod_min=mm(12))
    w.ha(1100, HA_ONLINE)
    w.run_until(1000 + 100 * S)
    t = w.now + 10
    w.cmd(t, CMD_LEGACY, 1)
    w.cmd(t + 10, CMD_ENABLE, 0)
    w.run_until(t + 5 * S)
    check(parked_at(w) == [t + 10], "a manual OFF 10 ms after a setting change parks at once: %s" % [x - t for x in parked_at(w)])
    check(not w.g.radar_powered and STATE_NAMES[w.rp.snapshot().state] == "forced_off", "... and the radars go off")
    # (5) a park already in progress is neither delayed nor cancelled by a setting change; its withdrawal is immediate
    nvs = Nvs(); nvs.data["sched"] = 1
    w = World(nvs=nvs, tod_min=mm(23, 55), ld=dict(stuck=True))
    w.ha(1100, HA_ONLINE)
    t_start = 1000 + 5 * 60 * S
    w.run_until(t_start + 2 * S)
    t_req = parked_at(w)
    check(len(t_req) == 1 and w.g.phase == PARKING, "the window start parks (the stuck LD2454Task never acks)")
    w.cmd(w.now + 100, CMD_LEGACY, 1)
    t_legacy = w.now + 100
    w.run_until(t_legacy + 2 * S)
    check(w.g.phase == PARKING and len(parked_at(w)) == 1 and not any("park withdrawn" in line for _, line in w.rp.serial),
          "a setting change does not cancel or restart a park in progress")
    t_wd = w.now + 100
    w.cmd(t_wd, CMD_SCHEDULE, 0)
    w.run_until(t_wd + 50)
    check(w.g.phase == ON and [t for t, line in w.rp.serial if line == "[RADAR] park withdrawn"] == [t_wd],
          "the withdrawal of a park is immediate, right after a setting change was applied")
    # (6) a fault power-cycle is not an automatic OFF decision: the settle rule does not apply
    w0 = World(nvs=Nvs(), tod_min=mm(12), ld=dict(silent=True))
    w0.ha(1100, HA_ONLINE)
    w0.run_until(1000 + 100 * S)
    t_cycle = parked_at(w0)
    check(len(t_cycle) >= 1, "the silent radar triggers a fault power-cycle")
    if t_cycle:
        w1 = World(nvs=Nvs(), tod_min=mm(12), ld=dict(silent=True))
        w1.ha(1100, HA_ONLINE)
        w1.cmd(t_cycle[0] - 300, CMD_LEGACY, 1)
        w1.at(t_cycle[0], lambda tt: w1.kick())
        w1.run_until(1000 + 100 * S)
        check(parked_at(w1)[:1] == [t_cycle[0]], "a setting change 300 ms before the fault cycle does not delay it: %s vs %s"
              % (parked_at(w1)[:1], t_cycle[:1]))


# =================================================================== links to the C++ sources ===========================
def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def cpp_array(src, fn_name):
    """The string initialiser of `static const char* const n[] = {...};` inside the C++ function fn_name."""
    m = re.search(r"const char\* " + fn_name + r"\(uint8_t \w+\) \{\s*static const char\* const n\[\] = \{([^}]*)\};", src)
    return re.findall(r'"([^"]*)"', m.group(1)) if m else None


def cpp_enum(src, name):
    m = re.search(r"enum " + name + r"\s*:\s*uint8_t\s*\{([^}]*)\}", src, flags=re.S)
    if not m:
        return None
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return [x.split("=")[0].strip() for x in body.split(",") if x.strip()]


def cpp_parse_on_off(text):
    """radarParseOnOff(), statement by statement."""
    if text == "1":
        return 1
    if text == "0":
        return 0
    if text.lower() == "on":
        return 1
    if text.lower() == "off":
        return 0
    return -1


def cpp_parse_time_of_day(text):
    """radarParseTimeOfDay(), character by character (a NUL-terminated string: p[0] / p[1] may read the terminator)."""
    s = text + "\0\0\0"
    p = 0
    h = nh = 0
    while "0" <= s[p] <= "9" and nh < 3:
        h = h * 10 + (ord(s[p]) - 48)
        p += 1
        nh += 1
    if nh < 1 or nh > 2 or s[p] != ":":
        return None
    p += 1
    if not ("0" <= s[p] <= "9" and "0" <= s[p + 1] <= "9"):
        return None
    m = (ord(s[p]) - 48) * 10 + (ord(s[p + 1]) - 48)
    p += 2
    sec = 0
    if s[p] == ":":
        p += 1
        if not ("0" <= s[p] <= "9" and "0" <= s[p + 1] <= "9"):
            return None
        sec = (ord(s[p]) - 48) * 10 + (ord(s[p + 1]) - 48)
        p += 2
    if s[p] != "\0" or h > 23 or m > 59 or sec > 59:
        return None
    return h * 60 + m


def test_cpp_links():
    print("links to include/radar_constants.h, include/radar_power.h, src/radar_power.cpp")
    hdr = read_source("include/radar_power.h")
    cpp_src = read_source("src/radar_power.cpp")
    consts = read_source("include/radar_constants.h")
    # the user decisions that the scenarios pin, straight from the header
    check(C["RADAR_NIGHT_START_DEFAULT_MIN"] == 0 and C["RADAR_NIGHT_END_DEFAULT_MIN"] == 360, "night window default 00:00-06:00")
    check(C["RADAR_ON_SETTLE_MS"] == 5 and C["RADAR_OFF_SETTLE_MS"] == 50 and C["RADAR_MIN_ON_MS"] == 60000
          and C["RADAR_MIN_OFF_MS"] == 10000 and C["RADAR_MAX_POWERUPS_PER_H"] == 12 and C["PARK_ACK_TIMEOUT_MS"] == 6000
          and C["RADAR_OFFLINE_GRACE_MS"] == 300000 and C["LD2412_STATIC_WARMUP_MS"] == 12000, "timing constants of the spec")
    check(C["RADAR_OFF_SETTLE_MS"] >= 50, "firmware waits at least 50 ms after GPIO9 LOW before treating the radars as off")
    check(C["RADAR_PARK_POLL_MS"] < C["PARK_ACK_TIMEOUT_MS"] < C["RADAR_MIN_ON_MS"], "park poll < park timeout < minimum ON")
    check(C["RADAR_HISTORY_MAX"] >= C["RADAR_MAX_POWERUPS_PER_H"], "the history holds the 12 power-ups of the throttle window")
    check("#ifdef BOARD_REV2" in consts and "#ifdef BOARD_REV2" in hdr and "#ifdef BOARD_REV2" in cpp_src,
          "headers and radar_power.cpp are gated by BOARD_REV2")
    # enum order
    check(cpp_enum(hdr, "RadarPhase") == ["RADAR_PHASE_" + n for n in PHASE_NAMES], "RadarPhase order: %s" % cpp_enum(hdr, "RadarPhase"))
    check(cpp_enum(hdr, "RadarState") == ["RADAR_ST_" + n.upper() for n in STATE_NAMES], "RadarState order: %s" % cpp_enum(hdr, "RadarState"))
    check(cpp_enum(hdr, "RadarReason") == ["RADAR_RS_" + n.upper() for n in REASON_NAMES], "RadarReason order: %s" % cpp_enum(hdr, "RadarReason"))
    check(cpp_enum(hdr, "RadarFault") == ["RADAR_FAULT_NONE", "RADAR_FAULT_PARK_TIMEOUT", "RADAR_FAULT_LD2454_NO_FRAMES"], "RadarFault order")
    check(cpp_enum(hdr, "RadarHaState") == ["RADAR_HA_" + n.upper() for n in HA_NAMES], "RadarHaState order")
    check(cpp_enum(hdr, "RadarCmd") == ["RADAR_CMD_" + n for n in ("ENABLE", "SCHEDULE", "NIGHT_START", "NIGHT_END", "SCHED_OFFLINE", "LEGACY", "COUNT")],
          "RadarCmd order: %s" % cpp_enum(hdr, "RadarCmd"))
    check('"ANFXS"' in hdr and SM_CODES == "ANFXS", "sm codes A N F X S")
    # name tables
    check(cpp_array(cpp_src, "radarStateName") == list(STATE_NAMES), "radarStateName table")
    check(cpp_array(cpp_src, "radarReasonName") == list(REASON_NAMES), "radarReasonName table")
    check(cpp_array(cpp_src, "radarFaultName") == list(FAULT_NAMES), "radarFaultName table")
    check(cpp_array(cpp_src, "radarHaName") == list(HA_NAMES), "radarHaName table")
    check(cpp_array(cpp_src, "radarPhaseName") == list(PHASE_NAMES), "radarPhaseName table")
    # NVS keys and defaults (spec 4.3)
    m = re.search(r"NVS_KEYS\[RADAR_CMD_COUNT\]\s*=\s*\{([^}]*)\}", cpp_src)
    check(m and re.findall(r'"([^"]*)"', m.group(1)) == list(CMD_KEYS), "NVS keys in RadarCmd order: %s" % (m and m.group(1)))
    check('RADAR_NVS_NAMESPACE' in consts and '"radar"' in consts, "NVS namespace radar")
    m = re.search(r"Settings defaultSettings\(\) \{(.*?)\n\}", cpp_src, flags=re.S)
    d = {}
    if m:
        for key, val in re.findall(r"s\.(en|sched|offl|legacy|ns|ne) = ([A-Za-z0-9_]+);", m.group(1)):
            d[key] = int(val) if val.isdigit() else C[val]
    check(d == Nvs.DEFAULTS, "NVS defaults en 1, sched 0 (off until commissioned), 00:00-06:00, offl 0, legacy 0: %s" % d)
    check(re.search(r"s\.en > 1.*s\.ns >= 1440.*s\.ne >= 1440", cpp_src, flags=re.S) is not None, "loadSettings() validates")
    # every C++ function the model names exists
    for name in ("RadarPower::afterOtaPause", "RadarPower::pass", "RadarPower::applyPending", "RadarPower::pruneHistory",
                 "RadarPower::powerupsLastHour", "RadarPower::oldestPowerupInHour", "RadarPower::recordPowerUp",
                 "RadarPower::recordPowerDown", "RadarPower::onShareX10", "RadarPower::stepPhase", "RadarPower::nextWait",
                 "RadarPower::updateStatus", "radarPowerRequest", "radarSetHaState", "radarStatusSnapshot", "radarPowerEarlyInit",
                 "initRadarPowerTask", "radarPowerKick", "decide", "inWindow", "localAfter",
                 "RadarPower::refreshStamps", "keepOld"):
        check(re.search(r"\b" + re.escape(name) + r"\(", cpp_src) is not None, "C++ function " + name + "() exists")
    # the phase machine writes GPIO9 only after the ack (PARKING) and at OFF -> POWERING
    code = strip_comments(cpp_src)
    writes = re.findall(r"setRadarPin\((true|false)\)", code)
    check(writes == ["false", "true"], "setRadarPin(): one LOW (after the ack) and one HIGH (after the OFF dwell): %s" % writes)
    check(len(re.findall(r"gpio_set_level\(", code)) == 2, "gpio_set_level(): the early latch and setRadarPin() only")
    pos_clear = code.index("g_parkAck = false;", code.index("case RADAR_PHASE_ON"))
    pos_raise = code.index("g_parkRequest = true;", pos_clear)
    pos_ack = code.index("g_parkAck.load()")
    pos_low = code.index("setRadarPin(false)")
    check(pos_clear < pos_raise and pos_ack < pos_low, "order: the ack is cleared before the request is raised; GPIO9 LOW only on the ack branch")
    check(code.index("gpio_set_level((gpio_num_t)RADAR_PWR_EN_PIN, en ? 1 : 0)") < code.index("pinMode(RADAR_PWR_EN_PIN, OUTPUT)"),
          "early init latches GPIO9 before pinMode(OUTPUT)")
    # the time zone is set before the first pass (NtpClockTask only sets it later; the clock can already be valid after a restart)
    early = code[code.index("void radarPowerEarlyInit()"):]
    early = early[:early.index("\n}\n")]
    check('setenv("TZ", TIME_ZONE, 1);' in early and "tzset();" in early and early.index("tzset();") < early.index("radarPhaseAfterBoot("),
          "radarPowerEarlyInit(): TZ is set (setenv + tzset) before radarPhaseAfterBoot()")
    # the 49.7-day refresh: keepOld(), refreshStamps() before decide(), the HA stamp inside the critical section
    check("age > RADAR_STAMP_MAX_AGE_MS && age < 0x80000000u" in code and "(uint32_t)(now - RADAR_STAMP_MAX_AGE_MS)" in code,
          "keepOld(): older than the limit and not 'ahead of now', moved to exactly the limit")
    pass_body = code[code.index("uint32_t RadarPower::pass("):]
    pass_body = pass_body[:pass_body.index("\n}\n")]
    check(pass_body.index("refreshStamps(now);") < pass_body.index("decide(s_,"), "pass(): refreshStamps() runs before decide()")
    check("if (ha_ != RADAR_HA_ONLINE) s_haLostMs = keepOld(now, s_haLostMs);" in pass_body
          and pass_body.index("portENTER_CRITICAL") < pass_body.index("keepOld(now, s_haLostMs)") < pass_body.index("portEXIT_CRITICAL"),
          "pass(): s_haLostMs is refreshed while HA is away, inside the critical section")
    refresh = code[code.index("void RadarPower::refreshStamps("):]
    refresh = refresh[:refresh.index("\n}\n")]
    check("phase == RADAR_PHASE_ON || phase == RADAR_PHASE_PARKING" in refresh and "g_poweredAtMs = kept;" in refresh
          and "phase == RADAR_PHASE_OFF" in refresh and "offSinceMs_ = keepOld(now, offSinceMs_);" in refresh
          and "if (haveFaultCycle_) lastFaultCycleMs_ = keepOld(now, lastFaultCycleMs_);" in refresh,
          "refreshStamps(): g_poweredAtMs while ON / PARKING, offSinceMs_ while OFF, lastFaultCycleMs_ once a cycle ran")
    # GPIO9 has one writer: no other source mentions RADAR_PWR_EN_PIN in code
    users = []
    for sub in ("src", "include"):
        for fn in sorted(os.listdir(os.path.join(ROOT, sub))):
            if fn.endswith((".cpp", ".h", ".ino")):
                if "RADAR_PWR_EN_PIN" in strip_comments(read_source(sub + "/" + fn)):
                    users.append(sub + "/" + fn)
    check(sorted(users) == ["include/pins.h", "src/radar_power.cpp"], "RADAR_PWR_EN_PIN is used only in pins.h and radar_power.cpp: %s" % users)
    # parsers: transliteration of the C++ against the model
    rng = random.Random(2454)
    alphabet = "0123456789:-+a "
    samples = ["", "0", "1", "ON", "off", "Off", "oN", "auto", "2", " 1", "1 ", "true", "00:00", "0:00", "6:00", "06:00:00", "23:59:59",
               "24:00", "12:60", "12:00:60", "12:0", "12:00:0", "-1:00", "+1:00", "12:00 ", " 12:00", "None", "unknown", "012:00", "1:2:3"]
    samples += ["".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 10))) for _ in range(40000)]
    bad = [t for t in samples if cpp_parse_time_of_day(t) != parse_time_of_day(t)]
    check(not bad, "radarParseTimeOfDay transliteration equals the model on %d strings (%s)" % (len(samples), bad[:3]))
    bad = [t for t in samples if cpp_parse_on_off(t) != parse_on_off(t)]
    check(not bad, "radarParseOnOff transliteration equals the model (%s)" % bad[:3])
    # the C++ keeps the statements the transliteration mirrors
    check("nh < 1 || nh > 2 || *p != ':'" in cpp_src and "h > 23 || m > 59 || sec > 59" in cpp_src and 'strcmp(text, "1") == 0' in cpp_src,
          "parser statements are those of spec Appendix D")


def cpp_body(code, signature):
    """The text of the C++ function that starts with `signature`, up to its closing brace in column 0 ('' when it is missing)."""
    i = code.find(signature)
    j = code.find("\n}\n", i) if i >= 0 else -1
    return code[i:j] if 0 <= i < j else ""


def in_order(text, *needles):
    """True when every needle occurs in `text`, each after the previous one."""
    pos = 0
    for n in needles:
        pos = text.find(n, pos)
        if pos < 0:
            return False
        pos += len(n)
    return True


def test_cpp_links_settings():
    print("links to src/radar_power.cpp: HA stamp clamp, NVS failure rule, NVS rate limit, settle rule")
    code = strip_comments(read_source("src/radar_power.cpp"))
    consts = read_source("include/radar_constants.h")
    check(C["RADAR_NVS_MIN_INTERVAL_MS"] == 1000 and C["RADAR_SETTING_SETTLE_MS"] == 500,
          "RADAR_NVS_MIN_INTERVAL_MS 1000 and RADAR_SETTING_SETTLE_MS 500 in include/radar_constants.h")
    check(re.search(r"RADAR_NVS_MIN_INTERVAL_MS\s*=\s*1000;\s*//", consts) and re.search(r"RADAR_SETTING_SETTLE_MS\s*=\s*500;\s*//", consts),
          "both constants carry a comment")
    # A1: a stamp that another task stored after `now` was sampled reads as `now`, outside the critical section
    pass_body = cpp_body(code, "uint32_t RadarPower::pass(")
    clamp = "if ((int32_t)(now - haLostMs) < 0) haLostMs = now;"
    check(in_order(pass_body, "portEXIT_CRITICAL(&s_mux);", clamp, "decide(s_,"),
          "pass(): a haLostMs ahead of now becomes now, after the critical section and before decide()")
    check(in_order(pass_body, "haLostMs = s_haLostMs;", clamp), "pass(): the clamp acts on the value read under the spinlock")
    check("const uint32_t storeWait = applyPending(now64);" in pass_body
          and "return minU32(minU32(wait, nextWait(dec, now)), storeWait);" in pass_body,
          "pass(): applyPending(now64) runs first and its retry hint is merged into the returned wait")
    # A2 + A3: applyPending()
    ap = cpp_body(code, "uint32_t RadarPower::applyPending(")
    check("if (!mask) return 0xFFFFFFFFu;" in ap and "uint32_t retryMs = 0xFFFFFFFFu;" in ap and ap.rstrip().endswith("return retryMs;"),
          "applyPending(): 'none' is 0xFFFFFFFF, the earliest retry is returned")
    needles = ("if (values[cmd] == settingValue(s_, c)) continue;",
               "const uint64_t sinceStore = now64 - lastStoreMs64_[cmd];",
               "if (sinceStore < RADAR_NVS_MIN_INTERVAL_MS) {",
               "retryMs = minU32(retryMs, (uint32_t)(RADAR_NVS_MIN_INTERVAL_MS - sinceStore));",
               "lastStoreMs64_[cmd] = now64;",
               "if (storeSetting(c, values[cmd])) {")
    pos = [ap.find(n) for n in needles]
    check(all(p >= 0 for p in pos) and pos == sorted(pos),
          "applyPending(): unchanged -> skip, then the interval test (defer, hint), then the stamp, then the store: %s" % pos)
    check(ap.count("lastStoreMs64_[cmd] = now64;") == 1, "one store stamp, taken for every attempt (a failed write counts)")
    check(ap.count("lastChangeMs64_ = now64;") == 2, "lastChangeMs64_ is stamped on the two applied-change paths only")
    # the three outcomes of the store: stored, failed 'en' = 1 (RAM only), every other failure (D32)
    i_ok, i_en1 = ap.find("if (storeSetting(c, values[cmd])) {"), ap.find("} else if (c == RADAR_CMD_ENABLE")
    i_plain = ap.find("} else {", i_en1) if i_en1 >= 0 else -1
    i_end = ap.find("if (deferred) {")
    ok_blk = ap[i_ok:i_en1] if 0 <= i_ok < i_en1 else ""
    en1_blk = ap[i_en1:i_plain] if 0 <= i_en1 < i_plain else ""
    plain_blk = ap[i_plain:i_end] if 0 <= i_plain < i_end else ""
    check("applySetting(s_, c, values[cmd]);" in ok_blk and "lastChangeMs64_ = now64;" in ok_blk, "a stored change applies and stamps")
    check("values[cmd] == 1) {" in en1_blk and "applySetting(s_, c, 1);" in en1_blk and "lastChangeMs64_ = now64;" in en1_blk
          and '"[RADAR] NVS write failed: %s (applied to RAM only)\\n"' in en1_blk,
          "a failed write of 'en' = 1 applies to RAM with the distinct log line")
    check("applySetting" not in plain_blk and "lastChangeMs64_" not in plain_blk and '"[RADAR] NVS write failed: %s\\n"' in plain_blk,
          "every other failed write keeps D32: RAM unchanged, plain failure line")
    putback = ap[i_end:] if i_end >= 0 else ""
    check(in_order(putback, "portENTER_CRITICAL(&s_mux);", "(s_pendingMask & bit)", "s_pendingValue[cmd] = values[cmd];", "s_pendingMask |= bit;",
                   "portEXIT_CRITICAL(&s_mux);"),
          "the deferred slots go back under the spinlock, only where no newer command waits")
    check("if (!(deferred & bit) || (s_pendingMask & bit)) continue;" in putback, "a slot that holds a newer command is skipped (never clobbered)")
    check(in_order(ap, "if (deferred) {", "g_stateDirty = true;", "return retryMs;"), "the echo flag is set after the put-back")
    check("RADAR_NVS_MIN_INTERVAL_MS" not in cpp_body(code, "void radarPowerRequest(") and "storeSetting(" not in cpp_body(code, "void radarPowerRequest("),
          "radarPowerRequest() still never writes NVS and does not rate-limit (a newer command only overwrites its slot)")
    begin = cpp_body(code, "void RadarPower::begin(")
    check("lastStoreMs64_[i] = nowMs64 - RADAR_NVS_MIN_INTERVAL_MS;" in begin and "lastChangeMs64_ = nowMs64 - RADAR_SETTING_SETTLE_MS;" in begin,
          "begin(): no store and no change yet (the first command and the first OFF are not delayed, even with an uptime below the interval)")
    check("uint64_t   lastStoreMs64_[RADAR_CMD_COUNT];" in code and "uint64_t   lastChangeMs64_ = 0;" in code,
          "the stamps are 64-bit members of RadarPower (not shared)")
    # A4: stepPhase()
    sp = cpp_body(code, "uint32_t RadarPower::stepPhase(")
    on_case = sp[sp.index("case RADAR_PHASE_ON:"):sp.index("case RADAR_PHASE_PARKING:")]
    settle = "else if (dec.automatic && now64 - lastChangeMs64_ < RADAR_SETTING_SETTLE_MS) {"
    check(in_order(on_case, "elapsed(now, g_poweredAtMs.load()) < RADAR_MIN_ON_MS", "thr_ = true;", settle, "g_parkRequest = true;"),
          "stepPhase(): in phase ON, after the minimum-ON deferral and the throttle, before the park request")
    check("wait = (uint32_t)(RADAR_SETTING_SETTLE_MS - (now64 - lastChangeMs64_));" in on_case, "the wake hint is the remaining settle time")
    check("RADAR_SETTING_SETTLE_MS" not in sp[sp.index("case RADAR_PHASE_PARKING:"):], "no other phase (park in progress, withdrawal, OFF, POWERING) uses the settle time")
    check(sp.count("RADAR_SETTING_SETTLE_MS") == 2 and "wantsOff" not in settle and "automatic &&" in settle and "dec.automatic" in settle,
          "the rule tests the schedule decision (dec.automatic): a manual OFF and a fault power-cycle are never delayed")


def main():
    for fn in (test_cpp_links, test_window_and_parsers, test_defaults_and_boot, test_schedule_days, test_settle_and_uart_rules,
               test_ntp_invalid, test_offline_grace, test_manual_off_persistence, test_rapid_toggling,
               test_throttle, test_states_domain, test_handover, test_park_cases, test_ota, test_fault_cycle,
               test_dst_and_wrap, test_stamp_wrap, test_powering_store_order, test_attr_json, test_ha_stamp_race,
               test_nvs_write_failure, test_nvs_rate_limit, test_settle_rule, test_cpp_links_settings):
        fn()
    print("\n%d checks passed, %d failed" % (PASSES[0], len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  FAIL:", f)
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
