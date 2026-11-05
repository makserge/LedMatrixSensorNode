"""P7 tuning aggregate: the nightly line, the pooled record, the privacy scan, the answer counters (spec 4.8, check P7-9
(U) (a)-(d))."""
import copy
import json
import re
import unittest

from radar_zone_learner.core import tuningagg as T
from radar_zone_learner.core.extraction import GATE_KEYS
from radar_zone_learner.core.types import TuningLine

from .p7_harness import (D0, add_label, add_stay, add_suggestion, add_zone, answer, cover, evidence, make, p7on, set_run,
                         use_days)
from .test_moves import N, Z, run_nights, sofa_scene
from .test_retire import retire_scene


def busy_learner():
    """A learner with stays, candidates, zones, a label, hidden spells and some answers: every key of the line is filled."""
    L, rec, s = sofa_scene()
    add_label(L, (5.0, 2.0), name="Plant", kind="manual")
    L.ns.candidates = [
        {"cid": "c0010-1", "cells": frozenset([1]), "cx": 2.0, "cy": 4.0, "streak": 3, "cls": "dwell", "n": 14,
         "core_days": 5, "minutes": 90.0, "median_s": 600.0, "ld_share": 0.9, "ghost_share": 0.07, "covis": 0.3,
         "conf": 0.55, "gates": {k: True for k in GATE_KEYS}, "outcome": "suggested"},
        {"cid": "c0010-2", "cells": frozenset([2]), "cx": 3.0, "cy": 5.0, "streak": 1, "cls": "short", "n": 4,
         "core_days": 1, "minutes": 5.0, "median_s": 20.0, "ld_share": 0.2, "ghost_share": 0.4, "covis": 0.85,
         "conf": 0.1, "gates": dict({k: True for k in GATE_KEYS}, g1_class=False, g3_ld_ghost=False, conf=False),
         "outcome": "failed"},
        {"cid": "c0010-3", "cells": frozenset([3]), "cx": 4.0, "cy": 5.0, "streak": 2, "cls": "dwell", "n": 9,
         "core_days": 4, "minutes": 40.0, "median_s": 300.0, "ld_share": 0.8, "ghost_share": 0.1, "covis": 0.1,
         "conf": 0.45, "gates": dict({k: True for k in GATE_KEYS}, conf=False), "outcome": "failed"}]
    L.zone6.delay["z0001"] = {"sp": [5, 30, 70, 400], "cd": 45}
    L.bh.door_events.append({"t": 1767571200 + 30 * 86400 + 5, "x": 100, "y": 200, "k": "b"})
    return L


def positions(L):
    return {(st["x"], st["y"]) for st in L.bh.stays}


