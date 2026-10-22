#include "web_server_ui.h"
#include <ArduinoJson.h>
#include <Update.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <esp_wifi.h>
#include "constants.h"
#include "display_manager.h"
#include "task_registry.h"

WebUi webUi;

// Server-side OTA state, polled by the web UI so it can confirm the flash
// actually succeeded (and show a real error) rather than just assuming
// success once the upload finishes.
static volatile bool otaActive = false;
static volatile size_t otaWrittenBytes = 0;
static String otaLastError = "";
static bool otaLastSuccess = false;
static unsigned long otaStartMs = 0;

// Minimal, dependency-free HTML/JS UI: firmware flash panel with
// upload/flash progress, running-firmware fingerprint and WiFi reset.
static const char INDEX_HTML[] PROGMEM = R"HTML(
<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Led Matrix Node</title>
<style>
/* Dark palette (night). The light palette below is switched in by
   applyTheme() from the browser's local time of day. */
:root{color-scheme:dark;
  --bg:#111;--fg:#eee;--card:#1c1c1c;--line:#333;--accent:#4ea3ff;--ok:#9a9;--muted:#888;
  --ctl-bg:#2a2a2a;--ctl-border:#444;--danger:#e05a5a;--danger-border:#6a3030}
:root[data-theme=light]{color-scheme:light;
  --bg:#f1f2f4;--fg:#1c1c1c;--card:#fff;--line:#dcdcdc;--accent:#1f6fd1;--ok:#3d7a3d;--muted:#777;
  --ctl-bg:#f7f7f7;--ctl-border:#bbb;--danger:#c43c3c;--danger-border:#e3a3a3}
body{font-family:system-ui,sans-serif;margin:0;padding:1rem;background:var(--bg);color:var(--fg)}
h1{font-size:1.2rem} .card{background:var(--card);border-radius:8px;padding:1rem;margin-bottom:.75rem}
.section-title{font-weight:600;margin-bottom:.6rem}
.status{font-size:.85rem;color:var(--ok)} .muted{color:var(--muted)}
button{background:var(--ctl-bg);color:var(--fg);border:1px solid var(--ctl-border);border-radius:6px;padding:.5rem .8rem}
button:disabled{opacity:.5}
button.btn-danger{color:var(--danger);border-color:var(--danger-border)}
input[type=file]{color:var(--fg);font-size:.85rem}
.progress-track{background:var(--ctl-bg);border-radius:4px;height:10px;overflow:hidden;margin-top:.6rem}
.progress-bar{background:var(--accent);height:100%;width:0%;transition:width .2s}
.progress-bar.error{background:var(--danger)}
</style>
<script>
// Light theme by day, dark by night - decided from the viewing browser's
// local clock, re-checked every minute. Runs before the body renders so
// the page never flashes the wrong theme on load.
function applyTheme(){
  const h = new Date().getHours();
  const light = h >= 7 && h < 19;
  document.documentElement.setAttribute('data-theme', light ? 'light' : 'dark');
}
applyTheme(); setInterval(applyTheme, 60000);
</script>
</head><body>
<h1>Led Matrix Node</h1>
<div class="card">
  <div class="section-title">Firmware update (OTA)</div>
  <div class="status" id="fwCurrent" style="margin-bottom:.6rem">loading...</div>
  <input type="file" id="otaFile" accept=".bin">
  <button id="otaBtn" onclick="flashFirmware()">Flash</button>
  <div class="progress-track"><div class="progress-bar" id="otaBar"></div></div>
  <div class="status" id="otaLabel">Select a firmware.bin file to flash over WiFi.</div>
</div>
<div class="card">
  <div class="section-title">WiFi</div>
  <div class="status muted" style="margin-bottom:.6rem">Erases the saved WiFi network and reboots into the setup access point.</div>
  <button id="resetWifiBtn" class="btn-danger" onclick="resetWifi()">Reset WiFi</button>
</div>
<script>
async function loadStatus(){
  try {
    const r = await fetch('/api/ota/status'); const s = await r.json();
    document.getElementById('fwCurrent').innerText =
      `Running firmware: MD5 ${s.fwMd5} (${s.fwSize} bytes) | IP ${s.ip} | up ${Math.floor(s.uptimeSec / 60)} min`;
  } catch (e) { /* device rebooting or offline - keep the last text */ }
}
async function resetWifi(){
  if (!confirm('This erases the saved WiFi network and reboots the device into setup mode. Continue?')) return;
  const btn = document.getElementById('resetWifiBtn');
  btn.innerText = 'Resetting...'; btn.disabled = true;
  let r = null;
  try {
    r = await fetch('/api/reset-wifi', {method:'POST'});
  } catch (e) { /* device is rebooting, connection may drop mid-response */ }
  if (r && !r.ok) {
    alert('WiFi reset refused: ' + await r.text());
    btn.innerText = 'Reset WiFi'; btn.disabled = false;
    return;
  }
  alert('WiFi reset - the device is rebooting into setup mode. Connect to its "Led Matrix Node" WiFi network to reconfigure it.');
}

function flashFirmware(){
  const fileInput = document.getElementById('otaFile');
  const btn = document.getElementById('otaBtn');
  const bar = document.getElementById('otaBar');
  const label = document.getElementById('otaLabel');
  if (!fileInput.files.length) { alert('Choose a firmware .bin file first'); return; }

  btn.disabled = true;
  bar.classList.remove('error');
  bar.style.width = '0%';
  label.innerText = 'Uploading...';

  const xhr = new XMLHttpRequest();
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      bar.style.width = pct + '%';
      label.innerText = `Uploading & flashing: ${pct}%`;
    }
  };
  xhr.onload = async () => {
    if (xhr.status === 200) {
      bar.style.width = '100%';
      label.innerText = 'Verifying flash...';
      // Confirm the device-side write actually succeeded before promising a reboot.
      try {
        const r = await fetch('/api/ota/status');
        const s = await r.json();
        if (s.error) {
          bar.classList.add('error');
          label.innerText = 'Flash failed: ' + s.error;
          btn.disabled = false;
          return;
        }
      } catch (e) { /* device may already be rebooting; fall through */ }
      label.innerText = 'Flash complete - device rebooting...';
      let secs = 8;
      const iv = setInterval(() => {
        secs--;
        label.innerText = `Rebooting... reconnecting in ${secs}s`;
        if (secs <= 0) { clearInterval(iv); location.reload(); }
      }, 1000);
    } else {
      bar.classList.add('error');
      label.innerText = 'Flash failed: ' + xhr.responseText;
      btn.disabled = false;
    }
  };
  xhr.onerror = () => {
    bar.classList.add('error');
    label.innerText = 'Upload error - check connection and try again.';
    btn.disabled = false;
  };
  const formData = new FormData();
  formData.append('firmware', fileInput.files[0]);
  xhr.open('POST', '/update');
  xhr.send(formData);
}

