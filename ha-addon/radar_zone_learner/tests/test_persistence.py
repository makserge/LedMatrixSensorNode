import json
import os
import unittest
from pathlib import Path
from unittest import mock

from radar_zone_learner.core.types import StateError
from radar_zone_learner.shell import persistence
from radar_zone_learner.shell.persistence import StateStore
from tests._tmp import tmpdir
from tests.fakes import quiet_logs


def state(n=1, schema=3):
    return {"schema": schema, "writer": "radar_zone_learner 0.3.0", "phase": "p3", "n": n,
            "settings": {"spot_name": "Bed"}}


def strict_validate(obj):
    if obj.get("n", 0) < 0:
        raise StateError("n must be >= 0")
    return ("built", obj["n"])


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self._tmp = tmpdir()
        self.dir = Path(self._tmp.name)
        self.clock = [1_767_571_200.0]
        self.store = StateStore(self.dir, validate=strict_validate, clock_s=lambda: self.clock[0])

    def tearDown(self):
        self._tmp.cleanup()

    def files(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_save_and_load(self):
        n = self.store.save(state(5))
        raw = (self.dir / "state.json").read_bytes()
        self.assertEqual(n, len(raw))
        self.assertEqual(raw, json.dumps(state(5), separators=(",", ":"), ensure_ascii=True).encode())
        self.assertEqual(self.store.load(), state(5))
        self.assertEqual(self.store.loaded_from, "state")
        self.assertEqual(self.store.validated, ("built", 5))
        self.assertEqual(self.files(), ["state.json"])        # no temporary file left

    def test_non_ascii_is_escaped(self):
        st = state()
        st["settings"]["spot_name"] = "Büro"
        self.store.save(st)
        self.assertNotIn(b"\xc3", (self.dir / "state.json").read_bytes())
        self.assertEqual(self.store.load()["settings"]["spot_name"], "Büro")

    def test_failed_write_keeps_old_file(self):
        self.store.save(state(1))
        with mock.patch.object(persistence.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.save(state(2))
        self.assertEqual(self.store.load(), state(1))
        self.assertEqual(self.files(), ["state.json"])

    def test_failed_replace_keeps_old_file(self):
        self.store.save(state(1))
        with mock.patch.object(persistence.os, "replace", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                self.store.save(state(2))
        self.assertEqual(json.loads((self.dir / "state.json").read_text()), state(1))
        self.assertEqual(self.files(), ["state.json"])

    def test_unserialisable_state_keeps_old_file(self):
        self.store.save(state(1))
        with self.assertRaises(TypeError):
            self.store.save({"schema": 3, "bad": object()})
        self.assertEqual(self.store.load(), state(1))

    def test_nothing_saved(self):
        self.assertIsNone(self.store.load())
        self.assertIsNone(self.store.loaded_from)

    def test_corrupt_json_falls_back_to_newest_snapshot(self):
        self.store.snapshot(state(10), "2026-01-10")
        self.store.snapshot(state(12), "2026-01-12")
        self.store.snapshot(state(11), "2026-01-11")
        (self.dir / "state.json").write_bytes(b'{"schema": 3, "trunc')
        self.assertEqual(self.store.load(), state(12))
        self.assertEqual(self.store.loaded_from, "snapshot 2026-01-12")
        self.assertIn("state.json.corrupt-1767571200", self.files())
        self.assertNotIn("state.json", self.files())

    def test_invalid_state_quarantined_and_invalid_snapshot_skipped(self):
        self.store.snapshot(state(7), "2026-01-07")
        self.store.snapshot(state(-1), "2026-01-08")           # fails validation
        (self.dir / "state.json").write_text(json.dumps(state(-5)))
        self.assertEqual(self.store.load(), state(7))
        self.assertIn("state.json.corrupt-1767571200", self.files())

    def test_wrong_types_are_corrupt(self):
        for content in (b"[1, 2, 3]", b"\xff\xfe", json.dumps({"schema": "3"}).encode(),
                        json.dumps({"schema": 2}).encode(), json.dumps({"schema": True}).encode()):
            with self.subTest(content=content):
                (self.dir / "state.json").write_bytes(content)
                self.assertIsNone(self.store.load())
                self.assertNotIn("state.json", self.files())
                self.clock[0] += 1

    def test_two_corrupt_files_in_one_second(self):
        for _ in range(2):
            (self.dir / "state.json").write_bytes(b"x")
            self.assertIsNone(self.store.load())
        self.assertEqual(self.files(), ["state.json.corrupt-1767571200", "state.json.corrupt-1767571200-1"])

    def test_only_the_newest_three_corrupt_files_are_kept(self):
        for k in range(6):
            (self.dir / "state.json").write_bytes(b"x")
            self.assertIsNone(self.store.load())
            dest = self.dir / ("state.json.corrupt-%d" % int(self.clock[0]))
            self.assertTrue(dest.exists())
            os.utime(dest, (self.clock[0], self.clock[0]))        # a known age
            self.clock[0] += 1
        self.assertEqual([n for n in self.files() if ".corrupt-" in n],
                         ["state.json.corrupt-1767571203", "state.json.corrupt-1767571204",
                          "state.json.corrupt-1767571205"])
        # a file with a newer schema is never deleted (D26)
        for k in range(5):
            (self.dir / "state.json").write_bytes(json.dumps(state(1, schema=4 + k)).encode())
            self.assertIsNone(self.store.load())
        self.assertEqual(len([n for n in self.files() if ".newer-" in n]), 5)

    def test_newer_schema_kept_aside_and_fresh_start(self):
        self.store.snapshot(state(3), "2026-01-03")
        newer = json.dumps(state(9, schema=4)).encode()
        (self.dir / "state.json").write_bytes(newer)
        self.assertIsNone(self.store.load())                  # fresh, not the snapshot
        self.assertEqual((self.dir / "state.json.newer-4").read_bytes(), newer)
        self.store.save(state(1))                             # the next save never touches it
        self.assertEqual((self.dir / "state.json.newer-4").read_bytes(), newer)

    def test_loader_crash_is_treated_as_corrupt(self):
        def crashing(obj):
            raise KeyError("grids")
        store = StateStore(self.dir, validate=crashing, clock_s=lambda: self.clock[0])
        store.save(state(1))
        with self.assertLogs("radar_zone_learner.persistence", level="ERROR"):
            self.assertIsNone(store.load())
        self.assertIn("state.json.corrupt-1767571200", self.files())

    def test_seven_snapshots_kept(self):
        for day in range(1, 11):
            self.store.snapshot(state(day), f"2026-01-{day:02d}")
        names = sorted(os.listdir(self.dir / "snapshots"))
        self.assertEqual(names, [f"state-2026-01-{d:02d}.json" for d in range(4, 11)])
        # the same date again replaces that snapshot
        self.store.snapshot(state(99), "2026-01-10")
        self.assertEqual(json.loads((self.dir / "snapshots" / "state-2026-01-10.json").read_text())["n"], 99)
        self.assertEqual(len(os.listdir(self.dir / "snapshots")), 7)

    def test_snapshot_date_checked(self):
        for bad in ("../state", "2026-1-1", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.store.snapshot(state(), bad)

    def test_quarantine_state(self):
        self.assertIsNone(self.store.quarantine_state())
        self.store.save(state(1))
        dest = self.store.quarantine_state()
        self.assertEqual(dest.name, "state.json.corrupt-1767571200")
        self.assertEqual(self.files(), ["state.json.corrupt-1767571200"])


if __name__ == "__main__":
    unittest.main()
