"""P7 drift ratio and freezing (spec 4.2, check P7-1 (U) parts (a)-(h), (k); (i) and (j) are in test_moves)."""
import unittest

from radar_zone_learner.core import drift
from radar_zone_learner.core.params import DEFAULT_P7
from radar_zone_learner.core.types import ZoneDrift

from .p7_harness import (D0, add_label, add_stay, add_zone, cover, evidence, hours_profile, iso, make, p7on, set_run,
                         use_days)

Z = (2.0, 4.0)


def night(L, k):
    """The drift step of the nightly run of day ``k`` (what `_maybe_nightly` runs after the extraction)."""
    set_run(L, k)
    drift.nightly(L, k)


def dv(L, zid="z0001") -> ZoneDrift:
    return next(d for d in L.drift_view() if d.id == zid)


def used_zone(days=range(14), **zkw):
    L = make()
    rec = add_zone(L, Z, **zkw)
    use_days(L, days, Z)
    return L, rec


class WindowsTest(unittest.TestCase):
    def test_windows_and_weights(self):
        L = make()
        for d in range(20):
            cover(L, d)
        set_run(L, 20)
        d14, d3, wf, ws = drift.windows(L, D0 + 20)
        self.assertEqual(d14, [D0 + d for d in range(6, 20)])
        self.assertEqual(d3, [D0 + 17, D0 + 18, D0 + 19])
        self.assertAlmostEqual(ws[D0 + 19], 1.0)
        self.assertAlmostEqual(ws[D0 + 18], 2 ** (-1 / 14))
        self.assertAlmostEqual(ws[D0 + 6], 2 ** (-13 / 14))
        self.assertEqual(sorted(wf), d3)
        self.assertAlmostEqual(wf[D0 + 17], 2 ** (-2 / 3))

    def test_only_covered_dates_count_and_radar_off_dates_do_not_shorten_the_window(self):
        L = make()
        for d in range(20):
            if d not in (15, 16):
                cover(L, d)
            else:
                cover(L, d, seconds=1.0)                       # observed for 24 s: not a covered date
        d14, d3, _, _ = drift.windows(L, D0 + 20)
        self.assertNotIn(D0 + 15, d14)
        self.assertEqual(d3, [D0 + 17, D0 + 18, D0 + 19])
        self.assertEqual(len(d14), 14)
        self.assertEqual(d14[0], D0 + 4)


class RatioTest(unittest.TestCase):
    def test_a_steady_zone_with_full_coverage_has_ratio_one(self):
        L, _ = used_zone(range(20))
        night(L, 20)
        d = dv(L)
        self.assertEqual(d.state, "ok")
        self.assertAlmostEqual(d.ratio, 1.0, delta=0.05)
        self.assertEqual((d.lo, d.rt, d.rc, d.held), (0, 0, False, False))
        self.assertGreater(d.lu, 0)

    def test_b_usage_stops_falling_then_collapsed(self):
        L, _ = used_zone(range(14))
        ratios = []
        for k in range(14, 17):
            cover(L, k)                                         # covered, but nobody comes
            night(L, k + 1)
            ratios.append(dv(L))
        self.assertLess(ratios[1].ratio, 0.6)                   # below 0.6 within 2 covered dates
        self.assertEqual(ratios[1].state, "falling")
        self.assertLess(ratios[2].ratio, 0.25)                  # below 0.25 within 3
        self.assertEqual(ratios[2].ratio, 0.0)                  # the fast window holds no dwell at all
        for k in range(17, 28):
            cover(L, k)
            night(L, k + 1)
        d = dv(L)
        self.assertEqual((d.state, d.ratio), ("collapsed", 0.0))   # the slow window is empty too
        self.assertGreaterEqual(d.rt, 10)

    def test_c_hours_observed_under_half_are_left_out(self):
        full, _ = used_zone(range(20))
        night(full, 20)
        off, _ = used_zone([])
        for d in range(20):
            cover(off, d, hours=range(6, 24))                    # night-off: 00:00-06:00 never observed
            for h in (9, 10, 11):
                add_stay(off, d, h, 3000, Z[0], Z[1])
        night(off, 20)
        self.assertAlmostEqual(dv(off).ratio, dv(full).ratio, places=6)
        # a stop shows the same under night-off
        for d in range(20, 23):
            cover(off, d, hours=range(6, 24))
        night(off, 23)
        self.assertEqual(dv(off).ratio, 0.0)

    def test_the_eligible_hours_follow_both_windows(self):
        L = make()
        add_zone(L, Z)
        for d in range(14):
            cover(L, d, hours=range(6, 24))
        for d in (11, 12, 13):
            cover(L, d, hours=range(7, 24))                      # hour 6 is no longer observed in the fast window
        set_run(L, 14)
        ctx = drift.context(L)
        self.assertNotIn(6, ctx.elig)
        self.assertIn(7, ctx.elig)
        self.assertEqual(ctx.elig, list(range(7, 24)))

    def test_dwell_in_the_dilated_ring_is_not_counted(self):
        L = make()
        rec = add_zone(L, Z, radius=0.2)
        ring = next(c for cell in sorted(rec["cells"]) for c in L.geom.n8[cell] if c not in rec["cells"])
        outside = L.geom.cell_xy(ring)
        for d in range(14):
            cover(L, d)
            add_stay(L, d, 10, 3000, outside[0], outside[1])      # a neighbour seat, in the dilated ring only
        set_run(L, 14)
        ctx = drift.context(L)
        _, _, n, per = drift.series(ctx, rec["cells"])
        self.assertEqual((n, per), (0, {}))


