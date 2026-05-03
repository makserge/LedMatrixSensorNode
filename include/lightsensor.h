#ifndef LIGHT_SENSOR_H
#define LIGHT_SENSOR_H

#include <Arduino.h>
#include <BH1750.h>
#include <Wire.h>
#include "constants.h"

class LightSensor {
public:
    void begin();
    void update();
    float getLux() const { return _currentLux; }

private:
    float _currentLux = 0.0f;
    BH1750 _lightMeter;
};

extern LightSensor lightSensor;
void initLightSensorTask();

#endif