#include "screen_renderers.h"
#include "spectrum_analyzer.h"
#include "display_manager.h"

static uint16_t colorForRow(int row) {
    static const uint16_t colorGreen  = MatrixPanel_I2S_DMA::color565(0, 255, 0);
    static const uint16_t colorYellow = MatrixPanel_I2S_DMA::color565(255, 255, 0);
    static const uint16_t colorRed    = MatrixPanel_I2S_DMA::color565(255, 0, 0);

    if (row < 7)  return colorGreen;
    if (row < 12) return colorYellow;
    return colorRed;
}

void renderSpectrumBarsView(uint8_t* currentBars, uint8_t* currentPeaks, uint16_t barColor, uint16_t peakColor) {
    spectrumAnalyzer.getBarsAndPeaks(currentBars, currentPeaks);
    for (int x = 0; x < 24; x++) {
        uint8_t height = currentBars[x];
        for (int y = 0; y < height; y++) {
            displayManager.drawPixel2D(x, 15 - y, colorForRow(y));
        }
        uint8_t peakPos = currentPeaks[x];
        if (peakPos > 0) {
            displayManager.drawPixel2D(x, 15 - (peakPos - 1), peakColor);
        }
    }
}