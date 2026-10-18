#ifndef TASK_REGISTRY_H
#define TASK_REGISTRY_H

#include <Arduino.h>

void pauseBackgroundTasksForOta();
void resumeBackgroundTasksAfterOta();

// Called by every background task at a safe point of its loop (no I2C/UART/socket
// call in flight). Blocks while an OTA update is running; returns true if it waited.
bool waitWhileOtaPaused();

#endif
