"""Time source abstraction.

All code that needs the current time goes through a ``Clock`` so tests can
control time deterministically (backoff, leases, uptime).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

# Fixed-width UTC format. Because every stored timestamp uses exactly this
# layout, lexicographic comparison in SQL equals chronological comparison.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def format_ts(value: datetime) -> str:
    """Render an aware datetime as the canonical UTC ISO 8601 string."""
    return value.astimezone(UTC).strftime(_TS_FORMAT)


def parse_ts(value: str) -> datetime:
    """Parse a timestamp produced by :func:`format_ts`."""
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=UTC)


class Clock:
    """Real wall-clock and monotonic time."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def now_ts(self, offset_seconds: float = 0.0) -> str:
        """Current time (optionally shifted) as a canonical timestamp string."""
        return format_ts(self.now() + timedelta(seconds=offset_seconds))


class FakeClock(Clock):
    """Manually advanced clock for tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds
