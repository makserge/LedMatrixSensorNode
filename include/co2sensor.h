#ifndef SENSORS_H
#define SENSORS_H

#include <Arduino.h>
#include <SensirionI2cScd4x.h>
#include <Wire.h>
#include "constants.h"

class Co2Sensor {
public:
    void begin();
    void update();

    uint16_t getCo2() const { return _currentCo2; }
    float getTemp() const { return _currentTemp; }
    float getHum() const { return _currentHum; }

private:
    uint16_t _currentCo2 = 0;
    float _currentTemp = 0.0f;
    float _currentHum = 0.0f;
    SensirionI2cScd4x _scd4x;
};

extern Co2Sensor co2Sensor;

void initCo2SensorTask();

#endif