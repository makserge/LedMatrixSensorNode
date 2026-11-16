"""0.7.2: the debug-data export (``shell/debugexport.py``) and the button 'Export debug data' of the P7 layer.

Every test works in temporary folders: the share root is always injected, so nothing here can touch the real /share.
"""
import json
import os
import re
import shutil
import unittest
from pathlib import Path

from radar_zone_learner.shell import debugexport as DX
from radar_zone_learner.shell.debugexport import EXPORT_RE, SHARE_SUBDIR, export_debug
from radar_zone_learner.shell.topics_p7 import ExportDebugPress
from tests._tmp import tmpdir
from tests.fakes import quiet_logs
from tests.p6_harness import zone_view
from tests.p7s_harness import LayerRig7

EXPORT_BTN = "button.radar_zones_learner_export_debug"
EVENT = "radar_zones/review/event"
T0 = 1_800_000_000          # 2027-01-15 08:00:00 UTC


def can_symlink() -> bool:
    tmp = tmpdir()
    try:
        os.symlink(tmp.name, os.path.join(tmp.name, "l"))
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        tmp.cleanup()


CAN_LINK = can_symlink()


class Base(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.tmp = tmpdir()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.share = root / "share"
        (self.data / "capture").mkdir(parents=True)
        self.share.mkdir()
        self.base = self.share / SHARE_SUBDIR

    def cap(self, name, text):
        p = self.data / "capture" / name
        p.write_bytes(text if isinstance(text, bytes) else text.encode())
        return p

    def state(self, text='{"schema":4}'):
        (self.data / "state.json").write_text(text)

    def run_export(self, at=T0, **kw):
        return export_debug(self.data, self.share, at, **kw)

    def exports(self):
        return sorted(p.name for p in self.base.iterdir() if p.name.startswith("export-")) if self.base.exists() else []


class CopyTest(Base):
    def test_copies_the_capture_files_the_state_and_a_readme(self):
        self.cap("capture-20270115-07.jsonl", '{"recv_ms":1}\n')
        self.cap("capture-20270115-08.jsonl", '{"recv_ms":2}\n{"recv_ms":3}\n')
        self.state('{"schema":4,"zones":[]}')
        r = self.run_export()
        self.assertTrue(r.ok, r)
        self.assertEqual(r.folder, "export-20270115-080000")
        out = self.base / r.folder
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["README.txt", "capture-20270115-07.jsonl", "capture-20270115-08.jsonl", "state.json"])
        self.assertEqual((out / "capture-20270115-08.jsonl").read_text(), '{"recv_ms":2}\n{"recv_ms":3}\n')
        self.assertEqual((out / "state.json").read_text(), '{"schema":4,"zones":[]}')
        readme = (out / "README.txt").read_text()
        self.assertIn("raw", readme)
        self.assertIn("Delete this folder", readme)
        self.assertIn("2 file(s)", readme)
        self.assertEqual(r.files, 4)
        self.assertEqual(r.bytes, sum(p.stat().st_size for p in out.iterdir() if p.name != "README.txt"))

    def test_the_sources_are_untouched(self):
        p = self.cap("capture-20270115-08.jsonl", '{"a":1}\n')
        self.state()
        self.run_export()
        self.assertEqual(p.read_text(), '{"a":1}\n')
        self.assertEqual((self.data / "state.json").read_text(), '{"schema":4}')

    def test_other_files_are_not_copied(self):
        self.cap("capture-20270115-08.jsonl", "{}\n")
        self.cap("notes.txt", "x")
        (self.data / "tuning").mkdir()
        (self.data / "tuning" / "nights.jsonl").write_text("{}\n")
        (self.data / "options.json").write_text("{}")
        r = self.run_export()
        names = {p.name for p in (self.base / r.folder).iterdir()}
        self.assertEqual(names, {"README.txt", "capture-20270115-08.jsonl"})

    def test_state_only_is_enough(self):
        self.state()
        r = self.run_export()
        self.assertTrue(r.ok)
        self.assertEqual(sorted(p.name for p in (self.base / r.folder).iterdir()), ["README.txt", "state.json"])
        self.assertIn("0 file(s)", (self.base / r.folder / "README.txt").read_text())

    def test_capture_only_is_enough(self):
        self.cap("capture-20270115-08.jsonl", "{}\n")
        r = self.run_export()
        self.assertTrue(r.ok)
        self.assertIn("there was no state file", (self.base / r.folder / "README.txt").read_text())

    def test_a_partial_last_line_is_cut(self):
        self.cap("capture-20270115-08.jsonl", '{"a":1}\n{"a":2}\n{"a":')
        r = self.run_export()
        self.assertEqual((self.base / r.folder / "capture-20270115-08.jsonl").read_text(), '{"a":1}\n{"a":2}\n')

    def test_the_readme_is_ascii_with_lf(self):
        self.state()
        r = self.run_export()
        raw = (self.base / r.folder / "README.txt").read_bytes()
        raw.decode("ascii")
        self.assertNotIn(b"\r", raw)


