"""P5 push planner: the setup push once, at most one a day, the head, acknowledgements per target, the retry rule, the
persistence across restarts and Forget (spec 6.2, §11.3 `test_p5_notify`)."""
import json
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core.notify import PushPlanner
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import (FixedOffsetTime, PushAck, PushRequest, ReviewAnswer, SaveRequested)

from .test_extraction import BASE_MS, DAY, make, night
from .test_p5_answers import ans
from .test_p5_items import BED_ITEM, POS, evidence, ids, places
from .test_review import NW_ITEM, add_place, ready_learner, with_proposal

H = 3600 * 1000


def at(day, hh=0, mm=0):
    return BASE_MS + day * DAY * 1000 + (hh * 60 + mm) * 60000


def reqs(outs):
    return [o for o in outs if isinstance(o, PushRequest)]


def push_saves(outs):
    return [o for o in outs if isinstance(o, SaveRequested) and o.reason == "push"]


def settle(L, day, hh, mm=0):
    """Ticks to the given local time and returns the outputs of that tick."""
    return L.tick(at(day, hh, mm))


class SetupPushTest(unittest.TestCase):
    def test_the_setup_push_is_made_once_inside_the_window(self):
        L = places(3)                                          # night 7: bed + 3 places = 4 items
        self.assertEqual(reqs(settle(L, 7, 7, 59)), [])        # before 08:00
        outs = settle(L, 7, 8, 0)
        r = reqs(outs)
        self.assertEqual(len(r), 1)
        head = ids(L, 2)[0]
        self.assertEqual(r[0], PushRequest(1, 0, "setup", 4, head, L.review.items(L)[1].rev, ()))
        self.assertEqual(len(push_saves(outs)), 1)             # saved at once: 'sent once' survives a crash
        self.assertEqual(reqs(settle(L, 7, 8, 1)), [])         # not twice
        self.assertEqual(reqs(settle(L, 7, 12, 0)), [])
        self.assertTrue(L.p5.push["setup_sent"])
        self.assertEqual(L.p5.push["last_day"], "2026-01-12")

    def test_not_from_21_00(self):
        L2 = places(3)
        self.assertEqual(reqs(settle(L2, 7, 21, 0)), [])
        self.assertEqual(reqs(settle(L2, 7, 23, 59)), [])
        self.assertEqual(reqs(settle(L2, 8, 7, 59)), [])
        self.assertEqual(len(reqs(settle(L2, 8, 8, 0))), 1)    # the next morning
        L3 = places(3)
        self.assertEqual(len(reqs(settle(L3, 7, 20, 59))), 1)

    def test_not_at_the_nightly_run(self):
        L = make()
        got = []
        for k in range(8):
            got += reqs(L.tick(at(k, 3, 31)))
        self.assertEqual(got, [])                              # the run at 03:30 never wakes anybody
        self.assertEqual(L.p5.push["seq"], 0)

    def test_not_with_fewer_than_two_items(self):
        L = ready_learner()
        self.assertEqual(ids(L), [BED_ITEM])
        self.assertEqual(reqs(settle(L, 7, 9, 0)), [])
        add_place(L, POS[0], sid="s0005-1", conf=0.8, evidence=evidence())
        self.assertEqual(len(reqs(settle(L, 7, 9, 1))), 1)     # two items: the bed item and a place

    def test_only_tier_one_items_make_a_setup_push_without_a_head(self):
        L = ready_learner()
        with_proposal(L)
        outs = settle(L, 7, 9, 0)
        r = reqs(outs)
        self.assertEqual(r, [PushRequest(1, 0, "setup", 2, None, None, ())])

    def test_not_before_the_setup_review_is_ready(self):
        L = places(3)
        L.night = 3
        self.assertEqual(reqs(settle(L, 7, 9, 0)), [])
        self.assertEqual(L.p5.push["seq"], 0)

    def test_the_local_time_decides_not_utc(self):
        L = Learner(P, FixedOffsetTime(3600))
        from radar_zone_learner.core.types import NodeStatus
        L.handle(NodeStatus(True, BASE_MS))
        night(L, 7)
        for i in range(3):
            add_place(L, POS[i], sid="s0005-%d" % (i + 1), conf=0.8 - 0.1 * i, evidence=evidence())
        self.assertEqual(reqs(L.tick(at(7, 6, 59))), [])       # 07:59 local
        self.assertEqual(len(reqs(L.tick(at(7, 7, 0)))), 1)    # 08:00 local

    def test_no_p5_data_means_no_extra_output_before_the_first_request(self):
        L = places(2)
        outs = []
        for t in (at(7, 4), at(7, 5), at(7, 6), at(7, 7)):
            outs += L.tick(t)
        self.assertEqual(reqs(outs), [])                       # the count alone is not emitted while seq is 0
        self.assertEqual(L.push_request(), PushRequest(0, 0, "none", 3, None, None, ()))


