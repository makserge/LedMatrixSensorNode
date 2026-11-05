"""The zone6 container, its validation and orphans (P6 spec 6, P6-R2 at unit level). Owner: CORE."""
import copy
import unittest

from radar_zone_learner.core.p5state import P5State
from radar_zone_learner.core.p6state import Zone6, variant_id
from radar_zone_learner.core.types import StateError

FULL = {
    "v": 1, "set": {"epoch": 2},
    "tune": {"z0001": {"cd": 120, "mg": 25}},
    "delay": {"z0001": {"sp": [95, 140, 210], "cd": 180, "upd": 12}},
    "ld": {"z0001": [5400, 6000]},
    "ldr": ["z0001"],
    "info": {"z0001": {"th": {"wd": ["09-12", "13-17"], "we": ["20-23"]},
                       "var": [{"d": "wd", "s": 9, "e": 17, "n": 12}], "conf": 0.8, "upd": 12}},
    "ve": {"z0001": ["wd0917"]},
}


class Zone6Test(unittest.TestCase):
    def test_default_is_default_and_exports_nothing(self):
        z = Zone6()
        self.assertTrue(z.is_default())
        p5 = P5State()
        z.sync_into(p5)
        self.assertNotIn("zone6", p5.extra)
        self.assertTrue(p5.is_default())

    def test_full_round_trip(self):
        z = Zone6.load(copy.deepcopy(FULL))
        self.assertFalse(z.is_default())
        self.assertEqual(z.export(), FULL)
        self.assertTrue(z.is_ldr("z0001"))
        self.assertFalse(z.is_ldr("z0002"))

    def test_epoch_alone_is_not_default(self):
        z = Zone6()
        z.epoch = 1
        self.assertFalse(z.is_default())
        p5 = P5State()
        z.sync_into(p5)
        self.assertEqual(p5.extra["zone6"]["set"], {"epoch": 1})
        self.assertFalse(p5.is_default())

    def test_sync_into_deletes_the_key_when_default(self):
        p5 = P5State()
        p5.extra["zone6"] = {"v": 1}
        Zone6().sync_into(p5)
        self.assertNotIn("zone6", p5.extra)

    def test_missing_or_non_object_is_the_defaults(self):
        for v in (None, [], "x", 3):
            self.assertTrue(Zone6.load(v).is_default())

    def test_unknown_keys_are_kept(self):
        d = copy.deepcopy(FULL)
        d["drift"] = {"z0001": [1, 2]}
        z = Zone6.load(d)
        self.assertEqual(z.export()["drift"], {"z0001": [1, 2]})

    def test_p5state_keeps_zone6_in_extra_untouched(self):
        p5 = P5State()
        p5.load({"zone6": copy.deepcopy(FULL)}, 2800)
        self.assertEqual(p5.extra["zone6"], FULL)
        self.assertEqual(p5.export()["zone6"], FULL)

    def test_validation_errors(self):
        def bad(mut):
            d = copy.deepcopy(FULL)
            mut(d)
            with self.assertRaises(StateError, msg=str(mut)):
                Zone6.validate(d)
        bad(lambda d: d.__setitem__("v", "1"))
        bad(lambda d: d.__setitem__("set", {"epoch": -1}))
        bad(lambda d: d.__setitem__("set", {"epoch": True}))
        bad(lambda d: d["tune"]["z0001"].__setitem__("cd", 2))
        bad(lambda d: d["tune"]["z0001"].__setitem__("cd", 901))
        bad(lambda d: d["tune"]["z0001"].__setitem__("mg", -51))
        bad(lambda d: d["tune"]["z0001"].__setitem__("mg", 101))
        bad(lambda d: d["tune"]["z0001"].__setitem__("mg", 2.5))
        bad(lambda d: d["tune"].__setitem__("zone1", {}))
        bad(lambda d: d["delay"]["z0001"].__setitem__("sp", list(range(1, 52))))
        bad(lambda d: d["delay"]["z0001"].__setitem__("sp", [0]))
        bad(lambda d: d["delay"]["z0001"].__setitem__("sp", [86401]))
        bad(lambda d: d["delay"]["z0001"].__setitem__("cd", 4))
        bad(lambda d: d["delay"]["z0001"].__setitem__("cd", 601))
        bad(lambda d: d["ld"].__setitem__("z0001", [7000, 6000]))
        bad(lambda d: d["ld"].__setitem__("z0001", [-1, 6000]))
        bad(lambda d: d["ld"].__setitem__("z0001", [1, float("inf")]))
        bad(lambda d: d.__setitem__("ldr", "z0001"))
        bad(lambda d: d["info"]["z0001"]["var"].append({"d": "xx", "s": 1, "e": 2}))
        bad(lambda d: d["info"]["z0001"]["var"].__setitem__(0, {"d": "wd", "s": 25, "e": 2, "n": 1}))
        bad(lambda d: d["info"]["z0001"].__setitem__("var", [{"d": "wd", "s": 1, "e": 2, "n": 1}] * 7))
        bad(lambda d: d["info"]["z0001"]["th"].__setitem__("wd", ["09-12", "13-17", "18-19", "20-21"]))
        bad(lambda d: d["info"]["z0001"]["th"].__setitem__("wd", ["9-12"]))
        bad(lambda d: d["info"]["z0001"].__setitem__("conf", 1.5))
        bad(lambda d: d["ve"].__setitem__("z0001", ["wd917"]))
        bad(lambda d: d["ve"].__setitem__("z0001", ["wd0917"] * 1 + ["we0001", "we0002", "we0003", "we0004",
                                                                       "we0005", "we0006"]))

    def test_prune_orphans(self):
        z = Zone6.load(copy.deepcopy(FULL))
        z.tune["l0003"] = {"cd": 30}
        self.assertEqual(z.prune(["z0001", "l0003"]), 0)
        self.assertEqual(z.prune(["l0003"]), 1)
        self.assertEqual((z.delay, z.ld, z.info, z.ve, z.ldr), ({}, {}, {}, {}, []))
        self.assertEqual(z.tune, {"l0003": {"cd": 30}})

    def test_forget_zone_drops_its_data_but_not_others(self):
        d = copy.deepcopy(FULL)
        d["tune"]["z0002"] = {"cd": 10}
        z = Zone6.load(d)
        z.forget_zone("z0001")
        self.assertEqual(z.export()["tune"], {"z0002": {"cd": 10}})
        self.assertEqual((z.delay, z.ld, z.info, z.ve, z.ldr), ({}, {}, {}, {}, []))

    def test_reset_for_forget_carries_the_epoch_plus_one_and_nothing_else(self):
        z = Zone6.load(copy.deepcopy(FULL))
        f = z.reset_for_forget()
        self.assertEqual((f.epoch, f.tune, f.delay, f.ve, f.extra), (3, {}, {}, {}, {}))

    def test_variant_id(self):
        self.assertEqual(variant_id({"d": "wd", "s": 9, "e": 17}), "wd0917")
        self.assertEqual(variant_id({"d": "we", "s": 0, "e": 24}), "we0024")


if __name__ == "__main__":
    unittest.main()
