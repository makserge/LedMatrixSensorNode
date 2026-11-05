"""The P7 evaluator (replay.evaluate_p7) on hand-made run records: a run that does what the spec expects passes each judge, and each
defect (an early or missing modify, a plain place at the new spot, an unfrozen bed, a retire that is early or late, a radar item that
opens early, closes again or never opens, an offered guest, a false modify, a private value in a tuning line, ...) fails the check that
is meant to catch it. No simulation and no learner run here."""
import copy
import json
import unittest
from dataclasses import asdict

from radar_zone_learner.core import tuningagg
from replay import evaluate_p7 as E
from replay import jobs as J
from replay import world as W
from replay.evaluate import FAIL, INCOMPLETE, PASS, REPORTED
from replay.truth import rle
from tests._tmp import tmpdir
from tests.p7_harness import make, set_run

DAY = 86400


def block(center, half=1):
    c0 = W.cell_of(*center)
    i, j = c0 % W.NX, c0 // W.NX
    return frozenset((j + dj) * W.NX + i + di for di in range(-half, half + 1) for dj in range(-half, half + 1))


def name_of(variant, window="w0000_0600"):
    return "bed_%s_%s" % (window, variant)


def spec(name, nights=None):
    j = J.parse_job(name, nights=nights)
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(j).items()}


def zrec(zid, kind, spot, night=7, rev=0, frozen=False, cells=None):
    pos = J.P6_SPOTS.get(spot, spot) if isinstance(spot, str) else spot
    cells = block(pos) if cells is None else cells
    return {"id": zid, "kind": kind, "cx": pos[0], "cy": pos[1], "cells_rle": rle(cells), "rev": rev, "ref": None, "frozen": frozen,
            "name": zid, "src": "approve", "night": night}


def drec(zid, vs="ok", ratio=1.0, fz=False, lo=0, rt=0, lu=0, rc=False, held=False):
    return {"id": zid, "vs": vs, "ratio": ratio, "fz": fz, "lo": lo, "rt": rt, "lu": lu, "rc": rc, "held": held}


def irec(i, typ="dwell_zone", rev=1, tier=2, **kw):
    d = {"id": i, "rev": rev, "type": typ, "tier": tier, "tag": "AB", "sid": i, "zid": None, "shift_cm": None, "r": None, "weeks": None,
         "src": None}
    d.update(kw)
    return d


def srec(sid, cx=0.0, cy=0.0, label="TP:desk", cells=None):
    return {"id": sid, "cx": cx, "cy": cy, "sim_label": label, "cells_rle": rle(cells if cells is not None else block((cx, cy)))}


def nrec(n, zones=(), drift=(), items=(), sugs=(), rm=None, sofa=None, answers=(), reset=None, tuning=(), status=None, dbg=None):
    p7 = {"items": [dict(i) for i in items], "zones": [dict(z) for z in zones], "drift": [dict(d) for d in drift], "mf": 0,
          "rm": rm or {"state": "none", "rev": 0, "streak": 0, "tx": [0, 0, 0], "hold": False}, "answers": list(answers), "tuning": list(tuning),
          "errors": {"p7_errors": 0}}
    if sofa is not None:
        p7["sofa_rle"] = rle(sofa)
    if reset is not None:
        p7["reset"] = reset
    if dbg is not None:
        p7["radar_dbg"] = dbg
    return {"night": n, "suggestions": [dict(s) for s in sugs], "p5": {"status": {"covered_days": min(n, 14)}}, "p7": p7}


def run(name, seed, nights, answers=(), resets=(), tuning=(), status="ok", core_errors=0, nights_spec=None):
    return {"job": name, "seed": seed, "status": status, "core_errors": core_errors, "first_error": None,
            "job_spec": spec(name, nights_spec), "nights": list(nights),
            "p7": {"user": {"answers": [dict(a) for a in answers]}, "resets": list(resets), "tuning": list(tuning)}}


def by_status(rows):
    return [r["status"] for r in rows]


# ---------------------------------------------------------------- P7-1
def ctl_run(name, seed, nights=30, day_frozen=False, bed_frozen=True, bed=True, with_zone=True):
    ns = []
    for n in range(1, nights + 1):
        zones, drift = [], []
        if with_zone and n >= 7:
            zones.append(zrec("z0001", "dwell", "desk"))
            drift.append(drec("z0001", "frozen" if day_frozen and n == 20 else "ok", fz=day_frozen and n == 20))
        if bed and n >= 7:
            zones.append(zrec("l0001", "bed", "bed", frozen=bed_frozen))
            drift.append(drec("l0001", "frozen", fz=True))
        ns.append(nrec(n, zones, drift))
    return run(name, seed, ns)


