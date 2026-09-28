"""``GET /health`` — process + database readiness (spec §5).

Returns 200 only if SQLite accepts a real write (the ``health_probe`` row is
updated and committed). Exposes no configuration or message data, and does
not check Telegram (that belongs in the logs).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.clock import Clock
from app.db import queries
from app.db.connection import Database
from app.logs import get_logger, log_event
from app.state import AppState

log = get_logger(__name__)
router = APIRouter()


def probe_database(db: Database, clock: Clock) -> None:
    """Raise ``sqlite3.Error`` unless a read/write round trip succeeds."""
    with db.transaction() as conn:
        queries.get_next_update_id(conn)  # read
        queries.touch_health_probe(conn, clock.now_ts())  # write


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    state: AppState = request.app.state.sb
    try:
        await asyncio.to_thread(probe_database, state.db, state.clock)
    except (sqlite3.Error, OSError) as exc:
        log_event(log, logging.ERROR, "health.failed", error_code=type(exc).__name__)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})
