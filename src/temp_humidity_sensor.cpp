#include "temp_humidity_sensor.h"

TempHumiditySensor tempHumSensor;

void TempHumiditySensor::begin() {
    _htu.begin();
}

void TempHumiditySensor::update() {
    float temp = _htu.readTemperature();
    float hum = _htu.readHumidity();
    
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
        tempHumiditySensorTask,
        "TempHumTask",
        SENSOR_TASK_STACK_SIZE,
        NULL,
        SENSOR_TASK_PRIORITY,
        NULL,
        0
    );
}