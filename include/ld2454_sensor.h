#ifndef LD2454_SENSOR_H
#define LD2454_SENSOR_H

#include <Arduino.h>
#include "constants.h"
#include "pins.h"
#include "task_registry.h"

#define LD2454_MAX_TARGETS 3

struct Ld2454TargetData {
    bool valid;
    int16_t x;         // mm
    int16_t y;         // mm
    int16_t speed;     // cm/s
    uint16_t distance; // mm
};

class Ld2454Sensor {
public:
    void begin();
    void update();

    Ld2454TargetData getTarget(uint8_t index) const;
    uint8_t getTargetCount() const { return LD2454_MAX_TARGETS; }
    bool hasAnyTarget() const;
    // True once the radar has ACKed multi-target mode; reset whenever it may have rebooted.
    bool isMultiTargetEnabled() const { return _multiTargetEnabled; }

private:
    void beginRadarLink();
    void enableMultiTargetTracking();
    bool sendCommand(uint8_t command, const uint8_t* value = nullptr, size_t valueLen = 0);
    void processByte(uint8_t byte);
    void processTrackingFrame();
    void processAckFrame();
    void clearTargets();

    Ld2454TargetData _targets[LD2454_MAX_TARGETS] = {};
    mutable portMUX_TYPE _targetsMux = portMUX_INITIALIZER_UNLOCKED;

    uint8_t _rxFrame[64];
    size_t _rxLen = 0;
    uint32_t _headerWindow = 0; // last 4 bytes received, newest in the low byte

    bool _ackReceived = false;
    uint8_t _ackCommand = 0;
    uint16_t _ackStatus = 0;

    volatile bool _multiTargetEnabled = false;
    bool _modeReapplyPending = false;
    bool _retryOnNextFrame = false;
    bool _immediateAttempt = false; // the next attempt was triggered by _retryOnNextFrame
    bool _configuring = false;      // inside enableMultiTargetTracking()
    bool _lateResumeSeen = false;
    uint32_t _lastConfigAttemptMs = 0;
    uint32_t _retryDelayMs = 0;
    uint32_t _lastDataMs = 0;
    uint32_t _lastFrameMs = 0; // 0 = no frame since the last configuration attempt
    uint32_t _configEndMs = 0;
    uint32_t _frameCount = 0;
};

extern Ld2454Sensor ld2454Sensor;

void initLd2454SensorTask();

#endif
