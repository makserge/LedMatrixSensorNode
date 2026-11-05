"""compare_runs.py for P7-R1 (P7 spec 8.5 (7), 9.1): the extended field list, the named exemptions (a) and the enabled=True comparison
up to the first modify, retire or reset (b), on small hand-made run dumps."""
import contextlib
import copy
import io
import json
import unittest
from pathlib import Path

from replay import compare_runs as CR
from tests._tmp import tmpdir


def item(i, typ="dwell_zone", rev=1, tier=2, tag="AB"):
    return {"id": i, "rev": rev, "type": typ, "tier": tier, "tag": tag}


def push(seq=1, kind="daily", head="s0004-1"):
    return {"seq": seq, "acked": 0, "kind": kind, "count": 1, "head_id": head, "head_rev": 1, "acks": []}


def night(n, items=(), sugg=(), push_request=None, status=None, p5extra=None, counters=None, zones=(), p6=None):
    rec = {"night": n, "suggestions": [dict(s) for s in sugg], "candidates": 2, "labels": [], "counters": dict(counters or {"stays": n}),
           "p5": {"items": [dict(i) for i in items], "push_request": push_request or push(), "zones": [dict(z) for z in zones],
                  "advanced": [], "suppressions": [], "status": status if status is not None else {"pending_places": len(items), "tier2": len(items),
                                                                                                     "more": 0, "zones_total": len(zones)},
                  "p5": {"masks": {}, "push": {"seq": 1}, "later": 0, "tags_n": 2}}}
    rec["p5"]["p5"].update(p5extra or {})
    if p6 is not None:
        rec["p6"] = p6
    return rec


def run(nights, state=None, script="p5setup"):
    return {"result": {"status": "ok", "labels": {}, "counters": {"stays": 9}, "job_spec": {"script": script}, "nights": nights},
            "final_state": state if state is not None else {"grids": {"a": 1}, "stays": [], "door_events": [], "ghost_events": [],
                                                           "coverage": {}, "zones": [], "suggestions": []}}


def base_nights():
    return [night(n, items=[item("s0004-1")], sugg=[{"id": "s0004-1", "rev": n}]) for n in range(1, 6)]


class ExtendedFieldListTest(unittest.TestCase):
    def test_identical_runs_pass(self):
        self.assertEqual(CR.compare_run(run(base_nights()), run(base_nights())), [])

    def test_a_review_item_a_push_request_and_a_zone_record_are_compared(self):
        a, b = run(base_nights()), run(base_nights())
        b["result"]["nights"][2]["p5"]["items"][0]["rev"] = 2
        b["result"]["nights"][3]["p5"]["push_request"]["count"] = 2
        b["result"]["nights"][4]["p5"]["zones"] = [{"id": "z0001"}]
        out = CR.compare_run(a, b)
        self.assertTrue(any("night 3" in d and "p5.items" in d for d in out), out)
        self.assertTrue(any("night 4" in d and "p5.push_request" in d for d in out), out)
        self.assertTrue(any("night 5" in d and "p5.zones" in d for d in out), out)

    def test_the_advanced_list_the_suppressions_and_the_p5_section_are_compared(self):
        a, b = run(base_nights()), run(base_nights())
        b["result"]["nights"][0]["p5"]["advanced"] = ["adv:remove"]
        b["result"]["nights"][1]["p5"]["suppressions"] = [{"type": "place"}]
        b["result"]["nights"][2]["p5"]["p5"]["tags_n"] = 9
        out = CR.compare_run(a, b)
        for name in ("p5.advanced", "p5.suppressions", "p5.p5"):
            self.assertTrue(any(name in d for d in out), (name, out))

    def test_the_status_values_of_a_night_are_compared_except_the_p7_keys(self):
        a, b = run(base_nights()), run(base_nights())
        for n in b["result"]["nights"]:
            n["p5"]["status"].update({k: 1 for k in CR.P7_STATUS_KEYS})
        self.assertEqual(CR.compare_run(a, b), [])
        b["result"]["nights"][1]["p5"]["status"]["pending_places"] = 7
        out = CR.compare_run(a, b)
        self.assertTrue(any("pending_places" in d for d in out), out)

    def test_the_counters_of_p7_are_exempt_and_the_others_are_not(self):
        a, b = run(base_nights()), run(base_nights())
        for n in b["result"]["nights"]:
            n["counters"].update({"zone7_pruned": 0, "zone7_reset": 0, "p7_errors": 0})
        b["result"]["counters"].update({"zone7_pruned": 1, "p7_errors": 2})
        self.assertEqual(CR.compare_run(a, b), [])
        b["result"]["nights"][0]["counters"]["stays"] = 99
        self.assertTrue(CR.compare_run(a, b))

    def test_the_learning_fields_are_compared(self):
        a, b = run(base_nights()), run(base_nights())
        b["result"]["nights"][3]["suggestions"][0]["rev"] = 99
        b["result"]["nights"][4]["candidates"] = 3
        out = CR.compare_run(a, b)
        self.assertTrue(any("suggestions" in d for d in out))
        self.assertTrue(any("candidates" in d for d in out))

    def test_a_p6_job_also_compares_zone6_and_its_zones(self):
        za = {"zone6": {"delay": {"z0001": [1]}}, "zones": [["z0001"]]}
        a = run([night(n, p6=copy.deepcopy(za)) for n in (1, 2)], script="p6hold")
        b = run([night(n, p6=copy.deepcopy(za)) for n in (1, 2)], script="p6hold")
        self.assertEqual(CR.compare_run(a, b), [])
        b["result"]["nights"][1]["p6"]["zone6"]["delay"]["z0001"] = [2]
        self.assertTrue(any("p6.zone6" in d for d in CR.compare_run(a, b)))
        # a P5 job has no such path; a run whose night records carry a p6 section is read as a P6 job
        c = run(base_nights())
        self.assertFalse(CR._is_p6(c["result"]))
        self.assertTrue(CR._is_p6(a["result"]))
        self.assertEqual(CR.compare_run(c, run(base_nights())), [])

    def test_the_final_state_is_compared_after_the_exemptions(self):
        a, b = run(base_nights()), run(base_nights())
        b["final_state"]["p5"] = {"zone7": {"v": 1}}
        b["final_state"]["writer"] = "radar_zone_learner 0.7.0"
        self.assertEqual(CR.compare_run(a, b), [])
        b["final_state"]["stays"] = [{"t0": 1}]
        self.assertTrue(any("final_state" in d for d in CR.compare_run(a, b)))

    def test_a_bare_run_result_has_no_final_state_to_compare(self):
        a, b = run(base_nights()), run(base_nights())
        self.assertEqual(CR.compare_run(a["result"], b), [])

    def test_the_nights_must_be_the_same(self):
        a, b = run(base_nights()), run(base_nights()[:4])
        self.assertTrue(any("result.nights" in d for d in CR.compare_run(a, b)))


