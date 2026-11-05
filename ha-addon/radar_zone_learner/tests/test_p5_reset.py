"""P5 'Forget all learned data': `Learner.reset()` carries exactly the node's window mirror, the four settings and the
push counters `seq` and `acked`; everything else equals a fresh learner (spec 7.2 step 2, D36, D40, §11.3
`test_p5_reset`)."""
import copy
import json
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core import p5state as PS
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.params import P5Params
from radar_zone_learner.core.types import (ContextState, FixedOffsetTime, NodeStatus, PushRequest, ScheduleState,
                                           SettingSwitch)

from .test_extraction import BASE_MS, DAY
from .test_p5_state import p5_learner
from .test_p5_items import POS, evidence
from .test_review import add_place, answer


def rich():
    L = p5_learner()
    t = BASE_MS + 8 * DAY * 1000
    L.handle(SettingSwitch("detail_text", True, t + 7000))
    L.handle(SettingSwitch("guests_pause", True, t + 7001))
    L.handle(ScheduleState(True, 1350, 400, t + 7002))                   # the node's window
    L.handle(ContextState("guests", True, t + 7003))
    L.p5.push.update({"seq": 5, "acked": 4})
    L.p5.tag_n = max(L.p5.tag_n, 3)
    return L


def flat(d, prefix=""):
    """The leaves of a nested dict as {path: value}, for a key-by-key comparison."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flat(v, prefix + k + "."))
        else:
            out[prefix + k] = v
    return out


class ResetTest(unittest.TestCase):
    def test_exactly_the_carried_items_differ_from_a_fresh_learner(self):
        L = rich()
        before = copy.deepcopy(L.export_state())
        fresh = L.reset()
        got = fresh.export_state()
        want = Learner(P, FixedOffsetTime(0)).export_state()
        p5 = got.pop("p5")
        self.assertNotIn("p5", want)
        self.assertEqual(got["night"].pop("active_window"), {"start": 1350, "end": 400, "sched": True})
        want["night"].pop("active_window")
        self.assertEqual(list(got), list(want))
        for k in want:                                                    # key by key: everything else is fresh
            self.assertEqual(got[k], want[k], k)
        expect = PS.P5State().export()                                     # the p5 section: defaults ...
        expect["settings"] = {"approve_sched": False, "guests_pause": True, "pets": True, "detail_text": True}
        expect["push"]["seq"], expect["push"]["acked"] = 5, 4              # ... except the settings and the counters
        # P6 (spec D24, D41): Forget all carries the installation epoch plus one, nothing else of the zone data
        expect["zone6"] = {"v": 1, "set": {"epoch": 1}, "tune": {}, "delay": {}, "ld": {}, "ldr": [], "info": {},
                           "ve": {}}
        self.assertEqual(p5, expect)
        self.assertEqual(L.export_state(), before)                        # the old learner is untouched

    def test_the_carried_values(self):
        L = rich()
        fresh = L.reset()
        st = fresh.export_state()
        self.assertEqual(st["night"]["active_window"], {"start": 1350, "end": 400, "sched": True})
        self.assertEqual(st["p5"]["settings"], {"approve_sched": False, "guests_pause": True, "pets": True,
                                                "detail_text": True})
        self.assertEqual((st["p5"]["push"]["seq"], st["p5"]["push"]["acked"]), (5, 4))
        self.assertIsNot(fresh.p5.settings, L.p5.settings)
        self.assertIsNot(fresh.window, L.window)
        fresh.p5.settings["pets"] = False
        self.assertTrue(L.p5.setting("pets"))

    def test_everything_else_is_fresh(self):
        L = rich()
        fresh = L.reset()
        self.assertEqual((fresh.review.zones, fresh.review.suppressions, fresh.review.next_zone_n,
                          fresh.review.bed_rev), ([], [], 1, 0))
        self.assertEqual((fresh.p5.later, fresh.p5.tags, fresh.p5.tag_n, fresh.p5.real, fresh.p5.masks,
                          fresh.p5.nw_applied), ({}, {}, 0, [], {}, None))
        p = fresh.p5.push
        self.assertEqual((p["kind"], p["setup_sent"], p["last_day"], p["sig"], p["acks"], p["head_id"], p["head_rev"]),
                         ("none", False, None, [], [], None, None))
        self.assertEqual((fresh.ns.suggestions, fresh.ns.candidates, fresh.label_list, fresh.bh.stays, fresh.spot_name),
                         ([], [], [], [], ""))
        self.assertEqual((fresh.night, fresh.created_ms, fresh.created_ord, fresh.last_run_ord), (0, None, None, None))
        self.assertEqual(fresh.night_m.onsets, {"radar": {}, "context": {}, "lux": {}})
        self.assertEqual((fresh.night_m.wakes, fresh.night_m.act), ({}, {}))                 # the sleep log is forgotten
        self.assertEqual(fresh.nl.active, False)
        self.assertIsNone(fresh.guests_ctx)
        self.assertEqual(fresh.status().state, "starting")
        self.assertEqual(fresh.planner.request([]), PushRequest(5, 4, "none", 0, None, None, ()))

    def test_the_export_of_a_reset_learner_loads(self):
        L = rich()
        fresh = L.reset()
        st = json.loads(json.dumps(fresh.export_state()))
        L2 = Learner(P, FixedOffsetTime(0), st)
        self.assertEqual(L2.export_state(), fresh.export_state())

    def test_the_params_are_kept(self):
        p5 = P5Params(zone_change_items=True)
        L = Learner(P, FixedOffsetTime(0), None, p5)
        fresh = L.reset()
        self.assertIs(fresh.p5params, p5)
        self.assertIs(fresh.p, L.p)
        self.assertIs(fresh.tm, L.tm)

    def test_a_carried_guests_pause_is_applied_at_the_first_event(self):
        L = rich()
        fresh = L.reset()
        self.assertFalse(fresh.obs.paused)
        fresh.handle(NodeStatus(True, BASE_MS + 9 * DAY * 1000))
        self.assertTrue(fresh.obs.paused)
        self.assertTrue(fresh.status().attributes["learning_paused"])

    def test_the_reset_learner_works(self):
        L = rich()
        fresh = L.reset()
        fresh.handle(NodeStatus(True, BASE_MS + 9 * DAY * 1000))
        self.assertEqual(fresh.review.items(fresh), [])                   # day 0 again: no setup review
        self.assertEqual(fresh.zones(), ())
        self.assertEqual(fresh.advanced(), ())
        self.assertEqual(fresh.review_state(), ((), None))
        self.assertEqual(fresh.push_request(), PushRequest(5, 4, "none", 0, None, None, ()))

    def test_a_second_reset_keeps_the_counters(self):
        L = rich()
        again = L.reset().reset()
        self.assertEqual((again.p5.push["seq"], again.p5.push["acked"]), (5, 4))
        self.assertTrue(again.p5.setting("detail_text"))


if __name__ == "__main__":
    unittest.main()
