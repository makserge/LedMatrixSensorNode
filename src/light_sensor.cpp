#include "light_sensor.h"
#include "constants.h"
#include "display_manager.h"

LightSensor lightSensor;

void LightSensor::begin() {
    if (_lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE)) {
        Serial.println("BH1750 initialized");
    }
}

void LightSensor::update() {
    _currentLux = _lightMeter.readLightLevel();
    Serial.printf("Lux: %.1f\n", _currentLux);

    float lux = _currentLux;
    if (lux < LUX_MIN_THRESHOLD) lux = LUX_MIN_THRESHOLD;
    if (lux > LUX_MAX_THRESHOLD) lux = LUX_MAX_THRESHOLD;

    float factor = (lux - LUX_MIN_THRESHOLD) / (LUX_MAX_THRESHOLD - LUX_MIN_THRESHOLD);
    uint8_t targetBrightness = MATRIX_MIN_BRIGHTNESS + static_cast<uint8_t>(factor * (MATRIX_MAX_BRIGHTNESS - MATRIX_MIN_BRIGHTNESS));

    _matrixBrightness = (_matrixBrightness * 4 + targetBrightness) / 5;
    
    MatrixPanel_I2S_DMA* matrixPtr = displayManager.getMatrixPtr();
    if (matrixPtr != nullptr) {
        matrixPtr->setPanelBrightness(_matrixBrightness);
    }
}

void lightSensorTask(void *pvParameters) {
    lightSensor.begin();
    for (;;) {
        lightSensor.update();
        vTaskDelay(pdMS_TO_TICKS(LIGHT_SENSOR_INTERVAL));
    }
}

void initLightSensorTask() {
    xTaskCreatePinnedToCore(
        lightSensorTask, 
        "LightTask", 
        SENSOR_TASK_STACK_SIZE, 
        NULL, 
        SENSOR_TASK_PRIORITY, 
        NULL, 
        0
    );
}
