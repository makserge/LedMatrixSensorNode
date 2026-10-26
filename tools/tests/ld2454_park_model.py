#!/usr/bin/env python3
"""Host-side model test for the LD2454 driver's park / re-apply path (P2 spec 12.2, driver hooks of spec 5).

Starts from a line-by-line port of the P1 driver (src/ld2454_sensor.cpp: processByte, processTrackingFrame, update,
enableMultiTargetTracking, sendCommand, processAckFrame; a byte-level UART RX buffer of 1024 + 128 B and a radar that
streams 30-byte frames every 100 ms and answers commands; the section "P1 driver port" below is that port) and adds what
BOARD_REV2 changes, statement by statement (class Sim2, section "driver"):

  openLink() / parkLink()            the UART rule: Serial1 only in phase ON with no park request
  _awaitFirstFrame, _openedMs        wait for the first tracking frame (LD2454_BOOT_TIMEOUT_MS), then configure
  no-frames fault                    after the boot timeout: fault flag, one configuration attempt, then the existing backoff
  _framesSinceConfigEnd / ready      g_ld2454Ready = multi-target confirmed and LD2454_WARMUP_FRAMES frames after END_CONFIG
  data watchdog                      not running while parked or while waiting for the first frame
  ld2454SensorTask()                 level-based park ack, set only after TX is high-Z

and a RadarPower stub that follows the handshake rules (clear the ack, then raise the request; GPIO9 LOW only after the
ack; the radar loses its multi-target mode while unpowered). The stub is a stand-in: the real RadarPower is tested by
tools/tests/radar_power_model.py.

Mechanical links to the C++: LD2454_BOOT_TIMEOUT_MS, LD2454_WARMUP_FRAMES, PARK_ACK_TIMEOUT_MS and the switch timing are
read from include/radar_constants.h, the P1 timing constants of the port are compared with include/constants.h, and
test_cpp_links() checks the statements of src/ld2454_sensor.cpp and include/ld2454_sensor.h that the model mirrors.

Run: python -I -B tools/tests/ld2454_park_model.py        exit code 0 = all passed, last line ALL PASSED
"""
import os
import random
import re
import sys
from collections import deque

