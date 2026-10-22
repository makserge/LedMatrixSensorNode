#include "screen_renderers.h"
#include "temp_humidity_sensor.h"

extern TempHumiditySensor tempHumSensor;

void renderTemperatureView(MatrixPanel_I2S_DMA* matrixPtr, uint16_t color) {
    char envCharBuf[16];
    if (tempHumSensor.hasFreshTemp()) {
        float rounded = roundf(tempHumSensor.getTemp());
        if (rounded == 0.0f) rounded = 0.0f; // -0.4 would print as "-0"
        snprintf(envCharBuf, sizeof(envCharBuf), "%.0f*C", rounded);
    } else {
        snprintf(envCharBuf, sizeof(envCharBuf), "--*C"); // sensor missing or failing: unknown, not 0
    }
    uint16_t envMatrixBuf[16];
    int len = strlen(envCharBuf);
    for(int i = 0; i < len; i++) envMatrixBuf[i] = (uint16_t)envCharBuf[i];
    if (matrixPtr != nullptr) {
        // Five cells ("-15*C") need all 24 columns; up to four keep the usual indent.
        int startX = (len * FONT_4X14_CELL_WIDTH > MATRIX_WIDTH - 4) ? 0 : 4;
        matrixPtr->drawText4x14(envMatrixBuf, len, startX, 1, color);
    }
}