class LineTest(unittest.TestCase):
    def test_a_line_is_whitelisted_with_exact_lengths_and_scans_clean(self):
        L = busy_learner()
        set_run(L, 32)
        T.nightly(L, 32)                                                        # the first call takes the spell snapshot
        L.zone6.delay["z0001"]["sp"].extend([8, 500])
        line = T.nightly(L, 33)
        self.assertIsInstance(line, TuningLine)
        self.assertEqual(line.night, 33)
        d = json.loads(line.text)
        self.assertTrue(set(d) <= {k for k in T.SHAPE})
        self.assertEqual(set(d), set(T.SHAPE))
        self.assertEqual(T.scan(line.text, positions(L)), [])
        self.assertEqual(d["v"], 1)
        self.assertEqual(len(d["sd"]), 6)
        self.assertEqual(len(d["hs"]), 10)
        self.assertEqual(d["hn"], 2)                                            # the two spells recorded since the last line
        self.assertEqual(sum(d["hs"]), 2)
        self.assertEqual(d["cn"], 3)
        self.assertEqual(sum(d["ch"]["n"]), 3)
        self.assertEqual(d["gf"][0], 1)                                         # g1 failed once
        self.assertEqual(d["nm"][7], 1)                                         # conf was the only failing gate once
        self.assertEqual(sum(1 for v in d["nm"] if v), 1)
        self.assertEqual(sum(d["zs"]), 2)                                       # the zone and the label
        self.assertEqual(d["dg"], 1)
        self.assertEqual(d["lb"][0] + d["lb"][1], 1)
        self.assertEqual(d["dl"]["dwell"][bin_of("dl", 45)], 1)

    def test_every_key_path_of_the_whitelist_exists_and_nothing_else_does(self):
        w = T.whitelist()
        for k in ("v", "ob", "cd", "ac", "ac.op", "ac.op.place", "ac.gh.cf", "ac.rh.mod_y", "dl.dwell", "ch.cd"):
            self.assertIn(k, w)
        for k in ("x", "zid", "date", "ch.x", "ac.op.other"):
            self.assertNotIn(k, w)

    def test_a_line_is_at_most_agg_night_bytes_max_even_with_big_counts(self):
        L = busy_learner()
        set_run(L, 32)
        for lst in L.zone7.ac["op"].values():
            lst[:] = [10 ** 9 - 1] * len(lst)
        for pair in L.zone7.ac["gh"].values():
            for h in pair:
                h[:] = [10 ** 9 - 1] * 6
        for h in L.zone7.ac["rh"].values():
            h[:] = [10 ** 9 - 1] * 6
        line = T.nightly(L, 32)
        self.assertLessEqual(len(line.text.encode("utf-8")), L.p7params.agg_night_bytes_max)
        self.assertEqual(T.scan(line.text), [])

    def test_the_line_has_no_date_night_index_or_hour(self):
        L = busy_learner()
        set_run(L, 32)
        text = T.nightly(L, 32).text
        self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}")
        self.assertNotRegex(text, r"\d{1,2}:\d{2}")
        self.assertNotIn("z0001", text)
        self.assertNotIn("Desk", text)
        self.assertNotIn("s0020", text)
        d = json.loads(text)
        self.assertNotIn("night", d)
        self.assertNotIn(str(D0), text)

    def test_a_line_is_a_function_of_the_learner_state(self):
        a, b = busy_learner(), busy_learner()
        for L in (a, b):
            set_run(L, 32)
        self.assertEqual(T.nightly(a, 32).text, T.nightly(b, 32).text)
        self.assertEqual(T.nightly(a, 33).text, T.nightly(b, 33).text)

    def test_nothing_without_a_run_and_an_empty_learner_gives_a_valid_line(self):
        L = make()
        self.assertIsNone(T.nightly(L, 1))
        set_run(L, 5)
        line = T.nightly(L, 5)
        self.assertEqual(T.scan(line.text), [])
        self.assertEqual(json.loads(line.text)["sn"], 0)

    def test_the_hidden_spell_delta_survives_the_bounded_list(self):
        self.assertEqual(T._new_spells([1, 2, 3], [1, 2, 3, 4, 5]), [4, 5])
        self.assertEqual(T._new_spells([1, 2, 3], [3, 4]), [4])                      # the front was trimmed
        self.assertEqual(T._new_spells([1, 2, 3], [7, 8]), [7, 8])                   # nothing in common: all new
        self.assertEqual(T._new_spells([], [7, 8]), [7, 8])
        self.assertEqual(T._new_spells([5, 6], [5, 6]), [])

    def test_the_first_line_after_a_start_counts_no_spells(self):
        L = busy_learner()
        set_run(L, 32)
        self.assertEqual(json.loads(T.nightly(L, 32).text)["hn"], 0)
        self.assertEqual(json.loads(T.nightly(L, 33).text)["hn"], 0)

    def test_the_drift_histogram_counts_unfrozen_zones_with_a_ratio(self):
        L, rec = retire_scene()
        set_run(L, 46)
        d = json.loads(T.nightly(L, 46).text)
        self.assertEqual(sum(d["dr"]), 1)
        self.assertEqual(d["dr"][0], 1)                                             # ratio 0.0: the lowest bin
        self.assertEqual(d["zs"][6], 1)                                             # collapsed
        self.assertEqual((d["zf"], d["zr"]), (1, 1))
        L.zone7.entry("z0001")["fz"] = True
        d = json.loads(T.nightly(L, 46).text)
        self.assertEqual(sum(d["dr"]), 0)                                           # a frozen zone has no ratio in the histogram


def bin_of(key, v):
    return T.bin_of(key, v)


