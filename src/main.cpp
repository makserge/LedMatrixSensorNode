#include <Arduino.h>
#include "wifi_management.h"
#include "co2sensor.h"
#include "lightsensor.h"
#include "temphumiditysensor.h"
#include "mqttmanager.h"

void setup() {
    Serial.begin(115200);
    
    initWiFiManagement();
    initCo2SensorTask();
    initLightSensorTask();
    initTempHumiditySensorTask();
    initMqttTask();
}

void loop() {
    updateWiFiManagement();
}
