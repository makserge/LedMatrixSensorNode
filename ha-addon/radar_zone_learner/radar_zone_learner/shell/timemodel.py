"""Local time from the Supervisor's TZ (spec 4.4): a ``TimeModel`` for the core."""
from __future__ import annotations

import datetime as _dt
import logging

from ..core.types import LocalTime

log = logging.getLogger("radar_zone_learner.time")


class ZoneInfoTime:
    """``TimeModel`` on ``zoneinfo``; an unknown or missing zone falls back to UTC with a warning."""

    def __init__(self, tz_name: str = "UTC") -> None:
        self.name = tz_name or "UTC"
        self.tz: _dt.tzinfo = _dt.timezone.utc
        try:
            import zoneinfo
            self.tz = zoneinfo.ZoneInfo(self.name)
        except Exception as exc:      # ZoneInfoNotFoundError, ValueError for a bad key, no tz database
            if self.name != "UTC":
                log.warning("time zone %r not available (%s): using UTC", self.name, exc.__class__.__name__)
            self.name = "UTC"
            self.tz = _dt.timezone.utc

    def local(self, epoch_ms: int) -> LocalTime:
        d = _dt.datetime.fromtimestamp(epoch_ms // 1000, self.tz)
        return LocalTime(d.toordinal(), d.weekday(), d.hour * 3600 + d.minute * 60 + d.second)

    def __repr__(self) -> str:
        return f"ZoneInfoTime({self.name!r})"
