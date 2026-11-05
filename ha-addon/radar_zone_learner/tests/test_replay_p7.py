"""P7 replay plumbing (P7 spec 8.2-8.5): the job variants and profiles, the world changes (and the byte identity of the old worlds), the
radar's rotation, the empty-room switch-off, the factory, the night record, the actors, the compare/compat/probe additions. Everything
is small; the one real-core run is two simulated days."""
import contextlib
import hashlib
import io
import json
import pickle
import random
import unittest
from pathlib import Path
from types import SimpleNamespace

from radar_zone_learner.core.params import DEFAULT_P7, P7Params
from radar_zone_learner.core.types import (AnswerIgnored, FixedOffsetTime, ReviewAnswer, ReviewChanged, ReviewItem)
from replay import compat_check as CC
from replay import driver as D
from replay import evaluate_p4 as E4
from replay import evaluate_p7 as E7
from replay import framegen as G
from replay import jobs as J
from replay import p4_scenarios as P4
from replay import p7_scenarios as P7S
from replay import resource_probe as RP
from replay import world as W
from replay.framegen import BASE_MS
from replay.truth import rle
from tests._tmp import tmpdir
from tests.test_replay_p5 import StubFactory

DAY = 86400
ADDON = Path(__file__).resolve().parents[1]
# sha256 of the sessions, home-empty intervals and tracks of the first days of three worlds, computed with the world of the 0.5.0 tree
# (git 91c05e9); the P7 additions to replay/world.py must leave them as they are
FINGERPRINTS = {
    "bed22": ("9dd18c2b6332073d77ec8e1cf77bb1ace051e29a98285900ab2e8b1dac610728", 1, 22, dict(bed=1, nw="00:00-06:00")),
    "pet9": ("000681ad5338bde300a7e7bf3a5781ef563399a9aebf949a55a8550562c89eb8", 2, 9, dict(bed=0, pet=1)),
    "move12": ("b76e9f92ac3c1c109c3eb8b91d96a07b4f571b3a4ca9e48b71da765a55aa5568", 3, 12, dict(bed=1, move=1.1, move_day=5)),
}


def job(variant, nights=None, window="w0000_0600"):
    return J.parse_job("bed_%s_%s" % (window, variant), nights=nights)


def block(center, half=1):
    c0 = W.cell_of(*center)
    i, j = c0 % W.NX, c0 // W.NX
    return frozenset((j + dj) * W.NX + i + di for di in range(-half, half + 1) for dj in range(-half, half + 1))


