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

#include "spectrum_dsp.h"   // the signal chain of the analyzer
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
    // Per column: lit rows 0..16 and the peak's row + 1 (0 = no peak).
    void getBarsAndPeaks(uint8_t* outBars, uint8_t* outPeaks);
    void runBallisticsTick();
    bool isAudioActive();

    // Stream side - StreamIngest task only.
    void beginStream(uint32_t sampleRate, uint16_t channels);
    size_t writeAudio(const uint8_t* buffer, size_t size);
    void setStreamConnected(bool connected);

private:
    SpectrumFft _fftLow;  // wide window - drives the bass bands
    SpectrumFft _fftHigh; // short window - drives mid/high bands, low latency
    // Per-FFT band plan and per-bin state (sd_fft_t, spectrum_dsp.h). Plans are rebuilt by StreamIngest (the only
    // task that touches them) when the sample rate changes - never allocated, never in the hot loop.
    sd_fft_t _planLow;
    sd_fft_t _planHigh;
    float _smoothLow[SPECTRUM_FFT_LOW_SIZE / 2];
    float _dbLow[SPECTRUM_FFT_LOW_SIZE / 2];
    float _smoothHigh[SPECTRUM_FFT_HIGH_SIZE / 2];
    float _dbHigh[SPECTRUM_FFT_HIGH_SIZE / 2];
    bool _planDirty = true;
    int _actFirstBin = 1;              // bins of the activity measure (22 Hz - 16 kHz of the long FFT)
    int _actLastBin = 0;
    float _activityScale = 1.0f;
    uint32_t _sampleRate = 0;

    static void fftCallbackLow(AudioFFTBase &fft_ref);
    static void fftCallbackHigh(AudioFFTBase &fft_ref);
    void configureFfts(uint32_t sampleRate);
    void refreshPlans();
    void rebuildPlans();
    void processFftFrame(AudioFFTBase &fft_ref, sd_fft_t& plan, bool isPrimary);
    void feedFfts(const int16_t* samples, size_t count);
    int16_t downmixFrame(const uint8_t* frame) const;
    void resetActivityLocked(); // _stateMux held

    // StreamIngest-only state
    uint16_t _channels = 1;
    uint8_t _frameBytes = 2;
    uint8_t _carry[STREAM_MAX_CHANNELS * 2];
    uint8_t _carryLen = 0;
    float _rollingAvgRawMag = 0.0f;
    bool _havePrimaryFrame = false;
    uint32_t _lastPrimaryFrameMs = 0;
    uint8_t _activeStreak = 0;

    // Written by StreamIngest (core 0), read by Ballistics/AudioViewSwitch (core 1).
    portMUX_TYPE _stateMux = portMUX_INITIALIZER_UNLOCKED;
    float _pendingDb[SD_BANDS];          // weighted dB per band (set in begin())
    bool _newBand[SD_BANDS] = {false};
    bool _streamConnected = false;
    bool _sleepMode = true;
    bool _frameQuiet = true;             // the last 2048-point frame was below SPECTRUM_SLEEP_ENTER_MAG
    uint32_t _lastFrameMs = 0;
    bool _hasActiveFrame = false;
    uint32_t _lastActiveFrameMs = 0;

    // Ballistics-only state
    float _valueDb[SD_BANDS];        // the bands' weighted dB until the next FFT frame of their FFT (set in begin())
    sd_agc_t _agc;                   // automatic gain: display range relative to the loudest band
    sd_peak_t _peak[SD_BANDS];
    float _visibility = 0.0f;

    portMUX_TYPE _ballisticsMux = portMUX_INITIALIZER_UNLOCKED;
    uint8_t _sharedBars[SD_BANDS] = {0};
    uint8_t _sharedPeaks[SD_BANDS] = {0};
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