class P71Test(unittest.TestCase):
    def results(self, **kw):
        return [ctl_run(name_of("p7ctl"), s, **kw) for s in range(1, 6)]

    def test_a_control_with_no_frozen_day_zone_and_a_frozen_bed_passes(self):
        c = E.crit_p71(self.results())
        self.assertEqual(c["status"], PASS)
        self.assertEqual(by_status(c["details"]["seeds"]), [PASS] * 5)

    def test_a_day_zone_frozen_by_coverage_fails_a_seed_and_two_fail_the_check(self):
        res = self.results()
        res[0] = ctl_run(name_of("p7ctl"), 1, day_frozen=True)
        self.assertEqual(E.crit_p71(res)["status"], PASS)                       # 4 of 5 seeds
        res[1] = ctl_run(name_of("p7ctl"), 2, day_frozen=True)
        c = E.crit_p71(res)
        self.assertEqual(c["status"], FAIL)
        self.assertEqual(tuple(c["details"]["seeds"][0]["day_frozen"][0][:2]), ("z0001", 20))

    def test_a_bed_that_is_not_frozen_on_one_night_fails_the_check_in_any_seed(self):
        res = self.results()
        res[3] = ctl_run(name_of("p7ctl"), 4, bed_frozen=False)
        c = E.crit_p71(res)
        self.assertEqual(c["status"], FAIL)
        self.assertEqual(tuple(c["details"]["seeds"][3]["bed_not_frozen"][0]), ("l0001", 7))

    def test_no_bed_or_too_short_a_run_is_incomplete_and_missing_seeds_too(self):
        self.assertEqual(E.crit_p71(self.results(bed=False))["status"], INCOMPLETE)
        self.assertEqual(E.crit_p71(self.results(nights=10))["status"], INCOMPLETE)
        self.assertEqual(E.crit_p71(self.results()[:3])["status"], INCOMPLETE)
        self.assertEqual(E.crit_p71([])["status"], INCOMPLETE)

    def test_a_run_that_is_not_ok_or_has_a_core_error_is_incomplete(self):
        res = self.results()
        res[0]["core_errors"] = 1
        res[1]["status"] = "over_budget"
        self.assertEqual(E.crit_p71(res)["status"], INCOMPLETE)

    def test_the_falling_share_and_the_use_are_reported_per_zone(self):
        ns = []
        for n in range(1, 31):
            z = [zrec("z0001", "dwell", "desk")] if n >= 7 else []
            d = [drec("z0001", "falling" if n in (25, 26) else "ok", lu=n)] if n >= 7 else []
            ns.append(nrec(n, z, d))
        c = E.crit_p71([run(name_of("p7ctl"), 1, ns)])
        z = c["details"]["seeds"][0]["zones"]["z0001"]
        self.assertEqual(z["falling"], 2)
        self.assertGreater(z["falling_share"], 0.0)


# ---------------------------------------------------------------- P7-2 and P7-4
def move_run(name, seed, first=40, approve=40, dup=False, no_sofa=False, bump=True, extra_zone=False, old_item=False, last=55, iou_ok=True,
             at_old=False):
    js = spec(name)
    md = js["move_day"]
    new = E._new_centre(js)
    old = J.P6_SPOTS["sofaAB"]
    ns = []
    for n in range(1, last + 1):
        zones, drift, items, sugs = [], [], [], []
        if not no_sofa and n >= 7:
            moved = approve is not None and n > approve and bump
            pos = new if moved else old
            cells = block(pos if iou_ok or not moved else (new[0] + 3.0, new[1]))
            zones.append(zrec("z0004", "dwell", pos, rev=(1 if moved else 0), cells=cells))
            drift.append(drec("z0004", "ok", 1.0))
        if n >= 7:
            zones.append(zrec("z0001", "dwell", "desk"))
        if extra_zone and approve is not None and n == approve + 2:
            zones.append(zrec("z0009", "dwell", new))
        if first is not None and n >= first and (approve is None or n <= approve):
            items.append(irec("mod-z0004", "modify_zone", zid="z0004", sid="s0090-1", shift_cm=108, src="cand", r=0.9))
        if dup and n == md + 3:
            items.append(irec("s0091-1"))
            sugs.append(srec("s0091-1", new[0], new[1], "TP:sofaAB"))
        if old_item and approve is not None and n == approve + 3:
            items.append(irec("s0092-1"))
            sugs.append(srec("s0092-1", old[0], old[1], "FP:old_sofa", cells=block(old)))
        ns.append(nrec(n, zones, drift, items, sugs, sofa=block(new) if n >= md - 1 else None))
    ans = []
    if approve is not None and first is not None:
        ans = [{"night": approve, "item": "mod-z0004", "type": "modify_zone", "op": "approve", "rev": 1, "outcome": "applied"}]
    return run(name, seed, ns, answers=ans)


def move_results(**kw):
    out = []
    for job, seeds in E.P72_JOBS.items():
        variant = job.split("_", 2)[2] if job.startswith("bed_none") else job.split("_", 3)[3]
        window = "none" if job.startswith("bed_none") else "w0000_0600"
        out += [move_run(name_of(variant, window), s, **kw) for s in range(1, seeds + 1)]
    return out


