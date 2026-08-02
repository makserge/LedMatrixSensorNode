#include "spectrum_analyzer.h"

SpectrumAnalyzer spectrumAnalyzer;

extern Ld2412Sensor ld2412Sensor;
extern Ld2450Sensor ld2450Sensor;

float SpectrumAnalyzer::_spectrumLevel[24] = {0};
volatile bool SpectrumAnalyzer::_newLowDataFlag = false;
volatile bool SpectrumAnalyzer::_newHighDataFlag = false;
float SpectrumAnalyzer::_lastFrameRawMag = 0.0f;

float SpectrumAnalyzer::_rollingAvgRawMag = 0.0f;
float SpectrumAnalyzer::_referenceMag = SPECTRUM_REF_FLOOR_MAG;
bool SpectrumAnalyzer::_sleepMode = true;
uint32_t SpectrumAnalyzer::_sleepFadeStartMs = 0;
uint32_t SpectrumAnalyzer::_lastFftFrameMs = 0;

static const uint16_t WAV_SAMPLE_RATE     = 44100;
static const uint8_t  WAV_BITS_PER_SAMPLE = 16;
static const uint8_t  FFT_CHANNELS        = 1;

namespace {
// The library's own Hann class uses the Hamming coefficient (0.54) instead of
// Hann's 0.5 - a bug in the third-party audio-tools implementation. Supply a
// correct one instead of relying on it.
class SpectrumHannWindow : public WindowFunction {
public:
    const char* name() override { return "SpectrumHann"; }
protected:
    float factor_internal(int idx) override {
        return 0.5f * (1.0f - cosf(twoPi * ratio(idx)));
    }
};
SpectrumHannWindow g_hannWindowLow;
SpectrumHannWindow g_hannWindowHigh;
}

void SpectrumAnalyzer::begin() {
    auto lowCfg = _fftLow.defaultConfig();
    lowCfg.length = SPECTRUM_FFT_LOW_SIZE;
    lowCfg.stride = SPECTRUM_FFT_LOW_STRIDE;
    lowCfg.sample_rate = WAV_SAMPLE_RATE;
    lowCfg.channels = FFT_CHANNELS;
    lowCfg.bits_per_sample = WAV_BITS_PER_SAMPLE;
    lowCfg.window_function_fft = &g_hannWindowLow;
    lowCfg.callback = &SpectrumAnalyzer::fftCallbackLow;
    _fftLow.begin(lowCfg);

    auto highCfg = _fftHigh.defaultConfig();
    highCfg.length = SPECTRUM_FFT_HIGH_SIZE;
    highCfg.stride = SPECTRUM_FFT_HIGH_STRIDE;
    highCfg.sample_rate = WAV_SAMPLE_RATE;
    highCfg.channels = FFT_CHANNELS;
    highCfg.bits_per_sample = WAV_BITS_PER_SAMPLE;
    highCfg.window_function_fft = &g_hannWindowHigh;
    highCfg.callback = &SpectrumAnalyzer::fftCallbackHigh;
    _fftHigh.begin(highCfg);
}

size_t SpectrumAnalyzer::writeAudio(const uint8_t* buffer, size_t size) {
    size_t usable_bytes = size & ~(size_t)1; // drop a possible trailing odd byte
    if (usable_bytes > 0) {
        _fftLow.write(const_cast<uint8_t*>(buffer), usable_bytes);
        _fftHigh.write(const_cast<uint8_t*>(buffer), usable_bytes);
    }
    return size;
}

void SpectrumAnalyzer::fftCallbackLow(AudioFFTBase &fft_ref) {
    processFftFrame(fft_ref, 0, SPECTRUM_LOW_BAND_COUNT, true);
}

void SpectrumAnalyzer::fftCallbackHigh(AudioFFTBase &fft_ref) {
    processFftFrame(fft_ref, SPECTRUM_LOW_BAND_COUNT, 24, false);
}

