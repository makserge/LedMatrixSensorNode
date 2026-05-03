#ifndef WIFI_MANAGEMENT_H
#define WIFI_MANAGEMENT_H

#include <Arduino.h>
#include <WiFiManager.h>
#include <ElegantOTA.h>
#include <ESPAsyncWebServer.h>
#include "constants.h"

void initWiFiManagement();
void updateWiFiManagement();

#endif