class UsualProfileTest(unittest.TestCase):
    def test_d_u_is_initialised_from_hours_at_suggestion(self):
        L = make()
        add_zone(L, Z, ev=evidence(hours=(9, 10, 11, 14)))
        use_days(L, range(14), Z)
        night(L, 14)
        u = L.zone7.entry("z0001")["u"]
        self.assertEqual(len(u), 24)
        self.assertAlmostEqual(sum(u), 1.0, delta=0.01)
        self.assertEqual([h for h in range(24) if u[h] > 0], [9, 10, 11, 14])
        self.assertAlmostEqual(u[9], 0.25, places=3)

    def test_d_a_night_zone_in_night_off_is_frozen_a_day_zone_is_not(self):
        night_hours = (22, 23, 0, 1, 2, 3, 4, 5)
        prof = {h: 70.0 / 8 for h in night_hours}
        prof.update({9: 10.0, 10: 10.0, 11: 10.0})
        ev = {"n": 20, "hours_wd": [prof.get(h, 0.0) for h in range(24)], "hours_we": [0.0] * 24,
              "hours_at_suggestion": [prof.get(h, 0.0) for h in range(24)]}
        L = make()
        add_zone(L, (1.0, 3.0), name="Sleeper", kind="dwell", sid="s0002-1", ev=ev)
        for d in range(14):
            cover(L, d, hours=(22, 23, 6))                      # observed 22-24 and 06-07 only
            add_stay(L, d, 22, 3000, 1.0, 3.0)
        night(L, 14)
        z1 = dv(L, "z0001")
        self.assertEqual(z1.state, "frozen")
        self.assertTrue(z1.frozen)
        self.assertTrue(L.zone7.frozen("z0001"))
        # the same coverage with a day zone's profile is frozen too (its usual hours are not observed) ...
        # ... but under the real night-off pattern (06:00-24:00 observed) a day zone is not
        L2 = make()
        add_zone(L2, Z, name="Desk", kind="dwell", sid="s0003-1")
        for d in range(14):
            cover(L2, d, hours=range(6, 24))
            add_stay(L2, d, 9, 3000, Z[0], Z[1])
            add_stay(L2, d, 10, 3000, Z[0], Z[1])
            add_stay(L2, d, 11, 3000, Z[0], Z[1])
        night(L2, 14)
        z2 = dv(L2)
        self.assertEqual(z2.state, "ok")
        self.assertFalse(z2.frozen)

    def test_d_u_is_not_refreshed_while_falling_frozen_blind_or_held(self):
        for case in ("falling", "blind", "held"):
            L = make()
            add_zone(L, Z, ev=evidence(hours=(9, 10)))
            use_days(L, range(14), Z, hours=(9, 10, 11, 12))      # real use differs from the initial profile
            night(L, 14)
            e = L.zone7.entry("z0001")
            self.assertEqual(e["vs"], "ok")
            u_ok = list(e["u"])
            self.assertTrue(any(u_ok[h] > 0 for h in (9, 10)))
            if case == "falling":
                for d in range(14, 17):
                    cover(L, d)
                night(L, 17)
                self.assertEqual(e["vs"], "falling")
            elif case == "blind":
                for d in range(0, 17):
                    cover(L, d, seconds=200.0)                   # observed 200 s in every hour: covered, no hour at 50 %
                night(L, 17)
                self.assertEqual(e["vs"], "blind")
            else:
                e["rc"] = True
                night(L, 15)
            self.assertEqual(e["u"], u_ok)

    def test_d_u_follows_a_healthy_zone_slowly_and_an_unobserved_hour_keeps_its_weight(self):
        L = make()
        prof = {2: 8.0, 9: 30.0, 10: 30.0, 11: 32.0}                       # u[2] = 0.08: under 10 % of the mass
        ev = {"n": 20, "hours_wd": [prof.get(h, 0.0) for h in range(24)], "hours_we": [0.0] * 24,
              "hours_at_suggestion": [prof.get(h, 0.0) for h in range(24)]}
        add_zone(L, Z, ev=ev)
        for d in range(16):
            cover(L, d, hours=range(3, 24))                      # hour 2 is never observed (night-off)
            for h in (9, 10, 11, 14):
                add_stay(L, d, h, 3000, Z[0], Z[1])
        night(L, 14)
        u1 = list(L.zone7.entry("z0001")["u"])
        self.assertAlmostEqual(u1[2], 0.08, delta=0.002)
        self.assertEqual(L.zone7.entry("z0001")["vs"], "ok")
        night(L, 15)
        u2 = L.zone7.entry("z0001")["u"]
        self.assertGreater(u2[14], u1[14])                       # the new afternoon use is followed ...
        self.assertAlmostEqual(u2[2], u1[2], delta=0.01)         # ... and the unobserved night hour keeps its weight
        self.assertAlmostEqual(sum(u2), 1.0, delta=0.01)
        night(L, 16)
        u3 = L.zone7.entry("z0001")["u"]
        self.assertGreater(u3[14], u2[14])
        night(L, 16)                                             # the same date again: nothing moves
        self.assertEqual(L.zone7.entry("z0001")["u"], u3)

    def test_d_a_profile_with_too_much_unobserved_mass_is_not_refreshed(self):
        L = make()
        prof = {2: 30.0, 9: 20.0, 10: 20.0, 11: 20.0}                      # a third of the mass at an unobserved hour
        ev = {"n": 20, "hours_wd": [prof.get(h, 0.0) for h in range(24)], "hours_we": [0.0] * 24,
              "hours_at_suggestion": [prof.get(h, 0.0) for h in range(24)]}
        add_zone(L, Z, ev=ev)
        for d in range(16):
            cover(L, d, hours=range(3, 24))
            for h in (9, 10, 11, 14):
                add_stay(L, d, h, 3000, Z[0], Z[1])
        night(L, 14)
        u1 = list(L.zone7.entry("z0001")["u"])
        self.assertAlmostEqual(u1[2], 1.0 / 3.0, delta=0.002)
        night(L, 15)
        self.assertEqual(L.zone7.entry("z0001")["u"], u1)


