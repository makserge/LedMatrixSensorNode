#ifndef LD2454_SENSOR_H
#define LD2454_SENSOR_H

#include <Arduino.h>
#include "constants.h"
#include "pins.h"
#include "task_registry.h"
#include "room_mapping.h"
#include "room_calibration.h"

#define LD2454_MAX_TARGETS 3

struct Ld2454TargetData {
    bool valid = false;
    int16_t x = 0;          // mm, radar frame
    int16_t y = 0;          // mm, radar frame
    int16_t speed = 0;      // cm/s
    uint16_t distance = 0;  // mm, radar frame
    float roomX = 0.f;      // cm, room frame (valid after calibration is applied)
    float roomY = 0.f;      // cm, room frame
    float angleDeg = 0.f;   // degrees from the radar front, clockwise positive
    bool inBoundary = false;
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

    // Room mapping. setCalibration() may be called from any task; the radar task applies it
    // on the next frame, so a change never lands in the middle of a frame.
    void setCalibration(const RadarCalibration& cal);
    // Presence after the room gate and presence timeout.
    bool isPresent() const;
    bool isAreaOccupied(uint8_t index) const;

private:
    void beginRadarLink();
    void enableMultiTargetTracking();
    bool sendCommand(uint8_t command, const uint8_t* value = nullptr, size_t valueLen = 0);
    void processByte(uint8_t byte);
    void processTrackingFrame();
    void processAckFrame();
    void clearTargets();
    void applyPendingCalibration_();

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

    // Room mapping. _cal, _rotation, _areas, _lastPresenceMs and _presenceTimeoutMs belong to
    // the radar task; _pendingCal, _calPending, _presenceOn and _areaBits are guarded by _targetsMux.
    RadarCalibration _cal;
    RadarCalibration _pendingCal;
    volatile bool _calPending = false;
    mmwave_room::Mat3 _rotation;
    mmwave_room::Occupancy _areas;
    uint32_t _presenceTimeoutMs = 5000;
    uint32_t _lastPresenceMs = 0;
    bool _presenceOn = false;
    uint8_t _areaBits = 0;
};

extern Ld2454Sensor ld2454Sensor;

void initLd2454SensorTask();

#endif
