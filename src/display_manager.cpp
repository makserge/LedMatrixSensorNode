#include "display_manager.h"

extern Co2Sensor co2Sensor;

DisplayManager displayManager;

DisplayView DisplayManager::_currentView = VIEW_TIME;
uint16_t DisplayManager::_customMsgBuffer[MQTT_MSG_MAX_LEN] = {0};
int DisplayManager::_customMsgLength = 0;
uint32_t DisplayManager::_customMsgGeneration = 0;
uint8_t DisplayManager::_customMsgPasses = 1;
unsigned long DisplayManager::_customMsgDurationMs = 0;
unsigned long DisplayManager::_iconDurationMs = ICON_DISPLAY_DURATION_MS;
int DisplayManager::_scrollCount = 0;
unsigned long DisplayManager::_viewStartMs = 0;
unsigned long DisplayManager::_viewDurationMs = 0;
bool DisplayManager::_beepRequested = false;
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
static const char* PREFS_SPECTRUM_OFF_KEY = "spec_off";
static const char* PREFS_COLOR_KEY = "color";

// The manual spectrum override is the one view choice with a lasting effect, so it
// is what survives a reboot. A persisted view never did: temp/CO2 time out at once
// and spectrum without audio falls back to the clock.
bool DisplayManager::loadPersistedSpectrumOverride() {
    Preferences prefs;
    prefs.begin(PREFS_NAMESPACE, true); // read-only
    bool stored = prefs.getBool(PREFS_SPECTRUM_OFF_KEY, false);
    prefs.end();
    return stored;
}

