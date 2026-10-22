"""The world port equals the simulator's world (P3 spec 11.2, 13.4)."""
import hashlib
import importlib.util
import json
import os
import random
import unittest

from replay import world as W

# sha256 of json.dumps([{tracks, occ, motion} for days 0-2], sort_keys=True) for seed 1, bedroom world
# (nw 00:00-06:00), computed once from the original simulator zone_sim_optA_dw.py
# (sha256 f4cc12eb195312bab47a60c48f672a3c091f0e893226af3c8f4735df3bc350b6).
FINGERPRINT_SEED1_BED_D0_2 = "47e66df5c1216c5482ce6da564b802d1b30269fcf0a1a1eabfc68140eeba6b1b"


def _load_sim(path):
    spec = importlib.util.spec_from_file_location("rzl_test_zone_sim", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WorldPortTest(unittest.TestCase):
    def test_fingerprint(self):
        cfg = W.make_cfg(bed=1, nw="00:00-06:00")
        rng = random.Random(1)
        world = W.World(rng, cfg)
        acc = []
        for d in range(3):
            S, he = W.plan_day(rng, d, cfg)
            tracks, occ, motion, sessions = world.day_tracks(d, S)
            acc.append({"tracks": tracks, "occ": occ, "motion": motion})
        digest = hashlib.sha256(json.dumps(acc, sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest, FINGERPRINT_SEED1_BED_D0_2)

    def test_equivalence_with_original(self):
        path = os.environ.get("RZL_SIM_PATH")
        if not path or not os.path.isfile(path):
            self.skipTest("set RZL_SIM_PATH to the original simulator to compare day by day")
        sim = _load_sim(path)
        for bed in (1, 0):
            for seed in (1, 2):
                cfg_o = dict(sim.CFG)
                cfg_o.update(bed=bed)
                cfg_p = W.make_cfg(bed=bed)
                ro, rp = random.Random(seed), random.Random(seed)
                wo, wp = sim.World(ro, cfg_o), W.World(rp, cfg_p)
                for d in range(7):
                    So, heo = sim.plan_day(ro, d, cfg_o)
                    Sp, hep = W.plan_day(rp, d, cfg_p)
                    self.assertEqual(list(So.items()), list(Sp.items()), (bed, seed, d))
                    self.assertEqual(heo, hep)
                    to = wo.day_tracks(d, So)
                    tp = wp.day_tracks(d, Sp)
                    self.assertEqual(to[0], tp[0], (bed, seed, d))
                    self.assertEqual(to[1], tp[1])
                    self.assertEqual(to[2], tp[2])
                    for d_ in (0, 1, 5):
                        for x, y in ((2.5, 2.2), (4.0, 2.2), (0.0, 0.0), (-2.4, 3.6)):
                            self.assertEqual(sim.label(x, y, d_, cfg_o), W.label(x, y, d_, cfg_p))
                for nw in ("00:00-06:00", "22:00-06:00"):
                    c1 = dict(cfg_o, nw=nw, nl=0)
                    c2 = dict(cfg_p, nw=nw, nl=0)
                    for d in range(4):
                        self.assertEqual(sim.night_window_off(d, c1)[0], W.night_window_off(d, c2))

    def test_sessions_record_true_positions(self):
        cfg = W.make_cfg(bed=1)
        rng = random.Random(3)
        world = W.World(rng, cfg)
        S, _ = W.plan_day(rng, 0, cfg)
        tracks, occ, motion, sessions = world.day_tracks(0, S)
        planned = [(ag, s[2], s[3], s[0], s[1]) for ag, ss in S.items() for s in ss]
        self.assertEqual([s[:5] for s in sessions], planned)
        for ag, place, mode, t0, t1, x, y in sessions:
            sx, sy = W.SPOT[place]
            self.assertLess(abs(x - sx), 1.0)
            self.assertLess(abs(y - sy), 1.0)

    def test_night_window_off(self):
        DAY = W.DAY
        c = W.make_cfg(nw="00:00-06:00")
        self.assertEqual(W.night_window_off(0, c), [])
        self.assertEqual(W.night_window_off(1, c), [(DAY, DAY + 6 * 3600)])
        c = W.make_cfg(nw="22:00-06:00")
        self.assertEqual(W.night_window_off(0, c), [(22 * 3600, DAY)])
        self.assertEqual(W.night_window_off(1, c), [(DAY, DAY + 6 * 3600), (DAY + 22 * 3600, 2 * DAY)])
        self.assertEqual(W.night_window_off(3, W.make_cfg()), [])

    def test_label(self):
        cfg = W.make_cfg(bed=1)
        self.assertEqual(W.label(2.55, 2.2, 0, cfg), "TP:desk")
        self.assertEqual(W.label(-2.5, 3.5, 0, cfg), "TP:sofaAB")
        self.assertEqual(W.label(2 * W.WALL_R - 2.55, 2.2, 0, cfg), "FP:mirror_desk")
        self.assertEqual(W.label(2.0, 0.8, 0, cfg), "FP:reflector")
        self.assertTrue(W.label(-5.0, 7.0, 0, cfg).startswith("FP:other"))

    def test_runworld_matches_manual_loop_and_calls_back_once(self):
        cfg = W.make_cfg(bed=0, nw="00:00-06:00")
        seen = []
        rw = W.RunWorld(2, cfg)
        rw.on_day.append(lambda rec: seen.append(rec.d))
        rw.ensure(1)
        day2 = rw.day(2)
        rw.ensure(2)
        self.assertEqual(seen, [0, 1, 2])
        rng = random.Random(2)
        world = W.World(rng, cfg)
        for d in range(3):
            S, he = W.plan_day(rng, d, cfg)
            out = world.day_tracks(d, S)
        self.assertEqual(day2.tracks, out[0])
        self.assertEqual(day2.sessions, out[3])


if __name__ == "__main__":
    unittest.main()
