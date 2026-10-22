import dataclasses
import unittest
from datetime import date

from radar_zone_learner.core import Learner
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import FixedOffsetTime, NightlyDone, NodeStatus

BASE_MS = 1767571200000          # Monday 2026-01-05 00:00 UTC
BASE_S = BASE_MS // 1000
DAY = 86400
DESK = (2.55, 2.2)
MIRROR = (3.45, 2.2)


def make(params=P):
    L = Learner(params, FixedOffsetTime(0))
    L.handle(NodeStatus(True, BASE_MS))             # creates the learner on day 0 at 00:00
    return L


def iso(d):
    return date.fromordinal(date(2026, 1, 5).toordinal() + d).isoformat()


def cover(L, d, seconds=DAY):
    a = BASE_MS + d * DAY * 1000
    L.obs.add_coverage(a, a + seconds * 1000)


def stay(L, d, h, dur, x, y, ld=1.0, arr="walk", exit_="moving"):
    """A committed stay, recorded exactly as Behaviours.commit records one."""
    t0 = BASE_S + d * DAY + int(h * 3600)
    L.bh.stays.append({"t0": t0 // 300 * 300, "t1": (t0 + dur) // 300 * 300, "dur": dur, "x": round(x * 100),
                       "y": round(y * 100), "sd": 5, "nf": 1, "ld": ld, "he": 0.0, "exit": exit_, "arr": arr,
                       "day": iso(d)})
    L.grids.splat("dwell_slow", x, y, dur / 60.0)
    L.grids.splat("dwell_fast", x, y, dur / 60.0)
    L.grids.splat("sessions", x, y, 1.0)


def desk_day(L, d, pos=DESK, hours=(9, 11, 14), dur=2400, ld=1.0, covered=True):
    if covered:
        cover(L, d)
    for k, h in enumerate(hours):
        stay(L, d, h, dur, pos[0] + 0.03 * ((d + k) % 3 - 1), pos[1] + 0.02 * ((d * 2 + k) % 3 - 1), ld=ld)


def night(L, k, minute=210):
    outs = L.tick(BASE_MS + k * DAY * 1000 + minute * 60000)
    return [o for o in outs if isinstance(o, NightlyDone)]


def places(L):
    return [s for s in L.suggestions() if s.type in ("dwell_zone", "short_task_zone")]


def cand_at(L, pos, tol=0.4):
    out = [c for c in L.candidates() if abs(c.cx_m - pos[0]) < tol and abs(c.cy_m - pos[1]) < tol]
    return out[0] if out else None