def ack(L, target="pa1b2c3d"):
    """The blueprint's acknowledgement of the current request (an unacknowledged one is retried the next day)."""
    L.handle(PushAck(L.p5.push["seq"], (L.now or BASE_MS) + 1, target))


class DailyPushTest(unittest.TestCase):
    def after_setup(self, n=2):
        L = places(n)
        settle(L, 7, 8, 0)
        ack(L)
        return L

    def test_one_a_day_and_only_for_something_new(self):
        L = self.after_setup()
        self.assertEqual(L.p5.push["seq"], 1)
        add_place(L, POS[5], sid="s0007-1", conf=0.95, evidence=evidence())
        self.assertEqual([x.seq for x in reqs(settle(L, 7, 10, 0)) if x.seq > 1], [])   # a new item, but a push was made today
        self.assertEqual(L.p5.push["seq"], 1)
        outs = settle(L, 8, 9, 0)                              # the next local day
        r = reqs(outs)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0].seq, r[0].kind, r[0].count, r[0].acks), (2, "daily", 4, ()))
        self.assertEqual(r[0].head_id, "s0007-1")              # the first tier-2 item now
        ack(L)
        add_place(L, POS[6], sid="s0008-9", conf=0.99, evidence=evidence())
        self.assertEqual([x.seq for x in reqs(settle(L, 8, 11, 0)) if x.seq > 2], [])   # at most one a day
        self.assertEqual(len(reqs(settle(L, 9, 9, 0))), 1)     # the new one is announced the day after
        self.assertEqual(L.p5.push["seq"], 3)

    def test_nothing_new_nothing_pushed(self):
        L = self.after_setup()
        for day in (8, 9, 10):
            self.assertEqual([r for r in reqs(settle(L, day, 9, 0)) if r.seq > 1], [], day)
        self.assertEqual(L.p5.push["seq"], 1)

    def test_a_new_revision_is_new(self):
        L = self.after_setup()
        L.ns.suggestions[0]["rev"] += 1                        # the region of an announced place moved
        L.review.touch()
        r = reqs(settle(L, 8, 9, 0))
        self.assertEqual([(x.seq, x.kind) for x in r], [(2, "daily")])
        ack(L)
        self.assertEqual(reqs(settle(L, 9, 9, 0)), [])         # announced now

    def test_a_confidence_change_is_not_new(self):
        L = self.after_setup()
        L.ns.suggestions[0]["conf"] = 0.123
        L.review.touch()
        self.assertEqual(reqs(settle(L, 8, 9, 0)), [])

    def test_later_items_that_return_are_never_announced_again(self):
        L = self.after_setup(3)                                # bed + s1, s2, s3 announced (and acknowledged)
        ans(L, "s0005-1", "later")
        self.assertNotIn("s0005-1", ids(L))
        r = reqs(settle(L, 8, 9, 0))                           # the next day the nightly run brings it back
        self.assertIn("s0005-1", ids(L))
        self.assertEqual([x.seq for x in r if x.seq > 1], [])  # listed again, no second push
        self.assertEqual(L.p5.push["seq"], 1)

    def test_a_later_bed_item_that_returns_is_never_announced_again(self):
        L = self.after_setup(3)                                # the bed item was announced with the setup push
        ans(L, BED_ITEM, "later")
        self.assertNotIn(BED_ITEM, ids(L))
        r = reqs(settle(L, 8, 9, 0))                           # the next day the nightly run ends the snooze
        self.assertIn(BED_ITEM, ids(L))                        # listed again ...
        self.assertEqual(L.review.bed_rev, 2)                  # ... as a new revision (P5-5d needs the two bumps)
        self.assertEqual([x.seq for x in r if x.seq > 1], [])  # ... but no second push: [Later] is not a daily reminder
        self.assertEqual(L.p5.push["seq"], 1)

    def test_a_bed_item_snoozed_before_any_push_never_gets_one_either(self):
        L = places(3)
        ans(L, BED_ITEM, "later")                              # the user was early: before any push mentioned it
        r = reqs(settle(L, 7, 8, 0))
        self.assertEqual([(x.seq, x.kind) for x in r], [(1, "setup")])
        ack(L)
        self.assertIn(BED_ITEM + "#0", L.p5.push["sig"])       # counted as announced
        r = reqs(settle(L, 8, 9, 0))
        self.assertIn(BED_ITEM, ids(L))                        # back in the review ...
        self.assertEqual([x for x in r if x.seq > 1], [])      # ... but not pushed
        self.assertEqual(L.p5.push["seq"], 1)

    def test_an_item_snoozed_before_any_push_never_gets_one_either(self):
        L = places(3)
        ans(L, "s0005-1", "later")                             # the user was early: before any push mentioned it
        r = reqs(settle(L, 7, 8, 0))
        self.assertEqual([(x.seq, x.kind, x.count) for x in r], [(1, "setup", 3)])     # bed + 2 places
        ack(L)
        self.assertTrue(any(s.startswith("s0005-1#") for s in L.p5.push["sig"]))      # counted as announced
        r = reqs(settle(L, 8, 9, 0))
        self.assertIn("s0005-1", ids(L))                       # back in the review ...
        self.assertEqual([x for x in r if x.seq > 1], [])      # ... but not pushed
        self.assertEqual(L.p5.push["seq"], 1)

    def test_a_returned_snooze_with_a_new_item_is_pushed_for_the_new_item_only(self):
        L = self.after_setup(2)
        ans(L, "s0005-1", "later")
        add_place(L, POS[5], sid="s0007-1", conf=0.95, evidence=evidence())
        r = reqs(settle(L, 8, 9, 0))
        self.assertEqual([(x.seq, x.kind, x.head_id) for x in r if x.seq > 1], [(2, "daily", "s0007-1")])

    def test_the_sig_keeps_at_most_40_and_forgets_ids_that_are_gone(self):
        L = places(1)
        for k in range(45):
            L.p5.push["sig"].append("s%04d-1#1" % (500 + k))
        L.p5.push["sig"] = L.p5.push["sig"][-40:]
        settle(L, 7, 8, 0)
        self.assertLessEqual(len(L.p5.push["sig"]), 40)
        L.p5.push["sig"].append("s0001-1#1")                   # an id that no longer exists
        night(L, 8)
        self.assertNotIn("s0001-1#1", L.p5.push["sig"])
        self.assertIn("s0005-1#1", L.p5.push["sig"])
        self.assertIn(BED_ITEM + "#0", L.p5.push["sig"])      # the tier-one ids always exist


