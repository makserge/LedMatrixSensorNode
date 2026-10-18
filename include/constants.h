#ifndef CONSTANTS_H
#define CONSTANTS_H

#include <Arduino.h>
#include "pins.h"
#include "secrets.h"

static const uint16_t MQTT_PORT = 1883;
static const char* MQTT_CLIENT_ID = "Led_Matrix_Node";
static const unsigned long MQTT_PUBLISH_INTERVAL = 10000;
static const unsigned long MQTT_RADAR_PUBLISH_INTERVAL = 1000;
// Home Assistant marks a sensor unavailable when no state arrived for this long.
static const uint16_t MQTT_EXPIRE_AFTER_S = 3 * MQTT_PUBLISH_INTERVAL / 1000;
// LD2454 is published every MQTT_RADAR_PUBLISH_INTERVAL: 5 missed reports -> unavailable.
static const uint16_t MQTT_RADAR_EXPIRE_AFTER_S = 5 * MQTT_RADAR_PUBLISH_INTERVAL / 1000;
static const unsigned long MQTT_RECONNECT_MIN_MS = 2000;   // gap between connect attempts, doubled after each failure ...
static const unsigned long MQTT_RECONNECT_MAX_MS = 60000;  // ... up to this
static const uint16_t MQTT_SOCKET_TIMEOUT_S = 5;           // CONNACK / packet read timeout (PubSubClient default: 15 s)

static const char* MQTT_STATE_TOPIC      = "led_matrix_node/sensors/state";
static const char* MQTT_STATUS_TOPIC     = "led_matrix_node/sensors/status";
static const char* MQTT_DISCOVERY_PREFIX = "homeassistant";

static const char* MQTT_CONFIG_TOPIC_TPL = "%s/sensor/%s_%s/config";

static const char* const MQTT_DISPLAY_MODE_TOPIC  = "led_matrix_node/display/mode";
static const char* const MQTT_DISPLAY_MSG_TOPIC   = "led_matrix_node/display/message";
static const char* const MQTT_DISPLAY_POWER_TOPIC = "led_matrix_node/display/power";
static const char* const MQTT_DISPLAY_COLOR_TOPIC = "led_matrix_node/display/color";
static const char* const MQTT_DISPLAY_ICON_TOPIC  = "led_matrix_node/display/icon";

static const char* const MQTT_BEEPER_TOPIC        = "led_matrix_node/beeper";

static const char* const MQTT_LD2412_TOPIC = "led_matrix_node/radar/ld2412";
static const char* const MQTT_LD2454_TOPIC = "led_matrix_node/radar/ld2454";
static const char* const MQTT_LD2454_CAL_STATE_TOPIC = "led_matrix_node/radar/ld2454/calibration";
static const char* const MQTT_LD2454_CAL_SET_TOPIC   = "led_matrix_node/radar/ld2454/calibration/set";

static const unsigned long CO2_SENSOR_INTERVAL = 30000;
static const unsigned long CO2_FAST_POLL_MS = 5000;        // poll interval while there is no fresh value (boot, after a
                                                           // watchdog restart): the SCD4x's own measurement period
static const unsigned long CO2_DATA_TIMEOUT_MS = 90000;    // no successful read this long -> value invalidated, measurement restarted
static const unsigned long CO2_DATA_STALE_MS = 120000;     // last good sample older than this -> CO2 unknown (backstop: the
                                                           // watchdog normally invalidates first; catches endless 0-ppm reads)
static const uint8_t CO2_START_ATTEMPTS = 3;               // startPeriodicMeasurement() tries per (re)start ...
static const unsigned long CO2_START_RETRY_MS = 1000;      // ... this far apart
static const unsigned long LIGHT_SENSOR_INTERVAL = 2000;
static const unsigned long LIGHT_DATA_STALE_MS = 10000;    // 5 read intervals without a good lux value -> unknown
static const unsigned long LIGHT_SENSOR_RETRY_MS = 30000;  // BH1750 begin() retry while it is not configured
static const uint8_t LIGHT_SENSOR_MAX_FAILED_READS = 3;    // failed reads in a row -> re-run begin()
static const unsigned long TEMP_HUM_SENSOR_INTERVAL = 15000;
static const unsigned long TEMP_HUM_DATA_STALE_MS = 60000; // 4 read intervals without a good value -> unknown
static const unsigned long LD2412_SENSOR_INTERVAL = 500;
static const unsigned long LD2454_SENSOR_INTERVAL = 50;

