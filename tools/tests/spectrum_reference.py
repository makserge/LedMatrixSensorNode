#!/usr/bin/env python3
"""Independent reference for the LED spectrum chain (src/spectrum_dsp.cpp).

Written from SPEC-LED.md and public standards (W3C Web Audio API AnalyserNode maths); no third-party code.

Pure Python 3, standard library only, double precision. It re-computes every expected number of the acceptance
table (band plans and bin maps, tilt, scalars, band values, automatic gain, peak trajectory, LED rows, whole-chain
tone responses) from the formulas alone, never from the C code, and writes test/host/spectrum_expected.h for the
host test. Before it writes anything it also checks its own results against the literal numbers printed in the
specification (SPEC_* tables below), so a mistake in a formula shows up here and not as a "passing" test.

    python3 -I -B tools/tests/spectrum_reference.py            # (re)write test/host/spectrum_expected.h
    python3 -I -B tools/tests/spectrum_reference.py --check    # exit 1 if the header is out of date
    python3 -I -B tools/tests/spectrum_reference.py --music    # music-like input, before / after 2026-11-25 (report)
    python3 -I -B tools/tests/spectrum_reference.py --panel    # the test signals of TESTING.md with the sleep logic (report)
    python3 -I -B tools/tests/spectrum_reference.py --print-literals   # SPEC_* blocks and tables from the formulas
"""
import cmath
import math
import os
import sys

HEADER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "test", "host",
                           "spectrum_expected.h")

# ---------------------------------------------------------------------------------------------- fixed look
BANDS = 24
TAU = 0.7
MIN_HZ, MAX_HZ = 30.0, 16000.0
WARP_HZ = 140.0                # band axis: equal steps of ln(1 + f / WARP_HZ)
TILT_DB_PER_OCTAVE = 3.0       # band weighting, 0 dB at TILT_REF_HZ
TILT_REF_HZ = 1000.0
AGC_RANGE_DB = 48.0            # bottom row .. top row
AGC_HEADROOM_DB = 6.0          # top row above the reference
AGC_FLOOR_DB = -60.0           # the reference never goes below this
AGC_ATTACK_S = 0.10            # time constants of the reference
AGC_RELEASE_S = 3.0
SILENCE_DB = -200.0
PEAK_HOLD_TICKS = 50           # 500 ms at 100 ticks/s
PEAK_FALL = 13.768116          # panel heights per s^2
TICK_FPS = 100.0
SPLIT_HZ = 500.0
LOW_N, LOW_FPS = 2048, 30.0
HIGH_N, HIGH_FPS = 1024, 100.0


# ---------------------------------------------------------------------------------------------- formulas
def window(r):
    """Blackman, Web Audio form with alpha = 0.16."""
    a = 0.16
    return (1 - a) / 2 - 0.5 * math.cos(2 * math.pi * r) + (a / 2) * math.cos(4 * math.pi * r)


def power_sum(n):
    return sum(window(i / (n - 1)) ** 2 for i in range(n))


def scales():
    s2048, s1024 = power_sum(2048), power_sum(1024)
    return 1.0 / 2048, math.sqrt(s2048 / s1024) / 2048, s2048, s1024


def retention(tau, fps):
    if tau <= 0:
        return 0.0
    return tau ** (60.0 / max(fps, 1.0))


def to_db(s):
    if not s > 1e-10:
        return -200.0
    return 20.0 * math.log10(s)


def tilt_db(centre_hz):
    """Band weighting: +3 dB per octave around 1 kHz."""
    return TILT_DB_PER_OCTAVE * math.log2(centre_hz / TILT_REF_HZ)


def agc_coeff(seconds, ticks_per_s):
    """Share of the way to the target covered in one tick by a first-order follower with this time constant."""
    return 1.0 - math.exp(-1.0 / (ticks_per_s * seconds))


class Agc:
    """Automatic gain: a reference level in dB that follows the loudest band (section 2.10)."""

    def __init__(self, ticks_per_s=100.0):
        self.ref = AGC_FLOOR_DB
        self.attack = agc_coeff(AGC_ATTACK_S, ticks_per_s)
        self.release = agc_coeff(AGC_RELEASE_S, ticks_per_s)

    def step(self, band_db, quiet=False):
        """Sound is playing: up fast, down slowly."""
        peak = max(band_db)
        target = max(peak, AGC_FLOOR_DB)
        if target > self.ref:
            self.ref += (target - self.ref) * self.attack
            if self.ref < peak - AGC_HEADROOM_DB:       # never let a band leave the top of the panel
                self.ref = peak - AGC_HEADROOM_DB
        else:
            if quiet:
                target = max(target, peak + AGC_RANGE_DB - AGC_HEADROOM_DB)
            if target < self.ref:
                self.ref += (target - self.ref) * self.release
        return self.ref

    def quiet(self, band_db):
        """Input too quiet to keep the display awake: the reference goes down only while that leaves the loudest
        band below the bottom row."""
        return self.step(band_db, quiet=True)

    def snap(self, band_db):
        """Nothing is shown: the reference is the loudest band (or the floor) at once."""
        self.ref = max(max(band_db), AGC_FLOOR_DB)
        return self.ref

    def level(self, db):
        return agc_level(self.ref, db)


def agc_level(ref, db):
    bottom = ref + AGC_HEADROOM_DB - AGC_RANGE_DB
    return min(1.0, max(0.0, (db - bottom) / AGC_RANGE_DB))


def steady_ref(band_db):
    """Where the reference settles when the band values no longer change."""
    return max(max(band_db), AGC_FLOOR_DB)


def bar_rows(v):
    """Solid bar: v * 16 rounded to the nearest row (half up), 0..16."""
    if not v > 0:
        return 0
    return int(math.floor(16 * min(v, 1.0) + 0.5))


def band_edges(fs):
    f_max = min(MAX_HZ, fs / 2.0)
    f_min = MIN_HZ
    if f_min >= f_max:
        f_min = f_max / 2.0
    a = math.log(1.0 + f_min / WARP_HZ)
    b = math.log(1.0 + f_max / WARP_HZ)
    e = [WARP_HZ * (math.exp(a + (b - a) * i / BANDS) - 1.0) for i in range(BANDS + 1)]
    e[0], e[BANDS] = f_min, f_max
    return e


def edge_to_bin(f, fs, n):
    max_bin = n // 2 - 1
    b = max(0, math.floor(f * n / fs))
    if b > max_bin:
        return max_bin, 0.0
    lower = 1.0 if b == 0 else b * fs / n
    upper = (b + 1) * fs / n
    frac = math.log2(f / lower) / math.log2(upper / lower) if f > 0 else 0.0   # f <= 0: no position, clamped to 0
    return b, min(1.0, max(0.0, frac))


def plan(fs, split=SPLIT_HZ):
    """Bands of both FFTs for a sample rate: list of dicts, plus the per-FFT bin ranges."""
    e = band_edges(fs)
    bands = []
    for i in range(BANDS):
        centre = math.sqrt(e[i] * e[i + 1])
        n = LOW_N if centre < split else HIGH_N
        lo, lof = edge_to_bin(e[i], fs, n)
        hi, hif = edge_to_bin(e[i + 1], fs, n)
        if hi < lo:
            hi, hif = lo, lof
        bands.append(dict(i=i, lo_hz=e[i], hi_hz=e[i + 1], centre=centre, n=n, lo=lo, lof=lof, hi=hi, hif=hif,
                          tilt=tilt_db(centre)))
    rng = {}
    for n in (LOW_N, HIGH_N):
        mine = [b for b in bands if b["n"] == n]
        if mine:
            rng[n] = (len(mine), min(b["lo"] for b in mine), max(min(b["hi"] + 1, n // 2 - 1) for b in mine))
        else:
            rng[n] = (0, 1, 0)
    return bands, rng, e


def band_value(d, b):
    n = len(d)

    def interp(bin_, frac):
        return d[bin_] + (d[bin_ + 1] - d[bin_]) * frac if bin_ < n - 1 else d[bin_]

    v = max(interp(b["lo"], b["lof"]), interp(b["hi"], b["hif"]))
    for j in range(b["lo"] + 1, b["hi"]):
        v = max(v, d[j])
    return v


def band_value_map(d, lo, lof, hi, hif):
    return band_value(d, dict(lo=lo, lof=lof, hi=hi, hif=hif))


def band_weighted(d, b):
    """Band value plus the band's tilt; silence stays silence."""
    v = band_value(d, b)
    return SILENCE_DB if v <= SILENCE_DB else v + b["tilt"]


def fft(x):
    """Radix-2 FFT (iterative), len(x) a power of two."""
    n = len(x)
    a = [complex(v) for v in x]
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        w_len = cmath.exp(-2j * math.pi / length)
        for start in range(0, n, length):
            w = 1.0 + 0j
            half = length // 2
            for k in range(half):
                u = a[start + k]
                v = a[start + k + half] * w
                a[start + k] = u + v
                a[start + k + half] = u - v
                w *= w_len
        length <<= 1
    return a


def signal_magnitudes(n, fs, tones):
    """|X_k| of sum A sin(2 pi f t / fs) * w(t / (N - 1)), k = 0 .. N/2 - 1 (bin 0: |re|), times the FFT's scale."""
    low_scale, high_scale, _, _ = scales()
    scale = low_scale if n == LOW_N else high_scale
    x = [sum(amp * math.sin(2 * math.pi * f * t / fs) for f, amp in tones) * window(t / (n - 1)) for t in range(n)]
    spec = fft(x)
    out = []
    for k in range(n // 2):
        m = abs(spec[0].real) if k == 0 else abs(spec[k])
        out.append(m * scale)
    return out


def steady_bands(fs, tones):
    """Settled state of a steady signal: weighted dB per band, the AGC reference, level and rows per band.
    (Smoothing has no effect once the input is constant, and the reference ends at max(loudest band, floor).)"""
    bands, _, _ = plan(fs)
    mags = {n: signal_magnitudes(n, fs, tones) for n in (LOW_N, HIGH_N)}
    dbs = {n: [to_db(m) for m in mags[n]] for n in mags}
    vals = [band_weighted(dbs[b["n"]], b) for b in bands]
    ref = steady_ref(vals)
    levels = [agc_level(ref, v) for v in vals]
    return vals, ref, levels, [bar_rows(lv) for lv in levels]


def band_response(fs, f, amp, band, frames_on, frames_off):
    """Weighted dB of one band after n frames of the tone (from silence) and after n frames of silence."""
    bands, rng, _ = plan(fs)
    b = bands[band]
    n = b["n"]
    r = retention(TAU, LOW_FPS if n == LOW_N else HIGH_FPS)
    steady = signal_magnitudes(n, fs, [(f, amp)])
    s = [0.0] * (n // 2)
    on, off = {}, {}
    for t in range(1, max(frames_on) + 1):
        s = [r * s[k] + (1 - r) * steady[k] for k in range(n // 2)]
        if t in frames_on:
            on[t] = band_weighted([to_db(v) for v in s], b)
    for t in range(1, max(frames_off) + 1):
        s = [r * s[k] for k in range(n // 2)]
        if t in frames_off:
            off[t] = band_weighted([to_db(v) for v in s], b)
    return on, off


# AGC alone: the loudest band value per tick, in segments (ticks, dB); a probe band sits AGC_PROBE_BELOW_DB lower.
AGC_SEQUENCE = [(100, SILENCE_DB), (300, -30.0), (600, -50.0), (300, -20.0), (500, SILENCE_DB), (300, -70.0)]
AGC_PROBE_BELOW_DB = 20.0


def agc_sequence():
    """Reference after every tick, and the levels of the loudest band and of the probe band."""
    agc = Agc(TICK_FPS)
    refs, top, probe = [], [], []
    for ticks, peak in AGC_SEQUENCE:
        for _ in range(ticks):
            low = SILENCE_DB if peak <= SILENCE_DB else peak - AGC_PROBE_BELOW_DB
            agc.step([peak, low])
            refs.append(agc.ref)
            top.append(agc.level(peak))
            probe.append(agc.level(low))
    return refs, top, probe


# AGC with the three moves of section 2.10 (0 = step, 1 = quiet, 2 = snap), as the firmware picks them: asleep,
# music starts while still dark, awake, noise after the music, faded out, a loud click in the noise, a visible
# tail that is too quiet to count, digital silence, music.
AGC_MODE_SEQUENCE = [(100, SILENCE_DB, 2), (50, -30.0, 2), (300, -30.0, 0), (400, -75.0, 1), (100, -75.0, 2),
                     (100, -20.0, 1), (100, -40.0, 1), (200, SILENCE_DB, 1), (200, -45.0, 0)]


def agc_mode_sequence():
    """Reference after every tick, and the levels of the loudest band and of the probe band."""
    agc = Agc(TICK_FPS)
    refs, top, probe = [], [], []
    for ticks, peak, mode in AGC_MODE_SEQUENCE:
        for _ in range(ticks):
            low = SILENCE_DB if peak <= SILENCE_DB else peak - AGC_PROBE_BELOW_DB
            (agc.step, agc.quiet, agc.snap)[mode]([peak, low])
            refs.append(agc.ref)
            top.append(agc.level(peak))
            probe.append(agc.level(low))
    return refs, top, probe


# Whole chain over time: a 1 kHz tone whose amplitude changes in steps (ticks, amplitude), fs 44100.
TIMELINE_HZ = 1000.0
TIMELINE = [(300, 0.125), (300, 1.0), (800, 0.0125), (400, 0.0)]
TIMELINE_TICKS = [0, 1, 2, 5, 10, 20, 50, 100, 299, 300, 301, 302, 305, 310, 320, 350, 400, 599, 600, 601, 605, 610,
                  620, 650, 700, 800, 1000, 1200, 1399, 1400, 1405, 1410, 1420, 1430, 1450, 1500, 1799]


def low_frame_due(tick):
    """Test schedule: one high frame on every 10 ms tick, a low frame on 3 ticks of 10 (30 per second)."""
    return ((tick + 1) * 3) // 10 > (tick * 3) // 10


def chain_timeline():
    """Per listed tick: (reference dB, level per band). The spectrum of the tone at amplitude 1 is scaled per
    segment; each FFT's bins are smoothed per frame, band values held until the FFT's next frame."""
    fs = 44100.0
    bands, rng, _ = plan(fs)
    unit = {n: signal_magnitudes(n, fs, [(TIMELINE_HZ, 1.0)]) for n in (LOW_N, HIGH_N)}
    r = {LOW_N: retention(TAU, LOW_FPS), HIGH_N: retention(TAU, HIGH_FPS)}
    smooth = {n: [0.0] * (n // 2) for n in (LOW_N, HIGH_N)}
    held = [SILENCE_DB] * BANDS
    agc = Agc(TICK_FPS)
    out = {}
    tick = 0
    for ticks, amp in TIMELINE:
        for _ in range(ticks):
            for n in ((HIGH_N, LOW_N) if low_frame_due(tick) else (HIGH_N,)):
                _, first, last = rng[n]
                s = smooth[n]
                for k in range(first, last + 1):
                    s[k] = r[n] * s[k] + (1 - r[n]) * amp * unit[n][k]
                d = [to_db(v) for v in s]
                for b in bands:
                    if b["n"] == n:
                        held[b["i"]] = band_weighted(d, b)
            agc.step(held)
            if tick in TIMELINE_TICKS:
                out[tick] = (agc.ref, [agc.level(v) for v in held])
            tick += 1
    return out


def peak_trajectory(ticks=120):
    """Bar value 0.8 on ticks 0..4, then 0. Closed form of the hold-then-fall law."""
    d = PEAK_FALL / (TICK_FPS * TICK_FPS)
    l0, last = 0.8, 4
    out = []
    for t in range(ticks):
        if t <= last + PEAK_HOLD_TICKS:
            out.append(l0)
        else:
            k = t - (last + PEAK_HOLD_TICKS)
            out.append(max(0.0, l0 - d * k * (k + 1) / 2.0))
    return out


def peak_zero_tick_from_full():
    d = PEAK_FALL / (TICK_FPS * TICK_FPS)
    last = 4
    k = 1
    while 1.0 - d * k * (k + 1) / 2.0 > 0:
        k += 1
    return last + PEAK_HOLD_TICKS + k


def band_quality(fs):
    """Layout checks of section 2.6 for one sample rate: (narrowest band in bins of its own FFT, columns whose
    upper edge is at most 510 Hz)."""
    bands, _, _ = plan(fs)
    narrow = min((b["hi_hz"] - b["lo_hz"]) * b["n"] / fs for b in bands)
    return narrow, sum(1 for b in bands if b["hi_hz"] <= 510.0)


def column_of(fs, hz):
    """Position of a frequency on the panel in columns (0 = left edge, 24 = right edge), from the axis formula."""
    f_max = min(MAX_HZ, fs / 2.0)
    f_min = MIN_HZ if MIN_HZ < f_max else f_max / 2.0
    a = math.log(1.0 + f_min / WARP_HZ)
    b = math.log(1.0 + f_max / WARP_HZ)
    return BANDS * (math.log(1.0 + hz / WARP_HZ) - a) / (b - a)


def bands_of(fs):
    return plan(float(fs))[0]


def band_of(fs, hz):
    for b in bands_of(fs):
        if b["lo_hz"] <= hz < b["hi_hz"]:
            return b["i"]
    return -1


# The steady signals of the acceptance table: (fs, [(Hz, amplitude), ...])
TONE_SIGNALS = [
    (44100, [(1000.0, 0.125)]),
    (44100, [(100.0, 0.125)]),
    (44100, [(10000.0, 0.125)]),
    (44100, [(250.0, 0.5)]),
    (44100, [(1000.0, 1.0)]),
    (44100, [(1000.0, 0.001)]),
    (44100, [(60.0, 0.5), (8000.0, 0.005)]),
    (48000, [(1000.0, 0.125)]),
    (32000, [(1000.0, 0.125)]),
    (22050, [(1000.0, 0.125)]),
    (8000, [(1000.0, 0.125)]),
    (44100, []),   # silence
]


# ---------------------------------------------------------------------------------------------- spec literals
# Numbers printed in SPEC-LED.md; the reference must reproduce them (tolerances as given there).
SPEC_PLAN_44100 = [  # loBin, loFrac, hiBin, hiFrac, tilt dB
    (1, 0.4784, 3, 0.0489, -13.49),
    (3, 0.0489, 5, 0.0397, -10.71),
    (5, 0.0397, 7, 0.4633, -8.77),
    (7, 0.4633, 10, 0.3718, -7.21),
    (10, 0.3718, 13, 0.8871, -5.86),
    (13, 0.8871, 18, 0.1453, -4.65),
    (18, 0.1453, 23, 0.2947, -3.53),
    (11, 0.6550, 14, 0.7633, -2.47),
    (14, 0.7633, 18, 0.5258, -1.47),
    (18, 0.5258, 23, 0.0684, -0.50),
    (23, 0.0684, 28, 0.5694, 0.43),
    (28, 0.5694, 35, 0.2141, 1.35),
    (35, 0.2141, 43, 0.2491, 2.25),
    (43, 0.2491, 52, 0.9611, 3.13),
    (52, 0.9611, 64, 0.7055, 4.00),
    (64, 0.7055, 78, 0.9009, 4.86),
    (78, 0.9009, 96, 0.0628, 5.72),
    (96, 0.0628, 116, 0.8107, 6.57),
    (116, 0.8107, 141, 0.8922, 7.41),
    (141, 0.8922, 172, 0.2141, 8.25),
    (172, 0.2141, 208, 0.8702, 9.09),
    (208, 0.8702, 253, 0.1843, 9.92),
    (253, 0.1843, 306, 0.7561, 10.76),
    (306, 0.7561, 371, 0.5196, 11.59),
]
SPEC_PLAN_PARTIAL = {  # fs -> {band: (loBin, loFrac, hiBin, hiFrac)}
    48000: {0: (1, 0.3561, 2, 0.8257), 6: (16, 0.6746, 21, 0.4035), 7: (10, 0.7089, 13, 0.5672), 23: (281, 0.8322, 341, 0.3337)},
    8000: {0: (7, 0.6943, 13, 0.8761), 9: (108, 0.2548, 128, 0.7565), 10: (64, 0.3797, 76, 0.0878), 23: (445, 0.9939, 511, 0.0000)},
    32000: {0: (1, 0.9411, 4, 0.2111), 6: (25, 0.0020, 32, 0.0983), 7: (16, 0.0499, 20, 0.3425), 23: (422, 0.7481, 511, 0.0000)},
    22050: {0: (2, 0.8178, 5, 0.8085), 7: (40, 0.5115, 50, 0.7039), 8: (25, 0.3554, 31, 0.4223), 23: (429, 0.0351, 511, 0.0000)},
}
SPEC_SUMMARY = {  # fs -> (low n, first, last, high n, first, last)
    44100: (7, 1, 24, 17, 11, 372), 48000: (7, 1, 22, 17, 10, 342), 8000: (10, 7, 129, 14, 64, 511),
    32000: (7, 1, 33, 17, 16, 511), 22050: (8, 2, 51, 16, 25, 511)}
SPEC_EDGES_44100 = [30.0000, 65.5147, 108.4487, 160.3522, 223.0987, 298.9536, 390.6554, 501.5146, 635.5333, 797.5499,
    993.4134, 1230.1947, 1516.4420, 1862.4891, 2280.8290, 2786.5642, 3397.9525, 4137.0658, 5030.5873, 6110.7743,
    7416.6231, 8995.2767, 10903.7266, 13210.8707, 16000.0000]
SPEC_QUALITY = {  # fs -> (narrowest band in bins of its FFT, columns with upper edge <= 510 Hz)
    44100: (1.65, 7), 48000: (1.52, 7), 8000: (6.19, 10), 32000: (2.27, 7), 22050: (3.01, 7)}
SPEC_TONES = [  # (fs, signal, AGC reference dB, {band: (dB, level, rows, band holds a tone)})
    (44100, [(1000.0, 0.125)], -34.58, {9: (-35.52, 0.8555, 14, 0), 10: (-34.58, 0.8750, 14, 1)}),
    (44100, [(100.0, 0.125)], -41.26,
        {0: (-57.54, 0.5359, 9, 0), 1: (-43.20, 0.8346, 13, 1), 2: (-41.26, 0.8750, 14, 0)}),
    (44100, [(10000.0, 0.125)], -24.88, {21: (-24.88, 0.8750, 14, 1)}),
    (44100, [(250.0, 0.5)], -26.11,
        {3: (-35.18, 0.6859, 11, 0), 4: (-26.11, 0.8750, 14, 1), 5: (-53.91, 0.2958, 5, 0)}),
    (44100, [(1000.0, 1.0)], -16.52, {9: (-17.46, 0.8555, 14, 0), 10: (-16.52, 0.8750, 14, 1)}),
    (44100, [(1000.0, 0.001)], -60.00, {9: (-77.46, 0.5113, 8, 0), 10: (-76.52, 0.5308, 8, 1)}),
    (44100, [(60.0, 0.5), (8000.0, 0.005)], -30.81,
        {0: (-33.59, 0.8171, 13, 1), 1: (-30.81, 0.8750, 14, 0), 2: (-56.22, 0.3455, 6, 0), 20: (-53.75, 0.3969, 6, 1)}),
    (48000, [(1000.0, 0.125)], -34.98, {9: (-35.91, 0.8555, 14, 0), 10: (-34.98, 0.8750, 14, 1)}),
    (32000, [(1000.0, 0.125)], -34.20, {9: (-36.07, 0.8360, 13, 0), 10: (-34.20, 0.8750, 14, 1)}),
    (22050, [(1000.0, 0.125)], -35.84, {10: (-35.84, 0.8750, 14, 1), 11: (-37.99, 0.8303, 13, 0)}),
    (8000, [(1000.0, 0.125)], -34.51, {14: (-34.51, 0.8750, 14, 1)}),
    (44100, [], -60.00, {}),
]
SPEC_AGC_SEQ = {  # tick: (reference dB, level of the loudest band, level of the probe band)
    0: (-60.000, 0.0000, 0.0000), 99: (-60.000, 0.0000, 0.0000), 100: (-36.000, 1.0000, 0.5833),
    101: (-35.429, 0.9881, 0.5714), 110: (-32.207, 0.9210, 0.5043), 120: (-30.812, 0.8919, 0.4753),
    150: (-30.040, 0.8758, 0.4592), 399: (-30.000, 0.8750, 0.4583), 400: (-30.067, 0.4597, 0.0431),
    500: (-35.717, 0.5774, 0.1608), 700: (-42.667, 0.7222, 0.3056), 999: (-47.293, 0.8186, 0.4019),
    1000: (-26.000, 1.0000, 0.5833), 1001: (-25.429, 0.9881, 0.5714), 1010: (-22.207, 0.9210, 0.5043),
    1050: (-20.040, 0.8758, 0.4592), 1299: (-20.000, 0.8750, 0.4583), 1300: (-20.133, 0.0000, 0.0000),
    1400: (-31.434, 0.0000, 0.0000), 1600: (-45.334, 0.0000, 0.0000), 1799: (-52.445, 0.0000, 0.0000),
    1800: (-52.470, 0.5098, 0.0931), 1900: (-54.605, 0.5543, 0.1376), 2099: (-57.221, 0.6088, 0.1921)}
SPEC_COL500 = {44100: 6.99, 48000: 6.99, 32000: 6.99, 22050: 7.60, 8000: 9.97}   # column position of 500 Hz
SPEC_AGC_MODE_SEQ = {  # tick: (reference dB, level of the loudest band, level of the probe band)
    0: (-60.000, 0.0000, 0.0000), 99: (-60.000, 0.0000, 0.0000), 100: (-30.000, 0.8750, 0.4583),
    149: (-30.000, 0.8750, 0.4583), 150: (-30.000, 0.8750, 0.4583), 449: (-30.000, 0.8750, 0.4583),
    450: (-30.010, 0.0000, 0.0000), 600: (-31.186, 0.0000, 0.0000), 849: (-32.209, 0.0000, 0.0000),
    850: (-60.000, 0.5625, 0.1458), 949: (-60.000, 0.5625, 0.1458), 950: (-26.000, 1.0000, 0.5833),
    951: (-25.429, 0.9881, 0.5714), 960: (-22.207, 0.9210, 0.5043), 1000: (-20.040, 0.8758, 0.4592),
    1049: (-20.000, 0.8750, 0.4583), 1050: (-20.000, 0.4583, 0.0417), 1149: (-20.000, 0.4583, 0.0417),
    1150: (-20.133, 0.0000, 0.0000), 1250: (-31.434, 0.0000, 0.0000), 1349: (-39.463, 0.0000, 0.0000),
    1350: (-39.482, 0.7600, 0.3434), 1450: (-41.046, 0.7926, 0.3760), 1549: (-42.157, 0.8158, 0.3991)}
SPEC_TR_HIGH_BAND = 10
SPEC_TR_HIGH_ON = {1: -48.89, 2: -43.74, 3: -41.07, 5: -38.23, 10: -35.67, 20: -34.70, 40: -34.58}
SPEC_TR_HIGH_OFF = {1: -36.44, 5: -43.88, 10: -53.17, 20: -71.76, 27: -84.77}
SPEC_TR_LOW_BAND = 1
SPEC_TR_LOW_ON = {1: -49.05, 2: -45.58, 3: -44.28, 5: -43.45, 10: -43.20}
SPEC_TIMELINE_BAND = 10
SPEC_TIMELINE = {  # tick: (reference dB, level, rows) of the tone's band
    0: (-54.89, 1.0000, 16), 1: (-49.74, 1.0000, 16), 2: (-47.07, 1.0000, 16), 5: (-43.40, 1.0000, 16),
    10: (-40.47, 0.9797, 16), 20: (-36.91, 0.9216, 15), 50: (-34.70, 0.8775, 14), 100: (-34.58, 0.8750, 14),
    299: (-34.58, 0.8750, 14), 300: (-33.16, 1.0000, 16), 301: (-29.86, 1.0000, 16), 302: (-27.88, 1.0000, 16),
    305: (-24.93, 1.0000, 16), 310: (-22.09, 0.9754, 16), 320: (-18.71, 0.9189, 15), 350: (-16.63, 0.8773, 14),
    400: (-16.52, 0.8750, 14), 599: (-16.52, 0.8750, 14), 600: (-16.53, 0.8369, 13), 601: (-16.54, 0.7991, 13),
    605: (-16.65, 0.6511, 10), 610: (-16.91, 0.4775, 8), 620: (-17.79, 0.2231, 4), 650: (-21.21, 0.1800, 3),
    700: (-26.33, 0.2865, 5), 800: (-34.34, 0.4533, 7), 1000: (-44.19, 0.6585, 11), 1200: (-49.25, 0.7638, 12),
    1399: (-51.83, 0.8177, 13), 1400: (-51.85, 0.7793, 12), 1405: (-51.98, 0.5884, 9), 1410: (-52.11, 0.3975, 6),
    1420: (-52.37, 0.0157, 0), 1430: (-52.62, 0.0000, 0), 1450: (-53.09, 0.0000, 0), 1500: (-54.15, 0.0000, 0),
    1799: (-57.84, 0.0000, 0)}
SPEC_EDGE_EXAMPLES_1024 = [(30, 0, 0.9039), (44.7, 1, 0.0537), (100, 2, 0.3682), (250, 5, 0.8188), (440, 10, 0.2250),
                           (1000, 23, 0.2236), (3150, 73, 0.1437), (8000, 185, 0.7601), (16000, 371, 0.5196),
                           (22050, 511, 0.0), (30000, 511, 0.0)]
SPEC_EDGE_EXAMPLES_2048 = [(30, 1, 0.4784), (44.7, 2, 0.0918), (100, 4, 0.6690), (250, 11, 0.6203), (440, 20, 0.4396),
                           (1000, 46, 0.4426), (3150, 146, 0.2864), (8000, 371, 0.5196), (16000, 743, 0.0386)]
SPEC_WINDOW = [(0.0, 0.0), (0.0625, 0.014629), (0.125, 0.066447), (0.25, 0.34), (0.375, 0.773553), (0.5, 1.0),
               (0.75, 0.34), (1.0, 0.0)]
SPEC_PEAK = {0: 0.8, 4: 0.8, 5: 0.8, 20: 0.8, 54: 0.8, 55: 0.798623, 56: 0.795870, 57: 0.791739, 60: 0.771087,
             70: 0.612754, 80: 0.316739, 87: 0.027610, 88: 0.0, 100: 0.0, 119: 0.0}
SPEC_PEAK_ROWS = {0: 13, 4: 13, 5: 13, 20: 13, 54: 13, 55: 13, 56: 13, 57: 13, 60: 12, 70: 10, 80: 5, 87: 0, 88: 0,
                  100: 0, 119: 0}
SPEC_ROWS = [(-0.5, 0), (0, 0), (0.01, 0), (0.03, 0), (0.0313, 1), (0.06, 1), (0.12, 2), (0.124, 2), (0.125, 2),
             (0.2, 3), (0.25, 4), (0.34, 5), (0.5, 8), (0.7, 11), (0.749, 12), (0.75, 12), (0.836, 13), (0.875, 14),
             (0.968, 15), (0.969, 16), (0.99, 16), (1.0, 16), (1.3, 16)]
SPEC_AGC_LEVEL = [  # reference dB, band dB, level
    (-60.0, -60.0, 0.875), (-60.0, -54.0, 1.0), (-60.0, -40.0, 1.0), (-60.0, -78.0, 0.5), (-60.0, -102.0, 0.0),
    (-60.0, -150.0, 0.0), (-30.0, -30.0, 0.875), (-30.0, -33.0, 0.8125), (-30.0, -51.0, 0.4375),
    (-30.0, -72.0, 0.0), (-12.5, -36.5, 0.375), (-60.0, SILENCE_DB, 0.0)]
SPEC_TILT = [(1000.0, 0.0), (2000.0, 3.0), (500.0, -3.0), (125.0, -9.0), (16000.0, 12.0), (44.7, -13.451)]

BAND_DB_CASES = [  # name, base or ramp, overrides, (lo, lof, hi, hif), expected spec value
    ("ramp", None, [], (10, 0.25, 14, 0.5), -75.5),
    ("spike between", -100.0, [(12, -40.0)], (10, 0.5, 14, 0.5), -40.0),
    ("same bin", -100.0, [(20, -60.0), (21, -50.0)], (20, 0.2, 20, 0.8), -52.0),
    ("last bin", -100.0, [(510, -70.0), (511, -50.0)], (510, 0.5, 511, 0.9), -50.0),
    ("edge beats middle", -100.0, [(30, -50.0), (31, -80.0), (32, -90.0), (33, -60.0)], (30, 0.25, 32, 0.75), -57.5),
]


class SpecMismatch(Exception):
    pass


def expect(what, got, want, tol):
    if not abs(got - want) <= tol:
        raise SpecMismatch("%s: reference %r, specification %r (tol %r)" % (what, got, want, tol))


def verify_against_spec():
    """The reference must reproduce every literal number of the specification."""
    n_ok = 0
    low_scale, high_scale, s2048, s1024 = scales()
    expect("power sum 2048", s2048, 623.516, 0.05)
    expect("power sum 1024", s1024, 311.606, 0.05)
    expect("scale high", high_scale, 6.9070e-4, 2e-7)
    expect("scale low", low_scale, 4.8828125e-4, 1e-12)
    for r, w in SPEC_WINDOW:
        expect("window %g" % r, window(r), w, 1e-5)
    for tau, fps, want in [(0.7, 30, 0.49), (0.7, 100, 0.807344), (0.5, 60, 0.5), (0.8, 60, 0.8), (0.5, 30, 0.25),
                           (0.5, 100, 0.659754), (0.9, 100, 0.938740), (0.0, 60, 0.0)]:
        expect("retention %g/%g" % (tau, fps), retention(tau, fps), want, 1e-6)
    for m, want in [(1.0, 0.0), (0.1, -20.0), (0.5, -6.0206), (0.0, -200.0), (1e-10, -200.0), (2e-10, -193.98)]:
        expect("db %g" % m, to_db(m), want, 1e-3)
    # smoothing step response
    for tau_r, n, want in [(0.49, 1, -5.85), (0.49, 2, -2.39), (0.49, 3, -1.09), (0.49, 5, -0.25),
                           (0.807344, 1, -14.30), (0.807344, 2, -9.16), (0.807344, 3, -6.49), (0.807344, 5, -3.65),
                           (0.807344, 10, -1.09), (0.807344, 20, -0.12), (0.807344, 40, -0.002)]:
        expect("step response r=%g n=%d" % (tau_r, n), 20 * math.log10(1 - tau_r ** n), want, 0.006)
    expect("decay high", 20 * math.log10(0.807344), -1.8588, 1e-3)
    expect("decay low", 20 * math.log10(0.49), -6.196, 1e-3)
    # band layout
    e = band_edges(44100.0)
    for i, (got, want) in enumerate(zip(e, SPEC_EDGES_44100)):
        expect("edge %d" % i, got / want - 1.0, 0.0, 2e-4)
    for fs, summ in SPEC_SUMMARY.items():
        _, rng, _ = plan(float(fs))
        got = rng[LOW_N] + rng[HIGH_N]
        if tuple(got) != summ:
            raise SpecMismatch("plan summary fs %d: %r, specification %r" % (fs, got, summ))
    bands, _, _ = plan(44100.0)
    for b, (lo, lof, hi, hif, tilt) in zip(bands, SPEC_PLAN_44100):
        expect("band %d loBin" % b["i"], b["lo"], lo, 0)
        expect("band %d hiBin" % b["i"], b["hi"], hi, 0)
        expect("band %d loFrac" % b["i"], b["lof"], lof, 2e-4)
        expect("band %d hiFrac" % b["i"], b["hif"], hif, 2e-4)
        expect("band %d tilt" % b["i"], b["tilt"], tilt, 6e-3)
    for fs, part in SPEC_PLAN_PARTIAL.items():
        bands, _, _ = plan(float(fs))
        for i, (lo, lof, hi, hif) in part.items():
            b = bands[i]
            expect("fs %d band %d loBin" % (fs, i), b["lo"], lo, 0)
            expect("fs %d band %d hiBin" % (fs, i), b["hi"], hi, 0)
            expect("fs %d band %d loFrac" % (fs, i), b["lof"], lof, 2e-4)
            expect("fs %d band %d hiFrac" % (fs, i), b["hif"], hif, 2e-4)
    for fs, (narrow, below) in SPEC_QUALITY.items():
        got_narrow, got_below = band_quality(float(fs))
        expect("fs %d narrowest band (bins)" % fs, got_narrow, narrow, 0.006)
        expect("fs %d columns below 510 Hz" % fs, got_below, below, 0)
        expect("fs %d column of 500 Hz" % fs, column_of(float(fs), 500.0), SPEC_COL500[fs], 0.006)
        if not got_narrow >= 1.5:
            raise SpecMismatch("fs %d: a band is narrower than 1.5 bins of its FFT (%r)" % (fs, got_narrow))
    for n, examples in ((1024, SPEC_EDGE_EXAMPLES_1024), (2048, SPEC_EDGE_EXAMPLES_2048)):
        for f, bin_, frac in examples:
            gb, gf = edge_to_bin(float(f), 44100.0, n)
            expect("edge %g N=%d bin" % (f, n), gb, bin_, 0)
            expect("edge %g N=%d frac" % (f, n), gf, frac, 2e-4)
    # band value cases
    for name, base, over, m, want in BAND_DB_CASES:
        d = band_db_array(base, over)
        expect("band db " + name, band_value_map(d, *m), want, 1e-4)
    # tilt, AGC
    for hz, want in SPEC_TILT:
        expect("tilt %g" % hz, tilt_db(hz), want, 1e-3)
    for ref, db, want in SPEC_AGC_LEVEL:
        expect("agc level %g/%g" % (ref, db), agc_level(ref, db), want, 1e-6)
    expect("agc attack per tick", agc_coeff(AGC_ATTACK_S, TICK_FPS), 0.0951626, 1e-6)
    expect("agc release per tick", agc_coeff(AGC_RELEASE_S, TICK_FPS), 0.00332779, 1e-7)
    refs, top, probe = agc_sequence()
    for t, (ref, lv_top, lv_probe) in SPEC_AGC_SEQ.items():
        expect("agc sequence tick %d ref" % t, refs[t], ref, 0.006)
        expect("agc sequence tick %d level" % t, top[t], lv_top, 6e-4)
        expect("agc sequence tick %d probe" % t, probe[t], lv_probe, 6e-4)
    refs, top, probe = agc_mode_sequence()
    for t, (ref, lv_top, lv_probe) in SPEC_AGC_MODE_SEQ.items():
        expect("agc mode sequence tick %d ref" % t, refs[t], ref, 0.006)
        expect("agc mode sequence tick %d level" % t, top[t], lv_top, 6e-4)
        expect("agc mode sequence tick %d probe" % t, probe[t], lv_probe, 6e-4)
    # peaks
    traj = peak_trajectory()
    for t, want in SPEC_PEAK.items():
        expect("peak tick %d" % t, traj[t], want, 1e-4)
        expect("peak rows tick %d" % t, bar_rows(traj[t]), SPEC_PEAK_ROWS[t], 0)
    expect("peak zero tick from 1.0", peak_zero_tick_from_full(), 92, 0)
    for v, want in SPEC_ROWS:
        expect("rows %g" % v, bar_rows(v), want, 0)
    # whole chain, settled
    if [(fs, sig) for fs, sig, _, _ in SPEC_TONES] != TONE_SIGNALS:
        raise SpecMismatch("SPEC_TONES does not list the signals of TONE_SIGNALS")
    for fs, tones, ref_want, listed in SPEC_TONES:
        vals, ref, levels, rows = steady_bands(float(fs), tones)
        name = "signal %r/%d" % (tones, fs)
        expect(name + " reference", ref, ref_want, 0.2)
        for i in range(BANDS):
            if i in listed:
                sdb, slv, srows, bold = listed[i]
                expect("%s band %d dB" % (name, i), vals[i], sdb, 0.2 if bold else 1.5)
                expect("%s band %d rows" % (name, i), rows[i], srows, 0 if bold else 1)
                expect("%s band %d level" % (name, i), levels[i], slv, 0.005 if bold else 0.04)
            else:
                # unlisted bands: leakage of at most two lit rows
                expect("%s band %d unlisted rows" % (name, i), rows[i], 0, 2)
    # time response of a band (dB) and of the whole chain with AGC
    on, off = band_response(44100.0, 1000.0, 0.125, SPEC_TR_HIGH_BAND, set(SPEC_TR_HIGH_ON), set(SPEC_TR_HIGH_OFF))
    for t, want in SPEC_TR_HIGH_ON.items():
        expect("1k on n=%d" % t, on[t], want, 0.06)
    for t, want in SPEC_TR_HIGH_OFF.items():
        expect("1k off n=%d" % t, off[t], want, 0.06)
    on, _ = band_response(44100.0, 100.0, 0.125, SPEC_TR_LOW_BAND, set(SPEC_TR_LOW_ON), {1})
    for t, want in SPEC_TR_LOW_ON.items():
        expect("100 on n=%d" % t, on[t], want, 0.06)
    tl = chain_timeline()
    for t, (ref, lv, rows) in SPEC_TIMELINE.items():
        expect("timeline tick %d ref" % t, tl[t][0], ref, 0.06)
        expect("timeline tick %d level" % t, tl[t][1][SPEC_TIMELINE_BAND], lv, 0.002)
        expect("timeline tick %d rows" % t, bar_rows(tl[t][1][SPEC_TIMELINE_BAND]), rows, 0)
    return n_ok


def band_db_array(base, over):
    if base is None:
        return [-90.0 + k for k in range(512)]
    d = [base] * 512
    for idx, val in over:
        d[idx] = val
    return d


# ---------------------------------------------------------------------------------------------- header writer
def fmt(v):
    if isinstance(v, int):
        return str(v)
    s = "%.10g" % v
    if "e" not in s and "." not in s:
        s += ".0"
    return s


def nest(v, ind=""):
    if isinstance(v, (list, tuple)):
        if v and isinstance(v[0], (list, tuple)):
            inner = ind + "    "
            return "{\n" + ",\n".join(inner + nest(x, inner) for x in v) + "\n" + ind + "}"
        return "{" + ", ".join(fmt(x) for x in v) + "}"
    return fmt(v)


def dims(v):
    out = []
    while isinstance(v, (list, tuple)):
        out.append(len(v))
        v = v[0]
    return out


class Out:
    def __init__(self):
        self.lines = []

    def define(self, name, value):
        self.lines.append("#define %s %s" % (name, value))

    def comment(self, text):
        self.lines.append("")
        self.lines.append("/* %s */" % text)

    def array(self, ctype, name, data):
        d = "".join("[%d]" % n for n in dims(data))
        self.lines.append("static const %s %s%s = %s;" % (ctype, name, d, nest(data)))

    def text(self):
        return "\n".join(self.lines) + "\n"


def generate():
    verify_against_spec()
    o = Out()
    o.lines.append("/*")
    o.lines.append(" * spectrum_expected.h - expected values of the spectrum host test.")
    o.lines.append(" *")
    o.lines.append(" * GENERATED by tools/tests/spectrum_reference.py from the formulas of the specification; do not edit.")
    o.lines.append(" * Written from SPEC-LED.md and public standards (W3C Web Audio API AnalyserNode maths); no third-party code.")
    o.lines.append(" * Regenerate: python3 -I -B tools/tests/spectrum_reference.py")
    o.lines.append(" */")
    o.lines.append("#ifndef SPECTRUM_EXPECTED_H")
    o.lines.append("#define SPECTRUM_EXPECTED_H")

    low_scale, high_scale, s2048, s1024 = scales()
    o.comment("window, power sums, scale")
    o.define("EXP_NWIN", len(SPEC_WINDOW))
    o.array("double", "EXP_WIN_R", [r for r, _ in SPEC_WINDOW])
    o.array("double", "EXP_WIN_W", [window(r) for r, _ in SPEC_WINDOW])
    o.array("double", "EXP_POWER_SUM", [s2048, s1024])
    o.array("double", "EXP_SCALE", [low_scale, high_scale])

    o.comment("retention: tau, fps, value")
    ret_cases = [(0.7, 30.0), (0.7, 100.0), (0.5, 60.0), (0.8, 60.0), (0.5, 30.0), (0.5, 100.0), (0.9, 100.0),
                 (0.0, 60.0), (-1.0, 30.0), (0.7, 0.5)]
    o.define("EXP_NRET", len(ret_cases))
    o.array("double", "EXP_RET", [[t, f, retention(t, f)] for t, f in ret_cases])

    o.comment("smoothing step: retention, previous, input, result")
    step_cases = [(0.5, 0.0, 1.0), (0.5, 0.5, 1.0), (0.5, 0.75, 0.0), (0.0, 0.375, 0.0)]
    o.define("EXP_NSTEP", len(step_cases))
    o.array("double", "EXP_STEP", [[r, p, i, r * p + (1 - r) * i] for r, p, i in step_cases])

    o.comment("magnitude -> dB")
    db_in = [1.0, 0.1, 0.5, 0.0, 1e-10, 2e-10]
    o.define("EXP_NDB", len(db_in))
    o.array("double", "EXP_DB", [[m, to_db(float(m))] for m in db_in])

    o.comment("tilt: band centre Hz, dB")
    tilt_in = [hz for hz, _ in SPEC_TILT] + [31.5, 250.0, 4000.0, 8000.0]
    o.define("EXP_NTILT", len(tilt_in))
    o.array("double", "EXP_TILT", [[hz, tilt_db(hz)] for hz in tilt_in])

    o.comment("AGC: constants (range, headroom, floor dB; attack, release s), coefficients per tick at 100 ticks/s "
              "and 30 ticks/s (attack, release), level cases (reference dB, band dB, level)")
    o.array("double", "EXP_AGC_CONST", [AGC_RANGE_DB, AGC_HEADROOM_DB, AGC_FLOOR_DB, AGC_ATTACK_S, AGC_RELEASE_S])
    o.array("double", "EXP_AGC_COEFF", [[agc_coeff(AGC_ATTACK_S, tps), agc_coeff(AGC_RELEASE_S, tps)]
                                        for tps in (100.0, 30.0)])
    o.define("EXP_NAGC_LEVEL", len(SPEC_AGC_LEVEL))
    o.array("double", "EXP_AGC_LEVEL", [[ref, db, agc_level(ref, db)] for ref, db, _ in SPEC_AGC_LEVEL])

    o.comment("AGC alone at 100 ticks/s: segments (ticks, loudest band dB; a probe band sits EXP_AGC_PROBE_BELOW "
              "dB lower, silent when the loudest is); per tick: reference dB, level of the loudest and of the probe band")
    refs, top, probe = agc_sequence()
    o.define("EXP_NAGC_SEG", len(AGC_SEQUENCE))
    o.define("EXP_NAGC_TICKS", len(refs))
    o.define("EXP_AGC_PROBE_BELOW", fmt(AGC_PROBE_BELOW_DB))
    o.array("double", "EXP_AGC_SEG", [[float(t), p] for t, p in AGC_SEQUENCE])
    o.array("double", "EXP_AGC_REF", refs)
    o.array("double", "EXP_AGC_TOP", top)
    o.array("double", "EXP_AGC_PROBE", probe)

    o.comment("AGC with step / quiet / snap at 100 ticks/s: segments (ticks, loudest band dB, move: 0 = step, "
              "1 = quiet, 2 = snap), the probe band as above; per tick: reference dB, level of the loudest and of "
              "the probe band")
    refs, top, probe = agc_mode_sequence()
    o.define("EXP_NAGCM_SEG", len(AGC_MODE_SEQUENCE))
    o.define("EXP_NAGCM_TICKS", len(refs))
    o.array("double", "EXP_AGCM_SEG", [[float(t), p, float(m)] for t, p, m in AGC_MODE_SEQUENCE])
    o.array("double", "EXP_AGCM_REF", refs)
    o.array("double", "EXP_AGCM_TOP", top)
    o.array("double", "EXP_AGCM_PROBE", probe)

    o.comment("band plans: sample rates, per-FFT summary (low n, first, last, high n, first, last)")
    fss = [44100.0, 48000.0, 8000.0, 32000.0, 22050.0]
    plans = [plan(fs) for fs in fss]
    o.define("EXP_NPLANS", len(fss))
    o.array("double", "EXP_PLAN_FS", fss)
    o.array("int", "EXP_PLAN_SUM", [list(p[1][LOW_N]) + list(p[1][HIGH_N]) for p in plans])
    o.array("double", "EXP_PLAN_EDGE", [p[2] for p in plans])
    o.array("double", "EXP_PLAN_CENTRE", [[b["centre"] for b in p[0]] for p in plans])
    o.array("double", "EXP_PLAN_TILT", [[b["tilt"] for b in p[0]] for p in plans])
    o.array("int", "EXP_PLAN_FFT", [[b["n"] for b in p[0]] for p in plans])
    o.array("int", "EXP_PLAN_LOBIN", [[b["lo"] for b in p[0]] for p in plans])
    o.array("int", "EXP_PLAN_HIBIN", [[b["hi"] for b in p[0]] for p in plans])
    o.array("double", "EXP_PLAN_LOFRAC", [[b["lof"] for b in p[0]] for p in plans])
    o.array("double", "EXP_PLAN_HIFRAC", [[b["hif"] for b in p[0]] for p in plans])
    o.array("int", "EXP_PLAN_BELOW_510", [band_quality(fs)[1] for fs in fss])
    o.array("double", "EXP_PLAN_COL500", [column_of(fs, 500.0) for fs in fss])

    o.comment("single edge -> bin (fs 44100): FFT size, frequency, bin, fraction")
    ex = [(1024, f) for f, _, _ in SPEC_EDGE_EXAMPLES_1024] + [(2048, f) for f, _, _ in SPEC_EDGE_EXAMPLES_2048]
    ex += [(1024, 5.0), (1024, 0.0), (2048, 22050.0), (2048, 1.0)]
    rows = []
    for n, f in ex:
        b, fr = edge_to_bin(float(f), 44100.0, n)
        rows.append([n, float(f), b, fr])
    o.define("EXP_NEDGE", len(rows))
    o.array("double", "EXP_EDGE", rows)

    o.comment("band value in dB (512 bins): kind (0 = ramp -90 + k, 1 = flat base + overrides), base, overrides "
              "(index, value; index -1 = unused), map (loBin, loFrac, hiBin, hiFrac), expected")
    o.define("EXP_NBD", len(BAND_DB_CASES))
    o.array("int", "EXP_BD_KIND", [0 if c[1] is None else 1 for c in BAND_DB_CASES])
    o.array("double", "EXP_BD_BASE", [0.0 if c[1] is None else c[1] for c in BAND_DB_CASES])
    o.array("int", "EXP_BD_OVER_IDX", [[(c[2][i][0] if i < len(c[2]) else -1) for i in range(4)] for c in BAND_DB_CASES])
    o.array("double", "EXP_BD_OVER_VAL", [[(c[2][i][1] if i < len(c[2]) else 0.0) for i in range(4)] for c in BAND_DB_CASES])
    o.array("int", "EXP_BD_LOBIN", [c[3][0] for c in BAND_DB_CASES])
    o.array("double", "EXP_BD_LOFRAC", [c[3][1] for c in BAND_DB_CASES])
    o.array("int", "EXP_BD_HIBIN", [c[3][2] for c in BAND_DB_CASES])
    o.array("double", "EXP_BD_HIFRAC", [c[3][3] for c in BAND_DB_CASES])
    o.array("double", "EXP_BD_VALUE", [band_value_map(band_db_array(c[1], c[2]), *c[3]) for c in BAND_DB_CASES])

    o.comment("peak trajectory: bar 0.8 on ticks 0..4, then 0; level and rows of every tick; first zero tick from bar 1.0")
    traj = peak_trajectory()
    o.define("EXP_NPEAK", len(traj))
    o.define("EXP_PEAK_BAR_TICKS", 5)
    o.define("EXP_PEAK_BAR_VALUE", "0.8")
    o.array("double", "EXP_PEAK_LEVEL", traj)
    o.array("int", "EXP_PEAK_ROWS", [bar_rows(v) for v in traj])
    o.define("EXP_PEAK_ZERO_TICK_FROM_1", peak_zero_tick_from_full())

    o.comment("rows: the specification's inputs, and an exact grid v = i / 256")
    o.define("EXP_NROWS", len(SPEC_ROWS))
    o.array("double", "EXP_ROWS_V", [v for v, _ in SPEC_ROWS])
    o.array("int", "EXP_ROWS_N", [bar_rows(v) for v, _ in SPEC_ROWS])
    grid = []
    for i in range(257):
        grid.append((i + 8) // 16)          # 16 * i / 256 + 0.5, rounded down
    o.define("EXP_NGRID", len(grid))
    o.array("int", "EXP_ROWS_GRID", grid)

    o.comment("whole chain, settled steady signals: fs, tone 1 Hz, amplitude, tone 2 Hz, amplitude (0 = none); "
              "the AGC reference; per band: weighted dB, level, rows (reference), and the specification's own "
              "figures (flag 0 = unlisted, 1 = listed, 2 = a band holding a tone)")
    tones = []
    tref, tdb, tlv, trw, sdb, slv, srw, sfl = [], [], [], [], [], [], [], []
    for fs, sig, _, listed in SPEC_TONES:
        vals, ref, levels, rws = steady_bands(float(fs), sig)
        two = list(sig) + [(0.0, 0.0)] * (2 - len(sig))
        tones.append([float(fs), two[0][0], two[0][1], two[1][0], two[1][1]])
        tref.append(ref)
        tdb.append(vals)
        tlv.append(levels)
        trw.append(rws)
        sdb.append([listed[i][0] if i in listed else 0.0 for i in range(BANDS)])
        slv.append([listed[i][1] if i in listed else 0.0 for i in range(BANDS)])
        srw.append([listed[i][2] if i in listed else 0 for i in range(BANDS)])
        sfl.append([(2 if listed[i][3] else 1) if i in listed else 0 for i in range(BANDS)])
    o.define("EXP_NTONES", len(tones))
    o.array("double", "EXP_TONE", tones)
    o.array("double", "EXP_TONE_REF", tref)
    o.array("double", "EXP_TONE_DB", tdb)
    o.array("double", "EXP_TONE_LEVEL", tlv)
    o.array("int", "EXP_TONE_ROWS", trw)
    o.array("double", "EXP_TONE_SPEC_DB", sdb)
    o.array("double", "EXP_TONE_SPEC_LEVEL", slv)
    o.array("int", "EXP_TONE_SPEC_ROWS", srw)
    o.array("int", "EXP_TONE_SPEC_FLAG", sfl)

    o.comment("time response of one band (44100 Hz, from silence): frames, weighted dB. "
              "1 kHz A 0.125 on / off (band EXP_TR_HI_BAND), 100 Hz A 0.125 on (band EXP_TR_LO_BAND)")
    hi_on, hi_off = band_response(44100.0, 1000.0, 0.125, SPEC_TR_HIGH_BAND, set(SPEC_TR_HIGH_ON), set(SPEC_TR_HIGH_OFF))
    lo_on, _ = band_response(44100.0, 100.0, 0.125, SPEC_TR_LOW_BAND, set(SPEC_TR_LOW_ON), {1})
    o.define("EXP_TR_HI_BAND", SPEC_TR_HIGH_BAND)
    o.define("EXP_TR_LO_BAND", SPEC_TR_LOW_BAND)
    for name, res in (("HI_ON", hi_on), ("HI_OFF", hi_off), ("LO_ON", lo_on)):
        keys = sorted(res)
        o.define("EXP_NTR_" + name, len(keys))
        o.array("int", "EXP_TR_%s_N" % name, keys)
        o.array("double", "EXP_TR_%s_DB" % name, [res[k] for k in keys])

    o.comment("whole chain over time with AGC (44100 Hz, EXP_TL_HZ tone, 100 ticks/s, one high frame per tick, a low "
              "frame when ((t + 1) * 3) / 10 > (t * 3) / 10): segments (ticks, amplitude); at the listed ticks: "
              "reference dB, level and rows of every band")
    tl = chain_timeline()
    o.define("EXP_TL_HZ", fmt(TIMELINE_HZ))
    o.define("EXP_NTL_SEG", len(TIMELINE))
    o.define("EXP_NTL", len(TIMELINE_TICKS))
    o.define("EXP_TL_BAND", SPEC_TIMELINE_BAND)
    o.array("double", "EXP_TL_SEG", [[float(t), a] for t, a in TIMELINE])
    o.array("int", "EXP_TL_TICK", TIMELINE_TICKS)
    o.array("double", "EXP_TL_REF", [tl[t][0] for t in TIMELINE_TICKS])
    o.array("double", "EXP_TL_LEVEL", [tl[t][1] for t in TIMELINE_TICKS])
    o.array("int", "EXP_TL_ROWS", [[bar_rows(v) for v in tl[t][1]] for t in TIMELINE_TICKS])

    o.lines.append("")
    o.lines.append("#endif /* SPECTRUM_EXPECTED_H */")
    return o.text()


def print_literals():
    """Prints the SPEC_* blocks of this file and the tables of SPEC-LED.md from the formulas (used when the
    specification changes; the output is reviewed and pasted, it is not read back automatically)."""
    bands, rng, e = plan(44100.0)
    print("SPEC_PLAN_44100 = [  # loBin, loFrac, hiBin, hiFrac, tilt dB")
    for b in bands:
        print("    (%d, %.4f, %d, %.4f, %.2f)," % (b["lo"], b["lof"], b["hi"], b["hif"], b["tilt"]))
    print("]")
    print("SPEC_PLAN_PARTIAL = {  # fs -> {band: (loBin, loFrac, hiBin, hiFrac)}")
    for fs in (48000, 8000, 32000, 22050):
        bs, r_, _ = plan(float(fs))
        nl = r_[LOW_N][0]
        print("    %d: {%s}," % (fs, ", ".join("%d: (%d, %.4f, %d, %.4f)" % (i, bs[i]["lo"], bs[i]["lof"], bs[i]["hi"],
                                                                          bs[i]["hif"]) for i in (0, nl - 1, nl, 23))))
    print("}")
    print("SPEC_SUMMARY = {  # fs -> (low n, first, last, high n, first, last)\n    %s}"
          % ", ".join("%d: %r" % (fs, tuple(plan(float(fs))[1][LOW_N] + plan(float(fs))[1][HIGH_N]))
                      for fs in (44100, 48000, 8000, 32000, 22050)))
    print("SPEC_EDGES_44100 = [%s]" % ", ".join("%.4f" % v for v in e))
    print("SPEC_QUALITY = {  # fs -> (narrowest band in bins of its FFT, columns with upper edge <= 510 Hz)\n    %s}"
          % ", ".join("%d: (%.2f, %d)" % ((fs,) + band_quality(float(fs))) for fs in (44100, 48000, 8000, 32000, 22050)))
    print("SPEC_TONES = [  # (fs, signal, AGC reference dB, {band: (dB, level, rows, band holds a tone)})")
    for fs, sig in TONE_SIGNALS:
        vals, ref, levels, rows = steady_bands(float(fs), sig)
        bs = bands_of(fs)
        listed = []
        for i in range(BANDS):
            if rows[i] >= 3:
                bold = 1 if any(bs[i]["lo_hz"] <= f < bs[i]["hi_hz"] for f, _ in sig) else 0
                listed.append("%d: (%.2f, %.4f, %d, %d)" % (i, vals[i], levels[i], rows[i], bold))
        print("    (%d, %r, %.2f, {%s})," % (fs, sig, ref, ", ".join(listed)))
    print("]")
    refs, top, probe = agc_sequence()
    ticks = [0, 99, 100, 101, 110, 120, 150, 399, 400, 500, 700, 999, 1000, 1001, 1010, 1050, 1299, 1300, 1400, 1600,
             1799, 1800, 1900, 2099]
    print("SPEC_AGC_SEQ = {  # tick: (reference dB, level of the loudest band, level of the probe band)\n    %s}"
          % ", ".join("%d: (%.3f, %.4f, %.4f)" % (t, refs[t], top[t], probe[t]) for t in ticks))
    print("SPEC_COL500 = {%s}   # column position of 500 Hz"
          % ", ".join("%d: %.2f" % (fs, column_of(float(fs), 500.0)) for fs in (44100, 48000, 32000, 22050, 8000)))
    refs, top, probe = agc_mode_sequence()
    ticks = [0, 99, 100, 149, 150, 449, 450, 600, 849, 850, 949, 950, 951, 960, 1000, 1049, 1050, 1149, 1150, 1250,
             1349, 1350, 1450, 1549]
    print("SPEC_AGC_MODE_SEQ = {  # tick: (reference dB, level of the loudest band, level of the probe band)\n    %s}"
          % ", ".join("%d: (%.3f, %.4f, %.4f)" % (t, refs[t], top[t], probe[t]) for t in ticks))
    for band, f, name, on_set, off_set in ((band_of(44100.0, 1000.0), 1000.0, "HIGH", (1, 2, 3, 5, 10, 20, 40),
                                            (1, 5, 10, 20, 27)),
                                           (band_of(44100.0, 100.0), 100.0, "LOW", (1, 2, 3, 5, 10), (1,))):
        on, off = band_response(44100.0, f, 0.125, band, set(on_set), set(off_set))
        print("SPEC_TR_%s_BAND = %d" % (name, band))
        print("SPEC_TR_%s_ON = {%s}" % (name, ", ".join("%d: %.2f" % (t, on[t]) for t in sorted(on))))
        if name == "HIGH":
            print("SPEC_TR_HIGH_OFF = {%s}" % ", ".join("%d: %.2f" % (t, off[t]) for t in sorted(off)))
    tl = chain_timeline()
    band = band_of(44100.0, TIMELINE_HZ)
    print("SPEC_TIMELINE_BAND = %d" % band)
    print("SPEC_TIMELINE = {  # tick: (reference dB, level, rows) of the tone's band\n    %s}"
          % ", ".join("%d: (%.2f, %.4f, %d)" % (t, tl[t][0], tl[t][1][band], bar_rows(tl[t][1][band]))
                      for t in TIMELINE_TICKS))
    print("#@END@")
    for fs in (44100, 48000, 32000, 22050, 8000):
        bs, r_, ee = plan(float(fs))
        print("\nfs = %d:\n" % fs)
        print("| band | lo Hz | hi Hz | centre Hz | tilt dB | FFT | loBin | loFrac | hiBin | hiFrac |")
        print("|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
        for b in bs:
            print("| %d | %.3f | %.3f | %.3f | %+.2f | %d | %d | %.4f | %d | %.4f |"
                  % (b["i"], b["lo_hz"], b["hi_hz"], b["centre"], b["tilt"], b["n"], b["lo"], b["lof"], b["hi"], b["hif"]))


# ---------------------------------------------------------------------------------------------- music-like input
# Not part of the generated header: a look at what the panel does with music-like input, for the report and for
# choosing the AGC constants.  python3 -I -B tools/tests/spectrum_reference.py --music
LEGACY_MIN_DB, LEGACY_MAX_DB = -85.0, -25.0     # the fixed range before 2026-11-25


def legacy_edges(fs):
    """Band edges before 2026-11-25: equal steps on the log axis."""
    f_max = min(MAX_HZ, fs / 2.0)
    return [MIN_HZ * (f_max / MIN_HZ) ** (i / BANDS) for i in range(BANDS + 1)]


def plan_from_edges(e, fs, with_tilt):
    bands = []
    for i in range(BANDS):
        centre = math.sqrt(e[i] * e[i + 1])
        n = LOW_N if centre < SPLIT_HZ else HIGH_N
        lo, lof = edge_to_bin(e[i], fs, n)
        hi, hif = edge_to_bin(e[i + 1], fs, n)
        if hi < lo:
            hi, hif = lo, lof
        bands.append(dict(i=i, n=n, lo=lo, lof=lof, hi=hi, hif=hif, centre=centre,
                          tilt=tilt_db(centre) if with_tilt else 0.0))
    return bands


def music_signal(fs, seconds, seed=1):
    """Pink noise + a kick drum every 0.5 s + a bass line, as floats (not yet scaled)."""
    import random
    rnd = random.Random(seed)
    n = int(fs * seconds)
    # pink noise: ten one-pole low-passes of independent white noise, corners an octave apart, equal power each
    poles = []
    for k in range(10):
        a = 1.0 - math.exp(-2.0 * math.pi * 20.0 * 2 ** k / fs)
        poles.append([a, 0.0, math.sqrt((2.0 - a) / a)])
    out = [0.0] * n
    notes = [55.0, 65.41, 73.42, 49.0]
    for t in range(n):
        v = 0.0
        for pole in poles:
            pole[1] += pole[0] * (rnd.uniform(-1.0, 1.0) - pole[1])
            v += pole[1] * pole[2]
        sec = t / fs
        # kick: 150 -> 50 Hz sweep, 0.12 s decay
        tk = sec % 0.5
        phase = 2.0 * math.pi * (50.0 * tk + 100.0 * 0.03 * (1.0 - math.exp(-tk / 0.03)))
        kick = math.sin(phase) * math.exp(-tk / 0.12)
        # bass: one note per second with harmonics 1/k
        f0 = notes[int(sec) % len(notes)]
        bass = sum(math.sin(2.0 * math.pi * f0 * k * sec) / k for k in range(1, 7))
        out[t] = 0.10 * v + 1.0 * kick + 0.35 * bass
    return out


def music_frames(signal, fs, rms_db):
    """Scaled magnitudes of every FFT frame of both FFTs, keyed by the sample index at which the frame is complete."""
    return signal_frames(scale_to_rms(signal, rms_db), fs)


def scale_to_rms(signal, rms_db):
    rms = math.sqrt(sum(v * v for v in signal) / len(signal))
    gain = 10.0 ** (rms_db / 20.0) / rms
    return [max(-1.0, min(1.0, v * gain)) for v in signal]     # 16-bit PCM clips at full scale


def signal_frames(x, fs):
    """As music_frames, for samples that are already scaled to +-1."""
    low_scale, high_scale, s2048, _ = scales()
    act_scale = math.sqrt(0.375 * LOW_N / s2048)               # the firmware's activity measure (Hann-equivalent)
    frames = {LOW_N: [], HIGH_N: []}
    activity = []
    for n, fps, scale in ((LOW_N, LOW_FPS, low_scale), (HIGH_N, HIGH_FPS, high_scale)):
        stride = int(round(fs / fps))
        win = [window(i / (n - 1)) for i in range(n)]
        end = n
        while end <= len(x):
            spec = fft([x[end - n + i] * win[i] for i in range(n)])
            mags = [abs(spec[k]) for k in range(n // 2)]
            if n == LOW_N:
                k0 = int(math.ceil(22.0 * n / fs))
                k1 = min(int(16000.0 * n / fs), n // 2 - 1)
                activity.append(sum(mags[k0:k1 + 1]) / (k1 - k0 + 1) * act_scale)
            frames[n].append((end, [m * scale for m in mags]))
            end += stride
    return frames, activity


def music_rows(frames, fs, bands, agc_on):
    """Bar rows of every 10 ms tick for one layout: list of 24-row-count lists. (agc_on: sd_agc_step on every tick;
    the report uses panel_ticks for the current chain, which also picks step / quiet as the firmware does.)"""
    r = {LOW_N: retention(TAU, LOW_FPS), HIGH_N: retention(TAU, HIGH_FPS)}
    smooth = {LOW_N: [0.0] * (LOW_N // 2), HIGH_N: [0.0] * (HIGH_N // 2)}
    nxt = {LOW_N: 0, HIGH_N: 0}
    held = [SILENCE_DB] * BANDS
    agc = Agc()
    tick_samples = int(round(fs / TICK_FPS))
    last = max(frames[LOW_N][-1][0], frames[HIGH_N][-1][0])
    rows, refs = [], []
    pos = tick_samples
    while pos <= last:
        for n in (LOW_N, HIGH_N):
            fresh = None
            while nxt[n] < len(frames[n]) and frames[n][nxt[n]][0] <= pos:
                mags = frames[n][nxt[n]][1]
                s = smooth[n]
                rr = r[n]
                for k in range(n // 2):
                    s[k] = rr * s[k] + (1.0 - rr) * mags[k]
                d = [to_db(v) for v in s]
                vals = {b["i"]: band_weighted(d, b) for b in bands if b["n"] == n}
                if fresh is None:
                    fresh = vals
                else:
                    fresh = {i: max(fresh[i], vals[i]) for i in vals}
                nxt[n] += 1
            if fresh:
                for i, v in fresh.items():
                    held[i] = v
        if agc_on:
            agc.step(held)
            rows.append([bar_rows(agc.level(v)) for v in held])
            refs.append(agc.ref)
        else:
            rows.append([bar_rows(min(1.0, max(0.0, (v - LEGACY_MIN_DB) / (LEGACY_MAX_DB - LEGACY_MIN_DB))))
                         for v in held])
        pos += tick_samples
    return rows, refs


def music_report(seconds=12.0, skip_s=2.0, levels=(-30.0, -18.0, -6.0)):
    fs = 44100.0
    signal = music_signal(fs, seconds)
    before = plan_from_edges(legacy_edges(fs), fs, False)
    after, _, _ = plan(fs)
    skip = int(skip_s * TICK_FPS)
    print("music-like input (pink noise + kick every 0.5 s + bass line), %g s at 44.1 kHz, the first %g s skipped"
          % (seconds, skip_s))
    for rms_db in levels:
        frames, activity = music_frames(signal, fs, rms_db)
        act = sorted(activity)
        print("")
        print("%g dBFS RMS: activity measure median %.2f (sleep below %.2f, wake above %.2f)"
              % (rms_db, act[len(act) // 2], 0.12, 0.18))
        for name, bands, agc_on in (("before", before, False), ("after ", after, True)):
            if agc_on:
                ticks = panel_ticks(None, fs, sleep=False, pre=(frames, activity))
                rows, refs = [t[2] for t in ticks], [t[1] for t in ticks]
            else:
                rows, refs = music_rows(frames, fs, bands, agc_on)
            rows = rows[skip:]
            mean = [sum(r_[i] for r_ in rows) / len(rows) for i in range(BANDS)]
            pinned = [100.0 * sum(1 for r_ in rows if r_[i] >= 16) / len(rows) for i in range(BANDS)]
            print("  %s mean rows  : %s" % (name, " ".join("%4.1f" % v for v in mean)))
            print("  %s pinned (%%) : %s" % (name, " ".join("%4.0f" % v for v in pinned)))
            print("  %s all columns: mean %.1f rows, any column pinned in %.1f %% of the ticks%s"
                  % (name, sum(mean) / BANDS, 100.0 * sum(1 for r_ in rows if max(r_) >= 16) / len(rows),
                     (", reference %.1f .. %.1f dB" % (min(refs[skip:]), max(refs[skip:]))) if refs else ""))


# ---------------------------------------------------------------------------------------------- panel with sleep
# Not part of the generated header either: the chain together with the firmware's sleep logic (spectrum_analyzer.cpp:
# activity measure, 2 s average, SPECTRUM_SLEEP_* thresholds, fades) and its choice of the AGC move per tick, for
# the on-device test signals of TESTING.md section 9.  python3 -I -B tools/tests/spectrum_reference.py --panel
SLEEP_ENTER_MAG, SLEEP_EXIT_MAG = 0.12, 0.18
SLEEP_AVG_S, SLEEP_FADE_S, WAKE_FADE_S = 2.0, 2.0, 0.25


def panel_ticks(x, fs, rule="gated", sleep=True, pre=None):
    """Per 10 ms tick: (visibility, reference dB, rows of the 24 columns, highest peak dot in rows).
    rule "gated": step / quiet / snap as in section 2.10; "first": the first build of 2026-11-25 (reference put to
    the floor while dark, then always a step). sleep=False: the bars are always fully visible. pre: the result of
    signal_frames(x, fs), if the caller has it already."""
    frames, activity = pre if pre is not None else signal_frames(x, fs)
    bands, _, _ = plan(fs)
    r = {LOW_N: retention(TAU, LOW_FPS), HIGH_N: retention(TAU, HIGH_FPS)}
    smooth = {LOW_N: [0.0] * (LOW_N // 2), HIGH_N: [0.0] * (HIGH_N // 2)}
    nxt = {LOW_N: 0, HIGH_N: 0}
    held = [SILENCE_DB] * BANDS
    agc = Agc()
    tick_samples = int(round(fs / TICK_FPS))
    low_dt = int(round(fs / LOW_FPS)) / fs
    rolling, asleep, quiet, vis = 0.0, True, True, 0.0
    marker = [[0.0, 0.0] for _ in range(BANDS)]     # peak dots: level, hold ticks (section 2.8)
    out = []
    pos = tick_samples
    last = len(x) if x is not None else max(frames[LOW_N][-1][0], frames[HIGH_N][-1][0])
    while pos <= last:
        for n in (LOW_N, HIGH_N):
            fresh = None
            while nxt[n] < len(frames[n]) and frames[n][nxt[n]][0] <= pos:
                mags = frames[n][nxt[n]][1]
                s = smooth[n]
                rr = r[n]
                for k in range(n // 2):
                    s[k] = rr * s[k] + (1.0 - rr) * mags[k]
                d = [to_db(v) for v in s]
                vals = {b["i"]: band_weighted(d, b) for b in bands if b["n"] == n}
                fresh = vals if fresh is None else {i: max(fresh[i], vals[i]) for i in vals}   # loudest since the tick
                for i, v in fresh.items():
                    held[i] = v
                if n == LOW_N:
                    act = activity[nxt[n]]
                    rolling += (act - rolling) * low_dt / (SLEEP_AVG_S + low_dt)
                    if asleep and rolling >= SLEEP_EXIT_MAG:
                        asleep = False
                    elif not asleep and rolling < SLEEP_ENTER_MAG:
                        asleep = True
                    quiet = act < SLEEP_ENTER_MAG
                nxt[n] += 1
        if not sleep:
            vis = 1.0
        elif asleep:
            vis = max(0.0, vis - 1.0 / (TICK_FPS * SLEEP_FADE_S))
        else:
            vis = min(1.0, vis + 1.0 / (TICK_FPS * WAKE_FADE_S))
        if rule == "first":
            if vis <= 0.0:
                agc.ref = AGC_FLOOR_DB
            agc.step(held)
        elif vis <= 0.0:
            agc.snap(held)
        elif quiet:
            agc.quiet(held)
        else:
            agc.step(held)
        levels = [agc.level(v) for v in held]
        for m, lv in zip(marker, levels):
            if rule == "gated" and vis <= 0.0:
                m[0], m[1] = 0.0, 0.0
            if lv >= m[0]:
                m[0], m[1] = lv, float(PEAK_HOLD_TICKS)
            elif m[0] > 0.0:
                m[1] -= 1.0
                if m[1] < 0.0:
                    m[0] = max(0.0, m[0] + m[1] * PEAK_FALL / (TICK_FPS * TICK_FPS))
        out.append((vis, agc.ref, [bar_rows(lv * vis) for lv in levels], max(bar_rows(m[0] * vis) for m in marker)))
        pos += tick_samples
    return out


def tone_samples(fs, seconds, hz, amp):
    return [amp * math.sin(2.0 * math.pi * hz * t / fs) for t in range(int(fs * seconds))]


def white_samples(fs, seconds, rms_db, seed=7):
    import random
    rnd = random.Random(seed)
    g = 10.0 ** (rms_db / 20.0)
    return [max(-1.0, min(1.0, rnd.gauss(0.0, g))) for _ in range(int(fs * seconds))]


def pink_samples(fs, seconds, rms_db, seed=3):
    import random
    rnd = random.Random(seed)
    poles = [[1.0 - math.exp(-2.0 * math.pi * 20.0 * 2 ** j / fs), 0.0] for j in range(10)]
    x = []
    for _ in range(int(fs * seconds)):
        v = 0.0
        for pole in poles:
            pole[1] += pole[0] * (rnd.uniform(-1.0, 1.0) - pole[1])
            v += pole[1] * math.sqrt((2.0 - pole[0]) / pole[0])
        x.append(v)
    return scale_to_rms(x, rms_db)


def first_lit(ticks, start=0):
    for i in range(start, len(ticks)):
        if max(ticks[i][2]) > 0:
            return i / TICK_FPS
    return None


def lit_columns(rows, least=3):
    return " ".join("%d:%d" % (i, v) for i, v in enumerate(rows) if v >= least) or "-"


def panel_report():
    fs = 44100.0
    print("on-device test signals with the sleep logic, 44.1 kHz; columns from 0, 'c:r' = column c shows r rows")
    print("")
    print("steady sines (amplitude A; ffmpeg's sine source is A = 0.125, with volume=12dB A = 0.5):")
    for hz, amp in ((1000.0, 0.125), (60.0, 0.125), (1000.0, 0.5), (60.0, 0.5), (100.0, 0.5), (250.0, 0.5),
                    (500.0, 0.5), (10000.0, 0.5), (1000.0, 1.0)):
        ticks = panel_ticks(tone_samples(fs, 8.0, hz, amp), fs)
        lit = first_lit(ticks)
        full = next((i / TICK_FPS for i, t in enumerate(ticks) if t[0] >= 1.0), None)
        print("  %6g Hz A %-5g: first row lit after %s, full brightness after %s, most rows on the way %d, at 8 s: %s"
              % (hz, amp, "%.1f s" % lit if lit is not None else "never (8 s)",
                 "%.1f s" % full if full is not None else "never", max(max(t[2]) for t in ticks),
                 lit_columns(ticks[-1][2])))
    print("")
    print("sweep 30 Hz -> 16 kHz in 20 s, A 0.5, f(t) = 30 * 533.33^(t/20): highest column once per second")
    k = math.log(16000.0 / 30.0) / 20.0
    x = [0.5 * math.sin(2.0 * math.pi * 30.0 / k * (math.exp(k * t / fs) - 1.0)) for t in range(int(fs * 20.0))]
    ticks = panel_ticks(x, fs)
    lit = first_lit(ticks)
    print("  first row lit after %.1f s (the sweep is at %.0f Hz then)" % (lit, 30.0 * math.exp(k * lit)))
    line = []
    for sec in range(1, 20):
        rows = ticks[sec * 100][2]
        top = max(range(BANDS), key=lambda i: rows[i])
        line.append("%ds %.0fHz %d:%d" % (sec, 30.0 * math.exp(k * sec), top, rows[top]))
    print("  " + ", ".join(line))
    print("")
    print("burst: 1 kHz A 1.0, 0.5 s on / 1.0 s off, 12 s: rows of column 10")
    x = [(1.0 if (t / fs) % 1.5 < 0.5 else 0.0) * math.sin(2.0 * math.pi * 1000.0 * t / fs)
         for t in range(int(fs * 12.0))]
    ticks = panel_ticks(x, fs)
    lit = first_lit(ticks)
    print("  first row lit after %.1f s; fading again at any time after that: %s"
          % (lit, "yes" if any(t[0] < 1.0 for t in ticks[int(lit * 100) + 30:]) else "no"))
    print("  from 9.0 s every 0.1 s: " + " ".join("%d" % ticks[i][2][10] for i in range(900, 1200, 10)))
    print("  most rows from 3 s on: %d" % max(max(t[2]) for t in ticks[300:]))
    print("")
    print("level switch: 1 kHz, A 1.0 for 10 s, A 0.25 for 10 s, A 1.0 for 4 s: rows of column 10")
    x = [(0.25 if 10.0 <= (t / fs) < 20.0 else 1.0) * math.sin(2.0 * math.pi * 1000.0 * t / fs)
         for t in range(int(fs * 24.0))]
    ticks = panel_ticks(x, fs)
    print("  after the drop at 10 s: " + ", ".join("+%.1fs %d" % (d, ticks[int((10.0 + d) * 100)][2][10])
                                                  for d in (0.1, 0.3, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 9.9)))
    print("  after the rise at 20 s: " + ", ".join("+%.2fs %d" % (d, ticks[int((20.0 + d) * 100)][2][10])
                                                  for d in (0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0)))
    print("")
    print("noise at -20 dBFS RMS, 8 s: mean rows per column from 4 s on")
    for name, x in (("pink ", pink_samples(fs, 8.0, -20.0)), ("white", white_samples(fs, 8.0, -20.0))):
        ticks = panel_ticks(x, fs)
        tail = [t[2] for t in ticks[400:]]
        print("  %s: first row lit after %.1f s; %s"
              % (name, first_lit(ticks), " ".join("%.0f" % (sum(r_[i] for r_ in tail) / len(tail)) for i in range(BANDS))))
    print("")
    print("music-like input at -12 dBFS RMS for 8 s, then white noise at -60 dBFS RMS for 10 s:")
    music = scale_to_rms(music_signal(fs, 8.0), -12.0)
    x = music + white_samples(fs, 10.0, -60.0)
    for name, rule in (("first build of 2026-11-25 (always stepping)", "first"), ("step / quiet / snap", "gated")):
        ticks = panel_ticks(x, fs, rule)
        dark = next((i / TICK_FPS for i in range(800, len(ticks)) if ticks[i][0] <= 0.0), None)
        print("  %s: most rows in each second after the music stops: %s; dark %s"
              % (name, " ".join("%d" % max(max(t[2]) for t in ticks[800 + 100 * j:900 + 100 * j]) for j in range(10)),
                 ("%.1f s after the stop" % (dark - 8.0)) if dark is not None else "not within 10 s"))
    print("")
    quiet_music = scale_to_rms(music_signal(fs, 5.0, seed=2), -30.0)
    for pause, what in ((3.0, "a pause, the bars stay awake"), (11.0, "the bars have faded out: a wake-up")):
        print("the same music, %g s of digital silence (%s), then music-like input at -30 dBFS RMS:" % (pause, what))
        x = music + [0.0] * int(fs * pause) + quiet_music
        start = int((8.0 + pause) * TICK_FPS)
        for name, rule in (("first build of 2026-11-25", "first"), ("step / quiet / snap  ", "gated")):
            ticks = panel_ticks(x, fs, rule)
            gone = next(i for i in range(800, len(ticks)) if max(ticks[i][2]) == 0)
            w = int(first_lit(ticks, start) * TICK_FPS)
            print("  %s: bars gone %.1f s after the stop, lit again %.1f s after the new start; in the next second "
                  "the highest bar is %d rows and the highest peak dot %d (a dot at 16 rows on %d ticks); mean of all "
                  "columns 1 / 2 s later %.1f / %.1f"
                  % (name, gone / TICK_FPS - 8.0, w / TICK_FPS - 8.0 - pause, max(max(t[2]) for t in ticks[w:w + 100]),
                     max(t[3] for t in ticks[w:w + 100]), sum(1 for t in ticks[w:w + 100] if t[3] >= 16),
                     sum(sum(t[2]) for t in ticks[w + 100:w + 200]) / (100.0 * BANDS),
                     sum(sum(t[2]) for t in ticks[w + 200:w + 300]) / (100.0 * BANDS)))


def main(argv):
    if "--music" in argv:
        music_report()
        return 0
    if "--panel" in argv:
        panel_report()
        return 0
    if "--print-literals" in argv:
        print_literals()
        return 0
    try:
        text = generate()
    except SpecMismatch as exc:
        sys.stderr.write("reference disagrees with the specification: %s\n" % exc)
        return 2
    if "--check" in argv:
        try:
            with open(HEADER_PATH, "rb") as fh:
                old = fh.read().decode("utf-8").replace("\r\n", "\n")
        except OSError:
            sys.stderr.write("%s is missing: run tools/tests/spectrum_reference.py\n" % HEADER_PATH)
            return 1
        if old != text:
            sys.stderr.write("spectrum_expected.h is out of date: run tools/tests/spectrum_reference.py\n")
            return 1
        print("spectrum_expected.h is up to date (reference agrees with all specification figures)")
        return 0
    with open(HEADER_PATH, "wb") as fh:
        fh.write(text.encode("utf-8"))
    print("wrote %s (%d lines); all specification figures reproduced" % (os.path.normpath(HEADER_PATH),
                                                                         text.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
