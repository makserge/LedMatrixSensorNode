#include "screen_renderers.h"
#include "spectrum_analyzer.h"
#include "display_manager.h"

void renderSpectrumBarsView(uint8_t* currentBars, uint8_t* currentPeaks, uint16_t barColor, uint16_t peakColor) {
    spectrumAnalyzer.getBarsAndPeaks(currentBars, currentPeaks);
    for (int x = 0; x < 24; x++) {
        uint8_t height = currentBars[x];
        for (int y = 0; y < height; y++) {
            displayManager.drawPixel2D(x, 15 - y, barColor);
        }
        uint8_t peakPos = currentPeaks[x];
        if (peakPos > 0) {
            displayManager.drawPixel2D(x, 15 - (peakPos - 1), peakColor);
        }
    }
}
