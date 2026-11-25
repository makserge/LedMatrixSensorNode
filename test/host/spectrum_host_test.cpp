/*
 * spectrum_host_test.cpp - PC test of the spectrum signal chain (src/spectrum_dsp.cpp).
 *
 * Written from SPEC-LED.md and public standards (W3C Web Audio API AnalyserNode maths); no third-party code.
 *
 * Common subset of C99 and C++11, no Arduino, no ESP-IDF. Expected numbers come from spectrum_expected.h, which
 * tools/tests/spectrum_reference.py generates (independently, from the formulas) and checks against the figures of
 * the specification. Tone spectra are built here with a direct double-precision DFT of
 * A sin(2 pi f n / fs) * sd_blackman(n / (N - 1)) (one or two tones).
 *
 *   make -C test/host                                    # g++
 *   make -C test/host CXX=gcc LANGOPT="-x c -std=c99"    # plain C compiler
 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "spectrum_dsp.h"
#include "spectrum_expected.h"

#define PI 3.14159265358979323846
#define LOW_N 2048
#define HIGH_N 1024
#define SPLIT_HZ 500.0f
#define LOW_FPS 30.0f
#define HIGH_FPS 100.0f
#define TICKS_PER_S 100.0f

static int g_checks = 0;
static int g_failed = 0;

/* ------------------------------------------------------------------------------------------ check helpers */

static void fail_double(const char* what, int i, int j, double got, double want)
{
    g_failed++;
    printf("FAIL %s [%d][%d]: got %.9g, expected %.9g\n", what, i, j, got, want);
}

/* |got - want| <= tol; NaN never passes. i, j: indices for the message (-1: none). */
static void near_at(const char* what, int i, int j, double got, double want, double tol)
{
    g_checks++;
    if (!(fabs(got - want) <= tol)) fail_double(what, i, j, got, want);
}

static void near1(const char* what, double got, double want, double tol)
{
    near_at(what, -1, -1, got, want, tol);
}

static void equal_at(const char* what, int i, int j, long got, long want)
{
    g_checks++;
    if (got != want) fail_double(what, i, j, (double)got, (double)want);
}

static void equal1(const char* what, long got, long want)
{
    equal_at(what, -1, -1, got, want);
}

static void truth(const char* what, int i, int ok)
{
    g_checks++;
    if (!ok) {
        g_failed++;
        printf("FAIL %s [%d]\n", what, i);
    }
}

/* ------------------------------------------------------------------------------------------ test state */

static float g_smoothLow[LOW_N / 2];
static float g_dbLow[LOW_N / 2];
static float g_smoothHigh[HIGH_N / 2];
static float g_dbHigh[HIGH_N / 2];

/* NaN and infinity made at run time, so that no compiler warns about a constant division. */
static float make_nan(void)
{
    volatile float zero = 0.0f;
    return zero / zero;
}

static float make_inf(void)
{
    volatile float zero = 0.0f;
    return 1.0f / zero;
}

static float g_scaleLow(void) { return 1.0f / (float)LOW_N; }

static double window_power_sum(int n)
{
    double sum = 0.0;
    int i;
    for (i = 0; i < n; i++) {
        double w = (double)sd_blackman((float)i / (float)(n - 1));
        sum += w * w;
    }
    return sum;
}

static float g_scaleHigh(void)
{
    return (float)(sqrt(window_power_sum(LOW_N) / window_power_sum(HIGH_N)) / (double)LOW_N);
}

/* Both FFTs set up the way the firmware does it. */
static void setup_pair(sd_fft_t* low, sd_fft_t* high)
{
    sd_fft_setup(low, LOW_N, LOW_FPS, g_smoothLow, g_dbLow);
    sd_fft_setup(high, HIGH_N, HIGH_FPS, g_smoothHigh, g_dbHigh);
    low->scale = g_scaleLow();
    high->scale = g_scaleHigh();
}

/* Raw magnitudes |X_k| (not scaled) of one or two sines, bins first..last, by direct DFT in double precision. */
static void tone_spectrum(int n, double fs, double freq, double amp, double freq2, double amp2, int first, int last,
                          float* raw)
{
    static double x[LOW_N];
    static double ct[LOW_N];
    static double st[LOW_N];
    int i, k;
    for (i = 0; i < n; i++) {
        double w = (double)sd_blackman((float)i / (float)(n - 1));
        x[i] = (amp * sin(2.0 * PI * freq * (double)i / fs) + amp2 * sin(2.0 * PI * freq2 * (double)i / fs)) * w;
        ct[i] = cos(2.0 * PI * (double)i / (double)n);
        st[i] = sin(2.0 * PI * (double)i / (double)n);
    }
    for (k = first; k <= last; k++) {
        double re = 0.0, im = 0.0;
        for (i = 0; i < n; i++) {
            int idx = (int)(((long)k * (long)i) % (long)n);
            re += x[i] * ct[idx];
            im -= x[i] * st[idx];
        }
        raw[k] = (float)((k == 0) ? fabs(re) : sqrt(re * re + im * im));
    }
}

static void feed_frame(sd_fft_t* f, const float* raw)
{
    int k;
    for (k = (int)f->firstBin; k <= (int)f->lastBin; k++) sd_fft_bin(f, k, raw[k]);
}

static void feed_zero_frame(sd_fft_t* f)
{
    int k;
    for (k = (int)f->firstBin; k <= (int)f->lastBin; k++) sd_fft_bin(f, k, 0.0f);
}

/* ------------------------------------------------------------------------------------------ scalars */

static void test_window_and_scale(void)
{
    int i;
    double s2048 = window_power_sum(LOW_N);
    double s1024 = window_power_sum(HIGH_N);

    for (i = 0; i < EXP_NWIN; i++) {
        near_at("window", i, -1, (double)sd_blackman((float)EXP_WIN_R[i]), EXP_WIN_W[i], 1e-5);
    }
    for (i = 0; i <= 64; i++) {
        double r = (double)i / 64.0;
        near_at("window symmetric", i, -1, (double)sd_blackman((float)r), (double)sd_blackman((float)(1.0 - r)), 1e-6);
        truth("window within 0..1", i, sd_blackman((float)r) >= 0.0f && sd_blackman((float)r) <= 1.0f);
    }
    near1("window r=0 below 1e-6", (double)sd_blackman(0.0f), 0.0, 1e-6);
    near1("window r=1 below 1e-6", (double)sd_blackman(1.0f), 0.0, 1e-6);
    near1("power sum 2048", s2048, EXP_POWER_SUM[0], 0.05);
    near1("power sum 1024", s1024, EXP_POWER_SUM[1], 0.05);
    near1("scale low", (double)g_scaleLow(), EXP_SCALE[0], 1e-12);
    near1("scale high", (double)g_scaleHigh(), EXP_SCALE[1], 2e-7);
    near1("scale high spec value", (double)g_scaleHigh(), 6.9070e-4, 2e-7);
}

static void test_smoothing_db(void)
{
    int i;
    float v;
    for (i = 0; i < EXP_NRET; i++) {
        near_at("retention", i, -1, (double)sd_smooth_retention((float)EXP_RET[i][0], (float)EXP_RET[i][1]),
                EXP_RET[i][2], 1e-6);
    }
    equal1("retention tau 0", (long)(sd_smooth_retention(0.0f, 30.0f) * 1e6f), 0);
    near1("retention fps<1 is 1", (double)sd_smooth_retention(0.7f, 0.0f), (double)sd_smooth_retention(0.7f, 1.0f), 1e-9);
    near1("retention constants", (double)sd_smooth_retention(SD_SMOOTH_TAU, 30.0f), 0.49, 1e-6);
    near1("retention constants fps 100", (double)sd_smooth_retention(SD_SMOOTH_TAU, 100.0f), 0.807344, 1e-6);

    for (i = 0; i < EXP_NSTEP; i++) {
        near_at("smooth step", i, -1,
                (double)sd_smooth_step((float)EXP_STEP[i][1], (float)EXP_STEP[i][2], (float)EXP_STEP[i][0]),
                EXP_STEP[i][3], 1e-6);
    }
    for (i = 0; i < EXP_NDB; i++) {
        near_at("magnitude to dB", i, -1, (double)sd_mag_to_db((float)EXP_DB[i][0]), EXP_DB[i][1], 1e-3);
    }
    near1("dB of NaN", (double)sd_mag_to_db(make_nan()), -200.0, 0.0);
    near1("dB of negative", (double)sd_mag_to_db(-1.0f), -200.0, 0.0);
    /* step response of one bin against 1 - r^n, and decay at the specified dB per frame */
    {
        sd_fft_t f;
        float sm[8], db[8];
        double r;
        int n;
        sd_fft_setup(&f, 16, HIGH_FPS, sm, db);
        f.scale = 1.0f;
        f.retention = sd_smooth_retention(SD_SMOOTH_TAU, HIGH_FPS);
        r = (double)f.retention;
        for (i = 0; i < 8; i++) { sm[i] = 0.0f; db[i] = -200.0f; }
        for (n = 1; n <= 40; n++) {
            sd_fft_bin(&f, 3, 1.0f);
            near_at("step response", n, -1, (double)sm[3], 1.0 - pow(r, (double)n), 1e-6);
        }
        near1("step response 3 dB", (double)db[3] - 20.0 * log10(1.0 - pow(r, 40.0)), 0.0, 1e-3);
        for (n = 1; n <= 10; n++) {
            double before = (double)db[3];
            sd_fft_bin(&f, 3, 0.0f);
            near_at("decay per high frame (dB)", n, -1, (double)db[3] - before, -1.8588, 2e-3);
        }
        near1("decay dB per high frame", 20.0 * log10(r), -1.8588, 1e-3);
        near1("decay dB per low frame", 20.0 * log10((double)sd_smooth_retention(SD_SMOOTH_TAU, LOW_FPS)), -6.196, 1e-3);
        v = sm[3];
        truth("decay shrinks", 0, v < 1.0f && v > 0.0f);
    }
}

