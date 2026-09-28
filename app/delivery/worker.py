"""Outbound delivery worker (spec §8).

One worker per process. Each delivery is three separate steps, so no SQLite
transaction is ever open during a network call:

1. **Claim** (transaction): pick the oldest due row, set ``sending``, take a
   lease, count the attempt.
2. **Send** (no transaction): Telegram ``sendMessage``.
3. **Record** (transaction): ``sent`` / ``retry`` / ``dead``.

If the process dies between 1 and 3 the row stays ``sending`` until its lease
expires, then it is claimed again. That is the documented at-least-once
window: if Telegram had already accepted the message, the owner sees it twice.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time
from dataclasses import dataclass

from app.clock import Clock, parse_ts
from app.db import queries
from app.db.connection import Database
from app.delivery.backoff import MAX_ATTEMPTS, MAX_DELAY_S, retry_delay
from app.delivery.formatting import format_outbound
from app.logs import get_logger, log_event
from app.telegram.client import TelegramClient, TelegramResult

log = get_logger(__name__)

LEASE_SECONDS = 60.0
IDLE_POLL_SECONDS = 5.0
# How long to stop all sending when Telegram rejects the bot token itself.
AUTH_FAILURE_PAUSE_SECONDS = MAX_DELAY_S


@dataclass(frozen=True)
class Claimed:
    id: str
    attempts: int
    text: str
    source: str
    kind: str


class DeliveryWorker:
    def __init__(self, db: Database, telegram: TelegramClient, chat_id: int, clock: Clock,
                 rng: random.Random | None = None) -> None:
        self._db = db
        self._telegram = telegram
        self._chat_id = chat_id
        self._clock = clock
        self._rng = rng or random.Random()
        self._wake = asyncio.Event()
        # Monotonic time before which nothing is sent (429 / auth failure).
        self._paused_until = 0.0

    def wake(self) -> None:
        """Signal that new work may be due (called after an enqueue commit)."""
        self._wake.set()

    # --- Loop -------------------------------------------------------------

    async def run(self) -> None:
        log_event(log, logging.INFO, "delivery.worker_started")
        while True:
            try:
                pause = self._paused_until - self._clock.monotonic()
                if pause > 0:
                    await asyncio.sleep(min(pause, IDLE_POLL_SECONDS))
                    continue
                # Clear before looking for work: a wake() that arrives while we
                # process is then seen by the wait below, never lost.
                self._wake.clear()
                if await self.process_one():
                    continue
                await self._idle_wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the worker must never die
                log_event(log, logging.ERROR, "delivery.worker_error",
                          error_code=type(exc).__name__)
                await asyncio.sleep(1.0)

    async def _idle_wait(self) -> None:
        """Sleep until woken, the next row is due, or the idle poll interval."""
        timeout = IDLE_POLL_SECONDS
        next_ts = await asyncio.to_thread(self._next_wakeup)
        if next_ts is not None:
            due_in = (parse_ts(next_ts) - self._clock.now()).total_seconds()
            timeout = max(0.05, min(timeout, due_in))
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except TimeoutError:
            pass

    def _next_wakeup(self) -> str | None:
        with self._db.reader() as conn:
            return queries.next_wakeup_time(conn)

    # --- One delivery -----------------------------------------------------

    async def process_one(self) -> bool:
        """Deliver at most one due message. Returns False if nothing was due."""
        claimed = await asyncio.to_thread(self._claim)
        if claimed is None:
            return False

        if claimed.attempts > MAX_ATTEMPTS:
            # A row whose lease expired after its final attempt (crash during
            # the last send): stop here rather than exceed the attempt budget.
            await asyncio.to_thread(self._record_dead, claimed, "retries_exhausted")
            return True

        started = time.monotonic()
        result = await self._telegram.send_message(self._chat_id, claimed.text)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        await asyncio.to_thread(self._record, claimed, result, elapsed_ms)
        return True

    def _claim(self) -> Claimed | None:
        with self._db.transaction() as conn:
            row = queries.select_next_due(conn, self._clock.now_ts())
            if row is None:
                return None
            queries.mark_sending(conn, row["id"], self._clock.now_ts(LEASE_SECONDS))
            return Claimed(
                id=row["id"],
                attempts=row["attempts"] + 1,
                text=format_outbound(row),
                source=row["source"],
                kind=row["kind"],
            )

    def _record_dead(self, claimed: Claimed, error_code: str) -> None:
        with self._db.transaction() as conn:
            queries.mark_dead(conn, claimed.id, error_code)
        log_event(log, logging.ERROR, "delivery.dead", message_id=claimed.id,
                  source=claimed.source, transition="sending->dead", attempts=claimed.attempts,
                  error_code=error_code)

    def _record(self, claimed: Claimed, result: TelegramResult, elapsed_ms: float) -> None:
        common = {"message_id": claimed.id, "source": claimed.source,
                  "attempts": claimed.attempts, "elapsed_ms": elapsed_ms}

        if result.outcome == "ok":
            telegram_message_id = None
            if isinstance(result.data, dict) and isinstance(result.data.get("message_id"), int):
                telegram_message_id = result.data["message_id"]
            with self._db.transaction() as conn:
                queries.mark_sent(conn, claimed.id, self._chat_id, telegram_message_id,
                                  self._clock.now_ts())
            log_event(log, logging.INFO, "delivery.sent", transition="sending->sent", **common)
            return

        error_code = result.error_code or result.outcome

        if result.outcome == "permanent":
            self._record_dead(claimed, error_code)
            return

        if result.outcome == "auth_failed":
            # The bot token is rejected: every message would fail the same way.
            # Do not burn this message's attempts or dead-letter the queue;
            # pause all delivery and keep retrying at the maximum interval.
            self._paused_until = self._clock.monotonic() + AUTH_FAILURE_PAUSE_SECONDS
            with self._db.transaction() as conn:
                queries.mark_retry(conn, claimed.id,
                                   self._clock.now_ts(AUTH_FAILURE_PAUSE_SECONDS),
                                   error_code, refund_attempt=True)
            log_event(log, logging.ERROR, "telegram.auth_failed", transition="sending->retry",
                      error_code=error_code, **common)
            return

        # transient or rate_limited
        if claimed.attempts >= MAX_ATTEMPTS:
            self._record_dead(claimed, "retries_exhausted")
            return
        delay = retry_delay(claimed.attempts, result.retry_after, self._rng)
        if result.outcome == "rate_limited" and result.retry_after:
            # Telegram's limit is per bot: hold back *all* sends, not just this row.
            self._paused_until = self._clock.monotonic() + result.retry_after
        with self._db.transaction() as conn:
            queries.mark_retry(conn, claimed.id, self._clock.now_ts(delay), error_code)
        log_event(log, logging.WARNING, "delivery.retry", transition="sending->retry",
                  error_code=error_code, retry_in_s=round(delay, 1), **common)


def log_queue_stats(db: Database) -> None:
    """Emit queue depth and dead-letter count (spec §9)."""
    try:
        with db.reader() as conn:
            counts = queries.outbound_status_counts(conn)
    except sqlite3.Error as exc:
        log_event(log, logging.ERROR, "queue.stats_failed", error_code=type(exc).__name__)
        return
    log_event(log, logging.INFO, "queue.stats",
              queued=counts["pending"] + counts["retry"] + counts["sending"],
              pending=counts["pending"], retry=counts["retry"], sending=counts["sending"],
              dead=counts["dead"], sent=counts["sent"])
