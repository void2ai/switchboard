"""§10 check 4: Telegram outage → message stays queued with growing backoff, recovers,
survives restart, and dead-letters after retry exhaustion."""

from __future__ import annotations

import random

import httpx
import respx

from app.api.messages import enqueue_event
from app.api.models import MessageIn
from app.clock import FakeClock, parse_ts
from app.db.connection import Database
from app.delivery.backoff import MAX_ATTEMPTS
from app.delivery.worker import LEASE_SECONDS, DeliveryWorker
from app.telegram.client import TelegramClient
from tests.conftest import OWNER_ID, TG_BOT, rows, sent_ok, valid_event


def _enqueue(db: Database, clock: FakeClock) -> str:
    return enqueue_event(db, clock, "research", MessageIn.model_validate(valid_event()),
                         None).message_id


def _row(db: Database) -> dict:
    (row,) = rows(db)
    return row


async def test_outage_backoff_then_recovery(db: Database, clock: FakeClock,
                                            worker: DeliveryWorker,
                                            mock_http: respx.MockRouter) -> None:
    route = mock_http.post(f"{TG_BOT}/sendMessage")
    _enqueue(db, clock)

    route.mock(return_value=httpx.Response(502))
    gaps = []
    for attempt in range(1, 5):
        assert await worker.process_one() is True
        row = _row(db)
        assert row["status"] == "retry" and row["attempts"] == attempt
        assert row["last_error_code"] == "telegram_502"
        gap = (parse_ts(row["next_attempt_at"]) - clock.now()).total_seconds()
        gaps.append(gap)
        # Not due yet: the worker must not retry early.
        assert await worker.process_one() is False
        clock.advance(gap + 0.001)

    # Delay grows (jittered in [base/2, base] with base 5, 10, 20, 40).
    assert 2.5 <= gaps[0] <= 5 and 20 <= gaps[3] <= 40
    assert gaps[3] > gaps[0]

    route.mock(side_effect=httpx.ConnectTimeout("boom"))
    assert await worker.process_one() is True
    assert _row(db)["last_error_code"] == "network_timeout"
    clock.advance(1000)

    route.mock(return_value=sent_ok(7))
    assert await worker.process_one() is True
    row = _row(db)
    assert row["status"] == "sent" and row["telegram_message_id"] == 7 and row["attempts"] == 6


async def test_retry_after_is_honored_and_pauses_worker(db: Database, clock: FakeClock,
                                                       worker: DeliveryWorker,
                                                       mock_http: respx.MockRouter) -> None:
    mock_http.post(f"{TG_BOT}/sendMessage").mock(return_value=httpx.Response(
        429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 30}}))
    _enqueue(db, clock)
    await worker.process_one()
    row = _row(db)
    assert row["status"] == "retry" and row["last_error_code"] == "telegram_429"
    assert (parse_ts(row["next_attempt_at"]) - clock.now()).total_seconds() >= 30
    assert worker._paused_until >= clock.monotonic() + 30


async def test_restart_reclaims_expired_lease(db: Database, clock: FakeClock,
                                              telegram: TelegramClient,
                                              mock_http: respx.MockRouter) -> None:
    route = mock_http.post(f"{TG_BOT}/sendMessage").mock(return_value=sent_ok(9))
    _enqueue(db, clock)

    crashed = DeliveryWorker(db, telegram, OWNER_ID, clock)
    claimed = crashed._claim()  # claimed, then the process "dies" before sending
    assert claimed is not None and _row(db)["status"] == "sending"

    restarted = DeliveryWorker(db, telegram, OWNER_ID, clock, rng=random.Random(1))
    assert await restarted.process_one() is False  # lease still valid
    clock.advance(LEASE_SECONDS + 1)
    assert await restarted.process_one() is True
    row = _row(db)
    assert row["status"] == "sent" and row["attempts"] == 2
    assert route.call_count == 1


async def test_dead_letter_after_max_attempts(db: Database, clock: FakeClock,
                                              worker: DeliveryWorker,
                                              mock_http: respx.MockRouter) -> None:
    route = mock_http.post(f"{TG_BOT}/sendMessage").mock(return_value=httpx.Response(503))
    _enqueue(db, clock)
    for _ in range(MAX_ATTEMPTS):
        assert await worker.process_one() is True
        clock.advance(1000)
    row = _row(db)
    assert row["status"] == "dead" and row["attempts"] == MAX_ATTEMPTS
    assert row["last_error_code"] == "retries_exhausted"
    assert await worker.process_one() is False
    assert route.call_count == MAX_ATTEMPTS


async def test_expired_lease_after_final_attempt_goes_dead_without_sending(
        db: Database, clock: FakeClock, worker: DeliveryWorker,
        mock_http: respx.MockRouter) -> None:
    route = mock_http.post(f"{TG_BOT}/sendMessage").mock(return_value=sent_ok())
    message_id = _enqueue(db, clock)
    with db.transaction() as conn:
        conn.execute("UPDATE messages SET status='sending', attempts=?, lease_until=? "
                     "WHERE id=?", (MAX_ATTEMPTS, clock.now_ts(-1), message_id))
    assert await worker.process_one() is True
    assert _row(db)["status"] == "dead"
    assert route.call_count == 0


async def test_permanent_error_dead_letters_immediately(db: Database, clock: FakeClock,
                                                        worker: DeliveryWorker,
                                                        mock_http: respx.MockRouter) -> None:
    mock_http.post(f"{TG_BOT}/sendMessage").mock(return_value=httpx.Response(
        400, json={"ok": False, "description": "Bad Request: chat not found"}))
    _enqueue(db, clock)
    await worker.process_one()
    row = _row(db)
    assert row["status"] == "dead" and row["last_error_code"] == "chat_not_found"
    assert row["attempts"] == 1


async def test_bad_bot_token_pauses_without_dead_lettering(db: Database, clock: FakeClock,
                                                          worker: DeliveryWorker,
                                                          mock_http: respx.MockRouter) -> None:
    mock_http.post(f"{TG_BOT}/sendMessage").mock(
        return_value=httpx.Response(401, json={"ok": False}))
    _enqueue(db, clock)
    await worker.process_one()
    row = _row(db)
    assert row["status"] == "retry" and row["attempts"] == 0  # attempt refunded
    assert row["last_error_code"] == "telegram_401"
    assert worker._paused_until > clock.monotonic()
