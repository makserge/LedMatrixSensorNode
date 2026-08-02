#ifndef SPECTRUM_ANALYZER_H
#define SPECTRUM_ANALYZER_H

#include <Arduino.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <WiFi.h>
#include <WiFiClient.h>

#include "AudioTools.h"
#include "AudioTools/AudioLibs/AudioRealFFT.h"

#include "constants.h"
#include "display_manager.h"
#include "ld2412_sensor.h"
#include "ld2450_sensor.h"
#include "task_registry.h"

class SpectrumAnalyzer {
public:
    void begin();
    void updateAudio();
    void getBarsAndPeaks(uint8_t* outBars, uint8_t* outPeaks);
    size_t writeAudio(const uint8_t* buffer, size_t size);
    void setStreamConnected(bool connected);
    void runBallisticsTick();
    bool isAudioActive() const;
    static float getFrameLevelRaw() { return _lastFrameRawMag; }

private:
    AudioRealFFT _fftLow;  // wide window - drives the bass bands
    AudioRealFFT _fftHigh; // short window - drives mid/high bands, low latency

    static float _spectrumLevel[24];

    static volatile bool _newLowDataFlag;
    static volatile bool _newHighDataFlag;
    static float _lastFrameRawMag;

    static void fftCallbackLow(AudioFFTBase &fft_ref);
    static void fftCallbackHigh(AudioFFTBase &fft_ref);
    static void processFftFrame(AudioFFTBase &fft_ref, int band_start, int band_end, bool isPrimary);

    static float _rollingAvgRawMag;
    static float _referenceMag;
    static bool _sleepMode;
    static uint32_t _sleepFadeStartMs;
    static uint32_t _lastFftFrameMs;

    struct BandBallistics {
        float current_level = 0.0f;
        float peak_hold_level = 0.0f;
        uint32_t peak_timer = 0;
        bool decaying = false;
        float peak_velocity = 0.0f;
    };
    BandBallistics _ballistics[24];

    portMUX_TYPE _ballisticsMux = portMUX_INITIALIZER_UNLOCKED;
    uint8_t _sharedBars[24] = {0};
    uint8_t _sharedPeaks[24] = {0};

    volatile bool _streamConnected = false;
    volatile uint32_t _lastAudioActiveMs = 0;
    uint8_t _activeStreak = 0;
    bool _wasAudioActive = false;
    DisplayView _previousView = VIEW_TIME;
    uint32_t _spectrumEnteredMs = 0;
    uint32_t _silenceStartMs = 0;
    bool _pendingSpectrumEntry = false;
};

extern SpectrumAnalyzer spectrumAnalyzer;

void initSpectrumAnalyzerTask();

#endif
