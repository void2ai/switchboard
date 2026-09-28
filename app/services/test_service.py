"""Client for the single registered test service (spec §7, §10).

Security properties:

* The URL is built only from configuration plus the fixed route ``/health``;
  nothing from Telegram text ever reaches it.
* Redirects are not followed.
* The response is read up to a small byte limit and reduced to a short,
  allowlisted summary. Raw bodies, exception text and stack traces are never
  returned to the chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

from app.logs import get_logger, log_event

log = get_logger(__name__)

HEALTH_ROUTE = "/health"
CALL_TIMEOUT = httpx.Timeout(3.0)
# httpx timeouts are per operation; this caps the whole call (slow-drip bodies).
TOTAL_TIMEOUT_S = 3.5
MAX_RESPONSE_BYTES = 4096
_SAFE_VALUE_RE = re.compile(r"[A-Za-z0-9 _.-]{1,40}")


@dataclass(frozen=True)
class ServiceStatus:
    # ok | degraded | timeout | unreachable | http_error | invalid_response | not_configured
    state: str
    summary: str  # short text safe to show in Telegram

    @property
    def reachable(self) -> bool:
        return self.state in ("ok", "degraded")


class TestServiceClient:
    __test__ = False  # not a pytest test class, despite the name

    def __init__(self, base_url: str | None, token: str | None, http: httpx.AsyncClient) -> None:
        self._base_url = base_url
        self._token = token
        self._http = http

    @property
    def configured(self) -> bool:
        return self._base_url is not None

    async def check_health(self) -> ServiceStatus:
        """``GET <base>/health`` with a short timeout; never raises."""
        if self._base_url is None:
            return ServiceStatus("not_configured", "not configured")

        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        started = time.monotonic()
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_S):
                status = await self._fetch(self._base_url + HEALTH_ROUTE, headers)
        except TimeoutError:
            status = ServiceStatus("timeout", "timed out")
        except Exception as exc:  # noqa: BLE001 - a command reply must always be produced
            log_event(log, logging.ERROR, "service.call_error", service="test",
                      error_code=type(exc).__name__)
            status = ServiceStatus("unreachable", "unreachable")
        log_event(log, logging.INFO, "service.call", service="test", route=HEALTH_ROUTE,
                  result=status.state, elapsed_ms=round((time.monotonic() - started) * 1000, 1))
        return status

    async def _fetch(self, url: str, headers: dict[str, str]) -> ServiceStatus:
        try:
            async with self._http.stream(
                "GET", url, headers=headers, timeout=CALL_TIMEOUT, follow_redirects=False
            ) as response:
                if response.status_code != 200:
                    return ServiceStatus("http_error", f"HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        return ServiceStatus("invalid_response", "response too large")
        except httpx.TimeoutException:
            return ServiceStatus("timeout", "timed out")
        except httpx.HTTPError:
            return ServiceStatus("unreachable", "unreachable")
        return summarize_health(bytes(body))


def summarize_health(body: bytes) -> ServiceStatus:
    """Reduce a health JSON body to an allowlisted summary."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return ServiceStatus("invalid_response", "invalid response")
    if not isinstance(data, dict):
        return ServiceStatus("invalid_response", "invalid response")

    status = data.get("status")
    name = data.get("name")
    if not isinstance(status, str) or not _SAFE_VALUE_RE.fullmatch(status):
        return ServiceStatus("invalid_response", "invalid response")
    safe_name = name if isinstance(name, str) and _SAFE_VALUE_RE.fullmatch(name) else None
    label = f" ({safe_name})" if safe_name else ""
    if status == "ok":
        return ServiceStatus("ok", f"ok{label}")
    return ServiceStatus("degraded", f"degraded: {status}{label}")