/* ------------------------------------------------------------------------------------------ tilt, automatic gain */

static void test_tilt_and_agc_level(void)
{
    sd_agc_t a;
    int i;

    for (i = 0; i < EXP_NTILT; i++) {
        near_at("tilt", i, -1, (double)sd_band_tilt((float)EXP_TILT[i][0]), EXP_TILT[i][1], 1e-4);
    }
    near1("tilt at 1 kHz", (double)sd_band_tilt(1000.0f), 0.0, 1e-6);
    near1("tilt per octave", (double)(sd_band_tilt(4000.0f) - sd_band_tilt(2000.0f)), 3.0, 1e-5);
    near1("tilt of 0 Hz", (double)sd_band_tilt(0.0f), 0.0, 0.0);
    near1("tilt of NaN", (double)sd_band_tilt(make_nan()), 0.0, 0.0);

    /* constants of the automatic gain, as the specification prints them */
    near1("SD_AGC_RANGE_DB", (double)SD_AGC_RANGE_DB, EXP_AGC_CONST[0], 0.0);
    near1("SD_AGC_HEADROOM_DB", (double)SD_AGC_HEADROOM_DB, EXP_AGC_CONST[1], 0.0);
    near1("SD_AGC_FLOOR_DB", (double)SD_AGC_FLOOR_DB, EXP_AGC_CONST[2], 0.0);
    near1("SD_AGC_ATTACK_S", (double)SD_AGC_ATTACK_S, EXP_AGC_CONST[3], 1e-7);
    near1("SD_AGC_RELEASE_S", (double)SD_AGC_RELEASE_S, EXP_AGC_CONST[4], 0.0);
    near1("range 48, headroom 6, floor -60", (double)(SD_AGC_RANGE_DB + SD_AGC_HEADROOM_DB + SD_AGC_FLOOR_DB), -6.0, 0.0);

    near1("attack coefficient, 100 ticks/s", (double)sd_agc_coeff(SD_AGC_ATTACK_S, 100.0f), EXP_AGC_COEFF[0][0], 1e-6);
    near1("release coefficient, 100 ticks/s", (double)sd_agc_coeff(SD_AGC_RELEASE_S, 100.0f), EXP_AGC_COEFF[0][1], 1e-7);
    near1("attack coefficient, 30 ticks/s", (double)sd_agc_coeff(SD_AGC_ATTACK_S, 30.0f), EXP_AGC_COEFF[1][0], 1e-6);
    near1("release coefficient, 30 ticks/s", (double)sd_agc_coeff(SD_AGC_RELEASE_S, 30.0f), EXP_AGC_COEFF[1][1], 1e-7);
    near1("coefficient without a time constant", (double)sd_agc_coeff(0.0f, 100.0f), 1.0, 0.0);
    truth("coefficient with a bad tick rate is 0..1", 0, sd_agc_coeff(1.0f, 0.0f) > 0.0f && sd_agc_coeff(1.0f, 0.0f) <= 1.0f);

    sd_agc_reset(&a, TICKS_PER_S);
    near1("reset: reference at the floor", (double)a.refDb, (double)SD_AGC_FLOOR_DB, 0.0);
    near1("reset: attack", (double)a.attack, EXP_AGC_COEFF[0][0], 1e-6);
    near1("reset: release", (double)a.release, EXP_AGC_COEFF[0][1], 1e-7);
    for (i = 0; i < EXP_NAGC_LEVEL; i++) {
        a.refDb = (float)EXP_AGC_LEVEL[i][0];
        near_at("agc level", i, -1, (double)sd_agc_level(&a, (float)EXP_AGC_LEVEL[i][1]), EXP_AGC_LEVEL[i][2], 1e-5);
    }
    a.refDb = -30.0f;
    near1("level of NaN", (double)sd_agc_level(&a, make_nan()), 0.0, 0.0);
    near1("level of +inf", (double)sd_agc_level(&a, make_inf()), 1.0, 0.0);
    near1("level of -inf", (double)sd_agc_level(&a, -make_inf()), 0.0, 0.0);
    near1("a band at the reference: 14 rows", (double)sd_bar_rows(sd_agc_level(&a, -30.0f)), 14.0, 0.0);
    near1("3 dB per row", (double)(sd_agc_level(&a, -30.0f) - sd_agc_level(&a, -33.0f)) * SD_ROWS, 1.0, 1e-5);
}

/* The reference alone: a loudest band and a probe band 20 dB below it, through silence, a step up from the floor,
 * a step down, a step up, silence again and a signal below the floor. */
