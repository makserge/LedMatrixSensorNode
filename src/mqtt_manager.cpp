#include "mqtt_manager.h"

MqttManager mqttManager;

extern Co2Sensor co2Sensor;
extern TempHumiditySensor tempHumSensor;
extern Ld2412Sensor ld2412Sensor;
extern Ld2450Sensor ld2450Sensor;

static bool isHexChar(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

static uint8_t hexNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return c - 'A' + 10;
}

static size_t decodeHexPayload(const byte* payload, unsigned int length, uint8_t* outBuf, size_t outBufCap) {
    if (length == 0 || length % 2 != 0) return 0;
    size_t outLen = length / 2;
    if (outLen > outBufCap) return 0;

    for (unsigned int i = 0; i < length; i++) {
        if (!isHexChar((char)payload[i])) return 0;
    }
    for (size_t i = 0; i < outLen; i++) {
        outBuf[i] = (hexNibble((char)payload[i * 2]) << 4) | hexNibble((char)payload[i * 2 + 1]);
    }
    return outLen;
}

static bool parseColorPayload(const char* payload, uint8_t& r, uint8_t& g, uint8_t& b) {
    const char* p = payload;
    if (p[0] == '#') p++;

    int rv, gv, bv;
    if (sscanf(p, "%d,%d,%d", &rv, &gv, &bv) == 3) {
        r = (uint8_t)constrain(rv, 0, 255);
        g = (uint8_t)constrain(gv, 0, 255);
        b = (uint8_t)constrain(bv, 0, 255);
        return true;
    }

    size_t len = strlen(p);
    if (len == 6) {
        bool allHex = true;
        for (size_t i = 0; i < len; i++) {
            if (!isxdigit((unsigned char)p[i])) { allHex = false; break; }
        }
        if (allHex) {
            long val = strtol(p, NULL, 16);
            r = (uint8_t)((val >> 16) & 0xFF);
            g = (uint8_t)((val >> 8) & 0xFF);
            b = (uint8_t)(val & 0xFF);
            return true;
        }
    }

    return false;
}

static void onMqttMessageReceived(char* topic, byte* payload, unsigned int length) {
    if (strcasecmp(topic, MQTT_DISPLAY_ICON_TOPIC) == 0) {
       static uint8_t iconDecodeBuf[MATRIX_TOTAL_PIXELS * 4];

        const byte* iconPayload = payload;
        unsigned int iconLength = length;

        size_t hexDecodedLen = decodeHexPayload(payload, length, iconDecodeBuf, sizeof(iconDecodeBuf));
        if (hexDecodedLen > 0) {
            iconPayload = iconDecodeBuf;
            iconLength = (unsigned int)hexDecodedLen;
            Serial.printf("[ICON] received %u hex chars -> %u binary bytes\n", length, iconLength);
        } else {
            Serial.printf("[ICON] received %u raw binary bytes\n", length);
        }

        if (iconLength == 0 || iconLength % 4 != 0) {
            Serial.println("[ICON] rejected: decoded payload length must be a non-zero multiple of 4");
            return;
        }

        int currentPixelIndex = 0;
        for (unsigned int i = 0; i < iconLength; i += 4) {
            byte runLength = iconPayload[i];
            byte r          = iconPayload[i + 1];
            byte g          = iconPayload[i + 2];
            byte b          = iconPayload[i + 3];

            for (int count = 0; count < runLength; count++) {
                if (currentPixelIndex >= MATRIX_TOTAL_PIXELS) break;

                int x = currentPixelIndex % MATRIX_WIDTH;
                int y = currentPixelIndex / MATRIX_WIDTH;

                DisplayManager::drawIconPixel(x, y, r, g, b);
                currentPixelIndex++;
            }
        }
        Serial.printf("[ICON] decoded %d pixels, showing icon view\n", currentPixelIndex);
        DisplayManager::showIconView();
    }
    else {
        char payloadBuf[MQTT_MSG_MAX_LEN + 1];
        unsigned int len = length > MQTT_MSG_MAX_LEN ? MQTT_MSG_MAX_LEN : length;
        memcpy(payloadBuf, payload, len);
        payloadBuf[len] = '\0';

        if (strcasecmp(topic, MQTT_DISPLAY_MODE_TOPIC) == 0) {
            if (strcasecmp(payloadBuf, "time") == 0) {
                DisplayManager::selectView(VIEW_TIME);
            } else if (strcasecmp(payloadBuf, "temp") == 0) {
                DisplayManager::selectView(VIEW_TEMP_DATA);
            } else if (strcasecmp(payloadBuf, "co2") == 0) {
                DisplayManager::selectView(VIEW_CO2);
            } else if (strcasecmp(payloadBuf, "spectrum") == 0) {
                DisplayManager::selectView(VIEW_SPECTRUM);
            }
        } 
        else if (strcasecmp(topic, MQTT_DISPLAY_MSG_TOPIC) == 0) {
            for (unsigned int i = 0; i < len; i++) {
                payloadBuf[i] = toupper((unsigned char)payloadBuf[i]);
            }
            DisplayManager::setCustomMessage(payloadBuf);
        }
        else if (strcasecmp(topic, MQTT_DISPLAY_POWER_TOPIC) == 0) {
            if (strcmp(payloadBuf, "1") == 0) {
                DisplayManager::setDisplayOff(false);
            } else if (strcmp(payloadBuf, "0") == 0) {
                DisplayManager::setDisplayOff(true);
            }
        }
        else if (strcasecmp(topic, MQTT_DISPLAY_COLOR_TOPIC) == 0) {
            uint8_t r, g, b;
            if (parseColorPayload(payloadBuf, r, g, b)) {
                DisplayManager::setClockColor(r, g, b);
            }
        }
        else if (strcasecmp(topic, MQTT_BEEPER_TOPIC) == 0) {
            digitalWrite(BEEPER_PIN, HIGH);
            delay(BEEP_DURATION_MS);
            digitalWrite(BEEPER_PIN, LOW);
        }
    }
}

