"""P7 review: item ids, order, the answers by source, `move_zone`, the busy guard, snoozes, the select of `radar_moved`,
`_forget(L)` (spec 6.1-6.3, check P7-11 (U) core parts, P7-2 (U))."""
import unittest

from radar_zone_learner.core import drift, moves, review as RV
from radar_zone_learner.core.types import (AnswerIgnored, DebugMapChanged, ReviewItem, ReviewSelect, SaveRequested)

from .p7_harness import (BASE_MS, D0, add_label, add_suggestion, add_zone, answer, cover, make, p7on, prime_own, set_run,
                         use_days)
from .test_moves import N, Z, night, run_nights, sofa_scene
from .test_retire import retire_scene


def ignored(outs):
    return [(o.item, o.op, o.reason) for o in outs if isinstance(o, AnswerIgnored)]


def kinds(outs, cls):
    return [o for o in outs if isinstance(o, cls)]


def tier2(L):
    return [i for i in L.review.items(L) if i.tier == 2]


def item_ids(L):
    return [i.id for i in L.review.items(L)]


def mod_scene():
    L, rec, s = sofa_scene()
    L.review.touch()
    return L, rec, s


def open_radar(L, rev=1, shift=(-80, 0), tenths=-100):
    L.zone7.rm.update(open=True, rev=rev, st=2, d0=D0 + 10, tx=[shift[0], shift[1], tenths])
    L.review.touch()