class BinsTest(unittest.TestCase):
    def test_bin_edges_and_lengths(self):
        self.assertEqual({k: len(v) + 1 for k, v in T.BINS.items()},
                         {"sd": 6, "sl": 5, "sf": 4, "sg": 5, "hs": 10, "dl": 6, "n": 6, "cd": 6, "ld": 6, "gs": 6, "cv": 6,
                          "cf": 6, "dr": 6, "la": 4})
        self.assertEqual([T.bin_of("n", v) for v in (5, 6, 8.9, 9, 17, 18, 29, 30, 99)], [0, 1, 1, 2, 3, 4, 4, 5, 5])
        self.assertEqual([T.bin_of("dr", v) for v in (0.0, 0.09, 0.1, 0.24, 0.25, 0.59, 0.6, 0.9, 1.19, 1.2, 5)],
                         [0, 0, 1, 1, 2, 2, 3, 4, 4, 5, 5])
        self.assertEqual([T.bin_of("hs", v) for v in (1, 9, 10, 19, 2559, 2560)], [0, 0, 1, 1, 8, 9])
        self.assertEqual(T.bin_of("sf", 1), 0)
        self.assertEqual(T.bin_of("la", 6), 0)
        self.assertEqual(T.bin_of("la", 28), 3)
        self.assertEqual(T.bin_of("n", "x"), 0)
        self.assertEqual(T.bin_of("n", float("nan")), 0)


class PoolTest(unittest.TestCase):
    def lines(self, n=3):
        L = busy_learner()
        out = []
        for k in range(n):
            set_run(L, 32 + k)
            L.zone7.ac["op"]["place"][0] = k + 1                                 # a cumulative counter
            out.append(T.nightly(L, 32 + k).text)
        return out

    def test_sum_keys_add_last_keys_take_the_newest_and_n_counts_the_lines(self):
        ls = self.lines(3)
        p = T.pool(ls)
        ds = [json.loads(x) for x in ls]
        self.assertEqual((p["v"], p["n"]), (1, 3))
        self.assertEqual(set(p), {"v", "n", "sum", "last"})
        self.assertEqual(p["sum"]["sn"], sum(d["sn"] for d in ds))
        self.assertEqual(p["sum"]["sd"], [sum(d["sd"][i] for d in ds) for i in range(6)])
        self.assertEqual(p["sum"]["ch"]["n"], [sum(d["ch"]["n"][i] for d in ds) for i in range(6)])
        self.assertAlmostEqual(p["sum"]["ob"], sum(d["ob"] for d in ds))
        self.assertEqual(p["last"]["cd"], ds[-1]["cd"])
        self.assertEqual(p["last"]["ac"]["op"]["place"][0], 3)                  # the newest cumulative value
        self.assertEqual(set(p["sum"]), set(T.SUM_KEYS))
        self.assertEqual(set(p["last"]), set(T.LAST_KEYS))
        for k in ("v",):
            self.assertNotIn(k, p["sum"])

    def test_pool_takes_dicts_or_text_and_handles_empty_and_one(self):
        ls = self.lines(2)
        self.assertEqual(T.pool(ls), T.pool([json.loads(x) for x in ls]))
        self.assertEqual(T.pool([]), {"v": 1, "n": 0, "sum": {}, "last": {}})
        self.assertEqual(T.pool(ls[:1])["n"], 1)

    def test_the_pooled_record_has_no_per_night_series_no_date_and_is_small(self):
        ls = self.lines(28)
        p = T.pool(ls)
        text = json.dumps(p)
        self.assertLessEqual(len(text.encode("utf-8")), make().p7params.agg_export_bytes_max)
        self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}")
        self.assertEqual(T.scan(p), [])
        self.assertEqual(T.scan(text), [])
        self.assertEqual(len(p["sum"]["sd"]), 6)                                 # a histogram, not one value per night

    def test_pool_is_pure(self):
        ls = self.lines(2)
        before = copy.deepcopy(ls)
        T.pool(ls)
        self.assertEqual(ls, before)


