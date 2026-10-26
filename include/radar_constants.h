#ifndef RADAR_CONSTANTS_H
#define RADAR_CONSTANTS_H

// Board revision 2 only (platformio.ini: env seeed_xiao_esp32s3_rev2, -D BOARD_REV2): GPIO9 radar power switch,
// GPIO8 button ladder, night schedule, manual radar control (P2 spec). All P2 constants live here.
// Without BOARD_REV2 this header is empty on purpose: P2 code outside #ifdef BOARD_REV2 fails to compile.
#ifdef BOARD_REV2

#include <Arduino.h>

// ---- RadarPower (spec 4): task, switch timing, guards, failsafe ------------------------------------------------------
static const uint32_t RADAR_TICK_MS                 = 1000;     // task tick; a notification starts a pass at once
static const uint32_t RADAR_PARK_POLL_MS            = 100;      // phase PARKING: ack poll (RADAR_TICK_MS after a park timeout)
static const uint32_t RADAR_ON_SETTLE_MS            = 5;        // GPIO9 HIGH -> radars up (OA 2.2: rail rises in about 1 ms)
static const uint32_t RADAR_OFF_SETTLE_MS           = 50;       // GPIO9 LOW -> radars off (simulated fall 20-45 ms; P0 (a) 2 may raise it)
static const uint32_t RADAR_MIN_ON_MS               = 60000;    // minimum ON dwell, from the power-up
static const uint32_t RADAR_MIN_OFF_MS              = 10000;    // minimum OFF dwell, from the end of the OFF settle
static const uint8_t  RADAR_MAX_POWERUPS_PER_H      = 12;       // throttle: automatic OFFs only (never a manual OFF)
static const uint32_t RADAR_OFFLINE_GRACE_MS        = 300000;   // HA not ONLINE this long counts as offline (5 min)
static const uint32_t PARK_ACK_TIMEOUT_MS           = 6000;     // no park ack: fault park_timeout, the radars stay ON
static const uint32_t RADAR_FAULT_CYCLE_MIN_MS      = 3600000;  // an ld2454_no_frames power-cycle at most this often
static const uint8_t  RADAR_HISTORY_MAX             = 32;       // power-up history entries (throttle, ups24, on24)
static const uint32_t RADAR_THROTTLE_WINDOW_MS      = 3600000;  // the throttle looks at this window
static const uint32_t RADAR_STATS_WINDOW_MS         = 86400000; // ups24 / on24 window
static const uint32_t RADAR_STAMP_MAX_AGE_MS        = 1073741824; // a 32-bit stamp older than this (12.4 days) is moved forward (49.7-day wrap)
static const uint32_t RADAR_SETTING_SETTLE_MS       = 500;      // an automatic OFF starts no park this soon after an applied setting change
static const uint32_t RADAR_TASK_STACK_SIZE         = 4096;     // RadarPower task: core 0, priority RADAR_TASK_PRIORITY
static const uint8_t  RADAR_TASK_PRIORITY           = 1;
static const uint32_t RADAR_MQTT_EXTRA_STACK        = 2048;     // added to MQTT_TASK_STACK_SIZE under REV2 (spec 7.9)

// ---- night window defaults (user decision: 00:00-06:00 every day) and NVS (namespace 'radar', spec 4.3) ----------------
static const uint16_t RADAR_NIGHT_START_DEFAULT_MIN = 0;        // 00:00
static const uint16_t RADAR_NIGHT_END_DEFAULT_MIN   = 360;      // 06:00
static const char* const RADAR_NVS_NAMESPACE        = "radar";  // keys: en u8 =1, sched u8 =0, ns u16 =0, ne u16 =360, offl u8 =0, legacy u8 =0
static const uint32_t RADAR_NVS_MIN_INTERVAL_MS     = 1000;     // per key at most one NVS store this often; a faster change waits, the newest wins

// ---- LD2454 (spec 5) -----------------------------------------------------------------------------------------------
static const uint32_t LD2454_BOOT_TIMEOUT_MS        = 8000;     // after a power-up: no tracking frame this long -> fault ld2454_no_frames
static const uint8_t  LD2454_WARMUP_FRAMES          = 5;        // frames after END_CONFIG before g_ld2454Ready