class RefuseTest(Base):
    def test_refuses_when_there_is_nothing_to_copy(self):
        r = self.run_export()
        self.assertEqual((r.ok, r.reason), (False, "empty"))
        self.assertFalse(self.base.exists())                       # not even the folder is made

    def test_empty_capture_files_do_not_count(self):
        self.cap("capture-20270115-08.jsonl", "")
        self.assertEqual(self.run_export().reason, "empty")

    def test_refuses_when_the_share_folder_is_missing(self):
        shutil.rmtree(self.share)
        self.state()
        r = self.run_export()
        self.assertEqual((r.ok, r.reason), (False, "error"))
        self.assertFalse(self.share.exists())                      # the share root is never created, only the subfolder

    def test_a_state_over_the_limit_is_refused(self):
        self.state("x" * 100)
        r = self.run_export(max_bytes=50)
        self.assertEqual((r.ok, r.reason), (False, "too_large"))
        self.assertEqual(self.exports(), [])

    def test_the_same_second_twice_is_refused_and_keeps_the_first(self):
        self.state()
        a = self.run_export()
        b = self.run_export()
        self.assertTrue(a.ok)
        self.assertEqual((b.ok, b.reason), (False, "exists"))
        self.assertEqual(self.exports(), [a.folder])


class LimitTest(Base):
    def test_size_limit_state_first_then_the_newest_files(self):
        self.state("s" * 10)
        self.cap("capture-20270115-06.jsonl", "a" * 39 + "\n")       # 40 bytes each
        self.cap("capture-20270115-07.jsonl", "b" * 39 + "\n")
        self.cap("capture-20270115-08.jsonl", "c" * 39 + "\n")
        r = self.run_export(max_bytes=10 + 80)
        self.assertTrue(r.ok)
        names = sorted(p.name for p in (self.base / r.folder).iterdir())
        self.assertEqual(names, ["README.txt", "capture-20270115-07.jsonl", "capture-20270115-08.jsonl", "state.json"])
        self.assertEqual(r.left_out, 1)
        self.assertLessEqual(r.bytes, 90)
        self.assertIn("left out", (self.base / r.folder / "README.txt").read_text())

    def test_the_default_limit_is_100_mib(self):
        self.assertEqual(DX.MAX_BYTES, 100 * 1024 * 1024)
        self.assertEqual(DX.KEEP, 3)

    def test_keeps_only_the_newest_three_export_folders(self):
        self.state()
        made = [self.run_export(at=T0 + i).folder for i in range(5)]
        self.assertEqual(self.exports(), made[2:])
        self.assertEqual(self.run_export(at=T0 + 5).pruned, 1)
        self.assertEqual(self.exports(), made[3:] + ["export-" + time_stamp(T0 + 5)])

    def test_prune_touches_only_export_folders_inside_the_base(self):
        self.state()
        self.base.mkdir()
        (self.base / "keep-me").mkdir()
        (self.base / "export-notes").mkdir()                       # not the pattern
        (self.base / "export-20200101-000000.txt").write_text("a file with a near name")
        (self.base / "other.txt").write_text("x")
        outside = self.share / "export-20200101-000000"            # next to the base, not inside it
        outside.mkdir()
        old = [self.base / ("export-20200101-00000%d" % i) for i in range(4)]
        for d in old:
            d.mkdir()
            (d / "f").write_text("x")
        self.run_export()
        self.assertEqual(sorted(p.name for p in self.base.iterdir() if EXPORT_RE.match(p.name)),
                         [old[2].name, old[3].name, "export-" + time_stamp(T0)])
        for name in ("keep-me", "export-notes", "export-20200101-000000.txt", "other.txt"):
            self.assertTrue((self.base / name).exists(), name)
        self.assertTrue(outside.exists())

    def test_a_stale_temporary_folder_is_removed(self):
        self.state()
        self.base.mkdir()
        stale = self.base / (DX.TMP_PREFIX + "20200101-000000")
        stale.mkdir()
        (stale / "x").write_text("x")
        self.run_export()
        self.assertFalse(stale.exists())


