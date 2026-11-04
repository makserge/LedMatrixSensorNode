"""P5 'Guests present - pause learning': a real pause, judged at the time of the evidence (spec 7.3, D34, §11.3
`test_p5_pause`): one test per guard row, the evidence-time cases, the OR with the context, persistence."""
import json
import random
import unittest

from radar_zone_learner.core import Learner
from radar_zone_learner.core.params import DEFAULT_PARAMS as P
from radar_zone_learner.core.types import (ContextState, FixedOffsetTime, MarkResult, SettingSwitch, StatusChanged)

from .test_behaviours import FakeTrack, T0, frag, make
from .test_labels import BED, press, setup
from .test_learner import BASE_MS, Feeder, sit_fn, start, walk_fn


def sw(L, on, t_ms):
    return L.handle(SettingSwitch("guests_pause", on, BASE_MS + t_ms))


class SpansTest(unittest.TestCase):
    def test_paused_in_overlap_rules(self):
        bh, *_ = make()
        self.assertFalse(bh.paused_in(T0, T0 + 100))
        bh.set_paused(True, T0 + 100)
        self.assertEqual(bh.paused_spans, [[T0 + 100, None]])
        self.assertTrue(bh.paused_in(T0 + 100, T0 + 100))            # a point at the start
        self.assertTrue(bh.paused_in(T0 + 50, T0 + 100))             # an interval that ends at the start
        self.assertFalse(bh.paused_in(T0, T0 + 99.9))
        self.assertTrue(bh.paused_in(T0 + 10 ** 6, T0 + 10 ** 6 + 1))   # an open span reaches infinity
        bh.set_paused(False, T0 + 200)
        self.assertEqual(bh.paused_spans, [[T0 + 100, T0 + 200]])
        self.assertTrue(bh.paused_in(T0 + 200, T0 + 300))            # an interval that starts at the end
        self.assertFalse(bh.paused_in(T0 + 200.1, T0 + 300))
        self.assertTrue(bh.paused_in(T0 + 150, T0 + 160))
        self.assertTrue(bh.paused_in(T0, T0 + 10 ** 5))              # an interval that contains the span

    def test_set_paused_is_idempotent(self):
        bh, *_ = make()
        bh.set_paused(False, T0)
        self.assertEqual(bh.paused_spans, [])
        bh.set_paused(True, T0)
        bh.set_paused(True, T0 + 5)
        self.assertEqual(bh.paused_spans, [[T0, None]])
        bh.set_paused(False, T0 + 10)
        bh.set_paused(False, T0 + 20)
        self.assertEqual(bh.paused_spans, [[T0, T0 + 10]])

    def test_a_span_that_ended_more_than_two_days_ago_is_dropped_and_at_most_32_are_kept(self):
        bh, *_ = make()
        for k in range(40):
            bh.set_paused(True, T0 + 1000 * k)
            bh.set_paused(False, T0 + 1000 * k + 10)
        self.assertEqual(len(bh.paused_spans), 32)
        self.assertEqual(bh.paused_spans[0][0], T0 + 8000)            # the newest 32
        bh.set_paused(True, T0 + 3 * 86400)
        self.assertEqual(bh.paused_spans, [[T0 + 3 * 86400, None]])   # all the old ones ended > 2 days ago
        bh.set_paused(False, T0 + 3 * 86400 + 5)
        bh.set_paused(True, T0 + 3 * 86400 + 86400)
        self.assertEqual(len(bh.paused_spans), 2)                     # the one of a day ago stays


