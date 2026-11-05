"""P7 nightly hook: the order of the steps, the containment of each, `NightlyDone` and the save always emitted (spec 4.1,
D37, 9.2 `test_p7_nightly`)."""
import contextlib
import unittest

from radar_zone_learner.core import drift, movedfrom, radarmoved, tuningagg, zoneinfo
from radar_zone_learner.core.params import DEFAULT_P7
from radar_zone_learner.core.types import (DebugMapChanged, NightlyDone, NightlyFailed, SaveRequested, TuningLine)

from .p7_harness import BASE_MS, DAY, add_zone, make, use_days
from .test_extraction import desk_day

STEPS = (("p7:drift", drift, "nightly"), ("p7:release", movedfrom, "release"), ("p7:radar", radarmoved, "nightly"),
         ("p7:tuning", tuningagg, "nightly"))


@contextlib.contextmanager
def patched(mod, name, fn):
    real = getattr(mod, name)
    setattr(mod, name, fn)
    try:
        yield
    finally:
        setattr(mod, name, real)


def tick(L, k, minute=210):
    return L.tick(BASE_MS + k * DAY * 1000 + minute * 60000)


def boom(*a, **k):
    raise RuntimeError("secret value 12345")


class OrderTest(unittest.TestCase):
    def test_the_steps_run_after_the_p6_step_and_before_nightly_done_in_the_spec_order(self):
        L = make()
        order = []
        reals = {}
        with contextlib.ExitStack() as st:
            for tag, mod, name in STEPS:
                reals[tag] = getattr(mod, name)

                def spy(*a, _tag=tag, _real=reals[tag], **k):
                    order.append(_tag)
                    return _real(*a, **k)
                st.enter_context(patched(mod, name, spy))
            real6 = zoneinfo.nightly

            def spy6(*a, **k):
                order.append("p6")
                return real6(*a, **k)
            st.enter_context(patched(zoneinfo, "nightly", spy6))
            outs = tick(L, 8)
        self.assertEqual(order, ["p6", "p7:drift", "p7:release", "p7:radar", "p7:tuning"])
        names = [type(o).__name__ for o in outs]
        self.assertLess(names.index("NightlyDone"), names.index("DebugMapChanged"))
        self.assertLess(names.index("DebugMapChanged"), names.index("TuningLine"))
        self.assertLess(names.index("TuningLine"), names.index("SaveRequested"))

    def test_one_tuning_line_per_nightly_run_and_it_is_not_repeated(self):
        L = make()
        outs = tick(L, 8)
        lines = [o for o in outs if isinstance(o, TuningLine)]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].night, 8)
        self.assertEqual([o for o in tick(L, 8, 500) if isinstance(o, (TuningLine, NightlyDone))], [])
        outs = tick(L, 9)
        self.assertEqual(len([o for o in outs if isinstance(o, TuningLine)]), 1)

    def test_a_disabled_learner_runs_no_step_and_emits_no_line(self):
        L = make(DEFAULT_P7)
        calls = []
        with contextlib.ExitStack() as st:
            for tag, mod, name in STEPS:
                st.enter_context(patched(mod, name, lambda *a, _t=tag, **k: calls.append(_t)))
            outs = tick(L, 8)
        self.assertEqual(calls, [])
        self.assertEqual([o for o in outs if isinstance(o, (TuningLine, NightlyFailed))], [])
        self.assertTrue(any(isinstance(o, NightlyDone) for o in outs))
        self.assertTrue(L.zone7.is_default())
        self.assertNotIn("p7_errors", L.counters())

    def test_the_review_is_touched_after_the_steps(self):
        L = make()
        tick(L, 7)
        v = L.review.ver
        tick(L, 8)
        self.assertGreater(L.review.ver, v)


