"""Minimal Telegram Bot API client with failure classification (spec §7, §8).

Only the four methods the MVP needs. Every call returns a
:class:`TelegramResult` instead of raising, so callers branch on
``result.outcome``:

* ``ok``           — success, ``result.data`` holds Telegram's ``result`` field
* ``transient``    — network error, timeout, 5xx, 409 conflict: retry later
* ``rate_limited`` — 429, ``result.retry_after`` seconds from Telegram
* ``permanent``    — 400/403 etc. for this request: do not retry
* ``auth_failed``  — 401/404: the bot token itself is wrong (global problem)

Exception text is never logged or returned: httpx errors contain the request
URL, which contains the bot token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

Outcome = Literal["ok", "transient", "rate_limited", "permanent", "auth_failed"]

SEND_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Substrings of Telegram error descriptions mapped to safe, stable codes.
_DESCRIPTION_CODES = (
    ("chat not found", "chat_not_found"),
    ("bot was blocked", "bot_blocked"),
    ("user is deactivated", "user_deactivated"),
    ("message is too long", "message_too_long"),
    ("message text is empty", "message_empty"),
    ("can't parse entities", "parse_error"),
)


@dataclass(frozen=True)
class TelegramResult:
    outcome: Outcome
    data: Any = None
    error_code: str | None = None
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


class TelegramClient:
    def __init__(self, token: str, http: httpx.AsyncClient,
                 api_base: str = "https://api.telegram.org") -> None:
        self._base = f"{api_base.rstrip('/')}/bot{token}"
        self._http = http

    async def _call(self, method: str, payload: dict[str, Any],
                    http_timeout: httpx.Timeout) -> TelegramResult:
        try:
            response = await self._http.post(
                f"{self._base}/{method}", json=payload, timeout=http_timeout
            )
        except httpx.TimeoutException:
            return TelegramResult("transient", error_code="network_timeout")
        except httpx.HTTPError:
            return TelegramResult("transient", error_code="network_error")
        return classify_response(response.status_code, _safe_json(response))

    async def get_me(self) -> TelegramResult:
        return await self._call("getMe", {}, SEND_TIMEOUT)

    async def delete_webhook(self) -> TelegramResult:
        # Polling and webhooks are mutually exclusive; keep pending updates.
        return await self._call("deleteWebhook", {"drop_pending_updates": False}, SEND_TIMEOUT)

    async def get_updates(self, offset: int, timeout_s: int) -> TelegramResult:
        # The HTTP read timeout must outlast Telegram's long-poll timeout.
        http_timeout = httpx.Timeout(timeout_s + 10.0, connect=5.0)
        return await self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout_s, "allowed_updates": ["message"]},
            http_timeout,
        )

    async def send_message(self, chat_id: int, text: str) -> TelegramResult:
        # No parse_mode: text is always plain, never HTML/Markdown.
        return await self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}},
            SEND_TIMEOUT,
        )


def _safe_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def classify_response(status: int, body: dict[str, Any] | None) -> TelegramResult:
    """Map an HTTP status + Telegram JSON body to a :class:`TelegramResult`."""
    if status == 200 and body is not None and body.get("ok") is True:
        return TelegramResult("ok", data=body.get("result"))

    if status == 429:
        retry_after = None
        params = (body or {}).get("parameters")
        if isinstance(params, dict) and isinstance(params.get("retry_after"), int | float):
            retry_after = float(params["retry_after"])
        return TelegramResult("rate_limited", error_code="telegram_429", retry_after=retry_after)
    if status >= 500 or status == 200:
        # A 200 without a valid ok-body is treated as a garbled transient response.
        return TelegramResult("transient", error_code=f"telegram_{status}"
                              if status >= 500 else "invalid_response")
    if status in (401, 404):
        return TelegramResult("auth_failed", error_code=f"telegram_{status}")
    if status == 409:
        # Another getUpdates consumer or an active webhook.
        return TelegramResult("transient", error_code="telegram_conflict")

    description = str((body or {}).get("description", "")).lower()
    for needle, code in _DESCRIPTION_CODES:
        if needle in description:
            return TelegramResult("permanent", error_code=code)
    return TelegramResult("permanent", error_code=f"telegram_{status}")
