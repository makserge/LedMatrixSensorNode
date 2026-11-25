/*
 * spectrum_dsp.h - signal chain of the LED spectrum analyzer (fixed look).
 *
 * Written from SPEC-LED.md and public standards (W3C Web Audio API AnalyserNode maths); no third-party code.
 *
 * Pure functions on caller-owned state: no globals, no mutable statics, no locks, no heap, no I/O. C99 / C++
 * common subset (<stdint.h> only), so the same source builds in the firmware and in the PC test in test/host.
 *
 * Chain per FFT frame and bin:  |X| * scale -> smoothing -> dBFS -> (per band) max over the band's bins with
 * linear interpolation at the band edges, plus the band's tilt (+3 dB per octave around 1 kHz).  Then per
 * ballistics tick: automatic gain (a reference level that follows the loudest band; a band's level 0..1 is its
 * dB relative to that reference over a 48 dB range), peak hold/fall and the height of the solid bar in rows (0..16).
 */
#ifndef SPECTRUM_DSP_H
#define SPECTRUM_DSP_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SD_BANDS         24          /* panel columns = bands */
#define SD_ROWS          16          /* panel rows */
#define SD_SILENCE_DB    -200.0f     /* dB of silence / invalid input */
#define SD_SMOOTH_TAU    0.7f        /* smoothing constant at 60 frames/s */
#define SD_MIN_HZ        30.0f       /* lowest band edge */
#define SD_MAX_HZ        16000.0f    /* highest band edge (limited to fs/2) */
#define SD_WARP_HZ       140.0f      /* band axis: equal steps of ln(1 + f / SD_WARP_HZ); 500 Hz is at column 7 */
#define SD_TILT_DB_PER_OCTAVE 3.0f   /* band weighting: dB per octave, 0 dB at SD_TILT_REF_HZ */
#define SD_TILT_REF_HZ   1000.0f
#define SD_AGC_RANGE_DB  48.0f       /* automatic gain: dB from the bottom to the top of the panel (3 dB per row) */
#define SD_AGC_HEADROOM_DB 6.0f      /* top of the panel above the reference: a steady loudest band shows 14 rows */
#define SD_AGC_FLOOR_DB  -60.0f      /* the reference never goes below this: silence and noise are not amplified */
#define SD_AGC_ATTACK_S  0.1f        /* time constant of the reference while the loudest band is above it */
#define SD_AGC_RELEASE_S 3.0f        /* ... and while it is below */
#define SD_PEAK_HOLD_MS  500         /* peak hold */
#define SD_PEAK_FALL     13.768116f  /* peak fall, panel heights per s^2 (= 3800/276) */

/* Where a band reads the bins of its FFT: edge position = bin + frac (frac 0..1 on the log axis). */
typedef struct { uint16_t loBin; uint16_t hiBin; float loFrac; float hiFrac; } sd_edgemap_t;

/* Peak marker of one column. level 0..1; hold = ticks left before the fall (< 0: falling). */
typedef struct { float level; float hold; } sd_peak_t;

/* Automatic gain: the reference level (weighted dB) and its per-tick follower coefficients. */
typedef struct { float refDb; float attack; float release; } sd_agc_t;

/* One FFT's band plan and per-bin state. The two arrays belong to the caller (nBins floats each). */
typedef struct sd_fft {
    uint16_t size;        /* FFT points; set by sd_fft_setup */
    uint16_t nBins;       /* size / 2 */
    float    fps;         /* nominal frames per second (30 or 100) */
    float    scale;       /* raw FFT magnitude -> |X|/N value; set by the CALLER after sd_fft_setup */
    float    retention;   /* smoothing keep factor, set by sd_plan_bands */
    uint16_t firstBin;    /* bins the caller feeds with sd_fft_bin: firstBin..lastBin inclusive (firstBin > lastBin: none) */
    uint16_t lastBin;
    uint8_t  nBands;      /* bands read from this FFT */
    uint8_t  bandIndex[SD_BANDS];   /* panel column of the j-th band of this FFT, ascending */
    sd_edgemap_t map[SD_BANDS];
    float    tiltDb[SD_BANDS];      /* weighting of the j-th band of this FFT in dB (from its centre frequency) */
    float*   smooth;      /* nBins floats, caller storage: smoothed linear magnitude */
    float*   db;          /* nBins floats, caller storage: dBFS of smooth */
} sd_fft_t;

