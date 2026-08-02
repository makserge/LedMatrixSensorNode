#include "screen_renderers.h"

void renderOtaProgressView(MatrixPanel_I2S_DMA* matrixPtr, uint8_t percent, uint16_t color) {
    if (matrixPtr == nullptr) return;

    char buf[8];
    snprintf(buf, sizeof(buf), "%u", percent);
    uint16_t textBuf[8];
    int len = strlen(buf);
    for (int i = 0; i < len; i++) textBuf[i] = (uint16_t)buf[i];

    matrixPtr->drawText4x14(textBuf, len, 4, 0, color);

    int filledCols = (percent * 24) / 100;
    for (int x = 0; x < 24; x++) {
        matrixPtr->drawPixel(x, 15, x < filledCols ? color : 0x0000);
    }
}