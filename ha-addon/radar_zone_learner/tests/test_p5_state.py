"""P5 state: the optional `p5` section, the zone-record extras, validation and compatibility (spec 9, §11.3)."""
import copy
import json
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core import p5state as PS
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import (CommandResult, FixedOffsetTime, ReviewAnswer, SettingSwitch, SpotName,
                                           StateError)

from .test_extraction import BASE_MS, DAY, make, night
from .test_review import add_place, answer, ready_learner, with_proposal
from .test_state import TOP, dumps, rich_learner, to_v3

HOURS = [0.0] * 24
HOURS[9] = HOURS[10] = 40.0
EV = {"n": 18, "hours_wd": list(HOURS), "hours_we": [0.0] * 24, "hours_at_suggestion": list(HOURS)}


def p5_learner():
    """A learner with every part of the p5 section in use, made only through public inputs."""
    L = ready_learner()
    with_proposal(L)
    add_place(L, (2.55, 2.2), sid="s0005-1", conf=0.8, evidence=copy.deepcopy(EV))
    add_place(L, (-2.3, 4.9), sid="s0006-1", conf=0.7, evidence=copy.deepcopy(EV))
    add_place(L, (1.5, 2.0), sid="s0007-1", conf=0.6, evidence=copy.deepcopy(EV))
    L.review.touch()
    t = BASE_MS + 8 * DAY * 1000
    L.handle(SpotName("Desk", t))
    answer(L, "s0005-1", "approve")                                   # a zone record
    answer(L, "s0006-1", "reject")                                    # a rejection and its mask
    answer(L, "s0007-1", "later")                                     # a snooze
    c = [o for o in answer(L, "night_window", "approve", sched=True) if o.__class__.__name__ == "NodeCommand"][0]
    L.handle(CommandResult(c.token, "window", True, None, t + 5000))  # nw_applied
    refl = add_place(L, (4.0, 3.0), typ="dwell_zone", sid="s0004-2", conf=0.5)
    refl["type"] = "reflection"
    L.p5.real.append({"ref": "s0004-2", "cx": 4.0, "cy": 3.0, "cells": frozenset(refl["cells"]), "night": 5})
    L.handle(SettingSwitch("pets", True, t + 6000))
    L.p5.push.update({"seq": 2, "acked": 2, "kind": "daily", "setup_sent": True, "last_day": "2026-01-14",
                      "sig": ["s0005-1#1", "s0006-1#1"], "acks": ["pa1b2c3d"], "head_id": "s0006-1", "head_rev": 1})
    return L


def load(d):
    return Learner(P, FixedOffsetTime(0), d)


