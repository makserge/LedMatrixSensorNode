#include "mqtt_manager.h"
#include "room_calibration.h"

MqttManager mqttManager;

extern Co2Sensor co2Sensor;
extern TempHumiditySensor tempHumSensor;
extern Ld2412Sensor ld2412Sensor;
extern Ld2454Sensor ld2454Sensor;

static bool isHexChar(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

static uint8_t hexNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return c - 'A' + 10;
}

static bool isSpaceChar(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

// Decodes an RLE icon - runs of [runLength, r, g, b] in row-major order - into a
// whole panel of colors. Pixels the runs don't cover stay black, never the previous
// icon. The payload is hex text if it holds only hex digits and whitespace (skipped
// anywhere: the final newline of mosquitto_pub -f or a YAML block, wrapped lines),
// else raw bytes. Returns false, after one log line saying why, if it isn't a usable icon.
static bool decodeIconPayload(const byte* payload, unsigned int length, uint16_t* pixels) {
    unsigned int hexDigits = 0;
    int firstNonHex = -1;   // offset of the first byte that is neither hex digit nor whitespace
    bool printable = true;  // nothing but printable ASCII and whitespace
    for (unsigned int i = 0; i < length; i++) {
        char c = (char)payload[i];
        if (isHexChar(c)) hexDigits++;
        else if (!isSpaceChar(c) && firstNonHex < 0) firstNonHex = (int)i;
        if (!isSpaceChar(c) && (payload[i] < 0x20 || payload[i] > 0x7E)) printable = false;
    }
    bool hexText = firstNonHex < 0;
    if (hexText && hexDigits == 0) {
        Serial.println("[ICON] ignored: empty payload");
        return false;
    }
    if (hexText && hexDigits % 8 != 0) {
        Serial.printf("[ICON] rejected: %u hex digits, need a multiple of 8 (4 bytes per run)\n", hexDigits);
        return false;
    }
    if (!hexText && printable) {
        // As raw bytes it could only be black (every color byte <= 0x7E): it is hex with a typo.
        Serial.printf("[ICON] rejected: '%c' at offset %d is not a hex digit\n",
                      (char)payload[firstNonHex], firstNonHex);
        return false;
    }
    if (!hexText && length % 4 != 0) {
        Serial.printf("[ICON] rejected: %u raw bytes, need a multiple of 4 (4 bytes per run)\n", length);
        return false;
    }

    memset(pixels, 0, MATRIX_TOTAL_PIXELS * sizeof(uint16_t));
    uint8_t run[4];
    uint8_t runBytes = 0;
    uint8_t value = 0;
    uint8_t nibbles = 0;
    unsigned int pixelCount = 0;
    for (unsigned int i = 0; i < length; i++) {
        if (hexText) {
            char c = (char)payload[i];
            if (isSpaceChar(c)) continue;
            value = (uint8_t)((value << 4) | hexNibble(c));
            if (++nibbles < 2) continue;
            nibbles = 0;
        } else {
            value = payload[i];
        }
        run[runBytes++] = value;
        if (runBytes < 4) continue;
        runBytes = 0;
        if (pixelCount + run[0] > MATRIX_TOTAL_PIXELS) {
            Serial.printf("[ICON] rejected: runs cover more than %u pixels (%ux%u)\n",
                          (unsigned)MATRIX_TOTAL_PIXELS, (unsigned)MATRIX_WIDTH, (unsigned)MATRIX_HEIGHT);
            return false;
        }
        uint16_t color = MatrixPanel_I2S_DMA::color565(run[1], run[2], run[3]); // >127 per channel = on
        for (uint8_t n = 0; n < run[0]; n++) pixels[pixelCount++] = color;
    }
    if (pixelCount == 0) {
        Serial.println("[ICON] rejected: runs cover no pixels");
        return false;
    }
    Serial.printf("[ICON] %s payload, %u of %u pixels set, showing icon view\n",
                  hexText ? "hex" : "raw", pixelCount, (unsigned)MATRIX_TOTAL_PIXELS);
    return true;
}

// Message text as the panel shows it: upper case, control characters as spaces,
// surrounding whitespace dropped, and each UTF-8 sequence as one character (drawn as
// the font's placeholder) instead of one per byte. Returns the length.
static int prepareMessageText(const byte* payload, unsigned int length, char* out, int outCap) {
    int len = 0;
    for (unsigned int i = 0; i < length && len < outCap - 1; i++) {
        uint8_t c = payload[i];
        if (c == 0xC2 && i + 1 < length && payload[i + 1] == 0xB0) c = '*'; // UTF-8 degree sign: the font's one
        if (c >= 0x80 && c < 0xC0) continue; // UTF-8 continuation byte
        if (c < 0x20 || c == 0x7F) c = ' ';
        if (c == ' ' && len == 0) continue;
        out[len++] = (char)toupper(c);
    }
    while (len > 0 && out[len - 1] == ' ') len--;
    out[len] = '\0';
    return len;
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
    if (strcasecmp(topic, MQTT_LD2454_CAL_SET_TOPIC) == 0) {
        // Room calibration: JSON with any subset of the keys; the state topic echoes the result.
        if (roomCalibration.applyJson((const char*)payload, length)) {
            ld2454Sensor.setCalibration(roomCalibration.get());
            roomCalibration.save();
            mqttManager.publishCalibrationState();
        }
        return;
    }
    if (strcasecmp(topic, MQTT_DISPLAY_ICON_TOPIC) == 0) {
        // Decoded aside and handed over whole: the shown icon never mixes with this one.
        static uint16_t iconPixels[MATRIX_TOTAL_PIXELS];
        if (decodeIconPayload(payload, length, iconPixels)) {
            DisplayManager::showIcon(iconPixels);
        }
    }
    else if (strcasecmp(topic, MQTT_DISPLAY_MSG_TOPIC) == 0) {
        char text[MQTT_MSG_MAX_LEN];
        if (prepareMessageText(payload, length, text, sizeof(text)) == 0) {
            // E.g. a retained message being cleared: keep the current view rather than
            // blanking the panel for the message's duration.
            Serial.println("[MQTT] empty message ignored");
            return;
        }
        DisplayManager::setCustomMessage(text);
    }
    else if (strcasecmp(topic, MQTT_BEEPER_TOPIC) == 0) {
        DisplayManager::requestBeep(); // sounded by the display task, the beeper's only driver
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
    }
}

void mqttTask(void *pvParameters) {
    mqttManager.begin();
    mqttManager.setCallback();
    
    unsigned long lastPublishTime = 0;
    unsigned long lastRadarPublishTime = 0;
    
    for (;;) {
        waitWhileOtaPaused();
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
        MQTT_TASK_PRIORITY, NULL, 1
    );
}

void MqttManager::begin() {
    _client.setClient(_espClient);
    _client.setServer(MQTT_HOST, MQTT_PORT);
    _client.setBufferSize(4096); 
    _client.setSocketTimeout(MQTT_SOCKET_TIMEOUT_S);
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
    _client.subscribe(MQTT_LD2454_CAL_SET_TOPIC);
}

void MqttManager::update() {
    if (WiFi.status() != WL_CONNECTED) {
        return; 
    }

    if (!_client.connected()) {
        // Back off between attempts: a refused login or a closed broker port used to be
        // retried every 40 ms, flooding the broker log (and tripping flapping bans).
        uint32_t nowMs = millis();
        if (nowMs - _lastConnectAttemptMs < _reconnectDelayMs) {
            return;
        }
        _lastConnectAttemptMs = nowMs;

        Serial.print("Attempting MQTT connection...");
        
        if (_client.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASS, 
                            MQTT_STATUS_TOPIC, 1, true, "offline")) {
            Serial.println("MQTT connected");
            // Not 0: a broker that drops the session right away (e.g. a duplicate
            // client ID) still gets a gap between reconnects.
            _reconnectDelayMs = MQTT_RECONNECT_MIN_MS;
            
            sendDiscovery();
            subscribeTopics();
            
            _client.publish(MQTT_STATUS_TOPIC, "online", true);
            publishCalibrationState();
        } else {
            _reconnectDelayMs = constrain(_reconnectDelayMs * 2, MQTT_RECONNECT_MIN_MS, MQTT_RECONNECT_MAX_MS);
            Serial.printf("MQTT failed, rc=%d, retrying in %lu s\n",
                          _client.state(), (unsigned long)(_reconnectDelayMs / 1000));
        }
    }
    _client.loop();
}