class ScanTest(unittest.TestCase):
    def line(self):
        L = busy_learner()
        set_run(L, 32)
        return json.loads(T.nightly(L, 32).text), L

    def test_a_clean_line_and_a_clean_pooled_record_pass(self):
        d, L = self.line()
        self.assertEqual(T.scan(d), [])
        self.assertEqual(T.scan(T.pool([d, d])), [])

    def test_keys_named_like_a_coordinate_name_id_date_time_hour_or_night_are_found(self):
        d, L = self.line()
        for key in ("x", "y", "cx", "cy", "zid", "sid", "name", "zone_name", "date", "day", "time", "hour", "night",
                    "t0", "ts", "id", "pos", "hh"):
            bad = dict(d)
            bad[key] = 1
            v = T.scan(bad)
            self.assertTrue(v, key)
            self.assertTrue(any(repr(key) in x for x in v), key)

    def test_a_nested_extra_key_or_a_missing_one_in_a_dict_shape_is_found(self):
        d, L = self.line()
        bad = copy.deepcopy(d)
        bad["ch"]["x"] = [0] * 6
        self.assertTrue(T.scan(bad))
        bad = copy.deepcopy(d)
        del bad["ch"]["n"]
        self.assertTrue(T.scan(bad))
        bad = copy.deepcopy(d)
        bad["ac"]["op"]["other"] = [0]
        self.assertTrue(T.scan(bad))

    def test_a_list_that_is_not_a_whitelisted_histogram_is_found_so_no_per_night_list(self):
        d, L = self.line()
        bad = dict(d)
        bad["nights"] = [1, 2, 3]
        self.assertTrue(T.scan(bad))
        bad = dict(d)
        bad["sn"] = [3, 4, 5, 6]                                               # a per-night series under a count key
        self.assertTrue(T.scan(bad))
        bad = dict(d)
        bad["sd"] = d["sd"] + [0]                                              # the wrong length
        self.assertTrue(T.scan(bad))
        bad = dict(d)
        bad["sd"] = [0.5] * 6                                                  # not ints
        self.assertTrue(T.scan(bad))

    def test_a_string_an_iso_date_or_an_hh_mm_value_is_found(self):
        d, L = self.line()
        for val in ("x", "2026-01-05", "08:30", "z0001"):
            bad = dict(d)
            bad["cn"] = val
            self.assertTrue(T.scan(bad), val)
        bad = dict(d)
        bad["extra"] = "2026-01-05"
        self.assertTrue(any("date" in x or "whitelisted" in x for x in T.scan(bad)))

    def test_a_pair_that_equals_a_stay_position_is_found(self):
        d, L = self.line()
        pos = positions(L)
        self.assertTrue(pos)
        x, y = sorted(pos)[0]
        bad = copy.deepcopy(d)
        bad["lb"] = [x, y]
        v = T.scan(bad, pos)
        self.assertTrue(any("stay position" in m for m in v))

    def test_a_pooled_record_with_an_extra_section_or_key_is_found(self):
        d, L = self.line()
        p = T.pool([d])
        bad = copy.deepcopy(p)
        bad["nights"] = [1, 2]
        self.assertTrue(T.scan(bad))                                           # not a pooled record any more: scanned as a line
        bad = copy.deepcopy(p)
        bad["sum"]["cd"] = 1                                                   # a last key in the sum section
        self.assertTrue(T.scan(bad))
        bad = copy.deepcopy(p)
        bad["last"]["when"] = 5
        self.assertTrue(T.scan(bad))
        self.assertEqual(T.scan("not json"), ["not JSON"])


