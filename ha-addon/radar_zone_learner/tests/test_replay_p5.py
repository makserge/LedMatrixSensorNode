"""P5 replay: the job variants and profiles, the driver additions (learner factory, restart), the actors against the real
core (a mini run without the frame stream) and on a stub learner, the scripts dump_run, compare_runs and compat_check, the
P5-R3 probe's state, and two short end-to-end runs (P5 spec 11.6)."""
import contextlib
import datetime
import heapq
import io
import json
import pickle
import unittest
from pathlib import Path
from types import SimpleNamespace

from radar_zone_learner.core.learner import Learner
from radar_zone_learner.core.params import DEFAULT_P5, DEFAULT_PARAMS, P5Params
from radar_zone_learner.core.types import (AnswerIgnored, FixedOffsetTime, MarkPress, MarkResult, NodeCommand, PushRequest,
                                           ReviewChanged, SaveRequested, StayView)
from replay import compare_runs as CR
from replay import compat_check as CC
from replay import driver as D
from replay import dump_run
from replay import evaluate_p5 as E5
from replay import jobs as J
from replay import p4_scenarios as P4
from replay import p5_scenarios as P5
from replay import resource_probe as RP
from replay import run_replay
from replay.framegen import BASE_MS
from replay.truth import rle
from tests._tmp import tmpdir
from tests.test_extraction import make
from tests.test_p5_items import places
from tests.test_replay_p4 import StubLearner
from tests.test_review import ready_learner, with_proposal

ADDON = Path(__file__).resolve().parents[1]
DAY_MS = 86400 * 1000
DAY = 86400
INF = P4.INF


# ---------------------------------------------------------------- a mini run: the real core without the frame stream
class FakeTimeline:
    offs = []

    def observing(self, t):
        return True


class MiniRun:
    """What an actor needs of ``replay.driver.Run`` (the learner, the clock, ``feed``, the node's answers, the restart), driven
    by hand: ``drive`` runs the actors' steps, the node's echoes and, with ``tick``, the learner's ticks in time order."""

    restart_learner = D.Run.restart_learner

    def __init__(self, learner, mark_ok_at=None):
        self.learner = learner
        self.now = learner.now
        self.params = DEFAULT_PARAMS
        self.stream = SimpleNamespace(timeline=FakeTimeline())
        self.node = P4.FakeNode(True, True, 0, 360)
        self.actors = []
        self.inject = []
        self.seq = 0
        self.restarts = []
        self.last_night = 7
        self.review_items = {}
        self.node_online = True
        self.home_empty = False
        self.p5 = P5.P5Recorder()
        self.mark_ok_at = mark_ok_at
        self.presses = 0

    def _new_learner(self, state):
        return Learner(DEFAULT_PARAMS, FixedOffsetTime(0), state, DEFAULT_P5)

    def add(self, *actors):
        for a in actors:
            a.attach(self)
            self.actors.append(a)
        return actors[0] if len(actors) == 1 else actors

    def feed(self, ev):
        if isinstance(ev, MarkPress) and self.mark_ok_at is not None:        # 'Mark spot here' answered by the script
            self.presses += 1
            outs = [MarkResult(True, None, "l9999", "bed") if self.presses >= self.mark_ok_at
                    else MarkResult(False, "no_stay", None, None)]
        else:
            outs = self.learner.handle(ev)
        self.process(outs)

    def process(self, outs):
        for o in outs:
            if isinstance(o, NodeCommand):
                for t, ev in self.node.command(o, self.now):
                    self.seq += 1
                    heapq.heappush(self.inject, (t, self.seq, ev))
            elif isinstance(o, ReviewChanged):
                self.review_items = {i.id: i for i in o.items}
            self.p5.on_output(o, self.now - BASE_MS)
            for a in self.actors:
                a.on_output(o)

    def tick(self, t):
        self.now = max(self.now, t)
        self.process(self.learner.tick(t))

    def rel(self):
        return self.now - BASE_MS

    def drive(self, until, tick=None):
        """Until ``until`` (ms since BASE_MS): ticks first on equal times, then the node's events, then the actors."""
        next_tick = ((self.rel() // tick) + 1) * tick if tick else INF
        while True:
            ta, best = INF, None
            for a in self.actors:
                x = a.next_time()
                if x < ta:
                    ta, best = x, a
            ti = (self.inject[0][0] - BASE_MS) if self.inject else INF
            t = min(ta, ti, next_tick)
            if t > until:
                break
            self.now = max(self.now, BASE_MS + t)
            if next_tick <= ti and next_tick <= ta:
                self.tick(BASE_MS + t)
                next_tick += tick
            elif ti <= ta:
                _, _, ev = heapq.heappop(self.inject)
                self.feed(ev)
            else:
                best.act(self.feed)

    def start(self):
        """The mirrors start from the learner's own list (``review_state``, as the shell seeds its mirrors) and the learner's
        first step follows a second later."""
        items, sel = self.learner.review_state()
        for a in self.actors:
            a.on_output(ReviewChanged(tuple(items), sel))
        self.tick(self.now + 1000)


def night_record(L, n):
    """A night record with what the P5 actors read from it (``Run._record_night``)."""
    return {"night": n, "suggestions": [
        {"id": s.id, "rev": s.rev, "type": s.type, "kind": s.kind, "name_hint": s.name_hint, "state": s.state,
         "cx": s.cx_m, "cy": s.cy_m, "conf": s.conf, "cells_rle": rle(s.cells), "iou": {}} for s in L.suggestions()]}


def mini(L, *actors, mark_ok_at=None):
    run = MiniRun(L, mark_ok_at)
    run.add(*actors)
    run.start()
    return run


def bed_label(L, kind="bed"):
    L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": -2.3, "y": 4.9, "name": "Bed" if kind == "bed" else "",
                         "kind": kind, "cells": frozenset(L.geom.cells_within(-2.3, 4.9, 0.4))})
    L.review.touch()