// A field is left out of the state JSON while its sensor has no fresh value. The
// templates turn a missing field into "None", which Home Assistant shows as unknown;
// a plain {{value_json.co2}} would render "", which HA ignores, keeping the old value
// (and every state message restarts expire_after, so that wouldn't catch it either).
void MqttManager::sendDiscovery() {
    publishConfig("co2", "CO2", "ppm", "carbon_dioxide",
                  "{{ value_json.co2 if 'co2' in value_json else None }}");
    publishConfig("temp", "Temperature", "°C",  "temperature",
                  "{{ value_json.temp if 'temp' in value_json else None }}");
    publishConfig("hum", "Humidity", "%", "humidity",
                  "{{ value_json.hum if 'hum' in value_json else None }}");
    publishConfig("lux", "Illuminance", "lx", "illuminance",
                  "{{ value_json.lux if 'lux' in value_json else None }}");

    // ── LD2454 radar: presence, areas and targets (JSON on MQTT_LD2454_TOPIC) ──
    publishRadarEntity("binary_sensor", "ld2454_presence", "LD2454 Presence",
                       "{{ 'ON' if value_json.presence else 'OFF' }}", nullptr, "occupancy", nullptr, false);
    publishRadarEntity("sensor", "ld2454_count", "LD2454 Target Count",
                       "{{ value_json.count }}", nullptr, nullptr, "measurement", false);
    publishRadarEntity("binary_sensor", "ld2454_multi_target", "LD2454 Multi-Target Mode",
                       "{{ 'ON' if value_json.multi_target else 'OFF' }}", nullptr, nullptr, nullptr, true);
    for (uint8_t a = 0; a < AREA_COUNT; a++) {
        const String tpl = "{{ 'ON' if value_json.areas[" + String((int)a) + "] else 'OFF' }}";
        publishRadarEntity("binary_sensor", "ld2454_area_" + String((int)(a + 1)),
                           "LD2454 Area " + String((int)(a + 1)) + " Occupied",
                           tpl, nullptr, "occupancy", nullptr, false);
    }

    // Per target slot: a slot without a target reads None (unknown), like the calibration-free sensors.
    static const char* const kTargetKeys[]  = {"x", "y", "speed", "distance", "angle", "room_x", "room_y"};
    static const char* const kTargetNames[] = {"X", "Y", "Speed", "Distance", "Angle", "Room X", "Room Y"};
    static const char* const kTargetUnits[] = {"cm", "cm", "cm/s", "cm", "°", "cm", "cm"};
    for (uint8_t t = 0; t < LD2454_MAX_TARGETS; t++) {
        const String sel = String("value_json.targets | selectattr('id', 'eq', ") + (int)t + ")";
        const String id = "ld2454_t" + String((int)t) + "_";
        const String name = "LD2454 Target " + String((int)(t + 1)) + " ";
        for (uint8_t k = 0; k < 7; k++) {
            const String tpl = "{{ " + sel + " | map(attribute='" + kTargetKeys[k] + "') | first | default(None) }}";
            publishRadarEntity("sensor", id + kTargetKeys[k], name + kTargetNames[k], tpl,
                               kTargetUnits[k], nullptr, "measurement", false);
        }
        const String boundaryTpl = "{{ 'ON' if (" + sel + " | map(attribute='in_boundary') | first | default(false)) else 'OFF' }}";
        publishRadarEntity("binary_sensor", id + "in_boundary", name + "In Boundary", boundaryTpl,
                           nullptr, nullptr, nullptr, false);
    }

    // ── Room calibration: config entities that write MQTT_LD2454_CAL_SET_TOPIC ──
    publishNumber("ld2454_radar_x", "LD2454 Mount X", "radar_x", -2000, 2000, 1, "cm");
    publishNumber("ld2454_radar_y", "LD2454 Mount Y", "radar_y", -2000, 2000, 1, "cm");
    publishNumber("ld2454_radar_z", "LD2454 Mount Height", "radar_z", 50, 400, 1, "cm");
    publishNumber("ld2454_yaw", "LD2454 Mount Yaw", "yaw", -180, 180, 0.1f, "°");
    publishNumber("ld2454_pitch", "LD2454 Mount Pitch", "pitch", -90, 90, 0.5f, "°");
    publishNumber("ld2454_roll", "LD2454 Mount Roll", "roll", -90, 90, 0.5f, "°");
    publishNumber("ld2454_presence_timeout", "LD2454 Presence Timeout", "presence_timeout_s", 0.5f, 60, 0.5f, "s");
    publishNumber("ld2454_area_hysteresis", "LD2454 Area Hysteresis", "area_hysteresis", 0, 200, 1, "cm");
    publishNumber("ld2454_area_still_speed", "LD2454 Area Still Speed", "area_still_speed", 0, 100, 1, "cm/s");
    publishNumber("ld2454_area_confirm", "LD2454 Area Confirm Delay", "area_confirm_s", 0, 10, 0.1f, "s");
    publishNumber("ld2454_area_clear", "LD2454 Area Clear Delay", "area_clear_s", 0, 30, 0.5f, "s");
    publishNumber("ld2454_area_pass_speed", "LD2454 Area Pass Speed", "area_pass_speed", 0, 300, 1, "cm/s");
    {
        JsonDocument sw;
        sw["name"] = "LD2454 Gate Presence To Room";
        sw["stat_t"] = MQTT_LD2454_CAL_STATE_TOPIC;
        sw["val_tpl"] = "{{ 'ON' if value_json.gates_presence else 'OFF' }}";
        sw["cmd_t"] = MQTT_LD2454_CAL_SET_TOPIC;
        sw["pl_on"] = "{\"gates_presence\":true}";
        sw["pl_off"] = "{\"gates_presence\":false}";
        sw["ent_cat"] = "config";
        sw["icon"] = "mdi:vector-polygon";
        publishEntity("switch", "ld2454_gates_presence", sw);
    }
    publishText("ld2454_polygon", "LD2454 Room Outline", "polygon");
    publishText("ld2454_area_1_outline", "LD2454 Area 1 Outline", "area_1");
    publishText("ld2454_area_2_outline", "LD2454 Area 2 Outline", "area_2");
    publishText("ld2454_area_3_outline", "LD2454 Area 3 Outline", "area_3");
}

