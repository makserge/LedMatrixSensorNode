"""Replay driver, capture replay and CLI (P3 spec 11.5, 11.9, 11.10, 13.4)."""
import contextlib
import datetime
import gzip
import io
import json
import unittest
from pathlib import Path

from radar_zone_learner.core.learner import Learner
from radar_zone_learner.core.params import DEFAULT_PARAMS
from radar_zone_learner.core.types import (ContextState, FixedOffsetTime, FramesMessage, MarkPress, MarkResult,
                                           NightlyDone, NodeStatus, ScheduleState, SpotName, StatusChanged,
                                           Suggestion)
from replay import driver as D
from replay import framegen as G
from replay import jobs as J
from replay import run_replay
from replay import world as W
from tests._tmp import tmpdir

BASE_ORD = datetime.date(2026, 1, 5).toordinal()


def core_ready() -> bool:
    try:
        Learner(DEFAULT_PARAMS, FixedOffsetTime(0))
        return True
    except NotImplementedError:
        return False


def _strip(res: dict) -> dict:
    res = dict(res)
    res.pop("timing")
    return res


class StubLearner:
    """Records its calls; nightly at 03:30 of every new date; every third press succeeds."""

    def __init__(self, params, tm):
        self.tm = tm
        self.calls = []
        self.now = None
        self.done = set()
        self.presses = 0
        self.name = ""
        self.nlabels = 0

    def _nightly(self):
        lt = self.tm.local(self.now)
        if lt.sec_of_day >= 210 * 60 and lt.date_ord not in self.done:
            self.done.add(lt.date_ord)
            n = lt.date_ord - BASE_ORD
            return [NightlyDone(n, datetime.date.fromordinal(lt.date_ord).isoformat(), 0, 0)]
        return []

    def handle(self, ev):
        self.calls.append((type(ev).__name__, ev.recv_ms))
        self.now = max(self.now or ev.recv_ms, ev.recv_ms)
        out = []
        if isinstance(ev, SpotName):
            self.name = ev.text
        if isinstance(ev, MarkPress):
            self.presses += 1
            if self.presses % 3 == 0:
                self.nlabels += 1
                out.append(MarkResult(True, None, "l%04d" % self.nlabels,
                                      "bed" if self.name.casefold() == "bed" else "manual"))
            else:
                out.append(MarkResult(False, "no_stay", None, None))
        return out + self._nightly()

    def tick(self, now):
        self.calls.append(("tick", now))
        self.now = max(self.now or now, now)
        return self._nightly()

    def suggestions(self):
        cells = frozenset(c for c in range(W.NC)
                          if abs(W.cell_xy(c)[0] - 2.55) <= 0.3 and abs(W.cell_xy(c)[1] - 2.2) <= 0.3)
        return (Suggestion("s0001-1", 1, "dwell_zone", "dwell", None, "shadow", 1, 1, 2.55, 2.2, cells, (), 0.9, {},
                           (), False, False, False, False),)

    def candidates(self):
        return ()

    def labels(self):
        return ()

    def counters(self):
        return {"batches": sum(1 for c in self.calls if c[0] == "FramesMessage")}

    def status(self):
        return StatusChanged("learning", {"label_recall": None, "label_precision": None})

    def stays(self):
        return ()


class DriverStubTest(unittest.TestCase):
    def test_event_order_ticks_labels_and_records(self):
        job = J.parse_job("bed_w0000_0600", nights=2)
        r = D.Run(job, 1, learner_factory=StubLearner)
        self.assertEqual(r.run(), "ok")
        calls = r.learner.calls
        self.assertEqual([c[0] for c in calls[:3]], ["NodeStatus", "ScheduleState", "ContextState"])
        self.assertTrue(all(c[1] == G.BASE_MS for c in calls[:3]))
        times = [c[1] for c in calls]
        self.assertEqual(times, sorted(times))
        ticks = [c[1] for c in calls if c[0] == "tick"]
        self.assertEqual(ticks[0], G.BASE_MS)
        self.assertTrue(all(b - a == D.TICK_MS for a, b in zip(ticks, ticks[1:])))
        # a tick due at a message's time runs before it
        for k in range(1, len(calls)):
            if calls[k][0] == "tick" and calls[k - 1][0] != "tick":
                self.assertLess(calls[k - 1][1], calls[k][1] + 1)
        res = r.result()
        self.assertEqual([n["night"] for n in res["nights"]], [0, 1, 2])
        desk = res["labels"]["desk"]
        self.assertTrue(desk["ok"])
        self.assertEqual(desk["attempts"], 3)
        self.assertEqual(desk["fails"], {"no_stay": 2})
        names = [c for c in calls if c[0] == "SpotName"]
        self.assertTrue(names)
        presses = [c[1] for c in calls if c[0] == "MarkPress"]
        self.assertEqual(presses[1] - presses[0], D.PRESS_EVERY_MS)
        bed = res["labels"]["bed"]
        self.assertTrue(bed["ok"])                  # evening bed session of day 1, before the 00:00 window
        s = res["nights"][-1]["suggestions"][0]
        self.assertEqual(s["sim_label"], "TP:desk")
        self.assertGreater(s["iou"].get("desk", 0.0), 0.3)
        self.assertIn("desk", s["max_in"])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["truth"]["core"], ["desk", "table", "counter", "sofaA", "sofaB", "bed"])

    def test_no_presses_while_the_radar_is_off(self):
        job = J.parse_job("bed_w2200_0600", nights=2)
        r = D.Run(job, 1, learner_factory=StubLearner)
        r.run()
        bed = r.result()["labels"]["bed"]
        self.assertEqual(bed["attempts"], 0)        # the evening bed sessions start inside 22:00-06:00
        self.assertGreater(bed["sessions"], 0)

    def test_tiny_budget_stops_the_run(self):
        job = J.parse_job("liv_w0000_0600", nights=5)
        r = D.Run(job, 1, cpu_budget_s_per_day=1e-9, learner_factory=StubLearner, budget_slack_s=0.0)
        self.assertEqual(r.run(), "over_budget")
        res = r.result()
        self.assertEqual(res["status"], "over_budget")
        self.assertLess(res["timing"]["sim_days"], 1.1)


