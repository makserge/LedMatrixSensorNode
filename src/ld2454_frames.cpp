#include "ld2454_frames.h"

#include <stdarg.h>

Ld2454FrameRing ld2454FrameRing;

static_assert(sizeof(Ld2454Frame) == 28, "Ld2454Frame is 28 bytes");
static_assert((LD2454_RING_FRAMES & (LD2454_RING_FRAMES - 1)) == 0,
              "LD2454_RING_FRAMES must be a power of two: seq % size must survive the uint32_t wrap");

// Longest possible payload: every number at its type's widest. Header 142 characters
// ("t" 16 digits: int64 microseconds / 1000; s, n, a0, a1 10 digits each), then per listed frame
// "[k,age" (2 x 10 digits) plus 3 x ",slot,x,y,v" (1 + 3 x 6 characters) and "]": 92 characters.
static const size_t FRAMES_JSON_WORST = 142 + LD2454_STREAM_MAX_FRAMES * 92 + (LD2454_STREAM_MAX_FRAMES - 1);
static_assert(FRAMES_JSON_WORST + 1 <= LD2454_FRAMES_JSON_MAX, "frames payload buffer too small");

void Ld2454FrameRing::push(const Ld2454TargetData* targets, uint32_t ms) {
    if (_muted) {
        _mutedFrames++;
        return;
    }
    Ld2454Frame frame = {};
    frame.ms = ms;
    for (uint8_t i = 0; i < LD2454_MAX_TARGETS; i++) {
        if (!targets[i].valid) continue;
        frame.mask |= (uint8_t)(1u << i);
        frame.x[i] = targets[i].x;
        frame.y[i] = targets[i].y;
        frame.v[i] = targets[i].speed;
    }

    portENTER_CRITICAL(&_mux);
    frame.seq = _head;
    if (_head - _tail == LD2454_RING_FRAMES) {   // full: drop the oldest unread frame
        _tail++;
        _overwritten++;
    }
    _frames[_head % LD2454_RING_FRAMES] = frame;
    _head++;
    portEXIT_CRITICAL(&_mux);
}

size_t Ld2454FrameRing::pop(Ld2454Frame* out, size_t maxFrames) {
    size_t count = 0;
    portENTER_CRITICAL(&_mux);
    while (count < maxFrames && _tail != _head) {
        out[count++] = _frames[_tail % LD2454_RING_FRAMES];
        _tail++;
    }
    portEXIT_CRITICAL(&_mux);
    return count;
}

void Ld2454FrameRing::beginMute() {
    _muted = true;
    _mutedFrames = 0;
}

uint32_t Ld2454FrameRing::endMute() {
    _muted = false;
    return _mutedFrames;
}

uint32_t Ld2454FrameRing::overwritten() const {
    portENTER_CRITICAL(&_mux);
    uint32_t count = _overwritten;
    portEXIT_CRITICAL(&_mux);
    return count;
}

const Ld2454Batch* Ld2454FrameBatcher::add(const Ld2454Frame& frame, uint32_t nowMs) {
    const Ld2454Batch* closed = nullptr;
    uint32_t expected = _pending.s + _pending.n;
    if (frame.seq != expected) {
        // Frames were lost on the way (ring overflow). A batch covers consecutive frames only.
        _seqGapFrames += frame.seq - expected;
        if (_pending.n > 0) closed = close(nowMs);
        _pending.s = frame.seq;
    } else if (_pending.n > 0 && frame.ms - _pending.lastMs >= LD2454_RESTART_GAP_MS) {
        closed = close(nowMs);   // the radar paused: no batch spans the pause
    }

    if (_pending.n == 0) _pending.firstMs = frame.ms;
    _pending.lastMs = frame.ms;
    _pending.n++;
    if (frame.mask != 0) {
        _pending.f[_pending.listed++] = frame;
        if (_pending.listed == LD2454_STREAM_MAX_FRAMES) {
            closed = close(nowMs);   // cannot already be set: a batch closed above holds only this frame
        }
    }
    return closed;
}

const Ld2454Batch* Ld2454FrameBatcher::poll(uint32_t nowMs) {
    if (!_targetGateOpen && nowMs - _lastTargetCloseMs >= LD2454_STREAM_BATCH_MS) {
        _targetGateOpen = true;
    }
    if (_pending.listed > 0 && _targetGateOpen) return close(nowMs);           // targets: <= 500 ms apart
    if (_prevHadTargets && nowMs - _lastCloseMs >= LD2454_STREAM_BATCH_MS) {     // targets gone: one more
        return close(nowMs);
    }
    if (nowMs - _lastCloseMs >= LD2454_STREAM_IDLE_MS) return close(nowMs);     // heartbeat
    return nullptr;
}

const Ld2454Batch* Ld2454FrameBatcher::close(uint32_t nowMs) {
    _closed = _pending;
    _prevHadTargets = _closed.listed > 0;
    if (_prevHadTargets) {
        _lastTargetCloseMs = nowMs;
        _targetGateOpen = false;
    }
    _lastCloseMs = nowMs;
    _pending.s = _closed.s + _closed.n;
    _pending.n = 0;
    _pending.listed = 0;
    return &_closed;
}

namespace {

// snprintf appender that stops at the first write that does not fit.
struct JsonOut {
    char* buf;
    size_t cap;
    size_t len;
    bool ok;

    void add(const char* fmt, ...) __attribute__((format(printf, 2, 3))) {
        if (!ok) return;
        va_list args;
        va_start(args, fmt);
        int written = vsnprintf(buf + len, cap - len, fmt, args);
        va_end(args);
        if (written < 0 || (size_t)written >= cap - len) {
            ok = false;
            return;
        }
        len += (size_t)written;
    }
};

}  // namespace

size_t formatLd2454Frames(char* out, size_t cap, const Ld2454Batch& batch, uint64_t tMs,
                          const char* bootId, bool multiTarget, bool ld2412Present,
                          uint8_t observingMask, char stateCode) {
    if (cap == 0) return 0;
    const uint32_t t32 = (uint32_t)tMs;   // = millis() now; every age is t32 - frame.ms
    JsonOut o = {out, cap, 0, true};
    o.add("{\"v\":1,\"b\":\"%s\",\"t\":%llu,\"s\":%lu,\"n\":%lu", bootId, (unsigned long long)tMs,
          (unsigned long)batch.s, (unsigned long)batch.n);
    if (batch.n > 0) {
        o.add(",\"a0\":%lu,\"a1\":%lu", (unsigned long)(t32 - batch.firstMs),
              (unsigned long)(t32 - batch.lastMs));
    }
    o.add(",\"mt\":%u,\"p2\":%u,\"pw\":%u,\"sm\":\"%c\"", multiTarget ? 1u : 0u, ld2412Present ? 1u : 0u,
          (unsigned)observingMask, stateCode);
    if (batch.listed > 0) {
        o.add(",\"f\":[");
        for (uint8_t i = 0; i < batch.listed; i++) {
            const Ld2454Frame& frame = batch.f[i];
            o.add("%s[%lu,%lu", i ? "," : "", (unsigned long)(frame.seq - batch.s),
                  (unsigned long)(t32 - frame.ms));
            for (uint8_t slot = 0; slot < LD2454_MAX_TARGETS; slot++) {
                if (frame.mask & (1u << slot)) {
                    o.add(",%u,%d,%d,%d", (unsigned)slot, frame.x[slot], frame.y[slot], frame.v[slot]);
                }
            }
            o.add("]");
        }
        o.add("]");
    }
    o.add("}");
    return o.ok ? o.len : 0;
}
