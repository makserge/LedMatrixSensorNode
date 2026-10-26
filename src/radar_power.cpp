// Board revision 2 only (-D BOARD_REV2): the radar power switch on GPIO9 and the night schedule (P2 spec, Section 4).
// Compiles to nothing without BOARD_REV2: on the current board GPIO9 is the power / LED-off button.
#ifdef BOARD_REV2

#include "radar_power.h"
#include <Preferences.h>
#include <driver/gpio.h>
#include <esp_task_wdt.h>
#include <esp_timer.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include "pins.h"
#include "constants.h"
#include "ntp_clock.h"
#include "task_registry.h"

// ---- shared flags (radar_power.h; writers in spec 4.2) -------------------------------------------------------------
// The boot values are set again by radarPowerEarlyInit() before any other task exists.
std::atomic<uint8_t>  g_radarPhase{RADAR_PHASE_POWERING};
std::atomic<bool>     g_radarPowered{false};
std::atomic<uint32_t> g_powerEpoch{0};
std::atomic<uint32_t> g_poweredAtMs{0};
std::atomic<bool>     g_parkRequest{false};
std::atomic<bool>     g_parkAck{false};
std::atomic<bool>     g_ld2454Ready{false};
std::atomic<uint8_t>  g_ld2454Fault{RADAR_FAULT_NONE};
std::atomic<bool>     g_stateDirty{false};

// ---- pure helpers (spec Appendix D; tested on the PC by tools/tests/radar_power_model.py) ---------------------------
int radarParseOnOff(const char* text) {
    if (!text) return -1;
    if (strcmp(text, "1") == 0) return 1;
    if (strcmp(text, "0") == 0) return 0;
    if (strcasecmp(text, "on") == 0) return 1;
    if (strcasecmp(text, "off") == 0) return 0;
    return -1;                                         // "", "auto", "2", " 1", "true", ... are refused
}

static bool isDigit(char c) { return c >= '0' && c <= '9'; }

bool radarParseTimeOfDay(const char* text, uint16_t* minutes) {
    if (!text || !minutes) return false;
    const char* p = text;
    int h = 0, nh = 0;
    while (isDigit(*p) && nh < 3) { h = h * 10 + (*p - '0'); ++p; ++nh; }
    if (nh < 1 || nh > 2 || *p != ':') return false;  // H or HH, then a colon
    ++p;
    if (!isDigit(p[0]) || !isDigit(p[1])) return false;
    const int m = (p[0] - '0') * 10 + (p[1] - '0');
    p += 2;
    int sec = 0;
    if (*p == ':') {                                   // optional seconds, validated and dropped
        ++p;
        if (!isDigit(p[0]) || !isDigit(p[1])) return false;
        sec = (p[0] - '0') * 10 + (p[1] - '0');
        p += 2;
    }
    if (*p != '\0' || h > 23 || m > 59 || sec > 59) return false;   // no trailing text, no spaces, no sign
    *minutes = (uint16_t)(h * 60 + m);
    return true;
}

void radarFormatHms(uint16_t minutes, char out[9]) {
    snprintf(out, 9, "%02u:%02u:00", (unsigned)(minutes / 60 % 24), (unsigned)(minutes % 60));
}

void radarFormatHm(int16_t minutes, char out[6]) {
    if (minutes < 0) { out[0] = '\0'; return; }
    snprintf(out, 6, "%02u:%02u", (unsigned)(minutes / 60 % 24), (unsigned)(minutes % 60));
}

const char* radarStateName(uint8_t s) {
    static const char* const n[] = {"on", "night_off", "forced_on", "forced_off", "failsafe_on"};
    return s < 5 ? n[s] : n[0];
}
const char* radarReasonName(uint8_t r) {
    static const char* const n[] = {"schedule", "manual", "failsafe", "ha_unknown", "ota", "fault", "throttle", "pending"};
    return r < 8 ? n[r] : n[0];
}
const char* radarFaultName(uint8_t f) {
    static const char* const n[] = {"", "park_timeout", "ld2454_no_frames"};
    return f < 3 ? n[f] : n[0];
}
const char* radarHaName(uint8_t h) {
    static const char* const n[] = {"unknown", "online", "offline"};
    return h < 3 ? n[h] : n[0];
}
const char* radarPhaseName(uint8_t p) {
    static const char* const n[] = {"ON", "PARKING", "OFF_SETTLING", "OFF", "POWERING"};
    return p < 5 ? n[p] : n[0];
}

