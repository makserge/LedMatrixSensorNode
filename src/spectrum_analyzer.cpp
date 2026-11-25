#include "spectrum_analyzer.h"
#include <ctype.h>

SpectrumAnalyzer spectrumAnalyzer;

extern Ld2412Sensor ld2412Sensor;
extern Ld2454Sensor ld2454Sensor;

static const uint32_t INITIAL_SAMPLE_RATE = 44100; // until a stream's WAV header says otherwise

namespace {
// The window of Web Audio's AnalyserNode: Blackman, alpha 0.16 (sd_blackman). The
// coefficients are tabulated because WindowFunction::factor() is called for every sample of every frame.
class SpectrumBlackmanWindow : public WindowFunction {
public:
    const char* name() override { return "SpectrumBlackman"; }

    void begin(int samples) override {
        if (samples == i_samples && !_table.empty()) return;
        WindowFunction::begin(samples);
        _table.resize(i_half_samples + 1);
        for (int idx = 0; idx <= i_half_samples; idx++) {
            _table[idx] = sd_blackman(ratio(idx));
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
SpectrumBlackmanWindow g_windowLow;
SpectrumBlackmanWindow g_windowHigh;

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

// |X[bin]| of FFTReal's packed output: real parts first, imaginary parts from 'half' on (bin 0 has none).
inline float binMagnitude(const float* packed, int half, int bin) {
    if (bin == 0) return fabsf(packed[0]);
    float re = packed[bin];
    float im = packed[half + bin];
    return sqrtf(re * re + im * im);
}

// Pushes the bins of one FFT frame through the dsp module (scale, smoothing, dB per bin) and reads the bands'
// weighted dB values back, in the plan's band order. Returns the number of bands of the plan.
int measureBands(sd_fft_t& plan, const float* packed, int half, float* bandDb) {
    for (int bin = plan.firstBin; bin <= plan.lastBin; bin++) {
        sd_fft_bin(&plan, bin, binMagnitude(packed, half, bin));
    }
    const int bands = plan.nBands;
    for (int j = 0; j < bands; j++) {
        bandDb[j] = sd_fft_band_db(&plan, j);
    }
    return bands;
}

// One tick of the sleep fade: towards 0 while silent, towards 1 otherwise, from wherever it is.
float stepVisibility(float visibility, bool silent) {
    if (silent) {
        return fmaxf(0.0f, visibility - (float)BALLISTICS_TICK_MS / (float)SPECTRUM_SLEEP_FADE_MS);
    }
    return fminf(1.0f, visibility + (float)BALLISTICS_TICK_MS / (float)SPECTRUM_WAKE_FADE_MS);
}

struct ColumnRows {
    uint8_t bar;   // lit rows
    uint8_t peak;  // row + 1 of the peak dot, 0 = none
};

// Rows of one column from the bar's held level and its peak marker, both scaled by the fade.
ColumnRows columnRows(float heldLevel, const sd_peak_t& marker, float visibility) {
    const int bar = sd_bar_rows(heldLevel * visibility);
    int peak = sd_bar_rows(sd_peak_level(&marker) * visibility);
    if (bar == 0 && peak <= 1) peak = 0; // a dot on the bottom row of an empty column would just flicker
    ColumnRows rows;
    rows.bar = (uint8_t)bar;
    rows.peak = (uint8_t)peak;
    return rows;
}
}

void SpectrumAnalyzer::begin() {
    for (int i = 0; i < SD_BANDS; i++) {
        sd_peak_reset(&_peak[i]);
        _pendingDb[i] = SD_SILENCE_DB;
        _valueDb[i] = SD_SILENCE_DB;
    }
    sd_agc_reset(&_agc, 1000.0f / (float)BALLISTICS_TICK_MS);
    sd_fft_setup(&_planLow, SPECTRUM_FFT_LOW_SIZE, SPECTRUM_FFT_LOW_FRAMES_PER_S, _smoothLow, _dbLow);
    sd_fft_setup(&_planHigh, SPECTRUM_FFT_HIGH_SIZE, SPECTRUM_FFT_HIGH_FRAMES_PER_S, _smoothHigh, _dbHigh);
    configureFfts(INITIAL_SAMPLE_RATE);
}

void SpectrumAnalyzer::configureFfts(uint32_t sampleRate) {
    auto lowCfg = _fftLow.defaultConfig();
    lowCfg.length = SPECTRUM_FFT_LOW_SIZE;
    lowCfg.stride = fftStride(sampleRate, SPECTRUM_FFT_LOW_FRAMES_PER_S, SPECTRUM_FFT_LOW_SIZE);
    lowCfg.sample_rate = sampleRate;
    lowCfg.channels = 1; // multichannel streams are downmixed in writeAudio()
    lowCfg.bits_per_sample = 16;
    lowCfg.window_function_fft = &g_windowLow;
    lowCfg.callback = &SpectrumAnalyzer::fftCallbackLow;
    lowCfg.ref = this;
    _fftLow.begin(lowCfg);

    auto highCfg = _fftHigh.defaultConfig();
    highCfg.length = SPECTRUM_FFT_HIGH_SIZE;
    highCfg.stride = fftStride(sampleRate, SPECTRUM_FFT_HIGH_FRAMES_PER_S, SPECTRUM_FFT_HIGH_SIZE);
    highCfg.sample_rate = sampleRate;
    highCfg.channels = 1;
    highCfg.bits_per_sample = 16;
    highCfg.window_function_fft = &g_windowHigh;
    highCfg.callback = &SpectrumAnalyzer::fftCallbackHigh;
    highCfg.ref = this;
    _fftHigh.begin(highCfg);

    // FFTReal output is unnormalised and its input is scaled to +-1.0. A band's level is |X| / N in dBFS with
    // N = 2048 for both FFTs (spectrum_dsp.h). The mean bin magnitude of noise grows with sqrt(sum w^2), i.e. ~3 dB
    // more in the 2048- than in the 1024-point FFT, so the short FFT is scaled to the long one's noise level:
    // broadband content then doesn't step at SPECTRUM_SPLIT_HZ. A pure tone in the short FFT reads about 3 dB
    // lower than in the long one.
    _planLow.scale = 1.0f / (float)SPECTRUM_FFT_LOW_SIZE;
    _planHigh.scale = sqrtf(g_windowLow.powerSum() / g_windowHigh.powerSum()) / (float)SPECTRUM_FFT_LOW_SIZE;
    // Activity thresholds (SPECTRUM_ABS_SILENCE_MAG ...) were tuned with a Hann window, whose noise level is
    // sqrt(3N/8 / sum w^2) times the Blackman window's.
    _activityScale = sqrtf(0.375f * (float)SPECTRUM_FFT_LOW_SIZE / g_windowLow.powerSum());

    _sampleRate = sampleRate;
    _planDirty = true; // band tables follow the sample rate
}

// Called at the start of every FFT frame (StreamIngest): rebuilds the band plans when the sample rate
// has changed.
void SpectrumAnalyzer::refreshPlans() {
    if (!_planDirty) return;
    rebuildPlans();
    _planDirty = false;
}

// Not in the hot loop: runs when the sample rate changes (a few ms).
void SpectrumAnalyzer::rebuildPlans() {
    const float sampleRate = (float)_sampleRate;

    // Bands, bin maps, tilt, smoothing factors; also clears the per-bin state of both FFTs.
    if (sd_plan_bands(&_planLow, &_planHigh, sampleRate, SPECTRUM_SPLIT_HZ) != SD_BANDS) {
        _planLow.nBands = 0;   // refused (cannot happen for a validated rate): no band is fed
        _planHigh.nBands = 0;
    }

    // The activity measure keeps its 22 Hz - 16 kHz mean over the long FFT.
    const float binWidthHz = sampleRate / (float)SPECTRUM_FFT_LOW_SIZE;
    _actFirstBin = (int)ceilf(22.0f / binWidthHz);
    _actLastBin = min((int)floorf(16000.0f / binWidthHz), SPECTRUM_FFT_LOW_SIZE / 2 - 1);

    // Values of the previous layout mean nothing now.
    portENTER_CRITICAL(&_stateMux);
    for (int i = 0; i < SD_BANDS; i++) _pendingDb[i] = SD_SILENCE_DB;
    memset(_newBand, 0, sizeof(_newBand));
    portEXIT_CRITICAL(&_stateMux);
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
    for (int i = 0; i < SD_BANDS; i++) _newBand[i] = false;
    _sleepMode = true;
    _frameQuiet = true;
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
    self.processFftFrame(fft_ref, self._planLow, true);
}

void SpectrumAnalyzer::fftCallbackHigh(AudioFFTBase &fft_ref) {
    SpectrumAnalyzer& self = fft_ref.reference<SpectrumAnalyzer>();
    self.processFftFrame(fft_ref, self._planHigh, false);
}

// One FFT frame (StreamIngest, inside FFT.write()). Per bin the spectrum_dsp module scales the magnitude,
// smooths it and converts it to dB; per band it then gives the weighted dB value (spectrum_dsp.h). The automatic
// gain turns those into levels 0..1 in runBallisticsTick().
void SpectrumAnalyzer::processFftFrame(AudioFFTBase &fft_ref, sd_fft_t& plan, bool isPrimary) {
    refreshPlans();
    if (!isPrimary && plan.nBands == 0) return;

    const float* packed = static_cast<AudioRealFFT&>(fft_ref).imgArray();
    const int half = fft_ref.size();
    uint32_t nowMs = millis();

    float frame_average_mag = 0.0f;
    if (isPrimary) {
        float frame_mag_sum = 0.0f; // sum of bin magnitudes (not energies) over 22 Hz-16 kHz
        int frame_bin_count = 0;
        for (int bin_idx = _actFirstBin; bin_idx <= _actLastBin; bin_idx++) {
            frame_mag_sum += binMagnitude(packed, half, bin_idx);
            frame_bin_count++;
        }
        frame_average_mag = (frame_bin_count > 0) ? (frame_mag_sum / frame_bin_count) * _activityScale : 0.0f;

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
    }

    float bandDb[SD_BANDS] = {0};
    const int bandCount = measureBands(plan, packed, half, bandDb);

    portENTER_CRITICAL(&_stateMux);
    for (int j = 0; j < bandCount; j++) {
        const int column = plan.bandIndex[j];
        // Frames that arrive in a TCP burst can outpace the 10 ms ballistics tick:
        // keep the loudest one until it is consumed instead of only the last.
        if (_newBand[column]) {
            _pendingDb[column] = fmaxf(_pendingDb[column], bandDb[j]);
        } else {
            _pendingDb[column] = bandDb[j];
            _newBand[column] = true;
        }
    }
    if (isPrimary) {
        _lastFrameMs = nowMs;
        // A frame that could not keep the bars awake must not pull the automatic gain down either.
        _frameQuiet = frame_average_mag < SPECTRUM_SLEEP_ENTER_MAG;
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
    memcpy(outBars, _sharedBars, SD_BANDS);
    memcpy(outPeaks, _sharedPeaks, SD_BANDS);
    portEXIT_CRITICAL(&_ballisticsMux);
}

// Every BALLISTICS_TICK_MS (core 1). The bars' smoothing happens per bin in processFftFrame(); what is left
// here is the automatic gain, the peak model, the sleep fade and the mapping to rows (solid bars).
void SpectrumAnalyzer::runBallisticsTick() {
    const float ticksPerSecond = 1000.0f / (float)BALLISTICS_TICK_MS;

    float latest[SD_BANDS];
    bool fresh[SD_BANDS];
    portENTER_CRITICAL(&_stateMux);
    memcpy(latest, _pendingDb, sizeof(latest));
    memcpy(fresh, _newBand, sizeof(fresh));
    memset(_newBand, 0, sizeof(_newBand));
    bool silent = _sleepMode || !_streamConnected;
    bool quiet = _frameQuiet || !_streamConnected;
    uint32_t lastFrameMs = _lastFrameMs;
    portEXIT_CRITICAL(&_stateMux);
    uint32_t nowMs = millis(); // after the snapshot, see isAudioActive()

    // No fresh FFT frame (stalled or closed stream) counts as silence too.
    if ((nowMs - lastFrameMs) > SPECTRUM_FRAME_STALE_MS) {
        silent = true;
        quiet = true;
    }

    // Fade from the current visibility in either direction, so a level hovering near
    // the sleep thresholds can't snap the bars on or restart the fade from full.
    _visibility = stepVisibility(_visibility, silent);
    const float visibility = _visibility;

    for (int column = 0; column < SD_BANDS; column++) {
        if (fresh[column]) _valueDb[column] = latest[column]; // held (already smoothed) until the next frame of its FFT
    }
    // Automatic gain: the display range follows the loudest band (fast up, slowly down, never below
    // SD_AGC_FLOOR_DB). It never amplifies what the sleep logic counts as silence:
    // - while nothing is visible the reference sits on the loudest band, so a wake-up (new stream, end of a
    //   pause) starts settled on what plays now, not on whatever played before, and the peak dots start from 0;
    // - while the input is below the sleep level (a pause, the noise after a track) the reference goes down only
    //   as far as leaves that input below the bottom row, so noise is not raised into view before the sleep fade.
    if (visibility <= 0.0f) {
        sd_agc_snap(&_agc, _valueDb, SD_BANDS);
        for (int column = 0; column < SD_BANDS; column++) sd_peak_reset(&_peak[column]);
    } else if (quiet) {
        sd_agc_quiet(&_agc, _valueDb, SD_BANDS);
    } else {
        sd_agc_step(&_agc, _valueDb, SD_BANDS);
    }

    uint8_t localBars[SD_BANDS];
    uint8_t localPeaks[SD_BANDS];
    for (int column = 0; column < SD_BANDS; column++) {   // column i shows band i
        const float level = sd_agc_level(&_agc, _valueDb[column]);
        sd_peak_step(&_peak[column], level, ticksPerSecond);
        const ColumnRows rows = columnRows(level, _peak[column], visibility);
        localBars[column] = rows.bar;
        localPeaks[column] = rows.peak;
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