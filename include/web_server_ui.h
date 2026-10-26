#ifndef WEB_SERVER_UI_H
#define WEB_SERVER_UI_H

#include <Arduino.h>
#include <WebServer.h>

class WebUi {
public:
    void begin();
    void loop();

private:
    WebServer _server{80};

    void handleRoot();
    void handleApiOtaStatus();         // GET /api/ota/status
    void handleApiResetWifi();         // POST /api/reset-wifi
#ifdef BOARD_REV2
    void handleApiRadar();             // POST /api/radar {"en":0|1}: the radars' manual switch
#endif
    void handleOtaUpload();            // POST /update (multipart firmware upload)
    void handleOtaDone();              // POST /update completion: reply, reboot on success
    // Host/Origin check against DNS rebinding and cross-site requests (see
    // .cpp). respond=false: only decide (used inside the upload stream).
    bool requestAllowed(bool mutating, bool respond = true);
};

extern WebUi webUi;

#endif
