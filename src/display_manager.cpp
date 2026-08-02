#include "display_manager.h"

extern Co2Sensor co2Sensor;

DisplayManager displayManager;

DisplayView DisplayManager::_currentView = VIEW_TIME;
uint16_t DisplayManager::_customMsgBuffer[MQTT_MSG_MAX_LEN] = {0};
int DisplayManager::_customMsgLength = 0;
int DisplayManager::_scrollCount = 0;
unsigned long DisplayManager::_viewExpireTime = 0;
bool DisplayManager::_displayOff = false;
uint32_t DisplayManager::_clockColorRGB = 0xFFFFFF;
uint32_t DisplayManager::_customMsgColorRGB = 0xFF0000;
bool DisplayManager::_spectrumAutoEntrySuppressed = false;
volatile bool DisplayManager::_otaActive = false;
volatile uint8_t DisplayManager::_otaProgress = 0;
volatile unsigned long DisplayManager::_otaActiveSinceMs = 0;
uint16_t DisplayManager::_iconBuffer[MATRIX_TOTAL_PIXELS] = {0};
portMUX_TYPE DisplayManager::_displayMux = portMUX_INITIALIZER_UNLOCKED;
DisplayView DisplayManager::_returnToView = VIEW_TIME;

static const char* PREFS_NAMESPACE = "display";
static const char* PREFS_VIEW_KEY = "view";
static const char* PREFS_COLOR_KEY = "color";

DisplayView DisplayManager::loadPersistedView() {
    Preferences prefs;
    prefs.begin(PREFS_NAMESPACE, true); // read-only
    uint8_t stored = prefs.getUChar(PREFS_VIEW_KEY, (uint8_t)VIEW_TIME);
    prefs.end();
    if (stored > (uint8_t)VIEW_CUSTOM_ICON) return VIEW_TIME; // guard against corrupt/out-of-range data
    return (DisplayView)stored;
}

void DisplayManager::persistView(DisplayView view) {
    Preferences prefs;
    prefs.begin(PREFS_NAMESPACE, false); // read-write
    prefs.putUChar(PREFS_VIEW_KEY, (uint8_t)view);
    prefs.end();
}

uint32_t DisplayManager::loadPersistedColor() {
    Preferences prefs;
    prefs.begin(PREFS_NAMESPACE, true); // read-only
    uint32_t stored = prefs.getUInt(PREFS_COLOR_KEY, 0xFFFFFF);
    prefs.end();
    return stored & 0xFFFFFF; // guard against corrupt/out-of-range data
}

void DisplayManager::persistColor(uint32_t rgb888) {
    Preferences prefs;
    prefs.begin(PREFS_NAMESPACE, false); // read-write
    prefs.putUInt(PREFS_COLOR_KEY, rgb888 & 0xFFFFFF);
    prefs.end();
}

void DisplayManager::begin() {
    _currentView = loadPersistedView();
    _clockColorRGB = loadPersistedColor();

    HUB75_I2S_CFG::i2s_pins matrix_pins = {
        R1_PIN, G1_PIN, B1_PIN, -1, -1, -1,
        A_PIN, B_PIN, C_PIN, -1, -1,        
        LAT_PIN, OE_PIN, CLK_PIN            
    };

    HUB75_I2S_CFG mxConfig(MATRIX_WIDTH, MATRIX_HEIGHT, 1, matrix_pins, L_EN_PIN, M_EN_PIN, R_EN_PIN);

    _matrix = new MatrixPanel_I2S_DMA(mxConfig);
    _matrix->begin(); 
    _matrix->setBrightness(DISPLAY_BRIGHTNESS);
    _matrix->clearScreen();
    _matrix->swapBuffers();
    
    pinMode(MODE_BUTTON_PIN, INPUT_PULLUP);
    pinMode(SWITCH_OFF_LED_PIN, INPUT_PULLUP);
    pinMode(BEEPER_PIN, OUTPUT);
    digitalWrite(BEEPER_PIN, LOW);

    updateText("INIT");
}

void DisplayManager::updateText(const String& newText) {
    if (_displayText == newText) return;
    _displayText = newText;
    
    if (_matrix != nullptr) {
        _matrix->clearScreen();
        uint16_t textLen = _displayText.length();
        if (textLen > 0) {
            uint16_t* textBuffer = new uint16_t[textLen];
            for (uint16_t i = 0; i < textLen; i++) {
                textBuffer[i] = (uint16_t)_displayText[i];
            }
            uint16_t greenColor = _matrix->color565(0, 255, 0);
            _matrix->drawText4x14(textBuffer, textLen, 0, 1, greenColor);
            delete[] textBuffer;
        }
        _matrix->swapBuffers();
    }
}

