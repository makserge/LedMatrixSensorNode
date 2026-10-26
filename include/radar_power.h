#ifndef RADAR_POWER_H
#define RADAR_POWER_H

// Board revision 2 only (-D BOARD_REV2): the radar power switch on GPIO9 and the night schedule (P2 spec, Section 4).
// Empty without BOARD_REV2.
#ifdef BOARD_REV2

#include <Arduino.h>
#include <atomic>
#include "radar_constants.h"

enum RadarPhase : uint8_t {
    RADAR_PHASE_ON = 0, RADAR_PHASE_PARKING, RADAR_PHASE_OFF_SETTLING, RADAR_PHASE_OFF, RADAR_PHASE_POWERING
};
enum RadarState : uint8_t {                      // the published 'Radar state'; sm codes A N F X S
    RADAR_ST_ON = 0, RADAR_ST_NIGHT_OFF, RADAR_ST_FORCED_ON, RADAR_ST_FORCED_OFF, RADAR_ST_FAILSAFE_ON
};
enum RadarReason : uint8_t {
    RADAR_RS_SCHEDULE = 0, RADAR_RS_MANUAL, RADAR_RS_FAILSAFE, RADAR_RS_HA_UNKNOWN, RADAR_RS_OTA /* never produced */,
    RADAR_RS_FAULT, RADAR_RS_THROTTLE, RADAR_RS_PENDING
};
enum RadarFault : uint8_t { RADAR_FAULT_NONE = 0, RADAR_FAULT_PARK_TIMEOUT, RADAR_FAULT_LD2454_NO_FRAMES };
enum RadarHaState : uint8_t { RADAR_HA_UNKNOWN = 0, RADAR_HA_ONLINE, RADAR_HA_OFFLINE };
enum RadarCmd : uint8_t {
    RADAR_CMD_ENABLE = 0, RADAR_CMD_SCHEDULE, RADAR_CMD_NIGHT_START, RADAR_CMD_NIGHT_END,
    RADAR_CMD_SCHED_OFFLINE, RADAR_CMD_LEGACY, RADAR_CMD_COUNT
};

// The snapshot MqttTask and the web page read (radarStatusSnapshot()). Written by RadarPower under its spinlock.
struct RadarStatus {
    uint32_t sinceMs;          // millis() of the last change of state or powered
    uint16_t ns, ne;           // night start / end, minutes after local midnight (0..1439)
    int16_t  nextMin;          // minute of day of the next scheduled or pending change, -1 = none
    uint16_t ups24;            // power-ups in the last 24 h
    uint16_t on24x10;          // on-share over min(24 h, uptime), tenths of a percent (0..1000)
    uint16_t parkTimeouts;     // park timeouts since boot
    uint8_t  en, sched, offl, legacy;            // stored settings, 0 / 1
    uint8_t  phase, state, reason, fault, ha;    // RadarPhase, RadarState, RadarReason, RadarFault, RadarHaState
    bool     powered;          // 'Radar power' and status "sw": GPIO9 with the OFF settle
    bool     observing;        // 'Radar observing' and frames pw bit0 (all three conditions, spec 4.9)
    bool     obs412;           // frames pw bit1
    bool     thr;              // an automatic OFF is refused by the throttle
    uint8_t  pw() const { return (uint8_t)((observing ? 1 : 0) | (obs412 ? 2 : 0)); }
    char     sm() const { return state < 5 ? "ANFXS"[state] : 'A'; }
};
static_assert(sizeof(RadarStatus) <= 32, "RadarStatus is copied under a spinlock: keep it small");

// Shared flags (defined in radar_power.cpp). Writers: see spec 4.2.
extern std::atomic<uint8_t>  g_radarPhase;      // RadarPhase
extern std::atomic<bool>     g_radarPowered;    // RADAR_5V can be present
extern std::atomic<uint32_t> g_powerEpoch;      // 0 = never powered up by the firmware
extern std::atomic<uint32_t> g_poweredAtMs;     // millis() of the last POWERING -> ON
extern std::atomic<bool>     g_parkRequest;
extern std::atomic<bool>     g_parkAck;
extern std::atomic<bool>     g_ld2454Ready;     // LD2454Task: multi-target confirmed + LD2454_WARMUP_FRAMES frames
extern std::atomic<uint8_t>  g_ld2454Fault;     // RadarFault, LD2454Task
extern std::atomic<bool>     g_stateDirty;      // RadarPower sets, MqttTask clears with exchange(false)

void radarPowerEarlyInit();                     // setup(), first call after Serial.begin()
void initRadarPowerTask();                      // setup(), before initWiFiManagement()
void radarPowerKick();                          // wakes the RadarPower task; any task, never inside a critical section
void radarPowerRequest(RadarCmd cmd, int32_t value);   // non-blocking, no NVS; invalid values are ignored
void radarSetHaState(RadarHaState state);              // MqttTask; a no-op when unchanged
RadarStatus radarStatusSnapshot();                     // a copy under the spinlock

// LD2412 static warm-up over: phase ON, no park request, and LD2412_STATIC_WARMUP_MS since the power-up
inline bool radarWarmedUp(uint32_t nowMs) {
    return g_radarPhase.load() == RADAR_PHASE_ON && !g_parkRequest.load() &&
           (uint32_t)(nowMs - g_poweredAtMs.load()) >= LD2412_STATIC_WARMUP_MS;
}

// Pure helpers (no state; tested on the PC)
int  radarParseOnOff(const char* text);                                // 1 / 0, or ON / OFF in any case; -1 otherwise
bool radarParseTimeOfDay(const char* text, uint16_t* minutes);         // H:MM, HH:MM, HH:MM:SS -> 0..1439; false otherwise
void radarFormatHms(uint16_t minutes, char out[9]);                    // "HH:MM:00"
void radarFormatHm(int16_t minutes, char out[6]);                      // "HH:MM"
const char* radarStateName(uint8_t state);                             // on night_off forced_on forced_off failsafe_on
const char* radarReasonName(uint8_t reason);                           // schedule manual failsafe ha_unknown ota fault throttle pending
const char* radarFaultName(uint8_t fault);                             // "" park_timeout ld2454_no_frames
const char* radarHaName(uint8_t ha);                                   // unknown online offline
const char* radarPhaseName(uint8_t phase);                             // ON PARKING OFF_SETTLING OFF POWERING

#endif  // BOARD_REV2
#endif  // RADAR_POWER_H
