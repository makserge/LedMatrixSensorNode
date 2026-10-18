#include "co2_sensor.h"

Co2Sensor co2Sensor;

static void logScd4xError(const char* what, int16_t error) {
    char message[64];
    errorToString(error, message, sizeof(message));
    Serial.printf("[CO2] %s failed: %s\n", what, message);
}

void Co2Sensor::begin() {
    _scd4x.begin(Wire, CO2_SENSOR_ADDRESS); // only stores the bus and address
    restartMeasurement();
    _lastDataReadyMs = millis();
}

// Stop first: a sensor still measuring (soft reboot, stall) ignores most commands.
// The start is retried because an idle sensor just answers "not ready" forever,
// which only the watchdog would notice, CO2_DATA_TIMEOUT_MS later.
void Co2Sensor::restartMeasurement() {
    xSemaphoreTake(i2cMutex, portMAX_DELAY);
    int16_t error = _scd4x.stopPeriodicMeasurement(); // includes the 500 ms the sensor needs to stop
    xSemaphoreGive(i2cMutex);
    if (error != 0) {
        logScd4xError("stopPeriodicMeasurement", error);
    }

    for (uint8_t attempt = 1; attempt <= CO2_START_ATTEMPTS; attempt++) {
        xSemaphoreTake(i2cMutex, portMAX_DELAY);
        error = _scd4x.startPeriodicMeasurement();
        xSemaphoreGive(i2cMutex);
        if (error == 0) return;

        logScd4xError("startPeriodicMeasurement", error);
        if (attempt < CO2_START_ATTEMPTS) {
            vTaskDelay(pdMS_TO_TICKS(CO2_START_RETRY_MS));
        }
    }
    Serial.println("[CO2] sensor not measuring, the watchdog retries later");
}

void Co2Sensor::update() {
    bool isDataReady = false;
    uint32_t nowMs = millis();

    xSemaphoreTake(i2cMutex, portMAX_DELAY);
    int16_t status = _scd4x.getDataReadyStatus(isDataReady);
    xSemaphoreGive(i2cMutex);
    if (status != 0) _lastError = status;

    if (status == 0 && isDataReady) {
        uint16_t co2;
        float temp, hum;

        xSemaphoreTake(i2cMutex, portMAX_DELAY);
        int16_t readStatus = _scd4x.readMeasurement(co2, temp, hum);
        xSemaphoreGive(i2cMutex);

        if (readStatus == 0) {
            if (co2 > 0) {
                _co2.set(co2);
                _temp.set(temp);
                _hum.set(hum);
                Serial.printf("CO2: %u ppm, T: %.1f, H: %.1f\n", co2, temp, hum);
            }
            _lastDataReadyMs = nowMs; // any successful read (even a rejected 0) proves the cycle is alive
            _lastError = 0;
        } else {
            _lastError = readStatus;
        }
    }

    if (nowMs - _lastDataReadyMs >= CO2_DATA_TIMEOUT_MS) {
        Serial.println("[CO2] watchdog: no data for too long, value now unknown, re-initializing sensor");
        if (_lastError != 0) {
            logScd4xError("last read", _lastError);
        }
        // The last value is at least CO2_DATA_TIMEOUT_MS old: it must not keep the alarm
        // going or be published as current.
        _co2.invalidate();
        _temp.invalidate();
        _hum.invalidate();
        _lastError = 0;
        restartMeasurement();
        _lastDataReadyMs = millis(); // the restart itself takes 0.5-2.5 s
    }

    // Needed even with the watchdog: a sensor that keeps answering with rejected
    // 0-ppm samples never trips it.
    _co2.expireIfStale(CO2_DATA_STALE_MS);
    _temp.expireIfStale(CO2_DATA_STALE_MS);
    _hum.expireIfStale(CO2_DATA_STALE_MS);
}

void co2SensorTask(void *pvParameters) {
    co2Sensor.begin();

    for (;;) {
        waitWhileOtaPaused();
        co2Sensor.update();
        // Without a fresh value (boot, after a watchdog restart) poll at the sensor's
        // own pace, so the first value arrives ~5 s after the start, not 30 s.
        vTaskDelay(pdMS_TO_TICKS(co2Sensor.hasFreshData() ? CO2_SENSOR_INTERVAL : CO2_FAST_POLL_MS));
    }
}

void initCo2SensorTask() {
    xTaskCreatePinnedToCore(
        co2SensorTask, "CO2Task", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, NULL, 0
    );
}