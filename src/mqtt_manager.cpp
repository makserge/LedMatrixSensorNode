#include "mqtt_manager.h"

#include <esp_random.h>
#include <esp_timer.h>

MqttManager mqttManager;

// The frames' "pw" (bit0 LD2454 observing, bit1 LD2412 observing) and "sm" (code of the radar state):
// fixed in P1, which has no radar switch. P2 takes both from the RadarStatus snapshot.
#ifndef BOARD_REV2
static const uint8_t FRAMES_PW_P1 = 3;
static const char FRAMES_SM_P1 = 'A';
#endif
static const size_t FRAMES_DRAIN_CHUNK = 16;              // ring entries copied out per spinlock hold
static const uint32_t FRAMES_LOG_INTERVAL_MS = 60000;     // drop counters logged at most this often

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

#ifdef BOARD_REV2
// Radar commands (the callback runs on MqttTask): validate, hand over, never publish, never write NVS, never block.
static void radarSwitchCommand(RadarCmd cmd, const char* topic, const char* payload) {
    const int v = radarParseOnOff(payload);               // "1" / "0" or ON / OFF in any case; -1 otherwise
    if (v < 0) { Serial.printf("[MQTT] %s '%s' ignored\n", topic, payload); return; }
    radarPowerRequest(cmd, v);                            // spinlock + notify
}
static void radarTimeCommand(RadarCmd cmd, const char* topic, const char* payload) {
    uint16_t minutes;
    if (!radarParseTimeOfDay(payload, &minutes)) { Serial.printf("[MQTT] %s '%s' ignored\n", topic, payload); return; }
    radarPowerRequest(cmd, minutes);
}
#endif

static void onMqttMessageReceived(char* topic, byte* payload, unsigned int length) {
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
#ifdef BOARD_REV2
        else if (strcasecmp(topic, MQTT_RADAR_POWER_CMD) == 0)         radarSwitchCommand(RADAR_CMD_ENABLE, topic, payloadBuf);
        else if (strcasecmp(topic, MQTT_RADAR_SCHEDULE_CMD) == 0)      radarSwitchCommand(RADAR_CMD_SCHEDULE, topic, payloadBuf);
        else if (strcasecmp(topic, MQTT_RADAR_SCHED_OFFLINE_CMD) == 0) radarSwitchCommand(RADAR_CMD_SCHED_OFFLINE, topic, payloadBuf);
        else if (strcasecmp(topic, MQTT_RADAR_LEGACY_CMD) == 0)        radarSwitchCommand(RADAR_CMD_LEGACY, topic, payloadBuf);
        else if (strcasecmp(topic, MQTT_RADAR_NIGHT_START_CMD) == 0)   radarTimeCommand(RADAR_CMD_NIGHT_START, topic, payloadBuf);
        else if (strcasecmp(topic, MQTT_RADAR_NIGHT_END_CMD) == 0)     radarTimeCommand(RADAR_CMD_NIGHT_END, topic, payloadBuf);
        else if (strcasecmp(topic, MQTT_HA_STATUS_TOPIC) == 0) {       // HA birth / will
            if (strcmp(payloadBuf, "online") == 0)       mqttManager.onHaStatus(true);
            else if (strcmp(payloadBuf, "offline") == 0) mqttManager.onHaStatus(false);
        }
#endif
    }
}

void mqttTask(void *pvParameters) {
    mqttManager.begin();
    mqttManager.setCallback();
    
    unsigned long lastPublishTime = 0;
    
    for (;;) {
        waitWhileOtaPaused();
        mqttManager.update();
        mqttManager.serviceFrameStream();   // every loop, connected or not: offline it drops, never queues

        if (mqttManager.isConnected()) {
            unsigned long now = millis();
            if (now - lastPublishTime >= MQTT_PUBLISH_INTERVAL) {
                lastPublishTime = now;
                mqttManager.publishSensorData();
            }
            mqttManager.publishLegacyRadar(now);
#ifdef BOARD_REV2
            mqttManager.serviceRadarStates(now);
#endif
        }

        vTaskDelay(pdMS_TO_TICKS(40));
    }
}