static const unsigned long LD2454_DATA_TIMEOUT_MS = 10000;
static const unsigned long LD2454_CONFIG_RETRY_MIN_MS = 2000; // first retry after a failed attempt, doubles each time
static const unsigned long LD2454_CONFIG_RETRY_MS = 60000;    // backoff ceiling
static const unsigned long LD2454_ACK_TIMEOUT_MS = 1500;
static const unsigned long LD2454_COMMAND_GAP_MS = 25;
static const unsigned long LD2454_RESTART_GAP_MS = 1000;

static const uint8_t CO2_SENSOR_ADDRESS = 0x62;

static const char* const WIFI_PORTAL_NAME = "Led Matrix Node";
static const uint16_t WIFI_CONFIG_TIMEOUT = 180;
static const unsigned long WIFI_BACKUP_TIMEOUT_MS = 15000;
static const unsigned long WIFI_IP_DISPLAY_DURATION_MS = 15000;
// Link supervision from loop(): the core retries only some disconnect reasons by itself.
static const unsigned long WIFI_RECONNECT_INTERVAL_MS = 30000;      // offline this long -> new connect attempt, repeated
static const unsigned long WIFI_SWITCH_NETWORK_AFTER_MS = 600000;   // offline this long -> other network (primary <-> backup), if a backup is set
// On the backup network: scan for the primary this often, doubled after each failed switch back.
static const unsigned long WIFI_PRIMARY_RETRY_MIN_MS = 300000;
static const unsigned long WIFI_PRIMARY_RETRY_MAX_MS = 3600000;
static const unsigned long WIFI_PRIMARY_CONNECT_TIMEOUT_MS = 20000; // switch not connected by then -> back to the backup

static const uint8_t DISPLAY_BRIGHTNESS = 20;

static const uint32_t LD2454_STARTUP_DELAY_MS = 5000;
static const uint32_t SENSOR_TASK_STACK_SIZE = 4096;
static const uint8_t SENSOR_TASK_PRIORITY = 1;

static const uint32_t MQTT_TASK_STACK_SIZE = 8192;
static const uint8_t MQTT_TASK_PRIORITY = 1;

static const char* const NTP_SERVER = "pool.ntp.org";
static const char* const TIME_ZONE  = "CET-1CEST,M3.5.0,M10.5.0/3";

static const uint32_t CLOCK_TASK_STACK_SIZE = 3072;
static const uint8_t CLOCK_TASK_PRIORITY = 1;

static const UBaseType_t CLOCK_QUEUE_LENGTH = 1;

enum DisplayView {
    VIEW_TIME,
    VIEW_TEMP_DATA,
    VIEW_CO2,
    VIEW_SPECTRUM,
    VIEW_CUSTOM_MSG,
    VIEW_CUSTOM_ICON
};

static const uint8_t MATRIX_WIDTH = 24;
static const uint8_t MATRIX_HEIGHT = 16;
static const uint16_t MATRIX_TOTAL_PIXELS = (uint16_t)MATRIX_WIDTH * MATRIX_HEIGHT;

static const unsigned long ICON_DISPLAY_DURATION_MS = 15000;
static const unsigned long OTA_TEXT_DISPLAY_MS = 2000;

// Extra DNS suffix the web UI accepts in the Host header (e.g. an office zone),
// on top of IP literals, single-label names and local-only suffixes.
// Override with -D WEB_EXTRA_HOST_SUFFIX=\".corp.example.com\".
#ifndef WEB_EXTRA_HOST_SUFFIX
#define WEB_EXTRA_HOST_SUFFIX ""
#endif

static const uint32_t AUDIO_TASK_STACK_SIZE = 8192;
static const uint8_t AUDIO_TASK_PRIORITY = 2;

static const uint8_t STREAM_TASK_PRIORITY = 4;

static const uint32_t BALLISTICS_TASK_STACK_SIZE = 3072;
static const uint8_t BALLISTICS_TASK_PRIORITY = AUDIO_TASK_PRIORITY + 1;