class ItemsTest(unittest.TestCase):
    def test_the_modify_item_and_its_data_keys(self):
        L, rec, s = mod_scene()
        its = tier2(L)
        self.assertEqual([i.id for i in its], ["mod-z0001"])               # the plain item of s0020-1 is consumed
        it = its[0]
        self.assertEqual((it.type, it.tier, it.rev), ("modify_zone", 2, 1))
        self.assertEqual(set(it.data), {"tag", "sid", "zid", "zname", "shift_cm", "dir", "r", "src"})
        self.assertEqual((it.data["sid"], it.data["zid"], it.data["zname"], it.data["src"]),
                         ("s0020-1", "z0001", "Desk", "cand"))
        self.assertEqual((it.data["shift_cm"], it.data["dir"]), (110, "to the right"))
        self.assertEqual(it.data["tag"], L.p5.tags["s0020-1"])
        self.assertIsInstance(it.data["r"], float)
        hash(tuple(sorted(it.data.items())))                                # flat, hashable values only

    def test_the_merge_item_data_keys_include_src_on_the_p7_path(self):
        L = make()
        add_zone(L, Z)
        use_days(L, range(20), Z)
        run_nights(L, 14, 20)
        add_suggestion(L, (2.5, 4.0), sid="s0020-1", first=20)
        L.review.touch()
        it = tier2(L)[0]
        self.assertEqual((it.id, it.type), ("mrg-s0020-1", "merge_into"))
        self.assertEqual(set(it.data), {"tag", "sid", "zid", "zname", "ext_cm", "dir", "r", "src"})
        self.assertEqual(it.data["src"], "cand")
        self.assertEqual(it.rev, L.ns.suggestions[-1]["rev"])

    def test_the_retire_item_and_its_data_keys(self):
        L, _ = retire_scene()
        L.review.touch()
        it = tier2(L)[0]
        self.assertEqual((it.id, it.type, it.tier, it.rev), ("ret-z0001", "retire_zone", 2, 1))
        self.assertEqual(set(it.data), {"tag", "zid", "zname", "weeks"})
        self.assertEqual((it.data["zid"], it.data["zname"]), ("z0001", "Desk"))
        self.assertEqual(it.data["weeks"], L.zone7.entry("z0001")["rt"] // 7)
        self.assertGreaterEqual(it.data["weeks"], 3)
        self.assertEqual(it.data["tag"], L.p5.tags["z0001"])                 # tagged by the zone id

    def test_the_order_is_modify_places_merge_retire_and_the_cap_counts_the_rest(self):
        L = make()
        L.night = 40
        L.last_run_ord = D0 + 40
        for k in range(1, 6):
            add_suggestion(L, (1.0 + k * 0.3, 7.0), sid="s%04d-1" % (10 + k), conf=0.5 + k * 0.01)
        recs = [add_zone(L, (4.0, 2.0), name="A", sid="s0001-1"), add_zone(L, (5.0, 2.0), name="B", sid="s0002-1")]
        real = moves.plan
        sug = {s["id"]: s for s in L.ns.suggestions}

        def fake(L_, pending, ctx=None):
            merges = [{"sid": "s0012-1", "zid": "z0001", "zname": "A", "ext_cm": 40, "dir": "to the left", "rev": 1,
                       "r": 0.9, "src": "cand"}]
            mods = [{"sid": "s0013-1", "zid": "z0002", "zname": "B", "shift_cm": 90, "dir": "to the right", "rev": 1,
                     "r": 0.8, "src": "cand"}]
            rets = [{"zid": "z0001", "zname": "A", "weeks": 4, "rev": 1}]
            return merges, mods, rets, {"s0012-1", "s0013-1"}
        RV.moves.plan = fake
        try:
            L.review.touch()
            self.assertEqual([i.id for i in tier2(L)], ["mod-z0002", "s0015-1", "s0014-1", "s0011-1", "mrg-s0012-1",
                                                       "ret-z0001"])
            self.assertEqual(L.review.more, 0)
            for k in range(6, 20):
                add_suggestion(L, (1.0 + k * 0.3, 6.0), sid="s%04d-1" % (10 + k), conf=0.4)
            L.review.touch()
            its = tier2(L)
            self.assertEqual(len(its), 12)
            self.assertEqual(its[0].id, "mod-z0002")                          # a modify is never cut by a full list of places
            self.assertGreater(L.review.more, 0)
            self.assertEqual(L.review.more, (1 + 17 + 1 + 1) - 12)                # 17 plain places (19 minus the 2 consumed)
        finally:
            RV.moves.plan = real

    def test_the_radar_moved_item_is_tier_1_after_the_two_p4_items(self):
        L = make()
        set_run(L, 40)
        open_radar(L)
        its = L.review.items(L)
        self.assertEqual([i.id for i in its][:2], ["bed_needs_data", "radar_moved"])
        rm = its[1]
        self.assertEqual((rm.type, rm.tier, rm.rev), ("radar_moved", 1, 1))
        self.assertEqual(rm.data, {"shift_cm": 80, "deg": 10.0, "streak": 2})
        self.assertIsInstance(rm.data["deg"], float)

    def test_the_radar_moved_item_closes_when_ignored_or_snoozed_or_not_open(self):
        L = make()
        set_run(L, 40)
        self.assertNotIn("radar_moved", item_ids(L))
        open_radar(L)
        self.assertIn("radar_moved", item_ids(L))
        L.zone7.rm["ign"] = 1
        self.assertNotIn("radar_moved", item_ids(L))
        L.zone7.rm["ign"] = 0
        L.zone7.snz["radar_moved"] = L.last_run_ord
        self.assertNotIn("radar_moved", item_ids(L))
        L.zone7.snz.clear()
        self.assertIn("radar_moved", item_ids(L))

    def test_the_revision_follows_the_candidate(self):
        L, rec, s = mod_scene()
        self.assertEqual(tier2(L)[0].rev, 1)
        s["rev"] = 2
        L.review.touch()
        self.assertEqual(tier2(L)[0].rev, 2)

    def test_probation_is_applied_to_the_plain_list_only_after_the_consumption(self):
        # a candidate first made on night 39 with an evidence span of 4 covered dates: consumed by a modify it is not
        # hidden by probation; the same candidate, not consumed, is hidden
        L, rec, s = sofa_scene(post=17, new_sid="s0039-1")
        s["first_night"] = 39
        L.review.touch()
        self.assertEqual([i.id for i in tier2(L)], ["mod-z0001"])
        self.assertEqual(L.review.probation_held, 0)
        L2 = make()
        L2.night = 40
        L2.last_run_ord = D0 + 40
        for d in range(40):
            cover(L2, d)
        for d in range(36, 40):
            from .p7_harness import add_stay
            add_stay(L2, d, 10, 3000, 5.0, 4.0)
        add_suggestion(L2, (5.0, 4.0), sid="s0039-1", first=39)
        L2.review.touch()
        self.assertEqual(tier2(L2), [])
        self.assertEqual(L2.review.probation_held, 1)
        self.assertEqual(L2.status().attributes["places_probation"], 1)
        self.assertEqual(L2.review.attributes(L2)["tier2"], 0)

    def test_an_rc_zone_hides_overlapping_plain_items_and_counts_them(self):
        L = make()
        L.night = 40
        L.last_run_ord = D0 + 40
        add_zone(L, Z)
        L.zone7.entry("z0001", create=True)["rc"] = True
        add_suggestion(L, (Z[0] + 0.1, Z[1]), sid="s0020-1", first=10)
        add_suggestion(L, (6.0, 6.0), sid="s0021-1", first=10)
        L.review.touch()
        self.assertEqual([i.id for i in tier2(L)], ["s0021-1"])
        self.assertEqual(L.status().attributes["places_recheck_held"], 1)

    def test_with_p7_disabled_there_is_no_p7_item_and_the_p5_path_runs(self):
        from radar_zone_learner.core.params import DEFAULT_P7
        L = make(DEFAULT_P7)
        L.night = 40
        L.last_run_ord = D0 + 40
        L.zone7.rm.update(open=True, rev=1, st=2)
        self.assertNotIn("radar_moved", item_ids(L))
        self.assertEqual(L.review.resolve(L, "radar_moved"), (None, None, None))
        self.assertEqual(L.review.resolve(L, "ret-z0001"), (None, None, None))
        self.assertFalse(L.review.open_tier1(L, "radar_moved"))


class ModifyAnswerTest(unittest.TestCase):
    def approve_cand(self):
        L, rec, s = mod_scene()
        old_cells = frozenset(rec["cells"])
        old_ref = rec["ref"]
        rev0 = rec["rev"]
        outs = answer(L, "mod-z0001", "approve", rev=1)
        return L, rec, s, old_cells, old_ref, rev0, outs

    def test_approve_moves_the_zone_keeping_id_name_and_kind(self):
        L, rec, s, old_cells, old_ref, rev0, outs = self.approve_cand()
        self.assertEqual(ignored(outs), [])
        self.assertEqual([z["zid"] for z in L.review.zones], ["z0001"])        # one zone, the same id
        z = L.review.zones[0]
        self.assertEqual((z["zid"], z["name"], z["kind"], z["rev"]), ("z0001", "Desk", "dwell", rev0 + 1))
        self.assertEqual(z["cells"], frozenset(s["cells"]))
        self.assertEqual((z["cx"], z["cy"]), (s["cx"], s["cy"]))
        self.assertEqual(z["srev"], s["rev"])
        self.assertEqual(z["ref"], "s0020-1")                                  # the swap: the new suggestion is covered
        # review fix (movedfrom F1): the old suggestion does NOT join also: after the moved-from mask lifts the old spot is
        # an ordinary pending suggestion again (the mask hides it until then)
        self.assertEqual(z["also"], [])
        self.assertEqual(z["upd"], L.night)
        self.assertEqual(len(kinds(outs, DebugMapChanged)), 1)
        self.assertEqual(kinds(outs, SaveRequested)[0].reason, "review")

    def test_approve_records_the_moved_from_mask_and_restarts_the_runs(self):
        L, rec, s, old_cells, old_ref, rev0, outs = self.approve_cand()
        e = L.zone7.entry("z0001")
        self.assertEqual((e["lo"], e["rt"], e["rc"], e["ms"]), (0, 0, False, ""))
        self.assertEqual(e["lu"], L.review._today(L))
        self.assertEqual(e["mr"], 1)                                           # mr is never reset
        self.assertEqual(len(L.zone7.mf), 1)
        m = L.zone7.mf[0]
        self.assertEqual((m["zid"], m["why"], m["day"], m["cells"]), ("z0001", "moved_from", L.review._today(L), old_cells))
        self.assertEqual(L.moved_from_count(), 1)
        self.assertEqual(L.status().attributes["moved_from"], 1)

    def test_after_the_approve_neither_suggestion_comes_back_as_a_plain_place(self):
        L, rec, s, old_cells, old_ref, rev0, outs = self.approve_cand()
        self.assertEqual(tier2(L), [])
        L.review.touch()
        self.assertEqual(tier2(L), [])
        old = next(x for x in L.ns.suggestions if x["id"] == old_ref)
        self.assertEqual(old["state"], "shadow")                                # it is still shadow (hidden by the mask)

    def test_the_old_spot_is_not_offered_again_as_a_new_place(self):
        L, rec, s, old_cells, old_ref, rev0, outs = self.approve_cand()
        # a new suggestion that overlaps the old cells: hidden by the open-item predicate
        again = add_suggestion(L, Z, sid="s0030-1", first=41)
        L.review.touch()
        self.assertNotIn("s0030-1", item_ids(L))

    def test_approve_for_the_own_source_leaves_the_ref_and_also_alone(self):
        L = make()
        rec = add_zone(L, Z)
        use_days(L, range(14), Z)
        for d in range(14, 17):
            cover(L, d)
        run_nights(L, 14, 17)
        own = next(x for x in L.ns.suggestions if x["id"] == rec["ref"])
        own["cells"] = frozenset(L.geom.cells_within(Z[0] + 0.5, Z[1], 0.3))
        own["cx"], own["cy"] = Z[0] + 0.5, Z[1]
        own["rev"] = rec["srev"] + 1
        prime_own(L)                                      # review fix: the own source needs two nightly runs
        L.review.touch()
        it = tier2(L)[0]
        self.assertEqual((it.id, it.data["src"], it.data["sid"]), ("mod-z0001", "own", rec["ref"]))
        outs = answer(L, "mod-z0001", "approve", rev=it.rev)
        self.assertEqual(ignored(outs), [])
        z = L.review.zones[0]
        self.assertEqual((z["ref"], z["also"], z["srev"], z["rev"]), (rec["ref"], [], own["rev"], 1))
        self.assertEqual(z["cells"], frozenset(own["cells"]))

    def test_cand_new_place_leaves_the_old_zone_untouched(self):
        L, rec, s = mod_scene()
        before = {k: (list(v) if isinstance(v, list) else v) for k, v in rec.items()}
        outs = answer(L, "mod-z0001", "rename", rev=1, name="Second sofa")
        self.assertEqual(ignored(outs), [])
        self.assertEqual([z["zid"] for z in L.review.zones], ["z0001", "z0002"])
        z1, z2 = L.review.zones
        self.assertEqual((z1["ref"], z1["also"], z1["srev"], z1["cells"], z1["rev"]),
                         (before["ref"], before["also"], before["srev"], before["cells"], before["rev"]))
        self.assertEqual((z2["name"], z2["ref"]), ("Second sofa", "s0020-1"))   # a new zone from the candidate
        self.assertEqual(tier2(L), [])                                          # the old zone's own suggestion is not uncovered
        self.assertEqual(L.zone7.entry("z0001")["ms"], "")
        self.assertEqual(L.zone7.mf, [])

    def test_own_new_place_is_the_p5_behaviour(self):
        L = make()
        rec = add_zone(L, Z)
        use_days(L, range(14), Z)
        for d in range(14, 17):
            cover(L, d)
        run_nights(L, 14, 17)
        own = next(x for x in L.ns.suggestions if x["id"] == rec["ref"])
        own["cells"] = frozenset(L.geom.cells_within(Z[0] + 0.5, Z[1], 0.3))
        own["cx"], own["cy"] = Z[0] + 0.5, Z[1]
        own["rev"] = rec["srev"] + 1
        prime_own(L)                                      # review fix: the own source needs two nightly runs
        L.review.touch()
        it = tier2(L)[0]
        old_ref = rec["ref"]
        outs = answer(L, "mod-z0001", "rename", rev=it.rev, name="Other")
        self.assertEqual(ignored(outs), [])
        z1, z2 = L.review.zones
        self.assertEqual((z1["ref"], z1["srev"]), (None, own["rev"]))           # S passes to the new zone
        self.assertEqual((z2["name"], z2["ref"]), ("Other", old_ref))

    def test_cand_reject_adds_only_the_rejection_mask(self):
        L, rec, s = mod_scene()
        srev = rec["srev"]
        outs = answer(L, "mod-z0001", "reject", rev=1)
        self.assertEqual(ignored(outs), [])
        self.assertEqual(rec["srev"], srev)                                     # srev means 'the rev of the ref suggestion'
        self.assertIn("s0020-1", L.p5.masks)
        self.assertEqual([x["ref"] for x in L.review.suppressions if x["type"] == "place"], ["s0020-1"])
        self.assertEqual(L.zone7.mf, [])
        self.assertNotIn("mod-z0001", item_ids(L))

    def test_own_reject_sets_srev_as_in_p5(self):
        L = make()
        rec = add_zone(L, Z)
        use_days(L, range(14), Z)
        for d in range(14, 17):
            cover(L, d)
        run_nights(L, 14, 17)
        own = next(x for x in L.ns.suggestions if x["id"] == rec["ref"])
        own["cells"] = frozenset(L.geom.cells_within(Z[0] + 0.5, Z[1], 0.3))
        own["cx"], own["cy"] = Z[0] + 0.5, Z[1]
        own["rev"] = rec["srev"] + 1
        prime_own(L)                                      # review fix: the own source needs two nightly runs
        L.review.touch()
        it = tier2(L)[0]
        answer(L, "mod-z0001", "reject", rev=it.rev)
        self.assertEqual(rec["srev"], own["rev"])
        self.assertEqual(L.p5.masks, {})
        self.assertNotIn("mod-z0001", item_ids(L))

    def test_later_snoozes_in_p5_later_and_adds_no_p7_key(self):
        L, rec, s = mod_scene()
        answer(L, "mod-z0001", "later", rev=1)
        self.assertEqual(L.p5.later, {"mod-z0001": L.last_run_ord})
        self.assertEqual(L.zone7.snz, {})
        self.assertNotIn("mod-z0001", item_ids(L))
        self.assertNotIn("s0020-1", item_ids(L))                                # consumed: not shown as a plain place either

    def test_a_stale_answer_is_refused_and_a_repeat_after_the_approve_is_refused(self):
        L, rec, s = mod_scene()
        outs = answer(L, "mod-z0001", "approve", rev=7)
        self.assertEqual(ignored(outs), [("mod-z0001", "approve", "stale")])
        self.assertEqual(L.review.zones[0]["rev"], 0)
        answer(L, "mod-z0001", "approve", rev=1)
        outs = answer(L, "mod-z0001", "approve", rev=1)
        self.assertEqual(ignored(outs), [("mod-z0001", "approve", "no_such_item")])

    def test_two_successive_modifies_get_different_revisions_and_the_first_tap_is_stale(self):
        L, rec, s = mod_scene()
        e = L.zone7.entry("z0001")
        first = tier2(L)[0]
        self.assertEqual(first.rev, 1)
        answer(L, "mod-z0001", "approve", rev=1)
        # the sofa moves again: the zone falls, a new candidate 1.1 m further
        e["vs"], e["lu"] = "collapsed", D0 + 30
        s2 = add_suggestion(L, (4.2, 4.0), sid="s0040-1", first=41)
        use_days(L, range(31, 46), (4.2, 4.0))
        L.review.touch()
        second = [i for i in tier2(L) if i.id == "mod-z0001"]
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].rev, 2)                                      # mr is never reset: a new revision
        self.assertNotEqual((first.id, first.rev), (second[0].id, second[0].rev))
        outs = answer(L, "mod-z0001", "approve", rev=1)                          # a tap on the first notification
        self.assertEqual(ignored(outs), [("mod-z0001", "approve", "stale")])
        self.assertEqual(L.review.zones[0]["ref"], "s0020-1")
        outs = answer(L, "mod-z0001", "approve", rev=2)
        self.assertEqual(ignored(outs), [])
        self.assertEqual(L.review.zones[0]["ref"], "s0040-1")
        self.assertEqual(L.review.zones[0]["also"], [])                  # review fix (movedfrom F1): no old ref in also
        self.assertEqual(len(L.zone7.mf), 2)

    def test_the_busy_guard_refuses_approve_but_never_reject_or_later(self):
        L, rec, s = mod_scene()
        L.zone_snapshot = lambda: (("z0001", True, True),)
        outs = answer(L, "mod-z0001", "approve", rev=1)
        self.assertEqual(ignored(outs), [("mod-z0001", "approve", "zone_busy")])
        self.assertEqual(L.review.zones[0]["rev"], 0)
        self.assertIn("mod-z0001", item_ids(L))                                 # the item stays open
        L.zone_snapshot = lambda: (("z0001", True, False),)                      # radars off: the zone is invalid
        L2, rec2, s2 = mod_scene()
        L2.zone_snapshot = lambda: (("z0001", True, True),)
        self.assertEqual(ignored(answer(L2, "mod-z0001", "later", rev=1)), [])
        L3, rec3, s3 = mod_scene()
        L3.zone_snapshot = lambda: (("z0001", True, True),)
        self.assertEqual(ignored(answer(L3, "mod-z0001", "reject", rev=1)), [])
        L4, rec4, s4 = mod_scene()
        L4.zone_snapshot = lambda: (("z0001", True, False),)
        self.assertEqual(ignored(answer(L4, "mod-z0001", "approve", rev=1)), [])   # not occupied-and-valid: allowed
        L5, rec5, s5 = mod_scene()
        L5.zone_snapshot = lambda: (("z0001", False, True),)
        self.assertEqual(ignored(answer(L5, "mod-z0001", "approve", rev=1)), [])

    def test_the_busy_guard_can_be_switched_off(self):
        L, rec, s = sofa_scene(p7=p7on(busy_guard=False))
        L.review.touch()
        L.zone_snapshot = lambda: (("z0001", True, True),)
        self.assertEqual(ignored(answer(L, "mod-z0001", "approve", rev=1)), [])

    def test_a_frozen_zone_or_a_bed_is_not_applicable(self):
        L, rec, s = mod_scene()
        prop = L.review._mod_by_zid.get("z0001") or L.review.resolve(L, "mod-z0001")[1]
        rec["kind"] = "bed"
        outs = []
        self.assertFalse(L.review._answer_modify(L, type("E", (), {"op": "approve", "name": None, "aid": None})(),
                                                 "mod-z0001", prop, outs, BASE_MS))
        self.assertEqual(ignored(outs), [("mod-z0001", "approve", "not_applicable")])
        rec["kind"] = "dwell"
        L.zone7.entry("z0001")["fz"] = True
        outs = []
        self.assertFalse(L.review._answer_modify(L, type("E", (), {"op": "approve", "name": None, "aid": None})(),
                                                 "mod-z0001", prop, outs, BASE_MS))
        self.assertEqual(ignored(outs), [("mod-z0001", "approve", "not_applicable")])

    def test_a_merge_approval_refuses_a_frozen_zone_and_extends_otherwise(self):
        L = make()
        rec = add_zone(L, Z)
        use_days(L, range(20), Z)
        run_nights(L, 14, 20)
        add_suggestion(L, (2.5, 4.0), sid="s0020-1", first=20)
        L.review.touch()
        L.zone7.entry("z0001")["fz"] = True                                      # frozen after the item was shown
        prop = {"sid": "s0020-1", "zid": "z0001", "zname": "Desk", "ext_cm": 50, "dir": "to the right", "rev": 1,
                "r": 1.0, "src": "cand"}
        outs = []
        ev = type("E", (), {"op": "approve", "name": None, "aid": None})()
        self.assertFalse(L.review._answer_merge(L, ev, "mrg-s0020-1", prop, outs, BASE_MS))
        self.assertEqual(ignored(outs), [("mrg-s0020-1", "approve", "not_applicable")])
        L.zone7.entry("z0001")["fz"] = False
        L.zone_snapshot = lambda: (("z0001", True, True),)                        # the busy guard does not apply to a merge
        outs = answer(L, "mrg-s0020-1", "approve", rev=1)
        self.assertEqual(ignored(outs), [])
        self.assertIn("s0020-1", L.review.zones[0]["also"])


