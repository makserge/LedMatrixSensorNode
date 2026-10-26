#ifndef LD2454_FRAMES_H
#define LD2454_FRAMES_H

#include <Arduino.h>
#include "constants.h"
#include "ld2454_sensor.h"

// One parsed LD2454 tracking frame on its way from LD2454Task to MqttTask (28 bytes).
struct Ld2454Frame {
    uint32_t seq;                    // frame number since boot: 0, 1, 2, ... one per parsed frame
    uint32_t ms;                     // millis() when processTrackingFrame() parsed it
    uint8_t mask;                    // bit i set = target slot i valid; 0 = empty frame
    uint8_t reserved;
    int16_t x[LD2454_MAX_TARGETS];   // mm
    int16_t y[LD2454_MAX_TARGETS];   // mm
    int16_t v[LD2454_MAX_TARGETS];   // speed, cm/s
};

// Frames from LD2454Task (core 0, the only producer) to MqttTask (core 1, the only consumer),
// behind a spinlock held only for copies. When the ring is full, push() overwrites the oldest
// unread frame: that frame's seq is then missing downstream, which is how a loss shows.
// After an aborted OTA upload the frames the UART buffered during the pause (up to about 38) are parsed
// when LD2454Task resumes, all at the resume time: the task mutes the ring for that one update(), so
// they never get a seq and stay out of the stream (the driver itself parses them as before).
class Ld2454FrameRing {
public:
    void push(const Ld2454TargetData* targets, uint32_t ms);   // targets: LD2454_MAX_TARGETS entries
    size_t pop(Ld2454Frame* out, size_t maxFrames);             // oldest first; returns how many
    uint32_t overwritten() const;                               // frames lost to overflow since boot
#ifdef BOARD_REV2
    uint32_t pushed() const;                                    // frames pushed since boot (= _head): the status JSON's seq
#endif
    // LD2454Task only, the producer (push() runs on it, so no lock): frames pushed between beginMute()
    // and endMute() are dropped without a seq. endMute() returns how many.
    void beginMute();
    uint32_t endMute();

private:
    Ld2454Frame _frames[LD2454_RING_FRAMES] = {};
    uint32_t _head = 0;         // frames pushed since boot = seq of the next frame
    uint32_t _tail = 0;         // seq of the oldest unread frame
    uint32_t _overwritten = 0;
    bool _muted = false;
    uint32_t _mutedFrames = 0;
    mutable portMUX_TYPE _mux = portMUX_INITIALIZER_UNLOCKED;
};

extern Ld2454FrameRing ld2454FrameRing;

// The frames s .. s+n-1 (consecutive seq) of one MQTT message. Only the frames with targets are
// listed; the empty ones are covered by n.
struct Ld2454Batch {
    uint32_t s;
    uint32_t n;
    uint32_t firstMs;   // parse time of frame s (valid while n > 0)
    uint32_t lastMs;    // parse time of frame s+n-1 (valid while n > 0)
    uint8_t listed;
    Ld2454Frame f[LD2454_STREAM_MAX_FRAMES];
};

// The batching rules of the frames stream (MqttTask only). Pure logic: the caller feeds frames in
// ring order and the time, and publishes or drops every batch it gets back. A returned batch stays
// valid until the next add() or poll().
class Ld2454FrameBatcher {
public:
    const Ld2454Batch* add(const Ld2454Frame& frame, uint32_t nowMs);
#ifdef BOARD_REV2
    const Ld2454Batch* poll(uint32_t nowMs, uint32_t idleMs);   // idleMs: the heartbeat interval of the radar phase
#else
    const Ld2454Batch* poll(uint32_t nowMs);
#endif
    uint32_t seqGapFrames() const { return _seqGapFrames; }    // frames missing between ring reads

private:
    const Ld2454Batch* close(uint32_t nowMs);

    Ld2454Batch _pending = {};
    Ld2454Batch _closed = {};
    bool _prevHadTargets = false;     // the last closed batch listed a frame
    bool _targetGateOpen = true;      // LD2454_STREAM_BATCH_MS passed since the last batch with targets
    uint32_t _lastCloseMs = 0;
    uint32_t _lastTargetCloseMs = 0;
    uint32_t _seqGapFrames = 0;
};

// Writes the JSON payload of one batch (P1 spec section 1.1) and returns its length, or 0 if it
// would not fit (it always fits LD2454_FRAMES_JSON_MAX: see the static_assert in the .cpp).
// tMs: uptime in ms from esp_timer_get_time(); its low 32 bits equal millis().
size_t formatLd2454Frames(char* out, size_t cap, const Ld2454Batch& batch, uint64_t tMs,
                          const char* bootId, bool multiTarget, bool ld2412Present,
                          uint8_t observingMask, char stateCode);

static const size_t LD2454_FRAMES_JSON_MAX = 2048;   // static payload buffer in MqttTask, incl. the NUL

#endif