class ClassifyItemsTest(unittest.TestCase):
    def test_a_plain_place_only_in_a_is_named_and_a_p7_item_only_in_b_is_named(self):
        a = [item("s0004-1"), item("s0005-1")]
        b = [item("s0004-1"), item("mod-z0001", "modify_zone"), item("ret-z0002", "retire_zone"), item("radar_moved", "radar_moved", tier=1)]
        named, unnamed = CR.classify_items(a, b)
        self.assertEqual(unnamed, [])
        self.assertEqual(len(named), 4)
        self.assertTrue(any("only in A" in t for t in named))
        self.assertEqual(sum("only in B" in t for t in named), 3)

    def test_any_other_difference_is_unnamed(self):
        named, unnamed = CR.classify_items([item("s0004-1")], [item("s0004-1", rev=2)])
        self.assertEqual((named, len(unnamed)), ([], 1))
        named, unnamed = CR.classify_items([item("night_window", "night_window", tier=1)], [])
        self.assertEqual(len(unnamed), 1)
        named, unnamed = CR.classify_items([], [item("s0009-1")])
        self.assertEqual(len(unnamed), 1)

    def test_the_tier_2_order_is_named(self):
        a = [item("s0004-1"), item("mrg-s0005-1", "merge_into")]
        b = [item("mrg-s0005-1", "merge_into"), item("s0004-1")]
        named, unnamed = CR.classify_items(a, b)
        self.assertEqual(unnamed, [])
        self.assertEqual(named, ["the order of the common items differs"])

    def test_the_same_lists_have_no_difference(self):
        self.assertEqual(CR.classify_items([item("s0004-1")], [item("s0004-1")]), ([], []))
        self.assertEqual(CR.classify_items(None, None), ([], []))