class RetireAnswerTest(unittest.TestCase):
    def test_approve_removes_the_zone_leaves_a_retired_mask_and_drops_the_entry(self):
        L, rec = retire_scene()
        L.review.touch()
        cells = frozenset(rec["cells"])
        L.zone7.snz["ret-z0001"] = 3
        outs = answer(L, "ret-z0001", "approve", rev=1)
        self.assertEqual(ignored(outs), [])
        self.assertEqual(L.review.zones, [])
        self.assertIsNone(L.zone7.entry("z0001"))
        self.assertNotIn("ret-z0001", L.zone7.snz)
        self.assertEqual([(m["zid"], m["why"], m["cells"]) for m in L.zone7.mf], [("z0001", "retired", cells)])
        self.assertEqual(len(kinds(outs, DebugMapChanged)), 1)
        self.assertNotIn("ret-z0001", item_ids(L))
        # the zone's own suggestion is uncovered now but matches the mask: it is never an item
        self.assertEqual(tier2(L), [])

    def test_keep_sets_kp_and_closes_the_item(self):
        L, rec = retire_scene()
        L.review.touch()
        outs = answer(L, "ret-z0001", "reject", rev=1)
        self.assertEqual(ignored(outs), [])
        e = L.zone7.entry("z0001")
        self.assertEqual((e["kp"], e["rt"], e["ro"]), (L.review._today(L) + 90, 0, False))
        self.assertEqual(len(L.review.zones), 1)
        self.assertNotIn("ret-z0001", item_ids(L))
        self.assertEqual(L.p5.later, {})

    def test_later_snoozes_in_zone7_snz_and_p5_later_has_no_new_key(self):
        L, rec = retire_scene()
        L.review.touch()
        answer(L, "ret-z0001", "later", rev=1)
        self.assertEqual(L.zone7.snz, {"ret-z0001": L.last_run_ord})
        self.assertEqual(L.p5.later, {})
        self.assertNotIn("ret-z0001", item_ids(L))
        self.assertEqual(L.zone7.entry("z0001")["rr"], 1)                        # the same revision when it returns
        L.review.on_nightly(L)                                                   # the next run of the same date keeps it
        self.assertIn("ret-z0001", L.zone7.snz)
        L.last_run_ord += 1
        L.review.on_nightly(L)                                                   # the next run clears it
        self.assertEqual(L.zone7.snz, {})

    def test_rename_is_not_applicable_and_a_stale_tap_is_refused(self):
        L, rec = retire_scene()
        L.review.touch()
        self.assertEqual(ignored(answer(L, "ret-z0001", "rename", rev=1, name="X")),
                         [("ret-z0001", "rename", "not_applicable")])
        self.assertEqual(ignored(answer(L, "ret-z0001", "approve", rev=5)), [("ret-z0001", "approve", "stale")])
        self.assertEqual(len(L.review.zones), 1)

    def test_the_busy_guard_applies_to_the_removal_and_not_to_keep(self):
        L, rec = retire_scene()
        L.review.touch()
        L.zone_snapshot = lambda: (("z0001", True, True),)
        self.assertEqual(ignored(answer(L, "ret-z0001", "approve", rev=1)), [("ret-z0001", "approve", "zone_busy")])
        self.assertEqual(len(L.review.zones), 1)
        self.assertEqual(ignored(answer(L, "ret-z0001", "reject", rev=1)), [])

    def test_a_frozen_zone_cannot_be_removed(self):
        L, rec = retire_scene()
        L.review.touch()
        prop = L.review.resolve(L, "ret-z0001")[1]
        L.zone7.entry("z0001")["fz"] = True
        outs = []
        ev = type("E", (), {"op": "approve", "name": None, "aid": None})()
        self.assertFalse(L.review._answer_retire(L, ev, "ret-z0001", prop, outs))
        self.assertEqual(ignored(outs), [("ret-z0001", "approve", "not_applicable")])

    def test_a_second_delivery_after_the_removal_is_refused(self):
        L, rec = retire_scene()
        L.review.touch()
        answer(L, "ret-z0001", "approve", rev=1)
        self.assertEqual(ignored(answer(L, "ret-z0001", "approve", rev=1)), [("ret-z0001", "approve", "no_such_item")])