class HeadTest(unittest.TestCase):
    def test_the_head_is_the_first_tier_two_item_and_never_a_tier_one_item(self):
        L = places(3)
        with_proposal(L)
        self.assertEqual(ids(L)[:2], [NW_ITEM, BED_ITEM])
        r = reqs(settle(L, 7, 9, 0))[0]
        self.assertEqual(r.head_id, "s0005-1")
        self.assertEqual(r.count, 5)
        self.assertEqual(r.head_rev, L.ns.suggestions[0]["rev"])

    def test_the_head_is_fixed_per_seq(self):
        L = places(3)
        r1 = reqs(settle(L, 7, 9, 0))[0]
        self.assertEqual(r1.head_id, "s0005-1")
        ans(L, "s0005-1", "approve")                           # the head was answered on the review device
        add_place(L, POS[5], sid="s0007-1", conf=0.99, evidence=evidence())
        outs = settle(L, 7, 9, 1)
        r = reqs(outs)
        self.assertEqual(len(r), 1)                            # the count changed: a new state, the same request
        self.assertEqual((r[0].seq, r[0].head_id, r[0].head_rev, r[0].count), (1, "s0005-1", r1.head_rev, 4))
        self.assertEqual(ids(L, 2)[0], "s0007-1")
        r2 = reqs(settle(L, 8, 9, 0))[0]                       # the next seq takes the head of that moment
        self.assertEqual((r2.seq, r2.head_id), (2, "s0007-1"))

    def test_the_head_survives_a_restart(self):
        L = places(3)
        reqs(settle(L, 7, 9, 0))
        st = json.loads(json.dumps(L.export_state()))
        L2 = Learner(L.p, L.tm, st)
        self.assertEqual(L2.push_request().head_id, "s0005-1")


