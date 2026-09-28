"""Tiny standalone test service (spec §10).

Exposes only ``GET /health``. No Telegram dependency, no mutating routes.

Environment:
    TEST_SERVICE_TOKEN    optional; if set, requests must send ``Bearer <token>``
    TEST_SERVICE_NAME     name reported in the health body (default ``test-service``)
    TEST_SERVICE_DELAY_S  artificial delay before answering, to exercise timeouts

Run:
    uvicorn test_service.main:app --host 127.0.0.1 --port 8081
"""

from __future__ import annotations

import asyncio
import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def create_app(
    token: str | None = None, name: str = "test-service", delay_s: float = 0.0
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        if token:
            presented = request.headers.get("authorization", "")
            if not hmac.compare_digest(presented.encode(), f"Bearer {token}".encode()):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        return JSONResponse({"status": "ok", "name": name})

    return app


def _app_from_env() -> FastAPI:
    return create_app(
        token=os.environ.get("TEST_SERVICE_TOKEN") or None,
        name=os.environ.get("TEST_SERVICE_NAME", "test-service"),
        delay_s=float(os.environ.get("TEST_SERVICE_DELAY_S", "0") or 0),
    )


app = _app_from_env()
