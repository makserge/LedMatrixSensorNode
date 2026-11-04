"""Evaluation of the P5 checks from synthetic run results (P5 spec 11.2, 11.6): PASS and FAIL for each check, INCOMPLETE for
missing runs and steps that were not exercised, the sub-check rule, the report and its separate-commands section."""
import json
import unittest

from replay import evaluate as E
from replay import evaluate_p5 as E5
from replay import world as W
from replay.truth import rle
from tests._tmp import tmpdir
from tests.test_replay_p4 import _app_actor, mk_p4_run

P, F, I, R = E.PASS, E.FAIL, E.INCOMPLETE, E.REPORTED
DAY_MS = 86400 * 1000
PUSH_FIELDS = ("seq", "acked", "kind", "count", "head_id", "head_rev", "acks")


def mk(script, seed=1, window="w0000_0600", nights=14, actors=None, p5=None, status="ok", recs=None, errors=0):
    """A synthetic result of a P5 job: night records with the keys the evaluation reads."""
    key = "p5" + script[2:] if script.startswith("p5") else script
    job = "bed_%s_%s" % (window, key)
    if recs is None:
        recs = [{"night": n, "candidates": 0, "suggestions": [], "p4": {"items": {}}, "p5": {}} for n in range(nights + 1)]
    return {"job": job, "world": "bed", "window": window, "variant": key, "gated": False, "seed": seed, "params": "x",
            "status": status, "core_errors": errors, "first_error": None,
            "job_spec": {"script": script, "window": "00:00-06:00" if window != "none" else None},
            "timing": {"cpu_s": 30.0, "wall_s": 31.0, "sim_days": nights + 0.15, "cpu_s_per_day": 2.0},
            "nights": recs, "truth": {"cut": {}, "core": []}, "labels": {}, "capture": {"seated": None},
            "counters": {}, "stream": {}, "p4": {"script": script, "actors": actors or {}},
            "p5": p5 if p5 is not None else {"push_log": [], "ignored_log": [], "saves": {}, "push_problems": [],
                                              "push_checked": 0, "push_fields": list(PUSH_FIELDS), "restarts": []}}


# ---------------------------------------------------------------- P5-1
def step(key, ok=True, **kw):
    d = {"step": key, "exercised": True, "ok": ok}
    d.update(kw)
    return d


def answer_steps(places=True, window=True, bed=True, bad=()):
    keys = []
    if places:
        keys += list(E5.PLACE_STEPS)
    if window:
        keys += list(E5.WINDOW_STEPS)
    if bed:
        keys += list(E5.BED_STEPS)
    return {"answer_script": {"steps": [step(k, k not in bad) for k in keys], "not_exercised": [], "rev_moves": [{"night": 9, "ids": ["s1"]}],
                              "done": True}}


class P51Test(unittest.TestCase):
    def runs(self, **kw):
        return [mk("p5ans", seed=s, actors=answer_steps(**kw)) for s in (1, 2, 3)]

    def test_every_exercised_step_in_every_seed_passes(self):
        c = E5.crit_p51(self.runs())
        self.assertEqual(c["status"], P, c["details"])
        self.assertEqual(c["counts"]["stale_place"], "3 of 3 seeds exercised")

    def test_a_failed_step_fails(self):
        res = self.runs()
        res[1]["p4"]["actors"] = answer_steps(bad=("redelivery",))
        c = E5.crit_p51(res)
        self.assertEqual(c["status"], F)
        self.assertEqual(c["details"]["seeds"][1]["failed"], ["redelivery"])

    def test_window_and_bed_steps_that_could_not_be_exercised_do_not_make_it_incomplete(self):
        c = E5.crit_p51(self.runs(window=False, bed=False))
        self.assertEqual(c["status"], P)
        self.assertEqual(sorted(c["details"]["steps_not_exercised_in_any_seed"]), sorted(E5.WINDOW_STEPS + E5.BED_STEPS))

    def test_no_seed_with_the_place_steps_is_incomplete(self):
        self.assertEqual(E5.crit_p51(self.runs(places=False))["status"], I)
        self.assertEqual(E5.crit_p51([])["status"], I)

    def test_a_seed_without_the_place_steps_next_to_one_with_them_passes(self):
        res = self.runs()
        res[2]["p4"]["actors"] = answer_steps(places=False)
        self.assertEqual(E5.crit_p51(res)["status"], P)

    def test_a_run_that_did_not_finish_is_incomplete(self):
        res = self.runs()
        res[0]["status"] = "over_budget"
        self.assertEqual(E5.crit_p51(res)["status"], I)


# ---------------------------------------------------------------- P5-2 (b), P5-3 (c)
def req(t, seq, kind="setup", acked=0, acks=(), after=None, count=6, head="s0004-1"):
    return {"t": t, "seq": seq, "kind": kind, "acked": acked, "acks": list(acks), "count": count, "head_id": head,
            "head_rev": 1, "after_restart": after}


T0 = 7 * DAY_MS + 8 * 3600 * 1000


def push_flow():
    return [req(T0 + 884, 1, after="R1"), req(T0 + 10_884, 1, after="R2"),
            req(T0 + 20_884, 1, acked=1, acks=["fake"], after="R2"), req(T0 + 30_884, 1, acked=1, acks=["fake"], after="R3"),
            req(T0 + DAY_MS, 2, "daily", acked=1, after="R3"), req(T0 + DAY_MS + 20_000, 2, "daily", 2, ["fake"], after="R3")]


def push_run(log=None, points=("R1", "R2", "R3"), setup_sent=True, nights=16):
    log = push_flow() if log is None else log
    recs = [{"night": n, "suggestions": [], "p4": {}, "p5": {"p5": {"push": {"setup_sent": setup_sent if n > 7 else False}}}}
            for n in range(nights + 1)]
    rs = [{"point": p, "t": T0 - 500 + 10_000 * i, "seq_before": 0 if p == "R1" else 1, "night": 7} for i, p in enumerate(points)]
    return mk("p5push", nights=nights, actors={"push_watcher": {"log": log, "restarts": rs, "acks_sent": {}, "done": False}}, recs=recs)