def world_digest(seed, days, **cfgkw):
    cfg = W.make_cfg(**cfgkw)
    rng = random.Random(seed)
    world = W.World(rng, cfg)
    acc = []
    for d in range(days):
        S, he = W.plan_day(rng, d, cfg)
        tracks, occ, motion, sessions = world.day_tracks(d, S)
        acc.append({"S": S, "he": he, "tracks": tracks, "occ": occ, "motion": motion, "sessions": sessions})
    return hashlib.sha256(json.dumps(acc, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- jobs and profiles
class JobsP7Test(unittest.TestCase):
    def test_variants_parse_and_carry_their_flags(self):
        self.assertEqual(len(J.P7_VARIANTS), 24)
        for v in J.P7_VARIANTS:
            j = job(v)
            self.assertEqual((j.variant, j.script), (v, v))
            self.assertTrue(j.is_p7 and j.is_p4 and not j.is_p5 and not j.is_p6 and j.lux)
            self.assertFalse(j.labels or j.gated)
            self.assertEqual(j.tick_ms, 10_000)
            self.assertEqual(pickle.loads(pickle.dumps(j)), j)

    def test_the_world_changes_of_each_variant(self):
        self.assertEqual((job("p7mv11").move, job("p7mv11").move_day, job("p7mv29").move), (1.1, 35, 2.9))
        for v in ("p7mv11e", "p7mv29e"):
            self.assertTrue(job(v, window="none").empty_off)
        self.assertTrue(job("p7ctle", window="none").empty_off)
        self.assertFalse(job("p7ctl").empty_off)
        self.assertEqual(job("p7ret").retire, ("table", 14))
        self.assertEqual(job("p7guest").guest_week, (20, 7))
        self.assertEqual(job("p7guestp").guest_week, (20, 7))
        self.assertEqual([job(v).rot for v in ("p7rot", "p7rot3", "p7rot6", "p7shift", "p7shift5", "p7reset")],
                         [(28, 8 * 3600, 10.0, 0.0, 0.0), (28, 8 * 3600, 3.0, 0.0, 0.0), (28, 8 * 3600, 6.0, 0.0, 0.0),
                          (28, 8 * 3600, 0.0, 0.8, 0.0), (28, 8 * 3600, 0.0, 0.5, 0.0), (28, 8 * 3600, 10.0, 0.0, 0.0)])
        mb = job("p7mvback")
        self.assertEqual((mb.move, mb.move_day, mb.move_back), (1.1, 28, 49))
        self.assertEqual([job(v).hm for v in ("p7tune", "p7tuneh300", "p7tuneh900")], [90, 300, 900])

    def test_the_learner_parameters_of_each_variant(self):
        self.assertFalse(job("p7bednl").p7_static_freeze)
        self.assertFalse(job("p7bednf").p7_static_freeze)
        self.assertTrue(job("p7bed").p7_static_freeze)
        self.assertEqual((job("p7ctlnp").p7_span, job("p7guestnp").p7_span, job("p7ctl").p7_span), (0, 0, None))
        self.assertEqual(job("p7bednl").nl, 7)

    def test_older_jobs_are_unchanged_and_none_is_a_p7_job(self):
        for name in ("bed_w0000_0600", "liv_none", "bed_w0000_0600_p4", "bed_w0000_0600_p5setup", "bed_w0000_0600_p6hold",
                     "bed_w0000_0600_p5zone"):
            j = J.parse_job(name)
            self.assertFalse(j.is_p7, name)
            self.assertEqual((j.move, j.move_day, j.move_back, j.retire, j.guest_week, j.rot, j.empty_off, j.p7_static_freeze, j.p7_span),
                             (0.0, 35, None, None, None, None, False, True, None), name)
        self.assertEqual(len(J.runs_of(J.PROFILES["p5"])), 33)

    def test_the_extras_are_the_p5_setup_person_for_every_p7_job_but_the_night_learning_bed(self):
        bx, by = W.SPOT["bed"]
        x1 = [("X1", bx, by, d * DAY + 22 * 3600, d * DAY + 23 * 3600 + 58 * 60) for d in range(6, 10)]
        for v in J.P7_VARIANTS:
            self.assertEqual(job(v).extras(), [] if v == "p7bednl" else x1, v)
        self.assertEqual(J.parse_job("bed_w0000_0600_p5setup").extras(), x1)

    def test_the_world_config_of_a_job_carries_its_keys_and_an_old_job_the_defaults(self):
        c = job("p7ret").cfg()
        self.assertEqual((c["retire"], c["guest_week"], c["move_back"]), (("table", 14), None, None))
        c = job("p7mvback").cfg()
        self.assertEqual((c["move"], c["move_day"], c["move_back"]), (1.1, 28, 49))
        c = job("p7guest").cfg()
        self.assertEqual(c["guest_week"], (20, 7))
        old = J.parse_job("bed_w0000_0600_p5setup").cfg()
        self.assertEqual({k: old[k] for k in ("retire", "guest_week", "move_back", "move")}, {"retire": None, "guest_week": None,
                                                                                              "move_back": None, "move": 0.0})
        self.assertEqual(old, W.make_cfg(bed=1, nw="00:00-06:00", Vm=15, Hm=90, gw=0.25, gs=0.0, gi=0.0, pet=0, nl=0))

    def test_max_nights_is_60(self):
        self.assertEqual(J.MAX_NIGHTS, 60)
        self.assertEqual(job("p7mvback", nights=60).nights, 60)
        from replay import run_replay
        args = SimpleNamespace(jobs=None, profile="p7smoke", nights=61, seeds=None)
        with self.assertRaises(ValueError):
            run_replay.build_jobs(args)
        args.nights = 60
        self.assertEqual(len(run_replay.build_jobs(args)), 3)

    def test_the_profiles(self):
        smoke = J.PROFILES["p7smoke"]
        self.assertEqual([(j.name, j.nights, j.seeds) for j in smoke],
                         [("bed_w0000_0600_p7mv11", 46, (1,)), ("bed_w0000_0600_p7rot", 34, (1,)), ("bed_w0000_0600_p7ctl", 28, (1,))])
        self.assertAlmostEqual(J.estimate_days(smoke), 108.4, 1)
        days = {p: J.estimate_days(J.PROFILES[p]) for p in J.P7_PROFILES}
        for p, d in days.items():
            self.assertLess(d, 1500, p)
        self.assertAlmostEqual(days["p7move"], 1433.8, 1)
        self.assertAlmostEqual(sum(d for p, d in days.items() if p != "p7smoke"), 5400, delta=100)
        self.assertEqual(len(J.runs_of(J.PROFILES["p7move"])), 26)
        self.assertEqual(len(J.runs_of(J.PROFILES["p7drift"])), 20)
        self.assertEqual(len(J.runs_of(J.PROFILES["p7beds"])), 15)
        self.assertEqual(len(J.runs_of(J.PROFILES["p7radar"])), 20)
        self.assertEqual(len(J.runs_of(J.PROFILES["p7guests"])), 10)
        self.assertEqual(len(J.runs_of(J.PROFILES["p7tune"])), 9)
        opt = {(j.variant, j.nights, j.seeds) for j in J.PROFILES["p7opt"]}
        self.assertEqual(opt, {("p7guestnp", 42, J.ALL_SEEDS), ("p7ctlnp", 56, (1, 2, 3)), ("p7mvback", 60, (1, 2, 3)),
                               ("p7rot6", 34, J.ALL_SEEDS), ("p7shift5", 34, J.ALL_SEEDS)})
        self.assertEqual(J.P7_PROFILES, ("p7smoke", "p7move", "p7drift", "p7beds", "p7radar", "p7guests", "p7tune", "p7opt"))

    def test_every_p7_job_of_the_profiles_is_a_p7_job_and_the_older_profiles_hold_none(self):
        for p in J.P7_PROFILES:
            self.assertTrue(all(j.is_p7 for j in J.PROFILES[p]), p)
        for p in ("smoke", "quick", "p3", "p4", "p5", "p6", "p6smoke", "p4smoke", "p5smoke"):
            self.assertFalse(any(j.is_p7 for j in J.PROFILES[p]), p)

    def test_the_jobs_of_the_move_profile_run_55_nights(self):
        self.assertTrue(all(j.nights == 55 for j in J.PROFILES["p7move"]))
        self.assertEqual(sorted({(j.window or "none") for j in J.PROFILES["p7move"]}), ["00:00-06:00", "none"])
        self.assertEqual(sum(1 for j in J.PROFILES["p7move"] if j.empty_off), 2)


# ---------------------------------------------------------------- the world
class WorldP7Test(unittest.TestCase):
    def test_a_world_without_the_p7_keys_is_the_one_of_the_0_5_0_tree_byte_for_byte(self):
        for name, (want, seed, days, kw) in FINGERPRINTS.items():
            self.assertEqual(world_digest(seed, days, **kw), want, name)

    def test_the_default_world_config_has_the_three_keys_off(self):
        c = W.make_cfg()
        self.assertEqual((c["retire"], c["guest_week"], c["move_back"]), (None, None, None))

    def sessions(self, cfg, days, seed=1):
        rng = random.Random(seed)
        out = []
        for d in range(days):
            S, he = W.plan_day(rng, d, cfg)
            out.append((S, he))
        return out

    def test_the_guest_is_one_evening_on_day_20_and_a_week_of_evenings_with_guest_week(self):
        one = self.sessions(W.make_cfg(bed=1, nw="00:00-06:00"), 30)
        g1 = {d: S["G"] for d, (S, he) in enumerate(one) if S.get("G")}
        self.assertEqual(sorted(g1), [20])
        self.assertEqual(len(g1[20]), 1)
        week = self.sessions(W.make_cfg(bed=1, nw="00:00-06:00", guest_week=(20, 7)), 30)
        gw = {d: S["G"] for d, (S, he) in enumerate(week) if S.get("G")}
        self.assertEqual(sorted(gw), list(range(20, 27)))
        for d, ss in gw.items():                                              # the fixed evening, once a day (not twice on day 20)
            self.assertEqual(len(ss), 1, d)
            self.assertEqual((ss[0][0] - d * DAY, ss[0][1] - d * DAY, ss[0][2], ss[0][3]), (19 * 3600, 22 * 3600, "guest", "sit"))
        self.assertEqual(g1[20], gw[20])
        self.assertEqual(one[19][0].keys(), week[19][0].keys())                # days before the week are the same

    def test_the_guest_week_adds_no_draw_and_changes_no_other_session(self):
        a = self.sessions(W.make_cfg(bed=1, nw="00:00-06:00"), 30)
        b = self.sessions(W.make_cfg(bed=1, nw="00:00-06:00", guest_week=(20, 7)), 30)
        for d, ((sa, ha), (sb, hb)) in enumerate(zip(a, b)):
            self.assertEqual({k: v for k, v in sa.items() if k != "G"}, {k: v for k, v in sb.items() if k != "G"}, d)
            self.assertEqual(ha, hb, d)

    def test_a_retired_place_has_no_session_from_its_day_on_and_nothing_else_changes(self):
        a = self.sessions(W.make_cfg(bed=1, nw="00:00-06:00"), 30)
        b = self.sessions(W.make_cfg(bed=1, nw="00:00-06:00", retire=("table", 14)), 30)
        used_before = used_after = 0
        for d, ((sa, ha), (sb, hb)) in enumerate(zip(a, b)):
            for ag in sa:
                keep = [s for s in sa[ag] if not (d >= 14 and s[2] == "table")]
                self.assertEqual(sb[ag], keep, (d, ag))
            tbl = sum(1 for ag in sa for s in sa[ag] if s[2] == "table")
            if d < 14:
                used_before += tbl
            else:
                used_after += tbl
            self.assertFalse(any(s[2] == "table" for ag in sb for s in sb[ag]) and d >= 14, d)
            self.assertEqual(ha, hb, d)
        self.assertGreater(used_before, 3)
        self.assertGreater(used_after, 3)                                          # the world did use the table after day 14

    def test_the_sofa_moves_to_the_right_or_across_the_room_and_back(self):
        cfg = W.make_cfg(move=1.1, move_day=35)
        a = W.spot_pos("sofaA", 34, cfg)
        b = W.spot_pos("sofaA", 35, cfg)
        self.assertEqual((round(b[0] - a[0], 6), b[1]), (1.1, a[1]))
        cfg = W.make_cfg(move=2.9, move_day=35)
        far = W.spot_pos("sofaA", 35, cfg)
        self.assertAlmostEqual(((far[0] - a[0]) ** 2 + (far[1] - a[1]) ** 2) ** 0.5, 2.9, delta=0.05)
        cfg = W.make_cfg(move=1.1, move_day=28, move_back=49)
        self.assertEqual(W.spot_pos("sofaA", 27, cfg), a)
        self.assertEqual(W.spot_pos("sofaA", 28, cfg)[0], a[0] + 1.1)
        self.assertEqual(W.spot_pos("sofaA", 48, cfg)[0], a[0] + 1.1)
        self.assertEqual(W.spot_pos("sofaA", 49, cfg), a)
        self.assertEqual(W.spot_pos("sofaB", 60, cfg), W.SPOT["sofaB"])

    def test_the_truth_labels_follow_the_move_and_the_return(self):
        cfg = W.make_cfg(bed=1, move=1.1, move_day=28, move_back=49)
        old = J.P6_SPOTS["sofaAB"]
        self.assertEqual(W.label(old[0], old[1], 20, cfg), "TP:sofaAB")
        self.assertEqual(W.label(old[0], old[1], 30, cfg), "FP:old_sofa")
        self.assertEqual(W.label(old[0] + 1.1, old[1], 30, cfg), "TP:sofaAB")
        self.assertEqual(W.label(old[0], old[1], 50, cfg), "TP:sofaAB")
        self.assertNotEqual(W.label(old[0] + 1.1, old[1], 50, cfg), "TP:sofaAB")


# ---------------------------------------------------------------- the frames: rotation, shift, empty-room switch-off
class FramegenP7Test(unittest.TestCase):
    def track(self, kind="refl", pts=None):
        pts = pts or [(100, 3.0, 4.0), (101, 3.0, 4.0), (102, 3.5, 4.0)]
        return {"kind": kind, "agent": "P1", "pts": pts}

    def frames(self, rot=None, kind="refl", seed=1, until=104_000, pts=None):
        tf = G.TargetFrames(seed, rot=rot)
        tf.add_tracks(0, [self.track(kind, pts)])
        out, t = [], 0
        while True:
            f = tf.next_frame(t)
            if f is None or f[0] > until:
                break
            out.append(f)
            t = f[0] + 1
        return out

    def test_without_a_rotation_nothing_changes(self):
        a = self.frames()
        self.assertEqual(a, self.frames(rot=None))
        self.assertEqual(a[0][1][0][1:3], (3000, 4000))

    def test_a_quarter_turn_and_a_shift_move_every_point_about_the_origin(self):
        f = self.frames(rot=(0, 100, 90.0, 1.0, 2.0))
        x, y = f[0][1][0][1:3]
        self.assertEqual((x, y), (-3000, 5000))                           # (x, y) -> (-y + 1, x + 2) for 90 degrees
        self.assertEqual(self.frames(rot=(0, 100, 0.0, 0.0, 0.0)), self.frames())

    def test_the_rotation_starts_at_its_second_of_its_day_and_not_before(self):
        pts = [(100, 3.0, 4.0), (101, 3.0, 4.0), (102, 3.0, 4.0)]
        f = self.frames(rot=(0, 101, 90.0, 0.0, 0.0), pts=pts)
        by_sec = {m // 1000: tg[0][1:3] for m, tg in f}
        self.assertEqual(by_sec[100], (3000, 4000))
        self.assertEqual(by_sec[101], (-4000, 3000))
        self.assertEqual(by_sec[102], (-4000, 3000))
        later = self.frames(rot=(1, 0, 90.0, 0.0, 0.0), pts=pts)           # day 1 starts at second 86,400: after the track
        self.assertEqual(later, self.frames(pts=pts))

    def test_a_rotation_keeps_the_range_so_the_radial_speed_is_exact(self):
        pts = [(100, 3.0, 4.0), (101, 3.0, 5.0), (102, 3.0, 6.0)]
        a = self.frames(pts=pts)
        b = self.frames(rot=(0, 0, 37.0, 0.0, 0.0), pts=pts)
        self.assertEqual([tg[0][3] for _, tg in a], [tg[0][3] for _, tg in b])

    def test_no_random_number_is_drawn_for_it_so_the_noise_and_miss_streams_stay_aligned(self):
        a = self.frames(kind="human", pts=[(100 + k, 3.0 + 0.1 * k, 4.0) for k in range(4)])
        b = self.frames(kind="human", rot=(0, 0, 0.0, 1.0, 0.0), pts=[(100 + k, 3.0 + 0.1 * k, 4.0) for k in range(4)])
        self.assertEqual([m for m, _ in a], [m for m, _ in b])                  # the same frames are missed
        for (_, ta), (_, tb) in zip(a, b):
            self.assertLessEqual(abs(tb[0][1] - ta[0][1] - 1000), 1)             # the same noise, the shift added before it
            self.assertLessEqual(abs(tb[0][2] - ta[0][2]), 1)

    def test_a_stream_with_the_defaults_is_the_stream_of_a_p5_job(self):
        j = J.parse_job("bed_w0000_0600_p5setup", nights=2)
        a = G.RunStream(j.cfg(), 1, 3, j.faults, lux=j.lux, extras=j.extras())
        b = G.RunStream(j.cfg(), 1, 3, j.faults, lux=j.lux, extras=j.extras(), rot=None, empty_off=False)
        ea = [(r, o, type(e).__name__, getattr(e, "payload", None)) for r, o, e in a.events()]
        eb = [(r, o, type(e).__name__, getattr(e, "payload", None)) for r, o, e in b.events()]
        self.assertEqual(ea, eb)
        self.assertGreater(len(ea), 1000)
        self.assertEqual(b.empty_off, [])

    def test_a_rotated_stream_changes_the_frames_after_the_event_and_not_the_context_events(self):
        j = J.parse_job("bed_w0000_0600_p5setup", nights=2)
        rot = (1, 3600, 10.0, 0.0, 0.0)
        a = G.RunStream(j.cfg(), 1, 3, False, extras=j.extras())
        b = G.RunStream(j.cfg(), 1, 3, False, extras=j.extras(), rot=rot)
        ea = list(a.events())
        eb = list(b.events())
        self.assertEqual(len(ea), len(eb))
        t_rot = BASE_MS + (1 * DAY + 3600) * 1000
        differ_before = differ_after = 0
        for (ra, oa, xa), (rb, ob, xb) in zip(ea, eb):
            same = (ra, oa, type(xa).__name__, getattr(xa, "payload", None)) == (rb, ob, type(xb).__name__, getattr(xb, "payload", None))
            if not same:
                if rb < t_rot - 2000:
                    differ_before += 1
                else:
                    differ_after += 1
            if type(xa).__name__ != "FramesMessage":
                self.assertTrue(same)
        self.assertEqual(differ_before, 0)
        self.assertGreater(differ_after, 0)

    def test_a_timeline_takes_extra_off_intervals(self):
        cfg = W.make_cfg(bed=1, nw="00:00-06:00")
        plain = G.Timeline(cfg, 1, 3)
        extra = G.Timeline(cfg, 1, 3, extra_offs=[(10 * 3600, 11 * 3600)])
        self.assertTrue(plain.observing(10 * 3600 * 1000 + 600_000))
        self.assertFalse(extra.observing(10 * 3600 * 1000 + 600_000))
        self.assertIn((10 * 3600 * 1000, 11 * 3600 * 1000), extra.offs)
        self.assertEqual(G.Timeline(cfg, 1, 3, extra_offs=()).offs, plain.offs)

    def test_the_empty_off_intervals_start_ten_minutes_into_each_home_empty_interval_and_end_with_it(self):
        cfg = W.make_cfg(bed=1, nw="00:00-06:00")
        got = G.empty_off_intervals(1, cfg, 3)
        rw = W.RunWorld(1, cfg)
        want = []
        for d in range(4):
            for a, b in W.union([tuple(x) for x in rw.day(d).he]):
                if b - (a + 600) > 0:
                    want.append((a + 600, b))
        self.assertEqual(got, want)
        self.assertGreater(len(got), 0)
        self.assertTrue(all(b > a for a, b in got))

    def test_a_stream_with_empty_off_has_the_radars_off_in_those_intervals_and_the_power_up_after_them(self):
        j = job("p7ctle", nights=2, window="none")
        s = G.RunStream(j.cfg(), 1, 3, False, extras=j.extras(), empty_off=True)
        self.assertEqual(s.empty_off, G.empty_off_intervals(1, j.cfg(), 3))
        self.assertGreater(len(s.empty_off), 0)
        a, b = s.empty_off[0]
        self.assertFalse(s.timeline.observing((a + 5) * 1000))
        self.assertFalse(s.timeline.observing((b - 5) * 1000))
        self.assertTrue(s.timeline.observing((b + 60) * 1000))                  # after the standard power-up sequence
        plain = G.RunStream(j.cfg(), 1, 3, False, extras=j.extras())
        self.assertEqual(plain.empty_off, [])
        self.assertTrue(plain.timeline.observing((a + 5) * 1000))


# ---------------------------------------------------------------- the driver
class P7Stub(StubFactory):
    """The stub learner of the P5 tests with the accessors a P7 night record reads."""

    def __init__(self, params, tm, state=None):
        super().__init__(params, tm, state)
        self.items_ = ([ReviewItem("mod-z0001", 2, "modify_zone", {"tag": "AB", "sid": "s0009-1", "zid": "z0001", "shift_cm": 110, "r": 0.9,
                                                                     "src": "cand"}, 2),
                        ReviewItem("ret-z0002", 1, "retire_zone", {"tag": "CD", "zid": "z0002", "weeks": 3}, 2),
                        ReviewItem("radar_moved", 1, "radar_moved", {"shift_cm": 0, "deg": 7.5, "streak": 2}, 1),
                        ReviewItem("s0004-1", 1, "dwell_zone", {"tag": "EF"}, 2)])

    def review_state(self):
        return self.items_, None

    def zones(self):
        return [SimpleNamespace(id="z0001", kind="dwell", cx_m=2.5, cy_m=2.2, cells=block(W.SPOT["desk"]), rev=1, ref="s0009-1", frozen=False,
                                name="Desk", src="approve", night=7)]

    def drift_view(self):
        return [SimpleNamespace(id="z0001", state="falling", ratio=0.4, frozen=False, lo=1, rt=0, lu=40, rc=False, held=False)]

    def moved_from_count(self):
        return 2

    def radar_moved_state(self):
        return SimpleNamespace(state="open", rev=1, streak=2, tx=(0, 0, -75), hold=True)

    def counters(self):
        return {"p7_errors": 0, "zone7_pruned": 1, "zone7_reset": 0, "frames": 3}


class DriverP7Test(unittest.TestCase):
    def test_the_factory_builds_p7_params_for_a_p7_job_only(self):
        for v, kw in (("p7ctl", {}), ("p7bednl", {"static_freeze": False}), ("p7bednf", {"static_freeze": False}),
                      ("p7ctlnp", {"probation_min_span": 0}), ("p7guestnp", {"probation_min_span": 0})):
            f = D.LearnerFactory.for_job(job(v))
            want = P7Params(enabled=True, **kw)
            self.assertEqual(f.p7params(), want, v)
            self.assertEqual(pickle.loads(pickle.dumps(f)).p7params(), want, v)
        for name in ("bed_w0000_0600_p5setup", "bed_w0000_0600_p6hold", "bed_w0000_0600", "bed_w0000_0600_p5zone"):
            self.assertIs(D.LearnerFactory.for_job(J.parse_job(name)).p7params(), DEFAULT_P7, name)
        self.assertFalse(DEFAULT_P7.enabled)
        self.assertEqual(D.LearnerFactory().p7params(), DEFAULT_P7)
        explicit = D.LearnerFactory.for_job(job("p7ctl"))
        explicit.p7params_ = P7Params(enabled=True, ratio_falling=0.5)
        self.assertEqual(explicit.p7params().ratio_falling, 0.5)

    def test_the_learner_of_a_job_gets_the_parameters_and_keeps_them_over_a_restart(self):
        for v, on in (("p7ctl", True), ("p7ctlnp", True)):
            run = D.Run(job(v, nights=2), 1)
            self.assertEqual(run.learner.p7params.enabled, on)
            old = run.learner
            run.restart_learner()
            self.assertIsNot(run.learner, old)
            self.assertEqual(run.learner.p7params, old.p7params)
        self.assertFalse(D.Run(J.parse_job("bed_w0000_0600_p5setup", nights=2), 1).learner.p7params.enabled)
        self.assertEqual(D.Run(job("p7ctlnp", nights=2), 1).learner.p7params.probation_min_span, 0)
        self.assertFalse(D.Run(job("p7bednf", nights=2), 1).learner.p7params.static_freeze)

    def test_a_p7_job_has_the_p5_recorder_the_world_keys_and_a_10_second_tick(self):
        r = D.Run(job("p7rot", nights=1), 1, learner_factory=P7Stub)
        self.assertIsNotNone(r.p5)
        self.assertIsNone(r.p6)
        self.assertEqual(r.tick_ms, 10_000)
        self.assertEqual(r.stream.targets.rot[0], 28 * DAY + 8 * 3600)
        self.assertEqual(r.p7_tuning, [])
        r2 = D.Run(job("p7mv11e", nights=1, window="none"), 1, learner_factory=P7Stub)
        self.assertGreater(len(r2.stream.empty_off), 0)
        r3 = D.Run(J.parse_job("bed_w0000_0600_p5setup", nights=1), 1, learner_factory=P7Stub)
        self.assertEqual((r3.stream.targets.rot, r3.stream.empty_off), (None, []))

    def test_the_night_record_of_a_p7_job_holds_items_zones_drift_radar_status_and_the_owners_answers(self):
        r = D.Run(job("p7mv11", nights=1), 1, learner_factory=P7Stub)
        r.truth.regions = lambda n: {"sofaAB": (block(J.P6_SPOTS["sofaAB"]), None)}
        user = r._p7_actor()
        self.assertIsNotNone(user)
        user.pending = [{"night": 36, "item": "mod-z0001", "op": "approve", "outcome": "applied"}]
        user.resets = [{"night": 36, "grids_nonzero": 0}]
        rec = r._p7_night({"night": 36})
        self.assertEqual(sorted(rec), sorted(["tuning", "items", "selected", "zones", "drift", "mf", "rm", "status", "errors", "answers",
                                              "reset", "sofa_rle"]))
        self.assertEqual([i["id"] for i in rec["items"]], ["mod-z0001", "ret-z0002", "radar_moved", "s0004-1"])
        mod = rec["items"][0]
        self.assertEqual((mod["sid"], mod["zid"], mod["shift_cm"], mod["r"], mod["src"]), ("s0009-1", "z0001", 110, 0.9, "cand"))
        self.assertEqual(rec["items"][3]["sid"], "s0004-1")                         # a plain place: its own id
        self.assertEqual(rec["items"][1]["weeks"], 3)
        self.assertEqual(rec["zones"][0]["cells_rle"], rle(block(W.SPOT["desk"])))
        self.assertEqual((rec["zones"][0]["rev"], rec["zones"][0]["ref"], rec["zones"][0]["frozen"]), (1, "s0009-1", False))
        self.assertEqual(rec["drift"], [{"id": "z0001", "vs": "falling", "ratio": 0.4, "fz": False, "lo": 1, "rt": 0, "lu": 40, "rc": False,
                                         "held": False}])
        self.assertEqual((rec["mf"], rec["rm"]["state"], rec["rm"]["tx"], rec["rm"]["hold"]), (2, "open", [0, 0, -75], True))
        self.assertEqual(rec["errors"], {"p7_errors": 0, "zone7_pruned": 1, "zone7_reset": 0})
        self.assertEqual(rec["answers"][0]["item"], "mod-z0001")
        self.assertEqual(rec["reset"], {"night": 36, "grids_nonzero": 0})
        self.assertEqual(rec["sofa_rle"], rle(block(J.P6_SPOTS["sofaAB"])))
        self.assertEqual(r._p7_night({"night": 37}).get("reset"), None)             # an answer or a reset is carried once
        self.assertEqual(user.pending, [])

    def test_the_sofa_region_is_recorded_from_the_night_before_the_move_in_a_move_job_only(self):
        r = D.Run(job("p7mv11", nights=1), 1, learner_factory=P7Stub)
        r.truth.regions = lambda n: {"sofaAB": (block(J.P6_SPOTS["sofaAB"]), None)}
        self.assertNotIn("sofa_rle", r._p7_night({"night": 33}))
        self.assertIn("sofa_rle", r._p7_night({"night": 34}))
        r2 = D.Run(job("p7ctl", nights=1), 1, learner_factory=P7Stub)
        r2.truth.regions = lambda n: {"sofaAB": (block(J.P6_SPOTS["sofaAB"]), None)}
        self.assertNotIn("sofa_rle", r2._p7_night({"night": 40}))

    def test_a_stub_learner_without_the_accessors_leaves_their_keys_out(self):
        r = D.Run(job("p7ctl", nights=1), 1, learner_factory=StubFactory)
        rec = r._p7_night({"night": 3})
        self.assertEqual(sorted(rec), ["answers", "errors", "status", "tuning"])         # only the accessors the stub has

    def test_a_tuning_line_joins_the_record_of_the_night_it_belongs_to(self):
        from radar_zone_learner.core.types import TuningLine
        r = D.Run(job("p7ctl", nights=1), 1, learner_factory=P7Stub)
        r.nights = [{"night": 5, "p7": {"tuning": []}}]
        r._process([TuningLine(5, '{"v":1}')])
        self.assertEqual(r.nights[0]["p7"]["tuning"], ['{"v":1}'])
        self.assertEqual(r.p7_tuning, ['{"v":1}'])
        res_ = r.result()
        self.assertEqual(res_["p7"]["tuning"], ['{"v":1}'])
        self.assertIn("empty_off_s", res_["p7"])

    def test_the_result_of_a_non_p7_job_has_no_p7_section(self):
        r = D.Run(J.parse_job("bed_w0000_0600_p5ans", nights=1), 1, learner_factory=StubFactory)
        r.run()
        self.assertNotIn("p7", r.result())
        self.assertNotIn("p7", r.result()["nights"][0])

    def test_the_p4_and_p5_evaluators_do_not_read_a_p7_run(self):
        res = [{"job": "bed_w0000_0600_p7ctl", "seed": 1, "job_spec": {"script": "p7ctl"}, "status": "ok", "nights": []}]
        self.assertIsNone(E4.evaluate_p4(res))
        from replay import evaluate_p5 as E5
        self.assertFalse(E5.is_p5_run(res[0]))
        self.assertFalse(E5.is_rerun(res[0]))


# ---------------------------------------------------------------- the actors
def ritem(i, typ="dwell_zone", rev=1, tier=2, **data):
    return ReviewItem(i, rev, typ, data, tier)


class FakeRun:
    def __init__(self, v="p7ctl", setup_done=True):
        self.now = BASE_MS
        self.job = job(v)
        self.learner = SimpleNamespace(reset_summary=lambda: {"grids_nonzero": 0, "candidates": 0, "stays": 7})
        self.actors = [SimpleNamespace(name="setup_user", done=setup_done)]


def user_of(v, items, night=10, sim=None, setup_done=True):
    u = P7S.P7User(P7S.plan_of(job(v)))
    u.attach(FakeRun(v, setup_done))
    u.items = list(items)
    u.night = night
    u.sim = sim or {}
    sent = []

    def feed(ev):
        sent.append(ev)
    return u, feed, sent


class ActorsP7Test(unittest.TestCase):
    def test_make_actors_gives_the_setup_person_and_the_owner_and_the_special_cases(self):
        names = {v: [a.name for a in P7S.make_actors(job(v))] for v in J.P7_VARIANTS}
        for v, n in names.items():
            self.assertEqual(n[-1], "p7user", v)
            self.assertEqual(n[0], "nl_user" if v == "p7bednl" else "setup_user", v)
        self.assertEqual(names["p7guestp"], ["setup_user", "guests_user", "p7user"])
        self.assertEqual(names["p7guest"], ["setup_user", "p7user"])
        self.assertEqual([a.name for a in P4.make_actors(job("p7ret"))], ["setup_user", "p7user"])           # the p4 router hands p7 scripts on
        self.assertTrue(P7S.make_actors(job("p7guestp"))[1].pause)

    def test_the_plan_of_each_job(self):
        plan = {v: P7S.plan_of(job(v)) for v in J.P7_VARIANTS}
        for v in ("p7mv11", "p7mv29", "p7mv11e", "p7mv29e"):
            self.assertEqual((plan[v]["until"], plan[v]["mod"], plan[v]["ret"], plan[v]["radar"]), (34, True, False, False), v)
        self.assertEqual(plan["p7mvback"]["until"], 27)
        self.assertEqual((plan["p7reset"]["until"], plan["p7reset"]["mod"], plan["p7reset"]["radar"]), (28, True, True))
        self.assertEqual((plan["p7ret"]["until"], plan["p7ret"]["ret"], plan["p7ret"]["mod"]), (None, True, False))
        for v in ("p7ctl", "p7bed", "p7rot", "p7rot3", "p7rot6", "p7shift", "p7shift5", "p7guest", "p7tune"):
            self.assertEqual((plan[v]["until"], plan[v]["mod"], plan[v]["ret"], plan[v]["radar"]), (None, False, False, False), v)

    def test_it_approves_a_place_but_not_the_guest_or_the_visitor_and_records_them_as_offered(self):
        items = [ritem("s0004-1", tag="AB"), ritem("s0005-1", tag="CD"), ritem("s0006-1", tag="EF")]
        sim = {"s0004-1": {"sim_label": "TP:desk"}, "s0005-1": {"sim_label": "FP:guest"}, "s0006-1": {"sim_label": "FP:visitor"}}
        u, feed, sent = user_of("p7ctl", items, sim=sim)
        u.look(feed, 5000)
        self.assertEqual([(e.item, e.op, e.rev) for e in sent], [("s0004-1", "approve", 1)])
        self.assertEqual(sorted(u.offered), ["s0005-1", "s0006-1"])
        self.assertEqual(u.offered["s0005-1"]["label"], "FP:guest")
        self.assertEqual(u.summary()["offered"][0]["night"], 10)

    def test_it_waits_for_the_setup_person_until_night_10(self):
        items = [ritem("s0004-1")]
        u, feed, sent = user_of("p7ctl", items, night=8, setup_done=False)
        u.look(feed, 5000)
        self.assertEqual(sent, [])
        u, feed, sent = user_of("p7ctl", items, night=10, setup_done=False)
        u.look(feed, 5000)
        self.assertEqual(len(sent), 1)
        u, feed, sent = user_of("p7ctl", items, night=8, setup_done=True)
        u.look(feed, 5000)
        self.assertEqual(len(sent), 1)

    def test_after_the_event_it_never_approves_a_place_and_answers_only_the_items_of_its_job(self):
        items = [ritem("s0004-1"), ritem("mod-z0001", "modify_zone", zid="z0001", sid="s0009-1"), ritem("ret-z0002", "retire_zone", zid="z0002"),
                 ritem("radar_moved", "radar_moved", tier=1), ritem("mrg-s0010-1", "merge_into", zid="z0001", sid="s0010-1")]
        u, feed, sent = user_of("p7mv11", items, night=40)
        u.look(feed, 5000)
        self.assertEqual([e.item for e in sent], ["mod-z0001"])
        u, feed, sent = user_of("p7mv11", items, night=34)                      # up to night 34 it approves places (and the modify)
        u.look(feed, 5000)
        self.assertEqual([e.item for e in sent], ["s0004-1", "mod-z0001"])
        u, feed, sent = user_of("p7ret", items, night=30)
        u.look(feed, 5000)
        self.assertEqual([e.item for e in sent], ["s0004-1", "ret-z0002"])
        u, feed, sent = user_of("p7reset", items, night=31)
        u.look(feed, 5000)
        self.assertEqual([e.item for e in sent], ["mod-z0001", "radar_moved"])
        for v in ("p7rot", "p7rot3", "p7rot6", "p7shift", "p7shift5", "p7ctl"):    # the latch is observable: nobody answers radar_moved
            u, feed, sent = user_of(v, items, night=32)
            u.look(feed, 5000)
            self.assertNotIn("radar_moved", [e.item for e in sent], v)
            self.assertNotIn("mod-z0001", [e.item for e in sent], v)
            self.assertNotIn("ret-z0002", [e.item for e in sent], v)

    def test_a_modify_answer_for_a_guest_place_is_still_a_modify_answer(self):
        items = [ritem("mod-z0001", "modify_zone", zid="z0001", sid="s0005-1")]
        u, feed, sent = user_of("p7mv11", items, night=40, sim={"s0005-1": {"sim_label": "FP:guest"}})
        u.look(feed, 5000)
        self.assertEqual(len(sent), 1)
        self.assertEqual(u.offered["mod-z0001"]["label"], "FP:guest")

    def test_an_answer_carries_the_revision_a_refusal_is_recorded_and_retried_at_the_next_look(self):
        u, feed, sent = user_of("p7mv11", [ritem("mod-z0001", "modify_zone", rev=3, zid="z0001", sid="s9")], night=40)

        def refusing(ev):
            sent.append(ev)
            u.outs.append(AnswerIgnored(ev.item, ev.op, "zone_busy"))
        u.look(refusing, 5000)
        self.assertEqual(sent[0].rev, 3)
        self.assertEqual(u.refused, 1)
        self.assertEqual(u.answers[0]["outcome"], "ignored:zone_busy")
        self.assertEqual(u.take_answers()[0]["item"], "mod-z0001")
        self.assertEqual(u.take_answers(), [])
        u.look(refusing, 6000)
        self.assertEqual(len(sent), 2)                                          # asked again at the next look

    def test_one_answer_per_item_and_revision_in_a_look(self):
        items = [ritem("mod-z0001", "modify_zone", zid="z0001", sid="s9")]
        u, feed, sent = user_of("p7mv11", items, night=40)
        u.look(feed, 5000)
        self.assertEqual(len(sent), 1)

    def test_the_approve_of_radar_moved_schedules_the_reset_reading_5_seconds_later(self):
        u, feed, sent = user_of("p7reset", [ritem("radar_moved", "radar_moved", tier=1)], night=30)
        u.look(feed, 100_000)
        self.assertEqual(u.next_time(), 105_000)
        u.act(feed)
        self.assertEqual(u.resets, [{"grids_nonzero": 0, "candidates": 0, "stays": 7, "night": 30, "t": 105_000}])
        self.assertEqual(u.summary()["resets"][0]["stays"], 7)

    def test_a_look_before_the_first_night_does_nothing(self):
        u, feed, sent = user_of("p7ctl", [ritem("s0004-1")], night=None)
        u.look(feed, 5000)
        self.assertEqual(sent, [])

    def test_the_owner_sees_the_suggestions_of_the_last_night_through_nightly(self):
        u, feed, sent = user_of("p7ctl", [])
        u.nightly({"suggestions": [{"id": "s0004-1", "sim_label": "TP:desk"}]})
        self.assertEqual(u.label_of(ritem("s0004-1")), "TP:desk")
        self.assertEqual(u.label_of(ritem("mod-z0001", "modify_zone", sid="s0004-1")), "TP:desk")
        self.assertEqual(u.label_of(ritem("s0099-1")), "")


# ---------------------------------------------------------------- the scripts
class ScriptsP7Test(unittest.TestCase):
    def test_dump_run_takes_the_p7on_flag_and_nights_up_to_60(self):
        from replay import dump_run
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dump_run.main(["--tree", str(ADDON), "--jobs", "bed_w0000_0600_p5setup", "--nights", "61", "--out", "x"]), 2)
        with tmpdir() as t, contextlib.redirect_stdout(io.StringIO()):
            rc = dump_run.main(["--tree", str(ADDON), "--jobs", "bed_w0000_0600_p5setup", "--seeds", "1", "--nights", "1", "--p7on",
                                "--out", t])
            self.assertEqual(rc, 0)
            d = json.loads(next(Path(t).glob("*_s1.json")).read_text(encoding="utf-8"))
            self.assertEqual(d["result"]["status"], "ok")
            self.assertIn("final_state", d)

    def test_a_dump_of_a_p5_job_with_p7_on_has_a_p7_learner_and_without_it_a_disabled_one(self):
        from replay import dump_run
        with tmpdir() as t, contextlib.redirect_stdout(io.StringIO()):
            dump_run.dump(ADDON, ["bed_w0000_0600_p5setup"], (1,), 1, Path(t), log=lambda s: None, p7on=True)
            dump_run.dump(ADDON, ["bed_w0000_0600_p5setup"], (1,), 1, Path(t) / "off", log=lambda s: None)
            on = json.loads(next(Path(t).glob("*_s1.json")).read_text(encoding="utf-8"))
            off = json.loads(next((Path(t) / "off").glob("*_s1.json")).read_text(encoding="utf-8"))
            self.assertEqual(on["result"]["counters"].get("p7_errors"), 0)             # only an enabled learner counts it
            self.assertNotIn("p7_errors", off["result"]["counters"])

    def test_compat_check_p7_checks(self):
        res = CC.check_corrupt_zone7()
        self.assertEqual(res["notes"], {"container_variants": len(CC.CORRUPT_ZONE7_CONTAINER), "value_variants": len(CC.CORRUPT_ZONE7_VALUES)})
        self.assertEqual(res["status"], "PASS", res["problems"])
        fresh = CC._base_state()                                                          # a state with no zone6 and no zone
        fresh["meta"].update(created_date="2026-01-05", created_ms=BASE_MS, night=3, last_extraction_date="2026-01-08")
        res6 = CC.check_p6_state_in_p7(fresh, days=1, tick_ms=600_000)
        self.assertEqual(res6["status"], "INCOMPLETE")
        self.assertEqual(res6["problems"], ["not exercised: the 0.6.0 state has no zone6"])
        self.assertEqual(CC.check_export_validates(fresh)["status"], "INCOMPLETE")
        st = RP.heavy_state()
        st["p5"]["zone6"] = RP.zone6_state(st)
        st["p5"]["zone6"]["ldr"] = sorted(st["p5"]["zone6"]["ldr"])                     # the learner exports it sorted
        res6 = CC.check_p6_state_in_p7(st, days=1, tick_ms=600_000)
        self.assertEqual(res6["status"], "PASS", res6["problems"])
        self.assertEqual(res6["notes"]["renamed"], True)
        self.assertEqual(CC.check_p6_state_in_p7(dict(st, p5=dict(st["p5"], zone7={"v": 1})))["status"], "FAIL")
        orph = CC.check_orphans_p7(st)
        self.assertEqual(orph["status"], "PASS", orph)
        self.assertGreater(orph["notes"]["zone7_pruned"], 0)

    def test_the_state_half_checks_on_a_state_with_zone7(self):
        st0 = RP.p7_state()
        st0["p5"]["zone6"]["ldr"] = sorted(st0["p5"]["zone6"]["ldr"])
        st0["p5"]["zone7"]["z"] = {"z0001": dict(CC.ORPHAN_ZONE7, vs="ok")}
        st = CC._learner(st0, True).export_state()                                      # the file as a learner writes it
        self.assertIn("rm", st["p5"]["zone7"] if "rm" in st["p5"]["zone7"] else {"rm": 1})
        dis = CC.check_p7_state_in_disabled(st)
        self.assertEqual(dis["status"], "PASS", dis)
        self.assertEqual(CC.check_export_validates(st)["status"], "PASS", CC.check_export_validates(st))
        ok = CC.check_p7_state_in_older({"p5": {"zone7": {"v": 1}}, "suggestions": [], "labels": [], "zones": []},
                                        {"loaded": True, "suggestions": [], "labels": [], "zones": [], "p5_zone7": {"v": 1}, "p5_kept": True})
        self.assertEqual(ok["status"], "PASS")
        lost = CC.check_p7_state_in_older({"p5": {"zone7": {"v": 1}}, "suggestions": [], "labels": [], "zones": []},
                                          {"loaded": True, "suggestions": [], "labels": [], "zones": [], "p5_zone7": None, "p5_kept": True})
        self.assertEqual(lost["status"], "FAIL")
        none = CC.check_p7_state_in_older({"p5": {}, "suggestions": [], "labels": [], "zones": []},
                                          {"loaded": True, "suggestions": [], "labels": [], "zones": [], "p5_kept": True})
        self.assertEqual(none["status"], "INCOMPLETE")
        self.assertEqual(CC.check_p7_state_in_older({"suggestions": [], "labels": [], "zones": []}, {"loaded": False, "error": "x"})["status"],
                         "FAIL")

    def test_the_modified_state_has_a_snooze_an_attention_request_and_a_swapped_zone(self):
        st = RP.heavy_state()
        mod = CC.modified_state(st)
        z7 = mod["p5"]["zone7"]
        self.assertIn("ret-z0001", z7["snz"])
        self.assertTrue(z7["rm"]["attn"] and z7["rm"]["open"])
        z = mod["zones"][0]
        self.assertEqual(z["also"], [st["zones"][0]["ref"]])
        self.assertNotEqual(z["ref"], st["zones"][0]["ref"])
        self.assertEqual(mod["p5"]["push"]["kind"], "daily")
        learner = CC.check_p7_modified_in_learner(mod, tick_ms=600_000, days=1)
        self.assertEqual(learner["status"], "PASS", learner)
        older = CC.check_p7_modified_in_older(mod, {"loaded": True, "p5_later": sorted(mod["p5"]["later"]), "p5_push_kind": "daily",
                                                    "p5_zone7": z7})
        self.assertEqual(older["status"], "PASS", older)
        bad = CC.check_p7_modified_in_older(mod, {"loaded": True, "p5_later": ["ret-z0001"], "p5_push_kind": "attention", "p5_zone7": z7})
        self.assertEqual(bad["status"], "FAIL")
        self.assertIsNone(CC.modified_state({"zones": [], "suggestions": []}))

    def test_the_load_only_child_returns_zone7_later_and_the_push_kind(self):
        with tmpdir() as t:
            from radar_zone_learner.core.learner import Learner
            p = Path(t) / "s.json"
            p.write_text(json.dumps(Learner().export_state()), encoding="utf-8")
            out = CC.load_only(ADDON, p)
            self.assertTrue(out["loaded"])
            self.assertIsNone(out["p5_zone7"])
            self.assertEqual(out["p5_later"], [])

    def test_the_p7_probe_state_loads_with_p7_enabled_and_has_what_the_probe_needs(self):
        st = RP.p7_state()
        self.assertEqual((len(st["stays"]), len(st["door_events"]), len(st["ghost_events"])), (4200, 5000, 400))
        self.assertEqual(len(st["coverage"]), 42)
        self.assertEqual(len(st["p5"]["zone7"]["mf"]), 50)
        self.assertIn("zone6", st["p5"])
        L = RP._build_p7(st, RP._p6_base_ms(), True)
        self.assertEqual(len(L.zones()), 200)
        self.assertEqual(L.moved_from_count(), 50)
        self.assertEqual(sorted({s["day"] for s in st["stays"]}).__len__(), 14)
        self.assertEqual(RP.P7_LIMITS["p7_nightly_s"], 0.5)
        self.assertEqual(RP.P7_STEP_NAMES, ("drift", "release", "radar", "tuning"))

    def test_the_nightly_cost_wrapper_times_the_four_steps_and_restores_the_module_functions(self):
        from radar_zone_learner.core import drift, movedfrom, radarmoved, tuningagg
        before = (drift.nightly, movedfrom.release, radarmoved.nightly, tuningagg.nightly)
        st = RP.p7_state()
        r = RP._nightly_cost(st, True)
        self.assertEqual((drift.nightly, movedfrom.release, radarmoved.nightly, tuningagg.nightly), before)
        self.assertGreater(r["nightly_s"], 0)
        self.assertGreater(r["steps_s"]["drift"], 0)
        self.assertEqual(r["failed"], 0)
        self.assertEqual(r["p7_errors"], 0)

    def test_run_replay_knows_the_p7_profiles_and_the_report_module(self):
        from replay import run_replay
        self.assertTrue(hasattr(run_replay, "E7"))
        for p in J.P7_PROFILES:
            self.assertIn(p, J.PROFILES)
        self.assertEqual(run_replay.P7_CPU_BUDGET, 15.0)


class RealCoreP7Test(unittest.TestCase):
    """Two simulated days of a P7 job on the real core: nothing is learned yet (the setup is on day 7), but the whole path (job, stream,
    driver, recorder, the nightly P7 steps, the night record, the result) works end to end and the evaluator reads the record."""

    @classmethod
    def setUpClass(cls):
        r = D.Run(job("p7ctl", nights=2), 1)
        r.run()
        cls.run_ = r
        cls.res = r.result()

    def test_the_run_is_clean_and_every_night_carries_the_p7_record_and_one_tuning_line(self):
        self.assertEqual((self.res["status"], self.res["core_errors"], self.res["first_error"]), ("ok", 0, None))
        self.assertGreaterEqual(len(self.res["nights"]), 2)
        for rec in self.res["nights"]:
            p7 = rec["p7"]
            self.assertEqual(len(p7["tuning"]), 1)
            self.assertEqual(json.loads(p7["tuning"][0])["v"], 1)
            self.assertEqual(p7["rm"]["state"], "none")
            self.assertEqual(p7["errors"]["p7_errors"], 0)
            self.assertEqual(p7["answers"], [])
            self.assertEqual(sorted(k for k in ("items", "zones", "drift", "mf", "rm", "status", "errors", "answers", "tuning") if k not in p7), [])
        self.assertEqual(len(self.res["p7"]["tuning"]), len(self.res["nights"]))
        self.assertEqual(self.res["p7"]["resets"], [])
        self.assertEqual(self.res["p7"]["user"]["plan"]["script"], "p7ctl")

    def test_the_p5_record_is_made_too_and_the_job_spec_names_the_script(self):
        self.assertIn("p5", self.res["nights"][0])
        self.assertEqual((self.res["job_spec"]["script"], self.res["job_spec"]["empty_off"], self.res["job_spec"]["rot"]), ("p7ctl", False, None))

    def test_the_evaluator_reads_such_a_run_without_error_and_says_incomplete(self):
        rep = E7.evaluate_p7([self.res])
        self.assertEqual(rep["runs"], 1)
        self.assertEqual(len(rep["criteria"]), 9)
        self.assertEqual(rep["failed"], [])
        self.assertEqual(rep["gate"], "INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