void SpectrumAnalyzer::processFftFrame(AudioFFTBase &fft_ref, int band_start, int band_end, bool isPrimary) {
    float* packed = static_cast<AudioRealFFT&>(fft_ref).imgArray();
    int num_bins = fft_ref.size();
    int half = num_bins;
    float bin_resolution = (float)fft_ref.config().sample_rate / (float)fft_ref.config().length;

    float band_magnitudes[24] = {0};
    float band_weight_sums[24] = {0};
    float frame_total_energy = 0.0f;
    int frame_bin_count = 0;

    static const float band_centers[24] = {
        31.0f,   63.0f,   80.0f,   100.0f,  125.0f,  160.0f,
        200.0f,  250.0f,  315.0f,  400.0f,  500.0f,  630.0f,
        800.0f,  1000.0f, 1250.0f, 1600.0f, 2000.0f, 2500.0f,
        3150.0f, 4000.0f, 5000.0f, 6300.0f, 8000.0f, 16000.0f
    };

    static const float band_edges[25] = {
        22.0f,    44.2f,    70.9f,    89.4f,    111.8f,   141.4f,   178.9f,
        223.6f,   280.6f,   355.0f,   447.2f,   565.7f,   714.1f,
        894.4f,   1118.0f,  1414.2f,  1788.9f,  2236.1f,  2806.2f,
        3549.6f,  4472.1f,  5656.9f,  7141.4f,  11313.7f, 16000.0f
    };

    static float band_log_centers[24];
    static float band_sigma_oct[24];
    static bool bandTablesInited = false;
    if (!bandTablesInited) {
        for (int i = 0; i < 24; i++) {
            band_log_centers[i] = log2f(band_centers[i]);
            band_sigma_oct[i] = (log2f(band_edges[i + 1]) - log2f(band_edges[i])) * 0.5f;
        }
        bandTablesInited = true;
    }

    for (int bin_idx = 0; bin_idx < num_bins; bin_idx++) {
        float current_freq = bin_idx * bin_resolution;
        float mag_val;
        if (bin_idx == 0) {
            mag_val = fabsf(packed[0]);
        } else {
            float re = packed[bin_idx];
            float im = packed[half + bin_idx];
            mag_val = sqrtf(re * re + im * im);
        }

        if (isPrimary && current_freq >= 22.0f && current_freq <= 16000.0f) {
            frame_total_energy += mag_val;
            frame_bin_count++;
        }

        if (current_freq < 22.0f || current_freq > 16000.0f) continue;

        float log_freq = log2f(current_freq);
        for (int band_idx = band_start; band_idx < band_end; band_idx++) {
            float sigma = band_sigma_oct[band_idx];
            float delta = fabsf(log_freq - band_log_centers[band_idx]);
            if (delta > sigma * 3.0f) continue;

            float weight = expf(-0.5f * (delta * delta) / (sigma * sigma));
            band_magnitudes[band_idx] += (mag_val * weight);
            band_weight_sums[band_idx] += weight;
        }
    }

    if (isPrimary) {
        float frame_average_mag = (frame_bin_count > 0) ? (frame_total_energy / frame_bin_count) : 0.0f;
        _lastFrameRawMag = frame_average_mag;

        uint32_t nowMs = millis();
        float dt = (_lastFftFrameMs == 0) ? (1.0f / 30.0f) : (float)(nowMs - _lastFftFrameMs) / 1000.0f;
        dt = constrain(dt, 0.005f, 0.5f);
        _lastFftFrameMs = nowMs;

        float avgAlpha = dt / ((SPECTRUM_SILENCE_AVG_WINDOW_MS / 1000.0f) + dt);
        _rollingAvgRawMag += (frame_average_mag - _rollingAvgRawMag) * avgAlpha;

        bool wasSleep = _sleepMode;
        _sleepMode = _rollingAvgRawMag < SPECTRUM_ABS_SILENCE_MAG;
        if (_sleepMode && !wasSleep) {
            _sleepFadeStartMs = nowMs;
        }

        float refTarget = fmaxf(frame_average_mag, SPECTRUM_REF_FLOOR_MAG);
        if (refTarget > _referenceMag) {
            _referenceMag += (refTarget - _referenceMag) * SPECTRUM_REF_RISE_ALPHA;
        } else {
            _referenceMag += (refTarget - _referenceMag) * SPECTRUM_REF_FALL_ALPHA;
        }
        if (_referenceMag < SPECTRUM_REF_FLOOR_MAG) _referenceMag = SPECTRUM_REF_FLOOR_MAG;
    }

    for (int i = band_start; i < band_end; i++) {
        float avg_magnitude = (band_weight_sums[i] > 0.0f) ? (band_magnitudes[i] / band_weight_sums[i]) : 0.0f;
        float bandDbfs = (avg_magnitude > 1e-6f)
            ? 20.0f * log10f(avg_magnitude / (_referenceMag * SPECTRUM_REF_SCALE))
            : -200.0f;

        float tiltDb = SPECTRUM_TILT_DB_PER_OCTAVE * log2f(band_centers[i] / SPECTRUM_TILT_REFERENCE_HZ);
        bandDbfs += tiltDb;

        float targetSteps;
        if (bandDbfs < SPECTRUM_NOISE_GATE_DBFS) {
            targetSteps = 0.0f;
        } else {
            float compressedDb = bandDbfs;
            if (compressedDb > SPECTRUM_COMPRESSION_THRESHOLD_DBFS) {
                float over = compressedDb - SPECTRUM_COMPRESSION_THRESHOLD_DBFS;
                compressedDb = SPECTRUM_COMPRESSION_THRESHOLD_DBFS + (over / SPECTRUM_COMPRESSION_RATIO);
            }
            targetSteps = (compressedDb / SPECTRUM_DB_PER_STEP) + SPECTRUM_AGC_TARGET_LED;
            targetSteps = constrain(targetSteps, 0.0f, BALLISTICS_MAX_LEVEL);
        }

        _spectrumLevel[i] = targetSteps;
    }

    if (isPrimary) {
        _newLowDataFlag = true;
    } else {
        _newHighDataFlag = true;
    }
}

