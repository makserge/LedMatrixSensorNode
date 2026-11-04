"""Debug capture (spec 7.8): the switch, the file format, the limits, the deletion, Forget all."""
import json
import os
import unittest
from pathlib import Path

from radar_zone_learner.core.types import FramesMessage
from radar_zone_learner.shell.capture import CAPTURE_KEEP_S, CAPTURE_MAX_BYTES, CAPTURE_MAX_ON_S, DebugCapture
from radar_zone_learner.shell.p5 import P5Layer
from radar_zone_learner.shell.p5_types import CaptureSwitch
from replay.capture_replay import read_messages
from tests._tmp import tmpdir
from tests.fakes import FakeClock, FakeLearner, quiet_logs
from tests.p5_harness import P5Harness

FRAMES = "led_matrix_node/radar/ld2454/frames"
CAP_SET = "radar_zones/learner/debug_capture/set"
CAP_STATE = "radar_zones/learner/debug_capture"
HOUR_MS = 3600 * 1000
T0 = 1_767_571_200_000                      # 2026-01-05 00:00:00 UTC


def payload(n):
    return json.dumps({"boot": "0a1b2c3d", "t": n, "frames": [{"x": n, "y": 2 * n}]}).encode()


class CaptureTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self._tmp = tmpdir()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.clock = FakeClock(mono=1000.0, wall_ms=T0)
        self.cap = DebugCapture(self.dir, self.clock.clock_ms, self.clock.monotonic, topic=FRAMES)
        self.folder = self.dir / "capture"

    def files(self):
        return sorted(p.name for p in self.folder.glob("*")) if self.folder.exists() else []

    def read(self, name):
        return list(read_messages(self.folder / name))

    # ------------------------------------------------------------ defaults and the format
    def test_off_by_default_and_frames_are_ignored(self):
        self.assertFalse(self.cap.active)
        self.cap.on_frames(T0, payload(1))
        self.assertEqual(self.files(), [])
        self.assertEqual(self.cap.stats(), {"on": False, "files": 0, "bytes": 0})

    def test_on_writes_one_json_line_per_message_in_the_replay_format(self):
        self.assertTrue(self.cap.start())
        for n in range(3):
            self.cap.on_frames(T0 + n * 1000, payload(n))
        self.cap.stop()
        self.assertEqual(self.files(), ["capture-20260105-00.jsonl"])
        raw = (self.folder / "capture-20260105-00.jsonl").read_bytes().split(b"\n")
        self.assertEqual(raw[-1], b"")
        lines = [json.loads(x) for x in raw[:-1]]
        self.assertEqual(len(lines), 3)
        self.assertEqual(list(lines[0]), ["recv_ms", "topic", "payload"])
        self.assertEqual(lines[1], {"recv_ms": T0 + 1000, "topic": FRAMES, "payload": payload(1).decode()})
        # replay/capture_replay.py reads the same payload bytes back
        got = self.read("capture-20260105-00.jsonl")
        self.assertEqual(got, [(T0 + n * 1000, FRAMES, payload(n)) for n in range(3)])
        self.assertEqual(read_messages.bad, 0)

    def test_a_payload_with_non_ascii_text_round_trips(self):
        text = 'café {"a":1}'
        self.cap.start()
        self.cap.on_frames(T0, text.encode("utf-8"))
        self.cap.stop()
        self.assertEqual(self.read("capture-20260105-00.jsonl")[0][2], text.encode("utf-8"))

    def test_a_payload_that_is_not_utf8_is_skipped_and_counted(self):
        self.cap.start()
        self.cap.on_frames(T0, b"\xff\xfe\x00")
        self.cap.on_frames(T0 + 1, payload(1))
        self.cap.stop()
        self.assertEqual(self.cap.skipped, 1)
        self.assertEqual(len(self.read("capture-20260105-00.jsonl")), 1)

    def test_the_file_name_follows_the_utc_hour(self):
        self.cap.start()
        self.clock.wall_ms = T0 + 23 * HOUR_MS + 59 * 60 * 1000
        self.cap.on_frames(self.clock.wall_ms, payload(1))
        self.clock.wall_ms = T0 + 24 * HOUR_MS + 5000
        self.cap.on_frames(self.clock.wall_ms, payload(2))
        self.cap.stop()
        self.assertEqual(self.files(), ["capture-20260105-23.jsonl", "capture-20260106-00.jsonl"])
        self.assertEqual(self.cap.stats()["files"], 2)

    def test_the_same_hour_appends_also_after_a_stop_and_start(self):
        self.cap.start()
        self.cap.on_frames(T0, payload(1))
        self.cap.stop()
        self.cap.start()
        self.cap.on_frames(T0 + 1, payload(2))
        self.cap.stop()
        self.assertEqual(len(self.read("capture-20260105-00.jsonl")), 2)

    def test_the_file_is_flushed_every_five_seconds(self):
        self.cap.start()
        self.cap.on_frames(T0, payload(1))
        path = self.folder / "capture-20260105-00.jsonl"
        self.clock.advance(4)
        self.cap.tick()
        self.assertEqual(path.stat().st_size, 0)                 # buffered
        self.clock.advance(1.5)
        self.cap.tick()
        self.assertGreater(path.stat().st_size, 0)
        self.cap.stop()

    # ------------------------------------------------------------ limits
    def test_it_switches_itself_off_after_48_hours(self):
        self.assertEqual(CAPTURE_MAX_ON_S, 172800)
        self.cap.start()
        self.cap.on_frames(T0, payload(1))
        self.clock.advance(CAPTURE_MAX_ON_S - 1)
        self.cap.tick()
        self.assertTrue(self.cap.active)
        self.clock.advance(1)
        self.cap.tick()
        self.assertFalse(self.cap.active)
        self.assertEqual(self.cap.stop_reason, "limit_time")
        self.cap.on_frames(T0, payload(2))                       # nothing more is written
        self.assertEqual(len(self.read("capture-20260105-00.jsonl")), 1)

    def test_it_stops_when_the_folder_holds_more_than_the_limit(self):
        self.assertEqual(CAPTURE_MAX_BYTES, 100 * 1024 * 1024)
        cap = DebugCapture(self.dir, self.clock.clock_ms, self.clock.monotonic, topic=FRAMES, max_bytes=2000)
        cap.start()
        for n in range(40):
            cap.on_frames(T0 + n, payload(n))
        cap.tick()
        self.assertFalse(cap.active)
        self.assertEqual(cap.stop_reason, "limit_size")
        self.assertGreater(cap.stats()["bytes"], 2000)

    def test_files_older_than_72_hours_are_deleted_at_start_and_every_hour_also_while_off(self):
        self.assertEqual(CAPTURE_KEEP_S, 259200)
        self.folder.mkdir()
        now_s = T0 / 1000.0
        old = self.folder / "capture-20260101-10.jsonl"
        fresh = self.folder / "capture-20260104-10.jsonl"
        other = self.folder / "notes.txt"
        for p in (old, fresh, other):
            p.write_text("x\n")
        os.utime(old, (now_s - CAPTURE_KEEP_S - 60, now_s - CAPTURE_KEEP_S - 60))
        os.utime(fresh, (now_s - CAPTURE_KEEP_S + 3600, now_s - CAPTURE_KEEP_S + 3600))
        os.utime(other, (now_s - 10 * CAPTURE_KEEP_S, now_s - 10 * CAPTURE_KEEP_S))
        self.cap.tick()                                          # the first call is the one at start
        self.assertEqual(self.files(), ["capture-20260104-10.jsonl", "notes.txt"])     # only capture files, only old ones
        # an hour later (the app is running, the capture is OFF) the fresh file has aged out
        self.clock.advance(3601)
        self.clock.wall_ms += 3601 * 1000
        self.cap.tick()
        self.assertEqual(self.files(), ["notes.txt"])
        self.assertEqual(self.cap.stats()["files"], 0)

    def test_the_clean_runs_only_hourly(self):
        self.folder.mkdir()
        self.cap.tick()
        old = self.folder / "capture-20260101-10.jsonl"
        old.write_text("x\n")
        os.utime(old, (0, 0))
        self.clock.advance(1800)
        self.cap.tick()
        self.assertTrue(old.exists())
        self.clock.advance(1801)
        self.cap.tick()
        self.assertFalse(old.exists())

    def test_the_file_being_written_is_never_deleted(self):
        self.cap.start()
        self.cap.on_frames(T0, payload(1))
        path = self.folder / "capture-20260105-00.jsonl"
        os.utime(path, (0, 0))
        self.clock.advance(3601)
        self.cap.tick()
        self.assertTrue(path.exists())
        self.cap.stop()

    # ------------------------------------------------------------ errors and purge
    def test_a_write_error_switches_it_off_without_raising(self):
        self.cap.start()
        self.cap.on_frames(T0, payload(1))

        class Broken:
            def write(self, data):
                raise OSError("disk full")

            def flush(self):
                pass

            def close(self):
                pass
        real = self.cap._file
        self.addCleanup(real.close)
        self.cap._file = Broken()
        self.cap.on_frames(T0 + 1, payload(2))                  # must not raise
        self.assertFalse(self.cap.active)
        self.assertEqual(self.cap.write_errors, 1)
        self.assertEqual(self.cap.stop_reason, "error")

    def test_a_folder_that_cannot_be_made_keeps_it_off(self):
        (self.dir / "capture").write_text("a file where the folder should be")
        self.assertFalse(self.cap.start())
        self.assertFalse(self.cap.active)
        self.assertEqual(self.cap.write_errors, 1)

    def test_purge_deletes_the_files_and_switches_it_off(self):
        self.cap.start()
        self.cap.on_frames(T0, payload(1))
        (self.folder / "keep.txt").write_text("not a capture")
        self.assertEqual(self.cap.purge(), 1)
        self.assertFalse(self.cap.active)
        self.assertEqual(self.files(), ["keep.txt"])
        self.assertEqual(self.cap.stats(), {"on": False, "files": 0, "bytes": 0})

    def test_stats_count_only(self):
        self.cap.start()
        for n in range(5):
            self.cap.on_frames(T0 + n, payload(n))
        stats = self.cap.stats()
        self.assertEqual(set(stats), {"on", "files", "bytes"})
        self.assertEqual((stats["on"], stats["files"]), (True, 1))
        self.assertGreater(stats["bytes"], 100)
        self.cap.stop()

    def test_no_position_is_logged(self):
        with self.assertLogs("radar_zone_learner.capture", level="DEBUG") as logs:
            self.cap.start()
            self.cap.on_frames(T0, payload(77))
            self.cap.stop()
        joined = " ".join(logs.output)
        self.assertNotIn("77", joined)
        self.assertNotIn("0a1b2c3d", joined)