loadStatus(); setInterval(loadStatus, 10000);
</script></body></html>
)HTML";

// Hostname part of a Host header or an Origin/Referer URL: no scheme, path
// or port, lowercased.
static String hostOnly(String s) {
    int scheme = s.indexOf("://");
    if (scheme >= 0) s = s.substring(scheme + 3);
    int slash = s.indexOf('/');
    if (slash >= 0) s = s.substring(0, slash);
    if (s.startsWith("[")) { // IPv6 literal
        int rb = s.indexOf(']');
        if (rb > 0) s = s.substring(0, rb + 1);
    } else {
        int colon = s.lastIndexOf(':');
        if (colon >= 0) s = s.substring(0, colon);
    }
    s.toLowerCase();
    if (s.endsWith(".")) s.remove(s.length() - 1); // FQDN form "name.lan."
    return s;
}

// Names this device can legitimately be reached by on a LAN: an IP
// literal, a single-label router-DNS name, or a local-only suffix. Anything
// else in Host means DNS rebinding (an attacker's domain re-pointed at the
// device so a web page can read its API).
static bool isLocalHostName(const String &h) {
    // No Host at all: not a browser (browsers always send it) - and the
    // WebServer keeps the previous request's value then anyway.
    if (h.length() == 0) return true;
    IPAddress ip;
    if (ip.fromString(h) || h.startsWith("[")) return true;
    if (h.indexOf('.') < 0) return true;
    // Local-only suffixes (plus ones whose DNS an attacker can't control:
    // ts.net = Tailscale MagicDNS, fritz.box/speedport.ip = router names).
    static const char *const suffixes[] = {".local", ".lan", ".home", ".home.arpa", ".internal",
                                           ".localdomain", ".fritz.box", ".speedport.ip",
                                           ".mshome.net", ".ts.net"};
    for (const char *suf : suffixes) {
        if (h.endsWith(suf)) return true;
    }
    // Site-specific extra suffix (e.g. an office DNS zone), set at build time.
    if (strlen(WEB_EXTRA_HOST_SUFFIX) > 0 && h.endsWith(WEB_EXTRA_HOST_SUFFIX)) return true;
    return false;
}