class P72Test(unittest.TestCase):
    def test_the_perfect_move_runs_pass_and_report_the_nights_to_the_modify(self):
        c = E.crit_p72(move_results())
        self.assertEqual(c["status"], PASS)
        self.assertIn("26 of 26", c["counts"]["pooled"])
        rep = c["details"]["reported"]["nights_from_the_move_to_the_first_modify"]
        self.assertEqual(rep[name_of("p7mv11", "none")]["median"], 5)

    def test_a_modify_before_two_nights_after_the_move_fails_and_so_does_one_after_ten(self):
        for first in (36, 46):
            res = move_results(first=first, approve=first)
            c = E.crit_p72(res)
            self.assertEqual(c["status"], FAIL, first)
            self.assertTrue(any("modify item opens" in p for r in c["details"]["by_job"][name_of("p7mv11")]["rows"] for p in r["problems"]))

    def test_no_modify_item_fails(self):
        c = E.crit_p72(move_results(first=None, approve=None))
        self.assertEqual(c["status"], FAIL)

    def test_a_plain_place_at_the_new_centre_fails_a_run(self):
        res = move_results()
        k = next(i for i, r in enumerate(res) if r["job"] == name_of("p7mv11") and r["seed"] == 1)
        res[k] = move_run(name_of("p7mv11"), 1, dup=True)
        c = E.crit_p72(res)
        row = c["details"]["by_job"][name_of("p7mv11")]["rows"][0]
        self.assertEqual(row["status"], FAIL)
        self.assertTrue(any("plain place item at the new place" in p for p in row["problems"]))
        self.assertEqual(c["status"], PASS)                                      # 25 of 26 and 4 of 5 in that job

    def test_two_failures_in_one_job_break_the_per_job_rule_and_three_the_pooled_rule(self):
        res = [r for r in move_results() if not (r["job"] == name_of("p7mv29") and r["seed"] in (1, 2))]
        res += [move_run(name_of("p7mv29"), s, dup=True) for s in (1, 2)]
        c = E.crit_p72(res)
        self.assertEqual(c["details"]["by_job"][name_of("p7mv29")]["status"], FAIL)
        self.assertEqual(c["status"], FAIL)
        res = [r for r in move_results() if r["seed"] not in (1, 2, 3) or r["job"] not in (name_of("p7mv11", "none"), name_of("p7mv29", "none"))]
        res += [move_run(name_of(v, "none"), s, dup=True) for v in ("p7mv11", "p7mv29") for s in (1, 2, 3)]
        self.assertEqual(E.crit_p72(res)["status"], FAIL)

    def test_the_zone_count_the_rev_and_the_new_zone_are_judged(self):
        row = E._p72_row(move_run(name_of("p7mv11"), 1, extra_zone=True))
        self.assertEqual(row["status"], FAIL)
        self.assertTrue(any("zone records changed" in p or "new zone record" in p for p in row["problems"]))
        row = E._p72_row(move_run(name_of("p7mv11"), 1, bump=False))
        self.assertEqual(row["status"], FAIL)
        self.assertTrue(any("rev" in p for p in row["problems"]))
        row = E._p72_row(move_run(name_of("p7mv11"), 1, iou_ok=False))
        self.assertEqual(row["status"], FAIL)
        self.assertTrue(any("IoU" in p for p in row["problems"]))

    def test_a_run_without_a_sofa_zone_is_incomplete_and_more_than_two_make_the_check_incomplete(self):
        self.assertEqual(E._p72_row(move_run(name_of("p7mv11"), 1, no_sofa=True))["status"], INCOMPLETE)
        res = move_results()
        for k in range(3):
            res[k] = move_run(res[k]["job"], res[k]["seed"], no_sofa=True)
        self.assertEqual(E.crit_p72(res)["status"], INCOMPLETE)

    def test_a_missing_run_is_incomplete(self):
        res = [r for r in move_results() if r["job"] != name_of("p7mv29", "none")]
        self.assertEqual(E.crit_p72(res)["status"], INCOMPLETE)

    def test_the_approve_that_was_refused_fails_the_run(self):
        r = move_run(name_of("p7mv11"), 1)
        r["p7"]["user"]["answers"][0]["outcome"] = "ignored:zone_busy"
        row = E._p72_row(r)
        self.assertEqual(row["status"], FAIL)
        self.assertIn("approve was not applied", row["problems"][0])


class P74Test(unittest.TestCase):
    def test_no_old_spot_item_in_the_14_nights_after_the_approve_passes(self):
        c = E.crit_p74(move_results())
        self.assertEqual(c["status"], PASS)

    def test_an_old_spot_item_fails_a_run_and_two_fail_the_job(self):
        res = move_results()
        res = [r for r in res if not (r["job"] == name_of("p7mv11") and r["seed"] in (1, 2))]
        res += [move_run(name_of("p7mv11"), s, old_item=True) for s in (1, 2)]
        c = E.crit_p74(res)
        self.assertEqual(c["details"]["by_job"][name_of("p7mv11")]["status"], FAIL)
        self.assertEqual(c["status"], FAIL)
        row = c["details"]["by_job"][name_of("p7mv11")]["rows"][0]
        self.assertEqual(row["old_spot_items"][0][0], 43)

    def test_a_late_approve_or_a_short_run_is_incomplete(self):
        row = E._p74_row(move_run(name_of("p7mv11"), 1, first=44, approve=44, last=55))
        self.assertEqual(row["status"], INCOMPLETE)
        row = E._p74_row(move_run(name_of("p7mv11"), 1, last=50))
        self.assertEqual(row["status"], INCOMPLETE)
        row = E._p74_row(move_run(name_of("p7mv11"), 1, first=None, approve=None))
        self.assertEqual(row["status"], INCOMPLETE)

    def test_the_return_of_the_sofa_is_reported(self):
        name = name_of("p7mvback")
        r = move_run(name, 1, first=33, approve=33, last=60)
        r["nights"][54]["p7"]["items"].append(irec("s0095-1"))
        r["nights"][54]["suggestions"].append(srec("s0095-1", *J.P6_SPOTS["sofaAB"], label="TP:sofaAB", cells=block(J.P6_SPOTS["sofaAB"])))
        rows = E._p7mvback([r])
        self.assertEqual(rows[0]["status"], REPORTED)
        self.assertEqual(rows[0]["old_spot_offered_night"], 55)
        self.assertEqual(rows[0]["nights_after_return"], 55 - J.P7_BACK_DAY)


# ---------------------------------------------------------------- P7-3
def bed_run(name, seed, label=True, nights=42, frozen=True, lo=0, rt=0, item=None, born=7, nl_not_frozen=False):
    ns = []
    for n in range(1, nights + 1):
        zones, drift, items = [], [], []
        if n >= born:
            zid = "l0001" if label else "z0005"
            zones.append(zrec(zid, "bed", "bed", night=born, frozen=frozen))
            if label:
                drift.append(drec(zid, "frozen", fz=frozen, lo=lo, rt=rt))
            else:
                vs = "ok" if (nl_not_frozen and n % 3 == 0) else "frozen"
                drift.append(drec(zid, vs, fz=False, lo=lo, rt=rt))
        if item is not None and n == 20:
            items.append(item)
        ns.append(nrec(n, zones, drift, items))
    return run(name, seed, ns)


