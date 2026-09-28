"""FastAPI application factory and lifespan (spec §2).

The lifespan owns every background task: the delivery worker, the Telegram
poller and a periodic queue-stats logger. They start after the database is
initialized and are cancelled cleanly on shutdown. The process holds an
exclusive lock file next to the database, so a second instance on the same
data fails fast instead of double-polling Telegram or double-sending.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import IO

import httpx
from fastapi import FastAPI

from app.api import health, messages
from app.auth import ProducerAuth
from app.clock import Clock
from app.config import Settings
from app.db.connection import Database
from app.delivery.worker import DeliveryWorker, log_queue_stats
from app.logs import get_logger, log_event
from app.ratelimit import KeyedRateLimiter
from app.services.test_service import TestServiceClient
from app.state import AppState
from app.telegram.client import TelegramClient
from app.telegram.poller import TelegramPoller

log = get_logger(__name__)

API_RATE_PER_MINUTE = 60
API_BURST = 20
QUEUE_STATS_INTERVAL_S = 60.0


class InstanceLockError(RuntimeError):
    """Another Switchboard process already owns this database."""


def _acquire_instance_lock(settings: Settings) -> IO[str]:
    lock_path = settings.database_path.with_name(settings.database_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 - held for process life
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise InstanceLockError(
            f"another Switchboard instance holds {lock_path}; only one instance is supported"
        ) from exc
    return handle


async def _stats_loop(db: Database) -> None:
    while True:
        await asyncio.to_thread(log_queue_stats, db)
        await asyncio.sleep(QUEUE_STATS_INTERVAL_S)


def create_app(settings: Settings, *, clock: Clock | None = None,
               start_background: bool = True) -> FastAPI:
    """Build the app. ``start_background=False`` serves only the HTTP API (tests)."""
    clock = clock or Clock()
    db = Database(settings.database_path)
    state = AppState(
        settings=settings,
        db=db,
        clock=clock,
        auth=ProducerAuth(settings.producer_keys),
        api_limiter=KeyedRateLimiter(API_RATE_PER_MINUTE, API_BURST, clock),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lock_handle = _acquire_instance_lock(settings) if start_background else None
        try:
            await asyncio.to_thread(db.initialize)
        except BaseException:
            if lock_handle is not None:
                lock_handle.close()
            raise
        if not settings.bind_is_loopback():
            log_event(log, logging.WARNING, "config.non_loopback_bind",
                      detail="API is reachable beyond loopback; require a trusted network "
                             "boundary and TLS")
        if not settings.telegram_api_is_https():
            # Plain HTTP is for tests and local Bot API servers only: the bot
            # token travels in the URL path. Never log the URL itself.
            log_event(log, logging.WARNING, "config.insecure_telegram_api_base",
                      detail="SWITCHBOARD_TELEGRAM_API_BASE is not https")

        tasks: list[asyncio.Task[None]] = []
        clients: list[httpx.AsyncClient] = []
        if start_background:
            telegram_http = httpx.AsyncClient(follow_redirects=False)
            service_http = httpx.AsyncClient(follow_redirects=False)
            clients += [telegram_http, service_http]
            telegram = TelegramClient(settings.bot_token, telegram_http,
                                      settings.telegram_api_base)
            worker = DeliveryWorker(db, telegram, settings.telegram_chat_id, clock)
            state.wake_worker = worker.wake
            poller = TelegramPoller(
                db, telegram,
                TestServiceClient(settings.test_service_url, settings.test_service_token,
                                  service_http),
                settings, clock, worker.wake, clock.monotonic(),
            )
            tasks = [
                asyncio.create_task(worker.run(), name="delivery-worker"),
                asyncio.create_task(poller.run(), name="telegram-poller"),
                asyncio.create_task(_stats_loop(db), name="queue-stats"),
            ]
        log_event(log, logging.INFO, "switchboard.started", bind_host=settings.bind_host,
                  bind_port=settings.bind_port, background=start_background)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for client in clients:
                await client.aclose()
            if lock_handle is not None:
                lock_handle.close()
            log_event(log, logging.INFO, "switchboard.stopped")

    app = FastAPI(
        title="Switchboard", lifespan=lifespan,
        # No interactive docs or schema endpoint: nothing extra to expose.
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.state.sb = state
    app.include_router(messages.router)
    app.include_router(health.router)
    return app