class AnswerCounterTest(unittest.TestCase):
    def test_counts_per_item_type_and_op(self):
        L = make()
        for kind, op in (("place", "approve"), ("place", "approve"), ("place", "reject"), ("merge", "rename"),
                         ("modify", "later"), ("retire", "reject"), ("radar", "approve"), ("radar", "later")):
            T.record_answer(L, kind, op)
        ac = L.zone7.ac["op"]
        self.assertEqual(ac["place"], [2, 1, 0, 0])
        self.assertEqual(ac["merge"], [0, 0, 1, 0])
        self.assertEqual(ac["modify"], [0, 0, 0, 1])
        self.assertEqual(ac["retire"], [0, 1, 0])
        self.assertEqual(ac["radar"], [1, 0, 1])
        T.record_answer(L, "retire", "rename")                                # not an op of that type: ignored
        T.record_answer(L, "nothing", "approve")
        self.assertEqual(L.zone7.ac["op"]["retire"], [0, 1, 0])

    def test_the_six_place_variables_go_into_the_accepted_or_rejected_histogram(self):
        L = make()
        s = add_suggestion(L, Z, ev={"n": 13, "core_days": 5, "ghost_share": 0.12, "covis": 0.65, "ld_share": 0.9},
                           conf=0.55)
        T.record_answer(L, "place", "approve", s)
        T.record_answer(L, "place", "reject", s)
        T.record_answer(L, "place", "rename", s)
        T.record_answer(L, "place", "later", s)                                    # later is not a verdict
        gh = L.zone7.ac["gh"]
        self.assertEqual(gh["n"][0][3], 2)                                         # 13 -> bin [12, 18)
        self.assertEqual(gh["n"][1][3], 1)
        self.assertEqual(gh["cd"][0][4], 2)                                        # 5 core days -> [5, 7)
        self.assertEqual(gh["gs"][0][2], 2)                                        # 0.12 -> [0.1, 0.2)
        self.assertEqual(gh["cv"][0][3], 2)                                        # 0.65 -> [0.6, 0.8)
        self.assertEqual(gh["ld"][0][4], 2)                                        # 0.9 -> [0.85, 0.95)
        self.assertEqual(gh["cf"][0][3], 2)                                        # 0.55 -> [0.5, 0.6)
        self.assertEqual(L.zone7.ac["op"]["place"], [1, 1, 1, 1])

    def test_a_suggestion_with_missing_evidence_is_counted_in_the_op_only(self):
        L = make()
        s = add_suggestion(L, Z, ev={})
        T.record_answer(L, "place", "approve", s)
        self.assertEqual(L.zone7.ac["op"]["place"][0], 1)
        self.assertEqual(sum(sum(h) for pair in L.zone7.ac["gh"].values() for h in pair), 1)      # only the conf

    def test_the_ratio_histograms(self):
        L = make()
        T.record_answer(L, "modify", "approve", {"ratio": 0.3})
        T.record_answer(L, "modify", "reject", {"ratio": 0.0})
        T.record_answer(L, "retire", "approve", {"ratio": 0.0})
        T.record_answer(L, "retire", "reject", {"ratio": 0.05})
        T.record_answer(L, "retire", "reject", {"ratio": None})
        T.record_answer(L, "modify", "later", {"ratio": 0.3})                       # later has no histogram
        rh = L.zone7.ac["rh"]
        self.assertEqual((rh["mod_y"][2], rh["mod_r"][0], rh["ret_rm"][0], rh["ret_kp"][0]), (1, 1, 1, 1))
        self.assertEqual(sum(sum(h) for h in rh.values()), 4)

    def test_the_review_handlers_count_through_the_real_answer_path(self):
        L, rec, s = sofa_scene()
        L.review.touch()
        answer(L, "mod-z0001", "approve", rev=1)
        ac = L.zone7.ac
        self.assertEqual(ac["op"]["modify"], [1, 0, 0, 0])
        self.assertEqual(sum(ac["rh"]["mod_y"]), 1)
        self.assertEqual(ac["rh"]["mod_y"][0], 1)                                  # a collapsed zone: ratio 0.0
        L2, rec2 = retire_scene()
        L2.review.touch()
        answer(L2, "ret-z0001", "reject", rev=1)
        self.assertEqual(L2.zone7.ac["op"]["retire"], [0, 1, 0])
        self.assertEqual(sum(L2.zone7.ac["rh"]["ret_kp"]), 1)

    def test_counters_are_capped_and_not_counted_when_disabled(self):
        L = make()
        L.zone7.ac["op"]["place"][0] = 10 ** 9
        T.record_answer(L, "place", "approve")
        self.assertEqual(L.zone7.ac["op"]["place"][0], 10 ** 9)
        from radar_zone_learner.core.params import DEFAULT_P7
        L2 = make(DEFAULT_P7)
        add_suggestion(L2, Z, sid="s0005-1", first=2)
        L2.night = 10
        L2.last_run_ord = D0 + 10
        answer(L2, "s0005-1", "approve", name="Desk")
        self.assertTrue(L2.zone7.is_default())


if __name__ == "__main__":
    unittest.main()
