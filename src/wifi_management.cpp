#include "wifi_management.h"
#include <esp_wifi.h>
#include "web_server_ui.h"

// The network WiFiManager saved, copied when the backup takes over the STA config
// (wifi_config_t doesn't terminate a 32-byte SSID or a 64-byte PSK).
static char primarySsid[sizeof(wifi_sta_config_t::ssid) + 1] = "";
static char primaryPass[sizeof(wifi_sta_config_t::password) + 1] = "";
static bool onBackup = false;

enum PrimaryRetryState { PRIMARY_IDLE, PRIMARY_SCANNING, PRIMARY_CONNECTING };
static PrimaryRetryState primaryState = PRIMARY_IDLE;
static unsigned long primaryStateSinceMs = 0;
static unsigned long primaryRetryIntervalMs = WIFI_PRIMARY_RETRY_MIN_MS;

static bool linkDown = false;
static unsigned long linkDownSinceMs = 0;
static unsigned long lastReconnectMs = 0;
static unsigned long lastSwitchMs = 0;

static void showIpAddress() {
    String ip = WiFi.localIP().toString();
    DisplayManager::setCustomMessage(ip.c_str(), WIFI_IP_DISPLAY_DURATION_MS, 0x00FF00);
}

// esp_wifi_disconnect() first: it also aborts an attempt still in flight (e.g. the
// core's own retry), and the driver refuses a new config or connect while connecting.
static void connectTo(const char *ssid, const char *pass) {
    esp_wifi_disconnect();
    WiFi.begin(ssid, pass);
}

// Called before every move to the backup. The STA config still holds the primary
// (the network WiFiManager saved, or the one this boot switched back to): keep a
// copy to go back to, then make the driver's config RAM-only for the rest of this
// boot. With the default flash storage WiFi.begin() would overwrite the saved
// primary in NVS, and every later boot would use only the backup.
// WiFi.persistent(false) can't do this: the core applies it only when the driver is
// first initialised, which autoConnect() has already done.
static bool prepareBackupSwitch() {
    wifi_config_t saved;
    if (esp_wifi_get_config(WIFI_IF_STA, &saved) == ESP_OK) {
        memcpy(primarySsid, saved.sta.ssid, sizeof(saved.sta.ssid));
        primarySsid[sizeof(saved.sta.ssid)] = '\0';
        memcpy(primaryPass, saved.sta.password, sizeof(saved.sta.password));
        primaryPass[sizeof(saved.sta.password)] = '\0';
    }
    if (strcmp(primarySsid, WIFI_BACKUP_SSID) == 0) primarySsid[0] = '\0'; // nothing to go back to
    if (esp_wifi_set_storage(WIFI_STORAGE_RAM) != ESP_OK) {
        Serial.println("[WiFi] Can't keep the backup network out of flash - not using it");
        return false;
    }
    return true;
}

static bool tryBackupWifiNetwork() {
    if (strlen(WIFI_BACKUP_SSID) == 0) return false; // not configured

    Serial.printf("Primary WiFi unavailable, trying backup network \"%s\"...\n", WIFI_BACKUP_SSID);
    WiFi.mode(WIFI_STA);
    if (!prepareBackupSwitch()) return false;
    WiFi.begin(WIFI_BACKUP_SSID, WIFI_BACKUP_PASS);

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_BACKUP_TIMEOUT_MS) {
        delay(250);
    }
    return WiFi.status() == WL_CONNECTED;
}

void initWiFiManagement() {
    WiFiManager wm;

    wm.setConfigPortalTimeout(WIFI_CONFIG_TIMEOUT);

    bool connected = wm.autoConnect(WIFI_PORTAL_NAME);

    if (!connected) {
        connected = onBackup = tryBackupWifiNetwork();
    }

    if (!connected) {
        Serial.println("WiFi Failed. Restarting...");
        delay(3000);
        ESP.restart();
    }

    WiFi.setSleep(false);

    showIpAddress();
    primaryStateSinceMs = millis(); // on the backup: first look for the primary one interval from now

    webUi.begin();
}

