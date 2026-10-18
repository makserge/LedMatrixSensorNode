#include "screen_renderers.h"
#include "constants.h"

void renderCo2View(MatrixPanel_I2S_DMA* matrixPtr, bool co2Known, uint16_t currentCo2, bool alarmActive,
                   uint16_t normalColor, uint16_t alertColor) {
    char co2CharBuf[16];
    if (co2Known) {
        snprintf(co2CharBuf, sizeof(co2CharBuf), "%u", currentCo2);
    } else {
        snprintf(co2CharBuf, sizeof(co2CharBuf), "----"); // no fresh sample: unknown, never a stale number
    }
    uint16_t co2MatrixBuf[16];
    int len = strlen(co2CharBuf);
    for(int i = 0; i < len; i++) co2MatrixBuf[i] = (uint16_t)co2CharBuf[i];
    if (matrixPtr != nullptr) {
        // Red for the whole alarm, i.e. until CO2 falls below the low threshold, so the
        // repeat popups in the hysteresis band don't read as "all clear".
        bool alert = alarmActive || (co2Known && currentCo2 >= CO2_WARNING_THRESHOLD);
        uint16_t targetColor = alert ? alertColor : normalColor;
        matrixPtr->drawText4x14(co2MatrixBuf, len, 0, 1, targetColor);
    }
}
