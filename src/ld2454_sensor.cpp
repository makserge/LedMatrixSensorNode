#include "ld2454_sensor.h"

Ld2454Sensor ld2454Sensor;

// Hi-Link "HLK-LD2454 serial communication protocol" V1.00 (2026-05-13), same framing as the LD2450:
//   tracking frame: AA FF 03 00 | 3 x 8-byte target | 55 CC   (10 frames/s)
//   command ACK:    FD FC FB FA | length (LE) | <cmd> 01 | status (LE) | data | 04 03 02 01
// The factory default is single-target tracking and the protocol has no command to read
// the mode back, so multi-target mode is (re)applied whenever the radar may have rebooted.
static const uint8_t TRACKING_HEADER[4] = {0xAA, 0xFF, 0x03, 0x00};
static const uint8_t TRACKING_FOOTER[2] = {0x55, 0xCC};
static const size_t TRACKING_FRAME_LEN = 30;
static const size_t TARGET_RECORD_LEN = 8;

static const uint8_t COMMAND_HEADER[4] = {0xFD, 0xFC, 0xFB, 0xFA};
static const uint8_t COMMAND_FOOTER[4] = {0x04, 0x03, 0x02, 0x01};
static const size_t COMMAND_PREFIX_LEN = 6; // header + 2-byte length
static const size_t COMMAND_MAX_VALUE_LEN = 4;

static const uint8_t CMD_ENABLE_CONFIG = 0xFF;
static const uint8_t CMD_END_CONFIG    = 0xFE;
static const uint8_t CMD_MULTI_TARGET  = 0x90;

// The 4 header bytes in arrival order, as seen in _headerWindow.
static const uint32_t TRACKING_HEADER_WORD = 0xAAFF0300;
static const uint32_t COMMAND_HEADER_WORD  = 0xFDFCFBFA;

static int16_t decodeSignMagnitude(uint8_t low, uint8_t high) {
    uint16_t raw = (uint16_t)low | ((uint16_t)high << 8);
    int16_t magnitude = (int16_t)(raw & 0x7FFF);
    return (raw & 0x8000) ? magnitude : (int16_t)-magnitude; // bit 15 set = positive
}

void Ld2454Sensor::beginRadarLink() {
    Serial1.setRxBufferSize(1024);
    Serial1.begin(RADAR_BAUD, SERIAL_8N1, RADAR_RX_PIN, RADAR_TX_PIN);
    _rxLen = 0;
    _headerWindow = 0;
}

void Ld2454Sensor::begin() {
    beginRadarLink();
    enableMultiTargetTracking();
}

void Ld2454Sensor::update() {
    while (Serial1.available() > 0) {
        processByte((uint8_t)Serial1.read());
    }

    uint32_t nowMs = millis();

    if (nowMs - _lastDataMs >= LD2454_DATA_TIMEOUT_MS) {
        Serial.println("[LD2454] watchdog: no data for too long, re-initializing UART link");
        clearTargets(); // stale targets would otherwise hold presence on indefinitely
        Serial1.end();
        delay(10);
        beginRadarLink();
        enableMultiTargetTracking(); // a power-cycled radar comes back in single-target mode
    } else if (_modeReapplyPending) {
        Serial.println("[LD2454] radar (re)started - re-applying multi-target mode");
        enableMultiTargetTracking();
    } else if (!_multiTargetEnabled && nowMs - _lastConfigAttemptMs >= _retryDelayMs) {
        enableMultiTargetTracking();
    }
}

void Ld2454Sensor::enableMultiTargetTracking() {
    static const uint8_t enableConfigValue[2] = {0x01, 0x00};

    _modeReapplyPending = false;
    _configuring = true;
    bool immediateAttempt = _immediateAttempt;
    _immediateAttempt = false;
    uint32_t framesBefore = _frameCount;

    bool radarAnswered = sendCommand(CMD_ENABLE_CONFIG, enableConfigValue, sizeof(enableConfigValue));
    bool configured = radarAnswered;
    if (configured) {
        vTaskDelay(pdMS_TO_TICKS(LD2454_COMMAND_GAP_MS));
        configured = sendCommand(CMD_MULTI_TARGET);
    }

    // Always leave configuration mode - the radar stops reporting targets while in it.
    vTaskDelay(pdMS_TO_TICKS(LD2454_COMMAND_GAP_MS));
    bool closed = sendCommand(CMD_END_CONFIG);

    _multiTargetEnabled = configured && closed;
    if (_multiTargetEnabled) {
        _retryDelayMs = 0;
        Serial.println("[LD2454] multi-target tracking enabled");
    } else {
        // Back off 2 s, 4 s, 8 s ... up to LD2454_CONFIG_RETRY_MS: quick recovery after a radar
        // reboot, no hammering when the radar never answers (e.g. TX not wired).
        _retryDelayMs = _retryDelayMs == 0 ? LD2454_CONFIG_RETRY_MIN_MS
                                           : min(_retryDelayMs * 2, (uint32_t)LD2454_CONFIG_RETRY_MS);
        Serial.printf("[LD2454] multi-target tracking not confirmed, retrying in %lu s\n",
                      (unsigned long)(_retryDelayMs / 1000));
    }

    // If this attempt failed because the radar was silent (still booting), retry once on its next
    // frame. A radar that answered ENABLE_CONFIG (it stops reporting in configuration mode), keeps
    // streaming without ACKs (e.g. TX not wired) or already failed an immediate retry waits for
    // the backoff instead.
    bool radarWasSilent = !radarAnswered && _frameCount == framesBefore;
    _retryOnNextFrame = !_multiTargetEnabled && radarWasSilent && !immediateAttempt;
    _configuring = false;

    // Reporting pauses in configuration mode and the protocol doesn't say how soon it resumes,
    // so the wait for the next frame is not measured as a gap between frames.
    _lastFrameMs = 0;
    _lastDataMs = millis();
    _lastConfigAttemptMs = _lastDataMs;
    _configEndMs = _lastDataMs;
}

