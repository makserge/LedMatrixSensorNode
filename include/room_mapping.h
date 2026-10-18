#ifndef ROOM_MAPPING_H
#define ROOM_MAPPING_H

// Radar-to-room mapping for the LD2454. Ported from mmwave-component
// (components/ld2454/ld2454_transform.h and area_occupancy.h, MIT licence).
//
// Conventions (same as the reference component and mmwave-card):
//   - Radar frame: X to the right, Y straight ahead of the radar, cm.
//   - Room frame: origin at a corner you choose, cm.
//   - yaw 0 means the radar faces room +Y; yaw grows clockwise (towards +X).
//   - R = Rz(yaw) * Rx(pitch) * Ry(roll). A 2D radar has no Z output, so only
//     the first two columns of R are used.

#include <Arduino.h>
#include <cmath>
#include <cstdint>

namespace mmwave_room {

static constexpr float kPi = 3.14159265358979f;

struct Vec2 {
    float x = 0.f;
    float y = 0.f;
};

struct Mat3 {
    float m[3][3] = {{1.f, 0.f, 0.f}, {0.f, 1.f, 0.f}, {0.f, 0.f, 1.f}};
};

inline Mat3 buildRotation(float yawDeg, float pitchDeg, float rollDeg) {
    const float d2r = kPi / 180.f;
    const float y = yawDeg * d2r;
    const float p = pitchDeg * d2r;
    const float r = rollDeg * d2r;
    const float sy = sinf(y), cy = cosf(y);
    const float sp = sinf(p), cp = cosf(p);
    const float sr = sinf(r), cr = cosf(r);
    Mat3 R;
    R.m[0][0] = cy * cr + sy * sp * sr;
    R.m[0][1] = sy * cp;
    R.m[0][2] = -cy * sr + sy * sp * cr;
    R.m[1][0] = -sy * cr + cy * sp * sr;
    R.m[1][1] = cy * cp;
    R.m[1][2] = sy * sr + cy * sp * cr;
    R.m[2][0] = cp * sr;
    R.m[2][1] = -sp;
    R.m[2][2] = cp * cr;
    return R;
}

// Ray casting. A room outline with fewer than 3 points filters nothing.
inline bool pointInRoom(float px, float py, const Vec2* poly, uint8_t n) {
    if (n < 3) return true;
    bool inside = false;
    for (uint8_t i = 0, j = n - 1; i < n; j = i++) {
        const float xi = poly[i].x, yi = poly[i].y;
        const float xj = poly[j].x, yj = poly[j].y;
        const bool crosses = ((yi > py) != (yj > py)) && (px < (xj - xi) * (py - yi) / (yj - yi) + xi);
        if (crosses) inside = !inside;
    }
    return inside;
}

struct RoomPoint {
    float x = 0.f;  // cm, room frame
    float y = 0.f;
    bool inBoundary = false;
};

// Radar-frame point (cm) -> room frame, plus the boundary test.
inline RoomPoint toRoom(float lx, float ly, const Mat3& R, float radarX, float radarY,
                        const Vec2* room, uint8_t roomN) {
    RoomPoint p;
    p.x = radarX + R.m[0][0] * lx + R.m[0][1] * ly;
    p.y = radarY + R.m[1][0] * lx + R.m[1][1] * ly;
    p.inBoundary = pointInRoom(p.x, p.y, room, roomN);
    return p;
}

// ── Occupancy areas (desk, bed, door) ──────────────────────────────────────

struct Area {
    static constexpr uint8_t kMaxVertices = 12;
    Vec2 v[kMaxVertices];
    uint8_t n = 0;

    bool ready() const { return n >= 3; }

    Vec2 centroid() const {
        Vec2 c;
        if (n == 0) return c;
        float sx = 0.f, sy = 0.f;
        for (uint8_t i = 0; i < n; i++) {
            sx += v[i].x;
            sy += v[i].y;
        }
        c.x = sx / n;
        c.y = sy / n;
        return c;
    }
};

// Even-odd test. An area with fewer than 3 points contains nothing.
inline bool inArea(float px, float py, const Area& a) {
    if (a.n < 3) return false;
    bool inside = false;
    for (uint8_t i = 0, j = a.n - 1; i < a.n; j = i++) {
        const float xi = a.v[i].x, yi = a.v[i].y;
        const float xj = a.v[j].x, yj = a.v[j].y;
        const float dy = yj - yi;
        if ((yi > py) == (yj > py) || dy == 0.f) continue;
        if (px < (xj - xi) * (py - yi) / dy + xi) inside = !inside;
    }
    return inside;
}

inline float distToSegment(float px, float py, float ax, float ay, float bx, float by) {
    const float vx = bx - ax, vy = by - ay;
    const float wx = px - ax, wy = py - ay;
    const float c1 = vx * wx + vy * wy;
    if (c1 <= 0.f) return hypotf(px - ax, py - ay);
    const float c2 = vx * vx + vy * vy;
    if (c2 <= c1) return hypotf(px - bx, py - by);
    const float t = c1 / c2;
    return hypotf(px - (ax + t * vx), py - (ay + t * vy));
}

inline float distOutside(float px, float py, const Area& a) {
    if (inArea(px, py, a)) return 0.f;
    float best = 1e9f;
    for (uint8_t i = 0; i < a.n; i++) {
        const Vec2& p = a.v[i];
        const Vec2& q = a.v[(i + 1) % a.n];
        best = fminf(best, distToSegment(px, py, p.x, p.y, q.x, q.y));
    }
    return best;
}

struct Sample {
    bool active = false;
    bool inBoundary = false;
    float x = 0.f;      // cm, room frame
    float y = 0.f;
    float speed = 0.f;  // cm/s
};

// Each target is assigned to at most one area. Hysteresis keeps it there while it
// stays close, a still target keeps its area, a fast target passing through does
// not count, and the occupied flag waits for a confirm / clear delay before changing.
class Occupancy {
public:
    static constexpr uint8_t kAreas = 3;
    static constexpr uint8_t kTargets = 4;