class P52Test(unittest.TestCase):
    def test_the_flow_passes(self):
        c = E5.crit_p52([push_run(), push_run()])
        self.assertEqual(c["status"], P, c["details"])

    def test_two_setup_requests_fail(self):
        log = push_flow() + [req(T0 + 2 * DAY_MS, 3, "setup")]
        c = E5.crit_p52([push_run(log)])
        self.assertEqual(c["status"], F)
        self.assertIn("setup requests with seq [1, 3]", c["details"]["seeds"][0]["note"])

    def test_the_interrupted_request_must_come_again_with_the_same_seq(self):
        log = push_flow()
        log[1] = req(T0 + 10_884, 2, after="R2")                    # R2 made a second seq
        c = E5.crit_p52([push_run(log)])
        self.assertEqual(c["status"], F)
        self.assertIn("after R2", c["details"]["seeds"][0]["note"])
        log = push_flow()
        log[1] = req(T0 + 10_884, 1, acked=1, acks=["fake"], after="R2")      # already acknowledged: it was not interrupted
        self.assertEqual(E5.crit_p52([push_run(log)])["status"], F)

    def test_the_acknowledgement_must_survive_r3(self):
        log = push_flow()
        log[3] = req(T0 + 30_884, 1, acked=0, acks=[], after="R3")
        self.assertEqual(E5.crit_p52([push_run(log)])["status"], F)

    def test_acked_must_not_decrease_and_seq_must_not_go_down(self):
        log = push_flow()
        log[4] = req(T0 + DAY_MS, 2, "daily", acked=0, after="R3")
        log[3] = req(T0 + 30_884, 1, acked=1, acks=["fake"], after="R3")
        log[2] = req(T0 + 20_884, 1, acked=1, acks=["fake"], after="R2")
        log.insert(4, req(T0 + 40_000, 1, acked=0, after="R3"))
        self.assertEqual(E5.crit_p52([push_run(log)])["status"], F)

    def test_at_most_one_daily_request_a_day(self):
        log = push_flow() + [req(T0 + DAY_MS + 3600_000, 3, "daily")]
        c = E5.crit_p52([push_run(log)])
        self.assertEqual(c["status"], F)
        self.assertIn("more than one daily request", c["details"]["seeds"][0]["note"])

    def test_a_request_emitted_again_on_a_later_day_is_not_a_new_request(self):
        # the unacknowledged daily request of day 8 changes its count on day 9 (the same seq): one request, not two on day 9
        log = push_flow() + [req(T0 + 2 * DAY_MS, 2, "daily", acked=2, acks=["fake"], count=8),
                                  req(T0 + 2 * DAY_MS + 5, 3, "daily", acked=2)]
        self.assertEqual(E5.crit_p52([push_run(log)])["status"], P)
        log = push_flow() + [req(T0 + 2 * DAY_MS, 3, "daily"), req(T0 + 2 * DAY_MS + 60_000, 4, "daily")]
        self.assertEqual(E5.crit_p52([push_run(log)])["status"], F)

    def test_setup_sent_must_stay_true(self):
        self.assertEqual(E5.crit_p52([push_run(setup_sent=False)])["status"], F)

    def test_a_missing_restart_point_or_request_is_incomplete(self):
        self.assertEqual(E5.crit_p52([push_run(points=("R1", "R2"))])["status"], I)
        self.assertEqual(E5.crit_p52([push_run(log=[])])["status"], I)
        self.assertEqual(E5.crit_p52([])["status"], I)


class P53Test(unittest.TestCase):
    def p5(self, checked=4, problems=()):
        return {"push_log": [], "ignored_log": [], "saves": {}, "push_problems": list(problems), "push_checked": checked,
                "push_fields": list(PUSH_FIELDS), "restarts": []}

    def test_clean_requests_pass(self):
        c = E5.crit_p53([mk("p5setup", p5=self.p5()), mk("p5ans", p5=self.p5(2))])
        self.assertEqual((c["status"], c["details"]["requests_checked"]), (P, 6))

    def test_a_text_or_name_field_fails(self):
        c = E5.crit_p53([mk("p5setup", p5=self.p5(problems=[{"t": 1, "problems": ["head_id='Desk'"]}]))])
        self.assertEqual(c["status"], F)
        bad = self.p5()
        bad["push_fields"] = ["seq", "acked", "kind", "count", "head_id", "head_rev", "acks", "text"]
        self.assertEqual(E5.crit_p53([mk("p5setup", p5=bad)])["status"], F)

    def test_no_request_at_all_says_nothing(self):
        self.assertEqual(E5.crit_p53([mk("p5setup", p5=self.p5(0))])["status"], I)
        self.assertEqual(E5.crit_p53([])["status"], I)


# ---------------------------------------------------------------- P5-4
def setup_actor(taps=7, places=5, failed=1, started=True, done=True, mark_ok=True):
    return {"setup_user": {"started": started, "done": done, "taps": taps, "places": places, "failed_marks": failed,
                           "items_at_start": ["night_window", "bed_needs_data"], "day": 7, "window_approved": True,
                           "mark": {"ok": mark_ok}, "mark_reasons": {"no_stay": failed}, "text_entries": 0}}


