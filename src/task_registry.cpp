#include "task_registry.h"

// Tasks park themselves instead of being vTaskSuspend()-ed from the OTA callback:
// a task suspended mid-call can hold the lwIP core lock (or the I2C/UART mutex)
// and freeze the network stack in the middle of the upload.
static volatile bool otaPauseRequested = false;

void pauseBackgroundTasksForOta() {
    otaPauseRequested = true;
}

void resumeBackgroundTasksAfterOta() {
    otaPauseRequested = false;
}

bool waitWhileOtaPaused() {
    if (!otaPauseRequested) return false;
    while (otaPauseRequested) {
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    return true;
}