class P73Test(unittest.TestCase):
    def results(self, **kw):
        return ([bed_run(name_of("p7bed"), s) for s in range(1, 6)]
                + [bed_run(name_of("p7bednl"), s, label=False, nights=56, born=9) for s in range(1, 6)])

    def test_a_frozen_bed_and_a_bed_that_is_never_flagged_pass(self):
        c = E.crit_p73(self.results())
        self.assertEqual(c["status"], PASS)
        self.assertEqual(by_status(c["details"]["p7bednl"]), [PASS] * 5)

    def test_an_unfrozen_bed_a_flag_or_a_modify_item_fails_p7bed_in_any_seed(self):
        for kw in (dict(frozen=False), dict(lo=3), dict(rt=1), dict(item=irec("mod-l0001", "modify_zone", zid="l0001", sid="s1"))):
            res = self.results()
            res[2] = bed_run(name_of("p7bed"), 3, **kw)
            c = E.crit_p73(res)
            self.assertEqual(c["status"], FAIL, kw)

    def test_p7bednl_needs_4_of_5_seeds(self):
        res = self.results()
        res[5] = bed_run(name_of("p7bednl"), 1, label=False, nights=56, born=9, lo=3)
        self.assertEqual(E.crit_p73(res)["status"], PASS)
        res[6] = bed_run(name_of("p7bednl"), 2, label=False, nights=56, born=9, rt=2)
        self.assertEqual(E.crit_p73(res)["status"], FAIL)

    def test_the_share_of_nights_frozen_by_coverage_is_reported_for_the_learned_bed(self):
        res = self.results()
        res[5] = bed_run(name_of("p7bednl"), 1, label=False, nights=56, born=9, nl_not_frozen=True)
        row = E.crit_p73(res)["details"]["p7bednl"][0]
        self.assertLess(row["frozen_by_coverage_share"], 1.0)
        self.assertGreater(row["frozen_by_coverage_share"], 0.5)

    def test_no_bed_by_night_8_or_too_few_nights_is_incomplete(self):
        res = self.results()
        res[0] = bed_run(name_of("p7bed"), 1, born=12)
        self.assertEqual(E.crit_p73(res)["status"], INCOMPLETE)
        res = self.results()
        res[0] = bed_run(name_of("p7bed"), 1, nights=20)
        self.assertEqual(E.crit_p73(res)["status"], INCOMPLETE)

    def test_p7bednf_is_reported_with_the_night_it_is_first_flagged(self):
        ns = []
        for n in range(1, 43):
            z = [zrec("l0001", "bed", "bed")] if n >= 7 else []
            d = [drec("l0001", "falling", 0.1, fz=False, lo=min(3, max(0, n - 14)), rt=max(0, n - 16))] if n >= 7 else []
            ns.append(nrec(n, z, d))
        rows = E._p73_nf([run(name_of("p7bednf"), 1, ns)])
        self.assertEqual(rows[0]["status"], REPORTED)
        self.assertEqual(rows[0]["first_flag_night"], 17)


# ---------------------------------------------------------------- P7-5
def ret_run(seed, n1=17, lag=21, approve=True, other=False, mod=False, back=False, last=42, rt_open=None):
    table = W.SPOT["table"]
    ns, gone = [], None
    for n in range(1, last + 1):
        zones, drift, items, sugs = [], [], [], []
        removed = approve and gone is not None and n > gone
        if n >= 7 and not removed:
            zones.append(zrec("z0002", "dwell", table))
            rt = max(0, n - n1 + 1) if n >= n1 else 0
            drift.append(drec("z0002", "collapsed" if rt else "ok", 0.0 if rt else 1.0, rt=rt))
        if n >= 7:
            zones.append(zrec("z0001", "dwell", "desk"))
        if n >= n1 + lag and not removed:
            it = irec("ret-z0002", "retire_zone", zid="z0002", weeks=3)
            items.append(it)
            if gone is None:
                gone = n
        if other and n == 30:
            items.append(irec("ret-z0001", "retire_zone", zid="z0001", weeks=3))
        if mod and n == 30:
            items.append(irec("mod-z0002", "modify_zone", zid="z0002", sid="s2"))
        if back and gone is not None and n == gone + 2:
            items.append(irec("s0077-1"))
            sugs.append(srec("s0077-1", *table, label="TP:table", cells=block(table)))
        ns.append(nrec(n, zones, drift, items, sugs))
    ans = [{"night": gone, "item": "ret-z0002", "type": "retire_zone", "op": "approve", "rev": 1, "outcome": "applied"}] if approve and gone else []
    return run(name_of("p7ret"), seed, ns, answers=ans)


class P75Test(unittest.TestCase):
    def test_a_retire_that_opens_after_21_low_verdicts_and_is_approved_passes(self):
        c = E.crit_p75([ret_run(s) for s in range(1, 6)])
        self.assertEqual(c["status"], PASS)
        row = c["details"]["seeds"][0]
        self.assertEqual((row["n1"], row["n_open"], row["lag"]), (17, 38, 21))
        self.assertGreaterEqual(row["rt_at_open"], 21)

    def test_an_early_or_a_late_item_fails_the_seed(self):
        for lag in (15, 26):
            res = [ret_run(s) for s in range(1, 6)]
            res[0] = ret_run(1, lag=lag)
            res[1] = ret_run(2, lag=lag)
            self.assertEqual(E.crit_p75(res)["status"], FAIL, lag)

    def test_one_bad_seed_of_five_is_tolerated_and_two_are_not(self):
        res = [ret_run(s) for s in range(1, 6)]
        res[0] = ret_run(1, lag=15)
        self.assertEqual(E.crit_p75(res)["status"], PASS)

    def test_a_retire_item_for_another_zone_fails_whatever_the_other_seeds_do(self):
        res = [ret_run(s) for s in range(1, 6)]
        res[4] = ret_run(5, other=True)
        c = E.crit_p75(res)
        self.assertEqual(c["status"], FAIL)
        self.assertTrue(c["details"]["seeds"][4]["other_retire"])

    def test_a_modify_item_for_the_table_zone_or_the_zone_left_standing_fails(self):
        row = E._p75_row(ret_run(1, mod=True))
        self.assertEqual(row["status"], FAIL)
        row = E._p75_row(ret_run(1, approve=False))
        self.assertEqual(row["status"], FAIL)
        self.assertTrue(any("approve was not applied" in p for p in row["problems"]))

    def test_a_plain_place_over_the_old_cells_after_the_removal_fails(self):
        row = E._p75_row(ret_run(1, back=True))
        self.assertEqual(row["status"], FAIL)
        self.assertTrue(any("plain place item over the old cells" in p for p in row["problems"]))

    def test_an_item_that_never_opens_in_a_long_run_fails_and_in_a_short_one_is_incomplete(self):
        r = ret_run(1, lag=100)
        self.assertEqual(E._p75_row(r)["status"], FAIL)
        r = ret_run(1, lag=100, last=20)
        self.assertEqual(E._p75_row(r)["status"], INCOMPLETE)

    def test_no_table_zone_is_incomplete(self):
        r = ret_run(1)
        for n in r["nights"]:
            n["p7"]["zones"] = [z for z in n["p7"]["zones"] if z["id"] != "z0002"]
        self.assertEqual(E._p75_row(r)["status"], INCOMPLETE)


