"""Tests for the gaps the independent P7 review found (coverage threshold, blind and young verdicts, radar-moved gain, correlation
and quiet time, merge and modify thresholds, moved-from release, the values of the tuning line, answer counters). Each one
was checked against a mutant of the rule it pins."""
import json
import random
import unittest

from radar_zone_learner.core import drift, moves, movedfrom, radarmoved as RM, tuningagg as T
from radar_zone_learner.core import zones as ZN

from .p7_harness import (D0, add_label, add_stay, add_suggestion, add_zone, answer, cover, evidence, make, p7on,
                         set_run, use_days)
from .test_moves import N, Z, night, run_nights, sofa_scene, ProbationTest
from .test_movedfrom import cand, with_entry
from .test_radarmoved import CLUSTERS, add_events, base_world, draw, moved_day, rot, run
from .test_retire import retire_scene
from .test_p7_review import mod_scene


class CoverageRule(unittest.TestCase):
    """D4/D5/D18/D19: an hour observed in a minority of the window is not eligible."""

    def scene(self, old_frac, new_frac):
        L = make()
        add_zone(L, Z)
        for d in range(14):
            cover(L, d)
            L.obs.coverage[D0 + d][3] = 3600.0 * (new_frac if d >= 11 else old_frac)
        set_run(L, 14)
        return L

    def test_slow_window_minority_hour_is_not_eligible(self):
        L = self.scene(old_frac=0.2, new_frac=1.0)           # the fast window is full, the slow one 42 %
        self.assertNotIn(3, drift.context(L).elig)

    def test_fast_window_minority_hour_is_not_eligible(self):
        L = self.scene(old_frac=1.0, new_frac=0.2)           # the slow window is 78 %, the fast one 20 %
        self.assertNotIn(3, drift.context(L).elig)


class BlindAndYoung(unittest.TestCase):
    def test_blind_is_frozen_by_coverage(self):
        L = make()
        add_zone(L, Z)
        use_days(L, range(14), Z)
        for d in range(17):
            cover(L, d, seconds=200.0)
        night(L, 17)
        e = L.zone7.entry("z0001")
        self.assertEqual(e["vs"], "blind")
        self.assertTrue(e["fz"])
        self.assertTrue(L.zones()[0].frozen)

    def test_a_zone_exactly_min_zone_age_nights_old_is_judged(self):
        L = make()
        add_zone(L, Z, night=7)
        use_days(L, range(14), Z)
        night(L, 14)                                            # 14 - 7 = 7 nights old
        self.assertEqual(L.zone7.entry("z0001")["vs"], "ok")
        L2 = make()
        add_zone(L2, Z)
        use_days(L2, range(7), Z)
        night(L2, 7)                                            # exactly 7 covered dates
        self.assertEqual(L2.zone7.entry("z0001")["vs"], "ok")


class Radar(unittest.TestCase):
    def test_quiet_time_means_no_evaluation(self):
        L, rng = base_world()
        L.zone7.rm.update(qt=D0 + 15 + 5)
        moved_day(L, rng, 14, deg=10.0)
        run(L, 15)
        self.assertEqual(L.zone7.rm["st"], 0)

    def test_the_gain_over_the_identity_is_needed(self):
        L = make()
        rng = random.Random(4)
        for d in range(14):
            cover(L, d)
            add_events(L, d, draw(rng, 24, sigma=0.3))          # wide door clusters: the identity already fits them
        cover(L, 14)
        add_events(L, 14, [(x + 0.8, y) for x, y in draw(rng, 24, sigma=0.3)])
        run(L, 15)
        dbg = L.p7_radar_dbg
        self.assertGreaterEqual(dbg["hit"], 0.7, dbg)
        self.assertLess(dbg["hit"] - dbg["hit_id"], 0.2, dbg)
        self.assertGreaterEqual(abs(dbg["tx"][2]) / 10, 5.0, dbg)      # the best transform is 'big' ...
        self.assertEqual(L.zone7.rm["st"], 0, dbg)                      # ... but it does not beat the identity by 0.2

    def test_the_hit_rate_of_the_best_transform_must_reach_corr_min(self):
        L, rng = base_world(seed=1)
        cover(L, 14)
        good = [(x + 0.8, y) for x, y in draw(rng, 14)]
        far = [(-6.0 + 0.3 * i, 9.0 - 0.2 * i) for i in range(10)]
        add_events(L, 14, good + far)
        run(L, 15)
        dbg = L.p7_radar_dbg
        self.assertLess(dbg["hit"], 0.7, dbg)
        self.assertGreaterEqual(dbg["hit"] - dbg["hit_id"], 0.2, dbg)
        self.assertGreaterEqual(abs(dbg["tx"][2]) / 10, 5.0, dbg)
        self.assertEqual(L.zone7.rm["st"], 0, dbg)

    def test_the_tolerance_ring_keeps_a_3_degree_turn_below_the_limit_on_every_seed(self):
        # DV12: with ring 0 a 3 degree turn of the same scatter is flagged (3 of these 8 seeds); with the default ring none
        flagged = []
        for seed in range(1, 9):
            L, rng = base_world(seed=seed)
            moved_day(L, rng, 14, deg=3.0)
            run(L, 15)
            if L.zone7.rm["st"]:
                flagged.append(seed)
        self.assertEqual(flagged, [])

    def test_recent_points_are_evenly_subsampled(self):
        L = make()
        pts = [(float(i), 0.0) for i in range(300)]
        sub = RM._subsample(pts, 200)
        self.assertEqual(sub[-1][0], 298.0)                       # spread over all 300, not the first 200


