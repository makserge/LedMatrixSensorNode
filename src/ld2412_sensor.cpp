#include "ld2412_sensor.h"
#ifdef BOARD_REV2
#include "radar_power.h"
#endif

Ld2412Sensor ld2412Sensor;

void Ld2412Sensor::begin() {
    // OUT is high while someone is present. The board has no pull resistor on it, so
    // without the pulldown an unplugged or unpowered radar leaves the pin floating next
    // to the bit-banged panel lines and presence flaps; this way it reads "no one".
    pinMode(RADAR_PRESENCE_PIN, INPUT_PULLDOWN);
}

void Ld2412Sensor::update() {
    bool newPresence = (digitalRead(RADAR_PRESENCE_PIN) == HIGH);
    if (newPresence != _presence) {
        _presence = newPresence;
        Serial.printf("LD2412 presence: %s\n", _presence ? "DETECTED" : "CLEAR");
    }
#ifdef BOARD_REV2
    updatePresence(millis(), newPresence);
#endif
}

#ifdef BOARD_REV2
Ld2412Presence Ld2412Sensor::presence() const {
    if (g_parkRequest.load() || !g_radarPowered.load()) return Ld2412Presence::UNKNOWN;   // at once, not at the next poll
    return (Ld2412Presence)_tri.load();
}

void Ld2412Sensor::updatePresence(uint32_t nowMs, bool out) {
    const bool powered = g_radarPowered.load();                           // read once: one view of the power state per pass
    if (!powered) {                                                       // from the power-off until the next power-up
        _tri = (uint8_t)Ld2412Presence::UNKNOWN; _highSinceValid = false; return;
    }
    if (g_parkRequest.load()) {                                           // parking, the radars still powered: UNKNOWN, but OUT keeps being
        _tri = (uint8_t)Ld2412Presence::UNKNOWN;                          // timed, so a withdrawn or timed-out park does not restart the 2 s
        if (out) { if (!_highSinceValid) { _highSinceMs = nowMs; _highSinceValid = true; } }   // timer and show a false ABSENT
        else     { _highSinceValid = false; }
        return;
    }
    uint32_t since = nowMs - g_poweredAtMs.load();                        // uint32 elapsed: wrap-safe
    if ((int32_t)since < 0) since = 0;                                    // stamped a few ms after nowMs was sampled: just powered up
    if (since < LD2412_OUT_IGNORE_MS) {                                   // the OUT mask after a power-up
        _tri = (uint8_t)Ld2412Presence::UNKNOWN; _highSinceValid = false; return;
    }
    if (out) { if (!_highSinceValid) { _highSinceMs = nowMs; _highSinceValid = true; } }
    else     { _highSinceValid = false; }
    if (out && (uint32_t)(nowMs - _highSinceMs) >= LD2412_OUT_MIN_HIGH_MS) _tri = (uint8_t)Ld2412Presence::PRESENT;
    else if (since < LD2412_STATIC_WARMUP_MS)                             _tri = (uint8_t)Ld2412Presence::UNKNOWN;  // LOW or short OUT: not yet ABSENT
    else                                                                  _tri = (uint8_t)Ld2412Presence::ABSENT;
}
#endif

void ld2412SensorTask(void *pvParameters) {
    ld2412Sensor.begin();

    for (;;) {
        waitWhileOtaPaused();
        ld2412Sensor.update();
        vTaskDelay(pdMS_TO_TICKS(LD2412_SENSOR_INTERVAL));
    }
}

void initLd2412SensorTask() {
    xTaskCreatePinnedToCore(
        ld2412SensorTask, "LD2412Task", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, NULL, 0
    );
}
