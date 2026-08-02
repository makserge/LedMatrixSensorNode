#ifndef TEMP_HUMIDITY_SENSOR_H
#define TEMP_HUMIDITY_SENSOR_H

#include <Arduino.h>
#include <HTU21D.h> 
#include <Wire.h>
#include "constants.h"
#include "i2c_mutex.h"
#include "task_registry.h"

class TempHumiditySensor {
public:
    void begin();
    void update();
    
    float getTemp() const { return _currentTemp; }
    float getHum() const { return _currentHum; }

private:
    float _currentTemp = 0.0f;
    float _currentHum = 0.0f;
    HTU21D _htu;
};

extern TempHumiditySensor tempHumSensor;

void initTempHumiditySensorTask();

#endif