class Moves(unittest.TestCase):
    def test_a_falling_zone_takes_no_merge(self):
        for state in ("falling", "ok"):
            L = make()
            add_zone(L, Z, radius=0.2)
            use_days(L, range(14), Z)
            if state == "falling":
                for d in range(14, 17):
                    cover(L, d)
                run_nights(L, 14, 17)
            else:
                use_days(L, range(14, 20), Z)
                run_nights(L, 14, 20)
            self.assertEqual(L.zone7.entry("z0001")["vs"], state)
            s = add_suggestion(L, (Z[0] + 0.45, Z[1]), sid="s0020-1", first=20, radius=0.2)   # touching, 0.45 m: no modify window
            merges, mods, _, _ = moves.plan(L, [s])
            self.assertEqual((len(merges), mods), (1 if state == "ok" else 0, []), state)

    def test_nearer_wins_a_tie_on_r_whatever_the_ids(self):
        L, rec, s_near = sofa_scene(new_sid="s0021-1")           # 1.1 m, id s0021-1
        use_days(L, range(14, 31), (2.0, 5.6))
        far = add_suggestion(L, (2.0, 5.6), sid="s0020-1", first=20, ev=evidence(hours=(9, 10, 11)))   # 1.6 m, lower id
        _, mods, _, _ = moves.plan(L, [far, s_near])
        self.assertEqual([m["sid"] for m in mods], ["s0021-1"])

    def test_a_candidate_with_r_between_0_and_0_6_is_not_a_move(self):
        L, rec, s = sofa_scene(new_hours=(9, 15, 16, 17), ev_new=evidence(hours=(9, 15, 16, 17)))
        self.assertEqual(moves.plan(L, [s])[1], [])

    def test_a_candidate_with_r_between_0_and_0_6_is_not_a_merge(self):
        L = make()
        add_zone(L, Z)
        use_days(L, range(20), Z)
        run_nights(L, 14, 20)
        s = add_suggestion(L, (2.5, 4.0), sid="s0020-1", first=20, ev=evidence(hours=(9, 15, 16, 17)))
        self.assertEqual(moves.plan(L, [s])[0], [])

    def test_merge_touch_distance_is_0_4_m(self):
        # candidate cells 0.3 m away (a gap of one or two cells): touching; 0.7 m away: not
        for gap, expect in ((0.55, 1), (0.95, 0), (1.3, 0)):
            L = make()
            add_zone(L, Z, radius=0.2)
            use_days(L, range(20), Z)
            run_nights(L, 14, 20)
            s = add_suggestion(L, (Z[0] + gap, Z[1]), sid="s0020-1", first=20, radius=0.2)
            self.assertEqual(len(moves.plan(L, [s])[0]), expect, gap)

    def test_merge_proposal_values(self):
        L = make()
        add_zone(L, Z)
        use_days(L, range(20), Z)
        run_nights(L, 14, 20)
        s = add_suggestion(L, (2.5, 4.0), sid="s0020-1", first=20)
        (m,) = moves.plan(L, [s])[0]
        self.assertEqual((m["ext_cm"], m["dir"]), (50, "to the right"))

    def test_probation_boundaries(self):
        pt = ProbationTest()
        L, s = pt.scene(list(range(29, 40)), first=36)            # span exactly 10
        self.assertTrue(moves.probation_ok(L, s))
        L2, s2 = pt.scene([39], first=14)                          # first made on night 14: not exempt
        self.assertFalse(moves.probation_ok(L2, s2))