class RadarAnswerTest(unittest.TestCase):
    def scene(self):
        L = make()
        set_run(L, 40)
        add_zone(L, Z)
        open_radar(L)
        return L

    def test_reject_is_ignore(self):
        L = self.scene()
        outs = answer(L, "radar_moved", "reject", rev=1)
        self.assertEqual(ignored(outs), [])
        rm = L.zone7.rm
        self.assertEqual((rm["ign"], rm["open"], rm["st"], rm["d0"]), (1, False, 0, 0))
        self.assertEqual(rm["qt"], L.last_run_ord + 14)
        self.assertNotIn("radar_moved", item_ids(L))
        self.assertEqual(L.radar_moved_state().state, "ignored")

    def test_approve_resets_the_maps_and_closes_the_item(self):
        L = self.scene()
        for d in range(10):
            cover(L, d)
        L.bh.stays.append({"t0": BASE_MS // 1000, "t1": BASE_MS // 1000, "dur": 600, "x": 200, "y": 400, "sd": 5,
                           "nf": 1, "ld": 1.0, "he": 0.0, "exit": "moving", "arr": "walk", "day": "2026-01-05"})
        outs = answer(L, "radar_moved", "approve", rev=1)
        self.assertEqual(ignored(outs), [])
        self.assertEqual(L.reset_summary()["stays"], 0)
        rm = L.zone7.rm
        self.assertEqual((rm["open"], rm["st"], rm["d0"], rm["rev"]), (False, 0, 0, 1))
        self.assertTrue(L.zone7.entry("z0001")["rc"])
        self.assertEqual(len(kinds(outs, DebugMapChanged)), 1)
        self.assertEqual(kinds(outs, SaveRequested)[0].reason, "review")
        self.assertNotIn("radar_moved", item_ids(L))

    def test_later_snoozes_and_a_stale_or_repeated_answer_is_refused(self):
        L = self.scene()
        answer(L, "radar_moved", "later", rev=1)
        self.assertEqual(L.zone7.snz, {"radar_moved": L.last_run_ord})
        self.assertEqual(L.p5.later, {})
        self.assertNotIn("radar_moved", item_ids(L))
        L2 = self.scene()
        self.assertEqual(ignored(answer(L2, "radar_moved", "approve", rev=9)), [("radar_moved", "approve", "stale")])
        answer(L2, "radar_moved", "approve", rev=1)
        self.assertEqual(ignored(answer(L2, "radar_moved", "approve", rev=1)), [("radar_moved", "approve", "not_pending")])
        L3 = self.scene()
        self.assertEqual(ignored(answer(L3, "radar_moved", "rename", rev=1, name="x")),
                         [("radar_moved", "rename", "not_applicable")])

    def test_the_select_accepts_the_radar_item_after_another_item_was_chosen(self):
        L = self.scene()
        add_suggestion(L, (6.0, 6.0), sid="s0021-1", first=10)
        L.review.touch()
        ids = item_ids(L)
        self.assertIn("radar_moved", ids)
        other = next(i for i in ids if i not in ("radar_moved",))
        L.handle(ReviewSelect(other, L.now + 1))
        self.assertEqual(L.review.selected(L), other)
        L.handle(ReviewSelect("radar_moved", L.now + 2))
        self.assertEqual(L.review.selected(L), "radar_moved")
        outs = answer(L, None, "reject", rev=1)                                  # the selected item: the radar item
        self.assertEqual(ignored(outs), [])
        self.assertEqual(L.zone7.rm["ign"], 1)

    def test_the_select_of_a_closed_radar_item_is_dropped(self):
        L = self.scene()
        L.zone7.rm["open"] = False
        L.handle(ReviewSelect("radar_moved", L.now + 1))
        self.assertNotEqual(L.review.selected(L), "radar_moved")
        self.assertFalse(L.review.select_chosen and L.review.select == "radar_moved")


