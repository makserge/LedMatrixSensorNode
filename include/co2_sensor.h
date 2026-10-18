#ifndef SENSORS_H
#define SENSORS_H

#include <Arduino.h>
#include <SensirionI2cScd4x.h>
#include <Wire.h>
#include "constants.h"
#include "i2c_mutex.h"
#include "sensor_reading.h"
#include "task_registry.h"

class Co2Sensor {
public:
    void begin();
    void update();

    // Last good sample; only meaningful while hasFreshData().
    uint16_t getCo2() const { return _co2.get(); }
    float getTemp() const { return _temp.get(); }
    float getHum() const { return _hum.get(); }

    // False before the first good sample, after the watchdog gave up on the sensor,
    // and once the last good sample is older than CO2_DATA_STALE_MS.
    bool hasFreshData() const { return _co2.isFresh(CO2_DATA_STALE_MS); }

private:
    void restartMeasurement();

    SensorReading<uint16_t> _co2;
    SensorReading<float> _temp;
    SensorReading<float> _hum;
    SensirionI2cScd4x _scd4x;
    uint32_t _lastDataReadyMs = 0;
    int16_t _lastError = 0;
};

extern Co2Sensor co2Sensor;

void initCo2SensorTask();

#endif