// ---- LD2412 presence() (spec 6) -------------------------------------------------------------------------------------
static const uint32_t LD2412_OUT_IGNORE_MS          = 3000;     // OUT mask after a power-up (P0 sets the final value)
static const uint32_t LD2412_OUT_MIN_HIGH_MS        = 2000;     // OUT must be HIGH this long to count as PRESENT
static const uint32_t LD2412_STATIC_WARMUP_MS       = 12000;    // a LOW OUT is UNKNOWN until this long after the power-up

// ---- button ladder on GPIO8 (spec 9; OA 2.3 levels, analogReadMilliVolts() at ADC_11db) ------------------------------
static const uint32_t LADDER_MODE_MAX_MV           = 500;      // <= : MODE (about 0 V; both pressed also reads 0 V)
static const uint32_t LADDER_POWER_MIN_MV          = 1150;     // 1150..2150 : POWER (about 1.65 V through R8)
static const uint32_t LADDER_POWER_MAX_MV          = 2150;
static const uint32_t LADDER_IDLE_MIN_MV           = 2600;     // >= : IDLE (about 3.3 V); between the bands: UNSURE
static const uint32_t LADDER_CONFIRM_MS            = 3;        // second read this long after a press edge

// ---- frames stream and legacy topics (spec 7.7, 7.8) -----------------------------------------------------------------
static const uint32_t LD2454_STREAM_OFF_IDLE_MS    = 30000;    // frames heartbeat while the phase is OFF
static const uint32_t LEGACY_HEARTBEAT_MS          = 30000;    // legacy LD2454 JSON heartbeat while 'Legacy radar JSON' is ON
static const uint32_t RADAR_PRESENCE_HOLD_MS       = 3000;     // 'Radar presence' stays ON this long after the last LD2454 target (frame flicker)
static const size_t   RADAR_ATTR_JSON_MAX          = 256;      // buffer of the radar/state/attr JSON (worst case 167 B)
static const uint32_t MQTT_BIRTH_RESEND_MAX_DELAY_MS = 2000;   // HA birth: resend after a random 0..2000 ms
static const uint32_t MQTT_BIRTH_SKIP_MS           = 5000;     // an 'online' within this long of a discovery burst triggers no resend

// ---- MQTT topics (spec 7.1); each state topic is its command topic + "/state" -----------------------------------------
static const char* const MQTT_RADAR_POWER_CMD          = "led_matrix_node/radar/power";
static const char* const MQTT_RADAR_POWER_STATE        = "led_matrix_node/radar/power/state";
static const char* const MQTT_RADAR_SCHEDULE_CMD       = "led_matrix_node/radar/schedule";
static const char* const MQTT_RADAR_SCHEDULE_STATE     = "led_matrix_node/radar/schedule/state";
static const char* const MQTT_RADAR_NIGHT_START_CMD    = "led_matrix_node/radar/night_start";
static const char* const MQTT_RADAR_NIGHT_START_STATE  = "led_matrix_node/radar/night_start/state";
static const char* const MQTT_RADAR_NIGHT_END_CMD      = "led_matrix_node/radar/night_end";
static const char* const MQTT_RADAR_NIGHT_END_STATE    = "led_matrix_node/radar/night_end/state";
static const char* const MQTT_RADAR_SCHED_OFFLINE_CMD  = "led_matrix_node/radar/sched_offline";
static const char* const MQTT_RADAR_SCHED_OFFLINE_STATE = "led_matrix_node/radar/sched_offline/state";
static const char* const MQTT_RADAR_LEGACY_CMD         = "led_matrix_node/radar/legacy";
static const char* const MQTT_RADAR_LEGACY_STATE       = "led_matrix_node/radar/legacy/state";
static const char* const MQTT_RADAR_POWERED_TOPIC      = "led_matrix_node/radar/powered";
static const char* const MQTT_RADAR_OBSERVING_TOPIC    = "led_matrix_node/radar/observing";
static const char* const MQTT_RADAR_STATE_TOPIC        = "led_matrix_node/radar/state";
static const char* const MQTT_RADAR_STATE_ATTR_TOPIC   = "led_matrix_node/radar/state/attr";
static const char* const MQTT_PRESENCE_TOPIC           = "led_matrix_node/presence";
static const char* const MQTT_HA_STATUS_TOPIC          = "homeassistant/status";

#endif  // BOARD_REV2
#endif  // RADAR_CONSTANTS_H