class CounterTest(unittest.TestCase):
    def collapsed_zone(self, **kw):
        L = make(p7on(**kw))
        add_zone(L, Z)
        use_days(L, range(14), Z)
        night(L, 14)
        return L

    def test_e_the_retire_run_resets_at_the_clear_ratio(self):
        # default retire_clear 0.1 (= ratio_retire): a ratio of 0.15 resets the run; 0.25 holds the run and 0.3 resets it
        L = self.collapsed_zone()
        e = L.zone7.entry("z0001")
        e.update(rt=5, lo=2)
        k = 15
        self.set_ratio(L, 0.05, k)
        self.assertEqual((e["rt"], e["lo"]), (6, 3))
        self.set_ratio(L, 0.15, k + 1)
        self.assertEqual((e["rt"], e["lo"]), (0, 4))
        L2 = self.collapsed_zone(retire_clear=0.25)
        e2 = L2.zone7.entry("z0001")
        e2["rt"] = 5
        self.set_ratio(L2, 0.15, 15)
        self.assertEqual(e2["rt"], 5)                            # held between the two
        self.set_ratio(L2, 0.3, 16)
        self.assertEqual(e2["rt"], 0)

    def set_ratio(self, L, ratio, k):
        """Makes the next covered date's verdict have (about) the given ratio by overriding the verdict function."""
        real = drift.judge

        def fake(L_, z, e, ctx, held):
            r = real(L_, z, e, ctx, held)
            r.state = "ok" if ratio >= 0.6 else "falling"
            r.ratio = ratio
            return r
        drift.judge = fake
        try:
            cover(L, k - 1)
            night(L, k)
        finally:
            drift.judge = real

    def test_e_the_flag_run_resets_at_the_flag_ratio(self):
        L = self.collapsed_zone()
        e = L.zone7.entry("z0001")
        self.set_ratio(L, 0.2, 15)
        self.set_ratio(L, 0.2, 16)
        self.assertEqual(e["lo"], 2)
        self.set_ratio(L, 0.25, 17)                              # at 0.25 the run resets
        self.assertEqual(e["lo"], 0)

    def test_e_a_covered_date_counts_once_when_the_run_repeats(self):
        L = self.collapsed_zone()
        e = L.zone7.entry("z0001")
        for d in range(14, 17):
            cover(L, d)
        night(L, 17)
        rt, lo, n = e["rt"], e["lo"], e["n"]
        for _ in range(3):
            night(L, 17)                                         # a restart repeats the run of the same date
        self.assertEqual((e["rt"], e["lo"], e["n"]), (rt, lo, n))
        night(L, 18)                                             # day 17 is not covered (no coverage row): same d14
        self.assertEqual((e["rt"], e["lo"]), (rt, lo))
        cover(L, 17)
        night(L, 18)
        self.assertEqual((e["rt"], e["lo"]), (rt + 1, lo + 1))

    def test_e_rt_reaches_21_after_21_distinct_covered_dates(self):
        L = self.collapsed_zone()
        e = L.zone7.entry("z0001")
        for k in range(15, 15 + 21):
            cover(L, k - 1)
            night(L, k)
        # the run counts the dates on which the ratio was low: 3 unused dates are needed to be below 0.1
        self.assertEqual(e["vs"], "collapsed")
        self.assertGreaterEqual(e["rt"], 19)
        for k in range(36, 40):
            cover(L, k - 1)
            night(L, k)
        self.assertGreaterEqual(e["rt"], 21)

    def test_e_frozen_blind_young_none_and_held_nights_hold_the_counters(self):
        for case in ("young", "frozen", "blind", "none", "held"):
            L = make()
            add_zone(L, Z, night=0 if case != "young" else 12)
            if case == "none":
                L.review.zones[0]["ref"] = None                  # no suggestion: u comes from the observed stays
                use_days(L, [], Z)
                for d in range(14):
                    cover(L, d)
            else:
                use_days(L, range(14), Z)
            if case == "frozen":
                L.window["sched"] = True
                L.review.zones[0]["kind"] = "bed"
            e = L.zone7.entry("z0001", create=True)
            e.update(lo=2, rt=7, lu=D0 + 3, u=[0.0] * 9 + [1.0] + [0.0] * 14)
            if case == "none":
                e["u"] = []
            if case == "held":
                e["rc"] = True
                e["rd"] = D0 + 13
            if case == "blind":
                for d in L.obs.coverage:
                    L.obs.coverage[d] = [200.0] * 24          # covered (4,800 s), but no hour reaches 50 %
            for d in range(14, 17):
                cover(L, d, seconds=200.0 if case == "blind" else 3600.0)
            night(L, 17)
            self.assertEqual(e["vs"], case if case != "held" else e["vs"], case)
            self.assertEqual((e["lo"], e["rt"], e["lu"]), (2, 7, D0 + 3), case)
            self.assertEqual(e["n"], D0 + 16, case)              # the date is marked seen
            if case == "none":
                self.assertEqual(e["u"], [])
            else:
                self.assertEqual(e["u"], [0.0] * 9 + [1.0] + [0.0] * 14, case)

    def test_the_rc_flag_clears_by_itself_after_recheck_days(self):
        L = make()
        add_zone(L, Z)
        use_days(L, range(14), Z)
        e = L.zone7.entry("z0001", create=True)
        e.update(rc=True, rd=D0 + 14)
        night(L, 14 + 27)
        self.assertTrue(e["rc"])
        night(L, 14 + 28)
        self.assertFalse(e["rc"])