# ---------------------------------------------------------------- jobs and profiles
class JobsP5Test(unittest.TestCase):
    def test_variants_parse_and_carry_their_flags(self):
        for v in J.P5_VARIANTS:
            job = J.parse_job("bed_w0000_0600_%s" % v)
            self.assertEqual((job.variant, job.script), (v, v))
            self.assertTrue(job.is_p5 and job.is_p4 and job.lux)
            self.assertFalse(job.labels or job.gated)
            self.assertEqual(job.pet, 1 if v == "p5pets" else 0)
            self.assertEqual(job.zone_change_items, v == "p5zone")
            self.assertEqual(job.cfg()["pet"], job.pet)
        self.assertEqual(J.P5_VARIANTS, ("p5setup", "p5ans", "p5push", "p5adv", "p5rej", "p5zone", "p5guests", "p5guestsc",
                                         "p5pets"))
        none = J.parse_job("bed_none_p5setup")
        self.assertEqual((none.window, none.cfg()["nw"]), (None, ""))

    def test_the_older_jobs_are_unchanged(self):
        for name in ("bed_w0000_0600", "liv_none", "bed_w0000_0600_p4", "bed_w0000_0600_p4mark", "bed_none_p4nop2"):
            job = J.parse_job(name)
            self.assertFalse(job.is_p5, name)
            self.assertEqual((job.pet, job.zone_change_items), (0, False))
            self.assertEqual(job.cfg()["pet"], 0)
        self.assertEqual(len(J.runs_of(J.PROFILES["p4"])), 47)
        self.assertEqual(len(J.runs_of(J.PROFILES["p3"])), 67)

    def test_p5setup_scripts_one_person_awake_in_bed_in_the_evenings_of_days_6_to_9(self):
        # P5-4: OA 7.6 has the user press 'Mark spot here' awake in bed; the simulator's sleeper is visible about 5 s in
        # 600 s, so the job adds that person (the same mechanism as the two people of p4mark). Nothing else gets one.
        import random
        from replay import framegen as G
        from replay import world as W
        for name in ("bed_w0000_0600_p5setup", "bed_none_p5setup"):
            ex = J.parse_job(name).extras()
            self.assertEqual([e[0] for e in ex], ["X1"] * 4, name)
            bx, by = W.SPOT["bed"]
            for e, d in zip(ex, (6, 7, 8, 9)):
                self.assertEqual(e, ("X1", bx, by, d * DAY + 22 * 3600, d * DAY + 23 * 3600 + 58 * 60))
            tr = G.extra_tracks(random.Random(1), ex)
            self.assertEqual([len(t["pts"]) for t in tr], [118 * 60] * 4)                 # one point a second, always visible
            self.assertEqual(tr[1]["pts"][0][0], 7 * DAY + 22 * 3600)
        for v in J.P5_VARIANTS:
            if v != "p5setup":
                self.assertEqual(J.parse_job("bed_w0000_0600_%s" % v).extras(), [], v)
        self.assertEqual([e[0] for e in J.parse_job("bed_w0000_0600_p4mark").extras()], ["X1", "X2"])   # unchanged
        self.assertEqual(J.parse_job("bed_w0000_0600_p4").extras(), [])

    def test_the_p5_profile_has_the_33_runs_of_the_spec(self):
        prof = J.PROFILES["p5"]
        got = {j.name: (j.nights, j.seeds) for j in prof}
        self.assertEqual(got, {
            "bed_w0000_0600_p5setup": (14, (1, 2, 3, 4, 5)), "bed_w0000_0600_p5ans": (14, (1, 2, 3)),
            "bed_w0000_0600_p5push": (16, (1, 2)), "bed_w0000_0600_p5adv": (16, (1, 2, 3)),
            "bed_w0000_0600_p5rej": (21, (1, 2)), "bed_w0000_0600_p5zone": (16, (1, 2)),
            "bed_w0000_0600_p5guests": (21, (1, 2)), "bed_w0000_0600_p5guestsc": (21, (1, 2)),
            "bed_w0000_0600_p5pets": (21, (1, 2)), "bed_none_p5setup": (9, (1,)),
            "bed_w0000_0600_p4mark": (16, (1, 2, 3)), "bed_w0000_0600_p4app": (16, (1, 2, 3)),
            "bed_w0000_0600_p4rej": (16, (1, 2, 3))})
        self.assertEqual(len(J.runs_of(prof)), 33)
        self.assertLess(J.estimate_days(prof), 1500)
        self.assertAlmostEqual(J.estimate_days(prof), 549.8, delta=1.0)
        smoke = J.PROFILES["p5smoke"]
        self.assertEqual([(j.name, j.nights, j.seeds) for j in smoke], [("bed_w0000_0600_p5setup", 9, (1,))])

    def test_the_run_replay_cli_knows_the_profiles(self):
        with tmpdir() as d, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_replay.main(["--out", d, "--profile", "p5", "--nights", "40"]), 2)
            self.assertEqual(run_replay.main(["--out", d, "--profile", "p5", "--jobs", "bed_w0000_0600_nope"]), 2)
        args = SimpleNamespace(jobs="bed_w0000_0600_p5ans,bed_none_p5setup", profile="p5", nights=None, seeds=None)
        jobs = run_replay.build_jobs(args)
        self.assertEqual([(j.name, j.nights, j.seeds) for j in jobs],
                         [("bed_w0000_0600_p5ans", 14, (1, 2, 3)), ("bed_none_p5setup", 9, (1,))])


# ---------------------------------------------------------------- the driver
class StubFactory(StubLearner):
    """The stub of the P4 tests, with the third argument of a restart (the exported state)."""

    def __init__(self, params, tm, state=None):
        super().__init__(params, tm)
        self.state = state

    def export_state(self):
        return {"night": {"onsets": [], "wakes": []}, "zones": [], "suppressions": [], "marker": id(self) % 1000}


class DriverP5Test(unittest.TestCase):
    def test_the_default_factory_is_a_picklable_class_that_carries_the_job_s_p5_parameters(self):
        job = J.parse_job("bed_w0000_0600_p5zone")
        f = D.LearnerFactory.for_job(job)
        self.assertTrue(f.p5params().zone_change_items)
        g = pickle.loads(pickle.dumps(f))
        self.assertEqual((type(g).__name__, g.zone_change_items), ("LearnerFactory", True))
        self.assertEqual(pickle.loads(pickle.dumps(job)), job)                        # the job crosses the spawn boundary as it is
        L = g(DEFAULT_PARAMS, FixedOffsetTime(0))
        self.assertEqual(L.p5params, P5Params(zone_change_items=True))
        self.assertFalse(D.LearnerFactory.for_job(J.parse_job("bed_w0000_0600_p5setup")).p5params().zone_change_items)
        self.assertEqual(D.LearnerFactory().p5params(), DEFAULT_P5)

    def test_zone_change_items_reaches_the_learner_of_a_run_and_survives_a_restart(self):
        for name, want in (("bed_w0000_0600_p5zone", True), ("bed_w0000_0600_p5setup", False), ("bed_w0000_0600", False)):
            run = D.Run(J.parse_job(name, nights=2), 1)
            self.assertEqual(run.learner.p5params.zone_change_items, want, name)
            old = run.learner
            run.restart_learner()
            self.assertIsNot(run.learner, old)
            self.assertEqual(run.learner.p5params.zone_change_items, want, name)
            self.assertEqual(run.restarts, [{"t": run.now - BASE_MS, "night": None}])

    def test_restart_learner_is_a_json_round_trip_and_feeds_the_retained_topics_again(self):
        L = places(2)
        with_proposal(L)
        L.handle(__import__("radar_zone_learner.core.types", fromlist=["SpotName"]).SpotName("Desk", L.now + 1))
        run = MiniRun(L)
        before = json.dumps(L.export_state(), sort_keys=True)
        run.node_online, run.home_empty = True, True
        run.node.sched, run.node.start_min, run.node.end_min = True, 1380, 375
        run.restart_learner()
        self.assertIsNot(run.learner, L)
        after = run.learner.export_state()
        for k in ("suggestions", "zones", "suppressions", "review", "labels"):
            self.assertEqual(json.loads(before)[k], after[k], k)
        self.assertEqual(run.learner.window, {"start": 1380, "end": 375, "sched": True})
        self.assertEqual(run.learner.p5params, DEFAULT_P5)
        self.assertEqual(len(run.restarts), 1)
        # what lives in RAM only is gone: the spot name is part of the state, the selected item is not
        self.assertFalse(run.learner.review.select_chosen)

    def test_a_p5_job_records_the_p5_night_and_the_run_record_on_a_stub_learner(self):
        job = J.parse_job("bed_w0000_0600_p5ans", nights=1)
        r = D.Run(job, 1, learner_factory=StubFactory)
        self.assertEqual(r.run(), "ok")
        res = r.result()
        self.assertIn("p5", res)
        self.assertEqual(sorted(res["p5"]), ["ignored_log", "push_checked", "push_fields", "push_log", "push_problems",
                                             "restarts", "saves"])
        self.assertIn("p5", res["nights"][0])                       # the stub has no review_state or zones: those keys stay out
        self.assertNotIn("items", res["nights"][0]["p5"])
        self.assertEqual(res["p4"]["actors"]["answer_script"]["steps"], [])
        self.assertEqual(res["p4"]["actors"]["answer_script"]["not_exercised"], ["place", "window", "bed"])
        # a restart through the stub: the factory gets the state as its third argument
        old = r.learner
        r.restart_learner()
        self.assertIsNot(r.learner, old)
        self.assertIsNotNone(r.learner.state)
        self.assertEqual([c[0] for c in r.learner.calls][:3], ["NodeStatus", "ScheduleState", "ContextState"])

    def test_a_p3_or_p4_job_gets_no_p5_keys(self):
        for name in ("bed_w0000_0600", "bed_w0000_0600_p4"):
            r = D.Run(J.parse_job(name, nights=1), 1, learner_factory=StubFactory)
            r.run()
            res = r.result()
            self.assertNotIn("p5", res)
            self.assertNotIn("p5", res["nights"][0])

    def test_the_readback_gains_the_zone_views_the_advanced_ids_and_the_push_request(self):
        L = places(2)
        run = mini(L)
        rb = P4.Actor()
        rb.run = run
        out = rb.readback()
        self.assertEqual(out["zones"], 0)                                           # the P4 key keeps its meaning (a count)
        self.assertEqual((out["zone_views"], out["advanced"]), ([], []))
        self.assertEqual(out["push"]["seq"], 0)
        # a stub learner has none of them: the P4 keys are still there, the P5 keys are left out
        run.learner = StubFactory(DEFAULT_PARAMS, FixedOffsetTime(0))
        rb.run = run
        out = rb.readback()
        self.assertEqual(out["bed_item"], {"state": "open"})
        self.assertFalse({"zone_views", "advanced", "push"} & set(out))


