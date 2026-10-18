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

    // Return true if the view was switched (spectrum can be refused by an active
    // message/icon or by the user's manual override). Switching back to a message or
    // icon (after the CO2 popup) replays it with its full duration.
    static bool setView(DisplayView view);
    static bool setView(DisplayView view, unsigned long autoReturnDelayMs);
    // Compare-and-set: switches only if the current view is still 'expected'.
    static bool trySetView(DisplayView expected, DisplayView view);
    // A deliberate choice (button, MQTT display/mode): any view but spectrum turns
    // spectrum's automatic entry off, spectrum turns it back on; that choice is
    // persisted. Temp/CO2 are 5 s peeks, so the boot view is always the clock.
    static void selectView(DisplayView view);

    static void nextView();
    // Scrolls in whole passes: as many as come closest to durationMs, at least one
    // and at most MSG_MAX_SCROLL_PASSES. An empty text is ignored.
    static void setCustomMessage(const char* msg,
                                  unsigned long durationMs = MQTT_MSG_DURATION_MS,
                                  uint32_t colorRGB = 0xFF0000);
    // 'generation' changes with every new (or replayed) message: the renderer
    // restarts its scroll when it does.
    static void getCustomMessage(uint16_t* outBuffer, int& outLength, uint32_t& generation);
    static uint32_t getCustomMsgColorRGB();
    // Counts a finished pass, unless a newer message has replaced 'generation' since.
    static void completeScrollPass(uint32_t generation);
    // One pass: the text enters at the right edge and leaves at the left, 1 px per step.
    static unsigned long scrollPassMs(int msgLen) {
        return (unsigned long)(MATRIX_WIDTH + 1 + msgLen * FONT_4X14_CELL_WIDTH) * MSG_SCROLL_STEP_MS;
    }

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

    // Shows a whole icon (MATRIX_TOTAL_PIXELS panel colors, row-major): copied in and
    // switched to in one step, so no frame mixes it with the previous icon.
    static void showIcon(const uint16_t* pixels, unsigned long durationMs = ICON_DISPLAY_DURATION_MS);
    static void getIconBuffer(uint16_t* outBuffer);

    // MQTT beeper command. The display task owns the beeper pin and sounds it (see
    // displayTask); requests made while it is busy merge into one.
    static void requestBeep();
    static bool takeBeepRequest();

private:
    MatrixPanel_I2S_DMA* _matrix = nullptr;
    String _displayText = "";

    static bool loadPersistedSpectrumOverride();
    static void persistSpectrumOverride(bool suppressed);

    static uint32_t loadPersistedColor();
    static void persistColor(uint32_t rgb888);

    // These expect _displayMux to be held.
    static void resolveExpiredViewLocked();
    static bool applyViewLocked(DisplayView view, unsigned long autoReturnDelayMs);
    static void startViewTimerLocked(unsigned long durationMs);

    static DisplayView _currentView;
    static uint16_t _customMsgBuffer[MQTT_MSG_MAX_LEN];
    static int _customMsgLength;
    static uint32_t _customMsgGeneration;
    static uint8_t _customMsgPasses;
    static unsigned long _customMsgDurationMs;  // backstop: all passes plus MSG_EXPIRY_MARGIN_MS
    static unsigned long _iconDurationMs;
    static int _scrollCount;
    // The current view's timer, as start + length so it stays right across the
    // 49.7-day millis() wrap.
    static unsigned long _viewStartMs;
    static unsigned long _viewDurationMs;
    static bool _beepRequested;
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