/* Blackman window (Web Audio AnalyserNode form, alpha 0.16), r in 0..1. */
float sd_blackman(float r);

/* Stores the arguments, nBands = 0, scale = 1, no allocation. Call once per FFT before use. */
void  sd_fft_setup(sd_fft_t* f, uint16_t size, float fps, float* smooth, float* db);

/* Builds the 24 bands for a sample rate and splits them between the FFTs: centre < splitHz -> low, else high.
 * Sets each band's bin map and tilt. Clears smooth[] / db[] of both. Returns SD_BANDS (0 on invalid arguments, nothing changed then). */
int   sd_plan_bands(sd_fft_t* low, sd_fft_t* high, float sampleRate, float splitHz);

/* One bin of one frame: scales, smooths, converts to dB. Bins outside 0..nBins-1 are ignored. */
void  sd_fft_bin(sd_fft_t* f, int bin, float rawMagnitude);

/* Weighted dB of the j-th band of f: the band's value plus its tilt (SD_SILENCE_DB for silence and for a j that
 * does not exist). */
float sd_fft_band_db(const sd_fft_t* f, int j);

/* Automatic gain, moved once per ballistics tick on the n held band values (weighted dB) by ONE of step / quiet /
 * snap; each returns the new reference.
 * reset: reference = SD_AGC_FLOOR_DB, follower coefficients for the tick rate.
 * step:  moves the reference towards the loudest band (never below the floor, never more than SD_AGC_HEADROOM_DB
 *        below the loudest band). The normal case: sound is playing.
 * quiet: as step, but the reference goes down only while that leaves every band below the bottom row. For input
 *        that is too quiet to keep the display awake (a pause, the noise after a track): what is still visible
 *        keeps its height and what is not visible is never raised into view.
 * snap:  reference = the loudest band (or the floor) at once. While nothing is shown, so that a wake-up starts
 *        settled on what plays now.
 * level: a band's value 0..1 for the current reference (NaN -> 0). */
void  sd_agc_reset(sd_agc_t* a, float ticksPerSecond);
float sd_agc_step(sd_agc_t* a, const float* bandDb, int n);
float sd_agc_quiet(sd_agc_t* a, const float* bandDb, int n);
float sd_agc_snap(sd_agc_t* a, const float* bandDb, int n);
float sd_agc_level(const sd_agc_t* a, float db);

void  sd_peak_reset(sd_peak_t* p);
void  sd_peak_step(sd_peak_t* p, float barValue, float ticksPerSecond);   /* one ballistics tick */
float sd_peak_level(const sd_peak_t* p);

/* Value 0..1 -> rows of a solid bar 0..16, value * 16 rounded to the nearest row (NaN -> 0, out of range clamped). */
int   sd_bar_rows(float value);

/* Building blocks, public so that the host test can check each of them. */
void  sd_band_edges(float sampleRate, float edges[SD_BANDS + 1]);
void  sd_edge_to_bin(float freq, float sampleRate, int fftSize, uint16_t* bin, float* frac);
float sd_smooth_retention(float tau, float fps);
float sd_smooth_step(float previous, float magnitude, float retention);
float sd_mag_to_db(float magnitude);
float sd_band_db(const float* dbBins, int nBins, const sd_edgemap_t* m);
float sd_band_tilt(float centreHz);
float sd_agc_coeff(float seconds, float ticksPerSecond);

#ifdef __cplusplus
}
#endif

#endif /* SPECTRUM_DSP_H */