static const uint32_t MQTT_MSG_MAX_LEN = 32;

static const float LUX_MIN_THRESHOLD = 2.0f;
static const float LUX_MAX_THRESHOLD = 30.0f;
static const uint8_t MATRIX_MIN_BRIGHTNESS = 20;
static const uint8_t MATRIX_MAX_BRIGHTNESS = 255;
static const uint8_t MATRIX_FALLBACK_BRIGHTNESS = 128;     // target while there is no fresh light reading

static const uint32_t DISPLAY_TASK_STACK_SIZE = 4096;
static const uint8_t DISPLAY_TASK_PRIORITY = 1;

// Panel scan engine (LedMatrix::displayTaskEngine, core 1): it busy-waits each column's
// full 180 us window (lit part and dark remainder alike, so CPU use does not depend on
// brightness) and sleeps one tick per frame, about 4.5 ms of every 5 ms. Anything on core 1
// that preempts it mid-frame shows as flicker, hence the top application priority.
static const uint32_t MATRIX_ENGINE_TASK_STACK_SIZE = 4096;
static const uint8_t MATRIX_ENGINE_TASK_PRIORITY = 5;

// Audio stream (AUDIO_STREAM_URL): a WAV stream of 16-bit PCM, fetched with HTTP/1.0
// (chunked replies are decoded).
static const uint32_t STREAM_CONNECT_TIMEOUT_MS = 3000;
static const uint32_t STREAM_HEADER_TIMEOUT_MS = 5000;  // HTTP status line + headers + WAV header
static const uint32_t STREAM_RX_TIMEOUT_MS = 5000;      // no bytes for this long -> reconnect (the ESP only
                                                        // receives, so TCP alone never notices a dead peer)
static const uint32_t STREAM_RETRY_MIN_MS = 2000;
static const uint32_t STREAM_RETRY_MAX_MS = 30000;      // backoff ceiling, doubled after each failed attempt
static const uint32_t STREAM_WAV_MAX_HEADER_BYTES = 4096;
static const uint32_t STREAM_MIN_SAMPLE_RATE = 8000;
static const uint32_t STREAM_MAX_SAMPLE_RATE = 48000;   // above this the 2048-point FFT can't resolve the bass bands
static const uint8_t STREAM_MAX_CHANNELS = 8;           // downmixed to mono before the FFTs
static const uint32_t STREAM_READ_BURST_BYTES = 2048;   // drained per 1 ms tick before yielding: 2 MB/s,
                                                        // 2.7x the largest format (8 ch x 48 kHz = 768 kB/s)

static const uint16_t SPECTRUM_FFT_LOW_SIZE = 2048;
static const uint16_t SPECTRUM_FFT_LOW_FRAMES_PER_S = 30;   // stride = sample rate / this (1470 at 44.1 kHz)

static const uint16_t SPECTRUM_FFT_HIGH_SIZE = 1024;
static const uint16_t SPECTRUM_FFT_HIGH_FRAMES_PER_S = 100; // stride 441 at 44.1 kHz

static const uint8_t SPECTRUM_LOW_BAND_COUNT = 10;

static const float SPECTRUM_REF_FLOOR_MAG = 0.7f;
static const float SPECTRUM_REF_RISE_ALPHA = 0.05f;
static const float SPECTRUM_REF_FALL_ALPHA = 0.01f;
static const float SPECTRUM_REF_SCALE = 0.35f;

// Frame level = mean bin magnitude (22 Hz-16 kHz) of a 2048-point frame; 0.15 is about -43 dBFS RMS.
static const float SPECTRUM_ABS_SILENCE_MAG = 0.15f;      // a frame at/above this counts as audio
static const uint8_t SPECTRUM_ACTIVE_MIN_FRAMES = 3;      // consecutive such frames (~100 ms) before audio is active
static const float SPECTRUM_SLEEP_ENTER_MAG = 0.12f;      // 2 s average below this: bars fade out ...
static const float SPECTRUM_SLEEP_EXIT_MAG = 0.18f;       // ... and fade back in above this (hysteresis)
static const uint32_t SPECTRUM_SILENCE_AVG_WINDOW_MS = 2000;
static const uint32_t SPECTRUM_SLEEP_FADE_MS = 2000;
static const uint32_t SPECTRUM_WAKE_FADE_MS = 250;
static const uint32_t SPECTRUM_FRAME_STALE_MS = 200;      // no FFT frame for this long counts as silence

