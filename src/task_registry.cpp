#include "task_registry.h"

TaskHandle_t co2TaskHandle = NULL;
TaskHandle_t tempHumTaskHandle = NULL;
TaskHandle_t lightTaskHandle = NULL;
TaskHandle_t ld2412TaskHandle = NULL;
TaskHandle_t ld2450TaskHandle = NULL;
TaskHandle_t ntpClockTaskHandle = NULL;
TaskHandle_t mqttTaskHandle = NULL;
TaskHandle_t streamIngestTaskHandle = NULL;
TaskHandle_t ballisticsTaskHandle = NULL;
TaskHandle_t spectrumViewTaskHandle = NULL;

static void suspendIfRunning(TaskHandle_t handle) {
    if (handle != NULL) vTaskSuspend(handle);
}

static void resumeIfSuspended(TaskHandle_t handle) {
    if (handle != NULL) vTaskResume(handle);
}

void pauseBackgroundTasksForOta() {
    suspendIfRunning(co2TaskHandle);
    suspendIfRunning(tempHumTaskHandle);
    suspendIfRunning(lightTaskHandle);
    suspendIfRunning(ld2412TaskHandle);
    suspendIfRunning(ld2450TaskHandle);
    suspendIfRunning(ntpClockTaskHandle);
    suspendIfRunning(mqttTaskHandle);
    suspendIfRunning(streamIngestTaskHandle);
    suspendIfRunning(ballisticsTaskHandle);
    suspendIfRunning(spectrumViewTaskHandle);
}

void resumeBackgroundTasksAfterOta() {
    resumeIfSuspended(co2TaskHandle);
    resumeIfSuspended(tempHumTaskHandle);
    resumeIfSuspended(lightTaskHandle);
    resumeIfSuspended(ld2412TaskHandle);
    resumeIfSuspended(ld2450TaskHandle);
    resumeIfSuspended(ntpClockTaskHandle);
    resumeIfSuspended(mqttTaskHandle);
    resumeIfSuspended(streamIngestTaskHandle);
    resumeIfSuspended(ballisticsTaskHandle);
    resumeIfSuspended(spectrumViewTaskHandle);
}