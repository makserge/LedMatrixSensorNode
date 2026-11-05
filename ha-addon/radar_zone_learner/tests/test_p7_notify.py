"""P7 push: the head rule, the attention request through the real planner, the announced-signature prune (spec 6.5,
check P7-11 (U) push parts)."""
import unittest

from radar_zone_learner.core.types import PushRequest, ReviewItem, SaveRequested

from .p7_harness import BASE_MS, D0, DAY, add_suggestion, add_zone, answer, make, set_run
from .test_moves import Z, sofa_scene
from .test_retire import retire_scene


def it(item_id, rev, typ, tier, **data):
    return ReviewItem(item_id, rev, typ, data, tier)


RADAR = lambda rev=1: it("radar_moved", rev, "radar_moved", 1, shift_cm=80, deg=10.0, streak=2)
RET = lambda rev=1: it("ret-z0001", rev, "retire_zone", 2, tag="A", zid="z0001", zname="Desk", weeks=3)
MOD = lambda rev=1, zid="z0002": it("mod-" + zid, rev, "modify_zone", 2, tag="B", sid="s0020-1", zid=zid, zname="Sofa",
                                    shift_cm=110, dir="to the right", r=0.9, src="cand")
PLACE = lambda sid="s0030-1", rev=1: it(sid, rev, "dwell_zone", 2, tag="C")
MRG = lambda rev=1: it("mrg-s0040-1", rev, "merge_into", 2, tag="D", sid="s0040-1", zid="z0001", zname="Desk", ext_cm=40,
                       dir="to the left", r=0.8, src="cand")


def at(day, hour=9, minute=0):
    return BASE_MS + day * DAY * 1000 + (hour * 60 + minute) * 60000


def step(L, items, day, hour=9, minute=0):
    outs = []
    L.planner.step(L, items, at(day, hour, minute), outs)
    return [o for o in outs if isinstance(o, PushRequest)]


def issued(L, items, day, hour=9, minute=0):
    """The requests this step issued as a NEW request (a higher seq); a re-emission of the current one (its count or acks
    changed) is not an issue."""
    seq = L.p5.push["seq"]
    return [r for r in step(L, items, day, hour, minute) if r.seq > seq]


def ready_learner():
    L = make()
    set_run(L, 40)
    L.p5.push["setup_sent"] = True
    return L


class HeadTest(unittest.TestCase):
    def test_a_retire_item_is_never_the_head(self):
        L = ready_learner()
        (req,) = step(L, [RET(), PLACE()], 40)
        self.assertEqual((req.kind, req.head_id, req.count), ("daily", "s0030-1", 2))
        self.assertFalse(req.attention)
        L2 = ready_learner()
        (req,) = step(L2, [RET()], 40)
        self.assertEqual((req.kind, req.head_id, req.head_rev, req.count), ("daily", None, None, 1))
        L3 = ready_learner()
        (req,) = step(L3, [RET(), MOD(), PLACE()], 40)
        self.assertEqual(req.head_id, "mod-z0002")                           # a modify may be the head (P5 rule)

    def test_the_first_tier_2_item_that_is_not_a_retire_is_the_head_and_a_merge_may_be_one(self):
        L = ready_learner()
        (req,) = step(L, [MRG(), RET()], 40)
        self.assertEqual((req.head_id, req.head_rev), ("mrg-s0040-1", 1))

    def test_radar_moved_is_tier_1_and_never_a_head_when_announced(self):
        L = ready_learner()
        L.p5.push["sig"] = ["radar_moved#1"]
        (req,) = step(L, [RADAR(), MOD()], 40)
        self.assertEqual((req.head_id, req.attention), ("mod-z0002", False))


