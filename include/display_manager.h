#ifndef DISPLAY_MANAGER_H
#define DISPLAY_MANAGER_H

#include <Arduino.h>
#include "constants.h"
#include "led_matrix.h"
#include "ntp_clock.h"

class DisplayManager {
public:
    void begin();
    void updateText(const String& newText);
    void drawPixel2D(int x, int y, uint16_t color);
    MatrixPanel_I2S_DMA* getMatrixPtr() const { return _matrix; }

    static DisplayView getCurrentView();
    static void setView(DisplayView view);
    static void nextView();
    static void setCustomMessage(const char* msg);
    static void getCustomMessage(uint16_t* outBuffer, int& outLength);
    
    static int getScrollCount();
    static void incrementScrollCount();
    static void resetScrollCount();

    static bool isDisplayOff();
    static void toggleDisplayPower();
    
    static void triggerCo2Alert();

private:
    MatrixPanel_I2S_DMA* _matrix = nullptr;
    String _displayText = "";

    static DisplayView _currentView;
    static uint16_t _customMsgBuffer[MQTT_MSG_MAX_LEN];
    static int _customMsgLength;
    static int _scrollCount;
    static unsigned long _viewExpireTime;
    static unsigned long _co2AlarmExpireTime;
    static bool _displayOff;
    static portMUX_TYPE _displayMux;
};

extern DisplayManager displayManager;

void initDisplayTask();

#endif
