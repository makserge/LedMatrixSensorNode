#ifndef CONSTANTS_H
#define CONSTANTS_H

#include <Arduino.h>
#include "pins.h"
#include "secrets.h"

static const uint16_t MQTT_PORT = 1883;
static const char* MQTT_CLIENT_ID = "Led_Matrix_Node";
static const unsigned long MQTT_PUBLISH_INTERVAL = 10000;
static const unsigned long MQTT_RADAR_PUBLISH_INTERVAL = 1000;

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
static const char* const MQTT_LD2450_TOPIC = "led_matrix_node/radar/ld2450";

static const unsigned long CO2_SENSOR_INTERVAL = 30000;
static const unsigned long CO2_DATA_TIMEOUT_MS = 90000;
static const unsigned long LIGHT_SENSOR_INTERVAL = 2000;
static const unsigned long TEMP_HUM_SENSOR_INTERVAL = 15000;
static const unsigned long LD2412_SENSOR_INTERVAL = 500;
static const unsigned long LD2450_SENSOR_INTERVAL = 50;

static const unsigned long LD2450_DATA_TIMEOUT_MS = 10000;

static const uint8_t CO2_SENSOR_ADDRESS = 0x62;

static const char* const WIFI_PORTAL_NAME = "Led Matrix Node";
static const uint16_t WIFI_CONFIG_TIMEOUT = 180;
static const unsigned long WIFI_BACKUP_TIMEOUT_MS = 15000;
static const unsigned long WIFI_IP_DISPLAY_DURATION_MS = 15000;

static const uint8_t DISPLAY_BRIGHTNESS = 20;

static const uint32_t LD2450_STARTUP_DELAY_MS = 5000;
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

static const uint32_t DISPLAY_TASK_STACK_SIZE = 4096;
static const uint8_t DISPLAY_TASK_PRIORITY = 1;

static const uint16_t SPECTRUM_FFT_LOW_SIZE = 2048;
static const uint16_t SPECTRUM_FFT_LOW_STRIDE = 1470;

static const uint16_t SPECTRUM_FFT_HIGH_SIZE = 1024;
static const uint16_t SPECTRUM_FFT_HIGH_STRIDE = 441;

static const uint8_t SPECTRUM_LOW_BAND_COUNT = 10;

static const float SPECTRUM_REF_FLOOR_MAG = 0.7f;
static const float SPECTRUM_REF_RISE_ALPHA = 0.05f;
static const float SPECTRUM_REF_FALL_ALPHA = 0.01f;
static const float SPECTRUM_REF_SCALE = 0.35f;

static const float SPECTRUM_ABS_SILENCE_MAG = 0.15f;
static const uint32_t SPECTRUM_SILENCE_AVG_WINDOW_MS = 2000;
static const uint32_t SPECTRUM_SLEEP_FADE_MS = 2000;

static const uint32_t AUDIO_SILENCE_TIMEOUT_MS = 2000;
static const uint32_t MIN_SPECTRUM_DISPLAY_MS = 5000;
static const uint32_t SPECTRUM_PRESENCE_OVERRIDE_MS = 5000;

static const float SPECTRUM_NOISE_GATE_DBFS = -45.0f;

static const float SPECTRUM_DB_WINDOW = 40.0f;
static const float SPECTRUM_DB_PER_STEP = SPECTRUM_DB_WINDOW / 16.0f;

static const float SPECTRUM_COMPRESSION_THRESHOLD_DBFS = -12.0f;
static const float SPECTRUM_COMPRESSION_RATIO = 2.0f;

static const float SPECTRUM_AGC_TARGET_LED = 8.0f;

static const float SPECTRUM_TILT_DB_PER_OCTAVE = 3.0f;
static const float SPECTRUM_TILT_REFERENCE_HZ = 1000.0f;

static const uint32_t BALLISTICS_TICK_MS = 10;

static const float BALLISTICS_ATTACK_COEFF = 0.0f;

static const float BALLISTICS_DECAY_COEFF = 0.75f;

static const uint32_t BALLISTICS_PEAK_HOLD_MS = 700;
static const uint32_t BALLISTICS_PEAK_DECAY_STEP_MS = 70; // superseded by gravity fall below, left in place unused

static const float BALLISTICS_MAX_LEVEL = 16.0f;

static const float SPECTRUM_PEAK_GRAVITY_STEPS_PER_S2 = 220.0f;

static const unsigned long MQTT_MSG_DURATION_MS = 15000;
static const uint16_t CO2_WARNING_THRESHOLD = 1000;
static const uint16_t CO2_ALARM_LOW_THRESHOLD = 900;
static const uint8_t CO2_ALARM_BEEP_COUNT = 5;

static const unsigned long DISPLAY_AUTO_RETURN_DELAY_MS = 5000;
static const unsigned long BEEP_DURATION_MS = 100;

static const unsigned long CO2_ALARM_INTERVAL_MS = 30000;
static const int CO2_ALARM_START_HOUR = 8;
static const int CO2_ALARM_END_HOUR = 22;
static const unsigned long CO2_ALARM_DISPLAY_DURATION_MS = 10000;
static const unsigned long CO2_ALARM_BEEP_ON_MS = 1000;
static const unsigned long CO2_ALARM_BEEP_OFF_MS = 1000;
static const unsigned long CO2_ALARM_POPUP_SAFETY_MS = 20000;

#endif