def time_stamp(s):
    import time
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(s))


class AtomicTest(Base):
    def test_a_failure_while_copying_leaves_no_export_and_no_temporary_folder(self):
        self.cap("capture-20270115-07.jsonl", "{}\n")
        self.cap("capture-20270115-08.jsonl", "{}\n")
        self.state()
        real = DX._copy
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(*a, **k)
        DX._copy = flaky
        try:
            r = self.run_export()
        finally:
            DX._copy = real
        self.assertEqual((r.ok, r.reason), (False, "error"))
        self.assertEqual([p.name for p in self.base.iterdir()], [])

    def test_a_failed_export_does_not_prune_the_older_ones(self):
        self.state()
        good = [self.run_export(at=T0 + i).folder for i in range(3)]
        real = DX._copy
        DX._copy = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
        try:
            r = self.run_export(at=T0 + 10)
        finally:
            DX._copy = real
        self.assertFalse(r.ok)
        self.assertEqual(self.exports(), good)

    def test_the_folder_appears_by_one_rename(self):
        self.state()
        seen = []
        real = os.rename

        def spy(src, dst):
            seen.append((Path(src).name, Path(dst).name, sorted(p.name for p in Path(src).iterdir())))
            return real(src, dst)
        os.rename = spy
        try:
            r = self.run_export()
        finally:
            os.rename = real
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0].startswith(DX.TMP_PREFIX))
        self.assertEqual(seen[0][1], r.folder)
        self.assertEqual(seen[0][2], ["README.txt", "state.json"])      # complete before the rename


class SafetyTest(Base):
    def test_everything_is_written_inside_the_base(self):
        self.cap("capture-20270115-08.jsonl", "{}\n")
        self.state()
        before = {p for p in Path(self.tmp.name).rglob("*")}
        self.run_export()
        new = {p for p in Path(self.tmp.name).rglob("*")} - before
        self.assertTrue(new)
        for p in new:
            self.assertTrue(self.base == p or self.base in p.parents, p)

    def test_the_default_base_is_the_share_mount(self):
        self.assertEqual((DX.SHARE_ROOT, DX.SHARE_SUBDIR), ("/share", "radar_zone_learner"))

    @unittest.skipUnless(CAN_LINK, "symbolic links are not available here")
    def test_symlinked_capture_and_state_files_are_ignored(self):
        secret = Path(self.tmp.name) / "secret.txt"
        secret.write_text("secret")
        os.symlink(secret, self.data / "capture" / "capture-20270115-09.jsonl")
        os.symlink(secret, self.data / "state.json")
        self.assertEqual(self.run_export().reason, "empty")
        self.cap("capture-20270115-08.jsonl", "{}\n")
        r = self.run_export()
        names = sorted(p.name for p in (self.base / r.folder).iterdir())
        self.assertEqual(names, ["README.txt", "capture-20270115-08.jsonl"])

    @unittest.skipUnless(CAN_LINK, "symbolic links are not available here")
    def test_a_symlinked_base_is_refused_and_nothing_is_written_through_it(self):
        target = Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        os.symlink(target, self.base, target_is_directory=True)
        self.state()
        self.assertEqual(self.run_export().reason, "error")
        self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(CAN_LINK, "symbolic links are not available here")
    def test_a_symlinked_export_folder_is_not_followed_by_the_prune(self):
        self.state()
        target = Path(self.tmp.name) / "precious"
        target.mkdir()
        (target / "f").write_text("keep")
        self.base.mkdir()
        os.symlink(target, self.base / "export-20200101-000000", target_is_directory=True)
        for i in range(4):
            self.run_export(at=T0 + i)
        self.assertEqual((target / "f").read_text(), "keep")
        self.assertTrue((self.base / "export-20200101-000000").is_symlink())

    def test_never_raises(self):
        self.state()
        r = export_debug(self.data, self.share / "no" / "such", T0)
        self.assertFalse(r.ok)
        r = export_debug(None, self.share, T0)
        self.assertFalse(r.ok)


