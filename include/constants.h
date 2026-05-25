#ifndef CONSTANTS_H
#define CONSTANTS_H

#include <Arduino.h>
#include "pins.h"

static const char* const MQTT_HOST = "192.168.8.100";
static const uint16_t MQTT_PORT = 1883;
static const char* MQTT_USER = "smarthome";
static const char* MQTT_PASS = "smarthome";
static const char* MQTT_CLIENT_ID = "Led_Matrix_Node";
static const unsigned long MQTT_PUBLISH_INTERVAL = 10000;

static const char* MQTT_STATE_TOPIC      = "led_matrix_node/sensors/state";
static const char* MQTT_STATUS_TOPIC     = "led_matrix_node/sensors/status";
static const char* MQTT_DISCOVERY_PREFIX = "homeassistant";

static const char* MQTT_CONFIG_TOPIC_TPL = "%s/sensor/%s_%s/config";

static const char* const MQTT_DISPLAY_MODE_TOPIC = "led_matrix_node/display/mode";
static const char* const MQTT_DISPLAY_MSG_TOPIC  = "led_matrix_node/display/message";
static const char* const MQTT_BEEPER_TOPIC       = "led_matrix_node/beeper";

static const unsigned long CO2_SENSOR_INTERVAL = 5000;
static const unsigned long LIGHT_SENSOR_INTERVAL = 1000;
static const unsigned long TEMP_HUM_SENSOR_INTERVAL = 2000;

static const uint8_t CO2_SENSOR_ADDRESS = 0x62;

static const char* const WIFI_PORTAL_NAME = "Led Matrix Node";
static const uint16_t WIFI_CONFIG_TIMEOUT = 180;

static const uint8_t DISPLAY_BRIGHTNESS = 20;

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
    VIEW_CUSTOM_MSG
};

static const char* const AUDIO_STREAM_URL = "http://bbcmedia.co.uk";
static const uint16_t FFT_SAMPLES = 256;
static const uint32_t AUDIO_TASK_STACK_SIZE = 8192;
static const uint8_t AUDIO_TASK_PRIORITY = 2;

static const uint32_t MQTT_MSG_MAX_LEN = 32;

static const float LUX_MIN_THRESHOLD = 2.0f;
static const float LUX_MAX_THRESHOLD = 300.0f;
static const uint8_t MATRIX_MIN_BRIGHTNESS = 2;
static const uint8_t MATRIX_MAX_BRIGHTNESS = 255;

static const uint32_t DISPLAY_TASK_STACK_SIZE = 4096;
static const uint8_t DISPLAY_TASK_PRIORITY = 1;

static const uint32_t SPECTRUM_PEAK_HOLD_TIME_MS = 300;
static const uint32_t SPECTRUM_PEAK_DECAY_TIME_MS = 80;

static const double NORMALIZE_DECAY_RATE = 0.995; 
static const double NORMALIZE_MIN_LIMIT = 500.0;

static const unsigned long MQTT_MSG_DURATION_MS = 15000;
static const uint16_t CO2_WARNING_THRESHOLD = 1000;

static const unsigned long DISPLAY_AUTO_RETURN_DELAY_MS = 5000;
static const unsigned long BEEP_DURATION_MS = 100;

static const unsigned long CO2_ALARM_INTERVAL_MS = 30000;
static const int CO2_ALARM_START_HOUR = 8;
static const int CO2_ALARM_END_HOUR = 21;
static const unsigned long CO2_ALARM_DISPLAY_DURATION_MS = 10000;
static const unsigned long CO2_ALARM_BEEP_ON_MS = 1000;
static const unsigned long CO2_ALARM_BEEP_OFF_MS = 1000;

#endif
