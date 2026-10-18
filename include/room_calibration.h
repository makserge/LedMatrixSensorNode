#ifndef ROOM_CALIBRATION_H
#define ROOM_CALIBRATION_H

// Room calibration for the LD2454: mounting pose, room outline, occupancy areas and
// their tuning. Persisted in NVS as the same JSON the MQTT command topic accepts.

#include <Arduino.h>
#include "room_mapping.h"

static const uint8_t ROOM_MAX_VERTS = 16;  // room outline; 16 points keep the HA text entity under 255 characters
static const uint8_t AREA_MAX_VERTS = 12;  // matches mmwave_room::Area
static const uint8_t AREA_COUNT = 3;

struct RoomPolygon {
    mmwave_room::Vec2 v[ROOM_MAX_VERTS];
    uint8_t n = 0;
};

struct RadarCalibration {
    float radarX = 0.f;          // radar position in the room, cm from the chosen origin
    float radarY = 0.f;
    float radarZ = 150.f;        // mounting height, cm (stored only; the mapping is 2D)
    float yaw = 0.f;             // degrees, clockwise positive; 0 = radar faces room +Y
    float pitch = 0.f;
    float roll = 0.f;
    RoomPolygon room;            // empty = no boundary filter
    RoomPolygon area[AREA_COUNT];
    bool gatesPresence = true;   // targets outside the room do not count toward presence
    float presenceTimeoutS = 5.f;
    float areaHysteresisCm = 50.f;
    float areaStillSpeed = 15.f;
    float areaConfirmS = 0.4f;
    float areaClearS = 2.0f;
    float areaPassSpeed = 80.f;
};

// Parses "x,y;x,y;..." in cm. An empty string clears the polygon. Bad points are
// skipped; returns false if any point was rejected.
bool parsePolygon(const char* text, RoomPolygon& out, uint8_t maxVerts);
void formatPolygon(const RoomPolygon& poly, char* buf, size_t cap);

// Owns the persisted calibration. Only the MQTT task uses it; the radar task gets copies
// through Ld2454Sensor::setCalibration().
class RoomCalibration {
public:
    // Loads the saved calibration from flash. Call once, before the radar task starts.
    void begin();

    const RadarCalibration& get() const { return _cal; }

    // Applies a JSON command such as {"radar_x":120,"polygon":"0,0;400,0;400,300;0,300"}.
    // Unknown keys are ignored and out-of-range numbers are clamped.
    // Returns false if the payload is not a JSON object.
    bool applyJson(const char* json, size_t len);

    // Writes the full state as JSON. Returns the number of bytes written.
    size_t toJson(char* buf, size_t cap) const;

    void save() const;

private:
    RadarCalibration _cal;
};

extern RoomCalibration roomCalibration;

#endif
