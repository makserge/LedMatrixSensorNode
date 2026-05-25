#ifndef SCREEN_RENDERERS_H
#define SCREEN_RENDERERS_H

#include "led_matrix.h"
#include "ntp_clock.h"

void renderClockView(MatrixPanel_I2S_DMA* matrixPtr, const ClockMessage& clockMsg, uint16_t color);
void renderTemperatureView(MatrixPanel_I2S_DMA* matrixPtr, uint16_t color);
void renderCo2View(MatrixPanel_I2S_DMA* matrixPtr, uint16_t currentCo2, uint16_t normalColor, uint16_t alertColor);
void renderSpectrumBarsView(uint8_t* currentBars, uint8_t* currentPeaks, uint16_t barColor, uint16_t peakColor);
void renderScrollingAlertView(MatrixPanel_I2S_DMA* matrixPtr, int& scrollX, unsigned long& lastScrollTime, uint16_t color);

#endif