# ---------------------------------------------------------------- P7-6
def radar_run(name, seed, first=30, until=None, close=None, nights=34, resets=(), zones_fn=None, mods=(), rc=True, drop_zone=False):
    ns = []
    for n in range(1, nights + 1):
        items = []
        if first is not None and n >= first and (until is None or n <= until) and not (close is not None and n in close):
            items.append(irec("radar_moved", "radar_moved", tier=1, rev=1, shift_cm=0, deg=7.5, streak=2))
        zones = []
        drift = []
        if zones_fn is not None and n >= 7:
            zones = [z for z in zones_fn() if not (drop_zone and resets and n > resets[0]["night"])]
            drift = [drec(z["id"], rc=(rc and resets and n > resets[0]["night"])) for z in zones]
        for m in mods:
            if m["from"] <= n <= m["to"]:
                items.append(irec("mod-" + m["zid"], "modify_zone", zid=m["zid"], sid="s9", shift_cm=m["shift"], src="own", r=0.0))
        hold = first is not None and 29 <= n <= nights
        ns.append(nrec(n, zones, drift, items, rm={"state": "open" if items else "none", "rev": 1, "streak": 2, "tx": [0, 0, -75],
                                                  "hold": hold}, dbg={"verdict": "moved", "hit": 1.0, "hit_id": 0.4}))
    return run(name, seed, ns, resets=list(resets))


class P76Test(unittest.TestCase):
    def detect(self, first=30, until=None, close=None, seeds=(30, 30, 30, 30, 30), variant="p7rot"):
        return [radar_run(name_of(variant), s + 1, first=f, until=until, close=close) for s, f in enumerate(seeds)]

    def test_an_item_that_opens_at_night_30_and_stays_passes(self):
        r = E._p76_detect(self.detect(), name_of("p7rot"))
        self.assertEqual(r["status"], PASS)
        self.assertIn("5 of 5", r["open_at_30"])

    def test_night_30_in_four_seeds_and_31_in_the_fifth_passes_and_31_in_two_fails(self):
        self.assertEqual(E._p76_detect(self.detect(seeds=(30, 30, 30, 30, 31)), name_of("p7rot"))["status"], PASS)
        self.assertEqual(E._p76_detect(self.detect(seeds=(30, 30, 30, 31, 31)), name_of("p7rot"))["status"], FAIL)

    def test_an_item_before_night_30_or_after_31_or_never_fails(self):
        for seeds in ((29, 30, 30, 30, 30), (30, 30, 30, 30, 32), (30, 30, 30, 30, None)):
            self.assertEqual(E._p76_detect(self.detect(seeds=seeds), name_of("p7rot"))["status"], FAIL, seeds)

    def test_an_item_that_closes_without_an_answer_fails_the_latch(self):
        res = self.detect(until=33)
        self.assertEqual(E._p76_detect(res, name_of("p7rot"))["status"], FAIL)
        res = self.detect(close=(32,))
        self.assertEqual(E._p76_detect(res, name_of("p7rot"))["status"], FAIL)

    def test_the_3_degree_job_has_no_item_in_any_seed(self):
        ok = [radar_run(name_of("p7rot3"), s, first=None) for s in range(1, 6)]
        self.assertEqual(E._p76_none(ok, name_of("p7rot3"))["status"], PASS)
        ok[2] = radar_run(name_of("p7rot3"), 3, first=33)
        r = E._p76_none(ok, name_of("p7rot3"))
        self.assertEqual(r["status"], FAIL)
        self.assertTrue(r["rows"][2]["open_nights"])

    def reset_run(self, seed=1, share_ok=True, grids=0, cands=0, rc=True, drop=False, reopen=False):
        zones = lambda: [zrec("z0001", "dwell", (2.0, 3.0)), zrec("z0002", "dwell", (3.0, 4.0)), zrec("l0001", "bed", "bed"),
                         zrec("z0003", "dwell", (0.4, 0.5))]
        exp1, exp2 = E._expected_shift(2.0, 3.0), E._expected_shift(3.0, 4.0)
        mods = [{"zid": "z0001", "from": 33, "to": 36, "shift": round(exp1 * 100)},
                {"zid": "z0002", "from": 33, "to": 36, "shift": round(exp2 * 100) if share_ok else 5}]
        r = radar_run(name_of("p7reset"), seed, first=30, until=30, nights=46, resets=[{"night": 30, "grids_nonzero": grids, "candidates": cands,
                                                                                      "stays": 0}], zones_fn=zones, mods=mods, rc=rc,
                      drop_zone=drop)
        if reopen:
            r["nights"][35]["p7"]["items"].append(irec("radar_moved", "radar_moved", tier=1))
        return r

    def test_a_reset_that_clears_the_maps_keeps_the_zones_and_re_anchors_them_passes(self):
        row = E._reset_row(self.reset_run())
        self.assertEqual(row["status"], PASS, row.get("problems"))
        self.assertEqual(row["reanchored_share"], 1.0)
        self.assertEqual(len(row["reanchored"]), 2)                      # the bed and the zone near the radar are not counted

    def test_a_grid_that_is_not_zero_a_candidate_a_removed_zone_or_a_zone_without_rc_fails(self):
        self.assertEqual(E._reset_row(self.reset_run(grids=5))["status"], FAIL)
        self.assertEqual(E._reset_row(self.reset_run(cands=2))["status"], FAIL)
        self.assertEqual(E._reset_row(self.reset_run(rc=False))["status"], FAIL)
        self.assertEqual(E._reset_row(self.reset_run(reopen=True))["status"], FAIL)

    def test_a_low_share_of_re_anchored_zones_fails(self):
        row = E._reset_row(self.reset_run(share_ok=False))
        self.assertEqual(row["status"], FAIL)
        self.assertTrue(any("percent of the displaced zones" in p for p in row["problems"]))

    def test_no_approve_taken_is_incomplete(self):
        r = self.reset_run()
        r["p7"]["resets"] = []
        self.assertEqual(E._reset_row(r)["status"], INCOMPLETE)

    def test_the_whole_check_combines_the_four_jobs(self):
        res = (self.detect() + self.detect(seeds=(30,) * 5, variant="p7shift")
               + [radar_run(name_of("p7rot3"), s, first=None) for s in range(1, 6)] + [self.reset_run(s) for s in range(1, 6)])
        c = E.crit_p76(res)
        self.assertEqual(c["status"], PASS, c["counts"])
        res = [r for r in res if r["job"] != name_of("p7shift")]
        self.assertEqual(E.crit_p76(res)["status"], INCOMPLETE)

    def test_the_6_degree_and_the_0_5_m_jobs_are_reported(self):
        res = [radar_run(name_of("p7rot6"), 1, first=31), radar_run(name_of("p7shift5"), 1, first=None)]
        rows = E._p76_opt(res)
        self.assertEqual([r["status"] for r in rows], [REPORTED, REPORTED])
        self.assertEqual([r["first_open_night"] for r in rows], [31, None])