static void test_agc_sequence(void)
{
    sd_agc_t a;
    float band[SD_BANDS];
    int seg, i, t = 0, pinned = 0;
    float before;

    sd_agc_reset(&a, TICKS_PER_S);
    for (seg = 0; seg < EXP_NAGC_SEG; seg++) {
        float peak = (float)EXP_AGC_SEG[seg][1];
        float low = (peak <= SD_SILENCE_DB) ? SD_SILENCE_DB : peak - (float)EXP_AGC_PROBE_BELOW;
        for (i = 0; i < SD_BANDS; i++) band[i] = SD_SILENCE_DB;
        band[5] = peak;
        band[17] = low;
        for (i = 0; i < (int)EXP_AGC_SEG[seg][0]; i++, t++) {
            float ref = sd_agc_step(&a, band, SD_BANDS);
            near_at("agc sequence: returned reference", t, -1, (double)ref, (double)a.refDb, 0.0);
            near_at("agc sequence: reference dB", t, -1, (double)ref, EXP_AGC_REF[t], 2e-3);
            near_at("agc sequence: loudest band level", t, -1, (double)sd_agc_level(&a, peak), EXP_AGC_TOP[t], 2e-4);
            near_at("agc sequence: probe band level", t, -1, (double)sd_agc_level(&a, low), EXP_AGC_PROBE[t], 2e-4);
            truth("agc sequence: reference never below the floor", t, ref >= SD_AGC_FLOOR_DB);
            truth("agc sequence: loudest band never above the top", t, peak <= ref + SD_AGC_HEADROOM_DB + 1e-3f);
            if (sd_bar_rows(sd_agc_level(&a, peak)) >= SD_ROWS) pinned++;
        }
    }
    equal1("agc sequence: all ticks", t, EXP_NAGC_TICKS);
    /* 16 rows only right after a step up: 9 ticks per step (two steps), never in steady state */
    truth("agc sequence: 16 rows for less than 0.1 s per step up", pinned, pinned > 0 && pinned <= 18);

    /* the specification's own figures: steady loudest band 0.875 (14 rows), probe 20 dB lower 0.4583 (7 rows) */
    near1("steady level", EXP_AGC_TOP[399], 0.875, 1e-4);
    equal1("steady rows", sd_bar_rows((float)EXP_AGC_TOP[399]), 14);
    equal1("steady probe rows", sd_bar_rows((float)EXP_AGC_PROBE[399]), 7);

    /* monotonic: up while the loudest band is above the reference, down while it is below */
    sd_agc_reset(&a, TICKS_PER_S);
    for (i = 0; i < SD_BANDS; i++) band[i] = SD_SILENCE_DB;
    band[0] = -40.0f;
    before = a.refDb;
    for (i = 0; i < 300; i++) {
        sd_agc_step(&a, band, SD_BANDS);
        truth("rising reference never falls", i, a.refDb >= before && a.refDb <= -40.0f);
        before = a.refDb;
    }
    near1("settles on the loudest band", (double)a.refDb, -40.0, 1e-3);
    band[0] = -52.0f;
    for (i = 0; i < 3000; i++) {
        sd_agc_step(&a, band, SD_BANDS);
        truth("falling reference never rises", i, a.refDb <= before && a.refDb >= -52.0f);
        before = a.refDb;
    }
    near1("settles on the quieter band (30 s)", (double)a.refDb, -52.0, 1e-2);
    /* release time constant 3 s: 12 dB down, 1/e of it left after 300 ticks */
    sd_agc_reset(&a, TICKS_PER_S);
    a.refDb = -40.0f;
    for (i = 0; i < 300; i++) sd_agc_step(&a, band, SD_BANDS);
    near1("release: 1/e left after 3 s", (double)a.refDb, -52.0 + 12.0 * exp(-1.0), 0.02);
    /* attack time constant 0.1 s, below the headroom so that the jump rule stays out of it */
    sd_agc_reset(&a, TICKS_PER_S);
    a.refDb = -45.0f;
    band[0] = -40.0f;
    for (i = 0; i < 10; i++) sd_agc_step(&a, band, SD_BANDS);
    near1("attack: 1/e left after 0.1 s", (double)a.refDb, -40.0 - 5.0 * exp(-1.0), 0.01);

    /* silence: the reference goes back to the floor and stays there; nothing is shown */
    for (i = 0; i < SD_BANDS; i++) band[i] = SD_SILENCE_DB;
    for (i = 0; i < 6000; i++) sd_agc_step(&a, band, SD_BANDS);
    near1("silence: reference back at the floor after 60 s", (double)a.refDb, (double)SD_AGC_FLOOR_DB, 1e-3);
    near1("silence: level 0", (double)sd_agc_level(&a, SD_SILENCE_DB), 0.0, 0.0);
    /* a signal below the floor is shown lower, it is not pulled up to 14 rows */
    band[3] = -78.0f;
    for (i = 0; i < 1000; i++) sd_agc_step(&a, band, SD_BANDS);
    near1("below the floor: reference stays", (double)a.refDb, (double)SD_AGC_FLOOR_DB, 1e-3);
    equal1("below the floor: 18 dB under it is 8 rows", sd_bar_rows(sd_agc_level(&a, band[3])), 8);
    band[3] = -110.0f;
    equal1("48 dB under the top: nothing", sd_bar_rows(sd_agc_level(&a, band[3])), 0);

    /* garbage in */
    sd_agc_reset(&a, TICKS_PER_S);
    for (i = 0; i < SD_BANDS; i++) band[i] = make_nan();
    sd_agc_step(&a, band, SD_BANDS);
    near1("NaN bands are silence", (double)a.refDb, (double)SD_AGC_FLOOR_DB, 0.0);
    band[2] = make_inf();
    sd_agc_step(&a, band, SD_BANDS);
    truth("infinite band is limited", 0, a.refDb == a.refDb && a.refDb <= 40.0f);
    a.refDb = make_nan();
    band[2] = -30.0f;
    sd_agc_step(&a, band, SD_BANDS);
    truth("a NaN reference recovers", 0, a.refDb == a.refDb && a.refDb >= SD_AGC_FLOOR_DB && a.refDb <= -30.0f);
    sd_agc_step(&a, band, 0);
    truth("no bands: silence", 0, a.refDb == a.refDb && a.refDb >= SD_AGC_FLOOR_DB);
}

/* The three moves the firmware chooses from per tick (step / quiet / snap), tick by tick against the model: asleep,
 * music that starts while the panel is still dark, awake, noise after the music, faded out, a click in the noise,
 * a visible tail that is too quiet to count, digital silence, music again. */
static void test_agc_moves(void)
{
    sd_agc_t a;
    float band[SD_BANDS];
    int seg, i, t = 0;
    float before;

    sd_agc_reset(&a, TICKS_PER_S);
    for (seg = 0; seg < EXP_NAGCM_SEG; seg++) {
        float peak = (float)EXP_AGCM_SEG[seg][1];
        float low = (peak <= SD_SILENCE_DB) ? SD_SILENCE_DB : peak - (float)EXP_AGC_PROBE_BELOW;
        int move = (int)EXP_AGCM_SEG[seg][2];
        for (i = 0; i < SD_BANDS; i++) band[i] = SD_SILENCE_DB;
        band[2] = peak;
        band[20] = low;
        for (i = 0; i < (int)EXP_AGCM_SEG[seg][0]; i++, t++) {
            float ref;
            before = a.refDb;
            ref = (move == 2) ? sd_agc_snap(&a, band, SD_BANDS)
                : (move == 1) ? sd_agc_quiet(&a, band, SD_BANDS) : sd_agc_step(&a, band, SD_BANDS);
            near_at("agc moves: returned reference", t, -1, (double)ref, (double)a.refDb, 0.0);
            near_at("agc moves: reference dB", t, -1, (double)ref, EXP_AGCM_REF[t], 2e-3);
            near_at("agc moves: loudest band level", t, -1, (double)sd_agc_level(&a, peak), EXP_AGCM_TOP[t], 2e-4);
            near_at("agc moves: probe band level", t, -1, (double)sd_agc_level(&a, low), EXP_AGCM_PROBE[t], 2e-4);
            truth("agc moves: reference never below the floor", t, ref >= SD_AGC_FLOOR_DB);
            truth("agc moves: loudest band never above the top", t, peak <= ref + SD_AGC_HEADROOM_DB + 1e-3f);
            if (move == 2) {    /* snap: settled at once, the loudest band at 14 rows (or the floor) */
                near_at("snap: reference on the loudest band or the floor", t, -1, (double)ref,
                        peak > SD_AGC_FLOOR_DB ? (double)peak : (double)SD_AGC_FLOOR_DB, 0.0);
            }
            if (move == 1 && peak <= before) {  /* quiet: what was dark stays dark, what was lit keeps its height */
                float bottom = before + SD_AGC_HEADROOM_DB - SD_AGC_RANGE_DB;
                if (peak <= bottom) {
                    equal_at("quiet: input below the bottom row is not raised into view", t, -1,
                             sd_bar_rows(sd_agc_level(&a, peak)), 0);
                    truth("quiet: reference only goes down", t, ref <= before);
                } else {
                    near_at("quiet: reference kept while the input is visible", t, -1, (double)ref, (double)before, 0.0);
                }
            }
        }
    }
    equal1("agc moves: all ticks", t, EXP_NAGCM_TICKS);
    /* the specification's own figures (2.10): a wake-up shows 14 rows from the first tick; noise 45 dB under the
     * music stays dark for the whole 4 s; after the fade the reference is back at the floor */
    near1("wake-up: first visible tick at 0.875", EXP_AGCM_TOP[150], 0.875, 1e-6);
    equal1("wake-up: 14 rows", sd_bar_rows((float)EXP_AGCM_TOP[150]), 14);
    near1("noise after music: level 0 at the end", EXP_AGCM_TOP[849], 0.0, 0.0);
    truth("noise after music: reference stops 42 dB above the noise", 0, EXP_AGCM_REF[849] > -75.0 + 42.0);
    near1("faded out: reference at the floor", EXP_AGCM_REF[850], (double)SD_AGC_FLOOR_DB, 0.0);

    /* quiet with noise for a long time: the reference ends 42 dB above the noise, the noise at exactly 0 rows */
    sd_agc_reset(&a, TICKS_PER_S);
    for (i = 0; i < SD_BANDS; i++) band[i] = -80.0f + 0.1f * (float)i;      /* loudest -77.7 */
    a.refDb = -10.0f;
    for (i = 0; i < 6000; i++) {
        before = a.refDb;
        sd_agc_quiet(&a, band, SD_BANDS);
        truth("quiet, noise: reference falls and stays 42 dB above it", i, a.refDb <= before && a.refDb >= -77.7f + 42.0f - 1e-3f);
        equal_at("quiet, noise: nothing lit", i, -1, sd_bar_rows(sd_agc_level(&a, band[SD_BANDS - 1])), 0);
    }
    near1("quiet, noise: ends 42 dB above the loudest band", (double)a.refDb, -35.7, 0.01);
    /* the same with sd_agc_step: the noise comes into view (that is what quiet is for) */
    a.refDb = -10.0f;
    for (i = 0; i < 6000; i++) sd_agc_step(&a, band, SD_BANDS);
    truth("step, noise: raised into view", 0, sd_bar_rows(sd_agc_level(&a, band[SD_BANDS - 1])) >= 8);
    /* quiet with digital silence: back to the floor like step */
    for (i = 0; i < SD_BANDS; i++) band[i] = SD_SILENCE_DB;
    a.refDb = -10.0f;
    for (i = 0; i < 6000; i++) sd_agc_quiet(&a, band, SD_BANDS);
    near1("quiet, silence: reference back at the floor after 60 s", (double)a.refDb, (double)SD_AGC_FLOOR_DB, 1e-3);
    /* quiet still rises: a click in the pause cannot leave the top either */
    band[7] = -5.0f;
    sd_agc_quiet(&a, band, SD_BANDS);
    near1("quiet: a jump is followed at once", (double)a.refDb, -11.0, 1e-4);
    /* snap */
    near1("snap: on the loudest band", (double)sd_agc_snap(&a, band, SD_BANDS), -5.0, 0.0);
    equal1("snap: the loudest band shows 14 rows", sd_bar_rows(sd_agc_level(&a, band[7])), 14);
    band[7] = -90.0f;
    near1("snap: never below the floor", (double)sd_agc_snap(&a, band, SD_BANDS), (double)SD_AGC_FLOOR_DB, 0.0);
    /* garbage in */
    for (i = 0; i < SD_BANDS; i++) band[i] = make_nan();
    near1("snap: NaN bands are silence", (double)sd_agc_snap(&a, band, SD_BANDS), (double)SD_AGC_FLOOR_DB, 0.0);
    band[1] = make_inf();
    truth("snap: infinite band is limited", 0, sd_agc_snap(&a, band, SD_BANDS) <= 40.0f && a.refDb == a.refDb);
    a.refDb = make_nan();
    band[1] = -30.0f;
    sd_agc_quiet(&a, band, SD_BANDS);
    truth("quiet: a NaN reference recovers", 0, a.refDb == a.refDb && a.refDb >= SD_AGC_FLOOR_DB && a.refDb <= -30.0f);
    a.refDb = make_nan();
    near1("snap: a NaN reference recovers", (double)sd_agc_snap(&a, band, SD_BANDS), -30.0, 0.0);
    near1("snap: no bands is the floor", (double)sd_agc_snap(&a, band, 0), (double)SD_AGC_FLOOR_DB, 0.0);
    sd_agc_quiet(&a, band, 0);
    truth("quiet: no bands", 0, a.refDb == a.refDb && a.refDb >= SD_AGC_FLOOR_DB);
}

