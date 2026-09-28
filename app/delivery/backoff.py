"""Retry delay computation (spec §8).

Exponential backoff: 5 s, 10 s, 20 s ... capped at 15 minutes, with jitter so
retries after an outage do not arrive in lockstep. Telegram's ``retry_after``
is a floor when supplied.
"""

from __future__ import annotations

import random

BASE_DELAY_S = 5.0
MAX_DELAY_S = 15 * 60.0
MAX_ATTEMPTS = 10


def base_delay(attempt: int) -> float:
    """Un-jittered delay after the ``attempt``-th failed attempt (1-based)."""
    exponent = max(attempt, 1) - 1
    # Cap the exponent too, so huge attempt counts cannot overflow.
    return min(BASE_DELAY_S * (2 ** min(exponent, 20)), MAX_DELAY_S)


def retry_delay(
    attempt: int, retry_after: float | None = None, rng: random.Random | None = None
) -> float:
    """Jittered delay in seconds: uniform in [base/2, base], at least ``retry_after``."""
    rng = rng or random  # type: ignore[assignment]
    base = base_delay(attempt)
    delay = rng.uniform(base / 2, base)
    if retry_after is not None and retry_after > 0:
        delay = max(delay, float(retry_after))
    return delay
