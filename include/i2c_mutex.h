#ifndef I2C_MUTEX_H
#define I2C_MUTEX_H

#include <Arduino.h>

extern SemaphoreHandle_t i2cMutex;

void initI2CMutex();

#endif