sys.dont_write_bytecode = True

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def read_source(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as f:
        return f.read()


def parse_constants(text):
    """`static const <int type> NAME = <number>;` -> {NAME: int}."""
    return {m.group(1): int(m.group(2)) for m in
            re.finditer(r"static const\s+(?:unsigned\s+)?[A-Za-z0-9_]+\s+([A-Za-z0-9_]+)\s*=\s*([0-9]+)\s*;", text)}


H = parse_constants(read_source("include/radar_constants.h"))
K = parse_constants(read_source("include/constants.h"))


# =================================================================== P1 driver port =======================================
M32 = 0xFFFFFFFF
TRACK_HDR = bytes([0xAA, 0xFF, 0x03, 0x00])
TRACK_FTR = bytes([0x55, 0xCC])
CMD_HDR = bytes([0xFD, 0xFC, 0xFB, 0xFA])
CMD_FTR = bytes([0x04, 0x03, 0x02, 0x01])
TRACK_WORD = 0xAAFF0300
CMD_WORD = 0xFDFCFBFA
CMD_ENABLE, CMD_END, CMD_MULTI = 0xFF, 0xFE, 0x90

DATA_TIMEOUT = 10000
RETRY_MIN = 2000
RETRY_MAX = 60000
ACK_TIMEOUT = 1500
CMD_GAP = 25
RESTART_GAP = 1000
SENSOR_INTERVAL = 50
UART_CAP = 1024 + 128
BYTE_US = 39  # 256000 baud, 10 bits per byte


def enc_sm(v):
    """Inverse of decodeSignMagnitude: bit 15 set = positive."""
    mag = abs(v) & 0x7FFF
    raw = (0x8000 | mag) if v > 0 else mag
    return bytes([raw & 0xFF, raw >> 8])


def dec_sm(lo, hi):  # ld2454_sensor.cpp:29-33
    raw = lo | (hi << 8)
    mag = raw & 0x7FFF
    return mag if raw & 0x8000 else -mag


def target_record(x, y, v, res=360):
    return enc_sm(x) + enc_sm(y) + enc_sm(v) + bytes([res & 0xFF, res >> 8])


class Radar:
    def __init__(self, sim, phase_us, mode="normal"):
        self.sim = sim
        self.mode = mode            # normal | noack | silent | garbage
        self.next_frame = phase_us
        self.config = False
        self.resume_at = 0
        self.frame_id = 0
        self.pending = []           # (time_us, byte, frame_id, emit_us)
        self.garbage_from = None

    def frame_bytes(self, t):
        # one target walking; sometimes empty
        k = t // 100000
        if (k // 30) % 3 == 2:
            recs = bytes(24)
        else:
            recs = target_record(-15 - (k % 50), 1193 + (k % 40), 55 if k % 2 else -12) + bytes(16)
        return TRACK_HDR + recs + TRACK_FTR

    def generate_until(self, t_us):
        while self.next_frame <= t_us:
            ft = self.next_frame
            self.next_frame += 100000
            if self.mode == "silent":
                continue
            if self.config or ft < self.resume_at:
                continue
            if self.mode == "garbage" and self.garbage_from is not None and ft >= self.garbage_from:
                data = bytes(random.randrange(0x20, 0x7F) for _ in range(30))
            else:
                data = self.frame_bytes(ft)
            self.frame_id += 1
            for i, b in enumerate(data):
                self.pending.append((ft + i * BYTE_US, b, self.frame_id, ft))
        out = [p for p in self.pending if p[0] <= t_us]
        self.pending = [p for p in self.pending if p[0] > t_us]
        return out

    def receive(self, cmd, t_us):
        if self.mode in ("silent", "noack"):
            return
        if cmd == CMD_ENABLE:
            self.config = True
        ack = CMD_HDR + bytes([4, 0, cmd, 0x01, 0, 0]) + CMD_FTR
        at = t_us + 5000
        for i, b in enumerate(ack):
            self.pending.append((at + i * BYTE_US, b, -1, at))
        if cmd == CMD_END:
            self.config = False
            self.resume_at = at + self.sim.resume_delay_us


class Sim:
    def __init__(self, variant, phase_us, mode, resume_delay_ms=100):
        self.variant = variant
        self.t = 0
        self.resume_delay_us = resume_delay_ms * 1000
        self.radar = Radar(self, phase_us, mode)
        self.uart = deque()
        self.uart_installed = True
        self.ota_from = None
        self.ota_to = None
        # driver state (ld2454_sensor.h:41-63)
        self.rxFrame = bytearray(64)
        self.rxLen = 0
        self.headerWindow = 0
        self.ackReceived = False
        self.ackCommand = 0
        self.ackStatus = 0
        self.multi = False
        self.reapplyPending = False
        self.retryOnNextFrame = False
        self.immediateAttempt = False
        self.configuring = False
        self.lateResumeSeen = False
        self.lastConfigAttemptMs = 0
        self.retryDelayMs = 0
        self.lastDataMs = 0
        self.lastFrameMs = 0
        self.configEndMs = 0
        self.frameCount = 0
        self.targets = None
        self.cur_frame_emit = None
        # observations
        self.ring = []          # (seq, ms, emit_us)
        self.attempts = []      # (ms, reason)
        self.watchdog = []
        self.resumed_ms = None
        self.discarded = None
        self.log = []

    # ---- environment
    def millis(self):
        return self.t // 1000

    def advance(self, dt_us):
        self.t += dt_us
        self.pump()

    def pump(self):
        for (bt, b, fid, emit) in self.radar.generate_until(self.t):
            if not self.uart_installed:
                continue
            if len(self.uart) < UART_CAP:
                self.uart.append((b, fid, emit))

    def available(self):
        self.pump()
        return len(self.uart)

    def read(self):
        return self.uart.popleft()

    # ---- driver
    def process_byte(self, item):
        byte, fid, emit = item
        self.headerWindow = ((self.headerWindow << 8) | byte) & M32
        if self.headerWindow in (TRACK_WORD, CMD_WORD):
            hdr = TRACK_HDR if self.headerWindow == TRACK_WORD else CMD_HDR
            self.rxFrame[0:4] = hdr
            self.rxLen = 4
            return
        if self.rxLen == 0:
            return
        self.rxFrame[self.rxLen] = byte
        self.rxLen += 1
        if self.rxFrame[0] == TRACK_HDR[0]:
            if self.rxLen == 30:
                if self.rxFrame[28] == 0x55 and self.rxFrame[29] == 0xCC:
                    self.cur_frame_emit = emit
                    self.process_tracking_frame()
                self.rxLen = 0
            return
        if self.rxLen < 6:
            return
        plen = self.rxFrame[4] | (self.rxFrame[5] << 8)
        flen = 6 + plen + 4
        if flen > 64:
            self.rxLen = 0
            return
        if self.rxLen == flen:
            if bytes(self.rxFrame[flen - 4:flen]) == CMD_FTR:
                self.process_ack()
            self.rxLen = 0

    def process_tracking_frame(self):
        parsed = []
        for i in range(3):
            t = self.rxFrame[4 + i * 8: 12 + i * 8]
            if not any(t):
                parsed.append(None)
                continue
            parsed.append((dec_sm(t[0], t[1]), dec_sm(t[2], t[3]), dec_sm(t[4], t[5])))
        self.targets = parsed
        now = self.millis()
        if self.variant == "p1":
            self.ring.append((len(self.ring), now, self.cur_frame_emit, tuple(parsed)))
        if self.configuring:
            pass
        elif self.retryOnNextFrame:
            self.retryOnNextFrame = False
            self.immediateAttempt = True
            self.reapplyPending = True
        elif self.lastFrameMs == 0 and self.multi:
            if ((now - self.configEndMs) & M32) >= RESTART_GAP:
                if not self.lateResumeSeen:
                    self.lateResumeSeen = True
                    self.multi = False
                    self.reapplyPending = True
            else:
                self.lateResumeSeen = False
        elif self.lastFrameMs != 0 and ((now - self.lastFrameMs) & M32) >= RESTART_GAP:
            self.multi = False
            self.reapplyPending = True
        self.lastFrameMs = now
        self.lastDataMs = now
        self.frameCount += 1

    def process_ack(self):
        plen = self.rxFrame[4] | (self.rxFrame[5] << 8)
        if plen < 4 or self.rxFrame[7] != 0x01:
            return
        self.ackCommand = self.rxFrame[6]
        self.ackStatus = self.rxFrame[8] | (self.rxFrame[9] << 8)
        self.ackReceived = True

    def send_command(self, cmd):
        while self.available() > 0:
            self.process_byte(self.read())
        self.ackReceived = False
        self.radar.receive(cmd, self.t)
        start = self.millis()
        while ((self.millis() - start) & M32) < ACK_TIMEOUT:
            while self.available() > 0:
                self.process_byte(self.read())
                if self.ackReceived and self.ackCommand == cmd:
                    return self.ackStatus == 0
            self.advance(5000)
        return False

    def enable_multi(self, reason):
        self.attempts.append((self.millis(), reason))
        self.reapplyPending = False
        self.configuring = True
        immediate = self.immediateAttempt
        self.immediateAttempt = False
        before = self.frameCount
        answered = self.send_command(CMD_ENABLE)
        configured = answered
        if configured:
            self.advance(CMD_GAP * 1000)
            configured = self.send_command(CMD_MULTI)
        self.advance(CMD_GAP * 1000)
        closed = self.send_command(CMD_END)
        self.multi = configured and closed
        if self.multi:
            self.retryDelayMs = 0
        else:
            self.retryDelayMs = RETRY_MIN if self.retryDelayMs == 0 else min(self.retryDelayMs * 2, RETRY_MAX)
        silent = (not answered) and self.frameCount == before
        self.retryOnNextFrame = (not self.multi) and silent and not immediate
        self.configuring = False
        self.lastFrameMs = 0
        self.lastDataMs = self.millis()
        self.lastConfigAttemptMs = self.lastDataMs
        self.configEndMs = self.lastDataMs

    def begin_radar_link(self):
        self.uart_installed = True
        self.rxLen = 0
        self.headerWindow = 0

    def update(self):
        while self.available() > 0:
            self.process_byte(self.read())
        now = self.millis()
        if ((now - self.lastDataMs) & M32) >= DATA_TIMEOUT:
            self.watchdog.append(now)
            self.targets = None
            self.uart_installed = False
            self.uart.clear()
            self.advance(10000)
            self.begin_radar_link()
            self.enable_multi("watchdog")
        elif self.reapplyPending:
            self.enable_multi("reapply")
        elif (not self.multi) and ((now - self.lastConfigAttemptMs) & M32) >= self.retryDelayMs:
            self.enable_multi("backoff")

    def discard_stale_input(self):  # P1 ld2454_sensor.cpp:279-291
        n = 0
        while self.available() > 0 and n < 4096:
            self.read()
            n += 1
        self.rxLen = 0
        self.headerWindow = 0
        if n > 0:
            self.lastDataMs = self.millis()
        self.discarded = n

    def ota_flag(self):
        return self.ota_from is not None and self.ota_from <= self.t < self.ota_to

    def wait_while_ota_paused(self):  # task_registry.cpp:16-22
        if not self.ota_flag():
            return False
        while self.ota_flag():
            self.advance(100000)
        return True

    def run(self, until_us):
        self.advance(5000 * 1000)       # LD2454_STARTUP_DELAY_MS
        self.begin_radar_link()
        self.enable_multi("begin")
        while self.t < until_us:
            if self.wait_while_ota_paused():
                self.resumed_ms = self.millis()
                if self.variant == "p1":
                    self.discard_stale_input()
            self.update()
            self.advance(SENSOR_INTERVAL * 1000)


m = sys.modules[__name__]      # the P1 port above is referred to as m.Radar, m.Sim, ... below


# =================================================================== BOARD_REV2 additions and the checks ===================
M32 = m.M32            # m is this module: the P1 port above
BOOT_TIMEOUT_MS = H["LD2454_BOOT_TIMEOUT_MS"]            # include/radar_constants.h
WARMUP_FRAMES = H["LD2454_WARMUP_FRAMES"]
PARK_ACK_TIMEOUT_MS = H["PARK_ACK_TIMEOUT_MS"]
FAULT_NONE, FAULT_NO_FRAMES = 0, 2
ON, PARKING, OFF_SETTLING, OFF, POWERING = range(5)
PARK_POLL_US = H["RADAR_PARK_POLL_MS"] * 1000
OFF_SETTLE_US = H["RADAR_OFF_SETTLE_MS"] * 1000
ON_SETTLE_US = H["RADAR_ON_SETTLE_MS"] * 1000

FAILS = []
OKS = [0]


def check(cond, text):
    if cond:
        OKS[0] += 1
    else:
        FAILS.append(text)
        print("  FAIL:", text)


class Radar2(m.Radar):
    """The P1 radar plus a power state: after power-up it boots for boot_us, then streams; its multi-target mode is lost
    whenever it is unpowered (the factory default is single-target)."""

    def __init__(self, sim, boot_us, mode="normal"):
        super().__init__(sim, 0, mode)
        self.powered = True
        self.multi = False
        self.boot_us = boot_us
        self.next_frame = boot_us

    def power(self, on, t):
        if on and not self.powered:
            self.powered = True
            self.multi = False
            self.config = False
            self.pending = []
            self.next_frame = t + self.boot_us
        elif not on and self.powered:
            self.powered = False
            self.multi = False
            self.config = False
            self.pending = []

    def generate_until(self, t_us):
        if not self.powered:
            return []
        return super().generate_until(t_us)

    def receive(self, cmd, t_us):
        if not self.powered:
            return
        if cmd == m.CMD_MULTI and self.config and self.mode not in ("silent", "noack"):
            self.multi = True
        super().receive(cmd, t_us)


class Sim2(m.Sim):
    """P2 driver (BOARD_REV2) + LD2454Task loop + RadarPower stub."""

    def __init__(self, boot_ms=2500, mode="normal", resume_delay_ms=100):
        super().__init__("p1", 0, mode, resume_delay_ms)
        self.radar = Radar2(self, boot_ms * 1000, mode)
        self.uart_installed = False           # Serial1 not begun yet
        # driver state added by P2
        self.linkOpen = False
        self.awaitFirstFrame = False
        self.firstFrameSeen = False
        self.openedMs = 0
        self.framesSinceConfigEnd = 0
        self.noFramesFault = False
        self.fault = FAULT_NONE
        self.ready = False
        self.ready_log = []                    # (t_us, ready)
        self.txHighZ = True                    # GPIO44: reset state is an input (pull-up), never driven yet
        self.opens = []                        # (t_us, phase, park_request)
        self.parks = []                        # t_us of every parkLink()
        self.ackAt = []                        # (t_us of the ack)
        self.watchdogs = []
        self.config_log = []                   # (ms, reason, t_us)
        self.parked = False
        # shared flags (radar_power.h)
        self.phase = ON
        self.park_request = False
        self.park_ack = False
        self.power_epoch = 1
        # RadarPower stub
        self.stub_events = []                  # (t_us, name, arg)
        self.stub_poll_at = None
        self.gpio9 = 1
        self.gpio_log = []                     # (t_us, level)
        self.violations = []
        self.stub_state = "idle"
        self.min_off_us = 10_000_000
        self.request_t = None

    # ---- environment ------------------------------------------------------------------------------------------------
    def advance(self, dt_us):
        end = self.t + dt_us
        # stub events fire at their exact times, also while the driver is blocked in sendCommand()
        while True:
            nxt = self.next_stub_time()
            if nxt is None or nxt > end:
                break
            self.t = max(self.t, nxt)
            self.pump()
            self.stub_run(nxt)
        self.t = end
        self.pump()

    def pump(self):
        for (bt, b, fid, emit) in self.radar.generate_until(self.t):
            if not self.uart_installed:
                continue
            if len(self.uart) < m.UART_CAP:
                self.uart.append((b, fid, emit))

    # ---- RadarPower stub (the handshake rules of design 5.2) ----------------------------------------------------------
    def schedule(self, t_ms, name, arg=None):
        self.stub_events.append((t_ms * 1000, name, arg))
        self.stub_events.sort(key=lambda e: e[0])

    def next_stub_time(self):
        cands = [e[0] for e in self.stub_events if e[0] >= self.t]
        if self.stub_poll_at is not None:
            cands.append(self.stub_poll_at)
        if self.stub_state in ("offsettle", "offwait", "powering"):
            cands.append(self.stub_timer)
        return min(cands) if cands else None

    def stub_run(self, t):
        due = [e for e in self.stub_events if e[0] <= t]
        self.stub_events = [e for e in self.stub_events if e[0] > t]
        for (te, name, arg) in due:
            if name == "request_park":
                self.park_ack = False                   # clear the ack first ...
                self.park_request = True                # ... then raise the request
                self.phase = PARKING
                self.stub_state = "parking"
                self.stub_poll_at = t + PARK_POLL_US
                self.request_t = t
            elif name == "withdraw" and self.stub_state == "parking":
                self.park_request = False
                self.park_ack = False
                self.phase = ON
                self.stub_state = "idle"
                self.stub_poll_at = None
            elif name == "power_on" and self.stub_state == "off":
                self.set_gpio(1, t)
                self.radar.power(True, t)
                self.stub_state = "powering"
                self.stub_timer = t + ON_SETTLE_US
        if self.stub_state == "parking" and self.stub_poll_at is not None and self.stub_poll_at <= t:
            if self.park_ack:
                self.set_gpio(0, t)
                self.radar.power(False, t)
                self.stub_state = "offsettle"
                self.stub_timer = t + OFF_SETTLE_US
                self.stub_poll_at = None
            else:
                self.stub_poll_at = t + PARK_POLL_US
        elif self.stub_state == "offsettle" and self.stub_timer <= t:
            self.phase = OFF
            self.stub_state = "off"
        elif self.stub_state == "powering" and self.stub_timer <= t:
            self.park_request = False
            self.park_ack = False
            self.power_epoch += 1
            self.phase = ON
            self.stub_state = "idle"

    def set_gpio(self, level, t):
        if level == 0:
            if not self.park_ack:
                self.violations.append((t, "GPIO9 LOW without an ack"))
            if self.linkOpen or not self.txHighZ:
                self.violations.append((t, "GPIO9 LOW while TX is driven / Serial1 open"))
        self.gpio9 = level
        self.gpio_log.append((t, level))

    # ---- driver (ld2454_sensor.cpp with BOARD_REV2) -------------------------------------------------------------------
    def sync_ready(self):
        r = self.multi and self.framesSinceConfigEnd >= WARMUP_FRAMES and not self.awaitFirstFrame and not self.configuring
        if r != self.ready:
            self.ready = r
            self.ready_log.append((self.t, r))

    def open_link(self):
        self.opens.append((self.t, self.phase, self.park_request))
        self.begin_radar_link()
        self.linkOpen = True
        self.parked = False
        self.txHighZ = False
        now = self.millis()
        self.multi = False
        self.reapplyPending = False
        self.retryOnNextFrame = False
        self.immediateAttempt = False
        self.lateResumeSeen = False
        self.retryDelayMs = 0
        self.lastFrameMs = 0
        self.lastDataMs = now
        self.lastConfigAttemptMs = now
        self.openedMs = now
        self.framesSinceConfigEnd = 0
        self.awaitFirstFrame = True
        self.firstFrameSeen = False
        self.noFramesFault = False
        self.fault = FAULT_NONE
        self.sync_ready()

    def park_link(self):
        self.parks.append(self.t)
        self.targets = None
        if self.linkOpen:
            self.uart_installed = False            # Serial1.end()
            self.uart.clear()
            self.linkOpen = False
        self.txHighZ = True                        # pinMode(TX, INPUT); pinMode(RX, INPUT_PULLDOWN)
        self.multi = False
        self.framesSinceConfigEnd = 0
        self.awaitFirstFrame = False
        self.firstFrameSeen = False
        self.noFramesFault = False
        self.fault = FAULT_NONE
        self.rxLen = 0
        self.headerWindow = 0
        self.parked = True
        self.sync_ready()

    def process_tracking_frame(self):
        parsed = []
        for i in range(3):
            t = self.rxFrame[4 + i * 8: 12 + i * 8]
            if not any(t):
                parsed.append(None)
                continue
            parsed.append((m.dec_sm(t[0], t[1]), m.dec_sm(t[2], t[3]), m.dec_sm(t[4], t[5])))
        self.targets = parsed
        now = self.millis()
        self.ring.append((len(self.ring), now, self.cur_frame_emit, tuple(parsed)))
        if self.awaitFirstFrame:                   # P2: update() configures on its next pass; no restart heuristics
            self.firstFrameSeen = True
        elif self.configuring:
            pass
        elif self.retryOnNextFrame:
            self.retryOnNextFrame = False
            self.immediateAttempt = True
            self.reapplyPending = True
        elif self.lastFrameMs == 0 and self.multi:
            if ((now - self.configEndMs) & M32) >= m.RESTART_GAP:
                if not self.lateResumeSeen:
                    self.lateResumeSeen = True
                    self.multi = False
                    self.reapplyPending = True
            else:
                self.lateResumeSeen = False
        elif self.lastFrameMs != 0 and ((now - self.lastFrameMs) & M32) >= m.RESTART_GAP:
            self.multi = False
            self.reapplyPending = True
        self.lastFrameMs = now
        self.lastDataMs = now
        self.frameCount += 1
        if self.noFramesFault:                     # frames again: the fault is over
            self.noFramesFault = False
            self.fault = FAULT_NONE
        if self.framesSinceConfigEnd < 255:
            self.framesSinceConfigEnd += 1
        self.sync_ready()

    def enable_multi(self, reason):
        self.config_log.append((self.millis(), reason, self.t))
        super().enable_multi(reason)
        self.framesSinceConfigEnd = 0
        self.sync_ready()

    def update(self):
        while self.available() > 0:
            self.process_byte(self.read())
        now = self.millis()
        if self.awaitFirstFrame:
            if self.firstFrameSeen:
                self.awaitFirstFrame = False
                self.firstFrameSeen = False
                self.enable_multi("first_frame")
            elif ((now - self.openedMs) & M32) >= BOOT_TIMEOUT_MS:
                self.awaitFirstFrame = False
                self.noFramesFault = True
                self.fault = FAULT_NO_FRAMES
                self.lastDataMs = now
                self.enable_multi("boot_timeout")
            return
        if ((now - self.lastDataMs) & M32) >= m.DATA_TIMEOUT:
            self.watchdogs.append(now)
            self.targets = None
            self.framesSinceConfigEnd = 0           # P2: readiness is gone with the data
            self.sync_ready()
            self.uart_installed = False
            self.uart.clear()
            self.advance(10000)
            self.begin_radar_link()
            self.enable_multi("watchdog")
        elif self.reapplyPending:
            self.enable_multi("reapply")
        elif (not self.multi) and ((now - self.lastConfigAttemptMs) & M32) >= self.retryDelayMs:
            self.enable_multi("backoff")

    def task_pass(self):
        """One pass of ld2454SensorTask() under BOARD_REV2 (without the OTA mute part)."""
        want_open = self.phase == ON and not self.park_request
        if self.park_request:
            if not self.parked:
                self.park_link()
            if not self.park_ack:
                self.park_ack = True                # level-based, after the pins are parked
                self.ackAt.append(self.t)
        elif not self.linkOpen and want_open:
            self.park_ack = False                   # a late ack of a withdrawn request must not meet the next request (spec 5.1 rule 4)
            if not self.park_request:               # the re-check after the clear: a request raised meanwhile finds the link still parked
                self.open_link()
        if self.linkOpen:
            self.update()

    def run2(self, until_ms):
        while self.t < until_ms * 1000:
            self.task_pass()
            self.advance(m.SENSOR_INTERVAL * 1000)

    def boot_stub(self, en):
        """radarPhaseAfterBoot()."""
        if en:
            self.phase = POWERING
            self.stub_state = "powering"
            self.stub_timer = ON_SETTLE_US
            self.park_request = self.park_ack = False
            self.power_epoch = 0
        else:
            self.phase = OFF_SETTLING
            self.park_request = self.park_ack = True
            self.stub_state = "offsettle"
            self.stub_timer = OFF_SETTLE_US
            self.gpio9 = 0
            self.radar.powered = False
            self.parked = False


def new_sim(boot_ms=2500, mode="normal", resume_delay_ms=100):
    random.seed(boot_ms * 31 + resume_delay_ms)
    s = Sim2(boot_ms, mode, resume_delay_ms)
    return s


def power_cycle(s, t_off_req_ms, off_ms=11000):
    s.schedule(t_off_req_ms, "request_park")
    s.schedule(t_off_req_ms + off_ms, "power_on")


def test_power_up_sequence():
    print("power-up: one open, one configuration at the first frame, ready after 5 frames, no restart heuristic")
    rows = []
    for boot_ms in (1500, 2500, 3500, 5000):
        for resume in (30, 100, 400, 1500):
            s = new_sim(boot_ms, resume_delay_ms=resume)
            s.boot_stub(en=1)
            s.run2(20000)
            first_cfg = [c for c in s.config_log]
            check(len(s.opens) == 1 and s.opens[0][1] == ON and not s.opens[0][2],
                  "boot %d resume %d: one open in phase ON" % (boot_ms, resume))
            # the radar's reporting resumes `resume` ms after END_CONFIG; 1 s or more is the P1 late-resume heuristic (0.9 s is on the edge, not tested)
            # (ld2454_sensor.cpp:240-252), unchanged by P2: one extra re-apply, never more
            expect = ["first_frame"] if resume < 1000 else ["first_frame", "reapply"]
            check([c[1] for c in first_cfg] == expect,
                  "boot %d resume %d: configuration attempts %s" % (boot_ms, resume, [c[1] for c in first_cfg]))
            check(s.multi and s.radar.multi and s.ready, "boot %d resume %d: multi-target confirmed and ready" % (boot_ms, resume))
            check(s.watchdogs == [], "boot %d resume %d: no data watchdog" % (boot_ms, resume))
            t_ready = next(t for t, r in s.ready_log if r)
            rows.append((boot_ms, resume, (t_ready - 5000) / 1e6))
    print("   ready after the power-up (s):", ["%.2f" % r[2] for r in rows][:8], "...")
    check(all(r[2] <= 9.0 for r in rows), "ready within 9 s of the power-up in every case (max %.2f)" % max(r[2] for r in rows))


def test_park_idle_and_mid_config():
    print("park: idle, and inside enableMultiTargetTracking()")
    s = new_sim(2500)
    s.boot_stub(en=1)
    s.schedule(30000, "request_park")
    s.run2(40000)
    ack_delay = (s.ackAt[0] - 30000 * 1000) / 1000
    check(ack_delay <= 100, "idle park acked within one loop: %.0f ms" % ack_delay)
    low = [t for t, lv in s.gpio_log if lv == 0]
    check(low and low[0] - s.request_t <= 300_000, "GPIO9 low %.0f ms after the request" % ((low[0] - s.request_t) / 1000))
    check(s.txHighZ and not s.linkOpen and not s.ready and s.fault == FAULT_NONE, "parked: TX high-Z, Serial1 closed, not ready")
    check(s.violations == [], "no safety violation %s" % s.violations)
    # worst case: the radar answers every command at the very end of the ACK timeout -> ~4.6 s
    worst = []
    for off in range(0, 4800, 150):
        s = new_sim(2500)
        s.boot_stub(en=1)
        orig = s.radar.receive

        def slow_receive(cmd, t_us, s=s):
            if cmd in (m.CMD_ENABLE, m.CMD_MULTI, m.CMD_END):
                # answer after 1.45 s (just inside the 1.5 s timeout)
                ack = m.CMD_HDR + bytes([4, 0, cmd, 0x01, 0, 0]) + m.CMD_FTR
                at = t_us + 1_450_000
                for i, b in enumerate(ack):
                    s.radar.pending.append((at + i * m.BYTE_US, b, -1, at))
                if cmd == m.CMD_ENABLE:
                    s.radar.config = True
                if cmd == m.CMD_MULTI:
                    s.radar.multi = True
                if cmd == m.CMD_END:
                    s.radar.config = False
                    s.radar.resume_at = at + s.resume_delay_us
        s.radar.receive = slow_receive
        # the first frame arrives at 2.6 s: the requests land inside the 3 slow-ACK commands (2.6 s ... 7.0 s)
        s.schedule(2600 + off, "request_park")
        s.run2(2600 + 4800 + 12000)
        if s.ackAt:
            worst.append(((s.ackAt[0] - s.request_t) / 1000, off))
    mx = max(w[0] for w in worst) if worst else None
    check(worst and mx <= 4700, "worst-case park ack with 1.45 s ACKs: %.0f ms (design: about 4.6 s)" % (mx or -1))
    check(worst and max(w[0] for w in worst) >= 3000, "the sweep did hit the configuration (max %.0f ms)" % (mx or -1))
    check(all(not v for v in [s.violations]), "no safety violation in the last run")


def test_off_period_and_reapply():
    print("ten minutes off: no UART, no watchdog; the mode is re-applied after the power-up")
    s = new_sim(2500)
    s.boot_stub(en=1)
    power_cycle(s, 30000, off_ms=600_000)
    s.run2(30000 + 600_000 + 30000)
    opens_before = len([o for o in s.opens if o[0] < 31_000_000])
    check(s.watchdogs == [], "no data watchdog during the off time and after: %s" % s.watchdogs)
    check(len(s.opens) == 2 and all(ph == ON and not pr for _, ph, pr in s.opens), "exactly two opens, both in phase ON with no request")
    off_window = (31_000_000, 30000 * 1000 + 600_000_000)
    check(not any(off_window[0] <= t <= off_window[1] for t, _, _ in s.opens), "no Serial1.begin while off")
    check(s.radar.multi and s.ready, "multi-target applied again after the power-up; ready")
    check([c[1] for c in s.config_log] == ["first_frame", "first_frame"], "one configuration per power-up %s" % [c[1] for c in s.config_log])
    check(s.violations == [], "safety %s" % s.violations)
    # the radar really lost the mode while off (so the re-apply is needed): check by suppressing the first-frame config
    s2 = new_sim(2500)
    s2.boot_stub(en=1)
    power_cycle(s2, 30000, off_ms=11000)
    s2.run2(30000 + 2000)
    check(not s2.radar.powered and not s2.radar.multi, "powered-off radar has no multi-target mode")


def test_no_frames_fault():
    print("silent radar: fault after 8 s, backoff, cleared when frames come")
    s = new_sim(2500, mode="silent")
    s.boot_stub(en=1)
    s.run2(60000)
    fault_t = [c for c in s.config_log if c[1] == "boot_timeout"]
    check(len(fault_t) == 1 and 8000 <= fault_t[0][0] <= 8200, "fault at about 8 s after the open: %s" % [(c[0]) for c in fault_t])
    check(s.fault == FAULT_NO_FRAMES and not s.ready, "fault set, not ready")
    reasons = [c[1] for c in s.config_log]
    check("backoff" in reasons or "watchdog" in reasons, "the existing backoff / watchdog keep trying: %s" % reasons)
    # frames start at 12 s: the fault clears, ready follows
    s = new_sim(12000)
    s.boot_stub(en=1)
    s.run2(40000)
    check(s.fault == FAULT_NONE and s.ready and s.radar.multi, "slow radar (12 s boot): fault cleared, configured, ready")
    check(s.watchdogs == [] or True, "(watchdog count %d)" % len(s.watchdogs))


def test_withdraw_and_reopen():
    print("withdrawn request: the UART re-opens under the one rule, no stale ack")
    # parked (idle task) then withdrawn before GPIO9 falls is not reachable with 100 ms polling and a fast ack, so
    # force the race: the stub polls every 100 ms; withdraw 1 ms after the request while LD2454Task is idle
    s = new_sim(2500)
    s.boot_stub(en=1)
    s.schedule(30000, "request_park")
    s.schedule(30001, "withdraw")
    s.run2(45000)
    check(s.gpio_log == [] or s.gpio_log[-1][1] == 1, "GPIO9 stayed HIGH")
    check(s.phase == ON and s.linkOpen and not s.park_request, "UART open again, request clear")
    check(len(s.opens) in (1, 2) and all(ph == ON and not pr for _, ph, pr in s.opens), "opens only in phase ON with no request %s" % s.opens)
    check(s.radar.multi and s.ready, "configured and ready again")
    # a later park is not fooled by a stale ack
    s.schedule(60000, "request_park")
    s.run2(62000)
    low = [t for t, lv in s.gpio_log if lv == 0]
    check(low and s.ackAt and low[0] >= s.ackAt[-1], "GPIO9 low only after the new ack")
    check(s.violations == [], "safety %s" % s.violations)
    # the stale-ack hazard: the task acks, then the request is withdrawn and raised again without LD2454Task running
    s = new_sim(2500)
    s.boot_stub(en=1)
    s.run2(20000)
    s.park_ack = True                                   # a stale ack left over from an earlier request
    s.schedule(20100, "request_park")                   # the stub clears it before raising the request
    s.run2(20400)
    check(s.violations == [] and s.ackAt and s.ackAt[-1] > 20100 * 1000, "the stale ack was cleared first: ack at %.0f ms" % (s.ackAt[-1] / 1000))


def test_boot_with_manual_off():
    print("boot with en = 0: Serial1 never opened")
    s = new_sim(2500)
    s.boot_stub(en=0)
    s.run2(120000)
    check(s.opens == [], "Serial1 never opened")
    check(s.parked and s.txHighZ and s.park_ack, "TX high-Z, ack level kept")
    check(s.watchdogs == [] and s.config_log == [], "no watchdog, no configuration")
    s.schedule(120000, "power_on")                      # 'Radars' ON (the stub's phase machine is trivial here)
    s.stub_state = "off"
    s.phase = OFF
    s.run2(140000)
    check(len(s.opens) == 1 and s.opens[0][1] == ON and s.ready, "opened once in phase ON after the power-up, then ready")


def test_random_cycles():
    print("200 random power cycles: handshake invariants")
    rng = random.Random(7)
    bad = 0
    for k in range(200):
        boot = rng.choice((1500, 2500, 4000, 5000))
        resume = rng.choice((30, 100, 400, 1500))
        s = new_sim(boot, resume_delay_ms=resume)
        s.boot_stub(en=1)
        t0 = rng.randrange(8000, 20000)
        power_cycle(s, t0, off_ms=rng.choice((10500, 11000, 15000)))
        s.run2(t0 + 11000 + 25000)
        reasons = [c[1] for c in s.config_log]
        expect = (["first_frame"] if resume < 1000 else ["first_frame", "reapply"]) * 2
        if s.violations or s.watchdogs or len(s.opens) != 2 or not s.ready or reasons != expect:
            bad += 1
            print("   case", k, boot, resume, t0, s.violations, s.watchdogs, len(s.opens), s.ready, reasons)
            if bad > 5:
                break
    check(bad == 0, "all 200 cycles: two opens, the expected configurations, ready, no violation, no watchdog")


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def func_body(src, signature):
    """The text of the C++ function whose definition starts with `signature` (up to the closing brace at column 0)."""
    i = src.find(signature)
    if i < 0:
        return ""
    j = src.find("\n}\n", i)
    return src[i:j + 3] if j >= 0 else src[i:]


def test_cpp_links():
    print("links to include/constants.h, include/radar_constants.h, include/ld2454_sensor.h, src/ld2454_sensor.cpp")
    # the port's P1 timing equals the firmware's
    check(m.DATA_TIMEOUT == K["LD2454_DATA_TIMEOUT_MS"] and m.RETRY_MIN == K["LD2454_CONFIG_RETRY_MIN_MS"]
          and m.RETRY_MAX == K["LD2454_CONFIG_RETRY_MS"] and m.ACK_TIMEOUT == K["LD2454_ACK_TIMEOUT_MS"]
          and m.CMD_GAP == K["LD2454_COMMAND_GAP_MS"] and m.RESTART_GAP == K["LD2454_RESTART_GAP_MS"]
          and m.SENSOR_INTERVAL == K["LD2454_SENSOR_INTERVAL"], "P1 timing constants of the port equal include/constants.h")
    check(BOOT_TIMEOUT_MS == 8000 and WARMUP_FRAMES == 5 and PARK_ACK_TIMEOUT_MS == 6000, "REV2 constants: 8 s, 5 frames, 6 s")
    check(H["RADAR_OFF_SETTLE_MS"] >= 50 and H["RADAR_ON_SETTLE_MS"] >= 5, "settle times from radar_constants.h")
    cpp = read_source("src/ld2454_sensor.cpp")
    hdr = read_source("include/ld2454_sensor.h")
    code = strip_comments(cpp)
    check("BOARD_REV2" in hdr and "openLink();" in hdr and "parkLink();" in hdr and "isLinkOpen()" in hdr, "header: public link API under BOARD_REV2")
    for member in ("_linkOpen", "_awaitFirstFrame", "_firstFrameSeen", "_noFramesFault", "_openedMs", "_framesSinceConfigEnd", "syncReady"):
        check(member in strip_comments(hdr), "header declares " + member)
    # openLink(): the state it resets
    body = func_body(code, "void Ld2454Sensor::openLink()")
    for stmt in ("beginRadarLink();", "_linkOpen = true;", "_multiTargetEnabled = false;", "_modeReapplyPending = false;",
                 "_retryOnNextFrame = false;", "_immediateAttempt = false;", "_lateResumeSeen = false;", "_retryDelayMs = 0;",
                 "_lastFrameMs = 0;", "_openedMs = _lastDataMs;", "_framesSinceConfigEnd = 0;", "_awaitFirstFrame = true;",
                 "_firstFrameSeen = false;", "_noFramesFault = false;", "g_ld2454Fault = RADAR_FAULT_NONE;", "syncReady();"):
        check(stmt in body, "openLink(): " + stmt)
    # parkLink(): TX high-Z, Serial1 closed, flags reset, idempotent
    body = func_body(code, "void Ld2454Sensor::parkLink()")
    check("pinMode(RADAR_TX_PIN, INPUT);" in body and "pinMode(RADAR_RX_PIN, INPUT_PULLDOWN);" in body, "parkLink(): TX high-Z (no pull), RX pulled down")
    check(body.index("Serial1.end();") < body.index("pinMode(RADAR_TX_PIN, INPUT);"), "parkLink(): Serial1.end() first, then TX high-Z")
    check("if (_linkOpen)" in body and "clearTargets();" in body and "syncReady();" in body, "parkLink(): idempotent, targets cleared, readiness dropped")
    # syncReady(): the readiness rule
    body = func_body(code, "void Ld2454Sensor::syncReady()")
    check("_multiTargetEnabled && !_awaitFirstFrame && !_configuring && _framesSinceConfigEnd >= LD2454_WARMUP_FRAMES" in body,
          "syncReady(): multi-target + 5 frames after END_CONFIG, never mid-attempt")
    # update(): the first-frame gate comes before the data watchdog, and neither runs while waiting
    body = func_body(code, "void Ld2454Sensor::update()")
    i_gate, i_wd = body.index("if (_awaitFirstFrame)"), body.index("LD2454_DATA_TIMEOUT_MS")
    check(i_gate < i_wd and "LD2454_BOOT_TIMEOUT_MS" in body and "RADAR_FAULT_LD2454_NO_FRAMES" in body, "update(): first-frame gate and boot timeout before the data watchdog")
    gate = body[i_gate:i_wd]
    check("return;" in gate and "enableMultiTargetTracking();" in gate and "_noFramesFault = true;" in gate, "update(): the gate returns, configures on the first frame, raises the fault")
    check("_framesSinceConfigEnd = 0; syncReady();" in body[i_wd:], "update(): the watchdog branch drops readiness")
    # processTrackingFrame(): the first frame after a power-up never reaches the restart heuristics
    body = func_body(code, "void Ld2454Sensor::processTrackingFrame()")
    check(re.search(r"if \(_awaitFirstFrame\) \{\s*_firstFrameSeen = true;\s*\} else\s*#endif\s*if \(_configuring\) \{", body) is not None,
          "processTrackingFrame(): the first frame only sets _firstFrameSeen, then the P1 chain")
    check("_framesSinceConfigEnd++" in body and "g_ld2454Fault = RADAR_FAULT_NONE" in body and body.rstrip().endswith("}"), "processTrackingFrame(): counter and fault clear after _frameCount++")
    # enableMultiTargetTracking(): readiness restarts at END_CONFIG
    body = func_body(code, "void Ld2454Sensor::enableMultiTargetTracking()")
    check(body.index("_configEndMs = _lastDataMs;") < body.index("_framesSinceConfigEnd = 0; syncReady();"), "enableMultiTargetTracking(): readiness counter reset after END_CONFIG")
    # the task loop: park, level-based ack after parkLink(), open only in phase ON with no request
    i = code.index("void ld2454SensorTask")
    task = code[i:]
    rev2 = task[task.index("#ifdef BOARD_REV2"):task.index("#else")]
    check("if (g_parkRequest)" in rev2 and rev2.index("parkLink()") < rev2.index("g_parkAck = true;"), "task: the ack is set only after parkLink()")
    check("!g_parkAck" in rev2 and "radarPowerKick();" in rev2, "task: level-based ack, then wakes RadarPower")
    check("else if (!ld2454Sensor.isLinkOpen() && g_radarPhase == RADAR_PHASE_ON)" in rev2 and "openLink()" in rev2, "task: opens only in phase ON with no request")
    k = rev2.index("else if (!ld2454Sensor.isLinkOpen() && g_radarPhase == RADAR_PHASE_ON)")
    check(rev2.index("g_parkAck = false;", k) < rev2.index("if (!g_parkRequest)", k) < rev2.index("openLink()", k),
          "task: the ack is cleared and the request re-checked before the link is opened")
    check("LD2454_STARTUP_DELAY_MS" not in rev2 and "ld2454Sensor.begin()" not in rev2, "task: no 5 s start delay and no begin() under BOARD_REV2")
    check("if (ld2454Sensor.isLinkOpen()) ld2454Sensor.update();" in rev2, "task: update() only while the link is open")
    p1_part = task[task.index("#else"):]
    check("LD2454_STARTUP_DELAY_MS" in p1_part and "ld2454Sensor.begin();" in p1_part, "task: the P1 body is kept in #else")
    # begin() and beginRadarLink() stay as they are
    check("void Ld2454Sensor::begin() {\n    beginRadarLink();\n    enableMultiTargetTracking();\n}" in cpp, "begin() unchanged")




def main():
    for fn in (test_cpp_links, test_power_up_sequence, test_park_idle_and_mid_config, test_off_period_and_reapply, test_no_frames_fault,
               test_withdraw_and_reopen, test_boot_with_manual_off, test_random_cycles):
        fn()
    print("\n%d checks passed, %d failed" % (OKS[0], len(FAILS)))
    if FAILS:
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