/* ------------------------------------------------------------------------------------------ band plan */

static void test_edges(void)
{
    int p, i;
    for (p = 0; p < EXP_NPLANS; p++) {
        float edges[SD_BANDS + 1];
        sd_band_edges((float)EXP_PLAN_FS[p], edges);
        for (i = 0; i <= SD_BANDS; i++) {
            near_at("band edge (relative)", p, i, (double)edges[i] / EXP_PLAN_EDGE[p][i], 1.0, 2e-6);
            if (i > 0) truth("edges ascending", p * 100 + i, edges[i] > edges[i - 1]);
        }
        near_at("first edge 30 Hz", p, -1, (double)edges[0], 30.0, 0.0);
        near_at("last edge min(16 kHz, Nyquist)", p, -1, (double)edges[SD_BANDS],
                EXP_PLAN_FS[p] * 0.5 < 16000.0 ? EXP_PLAN_FS[p] * 0.5 : 16000.0, 1e-2);
        /* the layout rules: bands grow from left to right (so do their widths), every band is at least 1.5 bins
         * of its own FFT wide, and the range below 500 Hz gets 7 columns (not 10.8 as on the plain log axis) */
        {
            int below = 0;
            for (i = 0; i < SD_BANDS; i++) {
                double width = (double)edges[i + 1] - (double)edges[i];
                double bin = EXP_PLAN_FS[p] / (double)EXP_PLAN_FFT[p][i];
                truth("band at least 1.5 bins wide", p * 100 + i, width >= 1.5 * bin);
                if (i > 0) truth("band wider than its left neighbour", p * 100 + i,
                                 width > (double)edges[i] - (double)edges[i - 1]);
                if (edges[i + 1] <= 510.0f) below++;
            }
            equal_at("columns below 510 Hz", p, -1, below, EXP_PLAN_BELOW_510[p]);
            /* where 500 Hz is on the panel, in columns: from the C edges, against the axis formula of the model.
             * 7 of 24 (29 %) with the full range; 7.6 (32 %) at 22.05 kHz, where the range ends at 11 kHz */
            for (i = 0; i < SD_BANDS; i++) {
                if (edges[i] <= 500.0f && 500.0f < edges[i + 1]) {
                    double x0 = log(1.0 + (double)edges[i] / (double)SD_WARP_HZ);
                    double x1 = log(1.0 + (double)edges[i + 1] / (double)SD_WARP_HZ);
                    double col = (double)i + (log(1.0 + 500.0 / (double)SD_WARP_HZ) - x0) / (x1 - x0);
                    near_at("column of 500 Hz", p, -1, col, EXP_PLAN_COL500[p], 1e-3);
                    if (EXP_PLAN_FS[p] >= 32000.0) {
                        truth("500 Hz within the first 7 columns", p, col <= 7.0);
                        equal_at("7 columns below 500 Hz", p, -1, below, 7);
                    } else if (EXP_PLAN_FS[p] >= 22050.0) {
                        truth("500 Hz within the first 7.7 columns at 22.05 kHz", p, col <= 7.7);
                    }
                    break;
                }
            }
            truth("500 Hz is inside the range", p, i < SD_BANDS);
            if (EXP_PLAN_FS[p] >= 32000.0) {    /* full range up to 16 kHz: edge 7 is 501.5 Hz */
                near_at("edge 7 at 500 Hz", p, -1, (double)edges[7], 501.5146, 0.01);
            }
        }
    }
    for (i = 0; i < EXP_NEDGE; i++) {
        uint16_t bin = 0;
        float frac = -1.0f;
        sd_edge_to_bin((float)EXP_EDGE[i][1], 44100.0f, (int)EXP_EDGE[i][0], &bin, &frac);
        equal_at("edge bin", i, -1, (long)bin, (long)EXP_EDGE[i][2]);
        near_at("edge frac", i, -1, (double)frac, EXP_EDGE[i][3], 1e-5);
    }
}