class AttentionTest(unittest.TestCase):
    def test_a_new_radar_item_with_other_new_items_gives_one_attention_request_kind_daily_head_none(self):
        L = ready_learner()
        reqs = step(L, [RADAR(), MOD(), PLACE()], 40)
        self.assertEqual(len(reqs), 1)
        r = reqs[0]
        self.assertEqual((r.kind, r.head_id, r.head_rev, r.attention, r.count, r.seq), ("daily", None, None, True, 3, 1))
        self.assertTrue(L.zone7.rm["attn"])
        self.assertEqual(L.p5.push["kind"], "daily")                          # the vocabulary of p5.push.kind is unchanged
        self.assertIn("radar_moved#1", L.p5.push["sig"])
        self.assertEqual(issued(L, [RADAR(), MOD(), PLACE()], 40, hour=10), [])  # the same day: nothing more

    def test_the_cap_is_shared_with_the_daily_push(self):
        L = ready_learner()
        step(L, [RADAR(), MOD()], 40)
        self.assertEqual(issued(L, [RADAR(), MOD(), PLACE()], 40, hour=12), [])   # a new item the same day: no second request
        (req,) = step(L, [RADAR(), MOD(), PLACE()], 41)                       # the next day: a normal daily push
        self.assertEqual((req.kind, req.head_id, req.attention), ("daily", "mod-z0002", False))
        self.assertFalse(L.zone7.rm["attn"])

    def test_a_retry_keeps_the_head_none_and_the_attention_flag(self):
        L = ready_learner()
        step(L, [RADAR(), MOD()], 40)
        (req,) = step(L, [RADAR(), MOD()], 41)                                 # nobody acknowledged: the retry (rule 6)
        self.assertEqual((req.seq, req.kind, req.head_id, req.attention), (2, "daily", None, True))
        self.assertTrue(L.zone7.rm["attn"])

    def test_a_retry_after_the_item_closed_recomputes_the_head_and_the_flag_falls(self):
        L = ready_learner()
        step(L, [RADAR(), MOD()], 40)
        (req,) = step(L, [MOD()], 41)                                          # the radar item is gone: a normal retry
        self.assertEqual((req.head_id, req.attention), ("mod-z0002", False))
        self.assertFalse(L.zone7.rm["attn"])

    def test_the_flag_falls_when_the_item_closes_with_no_other_write(self):
        L = ready_learner()
        step(L, [RADAR(), MOD()], 40)
        self.assertTrue(L.planner.request([RADAR(), MOD()]).attention)
        self.assertFalse(L.planner.request([MOD()]).attention)
        self.assertFalse(L.planner.request([]).attention)

    def test_an_announced_radar_item_does_not_make_a_new_attention_request(self):
        L = ready_learner()
        step(L, [RADAR(), MOD()], 40)
        L.p5.push["acked"] = L.p5.push["seq"]
        (req,) = step(L, [RADAR(), MOD(), PLACE()], 41)
        self.assertEqual((req.attention, req.head_id, req.seq), (False, "mod-z0002", 2))

    def test_a_setup_request_is_never_an_attention_request(self):
        L = make()
        set_run(L, 40)
        L.zone7.rm["attn"] = True
        (req,) = step(L, [RADAR(), MOD()], 40)
        self.assertEqual((req.kind, req.attention), ("setup", False))
        self.assertFalse(L.zone7.rm["attn"])
        self.assertEqual(req.head_id, "mod-z0002")

    def test_the_request_is_saved_and_a_new_revision_is_announced_again(self):
        L = ready_learner()
        outs = []
        L.planner.step(L, [RADAR()], at(40), outs)
        self.assertTrue(any(isinstance(o, SaveRequested) and o.reason == "push" for o in outs))
        L.p5.push["acked"] = L.p5.push["seq"]
        (req,) = step(L, [RADAR(2)], 41)                                       # a second detection: revision 2
        self.assertEqual((req.attention, req.head_id), (True, None))

    def test_without_p7_there_is_no_attention(self):
        from radar_zone_learner.core.params import DEFAULT_P7
        L = make(DEFAULT_P7)
        set_run(L, 40)
        L.p5.push["setup_sent"] = True
        self.assertIsNone(L.planner.zone7)
        (req,) = step(L, [RADAR(), MOD()], 40)
        self.assertEqual((req.attention, req.head_id), (False, "mod-z0002"))