# ---------------------------------------------------------------- helpers of the actors
class RecorderTest(unittest.TestCase):
    def test_push_problems(self):
        ok = PushRequest(2, 1, "daily", 5, "s0005-1", 1, ("pa1b2c3d",))
        self.assertEqual(P5.push_problems(ok), [])
        self.assertEqual(P5.push_problems(PushRequest(0, 0, "none", 0, None, None, ())), [])
        self.assertTrue(P5.push_problems(PushRequest(1, 0, "setup", 2, "night_window", 1, ())))      # a tier-1 item is never a head
        self.assertTrue(P5.push_problems(PushRequest(1, 0, "setup", 2, "Desk", 1, ())))              # a name
        self.assertTrue(P5.push_problems(PushRequest(1, 0, "setup", 2, "s0005-1", None, ())))        # head without rev
        self.assertTrue(P5.push_problems(PushRequest(1, 0, "text", 2, None, None, ())))
        self.assertTrue(P5.push_problems(PushRequest(True, 0, "setup", 2, None, None, ())))          # a bool is no int
        self.assertTrue(P5.push_problems(PushRequest(1, 0, "setup", 2, None, None, ("07:30",))))     # a time
        self.assertTrue(P5.push_problems(PushRequest(1, 0, "setup", 2, None, None, ["pa"])))         # not a tuple
        probs = P5.push_problems(SimpleNamespace(seq=1, acked=0, kind="setup", count=1, head_id=None, head_rev=None, acks=()))
        self.assertTrue(any("fields" in p for p in probs))                                           # not the dataclass

    def test_the_recorder_keeps_requests_refusals_and_saves(self):
        rec = P5.P5Recorder()
        rec.on_output(PushRequest(1, 0, "setup", 6, "s0004-1", 1, ()), 1000)
        rec.on_output(PushRequest(1, 0, "setup", 6, "Desk", 1, ()), 2000)
        rec.on_output(AnswerIgnored("s1", "approve", "stale"), 3000)
        rec.on_output(SaveRequested("review"), 4000)
        rec.on_output(SaveRequested("review"), 4001)
        res = rec.result([{"t": 5}])
        self.assertEqual((res["push_checked"], len(res["push_log"]), len(res["push_problems"])), (2, 2, 1))
        self.assertEqual(res["push_log"][0]["t"], 1000)
        self.assertEqual(res["ignored_log"], [{"t": 3000, "item": "s1", "op": "approve", "reason": "stale"}])
        self.assertEqual((res["saves"], res["restarts"]), ({"review": 2}, [{"t": 5}]))

    def test_helpers(self):
        self.assertEqual(P5.unrle([[3, 2], [10, 1]]), frozenset({3, 4, 10}))
        self.assertEqual(P5.overlap_coef({1, 2, 3, 4}, {3, 4}), 1.0)
        self.assertEqual(P5.overlap_coef({1, 2, 3, 4}, {4, 5, 6, 7, 8}), 0.25)
        self.assertEqual(P5.overlap_coef(set(), {1}), 0.0)
        self.assertTrue(P5.is_bed_like({"kind": "bed"}))
        self.assertTrue(P5.is_bed_like({"name_hint": " Bett "}))
        self.assertTrue(P5.is_bed_like({"iou": {"bed": 0.7}}))
        self.assertFalse(P5.is_bed_like({"kind": "dwell", "name_hint": None, "iou": {"bed": 0.1}}))
        self.assertEqual([type(a).__name__ for a in P5.make_actors(J.parse_job("bed_w0000_0600_p5guests"))], ["GuestsUser"])
        self.assertTrue(P5.make_actors(J.parse_job("bed_w0000_0600_p5guests"))[0].pause)
        self.assertFalse(P5.make_actors(J.parse_job("bed_w0000_0600_p5guestsc"))[0].pause)
        self.assertEqual(P5.make_actors(J.parse_job("bed_w0000_0600_p4")), [])
        self.assertEqual([type(a).__name__ for a in P4.make_actors(J.parse_job("bed_w0000_0600_p5pets"))], ["PetsUser"])


# ---------------------------------------------------------------- the actors against the real core
class AnswerScriptTest(unittest.TestCase):
    def run_script(self, L):
        a = P5.AnswerScript()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + 120_000)
        return a, run

    def test_all_nine_steps_pass_on_the_real_core(self):
        L = places(2)
        with_proposal(L)
        a, run = self.run_script(L)
        s = {x["step"]: x for x in a.summary()["steps"]}
        self.assertEqual(sorted(s), sorted(E5.PLACE_STEPS + E5.WINDOW_STEPS + E5.BED_STEPS))
        self.assertTrue(all(x["ok"] for x in s.values()), {k: v for k, v in s.items() if not v["ok"]})
        self.assertEqual(s["stale_place"]["ignored"], [("s0005-1", "approve", "stale")])
        self.assertEqual(s["stale_place"]["stale_answers"], [0, 1])
        self.assertEqual(s["current_place"]["records"], 1)
        self.assertEqual(s["window_current"]["times"], s["window_current"]["expected"])
        self.assertEqual((s["window_gone"]["state"], a.summary()["done"]), ("same", True))
        self.assertEqual(len(L.review.zones), 1)
        # the zone, the window answer, the node's confirmation of it (p5.nw_applied) and the snooze
        self.assertEqual(run.p5.result()["saves"].get("review"), 4)

    def test_a_step_whose_item_does_not_exist_is_not_exercised_and_the_rest_runs(self):
        L = places(1)                                                              # no night-window proposal
        a, _ = self.run_script(L)
        s = a.summary()
        self.assertEqual(s["not_exercised"], ["window"])
        self.assertEqual(sorted(x["step"] for x in s["steps"]), sorted(E5.PLACE_STEPS + E5.BED_STEPS))
        self.assertFalse(s["done"])                                                # it looks again at the next nightly run

    def test_a_learner_that_does_not_say_stale_fails_the_steps(self):
        L = places(2)
        orig = L.handle
        L.handle = lambda ev: [o for o in orig(ev) if not isinstance(o, AnswerIgnored)]
        a, _ = self.run_script(L)
        s = {x["step"]: x for x in a.summary()["steps"]}
        self.assertFalse(s["stale_place"]["ok"])
        self.assertFalse(s["redelivery"]["ok"])
        self.assertTrue(s["current_place"]["ok"])

    def test_the_revision_moves_are_counted_between_two_nightly_runs(self):
        L = places(2)
        a = P5.AnswerScript()
        mini(L, a)
        rec = night_record(L, 7)
        a.on_night(rec)
        rec2 = night_record(L, 8)
        rec2["suggestions"][0]["rev"] = 2
        a.on_night(rec2)
        a.on_night(night_record(L, 9) | {"suggestions": rec2["suggestions"]})
        self.assertEqual(a.rev_moves, [{"night": 8, "ids": [rec2["suggestions"][0]["id"]]}])