bool Ld2454Sensor::sendCommand(uint8_t command, const uint8_t* value, size_t valueLen) {
    if (valueLen > COMMAND_MAX_VALUE_LEN) return false;

    uint8_t frame[COMMAND_PREFIX_LEN + 2 + COMMAND_MAX_VALUE_LEN + sizeof(COMMAND_FOOTER)];
    uint16_t payloadLen = (uint16_t)(2 + valueLen);
    size_t len = 0;

    memcpy(&frame[len], COMMAND_HEADER, sizeof(COMMAND_HEADER));
    len += sizeof(COMMAND_HEADER);
    frame[len++] = (uint8_t)(payloadLen & 0xFF);
    frame[len++] = (uint8_t)(payloadLen >> 8);
    frame[len++] = command;
    frame[len++] = 0x00;
    if (valueLen > 0) {
        memcpy(&frame[len], value, valueLen);
        len += valueLen;
    }
    memcpy(&frame[len], COMMAND_FOOTER, sizeof(COMMAND_FOOTER));
    len += sizeof(COMMAND_FOOTER);

    // Consume what is already buffered so a stale ACK can't be taken for this command's.
    while (Serial1.available() > 0) {
        processByte((uint8_t)Serial1.read());
    }
    _ackReceived = false;
    Serial1.write(frame, len);

    uint32_t startMs = millis();
    while (millis() - startMs < LD2454_ACK_TIMEOUT_MS) {
        while (Serial1.available() > 0) {
            processByte((uint8_t)Serial1.read());
            if (_ackReceived && _ackCommand == command) {
                return _ackStatus == 0;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    return false;
}

void Ld2454Sensor::processByte(uint8_t byte) {
    // Neither header can occur inside a valid frame body: values are sign-magnitude (bit 15 =
    // positive), so within the 8 m range every high byte is 0x00-0x1F or 0x80-0x9F, and an
    // 0xAA or 0xFD high byte would need a value of 10752 or more. A header therefore always
    // starts a new frame, which also resyncs right after a truncated frame instead of
    // swallowing the next frame or ACK.
    _headerWindow = (_headerWindow << 8) | byte;
    if (_headerWindow == TRACKING_HEADER_WORD || _headerWindow == COMMAND_HEADER_WORD) {
        memcpy(_rxFrame, _headerWindow == TRACKING_HEADER_WORD ? TRACKING_HEADER : COMMAND_HEADER,
               sizeof(TRACKING_HEADER));
        _rxLen = sizeof(TRACKING_HEADER);
        return;
    }
    if (_rxLen == 0) return; // hunting for a header

    _rxFrame[_rxLen++] = byte;

    if (_rxFrame[0] == TRACKING_HEADER[0]) {
        if (_rxLen == TRACKING_FRAME_LEN) {
            if (_rxFrame[TRACKING_FRAME_LEN - 2] == TRACKING_FOOTER[0] &&
                _rxFrame[TRACKING_FRAME_LEN - 1] == TRACKING_FOOTER[1]) {
                processTrackingFrame();
            }
            _rxLen = 0;
        }
        return;
    }

    if (_rxLen < COMMAND_PREFIX_LEN) return;

    size_t payloadLen = (size_t)_rxFrame[4] | ((size_t)_rxFrame[5] << 8);
    size_t frameLen = COMMAND_PREFIX_LEN + payloadLen + sizeof(COMMAND_FOOTER);
    if (frameLen > sizeof(_rxFrame)) {
        _rxLen = 0;
        return;
    }
    if (_rxLen == frameLen) {
        if (memcmp(&_rxFrame[frameLen - sizeof(COMMAND_FOOTER)], COMMAND_FOOTER, sizeof(COMMAND_FOOTER)) == 0) {
            processAckFrame();
        }
        _rxLen = 0;
    }
}

void Ld2454Sensor::processTrackingFrame() {
    Ld2454TargetData parsed[LD2454_MAX_TARGETS] = {};

    for (uint8_t i = 0; i < LD2454_MAX_TARGETS; i++) {
        const uint8_t* t = &_rxFrame[sizeof(TRACKING_HEADER) + i * TARGET_RECORD_LEN];

        bool empty = true;
        for (size_t b = 0; b < TARGET_RECORD_LEN; b++) {
            if (t[b] != 0) {
                empty = false;
                break;
            }
        }
        if (empty) continue; // unused target slots are all zeros

        int16_t x = decodeSignMagnitude(t[0], t[1]);
        int16_t y = decodeSignMagnitude(t[2], t[3]);

        parsed[i].valid = true;
        parsed[i].x = x;
        parsed[i].y = y;
        parsed[i].speed = decodeSignMagnitude(t[4], t[5]);
        parsed[i].distance = (uint16_t)lroundf(sqrtf((float)x * x + (float)y * y));
    }

    // Radar task only: pick up a calibration the MQTT task has handed over.
    applyPendingCalibration_();

    // Radar frame (mm) -> room frame (cm), boundary test, and the inputs for presence and areas.
    const uint32_t frameMs = millis();
    mmwave_room::Sample samples[LD2454_MAX_TARGETS]{};
    bool framePresent = false;
    for (uint8_t i = 0; i < LD2454_MAX_TARGETS; i++) {
        if (!parsed[i].valid) continue;
        const float lx = parsed[i].x / 10.0f;
        const float ly = parsed[i].y / 10.0f;
        const mmwave_room::RoomPoint p = mmwave_room::toRoom(lx, ly, _rotation, _cal.radarX, _cal.radarY,
                                                             _cal.room.v, _cal.room.n);
        parsed[i].roomX = p.x;
        parsed[i].roomY = p.y;
        parsed[i].inBoundary = p.inBoundary;
        parsed[i].angleDeg = atan2f(lx, ly) * 180.0f / mmwave_room::kPi;
        samples[i] = {true, p.inBoundary, p.x, p.y, fabsf((float)parsed[i].speed)};
        if (p.inBoundary || !_cal.gatesPresence) framePresent = true;
    }

    _areas.update(samples, LD2454_MAX_TARGETS, frameMs);
    uint8_t areaBits = 0;
    for (uint8_t a = 0; a < mmwave_room::Occupancy::kAreas; a++) {
        if (_areas.occupied(a)) areaBits |= (uint8_t)(1u << a);
    }

    bool presenceNow;
    if (framePresent) {
        _lastPresenceMs = frameMs;
        presenceNow = true;
    } else {
        presenceNow = _presenceOn && (frameMs - _lastPresenceMs) <= _presenceTimeoutMs;
    }

    portENTER_CRITICAL(&_targetsMux);
    memcpy(_targets, parsed, sizeof(_targets));
    _presenceOn = presenceNow;
    _areaBits = areaBits;
    portEXIT_CRITICAL(&_targetsMux);

    uint32_t nowMs = millis();
    if (_configuring) {
        // The radar pauses reporting during our own configuration attempt; no restart heuristics.
    } else if (_retryOnNextFrame) {
        // The radar was silent during the last failed attempt and is reporting again.
        _retryOnNextFrame = false;
        _immediateAttempt = true;
        _modeReapplyPending = true;
    } else if (_lastFrameMs == 0 && _multiTargetEnabled) {
        // First frame after a successful attempt. A late one may hide a reboot in between, but
        // the protocol doesn't say how soon reporting resumes after END_CONFIG: re-apply once,
        // then accept late resumes until a prompt one shows the radar's normal timing again.
        if (nowMs - _configEndMs >= LD2454_RESTART_GAP_MS) {
            if (!_lateResumeSeen) {
                _lateResumeSeen = true;
                _multiTargetEnabled = false;
                _modeReapplyPending = true;
            }
        } else {
            _lateResumeSeen = false;
        }
    } else if (_lastFrameMs != 0 && nowMs - _lastFrameMs >= LD2454_RESTART_GAP_MS) {
        // At 10 frames/s a pause this long between frames means the radar stopped reporting -
        // most likely a reboot, which silently drops it back to single-target mode.
        _multiTargetEnabled = false;
        _modeReapplyPending = true;
    }
    _lastFrameMs = nowMs;
    _lastDataMs = nowMs;
    _frameCount++;
}

void Ld2454Sensor::processAckFrame() {
    size_t payloadLen = (size_t)_rxFrame[4] | ((size_t)_rxFrame[5] << 8);
    if (payloadLen < 4 || _rxFrame[7] != 0x01) return; // ACK command word is <cmd> 01

    _ackCommand = _rxFrame[6];
    _ackStatus = (uint16_t)_rxFrame[8] | ((uint16_t)_rxFrame[9] << 8);
    _ackReceived = true;
}

void Ld2454Sensor::clearTargets() {
    portENTER_CRITICAL(&_targetsMux);
    memset(_targets, 0, sizeof(_targets));
    _presenceOn = false;
    _areaBits = 0;
    portEXIT_CRITICAL(&_targetsMux);
}

Ld2454TargetData Ld2454Sensor::getTarget(uint8_t index) const {
    if (index >= LD2454_MAX_TARGETS) {
        Ld2454TargetData empty = {false, 0, 0, 0, 0};
        return empty;
    }
    portENTER_CRITICAL(&_targetsMux);
    Ld2454TargetData target = _targets[index];
    portEXIT_CRITICAL(&_targetsMux);
    return target;
}

bool Ld2454Sensor::hasAnyTarget() const {
    bool any = false;
    portENTER_CRITICAL(&_targetsMux);
    for (uint8_t i = 0; i < LD2454_MAX_TARGETS; i++) {
        if (_targets[i].valid) {
            any = true;
            break;
        }
    }
    portEXIT_CRITICAL(&_targetsMux);
    return any;
}

// ═══════════════════════════════════════════════════════════════════════════
// Room mapping: calibration hand-over and queries (ported from mmwave-component)
// ═══════════════════════════════════════════════════════════════════════════

void Ld2454Sensor::setCalibration(const RadarCalibration &cal) {
    portENTER_CRITICAL(&_targetsMux);
    _pendingCal = cal;
    _calPending = true;
    portEXIT_CRITICAL(&_targetsMux);
}

void Ld2454Sensor::applyPendingCalibration_() {
    if (!_calPending) return;

    RadarCalibration next;
    portENTER_CRITICAL(&_targetsMux);
    next = _pendingCal;
    _calPending = false;
    portEXIT_CRITICAL(&_targetsMux);

    _cal = next;
    _rotation = mmwave_room::buildRotation(_cal.yaw, _cal.pitch, _cal.roll);
    _presenceTimeoutMs = (uint32_t)(_cal.presenceTimeoutS * 1000.0f + 0.5f);

    _areas.setHysteresis(_cal.areaHysteresisCm);
    _areas.setStillSpeed(_cal.areaStillSpeed);
    _areas.setConfirmMs((uint32_t)(_cal.areaConfirmS * 1000.0f + 0.5f));
    _areas.setClearMs((uint32_t)(_cal.areaClearS * 1000.0f + 0.5f));
    _areas.setPassSpeed(_cal.areaPassSpeed);
    for (uint8_t a = 0; a < AREA_COUNT; a++) {
        _areas.clearArea(a);
        for (uint8_t p = 0; p < _cal.area[a].n; p++) {
            _areas.addPoint(a, _cal.area[a].v[p].x, _cal.area[a].v[p].y);
        }
    }
    Serial.printf("[LD2454] calibration applied: radar (%.0f,%.0f) yaw %.1f, room %u pts\n",
                  _cal.radarX, _cal.radarY, _cal.yaw, (unsigned)_cal.room.n);
}

bool Ld2454Sensor::isPresent() const {
    portENTER_CRITICAL(&_targetsMux);
    const bool present = _presenceOn;
    portEXIT_CRITICAL(&_targetsMux);
    return present;
}

bool Ld2454Sensor::isAreaOccupied(uint8_t index) const {
    if (index >= mmwave_room::Occupancy::kAreas) return false;
    portENTER_CRITICAL(&_targetsMux);
    const bool occupied = (_areaBits >> index) & 1u;
    portEXIT_CRITICAL(&_targetsMux);
    return occupied;
}

void ld2454SensorTask(void *pvParameters) {
    vTaskDelay(pdMS_TO_TICKS(LD2454_STARTUP_DELAY_MS));
    ld2454Sensor.begin();

    for (;;) {
        waitWhileOtaPaused();
        ld2454Sensor.update();
        vTaskDelay(pdMS_TO_TICKS(LD2454_SENSOR_INTERVAL));
    }
}

void initLd2454SensorTask() {
    // Load the saved room calibration before the radar task can see its first frame.
    roomCalibration.begin();
    ld2454Sensor.setCalibration(roomCalibration.get());
    xTaskCreatePinnedToCore(
        ld2454SensorTask, "LD2454Task", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, NULL, 0
    );
}
