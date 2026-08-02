#include "temp_humidity_sensor.h"

TempHumiditySensor tempHumSensor;

void TempHumiditySensor::begin() {
    xSemaphoreTake(i2cMutex, portMAX_DELAY);
    _htu.begin();
    xSemaphoreGive(i2cMutex);
}

void TempHumiditySensor::update() {
    float temp, hum;

    xSemaphoreTake(i2cMutex, portMAX_DELAY);
    temp = _htu.readTemperature();
    hum = _htu.readHumidity();
    xSemaphoreGive(i2cMutex);

    if (hum < 101.0f && temp > -40.0f) {
        _currentTemp = temp;
        _currentHum = hum;
        Serial.printf("T: %.1f, H: %.1f\n", _currentTemp, _currentHum);
    }
}

void tempHumiditySensorTask(void *pvParameters) {
    tempHumSensor.begin();

    for (;;) {
        tempHumSensor.update();
        vTaskDelay(pdMS_TO_TICKS(TEMP_HUM_SENSOR_INTERVAL));
    }
}

void initTempHumiditySensorTask() {
    xTaskCreatePinnedToCore(
        tempHumiditySensorTask, "TempHumTask", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, &tempHumTaskHandle, 0   // was NULL
    );
}