bool SpectrumAnalyzer::isAudioActive() const {
    return (millis() - _lastAudioActiveMs) < AUDIO_SILENCE_TIMEOUT_MS;
}

void SpectrumAnalyzer::updateAudio() {
    bool audioActive = _streamConnected && isAudioActive();
    DisplayView currentView = DisplayManager::getCurrentView();
    uint32_t nowMs = millis();
    bool overlayActive = (currentView == VIEW_CO2 || currentView == VIEW_CUSTOM_MSG);
    bool presenceDetected = ld2412Sensor.isPresent() || ld2450Sensor.hasAnyTarget();

    if (!audioActive) {
        if (_wasAudioActive) {
            _silenceStartMs = nowMs;
        }
        _pendingSpectrumEntry = false;
        bool minDisplayElapsed = (nowMs - _spectrumEnteredMs) >= MIN_SPECTRUM_DISPLAY_MS;
        bool longSilence = (nowMs - _silenceStartMs) >= SPECTRUM_PRESENCE_OVERRIDE_MS;

        if (currentView == VIEW_SPECTRUM && minDisplayElapsed && (!presenceDetected || longSilence)) {
            DisplayManager::setView(_previousView);
        }
    } else if (overlayActive) {
        _pendingSpectrumEntry = true;
    } else if (currentView != VIEW_SPECTRUM && (!_wasAudioActive || _pendingSpectrumEntry)) {
        _previousView = currentView;
        _spectrumEnteredMs = nowMs;
        DisplayManager::setView(VIEW_SPECTRUM);
        _pendingSpectrumEntry = false;
    }

    _wasAudioActive = audioActive;
}

