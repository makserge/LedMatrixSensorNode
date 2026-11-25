/*
 * spectrum_dsp.cpp - signal chain of the LED spectrum analyzer (fixed look). See spectrum_dsp.h.
 *
 * Written from SPEC-LED.md and public standards (W3C Web Audio API AnalyserNode maths); no third-party code.
 *
 * Hot path (per bin / per band / per tick) uses float only. The once-per-sample-rate planning code uses double.
 * Nothing here allocates, locks, prints or keeps state of its own.
 */
#include <math.h>
#include <stdint.h>

#include "spectrum_dsp.h"

#define SD_PI            3.14159265358979323846
#define SD_DB_FLOOR      SD_SILENCE_DB   /* dB of silence / invalid input */
#define SD_AGC_MAX_DB    40.0f       /* no band value is this high (full scale plus the tilt stays below 0 dB) */
#define SD_MAG_FLOOR     1e-10f      /* magnitudes up to this count as silence */
#define SD_TINY          1e-15f      /* smoothed magnitudes below this are flushed to 0 (no denormals) */

/* ------------------------------------------------------------------------------------------ window */

float sd_blackman(float r)
{
    /* w(r) = 0.42 - 0.5 cos(2 pi r) + 0.08 cos(4 pi r); double precision, it only runs at start-up */
    double x = 2.0 * SD_PI * (double)r;
    double w = 0.42 - 0.5 * cos(x) + 0.08 * cos(2.0 * x);
    if (w < 0.0) w = 0.0;       /* rounding at r = 0 and 1 */
    return (float)w;
}

/* ------------------------------------------------------------------------------------------ smoothing, dB */

float sd_smooth_retention(float tau, float fps)
{
    /* The filter constant is defined for 60 frames/s; a frame of another rate keeps tau^(60/fps). */
    if (!(tau > 0.0f)) return 0.0f;     /* also NaN */
    if (tau > 1.0f) tau = 1.0f;
    if (!(fps >= 1.0f)) fps = 1.0f;
    return (float)pow((double)tau, 60.0 / (double)fps);
}

float sd_smooth_step(float previous, float magnitude, float retention)
{
    return retention * previous + (1.0f - retention) * magnitude;
}

float sd_mag_to_db(float magnitude)
{
    if (!(magnitude > SD_MAG_FLOOR)) return SD_DB_FLOOR;   /* also NaN */
    return 20.0f * log10f(magnitude);
}

/* ------------------------------------------------------------------------------------------ band layout */

/* 25 edges between 30 Hz and min(16 kHz, Nyquist), equally spaced on the warped axis x(f) = ln(1 + f / SD_WARP_HZ):
 * close to linear in Hz well below SD_WARP_HZ, logarithmic well above it. On the plain log axis the range below
 * 500 Hz took 10.8 of the 24 columns; here it takes 7, and no band is narrower than 1.5 bins of its FFT. */
static void edges_double(double fs, double* e)
{
    double fMax = fs * 0.5;
    double fMin = (double)SD_MIN_HZ;
    double w = (double)SD_WARP_HZ;
    double xMin, xMax;
    int i;
    if (fMax > (double)SD_MAX_HZ) fMax = (double)SD_MAX_HZ;
    if (fMin >= fMax) fMin = fMax * 0.5;
    xMin = log(1.0 + fMin / w);
    xMax = log(1.0 + fMax / w);
    for (i = 0; i <= SD_BANDS; i++) e[i] = w * (exp(xMin + (xMax - xMin) * (double)i / (double)SD_BANDS) - 1.0);
    e[0] = fMin;                /* exact end points */
    e[SD_BANDS] = fMax;
}

float sd_band_tilt(float centreHz)
{
    if (!(centreHz > 0.0f)) return 0.0f;    /* also NaN */
    return (float)((double)SD_TILT_DB_PER_OCTAVE * log((double)centreHz / (double)SD_TILT_REF_HZ) / log(2.0));
}

void sd_band_edges(float sampleRate, float edges[SD_BANDS + 1])
{
    double e[SD_BANDS + 1];
    int i;
    edges_double((double)sampleRate, e);
    for (i = 0; i <= SD_BANDS; i++) edges[i] = (float)e[i];
}

/* Position of a frequency among the bins of an FFT: bin b = floor(f N / fs) and the fraction of the way from the
 * lower to the upper bin edge, measured on the log axis (bin 0 starts at 1 Hz because log of 0 is undefined). */
static void edge_position(double f, double fs, int n, uint16_t* bin, float* frac)
{
    int maxBin = n / 2 - 1;
    double b, lower, upper, t;
    int ib;

    if (maxBin < 0) { *bin = 0; *frac = 0.0f; return; }
    b = floor(f * (double)n / fs);
    if (!(b >= 0.0)) b = 0.0;               /* negative or NaN */
    if (b > (double)maxBin) {               /* at or above Nyquist */
        *bin = (uint16_t)maxBin;
        *frac = 0.0f;
        return;
    }
    ib = (int)b;
    lower = (ib == 0) ? 1.0 : (double)ib * fs / (double)n;
    upper = (double)(ib + 1) * fs / (double)n;
    t = 0.0;
    if (upper > lower) t = log(f / lower) / log(upper / lower);
    if (!(t > 0.0)) t = 0.0;                /* also NaN and -inf */
    if (t > 1.0) t = 1.0;
    *bin = (uint16_t)ib;
    *frac = (float)t;
}

