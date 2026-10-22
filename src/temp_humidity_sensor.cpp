#include "temp_humidity_sensor.h"

TempHumiditySensor tempHumSensor;

void TempHumiditySensor::begin() {
    xSemaphoreTake(i2cMutex, portMAX_DELAY);
    bool ok = _htu.begin();
    xSemaphoreGive(i2cMutex);
    // No retry needed: begin() only checks for the chip and writes the power-on
    // defaults, so reads work as soon as a late or reconnected sensor answers.
    if (!ok) {
        Serial.println("[HTU21D] not found, temperature/humidity unknown until it answers");
    }
}

void TempHumiditySensor::update() {
    float temp, hum;

    xSemaphoreTake(i2cMutex, portMAX_DELAY);
    temp = _htu.readTemperature();
    hum = _htu.readHumidity();
    xSemaphoreGive(i2cMutex);

    // Each read fails on its own (NACK, short read, CRC) and then returns HTU21D_ERROR (255).
    // Humidity is clamped to 0..100 when valid, so a combined check only caught a failed
    // humidity read and let a 255 degC temperature through.
    bool tempOk = temp != HTU21D_ERROR && temp >= -40.0f && temp <= 125.0f; // sensor's operating range
    bool humOk = hum != HTU21D_ERROR && hum >= 0.0f && hum <= 100.0f;

    if (tempOk) _temp.set(temp);
    if (humOk) _hum.set(hum);

    if (tempOk && humOk) {
        if (_failing) {
            Serial.println("[HTU21D] reads OK again");
            _failing = false;
        }
        Serial.printf("T: %.1f, H: %.1f\n", temp, hum);
    } else if (!_failing) {
        // Logged once per failure streak; the values go unknown after TEMP_HUM_DATA_STALE_MS.
        Serial.printf("[HTU21D] read failed (temperature %s, humidity %s)\n",
                      tempOk ? "ok" : "error", humOk ? "ok" : "error");
        _failing = true;
    }

    _temp.expireIfStale(TEMP_HUM_DATA_STALE_MS);
    _hum.expireIfStale(TEMP_HUM_DATA_STALE_MS);
}

void tempHumiditySensorTask(void *pvParameters) {
    tempHumSensor.begin();

    for (;;) {
        waitWhileOtaPaused();
        tempHumSensor.update();
        vTaskDelay(pdMS_TO_TICKS(TEMP_HUM_SENSOR_INTERVAL));
    }
}

void initTempHumiditySensorTask() {
    xTaskCreatePinnedToCore(
        tempHumiditySensorTask, "TempHumTask", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, NULL, 0
    );
}