static void test_plan_one(int p)
{
    static sd_fft_t low, high;
    sd_fft_t* fft[2];
    int which, i, total = 0, seen[SD_BANDS];
    int lowOk, highOk;
    float fs = (float)EXP_PLAN_FS[p];

    setup_pair(&low, &high);
    low.scale = 0.5f;           /* sentinels: planning must not touch size, fps, scale, the pointers */
    high.scale = 0.25f;
    for (i = 0; i < LOW_N / 2; i++) { g_smoothLow[i] = 1.0f; g_dbLow[i] = 1.0f; }
    for (i = 0; i < HIGH_N / 2; i++) { g_smoothHigh[i] = 1.0f; g_dbHigh[i] = 1.0f; }

    equal_at("sd_plan_bands returns 24", p, -1, sd_plan_bands(&low, &high, fs, SPLIT_HZ), SD_BANDS);

    fft[0] = &low;
    fft[1] = &high;
    for (i = 0; i < SD_BANDS; i++) seen[i] = 0;
    for (which = 0; which < 2; which++) {
        sd_fft_t* f = fft[which];
        int n = which == 0 ? LOW_N : HIGH_N;
        int j;
        const int* sum = &EXP_PLAN_SUM[p][which * 3];
        equal_at("plan nBands", p, which, (long)f->nBands, sum[0]);
        equal_at("plan firstBin", p, which, (long)f->firstBin, sum[1]);
        equal_at("plan lastBin", p, which, (long)f->lastBin, sum[2]);
        total += f->nBands;
        for (j = 0; j < (int)f->nBands; j++) {
            int band = (int)f->bandIndex[j];
            truth("band index in range", p * 100 + band, band >= 0 && band < SD_BANDS);
            if (band < 0 || band >= SD_BANDS) continue;
            seen[band]++;
            if (j > 0) truth("band indices ascending", p * 100 + band, band > (int)f->bandIndex[j - 1]);
            equal_at("band belongs to this FFT", p, band, (long)EXP_PLAN_FFT[p][band], (long)n);
            equal_at("band loBin", p, band, (long)f->map[j].loBin, (long)EXP_PLAN_LOBIN[p][band]);
            equal_at("band hiBin", p, band, (long)f->map[j].hiBin, (long)EXP_PLAN_HIBIN[p][band]);
            near_at("band loFrac", p, band, (double)f->map[j].loFrac, EXP_PLAN_LOFRAC[p][band], 1e-5);
            near_at("band hiFrac", p, band, (double)f->map[j].hiFrac, EXP_PLAN_HIFRAC[p][band], 1e-5);
            near_at("band tilt dB", p, band, (double)f->tiltDb[j], EXP_PLAN_TILT[p][band], 1e-4);
            truth("band spans more than one bin", p * 100 + band, f->map[j].hiBin > f->map[j].loBin);
            if (j > 0) truth("band reads other bins than its left neighbour", p * 100 + band,
                             f->map[j].loBin > f->map[j - 1].loBin && f->map[j].hiBin > f->map[j - 1].hiBin);
            truth("band bins inside the fed range", p * 100 + band,
                  f->map[j].loBin >= f->firstBin && (int)f->map[j].hiBin <= (int)f->lastBin);
        }
    }
    equal_at("plan covers 24 bands", p, -1, total, SD_BANDS);
    for (i = 0; i < SD_BANDS; i++) equal_at("every band in exactly one FFT", p, i, seen[i], 1);

    /* centre < 500 Hz decides */
    for (i = 0; i < SD_BANDS; i++) {
        truth("split at 500 Hz", p * 100 + i, (EXP_PLAN_CENTRE[p][i] < 500.0) == (EXP_PLAN_FFT[p][i] == LOW_N));
    }

    equal_at("size untouched (low)", p, 0, (long)low.size, LOW_N);
    equal_at("size untouched (high)", p, 0, (long)high.size, HIGH_N);
    equal_at("nBins (low)", p, 0, (long)low.nBins, LOW_N / 2);
    equal_at("nBins (high)", p, 0, (long)high.nBins, HIGH_N / 2);
    near_at("fps untouched", p, 0, (double)low.fps, (double)LOW_FPS, 0.0);
    near_at("fps untouched", p, 1, (double)high.fps, (double)HIGH_FPS, 0.0);
    near_at("scale untouched", p, 0, (double)low.scale, 0.5, 0.0);
    near_at("scale untouched", p, 1, (double)high.scale, 0.25, 0.0);
    truth("pointers untouched", p, low.smooth == g_smoothLow && low.db == g_dbLow && high.smooth == g_smoothHigh &&
                                       high.db == g_dbHigh);
    near_at("retention low", p, 0, (double)low.retention, 0.49, 1e-6);
    near_at("retention high", p, 1, (double)high.retention, 0.807344, 1e-6);

    lowOk = highOk = 1;
    for (i = 0; i < LOW_N / 2; i++) if (g_smoothLow[i] != 0.0f || g_dbLow[i] != -200.0f) lowOk = 0;
    for (i = 0; i < HIGH_N / 2; i++) if (g_smoothHigh[i] != 0.0f || g_dbHigh[i] != -200.0f) highOk = 0;
    truth("low arrays cleared (smooth 0, dB -200)", p, lowOk);
    truth("high arrays cleared (smooth 0, dB -200)", p, highOk);
}

static void test_plan_degenerate(void)
{
    static sd_fft_t low, high;
    setup_pair(&low, &high);

    /* nothing below the split: the low FFT has no band, the high one all 24 */
    equal1("split 0: returns 24", sd_plan_bands(&low, &high, 44100.0f, 0.0f), SD_BANDS);
    equal1("split 0: low nBands", low.nBands, 0);
    equal1("split 0: low firstBin", low.firstBin, 1);
    equal1("split 0: low lastBin", low.lastBin, 0);
    equal1("split 0: high nBands", high.nBands, SD_BANDS);
    equal1("split 0: high bandIndex[0]", high.bandIndex[0], 0);
    equal1("split 0: high bandIndex[23]", high.bandIndex[SD_BANDS - 1], SD_BANDS - 1);
    near1("split 0: a band that does not exist is silent", (double)sd_fft_band_db(&low, 0), (double)SD_SILENCE_DB, 0.0);

    /* everything below the split */
    equal1("split high: returns 24", sd_plan_bands(&low, &high, 44100.0f, 1.0e9f), SD_BANDS);
    equal1("split high: low nBands", low.nBands, SD_BANDS);
    equal1("split high: high nBands", high.nBands, 0);
    equal1("split high: high firstBin", high.firstBin, 1);
    equal1("split high: high lastBin", high.lastBin, 0);

    /* invalid arguments change nothing and say so */
    low.nBands = 7;
    equal1("fs 0 refused", sd_plan_bands(&low, &high, 0.0f, SPLIT_HZ), 0);
    equal1("fs 0 leaves the plan", low.nBands, 7);
    equal1("NULL refused", sd_plan_bands(0, &high, 44100.0f, SPLIT_HZ), 0);

    /* setup: documented initial state */
    setup_pair(&low, &high);
    equal1("setup nBands", low.nBands, 0);
    {
        sd_fft_t fresh;
        sd_fft_setup(&fresh, LOW_N, LOW_FPS, g_smoothLow, g_dbLow);
        near1("setup scale is 1 until the caller sets it", (double)fresh.scale, 1.0, 0.0);
        equal1("setup keeps the size", fresh.size, LOW_N);
    }
    equal1("setup firstBin > lastBin", low.firstBin > low.lastBin, 1);
    equal1("setup nBins", low.nBins, LOW_N / 2);
    /* a re-plan at another sample rate starts from scratch */
    equal1("replan 44100", sd_plan_bands(&low, &high, 44100.0f, SPLIT_HZ), SD_BANDS);
    sd_fft_bin(&high, 100, 0.01f);
    truth("state after one frame", 0, g_smoothHigh[100] > 0.0f && g_dbHigh[100] > -200.0f);
    equal1("replan 48000", sd_plan_bands(&low, &high, 48000.0f, SPLIT_HZ), SD_BANDS);
    near1("re-plan clears smooth", (double)g_smoothHigh[100], 0.0, 0.0);
    near1("re-plan clears dB", (double)g_dbHigh[100], -200.0, 0.0);
    near1("re-plan keeps the scale the caller set", (double)high.scale, (double)g_scaleHigh(), 0.0);
}

/* ------------------------------------------------------------------------------------------ band value */

static void test_band_db(void)
{
    static float d[512];
    int c, k, o;
    for (c = 0; c < EXP_NBD; c++) {
        sd_edgemap_t m;
        for (k = 0; k < 512; k++) d[k] = (EXP_BD_KIND[c] == 0) ? (float)(-90 + k) : (float)EXP_BD_BASE[c];
        for (o = 0; o < 4; o++) {
            if (EXP_BD_OVER_IDX[c][o] >= 0) d[EXP_BD_OVER_IDX[c][o]] = (float)EXP_BD_OVER_VAL[c][o];
        }
        m.loBin = (uint16_t)EXP_BD_LOBIN[c];
        m.hiBin = (uint16_t)EXP_BD_HIBIN[c];
        m.loFrac = (float)EXP_BD_LOFRAC[c];
        m.hiFrac = (float)EXP_BD_HIFRAC[c];
        near_at("band value in dB", c, -1, (double)sd_band_db(d, 512, &m), EXP_BD_VALUE[c], 1e-4);
    }
    /* the specification's own figures */
    {
        sd_edgemap_t m;
        for (k = 0; k < 512; k++) d[k] = -90.0f + (float)k;
        m.loBin = 10; m.loFrac = 0.25f; m.hiBin = 14; m.hiFrac = 0.5f;
        near1("ramp -75.5", (double)sd_band_db(d, 512, &m), -75.5, 1e-4);
        for (k = 0; k < 512; k++) d[k] = -100.0f;
        d[12] = -40.0f;
        m.loFrac = 0.5f;
        near1("spike between -40", (double)sd_band_db(d, 512, &m), -40.0, 1e-4);
        for (k = 0; k < 512; k++) d[k] = -100.0f;
        d[510] = -70.0f; d[511] = -50.0f;
        m.loBin = 510; m.loFrac = 0.5f; m.hiBin = 511; m.hiFrac = 0.9f;
        near1("last bin -50", (double)sd_band_db(d, 512, &m), -50.0, 1e-4);
    }
}