void MqttManager::publishEntity(const char* component, const String& id, JsonDocument& doc) {
    char configTopic[160];
    snprintf(configTopic, sizeof(configTopic), "%s/%s/%s_%s/config",
             MQTT_DISCOVERY_PREFIX, component, MQTT_CLIENT_ID, id.c_str());

    doc["uniq_id"] = String(MQTT_CLIENT_ID) + "_" + id;
    doc["avty_t"] = MQTT_STATUS_TOPIC;
    JsonObject dev = doc["dev"].to<JsonObject>();
    dev["ids"] = MQTT_CLIENT_ID;
    dev["name"] = WIFI_PORTAL_NAME;
    dev["mf"] = "";
    dev["mdl"] = "";

    char buffer[896];
    serializeJson(doc, buffer, sizeof(buffer));
    _client.publish(configTopic, buffer, true);
}

void MqttManager::publishRadarEntity(const char* component, const String& id, const String& name, const String& valTpl,
                                     const char* unit, const char* devClass, const char* stateClass, bool diagnostic) {
    JsonDocument doc;
    doc["name"] = name;
    doc["stat_t"] = MQTT_LD2454_TOPIC;
    doc["val_tpl"] = valTpl;
    doc["exp_aft"] = MQTT_RADAR_EXPIRE_AFTER_S;
    if (unit) doc["unit_of_meas"] = unit;
    if (devClass) doc["dev_cla"] = devClass;
    if (stateClass) doc["stat_cla"] = stateClass;
    if (diagnostic) doc["ent_cat"] = "diagnostic";
    publishEntity(component, id, doc);
}

