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
#include "ld2450_sensor.h"
#include "i2c_mutex.h"

void setup() {
    Serial.begin(115200);

    initI2CMutex();

    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

    initDisplayTask();
    initWiFiManagement();
    initSpectrumAnalyzerTask();
    initCo2SensorTask();
    initTempHumiditySensorTask();
    initNtpClockTask();
    initLightSensorTask();
    initLd2412SensorTask();
    initLd2450SensorTask();
    initMqttTask();
}

void loop() {
}
