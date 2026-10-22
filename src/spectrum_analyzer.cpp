#include "spectrum_analyzer.h"
#include <ctype.h>

SpectrumAnalyzer spectrumAnalyzer;

extern Ld2412Sensor ld2412Sensor;
extern Ld2454Sensor ld2454Sensor;

static const uint32_t INITIAL_SAMPLE_RATE = 44100; // until a stream's WAV header says otherwise

namespace {
const float kBandCenters[24] = {
    31.0f,   63.0f,   80.0f,   100.0f,  125.0f,  160.0f,
    200.0f,  250.0f,  315.0f,  400.0f,  500.0f,  630.0f,
    800.0f,  1000.0f, 1250.0f, 1600.0f, 2000.0f, 2500.0f,
    3150.0f, 4000.0f, 5000.0f, 6300.0f, 8000.0f, 16000.0f
};

const float kBandEdges[25] = {
    22.0f,    44.2f,    70.9f,    89.4f,    111.8f,   141.4f,   178.9f,
    223.6f,   280.6f,   355.0f,   447.2f,   565.7f,   714.1f,
    894.4f,   1118.0f,  1414.2f,  1788.9f,  2236.1f,  2806.2f,
    3549.6f,  4472.1f,  5656.9f,  7141.4f,  11313.7f, 16000.0f
};

float g_bandLogCenter[24];
float g_bandSigmaOct[24];
float g_bandTiltDb[24];

// LED rows added to (dB / SPECTRUM_DB_PER_STEP) so that SPECTRUM_FLOOR_DB lands on row 0.
const float kFloorSteps = -SPECTRUM_FLOOR_DB / SPECTRUM_DB_PER_STEP;

// The library's own Hann class uses the Hamming coefficient (0.54) instead of
// Hann's 0.5 - a bug in the third-party audio-tools implementation. Supply a
// correct one instead of relying on it. The coefficients are tabulated because
// WindowFunction::factor() is called for every sample of every frame.
class SpectrumHannWindow : public WindowFunction {
public:
    const char* name() override { return "SpectrumHann"; }

    void begin(int samples) override {
        if (samples == i_samples && !_table.empty()) return;
        WindowFunction::begin(samples);
        _table.resize(i_half_samples + 1);
        for (int idx = 0; idx <= i_half_samples; idx++) {
            _table[idx] = 0.5f * (1.0f - cosf(twoPi * ratio(idx)));
        }
        _powerSum = 0.0f;
        for (int idx = 0; idx < i_samples; idx++) {
            float w = factor(idx);
            _powerSum += w * w;
        }
    }

