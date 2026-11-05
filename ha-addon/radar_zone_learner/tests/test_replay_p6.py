"""P6 replay plumbing (P6 spec 11.1, 11.6): the job variants and profiles, the scripted people, the zone truth, the stream
additions (quiet days, drop windows, the dropped record), the driver (tick, factory, recorder, night record) on a stub learner,
the actors, the compare/compat/probe additions. Everything is small; the one real-core run is two simulated days."""
import contextlib
import io
import json
import pickle
import random
import unittest
from pathlib import Path
from types import SimpleNamespace

from radar_zone_learner.core.params import DEFAULT_P6, P6Params
from radar_zone_learner.core.types import (FixedOffsetTime, FramesMessage, MarkPress, MarkResult, SpotName, ZoneEvent,
                                           ZoneState, ZoneStates)
from replay import compare_runs as CR
from replay import compat_check as CC
from replay import driver as D
from replay import evaluate_p4 as E4
from replay import framegen as G
from replay import jobs as J
from replay import p4_scenarios as P4
from replay import p6_people as PP
from replay import p6_scenarios as P6
from replay import resource_probe as RP
from replay import truth as T
from replay import world as W
from replay.framegen import BASE_MS
from tests._tmp import tmpdir
from tests.test_replay_p5 import StubFactory

DAY = 86400
ADDON = Path(__file__).resolve().parents[1]


def job(variant, nights=10):
    return J.parse_job("bed_w0000_0600_" + variant, nights=nights)


def block(center, half=1):
    """The cells of a (2 half + 1)-square block of 0.2 m cells around ``center`` (x, y)."""
    c0 = W.cell_of(*center)
    i, j = c0 % W.NX, c0 // W.NX
    return frozenset((j + dj) * W.NX + i + di for di in range(-half, half + 1) for dj in range(-half, half + 1))


# ---------------------------------------------------------------- jobs and profiles
class JobsP6Test(unittest.TestCase):
    def test_variants_parse_and_carry_their_flags(self):
        for v in J.P6_VARIANTS:
            j = job(v)
            self.assertEqual((j.variant, j.script), (v, v))
            self.assertTrue(j.is_p6 and j.is_p4 and not j.is_p5 and j.lux)
            self.assertFalse(j.labels or j.gated)
            self.assertEqual(j.tick_ms, 1000)
            self.assertEqual(pickle.loads(pickle.dumps(j)), j)
        self.assertEqual(J.P6_VARIANTS, ("p6edge", "p6edge05", "p6edge08", "p6hold", "p6pow", "p6door", "p6ld", "p6ldghost",
                                         "p6count", "p6setup", "p6guests", "p6guestsc"))

    def test_noise_levels_have_their_own_job_names(self):
        self.assertEqual([job(v).noise_m for v in ("p6edge", "p6edge05", "p6edge08")], [0.02, 0.05, 0.08])
        self.assertEqual(len({job(v).name for v in ("p6edge", "p6edge05", "p6edge08")}), 3)
        self.assertEqual(job("p6hold").noise_m, 0.02)

    def test_older_jobs_are_unchanged(self):
        for name in ("bed_w0000_0600", "liv_none", "bed_w0000_0600_p4", "bed_w0000_0600_p5setup", "bed_w0000_0600_p5guests"):
            j = J.parse_job(name)
            self.assertFalse(j.is_p6, name)
            self.assertEqual((j.tick_ms, j.noise_m, j.spells, j.gaps, j.crossings, j.programs), (10_000, 0.02, (), (), (), ()), name)
        self.assertEqual(J.parse_job("bed_w0000_0600_p5setup").extras(), job("p6setup").extras()[:4])
        self.assertEqual(len(J.runs_of(J.PROFILES["p5"])), 33)

    def test_every_p6_job_carries_the_p5_setup_person_and_two_add_their_own(self):
        bx, by = W.SPOT["bed"]
        x1 = [("X1", bx, by, d * DAY + 22 * 3600, d * DAY + 23 * 3600 + 58 * 60) for d in range(6, 10)]
        for v in J.P6_VARIANTS:
            self.assertEqual(job(v).extras()[:4], x1, v)
        self.assertEqual(len(job("p6edge").extras()), 4)
        ghost = job("p6ldghost").extras()[4:]
        self.assertEqual([e[0] for e in ghost], ["Y1"])                       # the reflector, not fed to the LD2412 model
        self.assertEqual(ghost[0][1:3], W.REFLECTOR)
        self.assertEqual(len(job("p6door").extras()), 4)                  # the door-side label stayer is a scripted person
        # the door is not the world's D2: the learner's door rule never makes a suggestion there (measured)
        self.assertGreater(abs(J.DOOR_SITE[0] - W.DOORS["D2"][0]), 1.0)

    def test_quiet_days_and_the_setup_job(self):
        self.assertEqual(job("p6edge").quiet_from_day, J.SCRIPT_FROM_DAY)
        self.assertIsNone(job("p6setup").quiet_from_day)                    # the world's agents stay: variants need stays
        self.assertEqual(job("p6setup").programs, ())

    def test_the_spells_and_gaps_are_fixed_at_construction_from_the_default_delays(self):
        sp = job("p6hold").spells
        self.assertEqual(len(sp), 20)                                         # 5 spots x 4 lengths
        by = {}
        for spot, g in sp:
            by.setdefault(spot, []).append(g)
        self.assertEqual(by["desk"], [150, 270, 330, 450])                    # 0.5, 0.9, 1.1, 1.5 x 300 s
        self.assertEqual(by["bed"], [300, 540, 660, 900])                     # x 600 s
        self.assertEqual(by["counter"], [30, 54, 66, 90])                     # x 60 s, minimum 30 s
        gaps = job("p6pow").gaps
        self.assertEqual(len(gaps), 24)
        self.assertTrue(all(d == J.P6_GAP_S == 5 for _, d in gaps))
        self.assertTrue(job("p6pow").faults)

    def test_the_profiles(self):
        smoke = J.PROFILES["p6smoke"]
        self.assertEqual([(j.name, j.nights, j.seeds) for j in smoke],
                         [("bed_w0000_0600_p6edge", 10, (1,)), ("bed_w0000_0600_p6hold", 10, (1,)),
                          ("bed_w0000_0600_p6door", 10, (1,))])
        p6 = J.PROFILES["p6"]
        self.assertEqual({j.variant for j in p6}, set(J.P6_VARIANTS))
        self.assertLess(J.estimate_days(p6), 1500)
        self.assertGreater(J.estimate_days(p6), 700)
        self.assertEqual(len(J.runs_of(p6)), 3 * 5 + 5 * 5 + 5 + 3 + 2 * 5)

    def test_run_replay_knows_the_profiles_and_the_report_modules(self):
        from replay import run_replay
        self.assertIn("p6", J.PROFILES)
        self.assertTrue(hasattr(run_replay, "E6"))