void sd_edge_to_bin(float freq, float sampleRate, int fftSize, uint16_t* bin, float* frac)
{
    edge_position((double)freq, (double)sampleRate, fftSize, bin, frac);
}

/* Appends the band [fLo, fHi] (panel column `column`) to an FFT's plan. */
static void add_band(sd_fft_t* f, int column, double fLo, double fHi, double fs)
{
    sd_edgemap_t* m = &f->map[f->nBands];
    int last;
    int first = (f->nBands == 0);
    edge_position(fLo, fs, (int)f->size, &m->loBin, &m->loFrac);
    edge_position(fHi, fs, (int)f->size, &m->hiBin, &m->hiFrac);
    if (m->hiBin < m->loBin) {
        m->hiBin = m->loBin;
        m->hiFrac = m->loFrac;
    }
    f->bandIndex[f->nBands] = (uint8_t)column;
    f->tiltDb[f->nBands] = sd_band_tilt((float)sqrt(fLo * fHi));
    last = (int)m->hiBin + 1;
    if (last > (int)f->nBins - 1) last = (int)f->nBins - 1;
    if (first || m->loBin < f->firstBin) f->firstBin = m->loBin;
    if (first || last > (int)f->lastBin) f->lastBin = (uint16_t)last;
    f->nBands++;
}

static int plannable(const sd_fft_t* f)
{
    return f != 0 && f->size >= 2 && f->smooth != 0 && f->db != 0;
}

static void begin_plan(sd_fft_t* f)
{
    int k;
    f->nBands = 0;
    f->firstBin = 1;
    f->lastBin = 0;
    f->retention = sd_smooth_retention(SD_SMOOTH_TAU, f->fps);
    for (k = 0; k < (int)f->nBins; k++) {
        f->smooth[k] = 0.0f;
        f->db[k] = SD_DB_FLOOR;
    }
}

int sd_plan_bands(sd_fft_t* low, sd_fft_t* high, float sampleRate, float splitHz)
{
    double e[SD_BANDS + 1];
    double fs = (double)sampleRate;
    int i;

    if (!plannable(low) || !plannable(high) || !(fs > 0.0)) return 0;
    edges_double(fs, e);
    begin_plan(low);
    begin_plan(high);
    for (i = 0; i < SD_BANDS; i++) {
        double centre = sqrt(e[i] * e[i + 1]);
        sd_fft_t* owner = (centre < (double)splitHz) ? low : high;
        add_band(owner, i, e[i], e[i + 1], fs);
    }
    return SD_BANDS;
}

/* ------------------------------------------------------------------------------------------ per frame */

void sd_fft_setup(sd_fft_t* f, uint16_t size, float fps, float* smooth, float* db)
{
    f->size = size;
    f->nBins = (uint16_t)(size / 2);
    f->fps = fps;
    f->scale = 1.0f;
    f->retention = 0.0f;
    f->firstBin = 1;
    f->lastBin = 0;
    f->nBands = 0;
    f->smooth = smooth;
    f->db = db;
}

void sd_fft_bin(sd_fft_t* f, int bin, float rawMagnitude)
{
    float m, s;
    if (bin < 0 || bin >= (int)f->nBins) return;
    m = rawMagnitude * f->scale;
    if (!(m >= 0.0f && m <= 3.0e38f)) m = 0.0f;     /* NaN, negative, infinite: would poison the filter state */
    s = sd_smooth_step(f->smooth[bin], m, f->retention);
    if (s < SD_TINY) s = 0.0f;
    f->smooth[bin] = s;
    f->db[bin] = sd_mag_to_db(s);
}

static float interpolate_db(const float* d, int n, int bin, float frac)
{
    if (bin < n - 1) return d[bin] + (d[bin + 1] - d[bin]) * frac;
    return d[bin];
}

float sd_band_db(const float* dbBins, int nBins, const sd_edgemap_t* m)
{
    int lo, hi, j;
    float v, t;
    if (dbBins == 0 || m == 0 || nBins < 1) return SD_DB_FLOOR;
    lo = (int)m->loBin;
    hi = (int)m->hiBin;
    if (lo > nBins - 1) lo = nBins - 1;
    if (hi > nBins - 1) hi = nBins - 1;
    v = interpolate_db(dbBins, nBins, lo, m->loFrac);
    t = interpolate_db(dbBins, nBins, hi, m->hiFrac);
    if (t > v) v = t;
    for (j = lo + 1; j < hi; j++) {
        if (dbBins[j] > v) v = dbBins[j];
    }
    return v;
}

float sd_fft_band_db(const sd_fft_t* f, int j)
{
    float v;
    if (j < 0 || j >= (int)f->nBands) return SD_DB_FLOOR;
    v = sd_band_db(f->db, (int)f->nBins, &f->map[j]);
    if (!(v > SD_DB_FLOOR)) return SD_DB_FLOOR;     /* silence stays silence, whatever the tilt */
    return v + f->tiltDb[j];
}