void initMqttTask() {
#ifdef BOARD_REV2
    // The discovery burst (char buffer[512], nested publishes) and publishRadarStates() (char attr[256]) add stack.
    xTaskCreatePinnedToCore(
        mqttTask, "MqttTask", MQTT_TASK_STACK_SIZE + RADAR_MQTT_EXTRA_STACK, NULL,
        MQTT_TASK_PRIORITY, NULL, 1
    );
#else
    xTaskCreatePinnedToCore(
        mqttTask, "MqttTask", MQTT_TASK_STACK_SIZE, NULL,
        MQTT_TASK_PRIORITY, NULL, 1
    );
#endif
}

void MqttManager::begin() {
    _client.setClient(_espClient);
    _client.setServer(MQTT_HOST, MQTT_PORT);
    _client.setBufferSize(4096); 
    _client.setSocketTimeout(MQTT_SOCKET_TIMEOUT_S);
    // setup() has started WiFi before this task exists, so esp_random() draws on RF noise here.
    snprintf(_bootId, sizeof(_bootId), "%08lx", (unsigned long)esp_random());
    Serial.printf("[MQTT] frames boot id %s\n", _bootId);
}

void MqttManager::setCallback() {
    _client.setCallback(onMqttMessageReceived);
}

#ifdef BOARD_REV2
// False at the first refused SUBSCRIBE: the rest is not sent (update() closes the connection).
bool MqttManager::subscribeTopics() {
    return _client.subscribe(MQTT_DISPLAY_MODE_TOPIC)
        && _client.subscribe(MQTT_DISPLAY_MSG_TOPIC)
        && _client.subscribe(MQTT_DISPLAY_POWER_TOPIC)
        && _client.subscribe(MQTT_DISPLAY_COLOR_TOPIC)
        && _client.subscribe(MQTT_BEEPER_TOPIC)
        && _client.subscribe(MQTT_DISPLAY_ICON_TOPIC)
        && _client.subscribe(MQTT_RADAR_POWER_CMD)
        && _client.subscribe(MQTT_RADAR_SCHEDULE_CMD)
        && _client.subscribe(MQTT_RADAR_NIGHT_START_CMD)
        && _client.subscribe(MQTT_RADAR_NIGHT_END_CMD)
        && _client.subscribe(MQTT_RADAR_SCHED_OFFLINE_CMD)
        && _client.subscribe(MQTT_RADAR_LEGACY_CMD)
        && _client.subscribe(MQTT_HA_STATUS_TOPIC);   // HA's retained birth / will arrives right after this
}
#else
void MqttManager::subscribeTopics() {
    _client.subscribe(MQTT_DISPLAY_MODE_TOPIC);
    _client.subscribe(MQTT_DISPLAY_MSG_TOPIC);
    _client.subscribe(MQTT_DISPLAY_POWER_TOPIC);
    _client.subscribe(MQTT_DISPLAY_COLOR_TOPIC);
    _client.subscribe(MQTT_BEEPER_TOPIC);
    _client.subscribe(MQTT_DISPLAY_ICON_TOPIC);
}
#endif

