#include "screen_renderers.h"
#include "temp_humidity_sensor.h"

extern TempHumiditySensor tempHumSensor;

void renderTemperatureView(MatrixPanel_I2S_DMA* matrixPtr, uint16_t color) {
    char envCharBuf[16];
    snprintf(envCharBuf, sizeof(envCharBuf), "%.0f*C", tempHumSensor.getTemp());
    uint16_t envMatrixBuf[16];
    int len = strlen(envCharBuf);
    for(int i = 0; i < len; i++) envMatrixBuf[i] = (uint16_t)envCharBuf[i];
    if (matrixPtr != nullptr) {
        matrixPtr->drawText4x14(envMatrixBuf, len, 4, 1, color);
    }
}