    // Sum of w^2 over the window: the mean FFT magnitude of noise scales with its square root.
    float powerSum() const { return _powerSum; }

protected:
    float factor_internal(int idx) override { return _table[idx]; }

private:
    std::vector<float> _table;
    float _powerSum = 0.0f;
};
SpectrumHannWindow g_hannWindowLow;
SpectrumHannWindow g_hannWindowHigh;

// Views that end on their own (timer / scroll count): never a restore target.
bool isTransientView(DisplayView v) {
    return v == VIEW_TEMP_DATA || v == VIEW_CO2 || v == VIEW_CUSTOM_MSG || v == VIEW_CUSTOM_ICON;
}

// Views drawn on top of the current one: spectrum auto-entry waits until they end.
bool isOverlayView(DisplayView v) {
    return v == VIEW_CO2 || v == VIEW_CUSTOM_MSG || v == VIEW_CUSTOM_ICON;
}

int fftStride(uint32_t sampleRate, uint16_t framesPerSecond, uint16_t length) {
    int stride = (int)((sampleRate + framesPerSecond / 2) / framesPerSecond);
    return constrain(stride, 1, (int)length);
}

inline int16_t readLe16(const uint8_t* p) {
    return (int16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}
}

void SpectrumAnalyzer::begin() {
    for (int i = 0; i < 24; i++) {
        g_bandLogCenter[i] = log2f(kBandCenters[i]);
        g_bandSigmaOct[i] = (log2f(kBandEdges[i + 1]) - log2f(kBandEdges[i])) * 0.5f;
        g_bandTiltDb[i] = SPECTRUM_TILT_DB_PER_OCTAVE * log2f(kBandCenters[i] / SPECTRUM_TILT_REFERENCE_HZ);
    }
    configureFfts(INITIAL_SAMPLE_RATE);
}

void SpectrumAnalyzer::configureFfts(uint32_t sampleRate) {
    auto lowCfg = _fftLow.defaultConfig();
    lowCfg.length = SPECTRUM_FFT_LOW_SIZE;
    lowCfg.stride = fftStride(sampleRate, SPECTRUM_FFT_LOW_FRAMES_PER_S, SPECTRUM_FFT_LOW_SIZE);
    lowCfg.sample_rate = sampleRate;
    lowCfg.channels = 1; // multichannel streams are downmixed in writeAudio()
    lowCfg.bits_per_sample = 16;
    lowCfg.window_function_fft = &g_hannWindowLow;
    lowCfg.callback = &SpectrumAnalyzer::fftCallbackLow;
    lowCfg.ref = this;
    _fftLow.begin(lowCfg);

    auto highCfg = _fftHigh.defaultConfig();
    highCfg.length = SPECTRUM_FFT_HIGH_SIZE;
    highCfg.stride = fftStride(sampleRate, SPECTRUM_FFT_HIGH_FRAMES_PER_S, SPECTRUM_FFT_HIGH_SIZE);
    highCfg.sample_rate = sampleRate;
    highCfg.channels = 1;
    highCfg.bits_per_sample = 16;
    highCfg.window_function_fft = &g_hannWindowHigh;
    highCfg.callback = &SpectrumAnalyzer::fftCallbackHigh;
    highCfg.ref = this;
    _fftHigh.begin(highCfg);

    // FFTReal output is unnormalised: the mean bin magnitude of noise grows with
    // sqrt(sum w^2), i.e. ~3 dB more in the 2048- than in the 1024-point FFT. Scale the
    // short FFT to the long one, so the bass bands and the AGC reference (both from the
    // long FFT) keep their level and broadband content no longer steps down at 400/500 Hz.
    buildBandTable(_bandsLow, SPECTRUM_FFT_LOW_SIZE, sampleRate, 0, SPECTRUM_LOW_BAND_COUNT, true, 1.0f);
    buildBandTable(_bandsHigh, SPECTRUM_FFT_HIGH_SIZE, sampleRate, SPECTRUM_LOW_BAND_COUNT, 24, false,
                   sqrtf(g_hannWindowLow.powerSum() / g_hannWindowHigh.powerSum()));
    _sampleRate = sampleRate;
}

// Precomputes what processFftFrame() used to recompute every frame (log2f/expf per
// bin and band) - same expressions, same per-band accumulation order.
void SpectrumAnalyzer::buildBandTable(BandTable& table, int length, uint32_t sampleRate,
                                      int band_start, int band_end, bool isPrimary, float magScale) {
    table.entries.clear();
    for (int i = 0; i < 24; i++) table.weightSum[i] = 0.0f;
    table.firstBin = length;
    table.lastBin = -1;
    table.magScale = magScale;

    const int num_bins = length / 2;
    const float bin_resolution = (float)sampleRate / (float)length;
    for (int bin_idx = 0; bin_idx < num_bins; bin_idx++) {
        float current_freq = bin_idx * bin_resolution;
        if (current_freq < 22.0f || current_freq > 16000.0f) continue;

        bool used = isPrimary; // the primary FFT's frame level averages every in-range bin
        float log_freq = log2f(current_freq);
        for (int band_idx = band_start; band_idx < band_end; band_idx++) {
            float sigma = g_bandSigmaOct[band_idx];
            float delta = fabsf(log_freq - g_bandLogCenter[band_idx]);
            if (delta > sigma * 3.0f) continue;

            float weight = expf(-0.5f * (delta * delta) / (sigma * sigma));
            table.entries.push_back({(uint16_t)bin_idx, (uint8_t)band_idx, weight});
            table.weightSum[band_idx] += weight;
            used = true;
        }
        if (used) {
            if (bin_idx < table.firstBin) table.firstBin = bin_idx;
            table.lastBin = bin_idx;
        }
    }
}

void SpectrumAnalyzer::beginStream(uint32_t sampleRate, uint16_t channels) {
    if (sampleRate != _sampleRate) {
        configureFfts(sampleRate);
    }
    // Samples of the previous connection must not end up in this one's first frames.
    _fftLow.clearInput();
    _fftHigh.clearInput();
    _channels = channels;
    _frameBytes = (uint8_t)(channels * 2);
    _carryLen = 0;
    _rollingAvgRawMag = 0.0f;
    _referenceMag = SPECTRUM_REF_FLOOR_MAG;
    _havePrimaryFrame = false;
    _activeStreak = 0;

    portENTER_CRITICAL(&_stateMux);
    resetActivityLocked();
    portEXIT_CRITICAL(&_stateMux);
}

void SpectrumAnalyzer::setStreamConnected(bool connected) {
    portENTER_CRITICAL(&_stateMux);
    _streamConnected = connected;
    if (!connected) {
        resetActivityLocked();
    }
    portEXIT_CRITICAL(&_stateMux);
    if (!connected) {
        _activeStreak = 0;
    }
}

// Activity of a finished stream must not leak into the next one (a stale "active"
// stamp would hold or re-enter the spectrum view before any new sample arrives).
// Starting asleep also means a new stream has to wake the bars before it counts.
void SpectrumAnalyzer::resetActivityLocked() {
    _hasActiveFrame = false;
    _newLowData = false;
    _newHighData = false;
    _sleepMode = true;
}

int16_t SpectrumAnalyzer::downmixFrame(const uint8_t* frame) const {
    if (_channels == 1) return readLe16(frame);
    int32_t sum = 0;
    for (uint16_t ch = 0; ch < _channels; ch++) {
        sum += readLe16(frame + 2 * ch);
    }
    return (int16_t)(sum / (int32_t)_channels);
}

void SpectrumAnalyzer::feedFfts(const int16_t* samples, size_t count) {
    const uint8_t* bytes = reinterpret_cast<const uint8_t*>(samples);
    _fftLow.write(bytes, count * sizeof(int16_t));
    _fftHigh.write(bytes, count * sizeof(int16_t));
}

size_t SpectrumAnalyzer::writeAudio(const uint8_t* buffer, size_t size) {
    int16_t mono[256];
    size_t count = 0;
    size_t pos = 0;
    while (pos < size) {
        const uint8_t* frame;
        if (_carryLen > 0 || size - pos < _frameBytes) {
            // A TCP read can end mid-frame: keep the partial frame for the next call
            // instead of dropping it, which would shift every following sample.
            size_t take = min((size_t)(_frameBytes - _carryLen), size - pos);
            memcpy(_carry + _carryLen, buffer + pos, take);
            _carryLen += (uint8_t)take;
            pos += take;
            if (_carryLen < _frameBytes) break;
            _carryLen = 0;
            frame = _carry;
        } else {
            frame = buffer + pos;
            pos += _frameBytes;
        }
        mono[count++] = downmixFrame(frame);
        if (count == sizeof(mono) / sizeof(mono[0])) {
            feedFfts(mono, count);
            count = 0;
        }
    }
    if (count > 0) {
        feedFfts(mono, count);
    }
    return size;
}

void SpectrumAnalyzer::fftCallbackLow(AudioFFTBase &fft_ref) {
    SpectrumAnalyzer& self = fft_ref.reference<SpectrumAnalyzer>();
    self.processFftFrame(fft_ref, self._bandsLow, 0, SPECTRUM_LOW_BAND_COUNT, true);
}

void SpectrumAnalyzer::fftCallbackHigh(AudioFFTBase &fft_ref) {
    SpectrumAnalyzer& self = fft_ref.reference<SpectrumAnalyzer>();
    self.processFftFrame(fft_ref, self._bandsHigh, SPECTRUM_LOW_BAND_COUNT, 24, false);
}

void SpectrumAnalyzer::processFftFrame(AudioFFTBase &fft_ref, const BandTable& table, int band_start, int band_end, bool isPrimary) {
    const float* packed = static_cast<AudioRealFFT&>(fft_ref).imgArray();
    const int half = fft_ref.size();

    float band_magnitudes[24] = {0};
    float frame_mag_sum = 0.0f; // sum of bin magnitudes (not energies) over 22 Hz-16 kHz
    int frame_bin_count = 0;

    const BinWeight* entry = table.entries.data();
    const BinWeight* entriesEnd = entry + table.entries.size();
    for (int bin_idx = table.firstBin; bin_idx <= table.lastBin; bin_idx++) {
        float mag_val;
        if (bin_idx == 0) {
            mag_val = fabsf(packed[0]);
        } else {
            float re = packed[bin_idx];
            float im = packed[half + bin_idx];
            mag_val = sqrtf(re * re + im * im);
        }

        if (isPrimary) {
            frame_mag_sum += mag_val;
            frame_bin_count++;
        }

        for (; entry < entriesEnd && entry->bin == bin_idx; entry++) {
            band_magnitudes[entry->band] += (mag_val * entry->weight);
        }
    }

    uint32_t nowMs = millis();
    if (isPrimary) {
        float frame_average_mag = (frame_bin_count > 0) ? (frame_mag_sum / frame_bin_count) : 0.0f;

        float dt = !_havePrimaryFrame ? (1.0f / 30.0f) : (float)(nowMs - _lastPrimaryFrameMs) / 1000.0f;
        dt = constrain(dt, 0.005f, 0.5f);
        _lastPrimaryFrameMs = nowMs;
        _havePrimaryFrame = true;

        float avgAlpha = dt / ((SPECTRUM_SILENCE_AVG_WINDOW_MS / 1000.0f) + dt);
        _rollingAvgRawMag += (frame_average_mag - _rollingAvgRawMag) * avgAlpha;

        // Counted per FFT frame, not per ballistics tick, and the activity stamp below
        // is only taken here: a single loud frame (a click) doesn't count as audio, and
        // an old level can't keep audio "active" once frames stop arriving.
        if (frame_average_mag >= SPECTRUM_ABS_SILENCE_MAG) {
            if (_activeStreak < 255) _activeStreak++;
        } else {
            _activeStreak = 0;
        }

        float refTarget = fmaxf(frame_average_mag, SPECTRUM_REF_FLOOR_MAG);
        if (refTarget > _referenceMag) {
            _referenceMag += (refTarget - _referenceMag) * SPECTRUM_REF_RISE_ALPHA;
        } else {
            _referenceMag += (refTarget - _referenceMag) * SPECTRUM_REF_FALL_ALPHA;
        }
        if (_referenceMag < SPECTRUM_REF_FLOOR_MAG) _referenceMag = SPECTRUM_REF_FLOOR_MAG;
    }

    float levels[24];
    for (int i = band_start; i < band_end; i++) {
        float avg_magnitude = (table.weightSum[i] > 0.0f) ? (band_magnitudes[i] / table.weightSum[i]) : 0.0f;
        avg_magnitude *= table.magScale;
        float bandDb = (avg_magnitude > 1e-6f)
            ? 20.0f * log10f(avg_magnitude / (_referenceMag * SPECTRUM_REF_SCALE))
            : -200.0f;

        bandDb += g_bandTiltDb[i];

        float compressedDb = bandDb;
        if (compressedDb > SPECTRUM_COMPRESSION_KNEE_DB) {
            float over = compressedDb - SPECTRUM_COMPRESSION_KNEE_DB;
            compressedDb = SPECTRUM_COMPRESSION_KNEE_DB + (over / SPECTRUM_COMPRESSION_RATIO);
        }
        float targetSteps = (compressedDb / SPECTRUM_DB_PER_STEP) + kFloorSteps;
        levels[i] = constrain(targetSteps, 0.0f, BALLISTICS_MAX_LEVEL);
    }

    portENTER_CRITICAL(&_stateMux);
    bool& pending = isPrimary ? _newLowData : _newHighData;
    for (int i = band_start; i < band_end; i++) {
        // Frames that arrive in a TCP burst can outpace the 10 ms ballistics tick:
        // keep the loudest one until it is consumed instead of only the last.
        _pendingLevel[i] = pending ? fmaxf(_pendingLevel[i], levels[i]) : levels[i];
    }
    pending = true;
    if (isPrimary) {
        _lastFrameMs = nowMs;
        // SPECTRUM_ACTIVE_MIN_FRAMES in a row start activity; once active, any loud
        // frame keeps it (quiet passages are released by the bar fade instead).
        bool stillActive = _hasActiveFrame && (nowMs - _lastActiveFrameMs) < AUDIO_SILENCE_TIMEOUT_MS;
        if (_activeStreak >= SPECTRUM_ACTIVE_MIN_FRAMES || (_activeStreak > 0 && stillActive)) {
            _hasActiveFrame = true;
            _lastActiveFrameMs = nowMs;
        }
        if (_sleepMode && _rollingAvgRawMag >= SPECTRUM_SLEEP_EXIT_MAG) {
            _sleepMode = false;
        } else if (!_sleepMode && _rollingAvgRawMag < SPECTRUM_SLEEP_ENTER_MAG) {
            _sleepMode = true;
        }
    }
    portEXIT_CRITICAL(&_stateMux);
}

bool SpectrumAnalyzer::isAudioActive() {
    portENTER_CRITICAL(&_stateMux);
    bool connected = _streamConnected;
    bool hasActiveFrame = _hasActiveFrame;
    uint32_t lastActiveFrameMs = _lastActiveFrameMs;
    portEXIT_CRITICAL(&_stateMux);
    portENTER_CRITICAL(&_ballisticsMux);
    bool barsFaded = _barsFaded;
    portEXIT_CRITICAL(&_ballisticsMux);
    // Read after the snapshot, so no timestamp in it is newer and the subtraction can't wrap.
    uint32_t nowMs = millis();

    // Same criterion as the bars: once they have faded out (quiet content with only
    // the odd loud frame, or a new stream that hasn't woken them yet), the spectrum
    // view isn't held or entered either.
    return connected && hasActiveFrame && !barsFaded &&
           (nowMs - lastActiveFrameMs) < AUDIO_SILENCE_TIMEOUT_MS;
}

void SpectrumAnalyzer::updateAudio() {
    bool audioActive = isAudioActive();
    DisplayView currentView = DisplayManager::getCurrentView();
    uint32_t nowMs = millis();
    bool presenceDetected = ld2412Sensor.isPresent() || ld2454Sensor.hasAnyTarget();

    // A spectrum session lasts from entering spectrum (automatically or by the user)
    // until another base view takes over; a message/icon/CO2 popup on top of it and
    // returning to it doesn't end it.
    if (currentView == VIEW_SPECTRUM) {
        if (!_spectrumShown) {
            // Selected by the user (button or MQTT): show it
            // for the minimum time from now, then fall back to the clock if silent.
            _spectrumShown = true;
            _spectrumEnteredMs = nowMs;
            _previousView = VIEW_TIME;
        }
    } else if (!isOverlayView(currentView)) {
        _spectrumShown = false;
    }

    if (!audioActive) {
        if (_wasAudioActive) {
            _silenceStartMs = nowMs;
        }
        _pendingSpectrumEntry = false;
        bool minDisplayElapsed = (nowMs - _spectrumEnteredMs) >= MIN_SPECTRUM_DISPLAY_MS;
        bool longSilence = (nowMs - _silenceStartMs) >= SPECTRUM_PRESENCE_OVERRIDE_MS;

        if (currentView == VIEW_SPECTRUM && minDisplayElapsed && (!presenceDetected || longSilence)) {
            // Compare-and-set: a message/icon that arrived since getCurrentView() stays.
            if (DisplayManager::trySetView(VIEW_SPECTRUM, _previousView)) {
                _spectrumShown = false;
            }
        }
    } else if (currentView == VIEW_SPECTRUM) {
        _pendingSpectrumEntry = false;
    } else if (isOverlayView(currentView)) {
        _pendingSpectrumEntry = true; // enter once the message/icon/popup is over
    } else if (!_wasAudioActive || _pendingSpectrumEntry) {
        if (DisplayManager::isSpectrumAutoEntrySuppressed()) {
            _pendingSpectrumEntry = false; // the user picked another view: nothing to defer
        } else if (DisplayManager::trySetView(currentView, VIEW_SPECTRUM)) {
            _previousView = isTransientView(currentView) ? VIEW_TIME : currentView;
            _spectrumEnteredMs = nowMs;
            _spectrumShown = true;
            _pendingSpectrumEntry = false;
        } else {
            _pendingSpectrumEntry = true; // lost a race with a message/icon/popup: retry
        }
    }

    _wasAudioActive = audioActive;
}

void SpectrumAnalyzer::getBarsAndPeaks(uint8_t* outBars, uint8_t* outPeaks) {
    portENTER_CRITICAL(&_ballisticsMux);
    memcpy(outBars, _sharedBars, 24);
    memcpy(outPeaks, _sharedPeaks, 24);
    portEXIT_CRITICAL(&_ballisticsMux);
}

void SpectrumAnalyzer::runBallisticsTick() {
    static const float kBallisticsTickSec = BALLISTICS_TICK_MS / 1000.0f;

    float levels[24];
    portENTER_CRITICAL(&_stateMux);
    bool newLowData = _newLowData;
    bool newHighData = _newHighData;
    _newLowData = false;
    _newHighData = false;
    memcpy(levels, _pendingLevel, sizeof(levels));
    bool silent = _sleepMode || !_streamConnected;
    uint32_t lastFrameMs = _lastFrameMs;
    portEXIT_CRITICAL(&_stateMux);
    uint32_t nowMs = millis(); // after the snapshot, see isAudioActive()

    // No fresh FFT frame (stalled or closed stream) counts as silence too.
    if ((nowMs - lastFrameMs) > SPECTRUM_FRAME_STALE_MS) silent = true;

    // Fade from the current visibility in either direction, so a level hovering near
    // the sleep thresholds can't snap the bars on or restart the fade from full.
    if (silent) {
        _visibility = fmaxf(0.0f, _visibility - (float)BALLISTICS_TICK_MS / (float)SPECTRUM_SLEEP_FADE_MS);
    } else {
        _visibility = fminf(1.0f, _visibility + (float)BALLISTICS_TICK_MS / (float)SPECTRUM_WAKE_FADE_MS);
    }
    const float visibility = _visibility;

    uint8_t localBars[24];
    uint8_t localPeaks[24];

    for (int i = 0; i < 24; i++) {
        BandBallistics &b = _ballistics[i];

        bool newDataForBand = (i < SPECTRUM_LOW_BAND_COUNT) ? newLowData : newHighData;
        float target = newDataForBand ? levels[i] : (b.current_level * BALLISTICS_DECAY_COEFF);

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

    portENTER_CRITICAL(&_ballisticsMux);
    memcpy(_sharedBars, localBars, sizeof(localBars));
    memcpy(_sharedPeaks, localPeaks, sizeof(localPeaks));
    _barsFaded = (visibility <= 0.0f);
    portEXIT_CRITICAL(&_ballisticsMux);
}

namespace {
struct StreamUrl {
    char host[64];
    char hostHeader[72]; // host[:port] for the Host header
    char path[128];
    uint16_t port;
};

void parseStreamUrl(const char* url, StreamUrl& out) {
    if (strncmp(url, "http://", 7) == 0) url += 7;
    const char* slash = strchr(url, '/');
    size_t hostLen = slash ? (size_t)(slash - url) : strlen(url);
    if (hostLen >= sizeof(out.host)) hostLen = sizeof(out.host) - 1;
    memcpy(out.host, url, hostLen);
    out.host[hostLen] = '\0';
    strncpy(out.path, slash ? slash : "/", sizeof(out.path) - 1);
    out.path[sizeof(out.path) - 1] = '\0';

    out.port = 80;
    char* colon = strchr(out.host, ':');
    if (colon) {
        int port = atoi(colon + 1);
        if (port > 0 && port <= 65535) out.port = (uint16_t)port;
        *colon = '\0';
    }
    if (out.port == 80) {
        snprintf(out.hostHeader, sizeof(out.hostHeader), "%s", out.host);
    } else {
        snprintf(out.hostHeader, sizeof(out.hostHeader), "%s:%u", out.host, (unsigned)out.port);
    }
}

// Time budget for one phase of a connection (millis()-wrap safe).
struct Deadline {
    uint32_t startMs;
    uint32_t spanMs;
    explicit Deadline(uint32_t span) : startMs(millis()), spanMs(span) {}
    bool expired() const { return (millis() - startMs) >= spanMs; }
};

int hexDigit(uint8_t c) {
    if (c >= '0' && c <= '9') return c - '0';
    c |= 0x20; // lower case
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

// The HTTP body without Transfer-Encoding: chunked framing. ffmpeg's built-in server
// (-listen 1) always sends chunked, even for an HTTP/1.0 request; the framing would
// otherwise end up in the WAV header and the samples. read() keeps WiFiClient's
// contract: >0 bytes, 0 = nothing available yet, <0 = closed, failed or body ended.
struct BodyReader {
    WiFiClient& client;
    bool chunked = false;
    bool ended = false;      // the 0-size last chunk arrived
    bool badFraming = false;
    uint32_t chunkLeft = 0;  // payload bytes left in the current chunk
    uint32_t sizeValue = 0;  // chunk-size line being parsed: "<hex>[;ext]\r\n"
    uint8_t sizeDigits = 0;
    bool inExtension = false;

    explicit BodyReader(WiFiClient& c) : client(c) {}

    int read(uint8_t* buf, size_t len) {
        if (!chunked) return client.read(buf, len);
        while (chunkLeft == 0) {
            if (ended || badFraming) return -1;
            uint8_t c;
            int n = client.read(&c, 1);
            if (n <= 0) return n;
            parseSizeLine(c);
        }
        int n = client.read(buf, (len < chunkLeft) ? len : (size_t)chunkLeft);
        if (n > 0) chunkLeft -= (uint32_t)n;
        return n;
    }

    // The CRLF that closes the previous chunk's payload is skipped as an empty line.
    void parseSizeLine(uint8_t c) {
        if (c == '\n') {
            if (sizeDigits == 0) return;
            ended = (sizeValue == 0); // trailers after the last chunk are never read
            chunkLeft = sizeValue;
            sizeValue = 0;
            sizeDigits = 0;
            inExtension = false;
        } else if (c == '\r' || inExtension) {
            // line end, or a ";name=value" chunk extension: ignored
        } else if (sizeDigits > 0 && (c == ';' || c == ' ' || c == '\t')) {
            inExtension = true;
        } else {
            int digit = hexDigit(c);
            if (digit < 0 || sizeDigits == 8) {
                badFraming = true;
                return;
            }
            sizeValue = (sizeValue << 4) | (uint32_t)digit;
            sizeDigits++;
        }
    }
};

// Called when a read returned nothing. False once the peer has closed or an OTA pause
// happened (the connection is stale after one).
bool waitForData(WiFiClient& client) {
    if (!client.connected()) return false;
    vTaskDelay(pdMS_TO_TICKS(1));
    return !waitWhileOtaPaused();
}

// The deadline is checked before every read, not only while the socket is idle: a peer
// that keeps sending but never ends a line (e.g. a raw PCM socket) must not hold the
// header phase open.
bool readExact(BodyReader& body, uint8_t* buf, size_t len, const Deadline& deadline) {
    size_t got = 0;
    while (got < len) {
        if (deadline.expired()) return false;
        int n = body.read(buf + got, len - got);
        if (n > 0) {
            got += (size_t)n;
        } else if (n < 0 || !waitForData(body.client)) {
            return false;
        }
    }
    return true;
}

bool skipBytes(BodyReader& body, size_t len, const Deadline& deadline) {
    uint8_t scratch[64];
    while (len > 0) {
        size_t chunk = (len < sizeof(scratch)) ? len : sizeof(scratch);
        if (!readExact(body, scratch, chunk, deadline)) return false;
        len -= chunk;
    }
    return true;
}

// One header line without CR/LF; an over-long line is truncated but fully consumed.
bool readLine(BodyReader& body, char* line, size_t cap, const Deadline& deadline) {
    size_t len = 0;
    for (;;) {
        uint8_t c;
        if (!readExact(body, &c, 1, deadline)) return false;
        if (c == '\n') break;
        if (c != '\r' && len + 1 < cap) line[len++] = (char)c;
    }
    line[len] = '\0';
    return true;
}

// "HTTP/1.x 200 ..." or Shoutcast's "ICY 200 OK".
bool isOkStatusLine(const char* line) {
    const char* code = nullptr;
    if (strncmp(line, "HTTP/1.", 7) == 0 && line[7] != '\0' && line[8] == ' ') {
        code = line + 9;
    } else if (strncmp(line, "ICY ", 4) == 0) {
        code = line + 4;
    }
    return code != nullptr && strncmp(code, "200", 3) == 0 && (code[3] == ' ' || code[3] == '\0');
}

uint16_t le16(const uint8_t* p) {
    return (uint16_t)(p[0] | (p[1] << 8));
}

uint32_t le32(const uint8_t* p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

struct WavFormat {
    uint16_t formatTag = 0;
    uint16_t channels = 0;
    uint32_t sampleRate = 0;
    uint16_t blockAlign = 0;
    uint16_t bitsPerSample = 0;
};

// Walks the RIFF chunks up to 'data' (ffmpeg adds a LIST chunk, others JUNK/bext/...)
// and leaves the socket at the first audio byte.
bool readWavHeader(BodyReader& body, const Deadline& deadline, WavFormat& fmt, char* why, size_t whyLen) {
    uint8_t riff[12];
    if (!readExact(body, riff, sizeof(riff), deadline)) {
        snprintf(why, whyLen, "no WAV header after the HTTP headers");
        return false;
    }
    if ((memcmp(riff, "RIFF", 4) != 0 && memcmp(riff, "RF64", 4) != 0) || memcmp(riff + 8, "WAVE", 4) != 0) {
        snprintf(why, whyLen, "not a WAV stream (no RIFF/WAVE header)");
        return false;
    }

    bool haveFmt = false;
    uint32_t headerBytes = sizeof(riff);
    for (;;) {
        uint8_t chunk[8];
        if (!readExact(body, chunk, sizeof(chunk), deadline)) {
            snprintf(why, whyLen, "truncated WAV header");
            return false;
        }
        headerBytes += sizeof(chunk);
        if (memcmp(chunk, "data", 4) == 0) {
            if (haveFmt) return true; // its size is meaningless for a live stream
            snprintf(why, whyLen, "WAV data chunk before the fmt chunk");
            return false;
        }

        uint32_t size = le32(chunk + 4);
        if (size > STREAM_WAV_MAX_HEADER_BYTES || headerBytes + size + (size & 1) > STREAM_WAV_MAX_HEADER_BYTES) {
            snprintf(why, whyLen, "WAV header longer than %lu bytes", (unsigned long)STREAM_WAV_MAX_HEADER_BYTES);
            return false;
        }
        uint32_t padded = size + (size & 1); // chunks are word aligned

        if (memcmp(chunk, "fmt ", 4) == 0) {
            if (size < 16) {
                snprintf(why, whyLen, "WAV fmt chunk too short");
                return false;
            }
            uint8_t f[40];
            size_t take = (padded < sizeof(f)) ? padded : sizeof(f);
            if (!readExact(body, f, take, deadline) || !skipBytes(body, padded - take, deadline)) {
                snprintf(why, whyLen, "truncated WAV header");
                return false;
            }
            fmt.formatTag = le16(f);
            if (fmt.formatTag == 0xFFFE && size >= 26) {
                fmt.formatTag = le16(f + 24); // WAVE_FORMAT_EXTENSIBLE: the sub-format GUID starts with the tag
            }
            fmt.channels = le16(f + 2);
            fmt.sampleRate = le32(f + 4);
            fmt.blockAlign = le16(f + 12);
            fmt.bitsPerSample = le16(f + 14);
            haveFmt = true;
        } else if (!skipBytes(body, padded, deadline)) {
            snprintf(why, whyLen, "truncated WAV header");
            return false;
        }
        headerBytes += padded;
    }
}

bool isSupportedFormat(const WavFormat& fmt) {
    return fmt.formatTag == 1 && fmt.bitsPerSample == 16 &&
           fmt.channels >= 1 && fmt.channels <= STREAM_MAX_CHANNELS &&
           fmt.blockAlign == fmt.channels * 2 &&
           fmt.sampleRate >= STREAM_MIN_SAMPLE_RATE && fmt.sampleRate <= STREAM_MAX_SAMPLE_RATE;
}

// Log lines without repeating the same one on every retry.
char g_lastStreamFormat[48] = "";
char g_lastStreamProblem[160] = "";

void logStreamFormat(const WavFormat& fmt) {
    char msg[sizeof(g_lastStreamFormat)];
    snprintf(msg, sizeof(msg), "%lu Hz, %u ch, 16-bit PCM", (unsigned long)fmt.sampleRate, (unsigned)fmt.channels);
    if (strcmp(msg, g_lastStreamFormat) != 0) {
        Serial.printf("[Audio] Streaming %s%s\n", msg, fmt.channels > 1 ? " (downmixed to mono)" : "");
        strlcpy(g_lastStreamFormat, msg, sizeof(g_lastStreamFormat));
    }
}

void logStreamProblem(const char* why, bool failed) {
    if (why[0] != '\0' && strcmp(why, g_lastStreamProblem) != 0) {
        Serial.printf("[Audio] %s\n", why);
        strlcpy(g_lastStreamProblem, why, sizeof(g_lastStreamProblem));
    }
    if (failed) g_lastStreamFormat[0] = '\0'; // report the stream again once it recovers
}

enum StreamResult {
    STREAM_FAILED, // connection/protocol/format problem: retry with growing backoff
    STREAM_ENDED   // a valid stream ended or went quiet: retry soon
};

StreamResult runStreamSession(WiFiClient& client, const StreamUrl& url, char* why, size_t whyLen) {
    if (!client.connect(url.host, url.port, STREAM_CONNECT_TIMEOUT_MS)) {
        snprintf(why, whyLen, "cannot connect to %s", url.hostHeader);
        return STREAM_FAILED;
    }

    // HTTP/1.0, so compliant servers send the body as-is; a chunked reply (ffmpeg's
    // server always sends one) is decoded by BodyReader.
    char request[320];
    snprintf(request, sizeof(request),
             "GET %s HTTP/1.0\r\n"
             "Host: %s\r\n"
             "User-Agent: ESP32-S3-Analyser\r\n"
             "Accept: */*\r\n"
             "Connection: close\r\n\r\n",
             url.path, url.hostHeader);
    client.print(request);

    BodyReader body(client); // plain reads until the headers are through
    Deadline headerDeadline(STREAM_HEADER_TIMEOUT_MS);
    char line[160];
    if (!readLine(body, line, sizeof(line), headerDeadline)) {
        snprintf(why, whyLen, "no HTTP response from %s", url.hostHeader);
        return STREAM_FAILED;
    }
    if (!isOkStatusLine(line)) {
        snprintf(why, whyLen, "server answered \"%.60s\"", line);
        return STREAM_FAILED;
    }
    bool chunked = false;
    for (;;) {
        if (!readLine(body, line, sizeof(line), headerDeadline)) {
            snprintf(why, whyLen, "truncated HTTP headers");
            return STREAM_FAILED;
        }
        if (line[0] == '\0') break;
        for (char* p = line; *p != '\0'; p++) *p = (char)tolower((unsigned char)*p);
        if (strncmp(line, "transfer-encoding:", 18) == 0 && strstr(line, "chunked") != nullptr) {
            chunked = true;
        }
    }
    body.chunked = chunked; // the framing starts right after the blank line

    WavFormat fmt;
    if (!readWavHeader(body, headerDeadline, fmt, why, whyLen)) {
        if (body.badFraming) snprintf(why, whyLen, "malformed chunked transfer encoding");
        return STREAM_FAILED;
    }
    if (!isSupportedFormat(fmt)) {
        snprintf(why, whyLen, "unsupported WAV format (tag %u, %u ch, %u-bit, %lu Hz) - need 16-bit PCM",
                 (unsigned)fmt.formatTag, (unsigned)fmt.channels, (unsigned)fmt.bitsPerSample,
                 (unsigned long)fmt.sampleRate);
        return STREAM_FAILED;
    }
    spectrumAnalyzer.beginStream(fmt.sampleRate, fmt.channels);
    logStreamFormat(fmt);

    uint8_t buf[512];
    uint32_t lastRxMs = millis();
    uint32_t burstBytes = 0;
    bool flowing = false;
    for (;;) {
        if (waitWhileOtaPaused()) {
            snprintf(why, whyLen, "stream paused for an OTA update - reconnecting");
            return STREAM_ENDED;
        }
        int n = body.read(buf, sizeof(buf));
        if (n > 0) {
            lastRxMs = millis();
            spectrumAnalyzer.writeAudio(buf, (size_t)n);
            if (!flowing) {
                flowing = true;
                spectrumAnalyzer.setStreamConnected(true);
                g_lastStreamProblem[0] = '\0'; // the next problem is news again
            }
            // Drain what has arrived before sleeping: one buffer per 1 ms tick would cap
            // ingestion at 512 kB/s, below multichannel streams at 44.1/48 kHz.
            burstBytes += (uint32_t)n;
            if (burstBytes < STREAM_READ_BURST_BYTES) continue;
        } else if (body.badFraming) {
            snprintf(why, whyLen, "malformed chunked transfer encoding");
            return STREAM_FAILED;
        } else if (n < 0 || !client.connected()) {
            snprintf(why, whyLen, body.ended ? "stream ended" : "stream connection closed");
            return flowing ? STREAM_ENDED : STREAM_FAILED;
        } else if ((millis() - lastRxMs) >= STREAM_RX_TIMEOUT_MS) {
            // A live stream never pauses this long: the socket is stalled or half-open.
            snprintf(why, whyLen, "no audio data for %lu s - reconnecting", (unsigned long)(STREAM_RX_TIMEOUT_MS / 1000));
            return STREAM_ENDED;
        }
        burstBytes = 0;
        vTaskDelay(pdMS_TO_TICKS(1));
    }
}
}

void streamIngestionTask(void* pvParameters) {
    WiFiClient client;
    StreamUrl url;
    parseStreamUrl(AUDIO_STREAM_URL, url);

    uint32_t retryDelayMs = STREAM_RETRY_MIN_MS;
    char why[sizeof(g_lastStreamProblem)];

    for (;;) {
        waitWhileOtaPaused();
        if (WiFi.status() != WL_CONNECTED) {
            vTaskDelay(pdMS_TO_TICKS(500)); // not the server's fault: don't grow the backoff
            continue;
        }

        why[0] = '\0';
        StreamResult result = runStreamSession(client, url, why, sizeof(why));
        client.stop();
        spectrumAnalyzer.setStreamConnected(false);
        logStreamProblem(why, result == STREAM_FAILED);

        uint32_t delayMs = STREAM_RETRY_MIN_MS;
        if (result == STREAM_FAILED) {
            delayMs = retryDelayMs;
            retryDelayMs = min(retryDelayMs * 2, STREAM_RETRY_MAX_MS);
        } else {
            retryDelayMs = STREAM_RETRY_MIN_MS;
        }
        vTaskDelay(pdMS_TO_TICKS(delayMs));
    }
}

void ballisticsTask(void* pvParameters) {
    TickType_t xLastWakeTime = xTaskGetTickCount();
    const TickType_t xFrequency = pdMS_TO_TICKS(BALLISTICS_TICK_MS);

    for (;;) {
        if (waitWhileOtaPaused()) {
            xLastWakeTime = xTaskGetTickCount(); // don't replay the ticks missed while paused
        }
        vTaskDelayUntil(&xLastWakeTime, xFrequency);
        spectrumAnalyzer.runBallisticsTick();
    }
}

void spectrumAnalyzerTask(void* pvParameters) {
    for (;;) {
        waitWhileOtaPaused();
        spectrumAnalyzer.updateAudio();
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

void initSpectrumAnalyzerTask() {
    // Before the tasks exist: StreamIngest (core 0) feeds the FFTs as soon as it runs,
    // and task creation orders this setup before anything they do.
    spectrumAnalyzer.begin();

    xTaskCreatePinnedToCore(
        streamIngestionTask, "StreamIngest", AUDIO_TASK_STACK_SIZE, NULL,
        STREAM_TASK_PRIORITY, NULL, 0
    );

    xTaskCreatePinnedToCore(
        ballisticsTask, "Ballistics", BALLISTICS_TASK_STACK_SIZE, NULL,
        BALLISTICS_TASK_PRIORITY, NULL, 1
    );

    xTaskCreatePinnedToCore(
        spectrumAnalyzerTask, "AudioViewSwitch", SENSOR_TASK_STACK_SIZE, NULL,
        SENSOR_TASK_PRIORITY, NULL, 1
    );
}