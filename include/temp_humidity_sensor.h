#ifndef TEMP_HUMIDITY_SENSOR_H
#define TEMP_HUMIDITY_SENSOR_H

#include <Arduino.h>
#include <HTU21D.h> 
#include <Wire.h>
#include "constants.h"
#include "i2c_mutex.h"
#include "sensor_reading.h"
#include "task_registry.h"

class TempHumiditySensor {
public:
    void begin();
    void update();
    
    // Last good values; each only meaningful while its hasFresh*() is true.
    float getTemp() const { return _temp.get(); }
    float getHum() const { return _hum.get(); }
    bool hasFreshTemp() const { return _temp.isFresh(TEMP_HUM_DATA_STALE_MS); }
    bool hasFreshHum() const { return _hum.isFresh(TEMP_HUM_DATA_STALE_MS); }

private:
    SensorReading<float> _temp;
    SensorReading<float> _hum;
    HTU21D _htu;
    bool _failing = false;
};

extern TempHumiditySensor tempHumSensor;

void initTempHumiditySensorTask();

#endif