namespace {

// start == end: never; start < end: start <= t < end; start > end (crosses midnight): t >= start || t < end
bool inWindow(uint16_t t, uint16_t start, uint16_t end) {
    if (start == end) return false;
    if (start < end) return t >= start && t < end;
    return t >= start || t < end;
}

// elapsed time with the codebase's wrap rule
inline uint32_t elapsed(uint32_t now, uint32_t start) { return (uint32_t)(now - start); }

// The wrap rule holds for durations below 49.7 days, but some stamps stay unchanged as long as their state lasts (the
// power-up time while the radars stay ON, the HA-lost time without HA, the OFF time after a manual OFF). A stamp older
// than RADAR_STAMP_MAX_AGE_MS moves forward to exactly that age: every ">= X" test on it (X <= 24 h) stays true and the
// wrap moves out by another 37 days. A stamp a moment AHEAD of 'now' (another task stored it after 'now' was sampled)
// reads as an age near 2^32: that is not old and stays as it is.
inline uint32_t keepOld(uint32_t now, uint32_t stamp) {
    const uint32_t age = elapsed(now, stamp);
    return (age > RADAR_STAMP_MAX_AGE_MS && age < 0x80000000u) ? (uint32_t)(now - RADAR_STAMP_MAX_AGE_MS) : stamp;
}

// minute of day of the moment deltaMs from now (whole seconds, then floored to the minute); the pending-change time of 'next'
uint16_t localAfter(uint16_t nowMin, uint8_t nowSec, uint32_t deltaMs) {
    return (uint16_t)((((uint32_t)nowMin * 60u + nowSec + (deltaMs + 999u) / 1000u) / 60u) % 1440u);
}

inline uint32_t minU32(uint32_t a, uint32_t b) { return a < b ? a : b; }
inline uint32_t maxU32(uint32_t a, uint32_t b) { return a > b ? a : b; }

// ---- settings and NVS (spec 4.3) -----------------------------------------------------------------------------------
struct Settings {
    uint8_t  en, sched, offl, legacy;
    uint16_t ns, ne;
};

const char* const NVS_KEYS[RADAR_CMD_COUNT] = {"en", "sched", "ns", "ne", "offl", "legacy"};   // index = RadarCmd

Settings defaultSettings() {
    Settings s;
    s.en = 1; s.sched = 0; s.offl = 0; s.legacy = 0;
    s.ns = RADAR_NIGHT_START_DEFAULT_MIN;
    s.ne = RADAR_NIGHT_END_DEFAULT_MIN;
    return s;
}

// Used by early init and nowhere else. A missing namespace (fresh flash) reads as the defaults.
Settings loadSettings() {
    const Settings def = defaultSettings();
    Settings s = def;
    Preferences prefs;
    if (prefs.begin(RADAR_NVS_NAMESPACE, true)) {                  // read-only
        s.en     = prefs.getUChar(NVS_KEYS[RADAR_CMD_ENABLE], def.en);
        s.sched  = prefs.getUChar(NVS_KEYS[RADAR_CMD_SCHEDULE], def.sched);
        s.ns     = prefs.getUShort(NVS_KEYS[RADAR_CMD_NIGHT_START], def.ns);
        s.ne     = prefs.getUShort(NVS_KEYS[RADAR_CMD_NIGHT_END], def.ne);
        s.offl   = prefs.getUChar(NVS_KEYS[RADAR_CMD_SCHED_OFFLINE], def.offl);
        s.legacy = prefs.getUChar(NVS_KEYS[RADAR_CMD_LEGACY], def.legacy);
        prefs.end();
    }
    if (s.en > 1)      s.en = def.en;                              // corrupt data falls back to the default
    if (s.sched > 1)   s.sched = def.sched;
    if (s.offl > 1)    s.offl = def.offl;
    if (s.legacy > 1)  s.legacy = def.legacy;
    if (s.ns >= 1440)  s.ns = def.ns;
    if (s.ne >= 1440)  s.ne = def.ne;
    return s;
}

int32_t settingValue(const Settings& s, RadarCmd cmd) {
    switch (cmd) {
        case RADAR_CMD_ENABLE:        return s.en;
        case RADAR_CMD_SCHEDULE:      return s.sched;
        case RADAR_CMD_NIGHT_START:   return s.ns;
        case RADAR_CMD_NIGHT_END:     return s.ne;
        case RADAR_CMD_SCHED_OFFLINE: return s.offl;
        case RADAR_CMD_LEGACY:        return s.legacy;
        default:                      return 0;
    }
}

void applySetting(Settings& s, RadarCmd cmd, int32_t v) {
    switch (cmd) {
        case RADAR_CMD_ENABLE:        s.en = (uint8_t)v; break;
        case RADAR_CMD_SCHEDULE:      s.sched = (uint8_t)v; break;
        case RADAR_CMD_NIGHT_START:   s.ns = (uint16_t)v; break;
        case RADAR_CMD_NIGHT_END:     s.ne = (uint16_t)v; break;
        case RADAR_CMD_SCHED_OFFLINE: s.offl = (uint8_t)v; break;
        case RADAR_CMD_LEGACY:        s.legacy = (uint8_t)v; break;
        default: break;
    }
}

// One NVS write; true when stored. Every commit blanks the panel for milliseconds, so callers write on change only.
bool storeSetting(RadarCmd cmd, int32_t v) {
    Preferences prefs;
    if (!prefs.begin(RADAR_NVS_NAMESPACE, false)) return false;    // read-write
    size_t written;
    if (cmd == RADAR_CMD_NIGHT_START || cmd == RADAR_CMD_NIGHT_END) written = prefs.putUShort(NVS_KEYS[cmd], (uint16_t)v);
    else                                                              written = prefs.putUChar(NVS_KEYS[cmd], (uint8_t)v);
    prefs.end();
    return written != 0;
}

// ---- what the clock and the switches want (spec 4.5) ---------------------------------------------------------------
struct Decision {
    bool        off;          // GPIO9 should be LOW
    RadarState  state;        // the published state if this decision were applied now
    RadarReason reason;
    bool        automatic;    // an automatic OFF (NIGHT_OFF): throttled; a manual OFF never is
};

Decision decide(const Settings& s, bool timeValid, uint16_t nowMin, RadarHaState ha, uint32_t haLostMs, uint32_t nowMs) {
    if (!s.en)    return {true,  RADAR_ST_FORCED_OFF,  RADAR_RS_MANUAL,   false};
    if (!s.sched) return {false, RADAR_ST_FORCED_ON,   RADAR_RS_MANUAL,   false};
    if (!timeValid) return {false, RADAR_ST_FAILSAFE_ON, RADAR_RS_FAILSAFE, false};
    if (!inWindow(nowMin, s.ns, s.ne)) return {false, RADAR_ST_ON, RADAR_RS_SCHEDULE, false};
    const bool offline = ha != RADAR_HA_ONLINE && elapsed(nowMs, haLostMs) >= RADAR_OFFLINE_GRACE_MS;
    if (offline && !s.offl) {
        return {false, RADAR_ST_FAILSAFE_ON, ha == RADAR_HA_UNKNOWN ? RADAR_RS_HA_UNKNOWN : RADAR_RS_FAILSAFE, false};
    }
    return {true, RADAR_ST_NIGHT_OFF, RADAR_RS_SCHEDULE, true};
}

// ---- shared with other tasks, guarded by s_mux (spec 4.2): pending commands, HA state, the stored status ----------------
portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
TaskHandle_t s_task = nullptr;
int32_t      s_pendingValue[RADAR_CMD_COUNT];
uint8_t      s_pendingMask = 0;
RadarHaState s_haState = RADAR_HA_UNKNOWN;
uint32_t     s_haLostMs = 0;
RadarStatus  s_status;

struct HistEntry {
    uint64_t up;       // 64-bit uptime ms of the power-up
    uint64_t down;     // ... of the power-down; valid when closed
    bool     closed;
};

bool sameStatus(const RadarStatus& a, const RadarStatus& b) {   // every field except the moving on24x10
    return a.sinceMs == b.sinceMs && a.ns == b.ns && a.ne == b.ne && a.nextMin == b.nextMin && a.ups24 == b.ups24 &&
           a.parkTimeouts == b.parkTimeouts && a.en == b.en && a.sched == b.sched && a.offl == b.offl &&
           a.legacy == b.legacy && a.phase == b.phase && a.state == b.state && a.reason == b.reason &&
           a.fault == b.fault && a.ha == b.ha && a.powered == b.powered && a.observing == b.observing &&
           a.obs412 == b.obs412 && a.thr == b.thr;
}

void setRadarPin(bool on) {                          // the only writer of GPIO9
    gpio_set_level((gpio_num_t)RADAR_PWR_EN_PIN, on ? 1 : 0);
}

// ---- the RadarPower state machine (spec 4.6-4.10; the reference model is radar_power_ref.py) ----------------------------
class RadarPower {
public:
    void begin(const Settings& s, uint32_t nowMs, uint64_t nowMs64);
    uint32_t pass(uint64_t nowMs64);                 // one pass; returns the wait in ms until the next one (>= 1)
    void afterOtaPause(uint32_t nowMs);              // the upload does not count towards PARK_ACK_TIMEOUT_MS

private:
    uint32_t applyPending(uint64_t now64);           // returns the ms until a deferred store is due, 0xFFFFFFFF = none
    void refreshStamps(uint32_t now);
    uint32_t stepPhase(const Decision& dec, uint32_t now, uint64_t now64);
    uint32_t nextWait(const Decision& dec, uint32_t now) const;
    void updateStatus(const Decision& dec, bool valid, uint16_t nowMin, uint8_t nowSec, uint32_t now, uint64_t now64);

