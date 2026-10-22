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

    # 7.13 diagnostic switches (default = the design)
    envelope_hard_filter: bool = False
    conf_includes_covis: bool = True
    clutter_rule: str = "ratio"


DEFAULT_PARAMS = Params()


def params_digest(p: Params) -> str:
    """First 12 hex digits of sha256(repr(p)); stored in the state for diagnostics."""
    return hashlib.sha256(repr(p).encode("utf-8")).hexdigest()[:12]