void DisplayManager::drawPixel2D(int x, int y, uint16_t color) {
    if (_matrix != nullptr && !_displayOff) {
        _matrix->drawPixel(x, y, color);
    }
}

DisplayView DisplayManager::getCurrentView() {
    portENTER_CRITICAL(&_displayMux);
    bool expired = millis() > _viewExpireTime;
    if (_currentView == VIEW_CUSTOM_MSG && (_scrollCount >= 3 || expired)) {
        _currentView = _returnToView;
    } else if (_currentView == VIEW_CUSTOM_ICON && expired) {
        _currentView = _returnToView;
    } else if ((_currentView == VIEW_TEMP_DATA || _currentView == VIEW_CO2) && expired) {
        _currentView = VIEW_TIME;
    }
    DisplayView view = _currentView;
    portEXIT_CRITICAL(&_displayMux);
    return view;
}

void DisplayManager::setView(DisplayView view) {
    setView(view, DISPLAY_AUTO_RETURN_DELAY_MS);
}

void DisplayManager::setView(DisplayView view, unsigned long autoReturnDelayMs) {
    portENTER_CRITICAL(&_displayMux);

    bool blockedByActiveProtectedView =
        (_currentView == VIEW_CUSTOM_MSG || _currentView == VIEW_CUSTOM_ICON) &&
        (view == VIEW_SPECTRUM) &&
        (millis() <= _viewExpireTime);

    bool blockedByManualOverride = (view == VIEW_SPECTRUM) && _spectrumAutoEntrySuppressed;

    if (!blockedByActiveProtectedView && !blockedByManualOverride) {
        _currentView = view;
        if (view == VIEW_CUSTOM_MSG) {
            _scrollCount = 0;
        }
        if (view == VIEW_TEMP_DATA || view == VIEW_CO2) {
            _viewExpireTime = millis() + autoReturnDelayMs;
        }
    }
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::selectView(DisplayView view) {
    portENTER_CRITICAL(&_displayMux);
    _spectrumAutoEntrySuppressed = (view != VIEW_SPECTRUM);
    portEXIT_CRITICAL(&_displayMux);

    setView(view);
    persistView(view);
}

void DisplayManager::nextView() {
    DisplayView next;
    portENTER_CRITICAL(&_displayMux);
    if (_currentView == VIEW_TIME) {
        next = VIEW_TEMP_DATA;
    } else if (_currentView == VIEW_TEMP_DATA) {
        next = VIEW_CO2;
    } else if (_currentView == VIEW_CO2) {
        next = VIEW_SPECTRUM;
    } else {
        next = VIEW_TIME;
    }
    portEXIT_CRITICAL(&_displayMux);
    selectView(next);
}

void DisplayManager::setCustomMessage(const char* msg, unsigned long durationMs, uint32_t colorRGB) {
    portENTER_CRITICAL(&_displayMux);
    if (_currentView != VIEW_CUSTOM_MSG && _currentView != VIEW_CUSTOM_ICON) {
        _returnToView = _currentView;
    }
    _customMsgLength = 0;
    while (msg[_customMsgLength] != '\0' && _customMsgLength < (MQTT_MSG_MAX_LEN - 1)) {
        _customMsgBuffer[_customMsgLength] = static_cast<uint16_t>(msg[_customMsgLength]);
        _customMsgLength++;
    }
    _currentView = VIEW_CUSTOM_MSG;
    _scrollCount = 0;
    _viewExpireTime = millis() + durationMs;
    _customMsgColorRGB = colorRGB;
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::getCustomMessage(uint16_t* outBuffer, int& outLength) {
    portENTER_CRITICAL(&_displayMux);
    outLength = _customMsgLength;
    for(int i = 0; i < _customMsgLength; i++) {
        outBuffer[i] = _customMsgBuffer[i];
    }
    portEXIT_CRITICAL(&_displayMux);
}

uint32_t DisplayManager::getCustomMsgColorRGB() {
    portENTER_CRITICAL(&_displayMux);
    uint32_t c = _customMsgColorRGB;
    portEXIT_CRITICAL(&_displayMux);
    return c;
}

int DisplayManager::getScrollCount() {
    portENTER_CRITICAL(&_displayMux);
    int count = _scrollCount;
    portEXIT_CRITICAL(&_displayMux);
    return count;
}

void DisplayManager::incrementScrollCount() {
    portENTER_CRITICAL(&_displayMux);
    _scrollCount++;
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::resetScrollCount() {
    portENTER_CRITICAL(&_displayMux);
    _scrollCount = 0;
    portEXIT_CRITICAL(&_displayMux);
}

bool DisplayManager::isDisplayOff() {
    portENTER_CRITICAL(&_displayMux);
    bool status = _displayOff;
    portEXIT_CRITICAL(&_displayMux);
    return status;
}

void DisplayManager::toggleDisplayPower() {
    portENTER_CRITICAL(&_displayMux);
    _displayOff = !_displayOff;
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::setDisplayOff(bool off) {
    portENTER_CRITICAL(&_displayMux);
    _displayOff = off;
    portEXIT_CRITICAL(&_displayMux);
}

uint32_t DisplayManager::getClockColorRGB() {
    portENTER_CRITICAL(&_displayMux);
    uint32_t c = _clockColorRGB;
    portEXIT_CRITICAL(&_displayMux);
    return c;
}

void DisplayManager::setClockColor(uint8_t r, uint8_t g, uint8_t b) {
    uint32_t rgb = ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
    portENTER_CRITICAL(&_displayMux);
    _clockColorRGB = rgb;
    portEXIT_CRITICAL(&_displayMux);
    persistColor(rgb); // survives reboot
}

void DisplayManager::setOtaActive(bool active) {
    portENTER_CRITICAL(&_displayMux);
    _otaActive = active;
    if (active) _otaActiveSinceMs = millis();
    portEXIT_CRITICAL(&_displayMux);
}

bool DisplayManager::isOtaActive() {
    portENTER_CRITICAL(&_displayMux);
    bool active = _otaActive;
    portEXIT_CRITICAL(&_displayMux);
    return active;
}

void DisplayManager::setOtaProgress(uint8_t percent) {
    portENTER_CRITICAL(&_displayMux);
    _otaProgress = percent;
    portEXIT_CRITICAL(&_displayMux);
}

uint8_t DisplayManager::getOtaProgress() {
    portENTER_CRITICAL(&_displayMux);
    uint8_t p = _otaProgress;
    portEXIT_CRITICAL(&_displayMux);
    return p;
}

unsigned long DisplayManager::getOtaActiveSinceMs() {
    portENTER_CRITICAL(&_displayMux);
    unsigned long t = _otaActiveSinceMs;
    portEXIT_CRITICAL(&_displayMux);
    return t;
}

bool DisplayManager::isSpectrumAutoEntrySuppressed() {
    portENTER_CRITICAL(&_displayMux);
    bool s = _spectrumAutoEntrySuppressed;
    portEXIT_CRITICAL(&_displayMux);
    return s;
}

void DisplayManager::drawIconPixel(int x, int y, uint8_t r, uint8_t g, uint8_t b) {
    if (x < 0 || x >= MATRIX_WIDTH || y < 0 || y >= MATRIX_HEIGHT) return;

    uint16_t color = 0;
    if (r > 127) color |= 0x0004;
    if (g > 127) color |= 0x0002;
    if (b > 127) color |= 0x0001;

    int index = y * MATRIX_WIDTH + x;
    portENTER_CRITICAL(&_displayMux);
    _iconBuffer[index] = color;
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::showIconView(unsigned long durationMs) {
    portENTER_CRITICAL(&_displayMux);
    if (_currentView != VIEW_CUSTOM_MSG && _currentView != VIEW_CUSTOM_ICON) {
        _returnToView = _currentView;
    }
    portEXIT_CRITICAL(&_displayMux);

    setView(VIEW_CUSTOM_ICON, durationMs);

    portENTER_CRITICAL(&_displayMux);
    _viewExpireTime = millis() + durationMs; // setView() only sets this for TEMP/CO2
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::getIconBuffer(uint16_t* outBuffer) {
    portENTER_CRITICAL(&_displayMux);
    memcpy(outBuffer, _iconBuffer, sizeof(_iconBuffer));
    portEXIT_CRITICAL(&_displayMux);
}

enum Co2BeepState { BEEP_IDLE, BEEP_ON, BEEP_OFF };

void displayTask(void* pvParameters) {
    displayManager.begin();
    
    ClockMessage clockMsg = {{0}, 0, false};
    uint8_t currentBars[24] = {0};
    uint8_t currentPeaks[24] = {0};
    
    int scrollX = 24;
    unsigned long lastScrollTime = 0;
    DisplayView lastCheckedView = VIEW_TIME;
    bool otaActiveLastLoop = false;

    bool co2AlarmModeActive = false;   // true while CO2 is above the low threshold (hysteresis band)
    bool co2PopupShowing = false;      // true while the CO2 popup is currently on screen
    bool co2PopupBeepGated = false;    // true: this popup closes when the beep sequence reaches BEEP_IDLE
                                        // false: this popup closes at the fixed co2PopupEndTime instead
    DisplayView co2PopupPreviousView = VIEW_TIME;
    unsigned long co2PopupEndTime = 0;
    unsigned long nextCo2PopupTime = 0;

    Co2BeepState beepState = BEEP_IDLE;
    uint8_t beepsDone = 0;
    unsigned long beepStateChangeTime = 0;

    uint16_t colorWhite = MatrixPanel_I2S_DMA::color565(255, 255, 255);
    uint16_t colorGreen = MatrixPanel_I2S_DMA::color565(0, 255, 0);
    uint16_t colorRed   = MatrixPanel_I2S_DMA::color565(255, 0, 0);
    uint16_t colorBlue  = MatrixPanel_I2S_DMA::color565(0, 0, 255);

    for (;;) {
        unsigned long nowMs = millis();

        if (DisplayManager::isOtaActive()) {
            if (!otaActiveLastLoop) {
                Serial.println("[OTA] displayTask: entering OTA render mode");
                otaActiveLastLoop = true;
            }

            MatrixPanel_I2S_DMA* matrixPtr = displayManager.getMatrixPtr();
            if (matrixPtr != nullptr) {
                matrixPtr->clearScreen();
                uint16_t otaText[] = {'O', 'T', 'A'};
                matrixPtr->drawText4x14(otaText, 3, 4, 1, colorBlue);
                matrixPtr->swapBuffers();
            }
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        } else if (otaActiveLastLoop) {
            Serial.println("[OTA] displayTask: leaving OTA render mode");
            otaActiveLastLoop = false;
        }

        if (digitalRead(MODE_BUTTON_PIN) == LOW) {
            static unsigned long lastBtnPress = 0;
            if (nowMs - lastBtnPress > 300) {
                lastBtnPress = nowMs;
                DisplayManager::nextView();
            }
        }

        if (digitalRead(SWITCH_OFF_LED_PIN) == LOW) {
            static unsigned long lastPowerBtnPress = 0;
            if (nowMs - lastPowerBtnPress > 300) {
                lastPowerBtnPress = nowMs;
                DisplayManager::toggleDisplayPower();
            }
        }

        if (clockQueue != NULL) {
            xQueueReceive(clockQueue, &clockMsg, 0);
        }

        uint16_t currentCo2 = co2Sensor.getCo2();

        bool co2AboveHigh = currentCo2 >= CO2_WARNING_THRESHOLD;
        bool co2AboveLow  = currentCo2 >= CO2_ALARM_LOW_THRESHOLD;

        if (co2AboveHigh && !co2AlarmModeActive) {
            co2AlarmModeActive = true;
            nextCo2PopupTime = nowMs; // fire the first popup right away
        } else if (!co2AboveLow && co2AlarmModeActive) {
            co2AlarmModeActive = false;
            if (co2PopupShowing) {
                co2PopupShowing = false;
                if (DisplayManager::getCurrentView() == VIEW_CO2) {
                    DisplayManager::setView(co2PopupPreviousView);
                }
                if (beepState != BEEP_IDLE) {
                    digitalWrite(BEEPER_PIN, LOW);
                    beepState = BEEP_IDLE;
                }
            }
        }

        if (co2AlarmModeActive && !co2PopupShowing && nowMs >= nextCo2PopupTime) {
            co2PopupShowing = true;
            co2PopupPreviousView = DisplayManager::getCurrentView();
            DisplayManager::setView(VIEW_CO2, CO2_ALARM_POPUP_SAFETY_MS);

            bool allowBeep = true;
            struct tm timeinfo;
            if (getLocalTime(&timeinfo)) {
                int currentHour = timeinfo.tm_hour;
                if (currentHour < CO2_ALARM_START_HOUR || currentHour >= CO2_ALARM_END_HOUR) {
                    allowBeep = false;
                }
            }

            if (allowBeep) {
                digitalWrite(BEEPER_PIN, HIGH);
                beepState = BEEP_ON;
                beepsDone = 0;
                beepStateChangeTime = nowMs;
                co2PopupBeepGated = true;
            } else {
                beepState = BEEP_IDLE;
                co2PopupBeepGated = false;
                co2PopupEndTime = nowMs + CO2_ALARM_DISPLAY_DURATION_MS;
            }
        }

        if (beepState == BEEP_ON && nowMs - beepStateChangeTime >= CO2_ALARM_BEEP_ON_MS) {
            digitalWrite(BEEPER_PIN, LOW);
            beepsDone++;
            if (beepsDone >= CO2_ALARM_BEEP_COUNT) {
                beepState = BEEP_IDLE;
            } else {
                beepState = BEEP_OFF;
                beepStateChangeTime = nowMs;
            }
        } else if (beepState == BEEP_OFF && nowMs - beepStateChangeTime >= CO2_ALARM_BEEP_OFF_MS) {
            digitalWrite(BEEPER_PIN, HIGH);
            beepState = BEEP_ON;
            beepStateChangeTime = nowMs;
        }

        bool co2PopupShouldClose = co2PopupBeepGated
            ? (beepState == BEEP_IDLE)
            : (nowMs >= co2PopupEndTime);

        if (co2PopupShowing && co2PopupShouldClose) {
            if (DisplayManager::getCurrentView() == VIEW_CO2) {
                DisplayManager::setView(co2PopupPreviousView);
            }
            co2PopupShowing = false;
            nextCo2PopupTime = nowMs + CO2_ALARM_INTERVAL_MS;
        }

        MatrixPanel_I2S_DMA* matrixPtr = displayManager.getMatrixPtr();
        if (matrixPtr != nullptr) {
            matrixPtr->clearScreen();
        }

        if (DisplayManager::isDisplayOff()) {
            if (matrixPtr != nullptr) {
                matrixPtr->swapBuffers();
            }
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        if (WiFi.status() != WL_CONNECTED) {
            if (matrixPtr != nullptr) {
                if (WiFi.getMode() & WIFI_AP) {
                    uint16_t apText[] = {'A', 'P'};
                    matrixPtr->drawText4x14(apText, 2, 8, 1, colorBlue);
                } else {
                    uint16_t wifiText[] = {'W', 'I', 'F', 'I'};
                    matrixPtr->drawText4x14(wifiText, 4, 4, 1, colorBlue);
                }
                matrixPtr->swapBuffers();
            }
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }
        
        DisplayView activeView = DisplayManager::getCurrentView();
        if (activeView == VIEW_CUSTOM_MSG && lastCheckedView != VIEW_CUSTOM_MSG) {
            scrollX = 24;
            DisplayManager::resetScrollCount();
        }
        lastCheckedView = activeView;

        switch (activeView) {
            case VIEW_TIME: {
                uint32_t clockRgb = DisplayManager::getClockColorRGB();
                uint16_t clockColor = MatrixPanel_I2S_DMA::color565(
                    (uint8_t)((clockRgb >> 16) & 0xFF),
                    (uint8_t)((clockRgb >> 8) & 0xFF),
                    (uint8_t)(clockRgb & 0xFF));
                renderClockView(matrixPtr, clockMsg, clockColor);
                break;
            }
            case VIEW_TEMP_DATA:
                renderTemperatureView(matrixPtr, colorGreen);
                break;
            case VIEW_CO2:
                renderCo2View(matrixPtr, currentCo2, colorGreen, colorRed);
                break;
            case VIEW_SPECTRUM:
                renderSpectrumBarsView(currentBars, currentPeaks, colorGreen, colorBlue);
                break;
            case VIEW_CUSTOM_MSG: {
                uint32_t msgRgb = DisplayManager::getCustomMsgColorRGB();
                uint16_t msgColor = MatrixPanel_I2S_DMA::color565(
                    (uint8_t)((msgRgb >> 16) & 0xFF),
                    (uint8_t)((msgRgb >> 8) & 0xFF),
                    (uint8_t)(msgRgb & 0xFF));
                renderScrollingAlertView(matrixPtr, scrollX, lastScrollTime, msgColor);
                break;
            }
            case VIEW_CUSTOM_ICON:
                renderIconView(matrixPtr);
                break;
        }

        if (matrixPtr != nullptr) {
            matrixPtr->swapBuffers();
        }

        vTaskDelay(pdMS_TO_TICKS(activeView == VIEW_SPECTRUM ? 10 : 33));
    }
}

void initDisplayTask() {
    xTaskCreatePinnedToCore(
        displayTask,
        "DisplayTask",
        DISPLAY_TASK_STACK_SIZE,
        NULL,
        DISPLAY_TASK_PRIORITY,
        NULL,
        1
    );
}