    void pruneHistory(uint64_t now64);
    uint16_t powerupsLastHour(uint64_t now64) const;
    bool oldestPowerupInHour(uint64_t now64, uint64_t* up) const;
    void recordPowerUp(uint64_t now64);
    void recordPowerDown(uint64_t now64);
    uint16_t onShareX10(uint64_t now64) const;
    uint16_t ups24(uint64_t now64) const;

    Settings   s_;
    RadarHaState ha_ = RADAR_HA_UNKNOWN;             // sampled once per pass
    RadarStatus cur_;
    HistEntry  hist_[RADAR_HISTORY_MAX];
    uint8_t    histCount_ = 0;
    uint64_t   bootMs64_ = 0;
    uint64_t   lastStoreMs64_[RADAR_CMD_COUNT];      // per setting: uptime of the last NVS store attempt (not shared)
    uint64_t   lastChangeMs64_ = 0;                  // uptime of the last applied setting change (a changed value, not an echo)
    uint32_t   onAtMs_ = 0, offAtMs_ = 0, offSinceMs_ = 0, parkStartMs_ = 0;
    uint32_t   lastFaultCycleMs_ = 0;
    uint16_t   parkTimeouts_ = 0;
    bool       parkTimeoutFlag_ = false;
    bool       cycleActive_ = false;
    bool       haveFaultCycle_ = false;
    bool       thr_ = false;
    uint8_t    lastOnState_ = RADAR_ST_ON;
    uint8_t    lastOffState_ = RADAR_ST_FORCED_OFF;
};

RadarPower s_rp;

// The boot phase, before any other task exists (spec 4.4): RadarPower::begin() sets the atomics and the private state.
void radarPhaseAfterBoot(const Settings& s, uint32_t nowMs, uint64_t nowMs64) {
    s_rp.begin(s, nowMs, nowMs64);
}

void RadarPower::begin(const Settings& s, uint32_t nowMs, uint64_t nowMs64) {
    s_ = s;
    bootMs64_ = nowMs64;
    histCount_ = 0;
    parkTimeouts_ = 0;
    parkTimeoutFlag_ = cycleActive_ = haveFaultCycle_ = thr_ = false;
    lastFaultCycleMs_ = 0;
    lastOnState_ = RADAR_ST_ON;
    lastOffState_ = RADAR_ST_FORCED_OFF;
    onAtMs_ = offAtMs_ = offSinceMs_ = parkStartMs_ = 0;
    for (uint8_t i = 0; i < RADAR_CMD_COUNT; ++i) lastStoreMs64_[i] = nowMs64 - RADAR_NVS_MIN_INTERVAL_MS;   // no store yet: the first is not deferred
    // modulo 2^64 when the uptime is short: no change yet, nothing to settle
    lastChangeMs64_ = nowMs64 - RADAR_SETTING_SETTLE_MS;

    g_powerEpoch = 0;
    g_poweredAtMs = nowMs;
    g_ld2454Ready = false;
    g_ld2454Fault = RADAR_FAULT_NONE;
    portENTER_CRITICAL(&s_mux);
    s_haState = RADAR_HA_UNKNOWN;
    s_haLostMs = nowMs;
    s_pendingMask = 0;
    portEXIT_CRITICAL(&s_mux);
    ha_ = RADAR_HA_UNKNOWN;

    if (s.en) {
        g_radarPhase = RADAR_PHASE_POWERING;         // the 5 ms settle applies after every reboot
        g_parkRequest = false;
        g_parkAck = false;
        g_radarPowered = false;
        onAtMs_ = nowMs;
    } else {
        g_radarPhase = RADAR_PHASE_OFF_SETTLING;     // GPIO9 is already LOW; Serial1 is never opened
        g_parkRequest = true;
        g_parkAck = true;
        g_radarPowered = true;                       // until the settle ends
        offAtMs_ = nowMs;
    }

    memset(&cur_, 0, sizeof(cur_));
    cur_.sinceMs = nowMs;
    cur_.ns = s.ns; cur_.ne = s.ne;
    cur_.nextMin = -1;
    cur_.en = s.en; cur_.sched = s.sched; cur_.offl = s.offl; cur_.legacy = s.legacy;
    cur_.phase = (uint8_t)g_radarPhase.load();
    cur_.state = s.en ? RADAR_ST_FORCED_ON : RADAR_ST_FORCED_OFF;
    cur_.reason = RADAR_RS_MANUAL;
    cur_.ha = RADAR_HA_UNKNOWN;
    cur_.powered = g_radarPowered.load();
    portENTER_CRITICAL(&s_mux);
    s_status = cur_;
    portEXIT_CRITICAL(&s_mux);
    g_stateDirty = true;
}

void RadarPower::afterOtaPause(uint32_t nowMs) {
    if (g_radarPhase.load() == RADAR_PHASE_PARKING) parkStartMs_ = nowMs;
}

uint32_t RadarPower::pass(uint64_t now64) {
    const uint32_t now = (uint32_t)now64;
    const uint32_t storeWait = applyPending(now64);
    refreshStamps(now);

    struct tm tmv;
    const bool valid = readLocalTime(&tmv);
    const uint16_t nowMin = valid ? (uint16_t)(tmv.tm_hour * 60 + tmv.tm_min) : 0;
    const uint8_t nowSec = valid ? (uint8_t)tmv.tm_sec : 0;

    uint32_t haLostMs;
    portENTER_CRITICAL(&s_mux);
    ha_ = s_haState;
    if (ha_ != RADAR_HA_ONLINE) s_haLostMs = keepOld(now, s_haLostMs);   // no HA for 49.7 days must not read as 'just lost'
    haLostMs = s_haLostMs;
    portEXIT_CRITICAL(&s_mux);
    if ((int32_t)(now - haLostMs) < 0) haLostMs = now;      // MqttTask stored it after 'now' was sampled: that is 'just now', not 49.7 days ago

    const Decision dec = decide(s_, valid, nowMin, ha_, haLostMs, now);
    pruneHistory(now64);
    const uint32_t wait = stepPhase(dec, now, now64);
    updateStatus(dec, valid, nowMin, nowSec, now, now64);
    return minU32(minU32(wait, nextWait(dec, now)), storeWait);
}

// Takes the pending command slots; a value that equals the stored setting writes nothing, but is echoed (spec 4.11).
// A changed value is stored at most once per RADAR_NVS_MIN_INTERVAL_MS per setting (a flapping client must not wear the flash
// or blank the panel on every message): a faster one goes back into its slot and is retried when the interval is over.
// Returns the ms until the earliest retry (>= 1), 0xFFFFFFFF when nothing waits.
uint32_t RadarPower::applyPending(uint64_t now64) {
    int32_t values[RADAR_CMD_COUNT];
    uint8_t mask;
    portENTER_CRITICAL(&s_mux);
    mask = s_pendingMask;
    s_pendingMask = 0;
    memcpy(values, s_pendingValue, sizeof(values));
    portEXIT_CRITICAL(&s_mux);
    if (!mask) return 0xFFFFFFFFu;

    uint8_t deferred = 0;
    uint32_t retryMs = 0xFFFFFFFFu;
    for (uint8_t cmd = 0; cmd < RADAR_CMD_COUNT; ++cmd) {
        if (!(mask & (1u << cmd))) continue;
        const RadarCmd c = (RadarCmd)cmd;
        if (values[cmd] == settingValue(s_, c)) continue;
        const uint64_t sinceStore = now64 - lastStoreMs64_[cmd];
        if (sinceStore < RADAR_NVS_MIN_INTERVAL_MS) {       // too soon after the last store of this setting
            deferred |= (uint8_t)(1u << cmd);
            retryMs = minU32(retryMs, (uint32_t)(RADAR_NVS_MIN_INTERVAL_MS - sinceStore));
            continue;
        }
        lastStoreMs64_[cmd] = now64;
        if (storeSetting(c, values[cmd])) {
            applySetting(s_, c, values[cmd]);              // RAM follows only what was stored
            lastChangeMs64_ = now64;
            Serial.printf("[RADAR] %s = %ld\n", NVS_KEYS[cmd], (long)values[cmd]);
        } else if (c == RADAR_CMD_ENABLE && values[cmd] == 1) {
            // The one exception to D32, in the safe direction: with a broken NVS holding en = 0 every retry would fail and a
            // reboot would reload 0, so the radars could never be switched on again. The radars come on; a reboot may turn them off.
            applySetting(s_, c, 1);
            lastChangeMs64_ = now64;
            Serial.printf("[RADAR] NVS write failed: %s (applied to RAM only)\n", NVS_KEYS[cmd]);
        } else {
            Serial.printf("[RADAR] NVS write failed: %s\n", NVS_KEYS[cmd]);   // the echo shows the stored value (D32)
        }
    }
    if (deferred) {
        portENTER_CRITICAL(&s_mux);
        for (uint8_t cmd = 0; cmd < RADAR_CMD_COUNT; ++cmd) {
            const uint8_t bit = (uint8_t)(1u << cmd);
            if (!(deferred & bit) || (s_pendingMask & bit)) continue;   // a newer command for this setting is already waiting: it wins
            s_pendingValue[cmd] = values[cmd];
            s_pendingMask |= bit;
        }
        portEXIT_CRITICAL(&s_mux);
    }
    g_stateDirty = true;                                    // always: the echo converges (a repeated 0 is echoed again)
    return retryMs;
}

// The stamps that keepOld() guards, each only while its state lasts (s_haLostMs is refreshed in pass(), under s_mux).
void RadarPower::refreshStamps(uint32_t now) {
    const uint8_t phase = g_radarPhase.load();
    if (phase == RADAR_PHASE_ON || phase == RADAR_PHASE_PARKING) {
        const uint32_t at = g_poweredAtMs.load();               // RadarPower is its only writer
        const uint32_t kept = keepOld(now, at);
        if (kept != at) g_poweredAtMs = kept;
    } else if (phase == RADAR_PHASE_OFF) {
        offSinceMs_ = keepOld(now, offSinceMs_);
    }
    if (haveFaultCycle_) lastFaultCycleMs_ = keepOld(now, lastFaultCycleMs_);
}

// ---- history of power-ups: the throttle, ups24 and on24 (spec 4.7) ----------------------------------------------------
void RadarPower::pruneHistory(uint64_t now64) {
    uint8_t drop = 0;
    while (drop < histCount_ && hist_[drop].closed && now64 - hist_[drop].down > RADAR_STATS_WINDOW_MS) ++drop;
    if (drop) {
        memmove(&hist_[0], &hist_[drop], (histCount_ - drop) * sizeof(HistEntry));
        histCount_ = (uint8_t)(histCount_ - drop);
    }
}

uint16_t RadarPower::powerupsLastHour(uint64_t now64) const {
    uint16_t n = 0;
    for (uint8_t i = 0; i < histCount_; ++i) if (now64 - hist_[i].up < RADAR_THROTTLE_WINDOW_MS) ++n;
    return n;
}

bool RadarPower::oldestPowerupInHour(uint64_t now64, uint64_t* up) const {
    for (uint8_t i = 0; i < histCount_; ++i) {
        if (now64 - hist_[i].up < RADAR_THROTTLE_WINDOW_MS) { *up = hist_[i].up; return true; }
    }
    return false;
}

void RadarPower::recordPowerUp(uint64_t now64) {
    if (histCount_ == RADAR_HISTORY_MAX) {
        memmove(&hist_[0], &hist_[1], (RADAR_HISTORY_MAX - 1) * sizeof(HistEntry));
        histCount_ = (uint8_t)(RADAR_HISTORY_MAX - 1);
    }
    hist_[histCount_].up = now64;
    hist_[histCount_].down = 0;
    hist_[histCount_].closed = false;
    ++histCount_;
}

void RadarPower::recordPowerDown(uint64_t now64) {
    if (histCount_ && !hist_[histCount_ - 1].closed) {
        hist_[histCount_ - 1].down = now64;
        hist_[histCount_ - 1].closed = true;
    }
}

uint16_t RadarPower::ups24(uint64_t now64) const {
    uint16_t n = 0;
    for (uint8_t i = 0; i < histCount_; ++i) if (now64 - hist_[i].up <= RADAR_STATS_WINDOW_MS) ++n;
    return n;
}

// on-time in the last 24 h (each entry clipped to the window, an open entry runs to now) over min(24 h, uptime), tenths of a percent
uint16_t RadarPower::onShareX10(uint64_t now64) const {
    const uint64_t uptime = now64 - bootMs64_;
    const uint64_t window = uptime < RADAR_STATS_WINDOW_MS ? uptime : (uint64_t)RADAR_STATS_WINDOW_MS;
    if (window == 0) return 1000;
    const uint64_t lo = now64 - window;
    uint64_t total = 0;
    for (uint8_t i = 0; i < histCount_; ++i) {
        const uint64_t end = hist_[i].closed ? hist_[i].down : now64;
        const uint64_t a = hist_[i].up > lo ? hist_[i].up : lo;
        const uint64_t b = end < now64 ? end : now64;
        if (b > a) total += b - a;
    }
    const uint64_t v = (total * 1000u + window / 2u) / window;
    return (uint16_t)(v > 1000u ? 1000u : v);
}

// ---- the phase machine (spec 4.6); one transition per pass, returns the wait until the next pass --------------------------
uint32_t RadarPower::stepPhase(const Decision& dec, uint32_t now, uint64_t now64) {
    uint32_t wait = RADAR_TICK_MS;
    thr_ = false;
    uint8_t phase = g_radarPhase.load();

    // a fault power-cycle is an automatic OFF wanted on top of the decision (spec 4.8)
    if (phase == RADAR_PHASE_ON && !cycleActive_ && !dec.off &&
        g_ld2454Fault.load() == RADAR_FAULT_LD2454_NO_FRAMES &&
        (!haveFaultCycle_ || elapsed(now, lastFaultCycleMs_) >= RADAR_FAULT_CYCLE_MIN_MS) &&
        elapsed(now, g_poweredAtMs.load()) >= RADAR_MIN_ON_MS &&
        powerupsLastHour(now64) < RADAR_MAX_POWERUPS_PER_H) {
        cycleActive_ = true;
        haveFaultCycle_ = true;
        lastFaultCycleMs_ = now;
        Serial.println("[RADAR] LD2454 sends no frames - power-cycling the radars");
    }
    const bool wantsOff = dec.off || cycleActive_;
    const bool automatic = dec.automatic || (cycleActive_ && !dec.off);

    switch (phase) {
        case RADAR_PHASE_ON:
            if (wantsOff) {
                if (elapsed(now, g_poweredAtMs.load()) < RADAR_MIN_ON_MS) {
                    // deferred, not dropped: the decision is recomputed every pass
                } else if (automatic && powerupsLastHour(now64) >= RADAR_MAX_POWERUPS_PER_H) {
                    thr_ = true;                                        // an automatic OFF is refused (never a manual one)
                } else if (dec.automatic && now64 - lastChangeMs64_ < RADAR_SETTING_SETTLE_MS) {
                    // A setting changed a moment ago: the rest of a multi-command update (night start, end, schedule) may still be
                    // on its way and can withdraw this OFF. The OFF starts when the settings have been quiet for the settle time;
                    // a manual OFF, a park already running and the withdrawal of a park are never delayed by this.
                    wait = (uint32_t)(RADAR_SETTING_SETTLE_MS - (now64 - lastChangeMs64_));   // >= 1: no spinning
                } else {
                    g_parkAck = false;                                  // clear the ack first ...
                    g_parkRequest = true;                               // ... then raise the request
                    parkStartMs_ = now;
                    parkTimeoutFlag_ = false;
                    g_radarPhase = RADAR_PHASE_PARKING;
                    Serial.println("[RADAR] park requested");
                    wait = RADAR_PARK_POLL_MS;
                }
            }
            break;

        case RADAR_PHASE_PARKING:
            if (!wantsOff) {                                            // the OFF was withdrawn: back to ON, same epoch
                g_parkRequest = false;
                g_parkAck = false;
                parkTimeoutFlag_ = false;
                g_radarPhase = RADAR_PHASE_ON;
                Serial.println("[RADAR] park withdrawn");
            } else if (g_parkAck.load()) {                              // parked: now, and only now, GPIO9 goes LOW
                parkTimeoutFlag_ = false;
                setRadarPin(false);
                offAtMs_ = now;
                g_radarPhase = RADAR_PHASE_OFF_SETTLING;
                Serial.println("[RADAR] parked - radars off (GPIO9 LOW)");
                wait = RADAR_OFF_SETTLE_MS;
            } else if (elapsed(now, parkStartMs_) >= PARK_ACK_TIMEOUT_MS) {
                if (!parkTimeoutFlag_) {
                    parkTimeoutFlag_ = true;
                    if (parkTimeouts_ < 0xFFFF) ++parkTimeouts_;
                    Serial.println("[RADAR] fault park_timeout - the radars stay on");
                }
                wait = RADAR_TICK_MS;                                   // stays ON, keeps polling once per second
            } else {
                wait = RADAR_PARK_POLL_MS;
            }
            break;

        case RADAR_PHASE_OFF_SETTLING:
            if (elapsed(now, offAtMs_) >= RADAR_OFF_SETTLE_MS) {
                g_radarPowered = false;
                recordPowerDown(now64);
                offSinceMs_ = now;
                g_radarPhase = RADAR_PHASE_OFF;
            } else {
                wait = maxU32(1, RADAR_OFF_SETTLE_MS - elapsed(now, offAtMs_));
            }
            break;

        case RADAR_PHASE_OFF:
            if (!dec.off && elapsed(now, offSinceMs_) >= RADAR_MIN_OFF_MS) {   // a fault cycle ends here too
                lastOnState_ = (uint8_t)dec.state;                      // the ON state that is published from here on
                setRadarPin(true);
                onAtMs_ = now;
                g_radarPhase = RADAR_PHASE_POWERING;
                Serial.println("[RADAR] radars on (GPIO9 HIGH)");
                wait = RADAR_ON_SETTLE_MS;
            }
            break;

        case RADAR_PHASE_POWERING:
            if (elapsed(now, onAtMs_) >= RADAR_ON_SETTLE_MS) {
                g_parkRequest = false;                                  // presence() stays UNKNOWN: g_radarPowered is still false
                g_parkAck = false;
                g_powerEpoch.fetch_add(1);                              // the new epoch is stored BEFORE the phase
                g_poweredAtMs = now;                                    // ... and the new timestamp BEFORE g_radarPowered: a
                g_radarPowered = true;                                  // reader that sees 'powered' never pairs it with the old one
                recordPowerUp(now64);
                cycleActive_ = false;
                parkTimeoutFlag_ = false;
                g_radarPhase = RADAR_PHASE_ON;
            } else {
                wait = maxU32(1, RADAR_ON_SETTLE_MS - elapsed(now, onAtMs_));
            }
            break;

        default:
            break;
    }
    return wait;
}

// the earliest deadline that is not a phase step: the minimum-ON expiry, the warm-up end, the minimum-OFF expiry (D29)
uint32_t RadarPower::nextWait(const Decision& dec, uint32_t now) const {
    const uint8_t phase = g_radarPhase.load();
    const bool wantsOff = dec.off || cycleActive_;
    uint32_t w = RADAR_TICK_MS;
    if (phase == RADAR_PHASE_ON) {
        const uint32_t sincePowerUp = elapsed(now, g_poweredAtMs.load());
        if (wantsOff && sincePowerUp < RADAR_MIN_ON_MS) w = minU32(w, maxU32(1, RADAR_MIN_ON_MS - sincePowerUp));
        if (sincePowerUp < LD2412_STATIC_WARMUP_MS)     w = minU32(w, maxU32(1, LD2412_STATIC_WARMUP_MS - sincePowerUp));
    } else if (phase == RADAR_PHASE_OFF && !dec.off && elapsed(now, offSinceMs_) < RADAR_MIN_OFF_MS) {
        w = minU32(w, maxU32(1, RADAR_MIN_OFF_MS - elapsed(now, offSinceMs_)));
    }
    return w;
}

// ---- the published status (spec 4.9) --------------------------------------------------------------------------------
void RadarPower::updateStatus(const Decision& dec, bool valid, uint16_t nowMin, uint8_t nowSec, uint32_t now, uint64_t now64) {
    const uint8_t phase = g_radarPhase.load();
    const bool wantsOff = dec.off || cycleActive_;
    uint8_t state = (uint8_t)dec.state;
    uint8_t reason = (uint8_t)dec.reason;
    int16_t next = -1;
    if (s_.en && s_.sched && valid && s_.ns != s_.ne) {            // the next schedule boundary
        next = (int16_t)(inWindow(nowMin, s_.ns, s_.ne) ? s_.ne : s_.ns);
    }

    if (phase == RADAR_PHASE_ON || phase == RADAR_PHASE_PARKING || phase == RADAR_PHASE_POWERING) {   // powered or about to be
        if (phase == RADAR_PHASE_PARKING && parkTimeoutFlag_) {
            state = RADAR_ST_FAILSAFE_ON;
            reason = RADAR_RS_FAULT;
            next = valid ? (int16_t)nowMin : (int16_t)-1;
        } else if (thr_) {
            state = RADAR_ST_FAILSAFE_ON;
            reason = RADAR_RS_THROTTLE;
            uint64_t oldest;
            next = (valid && oldestPowerupInHour(now64, &oldest))
                       ? (int16_t)localAfter(nowMin, nowSec, (uint32_t)(RADAR_THROTTLE_WINDOW_MS - (now64 - oldest)))
                       : (int16_t)-1;
        } else if (wantsOff) {
            state = lastOnState_;                                   // keeps the previous ON state
            reason = RADAR_RS_PENDING;
            if (phase == RADAR_PHASE_PARKING) {
                next = valid ? (int16_t)nowMin : (int16_t)-1;
            } else if (valid) {
                const uint32_t since = elapsed(now, g_poweredAtMs.load());
                next = (int16_t)localAfter(nowMin, nowSec, since < RADAR_MIN_ON_MS ? RADAR_MIN_ON_MS - since : 0);
            } else {
                next = -1;
            }
        } else {
            lastOnState_ = state;
        }
    } else {                                                        // OFF_SETTLING, OFF: GPIO9 is LOW
        if (cycleActive_ && !dec.off) {                             // a fault power-cycle: the decision wants ON anyway
            reason = RADAR_RS_FAULT;
            if (valid && phase == RADAR_PHASE_OFF) {
                const uint32_t since = elapsed(now, offSinceMs_);
                next = (int16_t)localAfter(nowMin, nowSec, since < RADAR_MIN_OFF_MS ? RADAR_MIN_OFF_MS - since : 0);
            } else {
                next = -1;
            }
        } else if (dec.off) {
            lastOffState_ = state;
        } else {                                                    // an ON waits for the 10 s minimum OFF time
            state = lastOffState_;
            reason = RADAR_RS_PENDING;
            if (valid && phase == RADAR_PHASE_OFF) {
                const uint32_t since = elapsed(now, offSinceMs_);
                next = (int16_t)localAfter(nowMin, nowSec, since < RADAR_MIN_OFF_MS ? RADAR_MIN_OFF_MS - since : 0);
            } else {
                next = -1;
            }
        }
    }

    RadarStatus n = cur_;
    n.en = s_.en; n.sched = s_.sched; n.offl = s_.offl; n.legacy = s_.legacy;
    n.ns = s_.ns; n.ne = s_.ne;
    n.phase = phase;
    n.powered = g_radarPowered.load();
    const bool warm = radarWarmedUp(now);                           // spec 4.9: observing is never earlier than 12 s after the power-up
    n.obs412 = warm;
    n.observing = warm && g_ld2454Ready.load();
    n.state = state;
    n.reason = reason;
    n.nextMin = next;
    n.thr = thr_;
    n.fault = parkTimeoutFlag_ ? (uint8_t)RADAR_FAULT_PARK_TIMEOUT : g_ld2454Fault.load();
    n.parkTimeouts = parkTimeouts_;
    n.ha = ha_;
    n.ups24 = ups24(now64);
    n.on24x10 = onShareX10(now64);
    if (n.state != cur_.state || n.powered != cur_.powered) n.sinceMs = now;
    if (n.state != cur_.state || n.reason != cur_.reason) {         // bench aid (design 5.6 j test 6: the serial log shows failsafe_on)
        Serial.printf("[RADAR] state %s (%s)\n", radarStateName(n.state), radarReasonName(n.reason));
    }

    const bool changed = !sameStatus(n, cur_);
    cur_ = n;
    portENTER_CRITICAL(&s_mux);                                     // the stored copy always follows (on24x10 alone sets no flag)
    s_status = n;
    portEXIT_CRITICAL(&s_mux);
    if (changed) g_stateDirty = true;                               // after the copy is stored
}

// ---- task, early init and the interface of radar_power.h ------------------------------------------------------------------
// The build's TWDT is 5 s with panic: RadarPower subscribes, feeds it every pass and unsubscribes around the OTA wait.
bool s_twdtLogged = false;
void twdtCheck(esp_err_t err) {
    if (err != ESP_OK && !s_twdtLogged) {
        s_twdtLogged = true;
        Serial.printf("[RADAR] TWDT: %d\n", (int)err);
    }
}

void radarPowerTask(void*) {
    twdtCheck(esp_task_wdt_add(NULL));                              // subscribe this task
    uint32_t waitMs = 0;                                            // the first pass runs at once
    UBaseType_t stackFree = 0xFFFFFFFF;
    for (;;) {
        if (waitMs) ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(waitMs));    // woken early by radarPowerKick()
        if (isOtaPauseRequested()) {                                // at the top of EVERY pass: tick, park poll, settle wait
            twdtCheck(esp_task_wdt_delete(NULL));                   // an upload lasts far longer than the 5 s TWDT timeout
            waitWhileOtaPaused();
            twdtCheck(esp_task_wdt_add(NULL));
            s_rp.afterOtaPause(millis());                           // phase PARKING: parkStartMs = now (the upload does not count)
        }
        twdtCheck(esp_task_wdt_reset());
        waitMs = s_rp.pass((uint64_t)(esp_timer_get_time() / 1000));    // returns >= 1
        if (waitMs < 1) waitMs = 1;

        const UBaseType_t hw = uxTaskGetStackHighWaterMark(NULL);   // bench aid (spec 12.4): logged when it reaches a new low
        if (hw < stackFree) {
            stackFree = hw;
            Serial.printf("[RADAR] stack free: %u B\n", (unsigned)(hw * sizeof(StackType_t)));
        }
    }
}

}  // namespace