class EmissionTest(unittest.TestCase):
    def test_every_change_of_the_request_is_emitted_once(self):
        L = places(3)
        r1 = reqs(settle(L, 7, 9, 0))[0]
        self.assertEqual(reqs(settle(L, 7, 9, 1)), [])
        outs = ans(L, "s0005-2", "approve")
        r2 = reqs(outs)
        self.assertEqual(len(r2), 1)                           # the count fell: the blueprint sees it
        self.assertEqual((r2[0].seq, r2[0].count), (1, r1.count - 1))
        outs = ans(L, "s0005-3", "reject") + ans(L, BED_ITEM, "reject")
        self.assertEqual([r.count for r in reqs(outs)], [r1.count - 2, r1.count - 3])
        self.assertEqual(reqs(settle(L, 7, 9, 5)), [])

    def test_the_count_reaches_zero_so_the_notification_can_be_cleared(self):
        L = places(1)
        reqs(settle(L, 7, 9, 0))
        ans(L, "s0005-1", "approve")
        outs = ans(L, BED_ITEM, "reject")
        r = reqs(outs)
        self.assertEqual(r[-1].count, 0)
        self.assertEqual(r[-1].seq, 1)                         # the kept seq above 0 is what makes the blueprint clear

    def test_a_push_request_has_only_ints_strings_and_none(self):
        L = places(3)
        r = reqs(settle(L, 7, 9, 0))[0]
        for f in ("seq", "acked", "count"):
            self.assertIs(type(getattr(r, f)), int)
        self.assertIs(type(r.kind), str)
        self.assertTrue(r.head_id is None or isinstance(r.head_id, str))
        self.assertTrue(r.head_rev is None or type(r.head_rev) is int)
        self.assertIsInstance(r.acks, tuple)
        self.assertEqual(set(r.__dataclass_fields__), {"seq", "acked", "kind", "count", "head_id", "head_rev", "acks",
                                                       "attention"})                  # P7 (spec A.2): the trailing flag
        self.assertIs(r.attention, False)                                            # never set without P7


class AckTest(unittest.TestCase):
    def pushed(self):
        L = places(3)
        reqs(settle(L, 7, 9, 0))
        return L

    def test_an_ack_per_phone_is_recorded_and_emitted(self):
        L = self.pushed()
        outs = L.handle(PushAck(1, at(7, 9, 1), "pa1b2c3d"))
        r = reqs(outs)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0].seq, r[0].acked, r[0].acks), (1, 1, ("pa1b2c3d",)))
        self.assertEqual(len(push_saves(outs)), 1)             # so a phone is not notified again after a crash
        outs = L.handle(PushAck(1, at(7, 9, 2), "pe5f6a7b"))
        self.assertEqual(reqs(outs)[0].acks, ("pa1b2c3d", "pe5f6a7b"))
        self.assertEqual(len(push_saves(outs)), 1)
        outs = L.handle(PushAck(1, at(7, 9, 3), "pa1b2c3d"))   # a repeat: nothing changes, nothing is said
        self.assertEqual((reqs(outs), push_saves(outs)), ([], []))

    def test_a_wrong_seq_is_ignored(self):
        L = self.pushed()
        for seq in (0, 2, 99, -1, True, "1", None):
            outs = L.handle(PushAck(seq, at(7, 9, 1), "pa1b2c3d"))
            self.assertEqual((reqs(outs), push_saves(outs)), ([], []), seq)
        self.assertEqual((L.p5.push["acked"], L.p5.push["acks"]), (0, []))

    def test_a_plain_seq_sets_acked_only(self):
        L = self.pushed()
        outs = L.handle(PushAck(1, at(7, 9, 1)))
        r = reqs(outs)
        self.assertEqual((r[0].acked, r[0].acks), (1, ()))
        self.assertEqual(len(push_saves(outs)), 1)

    def test_a_bad_target_counts_as_a_plain_ack(self):
        L = self.pushed()
        outs = L.handle(PushAck(1, at(7, 9, 1), "not valid!"))
        self.assertEqual((reqs(outs)[0].acked, reqs(outs)[0].acks), (1, ()))
        outs = L.handle(PushAck(1, at(7, 9, 2), "x" * 13))
        self.assertEqual(L.p5.push["acks"], [])

    def test_at_most_eight_targets_the_oldest_dropped(self):
        L = self.pushed()
        for k in range(10):
            L.handle(PushAck(1, at(7, 9, 1) + k, "p%07d" % k))
        self.assertEqual(L.p5.push["acks"], ["p%07d" % k for k in range(2, 10)])
        self.assertEqual(L.push_request().acks, tuple("p%07d" % k for k in range(2, 10)))

    def test_a_new_seq_clears_the_acks(self):
        L = self.pushed()
        L.handle(PushAck(1, at(7, 9, 1), "pa1b2c3d"))
        add_place(L, POS[5], sid="s0007-1", conf=0.95, evidence=evidence())
        r = reqs(settle(L, 8, 9, 0))
        self.assertEqual((r[0].seq, r[0].acked, r[0].acks), (2, 1, ()))
        self.assertEqual(L.p5.push["acked"], 1)                # the highest seq acknowledged so far

    def test_an_ack_before_any_request_is_ignored(self):
        L = places(3)
        self.assertEqual(L.handle(PushAck(0, at(7, 5), "pa1b2c3d")).__class__, list)
        self.assertEqual((L.p5.push["acked"], L.p5.push["acks"]), (0, []))


