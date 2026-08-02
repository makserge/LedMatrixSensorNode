#include "i2c_mutex.h"

SemaphoreHandle_t i2cMutex = nullptr;

void initI2CMutex() {
    if (i2cMutex == nullptr) {
        i2cMutex = xSemaphoreCreateMutex();
    }
}