void DisplayManager::persistSpectrumOverride(bool suppressed) {
    Preferences prefs;
    prefs.begin(PREFS_NAMESPACE, false); // read-write
    prefs.putBool(PREFS_SPECTRUM_OFF_KEY, suppressed);
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
    _spectrumAutoEntrySuppressed = loadPersistedSpectrumOverride();
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

void DisplayManager::startViewTimerLocked(unsigned long durationMs) {
    _viewStartMs = millis();
    _viewDurationMs = durationMs;
}

void DisplayManager::resolveExpiredViewLocked() {
    bool expired = millis() - _viewStartMs >= _viewDurationMs;
    bool overlayDone = (_currentView == VIEW_CUSTOM_MSG && (_scrollCount >= _customMsgPasses || expired)) ||
                       (_currentView == VIEW_CUSTOM_ICON && expired);
    if (overlayDone) {
        // A temp/CO2 peek or CO2 popup the overlay interrupted has had its time, and its timer
        // was the overlay's: handing back to it would show a stray peek until that ran out.
        bool transient = (_returnToView == VIEW_TEMP_DATA || _returnToView == VIEW_CO2);
        _currentView = transient ? VIEW_TIME : _returnToView;
    } else if ((_currentView == VIEW_TEMP_DATA || _currentView == VIEW_CO2) && expired) {
        _currentView = VIEW_TIME;
    }
}

bool DisplayManager::applyViewLocked(DisplayView view, unsigned long autoReturnDelayMs) {
    bool blockedByActiveProtectedView =
        (_currentView == VIEW_CUSTOM_MSG || _currentView == VIEW_CUSTOM_ICON) &&
        (view == VIEW_SPECTRUM) &&
        (millis() - _viewStartMs < _viewDurationMs);

    bool blockedByManualOverride = (view == VIEW_SPECTRUM) && _spectrumAutoEntrySuppressed;

    if (blockedByActiveProtectedView || blockedByManualOverride) {
        return false;
    }
    _currentView = view;
    if (view == VIEW_CUSTOM_MSG) {
        // Back to a message the CO2 popup interrupted: replay it from the start.
        _scrollCount = 0;
        _customMsgGeneration++;
        startViewTimerLocked(_customMsgDurationMs);
    } else if (view == VIEW_CUSTOM_ICON) {
        startViewTimerLocked(_iconDurationMs);
    } else if (view == VIEW_TEMP_DATA || view == VIEW_CO2) {
        startViewTimerLocked(autoReturnDelayMs);
    }
    return true;
}

DisplayView DisplayManager::getCurrentView() {
    portENTER_CRITICAL(&_displayMux);
    resolveExpiredViewLocked();
    DisplayView view = _currentView;
    portEXIT_CRITICAL(&_displayMux);
    return view;
}

bool DisplayManager::setView(DisplayView view) {
    return setView(view, DISPLAY_AUTO_RETURN_DELAY_MS);
}

bool DisplayManager::setView(DisplayView view, unsigned long autoReturnDelayMs) {
    portENTER_CRITICAL(&_displayMux);
    bool switched = applyViewLocked(view, autoReturnDelayMs);
    portEXIT_CRITICAL(&_displayMux);
    return switched;
}

bool DisplayManager::trySetView(DisplayView expected, DisplayView view) {
    portENTER_CRITICAL(&_displayMux);
    resolveExpiredViewLocked(); // 'expected' came from getCurrentView(), which resolves the same way
    bool switched = (_currentView == expected) && applyViewLocked(view, DISPLAY_AUTO_RETURN_DELAY_MS);
    portEXIT_CRITICAL(&_displayMux);
    return switched;
}

void DisplayManager::selectView(DisplayView view) {
    bool suppress = (view != VIEW_SPECTRUM);
    portENTER_CRITICAL(&_displayMux);
    bool changed = (_spectrumAutoEntrySuppressed != suppress);
    _spectrumAutoEntrySuppressed = suppress;
    portEXIT_CRITICAL(&_displayMux);

    setView(view);
    if (changed) persistSpectrumOverride(suppress); // NVS commit only when the choice changes
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
    int len = 0;
    while (msg[len] != '\0' && len < (int)(MQTT_MSG_MAX_LEN - 1)) len++;
    if (len == 0) return; // nothing to show: keep the current view instead of a blank panel

    // Whole passes only, so a long text is never cut off before its end has been on screen.
    unsigned long passMs = scrollPassMs(len);
    unsigned long passes = (durationMs + passMs / 2) / passMs;
    passes = constrain(passes, 1UL, (unsigned long)MSG_MAX_SCROLL_PASSES);

    portENTER_CRITICAL(&_displayMux);
    resolveExpiredViewLocked(); // an expired view must not become the return target
    if (_currentView != VIEW_CUSTOM_MSG && _currentView != VIEW_CUSTOM_ICON) {
        _returnToView = _currentView;
    }
    for (int i = 0; i < len; i++) {
        _customMsgBuffer[i] = static_cast<uint8_t>(msg[i]);
    }
    _customMsgLength = len;
    _customMsgPasses = (uint8_t)passes;
    // Normally the pass count ends the message; this only ends one that can't scroll
    // (panel off, WiFi down).
    _customMsgDurationMs = passes * passMs + MSG_EXPIRY_MARGIN_MS;
    _customMsgGeneration++;
    _currentView = VIEW_CUSTOM_MSG;
    _scrollCount = 0;
    startViewTimerLocked(_customMsgDurationMs);
    _customMsgColorRGB = colorRGB;
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::getCustomMessage(uint16_t* outBuffer, int& outLength, uint32_t& generation) {
    portENTER_CRITICAL(&_displayMux);
    outLength = _customMsgLength;
    for(int i = 0; i < _customMsgLength; i++) {
        outBuffer[i] = _customMsgBuffer[i];
    }
    generation = _customMsgGeneration;
    portEXIT_CRITICAL(&_displayMux);
}

uint32_t DisplayManager::getCustomMsgColorRGB() {
    portENTER_CRITICAL(&_displayMux);
    uint32_t c = _customMsgColorRGB;
    portEXIT_CRITICAL(&_displayMux);
    return c;
}

void DisplayManager::completeScrollPass(uint32_t generation) {
    portENTER_CRITICAL(&_displayMux);
    if (generation == _customMsgGeneration) _scrollCount++;
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

void DisplayManager::showIcon(const uint16_t* pixels, unsigned long durationMs) {
    portENTER_CRITICAL(&_displayMux);
    memcpy(_iconBuffer, pixels, sizeof(_iconBuffer));
    resolveExpiredViewLocked(); // an expired view must not become the return target
    if (_currentView != VIEW_CUSTOM_MSG && _currentView != VIEW_CUSTOM_ICON) {
        _returnToView = _currentView;
    }
    _currentView = VIEW_CUSTOM_ICON;
    _iconDurationMs = durationMs;
    startViewTimerLocked(durationMs);
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::getIconBuffer(uint16_t* outBuffer) {
    portENTER_CRITICAL(&_displayMux);
    memcpy(outBuffer, _iconBuffer, sizeof(_iconBuffer));
    portEXIT_CRITICAL(&_displayMux);
}

void DisplayManager::requestBeep() {
    portENTER_CRITICAL(&_displayMux);
    _beepRequested = true;
    portEXIT_CRITICAL(&_displayMux);
}

bool DisplayManager::takeBeepRequest() {
    portENTER_CRITICAL(&_displayMux);
    bool requested = _beepRequested;
    _beepRequested = false;
    portEXIT_CRITICAL(&_displayMux);
    return requested;
}

enum BeepState { BEEP_IDLE, BEEP_ON, BEEP_OFF };

// Fires once per press, on the first poll that sees it pressed (a 33-50 ms poll can't
// afford to wait for a second sample: short taps would be missed), and is re-armed only
// once it has read released for BUTTON_DEBOUNCE_MS. Holding a button never repeats,
// and bounce on press or release can't fire it twice.
struct DebouncedButton {
    uint8_t pin;
    bool pressed;           // fired, not yet re-armed
    bool releasing;         // pressed, and read released since releasedSinceMs
    unsigned long releasedSinceMs;

    // After pinMode(): a button held during boot fires only after it has been let go.
    explicit DebouncedButton(uint8_t p)
        : pin(p), pressed(digitalRead(p) == LOW), releasing(false), releasedSinceMs(0) {}

    bool poll(unsigned long nowMs) {
        bool down = digitalRead(pin) == LOW;
        if (!pressed) {
            if (!down) return false;
            pressed = true;
            releasing = false;
            return true;
        }
        if (down) {
            releasing = false;
        } else if (!releasing) {
            releasing = true;
            releasedSinceMs = nowMs;
        } else if (nowMs - releasedSinceMs >= BUTTON_DEBOUNCE_MS) {
            pressed = false;
        }
        return false;
    }
};

void displayTask(void* pvParameters) {
    displayManager.begin();
    
    ClockMessage clockMsg = {{0}, 0, false};
    uint8_t currentBars[24] = {0};
    uint8_t currentPeaks[24] = {0};
    
    MessageScroll msgScroll = {MATRIX_WIDTH, 0, 0};
    bool otaActiveLastLoop = false;

    DebouncedButton modeButton(MODE_BUTTON_PIN);
    DebouncedButton powerButton(SWITCH_OFF_LED_PIN);

    // All timers below are start + length, compared as elapsed time (now - start),
    // so they stay right across the 49.7-day millis() wrap.
    bool co2AlarmModeActive = false;   // true while CO2 is above the low threshold (hysteresis band)
    bool co2AlarmLapsedOnDropout = false; // the alarm ended only because the CO2 value went unknown
    bool co2PopupShowing = false;      // true while the CO2 popup is currently on screen
    bool co2PopupBeepGated = false;    // true: this popup closes when the beep sequence reaches BEEP_IDLE
                                        // false: it closes CO2_ALARM_DISPLAY_DURATION_MS after co2PopupShownMs
    DisplayView co2PopupPreviousView = VIEW_TIME;
    unsigned long co2PopupShownMs = 0;
    unsigned long co2PopupWaitStartMs = 0; // the next popup is due co2PopupWaitMs after this
    unsigned long co2PopupWaitMs = 0;

    // The beeper has one owner, this task: the CO2 alarm sequence and the MQTT beep
    // command (DisplayManager::requestBeep) both run through this state machine.
    BeepState beepState = BEEP_IDLE;
    uint8_t beepsDone = 0;
    uint8_t beepCount = 0;             // beeps in the running sequence
    unsigned long beepOnMs = 0;        // length of each of them
    unsigned long beepStateChangeTime = 0;
    auto startBeeps = [&](uint8_t count, unsigned long onMs, unsigned long nowMs) {
        digitalWrite(BEEPER_PIN, HIGH);
        beepState = BEEP_ON;
        beepsDone = 0;
        beepCount = count;
        beepOnMs = onMs;
        beepStateChangeTime = nowMs;
    };

    uint16_t colorWhite = MatrixPanel_I2S_DMA::color565(255, 255, 255);
    uint16_t colorGreen = MatrixPanel_I2S_DMA::color565(0, 255, 0);
    uint16_t colorRed   = MatrixPanel_I2S_DMA::color565(255, 0, 0);
    uint16_t colorBlue  = MatrixPanel_I2S_DMA::color565(0, 0, 255);

    for (;;) {
        if (DisplayManager::isOtaActive()) {
            if (!otaActiveLastLoop) {
                Serial.println("[OTA] displayTask: entering OTA render mode");
                otaActiveLastLoop = true;
                // The beep state machine doesn't run during the upload: a beep that is
                // on now would sound until the reboot. Pending beep requests are dropped.
                digitalWrite(BEEPER_PIN, LOW);
                beepState = BEEP_IDLE;
                DisplayManager::takeBeepRequest();
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

        unsigned long buttonMs = millis();
        if (modeButton.poll(buttonMs)) {
            DisplayManager::nextView();
        }
        if (powerButton.poll(buttonMs)) {
            DisplayManager::toggleDisplayPower();
        }

        // After the buttons: a view selection may have waited on an NVS commit.
        unsigned long nowMs = millis();

        if (clockQueue != NULL) {
            xQueueReceive(clockQueue, &clockMsg, 0);
        }

        // Unknown CO2 (no fresh sample: sensor missing, failing or still starting) never
        // counts as high, so a frozen last value can't keep the alarm and its beeps going.
        bool co2Known = co2Sensor.hasFreshData();
        uint16_t currentCo2 = co2Known ? co2Sensor.getCo2() : 0;

        bool co2AboveHigh = co2Known && currentCo2 >= CO2_WARNING_THRESHOLD;
        bool co2AboveLow  = co2Known && currentCo2 >= CO2_ALARM_LOW_THRESHOLD;

        // An alarm that only lapsed while the value was unknown resumes at any reading still
        // in the band: there was no reading below CO2_ALARM_LOW_THRESHOLD to end it.
        if ((co2AboveHigh || (co2AlarmLapsedOnDropout && co2AboveLow)) && !co2AlarmModeActive) {
            co2AlarmModeActive = true;
            // Fire the first popup right away, unless the alarm only lapsed while the value
            // was unknown: then keep its cadence, so a flaky sensor can't re-beep on every recovery.
            if (!co2AlarmLapsedOnDropout) co2PopupWaitMs = 0;
            co2AlarmLapsedOnDropout = false;
        } else if (!co2AboveLow && co2AlarmModeActive) {
            co2AlarmModeActive = false;
            co2AlarmLapsedOnDropout = !co2Known;
            if (co2PopupShowing) {
                co2PopupShowing = false;
                co2PopupWaitStartMs = nowMs; // as if it had closed normally
                co2PopupWaitMs = CO2_ALARM_INTERVAL_MS;
                if (DisplayManager::getCurrentView() == VIEW_CO2) {
                    DisplayManager::setView(co2PopupPreviousView);
                }
                // A beep-gated popup's beeps are the alarm's own; with a silent popup any
                // beep is an MQTT one, which ends by itself.
                if (co2PopupBeepGated && beepState != BEEP_IDLE) {
                    digitalWrite(BEEPER_PIN, LOW);
                    beepState = BEEP_IDLE;
                    beepStateChangeTime = nowMs;
                }
            }
        } else if (co2Known && !co2AboveLow) {
            co2AlarmLapsedOnDropout = false; // a real reading below the band ends the episode
        }

        if (co2AlarmModeActive && !co2PopupShowing && nowMs - co2PopupWaitStartMs >= co2PopupWaitMs) {
            co2PopupShowing = true;
            co2PopupShownMs = nowMs;
            co2PopupPreviousView = DisplayManager::getCurrentView();
            DisplayManager::setView(VIEW_CO2, CO2_ALARM_POPUP_SAFETY_MS);

            // Quiet hours fail closed: beep only while the local time is known to be in
            // the window. Without an NTP fix since boot the popup still shows, silently.
            struct tm timeinfo;
            bool allowBeep = readLocalTime(&timeinfo) &&
                             timeinfo.tm_hour >= CO2_ALARM_START_HOUR &&
                             timeinfo.tm_hour < CO2_ALARM_END_HOUR;

            if (allowBeep) {
                startBeeps(CO2_ALARM_BEEP_COUNT, CO2_ALARM_BEEP_ON_MS, nowMs); // takes over an MQTT beep
                co2PopupBeepGated = true;
            } else {
                co2PopupBeepGated = false; // a running MQTT beep ends by itself
            }
        }

        if (beepState == BEEP_ON && nowMs - beepStateChangeTime >= beepOnMs) {
            digitalWrite(BEEPER_PIN, LOW);
            beepsDone++;
            beepStateChangeTime = nowMs;
            beepState = (beepsDone >= beepCount) ? BEEP_IDLE : BEEP_OFF;
        } else if (beepState == BEEP_OFF && nowMs - beepStateChangeTime >= CO2_ALARM_BEEP_OFF_MS) {
            digitalWrite(BEEPER_PIN, HIGH);
            beepState = BEEP_ON;
            beepStateChangeTime = nowMs;
        }

        bool co2PopupShouldClose = co2PopupBeepGated
            ? (beepState == BEEP_IDLE)
            : (nowMs - co2PopupShownMs >= CO2_ALARM_DISPLAY_DURATION_MS);

        if (co2PopupShowing && co2PopupShouldClose) {
            if (DisplayManager::getCurrentView() == VIEW_CO2) {
                DisplayManager::setView(co2PopupPreviousView);
            }
            co2PopupShowing = false;
            co2PopupWaitStartMs = nowMs;
            co2PopupWaitMs = CO2_ALARM_INTERVAL_MS;
        }

        // MQTT beep: after an alarm sequence (never cutting into it) and at least
        // BEEP_DURATION_MS after the last beep ended, so back-to-back requests stay
        // separate beeps; requests made meanwhile wait here as one.
        if (beepState == BEEP_IDLE && nowMs - beepStateChangeTime >= BEEP_DURATION_MS &&
            DisplayManager::takeBeepRequest()) {
            startBeeps(1, BEEP_DURATION_MS, nowMs);
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
            vTaskDelay(pdMS_TO_TICKS(50)); // as with the panel off: a short button tap can't fall between polls
            continue;
        }

        DisplayView activeView = DisplayManager::getCurrentView();

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
                renderCo2View(matrixPtr, co2Known, currentCo2, co2AlarmModeActive, colorGreen, colorRed);
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
                renderScrollingAlertView(matrixPtr, msgScroll, msgColor);
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