// No login on this LAN device (by design), but no other WEBSITE may drive
// it either: a page open in any browser on the LAN could otherwise POST to
// /update (multipart is CORS-safelisted, no preflight) and flash arbitrary
// firmware, or wipe the WiFi credentials. Browsers always send Origin on
// cross-site POSTs (and "null" from sandboxed frames), so a state-changing
// request whose Origin (or, without one, Referer) names a different host
// than the device is refused. Non-browser clients (curl) send neither and
// stay allowed.
bool WebUi::requestAllowed(bool mutating, bool respond) {
    String host = hostOnly(_server.hostHeader());
    bool ok = isLocalHostName(host);
    if (ok && mutating) {
        String origin = _server.header("Origin");
        if (origin == "null") {
            ok = false;
        } else {
            if (origin.length() == 0) origin = _server.header("Referer");
            if (origin.length() > 0 && hostOnly(origin) != host) ok = false;
        }
    }
    if (!ok) {
        Serial.printf("[Web] Refused %s request (Host '%s', Origin '%s')\n",
                      mutating ? "state-changing" : "read", host.c_str(), _server.header("Origin").c_str());
        if (respond) {
            _server.send(403, "text/plain", isLocalHostName(host)
                ? "forbidden: request from another website"
                : "forbidden: unknown host name - open the device by its IP address "
                  "(or add your DNS suffix via WEB_EXTRA_HOST_SUFFIX)");
        }
    }
    return ok;
}

// The node runs sensors, MQTT and the audio pipeline in the background; park
// them for the flash write and show "OTA" on the panel (see displayTask).
static void enterOtaMode() {
    pauseBackgroundTasksForOta();
    DisplayManager::setOtaProgress(0);
    DisplayManager::setOtaActive(true);

    MatrixPanel_I2S_DMA* matrixPtr = displayManager.getMatrixPtr();
    if (matrixPtr != nullptr) {
        matrixPtr->setBrightness(DISPLAY_BRIGHTNESS);
    }
}

static void leaveOtaMode() {
    if (!DisplayManager::isOtaActive()) return;
    DisplayManager::setOtaActive(false);

    MatrixPanel_I2S_DMA* matrixPtr = displayManager.getMatrixPtr();
    if (matrixPtr != nullptr) {
        matrixPtr->setBrightness(DISPLAY_BRIGHTNESS);
    }

    resumeBackgroundTasksAfterOta();
}

void WebUi::begin() {
    static const char *headerKeys[] = {"Origin", "Referer"};
    _server.collectHeaders(headerKeys, 2);

    auto get = [this](const char *path, std::function<void()> h) {
        _server.on(path, HTTP_GET, [this, h]() { if (requestAllowed(false)) h(); });
    };
    auto change = [this](const char *path, HTTPMethod method, std::function<void()> h) {
        _server.on(path, method, [this, h]() { if (requestAllowed(true)) h(); });
    };

    get("/", [this]() { handleRoot(); });
    get("/api/ota/status", [this]() { handleApiOtaStatus(); });
    change("/api/reset-wifi", HTTP_POST, [this]() { handleApiResetWifi(); });

    // OTA firmware upload: POST /update with multipart file "firmware".
    // Progress is tracked client-side (browser knows the file size via the
    // upload.onprogress event), since flash writes happen synchronously as
    // each chunk streams in; /api/ota/status lets the UI confirm success
    // before promising a reboot.
    _server.on("/update", HTTP_POST,
        [this]() { handleOtaDone(); },
        [this]() { handleOtaUpload(); }
    );

    _server.begin();
    Serial.printf("[Web] UI and OTA ready at http://%s/\n", WiFi.localIP().toString().c_str());
}

void WebUi::loop() {
    _server.handleClient();
}

void WebUi::handleRoot() {
    _server.send_P(200, "text/html", INDEX_HTML);
}