void MqttManager::publishNumber(const char* id, const char* name, const char* key, float minV, float maxV,
                                float step, const char* unit) {
    JsonDocument doc;
    doc["name"] = name;
    doc["stat_t"] = MQTT_LD2454_CAL_STATE_TOPIC;
    doc["val_tpl"] = String("{{ value_json.") + key + " }}";
    doc["cmd_t"] = MQTT_LD2454_CAL_SET_TOPIC;
    doc["cmd_tpl"] = String("{\"") + key + "\":{{ value }}}";
    doc["min"] = minV;
    doc["max"] = maxV;
    doc["step"] = step;
    doc["mode"] = "box";
    doc["unit_of_meas"] = unit;
    doc["ent_cat"] = "config";
    publishEntity("number", id, doc);
}

void MqttManager::publishText(const char* id, const char* name, const char* key) {
    JsonDocument doc;
    doc["name"] = name;
    doc["stat_t"] = MQTT_LD2454_CAL_STATE_TOPIC;
    doc["val_tpl"] = String("{{ value_json.") + key + " }}";
    doc["cmd_t"] = MQTT_LD2454_CAL_SET_TOPIC;
    doc["cmd_tpl"] = String("{\"") + key + "\":\"{{ value }}\"}";
    doc["min"] = 0;
    doc["max"] = 255;
    doc["mode"] = "text";
    doc["ent_cat"] = "config";
    publishEntity("text", id, doc);
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
    doc["stat_cla"] = "measurement"; // HA keeps long-term statistics only for sensors with a state class
    doc["exp_aft"] = MQTT_EXPIRE_AFTER_S; // no state for this long (node hung or gone) -> unavailable
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
    // Each field only while its own sensor has a fresh, valid value, so a dead SCD4x no
    // longer silences the other sensors and a failed one isn't reported as a live value.
    // Sent even when empty ("{}"), so HA shows all four as unknown rather than stale.
    JsonDocument doc;
    JsonObject state = doc.to<JsonObject>();

    if (co2Sensor.hasFreshData()) {
        state["co2"] = co2Sensor.getCo2();
    }
    if (tempHumSensor.hasFreshTemp()) {
        state["temp"] = round(tempHumSensor.getTemp() * 100.0f) / 100.0f;
    }
    if (tempHumSensor.hasFreshHum()) {
        state["hum"] = round(tempHumSensor.getHum() * 100.0f) / 100.0f;
    }
    if (lightSensor.hasFreshData()) {
        state["lux"] = round(lightSensor.getCurrentLux() * 100.0f) / 100.0f;
    }

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

    // LD2454: presence, occupancy areas and targets as one JSON object. Targets carry the
    // radar frame (x, y, distance) and the room frame (room_x, room_y), in cm.
    JsonDocument doc;
    doc["presence"] = ld2454Sensor.isPresent();
    doc["multi_target"] = ld2454Sensor.isMultiTargetEnabled();
    JsonArray areas = doc["areas"].to<JsonArray>();
    for (uint8_t a = 0; a < AREA_COUNT; a++) areas.add(ld2454Sensor.isAreaOccupied(a));

    JsonArray targets = doc["targets"].to<JsonArray>();
    uint8_t count = 0;
    for (uint8_t i = 0; i < ld2454Sensor.getTargetCount(); i++) {
        Ld2454TargetData t = ld2454Sensor.getTarget(i);
        if (!t.valid) continue;
        count++;
        JsonObject to = targets.add<JsonObject>();
        to["id"] = i;
        to["x"] = t.x / 10.0f;
        to["y"] = t.y / 10.0f;
        to["speed"] = t.speed;
        to["distance"] = t.distance / 10.0f;
        to["angle"] = roundf(t.angleDeg * 10.0f) / 10.0f;
        to["room_x"] = roundf(t.roomX * 10.0f) / 10.0f;
        to["room_y"] = roundf(t.roomY * 10.0f) / 10.0f;
        to["in_boundary"] = t.inBoundary;
    }
    doc["count"] = count;

    char buffer[1024];
    serializeJson(doc, buffer, sizeof(buffer));
    _client.publish(MQTT_LD2454_TOPIC, buffer);
}

void MqttManager::publishCalibrationState() {
    char buffer[1024];
    const size_t n = roomCalibration.toJson(buffer, sizeof(buffer));
    if (n > 0 && n < sizeof(buffer)) {
        _client.publish(MQTT_LD2454_CAL_STATE_TOPIC, buffer, true);
    }
}