class ExportTest(unittest.TestCase):
    def test_a_fresh_or_p4_style_learner_exports_no_p5_section(self):
        st = Learner(P, FixedOffsetTime(0)).export_state()
        self.assertEqual(list(st), TOP)
        self.assertNotIn("p5", st)
        L = ready_learner()                                                # the setup review exists, no place is listed
        self.assertEqual([i.id for i in L.review.items(L)], ["bed_needs_data"])
        self.assertNotIn("p5", L.export_state())

    def test_a_learner_that_only_ticked_exports_no_section_while_nothing_was_listed(self):
        L = make()
        night(L, 3)
        self.assertNotIn("p5", L.export_state())

    def test_listing_a_place_allocates_its_tag_and_that_makes_the_section(self):
        L = rich_learner()      # the place suggestion of night 9 was listed by the nightly step; a mark covers it since
        p5 = L.export_state()["p5"]
        self.assertEqual(p5["tags"], {"by_id": {"s0009-1": "A"}, "n": 1})
        self.assertEqual((p5["later"], p5["real"], p5["masks"], p5["nw_applied"]), ({}, [], {}, None))
        self.assertEqual(p5["push"]["seq"], 0)

    def test_the_section_is_written_after_settings_in_the_documented_key_order(self):
        L = p5_learner()
        st = L.export_state()
        self.assertEqual(list(st)[:len(TOP)], TOP)
        self.assertEqual(list(st).index("p5"), len(TOP))                  # right after `settings`, before any extra
        p5 = st["p5"]
        self.assertEqual(list(p5), ["later", "tags", "real", "masks", "nw_applied", "push", "settings"])
        self.assertEqual(list(p5["push"]), ["seq", "acked", "kind", "setup_sent", "last_day", "sig", "acks", "head_id",
                                            "head_rev"])
        self.assertEqual(p5["settings"], {"approve_sched": False, "guests_pause": False, "pets": True,
                                          "detail_text": False})
        self.assertEqual(set(p5["later"]), {"s0007-1"})
        self.assertEqual(list(p5["masks"]), ["s0006-1"])
        self.assertEqual(set(p5["masks"]["s0006-1"]), {"cx", "cy", "cells", "n0"})
        self.assertEqual(p5["masks"]["s0006-1"]["n0"], 18)
        self.assertEqual(p5["nw_applied"], {"rev": L.night_m.proposal_rev, "start": 1380, "end": 375})
        self.assertEqual(p5["real"][0]["ref"], "s0004-2")
        self.assertEqual(p5["tags"]["n"], len(p5["tags"]["by_id"]))

    def test_roundtrip_is_byte_identical(self):
        L = p5_learner()
        text = dumps(L.export_state())
        L2 = load(json.loads(text))
        self.assertEqual(dumps(L2.export_state()), text)
        self.assertEqual(L2.p5.export(), L.p5.export())
        self.assertEqual(L2.review.zones, L.review.zones)                  # extras round trip equal to the RAM records
        self.assertEqual(L2.review.suppressions, L.review.suppressions)

    def test_zone_records_carry_the_p5_extras(self):
        L = p5_learner()
        z = L.export_state()["zones"][0]
        self.assertEqual(list(z), ["zid", "name", "kind", "src", "ref", "cx", "cy", "cells", "night", "created_s",
                                   "poly_cm", "rev", "upd", "srev", "also"])
        self.assertEqual((z["rev"], z["upd"], z["srev"], z["also"]), (0, 7, 1, []))
        self.assertTrue(3 <= len(z["poly_cm"]) <= 12)
        self.assertTrue(all(isinstance(v, list) and len(v) == 2 for v in z["poly_cm"]))

    def test_unknown_key_inside_p5_is_kept_and_written_back(self):
        d = json.loads(dumps(p5_learner().export_state()))
        d["p5"]["future"] = {"x": [1, 2]}
        L2 = load(d)
        out = L2.export_state()
        self.assertEqual(out["p5"]["future"], {"x": [1, 2]})
        self.assertEqual(list(out["p5"])[-1], "future")
        d2 = json.loads(dumps(Learner(P, FixedOffsetTime(0)).export_state()))
        d2["p5"] = {"future": 1}
        self.assertEqual(load(d2).export_state()["p5"]["future"], 1)       # alone it still makes the section

    def test_the_four_settings_persist(self):
        for name in ("approve_sched", "guests_pause", "pets", "detail_text"):
            L = ready_learner()
            L.handle(SettingSwitch(name, True, BASE_MS + 8 * DAY * 1000))
            self.assertTrue(L.p5.setting(name), name)
            st = json.loads(dumps(L.export_state()))
            self.assertTrue(st["p5"]["settings"][name], name)
            self.assertTrue(load(st).p5.setting(name), name)


class ValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(dumps(p5_learner().export_state()))
        load(copy.deepcopy(cls.base))

    def bad(self, fn):
        d = copy.deepcopy(self.base)
        fn(d)
        with self.assertRaises(StateError):
            load(d)

    def test_p5_type(self):
        self.bad(lambda d: d.update(p5=[]))
        self.bad(lambda d: d.update(p5="x"))
        self.bad(lambda d: d.update(p5=None))
        for k, v in (("later", []), ("tags", []), ("real", {}), ("masks", []), ("push", []), ("settings", [])):
            self.bad(lambda d, k=k, v=v: d["p5"].update({k: v}))

    def test_later(self):
        self.bad(lambda d: d["p5"]["later"].update({"s0001-1": "yesterday"}))
        self.bad(lambda d: d["p5"]["later"].update({"nonsense": "2026-01-14"}))
        self.bad(lambda d: d["p5"]["later"].update({"s0001-1": 5}))
        self.bad(lambda d: d["p5"].update(later={"s%04d-1" % i: "2026-01-14" for i in range(401)}))
        self.base["p5"]["later"].update({"mrg-s0001-1": "2026-01-14", "mod-z0001": "2026-01-14"})
        load(copy.deepcopy(self.base))                                    # item ids of all grammars are fine

    def test_tags(self):
        self.bad(lambda d: d["p5"]["tags"]["by_id"].update({"s0001-1": "a"}))
        self.bad(lambda d: d["p5"]["tags"]["by_id"].update({"s0001-1": "AAAA"}))
        self.bad(lambda d: d["p5"]["tags"]["by_id"].update({"s0001-1": 5}))
        self.bad(lambda d: d["p5"]["tags"]["by_id"].update({"s0001-1": "A", "s0002-1": "A"}))      # a duplicate tag
        self.bad(lambda d: d["p5"]["tags"].update(n=-1))
        self.bad(lambda d: d["p5"]["tags"].update(n=0))                                          # n < number of tags
        self.bad(lambda d: d["p5"]["tags"].update(n=True))
        self.bad(lambda d: d["p5"]["tags"].update(by_id={"s%04d-1" % i: PS.tag_text(i) for i in range(401)}, n=401))

    def test_real(self):
        self.bad(lambda d: d["p5"]["real"][0].pop("ref"))
        self.bad(lambda d: d["p5"]["real"][0].update(cx=1e9))
        self.bad(lambda d: d["p5"]["real"][0].update(cy=float("nan")))
        self.bad(lambda d: d["p5"]["real"][0].update(cells=[[0, 99999]]))
        self.bad(lambda d: d["p5"]["real"][0].update(night=-1))
        self.bad(lambda d: d["p5"].update(real=[copy.deepcopy(d["p5"]["real"][0]) for _ in range(51)]))

    def test_masks(self):
        self.bad(lambda d: d["p5"]["masks"].update({"not-an-id": copy.deepcopy(d["p5"]["masks"]["s0006-1"])}))
        self.bad(lambda d: d["p5"]["masks"]["s0006-1"].update(cx=40.0))
        self.bad(lambda d: d["p5"]["masks"]["s0006-1"].update(cells=[[-1, 1]]))
        self.bad(lambda d: d["p5"]["masks"]["s0006-1"].update(n0=-1))
        self.bad(lambda d: d["p5"]["masks"]["s0006-1"].update(n0=1.5))
        self.bad(lambda d: d["p5"].update(masks={"s%04d-1" % i: copy.deepcopy(d["p5"]["masks"]["s0006-1"])
                                                 for i in range(201)}))

    def test_nw_applied(self):
        self.bad(lambda d: d["p5"]["nw_applied"].update(rev=-1))
        self.bad(lambda d: d["p5"]["nw_applied"].update(start=1440))
        self.bad(lambda d: d["p5"]["nw_applied"].update(end=-1))
        self.bad(lambda d: d["p5"].update(nw_applied=5))
        d = copy.deepcopy(self.base)
        d["p5"]["nw_applied"] = None
        self.assertIsNone(load(d).p5.nw_applied)

    def test_push(self):
        self.bad(lambda d: d["p5"]["push"].update(seq=-1))
        self.bad(lambda d: d["p5"]["push"].update(acked=3))               # acked <= seq
        self.bad(lambda d: d["p5"]["push"].update(kind="weekly"))
        self.bad(lambda d: d["p5"]["push"].update(setup_sent=1))
        self.bad(lambda d: d["p5"]["push"].update(last_day="14.01.2026"))
        self.bad(lambda d: d["p5"]["push"].update(sig="x"))
        self.bad(lambda d: d["p5"]["push"].update(sig=["x"] * 41))
        self.bad(lambda d: d["p5"]["push"].update(sig=["x" * 41]))
        self.bad(lambda d: d["p5"]["push"].update(acks=["a-b"]))
        self.bad(lambda d: d["p5"]["push"].update(acks=["p%07d" % i for i in range(9)]))
        self.bad(lambda d: d["p5"]["push"].update(acks="pa1"))
        self.bad(lambda d: d["p5"]["push"].update(head_id=None))          # both null or both set
        self.bad(lambda d: d["p5"]["push"].update(head_rev=None))
        self.bad(lambda d: d["p5"]["push"].update(head_rev=-1))
        self.bad(lambda d: d["p5"]["push"].update(head_id=5))
        d = copy.deepcopy(self.base)
        del d["p5"]["push"]["acks"], d["p5"]["push"]["head_id"], d["p5"]["push"]["head_rev"]   # older: missing = defaults
        L = load(d)
        self.assertEqual((L.p5.push["acks"], L.p5.push["head_id"], L.p5.push["head_rev"]), ([], None, None))

    def test_settings(self):
        self.bad(lambda d: d["p5"]["settings"].update(pets="yes"))
        self.bad(lambda d: d["p5"]["settings"].update(guests_pause=1))
        d = copy.deepcopy(self.base)
        d["p5"]["settings"] = {"pets": True}                              # a missing bool is False
        L = load(d)
        self.assertEqual(L.p5.settings, {"approve_sched": False, "guests_pause": False, "pets": True,
                                         "detail_text": False})

    def test_zone_records(self):
        self.bad(lambda d: d["zones"][0].update(poly_cm=[[1, 2]] * 13))
        self.bad(lambda d: d["zones"][0].update(poly_cm=[[1.5, 2]]))
        self.bad(lambda d: d["zones"][0].update(poly_cm=[[1, 2, 3]]))
        self.bad(lambda d: d["zones"][0].update(poly_cm="x"))
        self.bad(lambda d: d["zones"][0].update(rev=-1))
        self.bad(lambda d: d["zones"][0].update(upd=1.5))
        self.bad(lambda d: d["zones"][0].update(srev=True))
        self.bad(lambda d: d["zones"][0].update(also=["s0001-1"] * 9))
        self.bad(lambda d: d["zones"][0].update(also=[5]))
        self.bad(lambda d: d["zones"][0].update(also="s0001-1"))
        self.bad(lambda d: d["zones"][0].update(ref=5))
        self.bad(lambda d: d["zones"][0].update(kind="manual"))            # the P4 pins stay
        self.bad(lambda d: d["zones"][0].update(src="mark"))

    def test_a_zone_without_ref_is_a_state_error_not_a_key_error(self):
        d = copy.deepcopy(self.base)
        del d["zones"][0]["ref"]
        with self.assertRaises(StateError):
            load(d)
        d = copy.deepcopy(self.base)
        d["zones"][0]["ref"] = None                                       # after "No, it's a new place": allowed
        self.assertIsNone(load(d).review.zones[0]["ref"])