class AnnouncedIdsTest(unittest.TestCase):
    def test_an_unanswered_retire_item_announced_once_is_not_announced_again_across_five_nightly_runs(self):
        L, rec = retire_scene()
        L.review.touch()
        L.p5.push["setup_sent"] = True
        items = L.review.items(L)
        self.assertIn("ret-z0001", [i.id for i in items])
        (req,) = step(L, items, 46)
        self.assertEqual(req.kind, "daily")
        self.assertIn("ret-z0001#1", L.p5.push["sig"])
        seq = L.p5.push["seq"]
        L.p5.push["acked"] = seq
        for k in range(47, 52):
            set_run(L, k)
            L.review.on_nightly(L)                                             # prunes the announced ids
            self.assertIn("ret-z0001#1", L.p5.push["sig"], k)
            self.assertEqual(issued(L, L.review.items(L), k), [], k)
        self.assertEqual(L.p5.push["seq"], seq)

    def test_an_unanswered_radar_item_is_not_announced_again_and_a_reopened_one_is(self):
        L = ready_learner()
        add_zone(L, Z)
        L.zone7.rm.update(open=True, rev=1, st=2, d0=D0 + 38, tx=[-80, 0, -100])
        L.review.touch()
        (req,) = step(L, L.review.items(L), 40)
        self.assertTrue(req.attention)
        L.p5.push["acked"] = L.p5.push["seq"]
        for k in range(41, 46):
            set_run(L, k)
            L.review.on_nightly(L)
            self.assertIn("radar_moved#1", L.p5.push["sig"], k)
            self.assertEqual(issued(L, L.review.items(L), k), [], k)
        # answered (Ignore): the signature goes at the next nightly run
        answer(L, "radar_moved", "reject", rev=1)
        set_run(L, 46)
        L.review.on_nightly(L)
        self.assertNotIn("radar_moved#1", L.p5.push["sig"])
        # a new detection (revision 2) is announced as attention again
        L.zone7.rm.update(open=True, rev=2, st=2, ign=1)
        L.review.touch()
        (req,) = step(L, L.review.items(L), 47)
        self.assertEqual((req.attention, req.head_id), (True, None))

    def test_a_closed_and_reopened_retire_item_has_a_new_revision_and_is_announced(self):
        L, rec = retire_scene()
        L.review.touch()
        L.p5.push["setup_sent"] = True
        (req,) = step(L, L.review.items(L), 46)
        L.p5.push["acked"] = L.p5.push["seq"]
        e = L.zone7.entry("z0001")
        e["rt"] = 3
        L.review.touch()
        self.assertNotIn("ret-z0001", [i.id for i in L.review.items(L)])         # closed
        e["rt"] = 25
        L.review.touch()
        items = L.review.items(L)
        self.assertEqual([(i.id, i.rev) for i in items if i.id.startswith("ret-")], [("ret-z0001", 2)])
        (req,) = step(L, items, 47)
        self.assertIn("ret-z0001#2", L.p5.push["sig"])

    def test_an_item_that_no_longer_exists_loses_its_signature(self):
        L, rec = retire_scene()
        L.review.touch()
        L.p5.push["setup_sent"] = True
        step(L, L.review.items(L), 46)
        self.assertIn("ret-z0001#1", L.p5.push["sig"])
        answer(L, "ret-z0001", "approve", rev=1)
        set_run(L, 47)
        L.review.on_nightly(L)
        self.assertNotIn("ret-z0001#1", L.p5.push["sig"])

    def test_an_item_snoozed_before_a_push_counts_as_announced(self):
        L, rec = retire_scene()
        L.review.touch()
        L.p5.push["setup_sent"] = True
        answer(L, "ret-z0001", "later", rev=1)                                    # snoozed before any push mentioned it
        add_suggestion(L, (6.0, 6.0), sid="s0030-1", first=5)
        L.review.touch()
        items = L.review.items(L)
        self.assertEqual([i.id for i in items if i.tier == 2], ["s0030-1"])
        (req,) = step(L, items, 46)
        self.assertIn("ret-z0001#1", L.p5.push["sig"])
        self.assertIn("s0030-1#1", L.p5.push["sig"])

    def test_a_radar_item_snoozed_before_a_push_counts_as_announced(self):
        L = ready_learner()
        add_zone(L, Z)
        L.zone7.rm.update(open=True, rev=1, st=2, d0=D0 + 38)
        L.zone7.snz["radar_moved"] = L.last_run_ord
        add_suggestion(L, (6.0, 6.0), sid="s0030-1", first=5)
        L.review.touch()
        items = L.review.items(L)
        (req,) = step(L, items, 40)
        self.assertFalse(req.attention)
        self.assertIn("radar_moved#1", L.p5.push["sig"])

    def test_two_successive_modifies_are_announced_separately(self):
        L, rec, s = sofa_scene()
        L.review.touch()
        mods = [i for i in L.review.items(L) if i.id == "mod-z0001"]
        self.assertEqual(mods[0].rev, 1)
        L.p5.push["setup_sent"] = True
        step(L, L.review.items(L), 31)
        self.assertIn("mod-z0001#1", L.p5.push["sig"])
        e = L.zone7.entry("z0001")
        answer(L, "mod-z0001", "approve", rev=1)
        e["vs"], e["lu"] = "collapsed", D0 + 30
        from .p7_harness import use_days
        use_days(L, range(31, 46), (4.2, 4.0))
        add_suggestion(L, (4.2, 4.0), sid="s0040-1", first=41)
        L.p5.push["acked"] = L.p5.push["seq"]
        set_run(L, 46)
        L.review.touch()
        mods = [i for i in L.review.items(L) if i.id == "mod-z0001"]
        self.assertEqual(mods[0].rev, 2)
        (req,) = step(L, L.review.items(L), 46)
        self.assertIn("mod-z0001#2", L.p5.push["sig"])
        self.assertIn("mod-z0001#1", L.p5.push["sig"])


if __name__ == "__main__":
    unittest.main()
