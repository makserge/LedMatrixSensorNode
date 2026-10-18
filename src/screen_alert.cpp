#include "screen_renderers.h"
#include "constants.h"
#include "display_manager.h"

// One pass moves the text from x = MATRIX_WIDTH until it is fully off the left edge:
// MATRIX_WIDTH + 1 + width steps, the length DisplayManager::scrollPassMs() plans with.
void renderScrollingAlertView(MatrixPanel_I2S_DMA* matrixPtr, MessageScroll& scroll, uint16_t color) {
    uint16_t msgBuf[MQTT_MSG_MAX_LEN];
    int msgLen = 0;
    uint32_t generation = 0;
    DisplayManager::getCustomMessage(msgBuf, msgLen, generation);
    unsigned long nowMs = millis();

    if (generation != scroll.generation) {
        // A new message, or one replayed after the CO2 popup: start at the right edge,
        // not at the offset the previous text had reached.
        scroll.generation = generation;
        scroll.x = MATRIX_WIDTH;
        scroll.lastStepMs = nowMs;
    }
    if (msgLen <= 0) return;

    unsigned long steps = (nowMs - scroll.lastStepMs) / MSG_SCROLL_STEP_MS;
    if (steps > MSG_SCROLL_MAX_CATCHUP_STEPS) {
        // Not drawn for a while (panel off, WiFi down): carry on from here, don't jump.
        steps = 1;
        scroll.lastStepMs = nowMs;
    } else {
        // From the last step's due time, not from now: a late frame doesn't stretch the
        // step, so a pass takes exactly as long as setCustomMessage() planned.
        scroll.lastStepMs += steps * MSG_SCROLL_STEP_MS;
    }

    int totalTextWidth = msgLen * FONT_4X14_CELL_WIDTH;
    for (; steps > 0; steps--) {
        scroll.x--;
        if (scroll.x < -totalTextWidth) {
            scroll.x = MATRIX_WIDTH;
            DisplayManager::completeScrollPass(generation);
        }
    }
    if (matrixPtr != nullptr) {
        matrixPtr->drawText4x14(msgBuf, msgLen, scroll.x, 1, color);
    }
}
