"""Evaluation of the P3 criteria from synthetic run results (P3 spec 11.7, 13.4)."""
import copy
import unittest
from pathlib import Path

from replay import evaluate as E
from tests._tmp import tmpdir

WINDOWS = ("none", "w0000_0600", "w2200_0600", "w2300_0700", "w0000_0700")
DAY_FINDS = {"desk": 4, "table": 5, "counter": 4, "sofaAB": 6}


def _cut(world, window):
    cut = {p: 0.0 for p in ("desk", "table", "counter", "sofaA", "sofaB", "armchair")}
    if world == "bed":
        cut["bed"] = {"none": 0.0, "w0000_0600": 0.73, "w2200_0600": 0.82, "w2300_0700": 0.92,
                      "w0000_0700": 0.83}[window]
    if window == "w2200_0600":
        cut.update(sofaA=0.174, counter=0.19, sofaB=0.033, desk=0.003)
    return cut


def _sug(sid, place, night, iou=0.8, label=None, kind="dwell", typ="dwell_zone", **flags):
    if place == "sofaAB":
        ious, max_in = {"sofaAB": iou, "sofaA": 0.3, "sofaB": 0.3}, ["sofaA", "sofaB", "sofaAB"]
    elif place is None:
        ious, max_in = {}, []
    else:
        ious, max_in = {place: iou}, [place]
    s = {"id": sid, "rev": 1, "type": typ, "kind": kind, "name_hint": None, "state": "shadow", "first_night": night,
         "last_night": night, "cx": 0.0, "cy": 0.0, "cells_rle": [[0, 1]], "conf": 0.8,
         "variant_evidence_split": False, "variant_share_split": False, "variant_suppressed": False,
         "frozen": False, "variants": 0, "sim_label": label or ("TP:" + place if place else "FP:other(0.0,0.0)"),
         "iou": ious, "max_in": max_in}
    s.update(flags)
    return s


def mk_run(world, window, seed, variant="", nights=21, finds=None, extra=(), status="ok", per_night=None):
    finds = dict(DAY_FINDS if finds is None else finds)
    if world == "bed" and "bed" not in finds and window in ("none", "w0000_0600"):
        finds["bed"] = 9
    sugs = [_sug("s-%s" % p, p, n, kind="bed" if p == "bed" else "dwell") for p, n in finds.items() if n is not None]
    sugs += list(extra)
    snaps = []
    for N in range(nights + 1):
        cur = [copy.deepcopy(s) for s in sugs if s["first_night"] <= N]
        if per_night:
            per_night(N, cur)
        snaps.append({"night": N, "candidates": len(cur), "suggestions": cur, "truth": {}, "labels": [],
                      "counters": {}})
    core = ["desk", "table", "counter", "sofaA", "sofaB"] + (["bed"] if world == "bed" else [])
    return {"job": "%s_%s%s" % (world, window, ("_" + variant) if variant else ""), "world": world,
            "window": window, "variant": variant, "gated": window != "none", "seed": seed, "params": "x",
            "status": status, "core_errors": 0, "first_error": None,
            "timing": {"cpu_s": 40.0, "wall_s": 41.0, "sim_days": nights + 0.15, "cpu_s_per_day": 1.9},
            "nights": snaps, "truth": {"cut": _cut(world, window), "core": core},
            "labels": {"desk": {"ok": True, "night": 0, "attempts": 3}, "recall": 1.0, "precision": 0.5},
            "capture": {"seated": 0.95}, "counters": {}, "stream": {}}


def full_matrix(seeds=(1, 2, 3, 4, 5), mutate=None):
    runs = []
    for world in ("bed", "liv"):
        for window in WINDOWS:
            for seed in seeds:
                finds = dict(DAY_FINDS)
                if mutate:
                    mutate(world, window, seed, finds)
                runs.append(mk_run(world, window, seed, finds=finds))
    for seed in seeds:
        runs.append(mk_run("bed", "w0000_0600", seed, variant="ghost"))
    return runs


def status(report, cid):
    return next(c for c in report["criteria"] if c["id"] == cid)["status"]


