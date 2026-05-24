#include "light_sensor.h"

LightSensor lightSensor;

void LightSensor::begin() {
    if (_lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE)) {
        Serial.println("BH1750 initialized");
    }
}

void LightSensor::update() {
    _currentLux = _lightMeter.readLightLevel();
    Serial.printf("Lux: %.1f\n", _currentLux);
}

void lightSensorTask(void *pvParameters) {
    lightSensor.begin();
    for (;;) {
        lightSensor.update();
        vTaskDelay(pdMS_TO_TICKS(LIGHT_SENSOR_INTERVAL));
    }
}

void initLightSensorTask() {
    xTaskCreatePinnedToCore(lightSensorTask, "LightTask", SENSOR_TASK_STACK_SIZE, NULL, SENSOR_TASK_PRIORITY, NULL, 0);
}