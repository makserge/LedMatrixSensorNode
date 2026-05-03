#include "wifi_management.h"

AsyncWebServer server(80);

void initWiFiManagement() {
    WiFiManager wm;

    wm.setConfigPortalTimeout(WIFI_CONFIG_TIMEOUT);

    if (!wm.autoConnect(WIFI_PORTAL_NAME)) {
        Serial.println("WiFi Failed. Restarting...");
        delay(3000);
        ESP.restart();
    }

    ElegantOTA.begin(&server); 

    server.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
        request->send(200, "text/plain", "Led Matrix Node: Online");
    });

    server.begin();
    Serial.println("WiFi & OTA Server Ready.");
}

void updateWiFiManagement() {
    ElegantOTA.loop();
}