class CompatibilityTest(unittest.TestCase):
    def test_a_p4_file_loads_with_the_defaults(self):
        L = p5_learner()
        z0 = L.review.zones[0]
        d = json.loads(dumps(L.export_state()))
        del d["p5"]
        for z in d["zones"]:
            for k in ("poly_cm", "rev", "upd", "srev", "also"):
                del z[k]
        [s for s in d["suggestions"] if s["id"] == "s0005-1"][0]["rev"] = 3
        L2 = load(d)
        z = L2.review.zones[0]
        self.assertEqual((z["rev"], z["upd"], z["also"]), (0, z0["night"], []))
        self.assertEqual(z["srev"], 3)                                    # the ref suggestion's current rev
        self.assertEqual(z["poly_cm"], z0["poly_cm"])                     # computed from the cells
        self.assertEqual(L2.p5.export()["settings"], {"approve_sched": False, "guests_pause": False, "pets": False,
                                                      "detail_text": False})
        d = json.loads(dumps(L2.export_state()))                          # and writes the extras back
        self.assertEqual(d["zones"][0]["srev"], 3)

    def test_srev_is_zero_when_the_suggestion_is_gone(self):
        d = json.loads(dumps(p5_learner().export_state()))
        d["suggestions"] = [s for s in d["suggestions"] if s["id"] != "s0005-1"]
        del d["zones"][0]["srev"]
        self.assertEqual(load(d).review.zones[0]["srev"], 0)

    def test_a_p4_place_suppression_gets_its_mask_at_load(self):
        L = p5_learner()
        d = json.loads(dumps(L.export_state()))
        del d["p5"]["masks"]["s0006-1"]                                   # what a P4 file looks like
        L2 = load(d)
        self.assertEqual(list(L2.p5.masks), ["s0006-1"])
        self.assertEqual(L2.p5.masks["s0006-1"]["n0"], 18)
        self.assertEqual(L2.p5.masks["s0006-1"]["cells"], L.p5.masks["s0006-1"]["cells"])
        d["suggestions"] = [s for s in d["suggestions"] if s["id"] != "s0006-1"]   # its suggestion is gone: no mask
        self.assertEqual(load(d).p5.masks, {})

    def test_a_mask_without_a_suppression_is_dropped_at_load(self):
        d = json.loads(dumps(p5_learner().export_state()))
        d["suppressions"] = []
        self.assertEqual(load(d).p5.masks, {})

    def test_a_v3_file_still_migrates(self):
        L = rich_learner()
        st = L.export_state()
        L2 = load(json.loads(dumps(to_v3(st))))
        self.assertEqual(dumps(L2.export_state()), dumps(st))
        self.assertEqual((L2.review.zones, L2.review.suppressions), ([], []))

    def test_phase_and_writer(self):
        st = Learner(P, FixedOffsetTime(0)).export_state()
        self.assertEqual((st["schema"], st["writer"], st["phase"]), (4, "radar_zone_learner 0.7.0", "p5"))