# ---------------------------------------------------------------- P7-7
def guest_run(name, seed, offered=None, nights=42):
    ns = []
    for n in range(1, nights + 1):
        items, sugs = [], []
        if offered and n == offered[0]:
            items.append(irec("s0050-1"))
            sugs.append(srec("s0050-1", *W.SPOT["guest"], label=offered[1]))
        ns.append(nrec(n, [], [], items, sugs))
    return run(name, seed, ns)


class P77Test(unittest.TestCase):
    def results(self, off=None):
        return ([guest_run(name_of("p7guest"), s) for s in range(1, 6)] + [guest_run(name_of("p7guestp"), s) for s in range(1, 6)]) if off is None else off

    def test_no_item_for_the_guest_or_the_visitor_passes(self):
        self.assertEqual(E.crit_p77(self.results())["status"], PASS)

    def test_one_offered_item_in_ten_runs_fails(self):
        for label in ("FP:guest", "FP:visitor"):
            res = self.results()
            res[7] = guest_run(name_of("p7guestp"), 3, offered=(30, label))
            c = E.crit_p77(res)
            self.assertEqual(c["status"], FAIL, label)
            self.assertEqual(c["details"]["by_job"][name_of("p7guestp")]["rows"][2]["offered"][0][3], label)

    def test_a_seat_that_is_a_true_place_is_not_the_guest(self):
        res = self.results()
        res[0] = guest_run(name_of("p7guest"), 1, offered=(30, "TP:armchair"))
        self.assertEqual(E.crit_p77(res)["status"], PASS)

    def test_missing_runs_or_a_short_run_are_incomplete(self):
        self.assertEqual(E.crit_p77(self.results()[:9])["status"], INCOMPLETE)
        res = self.results()
        res[0] = guest_run(name_of("p7guest"), 1, nights=20)
        self.assertEqual(E.crit_p77(res)["status"], INCOMPLETE)

    def test_the_probation_off_job_and_the_seat_nights_are_reported(self):
        res = self.results() + [guest_run(name_of("p7guestnp"), 1, offered=(27, "FP:guest"))]
        c = E.crit_p77(res)
        self.assertEqual(c["details"]["p7guestnp"][0]["first_offered_night"], 27)
        ctl = guest_run(name_of("p7ctl"), 1, offered=(8, "TP:desk"))
        ctlnp = guest_run(name_of("p7ctlnp"), 1, offered=(7, "TP:desk"))
        c = E.crit_p77(res + [ctl, ctlnp])
        seats = c["details"]["reported"]["first_night_each_real_seat_is_offered"]
        self.assertEqual((seats[name_of("p7ctl")][1], seats[name_of("p7ctlnp")][1]), ({"desk": 8}, {"desk": 7}))


# ---------------------------------------------------------------- P7-8
def quiet_run(name, seed, nights=42, items_at=None, zones=True):
    ns = []
    for n in range(1, nights + 1):
        items = []
        if items_at and n == items_at[0]:
            items.append(items_at[1])
        z = [zrec("z0001", "dwell", "desk")] if zones and n >= 7 else []
        d = [drec("z0001", "ok" if n % 5 else "falling", 0.5)] if zones and n >= 7 else []
        ns.append(nrec(n, z, d, items))
    return run(name, seed, ns)


def gated_results(over=None):
    over = over or {}
    out = []
    for job, expected in E.P78_NOMOD_JOBS.items():
        for s in range(1, expected + 1):
            out.append(quiet_run(job, s, **over.get((job, s), {})))
    return out


