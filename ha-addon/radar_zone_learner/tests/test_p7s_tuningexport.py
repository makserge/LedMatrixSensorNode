"""The local nightly count lines and the pooled export (P7 spec 4.8, 7.1, 7.3, check P7-9 shell part): the writer, the trim to 28
lines, a re-delivered night, a refused line, the pooled record and its privacy scan and size cap, no export file, Forget all
deleting the folder, and the layer's handling of a ``TuningLine`` and of the button."""
import json
import unittest
from pathlib import Path

from radar_zone_learner.core import tuningagg
from radar_zone_learner.core.types import TuningLine
from radar_zone_learner.shell.p7 import P7Layer
from radar_zone_learner.shell.tuningexport import (DUPLICATE, REJECTED, STORED, TuningStore, state_text)
from tests._tmp import tmpdir
from tests.fakes import FakeLearner, clean_tuning_line, clean_tuning_text, quiet_logs
from tests.p6_harness import zone_view
from tests.p7_harness import add_stay, cover, make, set_run
from tests.p7s_harness import (BTN_ID, EXPORT_CMD, SENSOR_ID, TUNING_EXPORT, TUNING_STATE, LayerRig7, tuning_line)


def real_line(night):
    """A real nightly line of the core (a learner with a covered date, the day before the run, and a stay on it)."""
    L = make()
    set_run(L, night)
    cover(L, night - 1)
    add_stay(L, night - 1, 9, 600, 2.0, 3.0)
    return tuningagg.nightly(L, night)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = TuningStore(self.dir)

    def test_the_file_is_one_line_per_night_under_tuning(self):
        for n in (1, 2, 3):
            self.assertEqual(self.store.add(n, clean_tuning_text(sn=n)), STORED)
        self.assertEqual(self.store.path, self.dir / "tuning" / "nights.jsonl")
        raw = self.store.path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        rows = raw.decode().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertEqual([json.loads(r)["sn"] for r in rows], [1, 2, 3])
        self.assertEqual(self.store.count(), 3)

    def test_the_newest_28_lines_are_kept(self):
        for n in range(1, 41):
            self.assertEqual(self.store.add(n, clean_tuning_text(sn=n)), STORED)
        rows = [json.loads(r) for r in self.store.path.read_text().splitlines()]
        self.assertEqual(len(rows), 28)
        self.assertEqual([r["sn"] for r in rows], list(range(13, 41)))
        self.assertEqual(self.store.count(), 28)

    def test_the_keep_count_is_the_p7_constant(self):
        from radar_zone_learner.core.params import DEFAULT_P7
        self.assertEqual(DEFAULT_P7.agg_keep_nights, 28)
        self.assertEqual(self.store.keep, 28)
        self.assertEqual(TuningStore(self.dir, keep=3).keep, 3)

    def test_a_redelivered_night_is_dropped_also_after_a_restart(self):
        self.assertEqual(self.store.add(7, clean_tuning_text(sn=1)), STORED)
        self.assertEqual(self.store.add(7, clean_tuning_text(sn=99)), DUPLICATE)
        self.assertEqual(self.store.duplicates, 1)
        self.assertEqual(self.store.count(), 1)
        again = TuningStore(self.dir)                                    # the add-on restarted before the state was saved
        self.assertEqual(again.add(7, clean_tuning_text(sn=99)), DUPLICATE)
        self.assertEqual(again.add(8, clean_tuning_text(sn=2)), STORED)
        self.assertEqual([json.loads(r)["sn"] for r in again.lines()], [1, 2])

    def test_the_file_holds_no_night_number(self):
        self.store.add(7, clean_tuning_text())
        self.assertNotIn(b"night", self.store.path.read_bytes())
        self.assertEqual((self.dir / "tuning" / "last_night").read_text(), "7")        # one number, not a series

    def test_a_line_that_is_not_a_clean_count_line_is_refused(self):
        bad = {
            "a date": clean_tuning_text(dt="2026-01-05"),
            "a position key": clean_tuning_text(x=[1, 2]),
            "a string value": json.dumps(dict(clean_tuning_line(), ob="2026-01-05")),
            "not JSON": "not json",
            "a list": "[1,2,3]",
            "a newline": clean_tuning_text().replace("{", "{\n", 1),
            "too long": json.dumps(dict(clean_tuning_line(), cn=0)) + " " * 3000,
            "wrong shape": json.dumps(dict(clean_tuning_line(), sd=[1, 2])),
        }
        for why, text in bad.items():
            self.assertEqual(self.store.add(1, text), REJECTED, why)
        self.assertEqual(self.store.rejected, len(bad))
        self.assertEqual(self.store.count(), 0)
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.store.add(1, 12345), REJECTED)             # not even text

    def test_a_damaged_file_never_reaches_the_pool(self):
        self.store.add(1, clean_tuning_text(sn=1))
        with open(self.store.path, "ab") as f:
            f.write(b"garbage\n" + b'{"x": [1, 2], "date": "2026-01-05"}\n' + clean_tuning_text(sn=3).encode() + b"\n")
        self.assertEqual([json.loads(r)["sn"] for r in self.store.lines()], [1, 3])
        res = self.store.export()
        self.assertEqual(res.n, 2)
        self.assertEqual(json.loads(res.payload)["sum"]["sn"], 4)

    def test_no_file_means_no_lines_and_an_empty_export(self):
        self.assertEqual(self.store.lines(), [])
        self.assertEqual(self.store.count(), 0)
        res = self.store.export()
        self.assertIsNone(res.payload)
        self.assertEqual((res.n, res.reason), (0, "empty"))

    def test_the_state_text(self):
        self.assertEqual(state_text(0), "none")
        self.assertEqual(state_text(-1), "none")
        self.assertEqual(state_text(5), "5 nights")
        self.assertEqual(state_text(28), "28 nights")

    def test_a_write_error_is_raised_to_the_caller_and_leaves_the_old_file(self):
        self.store.add(1, clean_tuning_text(sn=1))
        before = self.store.path.read_bytes()
        from unittest import mock
        with mock.patch("radar_zone_learner.shell.tuningexport.atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.add(2, clean_tuning_text(sn=2))
        self.assertEqual(self.store.path.read_bytes(), before)


class PooledExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = TuningStore(self.dir)

    def test_the_export_is_the_pool_of_the_kept_lines_and_scans_clean(self):
        for n in range(1, 6):
            self.store.add(n, clean_tuning_text(sn=n * 10, cd=n))
        res = self.store.export()
        self.assertIsNotNone(res.payload)
        self.assertEqual(res.n, 5)
        pooled = json.loads(res.payload)
        self.assertEqual(pooled, tuningagg.pool([clean_tuning_text(sn=n * 10, cd=n) for n in range(1, 6)]))
        self.assertEqual(set(pooled), {"v", "n", "sum", "last"})
        self.assertEqual(pooled["n"], 5)
        self.assertEqual(pooled["sum"]["sn"], 150)                      # summed over the nights
        self.assertEqual(pooled["last"]["cd"], 5)                       # the newest line's value
        self.assertEqual(tuningagg.scan(pooled), [])

    def test_the_export_has_no_per_night_series_no_date_and_no_night_index(self):
        for n in range(1, 8):
            self.store.add(n, clean_tuning_text(sn=n))
        text = self.store.export().payload.decode()
        self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}")
        self.assertNotRegex(text, r"\d{1,2}:\d{2}")
        for word in ("night", "date", "day", "hour", "time", "zone", "name", "id"):
            self.assertNotIn('"%s"' % word, text)
        pooled = json.loads(text)
        self.assertEqual(pooled["n"], 7)
        # no value of 'sum' is a list of seven (a per-night list): the lists are the fixed-length histograms
        lens = {len(v) for v in pooled["sum"].values() if isinstance(v, list)}
        self.assertTrue(lens <= {2, 4, 5, 6, 7, 8, 10}, lens)

    def test_the_payload_is_small_and_within_the_cap(self):
        for n in range(1, 29):
            self.store.add(n, clean_tuning_text(sn=n))
        res = self.store.export()
        self.assertLessEqual(len(res.payload), 16384)
        self.assertLess(len(res.payload), 4000)

    def test_a_pooled_record_above_the_cap_is_not_published(self):
        small = TuningStore(self.dir, export_max=100)
        small.add(1, clean_tuning_text())
        res = small.export()
        self.assertIsNone(res.payload)
        self.assertEqual(res.reason, "too_large")

    def test_a_pool_that_fails_is_not_published(self):
        from unittest import mock
        self.store.add(1, clean_tuning_text())
        with mock.patch.object(tuningagg, "pool", side_effect=KeyError("x")):
            res = self.store.export()
        self.assertEqual((res.payload, res.reason), (None, "error"))

    def test_a_pooled_record_that_fails_the_scan_is_not_published(self):
        from unittest import mock
        self.store.add(1, clean_tuning_text())
        with mock.patch.object(tuningagg, "pool", return_value={"v": 1, "n": 1, "sum": {"zone": [1, 2]}, "last": {}}):
            res = self.store.export()
        self.assertEqual((res.payload, res.reason), (None, "unclean"))

    def test_the_export_writes_no_file(self):
        self.store.add(1, clean_tuning_text())
        before = sorted(p.name for p in (self.dir / "tuning").iterdir())
        self.store.export()
        self.store.export()
        self.assertEqual(sorted(p.name for p in (self.dir / "tuning").iterdir()), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["tuning"])

    def test_real_core_lines_are_clean_and_pool(self):
        for n in (4, 5, 6):
            self.assertEqual(self.store.add(n, real_line(n).text), STORED)
        res = self.store.export()
        self.assertEqual(res.n, 3)
        pooled = json.loads(res.payload)
        self.assertEqual(pooled["sum"]["sn"], 3)
        self.assertEqual(tuningagg.scan(pooled), [])