// The first call after Serial.begin() in setup(): GPIO9 is settled before anything that can block or crash (spec 4.4).
void radarPowerEarlyInit() {
    Serial.println("[BOARD] BOARD_REV2 firmware: GPIO9 = radar power enable (output), GPIO8 = button ladder");
    const Settings s = loadSettings();                              // the only NVS read at boot
    const uint8_t en = s.en;
    if (!en) {                                                      // stored manual OFF: TX high-Z BEFORE the radars lose VCC
        pinMode(RADAR_TX_PIN, INPUT);                               // no pull, not driven (design 5.4)
        pinMode(RADAR_RX_PIN, INPUT_PULLDOWN);
    }
    gpio_set_level((gpio_num_t)RADAR_PWR_EN_PIN, en ? 1 : 0);       // latch the level first ...
    pinMode(RADAR_PWR_EN_PIN, OUTPUT);                              // ... then enable the output driver: no glitch
    setenv("TZ", TIME_ZONE, 1);                                     // the clock can already be valid (it survives a software restart)
    tzset();                                                        // and NtpClockTask sets the zone later: local time from pass 1 on
    const uint64_t now64 = (uint64_t)(esp_timer_get_time() / 1000);
    radarPhaseAfterBoot(s, (uint32_t)now64, now64);                 // atomics, timers, first status
}

