"""§10 check 7: replaying a Telegram update causes no duplicate service call or reply;
health reports failure when the database is unusable."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import queries
from app.db.connection import Database
from app.telegram.poller import TelegramPoller
from tests.conftest import SERVICE_URL, TG_BOT, make_update, rows


def _service_ok(mock_http: respx.MockRouter) -> respx.Route:
    return mock_http.get(f"{SERVICE_URL}/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "name": "test-service"}))


async def test_replayed_update_runs_once(poller: TelegramPoller, db: Database,
                                         mock_http: respx.MockRouter) -> None:
    service = _service_ok(mock_http)
    update = make_update(10, "/test status")
    await poller.process_update(update)
    await poller.process_update(update)
    assert service.call_count == 1
    assert len(rows(db, "direction = 'inbound'")) == 1
    assert len(rows(db, "direction = 'outbound'")) == 1


async def test_replay_after_restart_runs_once(poller: TelegramPoller, db: Database,
                                              mock_http: respx.MockRouter,
                                              telegram, http_client, settings, clock) -> None:
    from app.services.test_service import TestServiceClient

    service = _service_ok(mock_http)
    update = make_update(10, "/test status")
    await poller.process_update(update)

    restarted = TelegramPoller(db, telegram,
                               TestServiceClient(SERVICE_URL, None, http_client),
                               settings, clock, lambda: None, clock.monotonic())
    await restarted.process_update(update)
    assert service.call_count == 1
    assert len(rows(db, "direction = 'outbound'")) == 1


async def test_crash_between_record_and_reply_is_recovered_once(
        poller: TelegramPoller, db: Database, mock_http: respx.MockRouter) -> None:
    from app.telegram.commands import parse_command

    service = _service_ok(mock_http)
    # Tx A committed, then the process died before the service call / Tx B.
    inbound_id = poller._begin_service_command(20, parse_command("/test status", None))
    assert inbound_id is not None
    assert rows(db, "direction = 'inbound'")[0]["status"] == "processing"

    # Telegram redelivers the update (offset already advanced): skipped.
    await poller.process_update(make_update(20, "/test status"))
    assert service.call_count == 0

    await poller.recover_unfinished()
    await poller.recover_unfinished()  # idempotent
    assert service.call_count == 1
    (inbound,) = rows(db, "direction = 'inbound'")
    assert inbound["status"] == "handled"
    (reply,) = rows(db, "direction = 'outbound'")
    assert reply["correlation_id"] == inbound_id


async def test_poll_loop_advances_offset(poller: TelegramPoller, db: Database,
                                         mock_http: respx.MockRouter) -> None:
    """Run the real loop: second getUpdates must ask for offset = last update_id + 1."""
    mock_http.post(f"{TG_BOT}/getMe").mock(return_value=httpx.Response(
        200, json={"ok": True, "result": {"username": "SwitchboardBot"}}))
    mock_http.post(f"{TG_BOT}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True}))
    offsets: list[int] = []
    second_poll = asyncio.Event()

    async def get_updates(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        offsets.append(offset)
        if len(offsets) == 1:
            return httpx.Response(200, json={"ok": True, "result": [
                make_update(100, "/help"), make_update(101, "/help")]})
        second_poll.set()
        await asyncio.sleep(3600)  # long poll that never returns
        raise AssertionError("unreachable")

    mock_http.post(f"{TG_BOT}/getUpdates").mock(side_effect=get_updates)
    task = asyncio.create_task(poller.run())
    try:
        await asyncio.wait_for(second_poll.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert offsets == [0, 102]
    assert len(rows(db, "direction = 'outbound'")) == 2


# --- health ---------------------------------------------------------------


def test_health_ok(api: TestClient) -> None:
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_fails_when_database_removed(api: TestClient, settings: Settings) -> None:
    for suffix in ("", "-wal", "-shm"):
        settings.database_path.with_name(settings.database_path.name + suffix).unlink(
            missing_ok=True)
    response = api.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores file permissions")
def test_health_fails_when_database_unreadable(api: TestClient, settings: Settings) -> None:
    os.chmod(settings.database_path, 0)
    try:
        assert api.get("/health").status_code == 503
    finally:
        os.chmod(settings.database_path, 0o600)


def test_health_fails_when_database_is_corrupt(api: TestClient, settings: Settings) -> None:
    for suffix in ("-wal", "-shm"):
        settings.database_path.with_name(settings.database_path.name + suffix).unlink(
            missing_ok=True)
    settings.database_path.write_bytes(b"this is not a sqlite database" * 100)
    assert api.get("/health").status_code == 503


def test_health_probe_writes(api: TestClient, db: Database) -> None:
    api.get("/health")
    with db.reader() as conn:
        (checked_at,) = conn.execute("SELECT checked_at FROM health_probe").fetchone()
    assert checked_at is not None
    with db.reader() as conn:
        assert queries.get_next_update_id(conn) == 0


# --- each replay-protection layer on its own ------------------------------


async def test_early_offset_check_skips_before_any_work(poller: TelegramPoller,
                                                        db: Database) -> None:
    await poller.process_update(make_update(30, "/help"))
    tokens_before = poller._limiter._tokens
    await poller.process_update(make_update(30, "/help"))
    # A replay is dropped before it can even consume a rate-limit token.
    assert poller._limiter._tokens == tokens_before


def test_in_transaction_offset_check(poller: TelegramPoller, db: Database) -> None:
    from app.telegram.commands import parse_command

    with db.transaction() as conn:
        queries.advance_next_update_id(conn, 41)
    # Both write paths re-check the offset inside their transaction.
    assert poller._begin_service_command(40, parse_command("/status", None)) is None
    poller._record_local(40, "command", "command.help", "/help", "handled", "reply")
    assert rows(db) == []


async def test_run_loop_recovers_unfinished_command_at_startup(
        poller: TelegramPoller, db: Database, mock_http: respx.MockRouter) -> None:
    from app.telegram.commands import parse_command

    service = _service_ok(mock_http)
    poller._begin_service_command(50, parse_command("/test status", None))  # "crashed"
    mock_http.post(f"{TG_BOT}/getMe").mock(return_value=httpx.Response(
        200, json={"ok": True, "result": {"username": "SwitchboardBot"}}))
    mock_http.post(f"{TG_BOT}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True}))
    polled = asyncio.Event()

    async def get_updates(request: httpx.Request) -> httpx.Response:
        polled.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    mock_http.post(f"{TG_BOT}/getUpdates").mock(side_effect=get_updates)
    task = asyncio.create_task(poller.run())
    try:
        await asyncio.wait_for(polled.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service.call_count == 1
    assert rows(db, "direction = 'inbound'")[0]["status"] == "handled"
    assert len(rows(db, "direction = 'outbound'")) == 1
