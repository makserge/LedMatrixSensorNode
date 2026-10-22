import base64
import copy
import json
import struct
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core import labels as LB
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import FixedOffsetTime, ScheduleState, SpotName, StateError

from .test_extraction import BASE_MS, BASE_S, DAY, DESK, desk_day, make, night

TOP = ["schema", "writer", "phase", "params", "meta", "clock", "grids", "days_bits", "covis", "coverage", "stays",
       "door_events", "ghost_events", "candidates", "doors_prev", "outline", "suggestions", "labels", "night",
       "settings"]


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
        self.assertEqual((st["schema"], st["writer"], st["phase"]), (3, "radar_zone_learner 0.3.0", "p3"))
        self.assertEqual(len(st["params"]), 12)
        self.assertEqual(st["meta"]["created_date"], "2026-01-05")
        self.assertEqual(st["meta"]["last_extraction_date"], "2026-01-14")
        self.assertEqual(st["meta"]["night"], 9)
        self.assertEqual(st["meta"]["next_label_n"], 2)
        self.assertEqual(st["night"], {"active_window": {"start": 0, "end": 360, "sched": True}})
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
        d["zones"] = [{"id": "z1", "future": True}]
        L2 = self.load(d)
        out = L2.export_state()
        self.assertEqual(out["zones"], [{"id": "z1", "future": True}])
        self.assertEqual(list(out)[-1], "zones")

    def test_validation_failures(self):
        base = json.loads(self.text)

        def bad(fn):
            d = copy.deepcopy(base)
            fn(d)
            with self.assertRaises(StateError):
                self.load(d)

        bad(lambda d: d.update(schema=4))
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


if __name__ == "__main__":
    unittest.main()