    void setHysteresis(float cm) { _hyst = cm < 0.f ? 0.f : cm; }
    void setStillSpeed(float cmPerS) { _still = cmPerS < 0.f ? 0.f : cmPerS; }
    void setPassSpeed(float cmPerS) { _pass = cmPerS < 0.f ? 0.f : cmPerS; }
    void setConfirmMs(uint32_t ms) { _confirmMs = ms; }
    void setClearMs(uint32_t ms) { _clearMs = ms; }

    void clearArea(uint8_t i) {
        if (i < kAreas) _areas[i].n = 0;
    }

    void addPoint(uint8_t i, float x, float y) {
        if (i >= kAreas || _areas[i].n >= Area::kMaxVertices) return;
        _areas[i].v[_areas[i].n].x = x;
        _areas[i].v[_areas[i].n].y = y;
        _areas[i].n++;
    }

    bool occupied(uint8_t i) const { return i < kAreas && _occupied[i]; }

    void update(const Sample* samples, uint8_t n, uint32_t nowMs) {
        bool next[kAreas] = {};
        float spd[kAreas];
        for (uint8_t a = 0; a < kAreas; a++) spd[a] = 1e9f;
        if (n > kTargets) n = kTargets;

        for (uint8_t t = 0; t < n; t++) {
            const Sample& s = samples[t];
            if (!s.active || !s.inBoundary) {
                _assign[t] = -1;
                continue;
            }
            const bool still = _still > 0.f && fabsf(s.speed) < _still;
            const int8_t cur = _assign[t];
            if (cur >= 0 && cur < (int8_t)kAreas && _areas[cur].ready()) {
                if (still || distOutside(s.x, s.y, _areas[cur]) <= _hyst) {
                    next[cur] = true;
                    spd[cur] = fminf(spd[cur], fabsf(s.speed));
                    continue;
                }
            }
            int8_t best = -1;
            float bestD = 1e9f;
            for (uint8_t a = 0; a < kAreas; a++) {
                if (!_areas[a].ready() || !inArea(s.x, s.y, _areas[a])) continue;
                const Vec2 c = _areas[a].centroid();
                const float d = hypotf(s.x - c.x, s.y - c.y);
                if (d < bestD) {
                    bestD = d;
                    best = (int8_t)a;
                }
            }
            _assign[t] = best;
            if (best >= 0) {
                next[best] = true;
                spd[best] = fminf(spd[best], fabsf(s.speed));
            }
        }
        for (uint8_t t = n; t < kTargets; t++) _assign[t] = -1;
        applyOccupied(next, spd, nowMs);
    }

private:
    void applyOccupied(const bool* next, const float* spd, uint32_t nowMs) {
        for (uint8_t a = 0; a < kAreas; a++) {
            bool want = next[a];
            if (want && !_occupied[a] && _pass > 0.f && spd[a] >= _pass) want = false;
            if (want) {
                _waitOff[a] = false;
                if (_occupied[a] || _confirmMs == 0) {
                    _occupied[a] = true;
                    _waitOn[a] = false;
                    continue;
                }
                if (!_waitOn[a]) {
                    _waitOn[a] = true;
                    _onSince[a] = nowMs;
                }
                if (nowMs - _onSince[a] >= _confirmMs) {
                    _occupied[a] = true;
                    _waitOn[a] = false;
                }
            } else {
                _waitOn[a] = false;
                if (!_occupied[a] || _clearMs == 0) {
                    _occupied[a] = false;
                    _waitOff[a] = false;
                    continue;
                }
                if (!_waitOff[a]) {
                    _waitOff[a] = true;
                    _offSince[a] = nowMs;
                }
                if (nowMs - _offSince[a] >= _clearMs) {
                    _occupied[a] = false;
                    _waitOff[a] = false;
                }
            }
        }
    }

    Area _areas[kAreas];
    bool _occupied[kAreas] = {};
    bool _waitOn[kAreas] = {};
    bool _waitOff[kAreas] = {};
    uint32_t _onSince[kAreas] = {};
    uint32_t _offSince[kAreas] = {};
    int8_t _assign[kTargets] = {-1, -1, -1, -1};
    float _hyst = 50.f;
    float _still = 15.f;
    float _pass = 80.f;
    uint32_t _confirmMs = 400;
    uint32_t _clearMs = 2000;
};

}  // namespace mmwave_room

#endif