class ExtractionTest(unittest.TestCase):
    def test_dwell_on_third_stable_night_and_numbering(self):
        L = make()
        for d in range(7):
            desk_day(L, d)
        nd = night(L, 7)                                  # the first run: night 7 (catch-up)
        self.assertEqual([(n.night, n.date) for n in nd], [(7, "2026-01-12")])
        c = cand_at(L, DESK)
        self.assertEqual((c.streak, c.outcome, c.n, c.core_days, c.cls), (1, "failed", 21, 7, "dwell"))
        self.assertFalse(c.gates["g5_stable"])
        self.assertEqual(places(L), [])
        desk_day(L, 7)
        night(L, 8)
        self.assertEqual(cand_at(L, DESK).streak, 2)
        self.assertEqual(places(L), [])
        desk_day(L, 8)
        nd = night(L, 9)
        self.assertEqual(nd[0].new_suggestions, 1)
        c = cand_at(L, DESK)
        self.assertEqual((c.streak, c.outcome), (3, "suggested"))
        self.assertTrue(all(c.gates.values()))
        s = places(L)[0]
        self.assertEqual((s.id, s.type, s.kind, s.state, s.first_night, s.rev), ("s0009-1", "dwell_zone", "dwell",
                                                                               "shadow", 9, 1))
        self.assertAlmostEqual(s.cx_m, DESK[0], delta=0.05)
        self.assertAlmostEqual(s.conf, c.conf, places=3)
        self.assertTrue(3 <= len(s.poly_cm) <= 12)
        self.assertEqual(s.evidence["n"], 27)
        self.assertEqual(len(s.evidence["hours_wd"]), 24)
        self.assertEqual(s.evidence["hours_at_suggestion"][9], s.evidence["hours_wd"][9] + s.evidence["hours_we"][9])
        self.assertIn(c.cid, [x.cid for x in L.candidates()])
        self.assertEqual(c.cid, "c0007-1")                 # the cid is inherited while stable
        self.assertEqual(len(nd), 1)
        self.assertEqual(night(L, 9, minute=600), [])      # one run per date

    def test_short_task(self):
        L = make()
        for d in range(9):
            cover(L, d)
            for h in (7.2, 12.5, 18.0, 21.5):
                stay(L, d, h, 150 + 10 * (d % 3), 1.9, 5.15)
        for k in (7, 8, 9):
            night(L, k)
        s = places(L)
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0].type, s[0].kind), ("short_task_zone", "short_task"))
        self.assertEqual(s[0].evidence["cls"], "short")
        self.assertLess(s[0].evidence["median_s"], 300)

    def test_gate_failing_alone(self):
        # g1 (dwell): 5 long stays on 5 days
        L = make()
        for d in range(5):
            desk_day(L, d, hours=(9,), dur=7200)
        for k in (5, 6, 7):
            night(L, k)
        g = cand_at(L, DESK).gates
        self.assertFalse(g["g1_class"])
        self.assertTrue(g["g2_core_days"] and g["g3_ld_ghost"] and g["g4_covis"] and g["g5_stable"])
        # g2: many stays on 3 days only
        L = make()
        for d in range(3):
            desk_day(L, d, hours=(8, 9, 10, 11, 13, 14, 15))
        for k in (3, 4, 5):
            night(L, k)
        g = cand_at(L, DESK).gates
        self.assertFalse(g["g2_core_days"])
        self.assertTrue(g["g1_class"] and g["g3_ld_ghost"] and g["g5_stable"])
        self.assertEqual(places(L), [])
        # g3: LD2412 share below 0.5
        L = make()
        for d in range(7):
            desk_day(L, d, ld=0.45)
        for k in (7, 8, 9):
            night(L, k)
        g = cand_at(L, DESK).gates
        self.assertFalse(g["g3_ld_ghost"])
        self.assertTrue(g["g1_class"] and g["g2_core_days"] and g["g5_stable"])
        # g3: ghost share >= 0.2
        L = make()
        for d in range(7):
            desk_day(L, d)
            L.grids.splat("ghost", DESK[0], DESK[1], 60.0)
        for k in (7, 8, 9):
            night(L, k)
        c = cand_at(L, DESK)
        self.assertGreaterEqual(c.ghost_share, 0.2)
        self.assertFalse(c.gates["g3_ld_ghost"])
        self.assertEqual(places(L), [])
        # confidence alone: n 6, 4 core days, 3 stable nights -> conf 0.30
        L = make()
        for d, hours in ((0, (9, 14)), (1, (9, 14)), (2, (9,)), (3, (9,))):
            desk_day(L, d, hours=hours)
        for k in (4, 5, 6):
            night(L, k)
        c = cand_at(L, DESK)
        self.assertEqual((c.n, c.core_days, c.streak), (6, 4, 3))
        self.assertFalse(c.gates["conf"])
        self.assertTrue(all(v for k, v in c.gates.items() if k != "conf"))
        self.assertEqual(c.outcome, "failed")

    def test_clutter_ratio_rule(self):
        for rule, found in (("ratio", True), ("abs", False)):
            L = make(dataclasses.replace(P, clutter_rule=rule))
            for d in range(7):
                desk_day(L, d)
                L.grids.splat("clutter", DESK[0], DESK[1], 30.0)    # well used, some clutter
            for k in (7, 8, 9):
                night(L, k)
            self.assertEqual(len(places(L)), 1 if found else 0, rule)

    def test_envelope_not_a_hard_filter(self):
        for hard, found in ((False, True), (True, False)):
            L = make(dataclasses.replace(P, envelope_hard_filter=hard))
            for d in range(7):
                desk_day(L, d)
                for k in range(50):
                    L.grids.add_walk(-5.0 + 0.02 * k, 6.5)
            for k in (7, 8, 9):
                night(L, k)
            self.assertEqual(len(places(L)), 1 if found else 0, hard)
            if found:
                self.assertEqual(places(L)[0].evidence["in_envelope"], 0.0)

    def test_reflection(self):
        L = make()
        tid = 1
        for d in range(7):
            desk_day(L, d)
            for k, h in enumerate((9, 11, 14)):
                t0 = BASE_S + d * DAY + h * 3600
                stay(L, d, h + 0.1, 1800, MIRROR[0], MIRROR[1])

                class Fr:
                    pass
                a, b = Fr(), Fr()
                a.t0, a.t1, a.cx, a.cy, a.track_id = t0, t0 + 2400, DESK[0], DESK[1], tid
                b.t0, b.t1, b.cx, b.cy, b.track_id = t0 + 360, t0 + 2160, MIRROR[0], MIRROR[1], tid + 1
                tid += 2
                L.covis.add(a)
                L.covis.add(b)
        for k in (7, 8, 9):
            night(L, k)
        m = cand_at(L, MIRROR, 0.3)
        self.assertGreaterEqual(m.covis, 0.8)
        self.assertEqual(m.outcome, "reflection")
        self.assertFalse(m.gates["g4_covis"])
        d = cand_at(L, DESK, 0.3)
        self.assertEqual(d.outcome, "suggested")
        self.assertEqual(d.covis, 0.0)                     # the source is the denser one
        ps = places(L)
        self.assertEqual(len(ps), 1)
        refl = [s for s in L.suggestions() if s.type == "reflection"]
        self.assertEqual(len(refl), 1)
        self.assertEqual(refl[0].evidence["covis_src"], ps[0].id)

    def test_reflection_seated_spread(self):
        """Mirror of a desk 0.45 m from the wall: the seat varies by about 0.08 m between sessions
        (SIM:220), so desk fragments fall on both sides of a 0.4 m column edge. The mirror's own
        0.4 m cells must not take in the desk's fragments (integration fix of the spec 7.7 query)."""
        L = make()
        tid = 1
        offs = (-0.10, -0.04, 0.02, 0.08, 0.14, 0.05, -0.07)
        for d in range(7):
            cover(L, d)
            for k, h in enumerate((9, 11, 14, 16)):
                o = offs[(d * 4 + k) % len(offs)]
                dx, dy = DESK[0] + o, DESK[1] + 0.5 * offs[(d * 3 + k) % len(offs)]
                mx, my = 6.0 - dx + 0.3 * offs[(d + 2 * k) % len(offs)], dy
                t0 = BASE_S + d * DAY + h * 3600
                stay(L, d, h, 2400, dx, dy)
                stay(L, d, h + 0.2, 600, mx, my)

                class Fr:
                    pass
                a, b = Fr(), Fr()
                a.t0, a.t1, a.cx, a.cy, a.track_id = t0, t0 + 2400, dx, dy, tid
                b.t0, b.t1, b.cx, b.cy, b.track_id = t0 + 720, t0 + 1320, mx, my, tid + 1
                tid += 2
                L.covis.add(a)
                L.covis.add(b)
        for k in (7, 8, 9):
            night(L, k)
        m = cand_at(L, MIRROR, 0.3)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(m.covis, 0.8)
        self.assertEqual(m.outcome, "reflection")
        d = cand_at(L, DESK, 0.3)
        self.assertEqual(d.outcome, "suggested")
        self.assertEqual(len(places(L)), 1)

    def test_dedupe_rev_and_stale(self):
        L = make()
        for d in range(7):
            desk_day(L, d)
        for k in (7, 8, 9):
            night(L, k)
        s0 = places(L)[0]
        desk_day(L, 9)
        night(L, 10)
        self.assertEqual([s.id for s in places(L)], [s0.id])          # updated, not duplicated
        s1 = places(L)[0]
        self.assertEqual((s1.rev, s1.last_night, s1.first_night), (1, 10, 9))
        # the stored cells drift away (IoU < 0.6) while the centroid still matches: cells replaced, rev + 1
        rec = L.ns.suggestions[0]
        rec["cells"] = frozenset(c + 1 for c in rec["cells"]) | frozenset([0])
        desk_day(L, 10)
        night(L, 11)
        s2 = places(L)[0]
        self.assertEqual((s2.id, s2.rev), (s0.id, 2))
        self.assertEqual(s2.cells, s0.cells)
        # a suggestion that is not matched goes stale after 7 nights, and is never deleted
        rec2 = dict(rec, id="s0005-9", cells=frozenset([100]), cx=-6.5, cy=0.3, last_night=5)
        L.ns.suggestions.append(rec2)
        desk_day(L, 11)
        night(L, 12)
        st = {s.id: s.state for s in L.suggestions()}
        self.assertEqual(st["s0005-9"], "stale")
        self.assertEqual(st[s0.id], "shadow")

    def test_decay_hours_are_capped_by_calendar_time(self):
        L = make()
        L.obs.obs_s_since_extraction = 1111 * 3600.0            # a corrupt counter must not decay every grid away
        self.assertEqual(len(night(L, 2)), 1)
        self.assertLessEqual(L.grids.eff_s, 24.0 * 3 + 1e-6)

    def test_suggestion_list_is_capped_stale_first(self):
        import radar_zone_learner.core.extraction as EX
        L = make()
        for i in range(1, 6):
            s = EX._new_suggestion("s%04d-1" % i, "dwell_zone", i, frozenset([100 + i]), -6.0 + i * 0.4, 0.3)
            s["last_night"] = i
            L.ns.suggestions.append(s)
        old = EX.MAX_SUGGESTIONS
        EX.MAX_SUGGESTIONS = 3
        try:
            night(L, 20)
        finally:
            EX.MAX_SUGGESTIONS = old
        self.assertEqual([s.last_night for s in L.suggestions()], [3, 4, 5])
        self.assertTrue(all(s.state == "stale" for s in L.suggestions()))
        night(L, 21)
        self.assertEqual(len(L.suggestions()), 3)               # below the cap nothing is deleted (D19)

    def test_doors(self):
        L = make()
        door = (-1.5, 5.4)
        for d in range(4):
            cover(L, d)
            for k in range(8):
                t = BASE_S + d * DAY + 8 * 3600 + 600 * k
                L.grids.splat("birth_moving", door[0], door[1], 1.0)
                L.grids.splat("death_moving", door[0], door[1], 1.0)
                L.bh.door_events.append({"t": t // 300 * 300, "x": -150, "y": 540, "k": "b"})
                L.bh.door_events.append({"t": t // 300 * 300, "x": -150, "y": 540, "k": "d"})
            night(L, d + 1)
        doors = [s for s in L.suggestions() if s.type == "entry_zone"]
        self.assertEqual(len(doors), 1)
        self.assertEqual((doors[0].first_night, doors[0].kind), (3, "entry"))
        self.assertGreaterEqual(doors[0].evidence["passages"], 10)
        self.assertEqual(L.debug_map().doors, ((doors[0].cx_m, doors[0].cy_m),))

    def test_outline(self):
        L = make()
        for d in range(4):
            cover(L, d)
            for k in range(300):
                x = -2.5 + 5.0 * (k % 100) / 100.0
                y = 1.0 + 1.5 * (k // 100)
                L.grids.add_walk(x, y)
            L.bh.walk_points_total += 300
            night(L, d + 1)
        out = [s for s in L.suggestions() if s.type == "room_outline"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].first_night, 3)
        self.assertTrue(L.status().attributes["outline"])
        self.assertGreater(len(L.debug_map().envelope), 100)

    def test_ignore_zone(self):
        L = make()
        for d in range(4):
            cover(L, d)
            t = BASE_S + d * DAY + 12 * 3600
            L.bh.ghost_events.append({"t": t, "x": 200, "y": 80, "dur": 3600, "why": "identical", "ld": 0.0,
                                      "arr": "static"})
            L.grids.splat("ghost", 2.0, 0.8, 60.0)
            night(L, d + 1)
        ig = [s for s in L.suggestions() if s.type == "ignore_zone"]
        self.assertEqual(len(ig), 1)
        self.assertEqual((ig[0].first_night, ig[0].kind), (3, "ignore"))
        self.assertEqual(places(L), [])

    def test_uncovered_dates_skipped(self):
        L = make()
        for d in range(6):
            desk_day(L, d, covered=(d != 2))
        cover(L, 2, 1800)                                   # 30 min observed: not a covered date
        night(L, 6)
        c = cand_at(L, DESK)
        self.assertEqual((c.core_days, c.n), (5, 15))

    def test_retention_28_days(self):
        L = make()
        for d in range(3):
            desk_day(L, d)
        night(L, 3)
        self.assertEqual(len(L.bh.stays), 9)
        cover(L, 40)
        night(L, 41)                                        # D14 = days 0-2 and 40, but 28 days at most
        self.assertEqual(L.bh.stays, [])
        self.assertEqual(L.status().attributes["nights"], 41)

    def test_catch_up_after_missed_run(self):
        L = make()
        desk_day(L, 0)
        self.assertEqual(night(L, 1, minute=209), [])       # 03:29: not yet
        nd = night(L, 1, minute=300)                        # first event after 03:30 runs it
        self.assertEqual([n.night for n in nd], [1])
        self.assertEqual(night(L, 1, minute=400), [])
        self.assertEqual([n.night for n in night(L, 4, minute=900)], [4])   # days 2-3 missed: one run


if __name__ == "__main__":
    unittest.main()