void SpectrumAnalyzer::setStreamConnected(bool connected) {
    _streamConnected = connected;
}

void SpectrumAnalyzer::getBarsAndPeaks(uint8_t* outBars, uint8_t* outPeaks) {
    portENTER_CRITICAL(&_ballisticsMux);
    memcpy(outBars, _sharedBars, 24);
    memcpy(outPeaks, _sharedPeaks, 24);
    portEXIT_CRITICAL(&_ballisticsMux);
}

void SpectrumAnalyzer::runBallisticsTick() {
    uint32_t nowMs = millis();
    static const float kBallisticsTickSec = BALLISTICS_TICK_MS / 1000.0f;

    bool newLowData = _newLowDataFlag;
    if (newLowData) _newLowDataFlag = false;
    bool newHighData = _newHighDataFlag;
    if (newHighData) _newHighDataFlag = false;

    float visibility = 1.0f;
    if (_sleepMode) {
        uint32_t elapsed = nowMs - _sleepFadeStartMs;
        float fadeOut = constrain((float)elapsed / (float)SPECTRUM_SLEEP_FADE_MS, 0.0f, 1.0f);
        visibility = 1.0f - fadeOut;
    }

    uint8_t localBars[24];
    uint8_t localPeaks[24];

    for (int i = 0; i < 24; i++) {
        BandBallistics &b = _ballistics[i];

        bool newDataForBand = (i < SPECTRUM_LOW_BAND_COUNT) ? newLowData : newHighData;
        float target = newDataForBand ? _spectrumLevel[i] : (b.current_level * BALLISTICS_DECAY_COEFF);

        if (target > b.current_level) {
            b.current_level = (target * (1.0f - BALLISTICS_ATTACK_COEFF))
                            + (b.current_level * BALLISTICS_ATTACK_COEFF);
        } else {
            b.current_level = (target * (1.0f - BALLISTICS_DECAY_COEFF))
                             + (b.current_level * BALLISTICS_DECAY_COEFF);
        }
        if (b.current_level < 0.05f) b.current_level = 0.0f;

        if (b.current_level >= b.peak_hold_level) {
            b.peak_hold_level = b.current_level;
            b.peak_timer = nowMs;
            b.decaying = false;
            b.peak_velocity = 0.0f;
        } else {
            if (!b.decaying && (nowMs - b.peak_timer) >= BALLISTICS_PEAK_HOLD_MS) {
                b.decaying = true;
                b.peak_velocity = 0.0f; // starts falling from rest, like a dropped object
            }
            if (b.decaying) {
                b.peak_velocity += SPECTRUM_PEAK_GRAVITY_STEPS_PER_S2 * kBallisticsTickSec;
                b.peak_hold_level -= b.peak_velocity * kBallisticsTickSec;

                if (b.peak_hold_level <= b.current_level) {
                    b.peak_hold_level = b.current_level;
                    b.decaying = false;
                    b.peak_velocity = 0.0f;
                }
            }
        }

        uint8_t barHeight = (uint8_t)((b.current_level * visibility) + 0.5f);
        uint8_t peakHeight = (uint8_t)((b.peak_hold_level * visibility) + 0.5f);
        if (barHeight == 0 && peakHeight <= 1) peakHeight = 0;

        localBars[i] = barHeight;
        localPeaks[i] = peakHeight;
    }

    if (SpectrumAnalyzer::getFrameLevelRaw() >= SPECTRUM_ABS_SILENCE_MAG) {
        if (_activeStreak < 255) _activeStreak++;
        if (_activeStreak >= 2) {
            _lastAudioActiveMs = nowMs;
        }
    } else {
        _activeStreak = 0;
    }

    portENTER_CRITICAL(&_ballisticsMux);
    memcpy(_sharedBars, localBars, sizeof(localBars));
    memcpy(_sharedPeaks, localPeaks, sizeof(localPeaks));
    portEXIT_CRITICAL(&_ballisticsMux);
}

