#include "co2_sensor.h"

Co2Sensor co2Sensor;

void Co2Sensor::begin() {
    _scd4x.begin(Wire, CO2_SENSOR_ADDRESS);
    _scd4x.stopPeriodicMeasurement();
    _scd4x.startPeriodicMeasurement();
}

void Co2Sensor::update() {
    bool isDataReady = false;
    if (_scd4x.getDataReadyStatus(isDataReady) == 0 && isDataReady) {
        uint16_t co2;
        float temp, hum;
        
        if (_scd4x.readMeasurement(co2, temp, hum) == 0) {
            _currentCo2 = co2;
            _currentTemp = temp;
            _currentHum = hum;
            Serial.printf("CO2: %u ppm, T: %.1f, H: %.1f\n", _currentCo2, _currentTemp, _currentHum);
        }
    }
}

void co2SensorTask(void *pvParameters) {
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN); 
    co2Sensor.begin();

    for (;;) {
        co2Sensor.update();
        vTaskDelay(pdMS_TO_TICKS(CO2_SENSOR_INTERVAL));
    }
}

void initCo2SensorTask() {
    xTaskCreatePinnedToCore(
        co2SensorTask,
        "CO2Task",
        SENSOR_TASK_STACK_SIZE,
        NULL,
        SENSOR_TASK_PRIORITY,
        NULL,
        0
    );
}