class P54Test(unittest.TestCase):
    def runs(self, taps):
        return [mk("p5setup", seed=s + 1, actors=setup_actor(taps=t, places=5)) for s, t in enumerate(taps)]

    def test_four_of_five_within_eight_taps_pass(self):
        c = E5.crit_p54(self.runs([7, 8, 6, 9, 8]))
        self.assertEqual(c["status"], P)
        self.assertEqual(c["counts"]["taps <= 8"], "4 of 5 seeds PASS (need 4): PASS")
        self.assertIn("0 of 1 misses have more than 5 places", c["details"]["note"])

    def test_three_of_five_fail(self):
        c = E5.crit_p54(self.runs([7, 8, 6, 9, 12]))
        self.assertEqual(c["status"], F)

    def test_the_limit_is_not_moved_for_a_seed_with_more_than_five_places(self):
        res = self.runs([7, 8, 6, 8, 11])
        res[4]["p4"]["actors"] = setup_actor(taps=11, places=7)
        c = E5.crit_p54(res)
        self.assertEqual(c["status"], P)                                         # 4 of 5
        self.assertTrue(c["details"]["seeds"][4]["more_than_5_places"])
        self.assertEqual(c["details"]["seeds"][4]["status"], F)
        self.assertIn("1 of 1 misses have more than 5 places", c["details"]["note"])

    def test_no_item_by_night_14_is_incomplete(self):
        res = self.runs([7, 8, 6, 8, 6])
        res[0]["p4"]["actors"] = setup_actor(started=False, done=False, taps=0)
        res[1]["p4"]["actors"] = setup_actor(started=True, done=False, taps=3)
        c = E5.crit_p54(res)
        self.assertEqual(c["status"], I)                                          # 3 PASS < need 4, nothing can fail it
        self.assertIn("no review item by night", c["details"]["seeds"][0]["note"])
        self.assertEqual(E5.crit_p54([])["status"], I)

    def test_fewer_seeds_need_all_of_them(self):
        self.assertEqual(E5.crit_p54(self.runs([7]))["status"], P)
        self.assertEqual(E5.crit_p54(self.runs([7, 9]))["status"], F)
        self.assertEqual(E5.crit_p54(self.runs([7, 8, 6]))["status"], P)

    def test_the_taps_of_the_walk_through_are_reported_next_to_the_gated_ones(self):
        res = [mk("p5setup", actors=setup_actor(taps=49, places=4, failed=40))]          # the window item was open
        self.assertEqual(E5.crit_p54(res)["details"]["seeds"][0]["taps_if_the_first_press_succeeded"], 6)
        a = setup_actor(taps=49, places=5, failed=40)
        a["setup_user"]["window_approved"] = False
        self.assertEqual(E5.rep_p5_taps([mk("p5setup", actors=a)])["details"][0]["taps_if_the_first_press_succeeded"], 6)
        self.assertIsNone(E5._taps_if_first_press_ok({"places": None}))

    def test_taps_without_failed_presses_are_reported(self):
        res = [mk("p5setup", actors=setup_actor(taps=49, places=4, failed=40))]
        c = E5.crit_p54(res)
        self.assertEqual(c["details"]["seeds"][0]["taps_without_failed_marks"], 9)
        rep = E5.rep_p5_taps(res)
        self.assertEqual(rep["details"][0]["taps_without_failed_marks"], 9)
        self.assertEqual(rep["status"], R)


# ---------------------------------------------------------------- P5-5
BED_TEXT = ("Bed zone: lie in bed and press Mark spot here once, before 00:00 or after 06:00 (recommended; no extra "
            "radar-on nights). Otherwise the learner finds it by itself.")


def bed_recs(text=BED_TEXT, start=0, end=360, show=True, nights=(7, 8, 9), extra=None, total=10):
    recs = []
    for n in range(total + 1):
        items = []
        if n in nights:
            d = {"start_min": start, "end_min": end, "sched": True if show else False, "show_window": show, "text": text}
            d.update(extra or {})
            items.append({"id": "bed_needs_data", "rev": 0, "type": "bed_needs_data", "tier": 1, "tag": "", "data": d})
        recs.append({"night": n, "suggestions": [], "p4": {"items": {}}, "p5": {"items": items}})
    return recs


def zv(kind="bed", src="mark", frozen=True, name="Bed"):
    return {"id": "l0001" if src == "mark" else "z0001", "name": name, "kind": kind, "src": src, "ref": None, "rev": 0,
            "cx": -2.3, "cy": 4.9, "cells": 12, "poly_cm": [], "frozen": frozen, "night": 8}


def mark_actor(kind="bed", views=None, manual_kinds=("manual",), manual_ok=True):
    rb = {"night": 8, "items": ["night_window"], "bed_item": {"state": "withdrawn", "reason": "bed_zone", "rev": 0},
          "bed": {"zones": 1, "frozen": True}, "labels": 2, "bed_places": [{"id": "s1", "frozen": True}],
          "zone_views": [zv()] if views is None else views}

    def fail(key, reason):
        return {"step": key, "skipped": False, "note": None, "attempts": 1, "reasons": {reason: 1},
                "last": {"t": 1, "ok": False, "reason": reason, "kind": None, "labels_before": 0, "labels_after": 0,
                         "bed_item": {"state": "open"}, "bed": {"zones": 0, "frozen": False}, "night": 8}, "readback": None}
    manual = {"step": "manual", "skipped": False, "note": None, "attempts": 3, "reasons": {"no_stay": 2, "ok": 1} if manual_ok else {"no_stay": 3},
              "last": {"t": 2, "ok": manual_ok, "reason": None, "kind": "manual", "labels_before": 0, "labels_after": 1,
                       "bed_item": {"state": "open"}, "night": 8, "zone_kinds": list(manual_kinds) if manual_kinds is not None else None},
              "readback": None}
    return {"mark": {"steps": [fail("not_observing", "not_observing"), fail("no_stay", "no_stay"),
                               fail("several_stays", "several_stays"), manual], "unfinished": [],
                      "bed_mark": {"night": 8, "t": 3, "kind": kind, "attempts": 7, "readback": rb}}}


def bed_steps(**bad):
    keys = ["bed_later", "bed_back", "bed_reject", "bed_away", "bed_undo"]
    return {"bed_answers": {"steps": [step(k, k not in bad) for k in keys], "phase": "done", "done": True}}


