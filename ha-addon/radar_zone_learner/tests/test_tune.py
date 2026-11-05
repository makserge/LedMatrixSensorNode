"""The PC-side tuner (replay/tune.py, P7 spec 4.8, check P7-9 (f)) on synthetic pooled records with known distributions."""
import contextlib
import copy
import io
import json
import unittest
from pathlib import Path

from radar_zone_learner.core import tuningagg
from replay import tune as TU
from tests._tmp import tmpdir

ROW_KEYS = ("delay_cd", "bridge_gap_s", "delay_default_s", "margin_cm", "conf_min", "min_core_days", "dwell_min_stays", "ghost_share_max",
            "covis_thr", "ld_share_min", "p7_ratios", "night_window")


def hist(n: int, **at) -> list:
    """A histogram of ``n`` bins with the counts ``{index: count}`` given as ``i<index>=count``."""
    h = [0] * n
    for k, v in at.items():
        h[int(k[1:])] = v
    return h


def empty_gh() -> dict:
    return {k: [[0] * 6, [0] * 6] for k in ("cf", "cd", "n", "gs", "cv", "ld")}


def pooled(hs=None, dl=None, gh=None, nm=None, la=None, cd=14, n=28) -> dict:
    s = {"hs": hs if hs is not None else [0] * 10, "hn": 0,
         "dl": dl if dl is not None else {k: [0] * 6 for k in TU.KINDS},
         "nm": nm if nm is not None else [0] * 8, "la": la if la is not None else [0] * 4}
    s["hn"] = sum(s["hs"])
    return {"v": 1, "n": n, "sum": s, "last": {"cd": cd, "ac": {"op": {}, "gh": gh if gh is not None else empty_gh(), "rh": {}}}}


def row(rep: dict, key: str) -> dict:
    return next(r for r in rep["rows"] if r["key"] == key)


class BinsTest(unittest.TestCase):
    def test_the_bin_edges_are_the_ones_of_the_core(self):
        for k, edges in TU.BINS.items():
            self.assertEqual(tuple(tuningagg.BINS[k]), tuple(edges), k)

    def test_bin_of_and_upper_edge(self):
        self.assertEqual(TU.bin_of("hs", 5), 0)
        self.assertEqual(TU.bin_of("hs", 10), 1)
        self.assertEqual(TU.bin_of("cf", 0.5), 3)
        self.assertEqual(TU.upper_edge("hs", 0), 10)
        self.assertEqual(TU.upper_edge("hs", 5), 320)
        self.assertEqual(TU.upper_edge("hs", 9), 5120)          # the open top bin: twice the last edge
        self.assertEqual(TU.upper_edge("dl", 5), 600)           # the clamp of the learned delay

    def test_quantile_bin_and_round_up(self):
        self.assertIsNone(TU.quantile_bin([0, 0], 0.95))
        self.assertEqual(TU.quantile_bin([1, 0, 18, 1], 0.95), 2)
        self.assertEqual(TU.quantile_bin([100, 0, 0], 0.5), 0)
        self.assertEqual(TU.round_up(320, 60), 360)
        self.assertEqual(TU.round_up(360, 60), 360)
        self.assertEqual(TU.round_up(5120, 60), 5160)


