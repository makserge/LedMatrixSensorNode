#ifndef DISPLAY_MANAGER_H
#define DISPLAY_MANAGER_H

#include <Arduino.h>
#include "led_matrix.h"
#include "constants.h"
#include "pins.h"

class DisplayManager {
public:
    void begin();
    void updateText(const String& newText);
    void drawPixel2D(int x, int y, uint16_t color);

private:
    MatrixPanel_I2S_DMA* _matrix = nullptr;
    String _displayText = "";
};

extern DisplayManager displayManager;

void initDisplayTask();

#endif