class GuardRowsTest(unittest.TestCase):
    """spec 7.3: one test per row of the table, each against a control without the pause."""

    def feed_walk(self, bh, tr, t0, n=12, speed=0.9):
        """A walking track (fast enough to be a moving birth and to make walking points)."""
        for k in range(n):
            bh.on_sample(tr, t0 + k, 1.0 + speed * k * 0.5, 2.0)

    def test_observation_adds_no_coverage_while_paused(self):
        from radar_zone_learner.core.observation import Observation
        obs = Observation(P, FixedOffsetTime(0))
        obs.add_coverage(1767571200000, 1767571200000 + 3600000)
        self.assertEqual(obs.obs_s_since_extraction, 3600.0)
        obs.set_paused(True)
        obs._add_observed(0, 1000)
        self.assertEqual((obs.obs_s_since_extraction, len(obs.coverage)), (3600.0, 1))   # untouched
        obs.set_paused(False)
        self.assertFalse(obs.paused)

    def test_coverage_through_the_frames_path(self):
        res = {}
        for paused in (False, True):
            L = Learner(P, FixedOffsetTime(0))
            start(L)
            if paused:
                sw(L, True, 500)
            f = Feeder(L)
            f.idle_until(60000)
            res[paused] = (round(sum(sum(r) for r in L.obs.coverage.values()), 1), L.obs.obs_s_since_extraction)
        self.assertGreater(res[False][0], 50.0)
        self.assertEqual(res[True], (0.0, 0.0))

    def test_birth_splat_and_door_event(self):
        for paused in (False, True):
            bh, grids, cov, _ = make()
            if paused:
                bh.set_paused(True, T0 - 10)
            self.feed_walk(bh, FakeTrack(1), T0)
            moved = sum(grids["birth_moving"])
            self.assertEqual((moved > 0, [e["k"] for e in bh.door_events if e["k"] == "b"]),
                             (not paused, [] if paused else ["b"]), paused)

    def test_birth_outside_the_pause_is_recorded(self):
        bh, grids, *_ = make()
        bh.set_paused(True, T0 - 100)
        bh.set_paused(False, T0 - 50)                                 # ended before the track was born
        self.feed_walk(bh, FakeTrack(1), T0)
        self.assertGreater(sum(grids["birth_moving"]), 0)

    def test_walking_points_and_the_activity_callback(self):
        seen = {}
        for paused in (False, True):
            bh, grids, cov, _ = make()
            calls = []
            bh.on_walk = calls.append
            if paused:
                bh.set_paused(True, T0 - 10)
            self.feed_walk(bh, FakeTrack(1), T0)
            seen[paused] = (bh.walk_points_total, len(calls), sum(grids["walk"]))
        self.assertGreater(seen[False][0], 5)
        self.assertEqual(seen[True], (0, 0, 0.0))

    def test_a_walking_point_inside_the_span_only(self):
        bh, grids, *_ = make()
        calls = []
        bh.on_walk = calls.append
        bh.set_paused(True, T0 + 4.5)
        bh.set_paused(False, T0 + 7.5)                                # the samples at T0+5, +6, +7 lie inside
        self.feed_walk(bh, FakeTrack(1), T0, n=12)
        self.assertEqual(sorted(calls), [T0 + k for k in (1, 2, 3, 4, 8, 9, 10, 11)])

    def test_death_splat_door_event_and_the_callback(self):
        res = {}
        for paused in (False, True):
            bh, grids, cov, _ = make()
            deaths = []
            bh.on_death = lambda *a: deaths.append(a)
            tr = FakeTrack(1)
            self.feed_walk(bh, tr, T0)
            if paused:
                bh.set_paused(True, T0 + 5)                           # the pause starts before the track's last sample
            bh.finish(tr, censored=False)
            res[paused] = (sum(grids["death_moving"]), [e["k"] for e in bh.door_events if e["k"] == "d"], len(deaths))
            res[paused] += (cov.frags[-1].death_class if cov.frags else "none",)
        self.assertEqual(res[False][1:3], (["d"], 1))
        self.assertGreater(res[False][0], 0)
        self.assertEqual(res[True][:3], (0.0, [], 0))
        self.assertEqual(res[True][3], res[False][3])                  # the fragment still has its death class

    def test_a_still_death_is_guarded_too(self):
        for paused in (False, True):
            bh, grids, *_ = make()
            tr = FakeTrack(1)
            for k in range(20):
                bh.on_sample(tr, T0 + k, 2.0, 3.0)
            if paused:
                bh.set_paused(True, T0)
            bh.finish(tr, censored=False)
            self.assertEqual(sum(grids["death_still"]) > 0, not paused, paused)

    def test_fragments_add_no_covis_and_an_epp_ghost_writes_nothing_while_paused(self):
        for paused in (False, True):
            bh, grids, cov, _ = make()
            if paused:
                bh.set_paused(True, T0 - 10)
            tr = FakeTrack(1)
            for k in range(400):                                      # 400 s at one spot, sd < 1 cm: an EPP ghost
                bh.on_sample(tr, T0 + k, 2.0 + 0.0005 * (k % 2), 3.0)
            bh.finish(tr, censored=False)
            self.assertEqual(len(cov.frags), 0 if paused else 1)         # covis.add is what the pause withholds
            n_pairs = len(cov.recent)
            self.assertEqual((n_pairs, len(bh.ghost_events), sum(grids["ghost"]) > 0),
                             ((0, 0, False) if paused else (1, 1, True)), paused)
            self.assertEqual(bh.counters["epp"], 1)                   # counted either way: a diagnostic only

    def test_fragments_still_reach_the_stay_logic_so_that_stays_can_bridge(self):
        bh, *_ = make()
        bh.set_paused(True, T0 - 10)
        tr = FakeTrack(1)
        for k in range(60):
            bh.on_sample(tr, T0 + k, 2.0 + 0.05 * (k % 3), 3.0)
        self.assertEqual(bh.pending, [])
        bh.finish(tr, censored=False)
        self.assertEqual(len(bh.pending), 1)                           # a stay is pending (bridging works) ...
        self.assertTrue(bh.pending[0].paused)                          # ... but it is tainted

    def test_pending_stay_flag_is_set_at_creation_and_ored_on_bridging(self):
        bh, *_ = make()
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 60))                    # before the pause: evidence older than the pause
        self.assertFalse(bh.pending[0].paused)
        bh.set_paused(True, T0 + 100)                                  # a pause starts while the stay is pending
        self.assertFalse(bh.pending[0].paused)                        # and does not taint it by itself
        bh.on_fragment(frag(1.0, 2.0, T0 + 110, T0 + 170, open_end=True))     # a bridging fragment inside the pause
        self.assertEqual(len(bh.pending), 1)
        self.assertTrue(bh.pending[0].paused)                          # taints the stay
        bh.set_paused(False, T0 + 300)
        bh.on_fragment(frag(1.0, 2.0, T0 + 400, T0 + 460, open_end=True))
        self.assertTrue(bh.pending[0].paused)                          # once tainted, always

    def test_a_new_pending_stay_inside_the_pause_is_paused(self):
        bh, *_ = make()
        bh.set_paused(True, T0)
        bh.on_fragment(frag(1.0, 2.0, T0 + 10, T0 + 70))
        self.assertTrue(bh.pending[0].paused)
        bh2, *_ = make()
        bh2.on_fragment(frag(1.0, 2.0, T0 + 10, T0 + 70))
        self.assertFalse(bh2.pending[0].paused)

    def test_commit_drops_a_paused_stay_without_any_record(self):
        bh, grids, cov, _ = make()
        bh.set_paused(True, T0 - 5)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual((bh.stays, bh.ghost_events, bh.door_events, bh.paused_stays), ([], [], [], 1))
        for name in ("dwell_slow", "dwell_fast", "sessions", "ghost", "clutter"):
            self.assertEqual(sum(grids[name]), 0, name)
        self.assertEqual(sum(grids.days_bits), 0)                      # no days_mark
        self.assertEqual(bh.counters["stays"], 0)

    def test_a_paused_stay_is_dropped_also_when_it_would_have_been_ghost_or_clutter_evidence(self):
        bh, grids, *_ = make(ld=0.0)                                   # the LD2412 is known absent: a 'ld_off' ghost stay
        bh.set_paused(True, T0 - 5)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual((bh.ghost_events, bh.paused_stays, sum(grids["ghost"])), ([], 1, 0))
        bh2, grids2, *_ = make(he=1.0)                                 # the home was empty: a clutter stay
        bh2.set_paused(True, T0 - 5)
        bh2.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual((bh2.ghost_events, sum(grids2["clutter"]), bh2.paused_stays), ([], 0, 1))

    def test_a_short_paused_stay_counts_as_short_not_paused(self):
        bh, *_ = make()
        bh.set_paused(True, T0 - 5)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 5, open_end=False))
        self.assertEqual((bh.paused_stays, bh.counters["short_stays"]), (0, 1))

    def test_flush_censored_goes_through_commit(self):
        bh, *_ = make()
        bh.set_paused(True, T0 - 5)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600))
        bh.flush_censored()
        self.assertEqual((bh.stays, bh.paused_stays), ([], 1))