class P55Test(unittest.TestCase):
    def test_every_sub_check_passes(self):
        res = [mk("p5setup", seed=s, recs=bed_recs()) for s in (1, 2)] + [mk("p5setup", window="none", recs=bed_recs(
            text="Bed zone: lie in bed and press Mark spot here once (recommended; no extra radar-on nights).", show=False,
            start=0, end=360))]
        res += [mk("p5adv", seed=s, actors=bed_steps()) for s in (1, 2, 3)]
        res += [mk_p4_run("mark", seed=s, nights=12, actors=mark_actor(), zone_night=8) for s in (1,)]
        res[-1]["nights"] = bed_recs(nights=(7,), total=12)
        res += [mk_p4_run("app", nights=16, actors={"place_app": dict(_app_actor()["place_app"], after=dict(
            _app_actor()["place_app"]["after"], zone_views=[zv("bed", "approve", True, "Bed")]))})]
        c = E5.crit_p55(res)
        self.assertEqual(c["status"], P, {k: [x["status"] for x in v] for k, v in c["details"].items()})
        self.assertEqual(sorted(c["counts"]), ["5a_text", "5b_bed_zone", "5c_manual", "5d_later", "5e_no_bed_in_view",
                                               "5f_undo", "5g_failures", "5h_times"])

    def test_5a_text_and_night_learning(self):
        for text, extra in (("Press Mark spot here once.", None), (BED_TEXT + " Night learning", None),
                            (BED_TEXT, {"night_learning": True})):
            c = E5.crit_p55([mk("p5setup", recs=bed_recs(text=text, extra=extra))])
            self.assertEqual(c["details"]["5a_text"][0]["status"], F, (text, extra))
        self.assertEqual(E5.crit_p55([mk("p5setup", recs=bed_recs(nights=()))])["details"]["5a_text"][0]["status"], I)

    def test_5b_the_press_in_bed_makes_a_frozen_bed_label(self):
        res = [mk_p4_run("mark", nights=12, actors=mark_actor(), zone_night=8)]
        self.assertEqual(E5.crit_p55(res)["details"]["5b_bed_zone"][0]["status"], P)
        res = [mk_p4_run("mark", nights=12, actors=mark_actor(views=[zv(frozen=False)]), zone_night=8)]
        self.assertEqual(E5.crit_p55(res)["details"]["5b_bed_zone"][0]["status"], F)
        res = [mk_p4_run("mark", nights=12, actors=mark_actor(views=[zv(kind="manual")]), zone_night=8)]
        self.assertEqual(E5.crit_p55(res)["details"]["5b_bed_zone"][0]["status"], F)
        res = [mk_p4_run("mark", nights=12, actors=mark_actor(kind="manual"), zone_night=8)]
        self.assertEqual(E5.crit_p55(res)["details"]["5b_bed_zone"][0]["status"], F)
        a = mark_actor()
        a["mark"]["bed_mark"]["readback"].pop("zone_views")
        self.assertEqual(E5.crit_p55([mk_p4_run("mark", nights=12, actors=a)])["details"]["5b_bed_zone"][0]["status"], I)

    def test_5b_approving_the_place_as_bed(self):
        a = _app_actor()
        a["place_app"]["after"]["zone_views"] = [zv("bed", "approve", True)]
        self.assertEqual(E5.crit_p55([mk_p4_run("app", nights=16, actors=a)])["details"]["5b_bed_zone"][0]["status"], P)
        a["place_app"]["after"]["zone_views"] = [zv("dwell", "approve", True)]
        self.assertEqual(E5.crit_p55([mk_p4_run("app", nights=16, actors=a)])["details"]["5b_bed_zone"][0]["status"], F)

    def test_5c_another_item_selected_gives_manual(self):
        row = lambda **kw: E5.crit_p55([mk_p4_run("mark", nights=12, actors=mark_actor(**kw))])["details"]["5c_manual"][0]
        self.assertEqual(row()["status"], P)
        self.assertEqual(row(manual_kinds=("bed",))["status"], F)
        self.assertEqual(row(manual_kinds=None)["status"], I)
        self.assertEqual(row(manual_ok=False)["status"], I)

    def test_5d_5e_5f_come_from_the_bed_actor(self):
        res = [mk("p5adv", seed=s, actors=bed_steps()) for s in (1, 2, 3)]
        d = E5.crit_p55(res)["details"]
        self.assertEqual([x["status"] for k in ("5d_later", "5e_no_bed_in_view", "5f_undo") for x in d[k]], [P] * 9)
        res[0]["p4"]["actors"] = bed_steps(bed_undo=True)
        res[1]["p4"]["actors"] = {"bed_answers": {"steps": [step("bed_later"), step("bed_back")], "phase": "back", "done": False}}
        d = E5.crit_p55(res)["details"]
        self.assertEqual(d["5f_undo"][0]["status"], F)
        self.assertEqual(d["5e_no_bed_in_view"][1]["status"], I)
        self.assertEqual(d["5d_later"][1]["status"], P)

    def test_5g_failures_leave_the_item_open(self):
        a = mark_actor()
        a["mark"]["steps"][1]["last"]["labels_after"] = 1                         # a failed press made a label
        c = E5.crit_p55([mk_p4_run("mark", nights=12, actors=a)])
        self.assertEqual(c["details"]["5g_failures"][0]["status"], F)

    def test_5h_the_times_of_the_window_and_none_while_the_schedule_is_off(self):
        d = E5.crit_p55([mk("p5setup", recs=bed_recs())])["details"]["5h_times"][0]
        self.assertEqual(d["status"], P, d)
        d = E5.crit_p55([mk("p5setup", recs=bed_recs(text=BED_TEXT.replace("00:00", "23:30"), start=1410))])["details"]["5h_times"][0]
        self.assertEqual(d["status"], F)                                          # the node's window is not 00:00-06:00
        off = bed_recs(text="Bed zone: lie in bed and press Mark spot here once (recommended; no extra radar-on nights).", show=False)
        self.assertEqual(E5.crit_p55([mk("p5setup", window="none", recs=off)])["details"]["5h_times"][0]["status"], P)
        bad = bed_recs(text="Bed zone, before 23:30 or after 06:30 (recommended; no extra radar-on nights). press Mark spot here once",
                       show=False)
        self.assertEqual(E5.crit_p55([mk("p5setup", window="none", recs=bad)])["details"]["5h_times"][0]["status"], F)

    def test_the_p4_record_is_used_for_a_job_without_the_p5_record(self):
        recs = [{"night": n, "suggestions": [], "p4": {"items": {"bed_needs_data": {"rev": 0, "data": {"text": BED_TEXT}}}}}
                for n in range(5)]
        row = E5._sub_5a(mk_p4_run("mark", nights=4) | {"nights": recs})
        self.assertEqual(row["status"], P)