class EnabledComparisonTest(unittest.TestCase):
    def pair(self):
        return run(base_nights()), run(base_nights())

    def test_identical_runs_pass_with_nothing_named(self):
        a, b = self.pair()
        diffs, named = CR.compare_run_p7(a, b)
        self.assertEqual((diffs, named), ([], []))

    def test_a_learning_difference_before_the_first_modify_fails(self):
        a, b = self.pair()
        b["result"]["nights"][1]["suggestions"][0]["rev"] = 77
        diffs, named = CR.compare_run_p7(a, b)
        self.assertTrue(any("night 2 suggestions" in d for d in diffs), diffs)

    def test_a_learning_difference_after_the_first_modify_is_informational(self):
        a, b = self.pair()
        b["result"]["nights"][2]["p5"]["items"].append(item("mod-z0001", "modify_zone"))
        b["result"]["nights"][3]["suggestions"][0]["rev"] = 77
        b["result"]["nights"][3]["counters"]["stays"] = 1000
        for n in (3, 4):
            b["result"]["nights"][n]["p5"]["push_request"]["count"] = 2
            b["result"]["nights"][n]["p5"]["items"].append(item("mod-z0001", "modify_zone"))
        diffs, named = CR.compare_run_p7(a, b)
        self.assertFalse(any("suggestions" in d and "night 4" in d for d in diffs), diffs)
        self.assertTrue(any("informational" in t and "night 4" in t for t in named), named)
        self.assertTrue(any("modify_zone item only in B" in t or "a modify_zone item only in B" in t for t in named), named)

    def test_a_push_request_difference_is_named_only_after_a_p7_item_appeared(self):
        a, b = self.pair()
        b["result"]["nights"][0]["p5"]["push_request"]["count"] = 5
        diffs, _ = CR.compare_run_p7(a, b)
        self.assertTrue(any("push_request" in d for d in diffs))
        a, b = self.pair()
        b["result"]["nights"][1]["p5"]["items"].append(item("ret-z0002", "retire_zone"))
        b["result"]["nights"][1]["p5"]["push_request"]["count"] = 2
        diffs, named = CR.compare_run_p7(a, b)
        self.assertEqual(diffs, [])
        self.assertTrue(any("push request follows" in t for t in named), named)

    def test_a_status_difference_in_the_item_counts_is_named_after_an_item_and_a_failure_before(self):
        a, b = self.pair()
        b["result"]["nights"][3]["p5"]["status"]["tier2"] = 9
        self.assertTrue(any("status" in d for d in CR.compare_run_p7(a, b)[0]))
        a, b = self.pair()
        b["result"]["nights"][1]["p5"]["items"].append(item("mod-z0001", "modify_zone"))
        b["result"]["nights"][1]["p5"]["status"]["tier2"] = 9
        self.assertEqual(CR.compare_run_p7(a, b)[0], [])
        b["result"]["nights"][2]["p5"]["status"]["zones_total"] = 5
        self.assertTrue(any("zones_total" in d for d in CR.compare_run_p7(a, b)[0]))

    def test_the_p7_status_keys_never_count(self):
        a, b = self.pair()
        for n in b["result"]["nights"]:
            n["p5"]["status"].update({k: 3 for k in CR.P7_STATUS_KEYS})
        self.assertEqual(CR.compare_run_p7(a, b)[0], [])

    def test_the_tag_counter_and_the_push_bookkeeping_follow_the_items_the_p5_section_does_not(self):
        a, b = self.pair()
        b["result"]["nights"][1]["p5"]["items"].append(item("mod-z0001", "modify_zone"))
        b["result"]["nights"][1]["p5"]["p5"]["tags_n"] = 3
        self.assertEqual(CR.compare_run_p7(a, b)[0], [])
        b["result"]["nights"][1]["p5"]["p5"]["masks"] = {"s0004-1": {}}
        self.assertTrue(any("p5 section" in d for d in CR.compare_run_p7(a, b)[0]))

    def test_the_p5_section_that_exists_because_zone7_holds_data_is_named_once_and_nothing_else_of_it_passes(self):
        a, b = self.pair()
        for n in a["result"]["nights"]:
            n["p5"]["p5"].update(push=None, settings=None, tags_n=None)
        for n in b["result"]["nights"]:
            n["p5"]["p5"].update(push=dict(CR.DEFAULT_PUSH), settings=dict(CR.DEFAULT_SETTINGS), tags_n=0)
        diffs, named = CR.compare_run_p7(a, b)
        self.assertEqual(diffs, [])
        self.assertEqual(sum("because zone7 holds data" in t for t in named), 1)
        self.assertTrue(CR.p5_exported_for_zone7({"push": None, "settings": None, "tags_n": None},
                                                 {"push": dict(CR.DEFAULT_PUSH), "settings": dict(CR.DEFAULT_SETTINGS), "tags_n": 0}))
        # a push that is not the default, a setting that is on, or an A side that has data of its own are real differences
        b["result"]["nights"][2]["p5"]["p5"]["push"]["seq"] = 3
        self.assertTrue(any("the p5 section differs" in d for d in CR.compare_run_p7(a, b)[0]))
        self.assertFalse(CR.p5_exported_for_zone7({"push": None, "settings": None, "tags_n": None},
                                                  {"push": dict(CR.DEFAULT_PUSH), "settings": dict(CR.DEFAULT_SETTINGS, pets=True), "tags_n": 0}))
        self.assertFalse(CR.p5_exported_for_zone7({"push": {"seq": 1}, "settings": None, "tags_n": None},
                                                  {"push": dict(CR.DEFAULT_PUSH), "settings": dict(CR.DEFAULT_SETTINGS), "tags_n": 0}))

    def test_the_zone_records_the_advanced_list_and_the_suppressions_must_stay_equal(self):
        a, b = self.pair()
        b["result"]["nights"][1]["p5"]["zones"] = [{"id": "z0001"}]
        self.assertTrue(any("p5.zones" in d for d in CR.compare_run_p7(a, b)[0]))
        a, b = self.pair()
        b["result"]["nights"][0]["p5"]["advanced"] = ["x"]
        self.assertTrue(any("p5.advanced" in d for d in CR.compare_run_p7(a, b)[0]))

    def test_the_suppressions_after_the_first_answer_are_informational(self):
        a, b = self.pair()
        b["result"]["nights"][0]["p5"]["items"].append(item("mod-z0001", "modify_zone"))
        b["result"]["nights"][3]["p5"]["suppressions"] = [{"type": "place"}]
        diffs, named = CR.compare_run_p7(a, b)
        self.assertFalse(any("suppressions" in d for d in diffs), diffs)
        self.assertTrue(any("suppressions" in t for t in named))

    def test_the_final_learning_keys_must_agree(self):
        a, b = self.pair()
        b["final_state"]["grids"] = {"a": 2}
        self.assertTrue(any("final_state/grids" in d for d in CR.compare_run_p7(a, b)[0]))
        a, b = self.pair()
        b["final_state"]["p5"] = {"zone7": {"v": 1}}                            # the p5 section is not a learning key
        self.assertEqual(CR.compare_run_p7(a, b)[0], [])

    def test_the_status_of_the_run_and_the_nights_must_agree(self):
        a, b = self.pair()
        b["result"]["status"] = "over_budget"
        self.assertTrue(any("result.status" in d for d in CR.compare_run_p7(a, b)[0]))
        a, b = run(base_nights()), run(base_nights()[:3])
        self.assertTrue(any("result.nights" in d for d in CR.compare_run_p7(a, b)[0]))

    def test_compare_run_with_the_flag_returns_the_failures_only(self):
        a, b = self.pair()
        b["result"]["nights"][1]["p5"]["items"].append(item("mod-z0001", "modify_zone"))
        self.assertEqual(CR.compare_run(a, b, p7on=True), [])