class CaptureLayerTest(P5Harness, unittest.TestCase):
    def make_learner(self):
        return FakeLearner()

    def test_off_at_every_start_and_a_stale_retained_on_is_overwritten(self):
        self.connect()
        self.assertEqual(self.published(CAP_STATE)[0][1:], (b"OFF", 1, True))
        self.assertFalse(self.layer.capture.active)
        self.assertEqual(self.layer.attributes()["capture"], {"on": False, "files": 0, "bytes": 0})

    def test_the_switch_starts_and_stops_it_and_publishes_the_state(self):
        self.connect()
        self.client.fire_message(CAP_SET, "ON")
        self.pump()
        self.assertTrue(self.layer.capture.active)
        self.assertEqual(self.last(CAP_STATE), "ON")
        self.client.fire_message(FRAMES, payload(1))
        self.client.fire_message(FRAMES, payload(2))
        self.pump()
        self.client.fire_message(CAP_SET, "OFF")
        self.pump()
        self.assertEqual(self.last(CAP_STATE), "OFF")
        files = sorted((self.dir / "capture").glob("capture-*.jsonl"))
        self.assertEqual(len(files), 1)
        got = list(read_messages(files[0]))
        self.assertEqual([g[2] for g in got], [payload(1), payload(2)])
        self.assertEqual({g[1] for g in got}, {FRAMES})
        self.assertEqual(got[0][0], self.clock.clock_ms())

    def test_only_the_frames_topic_is_recorded(self):
        self.connect()
        self.client.fire_message(CAP_SET, "ON")
        self.pump()
        self.client.fire_message("led_matrix_node/sensors/status", "online", retain=True)
        self.client.fire_message("led_matrix_node/radar/schedule/state", "1", retain=True)
        self.client.fire_message("radar_zones/context/guests", "on", retain=True)
        self.client.fire_message("radar_zones/review/answer", json.dumps({"op": "later"}))
        self.client.fire_message("radar_zones/review/name/set", "Desk")
        self.client.fire_message(FRAMES, payload(5))
        self.pump()
        self.layer.capture.stop()
        text = b"".join(p.read_bytes() for p in (self.dir / "capture").glob("capture-*.jsonl")).decode()
        self.assertEqual(len(text.splitlines()), 1)
        for word in ("sensors", "schedule", "guests", "answer", "Desk"):
            self.assertNotIn(word, text)

    def test_the_core_still_gets_every_frames_message(self):
        self.connect()
        self.client.fire_message(CAP_SET, "ON")
        self.client.fire_message(FRAMES, payload(5))
        self.pump()
        self.assertEqual([e.payload for e in self.learner.events if isinstance(e, FramesMessage)], [payload(5)])

    def test_the_self_switch_off_updates_the_retained_state(self):
        self.connect()
        self.client.fire_message(CAP_SET, "ON")
        self.pump()
        self.assertEqual(self.last(CAP_STATE), "ON")
        self.layer.capture.max_on_s = 5
        self.run_for(7)
        self.assertEqual(self.last(CAP_STATE), "OFF")
        self.assertFalse(self.layer.capture.active)

    def test_the_status_attribute_counts_files_and_bytes(self):
        self.connect()
        self.client.fire_message(CAP_SET, "ON")
        self.client.fire_message(FRAMES, payload(5))
        self.pump()
        attr = self.layer.attributes()["capture"]
        self.assertEqual((attr["on"], attr["files"]), (True, 1))
        self.assertGreater(attr["bytes"], 0)

    def test_a_retained_switch_command_is_cleared_and_does_not_start_it(self):
        self.connect()
        self.client.published.clear()
        self.client.fire_message(CAP_SET, "ON", retain=True)
        self.pump()
        self.assertFalse(self.layer.capture.active)
        cleared = [p for p in self.client.published if p[0] == CAP_SET and p[1] == b""]
        self.assertEqual(len(cleared), 1)
        self.assertTrue(cleared[0][3])

    def test_shutdown_stops_it(self):
        self.connect()
        self.client.fire_message(CAP_SET, "ON")
        self.pump()
        self.svc.shutdown(0)
        self.assertFalse(self.layer.capture.active)
        self.assertEqual(self.last(CAP_STATE), "OFF")

    def test_purge_capture_deletes_the_folder_files_and_publishes_off(self):
        self.connect()
        self.client.fire_message(CAP_SET, "ON")
        self.client.fire_message(FRAMES, payload(5))
        self.pump()
        self.assertEqual(self.layer.purge_capture(), 1)
        self.assertEqual(list((self.dir / "capture").glob("capture-*.jsonl")), [])
        self.assertEqual(self.last(CAP_STATE), "OFF")

    def test_no_data_dir_means_no_capture(self):
        layer = P5Layer(self.topics, lambda *a, **k: True, None)
        self.assertIsNone(layer.capture)
        layer.on_event(CaptureSwitch(True, 1), 1)
        self.assertEqual(layer.attributes()["capture"], {"on": False, "files": 0, "bytes": 0})
        self.assertEqual(layer.purge_capture(), 0)

    def test_the_only_new_messages_are_the_switch_state_and_the_status_attribute(self):
        self.connect()
        self.client.published.clear()
        self.client.fire_message(CAP_SET, "ON")
        self.client.fire_message(FRAMES, payload(1))
        self.pump()
        topics = {p[0] for p in self.client.published}
        self.assertEqual(topics, {CAP_STATE})


if __name__ == "__main__":
    unittest.main()