// On the backup network: now and then look for the primary with a scan filtered to
// its SSID (the backup link stays up meanwhile) and switch back once it is in range.
// A switch that doesn't connect returns to the backup and doubles the interval, so
// a primary that is visible but unusable (e.g. changed password) doesn't keep
// cutting the link.
static void updatePrimaryRetry(unsigned long now) {
    switch (primaryState) {
    case PRIMARY_IDLE:
        if (primarySsid[0] == '\0' || WiFi.status() != WL_CONNECTED) return;
        if (now - primaryStateSinceMs < primaryRetryIntervalMs) return;
        primaryStateSinceMs = now;
        if (WiFi.scanNetworks(true, false, false, 300, 0, primarySsid) == WIFI_SCAN_RUNNING) {
            primaryState = PRIMARY_SCANNING;
        }
        break;
    case PRIMARY_SCANNING: {
        int16_t found = WiFi.scanComplete();
        if (found == WIFI_SCAN_RUNNING) return;
        WiFi.scanDelete();
        primaryStateSinceMs = now;
        if (found > 0) {
            Serial.printf("[WiFi] Primary network \"%s\" in range - leaving the backup\n", primarySsid);
            connectTo(primarySsid, primaryPass);
            primaryState = PRIMARY_CONNECTING;
        } else {
            primaryState = PRIMARY_IDLE;
        }
        break;
    }
    case PRIMARY_CONNECTING:
        if (WiFi.status() == WL_CONNECTED) {
            Serial.printf("[WiFi] Back on the primary network, IP %s\n", WiFi.localIP().toString().c_str());
            onBackup = false;
            primaryState = PRIMARY_IDLE;
            showIpAddress();
        } else if (now - primaryStateSinceMs >= WIFI_PRIMARY_CONNECT_TIMEOUT_MS) {
            primaryRetryIntervalMs = min(primaryRetryIntervalMs * 2, WIFI_PRIMARY_RETRY_MAX_MS);
            Serial.printf("[WiFi] Primary network didn't connect - back to the backup, next try in %lu min\n",
                          primaryRetryIntervalMs / 60000);
            connectTo(WIFI_BACKUP_SSID, WIFI_BACKUP_PASS);
            primaryState = PRIMARY_IDLE;
            primaryStateSinceMs = now;
        }
        break;
    }
}

// Long outage with a backup configured: move to the other network in place. Going
// from a dead backup to the primary matters too - the primary retry scans only
// while the backup link is up, so a returning primary would otherwise be ignored.
static void switchNetwork(unsigned long now) {
    unsigned long downMin = (now - linkDownSinceMs) / 60000;
    if (!onBackup) {
        if (!prepareBackupSwitch()) return;
        Serial.printf("[WiFi] No connection for %lu min - switching to the backup network \"%s\"\n",
                      downMin, WIFI_BACKUP_SSID);
        connectTo(WIFI_BACKUP_SSID, WIFI_BACKUP_PASS);
        onBackup = true;
        primaryStateSinceMs = now; // first look for the primary one interval from now
    } else if (primarySsid[0] != '\0') {
        Serial.printf("[WiFi] No connection for %lu min - switching to the primary network \"%s\"\n",
                      downMin, primarySsid);
        connectTo(primarySsid, primaryPass);
        onBackup = false;
    } else {
        return; // the backup is the only network known: keep reconnecting to it
    }
    lastReconnectMs = now; // connectTo() is this round's connect attempt
}

// The core reconnects by itself only for some disconnect reasons (not ASSOC_LEAVE,
// BSS_TRANSITION_DISASSOC, AUTH_FAIL after the first connect, ...), so without this
// such a disconnect would leave the node offline until it is power-cycled.
// It never restarts the node: the boot would block in initWiFiManagement() for the
// whole outage - sensor tasks and CO2 alarm not running, open setup AP broadcast.
void updateWiFiManagement() {
    if (DisplayManager::isOtaActive()) return; // an upload is using the link - leave it alone
    unsigned long now = millis();

    if (onBackup) {
        updatePrimaryRetry(now);
        if (primaryState == PRIMARY_CONNECTING) return; // the link is down on purpose
    }

    if (WiFi.status() == WL_CONNECTED) {
        if (linkDown) {
            Serial.printf("[WiFi] Link restored after %lu s\n", (now - linkDownSinceMs) / 1000);
            linkDown = false;
        }
        return;
    }
    if (!linkDown) {
        linkDown = true;
        linkDownSinceMs = lastReconnectMs = lastSwitchMs = now;
        Serial.printf("[WiFi] Link lost (status %d)\n", (int)WiFi.status());
        return;
    }
    if (strlen(WIFI_BACKUP_SSID) > 0 && now - lastSwitchMs >= WIFI_SWITCH_NETWORK_AFTER_MS) {
        lastSwitchMs = now;
        switchNetwork(now);
    }
    if (now - lastReconnectMs >= WIFI_RECONNECT_INTERVAL_MS) {
        lastReconnectMs = now;
        Serial.printf("[WiFi] No connection for %lu s (status %d) - reconnecting\n",
                      (now - linkDownSinceMs) / 1000, (int)WiFi.status());
        esp_wifi_disconnect(); // see connectTo()
        if (esp_wifi_connect() != ESP_OK) WiFi.begin(); // STA not started: let the core bring it up
    }
}
