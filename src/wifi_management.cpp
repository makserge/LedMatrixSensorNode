#include "wifi_management.h"

AsyncWebServer server(80);

static void handleWifiReset(AsyncWebServerRequest *request) {
    request->send(200, "text/html",
        "<html><body><p>WiFi settings cleared. Rebooting into setup mode...</p></body></html>");

    WiFiManager wm;
    wm.resetSettings();
    delay(500); // let the response flush before the reboot drops the connection
    ESP.restart();
}

static bool tryBackupWifiNetwork() {
    if (strlen(WIFI_BACKUP_SSID) == 0) return false; // not configured

    Serial.printf("Primary WiFi unavailable, trying backup network \"%s\"...\n", WIFI_BACKUP_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_BACKUP_SSID, WIFI_BACKUP_PASS);

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_BACKUP_TIMEOUT_MS) {
        delay(250);
    }
    return WiFi.status() == WL_CONNECTED;
}

static unsigned long otaStartMs = 0;

static void onOtaStart() {
    otaStartMs = millis();
    Serial.println("[OTA] Update starting - pausing background tasks");
    pauseBackgroundTasksForOta();
    DisplayManager::setOtaProgress(0);
    DisplayManager::setOtaActive(true);

    MatrixPanel_I2S_DMA* matrixPtr = displayManager.getMatrixPtr();
    if (matrixPtr != nullptr) {
        matrixPtr->setBrightness(DISPLAY_BRIGHTNESS);
    }
}

static void onOtaProgress(size_t current, size_t total) {
    if (total > 0) {
        DisplayManager::setOtaProgress((uint8_t)((current * 100) / total));
    }
}

static void onOtaEnd(bool success) {
    Serial.printf("[OTA] Update %s (%lu ms)\n", success ? "finished successfully" : "failed", millis() - otaStartMs);
    DisplayManager::setOtaActive(false);

    MatrixPanel_I2S_DMA* matrixPtr = displayManager.getMatrixPtr();
    if (matrixPtr != nullptr) {
        matrixPtr->setBrightness(DISPLAY_BRIGHTNESS);
    }

    resumeBackgroundTasksAfterOta();
}

void initWiFiManagement() {
    WiFiManager wm;

    wm.setConfigPortalTimeout(WIFI_CONFIG_TIMEOUT);

    bool connected = wm.autoConnect(WIFI_PORTAL_NAME);

    if (!connected) {
        connected = tryBackupWifiNetwork();
    }

    if (!connected) {
        Serial.println("WiFi Failed. Restarting...");
        delay(3000);
        ESP.restart();
    }

    WiFi.setSleep(false);

    String ip = WiFi.localIP().toString();
    DisplayManager::setCustomMessage(ip.c_str(), WIFI_IP_DISPLAY_DURATION_MS, 0x00FF00);

    ElegantOTA.begin(&server);
    ElegantOTA.onStart(onOtaStart);
    ElegantOTA.onProgress(onOtaProgress);
    ElegantOTA.onEnd(onOtaEnd);

    server.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
        request->send(200, "text/html",
            "<html><body>"
            "<h3>Led Matrix Node: Online</h3>"
            "<p><a href=\"/update\">Firmware update</a></p>"
            "<p><a href=\"/wifi-reset\" onclick=\"return confirm('Reset WiFi settings and reboot?');\">Reset WiFi settings</a></p>"
            "</body></html>");
    });

    server.on("/wifi-reset", HTTP_GET, handleWifiReset);

    server.begin();
    Serial.println("WiFi & OTA Server Ready.");
}

void updateWiFiManagement() {
    ElegantOTA.loop();
}