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

static const unsigned long CO2_SENSOR_INTERVAL = 5000;
static const unsigned long LIGHT_SENSOR_INTERVAL = 1000;
static const unsigned long TEMP_HUM_SENSOR_INTERVAL = 2000;

static const uint8_t CO2_SENSOR_ADDRESS = 0x62;

static const char* const WIFI_PORTAL_NAME = "Led Matrix Node";
static const uint16_t WIFI_CONFIG_TIMEOUT = 180; // 3 minutes

static const uint8_t DISPLAY_BRIGHTNESS = 20;

static const uint32_t SENSOR_TASK_STACK_SIZE = 4096;
static const uint8_t SENSOR_TASK_PRIORITY = 1;

static const uint32_t MQTT_TASK_STACK_SIZE = 8192;
static const uint8_t MQTT_TASK_PRIORITY = 1;

#endif