class MovedFromTests(unittest.TestCase):
    def test_release_lifts_only_the_requalified_entries(self):
        L, rec = with_entry()
        rec2 = add_zone(L, (5.0, 2.0), name="Other", sid="s0002-1")
        movedfrom.add(L, "retired", rec2)
        L.zone7.mf[1]["day"] = D0 + 10
        L.p7_requalified.add(movedfrom.key_of(L.zone7.mf[0]))
        self.assertEqual(movedfrom.release(L), 1)
        self.assertEqual([m["zid"] for m in L.zone7.mf], ["z0002"])

    def test_the_answer_day_itself_is_pre_event(self):
        L, rec = with_entry()                                      # answered on D0 + 10
        c = cand(L, dates=range(D0 + 10, D0 + 14))                 # the answer day plus 3 post-event days
        self.assertTrue(movedfrom.blocks(L, c, 15))                # only 3 post-event core days

    def test_exactly_six_post_event_stays_requalify(self):
        L, rec = with_entry()
        c = cand(L, dates=[D0 + 11, D0 + 11, D0 + 12, D0 + 12, D0 + 13, D0 + 14], per_day=1)
        self.assertFalse(movedfrom.blocks(L, c, 15))


class Tuning(unittest.TestCase):
    def world(self):
        L = make()
        for d in range(10):
            cover(L, d)
        cover(L, 9, hours=range(5))                                # the newest covered date: 5 observed hours
        L.obs.coverage[D0 + 9] = [3600.0 if h < 5 else 0.0 for h in range(24)]
        L.obs.coverage[D0 + 8] = [3600.0] * 24                     # is not the newest
        add_stay(L, 9, 10, 600, 2.0, 3.0, ld=0.1)
        add_stay(L, 9, 11, 600, 2.0, 3.0, ld=0.9)
        L.bh.stays[-1]["sd"] = 25
        L.bh.stays[-2]["nf"] = 5
        add_stay(L, 8, 10, 600, 2.0, 3.0, ld=0.9)                  # an older date: not counted
        L.bh.door_events.append({"t": 1767571200 + 8 * 86400 + 5, "x": 1, "y": 2, "k": "b"})   # older date
        L.bh.door_events.append({"t": 1767571200 + 9 * 86400 + 5, "x": 1, "y": 2, "k": "b"})
        set_run(L, 11)
        return L

    def test_the_values_of_a_line(self):
        L = self.world()
        d = json.loads(T.nightly(L, 11).text)
        self.assertEqual((d["ob"], d["cd"], d["sn"], d["dg"]), (5.0, 10, 2, 1))
        self.assertEqual(d["sl"], [1, 0, 0, 0, 1])                  # 0.1 and 0.9
        self.assertEqual(d["sf"], [1, 0, 1, 0])                     # 1 and 5 fragments
        self.assertEqual(d["sg"], [1, 0, 1, 0, 0])                  # spread 5 cm and 25 cm
        self.assertEqual(d["sd"], [0, 0, 0, 2, 0, 0])               # 10 minutes each: bin [5, 15)

    def test_labels_matched_and_unmatched_by_age(self):
        L = make()
        for d in range(10):
            cover(L, d)
        s = add_suggestion(L, (2.0, 4.0), sid="s0005-1")
        add_label(L, (2.0, 4.0), name="Plant", kind="manual", lid="l0001")                  # matched
        add_label(L, (2.0, 4.1), name="Plant2", kind="manual", lid="l0003")                 # matched
        lb = add_label(L, (6.0, 6.0), name="Fern", kind="manual", lid="l0002")              # unmatched
        lb["created_s"] = lb["created_s"] + 8 * 86400                                       # 17 days old at the run
        set_run(L, 25)
        d = json.loads(T.nightly(L, 25).text)
        self.assertEqual(d["lb"], [2, 1])
        self.assertEqual(d["la"], [0, 0, 1, 0])                                             # [14, 28)

    def test_candidate_conf_histogram_and_state_order(self):
        L, rec = retire_scene()
        L.ns.candidates = [{"cid": "c1", "cells": frozenset([1]), "cx": 1.0, "cy": 1.0, "streak": 3, "cls": "dwell",
                            "n": 14, "core_days": 5, "minutes": 90.0, "median_s": 600.0, "ld_share": 0.9,
                            "ghost_share": 0.07, "covis": 0.3, "conf": 0.55, "gates": {}, "outcome": "suggested"}]
        set_run(L, 46)
        d = json.loads(T.nightly(L, 46).text)
        self.assertEqual(d["ch"]["cf"], [0, 0, 0, 1, 0, 0])
        self.assertEqual(d["zs"][6], 1)
        L2 = make()
        add_zone(L2, Z)
        use_days(L2, range(20), Z)
        night(L2, 20)
        set_run(L2, 20)
        d2 = json.loads(T.nightly(L2, 20).text)
        self.assertEqual(d2["zs"][0], 1)                                                    # `ok` is the first column

    def test_the_covered_dates_count_is_exact(self):
        L = make()
        for d in range(5):
            cover(L, d)
        set_run(L, 6)
        self.assertEqual(json.loads(T.nightly(L, 6).text)["cd"], 5)


