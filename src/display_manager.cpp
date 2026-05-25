#include "display_manager.h"
#include "screen_renderers.h"
#include "co2_sensor.h"
#include "time.h"

extern Co2Sensor co2Sensor;

DisplayManager displayManager;

DisplayView DisplayManager::_currentView = VIEW_TIME;
uint16_t DisplayManager::_customMsgBuffer[MQTT_MSG_MAX_LEN] = {0};
int DisplayManager::_customMsgLength = 0;
int DisplayManager::_scrollCount = 0;
unsigned long DisplayManager::_viewExpireTime = 0;
unsigned long DisplayManager::_co2AlarmExpireTime = 0;
bool DisplayManager::_displayOff = false;
portMUX_TYPE DisplayManager::_displayMux = portMUX_INITIALIZER_UNLOCKED;

void DisplayManager::begin() {
    HUB75_I2S_CFG::i2s_pins matrix_pins = {
        R1_PIN, G1_PIN, B1_PIN, -1, -1, -1,
        A_PIN, B_PIN, C_PIN, -1, -1,        
        LAT_PIN, OE_PIN, CLK_PIN            
    };

    HUB75_I2S_CFG mxConfig(24, 16, 1, matrix_pins, L_EN_PIN, M_EN_PIN, R_EN_PIN);

    _matrix = new MatrixPanel_I2S_DMA(mxConfig);
    _matrix->begin(); 
    _matrix->setBrightness8(DISPLAY_BRIGHTNESS);
    _matrix->clearScreen();
    
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
    }
}

void DisplayManager::drawPixel2D(int x, int y, uint16_t color) {
    if (_matrix != nullptr && !_displayOff) {
        _matrix->drawPixel(x, y, color);
    }
}

DisplayView DisplayManager::getCurrentView() {
    portENTER_CRITICAL(&_displayMux);
    if (_currentView != VIEW_CUSTOM_MSG && millis() < _co2AlarmExpireTime) {
        _currentView = VIEW_CO2;
    } else if ((_currentView == VIEW_CUSTOM_MSG && _scrollCount >= 3) ||
               ((_currentView == VIEW_TEMP_DATA || _currentView == VIEW_CO2) && millis() > _viewExpireTime && millis() >= _co2AlarmExpireTime)) {
        _currentView = VIEW_TIME;
    }
    DisplayView view = _currentView;
    portEXIT_CRITICAL(&_displayMux);
    return view;
}

void DisplayManager::setView(DisplayView view) {
    portENTER_CRITICAL(&_displayMux);
    _currentView = view;
    if (view == VIEW_CUSTOM_MSG) {
        _scrollCount = 0;
    }
    if (view == VIEW_TEMP_DATA || view == VIEW_CO2) {
        _viewExpireTime = millis() + DISPLAY_AUTO_RETURN_DELAY_MS;
    }
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::nextView() {
    portENTER_CRITICAL(&_displayMux);
    _co2AlarmExpireTime = 0; 
    if (_currentView == VIEW_TIME) {
        _currentView = VIEW_TEMP_DATA;
        _viewExpireTime = millis() + DISPLAY_AUTO_RETURN_DELAY_MS;
    } else if (_currentView == VIEW_TEMP_DATA) {
        _currentView = VIEW_CO2;
        _viewExpireTime = millis() + DISPLAY_AUTO_RETURN_DELAY_MS;
    } else {
        _currentView = VIEW_TIME;
    }
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::setCustomMessage(const char* msg) {
    portENTER_CRITICAL(&_displayMux);
    _customMsgLength = 0;
    while (msg[_customMsgLength] != '\0' && _customMsgLength < (MQTT_MSG_MAX_LEN - 1)) {
        _customMsgBuffer[_customMsgLength] = static_cast<uint16_t>(msg[_customMsgLength]);
        _customMsgLength++;
    }
    _currentView = VIEW_CUSTOM_MSG;
    _scrollCount = 0;
    _viewExpireTime = millis() + MQTT_MSG_DURATION_MS;
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

void DisplayManager::triggerCo2Alert() {
    portENTER_CRITICAL(&_displayMux);
    if (millis() >= _co2AlarmExpireTime) {
        _co2AlarmExpireTime = millis() + CO2_ALARM_DISPLAY_DURATION_MS;
    }
    portEXIT_CRITICAL(&_displayMux);
}

void displayTask(void* pvParameters) {
    displayManager.begin();
    
    ClockMessage clockMsg = {{'0', '0', ':', '0', '0', 0}, 0};
    uint8_t currentBars[24] = {0};
    uint8_t currentPeaks[24] = {0};
    
    int scrollX = 24;
    unsigned long lastScrollTime = 0;
    DisplayView lastCheckedView = VIEW_TIME;

    unsigned long lastCo2AlarmTime = 0;

    uint16_t colorWhite = MatrixPanel_I2S_DMA::color565(255, 255, 255);
    uint16_t colorGreen = MatrixPanel_I2S_DMA::color565(0, 255, 0);
    uint16_t colorRed   = MatrixPanel_I2S_DMA::color565(255, 0, 0);

    for (;;) {
        unsigned long nowMs = millis();

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
        if (currentCo2 >= CO2_WARNING_THRESHOLD) {
            DisplayManager::triggerCo2Alert();
            if (nowMs - lastCo2AlarmTime >= CO2_ALARM_INTERVAL_MS) {
                struct tm timeinfo;
                bool allowBeep = true;
                
                if (getLocalTime(&timeinfo)) {
                    int currentHour = timeinfo.tm_hour;
                    if (currentHour < CO2_ALARM_START_HOUR || currentHour >= CO2_ALARM_END_HOUR) {
                        allowBeep = false; 
                    }
                }
                
                if (allowBeep) {
                    lastCo2AlarmTime = nowMs;
                    
                    digitalWrite(BEEPER_PIN, HIGH);
                    delay(CO2_ALARM_BEEP_ON_MS);
                    digitalWrite(BEEPER_PIN, LOW);
                    
                    delay(CO2_ALARM_BEEP_OFF_MS);
                    
                    digitalWrite(BEEPER_PIN, HIGH);
                    delay(CO2_ALARM_BEEP_ON_MS);
                    digitalWrite(BEEPER_PIN, LOW);
                    
                    delay(2000);
                }
            }
        }

        MatrixPanel_I2S_DMA* matrixPtr = displayManager.getMatrixPtr();
        if (matrixPtr != nullptr) {
            matrixPtr->clearScreen();
        }

        if (DisplayManager::isDisplayOff()) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        
        DisplayView activeView = DisplayManager::getCurrentView();
        if (activeView == VIEW_CUSTOM_MSG && lastCheckedView != VIEW_CUSTOM_MSG) {
            scrollX = 24;
            DisplayManager::resetScrollCount();
        }
        lastCheckedView = activeView;

        switch (activeView) {
            case VIEW_TIME:
                renderClockView(matrixPtr, clockMsg, colorWhite);
                break;
            case VIEW_TEMP_DATA:
                renderTemperatureView(matrixPtr, colorGreen);
                break;
            case VIEW_CO2:
                renderCo2View(matrixPtr, currentCo2, colorGreen, colorRed);
                break;
            case VIEW_SPECTRUM:
                renderSpectrumBarsView(currentBars, currentPeaks, colorGreen, colorRed);
                break;
            case VIEW_CUSTOM_MSG:
                renderScrollingAlertView(matrixPtr, scrollX, lastScrollTime, colorRed);
                break;
        }

        vTaskDelay(pdMS_TO_TICKS(33));
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
