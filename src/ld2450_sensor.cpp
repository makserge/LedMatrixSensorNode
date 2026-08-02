#include "ld2450_sensor.h"

Ld2450Sensor ld2450Sensor;

static int16_t fixLd2450Sign(int16_t buggyValue) {
    uint16_t raw = (uint16_t)buggyValue;
    if (raw & 0x8000) {
        return (int16_t)(raw & 0x7FFF); // positive
    } else {
        return (int16_t)(-(int32_t)raw); // negative
    }
}

static void beginRadarLink() {
    Serial1.setRxBufferSize(1024);
    Serial1.begin(RADAR_BAUD, SERIAL_8N1, RADAR_RX_PIN, RADAR_TX_PIN);
}

void Ld2450Sensor::begin() {
    beginRadarLink();
    _radar.begin(Serial1, false);
    _radar.setNumberOfTargets(LD2450_MAX_TARGETS);
    _lastDataMs = millis();
}

void Ld2450Sensor::update() {
    int found = _radar.read();
    uint32_t nowMs = millis();

    if (found > 0) {
        _lastDataMs = nowMs;

        uint16_t supported = _radar.getSensorSupportedTargetCount();
        for (uint8_t i = 0; i < LD2450_MAX_TARGETS && i < supported; i++) {
            LD2450::RadarTarget t = _radar.getTarget(i);
            int16_t fixedX = fixLd2450Sign(t.x);
            int16_t fixedY = fixLd2450Sign(t.y);

            _targets[i].valid = (t.resolution != 0);
            _targets[i].x = fixedX;
            _targets[i].y = fixedY;
            _targets[i].speed = fixLd2450Sign(t.speed);
            _targets[i].distance = (uint16_t)lroundf(sqrtf((float)fixedX * fixedX + (float)fixedY * fixedY));
        }
    }

    if (nowMs - _lastDataMs >= LD2450_DATA_TIMEOUT_MS) {
        Serial.println("[LD2450] watchdog: no data for too long, re-initializing UART link");
        Serial1.end();
        delay(10);
        beginRadarLink();
        _radar.begin(Serial1, false);
        _radar.setNumberOfTargets(LD2450_MAX_TARGETS);
        _lastDataMs = nowMs;
    }
}

Ld2450TargetData Ld2450Sensor::getTarget(uint8_t index) const {
    if (index >= LD2450_MAX_TARGETS) {
        Ld2450TargetData empty = {false, 0, 0, 0, 0};
        return empty;
    }
    return _targets[index];
}

bool Ld2450Sensor::hasAnyTarget() const {
    for (uint8_t i = 0; i < LD2450_MAX_TARGETS; i++) {
        if (_targets[i].valid) return true;
    }
    return false;
}

void ld2450SensorTask(void *pvParameters) {
    vTaskDelay(pdMS_TO_TICKS(LD2450_STARTUP_DELAY_MS));
    ld2450Sensor.begin();

    for (;;) {
        ld2450Sensor.update();
        vTaskDelay(pdMS_TO_TICKS(LD2450_SENSOR_INTERVAL));
    }
}

void initLd2450SensorTask() {
    xTaskCreatePinnedToCore(
        ld2450SensorTask, "LD2450Task", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, &ld2450TaskHandle, 0   // was NULL
    );
}