void mqttTask(void *pvParameters) {
    mqttManager.begin();
    mqttManager.setCallback();
    
    unsigned long lastPublishTime = 0;
    unsigned long lastRadarPublishTime = 0;
    
    for (;;) {
        mqttManager.update();

        if (mqttManager.isConnected()) {
            unsigned long now = millis();
            if (now - lastPublishTime >= MQTT_PUBLISH_INTERVAL) {
                lastPublishTime = now;
                mqttManager.publishSensorData();
            }
            if (now - lastRadarPublishTime >= MQTT_RADAR_PUBLISH_INTERVAL) {
                lastRadarPublishTime = now;
                mqttManager.publishRadarData();
            }
        }

        vTaskDelay(pdMS_TO_TICKS(40));
    }
}

void initMqttTask() {
    xTaskCreatePinnedToCore(
        mqttTask, "MqttTask", MQTT_TASK_STACK_SIZE, NULL,
        MQTT_TASK_PRIORITY, &mqttTaskHandle, 1
    );
}

void MqttManager::begin() {
    pinMode(BEEPER_PIN, OUTPUT);
    digitalWrite(BEEPER_PIN, LOW);

    _client.setClient(_espClient);
    _client.setServer(MQTT_HOST, MQTT_PORT);
    _client.setBufferSize(4096); 
}

void MqttManager::setCallback() {
    _client.setCallback(onMqttMessageReceived);
}

void MqttManager::subscribeTopics() {
    _client.subscribe(MQTT_DISPLAY_MODE_TOPIC);
    _client.subscribe(MQTT_DISPLAY_MSG_TOPIC);
    _client.subscribe(MQTT_DISPLAY_POWER_TOPIC);
    _client.subscribe(MQTT_DISPLAY_COLOR_TOPIC);
    _client.subscribe(MQTT_BEEPER_TOPIC);
    _client.subscribe(MQTT_DISPLAY_ICON_TOPIC);
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
    if (co2Sensor.getCo2() == 0) {
        return;
    }

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

void MqttManager::publishRadarData() {
    // LD2412: simple presence state on its own topic.
    _client.publish(MQTT_LD2412_TOPIC, ld2412Sensor.isPresent() ? "1" : "0", true);

    // LD2450: up to 3 tracked targets as JSON on its own topic.
    JsonDocument doc;
    JsonArray targets = doc["targets"].to<JsonArray>();
    for (uint8_t i = 0; i < ld2450Sensor.getTargetCount(); i++) {
        Ld2450TargetData t = ld2450Sensor.getTarget(i);
        if (!t.valid) continue;
        JsonObject to = targets.add<JsonObject>();
        to["id"] = i;
        to["x"] = t.x;
        to["y"] = t.y;
        to["speed"] = t.speed;
        to["distance"] = t.distance;
    }
    doc["count"] = targets.size();

    char buffer[256];
    serializeJson(doc, buffer, sizeof(buffer));
    _client.publish(MQTT_LD2450_TOPIC, buffer);
}