class P78Test(unittest.TestCase):
    def test_the_54_runs_without_a_false_item_pass(self):
        c = E.crit_p78(gated_results())
        self.assertEqual(c["status"], PASS)
        self.assertEqual(c["details"]["evaluated_runs"], 54)

    def test_a_modify_a_retire_or_a_radar_item_in_the_gated_set_fails(self):
        for it in (irec("mod-z0001", "modify_zone", zid="z0001", sid="s1"), irec("ret-z0001", "retire_zone", zid="z0001"),
                   irec("radar_moved", "radar_moved", tier=1)):
            res = gated_results({(name_of("p7guest"), 2): {"items_at": (20, it)}})
            c = E.crit_p78(res)
            self.assertEqual(c["status"], FAIL, it["id"])
            self.assertEqual(c["details"]["violations"][0][:2], (name_of("p7guest"), 2))

    def test_the_retire_of_the_table_zone_in_p7ret_is_the_intended_output(self):
        r = ret_run(1)
        res = [x for x in gated_results() if not (x["job"] == name_of("p7ret") and x["seed"] == 1)] + [r]
        self.assertEqual(E.crit_p78(res)["status"], PASS)
        r2 = ret_run(1, other=True)
        res = [x for x in gated_results() if not (x["job"] == name_of("p7ret") and x["seed"] == 1)] + [r2]
        self.assertEqual(E.crit_p78(res)["status"], FAIL)

    def test_fewer_than_50_runs_is_incomplete(self):
        res = [r for r in gated_results() if r["job"] not in (name_of("p7tune"), name_of("p7tuneh300"), name_of("p7tuneh900"),
                                                               name_of("p7guestp"))]
        self.assertEqual(E.crit_p78(res)["status"], INCOMPLETE)

    def test_the_move_and_reset_jobs_are_not_gated_for_modifies_but_a_retire_of_the_moved_sofa_is(self):
        res = gated_results() + move_results()[:2]
        self.assertEqual(E.crit_p78(res)["status"], PASS)
        r = move_run(name_of("p7mv11"), 1)
        r["nights"][44]["p7"]["items"].append(irec("ret-z0004", "retire_zone", zid="z0004"))
        c = E.crit_p78(gated_results() + [r])
        self.assertEqual(c["status"], FAIL)
        self.assertTrue(any("retire" in x[2] for x in c["details"]["other_runs"]))

    def test_a_radar_item_in_a_job_that_does_not_allow_it_fails_and_in_p7rot_does_not(self):
        res = gated_results() + [radar_run(name_of("p7rot"), 1), radar_run(name_of("p7rot6"), 1)]
        self.assertEqual(E.crit_p78(res)["status"], PASS)
        res = gated_results() + [radar_run(name_of("p7rot3"), 1)]
        self.assertEqual(E.crit_p78(res)["status"], FAIL)

    def test_merge_items_and_the_falling_share_are_reported(self):
        res = gated_results({(name_of("p7ctl"), 1): {"items_at": (15, irec("mrg-s0004-1", "merge_into", zid="z0001", sid="s0004-1"))}})
        c = E.crit_p78(res)
        self.assertEqual(c["status"], PASS)
        rep = c["details"]["reported"]
        self.assertEqual(rep["merge_items_per_run"]["%s s1" % name_of("p7ctl")], 1)
        self.assertGreater(rep["falling_share_of_unfrozen_nights"]["%s s1" % name_of("p7ctl")]["z0001"], 0)


# ---------------------------------------------------------------- P7-9
def base_line():
    L = make()
    set_run(L, 5)
    return json.loads(tuningagg.nightly(L, 5).text)


def tune_run(name, seed, hs_bins, lines=28, leak=None, cd=14):
    d = base_line()
    d["cd"] = cd
    d["hs"] = list(hs_bins)
    d["hn"] = sum(hs_bins)
    texts = []
    for k in range(lines):
        x = copy.deepcopy(d)
        if leak and k == 3:
            x.update(leak)
        texts.append(json.dumps(x, separators=(",", ":")))
    return run(name, seed, [nrec(n) for n in range(1, 29)], tuning=texts)


SPELLS = {"p7tune": [0, 0, 0, 1, 2, 0, 0, 0, 0, 0], "p7tuneh300": [0, 0, 0, 0, 1, 2, 0, 0, 0, 0], "p7tuneh900": [0, 0, 0, 0, 0, 1, 2, 1, 0, 0]}


class P79Test(unittest.TestCase):
    def results(self, spells=None, **kw):
        sp = spells or SPELLS
        return [tune_run(name_of(v), s, sp[v], **kw) for v in sp for s in (1, 2, 3)]

    def test_clean_lines_and_a_bridge_gap_that_rises_with_the_hidden_spell_mean_pass(self):
        c = E.crit_p79(self.results())
        self.assertEqual(c["status"], PASS, c["counts"])
        got = [r["bridge_gap_s"] for r in c["details"]["bridge_gap_comparison"]]
        self.assertEqual(len(got), 3)
        self.assertTrue(all(g[0] <= g[1] <= g[2] and g[0] != g[2] for g in got), got)

    def test_a_private_value_in_a_line_fails(self):
        c = E.crit_p79(self.results(leak={"x": 120}))
        self.assertEqual(c["status"], FAIL)
        c = E.crit_p79(self.results(leak={"date": "2026-01-05"}))
        self.assertEqual(c["status"], FAIL)

    def test_a_bridge_gap_that_decreases_or_does_not_differ_fails(self):
        flat = {k: SPELLS["p7tune"] for k in SPELLS}
        self.assertEqual(E.crit_p79(self.results(spells=flat))["status"], FAIL)
        back = {"p7tune": SPELLS["p7tuneh900"], "p7tuneh300": SPELLS["p7tuneh300"], "p7tuneh900": SPELLS["p7tune"]}
        self.assertEqual(E.crit_p79(self.results(spells=back))["status"], FAIL)

    def test_too_few_spells_or_covered_dates_make_the_comparison_incomplete(self):
        few = {"p7tune": [0, 0, 0, 1, 0, 0, 0, 0, 0, 0], "p7tuneh300": [0, 0, 0, 0, 1, 0, 0, 0, 0, 0], "p7tuneh900": [0, 0, 0, 0, 0, 1, 0, 0, 0, 0]}
        self.assertEqual(E.crit_p79(self.results(spells=few, lines=5))["status"], INCOMPLETE)
        self.assertEqual(E.crit_p79(self.results(cd=10))["status"], INCOMPLETE)

    def test_missing_runs_or_lines_are_incomplete(self):
        self.assertEqual(E.crit_p79(self.results()[:6])["status"], INCOMPLETE)
        res = self.results()
        res[0]["p7"]["tuning"] = []
        self.assertEqual(E.crit_p79(res)["status"], INCOMPLETE)
        self.assertEqual(E.crit_p79([])["status"], INCOMPLETE)

    def test_the_pooled_record_of_a_run_is_small_and_scans_clean(self):
        row = E._p79_run(self.results()[0])
        self.assertEqual(row["status"], PASS, row.get("problems"))
        self.assertLess(row["pooled_bytes"], E.P7.agg_export_bytes_max)
        self.assertEqual(row["lines"], 28)


