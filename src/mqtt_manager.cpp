#include "mqtt_manager.h"

MqttManager mqttManager;

extern Co2Sensor co2Sensor;
extern TempHumiditySensor tempHumSensor;

static void onMqttMessageReceived(char* topic, byte* payload, unsigned int length) {
    char payloadBuf[MQTT_MSG_MAX_LEN + 1];
    unsigned int len = length > MQTT_MSG_MAX_LEN ? MQTT_MSG_MAX_LEN : length;
    memcpy(payloadBuf, payload, len);
    payloadBuf[len] = '\0';

    if (strcasecmp(topic, MQTT_DISPLAY_MODE_TOPIC) == 0) {
        if (strcasecmp(payloadBuf, "time") == 0) {
            DisplayManager::setView(VIEW_TIME);
        } else if (strcasecmp(payloadBuf, "temp") == 0) {
            DisplayManager::setView(VIEW_TEMP_DATA);
        } else if (strcasecmp(payloadBuf, "co2") == 0) {
            DisplayManager::setView(VIEW_CO2);
        } else if (strcasecmp(payloadBuf, "spectrum") == 0) {
            DisplayManager::setView(VIEW_SPECTRUM);
        }
    } 
    else if (strcasecmp(topic, MQTT_DISPLAY_MSG_TOPIC) == 0) {
        for (unsigned int i = 0; i < len; i++) {
            payloadBuf[i] = toupper((unsigned char)payloadBuf[i]);
        }
        DisplayManager::setCustomMessage(payloadBuf);
    }
    else if (strcasecmp(topic, MQTT_BEEPER_TOPIC) == 0) {
        digitalWrite(BEEPER_PIN, HIGH);
        delay(BEEP_DURATION_MS);
        digitalWrite(BEEPER_PIN, LOW);
    }
}

void mqttTask(void *pvParameters) {
    mqttManager.begin();
    mqttManager.setCallback();
    
    unsigned long lastPublishTime = 0;
    
    for (;;) {
        mqttManager.update();

        if (mqttManager.isConnected()) {
            unsigned long now = millis();
            if (now - lastPublishTime >= MQTT_PUBLISH_INTERVAL) {
                lastPublishTime = now;
                mqttManager.publishSensorData();
            }
        }

        vTaskDelay(pdMS_TO_TICKS(40));
    }
}

void initMqttTask() {
    xTaskCreatePinnedToCore(
        mqttTask,
        "MqttTask",
        MQTT_TASK_STACK_SIZE,
        NULL,
        MQTT_TASK_PRIORITY,
        NULL,
        1
    );
}

void MqttManager::begin() {
    pinMode(BEEPER_PIN, OUTPUT);
    digitalWrite(BEEPER_PIN, LOW);

    _client.setClient(_espClient);
    _client.setServer(MQTT_HOST, MQTT_PORT);
    _client.setBufferSize(512); 
}

void MqttManager::setCallback() {
    _client.setCallback(onMqttMessageReceived);
}

void MqttManager::subscribeTopics() {
    _client.subscribe(MQTT_DISPLAY_MODE_TOPIC);
    _client.subscribe(MQTT_DISPLAY_MSG_TOPIC);
    _client.subscribe(MQTT_BEEPER_TOPIC);
}

void MqttManager::update() {
    if (WiFi.status() != WL_CONNECTED) {
        return; 
    }

    if (!_client.connected()) {
        Serial.print("Attempting MQTT connection...");
        
        if (_client.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASS, 
                            MQTT_STATUS_TOPIC, 1, true, "offline")) {
            Serial.println("MQTT connected");
            
            sendDiscovery();
            subscribeTopics();
            
            _client.publish(MQTT_STATUS_TOPIC, "online", true);
        } else {
            Serial.print("MQTT failed, rc=");
            Serial.print(_client.state());
            Serial.println(" try again in next cycle");
        }
    }
    _client.loop();
}

void MqttManager::sendDiscovery() {
    publishConfig("co2", "CO2", "ppm", "carbon_dioxide", "{{value_json.co2}}");
    publishConfig("temp", "Temperature", "°C",  "temperature",  "{{value_json.temp}}");
    publishConfig("hum", "Humidity", "%", "humidity", "{{value_json.hum}}");
    publishConfig("lux", "Illuminance", "lx", "illuminance", "{{value_json.lux}}");
}

void MqttManager::publishConfig(const char* id, const char* name, const char* unit, const char* devClass, const char* valTpl) {
    JsonDocument doc;
    char configTopic[128];
    
    snprintf(configTopic, sizeof(configTopic), MQTT_CONFIG_TOPIC_TPL, 
             MQTT_DISCOVERY_PREFIX, MQTT_CLIENT_ID, id);

    doc["name"] = name;
    doc["stat_t"] = MQTT_STATE_TOPIC;
    doc["avty_t"] = MQTT_STATUS_TOPIC;
    doc["unit_of_meas"] = unit;
    doc["dev_cla"] = devClass;
    doc["val_tpl"] = valTpl;
    doc["uniq_id"] = String(MQTT_CLIENT_ID) + "_" + id;
    
    JsonObject dev = doc["dev"].to<JsonObject>();
    dev["ids"] = MQTT_CLIENT_ID;
    dev["name"] = WIFI_PORTAL_NAME;
    dev["mf"] = "";
    dev["mdl"] = "";

    char buffer[512];
    serializeJson(doc, buffer, sizeof(buffer));
    _client.publish(configTopic, buffer, true);
}

void MqttManager::publishSensorData() {
    JsonDocument doc;
    
    doc["co2"] = co2Sensor.getCo2();
    doc["temp"] = round(tempHumSensor.getTemp() * 100.0f) / 100.0f;
    doc["hum"] = round(tempHumSensor.getHum() * 100.0f) / 100.0f;
    doc["lux"] = round(lightSensor.getCurrentLux() * 100.0f) / 100.0f;

    char buffer[256];
    serializeJson(doc, buffer, sizeof(buffer));
    _client.publish(MQTT_STATE_TOPIC, buffer);
}

bool MqttManager::isConnected() {
    return _client.connected();
}