class LayerTest(unittest.TestCase):
    def setUp(self):
        self.rig = LayerRig7(self)
        self.rig.ready(zone_view("z0001", "Desk"))
        self.ha = self.rig.ha
        self.layer = self.rig.layer
        self.data = self.rig.data_dir
        self.base = self.rig.share_root / SHARE_SUBDIR

    def events(self):
        return [json.loads(p[1]) for p in self.rig.client.published_to(EVENT)]

    def test_nothing_is_exported_without_a_press(self):
        (self.data / "capture").mkdir()
        (self.data / "capture" / "capture-20270115-08.jsonl").write_text("{}\n")
        (self.data / "state.json").write_text("{}")
        self.rig.run(30)
        self.assertFalse(self.base.exists())
        self.assertEqual(self.layer.debug_exports, 0)

    def test_a_press_exports_counts_and_logs_numbers_only(self):
        (self.data / "state.json").write_text('{"zones":[{"x":1}]}')
        with self.assertLogs("radar_zone_learner.p7", level="INFO") as cm:
            self.ha.press(EXPORT_BTN)
        folders = [p for p in self.base.iterdir() if EXPORT_RE.match(p.name)]
        self.assertEqual(len(folders), 1)
        self.assertEqual((folders[0] / "state.json").read_text(), '{"zones":[{"x":1}]}')
        self.assertEqual(self.layer.debug_exports, 1)
        self.assertEqual(self.layer.attributes()["debug_exports"], 1)
        self.assertEqual(self.layer.attributes()["debug_export_refused"], 0)
        self.assertEqual(self.events(), [])
        text = " ".join(r.getMessage() for r in cm.records)
        self.assertRegex(text, r"debug export: \d+ files, \d+ bytes")
        self.assertNotIn(str(self.base), text)
        self.assertNotIn("Desk", text)

    def test_a_refused_press_is_counted_and_fires_an_event(self):
        self.ha.press(EXPORT_BTN)
        self.assertEqual((self.layer.debug_exports, self.layer.debug_export_refused), (0, 1))
        self.assertEqual(self.events(), [{"reason": "empty", "event_type": "export_refused"}])
        self.assertFalse(self.base.exists())

    def test_the_event_type_is_in_the_discovery_config_of_the_review_event(self):
        cfg = [json.loads(p) for t, p in self.layer.discovery() if t.endswith("radar_zones_review_event/config")][0]
        self.assertIn("export_refused", cfg["event_types"])
        self.assertEqual(len(cfg["event_types"]), 7)

    def test_the_layer_without_a_data_folder_refuses(self):
        from radar_zone_learner.shell.p7 import P7Layer
        layer = P7Layer(self.layer.topics, lambda *a, **k: True, None, share_root=self.rig.share_root)
        layer.on_event(ExportDebugPress(1), 1000)
        self.assertEqual((layer.debug_exports, layer.debug_export_refused), (0, 1))
        self.assertFalse(self.base.exists())

    def test_two_presses_in_one_second_are_one_export_and_one_refusal(self):
        (self.data / "state.json").write_text("{}")
        self.layer.on_event(ExportDebugPress(1), 1_800_000_000_000)
        self.layer.on_event(ExportDebugPress(2), 1_800_000_000_500)
        self.assertEqual((self.layer.debug_exports, self.layer.debug_export_refused), (1, 1))
        self.assertEqual(self.events(), [{"reason": "exists", "event_type": "export_refused"}])

    def test_the_default_share_root_is_the_constant(self):
        from radar_zone_learner.shell.p7 import P7Layer
        layer = P7Layer(self.layer.topics, lambda *a, **k: True, None)
        self.assertEqual(layer.share_root, DX.SHARE_ROOT)

    def test_forget_all_does_not_touch_an_export(self):
        (self.data / "state.json").write_text("{}")
        self.ha.press(EXPORT_BTN)
        before = sorted(p.name for p in self.base.iterdir())
        self.layer.purge_capture()
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
