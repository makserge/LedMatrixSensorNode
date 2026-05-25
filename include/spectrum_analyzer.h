#ifndef SPECTRUM_ANALYZER_H
#define SPECTRUM_ANALYZER_H

#include <Arduino.h>

class SpectrumAnalyzer {
public:
    void begin();
    void updateAudio();
    void getBarsAndPeaks(uint8_t* outBars, uint8_t* outPeaks);

private:
    uint8_t _bars[24] = {0};
};

extern SpectrumAnalyzer spectrumAnalyzer;

void initSpectrumAnalyzerTask();

#endif