class DriverCoreTest(unittest.TestCase):
    def setUp(self):
        if not core_ready():
            self.skipTest("core Learner not implemented yet")

    def test_two_nights_complete_and_deterministic(self):
        job = J.parse_job("bed_w0000_0600", nights=2)
        a = D.run_job(job, 1)
        self.assertEqual(a["status"], "ok", a.get("first_error"))
        self.assertEqual(a["core_errors"], 0, a.get("first_error"))
        self.assertEqual([n["night"] for n in a["nights"]], [0, 1, 2])
        self.assertGreater(a["counters"]["batches"], 50_000)
        self.assertEqual(a["counters"]["parse_errors"], 0)
        b = D.run_job(job, 1)
        self.assertEqual(json.dumps(_strip(a), sort_keys=True), json.dumps(_strip(b), sort_keys=True))

    def test_capture_replay_round_trip(self):
        job = J.parse_job("bed_w0000_0600", nights=1)
        rs = G.RunStream(job.cfg(), 3, horizon_days=2)
        with tmpdir() as d:
            cap = Path(d) / "cap.txt.gz"
            end = G.BASE_MS + 4 * 3600 * 1000
            with gzip.open(cap, "wt", encoding="utf-8", newline="\n") as f:
                f.write("%.3f led_matrix_node/sensors/status online\n" % (G.BASE_MS / 1000))
                f.write('{"recv_ms": %d, "topic": "led_matrix_node/radar/schedule/state", "payload": "1"}\n' % G.BASE_MS)
                f.write("not a line\n")
                for recv, _, ev in rs.events():
                    if recv >= end:
                        break
                    if isinstance(ev, FramesMessage):
                        f.write("%.6f led_matrix_node/radar/ld2454/frames %s\n" % (recv / 1000, ev.payload.decode()))
            from replay import capture_replay
            out = Path(d) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                rc = capture_replay.main(["--in", str(cap), "--out", str(out), "--utc-offset-min", "60", "--png"])
            self.assertEqual(rc, 0)
            state = json.loads((out / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["schema"], 3)
            self.assertTrue((out / "debug_map.png").read_bytes().startswith(b"\x89PNG"))


class CliTest(unittest.TestCase):
    def test_usage_errors_and_budget_refusal(self):
        with tmpdir() as d, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_replay.main(["--out", d, "--nights", "40"]), 2)
            self.assertEqual(run_replay.main(["--out", d, "--seeds", "0-9"]), 2)
            self.assertEqual(run_replay.main(["--out", d, "--jobs", "bed_w0100_0600"]), 2)
            self.assertEqual(run_replay.main(["--profile", "smoke"]), 2)            # --out is required
            p = Path(d) / "params.json"
            p.write_text('{"no_such_field": 1}', encoding="utf-8")
            self.assertEqual(run_replay.main(["--out", d, "--params-json", str(p)]), 2)
            # p3 with 28 nights: 67 runs, ~1,880 simulated days > 1,500
            self.assertEqual(run_replay.main(["--out", d, "--profile", "p3", "--nights", "28"]), 2)
            self.assertIn("refused", (Path(d) / "log.txt").read_text(encoding="utf-8"))
            self.assertFalse((Path(d) / "runs").exists())

    def test_params_json_and_profiles(self):
        with tmpdir() as d:
            p = Path(d) / "params.json"
            p.write_text('{"envelope_hard_filter": true, "default_window_min": [0, 420]}', encoding="utf-8")
            prm = run_replay.load_params(str(p))
            self.assertTrue(prm.envelope_hard_filter)
            self.assertEqual(prm.default_window_min, (0, 420))
        self.assertEqual(len(J.runs_of(J.PROFILES["p3"])), 67)
        self.assertAlmostEqual(J.estimate_days(J.PROFILES["p3"]), 1332.7, delta=1.0)
        self.assertEqual(len(J.runs_of(J.PROFILES["quick"])), 6)
        self.assertEqual(J.parse_job("bed_w0000_0600_v3h300").cfg()["Vm"], 3)
        self.assertFalse(J.parse_job("bed_w0000_0600_faults").gated)
        self.assertTrue(J.parse_job("bed_w0000_0600_ghost").gated)
        self.assertFalse(J.parse_job("liv_none").gated)
        self.assertEqual(run_replay.parse_seeds("1-3,5"), (1, 2, 3, 5))


if __name__ == "__main__":
    unittest.main()