class ContainmentTest(unittest.TestCase):
    def failing(self, tag, mod, name):
        L = make()
        ran = []
        with contextlib.ExitStack() as st:
            for t, m, n in STEPS:
                if t == tag:
                    st.enter_context(patched(m, n, boom))
                else:
                    real = getattr(m, n)
                    st.enter_context(patched(m, n, lambda *a, _t=t, _r=real, **k: (ran.append(_t), _r(*a, **k))[1]))
            outs = tick(L, 8)
        return L, outs, ran

    def test_a_failing_step_is_counted_reported_and_stops_nothing_else(self):
        for tag, mod, name in STEPS:
            L, outs, ran = self.failing(tag, mod, name)
            fails = [o for o in outs if isinstance(o, NightlyFailed)]
            self.assertEqual(len(fails), 1, tag)
            self.assertTrue(fails[0].error.startswith(tag + " RuntimeError"), fails[0].error)
            self.assertNotIn("secret", fails[0].error)                                    # never a value
            self.assertNotIn("12345", fails[0].error)
            self.assertEqual(L.p7_errors, 1, tag)
            self.assertEqual(L.counters()["p7_errors"], 1)
            self.assertEqual(sorted(ran), sorted(t for t, _, _ in STEPS if t != tag), tag)     # the other steps still ran
            self.assertTrue(any(isinstance(o, NightlyDone) for o in outs), tag)
            self.assertTrue(any(isinstance(o, SaveRequested) and o.reason == "nightly" for o in outs), tag)
            self.assertTrue(any(isinstance(o, DebugMapChanged) for o in outs), tag)
            self.assertEqual(any(isinstance(o, TuningLine) for o in outs), tag != "p7:tuning", tag)
            self.assertEqual(L.nightly_errors, 0, tag)                                    # the P4 counter is not touched

    def test_every_step_failing_still_emits_done_and_the_save(self):
        L = make()
        with contextlib.ExitStack() as st:
            for t, m, n in STEPS:
                st.enter_context(patched(m, n, boom))
            outs = tick(L, 8)
        self.assertEqual(sorted(o.error.split(" ")[0] for o in outs if isinstance(o, NightlyFailed)),
                         sorted(t for t, _, _ in STEPS))
        self.assertEqual(L.p7_errors, 4)
        self.assertTrue(any(isinstance(o, NightlyDone) for o in outs))
        self.assertTrue(any(isinstance(o, SaveRequested) for o in outs))
        self.assertFalse(any(isinstance(o, TuningLine) for o in outs))
        # the learner works on: the next night runs (the steps work again) and no error is added
        outs = tick(L, 9)
        self.assertEqual(L.p7_errors, 4)
        self.assertTrue(any(isinstance(o, NightlyDone) for o in outs))
        self.assertTrue(any(isinstance(o, TuningLine) for o in outs))

    def test_a_p6_failure_does_not_stop_the_p7_steps(self):
        L = make()
        with patched(zoneinfo, "nightly", boom):
            outs = tick(L, 8)
        self.assertEqual(L.nightly_errors, 1)
        self.assertEqual(L.p7_errors, 0)
        self.assertTrue(any(isinstance(o, TuningLine) for o in outs))
        self.assertTrue(any(isinstance(o, NightlyDone) for o in outs))

    def test_a_failure_does_not_touch_zones_items_or_suggestions(self):
        L = make()
        add_zone(L, (2.0, 4.0))
        use_days(L, range(8), (2.0, 4.0))
        n = len(L.review.zones)
        with patched(drift, "nightly", boom):
            tick(L, 8)
        self.assertEqual(len(L.review.zones), n)


class RealNightlyTest(unittest.TestCase):
    def test_the_drift_step_runs_inside_a_real_nightly(self):
        L = make()
        for d in range(8):
            desk_day(L, d)
        add_zone(L, (2.55, 2.2), night=4)
        outs = tick(L, 8)
        self.assertFalse([o for o in outs if isinstance(o, NightlyFailed)])
        self.assertEqual(L.p7_errors, 0)
        e = L.zone7.entry("z0001")
        self.assertIsNotNone(e)
        self.assertEqual(e["vs"], "young")                                           # created on night 4: 4 nights old
        self.assertEqual(L.drift_view()[0].state, "young")

    def test_the_status_and_counters_carry_the_p7_keys_when_enabled(self):
        L = make()
        tick(L, 8)
        a = L.status().attributes
        self.assertEqual(a["radar_moved"], "none")
        self.assertEqual(L.counters()["zone7_reset"], 0)
        self.assertEqual(L.counters()["zone7_pruned"], 0)


if __name__ == "__main__":
    unittest.main()
