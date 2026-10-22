import dataclasses
import unittest

from radar_zone_learner.core.params import DEFAULT_PARAMS, Params, params_digest

# Every parameter of the tables in spec Sections 6 and 7 (name, value), plus status_period_ms (7.11,
# D24) and the three diagnostic switches of Section 5.2.
TABLE = [
    # 6.4
    ("obs_gap_ms", 1000), ("silence_censor_ms", 15000), ("no_data_ms", 75000), ("compressed_spacing_ms", 50),
    ("offset_window_ms", 600000), ("boot_history", 4),
    # 7.1
    ("covered_min_s", 3600), ("coverage_days", 42),
    # 7.2
    ("gate_m", 0.9), ("alpha", 0.35), ("beta", 0.08), ("confirm_hits", 2), ("ttl_ms", 2000), ("max_tracks", 6),
    ("dt_max_s", 2.0), ("unassigned_cost_m", 1.0), ("sample_period_ms", 1000),
    # 7.3
    ("min_track_samples", 3), ("exit_window_samples", 3), ("moving_mps", 0.35), ("powerup_birth_ms", 3000),
    ("censor_death_ms", 3000),
    # 7.4
    ("walk_step_m", 0.3), ("frag_radius_m", 0.5), ("frag_min_s", 8), ("epp_sd_m", 0.01), ("epp_min_s", 300),
    # 7.5
    ("bridge_gap_s", 900), ("bridge_dist_m", 0.6), ("bridge_ld_share", 0.9), ("stay_min_s", 20),
    ("stay_ld_min", 0.5), ("home_empty_share", 0.5), ("retention_covered_days", 14), ("retention_max_days", 28),
    ("time_round_s", 300), ("dur_round_s", 10),
    # 7.6
    ("cell_m", 0.2), ("x0_m", -7.0), ("y0_m", 0.0), ("nx", 70), ("ny", 40), ("splat_sigma_m", 0.2),
    ("splat_radius_cells", 2), ("hl_slow_h", 336), ("hl_fast_h", 72), ("days_bits_days", 14),
    # 7.7
    ("covis_cell_m", 0.4), ("covis_hl_h", 336), ("covis_prune_s", 1.0), ("covis_cap", 20000), ("frag_ram_h", 24),
    # 7.8.7
    ("nightly_at_min", 210), ("env_walk_frac", 0.01), ("env_dilate_cells", 3), ("clutter_min_rate", 0.5),
    ("peak_min_rate", 0.5), ("grow_frac", 0.35), ("grow_mono", 1.05), ("class_split_s", 300),
    ("dwell_min_stays", 6), ("dwell_min_minutes", 20), ("short_min_stays", 12), ("short_max_spread_m", 0.6),
    ("min_core_days", 4), ("ld_share_min", 0.5), ("ghost_share_max", 0.2), ("covis_thr", 0.8), ("covis_min_n", 6),
    ("stable_nights", 3), ("stable_iou", 0.6), ("stable_shift_m", 0.15), ("conf_n_scale", 8), ("conf_days", 7),
    ("conf_min", 0.5), ("supp_overlap", 0.5),
    # 7.8.9
    ("hit_iou", 0.3), ("hit_dist_m", 0.5), ("stale_nights", 7), ("poly_max_vertices", 12),
    # 7.8.10
    ("door_min_rate", 0.3), ("door_min_passages", 10), ("door_min_days", 3), ("outline_iou", 0.8),
    ("outline_nights", 2), ("outline_min_days", 3), ("outline_min_walk_s", 200), ("ignore_min_share", 0.8),
    ("ignore_min_days", 3), ("ignore_max_ld", 0.3), ("ignore_min_static", 0.8),
    # 7.9
    ("tod_smooth_h", 3), ("tod_on_frac", 0.1), ("mode_min_stays", 10), ("mode_min_days", 4),
    ("share_rule_frac", 0.30), ("freeze_night_share", 0.5), ("default_window_min", (0, 360)),
    ("bed_name_hours", (21, 9)), ("bed_name_share", 0.6), ("bed_name_median_s", 7200),
    # 7.10
    ("mark_min_stay_s", 20), ("label_radius_m", 0.4), ("label_match_m", 0.6), ("bed_label_names", ("bed", "bett")),
    ("spot_name_max", 32),
    # 7.11 (D24)
    ("status_period_ms", 300000),
    # 5.2 / 7.13 diagnostic switches
    ("envelope_hard_filter", False), ("conf_includes_covis", True), ("clutter_rule", "ratio"),
]


class ParamsTest(unittest.TestCase):
    def test_every_row(self):
        names = [f.name for f in dataclasses.fields(Params)]
        self.assertEqual(sorted(names), sorted(n for n, _ in TABLE))
        self.assertEqual(len(set(n for n, _ in TABLE)), len(TABLE))
        for name, value in TABLE:
            got = getattr(DEFAULT_PARAMS, name)
            self.assertEqual(got, value, name)
            self.assertEqual(type(got) is bool, type(value) is bool, name)

    def test_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            DEFAULT_PARAMS.gate_m = 1.0

    def test_digest(self):
        d = params_digest(DEFAULT_PARAMS)
        self.assertEqual(len(d), 12)
        int(d, 16)
        self.assertEqual(d, params_digest(Params()))
        self.assertEqual(d, params_digest(DEFAULT_PARAMS))
        self.assertNotEqual(d, params_digest(dataclasses.replace(DEFAULT_PARAMS, conf_includes_covis=False)))
        self.assertNotEqual(d, params_digest(dataclasses.replace(DEFAULT_PARAMS, gate_m=0.91)))


if __name__ == "__main__":
    unittest.main()