class P4RerunTest(unittest.TestCase):
    def test_the_three_p4_jobs_are_read_with_the_p4_rules(self):
        res = [mk_p4_run("mark", nights=12, actors=mark_actor(), zone_night=8),
               mk_p4_run("app", nights=16, actors=_app_actor())]
        rr = E5.p4_reruns(res)
        self.assertEqual(rr["status"], P, rr["counts"])
        self.assertIn("P4-4 manual", rr["counts"])
        self.assertIn("P4-1", rr["not_run"])
        self.assertIsNone(E5.p4_reruns([mk("p5setup")]))

    def test_a_p4_failure_fails_the_rerun(self):
        a = mark_actor()
        a["mark"]["steps"][2]["last"]["labels_after"] = 1
        rr = E5.p4_reruns([mk_p4_run("mark", nights=12, actors=a)])
        self.assertEqual(rr["status"], F)

    def test_the_reject_run_is_checked_when_exercised(self):
        rej = {"place_rej": {"night": 12, "suggestion": "s", "name_hint": "bed",
                             "before": {"bed_item": {"state": "withdrawn"}}, "after": {"bed_item": {"state": "withdrawn"}},
                             "done": True}}
        rr = E5.p4_reruns([mk_p4_run("rej", nights=16, actors=rej)])
        self.assertEqual(rr["status"], F)
        rej["place_rej"]["after"]["bed_item"]["state"] = "open"
        self.assertNotEqual(E5.p4_reruns([mk_p4_run("rej", nights=16, actors=rej)])["status"], F)


# ---------------------------------------------------------------- P5-6
MASK_CELLS = [(10 * 70 + 10 + k) for k in range(3)] + [(11 * 70 + 10 + k) for k in range(3)]
OTHER_CELLS = [(30 * 70 + 30 + k) for k in range(4)]
CX, CY = W.cell_xy(10 * 70 + 11)


def sug(sid, cells, state="shadow", rev=1, typ="dwell_zone"):
    return {"id": sid, "rev": rev, "type": typ, "kind": "dwell", "name_hint": None, "state": state, "first_night": 4,
            "last_night": 9, "cx": CX, "cy": CY, "cells_rle": rle(cells), "conf": 0.8, "iou": {}}


def rej_recs(nights=21, reject=8, undo=18, open_after=None, rev_change=False, stale_leak=False, back=True,
             after_undo=False):
    """``after_undo``: the rev change and the status leak of ``rev_change`` and ``stale_leak`` happen only after the
    night of the undo (the suppression is gone then and the suggestion is an ordinary one)."""
    recs = []
    for n in range(nights + 1):
        late = n > undo if after_undo else True
        sugs = [sug("s0005-1", MASK_CELLS, state="stale" if n >= reject + 7 else "shadow",
                    rev=2 if (rev_change and n > reject + 2 and late) else 1), sug("s0006-1", OTHER_CELLS)]
        items = [{"id": "s0006-1", "rev": 1, "type": "dwell_zone", "tier": 2, "tag": "B"}] if n >= reject else []
        if back and n > undo:
            sugs.append(sug("s0009-1", MASK_CELLS[:4], typ="dwell_zone"))
            items.append({"id": "s0009-1", "rev": 1, "type": "dwell_zone", "tier": 2, "tag": "C"})
        if open_after is not None and reject < n <= undo:
            sugs.append(sug("s0010-1", MASK_CELLS[1:5]))
            items.append({"id": "s0010-1", "rev": 1, "type": "dwell_zone", "tier": 2, "tag": "D"})
        p5 = {"items": items, "status_sugg_ids": (["s0005-1"] if stale_leak and n >= reject + 7 and late else []),
              "map_sugg_ids": []}
        recs.append({"night": n, "suggestions": sugs, "p4": {"items": {}}, "p5": p5})
    return recs


def reject_actor(reject=8, undo=18, supp=None, mask=True, ignored=(), undone=True, undo_state=None):
    rejected = {"id": "s0005-1", "rev": 1, "night": reject, "t": 1, "tag": "A", "cells_rle": rle(MASK_CELLS), "cx": CX, "cy": CY,
                "conf": 0.8, "ignored": list(ignored), "saves": 1,
                "suppressions": [{"type": "place", "night": reject, "ref": "s0005-1"}] if supp is None else supp,
                "mask": {"cx": CX, "cy": CY, "cells": rle(MASK_CELLS), "n0": 18} if mask else None,
                "still_listed": False, "advanced": ["adv:undo:s0005-1"]}
    und = {"night": undo, "t": 2, "ignored": [], "suppressions": 0, "mask": False, "advanced": []}
    if undo_state:
        und.update(undo_state)
    return {"reject_user": {"rejected": rejected, "undone": und if undone else None, "undo_night": 18, "done": undone}}


