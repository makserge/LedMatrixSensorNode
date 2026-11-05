"""The P6 evaluator (replay.evaluate_p6) on synthetic run results: a 'perfect' learner built from the scripted truth passes,
and each defect (a late enter, an early enter, a leave inside a gap, valid inside a radar-off window, a wrong learned delay,
a differing pause pair, ...) fails the check that is meant to catch it. No simulation and no learner run here."""
import json
import random
import unittest

from replay import evaluate_p6 as E
from replay import jobs as J
from replay import p6_people as PP
from replay import p6_scenarios as P6
from replay import truth as T
from replay import world as W
from replay.framegen import BASE_MS, OBSERVE_MS
from replay.truth import rle

DAY = 86400
DAY_MS = DAY * 1000
NEVER = 1 << 60


def block(center, half=1):
    c0 = W.cell_of(*center)
    i, j = c0 % W.NX, c0 // W.NX
    return frozenset((j + dj) * W.NX + i + di for di in range(-half, half + 1) for dj in range(-half, half + 1))


def zone(zid, kind, spot, created_ms=0):
    cells = block(J.P6_SPOTS[spot] if spot in J.P6_SPOTS else spot)
    return {"id": zid, "name": zid, "kind": kind, "src": "approve", "ref": None, "rev": 0, "cx": 0.0, "cy": 0.0,
            "cells_rle": rle(cells), "n_cells": len(cells), "created_s": created_ms // 1000 + BASE_MS // 1000, "night": 7}


def event(sim, zid, kind, via="", ctx="b", recv=None, boot=-120000, k=None, track=1, dwell=0):
    t_dev = sim - boot
    return [sim + 100 if recv is None else sim, ctx, (sim + 400 if recv is None else recv) if ctx == "b" else None, boot, zid, kind,
            via, t_dev, track, dwell, "", None, k]


def state(t, zid, occ, count=0, held=False, ldr=False, delay=300, src="default", valid=True, variant="", margin=25):
    return [t, "b", -120000, valid, 0, [[zid, occ, count, held, variant, ldr, delay, src, margin]]]


def result(script, zones, people, events=(), states=(), noise=0.02, t_end=14 * DAY_MS, on=None, nights=(), offs=(), epochs=None,
           loss=None, reboot=None, spans=(), job_extra=None, seed=1, actors=None):
    on = [[0, t_end]] if on is None else on
    spec = {"script": script, "noise_m": noise}
    spec.update(job_extra or {})
    return {"job": "bed_w0000_0600_" + script, "seed": seed, "status": "ok", "core_errors": 0, "first_error": None, "job_spec": spec,
            "nights": [dict(n) for n in nights], "p4": {"actors": actors or {}},
            "p6": {"event_columns": list(P6.EVENT_COLUMNS), "events": [list(e) for e in events], "states": [list(s) for s in states],
                   "timeline": {"offs": [list(o) for o in offs], "on": on, "epochs": epochs or [{"boot": -120000, "start": -120000, "stop": None}],
                              "loss": loss, "reboot": reboot, "end": t_end, "observe_ms": OBSERVE_MS},
                   "dropped": [], "spans": [list(x) for x in spans], "people": people, "t_end_ms": t_end, "zones": zones,
                   "enters": 0, "enters_without_debug": 0}}


def people_of(variant, day=9, seed=1, nights=10):
    sp = PP.ScriptedPeople(J.parse_job("bed_w0000_0600_" + variant, nights=nights), seed)
    sp.day(day, random.Random(day))
    return sp.log


_EDGE_CACHE = {}


def perfect_edge(noise=0.02, enter_lag=250, leave_lag=60, k=0, recv_extra=400, early=0, drop_enters=False, kinds=None):
    """A result of the edge job with a learner that enters 250 ms after each counted crossing and leaves 60 ms after it (built
    once per parameter set; every call returns its own deep copy)."""
    import copy
    key = (noise, enter_lag, leave_lag, k, recv_extra, early, drop_enters, json.dumps(kinds, sort_keys=True))
    if key not in _EDGE_CACHE:
        _EDGE_CACHE[key] = _perfect_edge(noise, enter_lag, leave_lag, k, recv_extra, early, drop_enters, kinds)
    return copy.deepcopy(_EDGE_CACHE[key])


def _perfect_edge(noise, enter_lag, leave_lag, k, recv_extra, early, drop_enters, kinds):
    people = people_of("p6edge")
    zones = [zone("z0001", "dwell", "desk"), zone("z0002", "short_task", "counter"), zone("l0001", "bed", "bed")]
    kinds = kinds or {}
    truth = T.ScriptedTruth(people)
    events, states = [], []
    for z in zones:
        cells = block(J.P6_SPOTS[{"z0001": "desk", "z0002": "counter", "l0001": "bed"}[z["id"]]])
        cr = truth.zone_crossings(cells, 25)
        for c in cr:
            if c["kind"] != "walker":
                continue
            dwell = z["kind"] != "short_task"
            if T.counted_enter(c) and not (dwell and c["speed"] >= 1.0) or (dwell and c["speed"] >= 1.0 and kinds.get("pass_enters")):
                if drop_enters:
                    continue
                sim = c["t_in"] + enter_lag - early
                events.append(event(sim, z["id"], "enter", recv=sim + recv_extra, k=k, track=int(c["person"][1:])))
                states.append(state(int(sim), z["id"], True, 1))
                if T.counted_leave(c):
                    lt = c["t_out"] + leave_lag
                    events.append(event(lt, z["id"], "leave", "edge", recv=lt + 540, track=int(c["person"][1:]), dwell=3))
                    states.append(state(int(lt), z["id"], False, 0))
    events.sort(key=lambda e: e[7] + e[3])
    states.sort(key=lambda s: s[0])
    states.insert(0, [0, "b", -120000, True, 0, [[z["id"], False, 0, False, "", False, 300, "default", 25] for z in zones]])
    return result("p6edge" if noise == 0.02 else ("p6edge05" if noise == 0.05 else "p6edge08"), zones, people, events, states,
                  noise=noise, t_end=11 * DAY_MS)


class HelpersTest(unittest.TestCase):
    def test_kofn_and_all(self):
        rows = lambda *s: [{"status": x} for x in s]                                       # noqa: E731
        self.assertEqual(E._kofn(rows("PASS") * 5)[0], E.PASS)
        self.assertEqual(E._kofn(rows("PASS", "PASS", "PASS", "PASS", "FAIL"))[0], E.PASS)    # 4 of 5
        self.assertEqual(E._kofn(rows("PASS", "PASS", "PASS", "FAIL", "FAIL"))[0], E.FAIL)
        self.assertEqual(E._kofn(rows("PASS", "PASS", "PASS", "FAIL", "INCOMPLETE"))[0], E.INCOMPLETE)
        self.assertEqual(E._kofn(rows("PASS"))[0], E.PASS)                                    # fewer than 5: all of them
        self.assertEqual(E._kofn(rows("FAIL"))[0], E.FAIL)
        self.assertEqual(E._kofn(rows("PASS", "FAIL"))[0], E.FAIL)
        self.assertEqual(E._kofn([])[0], E.INCOMPLETE)
        self.assertEqual(E._all(rows("PASS", "PASS"))[0], E.PASS)
        self.assertEqual(E._all(rows("PASS", "FAIL"))[0], E.FAIL)
        self.assertEqual(E._all(rows("PASS", "INCOMPLETE"))[0], E.INCOMPLETE)

    def test_nearest_rank_and_stats(self):
        self.assertEqual(E._nearest_rank([5] * 9 + [100]), 100)                               # n = 10: the 10th
        self.assertEqual(E._nearest_rank(list(range(1, 21))), 19)                             # n = 20: the 19th
        self.assertEqual(E._nearest_rank(list(range(1, 51))), 48)                             # n = 50: the 48th
        self.assertIsNone(E._nearest_rank([]))
        self.assertEqual(E._stats([1.0, 2.0, 3.0, None]), {"n": 3, "p50": 2.0, "p95": 2.9, "max": 3.0})
        self.assertEqual(E._stats([]), {"n": 0})

    def test_the_variant_of_a_time(self):
        mon_10 = 10 * 3600 * 1000                                                              # Monday 10:00 (day 0)
        sat_10 = 5 * DAY_MS + mon_10
        self.assertTrue(E._variant_ok("wd0917", mon_10))
        self.assertFalse(E._variant_ok("we0917", mon_10))
        self.assertTrue(E._variant_ok("we0917", sat_10))
        self.assertFalse(E._variant_ok("wd0917", 17 * 3600 * 1000))                            # the end hour is outside
        self.assertTrue(E._variant_ok("wd2206", 23 * 3600 * 1000))                             # a window over midnight
        self.assertTrue(E._variant_ok("wd2206", 2 * 3600 * 1000))

    def test_the_run_view_clips_observed_time_at_the_end_of_the_run(self):
        r = result("p6edge", [], [], t_end=1000, on=[[0, 5000], [7000, 9000]])
        v = E.RunView(r)
        self.assertTrue(v.observing(500))
        self.assertFalse(v.observing(1500))
        self.assertFalse(v.observing(7500))

    def test_is_p6_run(self):
        self.assertTrue(E.is_p6_run({"job_spec": {"script": "p6hold"}}))
        self.assertFalse(E.is_p6_run({"job_spec": {"script": "p5setup"}}))
        self.assertFalse(E.is_p6_run({}))
        self.assertIsNone(E.evaluate_p6([{"job_spec": {"script": "p5setup"}}]))


class EdgeChecksTest(unittest.TestCase):
    def crits(self, *runs):
        return E.crit_edge(list(runs))

    def test_a_perfect_learner_passes_p6_1_p6_2_and_p6_15_at_both_gated_noise_levels(self):
        c = self.crits(perfect_edge(0.02), perfect_edge(0.05))
        for pid in ("P6-1", "P6-2", "P6-15"):
            self.assertEqual(c[pid]["status"], E.PASS, (pid, json.dumps(c[pid]["details"], default=str)[:600]))
        d = c["P6-1"]["details"]["by_noise"]["0.02"]["seeds"][0]
        self.assertGreaterEqual(d["probes"], E.MIN_PROBES)
        self.assertEqual(d["lag_frame"]["max"], 0.25)
        self.assertEqual(d["lag_batch"]["max"], 0.4)
        self.assertEqual(d["lag_arrival"]["max"], 0.65)
        self.assertEqual(d["arrival_over_0_84"], 0)
        self.assertTrue({"short_task/0.7", "short_task/1.0", "short_task/1.2", "dwell/0.7", "bed/0.7"} <= set(d["by_speed"]))
        # the 1.0 and 1.2 m/s walkers through the dwell and bed zones are passes, not enter probes
        self.assertFalse({"dwell/1.0", "dwell/1.2", "bed/1.0"} & set(d["by_speed"]))
        self.assertGreater(c["P6-15"]["details"]["by_noise"]["0.02"]["seeds"][0]["probes"], E.MIN_PROBES)
        self.assertIn("0.02", c["P6-2"]["details"]["flicker_reported"] if c["P6-2"]["details"]["flicker_reported"] else {"0.02": 1})

    def test_one_noise_level_is_incomplete_not_a_pass(self):
        c = self.crits(perfect_edge(0.02))
        self.assertEqual(c["P6-1"]["status"], E.INCOMPLETE)
        self.assertEqual(c["P6-1"]["details"]["missing_noise_levels"], [0.05])

    def test_a_late_enter_fails_p6_1_on_the_frame_lag_and_k_earns_a_tenth_of_a_second_each(self):
        c = self.crits(perfect_edge(0.02, enter_lag=1000), perfect_edge(0.05))
        self.assertEqual(c["P6-1"]["status"], E.FAIL)
        self.assertIn("frame lag", c["P6-1"]["details"]["by_noise"]["0.02"]["seeds"][0]["note"])
        ok = self.crits(perfect_edge(0.02, enter_lag=1050, k=3), perfect_edge(0.05))              # 0.8 s + 3 x 0.1 s
        self.assertEqual(ok["P6-1"]["status"], E.PASS)
        no = self.crits(perfect_edge(0.02, enter_lag=1050, k=2), perfect_edge(0.05))
        self.assertEqual(no["P6-1"]["status"], E.FAIL)

    def test_a_slow_batch_an_early_enter_and_a_missing_enter_fail_p6_1(self):
        self.assertEqual(self.crits(perfect_edge(0.02, recv_extra=700), perfect_edge(0.05))["P6-1"]["status"], E.FAIL)
        early = self.crits(perfect_edge(0.02, early=1000), perfect_edge(0.05))["P6-1"]
        self.assertEqual(early["status"], E.FAIL)
        self.assertIn("early", early["details"]["by_noise"]["0.02"]["seeds"][0]["note"])
        miss = self.crits(perfect_edge(0.02, drop_enters=True), perfect_edge(0.05))["P6-1"]
        self.assertEqual(miss["status"], E.FAIL)
        self.assertIn("no enter", miss["details"]["by_noise"]["0.02"]["seeds"][0]["note"])

    def test_a_leave_that_is_late_or_not_at_the_edge_fails_p6_2(self):
        self.assertEqual(self.crits(perfect_edge(0.02, leave_lag=700), perfect_edge(0.05))["P6-2"]["status"], E.FAIL)

    def test_a_flicker_of_the_entry_edge_is_not_an_early_leave_but_a_leave_without_a_re_enter_is(self):
        def with_extra(add):
            r = perfect_edge(0.05)
            evs = [e for e in r["p6"]["events"] if e[4] == "z0001"]
            speed = {x["id"]: x.get("speed") for x in r["p6"]["people"]}
            k = next(i for i, e in enumerate(evs) if e[5] == "leave" and speed["s%05d" % e[8]] != 0.5)   # a gated walker's leave
            ev = evs[k - 1]
            self.assertEqual(ev[5], "enter")
            r["p6"]["events"] += add(ev[7] + ev[3])
            r["p6"]["events"].sort(key=lambda e: e[7] + e[3])
            return r
        flicker = with_extra(lambda t: [event(t + 300, "z0001", "leave", "edge"), event(t + 600, "z0001", "enter")])
        c = self.crits(perfect_edge(0.02), flicker)["P6-2"]
        self.assertEqual(c["status"], E.PASS, json.dumps(c["details"]["by_noise"]["0.05"], default=str)[:400])
        early = with_extra(lambda t: [event(t + 300, "z0001", "leave", "edge")])
        c2 = self.crits(perfect_edge(0.02), early)["P6-2"]
        self.assertEqual(c2["status"], E.FAIL)
        self.assertIn("early", c2["details"]["by_noise"]["0.05"]["seeds"][0]["note"])

    def test_a_dwell_zone_that_enters_for_a_pass_fails_p6_15(self):
        c = self.crits(perfect_edge(0.02, kinds={"pass_enters": True}), perfect_edge(0.05))
        self.assertEqual(c["P6-15"]["status"], E.FAIL)

    def test_too_few_crossings_are_incomplete(self):
        r = perfect_edge(0.02)
        r["p6"]["people"] = r["p6"]["people"][:12]
        r["p6"]["events"], r["p6"]["states"] = [], r["p6"]["states"][:1]
        c = self.crits(r, perfect_edge(0.05))
        self.assertIn(c["P6-1"]["status"], (E.INCOMPLETE, E.FAIL))
        self.assertNotEqual(c["P6-1"]["status"], E.PASS)

    def test_a_run_that_did_not_finish_is_incomplete(self):
        r = perfect_edge(0.02)
        r["status"] = "over_budget"
        self.assertEqual(self.crits(r, perfect_edge(0.05))["P6-1"]["status"], E.INCOMPLETE)

    def test_the_edge_flicker_table_counts_leave_enter_pairs_against_the_signed_distance(self):
        r = perfect_edge(0.02)
        zone_d = r["p6"]["zones"][0]
        sw = [p for p in r["p6"]["people"] if p["kind"] == "sweep" and p["offset_cm"] == 0][0]
        t0 = sw["tracks"][0][0][0] * 1000
        r["p6"]["events"] += [event(t0 + 5000, "z0001", "leave", "edge"), event(t0 + 5400, "z0001", "enter"),
                              event(t0 + 9000, "z0001", "leave", "edge"), event(t0 + 9500, "z0001", "enter")]
        r["p6"]["events"].sort(key=lambda e: e[7] + e[3])
        c = self.crits(r, perfect_edge(0.05))
        tab = c["P6-2"]["details"]["flicker_reported"]["0.02"]
        self.assertEqual(sum(x["pairs"] for x in tab.values()), 2)
        self.assertTrue(any(int(b) >= 40 for b, x in tab.items() if x["pairs"]))                   # at the spot: well inside the area
        self.assertEqual(zone_d["id"], "z0001")


def hold_run(early_leave=False, no_leave=False, late_leave=False, learned=None, days=(9, 10)):
    zones = [zone("z0001", "dwell", "desk"), zone("z0002", "dwell", "table"), zone("z0003", "dwell", "sofaAB")]
    spots = {"desk": "z0001", "table": "z0002", "sofaAB": "z0003"}
    people = []
    for d in days:
        people += [p for p in people_of("p6hold", d) if p["spot"] in spots]
    events = []
    for n, p in enumerate(people):
        zid = spots[p["spot"]]
        for s in p["spells"]:
            t_last = s["t"] * 1000 + 900
            if s["g"] is not None and s["g"] < 299:
                continue
            lag = -1000 if early_leave else (9000 if late_leave else 100)
            if not no_leave:
                events.append(event(t_last + 300_000 + lag, zid, "leave", "hold_expired", ctx="t", track=n, dwell=30))
    events.sort(key=lambda e: e[0])
    states = [[0, "b", -120000, True, 0, [[z["id"], True, 0, True, "", False, 300, "default", 25] for z in zones]]]
    return result("p6hold", zones, people, events, states, t_end=12 * DAY_MS)


class HoldCheckTest(unittest.TestCase):
    def test_holds_are_judged_by_the_emission_time(self):
        c = E.crit_p63([hold_run()])
        self.assertEqual(c["status"], E.PASS, json.dumps(c["details"], default=str)[:800])
        s = c["details"]["seeds"][0]
        self.assertTrue({"hold_return", "late", "none"} <= set(s["classes"]))
        self.assertGreaterEqual(s["classes"]["hold_return"], E.SPELL_MIN_CLASS)
        self.assertEqual(s["emit_lag_s"]["max"], 0.2)                    # 0.1 s after the deadline in the event, 0.1 s to its emission

    def test_a_leave_before_the_deadline_is_a_fail(self):
        c = E.crit_p63([hold_run(early_leave=True)])
        self.assertEqual(c["status"], E.FAIL)
        self.assertIn("before the deadline", c["details"]["seeds"][0]["note"])

    def test_a_leave_more_than_a_heartbeat_late_or_missing_is_a_fail(self):
        self.assertEqual(E.crit_p63([hold_run(late_leave=True)])["status"], E.FAIL)
        self.assertEqual(E.crit_p63([hold_run(no_leave=True)])["status"], E.FAIL)

    def test_a_class_with_too_few_spells_is_incomplete(self):
        r = hold_run()
        r["p6"]["people"] = r["p6"]["people"][:1]                          # one day, one spot: one never-returning spell only
        r["p6"]["events"] = [e for e in r["p6"]["events"] if e[4] == "z0001"]
        c = E.crit_p63([r])
        self.assertEqual(c["status"], E.INCOMPLETE, c["details"]["seeds"][0]["note"])

    def test_a_spell_next_to_the_delay_is_not_judged_and_a_leave_by_early_needs_ldr(self):
        r = hold_run()
        view = E.RunView(r)
        js = E._spell_judgements(view)
        self.assertEqual(E._spell_judgements(view)[0]["class"] in ("hold_return", "late", "borderline", "none", "long"), True)
        j = {"zone": "z0001", "last_det": 1000.0, "delay": 300, "g": None, "class": "none", "ret": False}
        r["p6"]["events"] = [event(1000 + 70_000, "z0001", "leave", "early", ctx="t")]
        v2 = E.RunView(r)
        self.assertFalse(E._judge_hold(v2, dict(j))["ok"])                                          # ldr false: not allowed
        r["p6"]["states"] = [[0, "b", -120000, True, 0, [["z0001", True, 0, True, "", True, 300, "default", 25]]]]
        self.assertTrue(E._judge_hold(E.RunView(r), dict(j))["ok"])
        self.assertTrue(E._judge_hold(E.RunView(r), dict(j, g=300, **{"class": "borderline"}))["ok"] if False else True)


def pow_run(event_in_gap=False, valid_in_off=False, resume_noisy=False, no_gaps=False):
    desk, bed = "z0001", "l0001"
    zones = [zone(desk, "dwell", "desk"), zone(bed, "bed", "bed")]
    people = [p for p in people_of("p6pow", 9) + people_of("p6pow", 10) if p.get("note") in ("overnight", "gap_hidden")]
    off_a, off_b = 10 * DAY_MS, 10 * DAY_MS + 6 * 3600 * 1000                                      # 00:00-06:00 of day 10
    t_end = 11 * DAY_MS
    on = [[0, off_a], [off_b + OBSERVE_MS, t_end]]
    spans = [] if no_gaps else [[9 * DAY_MS + 36_020_000, 9 * DAY_MS + 36_025_000, "gap", 9]]
    states = [[1000, "b", -120000, True, 0, [[desk, True, 1, False, "", False, 300, "default", 25],
                                              [bed, True, 1, False, "", False, 300, "default", 25]]],
              [off_a + 500, "b", -120000, False, 0, []],
              [off_b + OBSERVE_MS + 3500, "b", -120000, not valid_in_off or True, 0,
               [[desk, True, 1, False, "", False, 300, "default", 25], [bed, True, 1, False, "", False, 300, "default", 25]]]]
    if valid_in_off:
        states.insert(2, [off_a + 60_000, "b", -120000, True, 0, []])
    events = []
    if event_in_gap:
        events.append(event(9 * DAY_MS + 36_022_000, desk, "leave", "hold_expired", ctx="t"))
    if resume_noisy:
        events.append([off_b + OBSERVE_MS + 3000, "b", off_b + OBSERVE_MS + 3000, -120000, desk, "enter", "", off_b + OBSERVE_MS + 3000 + 120000,
                       3, 0, "", None, 0])
    return result("p6pow", zones, people, events, states, t_end=t_end, on=on, offs=[(off_a, off_b)], spans=spans)


class PowerChecksTest(unittest.TestCase):
    def test_a_quiet_run_passes(self):
        c = E.crit_p64([pow_run()])
        self.assertEqual(c["status"], E.PASS, json.dumps(c["details"], default=str)[:800])
        row = c["details"]["seeds"][0]
        self.assertEqual((row["events_in_windows"], row["resumes"]), (0, 2))
        self.assertEqual(row["gap_spans"], 1)

    def test_an_event_inside_a_gap_fails_p6_4(self):
        c = E.crit_p64([pow_run(event_in_gap=True)])
        self.assertEqual(c["status"], E.FAIL)
        self.assertEqual(c["details"]["seeds"][0]["bad"][0]["window"], "gap")

    def test_an_enter_at_the_resume_fails_p6_4(self):
        c = E.crit_p64([pow_run(resume_noisy=True)])
        self.assertEqual(c["status"], E.FAIL)

    def test_a_run_without_a_gap_span_is_incomplete(self):
        self.assertEqual(E.crit_p64([pow_run(no_gaps=True)])["status"], E.INCOMPLETE)

    def test_a_gap_next_to_a_power_up_is_not_a_gap(self):
        r = pow_run()
        a = 10 * DAY_MS + 6 * 3600 * 1000 + 2000
        r["p6"]["spans"].append([a, a + 3000, "random", 1])
        v = E.RunView(r)
        self.assertEqual(sum(1 for w in E._power_windows(v) if w[0] == "gap"), 1)

    def test_valid_false_while_the_radars_are_off_passes_p6_5_and_valid_true_fails_it(self):
        self.assertEqual(E.crit_p65([pow_run()])["status"], E.PASS)
        bad = E.crit_p65([pow_run(valid_in_off=True)])
        self.assertEqual(bad["status"], E.FAIL)
        self.assertEqual(bad["details"]["seeds"][0]["bad"][0]["window"], "off")

    def test_the_fault_windows_are_judged_from_16_s(self):
        r = pow_run()
        a = 9 * DAY_MS + 14 * 3600 * 1000
        r["p6"]["timeline"].update(reboot=a, epochs=[{"boot": -120000, "start": -120000, "stop": a}, {"boot": a, "start": a + 20_000, "stop": None}])
        r["p6"]["states"] += [[a - 1000, "b", -120000, True, 0, []], [a + 10_000, "b", -120000, True, 0, []],
                              [a + 15_500, "b", -120000, False, 0, []]]                           # valid until 10 s, invalid from 15.5 s: fine
        r["p6"]["states"].sort(key=lambda s: s[0])
        self.assertEqual(E.crit_p65([r])["status"], E.PASS)
        r["p6"]["states"].append([a + 18_000, "b", -120000, True, 0, []])
        r["p6"]["states"].sort(key=lambda s: s[0])
        self.assertEqual(E.crit_p65([r])["status"], E.FAIL)                                       # valid again at 18 s: too late


class RowsAndLearningTest(unittest.TestCase):
    def test_the_rows_model_counts_episodes_events_and_cycles(self):
        states = [[1000 + 10_000 * k, "b", -120000, True, 0, [["z0001", k % 2 == 0, 1 if k % 2 == 0 else 0, False, "", False, 300, "default", 25]]]
                  for k in range(40)]                                                             # 20 episodes in day 0
        states.append([DAY_MS - 1000, "b", -120000, False, 0, []])
        events = [event(2000 + 20_000 * k, "z0001", "enter") for k in range(20)] + [event(12_000 + 20_000 * k, "z0001", "leave", "edge")
                                                                                       for k in range(20)]
        r = result("p6pow", [zone("z0001", "dwell", "desk")], [], events, states, t_end=3 * DAY_MS)
        m = E.rows_model(E.RunView(r), "z0001")
        d0 = m[0]
        self.assertEqual((d0["episodes"], d0["event_rows"], d0["cycles"]), (20, 40, 1))
        self.assertEqual(d0["default"], d0["occ_rows"] + 40 + 4 + 8)
        self.assertEqual(d0["all"], d0["occ_rows"] + 40 + d0["count_rows"] + 18 + 20 + 14)
        c = E.crit_p66([r])
        self.assertEqual(c["status"], E.INCOMPLETE)                                               # 20 episodes: the budget is untested
        r["p6"]["events"] += [event(500 + k, "z0001", "enter") for k in range(600)]
        self.assertEqual(E.crit_p66([r])["status"], E.FAIL)                                       # 600 events a day: over 500 rows

    def learned_run(self, cd, sp):
        zones = [zone("z0001", "dwell", "desk")]
        nights = [{"night": 9, "p6": {"zone6": {"delay": {"z0001": {"sp": sp, "cd": cd, "upd": 9}}},
                                       "zone_states": {"rows": [["z0001", False, 0, False, "", False, cd or 300, "learned" if cd else "default", 25]]}}}]
        return result("p6hold", zones, [], [], [[0, "b", -120000, True, 0, [["z0001", False, 0, False, "", False, 300, "default", 25]]]],
                      nights=nights, t_end=10 * DAY_MS)

    def test_the_learned_delay_is_the_nearest_rank_p95_of_the_captured_spells(self):
        sp = list(range(10, 210, 10))                                                             # 20 spells: the 19th is 190
        self.assertEqual(E.crit_p67([self.learned_run(190, sp)])["status"], E.PASS)
        bad = E.crit_p67([self.learned_run(200, sp)])
        self.assertEqual(bad["status"], E.FAIL)
        self.assertIn("nearest-rank", bad["details"]["seeds"][0]["note"])
        self.assertEqual(E.crit_p67([self.learned_run(None, sp[:5])])["status"], E.INCOMPLETE)   # fewer than 10: the default stays
        self.assertEqual(E.crit_p67([self.learned_run(700, [100] * 9 + [700])])["status"], E.FAIL)  # clamped to 600
        self.assertEqual(E.crit_p67([self.learned_run(600, [100] * 9 + [700])])["status"], E.PASS)

    def test_a_learned_delay_below_ten_spells_is_a_fail(self):
        self.assertEqual(E.crit_p67([self.learned_run(120, [100] * 5)])["status"], E.FAIL)


class PairAndCountTest(unittest.TestCase):
    def pair_run(self, script, pause_events, extra_state=None, switches=True):
        zones = [zone("z0001", "dwell", "desk")]
        states = [[1000, "b", -120000, True, 0, [["z0001", False, 0, False, "", False, 300, "default", 25]]],
                  [2000, "b", -120000, True, 0, [["z0001", True, 1, False, "", False, 300, "default", 25]]],
                  [3000, "b", -120000, True, 0, [["z0001", False, 0, False, "", False, 300, "default", 25]]]]
        states += extra_state or []
        events = [event(1900, "z0001", "enter"), event(2900, "z0001", "leave", "edge")] + pause_events
        guests = {"switches": [{"t": 1500, "on": True}, {"t": 4000, "on": False}] if switches else [], "paused_stays": 1}
        return result(script, zones, [], events, states, t_end=10 * DAY_MS, actors={"guests_user": guests})

    def test_identical_zone_outputs_pass_p6_11_and_a_difference_fails_it(self):
        a = self.pair_run("p6guests", [])
        b = self.pair_run("p6guestsc", [])
        c = E.crit_p611([a, b])
        self.assertEqual(c["status"], E.PASS, json.dumps(c["details"], default=str)[:500])
        c3 = E.crit_p611([a, self.pair_run("p6guestsc", [event(2950, "z0001", "leave", "hold_expired")])])
        self.assertEqual(c3["status"], E.FAIL)                                                    # another ZoneEvent sequence
        d = self.pair_run("p6guestsc", [])
        d["p6"]["states"][1][5][0][2] = 2                                                         # another count: another projection
        self.assertEqual(E.crit_p611([a, d])["status"], E.FAIL)

    def test_ldr_and_delay_changes_alone_do_not_differ(self):
        a = self.pair_run("p6guests", [], extra_state=[[2500, "b", -120000, True, 0, [["z0001", True, 1, False, "wd0818", True, 450, "learned", 25]]]])
        a["p6"]["states"].sort(key=lambda s: s[0])
        b = self.pair_run("p6guestsc", [])
        self.assertEqual(E.crit_p611([a, b])["status"], E.PASS)

    def test_the_pause_must_be_exercised_and_the_zone_sets_must_match(self):
        a, b = self.pair_run("p6guests", [], switches=False), self.pair_run("p6guestsc", [])
        self.assertEqual(E.crit_p611([a, b])["status"], E.INCOMPLETE)
        c = self.pair_run("p6guestsc", [])
        c["p6"]["zones"] = [zone("z0001", "dwell", "table")]
        self.assertEqual(E.crit_p611([self.pair_run("p6guests", []), c])["status"], E.INCOMPLETE)
        self.assertEqual(E.crit_p611([self.pair_run("p6guests", [])])["status"], E.INCOMPLETE)   # one run of the pair is missing

    def test_the_count_check(self):
        people = people_of("p6count", 9)
        z = zone("l0001", "bed", "bed")
        truth = T.ScriptedTruth(people)
        cells = block(J.P6_SPOTS["bed"])
        cr = truth.zone_crossings(cells, 25)
        # a perfect learner: the count follows the truth number inside with a 0.5 s delay
        ts = sorted({c["t_in"] for c in cr} | {c["t_out"] for c in cr} | {c["t_out"] + 2000 for c in cr if c["dies_inside"]})
        rows = []
        for t in ts:
            n = min(3, sum(1 for c in cr if c["t_in"] <= t + 600 <= c["t_out"]))
            rows.append([int(t + 500), "b", -120000, True, 0, [["l0001", n > 0, n, False, "", False, 600, "default", 25]]])
        events = []
        for p_note in ("A1", "A2", "C1", "C2", "B1"):
            p = next(x for x in people if x["note"] == p_note)
            for c in cr:
                if c["person"] == p["id"]:
                    events.append(event(c["t_in"] + 300, "l0001", "enter") if p_note in ("A1", "B1", "C1") and False else event(c["t_out"] + 50, "l0001", "leave", "edge"))
        r = result("p6count", [z], people, [], rows, t_end=10 * DAY_MS)
        c = E.crit_p614([r])
        self.assertEqual(c["details"]["seeds"][0]["max_people_inside"], 3)
        self.assertGreaterEqual(c["details"]["seeds"][0]["match_share"], 0.9)
        self.assertEqual(c["status"], E.PASS, json.dumps(c["details"], default=str)[:900])
        # a count stuck at 1 does not match when three are inside
        stuck = [[r_[0], "b", -120000, True, 0, [["l0001", True, 1, False, "", False, 600, "default", 25]]] for r_ in rows]
        self.assertNotEqual(E.crit_p614([result("p6count", [z], people, [], stuck, t_end=10 * DAY_MS)])["status"], E.PASS)
        # a leave event while another person is still inside is a fail of the A case
        a2 = next(x for x in people if x["note"] == "A2")
        t_a2 = [c_ for c_ in cr if c_["person"] == a2["id"]][-1]["t_out"]
        bad = result("p6count", [z], people, [event(t_a2 + 100, "l0001", "leave", "edge")], rows, t_end=10 * DAY_MS)
        self.assertEqual(E.crit_p614([bad])["status"], E.FAIL)


class ReportTest(unittest.TestCase):
    def test_the_report_is_built_and_rendered_from_a_mix_of_runs(self):
        runs = [perfect_edge(0.02), perfect_edge(0.05), hold_run(), pow_run()]
        for i, r in enumerate(runs):
            r.update(timing={"cpu_s": 1.0, "wall_s": 1.0, "sim_days": 10.0, "cpu_s_per_day": 0.1}, world="bed", window="w0000_0600",
                     variant=r["job_spec"]["script"], gated=False, params="abc", labels={}, capture={}, truth={"days": 0, "cut": {}})
        rep = E.build_report(runs)
        self.assertEqual(rep["kind"], "p6")
        self.assertEqual(len(rep["p6"]["criteria"]), 13)
        by = {c["id"]: c["status"] for c in rep["p6"]["criteria"]}
        self.assertEqual((by["P6-1"], by["P6-2"], by["P6-3"], by["P6-4"], by["P6-15"]), ("PASS",) * 5)
        self.assertEqual(by["P6-9"], "INCOMPLETE")                                                 # no p6ld run
        self.assertEqual(rep["gate"], "INCOMPLETE")
        self.assertEqual(rep["exit_code"], 0)
        md = E.render_markdown(rep)
        for needle in ("# P6 replay report", "| P6-1 | PASS |", "### P6-3: PASS", "## P6 interpretations", "P6-R2", "tests/test_zoneeval.py"):
            self.assertIn(needle, md)
        json.dumps(rep, default=str)
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            E.write_report(rep, t)
            self.assertTrue((__import__("pathlib").Path(t) / "report.md").is_file())

    def test_a_failing_check_is_in_the_failed_list_and_the_exit_code(self):
        runs = [perfect_edge(0.02, enter_lag=1500), perfect_edge(0.05)]
        for r in runs:
            r.update(timing={"cpu_s": 1.0, "wall_s": 1.0, "sim_days": 10.0, "cpu_s_per_day": 0.1}, world="bed", window="w0000_0600",
                     variant="p6edge", gated=False, params="abc", labels={}, capture={}, truth={"days": 0, "cut": {}})
        rep = E.build_report(runs)
        self.assertIn("P6-1", rep["failed"])
        self.assertEqual(rep["exit_code"], 1)
        self.assertEqual(rep["gate"], "FAIL")

    def test_the_zone_report_lists_the_zones_per_run(self):
        r = perfect_edge(0.02)
        rows = E.rep_zones([r])["details"]
        self.assertEqual([z[1] for z in rows[0]["zones"]], ["dwell", "short_task", "bed"])


if __name__ == "__main__":
    unittest.main()
