"""Shared runtime objects, attached to the FastAPI app as ``app.state.sb``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.auth import ProducerAuth
from app.clock import Clock
from app.config import Settings
from app.db.connection import Database
from app.ratelimit import KeyedRateLimiter


@dataclass
class AppState:
    settings: Settings
    db: Database
    clock: Clock
    auth: ProducerAuth
    api_limiter: KeyedRateLimiter
    # Called after a message is committed so the delivery worker wakes up now
    # instead of at its next poll. Replaced by the lifespan once the worker runs.
    wake_worker: Callable[[], None] = field(default=lambda: None)