static const uint32_t AUDIO_SILENCE_TIMEOUT_MS = 2000;
static const uint32_t MIN_SPECTRUM_DISPLAY_MS = 5000;
static const uint32_t SPECTRUM_PRESENCE_OVERRIDE_MS = 5000;

// Band level -> LED rows. Levels are dB relative to the AGC reference (_referenceMag * SPECTRUM_REF_SCALE)
// after the tilt EQ, not dBFS. Linear below the knee, SPECTRUM_COMPRESSION_RATIO:1 above it, so with the
// values below: -20 dB -> 0 rows, -12 dB (knee) -> 3.2, 0 dB -> 5.6, +12 dB -> 8, +52 dB -> 16.
static const float SPECTRUM_DB_PER_STEP = 2.5f;            // dB per LED row below the knee (twice that above it)
static const float SPECTRUM_FLOOR_DB = -20.0f;             // lights 0 rows; must stay below the knee
static const float SPECTRUM_COMPRESSION_KNEE_DB = -12.0f;
static const float SPECTRUM_COMPRESSION_RATIO = 2.0f;

static const float SPECTRUM_TILT_DB_PER_OCTAVE = 3.0f;
static const float SPECTRUM_TILT_REFERENCE_HZ = 1000.0f;

static const uint32_t BALLISTICS_TICK_MS = 10;

static const float BALLISTICS_ATTACK_COEFF = 0.0f;

static const float BALLISTICS_DECAY_COEFF = 0.75f;

static const uint32_t BALLISTICS_PEAK_HOLD_MS = 700;
static const uint32_t BALLISTICS_PEAK_DECAY_STEP_MS = 70; // superseded by gravity fall below, left in place unused

static const float BALLISTICS_MAX_LEVEL = 16.0f;

static const float SPECTRUM_PEAK_GRAVITY_STEPS_PER_S2 = 220.0f;

// Scrolling messages run in whole passes: as many as come closest to their duration
// (MQTT_MSG_DURATION_MS, WIFI_IP_DISPLAY_DURATION_MS), at least one - so a long text is
// always shown in full - and at most MSG_MAX_SCROLL_PASSES.
static const unsigned long MQTT_MSG_DURATION_MS = 15000;
static const uint8_t MSG_MAX_SCROLL_PASSES = 3;
static const unsigned long MSG_SCROLL_STEP_MS = 154;        // 1 px per step; a pass is 24 + 1 + 5 x chars steps
static const uint8_t MSG_SCROLL_MAX_CATCHUP_STEPS = 4;      // a longer gap (panel off, WiFi down) resumes instead of jumping
static const unsigned long MSG_EXPIRY_MARGIN_MS = 2000;     // backstop after the last pass is due, for a message that
                                                            // isn't scrolling (panel off, WiFi down)
static const unsigned long BUTTON_DEBOUNCE_MS = 30;         // released this long before a button can fire again
static const uint16_t CO2_WARNING_THRESHOLD = 1200;
static const uint16_t CO2_ALARM_LOW_THRESHOLD = 1000;
static const uint8_t CO2_ALARM_BEEP_COUNT = 3;

static const unsigned long DISPLAY_AUTO_RETURN_DELAY_MS = 5000;
static const unsigned long BEEP_DURATION_MS = 100;          // MQTT beeper command; also the minimum silence before it

static const unsigned long CO2_ALARM_INTERVAL_MS = 600000;
static const int CO2_ALARM_START_HOUR = 8;
static const int CO2_ALARM_END_HOUR = 21;
static const unsigned long CO2_ALARM_DISPLAY_DURATION_MS = 10000;
static const unsigned long CO2_ALARM_BEEP_ON_MS = 500;
static const unsigned long CO2_ALARM_BEEP_OFF_MS = 500;
static const unsigned long CO2_ALARM_POPUP_SAFETY_MS = 20000;

#endif