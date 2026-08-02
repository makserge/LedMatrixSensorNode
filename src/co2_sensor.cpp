#include "co2_sensor.h"

Co2Sensor co2Sensor;

void Co2Sensor::begin() {
    xSemaphoreTake(i2cMutex, portMAX_DELAY);
    _scd4x.begin(Wire, CO2_SENSOR_ADDRESS);
    _scd4x.stopPeriodicMeasurement();
    _scd4x.startPeriodicMeasurement();
    xSemaphoreGive(i2cMutex);
    _lastDataReadyMs = millis();
}

void Co2Sensor::update() {
    bool isDataReady = false;
    uint16_t status;
    uint32_t nowMs = millis();

    xSemaphoreTake(i2cMutex, portMAX_DELAY);
    status = _scd4x.getDataReadyStatus(isDataReady);
    xSemaphoreGive(i2cMutex);

    if (status == 0 && isDataReady) {
        uint16_t co2;
        float temp, hum;
        uint16_t readStatus;

        xSemaphoreTake(i2cMutex, portMAX_DELAY);
        readStatus = _scd4x.readMeasurement(co2, temp, hum);
        xSemaphoreGive(i2cMutex);

        if (readStatus == 0) {
            if (co2 > 0) {
                _currentCo2 = co2;
                _currentTemp = temp;
                _currentHum = hum;
                Serial.printf("CO2: %u ppm, T: %.1f, H: %.1f\n", _currentCo2, _currentTemp, _currentHum);
            }
            _lastDataReadyMs = nowMs; // any successful read (even a rejected 0) proves the cycle is alive
        }
    }

    if (nowMs - _lastDataReadyMs >= CO2_DATA_TIMEOUT_MS) {
        Serial.println("[CO2] watchdog: no data for too long, re-initializing sensor");
        xSemaphoreTake(i2cMutex, portMAX_DELAY);
        _scd4x.stopPeriodicMeasurement();
        _scd4x.startPeriodicMeasurement();
        xSemaphoreGive(i2cMutex);
        _lastDataReadyMs = nowMs;
    }
}

void co2SensorTask(void *pvParameters) {
    co2Sensor.begin();

    for (;;) {
        co2Sensor.update();
        vTaskDelay(pdMS_TO_TICKS(CO2_SENSOR_INTERVAL));
    }
}

void initCo2SensorTask() {
    xTaskCreatePinnedToCore(
        co2SensorTask, "CO2Task", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, &co2TaskHandle, 0   // was NULL
    );
}