#ifndef MQTT_MANAGER_H
#define MQTT_MANAGER_H

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

#include "constants.h"
#include "co2_sensor.h"
#include "temp_humidity_sensor.h"
#include "light_sensor.h"
#include "display_manager.h"

class MqttManager {
public:
    void begin();
    void update();
    void publishSensorData();
    bool isConnected();
    void setCallback();

private:
    void sendDiscovery();
    void publishConfig(const char* id, const char* name, const char* unit, const char* devClass, const char* valTpl);
    void subscribeTopics();
    
    WiFiClient _espClient;
    PubSubClient _client;
};

extern MqttManager mqttManager;

void initMqttTask();

#endif
