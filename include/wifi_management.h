#ifndef WIFI_MANAGEMENT_H
#define WIFI_MANAGEMENT_H

#include <Arduino.h>
#include <WiFiManager.h>
#include "constants.h"
#include "display_manager.h"
#include "task_registry.h"

void initWiFiManagement();
// Called from loop(): link supervision and the way back from the backup network.
void updateWiFiManagement();

#endif