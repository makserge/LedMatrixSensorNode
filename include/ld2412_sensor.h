#ifndef LD2412_SENSOR_H
#define LD2412_SENSOR_H

#include <Arduino.h>
#include "constants.h"
#include "pins.h"
#include "task_registry.h"

class Ld2412Sensor {
public:
    void begin();
    void update();

    bool isPresent() const { return _presence; }

private:
    bool _presence = false;
};

extern Ld2412Sensor ld2412Sensor;

void initLd2412SensorTask();

#endif