class DirsAndCliTest(unittest.TestCase):
    def write(self, d, name, doc):
        Path(d, name).write_text(json.dumps(doc), encoding="utf-8")

    def test_compare_dirs_lists_passes_failures_and_a_missing_run(self):
        with tmpdir() as da, tmpdir() as db:
            ra, rb = run(base_nights()), run(base_nights())
            self.write(da, "j_s1.json", ra)
            self.write(db, "j_s1.json", rb)
            self.write(da, "k_s1.json", ra)
            rb2 = run(base_nights())
            rb2["result"]["nights"][0]["suggestions"][0]["rev"] = 5
            self.write(db, "k_s1.json", rb2)
            self.write(da, "m_s1.json", ra)
            res = CR.compare_dirs(Path(da), Path(db))
            self.assertEqual(res["status"], "FAIL")
            self.assertEqual({k: v["status"] for k, v in res["runs"].items()}, {"j_s1.json": "PASS", "k_s1.json": "FAIL", "m_s1.json": "FAIL"})
            self.assertIn("exists in A only", res["runs"]["m_s1.json"]["diffs"][0])

    def test_the_p7on_mode_carries_the_named_differences(self):
        with tmpdir() as da, tmpdir() as db:
            ra, rb = run(base_nights()), run(base_nights())
            rb["result"]["nights"][2]["p5"]["items"].append(item("mod-z0001", "modify_zone"))
            self.write(da, "j_s1.json", ra)
            self.write(db, "j_s1.json", rb)
            res = CR.compare_dirs(Path(da), Path(db), p7on=True)
            self.assertEqual(res["status"], "PASS")
            self.assertTrue(res["runs"]["j_s1.json"]["named"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = CR.main(["--a", da, "--b", db, "--p7on"])
            self.assertEqual(rc, 0)
            self.assertIn("(named)", out.getvalue())
            self.assertIn("P7-R1 (b): PASS", out.getvalue())

    def test_the_cli_exit_codes(self):
        with tmpdir() as da, tmpdir() as db:
            self.write(da, "j_s1.json", run(base_nights()))
            self.write(db, "j_s1.json", run(base_nights()))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(CR.main(["--a", da, "--b", db]), 0)
            self.assertIn("P5-R1: PASS", out.getvalue())
            self.write(db, "j_s1.json", run(base_nights()[:2]))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(CR.main(["--a", da, "--b", db]), 1)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(CR.main(["--a", da, "--b", str(Path(db) / "none")]), 2)

    def test_an_empty_folder_pair_does_not_pass(self):
        with tmpdir() as da, tmpdir() as db:
            self.assertEqual(CR.compare_dirs(Path(da), Path(db))["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