class RowsTest(unittest.TestCase):
    def test_there_is_a_row_for_every_constant_of_the_table_and_each_has_a_proposal_or_a_reason(self):
        rep = TU.propose(pooled(hs=hist(10, i3=5, i4=30), dl={"dwell": hist(6, i3=5), **{k: [0] * 6 for k in ("short_task", "bed", "manual")}}))
        self.assertEqual([r["key"] for r in rep["rows"]], list(ROW_KEYS))
        for r in rep["rows"]:
            self.assertTrue(r["proposal"] is not None or r["reason"], r["key"])
            self.assertIn(r["class"], ("A", "P", "N"))

    def test_the_bridge_gap_is_the_upper_edge_of_the_q95_bin_rounded_up_to_60_s_with_a_minimum_of_300(self):
        # 65 spells; the cumulative counts 5, 15, 35, 65: the q95 (61.75) is in bin 5 = [160, 320): 320 -> 360
        rep = TU.propose(pooled(hs=hist(10, i2=5, i3=10, i4=20, i5=30)))
        self.assertEqual(row(rep, "bridge_gap_s")["proposal"], 360)
        # every spell under 40 s: the edge is 40, the minimum is 300
        self.assertEqual(row(TU.propose(pooled(hs=hist(10, i0=10, i1=10, i2=10))), "bridge_gap_s")["proposal"], 300)
        # the open top bin
        self.assertEqual(row(TU.propose(pooled(hs=hist(10, i9=30))), "bridge_gap_s")["proposal"], 5160)
        # an edge that is a multiple of 60 stays (640 -> 660 is the first edge above; 1280 -> 1320)
        self.assertEqual(row(TU.propose(pooled(hs=hist(10, i7=30))), "bridge_gap_s")["proposal"], 1320)

    def test_no_bridge_gap_proposal_below_20_spells_and_the_reason_says_so(self):
        r = row(TU.propose(pooled(hs=hist(10, i3=19))), "bridge_gap_s")
        self.assertIsNone(r["proposal"])
        self.assertIn("19 spells", r["reason"])
        r = row(TU.propose(pooled(hs=hist(10, i3=20))), "bridge_gap_s")
        self.assertIsNotNone(r["proposal"])

    def test_the_bridge_gap_proposal_does_not_decrease_with_the_hidden_spell_mean(self):
        shapes = [hist(10, i2=10, i3=20, i4=10), hist(10, i3=10, i4=20, i5=20), hist(10, i4=10, i5=20, i6=20, i7=10)]
        got = [row(TU.propose(pooled(hs=h)), "bridge_gap_s")["proposal"] for h in shapes]
        self.assertEqual(got, sorted(got))
        self.assertLess(got[0], got[-1])

    def test_the_delay_proposal_is_the_upper_edge_of_the_median_bin_and_needs_3_zone_nights_per_kind(self):
        dl = {"dwell": hist(6, i2=2, i3=5), "short_task": hist(6, i0=1, i1=1), "bed": hist(6, i5=4), "manual": [0] * 6}
        r = row(TU.propose(pooled(dl=dl)), "delay_default_s")
        self.assertEqual(r["proposal"], {"dwell": 80, "bed": 600})
        self.assertIn("short_task: 2 zone-nights", r["reason"])
        self.assertIn("manual: 0 zone-nights", r["reason"])
        none = row(TU.propose(pooled()), "delay_default_s")
        self.assertIsNone(none["proposal"])
        self.assertTrue(none["reason"])

    def test_the_margin_row_says_not_computed(self):
        r = row(TU.propose(pooled()), "margin_cm")
        self.assertEqual((r["class"], r["proposal"]), ("N", None))
        self.assertTrue(r["reason"].startswith("not computed"))

    def test_the_ratio_row_prints_the_distributions_and_no_proposal(self):
        p = pooled()
        p["sum"]["dr"] = [1, 2, 3, 4, 5, 6]
        p["sum"]["zs"] = [10, 1, 0, 0, 0, 0, 0]
        r = row(TU.propose(p), "p7_ratios")
        self.assertEqual((r["class"], r["proposal"]), ("N", None))
        self.assertEqual(json.loads(r["note"])["dr"], [1, 2, 3, 4, 5, 6])

    def test_the_learned_delay_and_the_night_window_are_automatic(self):
        rep = TU.propose(pooled())
        self.assertEqual((row(rep, "delay_cd")["class"], row(rep, "night_window")["class"]), ("A", "A"))


