import dataclasses
import unittest
from datetime import date

from radar_zone_learner.core import types as T

BASE_MS = 1767571200000      # 2026-01-05 00:00 UTC, a Monday (spec 4.4)


class TypesTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(T.SCHEMA_VERSION, 3)
        self.assertEqual(T.APP_VERSION, "0.3.0")
        self.assertEqual(T.STATUS_STATES, ("starting", "learning", "not_observing", "no_data"))
        self.assertEqual(T.MARK_FAIL_REASONS, ("not_observing", "no_stay", "several_stays", "too_soon"))
        self.assertEqual(len(T.SUGGESTION_TYPES), 6)
        self.assertIn("bed", T.KINDS)

    def test_frozen(self):
        objs = [T.FramesMessage(b"{}", 1), T.NodeStatus(True, 1), T.ScheduleState(None, None, None, 1),
                T.ContextState("home_empty", None, 1), T.MarkPress(1), T.SpotName("x", 1),
                T.Target(0, 1, 2, 3), T.ListedFrame(0, 0, ()), T.MarkResult(True, None, "l0001", "bed"),
                T.NameEcho("x"), T.NightlyDone(1, "2026-01-05", 0, 0), T.SaveRequested("name"),
                T.DebugMapChanged("start"), T.StatusChanged("starting", {}), T.LocalTime(1, 0, 0),
                T.FixedOffsetTime(0)]
        for o in objs:
            f = dataclasses.fields(o)[0].name
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(o, f, 1)
            self.assertFalse(hasattr(o, "__dict__"), type(o).__name__)    # slots

    def test_epoch_is_thursday(self):
        lt = T.FixedOffsetTime(0).local(0)
        self.assertEqual(lt.date_ord, date(1970, 1, 1).toordinal())
        self.assertEqual(lt.weekday, 3)
        self.assertEqual(lt.sec_of_day, 0)

    def test_base_is_monday(self):
        lt = T.FixedOffsetTime(0).local(BASE_MS)
        self.assertEqual(lt.date_ord, date(2026, 1, 5).toordinal())
        self.assertEqual(lt.weekday, 0)
        self.assertEqual(date.fromordinal(lt.date_ord).weekday(), lt.weekday)
        self.assertEqual(lt.sec_of_day, 0)
        lt = T.FixedOffsetTime(0).local(BASE_MS + 86400000 * 6 + 3600000 * 23 + 59999)
        self.assertEqual(lt.weekday, 6)
        self.assertEqual(lt.sec_of_day, 23 * 3600 + 59)

    def test_offset_moves_the_hour(self):
        a = T.FixedOffsetTime(0).local(BASE_MS + 3600 * 1000 * 10)
        b = T.FixedOffsetTime(3600).local(BASE_MS + 3600 * 1000 * 10)
        self.assertEqual(b.sec_of_day - a.sec_of_day, 3600)
        c = T.FixedOffsetTime(-3600).local(BASE_MS)
        self.assertEqual(c.date_ord, date(2026, 1, 4).toordinal())
        self.assertEqual(c.sec_of_day, 23 * 3600)
        self.assertEqual(c.weekday, 6)

    def test_time_model_protocol(self):
        tm: T.TimeModel = T.FixedOffsetTime(0)
        self.assertIsInstance(tm.local(BASE_MS), T.LocalTime)

    def test_errors(self):
        self.assertTrue(issubclass(T.PayloadError, ValueError))
        self.assertTrue(issubclass(T.StateError, ValueError))


if __name__ == "__main__":
    unittest.main()