void WebUi::handleApiOtaStatus() {
    JsonDocument doc;
    doc["active"] = (bool)otaActive;
    doc["written"] = (size_t)otaWrittenBytes;
    doc["success"] = otaLastSuccess;
    if (otaLastError.length()) doc["error"] = otaLastError;
    // MD5 of the running image (computed once, then cached by the core) - it
    // changes with every flashed build, so it shows whether an OTA took effect.
    doc["fwMd5"] = ESP.getSketchMD5();
    doc["fwSize"] = ESP.getSketchSize();
    doc["ip"] = WiFi.localIP().toString();
    doc["uptimeSec"] = millis() / 1000;
    String out;
    serializeJson(doc, out);
    _server.send(200, "application/json", out);
}

void WebUi::handleApiResetWifi() {
    Serial.println("[WiFi] Reset requested via web UI - erasing credentials and rebooting");
    _server.send(200, "text/plain", "WiFi credentials cleared - rebooting into setup mode");
    delay(300); // let the reply leave before the reboot drops the connection
    // After a fallback to the backup network the driver's config is RAM-only
    // (wifi_management.cpp): back to flash, or the erase would never reach NVS.
    esp_wifi_set_storage(WIFI_STORAGE_FLASH);
    WiFiManager wm;
    wm.resetSettings();
    delay(200);
    ESP.restart();
}

void WebUi::handleOtaDone() {
    if (!requestAllowed(true)) return; // the upload itself was already refused at its start
    // Success only if handleOtaUpload() actually completed a flash: a
    // refused upload (cross-site, begin() failed) never starts Update, so
    // !Update.hasError() alone would read as success and reboot the device
    // on the OLD firmware.
    bool ok = otaLastSuccess;
    _server.sendHeader("Connection", "close");
    _server.send(ok ? 200 : 500, "text/plain",
                 ok ? "OK, rebooting" : (otaLastError.length() ? otaLastError.c_str() : "OTA failed"));
    if (ok) {
        delay(500);
        ESP.restart();
    }
    leaveOtaMode();
}

void WebUi::handleOtaUpload() {
    HTTPUpload &upload = _server.upload();
    if (upload.status == UPLOAD_FILE_START) {
        if (!requestAllowed(true, false)) { // cross-site: never start writing an image
            otaLastError = "forbidden: cross-site request";
            otaLastSuccess = false;
            return;
        }
        Serial.printf("[OTA] Start: %s - pausing background tasks\n", upload.filename.c_str());
        otaActive = true;
        otaWrittenBytes = 0;
        otaLastError = "";
        otaLastSuccess = false;
        otaStartMs = millis();
        enterOtaMode();
        if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
            otaLastError = "Update.begin() failed - check partition scheme has room for OTA";
            Update.printError(Serial);
        }
    } else if (upload.status == UPLOAD_FILE_WRITE) {
        if (!Update.isRunning()) return; // begin() refused/never ran - drain the stream silently
        if (Update.write(upload.buf, upload.currentSize) != upload.currentSize) {
            otaLastError = "Flash write failed mid-upload";
            Update.printError(Serial);
        } else {
            otaWrittenBytes += upload.currentSize;
            int contentLength = _server.clientContentLength();
            if (contentLength > 0) {
                DisplayManager::setOtaProgress((uint8_t)min((otaWrittenBytes * 100) / contentLength, (size_t)100));
            }
        }
    } else if (upload.status == UPLOAD_FILE_END) {
        otaActive = false;
        // Not running: begin() refused/never ran, or a write failure already aborted it - error recorded.
        if (Update.isRunning()) {
            if (otaLastError.length() == 0 && Update.end(true)) {
                otaLastSuccess = true;
                Serial.printf("[OTA] Success, %u bytes in %lu ms\n", (unsigned)upload.totalSize, millis() - otaStartMs);
                return; // stay in OTA mode; handleOtaDone() reboots into the new image
            }
            if (otaLastError.length()) {
                Update.abort(); // a mid-upload write failed - don't finalize a partial image
            } else {
                otaLastError = "Update.end() failed - image may be corrupt or too large";
                Update.printError(Serial);
            }
        }
        Serial.printf("[OTA] Failed: %s - resuming normal operation\n", otaLastError.c_str());
        leaveOtaMode();
    } else if (upload.status == UPLOAD_FILE_ABORTED) {
        // The browser went away or stalled mid-upload; the boot partition is untouched.
        otaActive = false;
        otaLastError = "Upload aborted";
        Update.end(false);
        Serial.println("[OTA] Upload aborted - resuming normal operation");
        leaveOtaMode();
    }
}