class ForgetTest(unittest.TestCase):
    def test_forget_rejections_also_clears_the_p7_data(self):
        L, rec, s = mod_scene()
        answer(L, "mod-z0001", "approve", rev=1)
        L.zone7.snz["ret-z0001"] = 3
        L.zone7.entry("z0001")["kp"] = 999
        L.zone7.rm["ign"] = 1
        self.assertTrue(L.zone7.mf)
        outs = answer(L, None, "forget")
        self.assertEqual(ignored(outs), [])
        self.assertEqual((L.zone7.mf, L.zone7.snz, L.zone7.entry("z0001")["kp"], L.zone7.rm["ign"]), ([], {}, 0, 0))
        self.assertEqual(L.zone7.entry("z0001")["mr"], 1)                         # the revision counters stay

    def test_forget_changes_nothing_when_there_is_nothing_to_forget(self):
        L = make()
        set_run(L, 40)
        self.assertFalse(L.review._forget(L))


class HighlightTest(unittest.TestCase):
    def test_a_modify_item_highlights_the_candidate_and_a_retire_item_the_zone(self):
        L, rec, s = mod_scene()
        self.assertEqual(L._highlight_cells("mod-z0001"), frozenset(s["cells"]))
        self.assertNotEqual(L._highlight_cells("mod-z0001"), frozenset(rec["cells"]))
        L2, rec2 = retire_scene()
        L2.review.touch()
        self.assertEqual(L2._highlight_cells("ret-z0001"), frozenset(rec2["cells"]))
        self.assertIsNone(L2._highlight_cells("radar_moved"))

    def test_the_review_map_draws_the_other_open_items(self):
        L, rec, s = mod_scene()
        add_suggestion(L, (6.0, 6.0), sid="s0021-1", first=10)
        L.review.touch()
        m = L.review_map("mod-z0001")
        self.assertEqual(m.highlight, frozenset(s["cells"]))
        self.assertEqual(len(m.others), 1)
        L2, rec2 = retire_scene()
        L2.review.touch()
        add_suggestion(L2, (6.0, 6.0), sid="s0021-1", first=10)
        L2.review.touch()
        m2 = L2.review_map("s0021-1")
        self.assertIn(frozenset(rec2["cells"]), m2.others)


