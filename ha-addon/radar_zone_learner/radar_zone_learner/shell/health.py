"""TCP health port for the Supervisor watchdog and the 120 s stall self-exit (spec 10.4, OA 6.3, 7.1).

The Supervisor's ``tcp://[HOST]:[PORT:8099]`` watchdog only checks that the port accepts a connection;
the handler closes every connection at once. ``StallWatchdog`` ends the process with exit code 70 when
the worker loop has not called ``beat()`` for more than ``limit_s`` seconds; the Supervisor then
restarts the app. It does not save first: the state may be inconsistent, the last good save stays.
"""
from __future__ import annotations

import logging
import os
import socketserver
import threading
import time
from typing import Callable, Optional

EXIT_STALL = 70

log = logging.getLogger("radar_zone_learner.health")


class _CloseHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        pass                                   # returning closes the connection


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


class HealthServer:
    def __init__(self, port: int = 8099, host: str = "0.0.0.0") -> None:
        self.host = host
        self.port = port
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Binds and serves in a daemon thread; raises ``OSError`` if the port cannot be bound."""
        self._server = _Server((self.host, self.port), _CloseHandler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.5},
                                        name="rzl-health", daemon=True)
        self._thread.start()
        log.info("health port %d open", self.port)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


class StallWatchdog:
    def __init__(self, limit_s: float = 120.0, check_every_s: float = 10.0,
                 monotonic: Callable[[], float] = time.monotonic, exit_fn: Callable[[int], None] = os._exit,
                 log: logging.Logger = logging.getLogger("radar_zone_learner.watchdog")) -> None:
        self.limit_s = limit_s
        self.check_every_s = check_every_s
        self._monotonic = monotonic
        self._exit_fn = exit_fn
        self._log = log
        self._last = monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def beat(self) -> None:
        self._last = self._monotonic()

    def check_once(self) -> bool:
        """True while healthy; on a stall logs CRITICAL and calls ``exit_fn(70)``."""
        age = self._monotonic() - self._last
        if age <= self.limit_s:
            return True
        self._log.critical("stall: worker loop has not run for %.0f s (limit %.0f s); exiting", age, self.limit_s)
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass
        self._exit_fn(EXIT_STALL)
        return False

    def start(self) -> None:
        self.beat()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rzl-watchdog", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.check_every_s):
            self.check_once()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
