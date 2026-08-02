#include "screen_renderers.h"
#include "display_manager.h"
#include "constants.h"

void renderIconView(MatrixPanel_I2S_DMA* matrixPtr) {
    if (matrixPtr == nullptr) return;

    uint16_t buf[MATRIX_TOTAL_PIXELS];
    DisplayManager::getIconBuffer(buf);

    for (int y = 0; y < MATRIX_HEIGHT; y++) {
        for (int x = 0; x < MATRIX_WIDTH; x++) {
            matrixPtr->drawPixel(x, y, buf[y * MATRIX_WIDTH + x]);
        }
    }
}