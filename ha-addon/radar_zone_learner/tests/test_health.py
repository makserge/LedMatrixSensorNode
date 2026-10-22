import socket
import unittest

from radar_zone_learner.shell.health import HealthServer, StallWatchdog


class HealthServerTest(unittest.TestCase):
    def test_port_accepts_and_closes(self):
        server = HealthServer(port=0, host="127.0.0.1")
        server.start()
        try:
            self.assertNotEqual(server.port, 0)
            for _ in range(3):
                with socket.create_connection(("127.0.0.1", server.port), timeout=5) as conn:
                    conn.settimeout(5)
                    self.assertEqual(conn.recv(16), b"")        # closed by the server at once
        finally:
            server.stop()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", server.port), timeout=1).close()

    def test_stop_twice(self):
        server = HealthServer(port=0, host="127.0.0.1")
        server.start()
        server.stop()
        server.stop()


class StallWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.exits = []
        self.wd = StallWatchdog(limit_s=120.0, check_every_s=10.0, monotonic=lambda: self.now[0],
                                exit_fn=self.exits.append)

    def test_exit_after_120_s_and_not_before(self):
        self.wd.beat()
        for t in (0.0, 60.0, 119.9, 120.0):
            self.now[0] = 100.0 + t
            self.assertTrue(self.wd.check_once(), t)
        self.assertEqual(self.exits, [])
        self.now[0] = 100.0 + 120.1
        with self.assertLogs("radar_zone_learner.watchdog", level="CRITICAL") as logs:
            self.assertFalse(self.wd.check_once())
        self.assertEqual(self.exits, [70])
        self.assertIn("stall", logs.output[0])

    def test_beat_resets(self):
        for _ in range(10):
            self.now[0] += 100.0
            self.wd.beat()
            self.assertTrue(self.wd.check_once())
        self.assertEqual(self.exits, [])

    def test_thread_start_stop(self):
        wd = StallWatchdog(limit_s=120.0, check_every_s=0.01, exit_fn=self.exits.append)
        wd.start()
        wd.beat()
        wd.stop()
        self.assertEqual(self.exits, [])


if __name__ == "__main__":
    unittest.main()
