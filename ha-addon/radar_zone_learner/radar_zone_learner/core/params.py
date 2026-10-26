"""Learner parameters (P3 spec Sections 6 and 7). One field per table row, same names and defaults.

The app always uses DEFAULT_PARAMS. The replay may pass a modified Params, for example one of the
three diagnostic switches at the end (spec Section 7.13), which default to the design.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Params:
    # 6.4 frame times, wall clock, censoring
    obs_gap_ms: int = 1000
    silence_censor_ms: int = 15000
    no_data_ms: int = 75000
    compressed_spacing_ms: int = 50
    offset_window_ms: int = 600000
    boot_history: int = 4

    # 7.1 coverage
    covered_min_s: int = 3600
    coverage_days: int = 42

    # 7.2 tracker
    gate_m: float = 0.9
    alpha: float = 0.35
    beta: float = 0.08
    confirm_hits: int = 2
    ttl_ms: int = 2000
    max_tracks: int = 6
    dt_max_s: float = 2.0
    unassigned_cost_m: float = 1.0
    sample_period_ms: int = 1000

    # 7.3 births, deaths, exit classes
    min_track_samples: int = 3
    exit_window_samples: int = 3
    moving_mps: float = 0.35
    powerup_birth_ms: int = 3000
    censor_death_ms: int = 3000

    # 7.4 walking points, fragments, EPP ghosts
    walk_step_m: float = 0.3
    frag_radius_m: float = 0.5
    frag_min_s: int = 8
    epp_sd_m: float = 0.01
    epp_min_s: int = 300

    # 7.5 stays
    bridge_gap_s: int = 900
    bridge_dist_m: float = 0.6
    bridge_ld_share: float = 0.9
    stay_min_s: int = 20
    stay_ld_min: float = 0.5
    home_empty_share: float = 0.5
    retention_covered_days: int = 14
    retention_max_days: int = 28
    time_round_s: int = 300
    dur_round_s: int = 10

    # 7.6 grids and decay
    cell_m: float = 0.2
    x0_m: float = -7.0
    y0_m: float = 0.0
    nx: int = 70
    ny: int = 40
    splat_sigma_m: float = 0.2
    splat_radius_cells: int = 2
    hl_slow_h: int = 336
    hl_fast_h: int = 72
    days_bits_days: int = 14

    # 7.7 COVIS
    covis_cell_m: float = 0.4
    covis_hl_h: int = 336
    covis_prune_s: float = 1.0
    covis_cap: int = 20000
    frag_ram_h: int = 24

    # 7.8.7 nightly extraction, gates, confidence
    nightly_at_min: int = 210
    env_walk_frac: float = 0.01
    env_dilate_cells: int = 3
    clutter_min_rate: float = 0.5
    peak_min_rate: float = 0.5
    grow_frac: float = 0.35
    grow_mono: float = 1.05
    class_split_s: int = 300
    dwell_min_stays: int = 6
    dwell_min_minutes: int = 20
    short_min_stays: int = 12
    short_max_spread_m: float = 0.6
    min_core_days: int = 4
    ld_share_min: float = 0.5
    ghost_share_max: float = 0.2
    covis_thr: float = 0.8
    covis_min_n: int = 6
    stable_nights: int = 3
    stable_iou: float = 0.6
    stable_shift_m: float = 0.15
    conf_n_scale: int = 8
    conf_days: int = 7
    conf_min: float = 0.5
    supp_overlap: float = 0.5

    # 7.8.9 shadow suggestions
    hit_iou: float = 0.3
    hit_dist_m: float = 0.5
    stale_nights: int = 7
    poly_max_vertices: int = 12

    # 7.8.10 doors, room outline, ignore zones
    door_min_rate: float = 0.3
    door_min_passages: int = 10
    door_min_days: int = 3
    outline_iou: float = 0.8
    outline_nights: int = 2
    outline_min_days: int = 3
    outline_min_walk_s: int = 200
    ignore_min_share: float = 0.8
    ignore_min_days: int = 3
    ignore_max_ld: float = 0.3
    ignore_min_static: float = 0.8

    # 7.9 profiles, freeze, bed naming
    tod_smooth_h: int = 3
    tod_on_frac: float = 0.1
    mode_min_stays: int = 10
    mode_min_days: int = 4
    share_rule_frac: float = 0.30
    freeze_night_share: float = 0.5
    default_window_min: tuple = (0, 360)
    bed_name_hours: tuple = (21, 9)
    bed_name_share: float = 0.6
    bed_name_median_s: int = 7200

    # 7.10 'Mark spot here' labels
    mark_min_stay_s: int = 20
    label_radius_m: float = 0.4
    label_match_m: float = 0.6
    bed_label_names: tuple = ("bed", "bett")
    spot_name_max: int = 32

    # 7.11 status (D24)
    status_period_ms: int = 300000

    # P4 night evidence and the night-window proposal (OA 7.5, 7.8). Times of day are minutes of the local day.
    night_lux: float = 5.0               # lux below this is 'lights off' (a P0 measurement replaces it)
    lux_dark_min_s: int = 900            # the lux proxy: lux stays below night_lux for 15 min (OA 7.5)
    evening_start_min: int = 1200        # lights-off times and radar onsets count from 20:00 ...
    night_onset_end_min: int = 240       # ... until 04:00
    onset_quiet_s: int = 900             # radar onset: no moving target for 15 min (OA 7.5)
    onset_replace_max_min: int = 60      # a later quiet start replaces the night's first radar onset only within 60 min
    onset_dark_s: int = 600              # radar onset: lux dark for this long (or the lights_off context)
    onset_ld_min: float = 0.9            # radar onset: LD2412 present for this share of the 15 min
    wake_run_s: int = 600                # wake: the first activity, followed by more than 10 min of activity
    wake_rest_s: int = 1800              # ... a first activity follows 30 min without a moving sample (or unobserved)
    wake_earliest_min: int = 180         # a wake is the first run of the morning that starts 03:00 ...
    wake_latest_min: int = 720           # ... to 12:00
    night_keep_nights: int = 28          # onsets, wakes and activity are kept for 28 nights
    night_res_min: int = 5               # ... at 5 min resolution
    nw_min_nights: int = 7               # night_window: >= 7 nights with each edge observed
    nw_iqr_max_min: int = 90             # ... and an IQR < 90 min
    nw_round_min: int = 15               # start = P90(onset) rounded up, end = P10(wake) rounded down, to 15 min
    nw_min_len_min: int = 60             # a proposed window shorter than 1 h or longer than 16 h is no proposal
    nw_max_len_min: int = 960
    nw_same_min: int = 15                # a proposal within 15 min of both current edges is already applied
    nw_deny_days: int = 30               # 'Deny' suppresses the night_window item for 30 days
    nw_late_min: int = 30                # 'activity was still seen in the 30 min before your current start'
    nw_late_nights: int = 14             # ... on N of the last 14 nights

    # P4 review items and Night learning (OA 7.6, 7.9, 4.2)
    review_min_nights: int = 7           # the setup review (and with it the item list) exists after >= 7 nights
    nl_nights: int = 7                   # Night learning keeps the schedule OFF for 7 nights
    nl_retry_s: int = 600                # a restore that was not confirmed is sent again after 10 min ...
    nl_restore_tries: int = 12           # ... at most 12 times (a refusal such as node_offline is no try: it waits)
    nl_result_timeout_s: int = 300       # no CommandResult within 5 min counts as 'no_echo'
    nl_unconfirmed_wait_s: int = 900     # an OFF the node did not confirm may still take effect: watched this long
    nl_restore_late_s: int = 86400       # a restore more than 24 h overdue is not sent (the user is told instead)

    # 7.13 diagnostic switches (default = the design)
    envelope_hard_filter: bool = False
    conf_includes_covis: bool = True
    clutter_rule: str = "ratio"


DEFAULT_PARAMS = Params()


def params_digest(p: Params) -> str:
    """First 12 hex digits of sha256(repr(p)); stored in the state for diagnostics."""
    return hashlib.sha256(repr(p).encode("utf-8")).hexdigest()[:12]