# ---------------------------------------------------------------- the report
class ReportTest(unittest.TestCase):
    def full(self):
        res = [ctl_run(name_of("p7ctl"), s) for s in range(1, 6)] + move_results()
        return res

    def test_the_report_has_the_nine_gated_checks_and_the_reported_ones(self):
        rep = E.evaluate_p7(self.full())
        self.assertEqual([c["id"] for c in rep["criteria"]], ["P7-%d" % i for i in (1, 2, 3, 4, 5, 6, 7, 8, 9)])
        self.assertEqual([c["id"] for c in rep["reported"]], ["P7-10", "P7-runs"])
        by = {c["id"]: c["status"] for c in rep["criteria"]}
        self.assertEqual((by["P7-1"], by["P7-2"], by["P7-4"]), (PASS, PASS, PASS))
        self.assertEqual((by["P7-3"], by["P7-5"], by["P7-6"], by["P7-7"], by["P7-9"]), (INCOMPLETE,) * 5)
        self.assertEqual(rep["gate"], INCOMPLETE)
        self.assertEqual(rep["failed"], [])

    def test_no_p7_run_gives_no_p7_part(self):
        self.assertIsNone(E.evaluate_p7([]))
        self.assertIsNone(E.evaluate_p7([{"job": "bed_w0000_0600_p6hold", "seed": 1, "job_spec": {"script": "p6hold"}}]))

    def test_a_failure_fails_the_gate_and_the_exit_code(self):
        res = self.full()
        res[0] = ctl_run(name_of("p7ctl"), 1, bed_frozen=False)
        rep = E.build_report(res)
        self.assertEqual(rep["failed"], ["P7-1"])
        self.assertEqual(rep["gate"], FAIL)
        self.assertEqual(rep["exit_code"], 1)
        self.assertEqual(rep["kind"], "p7")

    def test_a_run_that_is_not_ok_is_listed_and_the_exit_code_is_1(self):
        res = self.full()
        res[1]["status"] = "over_budget"
        rep = E.build_report(res)
        self.assertEqual(rep["bad_runs"][0]["seed"], 2)
        self.assertEqual(rep["exit_code"], 1)

    def test_the_markdown_has_every_check_the_evidence_and_the_commands(self):
        text = E.render_markdown(E.build_report(self.full()))
        for needle in ("P7-1", "P7-9", "## P7 reported", "## Evidence: unit tests", "## Separate commands", "P7-R1", "P7-R2", "P7-R3",
                       "## P7 interpretations"):
            self.assertIn(needle, text)

    def test_write_report_writes_json_and_markdown_with_lf(self):
        from pathlib import Path
        with tmpdir() as d:
            E.write_report(E.build_report(self.full()), d)
            self.assertEqual(json.loads(Path(d, "report.json").read_text(encoding="utf-8"))["kind"], "p7")
            self.assertNotIn(b"\r", Path(d, "report.md").read_bytes())

    def test_the_job_tables_agree_with_the_profiles(self):
        runs = {(j.name, s) for p in ("p7move",) for j, s in J.runs_of(J.PROFILES[p])}
        want = {(job, s) for job, n in E.P72_JOBS.items() for s in range(1, n + 1)}
        self.assertEqual(runs, want)
        gated = {(j.name, s) for p in ("p7drift", "p7beds", "p7guests", "p7tune") for j, s in J.runs_of(J.PROFILES[p])}
        self.assertEqual(gated, {(job, s) for job, n in E.P78_NOMOD_JOBS.items() for s in range(1, n + 1)})
        self.assertEqual(sum(E.P78_NOMOD_JOBS.values()), 54)

    def test_the_helpers(self):
        self.assertEqual(E._gate([{"status": PASS}] * 4 + [{"status": FAIL}], 5), PASS)
        self.assertEqual(E._gate([{"status": PASS}] * 3 + [{"status": FAIL}] * 2, 5), FAIL)
        self.assertEqual(E._gate([{"status": PASS}] * 3, 5), INCOMPLETE)
        self.assertEqual(E._gate([{"status": PASS}] * 3, 3), PASS)
        self.assertEqual(E._gate([{"status": PASS}] * 4 + [{"status": FAIL}], 5, deterministic=True), FAIL)
        self.assertEqual(E._gate([{"status": PASS}] * 4, 5, deterministic=True), INCOMPLETE)
        self.assertEqual(E._combine([PASS, INCOMPLETE]), INCOMPLETE)
        self.assertEqual(E._combine([PASS, FAIL, INCOMPLETE]), FAIL)
        self.assertEqual(E._combine([]), INCOMPLETE)
        self.assertEqual(E.iou({1, 2}, {2, 3}), 1 / 3)
        self.assertEqual(E.overlap_coef({1, 2}, {2, 3, 4}), 0.5)
        self.assertAlmostEqual(E._expected_shift(3.4, 0.0), 0.5927, 3)


if __name__ == "__main__":
    unittest.main()
