#include "ld2412_sensor.h"

Ld2412Sensor ld2412Sensor;

void Ld2412Sensor::begin() {
    pinMode(RADAR_PRESENCE_PIN, INPUT);
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
        ld2412Sensor.update();
        vTaskDelay(pdMS_TO_TICKS(LD2412_SENSOR_INTERVAL));
    }
}

void initLd2412SensorTask() {
    xTaskCreatePinnedToCore(
        ld2412SensorTask, "LD2412Task", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, &ld2412TaskHandle, 0   // was NULL
    );
}