void streamIngestionTask(void* pvParameters) {
    WiFiClient client;

    char host[64];
    char resourcePath[128];
    uint16_t port = 80;
    {
        const char* url = AUDIO_STREAM_URL;
        if (strncmp(url, "http://", 7) == 0) url += 7;
        const char* slash = strchr(url, '/');
        size_t hostLen = slash ? (size_t)(slash - url) : strlen(url);
        if (hostLen >= sizeof(host)) hostLen = sizeof(host) - 1;
        memcpy(host, url, hostLen);
        host[hostLen] = '\0';
        strncpy(resourcePath, slash ? slash : "/", sizeof(resourcePath) - 1);
        resourcePath[sizeof(resourcePath) - 1] = '\0';

        char* colon = strchr(host, ':');
        if (colon) {
            port = (uint16_t)atoi(colon + 1);
            *colon = '\0';
        }
    }

    while (1) {
        if (client.connect(host, port)) {
            char request[256];
            snprintf(request, sizeof(request),
                     "GET %s HTTP/1.1\r\n"
                     "Host: %s\r\n"
                     "User-Agent: ESP32-S3-Analyser\r\n"
                     "Accept: */*\r\n"
                     "Connection: close\r\n\r\n",
                     resourcePath, host);
            client.print(request);

            unsigned long timeout = millis();
            while (client.connected() && !client.available()) {
                if (millis() - timeout > 3000) break;
                vTaskDelay(pdMS_TO_TICKS(10));
            }

            int newlineRun = 0;
            while (client.connected() && newlineRun < 2) {
                if (!client.available()) {
                    vTaskDelay(pdMS_TO_TICKS(1));
                    continue;
                }
                char c = (char)client.read();
                if (c == '\r') continue;
                newlineRun = (c == '\n') ? newlineRun + 1 : 0;
            }

            const size_t chunk_bytes = 512;
            uint8_t network_buffer[chunk_bytes];

            if (client.available() >= 44) {
                client.readBytes(network_buffer, 44);
            }

            spectrumAnalyzer.setStreamConnected(true);

            while (client.connected() || client.available()) {
                size_t available_bytes = client.available();
                if (available_bytes > 0) {
                    size_t bytes_to_read = (available_bytes > chunk_bytes) ? chunk_bytes : available_bytes;
                    client.readBytes(network_buffer, bytes_to_read);
                    spectrumAnalyzer.writeAudio(network_buffer, bytes_to_read);
                }
                vTaskDelay(pdMS_TO_TICKS(1));
            }
        }

        spectrumAnalyzer.setStreamConnected(false);
        client.stop();
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

void ballisticsTask(void* pvParameters) {
    TickType_t xLastWakeTime = xTaskGetTickCount();
    const TickType_t xFrequency = pdMS_TO_TICKS(BALLISTICS_TICK_MS);

    for (;;) {
        vTaskDelayUntil(&xLastWakeTime, xFrequency);
        spectrumAnalyzer.runBallisticsTick();
    }
}

void spectrumAnalyzerTask(void* pvParameters) {
    spectrumAnalyzer.begin();
    for (;;) {
        spectrumAnalyzer.updateAudio();
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

void initSpectrumAnalyzerTask() {
    xTaskCreatePinnedToCore(
        streamIngestionTask, "StreamIngest", AUDIO_TASK_STACK_SIZE, NULL,
        STREAM_TASK_PRIORITY, &streamIngestTaskHandle, 0   // was NULL
    );

    xTaskCreatePinnedToCore(
        ballisticsTask, "Ballistics", BALLISTICS_TASK_STACK_SIZE, NULL,
        BALLISTICS_TASK_PRIORITY, &ballisticsTaskHandle, 1   // was NULL
    );

    xTaskCreatePinnedToCore(
        spectrumAnalyzerTask, "AudioViewSwitch", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, &spectrumViewTaskHandle, 1   // was NULL
    );
}