/* ------------------------------------------------------------------------------------------ peaks and rows */

static void test_peaks(void)
{
    sd_peak_t p;
    int t;
    double minLevel = 1.0;

    sd_peak_reset(&p);
    near1("reset level", (double)sd_peak_level(&p), 0.0, 0.0);
    near1("reset hold", (double)p.hold, 0.0, 0.0);

    for (t = 0; t < EXP_NPEAK; t++) {
        sd_peak_step(&p, t < EXP_PEAK_BAR_TICKS ? (float)0.8 : 0.0f, TICKS_PER_S);
        near_at("peak level", t, -1, (double)sd_peak_level(&p), EXP_PEAK_LEVEL[t], 1e-4);
        equal_at("peak rows", t, -1, sd_bar_rows(sd_peak_level(&p)), EXP_PEAK_ROWS[t]);
        if (t >= EXP_PEAK_BAR_TICKS && t <= 54) {
            near_at("peak held exactly 50 ticks", t, -1, (double)sd_peak_level(&p), (double)(float)0.8, 0.0);
        }
        truth("peak never rises while the bar is 0", t, (double)sd_peak_level(&p) <= minLevel + 1e-9);
        minLevel = (double)sd_peak_level(&p);
    }

    /* from a full bar */
    sd_peak_reset(&p);
    for (t = 0; t < 5; t++) sd_peak_step(&p, 1.0f, TICKS_PER_S);
    for (t = 5; t < 200; t++) {
        sd_peak_step(&p, 0.0f, TICKS_PER_S);
        if (sd_peak_level(&p) <= 0.0f) break;
    }
    equal1("full bar: peak reaches 0 at tick", t, EXP_PEAK_ZERO_TICK_FROM_1);

    /* a bar below the level changes nothing; at or above takes over and reloads the hold */
    sd_peak_reset(&p);
    sd_peak_step(&p, 0.8f, TICKS_PER_S);
    for (t = 0; t < 30; t++) sd_peak_step(&p, 0.5f, TICKS_PER_S);
    near1("lower bar during hold", (double)sd_peak_level(&p), 0.8, 1e-6);
    for (t = 0; t < 40; t++) sd_peak_step(&p, 0.0f, TICKS_PER_S);          /* 70 ticks since the peak: falling */
    truth("falling", 0, sd_peak_level(&p) < 0.8f);
    sd_peak_step(&p, 0.9f, TICKS_PER_S);
    near1("new maximum takes over at once", (double)sd_peak_level(&p), 0.9, 1e-6);
    for (t = 0; t < 50; t++) sd_peak_step(&p, 0.0f, TICKS_PER_S);
    near1("hold reloaded: 50 ticks at 0.9", (double)sd_peak_level(&p), 0.9, 1e-6);
    sd_peak_step(&p, 0.0f, TICKS_PER_S);
    truth("then it falls", 0, sd_peak_level(&p) < 0.9f);
    sd_peak_step(&p, 0.9f, TICKS_PER_S);
    sd_peak_step(&p, 0.9f, TICKS_PER_S);
    near1("equal value keeps the level", (double)sd_peak_level(&p), 0.9, 1e-6);
    near1("equal value reloads the hold", (double)p.hold, 50.0, 0.0);
    /* garbage in */
    sd_peak_step(&p, make_nan(), TICKS_PER_S);
    truth("NaN bar is 0", 0, sd_peak_level(&p) == sd_peak_level(&p) && sd_peak_level(&p) <= 0.9f);
    sd_peak_reset(&p);
    sd_peak_step(&p, 7.0f, TICKS_PER_S);
    near1("bar above 1 clamped", (double)sd_peak_level(&p), 1.0, 0.0);
    /* a faster tick rate scales hold and fall */
    sd_peak_reset(&p);
    sd_peak_step(&p, 0.5f, 200.0f);
    near1("hold at 200 ticks/s", (double)p.hold, 100.0, 0.0);
}

static void test_rows(void)
{
    int i, c;
    for (i = 0; i < EXP_NROWS; i++) {
        equal_at("rows", i, -1, sd_bar_rows((float)EXP_ROWS_V[i]), EXP_ROWS_N[i]);
    }
    for (i = 0; i < EXP_NGRID; i++) {
        equal_at("rows grid", i, -1, sd_bar_rows((float)i / 256.0f), EXP_ROWS_GRID[i]);
    }
    equal1("rows NaN", sd_bar_rows(make_nan()), 0);
    equal1("rows +inf", sd_bar_rows(make_inf()), 16);
    equal1("rows -inf", sd_bar_rows(-make_inf()), 0);
    for (i = 0; i <= 4096; i++) {
        int rows = sd_bar_rows((float)i / 4096.0f);
        truth("rows are 0..16", i, rows >= 0 && rows <= SD_ROWS);
        truth("rows monotonic", i, i == 0 || rows >= sd_bar_rows((float)(i - 1) / 4096.0f));
    }
    /* solid bar: every row count 0..16 is reachable, k / 16 gives exactly k rows */
    for (c = 0; c <= SD_ROWS; c++) {
        equal_at("rows of k/16", c, -1, sd_bar_rows((float)c / (float)SD_ROWS), c);
    }
}

/* ------------------------------------------------------------------------------------------ whole chain */

/* rows may differ by one from the reference only when its level sits on a rounding boundary (k + 0.5) / 16 */
static void check_rows_vs_reference(const char* what, int t, int band, int got, int want, double refLevel)
{
    double x = refLevel * 16.0 - 0.5;
    double nearest = floor(x + 0.5);
    if (got != want && fabs(x - nearest) < 0.02 && (got - want <= 1 && want - got <= 1)) {
        g_checks++;     /* one row off at the exact boundary */
        return;
    }
    equal_at(what, t, band, got, want);
}

/* The test's frame schedule (specification 4.8): every 10 ms tick one frame of the high FFT, on 3 ticks of 10 a
 * frame of the low FFT (30 per second), then the automatic gain on the held band values. */
static int low_frame_due(int tick)
{
    return ((tick + 1) * 3) / 10 > (tick * 3) / 10;
}

typedef struct {
    sd_fft_t low, high;
    sd_agc_t agc;
    float held[SD_BANDS];
    int tick;
} chain_t;

static chain_t g_chain;
static float g_rawLow[LOW_N / 2];
static float g_rawHigh[HIGH_N / 2];
static float g_frameLow[LOW_N / 2];
static float g_frameHigh[HIGH_N / 2];

static void chain_start(chain_t* c, float fs)
{
    int i;
    setup_pair(&c->low, &c->high);
    equal1("chain plan", sd_plan_bands(&c->low, &c->high, fs, SPLIT_HZ), SD_BANDS);
    sd_agc_reset(&c->agc, TICKS_PER_S);
    for (i = 0; i < SD_BANDS; i++) c->held[i] = SD_SILENCE_DB;
    c->tick = 0;
}

static void chain_frame(chain_t* c, sd_fft_t* f, const float* raw, float* frame, float gain)
{
    int k, j;
    for (k = (int)f->firstBin; k <= (int)f->lastBin; k++) frame[k] = raw[k] * gain;
    feed_frame(f, frame);
    for (j = 0; j < (int)f->nBands; j++) c->held[f->bandIndex[j]] = sd_fft_band_db(f, j);
}

/* One tick with the spectra in g_rawLow / g_rawHigh times gain. */
static void chain_tick(chain_t* c, float gain)
{
    chain_frame(c, &c->high, g_rawHigh, g_frameHigh, gain);
    if (low_frame_due(c->tick)) chain_frame(c, &c->low, g_rawLow, g_frameLow, gain);
    sd_agc_step(&c->agc, c->held, SD_BANDS);
    c->tick++;
}

