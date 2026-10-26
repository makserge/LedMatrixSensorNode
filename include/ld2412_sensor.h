#ifndef LD2412_SENSOR_H
#define LD2412_SENSOR_H

#include <Arduino.h>
#include "constants.h"
#include "pins.h"
#include "task_registry.h"

#ifdef BOARD_REV2
#include <atomic>

// The tri-state of the radar presence (P2 spec section 6): UNKNOWN while the radars are off, parked or warming up.
enum class Ld2412Presence : uint8_t { UNKNOWN = 0, ABSENT = 1, PRESENT = 2 };
#endif

class Ld2412Sensor {
public:
    void begin();
    void update();

    bool isPresent() const { return _presence; }
#ifdef BOARD_REV2
    // The raw OUT level above stays as it is (the spectrum view reads it). This one is evaluated at call time,
    // so it is UNKNOWN from the moment a park is requested, not from the next 500 ms poll.
    Ld2412Presence presence() const;
#endif

private:
    bool _presence = false;
#ifdef BOARD_REV2
    void updatePresence(uint32_t nowMs, bool out);
    std::atomic<uint8_t> _tri{(uint8_t)Ld2412Presence::UNKNOWN};
    uint32_t _highSinceMs = 0;
    bool _highSinceValid = false;
#endif
};

extern Ld2412Sensor ld2412Sensor;

void initLd2412SensorTask();

#endif