class SetupUserTest(unittest.TestCase):
    def stays(self, a):
        a.add_day(SimpleNamespace(sessions=[("P1", "bed", "sleep", 7 * DAY + 23 * 3600, 8 * DAY, -2.3, 4.9),
                                            ("P1", "desk", "sit", 7 * DAY + 10 * 3600, 7 * DAY + 11 * 3600, 2.5, 2.2)]))

    def flow(self, L, mark_ok_at, hours=2):
        a = P5.SetupUser()
        run = mini(L, a, mark_ok_at=mark_ok_at)
        self.stays(a)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + (hours + 21) * 3600 * 1000)
        return a, run

    def test_the_first_evening_counts_the_taps_by_the_rule_of_the_spec(self):
        L = places(3)
        with_proposal(L)
        a, run = self.flow(L, mark_ok_at=3)
        s = a.summary()
        self.assertTrue(s["done"] and s["started"])
        self.assertEqual((s["places"], s["failed_marks"], s["window_approved"], s["day"]), (3, 2, True, 7))
        self.assertEqual(s["items_at_start"], ["night_window", "bed_needs_data", "s0005-1", "s0005-2", "s0005-3"])
        self.assertEqual(s["mark"]["ok"], True)
        # the window (1) + 3 presses + each place: the bed item stays selected in this script (no label is made), so a select
        # and an approve (2 each)
        self.assertEqual([k for _, k in s["tap_log"]], ["approve_window", "mark", "mark", "mark"] + ["select_place", "approve_place"] * 3)
        self.assertEqual(s["taps"], 1 + 3 + 6)
        self.assertEqual(len(L.review.zones), 3)
        self.assertEqual(s["text_entries"], 0)
        # the first press is 60 s into the stay, the window approval 2 s before it, the presses 30 s apart
        t0 = 7 * DAY * 1000 + 23 * 3600 * 1000
        log = s["tap_log"]
        self.assertEqual((log[0][0], log[1][0], log[2][0] - log[1][0]), (t0 + 58_000, t0 + 60_000, 30_000))
        self.assertTrue(log[1][0] < log[4][0])

    def test_without_a_bed_item_and_with_the_window_it_takes_one_tap_per_place(self):
        L = places(3)
        with_proposal(L)
        bed_label(L)
        a, run = self.flow(L, mark_ok_at=1)
        s = a.summary()
        self.assertEqual(s["items_at_start"], ["night_window", "s0005-1", "s0005-2", "s0005-3"])
        self.assertEqual(s["mark"]["note"], "no bed item")
        self.assertEqual([k for _, k in s["tap_log"]], ["approve_window"] + ["approve_place"] * 3)
        self.assertEqual((s["taps"], s["failed_marks"]), (4, 0))
        self.assertEqual(run.presses, 0)

    def test_no_window_item_means_no_tap_for_it(self):
        L = places(2)
        bed_label(L)
        a, _ = self.flow(L, mark_ok_at=1)
        self.assertEqual(a.summary()["window_approved"], False)
        self.assertEqual([k for _, k in a.summary()["tap_log"]], ["approve_place"] * 2)

    def test_a_press_that_never_succeeds_is_given_up_after_40_taps(self):
        L = places(2)
        a, run = self.flow(L, mark_ok_at=None)
        s = a.summary()
        self.assertEqual((s["failed_marks"], s["mark"]["ok"], sum(s["mark_reasons"].values())), (40, False, 40))
        self.assertIn("gave up", s["mark"]["note"])
        self.assertTrue(s["done"])
        self.assertEqual(len([k for _, k in s["tap_log"] if k == "mark"]), 40)
        self.assertEqual(s["taps"], 40 + 4)                                         # and the two places still cost a select each

    def test_nothing_happens_before_the_list_exists_and_the_stay_may_come_later(self):
        L = make()                                                                  # before the setup review: no items
        a = P5.SetupUser()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + 3 * 3600 * 1000)
        self.assertFalse(a.summary()["started"])
        # the stay is not known yet: the actor asks again an hour later
        L = places(2)
        a = P5.SetupUser()
        run = mini(L, a, mark_ok_at=1)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + 3600 * 1000)
        self.assertTrue(a.summary()["started"])
        self.assertEqual(a.summary()["taps"], 0)
        self.stays(a)
        run.drive(8 * DAY * 1000)
        self.assertTrue(a.summary()["done"])


class PushWatcherTest(unittest.TestCase):
    def test_the_three_restarts_and_the_acknowledgement(self):
        L = places(2)
        a = P5.PushWatcher()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        t0 = 7 * DAY_MS + 8 * 3600 * 1000
        run.drive(t0 + 60_000, tick=10_000)
        s = a.summary()
        self.assertEqual([r["point"] for r in s["restarts"]], ["R1", "R2", "R3"])
        self.assertEqual(s["restarts"][0]["t"], t0 - 500)                           # just before minute 480
        self.assertEqual(s["restarts"][0]["seq_before"], 0)
        self.assertEqual(len(run.restarts), 3)
        log = s["log"]
        self.assertEqual([(d["seq"], d["after_restart"], d["acked"]) for d in log],
                         [(1, "R1", 0), (1, "R2", 0), (1, "R2", 1), (1, "R3", 1)])
        self.assertEqual(log[0]["kind"], "setup")
        self.assertEqual(log[2]["acks"], ["fake"])
        self.assertEqual(s["acks_sent"], {"1": log[0]["t"] + 20_000})                 # 20 s after it first saw the request
        self.assertEqual(run.p5.result()["push_problems"], [])

    def test_a_request_that_is_not_acknowledged_by_a_phone_is_acknowledged_once_per_seq(self):
        L = places(2)
        a = P5.PushWatcher()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(7 * DAY_MS + 8 * 3600 * 1000 + 120_000, tick=10_000)
        self.assertEqual(len(a.acks_sent), 1)
        self.assertEqual(a.ack_due, {1: a.log[0]["t"] + 20_000})

    def test_no_restart_before_there_are_two_open_items(self):
        L = ready_learner()                                                         # the bed item only
        a = P5.PushWatcher()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(7 * DAY_MS + 9 * 3600 * 1000, tick=60_000)
        self.assertEqual(a.restarts, [])
        self.assertEqual(a.log, [])