class P56Test(unittest.TestCase):
    def runs(self, **kw):
        akw = {k: kw.pop(k) for k in ("supp", "mask", "ignored", "undone", "undo_state") if k in kw}
        return [mk("p5rej", seed=s, nights=21, actors=reject_actor(**akw), recs=rej_recs(**kw)) for s in (1, 2)]

    def test_all_three_parts_pass(self):
        c = E5.crit_p56(self.runs())
        self.assertEqual(c["status"], P, c["details"])
        self.assertEqual(c["details"]["seeds"][0]["back"], {"night": 19, "item": "s0009-1"})

    def test_the_suppression_must_keep_the_p4_form_and_the_mask_must_exist(self):
        self.assertEqual(E5.crit_p56(self.runs(supp=[{"type": "place", "night": 8, "ref": "s0005-1", "extra": 1}]))["status"], F)
        self.assertEqual(E5.crit_p56(self.runs(mask=False))["status"], F)
        self.assertEqual(E5.crit_p56(self.runs(ignored=[("s0005-1", "reject", "stale")]))["status"], F)

    def test_an_open_item_overlapping_the_mask_fails(self):
        c = E5.crit_p56(self.runs(open_after=True))
        self.assertEqual(c["status"], F)
        self.assertIn("overlap_coef", c["details"]["seeds"][0]["note"])

    def test_the_rejected_suggestion_must_never_change_its_rev_and_leave_status_and_map_once_stale(self):
        self.assertEqual(E5.crit_p56(self.runs(rev_change=True))["status"], F)
        self.assertEqual(E5.crit_p56(self.runs(stale_leak=True))["status"], F)

    def test_a_rev_change_or_a_status_entry_after_the_night_of_the_undo_is_not_a_violation(self):
        # the rejection is gone after the undo: the suggestion is an ordinary one again (spec P5-6 (2) and (3))
        self.assertEqual(E5.crit_p56(self.runs(rev_change=True, after_undo=True))["status"], P)
        self.assertEqual(E5.crit_p56(self.runs(stale_leak=True, after_undo=True))["status"], P)
        self.assertEqual(E5.crit_p56(self.runs(rev_change=True, stale_leak=True))["status"], F)     # before it: still one

    def test_the_undo_must_bring_a_place_back_within_three_nights(self):
        c = E5.crit_p56(self.runs(back=False))
        self.assertEqual(c["status"], F)
        self.assertIn("(3)", c["details"]["seeds"][0]["note"])
        self.assertEqual(E5.crit_p56(self.runs(undo_state={"suppressions": 1}))["status"], F)

    def test_not_exercised_is_incomplete(self):
        self.assertEqual(E5.crit_p56(self.runs(undone=False, back=False))["status"], I)
        self.assertEqual(E5.crit_p56(self.runs(undone=False))["status"], F)             # an item on the mask and no undo
        r = mk("p5rej", nights=21, actors={"reject_user": {"rejected": None, "undone": None, "done": False}}, recs=rej_recs())
        self.assertEqual(E5.crit_p56([r, r])["status"], I)
        self.assertEqual(E5.crit_p56([])["status"], I)

    def test_a_run_that_ends_before_three_nights_after_the_undo_is_incomplete(self):
        res = [mk("p5rej", nights=19, actors=reject_actor(), recs=rej_recs(nights=19, back=False))]
        self.assertEqual(E5.crit_p56(res)["status"], I)


# ---------------------------------------------------------------- P5-7
def zone(zid, name, kind="dwell", src="approve", ref="s1", rev=0, poly=((100, 100), (120, 100), (120, 140)), frozen=False):
    return {"id": zid, "name": name, "kind": kind, "src": src, "ref": ref, "rev": rev, "cx": 1.0, "cy": 1.0, "cells": 4,
            "poly_cm": [list(p) for p in poly], "frozen": frozen, "night": 7}


def snap(label, zones, n=3):
    return {"label": label, "t": 1, "night": 7, "zones": zones, "next_zone_n": n}


def zone_actor(bad=(), todo=(), mutate=None):
    z1, z2, z3 = zone("z0001", "Desk"), zone("z0002", "desk 2", ref="s2"), zone("z0003", "Bett", "bed", ref="s3", frozen=True)
    z1r = dict(z1, name="Office", rev=1)
    snaps = [snap("before_desk", [], 1), snap("after_desk", [z1], 2), snap("before_desk2", [z1], 2),
             snap("after_desk2", [z1, z2], 3), snap("before_rename", [z1, z2], 3), snap("after_rename", [z1r, z2], 3),
             snap("before_bett", [z1r, z2], 3), snap("after_bett", [z1r, z2, z3], 4), snap("before_remove", [z1r, z2, z3], 4),
             snap("after_remove", [z1r, z2], 4)]
    snaps = json.loads(json.dumps(snaps))                    # independent copies (no shared dicts), as in a real run
    if mutate:
        mutate(snaps)
    keys = ["approve_desk", "approve_desk2", "rename", "approve_bett", "remove"]
    return {"zone_user": {"tasks": [{"task": k, "ok": k not in bad, "night": 7} for k in keys if k not in todo],
                          "snapshots": snaps, "todo": list(todo), "done": not todo}}


class P57Test(unittest.TestCase):
    def runs(self, **kw):
        return [mk("p5zone", seed=s, nights=16, actors=zone_actor(**kw)) for s in (1, 2)]

    def test_passes(self):
        c = E5.crit_p57(self.runs())
        self.assertEqual(c["status"], P, c["details"])

    def test_a_failed_task_fails(self):
        c = E5.crit_p57(self.runs(bad=("rename",)))
        self.assertEqual(c["status"], F)
        self.assertIn("rename", c["details"]["seeds"][0]["failed"])

    def test_tasks_not_reached_are_incomplete(self):
        self.assertEqual(E5.crit_p57(self.runs(todo=("approve_bett", "remove")))["status"], I)
        self.assertEqual(E5.crit_p57([])["status"], I)

    def test_invariants_over_the_snapshots(self):
        def names(snaps):
            snaps[3]["zones"][1]["name"] = "Desk"
        self.assertEqual(E5.crit_p57(self.runs(mutate=names))["status"], F)

        def field(snaps):
            snaps[7]["zones"][2]["poly_cm"] = [[100, 100], [900, 100], [900, 140]]
        c = E5.crit_p57(self.runs(mutate=field))
        self.assertEqual(c["status"], F)
        self.assertIn("leaves the radar field", " ".join(c["details"]["seeds"][0]["invariants"]))

        def nzn(snaps):
            snaps[8]["next_zone_n"] = 2
        self.assertEqual(E5.crit_p57(self.runs(mutate=nzn))["status"], F)

        def order(snaps):
            snaps[3]["zones"] = [snaps[3]["zones"][1], snaps[3]["zones"][0]]
        self.assertEqual(E5.crit_p57(self.runs(mutate=order))["status"], F)

        def many(snaps):
            snaps[7]["zones"][2]["poly_cm"] = [[100 + k, 100] for k in range(13)]
        self.assertEqual(E5.crit_p57(self.runs(mutate=many))["status"], F)

        def changed(snaps):
            snaps[3]["zones"][0]["rev"] = 5                  # the first zone changed although another place was approved
        self.assertEqual(E5.crit_p57(self.runs(mutate=changed))["status"], F)

    def test_merge_and_modify_counts_are_reported_only(self):
        r = mk("p5zone", nights=16, actors=zone_actor())
        r["nights"][9]["p5"] = {"items": [{"id": "mrg-s1", "type": "merge_into", "tier": 2, "rev": 1, "tag": "A"},
                                           {"id": "mod-z0001", "type": "modify_zone", "tier": 2, "rev": 1, "tag": "B"}]}
        rep = E5.rep_p5_merge_modify([r])
        self.assertEqual(rep["status"], R)
        self.assertEqual((rep["details"][0]["merge_items"], rep["details"][0]["modify_items"]), (["mrg-s1"], ["mod-z0001"]))
        self.assertEqual(E5.crit_p57([r])["status"], P)


