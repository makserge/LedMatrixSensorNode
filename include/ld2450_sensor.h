#ifndef LD2450_SENSOR_H
#define LD2450_SENSOR_H

#include <Arduino.h>
#include <LD2450.h>
#include "constants.h"
#include "pins.h"
#include "task_registry.h"

#define LD2450_MAX_TARGETS 3

struct Ld2450TargetData {
    bool valid;
    int16_t x;
    int16_t y;
    int16_t speed;
    uint16_t distance;
};

class Ld2450Sensor {
public:
    void begin();
    void update();

    Ld2450TargetData getTarget(uint8_t index) const;
    uint8_t getTargetCount() const { return LD2450_MAX_TARGETS; }
    bool hasAnyTarget() const;

private:
    LD2450 _radar;
    Ld2450TargetData _targets[LD2450_MAX_TARGETS] = {};

    uint32_t _lastDataMs = 0;
};

extern Ld2450Sensor ld2450Sensor;

void initLd2450SensorTask();

#endif
