"""In-memory token bucket rate limiting (spec §9: "modest rate limiting").

State lives in process memory, which is fine because the MVP runs exactly
one process. Limits reset on restart.
"""

from __future__ import annotations

import math
import threading

from app.clock import Clock


class TokenBucket:
    """Classic token bucket: ``rate_per_minute`` refill, up to ``burst`` tokens."""

    def __init__(self, rate_per_minute: float, burst: int, clock: Clock) -> None:
        self._rate = rate_per_minute / 60.0  # tokens per second
        self._burst = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._updated = clock.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock.monotonic()
        self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    def try_acquire(self) -> tuple[bool, int]:
        """Take one token. Returns (allowed, seconds_until_next_token)."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True, 0
            wait = (1.0 - self._tokens) / self._rate if self._rate > 0 else 60.0
            return False, max(1, math.ceil(wait))


class KeyedRateLimiter:
    """One :class:`TokenBucket` per key (e.g. per producer source)."""

    def __init__(self, rate_per_minute: float, burst: int, clock: Clock) -> None:
        self._rate = rate_per_minute
        self._burst = burst
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def try_acquire(self, key: str) -> tuple[bool, int]:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self._rate, self._burst, self._clock)
                self._buckets[key] = bucket
        return bucket.try_acquire()