# ---------------------------------------------------------------- P5-8
SPOT = (0.2, 2.4)


def guest_actor(pause=True, near=(), paused_stays=3, states=("learning",) * 4, valid=True):
    sess = [{"agent": "G", "place": "guest", "t0": 20 * 86400 + 19 * 3600, "t1": 20 * 86400 + 22 * 3600, "x": SPOT[0],
             "y": SPOT[1], "on": (20 * 86400 + 19 * 3600) * 1000 - 60_000, "off": (20 * 86400 + 22 * 3600) * 1000 + 90_000}]
    tags = ("start", "on+5min", "middle", "off-5min")
    return {"guests_user": {"pause": pause, "sessions": sess,
                            "switches": [{"t": sess[0]["on"], "on": True}, {"t": sess[0]["off"], "on": False}] if pause else [],
                            "samples": [{"session": 0, "tag": t, "t": 1, "state": s, "radar_valid": valid,
                                         "learning_paused": pause} for t, s in zip(tags, states)],
                            "stays_near": list(near), "paused_stays": paused_stays if pause else 0, "done": False}}


def stay(t0_s, t1_s, dist=0.1):
    return {"t0_s": t0_s, "t1_s": t1_s, "dur_s": t1_s - t0_s, "x": 0.2, "y": 2.4, "session": 0, "dist": dist}


def cov_recs(covered, hours, nights=21):
    return [{"night": n, "suggestions": [], "p4": {}, "p5": {"status": {"covered_days": covered if n >= 7 else 0,
                                                                      "observed_h_24h": hours.get(n, 18.0)}}}
            for n in range(nights + 1)]


CTRL_NEAR = [stay(20 * 86400 + 19 * 3600 + 600, 20 * 86400 + 21 * 3600)]


class P58Test(unittest.TestCase):
    def pair(self, seed=1, pause_kw=None, ctrl_kw=None, pause_hours=None, ctrl_hours=None, covered=14):
        # the night after the session (night 21 at 03:30 of day 21) has it inside its last 24 h
        p = mk("p5guests", seed=seed, nights=21, actors=guest_actor(True, **(pause_kw or {})),
               recs=cov_recs(covered, pause_hours if pause_hours is not None else {21: 15.0}))
        c = mk("p5guestsc", seed=seed, nights=21, actors=guest_actor(False, near=CTRL_NEAR, **(ctrl_kw or {})),
               recs=cov_recs(covered, ctrl_hours if ctrl_hours is not None else {21: 18.0}))
        return [p, c]

    def test_the_pause_holds_against_the_control(self):
        c = E5.crit_p58(self.pair(1) + self.pair(2))
        self.assertEqual(c["status"], P, c["details"]["a_guests"])
        self.assertEqual(c["details"]["a_guests"][0]["stays_near_control"], 1)

    def test_a_stay_at_the_guest_spot_during_a_pause_fails(self):
        inside = stay(20 * 86400 + 20 * 3600, 20 * 86400 + 20 * 3600 + 900)
        c = E5.crit_p58(self.pair(pause_kw={"near": [inside]}))
        self.assertEqual(c["status"], F)
        self.assertIn("during a pause", c["details"]["a_guests"][0]["note"])
        # a stay that ends just before the pause (inside the 5-minute rounding) still counts as overlapping
        edge = stay(20 * 86400 + 18 * 3600, 20 * 86400 + 19 * 3600 - 120)
        self.assertEqual(E5.crit_p58(self.pair(pause_kw={"near": [edge]}))["status"], F)
        far = stay(20 * 86400 + 17 * 3600, 20 * 86400 + 18 * 3600 + 1800)
        self.assertEqual(E5.crit_p58(self.pair(pause_kw={"near": [far]}))["status"], P)
        elsewhere = dict(inside, dist=0.9)
        self.assertEqual(E5.crit_p58(self.pair(pause_kw={"near": [elsewhere]}))["status"], P)

    def test_paused_stays_must_be_positive(self):
        self.assertEqual(E5.crit_p58(self.pair(pause_kw={"paused_stays": 0}))["status"], F)

    def test_the_status_must_stay_learning_like_the_control(self):
        c = E5.crit_p58(self.pair(pause_kw={"states": ("learning", "not_observing", "learning", "learning")}))
        self.assertEqual(c["status"], F)
        self.assertEqual(E5.crit_p58(self.pair(pause_kw={"valid": False}))["status"], F)
        c = E5.crit_p58(self.pair(ctrl_kw={"states": ("not_observing",) * 4}, pause_kw={"states": ("not_observing",) * 4}))
        self.assertEqual(c["status"], I)                                          # the control never reads learning

    def test_no_coverage_is_credited_for_the_paused_time(self):
        self.assertEqual(E5.crit_p58(self.pair(pause_hours={21: 18.0}))["status"], F)       # not lower than the control
        self.assertEqual(E5.crit_p58(self.pair(pause_hours={21: 19.5}))["status"], F)       # above the control
        res = self.pair()
        res[0]["nights"][10]["p5"]["status"]["covered_days"] = 15                           # a covered date was added
        self.assertEqual(E5.crit_p58(res)["status"], F)

    def test_the_control_must_record_a_stay_there(self):
        res = self.pair()
        res[1]["p4"]["actors"]["guests_user"]["stays_near"] = []
        c = E5.crit_p58(res)
        self.assertEqual(c["status"], I)
        self.assertIn("control records no stay", c["details"]["a_guests"][0]["note"])
        self.assertEqual(E5.crit_p58(self.pair()[:1])["status"], I)                         # no control run
        self.assertEqual(E5.crit_p58([])["status"], I)

    def test_a_run_without_a_session_is_incomplete(self):
        res = self.pair()
        res[0]["p4"]["actors"]["guests_user"]["sessions"] = []
        res[0]["p4"]["actors"]["guests_user"]["switches"] = []
        self.assertEqual(E5.crit_p58(res)["status"], I)

    def test_pets_are_reported_not_gated(self):
        cells = [1 * 70 + 8 + k for k in range(3)]
        dogx, dogy = W.SPOT["dogbed"]
        recs = [{"night": n, "suggestions": [], "p4": {}, "p5": {"items": [], "status": {"pet_spots": 0}}} for n in range(10)]
        for n in range(5, 10):
            recs[n]["suggestions"] = [dict(sug("s0005-1", cells), kind="pet", type="ignore_zone", cx=dogx + 0.2, cy=dogy)]
        recs[7]["p5"]["items"] = [{"id": "s0006-1", "type": "dwell_zone", "tier": 2, "rev": 1, "tag": "A"}]
        recs[7]["suggestions"].append(sug("s0006-1", cells))
        r = mk("p5pets", nights=9, recs=recs)
        row = E5._p58b_row(r)
        self.assertEqual((row["status"], row["first_pet_night"], row["first_near_dog_spot_night"]), (R, 5, 5))
        self.assertEqual(row["near"]["spot"], "dogbed")
        self.assertEqual(row["tier2_items_overlapping"][0]["items"], ["s0006-1"])
        self.assertEqual([x["id"] for x in row["suggestions_near_dog_spots"]], ["s0005-1"])
        c = E5.crit_p58(self.pair() * 1 + [r])
        self.assertEqual(c["details"]["b_pets_reported"][0]["status"], R)


