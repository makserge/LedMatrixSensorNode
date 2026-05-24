#ifndef MQTT_MANAGER_H
#define MQTT_MANAGER_H

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include "constants.h"
#include "co2_sensor.h"
#include "light_sensor.h"
#include "temp_humidity_sensor.h"

class MqttManager {
public:
    void begin();
    void update();
    void publishSensorData();
    void sendDiscovery();
    bool isConnected();

private:
    WiFiClient _espClient;
    PubSubClient _client;
    void publishConfig(const char* id, const char* name, const char* unit, const char* devClass, const char* valTpl);
};

extern MqttManager mqttManager;

void mqttTask(void *pvParameters);
void initMqttTask();

#endif
