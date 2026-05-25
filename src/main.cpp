#include <Arduino.h>
#include "wifi_management.h"
#include "constants.h"
#include "co2_sensor.h"
#include "temp_humidity_sensor.h"
#include "light_sensor.h"
#include "spectrum_analyzer.h"
#include "display_manager.h"
#include "mqtt_manager.h"
#include "ntp_clock.h"

void setup() {
    Serial.begin(115200);
    
    initWiFiManagement();
    initCo2SensorTask();
    initTempHumiditySensorTask();
    initNtpClockTask();
    initSpectrumAnalyzerTask();
    initLightSensorTask();
    initDisplayTask(); 
    initMqttTask();
}

void loop() {
}