class EvaluateTest(unittest.TestCase):
    def test_all_pass(self):
        rep = E.evaluate(full_matrix(), unit_g5b=True)
        self.assertEqual({c["id"]: c["status"] for c in rep["criteria"]},
                         {k: "PASS" for k in ("G1", "G2", "G3", "G4", "G5a", "G5b", "G6")})
        self.assertEqual(rep["gate"], "PASS")
        self.assertEqual(rep["exit_code"], 0)
        g1 = next(c for c in rep["criteria"] if c["id"] == "G1")
        bed00 = next(d for d in g1["details"] if d["world"] == "bed" and d["window"] == "w0000_0600")
        self.assertEqual(bed00["gated_set"], ["counter", "desk", "sofaA", "sofaB", "table"])     # bed: 73% cut
        self.assertEqual(bed00["places"]["sofaA"]["found_nights"], [6] * 5)   # via the merged sofa (D36)

    def test_four_of_five_rule(self):
        def late_one(world, window, seed, finds):
            if (world, window, seed) == ("bed", "w0000_0600", 2):
                finds["desk"] = 8
        rep = E.evaluate(full_matrix(mutate=late_one), unit_g5b=True)
        self.assertEqual(status(rep, "G1"), "PASS")

        def late_two(world, window, seed, finds):
            if world == "bed" and window == "w0000_0600" and seed in (2, 3):
                finds["desk"] = 8
        rep = E.evaluate(full_matrix(mutate=late_two), unit_g5b=True)
        self.assertEqual(status(rep, "G1"), "FAIL")
        self.assertEqual(status(rep, "G2"), "FAIL")
        self.assertEqual(rep["exit_code"], 1)
        self.assertEqual(rep["failed"], ["G1", "G2"])

    def test_gated_set_follows_the_cut_share(self):
        def no_counter(world, window, seed, finds):
            if window == "w2200_0600":
                finds["counter"] = None                     # counter: 19% inside 22:00-06:00, not gated
        rep = E.evaluate(full_matrix(mutate=no_counter), unit_g5b=True)
        self.assertEqual(status(rep, "G1"), "PASS")
        g1 = next(c for c in rep["criteria"] if c["id"] == "G1")
        d = next(x for x in g1["details"] if x["world"] == "bed" and x["window"] == "w2200_0600")
        self.assertNotIn("counter", d["gated_set"])
        self.assertNotIn("sofaA", d["gated_set"])         # 17.4% inside the window
        self.assertIn("sofaB", d["gated_set"])

        def no_counter_00(world, window, seed, finds):
            if window == "w0000_0700":
                finds["counter"] = None                     # counter: 0% inside here, so gated
        rep = E.evaluate(full_matrix(mutate=no_counter_00), unit_g5b=True)
        self.assertEqual(status(rep, "G1"), "FAIL")

    def test_iou_fail_label_ok_is_flagged_and_still_fails(self):
        runs = full_matrix()
        for r in runs:
            if r["world"] == "liv" and r["window"] == "w0000_0600" and r["seed"] in (1, 4):
                for n in r["nights"]:
                    for s in n["suggestions"]:
                        if s["id"] == "s-desk":
                            s["iou"] = {"desk": 0.55}
        rep = E.evaluate(runs, unit_g5b=True)
        self.assertEqual(status(rep, "G1"), "FAIL")
        g1 = next(c for c in rep["criteria"] if c["id"] == "G1")
        d = next(x for x in g1["details"] if x["world"] == "liv" and x["window"] == "w0000_0600")
        self.assertEqual(d["places"]["desk"]["iou_fail_label_ok"], [1, 4])
        self.assertEqual(d["places"]["desk"]["label_nights"], [4] * 5)

    def test_g3_false_place(self):
        runs = [r for r in full_matrix() if not (r["world"] == "liv" and r["window"] == "w0000_0600" and r["seed"] == 3)]
        fp = _sug("s-fp", None, 9, label="FP:other(0.4,6.2)")
        rep = E.evaluate(runs + [mk_run("liv", "w0000_0600", 3, extra=[fp])], unit_g5b=True)
        self.assertEqual(status(rep, "G3"), "FAIL")
        g3 = next(c for c in rep["criteria"] if c["id"] == "G3")
        self.assertEqual(g3["details"][1]["false_places"][0]["label"], "FP:other(0.4,6.2)")

    def test_g4_bed_by_night_14(self):
        def late_bed(world, window, seed, finds):
            if world == "bed" and window == "w0000_0600" and seed in (1, 2):
                finds["bed"] = 15
        rep = E.evaluate(full_matrix(mutate=late_bed), unit_g5b=True)
        self.assertEqual(status(rep, "G4"), "FAIL")

        def bed14(world, window, seed, finds):
            if world == "bed" and window == "w0000_0600":
                finds["bed"] = 14
        self.assertEqual(status(E.evaluate(full_matrix(mutate=bed14), unit_g5b=True), "G4"), "PASS")

    def test_g5_variant_split_and_suppression(self):
        def split(suppressed):
            def f(N, cur):
                for s in cur:
                    if s["id"] == "s-bed" and N >= 12:
                        s.update(variant_evidence_split=True, variant_suppressed=suppressed, kind="bed")
            return f
        runs = [r for r in full_matrix() if not (r["world"] == "bed" and r["window"] == "w0000_0600"
                                                 and r["variant"] == "" and r["seed"] == 5)]
        rep = E.evaluate(runs + [mk_run("bed", "w0000_0600", 5, per_night=split(False))], unit_g5b=True)
        self.assertEqual(status(rep, "G5a"), "FAIL")
        self.assertEqual(status(rep, "G5b"), "FAIL")
        rep = E.evaluate(runs + [mk_run("bed", "w0000_0600", 5, per_night=split(True))], unit_g5b=True)
        self.assertEqual(status(rep, "G5a"), "FAIL")           # the evidence rule must not split at all
        self.assertEqual(status(rep, "G5b"), "PASS")
        rep = E.evaluate(full_matrix(), unit_g5b=False)
        self.assertEqual(status(rep, "G5b"), "FAIL")
        rep = E.evaluate(full_matrix(), unit_g5b=None)
        self.assertEqual(status(rep, "G5b"), "INCOMPLETE")

    def test_g6_ghost_place_and_reported_reflection(self):
        runs = [r for r in full_matrix() if not (r["variant"] == "ghost" and r["seed"] == 2)]
        refl = _sug("s-r", None, 8, label="FP:mirror_desk", typ="reflection", kind="reflection")
        ok = E.evaluate(runs + [mk_run("bed", "w0000_0600", 2, variant="ghost", extra=[refl])], unit_g5b=True)
        self.assertEqual(status(ok, "G6"), "PASS")
        g6 = next(c for c in ok["criteria"] if c["id"] == "G6")
        self.assertEqual(len(g6["details"][0]["reflections"]), 1)
        ghost = _sug("s-g", None, 10, label="FP:mirror_desk")
        bad = E.evaluate(runs + [mk_run("bed", "w0000_0600", 2, variant="ghost", extra=[ghost])], unit_g5b=True)
        self.assertEqual(status(bad, "G6"), "FAIL")

    def test_incomplete_and_bad_runs(self):
        rep = E.evaluate(full_matrix(seeds=(1, 2, 3)), unit_g5b=True)
        self.assertEqual(rep["gate"], "INCOMPLETE")
        self.assertEqual(rep["exit_code"], 0)
        g1 = next(c for c in rep["criteria"] if c["id"] == "G1")
        self.assertTrue(all(d.get("provisional") for d in g1["details"]))
        self.assertEqual(g1["details"][0]["places"]["desk"]["need"], 3)     # 4/5 scaled to 3 seeds
        runs = full_matrix()
        runs[0]["status"] = "over_budget"
        rep = E.evaluate(runs, unit_g5b=True)
        self.assertEqual(rep["exit_code"], 1)
        self.assertEqual(rep["bad_runs"][0]["status"], "over_budget")

    def test_reported_items_and_report_files(self):
        rep = E.evaluate(full_matrix(), unit_g5b=True)
        r1 = next(c for c in rep["reported"] if c["id"] == "R1")
        desk = next(x for x in r1["details"] if x["world"] == "bed" and x["place"] == "desk")
        self.assertEqual(desk["same_night"], 5)
        self.assertEqual(desk["sim_w0000_0600"], [4, 6, 6, 5, 5])
        r3 = next(c for c in rep["reported"] if c["id"] == "R3")
        sofa = next(x for x in r3["details"] if x["world"] == "bed" and x["window"] == "w2200_0600" and x["place"] == "sofaA")
        self.assertTrue(sofa["ge_10pct"])
        self.assertAlmostEqual(sofa["sim_cut_mean_56d"], 0.174, places=3)
        with tmpdir() as d:
            E.write_report(rep, Path(d))
            self.assertTrue((Path(d) / "report.json").is_file())
            md = (Path(d) / "report.md").read_text(encoding="utf-8")
            self.assertIn("Gate (G1-G6): **PASS**", md)
            self.assertNotIn("\r", md)


if __name__ == "__main__":
    unittest.main()
