"""Structured JSON logging with secret redaction (spec §4, §9).

Usage::

    log = get_logger(__name__)
    log_event(log, logging.INFO, "delivery.sent", message_id=mid, elapsed_ms=12)

Every record becomes one JSON object per line. A redaction filter removes
fields that could carry secrets or message bodies, and scrubs any configured
secret string (bot token, producer tokens) from the rendered text. The bot
token matters most: it is part of every Telegram API URL, so any exception
text or HTTP client log could otherwise contain it.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT_LOGGER = "switchboard"

# Field names that must never be written, whatever the caller passes.
_FORBIDDEN_FIELDS = frozenset(
    {"authorization", "token", "bot_token", "body", "message", "text", "password", "secret"}
)
_REDACTED = "[REDACTED]"


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``switchboard`` hierarchy."""
    if name == ROOT_LOGGER or name.startswith(ROOT_LOGGER + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Log a structured event. ``fields`` become top-level JSON keys."""
    logger.log(level, event, extra={"sb_fields": fields})


class RedactionFilter(logging.Filter):
    """Drops forbidden fields and scrubs known secret strings."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        # Longest first so a secret containing another is fully replaced.
        self._secrets = sorted({s for s in (secrets or []) if s}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        fields = getattr(record, "sb_fields", None)
        if isinstance(fields, dict):
            clean: dict[str, Any] = {}
            for key, value in fields.items():
                if key.lower() in _FORBIDDEN_FIELDS:
                    continue
                clean[key] = self.scrub(value) if isinstance(value, str) else value
            record.sb_fields = clean
        # Render msg % args now so secrets inside args are scrubbed too.
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed third-party log call
            rendered = str(record.msg)
        record.msg = self.scrub(rendered)
        record.args = None
        if record.exc_info:
            # Tracebacks may embed URLs (and thus the bot token); render and
            # scrub them here, then drop the raw exc_info.
            record.exc_text = self.scrub(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "sb_fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key not in payload:
                    payload[key] = value
        if record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(secrets: list[str], log_dir: Path | None, level: int = logging.INFO) -> None:
    """Install JSON handlers on the root logger (idempotent).

    Handlers sit on the root logger so uvicorn/httpx records also pass the
    redaction filter. httpx logs every request URL at INFO, and Telegram URLs
    contain the bot token, so httpx/httpcore are capped at WARNING regardless.
    """
    redaction = RedactionFilter(secrets)
    formatter = JsonFormatter()

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_switchboard", False):
            root.removeHandler(handler)
            handler.close()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_dir / "switchboard.log", maxBytes=10 * 1024 * 1024, backupCount=5,
                encoding="utf-8",
            )
        )
    for handler in handlers:
        handler.addFilter(redaction)
        handler.setFormatter(formatter)
        handler._switchboard = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level)

    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
