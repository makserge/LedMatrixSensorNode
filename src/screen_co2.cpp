#include "screen_renderers.h"
#include "constants.h"

void renderCo2View(MatrixPanel_I2S_DMA* matrixPtr, uint16_t currentCo2, uint16_t normalColor, uint16_t alertColor) {
    char co2CharBuf[16];
    snprintf(co2CharBuf, sizeof(co2CharBuf), "%u", currentCo2);
    uint16_t co2MatrixBuf[16];
    int len = strlen(co2CharBuf);
    for(int i = 0; i < len; i++) co2MatrixBuf[i] = (uint16_t)co2CharBuf[i];
    if (matrixPtr != nullptr) {
        uint16_t targetColor = (currentCo2 >= CO2_WARNING_THRESHOLD) ? alertColor : normalColor;
        matrixPtr->drawText4x14(co2MatrixBuf, len, 0, 1, targetColor);
    }
}