class RetryTest(unittest.TestCase):
    def pushed(self):
        L = places(3)
        reqs(settle(L, 7, 8, 0))
        return L

    def test_an_unacknowledged_request_is_issued_again_once_a_day_inside_the_window(self):
        L = self.pushed()
        self.assertEqual(reqs(settle(L, 7, 20, 0)), [])                  # the same day: no
        self.assertEqual(reqs(settle(L, 8, 7, 59)), [])                  # outside the window: no
        outs = settle(L, 8, 8, 0)
        r = reqs(outs)
        self.assertEqual([(x.seq, x.kind, x.acked, x.acks) for x in r], [(2, "setup", 0, ())])
        self.assertEqual(len(push_saves(outs)), 1)
        self.assertEqual(L.p5.push["last_day"], "2026-01-13")
        self.assertEqual(reqs(settle(L, 8, 12, 0)), [])                  # at most one a day
        self.assertEqual([x.seq for x in reqs(settle(L, 9, 9, 0))], [3])
        self.assertEqual(reqs(settle(L, 10, 21, 0)), [])                 # not outside the window
        self.assertEqual(reqs(settle(L, 10, 7, 0)), [])

    def test_the_retry_keeps_the_kind_and_the_announced_set(self):
        L = self.pushed()
        sig = list(L.p5.push["sig"])
        settle(L, 8, 9, 0)
        self.assertEqual((L.p5.push["kind"], L.p5.push["sig"], L.p5.push["setup_sent"]), ("setup", sig, True))

    def test_never_after_an_ack(self):
        L = self.pushed()
        L.handle(PushAck(1, at(7, 8, 5), "pa1b2c3d"))
        for day in (8, 9, 10):
            self.assertEqual(reqs(settle(L, day, 9, 0)), [], day)
        self.assertEqual(L.p5.push["seq"], 1)

    def test_a_new_item_makes_a_daily_push_instead_of_a_retry(self):
        L = self.pushed()
        add_place(L, POS[5], sid="s0007-1", conf=0.95, evidence=evidence())
        r = reqs(settle(L, 8, 9, 0))
        self.assertEqual([(x.seq, x.kind) for x in r], [(2, "daily")])

    def test_nothing_to_retry_when_nothing_is_open(self):
        L = places(1)
        reqs(settle(L, 7, 8, 0))
        ans(L, "s0005-1", "approve")
        ans(L, BED_ITEM, "reject")
        self.assertEqual(ids(L), [])
        self.assertEqual([x for x in reqs(settle(L, 8, 9, 0)) if x.seq > 1], [])
        self.assertEqual(L.p5.push["seq"], 1)

    def test_a_request_every_day_of_a_week_without_an_ack_is_one_a_day(self):
        L = self.pushed()
        seqs = []
        for day in range(8, 15):
            for hh in (8, 12, 20):
                seqs += [x.seq for x in reqs(settle(L, day, hh, 0)) if x.seq > 1]
        self.assertEqual(seqs, [2, 3, 4, 5, 6, 7, 8])