# ---------------------------------------------------------------- the report
def good_results():
    res = [mk("p5ans", seed=s, actors=answer_steps()) for s in (1, 2, 3)]
    res += [push_run(), mk("p5setup", actors=setup_actor(), recs=bed_recs())]
    return res


class ReportTest(unittest.TestCase):
    def test_none_without_p5_runs(self):
        self.assertIsNone(E5.evaluate_p5([]))
        self.assertIsNone(E5.evaluate_p5([mk_p4_run("base", nights=21)]))

    def test_gate_and_statuses(self):
        p5 = E5.evaluate_p5(good_results())
        ids = [c["id"] for c in p5["criteria"]]
        self.assertEqual(ids, ["P5-1", "P5-2", "P5-3", "P5-4", "P5-5", "P5-6", "P5-7", "P5-8"])
        st = {c["id"]: c["status"] for c in p5["criteria"]}
        self.assertEqual((st["P5-1"], st["P5-2"]), (P, P))
        self.assertEqual(st["P5-6"], I)                                          # no p5rej run
        self.assertEqual(p5["gate"], I)
        self.assertEqual(p5["failed"], [])
        res = good_results()
        res[0]["p4"]["actors"] = answer_steps(bad=("stale_place",))
        p5 = E5.evaluate_p5(res)
        self.assertEqual(p5["gate"], F)
        self.assertEqual(p5["failed"], ["P5-1"])

    def test_markdown_names_the_checks_the_unit_tests_and_the_commands(self):
        text = "\n".join(E5.render_p5(E5.evaluate_p5(good_results())))
        for needle in ("P5-1", "P5-8", "were **not run**", "Evidence: unit tests", "tests/test_p5_push_flow.py",
                       "compare_runs.py", "compat_check.py", "resource_probe.py --p5", "## P5 interpretations", "P5-4r"):
            self.assertIn(needle, text)

    def test_a_pure_p5_report_says_that_p3_and_p4_were_not_run(self):
        rep = E5.build_report(good_results())
        self.assertEqual(rep["kind"], "p5")
        self.assertEqual(rep["scope"], E5.SCOPE_P5)
        self.assertEqual(rep["exit_code"], 0)
        md = E5.render_markdown(rep)
        self.assertIn("# P5 replay report (G1-G6 and P4-1 to P4-7 not run)", md)
        self.assertIn("G1-G6 and P4-1 to P4-7 were not run", md)
        self.assertIn("| bed_w0000_0600_p5ans | 1,2,3 | 14 |", md)
        with tmpdir() as d:
            E5.write_report(rep, d)
            back = json.loads((__import__("pathlib").Path(d) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(back["p5"]["gate"], I)
            self.assertTrue((__import__("pathlib").Path(d) / "report.md").read_text(encoding="utf-8").startswith("# P5"))

    def test_the_exit_code_follows_the_failures_and_the_bad_runs(self):
        res = good_results()
        res[0]["p4"]["actors"] = answer_steps(bad=("stale_place",))
        self.assertEqual(E5.build_report(res)["exit_code"], 1)
        res = good_results()
        res[1]["status"] = "over_budget"
        rep = E5.build_report(res)
        self.assertEqual(rep["exit_code"], 1)
        self.assertEqual(rep["bad_runs"][0]["status"], "over_budget")

    def test_other_runs_are_given_to_the_standard_evaluation(self):
        other = mk_p4_run("base", seed=1, nights=21)
        rep = E5.build_report(good_results() + [other])
        self.assertIsNone(rep.get("kind"))
        self.assertIn("p5", rep)
        self.assertIn("p4", rep)
        md = E5.render_markdown(rep)
        self.assertIn("## P5: setup review and suggestion UI", md)
        self.assertIn("## P4: night window", md)

    def test_run_filters(self):
        self.assertTrue(E5.is_p5_run(mk("p5ans")))
        self.assertFalse(E5.is_p5_run(mk_p4_run("mark", nights=3)))
        self.assertTrue(E5.is_rerun(mk_p4_run("mark", nights=3)))
        self.assertFalse(E5.is_rerun(mk_p4_run("base", nights=3)))

    def test_every_interpretation_is_listed(self):
        text = " ".join(E5.INTERPRETATION)
        for needle in ("P5-1:", "P5-2 (b)", "P5-4:", "P5-6:", "P5-7:", "P5-8 (a)", "P5-8 (b)", "not run"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