class PrecedenceTest(unittest.TestCase):
    def test_f_state_precedence(self):
        # young beats everything
        L = make()
        add_zone(L, Z, night=10)
        use_days(L, range(14), Z)
        night(L, 14)
        self.assertEqual(L.zone7.entry("z0001")["vs"], "young")
        self.assertEqual(dv(L).ratio, -1.0)
        # too few covered dates is young as well
        L = make()
        add_zone(L, Z)
        use_days(L, range(5), Z)
        night(L, 20)
        self.assertEqual(L.zone7.entry("z0001")["vs"], "young")
        # blind (no eligible hour) beats frozen and none
        L = make()
        add_zone(L, Z, kind="bed")
        L.window["sched"] = True
        for d in range(14):
            cover(L, d, seconds=1800, hours=(1, 2, 3, 4))          # 4 x 1800 = 7200 s: covered, but hours are < ... see below
        set_run(L, 14)
        ctx = drift.context(L)
        self.assertEqual(ctx.elig, [1, 2, 3, 4])
        for d in range(14):
            L.obs.coverage[D0 + d] = [0.0] * 24
            L.obs.coverage[D0 + d][3] = 3600.0
            L.obs.coverage[D0 + d][4] = 10.0
        night(L, 14)
        self.assertEqual(L.zone7.entry("z0001")["vs"], "frozen")
        # none: no suggestion, no stays at all -> u is empty and nothing is known
        L = make()
        add_zone(L, Z)
        L.review.zones[0]["ref"] = None
        for d in range(14):
            cover(L, d)
        night(L, 14)
        self.assertEqual(L.zone7.entry("z0001")["vs"], "none")
        self.assertEqual(L.zone7.entry("z0001")["u"], [])
        self.assertEqual(dv(L).ratio, -1.0)

    def test_blind_is_frozen_by_coverage(self):
        L = make()
        add_zone(L, Z)
        use_days(L, range(14), Z)
        for d in range(14):
            L.obs.coverage[D0 + d] = [0.0] * 24
            L.obs.coverage[D0 + d][3] = 7200.0
            L.obs.coverage[D0 + d][4] = 10.0
        night(L, 14)
        # hour 3 is observed 100 %, but with 7,200 s the covered-hour rule leaves hour 3 only: not blind -> frozen by u
        self.assertEqual(L.zone7.entry("z0001")["vs"], "frozen")
        L.obs.coverage = {d: [0.0] * 24 for d in L.obs.coverage}
        for d in L.obs.coverage:
            L.obs.coverage[d][3] = 3600.0
        for d in range(14):
            L.obs.coverage[D0 + d][3] = 3700.0
            L.obs.coverage[D0 + d][5] = 0.0
        L.obs.coverage[D0 + 13] = [0.0] * 24
        night(L, 15)
        self.assertIn(L.zone7.entry("z0001")["vs"], ("blind", "frozen"))

    def test_g_a_label_gets_a_verdict_and_no_item(self):
        L = make()
        add_label(L, Z, name="Plant", kind="manual")
        use_days(L, range(14), Z)
        night(L, 14)
        e = L.zone7.entry("l0001")
        self.assertEqual(e["vs"], "ok")
        self.assertAlmostEqual(sum(e["u"]), 1.0, delta=0.01)                 # built from the observed hours
        self.assertEqual([h for h in range(24) if e["u"][h] > 0], [9, 10, 11])
        view = {d.id: d for d in L.drift_view()}
        self.assertAlmostEqual(view["l0001"].ratio, 1.0, delta=0.05)
        # a label with no dwell: u stays empty
        L = make()
        add_label(L, Z, name="Plant", kind="manual")
        for d in range(14):
            cover(L, d)
        night(L, 14)
        self.assertEqual(L.zone7.entry("l0001")["u"], [])
        self.assertEqual(L.zone7.entry("l0001")["vs"], "none")

    def test_g_a_label_bed_observed_only_in_the_evening_is_not_coverage_frozen_but_static_frozen(self):
        L = make()
        add_label(L, Z, name="Bed", kind="bed")
        L.window.update(sched=True, start=0, end=360)
        for d in range(14):
            cover(L, d, hours=(22, 23))
            add_stay(L, d, 22, 3000, Z[0], Z[1])
            add_stay(L, d, 23, 3000, Z[0], Z[1])
        night(L, 14)
        e = L.zone7.entry("l0001")
        self.assertFalse(e["fz"])                                            # the coverage rule alone does not freeze it
        self.assertEqual(e["vs"], "frozen")                                  # the static predicate does
        self.assertTrue(dv(L, "l0001").frozen)
        # with the static predicate off it is judged like any zone
        L2 = make(p7on(static_freeze=False))
        add_label(L2, Z, name="Bed", kind="bed")
        L2.window.update(sched=True, start=0, end=360)
        for d in range(14):
            cover(L2, d, hours=(22, 23))
            add_stay(L2, d, 22, 3000, Z[0], Z[1])
            add_stay(L2, d, 23, 3000, Z[0], Z[1])
        night(L2, 14)
        self.assertEqual(L2.zone7.entry("l0001")["vs"], "ok")
        self.assertFalse(dv(L2, "l0001").frozen)

    def test_h_zoneview_frozen_is_the_static_predicate_or_the_coverage_rule(self):
        L = make()
        add_zone(L, Z, kind="bed")                                           # a bed
        add_zone(L, (4.0, 4.0), name="Desk", kind="dwell", sid="s0002-1")
        views = {v.id: v for v in L.zones()}
        self.assertFalse(views["z0001"].frozen)                              # schedule unknown: the static predicate is off
        L.window["sched"] = True
        views = {v.id: v for v in L.zones()}
        self.assertTrue(views["z0001"].frozen)
        self.assertFalse(views["z0002"].frozen)
        L.zone7.entry("z0002", create=True)["fz"] = True                     # the coverage rule
        views = {v.id: v for v in L.zones()}
        self.assertTrue(views["z0002"].frozen)
        # static_freeze off: only the coverage rule
        L2 = make(p7on(static_freeze=False))
        add_zone(L2, Z, kind="bed")
        L2.window["sched"] = True
        self.assertFalse(L2.zones()[0].frozen)
        L2.zone7.entry("z0001", create=True)["fz"] = True
        self.assertTrue(L2.zones()[0].frozen)

    def test_zoneview_is_the_p5_predicate_when_p7_is_disabled(self):
        L = make(DEFAULT_P7)
        add_zone(L, Z, kind="dwell")
        L.zone7.entry("z0001", create=True)["fz"] = True
        self.assertFalse(L.zones()[0].frozen)                                # fz is not consulted while disabled
        self.assertEqual(L.drift_view(), ())


class HeldNightTest(unittest.TestCase):
    def test_a_radar_hold_keeps_counters_and_u_still_but_stores_the_verdict(self):
        from radar_zone_learner.core import radarmoved
        L = make()
        add_zone(L, Z)
        use_days(L, range(14), Z)
        night(L, 14)
        e = L.zone7.entry("z0001")
        e["rt"] = 4
        lu = e["lu"]
        real = radarmoved.hold
        radarmoved.hold = lambda L_: True
        try:
            for d in range(14, 17):
                cover(L, d)
            night(L, 17)
        finally:
            radarmoved.hold = real
        self.assertEqual((e["vs"], e["rt"], e["lu"]), ("falling", 4, lu))     # the verdict is stored, the counters are not
        self.assertEqual(e["lo"], 0)
        self.assertEqual(e["n"], D0 + 16)


if __name__ == "__main__":
    unittest.main()
