"""P4 replay: late sleep onsets, lux, scripted extras, fake node, scenarios and the numbered checks (OA 12 P4)."""
import datetime
import random
import unittest

from radar_zone_learner.core.frames import parse_frames
from radar_zone_learner.core.learner import Learner
from radar_zone_learner.core.params import DEFAULT_PARAMS
from radar_zone_learner.core.types import (CommandResult, FixedOffsetTime, FramesMessage, LuxSample,
                                           NightLearningChanged, NightLearningSwitch, NightlyDone, NodeCommand,
                                           StatusChanged)
from replay import driver as D
from replay import evaluate as E
from replay import evaluate_p4 as E4
from replay import framegen as G
from replay import jobs as J
from replay import p4_scenarios as P4
from replay import world as W
from replay.truth import NightTruth

BASE_ORD = datetime.date(2026, 1, 5).toordinal()
DAY = W.DAY


def core_ready() -> bool:
    try:
        Learner(DEFAULT_PARAMS, FixedOffsetTime(0))
        return True
    except NotImplementedError:
        return False


# ---------------------------------------------------------------- world, truth, lux, overlay
class LateOnsetTest(unittest.TestCase):
    """The simulator drops the sleep onset after 00:00 (3 of 280 nights); the replay keeps it."""

    def test_world_records_the_dropped_onsets_without_changing_the_world(self):
        cfg = W.make_cfg(bed=1, nw="00:00-06:00")
        rw = W.RunWorld(2, cfg)
        late = [(d, rw.day(d).late) for d in range(5)]
        self.assertEqual([d for d, x in late if x], [3])
        ag, onset = late[3][1][0]
        self.assertEqual(ag, "P1")
        self.assertTrue(2 <= (onset - 4 * DAY) / 60 <= 13, onset)
        # plan_day without the new argument draws the same numbers and returns the same sessions
        a, b = random.Random(7), random.Random(7)
        self.assertEqual(W.plan_day(a, 3, cfg), W.plan_day(b, 3, cfg, []))
        self.assertEqual(a.random(), b.random())

    def test_three_late_onsets_in_five_seeds_of_56_nights(self):
        cfg = W.make_cfg(bed=1, nw="00:00-06:00")
        found = []
        for seed in range(1, 6):
            rw = W.RunWorld(seed, cfg)
            for d in range(56):
                for ag, s0 in rw.day(d).late:
                    found.append((seed, d, round((s0 - (d + 1) * DAY) / 60, 1)))
                rw.forget_before(d)
        self.assertEqual(len(found), 3, found)                 # OA 7.5: 3 of 280 nights, 00:02-00:12
        self.assertTrue(all(2 <= m <= 13 for _, _, m in found), found)

    def test_night_truth_keeps_the_late_onset(self):
        cfg = W.make_cfg(bed=1, nw="00:00-06:00")
        rw = W.RunWorld(2, cfg)
        nt = NightTruth()
        rw.on_day.append(nt.add_day)
        rw.ensure(6)
        self.assertEqual(nt.late, [3])
        self.assertEqual(sorted(nt.onset_s), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(sorted(nt.wake_s), [-1, 0, 1, 2, 3, 4, 5])
        self.assertGreater(nt.nm(3, nt.onset_s[3]), 720)         # after 00:00 on the night axis (12:00 + 12 h)
        self.assertLess(nt.nm(2, nt.onset_s[2]), 720)
        self.assertTrue(1000 < nt.nm(0, nt.wake_s[0]) < 1250)    # about 06:50 on a weekday

    def test_late_overlay_moves_the_sleeper_in_at_the_onset(self):
        cfg = W.make_cfg(bed=1, nw="00:00-06:00")
        rw = W.RunWorld(2, cfg)
        rec = rw.day(4)
        onset = rw.day(3).late[0][1]
        tracks, occ = G.late_overlay(rec, onset, random.Random(1))
        arrival = [t for t in tracks if t["agent"] == "P1" and t["pts"][0][0] < onset]
        self.assertEqual(len(arrival), 1)                           # only the arrival: the bursts before it are gone
        pts = arrival[0]["pts"]
        self.assertGreaterEqual(pts[-1][0], onset + 4)
        bx, by = W.SPOT["bed"]
        self.assertLess(abs(pts[-1][1] - bx) + abs(pts[-1][2] - by), 0.6)
        for t in tracks:
            if t["agent"] == "P1" and t is not arrival[0]:
                self.assertGreaterEqual(t["pts"][0][0], onset)
        self.assertEqual(rec.tracks, rw.day(4).tracks)               # the record is not changed
        first = [o for o in occ if o[2] == "P1"][0]
        self.assertGreater(first[0], rec.d * DAY)                   # the occupancy starts with the walk
        self.assertLessEqual(first[0], onset)


class LuxStreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = W.make_cfg(bed=1, nw="00:00-06:00")
        cls.rs = G.RunStream(cls.cfg, 2, 6, lux=True)
        cls.lux = []
        for recv, order, ev in cls.rs.events():
            if isinstance(ev, LuxSample):
                cls.lux.append((recv - G.BASE_MS, ev.lux, ev.recv_ms))

    def test_ten_second_cadence_and_the_first_sample_after_the_first_wake(self):
        t = [x[0] for x in self.lux]
        self.assertEqual(t, sorted(t))
        gaps = {b - a for a, b in zip(t, t[1:])}
        self.assertTrue(all(9900 <= g <= 10100 for g in gaps), sorted(gaps)[:5])
        self.assertGreater(t[0], 6 * 3600 * 1000)                  # nothing is known before day 0's wake
        self.assertTrue(all(x[0] + G.BASE_MS == x[2] for x in self.lux))

    def test_dark_from_the_onset_to_the_wake_also_after_midnight(self):
        nt = self.rs.lux
        for night, onset in nt.onsets.items():
            if night + 1 > 5:
                continue
            wake = nt.wakes[night]
            dark = [(t, v) for t, v, _ in self.lux if onset * 1000 + 250_000 <= t <= wake * 1000 - 1000]
            self.assertTrue(dark)
            self.assertTrue(all(v < DEFAULT_PARAMS.night_lux for _, v in dark), night)
            lit = [v for t, v, _ in self.lux if wake * 1000 + 310_000 <= t <= (wake + 3600) * 1000]
            self.assertTrue(lit and all(v > 100 for v in lit), night)
            before = [v for t, v, _ in self.lux if (onset - 3600) * 1000 <= t <= onset * 1000 - 1000]
            self.assertTrue(before and all(v > 100 for v in before), night)
        self.assertEqual(self.rs.late_nights, [3])
        self.assertGreater(nt.onsets[3], 4 * DAY)                  # the night-3 onset is after 00:00

    def test_lux_is_off_by_default(self):
        rs = G.RunStream(self.cfg, 2, 2)
        n = 0
        for recv, order, ev in rs.events():
            n += isinstance(ev, LuxSample)
            if recv > G.BASE_MS + 3600_000:
                break
        self.assertEqual(n, 0)


class ExtrasTest(unittest.TestCase):
    def test_extras_are_scripted_people_with_one_point_a_second(self):
        job = J.parse_job("bed_w0000_0600_p4mark")
        ex = job.extras()
        self.assertEqual([e[0] for e in ex], ["X1", "X2"])
        tr = G.extra_tracks(random.Random(1), ex)
        self.assertEqual(len(tr), 2)
        self.assertEqual(len(tr[0]["pts"]), 1500)
        self.assertEqual([p[0] for p in tr[0]["pts"]][:3], [8 * DAY + 21 * 3600 + k for k in range(3)])
        self.assertEqual(J.parse_job("bed_w0000_0600_p4").extras(), [])
        self.assertEqual(J.parse_job("bed_w0000_0600").extras(), [])

    def test_two_people_show_in_the_frames_of_their_evening(self):
        job = J.parse_job("bed_w0000_0600_p4mark")
        rs = G.RunStream(job.cfg(), 1, 9, lux=True, extras=job.extras())
        a = G.BASE_MS + job.extras()[0][3] * 1000 + 600_000
        n_two = frames = 0
        for recv, order, ev in rs.events():
            if recv < a:
                continue
            if recv > a + 20_000:
                break
            if isinstance(ev, FramesMessage):
                for f in parse_frames(ev.payload).frames:
                    frames += 1
                    # two targets at the bed (x < -1 m, y > 4.5 m)
                    n_two += sum(1 for t in f.targets if t.y_mm > 4500 and t.x_mm < -1000) >= 2
        self.assertGreater(frames, 100)
        self.assertGreater(n_two, 0.8 * frames)


class JobsTest(unittest.TestCase):
    def test_p4_variants_and_profiles(self):
        for v in J.P4_VARIANTS:
            job = J.parse_job("bed_w0000_0600_%s" % v)
            self.assertEqual(job.variant, v)
            self.assertTrue(job.is_p4)
            self.assertTrue(job.lux)
            self.assertFalse(job.labels)
            self.assertFalse(job.gated)
        self.assertEqual(J.parse_job("bed_w0000_0600_p4nl").nl, 7)
        self.assertEqual(J.parse_job("bed_w0000_0600_p4nl").cfg()["nl"], 7)
        self.assertEqual(J.parse_job("bed_none_p4nop2").variant, "p4nop2")
        self.assertFalse(J.parse_job("bed_none_p4nop2").p2)
        p3 = J.parse_job("bed_w0000_0600")
        self.assertFalse(p3.is_p4 or p3.lux or p3.p2 is False)
        self.assertTrue(p3.labels)
        self.assertEqual(len(J.runs_of(J.PROFILES["p4"])), 47)
        self.assertLess(J.estimate_days(J.PROFILES["p4"]), 1500)
        self.assertEqual(len(J.runs_of(J.PROFILES["p4smoke"])), 1)


# ---------------------------------------------------------------- the fake node
class FakeNodeTest(unittest.TestCase):
    def cmd(self, sched=None, expect=None, kind="schedule", start=None, end=None, token="t1"):
        return NodeCommand(token, kind, sched, start, end, expect, "test")

    def test_schedule_off_is_applied_echoed_and_confirmed(self):
        n = P4.FakeNode(True, True, 0, 360)
        out = n.command(self.cmd(False, True), G.BASE_MS + 5000)
        self.assertEqual([type(e).__name__ for _, e in out], ["ScheduleState", "CommandResult"])
        self.assertEqual((out[0][1].sched, out[0][1].start_min, out[0][1].end_min), (False, 0, 360))
        self.assertTrue(out[1][1].ok)
        self.assertIsNone(out[1][1].reason)
        self.assertLess(out[0][0], out[1][0])
        self.assertFalse(n.sched)
        self.assertEqual(n.off_runs, [[5000, None]])
        n.command(self.cmd(True, False, token="t2"), G.BASE_MS + 9000)
        self.assertEqual(n.off_runs, [[5000, 9000]])
        self.assertTrue(n.sched)

    def test_refusals_and_no_change(self):
        n = P4.FakeNode(True, False, 0, 360)
        res = n.command(self.cmd(True, True), G.BASE_MS)                     # the learner believes ON, it is OFF
        self.assertEqual([type(e).__name__ for _, e in res], ["CommandResult"])
        self.assertEqual(res[0][1].reason, "changed_by_user")
        self.assertFalse(res[0][1].ok)
        self.assertFalse(n.sched)
        res = n.command(self.cmd(False, False), G.BASE_MS)                   # already OFF
        self.assertEqual([type(e).__name__ for _, e in res], ["CommandResult"])
        self.assertTrue(res[0][1].ok)
        self.assertEqual(res[0][1].reason, "unchanged")
        p1 = P4.FakeNode(False, False, 0, 360)
        self.assertIsNone(p1.initial_event().sched)
        res = p1.command(self.cmd(False, None), G.BASE_MS)
        self.assertEqual((res[0][1].ok, res[0][1].reason), (False, "no_p2"))
        self.assertEqual(p1.log[-1]["result"], "no_p2")

    def test_window_command_sets_times_and_switch(self):
        n = P4.FakeNode(True, False, 0, 360)
        out = n.command(self.cmd(True, None, "window", 1425, 390), G.BASE_MS)
        self.assertEqual((out[0][1].sched, out[0][1].start_min, out[0][1].end_min), (True, 1425, 390))
        self.assertTrue(out[-1][1].ok)

    def test_summary_checks_the_timeline_against_the_log(self):
        cfg = W.make_cfg(bed=1, nw="00:00-06:00", nl=7)
        tl = G.Timeline(cfg, 1, 12)
        n = P4.FakeNode(True, True, 0, 360)
        n.command(self.cmd(False, True), G.BASE_MS + 1000)
        n.command(self.cmd(True, False, token="t2"), G.BASE_MS + (7 * DAY + 6 * 3600) * 1000)
        s = n.summary(tl, cfg, 12)
        self.assertEqual((s["extra_on_h_node"], s["extra_on_h_timeline"]), (42.0, 42.0))
        self.assertTrue(s["consistent"])
        quiet = P4.FakeNode(True, True, 0, 360).summary(tl, cfg, 12)      # no command, but a timeline with nl=7
        self.assertEqual(quiet["extra_on_h_node"], 0.0)
        self.assertFalse(quiet["consistent"])


# ---------------------------------------------------------------- the driver with a stub learner
class StubLearner:
    """Night learning like the core's: a switch gives a NodeCommand, its CommandResult ends the arming."""

    def __init__(self, params, tm):
        self.tm = tm
        self.calls = []
        self.now = None
        self.nl_active = False
        self.token = None

    def handle(self, ev):
        self.calls.append((type(ev).__name__, ev.recv_ms, ev))
        self.now = max(self.now or ev.recv_ms, ev.recv_ms)
        out = []
        if isinstance(ev, NightLearningSwitch) and ev.on:
            self.token = "nl1"
            out.append(NodeCommand("nl1", "schedule", False, None, None, True, "night_learning_start"))
            out.append(NightLearningChanged(True, "2026-01-12", False))
        if isinstance(ev, CommandResult) and ev.token == self.token:
            out.append(NightLearningChanged(True, "2026-01-12", False))
        lt = self.tm.local(self.now)
        if lt.sec_of_day >= 210 * 60 and lt.date_ord not in getattr(self, "done", set()):
            self.done = getattr(self, "done", set()) | {lt.date_ord}
            out.append(NightlyDone(lt.date_ord - BASE_ORD, datetime.date.fromordinal(lt.date_ord).isoformat(), 0, 0))
        return out

    def tick(self, now):
        self.calls.append(("tick", now, None))
        self.now = max(self.now or now, now)
        return []

    def suggestions(self):
        return ()

    def candidates(self):
        return ()

    def labels(self):
        return ()

    def counters(self):
        return {}

    def stays(self):
        return ()

    def status(self):
        return StatusChanged("learning", {"label_recall": None, "label_precision": None, "night": {},
                                          "review": {"items": [], "bed_item": {"state": "open"}, "bed": {}},
                                          "night_learning": {"active": self.nl_active, "phase": "idle",
                                                             "last_event": None}, "schedule": {}})

    def export_state(self):
        return {"night": {"onsets": [{"n": "2026-01-05", "m": 675, "s": "lux"}], "wakes": []}, "zones": [],
                "suppressions": []}


class DriverP4StubTest(unittest.TestCase):
    def run_job(self, name, nights=2):
        job = J.parse_job(name, nights=nights)
        r = D.Run(job, 1, learner_factory=StubLearner)
        self.assertEqual(r.run(), "ok")
        return r

    def test_night_learning_start_node_answer_and_lux(self):
        r = self.run_job("bed_w0000_0600_p4nl")
        calls = r.learner.calls
        names = [c[0] for c in calls]
        self.assertEqual(names[:3], ["NodeStatus", "ScheduleState", "ContextState"])
        i = names.index("NightLearningSwitch")
        self.assertEqual(calls[i][1], G.BASE_MS + 1000)
        # the node's echo (150 ms later) and its result come after the command, the echo first
        j = [k for k, n in enumerate(names) if n == "ScheduleState"][1]
        k = names.index("CommandResult")
        self.assertTrue(i < j < k)
        self.assertFalse(calls[j][2].sched)
        self.assertTrue(calls[k][2].ok)
        self.assertEqual((calls[j][1], calls[k][1]), (G.BASE_MS + 1000 + P4.ECHO_MS, G.BASE_MS + 1000 + P4.ECHO_MS + 1))
        self.assertGreater(names.count("LuxSample"), 5000)
        res = r.result()
        p4 = res["p4"]
        self.assertEqual(p4["script"], "nl")
        self.assertEqual(len(p4["node"]["commands"]), 1)
        self.assertEqual(p4["node"]["commands"][0]["result"], "ok")
        self.assertEqual(p4["actors"]["nl_user"]["events"][0]["active"], True)
        self.assertEqual(res["labels"].get("desk"), None)          # no P3 labels in a P4 job
        n0 = res["nights"][0]["p4"]
        self.assertEqual(n0["onsets"], [[0, 675, "lux"]])
        self.assertEqual(p4["true_onsets"][0][0], 0)
        self.assertTrue(p4["true_onsets"][0][3])                    # observed: before the 00:00 window start

    def test_a_p3_job_is_unchanged(self):
        r = self.run_job("bed_w0000_0600")
        names = [c[0] for c in r.learner.calls]
        self.assertNotIn("LuxSample", names)
        self.assertNotIn("NightLearningSwitch", names)
        res = r.result()
        self.assertNotIn("p4", res)
        self.assertNotIn("p4", res["nights"][0])
        self.assertIn("desk", res["labels"])

    def test_node_without_p2(self):
        job = J.parse_job("bed_none_p4nop2", nights=1)
        r = D.Run(job, 1, learner_factory=StubLearner)
        r.run()
        first = r.learner.calls[1]
        self.assertEqual(first[0], "ScheduleState")
        self.assertIsNone(first[2].sched)
        self.assertIsNone(first[2].start_min)


@unittest.skipUnless(core_ready(), "core Learner not implemented yet")
class DriverP4CoreTest(unittest.TestCase):
    def test_night_learning_on_a_node_without_p2_sends_nothing(self):
        res = D.run_job(J.parse_job("bed_none_p4nop2", nights=2), 1)
        self.assertEqual(res["status"], "ok", res.get("first_error"))
        self.assertEqual(res["core_errors"], 0, res.get("first_error"))
        p4 = res["p4"]
        self.assertEqual(p4["node"]["commands"], [])
        ev = p4["actors"]["nl_user"]["events"]
        self.assertEqual([(e["active"], e["last_event"]) for e in ev], [(False, "no_p2")])
        self.assertIn("lux", [o[2] for o in res["nights"][-1]["p4"]["onsets"]])

    def test_night_learning_started_commands_the_node_off(self):
        res = D.run_job(J.parse_job("bed_w0000_0600_p4nl", nights=2), 1)
        self.assertEqual(res["core_errors"], 0, res.get("first_error"))
        cmds = res["p4"]["node"]["commands"]
        self.assertEqual([(c["sched"], c["expect"], c["result"]) for c in cmds], [(False, True, "ok")])
        self.assertLessEqual(cmds[0]["t"], 5000)
        self.assertEqual(res["nights"][-1]["p4"]["night_learning"]["phase"], "running")
        node = res["p4"]["node"]
        self.assertTrue(node["consistent"])
        self.assertEqual((node["extra_on_h_node"], node["extra_on_h_timeline"]), (24.0, 24.0))   # days 1-4, 6 h each

    def test_nothing_is_commanded_by_default(self):
        res = D.run_job(J.parse_job("bed_w0000_0600_p4", nights=2), 1)
        self.assertEqual(res["p4"]["node"]["commands"], [])
        self.assertEqual(res["p4"]["node"]["extra_on_h_node"], 0.0)
        self.assertFalse(res["nights"][-1]["p4"]["night_learning"]["active"])


# ---------------------------------------------------------------- the numbered checks on synthetic results
HOURS = {"00:00-06:00": ("w0000_0600", 0, 360), "22:00-06:00": ("w2200_0600", 1320, 360),
         "23:00-07:00": ("w2300_0700", 1380, 420), "00:00-07:00": ("w0000_0700", 0, 420)}


def _true(nights, onset_nm=675, wake_nm=1130, spread=20, obs_onset=True, obs_wake=True):
    on = [[n, onset_nm + (n % 5) * spread // 4, False, obs_onset] for n in range(nights)]
    wk = [[n, wake_nm + (n % 4) * spread // 3, obs_wake] for n in range(-1, nights)]
    return on, wk


def _bed_state(withdrawn=False, reason=None, zones=0, frozen=False):
    return {"bed_item": {"state": "withdrawn" if withdrawn else "open", "reason": reason, "rev": 0},
            "bed": {"zones": zones, "frozen": frozen}, "items": [] if withdrawn else ["bed_needs_data"],
            "labels": 0, "bed_places": []}


def mk_p4_run(script, window="00:00-06:00", seed=1, nights=21, prop=None, bed_found=9, commands=(), extra=0.0,
              actors=None, onsets=None, named_night=None, consistent=True, sched_end=True,
              obs_onset=True, status="ok", zone_night=None, **_ignored):
    key, am, bm = HOURS[window]
    on, wk = _true(nights, obs_onset=obs_onset)
    recs = []
    for N in range(nights + 1):
        named = [{"id": "s1", "iou_bed": 0.8, "kind": "bed", "name_hint": "bed"}] if (
            named_night is not None and N >= named_night) else []
        sugg = []
        if bed_found is not None and N >= bed_found:
            sugg.append({"id": "s-bed", "rev": 1, "type": "dwell_zone", "kind": "dwell", "name_hint": None,
                         "state": "shadow", "first_night": bed_found, "last_night": N, "cx": -2.3, "cy": 4.9,
                         "cells_rle": [[0, 1]], "conf": 0.8, "variant_evidence_split": False,
                         "variant_share_split": False, "variant_suppressed": False, "frozen": False,
                         "variants": 0, "sim_label": "TP:bed", "iou": {"bed": 0.8}, "max_in": ["bed"]})
        zone = zone_night is not None and N >= zone_night
        withdrawn = bool(named) or zone
        rv = {"ready": N >= 7, "items": [] if withdrawn else ["bed_needs_data"],
              "bed_item": {"state": "withdrawn" if withdrawn else "open",
                           "reason": ("bed_zone" if zone else "bed_suggested") if withdrawn else None, "rev": 0},
              "bed": {"zones": 1 if zone else 0, "frozen": zone}}
        p = None
        if prop is not None and N >= 8:
            p = dict(prop)
        recs.append({"night": N, "candidates": 0, "suggestions": sugg, "truth": {}, "labels": [], "counters": {},
                     "p4": {"night": {"onsets": onsets or {"radar": N, "lux": N}, "wakes": max(0, N - 1),
                                      "proposal": p},
                            "review": rv, "night_learning": {"active": False}, "items": {},
                            "onsets": [], "wakes": [], "zones": [], "bed_named": named}})
    node = {"p2": True, "commands": list(commands), "off_runs_s": [], "sched_end": sched_end,
            "extra_on_h_node": extra, "extra_on_h_timeline": extra, "consistent": consistent}
    return {"job": "bed_%s_p4%s" % (key, script if script != "base" else ""), "world": "bed", "window": key,
            "variant": "p4" if script == "base" else "p4" + script, "gated": False, "seed": seed, "params": "x",
            "status": status, "core_errors": 0, "first_error": None,
            "job_spec": {"script": script, "window": window, "nl": 7 if script == "nl" else 0},
            "timing": {"cpu_s": 40.0, "wall_s": 41.0, "sim_days": nights + 0.15, "cpu_s_per_day": 2.0},
            "nights": recs, "truth": {"cut": {"bed": 0.7}, "core": ["bed"]}, "labels": {"recall": None, "precision": None},
            "capture": {"seated": 0.9}, "counters": {}, "stream": {},
            "p4": {"script": script, "node": node, "actors": actors or {}, "late_nights": [3],
                   "true_onsets": on, "true_wakes": wk}}


def _prop(start_nm, end_nm, basis="radar"):
    s = (start_nm + 720) % 1440
    e = (end_nm + 720) % 1440
    return {"start_min": s, "end_min": e, "basis": basis, "from_lights_off": basis != "radar", "onsets_n": 14,
            "wakes_n": 14, "start": "%02d:%02d" % divmod(s, 60), "end": "%02d:%02d" % divmod(e, 60)}


class P41Test(unittest.TestCase):
    def runs(self, start_nm, end_nm, nights=21):
        return [mk_p4_run("base", seed=s, nights=nights, prop=_prop(start_nm, end_nm)) for s in range(1, 6)]

    def test_within_30_minutes_passes(self):
        res = self.runs(705, 1110)                  # true P90 about 23:35, P10 about 06:50 -> 23:45 and 06:30
        c = E4.crit_p41(res)
        self.assertEqual(c["status"], E.PASS, c["details"]["seeds"][0]["final"])
        f = c["details"]["seeds"][0]["final"]
        self.assertLess(abs(f["start_diff_min"]), 30)

    def test_too_far_fails(self):
        c = E4.crit_p41(self.runs(705, 1050))       # 06:00: the P10 wake is 06:50 -> 50 min
        self.assertEqual(c["status"], E.FAIL)

    def test_a_seed_without_a_proposal_is_reported_two_fail_the_check(self):
        res = self.runs(705, 1110)
        res[2] = mk_p4_run("base", seed=3, nights=21, prop=None)
        c = E4.crit_p41(res)
        self.assertEqual(c["status"], E.PASS)                       # 4 of 5 have one, all within 30 min
        self.assertIsNone(c["details"]["seeds"][2]["final"]["proposal"])
        self.assertIn("no_proposal_reason", c["details"]["seeds"][2])
        res[3] = mk_p4_run("base", seed=4, nights=21, prop=None)
        self.assertEqual(E4.crit_p41(res)["status"], E.FAIL)        # 3 of 5: a proposal is needed in >= 4

    def test_the_reason_for_no_proposal_is_the_iqr_rule(self):
        r = mk_p4_run("base", seed=1, nights=21, prop=None)
        last = r["nights"][-1]["p4"]
        last["onsets"] = [[n, 675 + n, "radar"] for n in range(10)]
        last["wakes"] = [[n, 1130 + (110 if n % 2 else 0)] for n in range(10)]
        self.assertIn("wake IQR", E4._explain(r, 21))
        last["wakes"] = last["wakes"][:5]
        self.assertEqual(E4._explain(r, 21), "5 wakes (< 7)")

    def test_missing_seeds_are_incomplete(self):
        c = E4.crit_p41(self.runs(705, 1110)[:2])
        self.assertEqual(c["status"], E.INCOMPLETE)

    def test_night_14_is_summarised_next_to_the_gated_last_night(self):
        """The gate reads the last night; the report must show that night 14 (the first proposals) reads differently."""
        res = self.runs(705, 1110)
        for k, kind in ((0, "far"), (1, "none"), (2, "far")):
            rec = res[k]["nights"][14]["p4"]["night"]
            rec["proposal"] = _prop(705, 1050) if kind == "far" else None
        c = E4.crit_p41(res)
        self.assertEqual(c["status"], E.PASS)                                  # night 21: all within 30 min
        a = c["details"]["at_14_summary"]
        self.assertEqual((a["with_proposal"], a["within_tolerance"], a["runs"]), (4, 2, 5))
        self.assertEqual(a["same_rule_reads"], E.FAIL)                        # the same rule at night 14
        self.assertIn("night 14 would be FAIL", c["details"]["note"])
        self.assertIn("the design names no night", c["details"]["note"])
        md = "\n".join(E4.render_p4(E4.evaluate_p4(res)))
        self.assertIn("Night 14 (first proposals reach the setup review): 2 of 5", md)
        self.assertIn("**FAIL**", md)
        ok = E4.crit_p41(self.runs(705, 1110))
        self.assertEqual(ok["details"]["at_14_summary"]["same_rule_reads"], E.PASS)
        short = E4.crit_p41(self.runs(705, 1110, nights=14))
        self.assertIsNone(short["details"]["at_14_summary"])

    def test_true_percentiles_use_the_nights_before_the_last_one(self):
        r = mk_p4_run("base", nights=10, prop=_prop(705, 1110))
        tp = E4.true_percentiles(r, 10)
        self.assertEqual((tp["onsets"], tp["wakes"]), (10, 10))        # onsets 0..9, wakes -1..8
        obs = E4.radar_observable(r, 10)
        self.assertEqual((obs["onsets"], obs["of_onsets"]), (10, 10))


class P42Test(unittest.TestCase):
    def runs(self, window, lights, start_nm=705):
        return [mk_p4_run("base", window=window, seed=s, nights=14, obs_onset=False,
                          prop=_prop(start_nm, 1110, "lux" if lights else "radar")) for s in range(1, 6)]

    def test_lights_off_label_is_required_when_no_onset_is_observable(self):
        res = self.runs("22:00-06:00", True) + self.runs("23:00-07:00", True)
        c = E4.crit_p42(res)
        self.assertEqual(c["status"], E.PASS, [d["seeds"][0]["problems"] for d in c["details"]])
        res = self.runs("22:00-06:00", False) + self.runs("23:00-07:00", True)
        c = E4.crit_p42(res)
        self.assertEqual(c["status"], E.FAIL)
        self.assertIn("not labelled from lights-off times", c["details"][0]["seeds"][0]["problems"])

    def test_a_run_without_a_proposal_has_nothing_to_label(self):
        res = self.runs("22:00-06:00", True)
        res[1] = mk_p4_run("base", window="22:00-06:00", seed=2, nights=14, obs_onset=False, prop=None)
        res += self.runs("23:00-07:00", True)
        c = E4.crit_p42(res)
        self.assertEqual(c["status"], E.PASS)
        self.assertEqual(c["details"][0]["with_proposal"], 4)
        self.assertIsNotNone(c["details"][0]["seeds"][1]["no_proposal_reason"])
        res[2] = mk_p4_run("base", window="22:00-06:00", seed=3, nights=14, obs_onset=False, prop=None)
        self.assertEqual(E4.crit_p42(res)["details"][0]["status"], E.INCOMPLETE)   # 3 of 5: not enough to check
        none = self.runs("22:00-06:00", True) + [mk_p4_run("base", window="23:00-07:00", seed=s, nights=14,
                                                              obs_onset=False, prop=None) for s in range(1, 6)]
        self.assertEqual(E4.crit_p42(none)["details"][1]["status"], E.INCOMPLETE)

    def test_start_must_be_near_the_true_percentile(self):
        res = self.runs("22:00-06:00", True, start_nm=600) + self.runs("23:00-07:00", True)
        self.assertEqual(E4.crit_p42(res)["status"], E.FAIL)

    def test_both_windows_need_four_of_five_proposals(self):
        def with_n(window, n):
            return [mk_p4_run("base", window=window, seed=s, nights=14, obs_onset=False,
                              prop=_prop(705, 1110, "lux") if s <= n else None) for s in range(1, 6)]
        for second, status in ((5, E.PASS), (4, E.PASS), (3, E.INCOMPLETE), (1, E.INCOMPLETE)):
            c = E4.crit_p42(with_n("22:00-06:00", 5) + with_n("23:00-07:00", second))
            self.assertEqual(c["details"][1]["status"], status, second)       # one proposal no longer passes
            self.assertEqual(c["status"], status, second)
        self.assertIn("ideal lux model", E4.crit_p42(with_n("22:00-06:00", 5) + with_n("23:00-07:00", 5))["title"])


def _fail_step(key, reason, bed_state="open"):
    return {"step": key, "skipped": False, "note": None, "attempts": 1, "reasons": {reason: 1},
            "last": {"t": 1, "ok": False, "reason": reason, "kind": None, "labels_before": 0, "labels_after": 0,
                     "bed_item": {"state": bed_state}, "bed": {"zones": 0, "frozen": False}, "night": 8},
            "readback": None}


def _mark_actor(manual=True, ok_bed=True, bad_reason=None):
    steps = [_fail_step("not_observing", "not_observing"), _fail_step("no_stay", "no_stay"),
             _fail_step("several_stays", bad_reason or "several_stays")]
    if manual:
        steps.append({"step": "manual", "skipped": False, "note": None, "attempts": 5, "reasons": {"no_stay": 4, "ok": 1},
                      "last": {"t": 2, "ok": True, "reason": None, "kind": "manual", "labels_before": 0,
                               "labels_after": 1, "bed_item": {"state": "open"}, "night": 8}, "readback": None})
    else:
        steps.append({"step": "manual", "skipped": True, "note": "not exercised: the night_window item is not shown",
                      "attempts": 0, "reasons": {}, "last": None, "readback": None})
    rb = {"night": 8, "items": ["night_window"], "bed_item": {"state": "withdrawn", "reason": "bed_zone", "rev": 0},
          "bed": {"zones": 1, "frozen": True}, "labels": 2, "bed_places": [{"id": "s1", "frozen": True}]}
    bm = {"night": 8, "t": 3, "kind": "bed", "attempts": 9, "readback": rb} if ok_bed else None
    return {"mark": {"steps": steps, "unfinished": [], "bed_mark": bm}}


def _names_actor(kinds=("dwell", "short_task", "bed", "bed", "bed", "bed")):
    rb_before = {"bed_item": {"state": "open"}, "bed": {"zones": 0, "frozen": False}, "items": []}
    rb_after = {"bed_item": {"state": "withdrawn", "reason": "bed_zone"}, "bed": {"zones": 1, "frozen": True},
                "items": [], "bed_places": []}
    steps = [{"op": "approve", "name": "Desk", "want_bed": False, "night": 4, "kind": kinds[0]},
             {"op": "approve", "name": "Bedroom", "want_bed": False, "night": 4, "kind": kinds[1]},
             {"op": "rename", "name": "Bed", "want_bed": True, "night": 4, "kind": kinds[2],
              "before": rb_before, "after": rb_after},
             {"op": "approve", "name": "bett", "want_bed": True, "night": 5, "kind": kinds[3]},
             {"op": "approve", "name": "BED", "want_bed": True, "night": 6, "kind": kinds[4]},
             {"op": "approve", "name": " Bed ", "want_bed": True, "night": 8, "kind": kinds[5]}]
    return {"place_names": {"steps": steps, "done": True, "mode": "names"}}


def _app_actor():
    rb = {"bed_item": {"state": "withdrawn", "reason": "bed_zone"}, "bed": {"zones": 1, "frozen": True},
          "items": ["night_window"], "bed_places": [{"id": "s", "frozen": True}]}
    return {"place_app": {"night": 13, "suggestion": "s0013-1", "iou_bed": 0.8, "before": _bed_state(),
                          "after": rb, "zone": {"name": "Bed", "kind": "bed"}, "done": True, "mode": "app"}}


class P43P44Test(unittest.TestCase):
    def names_actor(self, **kw):
        return _names_actor(**kw)

    def results(self, **kw):
        return [mk_p4_run("mark", nights=12, actors=_mark_actor(**kw), zone_night=8),
                mk_p4_run("names", nights=10, actors=_names_actor()),
                mk_p4_run("app", nights=16, actors=_app_actor())]

    def test_all_pass(self):
        res = self.results()
        self.assertEqual(E4.crit_p43(res)["status"], E.PASS, E4.crit_p43(res)["details"])
        self.assertEqual(E4.crit_p44(res)["status"], E.PASS, E4.crit_p44(res)["details"])

    def test_sub_check_rule_is_k_of_n_and_an_unexercised_row_is_no_pass(self):
        P, I, F = E.PASS, E.INCOMPLETE, E.FAIL

        def rows(*st):
            return [{"status": x} for x in st]
        self.assertEqual(E4._sub(rows(P, P, P)), (P, 3, 3))
        self.assertEqual(E4._sub(rows(P, P, I))[0], I)                         # 3 seeds: all three
        self.assertEqual(E4._sub(rows(P, P))[0], P)
        self.assertEqual(E4._sub(rows(P, I))[0], I)
        self.assertEqual(E4._sub(rows(P))[0], P)
        self.assertEqual(E4._sub(rows(P, P, P, P, I))[0], P)                   # 4 of 5
        self.assertEqual(E4._sub(rows(P, P, P, I, I))[0], I)
        self.assertEqual(E4._sub(rows(P, P, P, P, F))[0], F)
        self.assertEqual(E4._sub([])[0], I)

    def test_an_unexercised_seed_blocks_the_pass_and_the_seeds_are_printed(self):
        res = self.results() + [mk_p4_run("app", seed=2, nights=16, actors={"place_app": {"done": False}})]
        c = E4.crit_p43(res)
        self.assertEqual(c["details"]["approve_as_bed"][1]["status"], E.INCOMPLETE)
        self.assertEqual(c["status"], E.INCOMPLETE)                            # it was PASS when one seed sufficed
        self.assertIn("1 of 2 seeds PASS (need 2)", c["counts"]["approve_as_bed"])
        md = "\n".join(E4.render_p4(E4.evaluate_p4(res)))
        self.assertIn("Seeds behind each sub-check:", md)
        ok = E4.crit_p43(self.results())
        self.assertEqual(ok["status"], E.PASS)
        self.assertEqual(ok["counts"]["mark_spot_from_bed_item"], "1 of 1 seeds PASS (need 1): PASS")
        c4 = E4.crit_p44(self.results())
        self.assertIn("1 of 1 seeds PASS", c4["counts"]["manual"])

    def test_a_failed_press_that_made_a_label_fails(self):
        a = _mark_actor()
        a["mark"]["steps"][1]["last"]["labels_after"] = 1
        res = [mk_p4_run("mark", nights=12, actors=a), mk_p4_run("names", nights=10, actors=_names_actor())]
        c = E4.crit_p44(res)
        self.assertEqual(c["status"], E.FAIL)
        self.assertEqual(c["details"]["no_stay"][0]["status"], E.FAIL)

    def test_the_wrong_reason_and_an_open_item_are_caught(self):
        res = [mk_p4_run("mark", nights=12, actors=_mark_actor(bad_reason="no_stay")),
               mk_p4_run("names", nights=10, actors=_names_actor())]
        self.assertEqual(E4.crit_p44(res)["details"]["several_stays"][0]["status"], E.FAIL)
        a = _mark_actor()
        a["mark"]["steps"][0]["last"]["bed_item"] = {"state": "withdrawn"}
        res = [mk_p4_run("mark", nights=12, actors=a), mk_p4_run("names", nights=10, actors=_names_actor())]
        self.assertEqual(E4.crit_p44(res)["details"]["not_observing"][0]["status"], E.FAIL)

    def test_a_skipped_manual_step_is_incomplete(self):
        res = [mk_p4_run("mark", nights=12, actors=_mark_actor(manual=False)),
               mk_p4_run("names", nights=10, actors=_names_actor())]
        c = E4.crit_p44(res)
        self.assertEqual(c["status"], E.INCOMPLETE)
        self.assertEqual(c["details"]["manual"][0]["status"], E.INCOMPLETE)

    def test_names_that_do_not_follow_the_rule_fail(self):
        res = [mk_p4_run("mark", nights=12, actors=_mark_actor()),
               mk_p4_run("names", nights=10, actors=_names_actor(kinds=("dwell", "bed", "bed", "bed", "bed", "dwell")))]
        c = E4.crit_p44(res)
        self.assertEqual(c["status"], E.FAIL)
        self.assertEqual(c["details"]["names"][0]["status"], E.FAIL)

    def test_the_mark_that_made_no_zone_fails_the_default_method(self):
        res = [mk_p4_run("mark", nights=12, actors=_mark_actor(ok_bed=False), zone_night=8)]
        res[0]["p4"]["actors"]["mark"]["unfinished"] = [{"step": "bed", "attempts": 40}]
        self.assertEqual(E4.crit_p43(res)["details"]["mark_spot_from_bed_item"][0]["status"], E.FAIL)

    def test_a_zone_without_the_freeze_fails(self):
        a = _app_actor()
        a["place_app"]["after"]["bed"]["frozen"] = False
        res = [mk_p4_run("app", nights=16, actors=a)]
        self.assertEqual(E4.crit_p43(res)["details"]["approve_as_bed"][0]["status"], E.FAIL)


class P45P46P47Test(unittest.TestCase):
    def nl_cmds(self, off_t=1000, on_t=None, window_end=360):
        on_t = (7 * 86400 + window_end * 60) * 1000 + 150 if on_t is None else on_t
        return [{"t": off_t, "token": "nl1", "kind": "schedule", "sched": False, "expect": True, "result": "ok"},
                {"t": on_t, "token": "nl2", "kind": "schedule", "sched": True, "expect": False, "result": "ok"}]

    def test_off_by_default(self):
        res = [mk_p4_run("base", seed=s, nights=21) for s in (1, 2)] + [
            mk_p4_run("nop2", window="00:00-06:00", nights=3, nl_events=())]
        res[-1]["p4"]["actors"] = {"nl_user": {"events": [{"last_event": "no_p2", "active": False}]}}
        c = E4.crit_p45(res)
        self.assertEqual(c["status"], E.PASS, c["details"])
        res[0]["p4"]["node"]["commands"] = [{"t": 5, "sched": False}]
        self.assertEqual(E4.crit_p45(res)["status"], E.FAIL)

    def test_extra_radar_on_time_without_the_switch_fails(self):
        res = [mk_p4_run("base", nights=21, extra=6.0), mk_p4_run("nop2", nights=3)]
        res[1]["p4"]["actors"] = {"nl_user": {"events": [{"last_event": "no_p2", "active": False}]}}
        self.assertEqual(E4.crit_p45(res)["status"], E.FAIL)

    def test_only_short_runs_are_incomplete(self):
        self.assertEqual(E4.crit_p45([mk_p4_run("base", nights=8)])["status"], E.INCOMPLETE)

    def nl_runs(self, window, found=(5, 6, 8, 6, 49), **kw):
        am, bm = HOURS[window][1], HOURS[window][2]
        hours = ((bm - am) % 1440) / 60.0
        out = []
        for s, f in zip(range(1, 6), found):
            r = mk_p4_run("nl", window=window, seed=s, nights=14, bed_found=f, extra=7 * hours,
                          commands=self.nl_cmds(window_end=bm, **kw))
            r["p4"]["actors"] = {"nl_user": {"events": [{"last_event": e} for e in
                                                        ("started", "running", "ending", "finished")]}}
            out.append(r)
        return out

    def test_night_learning_found_by_night_8_in_four_of_five(self):
        res = []
        for w in HOURS:
            res += self.nl_runs(w)
        c = E4.crit_p46(res)
        self.assertEqual(c["status"], E.PASS, [(d["window"], d["bed_by_night_8"], [x["problems"] for x in d["seeds"]])
                                              for d in c["details"]])
        self.assertEqual([d["bed_by_night_8"] for d in c["details"]], [4, 4, 4, 4])
        self.assertEqual([d["margin"] for d in c["details"]], [0, 0, 0, 0])
        self.assertIn("(need 4, at the minimum)", "\n".join(E4.render_p4(E4.evaluate_p4(res))))

    def test_three_of_five_fails(self):
        res = []
        for w in HOURS:
            res += self.nl_runs(w, found=(5, 6, 12, 9, 49) if w == "23:00-07:00" else (5, 6, 8, 6, 49))
        c = E4.crit_p46(res)
        self.assertEqual(c["status"], E.FAIL)
        self.assertEqual(c["details"][2]["status"], E.FAIL)

    def test_the_commands_must_match_the_design(self):
        late = self.nl_runs("00:00-06:00", on_t=(7 * 86400 + 360 * 60) * 1000 + 3 * 3600 * 1000)
        c = E4.crit_p46(late)
        self.assertEqual(c["status"], E.FAIL)
        self.assertTrue(any("restore command" in p for p in c["details"][0]["seeds"][0]["problems"]))
        r = self.nl_runs("00:00-06:00")
        r[0]["p4"]["node"]["extra_on_h_node"] = 6.0
        self.assertEqual(E4.crit_p46(r)["status"], E.FAIL)

    def test_bed_item_lifecycle(self):
        res = [mk_p4_run("base", seed=s, nights=21, named_night=(None if s % 2 else 12)) for s in range(1, 6)]
        res.append(mk_p4_run("app", nights=16, actors=_app_actor()))
        c = E4.crit_p47(res)
        self.assertEqual(c["status"], E.PASS, c["details"]["base"])
        self.assertEqual(c["details"]["base"]["seeds_naming_bed_by_itself"], 2)
        self.assertEqual(c["details"]["base"]["naming_nights"], [12, 12])
        self.assertIn("2 of 5 seeds name the bed place 'bed' by themselves", c["note"])
        self.assertIn("covered by unit tests only", c["note"])
        # an item that stays shown although a 'bed' place is suggested is a violation
        res[1]["nights"][15]["p4"]["review"]["bed_item"]["state"] = "open"
        c = E4.crit_p47(res)
        self.assertEqual(c["status"], E.FAIL)
        self.assertEqual(c["details"]["base"]["violations"][0]["night"], 15)

    def test_reject_is_checked_when_exercised(self):
        rej = {"place_rej": {"night": 12, "suggestion": "s", "name_hint": "bed",
                             "before": {"bed_item": {"state": "withdrawn"}}, "after": {"bed_item": {"state": "open"}},
                             "done": True}}
        res = [mk_p4_run("base", seed=s, nights=21) for s in range(1, 6)] + [mk_p4_run("rej", nights=16, actors=rej),
               mk_p4_run("app", nights=16, actors=_app_actor())]
        c = E4.crit_p47(res)
        self.assertEqual(c["details"]["reject"][0]["status"], E.PASS)
        self.assertEqual(c["status"], E.PASS)
        rej["place_rej"]["after"]["bed_item"]["state"] = "withdrawn"
        self.assertEqual(E4.crit_p47(res)["status"], E.FAIL)
        res[-2] = mk_p4_run("rej", nights=16, actors={"place_rej": {"done": False}})
        c = E4.crit_p47(res)
        self.assertEqual(c["details"]["reject"][0]["status"], E.REPORTED)
        self.assertEqual(c["status"], E.PASS)


class EvaluateIntegrationTest(unittest.TestCase):
    def test_p3_report_is_unchanged_without_p4_runs(self):
        rep = E.evaluate([])
        self.assertNotIn("p4", rep)
        self.assertNotIn("P4-1", E.render_markdown(rep))
        self.assertEqual(rep["scope"], E.SCOPE_P3)
        self.assertNotIn("not run", E.render_markdown(rep).split("\n")[2])

    def test_the_home_assistant_note_does_not_claim_the_trigger_is_tested(self):
        self.assertIn("is NOT verified here", E4.HA_NOTE)
        self.assertIn("needs a running Home Assistant", E4.HA_NOTE)
        self.assertIn("checks the blueprint text only", E4.HA_NOTE)
        self.assertIn("covered by unit tests", E4.HA_NOTE)
        rep = E.evaluate([mk_p4_run("base", seed=1, nights=21, prop=_prop(705, 1110))])
        self.assertIn("is NOT verified here", E.render_markdown(rep))
        self.assertIn("is NOT verified here", rep["p4"]["ha_note"])

    def test_p4_part_joins_the_report_and_the_exit_code(self):
        res = [mk_p4_run("base", seed=s, nights=21, prop=_prop(705, 1050)) for s in range(1, 6)]
        rep = E.evaluate(res)
        self.assertIn("p4", rep)
        self.assertEqual(rep["p4"]["criteria"][0]["id"], "P4-1")
        self.assertIn("P4-1", rep["failed"])
        self.assertEqual(rep["exit_code"], 1)
        self.assertEqual(rep["gate"], E.FAIL)                  # a P4-only invocation takes the P4 gate
        md = E.render_markdown(rep)
        self.assertIn("P4-1", md)
        self.assertIn("## P4 interpretations", md)
        self.assertIn("Not replay checks", md)
        self.assertEqual(rep["scope"], E.SCOPE_P4_ONLY)
        self.assertIn("G1-G6 were not run", md)                     # a P4-only PASS says nothing about G1-G6
        self.assertIn("Gate (P4-1 to P4-7 only; G1-G6 not run", md)
        self.assertIn("| bed_w0000_0600_p4 | 1,2,3,4,5 | 21 |", md)    # the jobs, seeds and nights really run
        self.assertEqual(rep["job_matrix"][0]["seeds"], [1, 2, 3, 4, 5])
        ok = [mk_p4_run("base", seed=s, nights=21, prop=_prop(705, 1110)) for s in range(1, 6)]
        rep = E.evaluate(ok)
        self.assertNotIn("P4-1", rep["failed"])

    def test_the_evidence_report_lists_the_errors(self):
        r = mk_p4_run("base", seed=1, nights=10, prop=_prop(705, 1110))
        last = r["nights"][-1]["p4"]
        last["onsets"] = [[n, 675 + (n % 5) * 5 + 3, "radar"] for n in range(9)]
        last["wakes"] = [[n, 1130 + (n % 4) * 6 - 10] for n in range(-1, 8)]
        rep = E4.rep_evidence([r])
        d = rep["details"][0]
        self.assertEqual(d["onset_error_min"]["radar"]["n"], 9)
        self.assertEqual(d["wake_error_min"]["n"], 9)
        self.assertEqual(d["radar_observable_onsets_without_radar_onset"], [9])
        self.assertEqual(d["wake_error_min"]["share_within_30"], 1.0)
        self.assertEqual(d["onset_error_min"]["radar"]["share_within_30"], 1.0)
        wi = d["wake_iqr_min"]
        self.assertEqual(wi["learner"], round(E4._pct([w[1] for w in last["wakes"]], 0.75)
                                              - E4._pct([w[1] for w in last["wakes"]], 0.25), 1))
        self.assertIsNotNone(wi["true_same_nights"])
        self.assertIsNotNone(wi["true_all_nights"])
        self.assertIn("ideal lux model", rep["title"])

    def test_the_evidence_report_shows_noisy_wakes_and_a_compressed_iqr(self):
        r = mk_p4_run("base", seed=1, nights=10, prop=_prop(705, 1110))
        last = r["nights"][-1]["p4"]
        true = {n: nm for n, nm, obs in r["p4"]["true_wakes"]}
        last["onsets"] = []
        # the learner's wakes: three of eight 100 min off (late), the rest exact; all of them inside one 12 min band
        last["wakes"] = [[n, true[n] + (100 if n in (0, 3, 5) else 0)] for n in range(0, 8)]
        d = E4.rep_evidence([r])["details"][0]
        self.assertEqual(d["wake_error_min"]["n"], 8)
        self.assertEqual(d["wake_error_min"]["share_within_30"], round(5 / 8, 3))
        self.assertEqual(d["wake_error_min"]["max_abs"], 100)
        last["wakes"] = [[n, 1135] for n in range(0, 8)]                       # no spread at all
        d = E4.rep_evidence([r])["details"][0]
        self.assertEqual(d["wake_iqr_min"]["learner"], 0.0)
        self.assertGreater(d["wake_iqr_min"]["true_same_nights"], 0)


if __name__ == "__main__":
    unittest.main()
