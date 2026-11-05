import base64
import copy
import json
import struct
import unittest
from datetime import date

from radar_zone_learner.core import Learner
from radar_zone_learner.core import labels as LB
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core import state as ST
from radar_zone_learner.core.types import (FixedOffsetTime, NightLearningSwitch, ScheduleState, SpotName,
                                           StateError)

from .test_extraction import BASE_MS, BASE_S, DAY, DESK, desk_day, make, night

D0 = date(2026, 1, 5).toordinal()

TOP = ["schema", "writer", "phase", "params", "meta", "clock", "grids", "days_bits", "covis", "coverage", "stays",
       "door_events", "ghost_events", "candidates", "doors_prev", "outline", "suggestions", "labels", "zones",
       "suppressions", "review", "night", "settings"]


class Fr:
    def __init__(self, t0, t1, cx, cy, tid):
        self.t0, self.t1, self.cx, self.cy, self.track_id = t0, t1, cx, cy, tid


def rich_learner():
    L = make()
    L.handle(ScheduleState(True, 0, 360, BASE_MS + 1))
    L.handle(SpotName("Bed", BASE_MS + 2))
    for d in range(7):
        desk_day(L, d)
        L.bh.door_events.append({"t": BASE_S + d * DAY + 3600, "x": -150, "y": 540, "k": "b"})
        L.bh.ghost_events.append({"t": BASE_S + d * DAY + 7200, "x": 200, "y": 80, "dur": 600, "why": "identical",
                                  "ld": 0.0, "arr": "static"})
        L.covis.add(Fr(BASE_S + d * DAY + 100, BASE_S + d * DAY + 400, 2.5, 2.2, 2 * d + 1))
        L.covis.add(Fr(BASE_S + d * DAY + 200, BASE_S + d * DAY + 500, 3.5, 2.2, 2 * d + 2))
    for k in (7, 8, 9):
        night(L, k)
    lb = LB.make_label(L, (0, 0, DESK[0], DESK[1], 10), BASE_MS + 9 * DAY * 1000)
    L.label_list.append(lb)
    return L


def dumps(d):
    return json.dumps(d, separators=(",", ":"), ensure_ascii=True)


class StateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = rich_learner()
        cls.state = cls.L.export_state()
        cls.text = dumps(cls.state)

    def load(self, d):
        return Learner(P, FixedOffsetTime(0), d)

    def test_shape(self):
        st = self.state
        self.assertEqual(list(st)[:len(TOP)], TOP)
        self.assertEqual((st["schema"], st["writer"], st["phase"]), (4, "radar_zone_learner 0.7.0", "p5"))
        self.assertEqual(len(st["params"]), 12)
        self.assertEqual(st["meta"]["created_date"], "2026-01-05")
        self.assertEqual(st["meta"]["last_extraction_date"], "2026-01-14")
        self.assertEqual(st["meta"]["night"], 9)
        self.assertEqual(st["meta"]["next_label_n"], 2)
        self.assertEqual(st["night"], {
            "active_window": {"start": 0, "end": 360, "sched": True}, "night_lux": 5.0, "onsets": [], "wakes": [],
            "act": {}, "proposal_rev": 0, "night_learning": {"active": False, "until": None, "set_by_learner": None}})
        self.assertEqual((st["zones"], st["suppressions"]), ([], []))
        self.assertEqual(st["review"], {"later": {"night_window": None, "bed_needs_data": None}, "bed_rev": 0,
                                        "next_zone_n": 1})
        self.assertEqual(st["settings"], {"spot_name": "Bed"})
        self.assertEqual(len(st["suggestions"]), 1)
        self.assertEqual(st["suggestions"][0]["cells"], [list(r) for r in st["suggestions"][0]["cells"]])
        self.assertEqual(len(base64.b64decode(st["grids"]["dwell_slow"])), 2800 * 4)
        self.assertEqual(len(base64.b64decode(st["covis"]["fragsec"])), 700 * 4)
        self.assertEqual(len(base64.b64decode(st["covis"]["pairs"])) % 8, 0)
        self.assertEqual(len(st["coverage"]["2026-01-05"]), 24)
        self.assertEqual(st["stays"][0]["t0"] % 300, 0)
        self.assertEqual(st["labels"][0]["id"], "l0001")
        for k in ("tracks", "pending", "fragments", "epochs", "ld_runs"):
            self.assertNotIn(k, st)
        self.assertNotIn("_touched", self.text)

    def test_roundtrip_byte_identical(self):
        L2 = self.load(json.loads(self.text))
        self.assertEqual(dumps(L2.export_state()), self.text)
        self.assertEqual(L2.suggestions(), self.L.suggestions())
        self.assertEqual(L2.labels(), self.L.labels())
        self.assertEqual(L2.candidates(), self.L.candidates())
        self.assertEqual(dumps(self.L.export_state()), self.text)       # export never mutates

    def test_export_shares_nothing(self):
        st = self.L.export_state()
        st["suggestions"][0]["evidence"]["n"] = -1
        st["stays"][0]["x"] = -1
        st["labels"][0]["name"] = "changed"
        self.assertEqual(dumps(self.L.export_state()), self.text)

    def test_ram_only_parts_absent_after_load(self):
        L2 = self.load(json.loads(self.text))
        self.assertEqual((L2.tracker.live, L2.tracker.dying, L2.bh.pending, L2.covis.recent), ([], [], [], []))
        self.assertIsNone(L2.obs.epoch)
        self.assertEqual(L2.obs.ld.ends, [])
        self.assertEqual(L2.tracker.next_id, self.L.tracker.next_id)
        self.assertEqual(L2.status().state, "starting")

    def test_unknown_key_survives(self):
        d = json.loads(self.text)
        d["future_section"] = [{"id": "z1", "future": True}]
        L2 = self.load(d)
        out = L2.export_state()
        self.assertEqual(out["future_section"], [{"id": "z1", "future": True}])
        self.assertEqual(list(out)[-1], "future_section")

    def test_validation_failures(self):
        base = json.loads(self.text)

        def bad(fn):
            d = copy.deepcopy(base)
            fn(d)
            with self.assertRaises(StateError):
                self.load(d)

        bad(lambda d: d.update(schema=5))
        bad(lambda d: d.update(schema=2))
        bad(lambda d: d.update(schema=True))
        for k in TOP:
            bad(lambda d, k=k: d.pop(k))
        bad(lambda d: d.update(stays={}))
        bad(lambda d: d.update(meta=[]))
        bad(lambda d: d["meta"].update(night=-1))
        bad(lambda d: d["meta"].update(created_date="2026-13-01"))
        bad(lambda d: d["grids"].update(dwell_slow=base64.b64encode(b"\0" * 100).decode()))
        bad(lambda d: d["grids"].update(ghost=base64.b64encode(struct.pack("<f", float("nan")) * 2800).decode()))
        bad(lambda d: d["grids"].update(nx=71))
        bad(lambda d: d["grids"].update(walk="not base64!"))
        bad(lambda d: d["days_bits"].update(data=base64.b64encode(b"\0" * 10).decode()))
        bad(lambda d: d["covis"].update(fragsec=base64.b64encode(b"\0" * 8).decode()))
        bad(lambda d: d["covis"].update(pairs=base64.b64encode(struct.pack("<If", 700 * 700, 1.0)).decode()))
        bad(lambda d: d["suggestions"][0].update(cells=[[2799, 2]]))
        bad(lambda d: d["suggestions"][0].update(cells=[[-1, 1]]))
        bad(lambda d: d["suggestions"][0].pop("conf"))
        bad(lambda d: d["labels"][0].update(cells=[[3000, 1]]))
        bad(lambda d: d["stays"][0].update(day="yesterday"))
        bad(lambda d: d["stays"][0].update(x=1.5))
        bad(lambda d: d["coverage"].update({"2026-01-05": [1.0] * 23}))
        bad(lambda d: d["coverage"].update({"x": [1.0] * 24}))
        bad(lambda d: d["night"]["active_window"].update(start=1440))
        bad(lambda d: d["settings"].update(spot_name=None))
        bad(lambda d: d["candidates"][0].update(streak="3"))
        self.load(copy.deepcopy(base))

    def test_range_failures(self):
        """A state that is valid JSON of the right types but holds impossible numbers would load and then
        raise in every nightly run (a time of 2**63 broke the time model): it is refused at load."""
        base = json.loads(self.text)

        def bad(fn):
            d = copy.deepcopy(base)
            fn(d)
            with self.assertRaises(StateError):
                self.load(d)

        bad(lambda d: d["door_events"][0].update(t=2 ** 63))
        bad(lambda d: d["door_events"][0].update(t=0))
        bad(lambda d: d["door_events"][0].update(x=10 ** 9))
        bad(lambda d: d["door_events"][0].update(k="x"))
        bad(lambda d: d["ghost_events"][0].update(t=-5))
        bad(lambda d: d["ghost_events"][0].update(dur=10 ** 12))
        bad(lambda d: d["ghost_events"][0].update(why="other"))
        bad(lambda d: d["stays"][0].update(t0=2 ** 63))
        bad(lambda d: d["stays"][0].update(t1=d["stays"][0]["t0"] - 300))
        bad(lambda d: d["stays"][0].update(dur=-1))
        bad(lambda d: d["stays"][0].update(dur=10 ** 9))
        bad(lambda d: d["stays"][0].update(y=-10 ** 6))
        bad(lambda d: d["stays"][0].update(ld=1.5))
        bad(lambda d: d["stays"][0].update(exit="gone"))
        bad(lambda d: d["stays"][0].update(arr="fly"))
        bad(lambda d: d["labels"][0].update(created_s=2 ** 40))
        bad(lambda d: d["labels"][0].update(x=1e6))
        bad(lambda d: d["candidates"][0].update(cx=1e9))
        bad(lambda d: d["suggestions"][0].update(cy=-1e9))
        bad(lambda d: d["meta"].update(created_ms=2 ** 62))
        bad(lambda d: d["clock"].update(eff_s=-1.0))
        bad(lambda d: d["clock"].update(obs_s_since_extraction=1e12))
        self.load(copy.deepcopy(base))

    def test_fresh_state_roundtrip(self):
        L = Learner(P, FixedOffsetTime(0))
        st = L.export_state()
        self.assertIsNone(st["meta"]["created_date"])
        L2 = self.load(json.loads(dumps(st)))
        self.assertEqual(dumps(L2.export_state()), dumps(st))

    def test_continues_after_load(self):
        L2 = self.load(json.loads(self.text))
        desk_day(L2, 9)
        night(L2, 10)
        s = [x for x in L2.suggestions() if x.type == "dwell_zone"]
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0].last_night, 10)
        self.assertEqual((s[0].kind, s[0].name_hint), ("bed", "Bed"))     # linked to the 'Bed' label