static void test_tones(void)
{
    chain_t* c = &g_chain;
    int t, b, i;

    for (t = 0; t < EXP_NTONES; t++) {
        float fs = (float)EXP_TONE[t][0];
        int loudest = 0, toneBands = 0;

        chain_start(c, fs);
        tone_spectrum(LOW_N, (double)fs, EXP_TONE[t][1], EXP_TONE[t][2], EXP_TONE[t][3], EXP_TONE[t][4],
                      c->low.firstBin, c->low.lastBin, g_rawLow);
        tone_spectrum(HIGH_N, (double)fs, EXP_TONE[t][1], EXP_TONE[t][2], EXP_TONE[t][3], EXP_TONE[t][4],
                      c->high.firstBin, c->high.lastBin, g_rawHigh);
        for (i = 0; i < 1000; i++) chain_tick(c, 1.0f);     /* 10 s: smoothing and reference have settled */

        near_at("tone: reference dB", t, -1, (double)c->agc.refDb, EXP_TONE_REF[t], 0.02);
        for (b = 0; b < SD_BANDS; b++) {
            float level = sd_agc_level(&c->agc, c->held[b]);
            int rows = sd_bar_rows(level);
            int flag = EXP_TONE_SPEC_FLAG[t][b];
            double refLevel = EXP_TONE_LEVEL[t][b];

            if (EXP_TONE_DB[t][b] > -100.0) near_at("tone dB (reference)", t, b, (double)c->held[b], EXP_TONE_DB[t][b], 0.05);
            near_at("tone level (reference)", t, b, (double)level, refLevel, 1.5e-3);
            check_rows_vs_reference("tone rows (reference)", t, b, rows, EXP_TONE_ROWS[t][b], refLevel);
            truth("tone: no band above 15 rows in steady state", t * 100 + b, rows < SD_ROWS);
            if (c->held[b] > c->held[loudest]) loudest = b;
            if (flag == 2) {            /* a band holding a tone: 0.2 dB and exact rows */
                toneBands++;
                near_at("tone dB (spec, +-0.2)", t, b, (double)c->held[b], EXP_TONE_SPEC_DB[t][b], 0.2);
                equal_at("tone rows (spec, exact)", t, b, rows, EXP_TONE_SPEC_ROWS[t][b]);
            } else if (flag == 1) {     /* neighbours: 1.5 dB and +-1 row */
                near_at("tone dB (spec, +-1.5)", t, b, (double)c->held[b], EXP_TONE_SPEC_DB[t][b], 1.5);
                near_at("tone rows (spec, +-1)", t, b, (double)rows, (double)EXP_TONE_SPEC_ROWS[t][b], 1.0);
            } else {                    /* unlisted: at most two lit rows */
                truth("tone unlisted rows <= 2", t * 100 + b, rows <= 2);
            }
        }
        if (EXP_TONE[t][2] > 0.0) {
            truth("tone: its band is listed", t, toneBands >= 1);
            /* automatic gain: whatever the amplitude and the sample rate, the loudest band settles at 14 rows -
             * unless it is below the floor (the -60 dBFS tone), which is shown lower */
            if (EXP_TONE_REF[t] > (double)SD_AGC_FLOOR_DB + 0.01) {
                equal_at("tone: loudest band 14 rows", t, loudest, sd_bar_rows(sd_agc_level(&c->agc, c->held[loudest])), 14);
                near_at("tone: loudest band level 0.875", t, loudest, (double)sd_agc_level(&c->agc, c->held[loudest]), 0.875, 1e-3);
            } else {
                truth("tone below the floor: fewer than 14 rows", t, sd_bar_rows(sd_agc_level(&c->agc, c->held[loudest])) < 14);
            }
        } else {
            for (b = 0; b < SD_BANDS; b++) {
                near_at("silence: band dB", t, b, (double)c->held[b], (double)SD_SILENCE_DB, 0.0);
                equal_at("silence: rows", t, b, sd_bar_rows(sd_agc_level(&c->agc, c->held[b])), 0);
            }
            near_at("silence: reference at the floor", t, -1, (double)c->agc.refDb, (double)SD_AGC_FLOOR_DB, 0.0);
        }
    }
}

/* Loud bass and quiet treble together, at three loudness levels 12 dB apart: the picture is the same. */
static void test_bass_and_treble(void)
{
    chain_t* c = &g_chain;
    static const float gains[3] = {1.0f, 0.25f, 0.0625f};
    int rows[3][SD_BANDS];
    int g, b, i, bass = 0, treble = 0;

    for (g = 0; g < 3; g++) {
        chain_start(c, 44100.0f);
        tone_spectrum(LOW_N, 44100.0, 60.0, 0.5, 8000.0, 0.005, c->low.firstBin, c->low.lastBin, g_rawLow);
        tone_spectrum(HIGH_N, 44100.0, 60.0, 0.5, 8000.0, 0.005, c->high.firstBin, c->high.lastBin, g_rawHigh);
        for (i = 0; i < 1000; i++) {
            chain_tick(c, gains[g]);
            if (i > 100) {
                for (b = 0; b < SD_BANDS; b++) {
                    if (sd_bar_rows(sd_agc_level(&c->agc, c->held[b])) >= SD_ROWS) break;
                }
                truth("bass + treble: no column at 16 rows once settled", g * 10000 + i, b == SD_BANDS);
            }
        }
        for (b = 0; b < SD_BANDS; b++) rows[g][b] = sd_bar_rows(sd_agc_level(&c->agc, c->held[b]));
    }
    for (b = 0; b < SD_BANDS; b++) {
        equal_at("bass + treble: same rows 12 dB quieter", 1, b, rows[1][b], rows[0][b]);
        equal_at("bass + treble: same rows 24 dB quieter", 2, b, rows[2][b], rows[0][b]);
        if (rows[0][b] > rows[0][bass]) bass = b;
    }
    for (b = 12; b < SD_BANDS; b++) if (rows[0][b] > rows[0][treble] || treble < 12) treble = b;
    truth("bass + treble: the loudest column is a bass column", bass, bass <= 2);
    equal1("bass + treble: bass at 14 rows, not pinned", rows[0][bass], 14);
    truth("bass + treble: the treble tone 40 dB lower is still visible", treble, rows[0][treble] >= 5 && rows[0][treble] <= 8);
}

/* A 1 kHz tone from silence, then 18 dB louder, then 38 dB quieter, then off (specification 4.8). */
static void test_timeline(void)
{
    chain_t* c = &g_chain;
    int seg, i, k = 0, b;
    int band = EXP_TL_BAND;

    chain_start(c, 44100.0f);
    tone_spectrum(LOW_N, 44100.0, (double)EXP_TL_HZ, 1.0, 0.0, 0.0, c->low.firstBin, c->low.lastBin, g_rawLow);
    tone_spectrum(HIGH_N, 44100.0, (double)EXP_TL_HZ, 1.0, 0.0, 0.0, c->high.firstBin, c->high.lastBin, g_rawHigh);
    for (seg = 0; seg < EXP_NTL_SEG; seg++) {
        for (i = 0; i < (int)EXP_TL_SEG[seg][0]; i++) {
            int tick = c->tick;
            float top = SD_SILENCE_DB;
            chain_tick(c, (float)EXP_TL_SEG[seg][1]);
            for (b = 0; b < SD_BANDS; b++) if (c->held[b] > top) top = c->held[b];
            truth("timeline: loudest band never above the top row", tick, top <= c->agc.refDb + SD_AGC_HEADROOM_DB + 1e-3f);
            truth("timeline: reference never below the floor", tick, c->agc.refDb >= SD_AGC_FLOOR_DB);
            if (seg == 3 && i >= 30) {      /* 0.3 s after the tone stopped: dark, and it stays dark */
                for (b = 0; b < SD_BANDS; b++) {
                    if (sd_bar_rows(sd_agc_level(&c->agc, c->held[b])) != 0) break;
                }
                truth("timeline: silence is not amplified", tick, b == SD_BANDS);
            }
            if (k < EXP_NTL && EXP_TL_TICK[k] == tick) {
                near_at("timeline: reference dB", tick, -1, (double)c->agc.refDb, EXP_TL_REF[k], 0.02);
                for (b = 0; b < SD_BANDS; b++) {
                    float level = sd_agc_level(&c->agc, c->held[b]);
                    near_at("timeline: level", tick, b, (double)level, EXP_TL_LEVEL[k][b], 2e-3);
                    check_rows_vs_reference("timeline: rows", tick, b, sd_bar_rows(level), EXP_TL_ROWS[k][b], EXP_TL_LEVEL[k][b]);
                }
                k++;
            }
        }
    }
    equal1("timeline: every listed tick checked", k, EXP_NTL);
    /* the specification's figures for the tone's band */
    equal1("timeline: 14 rows after 1 s", EXP_TL_ROWS[7][band], 14);
    equal1("timeline: 14 rows again 1 s after +18 dB", EXP_TL_ROWS[16][band], 14);
    truth("timeline: visible 1 s after -38 dB", 0, EXP_TL_ROWS[24][band] >= 4);
    truth("timeline: 12 rows or more 6 s after -38 dB", 0, EXP_TL_ROWS[27][band] >= 12);
    equal1("timeline: dark at the end", EXP_TL_ROWS[EXP_NTL - 1][band], 0);
}

