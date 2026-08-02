#include "screen_renderers.h"

void renderClockView(MatrixPanel_I2S_DMA* matrixPtr, const ClockMessage& clockMsg, uint16_t color) {
    if (matrixPtr == nullptr) return;

    if (!clockMsg.synced) {
        static uint16_t syncText[] = {'S', 'Y', 'N', 'C'};
        matrixPtr->drawText4x14(syncText, 4, 4, 1, color);
        return;
    }

    int stringLen = (clockMsg.startX == 2) ? 4 : 5;
    matrixPtr->drawText4x14(clockMsg.text, stringLen, clockMsg.startX, 1, color);
}