def to_v3(st):
    """The same state as a P3 writer produced it: no zones, suppressions or review, a 'night' with the window only."""
    d = copy.deepcopy(st)
    d["schema"] = 3
    d["phase"] = "p3"
    for k in ("zones", "suppressions", "review"):
        del d[k]
    d["night"] = {"active_window": d["night"]["active_window"]}
    return d


class SchemaV4Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = rich_learner()
        cls.state = cls.L.export_state()
        cls.text = dumps(cls.state)

    def load(self, d):
        return Learner(P, FixedOffsetTime(0), d)

    def test_a_v3_state_migrates_to_the_same_v4_state(self):
        v3 = to_v3(self.state)
        v3_before = copy.deepcopy(v3)
        L2 = self.load(json.loads(dumps(v3)))
        self.assertEqual(dumps(L2.export_state()), self.text)               # defaults for everything new
        self.assertEqual(v3, v3_before)                                      # the dict that was handed in is untouched
        self.assertEqual(L2.suggestions(), self.L.suggestions())
        self.assertEqual(L2.labels(), self.L.labels())
        self.assertEqual((L2.nl.active, L2.review.zones, L2.review.suppressions), (False, [], []))
        self.assertEqual(L2.window, {"start": 0, "end": 360, "sched": True})

    def test_migrate_function(self):
        v3 = to_v3(self.state)
        before = copy.deepcopy(v3)
        out = ST.migrate_3_to_4(v3)
        self.assertEqual(v3, before)                                         # pure
        self.assertEqual(list(out), list(self.state))                        # the key order of a v4 file
        self.assertEqual(out["schema"], 4)
        self.assertEqual(out["night"]["night_lux"], P.night_lux)
        self.assertEqual(out["night"]["night_learning"], {"active": False, "until": None, "set_by_learner": None})
        self.assertEqual(out["review"], {"later": {"night_window": None, "bed_needs_data": None}, "bed_rev": 0,
                                         "next_zone_n": 1})
        for bad in (self.state, {"schema": 2}, {"schema": True}, [], None):
            with self.assertRaises(StateError):
                ST.migrate_3_to_4(bad)

    def test_a_v3_file_keeps_unknown_keys_and_a_bad_one_is_refused(self):
        v3 = to_v3(self.state)
        v3["future"] = {"x": 1}
        out = self.load(json.loads(dumps(v3))).export_state()
        self.assertEqual(out["future"], {"x": 1})
        self.assertEqual(out["schema"], 4)
        v3 = to_v3(self.state)
        v3["night"]["active_window"]["start"] = 5000
        with self.assertRaises(StateError):
            self.load(v3)
        v3 = to_v3(self.state)
        del v3["labels"]
        with self.assertRaises(StateError):
            self.load(v3)

    def test_schemas_that_are_not_3_or_4_are_refused(self):
        for sch in (0, 1, 2, 5, 99, True, "4", None, 4.0):
            d = copy.deepcopy(self.state)
            d["schema"] = sch
            with self.assertRaises(StateError, msg=repr(sch)):
                self.load(d)
        d = copy.deepcopy(self.state)
        del d["schema"]
        with self.assertRaises(StateError):
            self.load(d)

    def busy(self):
        """A learner with everything P4 adds: zones, suppressions, evidence and a Night learning that is on."""
        from .test_review import add_place, answer, ready_learner, with_proposal
        L = ready_learner()
        with_proposal(L)
        s = add_place(L, (-2.3, 4.9), evidence={"hours_at_suggestion": [0.0] * 24, "hours_wd": [0.0] * 24,
                                                "hours_we": [0.0] * 24})
        L.handle(SpotName("Bett", BASE_MS + 8 * DAY * 1000))
        answer(L, s["id"], "approve")
        add_place(L, (1.5, 2.0), sid="s0006-1")
        answer(L, "s0006-1", "reject")
        answer(L, "night_window", "reject")
        L.night_m.onsets["lux"][D0] = 700
        L.night_m.onsets["context"][D0 + 1] = 705
        L.night_m.act[D0 + 2] = (1 << 200) | 5
        L.handle(NightLearningSwitch(True, BASE_MS + 8 * DAY * 1000 + 100))     # the schedule is ON: a command goes out
        return L

    def test_p4_sections_roundtrip_byte_identical(self):
        L = self.busy()
        st = L.export_state()
        self.assertEqual(st["zones"][0]["zid"], "z0001")
        self.assertEqual([x["type"] for x in st["suppressions"]], ["place", "night_window"])
        self.assertEqual(st["night"]["night_learning"]["active"], True)
        text = dumps(st)
        L2 = self.load(json.loads(text))
        self.assertEqual(dumps(L2.export_state()), text)
        self.assertEqual(L2.review.zones, L.review.zones)
        self.assertEqual(L2.review.suppressions, L.review.suppressions)
        self.assertEqual(L2.night_m.onsets, L.night_m.onsets)
        self.assertEqual(L2.night_m.act, L.night_m.act)

    def test_validation_of_the_new_sections(self):
        base = json.loads(dumps(self.busy().export_state()))
        self.load(copy.deepcopy(base))

        def bad(fn):
            d = copy.deepcopy(base)
            fn(d)
            with self.assertRaises(StateError):
                self.load(d)

        bad(lambda d: d["zones"][0].update(kind="manual"))
        bad(lambda d: d["zones"][0].update(src="mark"))
        bad(lambda d: d["zones"][0].update(cells=[[0, 99999]]))
        bad(lambda d: d["zones"][0].update(cx=1e9))
        bad(lambda d: d["zones"][0].update(created_s=1))
        bad(lambda d: d["zones"][0].update(zid=5))
        bad(lambda d: d["zones"].extend([copy.deepcopy(d["zones"][0]) for _ in range(120)]))
        bad(lambda d: d["suppressions"][0].update(type="bogus"))
        bad(lambda d: d["suppressions"][1].update(until="nope"))
        bad(lambda d: d["suppressions"][0].pop("ref"))
        bad(lambda d: d["suppressions"].append({"type": "bed_needs_data", "night": -1}))
        bad(lambda d: d["review"]["later"].update(extra=None))
        bad(lambda d: d["review"]["later"].update(bed_needs_data="x"))
        bad(lambda d: d["review"].update(bed_rev=-1))
        bad(lambda d: d["review"].update(next_zone_n=0))
        bad(lambda d: d["review"].pop("later"))
        bad(lambda d: d["night"].pop("onsets"))
        bad(lambda d: d["night"].update(wakes=[{"n": "2026-01-05", "m": 5, "s": "lux"}]))
        bad(lambda d: d["night"]["act"].update({"2026-01-05": "xyz"}))
        bad(lambda d: d["night"].pop("night_learning"))
        bad(lambda d: d["night"]["night_learning"].update(active=True, until=None))
        bad(lambda d: d["night"]["onsets"].extend([{"n": "2026-01-05", "m": 5, "s": "lux"}] * 500))

    def test_the_night_lux_of_the_options_wins_over_the_file(self):
        d = copy.deepcopy(self.state)
        d["night"]["night_lux"] = 99.0
        L2 = self.load(d)
        self.assertEqual(L2.night_m.night_lux, P.night_lux)
        self.assertEqual(L2.export_state()["night"]["night_lux"], P.night_lux)


if __name__ == "__main__":
    unittest.main()