class BedAnswersTest(unittest.TestCase):
    def nights(self, L, run, a, k):
        run.now = BASE_MS + k * DAY_MS + 210 * 60_000
        run.process(L.tick(run.now))
        a.on_night(night_record(L, k))
        run.drive(run.rel() + 120_000)

    def test_later_back_reject_away_and_undo(self):
        L = ready_learner()
        a = P5.BedAnswers()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + 120_000)
        self.nights(L, run, a, 8)
        self.nights(L, run, a, 9)
        s = {x["step"]: x for x in a.summary()["steps"]}
        self.assertEqual(list(s), ["bed_later", "bed_back", "bed_reject", "bed_away", "bed_undo"])
        self.assertTrue(all(x["ok"] for x in s.values()), {k: v for k, v in s.items() if not v["ok"]})
        self.assertEqual((s["bed_later"]["rev"], s["bed_back"]["rev"]), ([0, 1], [0, 2]))
        self.assertEqual((s["bed_reject"]["rev"], s["bed_undo"]["rev"]), ([2, 3], [3, 4]))
        self.assertEqual((s["bed_reject"]["entry"], s["bed_reject"]["entry_kind"]), ("adv:undo:bed", ["undo", "bed"]))
        self.assertEqual(s["bed_reject"]["label"], "Undo: No bed in view")
        self.assertEqual((s["bed_away"]["state"], s["bed_undo"]["state"]), ("suppressed", "open"))
        self.assertTrue(a.summary()["done"])
        self.assertEqual(L.review.suppressions, [])

    def test_a_bed_item_that_is_not_open_waits(self):
        L = ready_learner()
        bed_label(L)
        a = P5.BedAnswers()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + 120_000)
        self.assertEqual((a.summary()["steps"], a.summary()["phase"], a.summary()["done"]), ([], "later", False))

    def test_the_label_check_accepts_only_the_shell_text(self):
        entry = SimpleNamespace(verb="undo", kind="bed", id="adv:undo:bed", name="", tag="", until="", target="bed", rev=0)
        self.assertEqual(P5.BedAnswers._label(entry), "Undo: No bed in view")


class RejectUserTest(unittest.TestCase):
    def test_reject_records_the_form_and_the_mask_and_the_undo_clears_them(self):
        L = places(3)
        a = P5.RejectUser(undo_night=8)
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + 120_000)
        r = a.summary()["rejected"]
        self.assertEqual((r["id"], r["night"], r["ignored"], r["still_listed"]), ("s0005-1", 7, [], False))
        self.assertEqual(r["suppressions"], [{"type": "place", "night": 7, "ref": "s0005-1"}])
        self.assertEqual(set(r["mask"]), {"cx", "cy", "cells", "n0"})
        self.assertEqual(r["mask"]["cells"], r["cells_rle"])
        self.assertIsNone(a.summary()["undone"])
        run.now = BASE_MS + 8 * DAY_MS + 210 * 60_000
        run.process(L.tick(run.now))
        a.on_night(night_record(L, 8))
        run.drive(run.rel() + 120_000)
        u = a.summary()["undone"]
        self.assertEqual((u["night"], u["ignored"], u["suppressions"], u["mask"]), (8, [], 0, False))
        self.assertTrue(a.summary()["done"])

    def test_the_bed_place_is_not_rejected(self):
        L = places(3)
        L.ns.suggestions[0]["kind"] = "bed"                                          # the most confident place is the bed
        L.review.touch()
        a = P5.RejectUser()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + 120_000)
        self.assertEqual(a.summary()["rejected"]["id"], "s0005-2")

    def test_no_place_no_rejection(self):
        L = ready_learner()
        a = P5.RejectUser()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + 120_000)
        self.assertIsNone(a.summary()["rejected"])


class ZoneUserTest(unittest.TestCase):
    def test_all_five_tasks_on_the_real_core(self):
        L = places(4)
        with_proposal(L)                                                            # the schedule is ON: a bed zone is frozen
        a = P5.ZoneUser()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + 120_000)
        s = a.summary()
        self.assertTrue(s["done"] and not s["todo"], s["todo"])
        self.assertEqual([(t["task"], t["ok"]) for t in s["tasks"]],
                         [("approve_desk", True), ("approve_desk2", True), ("rename", True), ("approve_bett", True),
                          ("remove", True)])
        d = {t["task"]: t for t in s["tasks"]}
        self.assertEqual(d["approve_desk"]["echo"], [""])
        self.assertEqual(d["approve_desk2"]["zone"]["name"], "desk 2")
        self.assertEqual(d["approve_bett"]["zone"]["kind"], "bed")
        self.assertTrue(d["approve_bett"]["zone"]["frozen"])
        self.assertEqual(d["approve_bett"]["bed_item"]["state"], "withdrawn")
        self.assertTrue(d["remove"]["open_again"])
        self.assertEqual([z["name"] for z in s["snapshots"][-1]["zones"]], ["Office", "desk 2"])
        self.assertEqual(E5._p57_invariants(s), [])
        c = E5.crit_p57([{"job": "bed_w0000_0600_p5zone", "seed": 1, "status": "ok", "core_errors": 0, "window": "w0000_0600",
                          "job_spec": {"script": "p5zone"}, "nights": [{"night": 7, "p5": {"items": []}}],
                          "p4": {"actors": {"zone_user": s}}}])
        self.assertEqual(c["status"], E5.PASS)

    def test_a_task_waits_for_its_precondition(self):
        L = places(1)
        a = P5.ZoneUser()
        run = mini(L, a)
        a.on_night(night_record(L, 7))
        run.drive(run.rel() + 120_000)
        self.assertEqual(a.summary()["todo"], ["approve_desk2", "rename", "approve_bett", "remove"])   # one place: the first task only
        self.assertEqual([t["task"] for t in a.summary()["tasks"]], ["approve_desk"])
        self.assertFalse(a.summary()["done"])


