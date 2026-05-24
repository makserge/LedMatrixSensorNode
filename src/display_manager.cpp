#include "display_manager.h"

DisplayManager displayManager;

void initDisplayTask() {
    displayManager.begin();
}

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

    updateText("INIT");
}

void DisplayManager::updateText(const String& newText) {
    if (_displayText == newText) return; 
    _displayText = newText;
    
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

void DisplayManager::drawPixel2D(int x, int y, uint16_t color) {
    _matrix->drawPixel(x, y, color);
}
