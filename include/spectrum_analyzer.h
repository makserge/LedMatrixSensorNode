#ifndef SPECTRUM_ANALYZER_H
#define SPECTRUM_ANALYZER_H

#include <Arduino.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <vector>
#include <WiFi.h>
#include <WiFiClient.h>

#include "AudioTools.h"
#include "AudioTools/AudioLibs/AudioRealFFT.h"

#include "constants.h"
#include "display_manager.h"
#include "ld2412_sensor.h"
#include "ld2454_sensor.h"
#include "task_registry.h"

// AudioRealFFT that can drop its partly filled input window, so samples of the
// previous connection never end up in the first frame of the next one.
class SpectrumFft : public AudioRealFFT {
public:
    void clearInput() { stride_buffer.reset(); }
};

class SpectrumAnalyzer {
public:
    // Call once before the tasks start: the FFTs must be set up before StreamIngest
    // (other core) can feed them.
    void begin();
    void updateAudio();
    void getBarsAndPeaks(uint8_t* outBars, uint8_t* outPeaks);
    void runBallisticsTick();
    bool isAudioActive();

    // Stream side - StreamIngest task only.
    void beginStream(uint32_t sampleRate, uint16_t channels);
    size_t writeAudio(const uint8_t* buffer, size_t size);
    void setStreamConnected(bool connected);

private:
    struct BinWeight {
        uint16_t bin;
        uint8_t band;
        float weight;
    };
    // Gaussian band weights of one FFT; they only change with the sample rate.
    struct BandTable {
        std::vector<BinWeight> entries; // ordered by bin
        float weightSum[24];
        int firstBin;                   // magnitudes are needed for firstBin..lastBin
        int lastBin;
        float magScale;                 // noise-equivalent normalisation to the 2048-point FFT
    };

    SpectrumFft _fftLow;  // wide window - drives the bass bands
    SpectrumFft _fftHigh; // short window - drives mid/high bands, low latency
    BandTable _bandsLow;
    BandTable _bandsHigh;
    uint32_t _sampleRate = 0;

    static void fftCallbackLow(AudioFFTBase &fft_ref);
    static void fftCallbackHigh(AudioFFTBase &fft_ref);
    void configureFfts(uint32_t sampleRate);
    static void buildBandTable(BandTable& table, int length, uint32_t sampleRate,
                               int band_start, int band_end, bool isPrimary, float magScale);
    void processFftFrame(AudioFFTBase &fft_ref, const BandTable& table, int band_start, int band_end, bool isPrimary);
    void feedFfts(const int16_t* samples, size_t count);
    int16_t downmixFrame(const uint8_t* frame) const;
    void resetActivityLocked(); // _stateMux held

    // StreamIngest-only state
    uint16_t _channels = 1;
    uint8_t _frameBytes = 2;
    uint8_t _carry[STREAM_MAX_CHANNELS * 2];
    uint8_t _carryLen = 0;
    float _rollingAvgRawMag = 0.0f;
    float _referenceMag = SPECTRUM_REF_FLOOR_MAG;
    bool _havePrimaryFrame = false;
    uint32_t _lastPrimaryFrameMs = 0;
    uint8_t _activeStreak = 0;

    // Written by StreamIngest (core 0), read by Ballistics/AudioViewSwitch (core 1).
    portMUX_TYPE _stateMux = portMUX_INITIALIZER_UNLOCKED;
    float _pendingLevel[24] = {0};
    bool _newLowData = false;
    bool _newHighData = false;
    bool _streamConnected = false;
    bool _sleepMode = true;
    uint32_t _lastFrameMs = 0;
    bool _hasActiveFrame = false;
    uint32_t _lastActiveFrameMs = 0;

    // Ballistics-only state
    struct BandBallistics {
        float current_level = 0.0f;
        float peak_hold_level = 0.0f;
        uint32_t peak_timer = 0;
        bool decaying = false;
        float peak_velocity = 0.0f;
    };
    BandBallistics _ballistics[24];
    float _visibility = 0.0f;

    portMUX_TYPE _ballisticsMux = portMUX_INITIALIZER_UNLOCKED;
    uint8_t _sharedBars[24] = {0};
    uint8_t _sharedPeaks[24] = {0};
    bool _barsFaded = true;           // sleep fade complete: nothing visible, so the view isn't held

    // AudioViewSwitch-only state
    bool _wasAudioActive = false;
    DisplayView _previousView = VIEW_TIME;
    bool _spectrumShown = false;      // a spectrum session (auto or manual) is on, possibly under an overlay
    uint32_t _spectrumEnteredMs = 0;
    uint32_t _silenceStartMs = 0;
    bool _pendingSpectrumEntry = false;
};

extern SpectrumAnalyzer spectrumAnalyzer;

void initSpectrumAnalyzerTask();

#endif
