#ifndef LIGHT_SENSOR_H
#define LIGHT_SENSOR_H

#include <Arduino.h>
#include <BH1750.h>
#include <math.h>
#include "constants.h"
#include "display_manager.h"
#include "i2c_mutex.h"
#include "led_matrix.h"
#include "sensor_reading.h"
#include "task_registry.h"

class LightSensor {
public:
    void begin();
    void update();

    // Last good reading; only meaningful while hasFreshData().
    float getCurrentLux() const { return _lux.get(); }
    bool hasFreshData() const { return _lux.isFresh(LIGHT_DATA_STALE_MS); }
    uint8_t getMatrixBrightness() const { return _matrixBrightness; }

private:
    void tryBegin();
    void readLux();
    void updateBrightness();

    BH1750 _lightMeter;
    SensorReading<float> _lux;
    uint8_t _matrixBrightness = 32;
    bool _configured = false;
    uint32_t _lastBeginAttemptMs = 0;
    uint8_t _failedReads = 0;
};

extern LightSensor lightSensor;

void initLightSensorTask();

#endif