class GuestsAndPetsTest(unittest.TestCase):
    def day(self):
        t0, t1 = 5 * DAY + 15 * 3600, 5 * DAY + 17 * 3600
        return SimpleNamespace(sessions=[("P1", "desk", "sit", 5 * DAY + 9 * 3600, 5 * DAY + 11 * 3600, 2.5, 2.2),
                                         ("V", "visitor", "sit", t0, t1, -0.4, 2.0)]), t0, t1

    def run_pair(self, pause):
        L = ready_learner()
        a = P5.GuestsUser(pause)
        run = mini(L, a)
        rec, t0, t1 = self.day()
        a.add_day(rec)
        run.drive((t1 + 600) * 1000)
        return a, run, t0, t1

    def test_the_pause_is_switched_on_before_and_off_after_each_session(self):
        a, run, t0, t1 = self.run_pair(True)
        s = a.summary()
        self.assertEqual(s["switches"], [{"t": t0 * 1000 - 60_000, "on": True}, {"t": t1 * 1000 + 90_000, "on": False}])
        self.assertEqual([x["tag"] for x in s["samples"]], ["start", "on+5min", "middle", "off-5min"])
        self.assertTrue(all(x["learning_paused"] for x in s["samples"]), s["samples"])
        self.assertFalse(run.learner.status().attributes["learning_paused"])               # released
        self.assertEqual(len(s["sessions"]), 1)
        self.assertEqual(s["sessions"][0]["agent"], "V")

    def test_the_control_samples_the_same_times_and_never_switches(self):
        a, run, t0, t1 = self.run_pair(False)
        s = a.summary()
        self.assertEqual(s["switches"], [])
        self.assertEqual([x["tag"] for x in s["samples"]], ["start", "on+5min", "middle", "off-5min"])
        self.assertFalse(any(x["learning_paused"] for x in s["samples"]))
        self.assertEqual(s["pause"], False)
        b, _, _, _ = self.run_pair(True)
        self.assertEqual([x["t"] for x in s["samples"]], [x["t"] for x in b.summary()["samples"]])

    def test_overlapping_sessions_keep_the_pause_on_until_the_last_one_ends(self):
        L = ready_learner()
        a = P5.GuestsUser(True)
        run = mini(L, a)
        a.add_day(SimpleNamespace(sessions=[("G", "guest", "sit", 5 * DAY + 100, 5 * DAY + 5000, 0.2, 2.4),
                                            ("V", "visitor", "sit", 5 * DAY + 3000, 5 * DAY + 9000, -0.4, 2.0)]))
        run.drive(5 * DAY_MS + 12_000_000)
        self.assertEqual([x["on"] for x in a.summary()["switches"]], [True, False])
        self.assertEqual(a.summary()["switches"][1]["t"], (5 * DAY + 9000) * 1000 + 90_000)

    def test_the_stays_near_a_guest_spot_are_collected_after_a_nightly_run(self):
        a = P5.GuestsUser(True)
        a.run = SimpleNamespace(learner=SimpleNamespace(stays=lambda: (
            StayView(1000, 4000, 3000, 0.25, 2.35, 1.0, 0.0, "moving", "walk", "2026-01-25"),      # at the guest spot
            StayView(1000, 4000, 3000, 3.0, 5.0, 1.0, 0.0, "moving", "walk", "2026-01-25"),        # far away
            StayView(5000, 6000, 1000, 0.8, 2.4, 1.0, 0.0, "moving", "walk", "2026-01-25")), paused_stays=lambda: 2))
        a.sessions.append({"agent": "G", "x": 0.2, "y": 2.4, "t0": 0, "t1": 1, "on": 0, "off": 1, "place": "guest"})
        a.nightly({"night": 20})
        self.assertEqual(len(a.near), 2)
        self.assertEqual(sorted(x["dist"] for x in a.summary()["stays_near"]), [0.071, 0.6])
        self.assertEqual(a.summary()["paused_stays"], 2)

    def test_pets_are_switched_on_in_the_first_second(self):
        L = ready_learner()
        a = P5.PetsUser()
        run = mini(L, a)
        run.drive(5000)
        self.assertTrue(L.p5.setting("pets"))
        self.assertEqual(a.summary(), {"on": True, "at_ms": 1000})
        self.assertTrue(L.status().attributes["settings"]["pets"])


# ---------------------------------------------------------------- the scripts
def fake_dump(**over):
    base = {"result": {"status": "ok", "labels": {"desk": {"ok": True}}, "counters": {"batches": 10},
                       "nights": [{"night": 1, "suggestions": [{"id": "s1", "rev": 1, "cells_rle": [[1, 2]]}],
                                   "candidates": 2, "labels": [], "counters": {"batches": 5}, "date": "2026-01-06"}],
                       "timing": {"cpu_s": 1.0}, "stream": {"messages": 4}},
            "final_state": {"schema": 4, "writer": "radar_zone_learner 0.4.0", "phase": "p4", "params": "dbe9b3e9fea0",
                            "meta": {"night": 1}, "zones": [{"zid": "z0001", "name": "Desk", "kind": "dwell", "src": "approve",
                                                             "ref": "s1", "cx": 1.0, "cy": 2.0, "cells": [[1, 2]], "night": 7,
                                                             "created_s": 5}],
                            "suggestions": [{"id": "s2", "type": "ignore_zone", "evidence": {"n": 3}},
                                            {"id": "s1", "type": "dwell_zone", "evidence": {"he_share": 0.3}}],
                            "settings": {"spot_name": ""}}}
    base.update(over)
    return base


class CompareRunsTest(unittest.TestCase):
    def write(self, d, name, dump):
        Path(d).mkdir(parents=True, exist_ok=True)
        (Path(d) / name).write_text(json.dumps(dump), encoding="utf-8")

    def p5_side(self):
        d = fake_dump()
        st = d["final_state"]
        st.update(writer="radar_zone_learner 0.5.0", phase="p5", p5={"settings": {"pets": True}})
        z = st["zones"][0]
        z.update(poly_cm=[[1, 2]], rev=0, upd=7, srev=1, also=[])
        st["suggestions"][0]["evidence"]["he_share"] = 0.6
        d["result"]["timing"] = {"cpu_s": 9.0}
        d["result"]["stream"] = {"messages": 5}
        d["result"]["calls"] = {"handle": 1}
        return d

    def test_identical_folders_pass_and_the_exempt_keys_are_ignored(self):
        with tmpdir() as t:
            a, b = Path(t) / "a", Path(t) / "b"
            self.write(a, "bed_w0000_0600_s1.json", fake_dump())
            self.write(b, "bed_w0000_0600_s1.json", self.p5_side())
            res = CR.compare_dirs(a, b)
            self.assertEqual(res["status"], "PASS", res)
            self.write(b, "bed_w0000_0600_s1.json", fake_dump())
            self.assertEqual(CR.compare_dirs(a, b)["status"], "PASS")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(CR.main(["--a", str(a), "--b", str(b)]), 0)
            self.assertIn("P5-R1: PASS (1 runs)", out.getvalue())

    def mutate(self, fn):
        d = self.p5_side()
        fn(d)
        with tmpdir() as t:
            a, b = Path(t) / "a", Path(t) / "b"
            self.write(a, "bed_w0000_0600_s1.json", fake_dump())
            self.write(b, "bed_w0000_0600_s1.json", d)
            return CR.compare_dirs(a, b)

    def test_one_difference_in_a_compared_field_fails(self):
        res = self.mutate(lambda d: d["result"]["nights"][0]["suggestions"][0].update(rev=2))
        self.assertEqual(res["status"], "FAIL")
        self.assertIn("result.nights[night 1].suggestions[0]/rev: 1 != 2", res["runs"]["bed_w0000_0600_s1.json"]["diffs"])
        self.assertEqual(self.mutate(lambda d: d["result"]["nights"][0]["counters"].update(batches=6))["status"], "FAIL")
        self.assertEqual(self.mutate(lambda d: d["result"]["nights"][0].update(candidates=3))["status"], "FAIL")
        self.assertEqual(self.mutate(lambda d: d["result"]["counters"].update(batches=11))["status"], "FAIL")
        self.assertEqual(self.mutate(lambda d: d["result"]["labels"]["desk"].update(ok=False))["status"], "FAIL")
        self.assertEqual(self.mutate(lambda d: d["result"].update(status="over_budget"))["status"], "FAIL")
        self.assertEqual(self.mutate(lambda d: d["result"]["nights"].append({"night": 2}))["status"], "FAIL")

    def test_a_non_exempt_state_key_fails(self):
        self.assertEqual(self.mutate(lambda d: d["final_state"]["zones"][0].update(name="Office"))["status"], "FAIL")
        self.assertEqual(self.mutate(lambda d: d["final_state"]["meta"].update(night=2))["status"], "FAIL")
        self.assertEqual(self.mutate(lambda d: d["final_state"].update(params="other"))["status"], "FAIL")
        self.assertEqual(self.mutate(lambda d: d["final_state"].update(extra_key=1))["status"], "FAIL")
        self.assertEqual(self.mutate(lambda d: d["final_state"]["suggestions"][1]["evidence"].update(n=4))["status"], "FAIL")
        # he_share is exempt only inside an ignore_zone suggestion
        self.assertEqual(self.mutate(lambda d: d["final_state"]["suggestions"][1]["evidence"].update(he_share=0.9))["status"], "FAIL")

    def test_a_missing_run_fails_and_the_exempt_set_is_exact(self):
        with tmpdir() as t:
            a, b = Path(t) / "a", Path(t) / "b"
            self.write(a, "x_s1.json", fake_dump())
            self.write(a, "y_s1.json", fake_dump())
            self.write(b, "x_s1.json", fake_dump())
            res = CR.compare_dirs(a, b)
            self.assertEqual((res["status"], res["runs"]["y_s1.json"]["diffs"]), ("FAIL", ["the run exists in A only"]))
            self.assertEqual(CR.compare_dirs(Path(t) / "none", Path(t) / "nothing")["status"], "FAIL")
        self.assertEqual(CR.EXEMPT_TOP, ("p5", "phase", "writer"))
        self.assertEqual(CR.EXEMPT_ZONE, ("poly_cm", "rev", "upd", "srev", "also"))
        st = CR.normalise_state(self.p5_side()["final_state"])
        self.assertEqual(sorted(st["zones"][0]), ["cells", "created_s", "cx", "cy", "kind", "name", "night", "ref", "src", "zid"])
        self.assertNotIn("p5", st)
        self.assertEqual(st["suggestions"][0]["evidence"], {"n": 3})
        self.assertEqual(st["suggestions"][1]["evidence"], {"he_share": 0.3})            # a dwell_zone keeps it

    def test_usage_errors(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CR.main(["--a", "no_such_dir", "--b", "none"]), 2)
            self.assertEqual(CR.main(["--a", "x"]), 2)