# ---------------------------------------------------------------- the scripted people
class PeopleTest(unittest.TestCase):
    def day(self, variant, d=9, seed=1):
        sp = PP.ScriptedPeople(job(variant), seed)
        tracks, occ = sp.day(d, random.Random(d))
        return sp, tracks, occ

    def test_nothing_before_day_8_and_nothing_without_programs(self):
        sp = PP.ScriptedPeople(job("p6edge"), 1)
        self.assertEqual(sp.day(7, random.Random(1)), ([], []))
        self.assertEqual(PP.ScriptedPeople(job("p6setup"), 1).day(9, random.Random(1)), ([], []))

    def test_the_people_are_deterministic_in_the_seed_and_the_day(self):
        a, _, _ = self.day("p6edge")
        b, _, _ = self.day("p6edge")
        self.assertEqual(a.log, b.log)
        sp = PP.ScriptedPeople(job("p6edge"), 1)
        sp.day(9, random.Random(99))
        self.assertNotEqual(sp.log[0]["tracks"], a.log[0]["tracks"])           # another random stream: another order

    def test_track_points_are_one_per_second_and_share_the_segment_ends(self):
        tr = [[10, 0.0, 0.0, 13, 3.0, 0.0], [13, 3.0, 0.0, 15, 3.0, 2.0]]
        pts = PP.track_points(tr)
        self.assertEqual([p[0] for p in pts], [10, 11, 12, 13, 14, 15])
        self.assertEqual(pts[1], (11, 1.0, 0.0))
        self.assertEqual(pts[4], (14, 3.0, 1.0))

    def test_a_walker_is_straight_exact_in_speed_and_centred_on_the_spot(self):
        for v in (0.5, 0.7, 1.0, 1.2):
            seg = PP.walk_through((2.0, 3.0), 45, 1, v, 100)
            n = seg[3] - seg[0]
            length = ((seg[4] - seg[1]) ** 2 + (seg[5] - seg[2]) ** 2) ** 0.5
            self.assertAlmostEqual(length / n, v, places=9)                    # exactly v metres per second
            self.assertGreaterEqual(length, 5.0 - 1e-9)                         # half length at least 2.5 m
            self.assertAlmostEqual((seg[1] + seg[4]) / 2, 2.0, places=9)
            self.assertAlmostEqual((seg[2] + seg[5]) / 2, 3.0, places=9)
        rev = PP.walk_through((2.0, 3.0), 45, -1, 1.0, 100)
        fwd = PP.walk_through((2.0, 3.0), 45, 1, 1.0, 100)
        self.assertAlmostEqual(rev[1], fwd[4], places=9)

    def test_the_edge_day_has_160_passes_and_the_52_sweep_stayers(self):
        sp, tracks, occ = self.day("p6edge")
        kinds = [p["kind"] for p in sp.log]
        self.assertEqual((kinds.count("walker"), kinds.count("sweep")), (160, 52))
        walkers = [p for p in sp.log if p["kind"] == "walker"]
        self.assertEqual({(p["spot"], p["speed"], p["angle"], p["dir"]) for p in walkers},
                         {(s, v, a, d) for s in J.P6_SPOTS for v in J.P6_SPEEDS for a in (0, 45, 90, 135) for d in (1, -1)})
        # one at a time, 10 s apart, from 08:00, noise-free kind human, not fed to the LD2412 model
        ends = sorted((p["tracks"][0][0][0], p["tracks"][0][0][3]) for p in walkers)
        for (a0, a1), (b0, b1) in zip(ends, ends[1:]):
            self.assertGreaterEqual(b0 - a1, 1 + PP.PASS_GAP_S)
        self.assertGreaterEqual(ends[0][0], 9 * DAY + 8 * 3600)
        self.assertTrue(all(t["kind"] == "human" for t in tracks))
        self.assertEqual(len(tracks), 212)
        ld = [x for p in sp.log if p["ld"] for x in p["ld_occ"]]
        self.assertEqual(len(occ), len(ld))
        self.assertEqual(len(occ), 52)                                         # only the sweep stayers feed the LD2412 model
        offs = sorted({p["offset_cm"] for p in sp.log if p["kind"] == "sweep"})
        self.assertEqual(offs, list(range(0, 61, 5)))

    def test_the_hold_program_has_16_spells_and_a_final_one_per_spot_and_feeds_the_ld_model_through_them(self):
        sp, tracks, occ = self.day("p6hold")
        self.assertEqual(len(sp.log), 5)
        for p in sp.log:
            self.assertEqual(len(p["spells"]), 17)
            self.assertEqual([s["ret"] for s in p["spells"]].count(False), 1)
            self.assertIsNone(p["spells"][-1]["g"])
            lengths = sorted(s["g"] for s in p["spells"][:-1])
            want = sorted(g for sp_, g in job("p6hold").spells if sp_ == p["spot"]) * 4
            self.assertEqual(lengths, sorted(want))
            # the hidden spells are inside the LD2412 occupancy of the person
            a, b = p["ld_occ"][0]
            self.assertLessEqual(a, p["tracks"][0][0][0])
            self.assertGreater(b, p["spells"][-1]["t"])
            # between two spells the person is visible for 30 s
            for t0, t1 in zip(p["tracks"], p["tracks"][1:]):
                self.assertEqual(t0[0][3] - t0[0][0], PP.HOLD_VISIBLE_S)
        for p in sp.log[1:]:
            self.assertGreater(p["tracks"][0][0][0], sp.log[0]["spells"][-1]["t"] + 100)

    def test_the_ld_program_has_the_hourly_cycles_and_an_unseen_leave(self):
        sp, tracks, occ = self.day("p6ld")
        self.assertEqual(len(sp.log), 14)
        for p in sp.log:
            sps = p["spells"]
            self.assertEqual(len(sps), PP.LD_CYCLES_PER_HOUR + 1)
            self.assertTrue(sps[-1].get("unseen"))
            self.assertEqual({s["g"] for s in sps[:-1]}, {240})
        self.assertEqual(len(tracks), 14 * 11)

    def test_the_pow_program_crosses_the_night_and_the_scripted_gaps(self):
        sp, tracks, occ = self.day("p6pow", d=9)
        notes = {p["note"]: p for p in sp.log}
        over = [p for p in sp.log if p["note"] == "overnight"]
        self.assertEqual(len(over), 2)
        for p in over:
            self.assertLess(p["tracks"][0][0][0], 10 * DAY)
            self.assertGreater(p["tracks"][0][0][3], 10 * DAY + 6 * 3600)       # past the 06:00 resume
        gap_s = {a for a, _ in job("p6pow").gaps if a // DAY == 9}
        vis = notes["gap_visible"]["tracks"][0][0]
        self.assertTrue(any(vis[0] < g < vis[3] for g in gap_s))                  # visible across a gap
        hid = notes["gap_hidden"]
        self.assertTrue(any(hid["spells"][0]["t"] < g < hid["spells"][0]["t"] + 60 for g in gap_s))   # hidden across one
        self.assertTrue(notes["fault_visible"]["tracks"][0][0][0] < 9 * DAY + 14 * 3600 < notes["fault_visible"]["tracks"][0][0][3])

    def test_the_door_program_has_no_exit_before_day_12_and_the_desk_movers_always(self):
        sp9, _, _ = self.day("p6door", d=9)
        sp12, _, _ = self.day("p6door", d=12)
        k9, k12 = [p["kind"] for p in sp9.log], [p["kind"] for p in sp12.log]
        self.assertEqual((k9.count("door_in"), k9.count("door_exit"), k9.count("mover_vanish"), k9.count("door_sitter")), (24, 0, 4, 4))
        sp8, _, _ = self.day("p6door", d=8)
        label = [p for p in sp8.log if p.get("note") == "label"]
        self.assertEqual(len(label), 1)                                              # one stationary stayer at the door-side spot
        self.assertEqual(label[0]["tracks"][0][0][1:3], list(J.DOOR_PROBE))
        self.assertEqual(label[0]["tracks"][0][0][0], 8 * DAY + J.REFLECTOR_LABEL_S[0])
        self.assertFalse([p for p in sp9.log if p.get("note") == "label"])
        self.assertEqual((k12.count("door_in"), k12.count("door_exit")), (24, 8))
        ex = next(p for p in sp12.log if p["kind"] == "door_exit")
        pts = PP.track_points(ex["tracks"][0])
        self.assertAlmostEqual(pts[-1][1], J.DOOR_SITE[0] - 0.2, places=3)         # ends 0.2 m inside the door
        # the speed over the last 3 s up to the last detection (the last waypoint is held for 0.9 s) is above 0.35 m/s
        last = T.track_position(pts, pts[-1][0] * 1000 + T.FINAL_HOLD_MS)
        before = T.track_position(pts, pts[-1][0] * 1000 + T.FINAL_HOLD_MS - 3000)
        self.assertGreater(((last[0] - before[0]) ** 2 + (last[1] - before[1]) ** 2) ** 0.5 / 3.0, 0.35)
        for p in sp12.log:
            self.assertEqual(len(p["spells"]), 0 if p["kind"] == "door_in" else 1)

    def test_the_count_program_has_the_three_cases(self):
        sp, tracks, occ = self.day("p6count", d=9)
        self.assertEqual(sorted(p["note"] for p in sp.log), ["A1", "A2", "B1", "B2", "B3", "C1", "C2"])
        c2 = next(p for p in sp.log if p["note"] == "C2")
        self.assertEqual(len(c2["spells"]), 1)                                      # it vanishes inside while C1 stays

    def test_the_busy_program_fills_the_day(self):
        sp, tracks, occ = self.day("p6guests", d=12)
        starts = [p["tracks"][0][0][0] for p in sp.log]
        self.assertGreater(len(sp.log), 150)
        self.assertGreaterEqual(min(starts), 12 * DAY + 8 * 3600)
        self.assertLess(max(starts), 12 * DAY + 22 * 3600)


# ---------------------------------------------------------------- the zone truth
class TruthP6Test(unittest.TestCase):
    def setUp(self):
        self.cells = block(W.SPOT["desk"])                                         # 3 x 3 cells = 0.6 m square
        self.xy = W.SPOT["desk"]

    def test_the_area_is_the_mask_grown_by_the_margin(self):
        a = T.ZoneArea(self.cells, 25)
        c0 = W.cell_of(*self.xy)
        x0 = W.GX0 + (c0 % W.NX) * W.CELL                                          # the left edge of the centre cell
        self.assertTrue(a.inside(self.xy[0], self.xy[1]))
        right = x0 + W.CELL * 2                                                    # the right edge of the mask (centre + 1 cell)
        self.assertTrue(a.inside(right + 0.20, self.xy[1]))                        # 20 cm outside the mask: in the margin
        self.assertFalse(a.inside(right + 0.30, self.xy[1]))                       # 30 cm: out
        self.assertFalse(T.ZoneArea(self.cells, 0).inside(right + 0.05, self.xy[1]))
        self.assertTrue(T.ZoneArea(self.cells, 100).inside(right + 0.9, self.xy[1]))
        self.assertAlmostEqual(a.signed_distance(right + 0.30, self.xy[1]), -0.05, places=6)
        self.assertGreater(a.signed_distance(*self.xy), 0.4)
        # a negative margin shrinks the area
        s = T.ZoneArea(self.cells, -20)
        self.assertTrue(s.inside(*self.xy))
        self.assertFalse(s.inside(right - 0.1, self.xy[1]))

    def test_a_walker_through_the_zone_has_one_crossing_with_exact_times(self):
        seg = PP.walk_through(self.xy, 0, 1, 1.0, 1000)                              # along +x at 1 m/s, centred on the spot
        pts = PP.track_points([seg])
        cr = T.track_crossings(pts, T.ZoneArea(self.cells, 25))
        self.assertEqual(len(cr), 1)
        c = cr[0]
        self.assertFalse(c["born_inside"] or c["dies_inside"])
        half = 0.3 + 0.25                                                            # the mask half width plus the margin (at the spot's y)
        # the spot is not the cell centre, so the crossing is within a cell of the symmetric one
        self.assertAlmostEqual(c["inside_m"], 2 * half, delta=0.21)
        self.assertGreater(c["before_m"], 1.5)
        self.assertGreater(c["after_m"], 1.5)
        mid_ms = (seg[0] + (seg[3] - seg[0]) / 2) * 1000
        self.assertLess(c["t_in"], mid_ms)
        self.assertGreater(c["t_out"], mid_ms)
        self.assertTrue(T.counted_enter(c) and T.counted_leave(c))

    def test_a_path_that_skims_the_edge_is_a_graze_not_a_counted_crossing(self):
        area = T.ZoneArea(self.cells, 25)
        c0 = W.cell_of(*self.xy)
        top = W.GY0 + (c0 // W.NX + 2) * W.CELL                                     # the upper edge of the mask (centre + 1 cell)
        # a walk along +x, 5 cm inside the area's upper edge (mask edge + 25 cm margin - 5 cm)
        y = top + 0.25 - 0.05
        graze = T.track_crossings(PP.track_points([PP.leg((self.xy[0] - 3.0, y), (self.xy[0] + 3.0, y), 1.0, 10)]), area)
        self.assertEqual(len(graze), 1)
        self.assertGreater(graze[0]["inside_m"], 0.5)                                 # long enough along the path ...
        self.assertAlmostEqual(graze[0]["depth_m"], 0.05, delta=0.02)                 # ... but never deeper than 5 cm
        self.assertFalse(T.counted_enter(graze[0]) or T.counted_leave(graze[0]))
        deep = T.track_crossings(PP.track_points([PP.leg((self.xy[0] - 3.0, self.xy[1]), (self.xy[0] + 3.0, self.xy[1]), 1.0, 10)]), area)
        self.assertGreater(deep[0]["depth_m"], 0.4)
        self.assertTrue(T.counted_enter(deep[0]) and T.counted_leave(deep[0]))

    def test_a_track_that_vanishes_inside_and_one_born_inside(self):
        area = T.ZoneArea(self.cells, 25)
        end_in = PP.track_points([PP.leg((self.xy[0] - 3.0, self.xy[1]), self.xy, 0.7, 50)])
        c = T.track_crossings(end_in, area)[0]
        self.assertTrue(c["dies_inside"] and not c["born_inside"])
        self.assertFalse(T.counted_leave(c))
        # the last waypoint is held for 0.9 s: the person is there until 0.9 s after the last second
        self.assertAlmostEqual(c["t_out"], end_in[-1][0] * 1000 + T.FINAL_HOLD_MS, delta=1)
        born = PP.track_points([PP.leg(self.xy, (self.xy[0] + 3.0, self.xy[1]), 0.7, 50)])
        b = T.track_crossings(born, area)[0]
        self.assertTrue(b["born_inside"])
        self.assertFalse(T.counted_enter(b))
        self.assertEqual(T.track_crossings(PP.track_points([PP.stay(-5.0, 6.0, 10, 20)]), area), [])

    def test_the_position_of_a_track(self):
        pts = PP.track_points([[10, 0.0, 0.0, 12, 2.0, 0.0]])
        self.assertEqual(T.track_position(pts, 11_000), (1.0, 0.0))
        self.assertEqual(T.track_position(pts, 12_900), (2.0, 0.0))                  # held 0.9 s
        self.assertIsNone(T.track_position(pts, 13_000))
        self.assertIsNone(T.track_position(pts, 9_999))

    def test_the_scripted_truth_crosses_every_person_with_the_zone(self):
        sp = PP.ScriptedPeople(job("p6edge"), 1)
        sp.day(9, random.Random(1))
        st = T.ScriptedTruth(sp.log)
        cr = st.zone_crossings(self.cells, 25)
        walkers = [c for c in cr if c["kind"] == "walker"]
        self.assertGreaterEqual(len(walkers), 20)
        self.assertTrue(any(c["spot"] == "desk" and c["speed"] == 0.7 and T.counted_enter(c) for c in walkers))
        self.assertEqual(sorted(cr, key=lambda c: (c["t_in"], c["person"])), cr)
        iv = st.zone_inside_intervals(self.cells, 25)
        self.assertEqual(len(iv), len(cr))
        t = iv[0][0] + 100
        self.assertGreaterEqual(st.inside_count(self.cells, 25, t), 1)
        self.assertEqual(st.inside_count(self.cells, 25, 9 * DAY * 1000 + 3 * 3600 * 1000), 0)
        sweep = next(p for p in sp.log if p["kind"] == "sweep" and p["offset_cm"] == 0)
        self.assertGreater(st.stayer_signed_distance(sweep, self.cells, 25), 0.4)
        far = next(p for p in sp.log if p["kind"] == "sweep" and p["offset_cm"] == 60)
        self.assertLess(st.stayer_signed_distance(far, self.cells, 25), st.stayer_signed_distance(sweep, self.cells, 25))


# ---------------------------------------------------------------- the stream additions
def stream(variant="p6edge", seed=1, horizon=10, **kw):
    j = job(variant)
    sp = PP.ScriptedPeople(j, seed) if j.programs else None
    return G.RunStream(j.cfg(), seed, horizon, j.faults, noise_m=j.noise_m, lux=j.lux, extras=j.extras(),
                       quiet_from_day=kw.pop("quiet_from_day", j.quiet_from_day), scripted=sp, gaps=j.gaps, **kw), sp


class StreamP6Test(unittest.TestCase):
    def test_defaults_leave_a_p5_stream_unchanged(self):
        j = J.parse_job("bed_w0000_0600_p5setup", nights=2)
        a = G.RunStream(j.cfg(), 1, 3, j.faults, lux=j.lux, extras=j.extras())
        b = G.RunStream(j.cfg(), 1, 3, j.faults, noise_m=j.noise_m, lux=j.lux, extras=j.extras(), quiet_from_day=None,
                        quiet_keep=(), scripted=None, gaps=())
        ea = [(r, o, type(e).__name__, getattr(e, "payload", None)) for r, o, e in a.events()]
        eb = [(r, o, type(e).__name__, getattr(e, "payload", None)) for r, o, e in b.events()]
        self.assertEqual(ea, eb)
        self.assertGreater(len(ea), 1000)
        self.assertEqual((b.dropped, b.spans), ([], []))

    def captured(self, **kw):
        j = job("p6setup")
        s = G.RunStream(j.cfg(), 1, 14, False, extras=j.extras(), **kw)
        cap = {}
        s.targets.add_tracks = lambda d, tracks: cap.setdefault(d, []).extend(tracks)
        s.world.ensure(13)
        return s, cap

    def test_quiet_days_drop_the_world_agents_but_keep_the_reflector_the_extras_and_the_exempt_agents(self):
        _, open_world = self.captured()
        s, quiet = self.captured(quiet_from_day=8, quiet_keep=("V",))
        agents = lambda cap, d: {t["agent"] for t in cap.get(d, [])}                     # noqa: E731
        self.assertIn("P1", agents(open_world, 9))
        self.assertEqual(quiet[7], open_world[7])                                       # before day 8 nothing changes
        for d in (8, 9, 10, 13):
            self.assertLessEqual(agents(quiet, d), {"refl"}, d)
        self.assertIn("V", agents(quiet, 12))                                           # the Saturday visitor is exempt
        self.assertNotIn("P1", agents(quiet, 12))
        self.assertTrue(any(t["agent"].startswith("X") for t in quiet[0]))              # the first-day extras are added on day 0
        # the LD2412 model gets no occupancy of the dropped agents either: only the extras, and V
        occ_days = {a // DAY for a, b in s.ld.occ if a // DAY >= 8}
        self.assertEqual(occ_days <= {12, 8, 9}, True)                                  # the X1 persons of days 8-9, the visitor of day 12

    def test_the_gap_windows_drop_messages_and_every_drop_is_recorded(self):
        j = J.parse_job("bed_w0000_0600_p5setup")
        gaps = ((1 * DAY + 10 * 3600, 5), (1 * DAY + 11 * 3600, 5))
        s = G.RunStream(j.cfg(), 1, 3, False, lux=False, gaps=gaps)
        n = sum(1 for _, _, ev in s.events() if isinstance(ev, FramesMessage))
        self.assertGreater(n, 100)
        self.assertGreater(len(s.dropped), 0)
        self.assertTrue(all(c == "gap" for _, c in s.dropped))
        self.assertTrue(all(any(a * 1000 <= t < (a + d) * 1000 for a, d in gaps) for t, _ in s.dropped))
        self.assertEqual(s.stats["lost_gap"], len(s.dropped))
        self.assertEqual(s.stats["messages"], s.stats["delivered"] + len(s.dropped) + s.stats["pre_base"])
        # every run of drops is closed by the next delivered message
        self.assertEqual(len(s.spans), 2)
        for a, b, cause, cnt in s.spans:
            self.assertEqual(cause, "gap")
            self.assertGreater(cnt, 0)
            self.assertLess(a, b)
            self.assertLess(b - a, 15_000)                                              # shorter than the 15 s POWER rule

    def test_the_fault_run_records_its_random_and_window_losses(self):
        j = J.parse_job("bed_w0000_0600_faults")
        s = G.RunStream(j.cfg(), 1, 13, True)
        a, b = s.timeline.loss                                                      # the 10 minute message loss of day 12
        out = []
        s.rng_fault.random = lambda: 0.5                                            # no random loss
        s._deliver(a - 5000, "{}", out)                                             # delivered before the window
        s._deliver(a + 1000, "{}", out)
        s._deliver(a + 400_000, "{}", out)
        s.rng_fault.random = lambda: 0.0                                            # a random loss
        s._deliver(b + 1000, "{}", out)
        s.rng_fault.random = lambda: 0.5
        s._deliver(b + 2000, "{}", out)                                             # delivered again
        self.assertEqual(s.dropped, [(a + 1000, "window"), (a + 400_000, "window"), (b + 1000, "random")])
        self.assertEqual(len(out), 2)
        self.assertEqual(s.stats["lost_window"] + s.stats["lost_fault"], 3)
        # the three drops are one run: closed by the message delivered after them, opened by the one before
        self.assertEqual(s.spans, [[a - 5000, b + 2000, "random+window", 3]])

    def test_a_y_extra_does_not_feed_the_ld_model_and_an_x_extra_does(self):
        j = J.parse_job("bed_w0000_0600_p5setup")
        extras = [("X1", 1.0, 2.0, 100, 200), ("Y1", 2.0, 3.0, 300, 400)]
        s = G.RunStream(j.cfg(), 1, 2, False, extras=extras, quiet_from_day=0)           # quiet: the world adds no occupancy
        s.world.ensure(0)
        self.assertTrue(s.ld.occupied(150.0))
        self.assertFalse(s.ld.occupied(350.0))


# ---------------------------------------------------------------- the driver on a stub learner
class ZoneStub(StubFactory):
    """The stub learner of the P5 tests that also answers the P6 accessors and makes events and states on demand."""

    def __init__(self, params, tm, state=None):
        super().__init__(params, tm, state)
        self.zone_list = ()
        self.dbg = ()
        self.out_next = []

    def zones(self):
        return self.zone_list

    def zone_debug(self):
        return self.dbg

    def zone_states(self):
        return ZoneStates(True, 0, ())

    def handle(self, ev):
        out = super().handle(ev)
        out += self.out_next
        self.out_next = []
        return out

    def counters(self):
        return {"zone6_pruned": 0, "frames": 1}


def zview(zid="z0001", kind="dwell", cells=None, created_s=BASE_MS // 1000 + 3600):
    cells = block(W.SPOT["desk"]) if cells is None else cells
    return SimpleNamespace(id=zid, name="Desk", kind=kind, src="approve", ref="s0001-1", rev=0, cx_m=2.5, cy_m=2.2, cells=cells,
                           poly_cm=((250, 220),), created_s=created_s, night=7, frozen=False)


class DriverP6Test(unittest.TestCase):
    def test_the_factory_builds_the_learner_with_the_p6_parameters(self):
        f = D.LearnerFactory.for_job(job("p6hold"))
        self.assertEqual(f.p6params(), DEFAULT_P6)
        g = pickle.loads(pickle.dumps(f))
        L = g(__import__("radar_zone_learner.core.params", fromlist=["DEFAULT_PARAMS"]).DEFAULT_PARAMS, FixedOffsetTime(0))
        self.assertIsInstance(L.p6params, P6Params)

    def test_a_p6_run_ticks_every_second_and_carries_the_p6_section_a_p5_run_does_not(self):
        r = D.Run(job("p6hold", nights=1), 1, learner_factory=ZoneStub)
        self.assertEqual(r.tick_ms, 1000)
        self.assertIsNotNone(r.p6)
        self.assertIsNotNone(r.p5)                                           # the P5 recorder is also made for a P6 job
        self.assertEqual(r.run(), "ok")
        self.assertGreater(r.counts["tick"], 86_000)                          # one a second
        res = r.result()
        self.assertEqual(sorted(res["p6"]), sorted(["event_columns", "events", "state_row", "states", "timeline", "dropped", "spans",
                                                    "people", "t_end_ms", "zones", "enters", "enters_without_debug"]))
        self.assertIn("p6", res["nights"][0])
        self.assertEqual(tuple(res["job_spec"]["spells"][0]), ("desk", 150))
        self.assertEqual(res["job_spec"]["noise_m"], 0.02)
        p5 = D.Run(J.parse_job("bed_w0000_0600_p5ans", nights=1), 1, learner_factory=ZoneStub)
        self.assertEqual(p5.tick_ms, 10_000)
        self.assertIsNone(p5.p6)
        p5.run()
        self.assertNotIn("p6", p5.result())
        self.assertNotIn("p6", p5.result()["nights"][0])

    def test_scripted_people_and_noise_reach_the_stream(self):
        r = D.Run(job("p6edge08", nights=1), 1, learner_factory=ZoneStub)
        self.assertEqual(r.stream.targets.noise_m, 0.08)
        self.assertIs(r.stream.scripted, r.scripted)
        self.assertEqual(r.stream.quiet_from_day, 8)
        r5 = D.Run(J.parse_job("bed_w0000_0600_p5setup", nights=1), 1, learner_factory=ZoneStub)
        self.assertEqual((r5.stream.targets.noise_m, r5.stream.quiet_from_day, r5.stream.scripted, r5.stream.gap_windows),
                         (0.02, None, None, []))
        guests = D.Run(job("p6guests", nights=1), 1, learner_factory=ZoneStub)
        self.assertEqual(guests.stream.quiet_keep, ("G", "V"))

    def test_the_recorder_stamps_the_clocks_and_keeps_changed_rows_only(self):
        r = D.Run(job("p6hold", nights=1), 1, learner_factory=ZoneStub)
        rec = r.p6
        r.now = BASE_MS + 5_000
        rec.note_batch(BASE_MS + 4_990)
        rec.ctx = "b"
        L = r.learner
        L.dbg = (("z0001", 7, 1000, 1200, 2),)
        rec.on_output(ZoneEvent("z0001", "enter", "", 5_000, 7, 0, "wd0818"), r.now)
        rec.ctx = "t"
        rec.on_output(ZoneEvent("z0001", "leave", "hold_expired", 9_000, 7, 4, ""), r.now + 1000)
        self.assertEqual(rec.events[0], [5_000, "b", 4_990, G.BOOT_SIM_MS, "z0001", "enter", "", 5_000, 7, 0, "wd0818", 1000, 2])
        self.assertEqual(rec.events[1][1:3], ["t", None])                      # a tick: no batch
        self.assertEqual(rec.events[1][11:], [None, None])
        z = ZoneState("z0001", True, 1, False, "", False, 300, "default", 25)
        rec.ctx = "b"
        rec.on_output(ZoneStates(True, 5_000, (z,)), r.now)
        rec.on_output(ZoneStates(True, 5_100, (z,)), r.now + 100)                 # nothing changed: no new record
        z2 = ZoneState("z0001", True, 1, False, "", True, 300, "default", 25)       # only ldr changed: a row
        rec.on_output(ZoneStates(True, 5_200, (z2,)), r.now + 200)
        rec.on_output(ZoneStates(False, 5_300, (z2,)), r.now + 300)                # valid flipped: a record without rows
        self.assertEqual(len(rec.states), 3)
        self.assertEqual(rec.states[0][5], [["z0001", True, 1, False, "", False, 300, "default", 25]])
        self.assertEqual(rec.states[2][3:], [False, 5_300, []])
        self.assertEqual(rec.enters_seen, 1)

    def test_the_epoch_of_a_batch_follows_the_reboot_of_the_fault_run(self):
        j = J.parse_job("bed_w0000_0600_p6pow", nights=1)
        r = D.Run(j, 1, learner_factory=ZoneStub, horizon_days=14)
        tl = r.stream.timeline
        self.assertEqual(len(tl.epochs), 2)
        rec = r.p6
        before = BASE_MS + tl.reboot - 500
        after = BASE_MS + tl.epochs[1].start + 5_000
        self.assertEqual(rec.epoch_boot(before), tl.epochs[0].boot)
        self.assertEqual(rec.epoch_boot(after), tl.epochs[1].boot)
        t = rec.timeline()
        self.assertEqual(t["reboot"], tl.reboot)
        self.assertEqual(len(t["epochs"]), 2)
        self.assertEqual(t["loss"], list(tl.loss))
        for a, b in t["on"]:
            self.assertFalse(tl.loss[0] <= a < tl.loss[1])                         # nothing observes through the loss window
        self.assertTrue(all(b > a for a, b in t["on"]))

    def test_the_night_record_holds_the_zones_the_state_and_zone6(self):
        class NL(ZoneStub):
            def zone_states(self):
                return ZoneStates(True, 0, (ZoneState("z0001", False, 0, False, "wd0818", False, 300, "default", 25, ("wd0818",), ()),))

            def export_state(self):
                st = super().export_state()
                st["p5"] = {"zone6": {"v": 1, "delay": {"z0001": {"sp": [3, 4], "upd": 8}}, "ld": {"z0001": [10, 12]}, "ldr": [],
                                      "x": 1}}
                return st
        r = D.Run(job("p6hold", nights=1), 1, learner_factory=NL)
        r.learner.zone_list = (zview(),)
        n = r.p6.night(r, {"night": 8})
        self.assertEqual(n["zones"][0]["id"], "z0001")
        self.assertEqual(n["zones"][0]["n_cells"], 9)
        self.assertEqual(n["zone_states"]["rows"], [["z0001", False, 0, False, "wd0818", False, 300, "default", 25]])
        self.assertEqual(n["zone_states"]["variants"], {"z0001": ["wd0818"]})
        self.assertEqual(sorted(n["zone6"]), ["delay", "ld", "ldr", "v"])           # only the keys the checks read
        self.assertEqual(n["counters"], {"zone6_pruned": 0})
        # a stub without any P6 accessor still gives a record
        plain = D.Run(job("p6hold", nights=1), 1, learner_factory=StubFactory)
        n2 = plain.p6.night(plain, {"night": 8})
        self.assertEqual(n2["zones"], [])
        self.assertNotIn("zone_states", n2)


# ---------------------------------------------------------------- the actors
class ActorsP6Test(unittest.TestCase):
    def fake_run(self, observing=True):
        tl = SimpleNamespace(observing=lambda t: observing, offs=[])
        return SimpleNamespace(stream=SimpleNamespace(timeline=tl), now=BASE_MS, job=job("p6hold"),
                               learner=ZoneStub(None, FixedOffsetTime(0)))

    def test_make_actors_gives_the_p5_approval_actor_the_probe_and_the_job_script(self):
        names = {v: [a.name for a in P6.make_actors(job(v))] for v in J.P6_VARIANTS}
        for v, n in names.items():
            self.assertEqual(n[:2], ["setup_user", "zone_probe"], v)
        self.assertEqual(names["p6hold"][2:], ["hold_script"])
        self.assertEqual(names["p6ld"][2:], ["hold_script"])
        self.assertEqual(names["p6pow"][2:], ["power_script"])
        self.assertEqual(names["p6door"][2:], ["door_script"])
        self.assertEqual(names["p6ldghost"][2:], ["ld_script"])
        self.assertEqual(names["p6guests"][2:], ["guests_user"])
        self.assertEqual(names["p6guestsc"][2:], ["guests_user"])
        self.assertEqual(names["p6edge"][2:], [])
        self.assertEqual(P4.make_actors(job("p6door"))[1].name, "zone_probe")           # the p4 router hands p6 scripts on
        gu = {v: P6.make_actors(job(v))[2] for v in ("p6guests", "p6guestsc")}
        self.assertTrue(gu["p6guests"].pause)
        self.assertFalse(gu["p6guestsc"].pause)

    def test_a_press_script_names_the_spot_presses_until_it_succeeds_and_stops(self):
        a = P6.DoorScript()
        run = self.fake_run()
        a.attach(run)
        fed = []
        t0 = 8 * DAY * 1000 + 10 * 3600 * 1000
        self.assertEqual(a.next_time(), t0 + 60_000)
        a.act(fed.append)                                                      # the name and the first press
        self.assertIsInstance(fed[0], SpotName)
        self.assertEqual(fed[0].text, "Door spot")
        self.assertIsInstance(fed[1], MarkPress)
        a.act(fed.append)
        self.assertEqual(sum(isinstance(e, SpotName) for e in fed), 1)         # named once
        self.assertEqual(a.next_time(), t0 + 120_000)
        a.pending = True
        a.on_output(MarkResult(False, "several_stays", None, None))
        a.on_output(MarkResult(True, None, "l0002", "manual"))
        self.assertTrue(a.done)
        self.assertEqual(a.summary()["results"], {"several_stays": 1, "ok": 1})
        self.assertEqual(a.summary()["mark"]["label"], "l0002")
        self.assertEqual(a.next_time(), P4.INF)

    def test_a_press_is_not_made_while_the_radars_do_not_observe(self):
        a = P6.LdScript()
        a.attach(self.fake_run(observing=False))
        fed = []
        a.act(fed.append)
        self.assertEqual(fed, [])
        self.assertEqual(a.attempts, 0)

    def test_the_zone_probe_says_which_spot_each_zone_covers(self):
        a = P6.ZoneProbe()
        run = self.fake_run()
        run.learner.zone_list = (zview("z0001"), zview("z0002", "short_task", block(W.SPOT["counter"])))
        a.attach(run)
        a.on_night({"night": 8})
        s = a.summary()
        self.assertEqual(s["first_zone_night"], 8)
        self.assertEqual(s["last"]["spots"], {"desk": ["z0001"], "counter": ["z0002"]})


# ---------------------------------------------------------------- the scripts
class ScriptsP6Test(unittest.TestCase):
    def test_compare_runs_ignores_the_zone_counters_and_nothing_else(self):
        def dump(counters):
            return {"result": {"labels": {}, "counters": dict(counters), "status": "ok",
                               "nights": [{"night": 1, "suggestions": [], "candidates": 0, "labels": [], "counters": dict(counters)}]},
                    "final_state": {"suggestions": [], "zones": []}}
        base = {"frames": 5}
        self.assertEqual(CR.compare_run(dump(base), dump(dict(base, zone6_pruned=0, zones_skipped_kind=2))), [])
        self.assertTrue(CR.compare_run(dump(base), dump({"frames": 6})))
        self.assertTrue(CR.compare_run(dump(base), dump(dict(base, other=1))))           # a new key that is not a zone counter
        self.assertEqual(CR.compare_run(dump(dict(base, zones_x=1)), dump(base)), [])      # the prefix rule is symmetric
        # zone6 sits inside p5, which is exempt
        a, b = dump(base), dump(base)
        b["final_state"]["p5"] = {"zone6": {"v": 1}}
        self.assertEqual(CR.compare_run(a, b), [])

    def test_dump_run_has_a_slim_option(self):
        from replay import dump_run
        with tmpdir() as t, contextlib.redirect_stdout(io.StringIO()):
            rc = dump_run.main(["--tree", str(ADDON), "--jobs", "bed_w0000_0600_p6hold", "--seeds", "1", "--nights", "1", "--slim",
                                "--out", t])
            self.assertEqual(rc, 0)
            d = json.loads(next(Path(t).glob("*_s1.json")).read_text(encoding="utf-8"))
            self.assertNotIn("p6", d["result"])
            self.assertIn("final_state", d)

    def test_compat_check_p6_checks(self):
        res = CC.check_corrupt_zone6()
        self.assertEqual(res["notes"]["variants"], len(CC.CORRUPT_ZONE6))
        self.assertEqual(res["status"], "PASS", res["problems"])
        orphans = CC.check_orphans()
        self.assertEqual(orphans["status"], "PASS", orphans)
        st = RP.heavy_state()                                                                 # a 0.5.0-shaped state with 100 zones
        res5 = CC.check_p5_state_in_p6(st, days=1, tick_ms=600_000)
        self.assertEqual(res5["status"], "PASS", res5)
        self.assertEqual(res5["notes"]["renamed"], True)
        self.assertNotIn("zone6", st["p5"])
        ok = CC.check_p6_state_in_baseline({"p5": {"zone6": {"v": 1}}, "suggestions": [], "labels": [], "zones": []},
                                           {"loaded": True, "suggestions": [], "labels": [], "zones": [], "p5_zone6": {"v": 1},
                                            "p5_kept": True})
        self.assertEqual(ok["status"], "PASS")
        lost = CC.check_p6_state_in_baseline({"p5": {"zone6": {"v": 1}}, "suggestions": [], "labels": [], "zones": []},
                                             {"loaded": True, "suggestions": [], "labels": [], "zones": [], "p5_zone6": None,
                                              "p5_kept": True})
        self.assertEqual(lost["status"], "FAIL")
        none = CC.check_p6_state_in_baseline({"p5": {}, "suggestions": [], "labels": [], "zones": []},
                                             {"loaded": True, "suggestions": [], "labels": [], "zones": [], "p5_kept": True})
        self.assertEqual(none["status"], "INCOMPLETE")
        refused = CC.check_p6_state_in_baseline({"suggestions": [], "labels": [], "zones": []}, {"loaded": False, "error": "x"})
        self.assertEqual(refused["status"], "FAIL")

    def test_the_load_only_child_returns_the_zone6_of_the_p5_extra(self):
        with tmpdir() as t:
            from radar_zone_learner.core.learner import Learner
            st = Learner().export_state()
            p = Path(t) / "s.json"
            p.write_text(json.dumps(st), encoding="utf-8")
            out = CC.load_only(ADDON, p)
            self.assertTrue(out["loaded"])
            self.assertIn("p5_zone6", out)
            self.assertIsNone(out["p5_zone6"])

    def test_the_probe_state_and_batches(self):
        st = RP.heavy_state()
        z6 = RP.zone6_state(st)
        self.assertEqual((len(z6["delay"]), len(z6["tune"]), len(z6["ve"])), (200, 24, 24))
        self.assertEqual(len(z6["delay"]["z0001"]["sp"]), 50)
        self.assertTrue(all(5 <= x <= 86400 for d in z6["delay"].values() for x in d["sp"]))
        st["p5"]["zone6"] = z6
        from radar_zone_learner.core.learner import Learner
        L = Learner(DEFAULT_PARAMS_, FixedOffsetTime(0), json.loads(json.dumps(st)))
        self.assertEqual(len(L.zones()), 200)
        self.assertEqual(len((L.export_state()["p5"].get("zone6") or {}).get("delay", {})), 200)
        b = RP.synthetic_batches(3, 5)
        from radar_zone_learner.core.frames import parse_frames
        batch = parse_frames(b[0][1].encode("ascii"))
        self.assertEqual((batch.n, len(batch.frames)), (5, 5))
        self.assertEqual(len(batch.frames[0].targets), 3)
        self.assertEqual(batch.pw, 3)
        self.assertEqual(RP.P6_LIMITS["evaluator_us_per_detection"], 8.3)


DEFAULT_PARAMS_ = __import__("radar_zone_learner.core.params", fromlist=["DEFAULT_PARAMS"]).DEFAULT_PARAMS


# ---------------------------------------------------------------- the guards of the P4 evaluator and the real core
class GuardsTest(unittest.TestCase):
    def test_a_p6_run_is_not_a_p4_scenario(self):
        res = [{"job": "bed_w0000_0600_p6hold", "seed": 1, "job_spec": {"script": "p6hold"}, "status": "ok", "nights": []}]
        self.assertIsNone(E4.evaluate_p4(res))


class RealCoreP6Test(unittest.TestCase):
    """One short run of a P6 job on the real core: the scripted day-8 program of the edge job starts after night 7, so two days
    give no zone; what is checked is that the whole path (job, stream, driver, recorder, result) works end to end."""

    @classmethod
    def setUpClass(cls):
        r = D.Run(job("p6edge", nights=2), 1)
        r.run()
        cls.run_ = r
        cls.res = r.result()

    def test_the_run_is_clean_and_records_the_timeline_and_the_night_records(self):
        self.assertEqual((self.res["status"], self.res["core_errors"], self.res["first_error"]), ("ok", 0, None))
        p6 = self.res["p6"]
        self.assertGreater(len(p6["timeline"]["on"]), 1)
        self.assertGreaterEqual(len(self.res["nights"]), 2)
        self.assertIn("p6", self.res["nights"][0])
        self.assertEqual(p6["people"], [])                                         # no scripted person before day 8
        self.assertEqual(p6["events"], [])                                         # and no zone yet

    def test_the_evaluator_reads_such_a_run_without_error_and_says_incomplete(self):
        from replay import evaluate_p6 as E6
        rep = E6.evaluate_p6([self.res])
        self.assertEqual(rep["runs"], 1)
        self.assertEqual(len(rep["criteria"]), 13)
        by = {c["id"]: c["status"] for c in rep["criteria"]}
        self.assertEqual(by["P6-1"], "INCOMPLETE")
        self.assertNotEqual(rep["gate"], "PASS")


if __name__ == "__main__":
    unittest.main()
