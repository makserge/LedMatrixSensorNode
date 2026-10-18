#ifndef SENSOR_READING_H
#define SENSOR_READING_H

#include <Arduino.h>
#include <atomic>

// The last good value of one sensor quantity and when it was read. The sensor task
// writes it; the display and MQTT tasks read it from the other core, hence the atomics.
// A value is usable only while isFresh(): never read, invalidated, or older than the
// caller's limit means unknown, so a dead sensor can't keep feeding its last value to
// the CO2 alarm or to Home Assistant.
template <typename T>
class SensorReading {
public:
    void set(T value) {
        _value.store(value);
        _readAtMs.store(millis());
        _valid.store(true);
    }

    void invalidate() { _valid.store(false); }

    T get() const { return _value.load(); }

    bool isFresh(uint32_t maxAgeMs) const {
        if (!_valid.load()) return false;
        // Load the stamp before calling millis(): a stamp written in between would
        // otherwise lie in the future and wrap to a huge age.
        uint32_t readAtMs = _readAtMs.load();
        return millis() - readAtMs < maxAgeMs;
    }

    // The owning task calls this every cycle, well inside maxAgeMs. It makes staleness
    // sticky: otherwise a value never refreshed again would count as fresh once more
    // when its 32-bit age wraps after 49.7 days.
    void expireIfStale(uint32_t maxAgeMs) {
        if (_valid.load() && !isFresh(maxAgeMs)) _valid.store(false);
    }

private:
    std::atomic<T> _value{T()};
    std::atomic<uint32_t> _readAtMs{0};
    std::atomic<bool> _valid{false};
};

#endif
