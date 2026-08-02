#ifndef TASK_REGISTRY_H
#define TASK_REGISTRY_H

#include <Arduino.h>

extern TaskHandle_t co2TaskHandle;
extern TaskHandle_t tempHumTaskHandle;
extern TaskHandle_t lightTaskHandle;
extern TaskHandle_t ld2412TaskHandle;
extern TaskHandle_t ld2450TaskHandle;
extern TaskHandle_t ntpClockTaskHandle;
extern TaskHandle_t mqttTaskHandle;
extern TaskHandle_t streamIngestTaskHandle;
extern TaskHandle_t ballisticsTaskHandle;
extern TaskHandle_t spectrumViewTaskHandle;

void pauseBackgroundTasksForOta();
void resumeBackgroundTasksAfterOta();

#endif