/* ------------------------------------------------------------------------------------------ automatic gain */

float sd_agc_coeff(float seconds, float ticksPerSecond)
{
    /* share of the way to the target that a first-order follower with this time constant covers in one tick */
    if (!(ticksPerSecond > 0.0f)) ticksPerSecond = 1.0f;
    if (!(seconds > 0.0f)) return 1.0f;             /* no time constant: follow at once */
    return (float)(1.0 - exp(-1.0 / ((double)ticksPerSecond * (double)seconds)));
}

void sd_agc_reset(sd_agc_t* a, float ticksPerSecond)
{
    a->refDb = SD_AGC_FLOOR_DB;
    a->attack = sd_agc_coeff(SD_AGC_ATTACK_S, ticksPerSecond);
    a->release = sd_agc_coeff(SD_AGC_RELEASE_S, ticksPerSecond);
}

/* The loudest of the n band values, limited to SD_AGC_MAX_DB; puts a broken reference back to the floor. */
static float agc_loudest(sd_agc_t* a, const float* bandDb, int n)
{
    float peak = SD_DB_FLOOR;
    int i;
    for (i = 0; i < n; i++) {
        if (bandDb[i] > peak) peak = bandDb[i];     /* a NaN never compares greater */
    }
    if (peak > SD_AGC_MAX_DB) peak = SD_AGC_MAX_DB;
    if (!(a->refDb >= SD_AGC_FLOOR_DB && a->refDb <= SD_AGC_MAX_DB)) a->refDb = SD_AGC_FLOOR_DB;   /* also NaN */
    return peak;
}

static float agc_move(sd_agc_t* a, const float* bandDb, int n, int quiet)
{
    float peak = agc_loudest(a, bandDb, n);
    float target = (peak > SD_AGC_FLOOR_DB) ? peak : SD_AGC_FLOOR_DB;

    if (target > a->refDb) {
        a->refDb += (target - a->refDb) * a->attack;
        /* a jump of more than the headroom: follow at once, so that no band leaves the top of the panel */
        if (a->refDb < peak - SD_AGC_HEADROOM_DB) a->refDb = peak - SD_AGC_HEADROOM_DB;
    } else {
        if (quiet) {        /* down only as far as leaves this input below the bottom row */
            float limit = peak + (SD_AGC_RANGE_DB - SD_AGC_HEADROOM_DB);
            if (limit > target) target = limit;
        }
        if (target < a->refDb) a->refDb += (target - a->refDb) * a->release;
    }
    return a->refDb;
}

float sd_agc_step(sd_agc_t* a, const float* bandDb, int n)
{
    return agc_move(a, bandDb, n, 0);
}

float sd_agc_quiet(sd_agc_t* a, const float* bandDb, int n)
{
    return agc_move(a, bandDb, n, 1);
}

float sd_agc_snap(sd_agc_t* a, const float* bandDb, int n)
{
    float peak = agc_loudest(a, bandDb, n);
    a->refDb = (peak > SD_AGC_FLOOR_DB) ? peak : SD_AGC_FLOOR_DB;
    return a->refDb;
}

float sd_agc_level(const sd_agc_t* a, float db)
{
    float level;
    if (!(db == db)) return 0.0f;
    level = (db - (a->refDb + SD_AGC_HEADROOM_DB - SD_AGC_RANGE_DB)) / SD_AGC_RANGE_DB;
    if (!(level > 0.0f)) return 0.0f;
    if (level > 1.0f) return 1.0f;
    return level;
}

/* ------------------------------------------------------------------------------------------ ballistics */

void sd_peak_reset(sd_peak_t* p)
{
    p->level = 0.0f;
    p->hold = 0.0f;
}

void sd_peak_step(sd_peak_t* p, float barValue, float ticksPerSecond)
{
    if (!(barValue > 0.0f)) barValue = 0.0f;        /* also NaN */
    if (barValue > 1.0f) barValue = 1.0f;
    if (!(ticksPerSecond > 0.0f)) ticksPerSecond = 1.0f;

    if (barValue >= p->level) {                     /* the bar reaches the marker: it follows and the hold restarts */
        p->level = barValue;
        p->hold = ticksPerSecond * (float)SD_PEAK_HOLD_MS / 1000.0f;
    } else if (p->level > 0.0f) {
        p->hold -= 1.0f;
        if (p->hold < 0.0f) {                       /* falling: speed grows by SD_PEAK_FALL / fps^2 per tick */
            p->level += p->hold * (SD_PEAK_FALL / (ticksPerSecond * ticksPerSecond));
            if (p->level < 0.0f) p->level = 0.0f;
        }
    }
}

float sd_peak_level(const sd_peak_t* p)
{
    return p->level;
}

/* ------------------------------------------------------------------------------------------ rows */

int sd_bar_rows(float value)
{
    if (!(value > 0.0f)) return 0;                  /* also NaN */
    if (value > 1.0f) value = 1.0f;
    return (int)floorf(value * (float)SD_ROWS + 0.5f);   /* solid bar, nearest row */
}