static void response_up(sd_fft_t* f, const float* raw, int band, const int* frames, const double* want, int count,
                        const char* what)
{
    int n = 0, i;
    int j = 0;
    while (j < (int)f->nBands && (int)f->bandIndex[j] != band) j++;
    for (i = 0; i < count; i++) {
        while (n < frames[i]) { feed_frame(f, raw); n++; }
        near_at(what, frames[i], -1, (double)sd_fft_band_db(f, j), want[i], 0.02);
    }
}

static void test_time_response(void)
{
    static sd_fft_t low, high;
    static float rawLow[LOW_N / 2];
    static float rawHigh[HIGH_N / 2];
    int i;

    /* 1 kHz, A = 0.125, high FFT: switch on, then off */
    setup_pair(&low, &high);
    sd_plan_bands(&low, &high, 44100.0f, SPLIT_HZ);
    tone_spectrum(HIGH_N, 44100.0, 1000.0, 0.125, 0.0, 0.0, high.firstBin, high.lastBin, rawHigh);
    response_up(&high, rawHigh, EXP_TR_HI_BAND, EXP_TR_HI_ON_N, EXP_TR_HI_ON_DB, EXP_NTR_HI_ON, "1 kHz step response (dB)");
    {
        int j = 0, k = 0;
        while ((int)high.bandIndex[j] != EXP_TR_HI_BAND) j++;
        for (i = 1; i <= EXP_TR_HI_OFF_N[EXP_NTR_HI_OFF - 1]; i++) {
            feed_zero_frame(&high);
            if (EXP_TR_HI_OFF_N[k] == i) {
                near_at("1 kHz decay (dB)", i, -1, (double)sd_fft_band_db(&high, j), EXP_TR_HI_OFF_DB[k], 0.02);
                k++;
            }
        }
        equal1("decay checked at every listed frame", k, EXP_NTR_HI_OFF);
        for (i = 0; i < 200; i++) feed_zero_frame(&high);
        near1("silent in the end", (double)sd_fft_band_db(&high, j), (double)SD_SILENCE_DB, 0.0);
    }

    /* 100 Hz, A = 0.125, low FFT */
    setup_pair(&low, &high);
    sd_plan_bands(&low, &high, 44100.0f, SPLIT_HZ);
    tone_spectrum(LOW_N, 44100.0, 100.0, 0.125, 0.0, 0.0, low.firstBin, low.lastBin, rawLow);
    response_up(&low, rawLow, EXP_TR_LO_BAND, EXP_TR_LO_ON_N, EXP_TR_LO_ON_DB, EXP_NTR_LO_ON, "100 Hz step response (dB)");
}

/* ------------------------------------------------------------------------------------------ robustness */

static void test_robustness(void)
{
    static sd_fft_t low, high;
    int k;
    setup_pair(&low, &high);
    sd_plan_bands(&low, &high, 44100.0f, SPLIT_HZ);

    /* garbage never poisons the filter */
    sd_fft_bin(&high, 50, make_nan());
    near1("NaN magnitude is silence", (double)g_smoothHigh[50], 0.0, 0.0);
    sd_fft_bin(&high, 50, make_inf());
    near1("infinite magnitude is silence", (double)g_smoothHigh[50], 0.0, 0.0);
    sd_fft_bin(&high, 50, 0.001f);
    truth("recovers after garbage", 0, g_smoothHigh[50] > 0.0f);
    /* out-of-range bins are ignored (guard bytes stay untouched: only the arrays exist, nothing crashes) */
    sd_fft_bin(&high, -1, 1.0f);
    sd_fft_bin(&high, HIGH_N / 2, 1.0f);
    sd_fft_bin(&high, 100000, 1.0f);
    truth("out-of-range bins ignored", 0, g_smoothHigh[HIGH_N / 2 - 1] == 0.0f);
    near1("band dB j<0", (double)sd_fft_band_db(&high, -1), (double)SD_SILENCE_DB, 0.0);
    near1("band dB j>=nBands", (double)sd_fft_band_db(&high, high.nBands), (double)SD_SILENCE_DB, 0.0);
    near1("band dB j huge", (double)sd_fft_band_db(&high, 1000), (double)SD_SILENCE_DB, 0.0);
    /* silence decays to exactly 0 (no denormal tail) */
    sd_fft_bin(&high, 60, 0.5f);
    for (k = 0; k < 3000; k++) sd_fft_bin(&high, 60, 0.0f);
    near1("silence reaches exactly 0", (double)g_smoothHigh[60], 0.0, 0.0);
    near1("silence dB", (double)g_dbHigh[60], -200.0, 0.0);
    /* silent frame: every band is silent, whatever its tilt */
    setup_pair(&low, &high);
    sd_plan_bands(&low, &high, 44100.0f, SPLIT_HZ);
    for (k = 0; k < 5; k++) { feed_zero_frame(&low); feed_zero_frame(&high); }
    for (k = 0; k < (int)low.nBands; k++) near_at("silence (low)", k, -1, (double)sd_fft_band_db(&low, k), (double)SD_SILENCE_DB, 0.0);
    for (k = 0; k < (int)high.nBands; k++) near_at("silence (high)", k, -1, (double)sd_fft_band_db(&high, k), (double)SD_SILENCE_DB, 0.0);
    /* a band value is the bins' value plus the band's tilt */
    for (k = 0; k < HIGH_N / 2; k++) g_dbHigh[k] = -50.0f;
    near1("band dB = value + tilt", (double)sd_fft_band_db(&high, 5), -50.0 + (double)high.tiltDb[5], 1e-5);
    truth("tilt rises with the band", 0, high.tiltDb[5] > high.tiltDb[4] && low.tiltDb[0] < -13.0f &&
                                         high.tiltDb[high.nBands - 1] > 11.0f);
    /* band value of a map reaching beyond the array is clamped, not read out of bounds */
    {
        static float d[8];
        sd_edgemap_t m;
        for (k = 0; k < 8; k++) d[k] = -50.0f;
        m.loBin = 6; m.hiBin = 400; m.loFrac = 0.5f; m.hiFrac = 0.5f;
        near1("band value clamps bins", (double)sd_band_db(d, 8, &m), -50.0, 1e-6);
        near1("band value with no bins", (double)sd_band_db(d, 0, &m), -200.0, 0.0);
    }
    /* constants of the fixed look */
    equal1("SD_BANDS", SD_BANDS, 24);
    equal1("SD_ROWS", SD_ROWS, 16);
    near1("SD_SILENCE_DB", (double)SD_SILENCE_DB, -200.0, 0.0);
    near1("SD_WARP_HZ", (double)SD_WARP_HZ, 140.0, 0.0);
    near1("SD_TILT_DB_PER_OCTAVE", (double)SD_TILT_DB_PER_OCTAVE, 3.0, 0.0);
    near1("SD_TILT_REF_HZ", (double)SD_TILT_REF_HZ, 1000.0, 0.0);
    near1("SD_AGC_RANGE_DB", (double)SD_AGC_RANGE_DB, 48.0, 0.0);
    near1("SD_AGC_HEADROOM_DB", (double)SD_AGC_HEADROOM_DB, 6.0, 0.0);
    near1("SD_AGC_FLOOR_DB", (double)SD_AGC_FLOOR_DB, -60.0, 0.0);
    near1("SD_PEAK_FALL", (double)SD_PEAK_FALL, 3800.0 / 276.0, 1e-6);
    equal1("SD_PEAK_HOLD_MS", SD_PEAK_HOLD_MS, 500);
}

int main(void)
{
    test_window_and_scale();
    test_smoothing_db();
    test_tilt_and_agc_level();
    test_agc_sequence();
    test_agc_moves();
    test_edges();
    {
        int p;
        for (p = 0; p < EXP_NPLANS; p++) test_plan_one(p);
    }
    test_plan_degenerate();
    test_band_db();
    test_peaks();
    test_rows();
    test_tones();
    test_bass_and_treble();
    test_timeline();
    test_time_response();
    test_robustness();

    printf("%d checks, %d failed\n", g_checks, g_failed);
    if (g_failed == 0) {
        printf("ALL PASSED\n");
        return 0;
    }
    printf("FAILED\n");
    return 1;
}