class P5StateUnitTest(unittest.TestCase):
    def test_default_and_is_default(self):
        s = PS.P5State()
        self.assertTrue(s.is_default())
        s.alloc_tag("s0001-1")
        self.assertFalse(s.is_default())
        s = PS.P5State()
        s.set_setting("pets", True)
        self.assertFalse(s.is_default())
        s.set_setting("pets", False)
        self.assertTrue(s.is_default())

    def test_tag_sequence(self):
        self.assertEqual([PS.tag_text(i) for i in (0, 1, 25, 26, 27, 51, 52, 701, 702, 703)],
                         ["A", "B", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA", "AAB"])
        s = PS.P5State()
        tags = [s.alloc_tag("s%04d-1" % i) for i in range(30)]
        self.assertEqual(tags[:3] + tags[26:28], ["A", "B", "C", "AA", "AB"])
        self.assertEqual(s.alloc_tag("s0000-1"), "A")                     # once
        self.assertEqual(s.tag_n, 30)
        s.prune_tags(lambda k: k == "s0003-1")
        self.assertEqual(s.tags, {"s0003-1": "D"})
        self.assertEqual(s.tag_n, 30)                                      # never decreases
        self.assertEqual(s.alloc_tag("new"), "AE")

    def test_set_later_keeps_the_newest_400(self):
        s = PS.P5State()
        for i in range(405):
            s.set_later("s%04d-1" % i, 1000 + i)
        self.assertEqual(len(s.later), 400)
        self.assertNotIn("s0000-1", s.later)
        self.assertIn("s0404-1", s.later)

    def test_unknown_setting_is_ignored(self):
        s = PS.P5State()
        self.assertFalse(s.set_setting("nonsense", True))
        self.assertFalse(s.set_setting("pets", False))
        self.assertTrue(s.set_setting("pets", True))
        self.assertFalse(s.set_setting("pets", True))

    def test_reset_for_forget_keeps_the_settings_and_the_counters_only(self):
        L = p5_learner()
        L.p5.set_setting("detail_text", True)
        fresh = L.p5.reset_for_forget()
        self.assertEqual(fresh.settings, L.p5.settings)
        self.assertEqual((fresh.push["seq"], fresh.push["acked"]), (2, 2))
        self.assertEqual((fresh.push["kind"], fresh.push["setup_sent"], fresh.push["last_day"], fresh.push["sig"],
                          fresh.push["acks"], fresh.push["head_id"]), ("none", False, None, [], [], None))
        self.assertEqual((fresh.later, fresh.tags, fresh.tag_n, fresh.real, fresh.masks, fresh.nw_applied),
                         ({}, {}, 0, [], {}, None))
        self.assertIsNot(fresh.settings, L.p5.settings)


if __name__ == "__main__":
    unittest.main()