class StatusTest(unittest.TestCase):
    def test_the_status_counts_only_when_enabled_and_carry_no_name_or_id(self):
        L, rec, s = mod_scene()
        a = L.status().attributes
        for k in ("zones_frozen", "zones_blind", "zones_falling", "zones_unused", "zones_recheck", "moved_from",
                  "radar_moved", "places_probation", "places_recheck_held"):
            self.assertIn(k, a)
        self.assertEqual((a["radar_moved"], a["zones_recheck"]), ("none", 0))
        text = repr({k: a[k] for k in ("zones_frozen", "zones_blind", "zones_falling", "zones_unused", "zones_recheck",
                                       "moved_from", "radar_moved", "places_probation", "places_recheck_held")})
        self.assertNotIn("z0001", text)
        self.assertNotIn("Desk", text)
        from radar_zone_learner.core.params import DEFAULT_P7
        L2 = make(DEFAULT_P7)
        self.assertNotIn("zones_frozen", L2.status().attributes)
        self.assertNotIn("radar_moved", L2.status().attributes)

    def test_the_radar_state_names_in_the_status(self):
        L = make()
        set_run(L, 40)
        self.assertEqual(L.status().attributes["radar_moved"], "none")
        L.zone7.rm["st"] = 1
        self.assertEqual(L.status().attributes["radar_moved"], "watching")
        open_radar(L)
        self.assertEqual(L.status().attributes["radar_moved"], "open")
        answer(L, "radar_moved", "reject", rev=1)
        self.assertEqual(L.status().attributes["radar_moved"], "ignored")


if __name__ == "__main__":
    unittest.main()