class PersistenceTest(unittest.TestCase):
    def roundtrip(self, L):
        return Learner(L.p, L.tm, json.loads(json.dumps(L.export_state())))

    def test_push_request_right_after_a_load_equals_the_last_emitted(self):
        L = places(3)
        last = reqs(settle(L, 7, 9, 0))[0]
        self.assertEqual(self.roundtrip(L).push_request(), last)
        L.handle(PushAck(1, at(7, 9, 1), "pa1b2c3d"))
        last = L.push_request()
        self.assertEqual(self.roundtrip(L).push_request(), last)
        ans(L, "s0005-1", "approve")
        L.tick(at(7, 9, 2))
        self.assertEqual(self.roundtrip(L).push_request(), L.push_request())
        self.assertEqual(L.push_request().count, 3)

    def test_a_restart_after_the_request_and_before_the_ack_emits_the_same_seq_again(self):
        L = places(3)
        r1 = reqs(settle(L, 7, 9, 0))[0]
        L2 = self.roundtrip(L)
        r = reqs(L2.tick(at(7, 9, 1)))
        self.assertEqual(r, [r1])                              # no loss, no second seq
        self.assertEqual(L2.p5.push["seq"], 1)
        self.assertTrue(L2.p5.push["setup_sent"])

    def test_a_restart_before_the_request_is_due_creates_it_once(self):
        L = places(3)
        L2 = self.roundtrip(L)
        self.assertEqual(reqs(L2.tick(at(7, 7, 59))), [])
        self.assertEqual(len(reqs(L2.tick(at(7, 8, 0)))), 1)
        L3 = self.roundtrip(L2)
        self.assertEqual(reqs(L3.tick(at(7, 8, 1)))[0].seq, 1)
        self.assertEqual(L3.p5.push["seq"], 1)

    def test_a_restart_after_the_ack_does_not_retry(self):
        L = places(3)
        reqs(settle(L, 7, 9, 0))
        L.handle(PushAck(1, at(7, 9, 1), "pa1b2c3d"))
        L2 = self.roundtrip(L)
        self.assertEqual(L2.p5.push["acks"], ["pa1b2c3d"])
        self.assertEqual([x.seq for x in reqs(L2.tick(at(8, 9, 0)))], [1])    # the state, unchanged; no new seq
        self.assertEqual(L2.p5.push["seq"], 1)


class ForgetTest(unittest.TestCase):
    def test_forget_keeps_seq_and_acked_and_clears_the_rest(self):
        L = places(3)
        reqs(settle(L, 7, 9, 0))
        L.handle(PushAck(1, at(7, 9, 1), "pa1b2c3d"))
        add_place(L, POS[5], sid="s0007-1", conf=0.95, evidence=evidence())
        reqs(settle(L, 8, 9, 0))
        self.assertEqual((L.p5.push["seq"], L.p5.push["acked"]), (2, 1))
        fresh = L.reset()
        p = fresh.p5.push
        self.assertEqual((p["seq"], p["acked"]), (2, 1))
        self.assertEqual((p["kind"], p["setup_sent"], p["last_day"], p["sig"], p["acks"], p["head_id"], p["head_rev"]),
                         ("none", False, None, [], [], None, None))
        self.assertEqual(fresh.push_request(), PushRequest(2, 1, "none", 0, None, None, ()))
        from radar_zone_learner.core.types import NodeStatus
        outs = fresh.handle(NodeStatus(True, at(9, 9, 0)))
        self.assertEqual(reqs(outs), [PushRequest(2, 1, "none", 0, None, None, ())])   # the blueprint clears (seq > 0)


class PlannerUnitTest(unittest.TestCase):
    def test_state_and_request(self):
        L = ready_learner()
        pl = L.planner
        self.assertIsInstance(pl, PushPlanner)
        self.assertEqual(pl.state()["seq"], 0)
        self.assertEqual(pl.request([]), PushRequest(0, 0, "none", 0, None, None, ()))
        self.assertFalse(pl.ack(0, "p1"))
        self.assertFalse(pl.ack(1, "p1"))

    def test_the_planner_never_reads_text_times_or_names(self):
        import inspect
        import radar_zone_learner.core.notify as N
        src = inspect.getsource(N)
        for word in ('["text"]', "spot_name", '["name"]', "name_hint"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
