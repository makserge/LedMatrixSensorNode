#include <Arduino.h>
#include "wifi_management.h"
#include "co2_sensor.h"
#include "light_sensor.h"
#include "temp_humidity_sensor.h"
#include "mqtt_manager.h"
#include "display_manager.h"

void setup() {
    Serial.begin(115200);
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN); 
    
    initWiFiManagement();
    initCo2SensorTask();
    initLightSensorTask();
    initTempHumiditySensorTask();
    initMqttTask();
    initDisplayTask();
}

void loop() {
    updateWiFiManagement();
}