class DumpRunTest(unittest.TestCase):
    def test_a_short_run_writes_the_result_and_the_final_state(self):
        with tmpdir() as t, contextlib.redirect_stdout(io.StringIO()) as out:
            rc = dump_run.main(["--tree", str(ADDON), "--jobs", "bed_w0000_0600", "--seeds", "1", "--nights", "1", "--out", t])
            self.assertEqual(rc, 0)
            d = json.loads((Path(t) / "bed_w0000_0600_s1.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(d), ["final_state", "result"])
            self.assertEqual(d["result"]["status"], "ok")
            self.assertEqual(d["result"]["nights"][-1]["night"], 1)
            self.assertEqual(d["final_state"]["schema"], 4)
            self.assertNotIn("p5", d["result"])                                   # a P3 job has no P5 record
            self.assertIn("bed_w0000_0600 s1 ok", out.getvalue())
            self.assertEqual(CR.compare_dirs(Path(t), Path(t))["status"], "PASS")

    def test_usage_errors_and_seed_parsing(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dump_run.main(["--tree", str(ADDON), "--jobs", "bed_w0000_0600", "--out", "x", "--seeds", "0-9"]), 2)
            self.assertEqual(dump_run.main(["--tree", "no_such_tree", "--jobs", "bed_w0000_0600", "--out", "x"]), 2)
            self.assertEqual(dump_run.main(["--tree", str(ADDON), "--jobs", "bed_w0000_0600", "--out", "x", "--nights", "40"]), 2)
        self.assertEqual(dump_run._parse_seeds("1-3,5"), (1, 2, 3, 5))


def p4_form_state():
    """The state of a learner with two approved zones, written as a 0.4.0 app wrote it: no p5 section, no zone extras."""
    L = places(3)
    L.handle(__import__("radar_zone_learner.core.types", fromlist=["ReviewAnswer"]).ReviewAnswer(
        "s0005-1", "approve", None, False, L.now + 1, name="Desk"))
    L.handle(__import__("radar_zone_learner.core.types", fromlist=["ReviewAnswer"]).ReviewAnswer(
        "s0005-2", "approve", None, False, L.now + 2, name="Sofa"))
    st = json.loads(json.dumps(L.export_state()))
    st.pop("p5", None)
    for z in st["zones"]:
        for k in CC_EXTRAS:
            z.pop(k, None)
    st["writer"], st["phase"] = "radar_zone_learner 0.4.0", "p4"
    return st


CC_EXTRAS = ("poly_cm", "rev", "upd", "srev", "also")


class CompatCheckTest(unittest.TestCase):
    def test_a_0_4_0_file_loads_with_the_defaults_survives_idle_days_and_a_rename(self):
        st = p4_form_state()
        self.assertEqual(len(st["zones"]), 2)
        res = CC.check_p4_state_in_p5(st, days=3)
        self.assertEqual(res["status"], "PASS", res["problems"])
        self.assertEqual((res["notes"]["zones"], res["notes"]["nightly_runs"], res["notes"]["renamed"]), (2, 3, True))

    def test_a_state_p5_refuses_or_changes_is_a_fail(self):
        st = p4_form_state()
        st["schema"] = 9
        self.assertEqual(CC.check_p4_state_in_p5(st)["status"], "FAIL")
        st = p4_form_state()
        st["zones"][0].pop("ref")
        self.assertIn("refused", " ".join(CC.check_p4_state_in_p5(st)["problems"]))

    def test_a_state_without_a_zone_is_not_exercised(self):
        st = p4_form_state()
        st["zones"] = []
        res = CC.check_p4_state_in_p5(st, days=3)
        self.assertEqual(res["status"], "INCOMPLETE")
        self.assertIn("not exercised", " ".join(res["problems"]))

    def test_the_p5_state_in_the_baseline(self):
        L = places(3)
        L.handle(__import__("radar_zone_learner.core.types", fromlist=["ReviewAnswer"]).ReviewAnswer(
            "s0005-1", "approve", None, False, L.now + 1, name="Desk"))
        st = json.loads(json.dumps(L.export_state()))
        self.assertIn("p5", st)
        with tmpdir() as t:
            f = Path(t) / "s.json"
            f.write_text(json.dumps(st), encoding="utf-8")
            loaded = CC.load_only(ADDON, f)                                           # this tree stands in for the baseline
        self.assertTrue(loaded["loaded"])
        res = CC.check_p5_state_in_baseline(st, loaded)
        self.assertEqual(res["status"], "PASS", res["problems"])
        self.assertTrue(res["notes"]["p5_in_file"])
        # what is lost shows
        bad = dict(loaded, zones=[], suggestions=loaded["suggestions"][:-1])
        res = CC.check_p5_state_in_baseline(st, bad)
        self.assertEqual(res["status"], "FAIL")
        self.assertTrue(any("zone ids" in p for p in res["problems"]) and any("suggestion ids" in p for p in res["problems"]))
        z = dict(loaded["zones"][0], name="Other")
        self.assertEqual(CC.check_p5_state_in_baseline(st, dict(loaded, zones=[z]))["status"], "FAIL")
        self.assertEqual(CC.check_p5_state_in_baseline(st, {"loaded": False, "error": "StateError: x"})["status"], "FAIL")
        st2 = dict(st, zones=[])
        self.assertEqual(CC.check_p5_state_in_baseline(st2, dict(loaded, zones=[]))["status"], "INCOMPLETE")

    def test_the_idle_start_is_after_the_last_nightly_run(self):
        st = {"meta": {"last_extraction_date": "2026-01-21"}}
        t = CC.idle_start_ms(st)
        self.assertEqual(datetime.datetime.fromtimestamp(t / 1000, datetime.timezone.utc).isoformat(), "2026-01-21T03:31:00+00:00")

    def test_usage_errors(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CC.main([]), 2)
            self.assertEqual(CC.main(["--base", "no_such_tree", "--out", "x"]), 2)
            self.assertEqual(CC.main(["--load-only"]), 2)


class ProbeStateTest(unittest.TestCase):
    def test_the_full_state_has_every_cap_near_full_and_loads(self):
        st = RP.heavy_state()
        L = Learner(DEFAULT_PARAMS, FixedOffsetTime(0), json.loads(json.dumps(st)), DEFAULT_P5)
        self.assertEqual((len(st["suggestions"]), len(st["zones"]), len(st["labels"]), len(st["suppressions"])), (200, 100, 100, 200))
        p5 = st["p5"]
        self.assertEqual((len(p5["tags"]["by_id"]), len(p5["later"]), len(p5["masks"]), len(p5["real"])), (400, 400, 200, 50))
        self.assertEqual((len(L.review.zones), len(L.label_list), len(L.review.suppressions)), (100, 100, 200))
        its = L.review.items(L)
        self.assertEqual((len([i for i in its if i.tier == 2]), L.review.more), (12, 0))
        self.assertGreater(len(L.zones()), 199)
        self.assertEqual(L.export_state()["p5"]["tags"]["n"], 400)
        self.assertEqual(RP.P5_LIMITS["memory_growth_mb"], 2.0)


# ---------------------------------------------------------------- the driver and the actors on the real simulator
class RealRunTest(unittest.TestCase):
    """One short end-to-end run with the real core and the simulated bedroom (about 20 s): the first setup review (job
    p5setup, 8 nights) with the reject user, the push watcher (three restarts of the learner in the middle of the
    stream), the pets switch and the guests pause added. The answer script, the bed answers and the zone user run
    against the real core in the mini runs above."""

    @classmethod
    def setUpClass(cls):
        run = D.Run(J.parse_job("bed_w0000_0600_p5setup", nights=8), 1)
        for a in (P5.RejectUser(), P5.PushWatcher(), P5.PetsUser(), P5.GuestsUser(True)):
            a.attach(run)
            run.stream.world.on_day.append(a.add_day)
            run.actors.append(a)
        run.run()
        cls.res = run.result()

    def actor(self, name):
        return self.res["p4"]["actors"][name]

    def test_the_run_is_clean_and_every_night_has_its_p5_record(self):
        res = self.res
        self.assertEqual((res["status"], res["core_errors"]), ("ok", 0), res.get("first_error"))
        self.assertEqual(res["job_spec"]["script"], "p5setup")
        for n in res["nights"]:
            self.assertEqual(sorted(n["p5"]), ["advanced", "items", "next_zone_n", "p5", "paused_stays", "push_request",
                                               "selected", "status", "suppressions", "zones"], n["night"])
        self.assertEqual(sorted(res["p5"]), ["ignored_log", "push_checked", "push_fields", "push_log", "push_problems",
                                             "restarts", "saves"])
        # the night record is the learner's own list at the nightly run: tier-1 items carry their data
        night7 = [n for n in res["nights"] if n["night"] == 7][0]["p5"]["items"]
        bed = [i for i in night7 if i["id"] == "bed_needs_data"][0]
        self.assertIn("press Mark spot here once", bed["data"]["text"])
        self.assertTrue(all("data" not in i for i in night7 if i["tier"] == 2))
        for name in ("setup_user", "reject_user", "push_watcher", "pets_user", "guests_user"):
            self.assertIn(name, res["p4"]["actors"])

    def test_the_push_watcher_restarted_the_learner_three_times_and_the_flow_held(self):
        s = self.actor("push_watcher")
        self.assertEqual([r["point"] for r in s["restarts"]], ["R1", "R2", "R3"])
        self.assertEqual(len(self.res["p5"]["restarts"]), 3)
        first = [d for d in s["log"] if d["after_restart"] == "R1"][0]
        self.assertEqual((first["seq"], first["kind"], first["acked"]), (1, "setup", 0))
        self.assertEqual([d["seq"] for d in s["log"] if d["after_restart"] in ("R2", "R3")][:3], [1, 1, 1])
        self.assertEqual(self.res["p5"]["push_problems"], [])
        self.assertGreater(self.res["p5"]["push_checked"], 3)
        c = E5.crit_p52([self.res | {"job_spec": dict(self.res["job_spec"], script="p5push")}])
        self.assertNotEqual(c["status"], E5.FAIL, c["details"])
        # the request survived the restarts in the state: setup_sent is true from the night after it
        self.assertTrue(self.res["nights"][-1]["p5"]["p5"]["push"]["setup_sent"])

    def test_the_pets_switch_and_the_guests_pause_reached_the_learner(self):
        self.assertTrue(self.actor("pets_user")["on"])
        for n in self.res["nights"]:
            self.assertTrue(n["p5"]["status"]["settings"]["pets"], n["night"])
        g = self.actor("guests_user")
        self.assertEqual([x["on"] for x in g["switches"]], [True, False])              # the visitor of day 5
        self.assertEqual(g["sessions"][0]["agent"], "V")
        mid = [x for x in g["samples"] if x["tag"] == "middle"][0]
        self.assertEqual((mid["state"], mid["learning_paused"]), ("learning", True))
        self.assertFalse(self.res["nights"][-1]["p5"]["status"]["learning_paused"])
        self.assertIsNotNone(g["paused_stays"])

    def test_the_setup_user_did_the_first_evening_and_the_reject_user_rejected_a_place(self):
        s = self.actor("setup_user")
        self.assertTrue(s["started"] and s["done"], s)
        self.assertEqual(s["day"], 7)
        kinds = [k for _, k in s["tap_log"]]
        self.assertEqual(len(kinds), s["taps"])
        self.assertEqual(kinds.count("mark"), s["failed_marks"] + (1 if (s["mark"] or {}).get("ok") else 0))
        zones = self.res["nights"][-1]["p5"]["zones"]
        # one zone record per approved place; a successful 'Mark spot here' (the person awake in bed of p5setup) adds a label
        self.assertEqual(kinds.count("approve_place"), len([z for z in zones if z["src"] == "approve"]))
        self.assertEqual(len([z for z in zones if z["src"] == "mark"]), 1 if (s["mark"] or {}).get("ok") else 0)
        r = self.actor("reject_user")["rejected"]
        self.assertIsNotNone(r)
        self.assertEqual(r["suppressions"], [{"type": "place", "night": r["night"], "ref": r["id"]}])
        self.assertEqual(r["mask"]["cells"], r["cells_rle"])
        self.assertNotIn(r["id"], [z["ref"] for z in zones])
        c = E5.crit_p54([self.res])
        self.assertIn(c["status"], (E5.PASS, E5.FAIL, E5.INCOMPLETE))
        self.assertEqual(c["details"]["seeds"][0]["taps"], s["taps"])

    def test_the_whole_report_is_built_from_the_real_result(self):
        p5 = E5.evaluate_p5([self.res])
        self.assertEqual([c["id"] for c in p5["criteria"]][:2], ["P5-1", "P5-2"])
        text = "\n".join(E5.render_p5(p5))
        self.assertIn("P5-4", text)
        self.assertEqual(json.loads(json.dumps(p5))["gate"], p5["gate"])
        rep = E5.build_report([self.res])
        self.assertEqual(rep["kind"], "p5")
        json.dumps(rep)


if __name__ == "__main__":
    unittest.main()