class GateRowsTest(unittest.TestCase):
    def gh_cf(self, acc3, rej3):
        gh = empty_gh()
        gh["cf"][0][3] = acc3           # the bin that starts at the threshold 0.5: just above it
        gh["cf"][1][3] = rej3
        return gh

    def test_raise_one_step_when_the_bin_above_the_threshold_holds_30_percent_rejections_over_10_answers(self):
        r = row(TU.propose(pooled(gh=self.gh_cf(6, 4))), "conf_min")
        self.assertEqual(r["proposal"], 0.6)
        self.assertIn("4 of 10", r["note"])
        r = row(TU.propose(pooled(gh=self.gh_cf(0, 10))), "conf_min")
        self.assertEqual(r["proposal"], 0.6)

    def test_no_raise_with_9_answers_or_a_rejection_share_under_30_percent(self):
        r = row(TU.propose(pooled(gh=self.gh_cf(5, 4))), "conf_min")
        self.assertIsNone(r["proposal"])
        self.assertIn("9 answers", r["reason"])
        r = row(TU.propose(pooled(gh=self.gh_cf(8, 2))), "conf_min")
        self.assertIsNone(r["proposal"])
        self.assertIn("20 %", r["reason"])

    def test_the_other_two_gates_use_their_own_variables(self):
        gh = empty_gh()
        gh["cd"][0][bisect(TU.BINS["cd"], 4)] = 5          # the bin at the threshold 4: [4, 5)
        gh["cd"][1][bisect(TU.BINS["cd"], 4)] = 5
        gh["n"][0][bisect(TU.BINS["n"], 6)] = 7            # [6, 9)
        gh["n"][1][bisect(TU.BINS["n"], 6)] = 3
        rep = TU.propose(pooled(gh=gh))
        self.assertEqual(row(rep, "min_core_days")["proposal"], 5)
        self.assertEqual(row(rep, "dwell_min_stays")["proposal"], 9)

    def test_lowering_needs_label_evidence_and_the_gate_must_be_the_most_frequent_single_failing_gate(self):
        nm_conf = [0, 1, 0, 0, 0, 0, 0, 9]
        # 3 unmatched label-nights of 14 days or more and conf the most frequent gate: one step lower
        r = row(TU.propose(pooled(nm=nm_conf, la=[0, 0, 2, 1])), "conf_min")
        self.assertEqual(r["proposal"], 0.4)
        self.assertIn("3 unmatched", r["note"])
        # the same without labels, with young labels only, or with another gate on top: nothing
        for la, nm in (([0, 0, 0, 0], nm_conf), ([5, 5, 0, 0], nm_conf), ([0, 0, 2, 1], [0, 9, 0, 0, 0, 0, 0, 1]),
                       ([0, 0, 2, 1], [0, 0, 0, 0, 0, 0, 0, 0])):
            r = row(TU.propose(pooled(nm=nm, la=la)), "conf_min")
            self.assertIsNone(r["proposal"], (la, nm))
            self.assertIn("lowering needs label evidence", r["reason"])
        # a tie is not 'the most frequent'
        r = row(TU.propose(pooled(nm=[0, 9, 0, 0, 0, 0, 0, 9], la=[0, 0, 3, 0])), "conf_min")
        self.assertIsNone(r["proposal"])

    def test_a_threshold_is_raised_only_from_the_answers_and_lowered_only_from_the_labels(self):
        # rejections in the bin above the threshold AND label evidence: the raise wins (one step at most, never both)
        r = row(TU.propose(pooled(gh=self.gh_cf(0, 12), nm=[0, 0, 0, 0, 0, 0, 0, 9], la=[0, 0, 3, 0])), "conf_min")
        self.assertEqual(r["proposal"], 0.6)

    def test_the_separating_edge_rows_need_5_answers_in_each_group(self):
        gh = empty_gh()
        gh["gs"] = [hist(6, i0=10, i1=5), hist(6, i3=5, i4=5)]
        r = row(TU.propose(pooled(gh=gh)), "ghost_share_max")
        self.assertEqual(r["proposal"], 0.1)                    # the first edge that puts every answer on its side
        gh["gs"] = [hist(6, i0=10, i1=4), hist(6, i3=4)]
        r = row(TU.propose(pooled(gh=gh)), "ghost_share_max")
        self.assertIsNone(r["proposal"])
        self.assertIn("each group needs 5", r["reason"])

    def test_the_min_side_rule_for_the_ld_share(self):
        gh = empty_gh()
        gh["ld"] = [hist(6, i3=10, i4=5), hist(6, i0=5, i1=5)]          # accepted high, rejected low: values below the edge are rejected
        r = row(TU.propose(pooled(gh=gh)), "ld_share_min")
        self.assertIsNotNone(r["proposal"])
        self.assertIn(r["proposal"], TU.BINS["ld"])
        gh["ld"] = [hist(6, i0=5, i3=5), hist(6, i0=5, i3=5)]            # no edge separates identical groups
        r = row(TU.propose(pooled(gh=gh)), "ld_share_min")
        self.assertIsNone(r["proposal"])
        self.assertIn("no bin edge separates", r["reason"])


