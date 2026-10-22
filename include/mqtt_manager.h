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
#include "ld2454_frames.h"
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
    void publishLegacyRadar(uint32_t nowMs);
    void serviceFrameStream();
    bool isConnected();
    void setCallback();

private:
    void sendDiscovery();
    bool publishDiscovery(const char* component, const char* id, JsonDocument& doc);
    void publishConfig(const char* id, const char* name, const char* unit, const char* devClass, const char* valTpl);
    void publishLd2454LegacyJson();
    void sendFrameBatch(const Ld2454Batch& batch, bool& publishFailed);
    void subscribeTopics();
    
    MqttSocket _espClient;
    PubSubClient _client;
    uint32_t _lastConnectAttemptMs = 0;
    uint32_t _reconnectDelayMs = 0;   // minimum gap from the last connect attempt to the next one

    char _bootId[9] = "";             // 8 hex digits, random per boot (frames "b")
    Ld2454FrameBatcher _frameBatcher;
    uint32_t _framesDroppedOffline = 0;   // frames in batches closed while MQTT was down
    uint32_t _framesDroppedPublish = 0;   // frames in batches whose publish failed
    uint32_t _framesLogMs = 0;
    uint32_t _framesLoggedTotal = 0;

    int8_t _ld2412Published = -1;     // last LD2412 value sent since the connect; -1 = none yet
    uint32_t _ld2412PublishedMs = 0;
    bool _ld2454LegacyJson = false;   // the 1 Hz LD2454 JSON; P2: 'Legacy radar JSON' switch (#11)
    uint32_t _ld2454LegacyMs = 0;
};

extern MqttManager mqttManager;

void initMqttTask();

#endif