void initRadarPowerTask() {
    xTaskCreatePinnedToCore(radarPowerTask, "RadarPower", RADAR_TASK_STACK_SIZE, NULL, RADAR_TASK_PRIORITY, &s_task, 0);
}

void radarPowerKick() {
    if (s_task) xTaskNotifyGive(s_task);
}

void radarPowerRequest(RadarCmd cmd, int32_t value) {
    if (cmd >= RADAR_CMD_COUNT) return;
    const bool minutes = cmd == RADAR_CMD_NIGHT_START || cmd == RADAR_CMD_NIGHT_END;
    if (minutes ? (value < 0 || value > 1439) : (value != 0 && value != 1)) return;   // invalid values are ignored
    portENTER_CRITICAL(&s_mux);
    s_pendingValue[cmd] = value;                                    // one slot per command, last wins
    s_pendingMask |= (uint8_t)(1u << cmd);
    portEXIT_CRITICAL(&s_mux);
    radarPowerKick();
}

void radarSetHaState(RadarHaState state) {
    bool changed = false;
    portENTER_CRITICAL(&s_mux);
    if (state != s_haState) {
        if (s_haState == RADAR_HA_ONLINE) s_haLostMs = millis();   // leaving ONLINE starts the grace;
        s_haState = state;                                          // UNKNOWN <-> OFFLINE keeps the earlier haLostMs
        changed = true;
    }
    portEXIT_CRITICAL(&s_mux);
    if (changed) radarPowerKick();
}

RadarStatus radarStatusSnapshot() {
    RadarStatus copy;
    portENTER_CRITICAL(&s_mux);
    copy = s_status;
    portEXIT_CRITICAL(&s_mux);
    return copy;
}

#endif  // BOARD_REV2
