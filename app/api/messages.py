"""``POST /api/v1/messages`` — authenticated, durable event enqueue (spec §5).

Order of checks: authentication (401) → rate limit (429) → body size (413)
→ content type (415) → JSON/schema validation (422) → idempotency (409) →
commit → 202. A 202 is returned only after the SQLite transaction commits;
it means "durably queued", not "delivered to Telegram".
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.models import MessageIn, valid_idempotency_key
from app.clock import Clock
from app.db import queries
from app.db.connection import Database
from app.logs import get_logger, log_event
from app.state import AppState

log = get_logger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 16 * 1024


@dataclass(frozen=True)
class EnqueueOutcome:
    message_id: str
    status: str
    replayed: bool  # True when an Idempotency-Key matched an earlier request


class IdempotencyConflict(Exception):
    """Same Idempotency-Key, different validated content."""


def enqueue_event(
    db: Database, clock: Clock, source: str, event: MessageIn, idempotency_key: str | None
) -> EnqueueOutcome:
    """Persist one outbound event (and its idempotency key) in one transaction.

    ``BEGIN IMMEDIATE`` serializes writers, so the check-then-insert on the
    idempotency key cannot race with a concurrent identical request.
    """
    request_hash = event.request_hash()
    with db.transaction() as conn:
        if idempotency_key is not None:
            existing = queries.get_idempotency_record(conn, source, idempotency_key)
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict
                return EnqueueOutcome(existing["message_id"], existing["status"], True)

        now = clock.now_ts()
        message_id = str(uuid.uuid4())
        queries.insert_message(
            conn,
            id=message_id,
            direction="outbound",
            source=source,
            destination=event.destination,
            kind=event.kind,
            topic=event.topic,
            severity=event.severity,
            title=event.title,
            body=event.message,
            correlation_id=event.correlation_id,
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
        if idempotency_key is not None:
            queries.insert_idempotency_record(
                conn, source, idempotency_key, request_hash, message_id, now
            )
    return EnqueueOutcome(message_id, "pending", False)


def _error(status_code: int, error: str, **extra: object) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=status_code)


async def _read_limited_body(request: Request, limit: int) -> bytes | None:
    """Read the body, or return None as soon as it exceeds ``limit`` bytes.

    Checks Content-Length first, then counts streamed bytes, so chunked
    uploads without a length are bounded too.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/api/v1/messages")
async def post_message(request: Request) -> JSONResponse:
    state: AppState = request.app.state.sb
    request_id = uuid.uuid4().hex
    started = time.monotonic()

    source = state.auth.authenticate(request.headers.get("authorization"))
    if source is None:
        log_event(log, logging.WARNING, "api.rejected", request_id=request_id,
                  error_code="unauthorized")
        return JSONResponse(
            {"error": "unauthorized"}, status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    allowed, retry_after = state.api_limiter.try_acquire(source)
    if not allowed:
        log_event(log, logging.WARNING, "api.rejected", request_id=request_id, source=source,
                  error_code="rate_limited")
        return JSONResponse(
            {"error": "rate_limited"}, status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    body = await _read_limited_body(request, MAX_BODY_BYTES)
    if body is None:
        log_event(log, logging.WARNING, "api.rejected", request_id=request_id, source=source,
                  error_code="payload_too_large")
        return _error(413, "payload_too_large")

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        log_event(log, logging.WARNING, "api.rejected", request_id=request_id, source=source,
                  error_code="unsupported_media_type")
        return _error(415, "unsupported_media_type")

    idempotency_key = request.headers.get("idempotency-key")
    if idempotency_key is not None and not valid_idempotency_key(idempotency_key):
        log_event(log, logging.WARNING, "api.rejected", request_id=request_id, source=source,
                  error_code="invalid_idempotency_key")
        return _error(422, "invalid_payload",
                      detail=[{"field": "Idempotency-Key", "error": "invalid"}])

    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        log_event(log, logging.WARNING, "api.rejected", request_id=request_id, source=source,
                  error_code="invalid_json")
        return _error(422, "invalid_payload", detail=[{"field": "", "error": "invalid_json"}])
    if not isinstance(data, dict):
        return _error(422, "invalid_payload", detail=[{"field": "", "error": "not_an_object"}])

    try:
        event = MessageIn.model_validate(data)
    except ValidationError as exc:
        # Report field names and error types only — never echo input values.
        detail = [
            {"field": ".".join(str(p) for p in err["loc"]), "error": err["type"]}
            for err in exc.errors(include_url=False, include_input=False)
        ][:20]
        log_event(log, logging.WARNING, "api.rejected", request_id=request_id, source=source,
                  error_code="invalid_payload")
        return _error(422, "invalid_payload", detail=detail)

    try:
        outcome = await asyncio.to_thread(
            enqueue_event, state.db, state.clock, source, event, idempotency_key
        )
    except IdempotencyConflict:
        log_event(log, logging.WARNING, "api.rejected", request_id=request_id, source=source,
                  error_code="idempotency_conflict")
        return _error(409, "idempotency_conflict")
    except sqlite3.Error as exc:
        log_event(log, logging.ERROR, "api.db_unavailable", request_id=request_id,
                  source=source, error_code=type(exc).__name__)
        return _error(503, "database_unavailable")

    if not outcome.replayed:
        state.wake_worker()
    log_event(
        log, logging.INFO, "api.enqueued", request_id=request_id, message_id=outcome.message_id,
        source=source, topic=event.topic, severity=event.severity,
        transition="new->pending" if not outcome.replayed else "replay",
        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
    )
    return JSONResponse({"id": outcome.message_id, "status": outcome.status}, status_code=202)
