#ifndef LIGHT_SENSOR_H
#define LIGHT_SENSOR_H

#include <Arduino.h>
#include <BH1750.h>
#include "constants.h"
#include "led_matrix.h"

class LightSensor {
public:
    void begin();
    void update();
    float getCurrentLux() const { return _currentLux; }
    uint8_t getMatrixBrightness() const { return _matrixBrightness; }

private:
    BH1750 _lightMeter;
    float _currentLux = 0.0f;
    uint8_t _matrixBrightness = 32;
};

extern LightSensor lightSensor;

void initLightSensorTask();

#endif
