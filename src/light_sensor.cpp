#include "light_sensor.h"

LightSensor lightSensor;

void LightSensor::begin() {
    tryBegin();
}

void LightSensor::tryBegin() {
    _lastBeginAttemptMs = millis();
    xSemaphoreTake(i2cMutex, portMAX_DELAY);
    _configured = _lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE);
    xSemaphoreGive(i2cMutex);
    _failedReads = 0;
    if (_configured) {
        Serial.println("BH1750 initialized");
    } else {
        Serial.printf("[BH1750] not responding, retrying every %lu s\n", LIGHT_SENSOR_RETRY_MS / 1000);
    }
}

void LightSensor::readLux() {
    // Right after begin() the first conversion (up to 180 ms) isn't done yet.
    if (!_lightMeter.measurementReady(true)) return;

    xSemaphoreTake(i2cMutex, portMAX_DELAY);
    float lux = _lightMeter.readLightLevel(); // -1: read failed, -2: never configured
    xSemaphoreGive(i2cMutex);

    if (lux >= 0.0f) {
        _failedReads = 0;
        _lux.set(lux);
        Serial.printf("Lux: %.1f\n", lux);
        return;
    }

    _failedReads++;
    if (_failedReads == 1) {
        Serial.println("[BH1750] read failed");
    }
    // Several in a row: the sensor is gone or lost power (it then wakes up powered
    // down and unconfigured), so set it up again.
    if (_failedReads >= LIGHT_SENSOR_MAX_FAILED_READS) {
        Serial.printf("[BH1750] %u reads failed, re-initializing\n", _failedReads);
        _configured = false;
    }
}

void LightSensor::updateBrightness() {
    // An error code (-1/-2 lx) used to be clamped up to LUX_MIN_THRESHOLD and dimmed the
    // panel to its minimum; without a usable reading, aim for a fixed middle level.
    uint8_t targetBrightness = MATRIX_FALLBACK_BRIGHTNESS;
    if (hasFreshData()) {
        float lux = _lux.get();
        if (lux < LUX_MIN_THRESHOLD) lux = LUX_MIN_THRESHOLD;
        if (lux > LUX_MAX_THRESHOLD) lux = LUX_MAX_THRESHOLD;

        float normalized = (lux - LUX_MIN_THRESHOLD) / (LUX_MAX_THRESHOLD - LUX_MIN_THRESHOLD);
        float factor = powf(normalized, 0.6f);
        targetBrightness = MATRIX_MIN_BRIGHTNESS + static_cast<uint8_t>(factor * (MATRIX_MAX_BRIGHTNESS - MATRIX_MIN_BRIGHTNESS));
    }

    _matrixBrightness = (_matrixBrightness * 4 + targetBrightness) / 5;
    
    MatrixPanel_I2S_DMA* matrixPtr = displayManager.getMatrixPtr();
    if (matrixPtr != nullptr) {
        matrixPtr->setBrightness(_matrixBrightness);
    }
}

void LightSensor::update() {
    // A failed begin() leaves the BH1750 unconfigured: every read would return -2.
    if (!_configured && millis() - _lastBeginAttemptMs >= LIGHT_SENSOR_RETRY_MS) {
        tryBegin();
    }
    if (_configured) {
        readLux();
    }
    _lux.expireIfStale(LIGHT_DATA_STALE_MS);
    updateBrightness();
}

void lightSensorTask(void *pvParameters) {
    lightSensor.begin();
    for (;;) {
        waitWhileOtaPaused();
        lightSensor.update();
        vTaskDelay(pdMS_TO_TICKS(LIGHT_SENSOR_INTERVAL));
    }
}

void initLightSensorTask() {
    xTaskCreatePinnedToCore(
        lightSensorTask, "LightTask", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, NULL, 0
    );
}
