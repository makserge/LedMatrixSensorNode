#include "mqtt_manager.h"

#include <esp_random.h>
#include <esp_timer.h>

MqttManager mqttManager;

// The frames' "pw" (bit0 LD2454 observing, bit1 LD2412 observing) and "sm" (code of the radar state):
// fixed in P1, which has no radar switch. P2 takes both from the RadarStatus snapshot.
static const uint8_t FRAMES_PW_P1 = 3;
static const char FRAMES_SM_P1 = 'A';
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
    // setup() has started WiFi before this task exists, so esp_random() draws on RF noise here.
    snprintf(_bootId, sizeof(_bootId), "%08lx", (unsigned long)esp_random());
    Serial.printf("[MQTT] frames boot id %s\n", _bootId);
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
            
            sendDiscovery();
            subscribeTopics();
            
            _client.publish(MQTT_STATUS_TOPIC, "online", true);
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
}

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
void MqttManager::publishConfig(const char* id, const char* name, const char* unit, const char* devClass, const char* valTpl) {
    JsonDocument doc;
    doc["name"] = name;
    doc["stat_t"] = MQTT_STATE_TOPIC;
    doc["avty_t"] = MQTT_STATUS_TOPIC;
    doc["unit_of_meas"] = unit;
    doc["dev_cla"] = devClass;
    doc["stat_cla"] = "measurement"; // HA keeps long-term statistics only for sensors with a state class
    doc["exp_aft"] = MQTT_EXPIRE_AFTER_S; // no state for this long (node hung or gone) -> unavailable
    doc["val_tpl"] = valTpl;
    publishDiscovery("sensor", id, doc);
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
    return WiFi.status() == WL_CONNECTED && _client.connected();
}

// The legacy radar topics. Nothing consumes them; the frames stream replaces the LD2454 JSON.
void MqttManager::publishLegacyRadar(uint32_t nowMs) {
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
    const Ld2454Batch* closed = _frameBatcher.poll(millis());
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
    size_t len = formatLd2454Frames(payload, sizeof(payload), batch, tMs, _bootId,
                                    ld2454Sensor.isMultiTargetEnabled(), ld2412Sensor.isPresent(),
                                    FRAMES_PW_P1, FRAMES_SM_P1);
    if (len == 0) {
        _framesDroppedPublish += batch.n;
    } else if (!_client.publish(MQTT_LD2454_FRAMES_TOPIC, (const uint8_t*)payload, len, false)) {
        _framesDroppedPublish += batch.n;
        publishFailed = true;
    }
}