def bisect(edges, v):
    from bisect import bisect_right
    return bisect_right(edges, v)


class CoveredDatesTest(unittest.TestCase):
    def test_with_fewer_than_14_covered_dates_there_is_no_proposal_and_the_report_says_why(self):
        p = pooled(hs=hist(10, i3=5, i5=30), dl={"dwell": hist(6, i3=5), **{k: [0] * 6 for k in ("short_task", "bed", "manual")}}, cd=13)
        rep = TU.propose(p)
        self.assertFalse(rep["ok"])
        self.assertIn("13 covered dates", rep["reason"])
        self.assertTrue(all(r["proposal"] is None for r in rep["rows"]))
        self.assertTrue(all(r["reason"] for r in rep["rows"]))
        self.assertEqual(row(rep, "bridge_gap_s")["withheld"], 360)
        self.assertIn("NO PROPOSAL", TU.render(rep))
        self.assertTrue(TU.propose(dict(p, last=dict(p["last"], cd=14)))["ok"])

    def test_a_record_without_covered_dates_has_no_proposal_either(self):
        p = pooled()
        p["last"].pop("cd")
        rep = TU.propose(p)
        self.assertFalse(rep["ok"])
        self.assertIn("no covered dates", rep["reason"])

    def test_the_empty_pool_of_the_core_is_read_without_error(self):
        rep = TU.propose(tuningagg.pool([]))
        self.assertFalse(rep["ok"])
        self.assertEqual(len(rep["rows"]), len(ROW_KEYS))

    def test_a_record_that_is_not_pooled_is_refused(self):
        for bad in ({}, {"sum": {}}, {"sum": [], "last": {}}, [], None):
            with self.assertRaises(ValueError):
                TU.propose(bad)

    def test_the_proposal_never_changes_the_input(self):
        p = pooled(hs=hist(10, i3=30))
        before = copy.deepcopy(p)
        TU.propose(p)
        self.assertEqual(p, before)


class CliTest(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = TU.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_the_table_is_printed_for_a_file_and_json_is_available(self):
        with tmpdir() as d:
            f = Path(d) / "pooled.json"
            f.write_text(json.dumps(pooled(hs=hist(10, i3=5, i4=30))), encoding="utf-8")
            rc, out, _ = self.run_cli(["--aggregate", str(f)])
            self.assertEqual(rc, 0)
            self.assertIn("bridge_gap_s", out)
            self.assertIn("not computed", out)
            rc, out, _ = self.run_cli(["--aggregate", str(f), "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["covered_dates"], 14)

    def test_a_copy_with_a_topic_prefix_is_read(self):
        with tmpdir() as d:
            f = Path(d) / "pooled.txt"
            f.write_text("radar_zones/learner/tuning/export " + json.dumps(pooled(hs=hist(10, i3=30))) + "\n", encoding="utf-8")
            rc, out, _ = self.run_cli(["--aggregate", str(f)])
            self.assertEqual(rc, 0)
            self.assertIn("bridge_gap_s", out)

    def test_usage_errors_exit_2(self):
        with tmpdir() as d:
            f = Path(d) / "bad.json"
            f.write_text("[1, 2]", encoding="utf-8")
            self.assertEqual(self.run_cli(["--aggregate", str(f)])[0], 2)
            self.assertEqual(self.run_cli(["--aggregate", str(Path(d) / "none.json")])[0], 2)
            f.write_text("not json", encoding="utf-8")
            self.assertEqual(self.run_cli(["--aggregate", str(f)])[0], 2)
        self.assertEqual(self.run_cli([])[0], 2)


if __name__ == "__main__":
    unittest.main()
