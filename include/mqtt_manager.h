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
#ifdef BOARD_REV2
#include "radar_constants.h"
#include "radar_power.h"
#endif

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
#ifdef BOARD_REV2
    // HA birth / will (homeassistant/status). Called from the MQTT callback: it only sets fields and the HA state.
    void onHaStatus(bool online);
    // MqttTask loop, connected: the HA-birth resend, the dirty-flag publish of the radar states, presence on change.
    void serviceRadarStates(uint32_t nowMs);
#endif

private:
#ifdef BOARD_REV2
    bool sendDiscovery();             // false at the first refused publish: the rest is not sent
#else
    void sendDiscovery();
#endif
    bool publishDiscovery(const char* component, const char* id, JsonDocument& doc);
#ifdef BOARD_REV2
    bool publishConfig(const char* id, const char* name, const char* unit, const char* devClass, const char* valTpl);
#else
    void publishConfig(const char* id, const char* name, const char* unit, const char* devClass, const char* valTpl);
#endif
    void publishLd2454LegacyJson();
    void sendFrameBatch(const Ld2454Batch& batch, bool& publishFailed);
#ifdef BOARD_REV2
    bool subscribeTopics();           // false at the first refused SUBSCRIBE: the rest is not sent
#else
    void subscribeTopics();
#endif
#ifdef BOARD_REV2
    bool publishSwitchConfig(const char* id, const char* name, const char* cmdTopic, const char* stateTopic,
                             const char* icon, const char* entCat /*nullable*/);
    bool publishTimeConfig(const char* id, const char* name, const char* cmdTopic, const char* stateTopic,
                           const char* icon);
    bool publishBinaryConfig(const char* id, const char* name, const char* stateTopic, const char* devClass /*nullable*/,
                             const char* entCat /*nullable*/, const char* icon /*nullable*/, const char* avty2Topic /*nullable*/);
    bool publishEnumSensorConfig(const char* id, const char* name, const char* stateTopic, const char* const* options,
                                 size_t nOptions, const char* attrTopic, const char* entCat, const char* icon);
    bool clearRetainedCommands();     // false at the first refused publish: the rest is not sent
    bool publishRadarStates(const RadarStatus& s);   // all retained; false if any publish failed
    bool presenceNow(const RadarStatus& s);
    void abortBurst(const char* step);               // a connect / birth burst step was refused: close the connection
#endif
    
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
#ifdef BOARD_REV2
    bool _birthPending = false;       // HA announced itself: resend the discovery and the states after a random delay
    uint32_t _birthAtMs = 0;
    uint32_t _birthDelayMs = 0;
    uint32_t _lastDiscoveryMs = 0;    // the last discovery burst (connect or birth resend)
    int8_t _presencePublished = -1;   // 'Radar presence' last sent since the connect; -1 = none yet
    uint32_t _lastTargetMs = 0;       // the last millis() with an LD2454 target (valid while _targetHeld)
    bool _targetHeld = false;         // an LD2454 target was seen less than RADAR_PRESENCE_HOLD_MS ago
#endif
};

extern MqttManager mqttManager;

void initMqttTask();

#endif
