#ifndef DISPLAY_MANAGER_H
#define DISPLAY_MANAGER_H

#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include "constants.h"
#include "led_matrix.h"
#include "ntp_clock.h"
#include "screen_renderers.h"
#include "co2_sensor.h"
#include "time.h"

class DisplayManager {
public:
    void begin();
    void updateText(const String& newText);
    void drawPixel2D(int x, int y, uint16_t color);
    MatrixPanel_I2S_DMA* getMatrixPtr() const { return _matrix; }

    static DisplayView getCurrentView();

    static void setView(DisplayView view);
    static void setView(DisplayView view, unsigned long autoReturnDelayMs);
    static void selectView(DisplayView view);

    static void nextView();
    static void setCustomMessage(const char* msg,
                                  unsigned long durationMs = MQTT_MSG_DURATION_MS,
                                  uint32_t colorRGB = 0xFF0000);
    static void getCustomMessage(uint16_t* outBuffer, int& outLength);
    static uint32_t getCustomMsgColorRGB();

    static int getScrollCount();
    static void incrementScrollCount();
    static void resetScrollCount();

    static bool isDisplayOff();
    static void toggleDisplayPower();
    static void setDisplayOff(bool off);

    static void setClockColor(uint8_t r, uint8_t g, uint8_t b);
    static uint32_t getClockColorRGB();

    static void setOtaActive(bool active);
    static bool isOtaActive();
    static void setOtaProgress(uint8_t percent);
    static uint8_t getOtaProgress();
    static unsigned long getOtaActiveSinceMs();

    static bool isSpectrumAutoEntrySuppressed();

    static void drawIconPixel(int x, int y, uint8_t r, uint8_t g, uint8_t b);
    static void showIconView(unsigned long durationMs = ICON_DISPLAY_DURATION_MS);
    static void getIconBuffer(uint16_t* outBuffer);

private:
    MatrixPanel_I2S_DMA* _matrix = nullptr;
    String _displayText = "";

    static DisplayView loadPersistedView();
    static void persistView(DisplayView view);

    static uint32_t loadPersistedColor();
    static void persistColor(uint32_t rgb888);

    static DisplayView _currentView;
    static uint16_t _customMsgBuffer[MQTT_MSG_MAX_LEN];
    static int _customMsgLength;
    static int _scrollCount;
    static unsigned long _viewExpireTime;
    static bool _displayOff;
    static uint32_t _clockColorRGB;
    static uint32_t _customMsgColorRGB;
    static bool _spectrumAutoEntrySuppressed;
    static volatile bool _otaActive;
    static volatile uint8_t _otaProgress;
    static volatile unsigned long _otaActiveSinceMs;
    static uint16_t _iconBuffer[MATRIX_TOTAL_PIXELS];
    static portMUX_TYPE _displayMux;
    static DisplayView _returnToView;
};

extern DisplayManager displayManager;

void initDisplayTask();

#endif