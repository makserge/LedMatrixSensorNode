#include "room_calibration.h"

#include <ArduinoJson.h>
#include <Preferences.h>
#include <cmath>
#include <cstring>

RoomCalibration roomCalibration;

static const char* const PREFS_NAMESPACE = "ld2454cal";
static const char* const PREFS_KEY = "json";
static const size_t STATE_JSON_MAX = 1280;

static void skipToNextPoint(const char*& p) {
    while (*p && *p != ';') p++;
    while (*p == ';') p++;
}

bool parsePolygon(const char* text, RoomPolygon& out, uint8_t maxVerts) {
    out.n = 0;
    if (text == nullptr) return false;

    bool ok = true;
    const char* p = text;
    while (*p) {
        while (*p == ';' || *p == ' ') p++;
        if (!*p) break;
        if (out.n >= maxVerts) {
            ok = false;
            break;
        }

        char* end = nullptr;
        const float x = strtof(p, &end);
        if (end == p || *end != ',') {
            ok = false;
            skipToNextPoint(p);
            continue;
        }
        p = end + 1;

        const float y = strtof(p, &end);
        if (end == p) {
            ok = false;
            skipToNextPoint(p);
            continue;
        }
        p = end;

        if (!std::isfinite(x) || !std::isfinite(y)) {
            ok = false;
            skipToNextPoint(p);
            continue;
        }
        out.v[out.n].x = x;
        out.v[out.n].y = y;
        out.n++;
        skipToNextPoint(p);
    }
    return ok;
}

void formatPolygon(const RoomPolygon& poly, char* buf, size_t cap) {
    if (cap == 0) return;
    size_t len = 0;
    buf[0] = '\0';
    for (uint8_t i = 0; i < poly.n && len + 1 < cap; i++) {
        const int w = snprintf(buf + len, cap - len, "%s%.1f,%.1f", i ? ";" : "",
                               poly.v[i].x, poly.v[i].y);
        if (w < 0) break;
        len += (size_t)w;
        if (len >= cap) len = cap - 1;
    }
    buf[len] = '\0';
}

void RoomCalibration::begin() {
    Preferences prefs;
    prefs.begin(PREFS_NAMESPACE, true);  // read-only
    String stored = prefs.getString(PREFS_KEY, "");
    prefs.end();
    if (stored.length() > 0) {
        applyJson(stored.c_str(), stored.length());
    }
}

bool RoomCalibration::applyJson(const char* json, size_t len) {
    JsonDocument doc;
    if (deserializeJson(doc, json, len) != DeserializationError::Ok || !doc.is<JsonObject>()) {
        Serial.println("[ROOM] calibration command ignored: not a JSON object");
        return false;
    }

    RadarCalibration c = _cal;

    // Numbers: anything that is not a number is left alone; numbers are clamped to range.
    auto setNum = [&doc](const char* key, float& field, float lo, float hi) {
        if (!(doc[key].is<float>() || doc[key].is<int>())) return;
        const float v = doc[key].as<float>();
        if (!std::isfinite(v)) return;
        field = v < lo ? lo : (v > hi ? hi : v);
    };
    setNum("radar_x", c.radarX, -2000.f, 2000.f);
    setNum("radar_y", c.radarY, -2000.f, 2000.f);
    setNum("radar_z", c.radarZ, 50.f, 400.f);
    setNum("yaw", c.yaw, -180.f, 180.f);
    setNum("pitch", c.pitch, -90.f, 90.f);
    setNum("roll", c.roll, -90.f, 90.f);
    setNum("presence_timeout_s", c.presenceTimeoutS, 0.5f, 60.f);
    setNum("area_hysteresis", c.areaHysteresisCm, 0.f, 200.f);
    setNum("area_still_speed", c.areaStillSpeed, 0.f, 100.f);
    setNum("area_confirm_s", c.areaConfirmS, 0.f, 10.f);
    setNum("area_clear_s", c.areaClearS, 0.f, 30.f);
    setNum("area_pass_speed", c.areaPassSpeed, 0.f, 300.f);

    if (doc["gates_presence"].is<bool>()) {
        c.gatesPresence = doc["gates_presence"].as<bool>();
    }

    if (doc["polygon"].is<const char*>()) {
        if (!parsePolygon(doc["polygon"].as<const char*>(), c.room, ROOM_MAX_VERTS)) {
            Serial.println("[ROOM] room outline: malformed points ignored");
        }
    }
    for (uint8_t a = 0; a < AREA_COUNT; a++) {
        char key[12];
        snprintf(key, sizeof(key), "area_%u", (unsigned)(a + 1));
        if (doc[key].is<const char*>()) {
            if (!parsePolygon(doc[key].as<const char*>(), c.area[a], AREA_MAX_VERTS)) {
                Serial.printf("[ROOM] area %u outline: malformed points ignored\n", (unsigned)(a + 1));
            }
        }
    }

    _cal = c;
    return true;
}

size_t RoomCalibration::toJson(char* buf, size_t cap) const {
    JsonDocument doc;
    doc["radar_x"] = _cal.radarX;
    doc["radar_y"] = _cal.radarY;
    doc["radar_z"] = _cal.radarZ;
    doc["yaw"] = _cal.yaw;
    doc["pitch"] = _cal.pitch;
    doc["roll"] = _cal.roll;
    doc["gates_presence"] = _cal.gatesPresence;
    doc["presence_timeout_s"] = _cal.presenceTimeoutS;
    doc["area_hysteresis"] = _cal.areaHysteresisCm;
    doc["area_still_speed"] = _cal.areaStillSpeed;
    doc["area_confirm_s"] = _cal.areaConfirmS;
    doc["area_clear_s"] = _cal.areaClearS;
    doc["area_pass_speed"] = _cal.areaPassSpeed;

    char poly[320];
    formatPolygon(_cal.room, poly, sizeof(poly));
    doc["polygon"] = poly;
    for (uint8_t a = 0; a < AREA_COUNT; a++) {
        char key[12];
        snprintf(key, sizeof(key), "area_%u", (unsigned)(a + 1));
        formatPolygon(_cal.area[a], poly, sizeof(poly));
        doc[key] = poly;
    }
    return serializeJson(doc, buf, cap);
}

void RoomCalibration::save() const {
    char buf[STATE_JSON_MAX];
    const size_t n = toJson(buf, sizeof(buf));
    if (n == 0 || n >= sizeof(buf)) return;
    Preferences prefs;
    prefs.begin(PREFS_NAMESPACE, false);  // read-write
    prefs.putString(PREFS_KEY, buf);
    prefs.end();
}
