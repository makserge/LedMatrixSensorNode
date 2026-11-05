"""P7 retire after 21 covered days (spec 4.5, check P7-5 (U))."""
import unittest

from radar_zone_learner.core import drift, moves
from radar_zone_learner.core.params import DEFAULT_P7

from .p7_harness import D0, add_label, add_zone, cover, make, p7on, set_run, use_days

Z = (2.0, 4.0)


def night(L, k):
    set_run(L, k)
    drift.nightly(L, k)


def retire_scene(unused_until=46, p7=None, hours_cov=range(24), **zkw):
    """A zone used on days 0-13 and never again; every date is covered; the drift step has run every night."""
    L = make(p7)
    rec = add_zone(L, Z, **zkw)
    use_days(L, range(14), Z, cover_hours=hours_cov)
    for k in range(14, unused_until + 1):
        cover(L, k - 1, hours=hours_cov)
        night(L, k)
    return L, rec


class RetireTest(unittest.TestCase):
    def first_night_open(self, **kw):
        L = make(kw.pop("p7", None))
        add_zone(L, Z)
        use_days(L, range(14), Z)
        n1 = n_open = None
        for k in range(14, 60):
            cover(L, k - 1)
            night(L, k)
            e = L.zone7.entry("z0001")
            if n1 is None and e["rt"] >= 1:
                n1 = k
            rets = moves.plan(L, [])[2]
            if n_open is None and rets:
                n_open = k
                break
        return L, n1, n_open

    def test_a_retire_proposal_exists_after_21_low_verdicts_and_not_before(self):
        L, n1, n_open = self.first_night_open()
        self.assertIsNotNone(n1)
        self.assertEqual(L.zone7.entry("z0001")["rt"], 21)
        self.assertEqual(n_open - n1, 20)                          # the 21st low verdict is the 21st covered date
        rets = moves.plan(L, [])[2]
        self.assertEqual(len(rets), 1)
        r = rets[0]
        self.assertEqual((r["zid"], r["zname"], r["weeks"]), ("z0001", "Desk", 3))
        self.assertEqual(r["rev"], 1)                               # the first opening: rr 0 -> 1
        self.assertTrue(L.zone7.entry("z0001")["ro"])

    def test_no_proposal_with_20_low_verdicts(self):
        L, n1, n_open = self.first_night_open()
        e = L.zone7.entry("z0001")
        self.assertEqual(e["rt"], 21)
        e["rt"] = 20
        self.assertEqual(moves.plan(L, [])[2], [])
        e["rt"] = 21
        self.assertEqual(len(moves.plan(L, [])[2]), 1)

    def test_the_revision_bumps_each_time_the_item_opens_after_a_close_and_is_never_reset(self):
        L, n1, n_open = self.first_night_open()
        e = L.zone7.entry("z0001")
        self.assertEqual((e["rr"], e["ro"]), (1, True))
        self.assertEqual(moves.plan(L, [])[2][0]["rev"], 1)          # a read: the same revision
        e["rt"] = 3                                                  # the proposal is withdrawn ...
        self.assertEqual(moves.plan(L, [])[2], [])
        self.assertEqual((e["rr"], e["ro"]), (1, False))             # ... the item closes, the counter stays
        e["rt"] = 25
        self.assertEqual(moves.plan(L, [])[2][0]["rev"], 2)          # ... and reopens with the next revision

    def test_keep_returns_from_the_run_of_day_d_plus_90(self):
        # Keep answered on date D = 50 sets kp = D + 90 and rt = 0 (a fake clock): no item through the run of day 139,
        # one at the run of day 140 if the zone is still unused
        L, _, _ = self.first_night_open()
        e = L.zone7.entry("z0001")
        e.update(kp=D0 + 50 + 90, rt=25)
        for run_day, expect in ((100, 0), (139, 0), (140, 1), (141, 1)):
            set_run(L, run_day)
            self.assertEqual(len(moves.plan(L, [])[2]), expect, run_day)

    def test_the_usual_hours_must_have_been_observed(self):
        # the zone's usual hours (9-11) were radar-off on 15 of the last 21 dates: it is not retired (coverage 29 %)
        L, _ = retire_scene(unused_until=46)
        e = L.zone7.entry("z0001")
        e.update(rt=25, vs="collapsed")
        self.assertEqual(len(moves.plan(L, [])[2]), 1)
        for d in range(25, 40):
            cover(L, d, hours=[h for h in range(24) if h not in (9, 10, 11)])
        self.assertEqual(moves.plan(L, [])[2], [])
        for d in range(25, 29):                                      # observed again on 4 more: 10 of 21 dates, 48 %
            cover(L, d)
        self.assertEqual(moves.plan(L, [])[2], [])
        cover(L, 29)                                                 # 11 of 21 dates: 52 % observed, retired
        self.assertEqual(len(moves.plan(L, [])[2]), 1)

    def test_the_hours_are_judged_on_the_covered_dates_present(self):
        # a retire window with fewer than 21 covered dates in the table is not retired yet
        L = make()
        add_zone(L, Z)
        use_days(L, range(14), Z)
        for k in range(14, 46):
            cover(L, k - 1)
            night(L, k)
        e = L.zone7.entry("z0001")
        self.assertGreaterEqual(e["rt"], 21)
        self.assertEqual(len(moves.plan(L, [])[2]), 1)
        for d in list(L.obs.coverage):
            if d < D0 + 40:
                del L.obs.coverage[d]
        self.assertEqual(moves.plan(L, [])[2], [])

    def test_frozen_bed_label_rc_and_held_zones_are_not_retired(self):
        for case in ("frozen", "bed", "rc", "young", "blind", "none"):
            L, _ = retire_scene()
            e = L.zone7.entry("z0001")
            e["rt"] = 25
            if case == "frozen":
                e["fz"] = True
            elif case == "bed":
                L.review.zones[0]["kind"] = "bed"
            elif case == "rc":
                e["rc"] = True
            else:
                e["vs"] = case
            self.assertEqual(moves.plan(L, [])[2], [], case)
        L = make()
        add_label(L, Z, name="Plant", kind="manual")
        use_days(L, range(14), Z)
        for k in range(14, 46):
            cover(L, k - 1)
            night(L, k)
        L.zone7.entry("l0001")["rt"] = 30
        self.assertEqual(moves.plan(L, [])[2], [])                    # a label never gets an item

    def test_the_radar_hold_withholds_the_retire(self):
        from radar_zone_learner.core import radarmoved
        L, _ = retire_scene()
        L.zone7.entry("z0001")["rt"] = 25
        self.assertEqual(len(moves.plan(L, [])[2]), 1)
        real = radarmoved.hold
        radarmoved.hold = lambda L_: True
        try:
            self.assertEqual(moves.plan(L, []), ([], [], [], set()))
        finally:
            radarmoved.hold = real

    def test_a_pending_modify_or_merge_blocks_the_retire(self):
        from .p7_harness import add_suggestion
        L, _ = retire_scene(unused_until=46)
        L.zone7.entry("z0001")["rt"] = 25
        s = add_suggestion(L, (3.1, 4.0), sid="s0030-1", first=40)         # a candidate 1.1 m away, same hours
        use_days(L, range(34, 46), (3.1, 4.0))
        merges, mods, rets, consumed = moves.plan(L, [s])
        self.assertEqual(len(mods), 1)
        self.assertEqual(rets, [])

    def test_a_rarely_used_seat_does_not_reach_21(self):
        # used 2 days in 7: falling about a third of the nights, but the run resets on the days it is used
        L = make()
        add_zone(L, Z)
        for d in range(60):
            cover(L, d)
            if d % 7 in (1, 4):
                from .p7_harness import add_stay
                for h in (9, 10, 11):
                    add_stay(L, d, h, 3000, *Z)
        for k in range(14, 60):
            night(L, k)
        self.assertLess(L.zone7.entry("z0001")["rt"], 21)
        self.assertEqual(moves.plan(L, [])[2], [])

    def test_disabled_learner_plans_nothing(self):
        L = make(DEFAULT_P7)
        self.assertEqual(moves.plan(L, []), ([], [], [], set()))


if __name__ == "__main__":
    unittest.main()
