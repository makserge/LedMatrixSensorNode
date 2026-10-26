#include <Arduino.h>
#include <Wire.h>
#include "wifi_management.h"
#include "constants.h"
#include "co2_sensor.h"
#include "temp_humidity_sensor.h"
#include "light_sensor.h"
#include "spectrum_analyzer.h"
#include "display_manager.h"
#include "mqtt_manager.h"
#include "ntp_clock.h"
#include "ld2412_sensor.h"
#include "ld2454_sensor.h"
#include "i2c_mutex.h"
#include "web_server_ui.h"
#ifdef BOARD_REV2
#include "radar_power.h"
#endif

void setup() {
    Serial.begin(115200);
#ifdef BOARD_REV2
    radarPowerEarlyInit();         // FIRST: drives GPIO9 before anything else runs (spec 4.4); R7 kept the radars ON until now
#endif

    initI2CMutex();

    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

    initDisplayTask();
#ifdef BOARD_REV2
    initRadarPowerTask();          // before Wi-Fi: WiFiManager can block for 180 s (wifi_management.cpp:71-86) and then restart,
    initLd2412SensorTask();        // and LD2454Task must run to answer park requests
    initLd2454SensorTask();
#endif
    initWiFiManagement();
    initSpectrumAnalyzerTask();
    initCo2SensorTask();
    initTempHumiditySensorTask();
    initNtpClockTask();
    initLightSensorTask();
#ifndef BOARD_REV2
    initLd2412SensorTask();
    initLd2454SensorTask();
#endif
    initMqttTask();
}

void loop() {
    webUi.loop(); // web UI + OTA; a successful upload reboots from inside this call
    updateWiFiManagement();
    vTaskDelay(pdMS_TO_TICKS(5));
}
