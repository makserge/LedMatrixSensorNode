"""StateStore.wipe() (P5 spec 7.2 step 3): exactly the learned-data files go, nothing else."""
import os
import unittest
from pathlib import Path
from unittest import mock

from radar_zone_learner.shell.persistence import StateStore
from tests._tmp import tmpdir
from tests.fakes import quiet_logs


def state(n=1):
    return {"schema": 4, "writer": "radar_zone_learner 0.5.0", "phase": "p5", "n": n, "settings": {"spot_name": "Bed"}}


class WipeTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self._tmp = tmpdir()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = StateStore(self.dir, clock_s=lambda: 1_767_571_200.0)

    def names(self):
        out = []
        for root, dirs, files in os.walk(self.dir):
            rel = Path(root).relative_to(self.dir)
            out += [(rel / f).as_posix() for f in files]
            out += [(rel / d).as_posix() + "/" for d in dirs]
        return sorted(out)

    def fill(self):
        self.store.save(state(1))
        for day in range(1, 4):
            self.store.snapshot(state(day), "2026-01-0%d" % day)
        (self.dir / "state.json.tmp").write_text("half a write")
        (self.dir / "state.json.corrupt-1767571200").write_text("{")
        (self.dir / "state.json.corrupt-1767571201-1").write_text("{")
        (self.dir / "state.json.newer-5").write_text("{}")
        (self.dir / "rf_stats.json").write_text('{"buckets": []}')
        (self.dir / "options.json").write_text("{}")
        (self.dir / "capture").mkdir()
        (self.dir / "capture" / "capture-20260101-10.jsonl").write_text("x\n")
        (self.dir / "other.txt").write_text("x")
        (self.dir / "snapshots" / "notes.txt").write_text("x")

    def test_deletes_exactly_the_learned_data_files(self):
        self.fill()
        removed = self.store.wipe()
        self.assertEqual(removed, 1 + 3 + 1 + 2 + 1)
        self.assertEqual(self.names(), ["capture/", "capture/capture-20260101-10.jsonl", "options.json", "other.txt",
                                        "rf_stats.json", "snapshots/", "snapshots/notes.txt"])

    def test_the_folder_stays_and_the_store_still_works(self):
        self.fill()
        self.store.wipe()
        self.assertTrue((self.dir / "snapshots").is_dir())
        self.assertIsNone(self.store.load())
        self.store.save(state(7))
        self.assertEqual(self.store.load(), state(7))

    def test_a_leftover_temporary_snapshot_goes_too(self):
        self.store.snapshot(state(1), "2026-01-01")
        (self.dir / "snapshots" / "state-2026-01-02.json.tmp").write_text("half")
        self.assertEqual(self.store.wipe(), 2)
        self.assertEqual(self.names(), ["snapshots/"])

    def test_nothing_to_delete_is_not_an_error(self):
        self.assertEqual(self.store.wipe(), 0)
        self.assertEqual(self.names(), [])

    def test_a_file_that_cannot_be_deleted_is_logged_and_left_and_the_rest_goes(self):
        self.fill()
        real_unlink = Path.unlink

        def unlink(path, *a, **k):
            if path.name == "state.json":
                raise PermissionError("busy")
            return real_unlink(path, *a, **k)
        with mock.patch.object(Path, "unlink", unlink):
            removed = self.store.wipe()                      # never raises
        self.assertEqual(removed, 3 + 1 + 2 + 1)
        self.assertIn("state.json", self.names())
        self.assertNotIn("state.json.tmp", self.names())

    def test_a_missing_data_folder_is_harmless(self):
        gone = StateStore(self.dir / "nowhere")
        self.assertEqual(gone.wipe(), 0)

    def test_the_loaded_marker_is_reset(self):
        self.store.save(state(1))
        self.store.load()
        self.assertEqual(self.store.loaded_from, "state")
        self.store.wipe()
        self.assertIsNone(self.store.loaded_from)
        self.assertIsNone(self.store.validated)


if __name__ == "__main__":
    unittest.main()