class PurgeTest(unittest.TestCase):
    def test_purge_deletes_the_whole_folder(self):
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        store = TuningStore(data)
        store.add(1, clean_tuning_text())
        (data / "tuning" / "nights.jsonl.tmp").write_bytes(b"x")
        self.assertEqual(store.purge(), 3)
        self.assertFalse((data / "tuning").exists())
        self.assertEqual(store.count(), 0)
        self.assertEqual(store.add(1, clean_tuning_text()), STORED)         # the night counter was forgotten too
        self.assertEqual(store.purge(), 2)
        self.assertEqual(store.purge(), 0)                                  # nothing there: a no-op, never an error

    def test_purge_does_not_touch_the_state_or_the_capture(self):
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        (data / "capture").mkdir()
        (data / "capture" / "capture-20260101-00.jsonl").write_bytes(b"x")
        (data / "state.json").write_bytes(b"{}")
        store = TuningStore(data)
        store.add(1, clean_tuning_text())
        store.purge()
        self.assertEqual(sorted(p.name for p in data.iterdir()), ["capture", "state.json"])


class LayerTest(unittest.TestCase):
    """The layer: a TuningLine is written, the button publishes the pooled record to a topic that is not retained, the sensor
    follows the file, Forget all deletes it."""

    def setUp(self):
        self.rig = LayerRig7(self)
        self.rig.ready(zone_view("z0001", "Desk"))
        self.ha = self.rig.ha
        self.layer = self.rig.layer
        self.store = self.layer.tuning

    def feed(self, night, **changes):
        self.rig.feed(tuning_line(night, **changes))

    def test_a_tuning_line_is_consumed_by_the_layer_and_written(self):
        self.assertEqual(self.rig.consumed, [("StatusChanged", False), ("ZoneStates", True)])
        self.feed(1)
        self.assertEqual(self.rig.consumed[-1], ("TuningLine", True))
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.ha.state(SENSOR_ID), "1 nights")

    def test_the_sensor_follows_the_number_of_kept_nights_and_is_retained(self):
        self.assertEqual(self.ha.state(SENSOR_ID), "none")
        for n in range(1, 31):
            self.feed(n)
        self.assertEqual(self.ha.state(SENSOR_ID), "28 nights")
        self.assertTrue(self.rig.client.published_to(TUNING_STATE)[-1][3])           # retained
        self.assertEqual(self.rig.client.published_to(TUNING_STATE)[-1][2], 1)       # QoS 1

    def test_a_refused_line_changes_nothing_and_a_duplicate_is_dropped(self):
        self.rig.feed(TuningLine(1, "not json"))
        self.feed(2)
        self.feed(2, sn=50)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.rejected, 1)
        self.assertEqual(self.store.duplicates, 1)
        self.assertEqual(self.layer.p7_shell_errors, 0)

    def test_a_write_error_is_counted_and_nothing_else_happens(self):
        from unittest import mock
        with mock.patch("radar_zone_learner.shell.tuningexport.atomic_write", side_effect=OSError("disk full")):
            self.feed(1)
        self.assertEqual(self.layer.tuning_write_errors, 1)
        self.assertEqual(self.layer.p7_shell_errors, 0)
        self.assertEqual(self.ha.state(SENSOR_ID), "none")
        self.feed(2)
        self.assertEqual(self.ha.state(SENSOR_ID), "1 nights")

    def test_the_button_publishes_one_pooled_record_not_retained_and_writes_no_file(self):
        for n in range(1, 6):
            self.feed(n, sn=n)
        listened = self.ha.listen(TUNING_EXPORT)                                    # HA[U8], an assumption
        files = sorted(p.name for p in self.rig.data_dir.rglob("*"))
        self.ha.press(BTN_ID)
        got = self.rig.client.published_to(TUNING_EXPORT)
        self.assertEqual(len(got), 1)
        topic, payload, qos, retain = got[0]
        self.assertFalse(retain)
        self.assertEqual(qos, 1)
        pooled = json.loads(payload)
        self.assertEqual((pooled["n"], pooled["sum"]["sn"]), (5, 15))
        self.assertEqual(tuningagg.scan(pooled), [])
        self.assertEqual(listened, [payload])
        self.assertNotIn(TUNING_EXPORT, self.ha.broker_retained)                    # the broker keeps nothing of it
        self.assertEqual(sorted(p.name for p in self.rig.data_dir.rglob("*")), files)   # no export file
        self.assertEqual(self.layer.tuning_exports, 1)
        self.assertEqual(self.layer.attributes()["tuning_exports"], 1)

    def test_a_late_listener_gets_nothing_because_the_message_was_not_retained(self):
        self.feed(1)
        self.ha.press(BTN_ID)
        self.assertEqual(self.ha.listen(TUNING_EXPORT), [])

    def test_a_press_with_no_nights_publishes_nothing_and_is_counted(self):
        self.ha.press(BTN_ID)
        self.assertEqual(self.rig.client.published_to(TUNING_EXPORT), [])
        self.assertEqual((self.layer.tuning_exports, self.layer.tuning_export_refused), (0, 1))

    def test_a_press_while_the_broker_link_is_down_is_counted_and_not_retried(self):
        self.feed(1)
        self.rig.connected = False
        self.layer.on_event(self.layer.topics.make_mapper().map(EXPORT_CMD, b"PRESS", False, 1)[0], self.rig.ms)
        self.rig.connected = True
        self.rig.run(5)
        self.assertEqual(self.rig.client.published_to(TUNING_EXPORT), [])
        self.assertEqual((self.layer.tuning_exports, self.layer.tuning_export_refused), (0, 1))

    def test_the_export_logs_the_count_of_nights_and_nothing_else(self):
        for n in range(1, 4):
            self.feed(n)
        with self.assertLogs("radar_zone_learner.p7", level="INFO") as cm:
            self.ha.press(BTN_ID)
        self.assertEqual([r.getMessage() for r in cm.records], ["tuning export: 3 nights"])

    def test_forget_all_deletes_the_folder_and_the_sensor_says_none(self):
        for n in range(1, 4):
            self.feed(n)
        self.assertTrue((self.rig.data_dir / "tuning" / "nights.jsonl").exists())
        self.layer.purge_capture()                                                   # the service's step 3
        self.layer.reset()                                                           # step 4
        self.ha.process()
        self.assertFalse((self.rig.data_dir / "tuning").exists())
        self.assertEqual(self.ha.state(SENSOR_ID), "none")
        self.feed(1)                                                                 # the new learner's night 1 is not a duplicate
        self.assertEqual(self.store.count(), 1)

    def test_a_layer_without_a_data_folder_consumes_the_line_and_keeps_nothing(self):
        quiet_logs(self)
        layer = P7Layer(self.layer.topics, lambda *a, **k: True, None)
        self.assertTrue(layer.on_output(TuningLine(1, clean_tuning_text()), 0))
        self.assertIsNone(layer.tuning)
        layer.on_event(self.layer.topics.make_mapper().map(EXPORT_CMD, b"PRESS", False, 1)[0], 0)
        self.assertEqual((layer.tuning_exports, layer.tuning_export_refused), (0, 1))

    def test_an_error_inside_the_layer_is_contained(self):
        from unittest import mock
        with mock.patch.object(self.store, "add", side_effect=RuntimeError("boom")), self.assertLogs("radar_zone_learner.p7"):
            self.assertTrue(self.layer.on_output(tuning_line(9), self.rig.ms))
        self.assertEqual(self.layer.p7_shell_errors, 1)

    def test_the_status_attributes_carry_counts_only(self):
        attrs = self.layer.attributes()
        for key in ("tuning_exports", "tuning_export_refused", "tuning_write_errors", "p7_shell_errors"):
            self.assertEqual(attrs[key], 0)
        self.assertEqual(len(json.dumps(attrs)), len(json.dumps(attrs).encode("ascii")))


class FakeLearnerLineTest(unittest.TestCase):
    def test_the_fakes_line_is_clean_and_a_real_learners_line_has_the_same_keys(self):
        self.assertEqual(tuningagg.scan(FakeLearner.p7_line(3).text), [])
        self.assertEqual(set(json.loads(real_line(5).text)), set(clean_tuning_line()))
        self.assertTrue(set(clean_tuning_line()) <= tuningagg.whitelist() | {"v"} | {k.split(".")[0] for k in tuningagg.whitelist()})


if __name__ == "__main__":
    unittest.main()
