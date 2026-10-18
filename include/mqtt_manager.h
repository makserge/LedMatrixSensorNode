#ifndef MQTT_MANAGER_H
#define MQTT_MANAGER_H

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

#include "constants.h"
#include "co2_sensor.h"
#include "display_manager.h"
#include "ld2412_sensor.h"
#include "ld2454_sensor.h"
#include "light_sensor.h"
#include "temp_humidity_sensor.h"

// PubSubClient waits for CONNACK, and for the rest of a packet, in a tight
// `while (!available())` loop: up to its socket timeout of busy-spinning on core 1.
// Sleeping a tick whenever nothing has arrived turns that spin into a real wait.
class MqttSocket : public WiFiClient {
public:
    int available() override {
        int n = WiFiClient::available();
        if (n == 0) vTaskDelay(1);
        return n;
    }
};

class MqttManager {
public:
    void begin();
    void update();
    void publishSensorData();
    void publishRadarData();
    void publishCalibrationState();
    bool isConnected();
    void setCallback();

private:
    void sendDiscovery();
    void publishConfig(const char* id, const char* name, const char* unit, const char* devClass, const char* valTpl);
    void publishEntity(const char* component, const String& id, JsonDocument& doc);
    void publishRadarEntity(const char* component, const String& id, const String& name, const String& valTpl,
                            const char* unit, const char* devClass, const char* stateClass, bool diagnostic);
    void publishNumber(const char* id, const char* name, const char* key, float minV, float maxV, float step,
                       const char* unit);
    void publishText(const char* id, const char* name, const char* key);
    void subscribeTopics();
    
    MqttSocket _espClient;
    PubSubClient _client;
    uint32_t _lastConnectAttemptMs = 0;
    uint32_t _reconnectDelayMs = 0;   // minimum gap from the last connect attempt to the next one
};

extern MqttManager mqttManager;

void initMqttTask();

#endif