class Review(unittest.TestCase):
    def test_forget_clears_a_lone_keep_date_a_lone_snooze_and_a_lone_ignore_mark(self):
        for what in ("kp", "snz", "ign"):
            L = make()
            set_run(L, 40)
            add_zone(L, Z)
            e = L.zone7.entry("z0001", create=True)
            if what == "kp":
                e["kp"] = 999
            elif what == "snz":
                L.zone7.snz["ret-z0001"] = L.last_run_ord
            else:
                L.zone7.rm["ign"] = 1
            answer(L, None, "forget")
            self.assertEqual((e["kp"], L.zone7.snz, L.zone7.rm["ign"]), (0, {}, 0), what)

    def test_every_p7_answer_counts(self):
        # radar approve / reject / later, retire approve / later, modify reject / rename / later, merge approve
        from .test_p7_review import open_radar
        res = {}
        for op in ("approve", "reject", "later"):
            L = make()
            set_run(L, 40)
            add_zone(L, Z)
            open_radar(L)
            answer(L, "radar_moved", op, rev=1)
            res["radar_" + op] = sum(sum(v) for k, v in L.zone7.ac["op"].items() if k == "radar")
        for op in ("approve", "later"):
            L, _ = retire_scene()
            L.review.touch()
            answer(L, "ret-z0001", op, rev=1)
            res["retire_" + op] = sum(L.zone7.ac["op"]["retire"])
        for op in ("reject", "rename", "later"):
            L, rec, s = mod_scene()
            answer(L, "mod-z0001", op, rev=1, name="Second")
            res["modify_" + op] = sum(L.zone7.ac["op"]["modify"])
        self.assertTrue(all(v == 1 for v in res.values()), res)

    def test_a_retire_items_tag_survives_the_nightly_prune(self):
        L, _ = retire_scene()
        L.review.touch()
        tag = [i for i in L.review.items(L) if i.id == "ret-z0001"][0].data["tag"]
        set_run(L, 47)
        L.review.on_nightly(L)
        self.assertEqual(L.p5.tags.get("z0001"), tag)

    def test_the_status_counts_are_not_always_zero(self):
        L, rec, s = mod_scene()
        e = L.zone7.entry("z0001")
        e.update(vs="falling", lo=3)
        a = L.status().attributes
        self.assertEqual((a["zones_falling"], a["zones_unused"], a["zones_blind"], a["zones_frozen"]), (1, 1, 0, 0))
        e.update(vs="blind", fz=True, lo=0)
        a = L.status().attributes
        self.assertEqual((a["zones_falling"], a["zones_unused"], a["zones_blind"], a["zones_frozen"]), (0, 0, 1, 1))

    def test_a_merge_approve_counts(self):
        L = make()
        add_zone(L, Z)
        use_days(L, range(20), Z)
        run_nights(L, 14, 20)
        add_suggestion(L, (2.5, 4.0), sid="s0020-1", first=20)
        L.review.touch()
        answer(L, "mrg-s0020-1", "approve", rev=L.ns.suggestions[-1]["rev"])
        self.assertEqual(L.zone7.ac["op"]["merge"], [1, 0, 0, 0])

    def test_a_modify_answer_clears_rc(self):
        L, rec, s = mod_scene()
        L.zone7.entry("z0001")["rc"] = True
        L.review.touch()
        answer(L, "mod-z0001", "approve", rev=L.review.items(L)[-1].rev)
        self.assertFalse(L.zone7.entry("z0001")["rc"])


if __name__ == "__main__":
    unittest.main()
