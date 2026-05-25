#include "screen_renderers.h"

void renderClockView(MatrixPanel_I2S_DMA* matrixPtr, const ClockMessage& clockMsg, uint16_t color) {
    if (matrixPtr != nullptr) {
        int stringLen = (clockMsg.startX == 2) ? 4 : 5;
        matrixPtr->drawText4x14(clockMsg.text, stringLen, clockMsg.startX, 1, color);
    }
}