class EvidenceTimeTest(unittest.TestCase):
    """The pause is judged when the evidence is taken, never when a stay commits (up to 900 s later)."""

    def test_released_before_the_flush_a_stay_formed_in_the_pause_is_still_dropped(self):
        bh, *_ = make()
        bh.set_paused(True, T0 - 5)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600))                   # pending; open end
        bh.set_paused(False, T0 + 700)                                 # the guest left, the switch is released
        bh.flush(T0 + 600 + 901)                                       # 15 minutes after the last fragment
        self.assertEqual((len(bh.stays), bh.paused_stays), (0, 1))

    def test_pending_before_the_pause_is_recorded_whenever_it_commits(self):
        bh, *_ = make()
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600))
        bh.set_paused(True, T0 + 700)                                  # the pause starts while it is pending
        bh.flush(T0 + 600 + 901)                                       # ... and it commits inside the pause
        self.assertEqual((len(bh.stays), bh.paused_stays), (1, 0))

    def test_a_bridging_fragment_in_the_pause_taints(self):
        bh, *_ = make()
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600))
        bh.set_paused(True, T0 + 650)
        bh.on_fragment(frag(1.0, 2.0, T0 + 700, T0 + 760, open_end=False))   # bridges: a guest sat down there
        bh.set_paused(False, T0 + 800)
        bh.flush(T0 + 10 ** 5)
        self.assertEqual((len(bh.stays), bh.paused_stays), (0, 1))

    def test_a_stay_that_ends_after_the_release_but_overlaps_the_pause_is_dropped(self):
        bh, *_ = make()
        bh.set_paused(True, T0 - 100)
        bh.set_paused(False, T0 + 30)                                  # released 30 s into the stay
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual((len(bh.stays), bh.paused_stays), (0, 1))

    def test_a_stay_after_the_release_is_recorded(self):
        bh, *_ = make()
        bh.set_paused(True, T0 - 100)
        bh.set_paused(False, T0 - 1)
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual((len(bh.stays), bh.paused_stays), (1, 0))

    def test_no_pause_means_the_old_behaviour(self):
        bh, *_ = make()
        bh.on_fragment(frag(1.0, 2.0, T0, T0 + 600, open_end=False))
        self.assertEqual((len(bh.stays), bh.paused_stays, bh.paused_spans), (1, 0, []))


