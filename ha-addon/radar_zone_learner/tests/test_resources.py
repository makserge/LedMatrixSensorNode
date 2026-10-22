import unittest
from unittest import mock

from radar_zone_learner.shell import resources
from radar_zone_learner.shell.resources import CpuMeter, peak_rss_mb, rss_mb
from tests._tmp import tmpdir


class ResourcesTest(unittest.TestCase):
    def test_rss_is_float_or_none(self):
        for fn in (rss_mb, peak_rss_mb):
            value = fn()
            self.assertTrue(value is None or (isinstance(value, float) and 0 < value < 100_000), value)

    def test_proc_status_parser(self):
        with tmpdir() as d:
            path = f"{d}/status"
            with open(path, "w", encoding="ascii") as f:
                f.write("Name:\tpython3\nVmHWM:\t   30720 kB\nVmRSS:\t   24576 kB\nThreads:\t3\n")
            self.assertEqual(resources._proc_status_kb("VmRSS", path), 24576.0)
            self.assertEqual(resources._proc_status_kb("VmHWM", path), 30720.0)
            self.assertIsNone(resources._proc_status_kb("VmSwap", path))
        self.assertIsNone(resources._proc_status_kb("VmRSS", "/nonexistent/status"))

    def test_linux_path(self):
        with mock.patch.object(resources.os, "name", "posix"), \
                mock.patch.object(resources, "_proc_status_kb", return_value=24576.0):
            self.assertEqual(rss_mb(), 24.0)
            self.assertEqual(peak_rss_mb(), 24.0)
        with mock.patch.object(resources.os, "name", "posix"), \
                mock.patch.object(resources, "_proc_status_kb", return_value=None):
            self.assertIsNone(rss_mb())


class CpuMeterTest(unittest.TestCase):
    def setUp(self):
        self.mono = [0.0]
        self.cpu = [0.0]
        self.meter = CpuMeter(process_time=lambda: self.cpu[0], monotonic=lambda: self.mono[0])

    def step(self, seconds, cpu_seconds):
        self.mono[0] += seconds
        self.cpu[0] += cpu_seconds
        self.meter.sample()

    def test_needs_two_samples(self):
        self.assertIsNone(self.meter.percent_1h())
        self.meter.sample()
        self.assertIsNone(self.meter.percent_1h())

    def test_share_before_one_hour(self):
        self.meter.sample()
        self.step(60, 0.6)
        self.assertAlmostEqual(self.meter.percent_1h(), 1.0)

    def test_sliding_window(self):
        self.meter.sample()
        for _ in range(60):                      # first hour: 2% load
            self.step(60, 1.2)
        self.assertAlmostEqual(self.meter.percent_1h(), 2.0)
        for _ in range(60):                      # second hour: 0.1% load
            self.step(60, 0.06)
        self.assertAlmostEqual(self.meter.percent_1h(), 0.1)
        self.assertLessEqual(len(self.meter._samples), 62)

    def test_window_spans_at_least_an_hour(self):
        self.meter.sample()
        for _ in range(90):
            self.step(60, 0.6)
        m0 = self.meter._samples[0][0]
        self.assertLessEqual(m0, self.mono[0] - 3600)
        self.assertAlmostEqual(self.meter.percent_1h(), 1.0)


if __name__ == "__main__":
    unittest.main()