void MqttManager::update() {
#ifdef BOARD_REV2
    // The loss check comes before the Wi-Fi early return: an AP reboot, or the up to 10 min before the switch to the
    // backup network, counts as "HA down" from the moment the node loses Wi-Fi (the grace counts from haLostMs).
    if (WiFi.status() != WL_CONNECTED || !_client.connected()) radarSetHaState(RADAR_HA_UNKNOWN);   // a no-op when unchanged
#endif
    if (WiFi.status() != WL_CONNECTED) {
        // The dead socket would look connected until lwIP gives up on it (up to minutes; PubSubClient
        // checks the keepalive only in loop()), and every publish into it can block MqttTask for ~10 s.
        // Closed now, the session is re-established after the link returns and publishes "online".
        if (_client.connected()) {
            Serial.println("[MQTT] Wi-Fi lost, closing the MQTT connection");
            _espClient.stop();
        }
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
            _ld2412Published = -1;   // resend at once: the retained value may predate the outage
#ifdef BOARD_REV2
            radarSetHaState(RADAR_HA_UNKNOWN);   // before the subscription: a status from before the connect is never reused
            _presencePublished = -1;
            _birthPending = false;               // this connect sends the discovery and the states itself
#endif
            
#ifdef BOARD_REV2
            // The first refused step ends the burst: with the link stalled, every further publish would block MqttTask
            // for ~10 s. The connection is closed and the backoff above retries the whole burst, 'online' last.
            if (!sendDiscovery())         { abortBurst("discovery");     return; }
            if (!clearRetainedCommands()) { abortBurst("command clear"); return; }   // before the subscriptions: the deleted message is not delivered back
            if (!subscribeTopics())       { abortBurst("subscribe");     return; }
            g_stateDirty = false;                // the next lines publish a fresh snapshot anyway
            if (!publishRadarStates(radarStatusSnapshot())) { abortBurst("states"); return; }
            _lastDiscoveryMs = millis();         // for the HA-birth rule
            static bool stackLogged = false;
            if (!stackLogged) {
                stackLogged = true;
                Serial.printf("[MQTT] stack free: %u B\n", (unsigned)uxTaskGetStackHighWaterMark(NULL));
            }
#else
            sendDiscovery();
            subscribeTopics();
#endif
            
#ifdef BOARD_REV2
            if (!_client.publish(MQTT_STATUS_TOPIC, "online", true)) { abortBurst("online"); return; }
#else
            _client.publish(MQTT_STATUS_TOPIC, "online", true);
#endif
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
#ifdef BOARD_REV2
static const char* const RADAR_STATE_OPTIONS[] = {"on", "night_off", "forced_on", "forced_off", "failsafe_on"};

// False at the first refused publish: the rest is not sent (the caller closes the connection, see abortBurst()).
// A config skipped by publishDiscovery() counts as refused too; none is skipped (tools/tests/discovery_payloads_check.py).
bool MqttManager::sendDiscovery() {
    return publishConfig("co2", "CO2", "ppm", "carbon_dioxide",
                         "{{ value_json.co2 if 'co2' in value_json else None }}")
        && publishConfig("temp", "Temperature", "°C",  "temperature",
                         "{{ value_json.temp if 'temp' in value_json else None }}")
        && publishConfig("hum", "Humidity", "%", "humidity",
                         "{{ value_json.hum if 'hum' in value_json else None }}")
        && publishConfig("lux", "Illuminance", "lx", "illuminance",
                         "{{ value_json.lux if 'lux' in value_json else None }}")
        // The 'Radars' switch (radar/power) and the read-only 'Radar power' binary sensor (radar/powered) are two entities.
        && publishSwitchConfig("radar_power",    "Radars",               MQTT_RADAR_POWER_CMD,    MQTT_RADAR_POWER_STATE,    "mdi:radar",         nullptr)
        && publishSwitchConfig("radar_schedule", "Radar night schedule", MQTT_RADAR_SCHEDULE_CMD, MQTT_RADAR_SCHEDULE_STATE, "mdi:weather-night", nullptr)
        && publishBinaryConfig("radar_powered",  "Radar power",          MQTT_RADAR_POWERED_TOPIC,   "power", "diagnostic", nullptr, nullptr)
        && publishBinaryConfig("radar_observing", "Radar observing",     MQTT_RADAR_OBSERVING_TOPIC, nullptr, "diagnostic", "mdi:eye", nullptr)
        && publishEnumSensorConfig("radar_state", "Radar state", MQTT_RADAR_STATE_TOPIC, RADAR_STATE_OPTIONS, 5, MQTT_RADAR_STATE_ATTR_TOPIC, "diagnostic", "mdi:radar")
        && publishBinaryConfig("radar_presence", "Radar presence",       MQTT_PRESENCE_TOPIC, "occupancy", nullptr, nullptr, MQTT_RADAR_OBSERVING_TOPIC)
        && publishSwitchConfig("radar_sched_offline", "Radar schedule without HA", MQTT_RADAR_SCHED_OFFLINE_CMD, MQTT_RADAR_SCHED_OFFLINE_STATE, "mdi:lan-disconnect", "config")
        && publishSwitchConfig("radar_legacy",   "Legacy radar JSON",    MQTT_RADAR_LEGACY_CMD,   MQTT_RADAR_LEGACY_STATE,   "mdi:code-json",     "config")
        && publishTimeConfig("radar_night_start", "Radar night start", MQTT_RADAR_NIGHT_START_CMD, MQTT_RADAR_NIGHT_START_STATE, "mdi:clock-start")
        && publishTimeConfig("radar_night_end",   "Radar night end",   MQTT_RADAR_NIGHT_END_CMD,   MQTT_RADAR_NIGHT_END_STATE,   "mdi:clock-end");
}
#else
void MqttManager::sendDiscovery() {
    publishConfig("co2", "CO2", "ppm", "carbon_dioxide",
                  "{{ value_json.co2 if 'co2' in value_json else None }}");
    publishConfig("temp", "Temperature", "°C",  "temperature",
                  "{{ value_json.temp if 'temp' in value_json else None }}");
    publishConfig("hum", "Humidity", "%", "humidity",
                  "{{ value_json.hum if 'hum' in value_json else None }}");
    publishConfig("lux", "Illuminance", "lx", "illuminance",
                  "{{ value_json.lux if 'lux' in value_json else None }}");
}
#endif

// One retained discovery config for any HA component. It appends uniq_id, avty_t (unless the entity
// has an "avty" list; an avty_t already set keeps its place) and the device block, in that order.
// A config that would not fit DISCOVERY_JSON_MAX is logged and skipped, never truncated.
bool MqttManager::publishDiscovery(const char* component, const char* id, JsonDocument& doc) {
    char topic[128];
    int topicLen = snprintf(topic, sizeof(topic), MQTT_CONFIG_TOPIC_TPL,
                            MQTT_DISCOVERY_PREFIX, component, MQTT_CLIENT_ID, id);
    if (topicLen < 0 || (size_t)topicLen >= sizeof(topic)) {
        Serial.printf("[MQTT] discovery %s/%s: topic too long, skipped\n", component, id);
        return false;
    }

    doc["uniq_id"] = String(MQTT_CLIENT_ID) + "_" + id;
    if (!doc["avty"].is<JsonArray>()) doc["avty_t"] = MQTT_STATUS_TOPIC;

    JsonObject dev = doc["dev"].to<JsonObject>();
    dev["ids"] = MQTT_CLIENT_ID;
    dev["name"] = WIFI_PORTAL_NAME;
    dev["mf"] = "";
    dev["mdl"] = "";

    char buffer[DISCOVERY_JSON_MAX];
    size_t len = measureJson(doc);
    if (doc.overflowed() || len >= sizeof(buffer)) {
        Serial.printf("[MQTT] discovery %s/%s is %u B, skipped\n", component, id, (unsigned)len);
        return false;
    }
    serializeJson(doc, buffer, sizeof(buffer));
    return _client.publish(topic, buffer, true);
}

// The four sensor configs: same topic and same bytes as before publishDiscovery() existed.
#ifdef BOARD_REV2
bool MqttManager::publishConfig(const char* id, const char* name, const char* unit, const char* devClass, const char* valTpl) {
#else
void MqttManager::publishConfig(const char* id, const char* name, const char* unit, const char* devClass, const char* valTpl) {
#endif
    JsonDocument doc;
    doc["name"] = name;
    doc["stat_t"] = MQTT_STATE_TOPIC;
    doc["avty_t"] = MQTT_STATUS_TOPIC;
    doc["unit_of_meas"] = unit;
    doc["dev_cla"] = devClass;
    doc["stat_cla"] = "measurement"; // HA keeps long-term statistics only for sensors with a state class
    doc["exp_aft"] = MQTT_EXPIRE_AFTER_S; // no state for this long (node hung or gone) -> unavailable
    doc["val_tpl"] = valTpl;
#ifdef BOARD_REV2
    return publishDiscovery("sensor", id, doc);
#else
    publishDiscovery("sensor", id, doc);
#endif
}

#ifdef BOARD_REV2
// P2 radar entities. The key order is the byte layout of the payload (tools/tests/discovery_payloads_check.py checks it).
bool MqttManager::publishSwitchConfig(const char* id, const char* name, const char* cmdTopic, const char* stateTopic,
                                      const char* icon, const char* entCat /*nullable*/) {
    JsonDocument doc;
    doc["name"] = name;  doc["cmd_t"] = cmdTopic;  doc["stat_t"] = stateTopic;
    doc["pl_on"] = "1";  doc["pl_off"] = "0";  doc["stat_on"] = "1";  doc["stat_off"] = "0";
    doc["opt"] = false;  doc["ret"] = false;
    if (entCat) doc["ent_cat"] = entCat;
    doc["ic"] = icon;
    return publishDiscovery("switch", id, doc);
}

bool MqttManager::publishTimeConfig(const char* id, const char* name, const char* cmdTopic, const char* stateTopic,
                                    const char* icon) {                  // MQTT `time` entity (HA 2026.5+): no opt, ptrn, min or max
    JsonDocument doc;
    doc["name"] = name;  doc["cmd_t"] = cmdTopic;  doc["stat_t"] = stateTopic;
    doc["ret"] = false;  doc["ent_cat"] = "config";  doc["ic"] = icon;
    return publishDiscovery("time", id, doc);
}

bool MqttManager::publishBinaryConfig(const char* id, const char* name, const char* stateTopic, const char* devClass /*nullable*/,
                                      const char* entCat /*nullable*/, const char* icon /*nullable*/, const char* avty2Topic /*nullable*/) {
    JsonDocument doc;
    doc["name"] = name;  doc["stat_t"] = stateTopic;
    if (devClass) doc["dev_cla"] = devClass;
    if (entCat)   doc["ent_cat"] = entCat;
    if (icon)     doc["ic"] = icon;
    if (avty2Topic) {                                                    // replaces avty_t: publishDiscovery() sees the avty array
        JsonArray avty = doc["avty"].to<JsonArray>();
        avty.add<JsonObject>()["t"] = MQTT_STATUS_TOPIC;
        JsonObject a2 = avty.add<JsonObject>();
        a2["t"] = avty2Topic;  a2["pl_avail"] = "ON";  a2["pl_not_avail"] = "OFF";   // the second value is required: with HA's
        doc["avty_mode"] = "all";                                                     // default "offline" an OFF would match neither
    }
    return publishDiscovery("binary_sensor", id, doc);
}

bool MqttManager::publishEnumSensorConfig(const char* id, const char* name, const char* stateTopic, const char* const* options,
                                          size_t nOptions, const char* attrTopic, const char* entCat, const char* icon) {
    JsonDocument doc;
    doc["name"] = name;  doc["stat_t"] = stateTopic;  doc["dev_cla"] = "enum";
    JsonArray ops = doc["ops"].to<JsonArray>();
    for (size_t i = 0; i < nOptions; ++i) ops.add(options[i]);
    doc["json_attr_t"] = attrTopic;  doc["ent_cat"] = entCat;  doc["ic"] = icon;
    return publishDiscovery("sensor", id, doc);
}
#endif

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
    return WiFi.status() == WL_CONNECTED && _client.connected();
}

// The legacy radar topics. Nothing consumes them; the frames stream replaces the LD2454 JSON.
void MqttManager::publishLegacyRadar(uint32_t nowMs) {
#ifdef BOARD_REV2
    // LD2412: 1 / 0 from presence(); the retained "None" once on entering UNKNOWN; no heartbeat while UNKNOWN.
    const Ld2412Presence p = ld2412Sensor.presence();
    const int8_t code = p == Ld2412Presence::PRESENT ? 1 : (p == Ld2412Presence::ABSENT ? 0 : 2);   // 2 = None
    const bool changed   = code != _ld2412Published;                  // -1 after every connect: re-sent at once
    const bool heartbeat = code != 2 && nowMs - _ld2412PublishedMs >= LD2412_HEARTBEAT_MS;
    if (changed || heartbeat) {
        if (_client.publish(MQTT_LD2412_TOPIC, code == 2 ? "None" : (code ? "1" : "0"), true)) {
            _ld2412Published = code;
            _ld2412PublishedMs = nowMs;
        }
    }

    // LD2454 1 Hz JSON: only while 'Legacy radar JSON' is ON and the radars are observing; targets: every
    // MQTT_RADAR_PUBLISH_INTERVAL (1 s); none: a LEGACY_HEARTBEAT_MS (30 s) heartbeat.
    const RadarStatus s = radarStatusSnapshot();
    if (s.legacy && s.observing) {
        const uint32_t every = ld2454Sensor.hasAnyTarget() ? MQTT_RADAR_PUBLISH_INTERVAL : LEGACY_HEARTBEAT_MS;
        if (nowMs - _ld2454LegacyMs >= every) {
            _ld2454LegacyMs = nowMs;
            publishLd2454LegacyJson();
        }
    }
#else
    // LD2412: the raw OUT level, retained, on change plus a heartbeat (it was sent every second).
    int8_t present = ld2412Sensor.isPresent() ? 1 : 0;
    if (present != _ld2412Published || nowMs - _ld2412PublishedMs >= LD2412_HEARTBEAT_MS) {
        if (_client.publish(MQTT_LD2412_TOPIC, present ? "1" : "0", true)) {
            _ld2412Published = present;
            _ld2412PublishedMs = nowMs;
        }
    }

    // LD2454 1 Hz JSON: off. The code path stays for P2's 'Legacy radar JSON' switch (#11).
    if (_ld2454LegacyJson && nowMs - _ld2454LegacyMs >= MQTT_RADAR_PUBLISH_INTERVAL) {
        _ld2454LegacyMs = nowMs;
        publishLd2454LegacyJson();
    }
#endif
}

void MqttManager::publishLd2454LegacyJson() {
    // LD2454: up to 3 tracked targets as JSON on its own topic.
    JsonDocument doc;
    JsonArray targets = doc["targets"].to<JsonArray>();
    for (uint8_t i = 0; i < ld2454Sensor.getTargetCount(); i++) {
        Ld2454TargetData t = ld2454Sensor.getTarget(i);
        if (!t.valid) continue;
        JsonObject to = targets.add<JsonObject>();
        to["id"] = i;
        to["x"] = t.x;
        to["y"] = t.y;
        to["speed"] = t.speed;
        to["distance"] = t.distance;
    }
    doc["count"] = targets.size();
    doc["multi_target"] = ld2454Sensor.isMultiTargetEnabled();

    char buffer[384]; // 3 targets with worst-case coordinates need ~250 bytes
    serializeJson(doc, buffer, sizeof(buffer));
    _client.publish(MQTT_LD2454_TOPIC, buffer);
}

// Frames stream (P1 spec section 1). Drains the ring every MqttTask loop, connected or not, so the
// frames never queue up: while Wi-Fi or MQTT is down their batches are dropped and the next published
// batch starts at a later seq. At most one ring's worth per call, so the timer rules always run.
void MqttManager::serviceFrameStream() {
    Ld2454Frame chunk[FRAMES_DRAIN_CHUNK];
    bool publishFailed = false;   // set by the first failed publish: the rest of this call is dropped
    size_t drained = 0;
    while (drained < LD2454_RING_FRAMES) {
        size_t got = ld2454FrameRing.pop(chunk, FRAMES_DRAIN_CHUNK);
        if (got == 0) break;
        drained += got;
        for (size_t i = 0; i < got; i++) {
            const Ld2454Batch* closed = _frameBatcher.add(chunk[i], millis());
            if (closed) sendFrameBatch(*closed, publishFailed);
        }
    }
#ifdef BOARD_REV2
    // 5 s heartbeat in every phase except OFF: 30 s (the radars are off, nothing to see)
    const uint32_t idleMs = g_radarPhase.load() == RADAR_PHASE_OFF ? LD2454_STREAM_OFF_IDLE_MS : LD2454_STREAM_IDLE_MS;
    const Ld2454Batch* closed = _frameBatcher.poll(millis(), idleMs);
#else
    const Ld2454Batch* closed = _frameBatcher.poll(millis());
#endif
    if (closed) sendFrameBatch(*closed, publishFailed);

    uint32_t nowMs = millis();
    uint32_t overflow = ld2454FrameRing.overwritten();
    uint32_t total = overflow + _framesDroppedOffline + _framesDroppedPublish;
    if (total != _framesLoggedTotal && nowMs - _framesLogMs >= FRAMES_LOG_INTERVAL_MS) {
        _framesLoggedTotal = total;
        _framesLogMs = nowMs;
        Serial.printf("[FRAMES] dropped since boot: %lu ring overflow, %lu MQTT down, %lu publish failed\n",
                      (unsigned long)overflow, (unsigned long)_framesDroppedOffline,
                      (unsigned long)_framesDroppedPublish);
    }
}

// A publish fails when the socket stays unwritable for ~10 s (a stalled link), so once one has failed
// (publishFailed) the batches after it in the same serviceFrameStream() call are dropped unsent: one
// write timeout per MqttTask loop, as for the other topics, instead of one per batch.
void MqttManager::sendFrameBatch(const Ld2454Batch& batch, bool& publishFailed) {
    static char payload[LD2454_FRAMES_JSON_MAX];   // MqttTask is the only caller
    if (!isConnected()) {
        _framesDroppedOffline += batch.n;   // dropped, not queued: the seq gap shows it downstream
        return;
    }
    if (publishFailed) {
        _framesDroppedPublish += batch.n;
        return;
    }
    uint64_t tMs = (uint64_t)(esp_timer_get_time() / 1000);
#ifdef BOARD_REV2
    const RadarStatus rs = radarStatusSnapshot();   // once per batch: sampled when the payload is built, like mt
    size_t len = formatLd2454Frames(payload, sizeof(payload), batch, tMs, _bootId,
                                    ld2454Sensor.isMultiTargetEnabled(), ld2412Sensor.presence() == Ld2412Presence::PRESENT,
                                    rs.pw(), rs.sm());
#else
    size_t len = formatLd2454Frames(payload, sizeof(payload), batch, tMs, _bootId,
                                    ld2454Sensor.isMultiTargetEnabled(), ld2412Sensor.isPresent(),
                                    FRAMES_PW_P1, FRAMES_SM_P1);
#endif
    if (len == 0) {
        _framesDroppedPublish += batch.n;
    } else if (!_client.publish(MQTT_LD2454_FRAMES_TOPIC, (const uint8_t*)payload, len, false)) {
        _framesDroppedPublish += batch.n;
        publishFailed = true;
    }
}

#ifdef BOARD_REV2
// 'Radar presence' = an LD2412 PRESENT or any LD2454 target, only while the radars observe. The driver has no
// separate 'moving' flag; the spectrum view reads hasAnyTarget() the same way. The target flag follows every frame
// (10 per second) and flickers at the edge of the field of view, so an LD2454 target holds the presence ON for
// RADAR_PRESENCE_HOLD_MS after the last one: a flicker does not become a stream of retained ON / OFF changes.
bool MqttManager::presenceNow(const RadarStatus& s) {
    if (!s.observing) { _targetHeld = false; return false; }
    const uint32_t now = millis();
    if (ld2454Sensor.hasAnyTarget()) {
        _lastTargetMs = now;
        _targetHeld = true;
    } else if (_targetHeld && (uint32_t)(now - _lastTargetMs) >= RADAR_PRESENCE_HOLD_MS) {
        _targetHeld = false;                                 // cleared here, so the hold cannot come back at the 49.7-day wrap
    }
    return ld2412Sensor.presence() == Ld2412Presence::PRESENT || _targetHeld;
}

// At every connect, before the subscriptions: delete a stray retained radar command on the broker with an empty
// retained payload (MQTT 3.1.1 3.3.1.3). Sent before the SUBSCRIBE it is not delivered back. The display topics
// are left alone (a retained display/message may be wanted). False at the first refused publish: the rest is not sent.
bool MqttManager::clearRetainedCommands() {
    static const char* const cmdTopics[] = {
        MQTT_RADAR_POWER_CMD, MQTT_RADAR_SCHEDULE_CMD, MQTT_RADAR_NIGHT_START_CMD,
        MQTT_RADAR_NIGHT_END_CMD, MQTT_RADAR_SCHED_OFFLINE_CMD, MQTT_RADAR_LEGACY_CMD
    };
    for (const char* t : cmdTopics) {
        if (!_client.publish(t, (const uint8_t*)"", 0, true)) return false;
    }
    return true;
}

// A step of the connect burst (update()) or of the HA-birth resend (serviceRadarStates()) was refused: the socket takes
// no more data (a stalled link, the send buffer is full) and every further write would block MqttTask for ~10 s. The
// socket is closed without a DISCONNECT (it would block too; the broker publishes the will 'offline'). The next update()
// sees the lost connection (HA state UNKNOWN, the frames are dropped) and, after the backoff, repeats the whole burst.
// _lastDiscoveryMs (the 'online' skip window) and _birthPending are left alone: the new connect sets both anyway.
void MqttManager::abortBurst(const char* step) {
    Serial.printf("[MQTT] %s refused, closing the connection\n", step);
    _espClient.stop();
    g_stateDirty = true;   // the states that did not go out are republished by the next connect
}

// Home Assistant's birth ("online") and will ("offline") on homeassistant/status. Runs in the MQTT callback: it only
// sets the HA state and the resend fields; the resend itself is done in serviceRadarStates().
void MqttManager::onHaStatus(bool online) {
    radarSetHaState(online ? RADAR_HA_ONLINE : RADAR_HA_OFFLINE);
    const uint32_t now = millis();
    // The retained "online" that arrives right after every subscribe is skipped: the connect path sent the discovery
    // less than MQTT_BIRTH_SKIP_MS ago. A live birth (HA reloaded its MQTT entry) triggers a resend.
    if (online && (uint32_t)(now - _lastDiscoveryMs) >= MQTT_BIRTH_SKIP_MS) {
        _birthAtMs = now;
        _birthDelayMs = esp_random() % MQTT_BIRTH_RESEND_MAX_DELAY_MS;
        _birthPending = true;
    }
}

// All retained, from one RadarStatus snapshot. False if any publish failed (the rest is skipped: one write timeout
// per loop).
bool MqttManager::publishRadarStates(const RadarStatus& s) {
    bool ok = true;
    auto pub = [&](const char* topic, const char* payload) { if (ok) ok = _client.publish(topic, payload, true); };
    char hms[9];
    pub(MQTT_RADAR_POWER_STATE,      s.en ? "1" : "0");
    pub(MQTT_RADAR_SCHEDULE_STATE,   s.sched ? "1" : "0");
    radarFormatHms(s.ns, hms);  pub(MQTT_RADAR_NIGHT_START_STATE, hms);
    radarFormatHms(s.ne, hms);  pub(MQTT_RADAR_NIGHT_END_STATE,   hms);
    pub(MQTT_RADAR_SCHED_OFFLINE_STATE, s.offl ? "1" : "0");
    pub(MQTT_RADAR_LEGACY_STATE,     s.legacy ? "1" : "0");
    pub(MQTT_RADAR_POWERED_TOPIC,    s.powered ? "ON" : "OFF");
    const bool presenceOn = presenceNow(s);                             // published BEFORE observing, so an observing ON
    _presencePublished = presenceOn ? 1 : 0;                            // never shows a stale presence
    pub(MQTT_PRESENCE_TOPIC,         presenceOn ? "ON" : "OFF");
    pub(MQTT_RADAR_OBSERVING_TOPIC,  s.observing ? "ON" : "OFF");
    pub(MQTT_RADAR_STATE_TOPIC,      radarStateName(s.state));
    char attr[RADAR_ATTR_JSON_MAX];
    char next[6] = "";
    if (s.nextMin >= 0) radarFormatHm(s.nextMin, next);
    const int n = snprintf(attr, sizeof(attr),
        "{\"sw\":%d,\"since\":%lu,\"next\":\"%s\",\"reason\":\"%s\",\"ha\":\"%s\",\"ups24\":%u,\"on24\":%u.%u,\"thr\":%d,"
        "\"fault\":\"%s\",\"b\":\"%s\",\"seq\":%lu}",
        s.powered ? 1 : 0, (unsigned long)(s.sinceMs / 1000), next, radarReasonName(s.reason), radarHaName(s.ha),
        (unsigned)s.ups24, (unsigned)(s.on24x10 / 10), (unsigned)(s.on24x10 % 10), s.thr ? 1 : 0,
        radarFaultName(s.fault), _bootId, (unsigned long)ld2454FrameRing.pushed());
    if (n < 0 || (size_t)n >= sizeof(attr)) {
        Serial.printf("[MQTT] radar attr %d B, skipped\n", n);
        ok = false;
    } else {
        pub(MQTT_RADAR_STATE_ATTR_TOPIC, attr);
    }
    return ok;
}

// Every MqttTask loop (40 ms) while connected: the HA-birth resend, the dirty-flag publish, presence on change.
void MqttManager::serviceRadarStates(uint32_t nowMs) {
    if (_birthPending && (uint32_t)(nowMs - _birthAtMs) >= _birthDelayMs) {          // HA birth: resend
        _birthPending = false;
        if (!sendDiscovery()) { abortBurst("birth discovery"); return; }             // the reconnect repeats the whole burst
        g_stateDirty = false;                                                        // the next line publishes a fresh snapshot
        if (!publishRadarStates(radarStatusSnapshot())) { abortBurst("birth states"); return; }
        _lastDiscoveryMs = millis();
        return;
    }
    const bool dirty = g_stateDirty.exchange(false);        // clear FIRST: a change during the publish sets it again
    const RadarStatus s = radarStatusSnapshot();            // a copy under the RadarPower spinlock
    if (dirty) {
        if (!publishRadarStates(s)) g_stateDirty = true;    // a failed publish is retried in the next loop
        return;
    }
    const int8_t presenceOn = presenceNow(s) ? 1 : 0;       // presence on change
    if (presenceOn != _presencePublished && _client.publish(MQTT_PRESENCE_TOPIC, presenceOn ? "ON" : "OFF", true)) {
        _presencePublished = presenceOn;
    }
}
#endif