class LearnerPauseTest(unittest.TestCase):
    def session(self, pause, release_ms=None):
        """A guest sits 3 minutes at the bed; with `pause` the switch is on for the visit."""
        L, f = setup()
        rng = random.Random(4)
        if pause:
            sw(L, True, f.next_ms)
        f.targets_until(f.next_ms + 180000, sit_fn(rng, BED))
        f.idle_until(f.next_ms + 12000)
        if pause and release_ms is not None:
            sw(L, False, f.next_ms + release_ms)
        return L, f

    def commit(self, L, f):
        L.tick(BASE_MS + f.next_ms + 16 * 60 * 1000)                   # 16 minutes later the pending stay commits
        L.tick(BASE_MS + f.next_ms + 17 * 60 * 1000)

    def test_a_paused_visit_leaves_no_stay_a_control_visit_does(self):
        L, f = self.session(pause=False)
        self.commit(L, f)
        self.assertEqual((len(L.stays()), L.paused_stays()), (1, 0))
        L, f = self.session(pause=True, release_ms=500)                # released right after: judged at the evidence
        self.commit(L, f)
        self.assertEqual((len(L.stays()), L.paused_stays()), (0, 1))

    def test_counters_have_exactly_the_observation_keys(self):
        L, f = self.session(pause=True, release_ms=500)
        self.commit(L, f)
        self.assertEqual(set(L.counters()), set(L.obs.counters))
        self.assertNotIn("paused_stays", L.counters())
        self.assertEqual(L.paused_stays(), 1)

    def test_the_status_stays_learning_and_the_attribute_shows_the_pause(self):
        L, f = setup()
        self.assertFalse(L.status().attributes["learning_paused"])
        outs = sw(L, True, f.next_ms)
        st = [o for o in outs if isinstance(o, StatusChanged)][-1]
        self.assertEqual((st.state, st.attributes["learning_paused"]), ("learning", True))
        self.assertTrue(st.attributes["settings"]["guests_pause"])
        rng = random.Random(2)
        f.targets_until(f.next_ms + 5000, sit_fn(rng, BED))
        self.assertEqual(L.status().state, "learning")                 # tracking goes on
        self.assertTrue(L.status().attributes["radar_valid"])
        self.assertEqual(len(L.live_view()), 1)                        # and so does the live view
        outs = sw(L, False, f.next_ms + 100)
        self.assertFalse([o for o in outs if isinstance(o, StatusChanged)][-1].attributes["learning_paused"])

    def test_mark_spot_here_works_while_paused(self):
        L, f = setup()
        sw(L, True, f.next_ms)
        rng = random.Random(3)
        f.targets_until(f.next_ms + 40000, sit_fn(rng, BED))
        outs = press(L, f)
        self.assertEqual(outs[0], MarkResult(True, None, "l0001", "manual"))

    def test_the_context_pauses_too_and_the_pause_is_the_or_of_both(self):
        L, f = setup()
        L.handle(ContextState("guests", True, BASE_MS + f.next_ms))
        self.assertTrue(L.status().attributes["learning_paused"])
        self.assertTrue(L.bh.paused_spans and L.obs.paused)
        sw(L, True, f.next_ms + 1000)                                  # the switch as well: still one span
        self.assertEqual(len(L.bh.paused_spans), 1)
        L.handle(ContextState("guests", False, BASE_MS + f.next_ms + 2000))
        self.assertTrue(L.status().attributes["learning_paused"])      # the switch still holds it
        self.assertTrue(L.obs.paused)
        sw(L, False, f.next_ms + 3000)
        self.assertFalse(L.status().attributes["learning_paused"])
        self.assertFalse(L.obs.paused)
        self.assertIsNotNone(L.bh.paused_spans[-1][1])
        L.handle(ContextState("guests", True, BASE_MS + f.next_ms + 4000))
        L.handle(ContextState("guests", None, BASE_MS + f.next_ms + 5000))     # cleared: not paused
        self.assertFalse(L.status().attributes["learning_paused"])
        self.assertIsNone(L.guests_ctx)

    def test_the_switch_persists_and_pauses_at_the_first_event_after_a_load(self):
        L, f = setup()
        sw(L, True, f.next_ms)
        st = json.loads(json.dumps(L.export_state()))
        self.assertTrue(st["p5"]["settings"]["guests_pause"])
        L2 = Learner(P, FixedOffsetTime(0), st)
        self.assertFalse(L2.obs.paused)                                # nothing is applied before the first event
        start(L2)
        self.assertTrue(L2.obs.paused)
        self.assertTrue(L2.status().attributes["learning_paused"])
        self.assertTrue(L2.bh.paused_in(BASE_MS / 1000.0, BASE_MS / 1000.0 + 10))

    def test_resume_records_again(self):
        L, f = setup()
        sw(L, True, f.next_ms)
        rng = random.Random(5)
        f.targets_until(f.next_ms + 180000, sit_fn(rng, BED))
        f.idle_until(f.next_ms + 12000)
        sw(L, False, f.next_ms + 500)
        f.idle_until(f.next_ms + 120000)
        f.targets_until(f.next_ms + 180000, sit_fn(rng, (1.5, 2.0)))   # a second visit, the pause is off
        f.idle_until(f.next_ms + 12000)
        self.commit(L, f)
        self.assertEqual(L.paused_stays(), 1)
        self.assertEqual(len(L.stays()), 1)
        self.assertAlmostEqual(L.stays()[0].x_m, 1.5, delta=0.1)

    def test_the_nightly_run_and_the_review_go_on_while_paused(self):
        L, f = setup()
        sw(L, True, f.next_ms)
        for k in range(8):
            L.tick(BASE_MS + (k + 1) * 86400000 + 4 * 3600000)
        self.assertEqual(L.night, 8)                                   # nights run
        self.assertTrue(L.review.ready(L))
        self.assertEqual([i.id for i in L.review.items(L)], ["bed_needs_data"])

    def test_no_p5_switch_no_change(self):
        """A learner that was never paused records exactly what P4 recorded (spec P5-R1 in miniature)."""
        res = []
        for toggle in (False, True):
            L, f = setup()
            if toggle:
                sw(L, True, f.next_ms)
                sw(L, False, f.next_ms + 1)                            # an empty span: nothing happens in it
            rng = random.Random(4)
            f.targets_until(f.next_ms + 180000, sit_fn(rng, BED))
            f.idle_until(f.next_ms + 12000)
            self.commit(L, f)
            res.append([(s.t0_s, s.dur_s, round(s.x_m, 2), round(s.y_m, 2)) for s in L.stays()])
        self.assertEqual(res[0], res[1])
        self.assertEqual(len(res[0]), 1)


if __name__ == "__main__":
    unittest.main()
