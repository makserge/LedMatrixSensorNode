#include "ld2412_sensor.h"

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
}

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
