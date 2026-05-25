#include "screen_renderers.h"
#include "constants.h"
#include "display_manager.h"

void renderScrollingAlertView(MatrixPanel_I2S_DMA* matrixPtr, int& scrollX, unsigned long& lastScrollTime, uint16_t color) {
    uint16_t msgBuf[MQTT_MSG_MAX_LEN];
    int msgLen = 0;
    DisplayManager::getCustomMessage(msgBuf, msgLen);
    
    if (matrixPtr != nullptr && msgLen > 0) {
        int totalTextWidth = msgLen * 5;
        if (millis() - lastScrollTime >= 154) {
            lastScrollTime = millis();
            scrollX--;
            if (scrollX < -totalTextWidth) {
                scrollX = 24;
                DisplayManager::incrementScrollCount();
            }
        }
        if (DisplayManager::getScrollCount() < 3) {
            matrixPtr->drawText4x14(msgBuf, msgLen, scrollX, 1, color);
        }
    }
}
