#ifndef SCREEN_RENDERERS_H
#define SCREEN_RENDERERS_H

#include "led_matrix.h"
#include "ntp_clock.h"

// Scroll position of the custom message, kept by the display task between frames.
struct MessageScroll {
    int x;                    // left edge of the text
    unsigned long lastStepMs; // time of the last 1-px step
    uint32_t generation;      // the message this belongs to (DisplayManager::getCustomMessage)
};

void renderClockView(MatrixPanel_I2S_DMA* matrixPtr, const ClockMessage& clockMsg, uint16_t color);
void renderTemperatureView(MatrixPanel_I2S_DMA* matrixPtr, uint16_t color);
void renderCo2View(MatrixPanel_I2S_DMA* matrixPtr, bool co2Known, uint16_t currentCo2, bool alarmActive,
                   uint16_t normalColor, uint16_t alertColor);
void renderSpectrumBarsView(uint8_t* currentBars, uint8_t* currentPeaks, uint16_t barColor, uint16_t peakColor);
void renderScrollingAlertView(MatrixPanel_I2S_DMA* matrixPtr, MessageScroll& scroll, uint16_t color);
void renderOtaProgressView(MatrixPanel_I2S_DMA* matrixPtr, uint8_t percent, uint16_t color);
void renderIconView(MatrixPanel_I2S_DMA* matrixPtr);

#endif